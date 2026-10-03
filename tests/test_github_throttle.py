"""Tests for coord.github_throttle (#2809: the shared per-machine GitHub
rate-limit backoff state; #2934: made fleet-wide via the board daemon).

Scope: the module is a small, best-effort, file-backed signal — record a
hit, consult it before the next call, never shrink an active window, never
raise into a caller regardless of what the on-disk state looks like. The
`tests/conftest.py` autouse `_no_real_github_backoff_store` fixture already
redirects every test's `$COORD_GITHUB_BACKOFF_STATE` to a private tmp file,
so these tests never touch the operator's real `~/.coord/github_backoff.json`.

Every test above `TestDaemonRouting` runs with no board service configured
(the default), so `record()`/`consult()` exercise exactly the same local-file
code path their pre-#2934 counterparts — `local_record()`/`local_consult()` —
always did; #2934 only added a daemon-first branch in front of it.
`TestDaemonRouting` below is the unit-level coverage for THAT branch (mocking
`coord.board_service.resolve()` and the `coord.client` transport functions,
same pattern as `tests/test_machine_pause.py`'s `_remote()` helper); the full
cross-process round trip through a real daemon app — one host's `record()`
observed by another host's `consult()` — is covered separately in
`tests/test_serve.py::test_github_backoff_recorded_on_one_host_is_honoured_by_another`.
"""

from __future__ import annotations

import json

import httpx
import pytest

from coord import client as coord_client
from coord import github_throttle


class TestRecordAndCurrent:
    def test_no_backoff_by_default(self) -> None:
        assert github_throttle.current() is None

    def test_record_then_current_reads_it_back(self) -> None:
        github_throttle.record(
            reason="secondary_rate_limit", status=403,
            request_id="ABCD:1234", retry_after_s=30.0, now=1000.0,
        )
        b = github_throttle.current(now=1010.0)
        assert b is not None
        assert b.reason == "secondary_rate_limit"
        assert b.status == 403
        assert b.request_id == "ABCD:1234"
        assert b.retry_after_s == 30.0
        assert b.until == pytest.approx(1030.0)

    def test_expired_backoff_reads_as_none(self) -> None:
        github_throttle.record(
            reason="primary_rate_limit", status=403,
            request_id=None, retry_after_s=10.0, now=1000.0,
        )
        assert github_throttle.current(now=1011.0) is None

    def test_missing_retry_after_uses_default_backoff(self) -> None:
        github_throttle.record(
            reason="secondary_rate_limit", status=403,
            request_id=None, retry_after_s=None, now=1000.0,
        )
        b = github_throttle.current(now=1000.0)
        assert b is not None
        assert b.until == pytest.approx(1000.0 + github_throttle.DEFAULT_BACKOFF_S)

    def test_second_hit_never_shrinks_an_active_window(self) -> None:
        """A fresh, SHORTER observation while a longer backoff is still
        active must not pull the shared window in early -- #2809's damping
        only works if a hit can extend the wait, never race it shorter."""
        github_throttle.record(
            reason="secondary_rate_limit", status=403,
            request_id=None, retry_after_s=120.0, now=1000.0,
        )
        github_throttle.record(
            reason="primary_rate_limit", status=403,
            request_id=None, retry_after_s=5.0, now=1001.0,
        )
        b = github_throttle.current(now=1001.0)
        assert b is not None
        assert b.until == pytest.approx(1120.0)

    def test_outsized_retry_after_is_capped_not_sticky(self) -> None:
        """#2809 review: `record()` never shrinks an existing window (see
        the test above), so an uncapped, outsized `retry_after_s` — a
        malformed header, a parsing edge case — would otherwise become
        effectively sticky: every later hit while it's active inherits
        `max(existing.until, new_until)` and re-extends a window already far
        past GitHub's own "a few minutes" secondary-limit guidance. A single
        huge value must be clamped to `MAX_BACKOFF_S` up front."""
        github_throttle.record(
            reason="secondary_rate_limit", status=403,
            request_id=None, retry_after_s=999_999.0, now=1000.0,
        )
        b = github_throttle.current(now=1000.0)
        assert b is not None
        assert b.until == pytest.approx(1000.0 + github_throttle.MAX_BACKOFF_S)

    def test_second_hit_extends_a_shorter_active_window(self) -> None:
        github_throttle.record(
            reason="primary_rate_limit", status=403,
            request_id=None, retry_after_s=5.0, now=1000.0,
        )
        github_throttle.record(
            reason="secondary_rate_limit", status=403,
            request_id="X:1", retry_after_s=120.0, now=1001.0,
        )
        b = github_throttle.current(now=1001.0)
        assert b is not None
        assert b.until == pytest.approx(1121.0)
        assert b.reason == "secondary_rate_limit"

    def test_clear_removes_the_recorded_backoff(self) -> None:
        github_throttle.record(
            reason="secondary_rate_limit", status=403,
            request_id=None, retry_after_s=60.0,
        )
        assert github_throttle.current() is not None
        github_throttle.clear()
        assert github_throttle.current() is None

    def test_clear_on_missing_file_does_not_raise(self) -> None:
        github_throttle.clear()
        github_throttle.clear()  # second call: file already gone

    def test_corrupt_state_file_reads_as_no_backoff(self, monkeypatch, tmp_path) -> None:
        bad = tmp_path / "corrupt.json"
        bad.write_text("not json at all", encoding="utf-8")
        monkeypatch.setenv("COORD_GITHUB_BACKOFF_STATE", str(bad))
        assert github_throttle.current() is None

    def test_record_is_best_effort_on_unwritable_path(self, monkeypatch, tmp_path) -> None:
        # Point the state file at a path whose parent can't be created
        # (a file standing where a directory would need to go).
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setenv(
            "COORD_GITHUB_BACKOFF_STATE", str(blocker / "nested" / "github_backoff.json")
        )
        # Must not raise -- record() is unconditionally best-effort.
        github_throttle.record(
            reason="secondary_rate_limit", status=403,
            request_id=None, retry_after_s=30.0,
        )
        assert github_throttle.current() is None


