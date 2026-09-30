"""Outgoing Slack operations behind an interface.

The bridge only talks to Slack through `SlackTransport`. The default
implementation wraps `slack_sdk.WebClient` (used with Socket Mode). A future
shared-app relay hub can provide another implementation without touching the
bridge.

Failures are raised as `SlackTransientError` (worth retrying: network trouble,
rate limits, Slack-side errors) or `SlackPermanentError` (retrying cannot help:
bad auth, missing channel, invalid blocks ...). A post whose outcome is unknown (the
connection broke after the request was sent, or Slack answered 5xx) raises
`SlackUncertainError`: Slack may have accepted it, so it must be reconciled with
`find_message()` before it is re-posted. Posts carry their `op` marker twice: in message
metadata and as the `block_id` of a block (fallback in case metadata is not retained).
`find_message()` pages through the whole range and raises `SlackLookupIncomplete` when it
cannot prove absence (truncated scan, or bot messages visible without any marker): an
unknown outcome is never treated as "not posted".

slack_sdk's own retry handlers are disabled on the wrapped client: an SDK-level retry of an
accepted chat.postMessage would duplicate it before this layer could reconcile.
"""

from __future__ import annotations

import http.client
import logging
import socket
import urllib.error
from typing import Protocol

log = logging.getLogger(__name__)

PERMANENT_ERRORS = frozenset({
    "invalid_auth", "not_authed", "account_inactive", "token_revoked", "token_expired", "no_permission",
    "missing_scope", "channel_not_found", "is_archived", "not_in_channel", "user_not_found",
    "invalid_blocks", "invalid_blocks_format", "msg_too_long", "no_text", "invalid_arguments",
    "message_not_found", "cant_update_message", "edit_window_closed", "expired_trigger_id",
    "invalid_trigger_id", "not_found", "view_too_large", "hash_conflict",
})


UNCERTAIN_API_ERRORS = frozenset({"internal_error", "fatal_error", "service_unavailable"})
POST_MARKER_EVENT = "herdr_slackbot_post"  # message metadata event_type carrying {"op": ...}
BLOCK_MARKER_PREFIX = "hsm:"  # block_id fallback marker: "hsm:<op>"
MAX_LOOKUP_PAGES = 10


class SlackTransportError(Exception):
    def __init__(self, error: str, retry_after: float | None = None):
        super().__init__(error)
        self.error = error
        self.retry_after = retry_after


class SlackTransientError(SlackTransportError):
    """Retry later (after `retry_after` seconds when Slack said so)."""


class SlackPermanentError(SlackTransportError):
    """Retrying the same request cannot succeed."""


class SlackUncertainError(SlackTransientError):
    """A post may or may not have been accepted: reconcile (find_message) before re-posting."""


class SlackLookupIncomplete(SlackTransientError):
    """A marker lookup could not prove absence: keep the outcome unknown and retry later."""


def message_markers(message: dict) -> set[str]:
    """All op markers carried by a message (metadata and block_id fallback)."""
    ops = set()
    meta = message.get("metadata") or {}
    if meta.get("event_type") == POST_MARKER_EVENT:
        op = (meta.get("event_payload") or {}).get("op")
        if op:
            ops.add(op)
    for block in message.get("blocks") or []:
        block_id = block.get("block_id") if isinstance(block, dict) else None
        if isinstance(block_id, str) and block_id.startswith(BLOCK_MARKER_PREFIX):
            ops.add(block_id[len(BLOCK_MARKER_PREFIX):])
    return ops


def with_block_marker(blocks: list | None, op: str) -> list | None:
    """Copy of `blocks` with the op marker as block_id of the first block that has none."""
    if not blocks:
        return blocks
    marked = [dict(b) for b in blocks]
    for block in marked:
        if not block.get("block_id"):
            block["block_id"] = (BLOCK_MARKER_PREFIX + op)[:255]
            break
    return marked


