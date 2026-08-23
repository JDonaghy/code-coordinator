"""#1632: the notifier is ADVISORY and ISOLATED.

    "An unreachable ntfy server must not affect dispatch, routing, the
    board, or any verdict. Assert this — same rule and same reason as
    #1485, where `/health` data was read as authoritative and silently
    degraded review routing."

The property is negative, which makes it exactly the kind of thing that
quietly stops being true. These tests pin it from both directions: the
transport can fail in every way an HTTP client can fail without the tick
raising, and a tick that fails for any reason at all returns a report
rather than an exception to the daemon's hot path.
"""

from __future__ import annotations

import pytest

from coord.config import NotificationsConfig
from coord.notifier import service, store
from coord.notifier.baseline import build_baselines
from coord.notifier.models import Message
from coord.notifier.predicate import PipelineSnapshot, WorkerProbe
from coord.notifier.transport import (
    MemoryTransport,
    NtfyTransport,
    NullTransport,
    SendResult,
    build_transport,
    safe_send,
)

HOUR = 3600.0
NOW = 1_800_000.0


class FakeConfig:
    def __init__(self, **kw):
        kw.setdefault("enabled", True)
        self.notifications = NotificationsConfig(**kw)
        self.machines = []


def history(n=10, secs=600.0):
    return [
        {"repo_name": "coord", "type": "work", "issue_number": 1, "status": "done",
         "dispatched_at": 0.0, "finished_at": secs}
        for _ in range(n)
    ]


def slow_snapshot(now=NOW):
    # #2609: over_baseline is gated on output silence, so this fixture needs
    # a stale `last_output_at` to raise anything at all — these tests are
    # about transport/state isolation, not the predicate, so just clear the
    # default warm-baseline silence threshold `history()` builds elsewhere.
    return PipelineSnapshot(
        now=now,
        probes=[
            WorkerProbe(assignment_id="a1", repo="coord", issue=42,
                        machine="dellserver", dispatched_at=now - 10 * HOUR,
                        last_output_at=now - 20 * 60.0)
        ],
    )


# ── the transport can never raise ────────────────────────────────────────


def test_ntfy_connection_failure_returns_a_result_not_an_exception(monkeypatch):
    import httpx

    def boom(*a, **kw):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "post", boom)
    result = NtfyTransport(base_url="http://nope:7440", topic="coord").send(
        Message(title="t", body="b")
    )
    assert result.ok is False
    assert "ConnectError" in (result.error or "")


def test_ntfy_timeout_returns_a_result_not_an_exception(monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx, "post", lambda *a, **kw: (_ for _ in ()).throw(httpx.ReadTimeout("slow"))
    )
    assert NtfyTransport(base_url="http://x", topic="c").send(Message("t", "b")).ok is False


def test_ntfy_http_error_status_is_a_failed_send(monkeypatch):
    import httpx

    class Response:
        status_code = 403

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: Response())
    result = NtfyTransport(base_url="http://x", topic="c").send(Message("t", "b"))
    assert result.ok is False and result.status == 403


def test_non_latin1_titles_do_not_break_delivery(monkeypatch):
    """Issue titles carry em dashes and CJK; HTTP headers cannot. A
    cosmetic encoding problem must not become a failed delivery."""
    import httpx

    captured = {}

    class Response:
        status_code = 200

    def fake_post(url, content=None, headers=None, timeout=None):
        captured["headers"] = headers
        headers["Title"].encode("latin-1")  # must not raise
        return Response()

    monkeypatch.setattr(httpx, "post", fake_post)
    result = NtfyTransport(base_url="http://x", topic="c").send(
        Message(title="coord#42 — 日本語 stalled", body="b")
    )
    assert result.ok is True
    assert captured["headers"]["Title"]


def test_a_third_party_transport_that_raises_is_contained():
    class Rogue:
        name = "rogue"

        def send(self, message):
            raise RuntimeError("I did not read the docstring")

    assert safe_send(Rogue(), Message("t", "b")).ok is False


# ── the tick can never raise ─────────────────────────────────────────────


