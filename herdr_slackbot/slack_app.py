"""slack_bolt wiring (Socket Mode). All logic lives in `Bridge`; this module only
translates Slack payloads and enforces the owner guard on every entry point."""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable, Mapping

from slack_bolt import App, BoltResponse

from . import blocks as B
from .bridge import Bridge
from .commands import parse_command
from .config import Config
from .pairing import Pairing, activating_text, activation_failed_text, not_paired_text
from .pairing import result_text as pairing_result_text

log = logging.getLogger(__name__)

NOT_OWNER_TEXT = "⛔ This Herdr bridge only accepts requests from its owner."


def request_user(body: Mapping) -> str | None:
    """The Slack user behind any request type (command, action, view, options, event)."""
    if body.get("user_id"):  # slash command
        return body["user_id"]
    user = body.get("user")
    if isinstance(user, Mapping) and user.get("id"):  # block_actions, view_*, block_suggestion
        return user["id"]
    if isinstance(user, str):
        return user
    event = body.get("event")
    if isinstance(event, Mapping):
        if event.get("bot_id"):
            return None
        return event.get("user")
    return None


def _in_background(fn: Callable[[], None]) -> None:
    def run():
        try:
            fn()
        except Exception:
            log.exception("background Slack work failed")
    threading.Thread(target=run, name="slack-pairing", daemon=True).start()


def not_paired_home_view(cfg: Config, text: str | None = None) -> dict:
    return {"type": "home", "blocks": [
        {"type": "header", "text": B.plain(cfg.bot_display_name[:150])},
        *B.notice_blocks(text or not_paired_text(cfg.slash_command)),
    ]}


def not_paired_modal(cfg: Config, text: str | None = None) -> dict:
    return {"type": "modal", "title": B.plain("Herdr"), "close": B.plain("OK"),
            "blocks": B.notice_blocks(text or not_paired_text(cfg.slash_command))}


