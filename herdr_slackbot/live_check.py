"""`python -m herdr_slackbot marker-check`: verify the post-marker round trip on real Slack.

Posts a root message and a threaded reply in the owner's DM, each with an `op` marker
(message metadata + block_id fallback), reads them back with `include_all_metadata`, and
reports which marker representation Slack retained and whether `find_message()` finds both.
Idempotent Slack posts (docs/review/M2-recheck2.md R9b) depend on this; see docs/LIVE_TEST.md.
"""

from __future__ import annotations

import time
import uuid
from typing import Callable

from . import blocks as B
from .slack_transport import (
    BLOCK_MARKER_PREFIX,
    POST_MARKER_EVENT,
    SlackLookupIncomplete,
    SlackTransportError,
)


def _inspect(message: dict | None, op: str) -> tuple[bool, bool]:
    if not message:
        return False, False
    meta = message.get("metadata") or {}
    meta_ok = meta.get("event_type") == POST_MARKER_EVENT and (meta.get("event_payload") or {}).get("op") == op
    block_ok = any(isinstance(b, dict) and b.get("block_id") == BLOCK_MARKER_PREFIX + op
                   for b in message.get("blocks") or [])
    return meta_ok, block_ok


def marker_check(transport, owner_user_id: str, out: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep) -> int:
    """0 = PASS, 1 = FAIL/UNKNOWN (markers not reconcilable), 2 = ERROR (a Slack call failed)."""
    tag = uuid.uuid4().hex[:8]
    step = "open DM (conversations.open)"
    try:
        channel = transport.open_dm(owner_user_id)
        text = f"🔧 herdr-slackbot marker check {tag} (safe to delete)"
        ops = {"root": f"check:root:{tag}", "reply": f"check:reply:{tag}"}
        step = "post root (chat.postMessage)"
        root_ts = transport.post_message(channel, text, B.notice_blocks(text), None, op=ops["root"])
        step = "post reply (chat.postMessage)"
        reply_ts = transport.post_message(channel, "reply " + tag, B.notice_blocks("reply " + tag), root_ts,
                                          op=ops["reply"])
    except SlackTransportError as exc:
        out(f"ERROR at {step}: {exc.error}")
        out("ERROR: the check could not run (see the step above; e.g. missing_scope / invalid_auth)")
        return 2
    sleep(2.0)
    oldest = float(root_ts) - 1
    ok, errored = True, False
    for label, ts, thread in (("root", root_ts, None), ("reply", reply_ts, root_ts)):
        op = ops[label]
        method = "conversations.replies" if thread else "conversations.history"
        try:
            messages = transport.recent_messages(channel, thread, oldest)
            meta_ok, block_ok = _inspect(next((m for m in messages if m.get("ts") == ts), None), op)
            shown = f"metadata={'yes' if meta_ok else 'no'}  block_id={'yes' if block_ok else 'no'}"
        except SlackTransportError as exc:
            shown = f"read ERROR ({method}: {exc.error})"
            errored = True
        try:
            found = transport.find_message(channel, op, thread, oldest)
            lookup = "found" if found == ts else f"NOT FOUND ({found!r})"
        except SlackLookupIncomplete as exc:
            found, lookup = None, f"UNKNOWN ({exc.error})"
        except SlackTransportError as exc:
            found, lookup = None, f"ERROR ({method}: {exc.error})"
            errored = True
        out(f"{label:5s}: {shown}  find_message={lookup}")
        ok = ok and found == ts
    if ok:
        out("PASS: accepted posts can be reconciled")
        return 0
    if errored:
        out("ERROR: reading the DM failed (see above; e.g. missing im:history scope); markers were not judged")
        return 2
    out("FAIL: markers could not be reconciled; see docs/LIVE_TEST.md §3")
    return 1
