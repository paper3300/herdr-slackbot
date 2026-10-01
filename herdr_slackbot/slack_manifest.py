"""Slack app manifest generation (D13: one Slack app per user/PC).

Bot scopes are derived from the Slack Web API methods the bridge actually calls
(`slack_transport.WebClientTransport`) plus the app features it uses (slash command,
`message.im` events). `tests/test_slack_manifest.py` scans the transport source so a
new API call without a scope entry here fails the tests.
"""

from __future__ import annotations

import json

from .config import Config

# Web API method (slack_sdk WebClient attribute name) -> bot scopes it needs.
METHOD_SCOPES: dict[str, frozenset[str]] = {
    "conversations_open": frozenset({"im:write"}),       # open the owner's DM
    "chat_postMessage": frozenset({"chat:write"}),
    "chat_update": frozenset({"chat:write"}),
    "chat_postEphemeral": frozenset({"chat:write"}),
    "files_upload_v2": frozenset({"files:write"}),      # [View full] full-text upload
    "conversations_history": frozenset({"im:history"}),  # reconcile a post with an unknown outcome
    "conversations_replies": frozenset({"im:history"}),
    "views_open": frozenset(),                          # modals: trigger id only
    "views_update": frozenset(),
    "views_publish": frozenset(),                       # App Home tab (no scope required)
}

# App features -> scopes.
FEATURE_SCOPES: dict[str, frozenset[str]] = {
    "slash_command": frozenset({"commands"}),
    "message.im": frozenset({"im:history"}),            # thread replies in the bot DM
    "app_home_opened": frozenset(),                     # App Home tab refresh (no scope required)
}

BOT_EVENTS = ("message.im", "app_home_opened")

# Slack manifest limits.
APP_NAME_MAX = 35
BOT_NAME_MAX = 80
COMMAND_DESC_MAX = 100

USAGE_HINT = "list | new | send <agent> <text> | status"


def required_scopes() -> list[str]:
    scopes: set[str] = set()
    for group in (*METHOD_SCOPES.values(), *FEATURE_SCOPES.values()):
        scopes |= group
    return sorted(scopes)


def build_manifest(slash_command: str, display_name: str, username: str) -> dict:
    """The Slack app manifest (JSON form) for this user's bridge app."""
    name = display_name.strip() or f"Herdr ({username})"
    return {
        "_metadata": {"major_version": 1, "minor_version": 1},
        "display_information": {
            "name": name[:APP_NAME_MAX],
            "description": f"Herdr agents on {username}'s PC — notifications and prompts via DM",
            "background_color": "#282a36",
        },
        "features": {
            "app_home": {
                "home_tab_enabled": True,  # App Home: status, buttons, agent list
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,  # DM thread replies go to agents
            },
            "bot_user": {"display_name": name[:BOT_NAME_MAX], "always_online": True},
            "slash_commands": [{
                "command": slash_command,
                "description": f"Control Herdr agents on {username}'s PC"[:COMMAND_DESC_MAX],
                "usage_hint": USAGE_HINT,
                "should_escape": False,
            }],
        },
        "oauth_config": {"scopes": {"bot": required_scopes()}},
        "settings": {
            "event_subscriptions": {"bot_events": list(BOT_EVENTS)},
            "interactivity": {"is_enabled": True},
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


def manifest_for_config(cfg: Config) -> dict:
    return build_manifest(cfg.slash_command, cfg.bot_display_name, cfg.username)


def manifest_json(manifest: dict) -> str:
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