def build_app(cfg: Config, bridge: Bridge, *, pairing: Pairing | None = None,
              on_paired: Callable[[str], None] | None = None, transport=None,
              background: Callable[[Callable[[], None]], None] = _in_background, **app_kwargs) -> App:
    """Create the bolt App. `app_kwargs` override App() arguments (tests pass `authorize`).

    With `pairing` and no configured owner, the app runs in pairing mode (see pairing.py): only
    `/<cmd> pair <code>` is accepted; everything else gets a "not paired yet" notice (through
    `transport` where Slack needs a separate call). After a successful pairing `on_paired(user)`
    runs in the background (normally `pairing.complete(...)`); the owner guard serves that user only
    once `pairing.owner` is set, i.e. after owner mode really started. Until then the paired user
    gets a "still starting" (or "could not start") notice and everyone else the owner rejection."""
    kwargs: dict[str, Any] = {"request_verification_enabled": False}  # Socket Mode: no HTTP signatures
    if "authorize" not in app_kwargs and "client" not in app_kwargs:
        kwargs["token"] = cfg.slack_bot_token
    kwargs.update(app_kwargs)
    app = App(**kwargs)

    def current_owner() -> str:
        return cfg.slack_owner_user_id or (pairing.owner if pairing is not None else None) or ""

    def notify(fn: Callable[[], None]) -> None:
        if transport is not None:
            background(fn)

    def pairing_gate(body, ack, user, text: str, allow_pair: bool = True):
        if body.get("command"):
            sub, rest = parse_command(body.get("text") or "")
            if allow_pair and sub == "pair" and user:
                result = pairing.attempt(user, rest)
                resp = ack(text=pairing_result_text(result, cfg.slash_command), response_type="ephemeral")
                if result.ok and on_paired is not None:
                    background(lambda: on_paired(user))
                return resp
            return ack(text=text, response_type="ephemeral")
        kind = body.get("type")
        if kind == "block_suggestion":
            return ack(options=[])
        if kind == "view_submission":
            return ack(response_action="update", view=not_paired_modal(cfg, text))
        if kind == "event_callback" or "event" in body:
            event = body.get("event") or {}
            if user and event.get("type") == "app_home_opened" and event.get("tab", "home") == "home":
                notify(lambda: transport.publish_view(user, not_paired_home_view(cfg, text)))
            elif (user and event.get("type") == "message" and event.get("channel_type") == "im"
                  and not event.get("subtype") and event.get("channel")):
                notify(lambda: transport.post_ephemeral(event["channel"], user, text))
            return BoltResponse(status=200, body="")
        resp = ack()
        if kind == "block_actions" and user:
            channel = (body.get("channel") or {}).get("id")
            if body.get("response_url"):
                notify(lambda: transport.respond(body["response_url"], text))
            elif (body.get("container") or {}).get("type") == "view" and (body.get("view") or {}).get("type") == "home":
                notify(lambda: transport.publish_view(user, not_paired_home_view(cfg, text)))
            elif channel:
                notify(lambda: transport.post_ephemeral(channel, user, text))
        return resp

    @app.middleware
    def owner_guard(body, next, ack):  # noqa: A002 - bolt's argument name
        user = request_user(body)
        owner = current_owner()
        if owner and user == owner:
            return next()
        if not owner and pairing is not None:
            if pairing.pending is None:
                return pairing_gate(body, ack, user, not_paired_text(cfg.slash_command))
            if user == pairing.pending:  # paired, owner mode not started (yet)
                text = activation_failed_text() if pairing.failed else activating_text()
                return pairing_gate(body, ack, user, text, allow_pair=False)
        if user:
            log.warning("rejected %s request from non-owner %s", body.get("type") or "command", user)
        if body.get("command"):
            return ack(text=NOT_OWNER_TEXT, response_type="ephemeral")
        if body.get("type") == "block_suggestion":
            return ack(options=[])
        if body.get("type") == "event_callback" or "event" in body:
            return BoltResponse(status=200, body="")
        return ack()

    @app.command(cfg.slash_command)
    def on_command(ack, body):
        # Ack before any Herdr call (Slack's 3s window); the answer goes to response_url
        # and modals open with the trigger id right away (see Bridge.run_command).
        ack()
        try:
            bridge.run_command(body.get("user_id"), body.get("channel_id"), body.get("text") or "",
                               body.get("trigger_id"), body.get("response_url"))
        except Exception:
            log.exception("slash command failed")

    @app.view(B.NEW_CALLBACK)
    def on_new_submit(ack, body):
        errors = bridge.submit_new_view(body["view"])
        if errors:
            ack(response_action="errors", errors=errors)
        else:
            ack()

    @app.view(B.SEND_CALLBACK)
    def on_send_submit(ack, body):
        errors = bridge.submit_send_view(body["view"])
        if errors:
            ack(response_action="errors", errors=errors)
        else:
            ack()

    @app.action(B.ACTION_NEW_WS)
    @app.action(B.ACTION_NEW_KIND)
    def on_new_modal_change(ack, body, action):
        ack()
        bridge.update_new_modal(action["action_id"], body["view"])

    @app.action(B.ACTION_SEND_TARGET)
    def on_send_target_change(ack, body, action):
        ack()  # ack first; the preview is fetched afterwards
        try:
            bridge.update_send_modal(body["view"], action.get("action_ts"))
        except Exception:
            log.exception("send modal update failed")

    @app.action(B.ACTION_MUTE)
    def on_mute(ack, body, action):
        ack()
        bridge.toggle_mute(body["channel"]["id"], body["message"], action.get("value") or "")

    @app.action(B.ACTION_SHOW_FULL)
    def on_show_full(ack, body, action):
        ack()
        bridge.show_full(body["channel"]["id"], body["message"], action.get("value") or "")

    @app.action(re.compile("^" + re.escape(B.ACTION_DIALOG_PREFIX)))
    def on_dialog_action(ack, body, action):
        ack()  # ack first; the free-text modal then opens with the trigger id before any Herdr I/O
        try:
            bridge.dialog_action(action["action_id"], action.get("value") or "", body.get("trigger_id"),
                                 (body.get("channel") or {}).get("id"), body.get("message") or {})
        except Exception:
            log.exception("dialog action failed")

    @app.view(B.DIALOG_TEXT_CALLBACK)
    def on_dialog_text_submit(ack, body):
        errors = bridge.submit_dialog_text(body["view"])
        if errors:
            ack(response_action="errors", errors=errors)
        else:
            ack()

    @app.event("app_home_opened")
    def on_home_opened(event):
        # The owner guard already dropped other users' events: they see no agent information.
        if event.get("tab", "home") == "home" and event.get("user"):
            bridge.handle_home_opened(event["user"])

    for home_action_id in B.HOME_ACTIONS:
        @app.action(home_action_id)
        def on_home_action(ack, body, action):
            ack()  # ack first; modals then open with the trigger id before any Herdr I/O
            try:
                bridge.home_action(action["action_id"], body.get("trigger_id"), action.get("value"))
            except Exception:
                log.exception("Home tab action failed")

    @app.event("message")
    def on_message(event):
        if event.get("subtype") or event.get("bot_id") or event.get("channel_type") != "im":
            return
        bridge.handle_dm_message(event["channel"], event.get("user"), event.get("text") or "",
                                 event.get("ts"), event.get("thread_ts"))

    return app


def run_socket_mode(app: App, app_token: str):
    """Connect via Socket Mode without blocking; returns the handler (call .close() to stop)."""
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    handler = SocketModeHandler(app, app_token)
    handler.connect()
    return handler
