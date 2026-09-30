"""Owner pairing (release R2).

With `SLACK_OWNER_USER_ID` empty the bridge starts in *pairing mode*: it issues a random 6-digit
code, keeps it in `STATE_DIR/pairing.json` (so the setup wizard can show it too), prints it in the
bridge pane and shows it as a Herdr notification. The only Slack input accepted then is
`/<cmd> pair <code>` (from any user); the first correct code makes that user the owner.

Success is two-phase, so the owner is served only once the bridge really works for them:
1. `attempt()` with the right code saves `SLACK_OWNER_USER_ID` into `.env` (in place) and marks
   that user *pending* (the code is consumed; `pairing.json` says "activating").
2. `complete(activate)` starts the owner-mode bridge (DM, notifier, subscriptions), retrying a
   few times. On success the user becomes `owner` (the Slack owner guard opens) and
   `pairing.json` is deleted; on failure it says "failed" (the wizard reports it) and the pane /
   a notification ask for a bridge restart, which then starts normally with the saved owner.

Brute force: MAX_WRONG wrong attempts (all users together) rotate the code, and each Slack user
may make at most USER_MAX_WRONG wrong attempts per USER_WINDOW (then that user alone waits). No
one can lock the owner out: a user who has not guessed wrong is never throttled. Codes expire
after CODE_TTL, are compared in constant time and are never logged (only the pane / notification
show them). Re-pairing: clear SLACK_OWNER_USER_ID in `.env` and restart the bridge.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from .state import atomic_write_text

log = logging.getLogger(__name__)

PAIRING_FILE = "pairing.json"
CODE_DIGITS = 6
CODE_TTL = 15 * 60.0
MAX_WRONG = 5  # wrong attempts (all users) before the code rotates
USER_MAX_WRONG = 5  # wrong attempts one Slack user may make per USER_WINDOW
USER_WINDOW = 10 * 60.0
ACTIVATE_DELAYS = (1.0, 3.0, 10.0)  # retries of the owner-mode start after a correct code
_CODE_RE = re.compile(rf"\d{{{CODE_DIGITS}}}")

# pairing.json "state" values
STATE_WAITING = "waiting"
STATE_ACTIVATING = "activating"
STATE_FAILED = "failed"

# announce(code, reason): reason "start" / "expired" / "attempts" come with the new code;
# "failed" (owner saved, but the owner-mode start failed) comes with code "".
Announce = Callable[[str, str], None]


class PairStatus(Enum):
    PAIRED = "paired"  # code accepted, owner saved; activation follows (`complete`)
    WRONG = "wrong"
    ROTATED = "rotated"  # wrong, and that was the MAX_WRONG-th attempt: a new code was issued
    EXPIRED = "expired"  # the code had expired: a new code was issued
    THROTTLED = "throttled"  # this user guessed wrong too often; not checked, not counted
    NO_CODE = "no_code"  # no code issued yet (bridge still starting)
    USAGE = "usage"  # `pair` without a code
    ALREADY = "already"  # a code was already accepted: pairing is over
    ERROR = "error"  # correct code, but saving the owner failed


@dataclass(frozen=True)
class PairResult:
    status: PairStatus
    owner: str | None = None
    remaining: int = 0  # wrong attempts left for the current code
    retry_after: float = 0.0  # THROTTLED: seconds until this user may try again

    @property
    def ok(self) -> bool:
        return self.status is PairStatus.PAIRED


def generate_code(randbelow: Callable[[int], int] = secrets.randbelow) -> str:
    return f"{randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"


def pairing_path(state_dir: Path) -> Path:
    return Path(state_dir) / PAIRING_FILE


def read_pairing_record(state_dir: Path) -> dict | None:
    """The raw `pairing.json` record, or None."""
    try:
        data = json.loads(pairing_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_pairing(state_dir: Path) -> dict | None:
    """The current code record ({"code", "created", "attempts", "state"}) while a code is waiting."""
    data = read_pairing_record(state_dir)
    if not data or not _CODE_RE.fullmatch(str(data.get("code") or "")):
        return None
    return data


def _write_pairing(state_dir: Path, record: dict) -> None:
    path = pairing_path(state_dir)
    atomic_write_text(path, json.dumps(record))
    try:
        os.chmod(path, 0o600)  # best effort (Windows only knows read-only); the dir is per-user anyway
    except OSError:
        pass


def delete_pairing(state_dir: Path) -> None:
    try:
        pairing_path(state_dir).unlink()
    except OSError:
        pass


class Pairing:
    """The pairing state machine. `persist_owner(user)` must durably record the owner (it runs
    under the pairing lock before success is reported); `announce` shows a new code on the PC."""

    def __init__(self, state_dir: Path, persist_owner: Callable[[str], None], announce: Announce, *,
                 clock: Callable[[], float] = time.time, new_code: Callable[[], str] = generate_code,
                 sleep: Callable[[float], None] = time.sleep, ttl: float = CODE_TTL, max_wrong: int = MAX_WRONG,
                 user_max_wrong: int = USER_MAX_WRONG, user_window: float = USER_WINDOW,
                 activate_delays: tuple[float, ...] = ACTIVATE_DELAYS):
        self.state_dir = Path(state_dir)
        self.persist_owner = persist_owner
        self.announce = announce
        self.clock = clock
        self.new_code = new_code
        self.sleep = sleep
        self.ttl = ttl
        self.max_wrong = max_wrong
        self.user_max_wrong = user_max_wrong
        self.user_window = user_window
        self.activate_delays = activate_delays
        self.owner: str | None = None  # set once the owner-mode bridge runs (`complete`)
        self.pending: str | None = None  # code accepted and saved, activation not finished
        self.failed = False  # activation gave up; a bridge restart is needed
        self._code: str | None = None
        self._created = 0.0
        self._attempts = 0
        self._user_wrong: dict[str, deque] = defaultdict(deque)  # user -> times of wrong attempts
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        """Still waiting for a correct code."""
        return self.owner is None and self.pending is None

    def begin(self) -> None:
        """Issue the first code (a code left over from an earlier bridge run is replaced)."""
        with self._lock:
            if self.active:
                self._rotate("start")

    def tick(self) -> None:
        """Rotate an expired code (the bridge calls this about once a second)."""
        with self._lock:
            if self.active and self._code is not None and self._expired():
                self._rotate("expired")

    def attempt(self, user: str, text: str) -> PairResult:
        code = (text or "").strip()
        with self._lock:
            if not self.active:
                return PairResult(PairStatus.ALREADY, self.owner or self.pending)
            if not code:
                return PairResult(PairStatus.USAGE, remaining=self._remaining())
            if self._code is None:
                return PairResult(PairStatus.NO_CODE)
            wait = self._throttled_for(user)
            if wait > 0:
                return PairResult(PairStatus.THROTTLED, retry_after=wait)
            if self._expired():
                self._rotate("expired")
                return PairResult(PairStatus.EXPIRED, remaining=self._remaining())
            if hmac.compare_digest(code.encode("utf-8"), self._code.encode("utf-8")):
                try:
                    self.persist_owner(user)
                except Exception:
                    log.exception("saving the paired owner failed")
                    return PairResult(PairStatus.ERROR)
                self.pending = user
                self._code = None
                self._save_state(STATE_ACTIVATING)
                log.info("pairing code accepted from %s; starting owner mode", user)
                return PairResult(PairStatus.PAIRED, user)
            self._attempts += 1
            self._user_wrong[user].append(self.clock())
            log.warning("wrong pairing code from %s (%d/%d for this code)", user, self._attempts, self.max_wrong)
            if self._attempts >= self.max_wrong:
                self._rotate("attempts")
                return PairResult(PairStatus.ROTATED, remaining=self._remaining())
            self._save()
            return PairResult(PairStatus.WRONG, remaining=self._remaining())

    def complete(self, activate: Callable[[str], None]) -> bool:
        """Phase 2 after a correct code (runs off the Slack handler): start owner mode for the
        pending user, retrying with ACTIVATE_DELAYS. True once the owner is served."""
        user = self.pending
        if user is None:
            return self.owner is not None
        for attempt, delay in enumerate((0.0, *self.activate_delays)):
            if delay:
                self.sleep(delay)
            try:
                activate(user)
            except Exception:
                log.exception("starting owner mode for %s failed (attempt %d)", user, attempt + 1)
                continue
            with self._lock:
                self.owner, self.pending = user, None
                delete_pairing(self.state_dir)
            log.info("paired: %s is now the owner", user)
            return True
        with self._lock:
            self.failed = True
            self._save_state(STATE_FAILED)
        log.error("owner mode did not start; %s is saved as owner in .env, restart the bridge", user)
        self._announce("", "failed")
        return False

    # --- internals (under self._lock) ------------------------------------------------------
    def _expired(self) -> bool:
        return self.clock() - self._created >= self.ttl

    def _remaining(self) -> int:
        return max(0, self.max_wrong - self._attempts)

    def _throttled_for(self, user: str) -> float:
        """Seconds until `user` may try again (0: not throttled)."""
        times = self._user_wrong.get(user)
        if not times:
            return 0.0
        now = self.clock()
        while times and now - times[0] >= self.user_window:
            times.popleft()
        if len(times) < self.user_max_wrong:
            return 0.0
        return self.user_window - (now - times[0])

    def _rotate(self, reason: str) -> None:
        self._code = self.new_code()
        self._created = self.clock()
        self._attempts = 0
        self._save()
        log.info("pairing code issued (%s)", reason)  # the code itself only goes to the pane
        self._announce(self._code, reason)

    def _save(self) -> None:
        self._write({"state": STATE_WAITING, "code": self._code, "created": self._created,
                     "attempts": self._attempts})

    def _save_state(self, state: str) -> None:
        self._write({"state": state, "at": self.clock()})

    def _write(self, record: dict) -> None:
        try:
            _write_pairing(self.state_dir, record)
        except OSError:
            log.exception("writing %s failed", PAIRING_FILE)

    def _announce(self, code: str, reason: str) -> None:
        try:
            self.announce(code, reason)
        except Exception:
            log.exception("announcing the pairing code failed")


def result_text(result: PairResult, slash_command: str) -> str:
    """The ephemeral Slack reply to `/<cmd> pair <code>`."""
    s = result.status
    usage = f"`{slash_command} pair <code>`"
    if s is PairStatus.PAIRED:
        return ("paired ✅ Connecting this Herdr bridge to you now; a welcome message arrives in the bot DM "
                f"in a moment. Then try `{slash_command} list`.")
    if s is PairStatus.WRONG:
        return f"❌ Wrong pairing code ({result.remaining} attempts left for this code). Check the code on your PC."
    if s is PairStatus.ROTATED:
        return "❌ Wrong pairing code. Too many attempts: a new code is now shown on your PC."
    if s is PairStatus.EXPIRED:
        return f"⌛ That code expired. A new code is now shown on your PC; run {usage} again."
    if s is PairStatus.THROTTLED:
        minutes = max(1, int(-(-result.retry_after // 60)))
        return f"⏳ Too many wrong codes from you. Try again in {minutes} min."
    if s is PairStatus.NO_CODE:
        return "⏳ The bridge is still starting; try again in a few seconds."
    if s is PairStatus.USAGE:
        return f"Usage: {usage} with the 6-digit code shown on your PC (herdr-slack pane)."
    if s is PairStatus.ALREADY:
        return "⛔ This Herdr bridge only accepts requests from its owner."
    return "❌ The code was right, but saving the owner failed on the PC (see bridge.log). Try again."


def not_paired_text(slash_command: str) -> str:
    return (f"🔗 This Herdr bridge is not paired yet (waiting for pairing, code in the herdr-slack pane). "
            f"Run `{slash_command} pair <code>` with the code shown on your PC.")


def activating_text() -> str:
    return "⏳ Pairing accepted; the bridge is still starting for you. Try again in a few seconds."


def activation_failed_text() -> str:
    return ("❌ You are paired, but the bridge could not start on the PC (see the herdr-slack pane). "
            "Restart it: herdr plugin action invoke restart --plugin herdr-slackbot")


def code_banner(code: str, slash_command: str, reason: str) -> str:
    """What the bridge prints in its pane for a new code (or a failed activation)."""
    if reason == "failed":
        return ("\n" + "=" * 64 + "\n  Slack pairing was accepted and saved, but the bridge could not start\n"
                "  in owner mode (see bridge.log). Restart the bridge:\n"
                "    herdr plugin action invoke restart --plugin herdr-slackbot\n" + "=" * 64 + "\n")
    why = {"expired": "the previous code expired", "attempts": "too many wrong attempts"}.get(reason)
    lines = [
        "=" * 64,
        f"  Slack pairing code:  {code}" + (f"   (new: {why})" if why else ""),
        f"  In the bot's Slack DM, run:  {slash_command} pair {code}",
        f"  The code expires in {int(CODE_TTL // 60)} minutes.",
        "=" * 64,
    ]
    return "\n" + "\n".join(lines) + "\n"