class TestConsult:
    def test_no_backoff_returns_zero_sleep(self) -> None:
        sleep_s, backoff = github_throttle.consult(now=1000.0)
        assert sleep_s == 0.0
        assert backoff is None

    def test_short_remaining_window_is_reflected_in_sleep(self) -> None:
        github_throttle.record(
            reason="secondary_rate_limit", status=403,
            request_id=None, retry_after_s=5.0, now=1000.0,
        )
        sleep_s, backoff = github_throttle.consult(now=1002.0)
        assert backoff is not None
        # 3s remaining, +/-20% jitter.
        assert 2.0 <= sleep_s <= 4.0

    def test_long_remaining_window_caps_sleep_at_the_precall_ceiling(self) -> None:
        github_throttle.record(
            reason="secondary_rate_limit", status=403,
            request_id=None, retry_after_s=600.0, now=1000.0,
        )
        sleep_s, backoff = github_throttle.consult(now=1000.0)
        assert backoff is not None
        # Jittered around the cap, never anywhere near the full 600s.
        assert sleep_s <= github_throttle.MAX_PRECALL_SLEEP_S * 1.25

    def test_consult_never_sleeps_itself(self, monkeypatch) -> None:
        """Pure read: consult() must never call time.sleep -- the caller
        (`github_ops._gh`) decides whether/how to act on the result."""
        called = []
        monkeypatch.setattr(github_throttle.time, "sleep", lambda s: called.append(s))
        github_throttle.record(
            reason="secondary_rate_limit", status=403,
            request_id=None, retry_after_s=30.0,
        )
        github_throttle.consult()
        assert called == []


