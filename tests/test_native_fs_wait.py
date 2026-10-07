"""Tests for :mod:`coord.native_fs_wait` — the one shared ``expect_file``
check every Tier-2 native driver (mac/win/gtk/tui-pty) calls through (#3650,
#2096 "one question, one answer")."""

from __future__ import annotations

import os
import threading
import time

from coord.native_fs_wait import wait_for_file


def test_file_already_present_passes_immediately(tmp_path):
    target = tmp_path / "probe.txt"
    target.write_text("hello")
    ok, reason = wait_for_file(str(target), timeout_ms=1000)
    assert ok is True
    assert reason == ""


def test_missing_file_times_out_with_a_reason(tmp_path):
    target = tmp_path / "never-appears.txt"
    start = time.monotonic()
    ok, reason = wait_for_file(str(target), timeout_ms=200)
    elapsed = time.monotonic() - start
    assert ok is False
    assert "did not appear" in reason
    assert elapsed < 2.0  # bounded by the deadline, not blocking forever


def test_file_appearing_mid_wait_is_observed(tmp_path):
    target = tmp_path / "delayed.txt"

    def _write_later():
        time.sleep(0.2)
        target.write_text("ready")

    threading.Thread(target=_write_later, daemon=True).start()
    ok, reason = wait_for_file(str(target), timeout_ms=3000, poll_interval_s=0.02)
    assert ok is True
    assert reason == ""


def test_contains_check_passes_when_substring_present(tmp_path):
    target = tmp_path / "content.txt"
    target.write_text("the quick brown fox")
    ok, reason = wait_for_file(str(target), timeout_ms=1000, contains="brown")
    assert ok is True
    assert reason == ""


def test_contains_check_fails_with_actual_content_in_message(tmp_path):
    target = tmp_path / "content.txt"
    target.write_text("the quick brown fox")
    ok, reason = wait_for_file(str(target), timeout_ms=200, contains="giraffe")
    assert ok is False
    assert "giraffe" in reason
    assert "quick brown fox" in reason


def test_directory_instead_of_file_fails_without_hanging(tmp_path):
    ok, reason = wait_for_file(str(tmp_path), timeout_ms=1000)
    assert ok is False
    assert "directory" in reason


def test_expands_tilde_and_env_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("COORD_TEST_PROBE_DIR", str(tmp_path))
    target = tmp_path / "env-expanded.txt"
    target.write_text("x")
    ok, _reason = wait_for_file("$COORD_TEST_PROBE_DIR/env-expanded.txt", timeout_ms=500)
    assert ok is True


def test_expands_powershell_style_env_token(tmp_path, monkeypatch):
    monkeypatch.setenv("COORD_TEST_PROBE_DIR", str(tmp_path))
    target = tmp_path / "ps-env.txt"
    target.write_text("x")
    ok, _reason = wait_for_file(r"$env:COORD_TEST_PROBE_DIR/ps-env.txt", timeout_ms=500)
    assert ok is True
