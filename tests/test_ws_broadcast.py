"""The state broadcast path: fan-out to WebSocket clients, and the on-disk
draw trace.

Two properties are worth pinning here, both of them about what happens when
something stops keeping up:

  * one client that has stopped draining its socket must not stop the others.
    Before per-client queues the fan-out awaited each socket in turn, so a
    backgrounded phone tab froze the overlay for every other viewer.
  * the draw trace must stop growing at some point inside a single run. It is
    truncated per run, so it never accumulated across jobs — but one long job
    wrote a point per motion segment for as long as it plotted.

Driven with asyncio.run rather than a pytest-asyncio fixture: the suite has no
async plugin and does not need one for two coroutines.
"""
import asyncio
import json

import pytest

from app import config, state


class FakeWS:
    """Records what it was sent, immediately."""

    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))


class StalledWS(FakeWS):
    """A socket that never completes a send — the backgrounded tab."""

    async def send_text(self, text):
        await asyncio.Event().wait()


@pytest.fixture
def broadcast_loop(monkeypatch):
    """Give state a fresh event queue bound to the running loop, and take the
    client registry back afterwards so no fake socket leaks into another test.
    """
    monkeypatch.setattr(state, "_clients", {})
    monkeypatch.setattr(state, "_event_queue", None)
    monkeypatch.setattr(state, "_loop", None)

    async def run(body):
        state._loop = asyncio.get_running_loop()
        state._event_queue = asyncio.Queue(maxsize=state._EVENT_QUEUE_MAXSIZE)
        drain = asyncio.create_task(state.drain_events())
        try:
            return await body()
        finally:
            drain.cancel()
            for client in list(state._clients.values()):
                if client.task is not None:
                    client.task.cancel()

    return lambda body: asyncio.run(run(body))


async def _settle(rounds=20):
    """Let the drain task and every sender task run to quiescence."""
    for _ in range(rounds):
        await asyncio.sleep(0)


def test_stalled_client_does_not_block_a_healthy_one(broadcast_loop, monkeypatch):
    # Draw stream on, so emit_position isn't throttled to 10Hz and every
    # sample reaches the fan-out.
    monkeypatch.setattr(config, "DRAW_STREAM_ENABLED", True)

    async def body():
        stalled, healthy = StalledWS(), FakeWS()
        state.add_client(stalled)
        state.add_client(healthy)
        await _settle()
        for i in range(10):
            state.emit_position(float(i), 0.0, True)
        await _settle()
        return healthy.sent

    sent = broadcast_loop(body)
    # Opening snapshot plus every position, in order, despite the other client
    # being stuck on its very first send.
    assert sent[0]["type"] == "state"
    positions = [m["x_mm"] for m in sent if m["type"] == "position"]
    assert positions == [float(i) for i in range(10)]


def test_stalled_client_backlog_stays_bounded_and_keeps_state_frames(
        broadcast_loop, monkeypatch):
    monkeypatch.setattr(config, "DRAW_STREAM_ENABLED", True)
    overflow = state._CLIENT_BACKLOG_MAX * 3

    async def body():
        stalled = StalledWS()
        state.add_client(stalled)
        await _settle()
        for i in range(overflow):
            state.emit_position(float(i), 0.0, True)
        state.broadcast()
        await _settle()
        return list(state._clients[stalled].pending)

    pending = broadcast_loop(body)
    assert len(pending) == state._CLIENT_BACKLOG_MAX
    # Positions were what got dropped; the transition survived.
    assert pending[-1][0] == "state"
    # And the ones kept are the most recent samples, not the stalest.
    kept = [json.loads(text)["x_mm"] for kind, text in pending if kind == "position"]
    assert kept == sorted(kept)
    assert kept[-1] == float(overflow - 1)


def test_removed_client_stops_its_sender_task(broadcast_loop):
    async def body():
        ws = FakeWS()
        state.add_client(ws)
        await _settle()
        task = state._clients[ws].task
        state.remove_client(ws)
        await _settle()
        return task.cancelled(), state._clients

    cancelled, clients = broadcast_loop(body)
    assert cancelled
    assert clients == {}


def test_event_queue_drops_rather_than_raising_when_full(broadcast_loop):
    """A blocked loop must not turn into an unhandled QueueFull."""
    async def body():
        # No drain running for this one: fill past the bound by hand.
        q = asyncio.Queue(maxsize=2)
        state._event_queue = q
        for _ in range(5):
            state._offer_event({"type": "position"})
        return q.qsize()

    assert broadcast_loop(body) == 2


def test_ws_route_still_opens_with_a_snapshot(client, monkeypatch):
    """End-to-end through the real route: the opening frame moved from the
    route body into add_client, and must still arrive."""
    monkeypatch.setattr(state, "_clients", {})
    with client.websocket_connect("/ws/state") as ws:
        first = ws.receive_json()
    assert first["type"] == "state"
    assert "queue" in first


# Draw trace ---------------------------------------------------------------

def test_draw_trace_stops_appending_at_the_cap(monkeypatch):
    monkeypatch.setattr(config, "DRAW_STREAM_ENABLED", True)
    monkeypatch.setattr(state, "_DRAW_TRACE_MAX_BYTES", 2000)
    monkeypatch.setattr(state, "_loop", None)  # no fan-out needed here
    try:
        state.set_active("tracecap")
        for i in range(500):
            state.emit_position(float(i), 1.0, True)
        size = state.DRAW_TRACE_PATH.stat().st_size
    finally:
        state.set_active(None)
    assert 2000 <= size < 2000 + 200, size


def test_draw_trace_is_truncated_and_the_cap_resets_per_run(monkeypatch):
    monkeypatch.setattr(config, "DRAW_STREAM_ENABLED", True)
    monkeypatch.setattr(state, "_DRAW_TRACE_MAX_BYTES", 2000)
    monkeypatch.setattr(state, "_loop", None)
    try:
        state.set_active("run-one")
        for i in range(500):
            state.emit_position(float(i), 1.0, True)
        state.set_active(None)
        state.set_active("run-two")
        state.emit_position(1.0, 1.0, True)
        lines = state.DRAW_TRACE_PATH.read_text().splitlines()
    finally:
        state.set_active(None)
    # A fresh run starts from an empty file with a fresh byte budget, rather
    # than inheriting the previous run's exhausted one.
    assert len(lines) == 1