class TestStatePathOverride:
    def test_state_file_contents_are_plain_json(self, tmp_path, monkeypatch) -> None:
        path = tmp_path / "custom-backoff.json"
        monkeypatch.setenv("COORD_GITHUB_BACKOFF_STATE", str(path))
        github_throttle.record(
            reason="secondary_rate_limit", status=403,
            request_id="req-1", retry_after_s=42.0, now=500.0,
        )
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["reason"] == "secondary_rate_limit"
        assert data["status"] == 403
        assert data["request_id"] == "req-1"
        assert data["retry_after_s"] == 42.0
        assert data["until"] == pytest.approx(542.0)


# ── #2934: daemon-aware routing ─────────────────────────────────────────────
#
# `record()`/`consult()` route through the board daemon when one is
# configured (thin client), following the same daemon-aware pattern
# `coord.machine_pause` established for pause/quiet-hours/cordons. These
# tests cover the unit-level routing decision in isolation — see the module
# docstring for where the real cross-process round trip lives.


def _remote(monkeypatch, url: str = "http://daemon:7435") -> None:
    """Make `coord.board_service.resolve()` (and therefore github_throttle's
    daemon-aware functions) act as a thin client pointed at *url* — same
    helper `tests/test_machine_pause.py` uses for the identical seam."""
    monkeypatch.setattr(
        coord_client, "resolve_board_service",
        lambda *a, **k: coord_client.ServiceConfig(url=url),
    )


class TestDaemonRouting:
    def test_record_posts_to_the_daemon_instead_of_the_local_file(
        self, monkeypatch, tmp_path
    ) -> None:
        _remote(monkeypatch)
        posted = {}

        def _fake_post(svc, **kwargs):
            posted["url"] = svc.url
            posted.update(kwargs)
            return {"backoff": None}

        monkeypatch.setattr(coord_client, "post_github_backoff", _fake_post)

        github_throttle.record(
            reason="secondary_rate_limit", status=403,
            request_id="req-1", retry_after_s=120.0,
        )
        assert posted == {
            "url": "http://daemon:7435",
            "reason": "secondary_rate_limit", "status": 403,
            "request_id": "req-1", "retry_after_s": 120.0,
        }
        # Never fell through to the local file.
        assert github_throttle.current() is None

    def test_record_falls_back_to_the_local_file_when_the_daemon_is_unreachable(
        self, monkeypatch
    ) -> None:
        _remote(monkeypatch)

        def _raise(*_a, **_k):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(coord_client, "post_github_backoff", _raise)

        # Must not raise -- record() is unconditionally best-effort, daemon
        # routing included (module docstring: "damping must never become a
        # new way to break `gh` access").
        github_throttle.record(
            reason="secondary_rate_limit", status=403,
            request_id=None, retry_after_s=90.0, now=1000.0,
        )
        b = github_throttle.current(now=1000.0)
        assert b is not None
        assert b.until == pytest.approx(1090.0)

    def test_consult_asks_the_daemon_when_remote(self, monkeypatch) -> None:
        _remote(monkeypatch)
        monkeypatch.setattr(
            coord_client, "fetch_github_backoff",
            lambda svc, **k: {
                "until": 1120.0, "reason": "secondary_rate_limit", "status": 403,
                "request_id": "remote-hit", "retry_after_s": 120.0, "recorded_at": 1000.0,
            },
        )
        sleep_s, backoff = github_throttle.consult(now=1002.0)
        assert backoff is not None
        assert backoff.request_id == "remote-hit"
        # 118s remaining, capped at MAX_PRECALL_SLEEP_S +/- jitter.
        assert sleep_s <= github_throttle.MAX_PRECALL_SLEEP_S * 1.25
        # Never touched the local file to answer this.
        assert github_throttle.current() is None

    def test_consult_reports_no_backoff_when_the_daemon_has_none(self, monkeypatch) -> None:
        _remote(monkeypatch)
        monkeypatch.setattr(coord_client, "fetch_github_backoff", lambda svc, **k: None)
        sleep_s, backoff = github_throttle.consult(now=1000.0)
        assert sleep_s == 0.0
        assert backoff is None

    def test_consult_falls_back_to_the_local_file_when_the_daemon_is_unreachable(
        self, monkeypatch
    ) -> None:
        """The other three hosts must still honour their OWN last-known
        local state when the shared daemon can't be reached -- never 'no
        damping' just because the network blipped."""
        _remote(monkeypatch)

        def _raise(*_a, **_k):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(coord_client, "fetch_github_backoff", _raise)

        # Seed the LOCAL file directly (bypassing record(), which would also
        # try the daemon) to isolate consult()'s own fallback.
        github_throttle.local_record(
            reason="secondary_rate_limit", status=403,
            request_id=None, retry_after_s=60.0, now=1000.0,
        )
        sleep_s, backoff = github_throttle.consult(now=1002.0)
        assert backoff is not None
        assert backoff.until == pytest.approx(1060.0)

    def test_consult_treats_a_malformed_daemon_response_as_no_backoff(
        self, monkeypatch
    ) -> None:
        """A successful GET carrying junk (not an exception) reads as 'no
        backoff' -- same fail-open posture `_read()` already has for a
        corrupt local file -- rather than falling back to the local file,
        which the daemon DID successfully answer for."""
        _remote(monkeypatch)
        monkeypatch.setattr(
            coord_client, "fetch_github_backoff", lambda svc, **k: {"not": "a backoff"},
        )
        sleep_s, backoff = github_throttle.consult(now=1000.0)
        assert sleep_s == 0.0
        assert backoff is None