class SlackTransport(Protocol):
    def open_dm(self, user_id: str) -> str:
        """Channel id of the bot's DM with `user_id`."""

    def post_message(self, channel: str, text: str, blocks: list | None = None,
                     thread_ts: str | None = None, op: str | None = None) -> str:
        """Post a message; returns its ts. `op` is stored as a message-metadata marker."""

    def find_message(self, channel: str, op: str, thread_ts: str | None = None,
                     oldest: float | None = None) -> str | None:
        """ts of the bot message carrying marker `op` (in `thread_ts`, or top level), or None."""

    def update_message(self, channel: str, ts: str, text: str, blocks: list | None = None) -> None: ...

    def post_ephemeral(self, channel: str, user: str, text: str, blocks: list | None = None,
                       thread_ts: str | None = None) -> None: ...

    def respond(self, response_url: str, text: str, blocks: list | None = None) -> None:
        """Ephemeral reply to a slash command after it was acknowledged."""

    def upload_text_file(self, channel: str, thread_ts: str | None, filename: str, content: str,
                         title: str) -> None: ...

    def open_view(self, trigger_id: str, view: dict) -> str:
        """Open a modal; returns the view id."""

    def update_view(self, view_id: str, view: dict, view_hash: str | None = None) -> str | None:
        """Update a modal; returns the view's new hash (None if unknown)."""

    def publish_view(self, user_id: str, view: dict) -> None:
        """Publish the App Home tab view for `user_id` (views.publish)."""


