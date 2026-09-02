"""#3052: ``fetch_remote_config`` must write the thin-client config cache

atomically. A plain truncating write (the old ``Path.write_text``) leaves a
window where the on-disk file is empty, then progressively partial, while a
concurrent ``coord`` command loads it. The benign half of that race raises
"Config file is empty"; the dangerous half silently parses a
truncated-but-valid YAML document with repos/machines missing.

These tests drive the real ``fetch_remote_config`` (only ``httpx.get`` and
``os.fdopen`` are faked) and assert a concurrent reader of
``REMOTE_CONFIG_CACHE`` never observes anything but the complete old content
or the complete new content -- never a fragment.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import coord.client as cc


class _FakeResp:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


def _fake_svc() -> cc.ServiceConfig:
    return cc.ServiceConfig(url="http://daemon.example:7435", token=None)


def test_fetch_remote_config_writes_via_tempfile_and_replace(tmp_path, monkeypatch):
    """Sanity check the mechanism: a tmp file lands in the same dir, then
    ``os.replace`` swaps it in -- never a direct truncating write to the
    cache path itself.
    """
    cache = tmp_path / "coordinator.remote.yml"
    monkeypatch.setattr(cc, "COORD_DIR", tmp_path)
    monkeypatch.setattr(cc, "REMOTE_CONFIG_CACHE", cache)
    monkeypatch.setattr(cc.httpx, "get", lambda *a, **kw: _FakeResp("repos: []\n"))

    replace_calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def spy_replace(src, dst):
        replace_calls.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(cc.os, "replace", spy_replace)

    result = cc.fetch_remote_config(_fake_svc())

    assert result == cache
    assert cache.read_text() == "repos: []\n"
    assert len(replace_calls) == 1
    src, dst = replace_calls[0]
    assert dst == cache
    assert src.parent == cache.parent
    assert src != cache
    # No leftover tempfile after a successful run.
    assert list(tmp_path.iterdir()) == [cache]


def test_fetch_remote_config_cleans_up_tempfile_on_failure(tmp_path, monkeypatch):
    """If the write itself blows up, no stray ``.tmp`` file should survive,
    and the previously-cached config must be left untouched.
    """
    cache = tmp_path / "coordinator.remote.yml"
    cache.write_text("repos:\n  - name: old\n")
    monkeypatch.setattr(cc, "COORD_DIR", tmp_path)
    monkeypatch.setattr(cc, "REMOTE_CONFIG_CACHE", cache)
    monkeypatch.setattr(cc.httpx, "get", lambda *a, **kw: _FakeResp("repos: [oops"))

    real_fdopen = os.fdopen

    def boom_fdopen(fd, *a, **kw):
        fh = real_fdopen(fd, *a, **kw)

        def boom_write(_data):
            raise OSError("disk full")

        fh.write = boom_write
        return fh

    monkeypatch.setattr(cc.os, "fdopen", boom_fdopen)

    import pytest

    with pytest.raises(OSError):
        cc.fetch_remote_config(_fake_svc())

    # Old content is untouched, and the tempfile was cleaned up.
    assert cache.read_text() == "repos:\n  - name: old\n"
    assert list(tmp_path.iterdir()) == [cache]


def test_concurrent_reader_never_observes_a_partial_config(tmp_path, monkeypatch):
    """The race from #3052: a reader polling ``REMOTE_CONFIG_CACHE`` while a
    refresh is in flight must see either the complete old document or the
    complete new one -- never empty, never truncated mid-write.
    """
    old_content = "repos:\n  - name: old-repo-1\n  - name: old-repo-2\n"
    new_content = "repos:\n" + "".join(f"  - name: repo-{i}\n" for i in range(5000))

    cache = tmp_path / "coordinator.remote.yml"
    cache.write_text(old_content)
    monkeypatch.setattr(cc, "COORD_DIR", tmp_path)
    monkeypatch.setattr(cc, "REMOTE_CONFIG_CACHE", cache)
    monkeypatch.setattr(cc.httpx, "get", lambda *a, **kw: _FakeResp(new_content))

    real_fdopen = os.fdopen
    write_started = threading.Event()
    release_write = threading.Event()

    def slow_fdopen(fd, *a, **kw):
        fh = real_fdopen(fd, *a, **kw)
        orig_write = fh.write

        def slow_write(data):
            write_started.set()
            # Give the reader thread a real window mid-write, before the
            # tempfile is even flushed, let alone renamed into place.
            release_write.wait(timeout=5)
            return orig_write(data)

        fh.write = slow_write
        return fh

    monkeypatch.setattr(cc.os, "fdopen", slow_fdopen)

    observed: list[str] = []

    def reader() -> None:
        assert write_started.wait(timeout=5), "writer never started"
        for _ in range(20):
            # Read exactly what a concurrent `coord` command would load.
            observed.append(cache.read_text())
        release_write.set()

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    result = cc.fetch_remote_config(_fake_svc())
    reader_thread.join(timeout=5)

    assert not reader_thread.is_alive()
    assert observed, "reader thread never ran"
    # Every observation is a *complete* document -- old or new, never a
    # fragment (empty string or a byte-count between the two).
    for snapshot in observed:
        assert snapshot in (old_content, new_content), (
            f"reader observed a partial config of length {len(snapshot)}"
        )

    assert result == cache
    assert cache.read_text() == new_content