def test_tick_returns_a_report_when_the_transport_is_down():
    transport = MemoryTransport(fail=True)
    result = service.tick(
        FakeConfig(),
        now=NOW,
        transport=transport,
        snapshot=slow_snapshot(),
        baselines=build_baselines(history()),
    )
    assert result.error is None
    assert result.delivered == []
    assert len(result.failed) == 1


def test_an_undelivered_event_is_not_ledgered_and_retries():
    """A failed send is NOT "already told". An ntfy server down for an hour
    must cost a delayed notification, never a lost one."""
    down = MemoryTransport(fail=True)
    config = FakeConfig()
    baselines = build_baselines(history())
    service.tick(config, now=NOW, transport=down, snapshot=slow_snapshot(),
                 baselines=baselines)
    assert store.load_state().ledger == {}

    up = MemoryTransport()
    result = service.tick(config, now=NOW + HOUR, transport=up,
                          snapshot=slow_snapshot(NOW + HOUR), baselines=baselines)
    assert len(result.delivered) == 1
    assert len(up.sent) == 1


def test_tick_never_raises_even_when_the_predicate_explodes(monkeypatch):
    monkeypatch.setattr(
        service, "evaluate", lambda *a, **kw: (_ for _ in ()).throw(ValueError("boom"))
    )
    result = service.tick(
        FakeConfig(), now=NOW, transport=MemoryTransport(),
        snapshot=slow_snapshot(), baselines={},
    )
    assert result.error is not None
    assert "ValueError" in result.error


def test_tick_never_raises_when_collection_explodes(monkeypatch):
    monkeypatch.setattr(
        service.collect_mod, "collect",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("board unreachable")),
    )
    result = service.tick(FakeConfig(), now=NOW, transport=MemoryTransport())
    assert result.error is not None


def test_tick_never_raises_when_the_state_file_is_unwritable(tmp_path, monkeypatch):
    # A directory where the file should be: every write fails, for ever.
    bad = tmp_path / "notifier.json"
    bad.mkdir()
    monkeypatch.setenv("COORD_NOTIFIER_STATE", str(bad))
    transport = MemoryTransport()
    result = service.tick(
        FakeConfig(), now=NOW, transport=transport,
        snapshot=slow_snapshot(), baselines=build_baselines(history()),
    )
    assert result.error is None
    # Delivery still happened — a state file we cannot write is a reason to
    # notify MORE, never a reason to take the daemon down.
    assert len(transport.sent) == 1


def test_a_corrupt_state_file_degrades_to_empty(tmp_path, monkeypatch):
    bad = tmp_path / "notifier.json"
    bad.write_text("{not json at all", encoding="utf-8")
    monkeypatch.setenv("COORD_NOTIFIER_STATE", str(bad))
    state = store.load_state()
    assert state.ledger == {} and state.deferred == [] and state.urgent == {}


# ── a half-configured notifier is silent, not broken ─────────────────────


def test_missing_ntfy_settings_fall_back_to_the_null_transport():
    assert isinstance(build_transport(NotificationsConfig(transport="ntfy")), NullTransport)


def test_transport_none_is_the_null_transport():
    assert isinstance(build_transport(NotificationsConfig(transport="none")), NullTransport)


def test_an_unknown_transport_is_silent_rather_than_fatal():
    class Weird:
        transport = "carrier-pigeon"

    assert isinstance(build_transport(Weird()), NullTransport)


# ── nothing on the dispatch path imports this ────────────────────────────


@pytest.mark.parametrize(
    "module",
    ["coord.dispatch", "coord.brain", "coord.review", "coord.merge_queue", "coord.reconcile"],
)
def test_the_dispatch_path_does_not_depend_on_the_notifier(module):
    """The #1485 failure mode was an advisory signal becoming load-bearing
    for routing. The cheapest structural guard against a repeat is that the
    modules which decide dispatch, review routing and merges do not import
    this package at all."""
    import importlib
    import inspect

    source = inspect.getsource(importlib.import_module(module))
    assert "coord.notifier" not in source


def test_send_result_is_a_plain_value():
    assert SendResult(ok=True).ok is True
    assert SendResult(ok=False, error="x").error == "x"