def _definitely_unsent(exc: BaseException) -> bool:
    """Network errors raised before the request could reach Slack."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) and not isinstance(
        exc, urllib.error.HTTPError) else exc
    return isinstance(reason, (ConnectionRefusedError, socket.gaierror))


def classify_slack_exception(exc: BaseException, mutating: bool = False) -> SlackTransportError:
    """Map a slack_sdk / network exception to a transient, uncertain or permanent error.

    `mutating`: the call creates something (chat.postMessage), so an unknown outcome is
    reported as `SlackUncertainError` instead of a plain transient error."""
    from slack_sdk.errors import SlackApiError

    if isinstance(exc, SlackApiError):
        response = exc.response
        error = str((response.get("error") if response is not None else None) or "unknown_error")
        status = getattr(response, "status_code", None)
        if mutating and ((isinstance(status, int) and status >= 500) or error in UNCERTAIN_API_ERRORS):
            return SlackUncertainError(error)
        if error == "ratelimited" or getattr(response, "status_code", None) == 429:
            headers = getattr(response, "headers", None) or {}
            retry_after = headers.get("Retry-After") or headers.get("retry-after")
            try:
                wait = float(retry_after) if retry_after is not None else None
            except (TypeError, ValueError):
                wait = None
            return SlackTransientError("ratelimited", wait)
        if error in PERMANENT_ERRORS:
            return SlackPermanentError(error)
        return SlackTransientError(error)
    if mutating and not _definitely_unsent(exc):
        return SlackUncertainError(type(exc).__name__)
    return SlackTransientError(type(exc).__name__)


class WebClientTransport:
    """`SlackTransport` over a `slack_sdk.WebClient`."""

    def __init__(self, client, webhook_factory=None):
        self.client = client
        self._webhook_factory = webhook_factory
        if client is not None and hasattr(client, "retry_handlers"):
            client.retry_handlers = []  # only this layer decides whether a request is repeated

    def _call(self, method: str, mutating: bool = False, **kwargs):
        from slack_sdk.errors import SlackClientError

        try:
            return getattr(self.client, method)(**kwargs)
        except (SlackClientError, OSError, http.client.HTTPException) as exc:
            raise classify_slack_exception(exc, mutating) from exc

    def open_dm(self, user_id: str) -> str:
        return self._call("conversations_open", users=user_id)["channel"]["id"]

    def post_message(self, channel: str, text: str, blocks: list | None = None,
                     thread_ts: str | None = None, op: str | None = None) -> str:
        kwargs = {"channel": channel, "text": text, "unfurl_links": False, "unfurl_media": False}
        if blocks:
            kwargs["blocks"] = with_block_marker(blocks, op) if op else blocks
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        if op:
            kwargs["metadata"] = {"event_type": POST_MARKER_EVENT, "event_payload": {"op": op}}
        return self._call("chat_postMessage", mutating=True, **kwargs)["ts"]

    def find_message(self, channel: str, op: str, thread_ts: str | None = None,
                     oldest: float | None = None) -> str | None:
        """ts if found; None only after a complete scan that could have shown the marker.
        Raises SlackLookupIncomplete (unknown) otherwise, and transport errors as usual."""
        kwargs = {"channel": channel, "include_all_metadata": True, "limit": 200}
        if oldest:
            kwargs["oldest"] = f"{oldest:.6f}"
        saw_marker = saw_bot = False
        cursor = None
        for _ in range(MAX_LOOKUP_PAGES):
            page = dict(kwargs, cursor=cursor) if cursor else kwargs
            if thread_ts:
                resp = self._call("conversations_replies", ts=thread_ts, **page)
            else:
                resp = self._call("conversations_history", **page)
            for message in resp.get("messages") or []:
                ops = message_markers(message)
                if op in ops:
                    return message.get("ts")
                saw_marker = saw_marker or bool(ops)
                saw_bot = saw_bot or bool(message.get("bot_id") or message.get("app_id"))
            next_cursor = (resp.get("response_metadata") or {}).get("next_cursor") or None
            if next_cursor:
                if next_cursor == cursor:
                    raise SlackLookupIncomplete("pagination_inconsistent")  # would loop on the same page
                cursor = next_cursor  # a continuation cursor alone means "more pages" (has_more or not)
                continue
            if resp.get("has_more"):
                # More results announced but no way to fetch them: the scan can't be complete.
                raise SlackLookupIncomplete("pagination_inconsistent")
            break
        else:
            raise SlackLookupIncomplete("lookup_truncated")
        if saw_bot and not saw_marker:
            # Bot messages are visible but none carries a marker: markers are not retained,
            # so "not found" proves nothing.
            raise SlackLookupIncomplete("markers_not_visible")
        return None

    def recent_messages(self, channel: str, thread_ts: str | None = None,
                        oldest: float | None = None) -> list[dict]:
        """One page of messages (with metadata), for diagnostics (`marker-check`)."""
        kwargs = {"channel": channel, "include_all_metadata": True, "limit": 200}
        if oldest:
            kwargs["oldest"] = f"{oldest:.6f}"
        if thread_ts:
            return self._call("conversations_replies", ts=thread_ts, **kwargs).get("messages") or []
        return self._call("conversations_history", **kwargs).get("messages") or []

    def update_message(self, channel: str, ts: str, text: str, blocks: list | None = None) -> None:
        kwargs = {"channel": channel, "ts": ts, "text": text}
        if blocks is not None:
            kwargs["blocks"] = blocks
        self._call("chat_update", **kwargs)

    def post_ephemeral(self, channel: str, user: str, text: str, blocks: list | None = None,
                       thread_ts: str | None = None) -> None:
        kwargs = {"channel": channel, "user": user, "text": text}
        if blocks:
            kwargs["blocks"] = blocks
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        self._call("chat_postEphemeral", **kwargs)

    def respond(self, response_url: str, text: str, blocks: list | None = None) -> None:
        if self._webhook_factory is not None:
            webhook = self._webhook_factory(response_url)
        else:
            from slack_sdk.webhook import WebhookClient
            webhook = WebhookClient(response_url)
        try:
            resp = webhook.send(text=text, blocks=blocks, response_type="ephemeral", replace_original=False)
        except (OSError, http.client.HTTPException) as exc:
            if _definitely_unsent(exc):
                raise SlackTransientError(type(exc).__name__) from exc
            raise SlackUncertainError(type(exc).__name__) from exc  # the reply may have been shown
        status = getattr(resp, "status_code", 200)
        if status == 429:
            raise SlackTransientError(f"http_{status}")
        if status >= 500:
            raise SlackUncertainError(f"http_{status}")
        if status >= 400:
            raise SlackPermanentError(f"http_{status}: {getattr(resp, 'body', '')}")

    def upload_text_file(self, channel: str, thread_ts: str | None, filename: str, content: str,
                         title: str) -> None:
        kwargs = {"channel": channel, "filename": filename, "content": content, "title": title}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        self._call("files_upload_v2", **kwargs)

    def open_view(self, trigger_id: str, view: dict) -> str:
        return self._call("views_open", trigger_id=trigger_id, view=view)["view"]["id"]

    def update_view(self, view_id: str, view: dict, view_hash: str | None = None) -> str | None:
        kwargs = {"view_id": view_id, "view": view}
        if view_hash:
            kwargs["hash"] = view_hash
        resp = self._call("views_update", **kwargs)
        try:
            return (resp.get("view") or {}).get("hash")
        except AttributeError:
            return None

    def publish_view(self, user_id: str, view: dict) -> None:
        self._call("views_publish", user_id=user_id, view=view)
