"""Read-only checks against the running Herdr server. Opt-in: HERDR_LIVE_TESTS=1."""

import os
import threading
import time

import pytest

from herdr_slackbot.events import LIFECYCLE_SUBSCRIPTIONS, SubscriptionManager
from herdr_slackbot.herdr_client import CliTransport, HerdrClient, HerdrError

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("HERDR_LIVE_TESTS") != "1" or not os.environ.get("HERDR_SOCKET_PATH"),
                       reason="set HERDR_LIVE_TESTS=1 inside a Herdr pane"),
]


@pytest.fixture(scope="module")
def client():
    return HerdrClient.from_env()


def test_ping_and_lists(client):
    pong = client.ping()
    assert pong["type"] == "pong" and pong["protocol"] >= 20
    agents = client.list_agents()
    for a in agents:
        assert {"pane_id", "workspace_id", "agent_status"} <= set(a)
    assert isinstance(client.list_workspaces(), list)


def test_errors_are_typed(client):
    with pytest.raises(HerdrError) as exc:
        client.get_agent("no-such-agent-xyz")
    assert exc.value.code == "agent_not_found"
    assert client.find_agent("no-such-agent-xyz") is None


def test_cli_matches_pipe(client):
    cli = HerdrClient(CliTransport())
    assert {a["pane_id"] for a in cli.list_agents()} == {a["pane_id"] for a in client.list_agents()}


def test_subscribe_ack_and_cancel(client):
    stream = client.subscribe(LIFECYCLE_SUBSCRIPTIONS)
    assert stream.ack == {"type": "subscription_started"}
    events, ended = [], threading.Event()

    def run():
        for env in stream:
            events.append(env)
        ended.set()

    threading.Thread(target=run, daemon=True).start()
    time.sleep(0.3)  # replayed history arrives ~0.1s apart
    stream.close()
    assert ended.wait(3)


def test_manager_seeds_all_agent_panes(client):
    mgr = SubscriptionManager(client, lambda t: None, resync_interval=3600)
    mgr.start()
    try:
        live = {a["pane_id"] for a in client.list_agents()}
        assert live <= mgr.subscribed_panes()
    finally:
        mgr.stop()