# ── #3536: proactive pacing (a token bucket, ahead of any 403) ──────────────


class TestPaceStep:
    """`_pace_step` is the pure bucket arithmetic -- no file I/O, no clock
    read -- so it is tested directly, independent of `local_pace_acquire`'s
    read/write wrapper (covered by `TestLocalPaceAcquire` below)."""

    def test_fresh_bucket_grants_immediately(self) -> None:
        sleep_s, state = github_throttle._pace_step(
            None, now=1000.0, rate_per_s=1.0, burst=5.0,
        )
        assert sleep_s == 0.0
        assert state["tokens"] == pytest.approx(4.0)
        assert state["last"] == 1000.0

    def test_burst_bounds_how_many_immediate_grants_in_a_row(self) -> None:
        """Simulated concurrent callers: repeated acquisitions at the SAME
        instant (no time to refill between them) are bounded by *burst* --
        the whole point of a token bucket over a bare "one call every N
        seconds" rate limiter, which would reject every one of a legitimate
        short clump."""
        state = None
        now = 1000.0
        granted = 0
        for _ in range(12):
            sleep_s, state = github_throttle._pace_step(
                state, now=now, rate_per_s=0.5, burst=5.0,
            )
            if sleep_s == 0.0:
                granted += 1
        assert granted == 5

    def test_once_the_burst_is_spent_a_caller_is_charged_a_wait(self) -> None:
        state = None
        now = 1000.0
        for _ in range(5):
            _, state = github_throttle._pace_step(
                state, now=now, rate_per_s=0.5, burst=5.0,
            )
        sleep_s, state = github_throttle._pace_step(
            state, now=now, rate_per_s=0.5, burst=5.0,
        )
        # Empty bucket, rate 0.5 tok/s -> one token takes 2s to accrue.
        assert sleep_s == pytest.approx(2.0)
        assert state["tokens"] == 0.0

    def test_tokens_refill_over_elapsed_time(self) -> None:
        _, state = github_throttle._pace_step(
            None, now=1000.0, rate_per_s=1.0, burst=5.0,
        )
        # Drain the rest of the burst at the same instant.
        for _ in range(3):
            _, state = github_throttle._pace_step(
                state, now=1000.0, rate_per_s=1.0, burst=5.0,
            )
        assert state["tokens"] == pytest.approx(1.0)
        # 10s later at 1 tok/s the bucket is back to full (capped at burst),
        # so a fresh caller is granted immediately again.
        sleep_s, state = github_throttle._pace_step(
            state, now=1010.0, rate_per_s=1.0, burst=5.0,
        )
        assert sleep_s == 0.0
        assert state["tokens"] == pytest.approx(4.0)

    def test_malformed_state_is_treated_as_a_fresh_full_bucket(self) -> None:
        sleep_s, state = github_throttle._pace_step(
            {"tokens": "not a number"}, now=1000.0, rate_per_s=1.0, burst=5.0,
        )
        assert sleep_s == 0.0
        assert state["tokens"] == pytest.approx(4.0)


class TestLocalPaceAcquire:
    def test_first_call_is_never_delayed(self) -> None:
        assert github_throttle.local_pace_acquire(now=1000.0) == 0.0

    def test_state_file_persists_across_calls(self, tmp_path, monkeypatch) -> None:
        path = tmp_path / "pace.json"
        monkeypatch.setenv("COORD_GITHUB_PACE_STATE", str(path))
        for _ in range(int(github_throttle.PACE_DEFAULT_BURST)):
            github_throttle.local_pace_acquire(
                now=1000.0, rate_per_s=0.1, burst=github_throttle.PACE_DEFAULT_BURST,
            )
        assert path.exists()
        sleep_s = github_throttle.local_pace_acquire(
            now=1000.0, rate_per_s=0.1, burst=github_throttle.PACE_DEFAULT_BURST,
        )
        # Burst exhausted at the same instant -- a jittered wait for the
        # next token at a slow 0.1 tok/s rate (~10s +/- 20%).
        assert sleep_s > 0.0

    def test_bounds_calls_per_window_across_simulated_concurrent_callers(
        self, tmp_path, monkeypatch,
    ) -> None:
        """#3536 acceptance: a token bucket shared via the same on-disk file
        every process on this host reads/writes bounds how many of a batch
        of "concurrent" callers (here: back-to-back calls at the same
        instant, standing in for separate processes racing the same file)
        get waved through immediately."""
        path = tmp_path / "pace.json"
        monkeypatch.setenv("COORD_GITHUB_PACE_STATE", str(path))
        granted = 0
        for _ in range(20):
            sleep_s = github_throttle.local_pace_acquire(
                now=2000.0, rate_per_s=0.5, burst=6.0,
            )
            if sleep_s == 0.0:
                granted += 1
        assert granted == 6

    def test_best_effort_on_unwritable_path(self, tmp_path, monkeypatch) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setenv(
            "COORD_GITHUB_PACE_STATE", str(blocker / "nested" / "github_pace.json")
        )
        # Must not raise -- best-effort, same posture as `local_record`.
        assert github_throttle.local_pace_acquire(now=1000.0) == 0.0

    def test_corrupt_state_file_is_treated_as_a_fresh_bucket(
        self, tmp_path, monkeypatch,
    ) -> None:
        bad = tmp_path / "corrupt.json"
        bad.write_text("not json at all", encoding="utf-8")
        monkeypatch.setenv("COORD_GITHUB_PACE_STATE", str(bad))
        assert github_throttle.local_pace_acquire(now=1000.0) == 0.0


class TestPace:
    def test_pace_is_local_pace_acquire_today(self, monkeypatch) -> None:
        """#3536: no daemon route exists yet (see the module docstring) --
        `pace()` is a thin, stable name in front of the same single-host
        bucket `local_pace_acquire` implements."""
        calls = []
        monkeypatch.setattr(
            github_throttle, "local_pace_acquire",
            lambda **kw: calls.append(kw) or 0.0,
        )
        result = github_throttle.pace(now=1000.0, rate_per_s=2.0, burst=9.0)
        assert result == 0.0
        assert calls == [{"now": 1000.0, "rate_per_s": 2.0, "burst": 9.0}]
