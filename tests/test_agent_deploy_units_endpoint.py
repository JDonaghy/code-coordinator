"""`POST /deploy-units` — the agent side of the `deploy/**` lane (#1831/#1835).

The property that makes this endpoint safe to call from an unattended
propagation timer, and the one worth a test: **it restarts nothing.** A
`systemctl --user daemon-reload` re-reads unit files; it does not restart
running services, so no in-flight headless worker dies here. That is the
whole reason it can be a synchronous 200 rather than `/update`'s
fire-and-forget 202.

The filesystem behaviour itself lives in `tests/test_deploy_units_lane.py`;
what is tested here is the HTTP contract the propagation shell depends on.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from coord import deploy_units as du
from coord.agent import AgentServer
from coord.agent_app import build_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    server = AgentServer(
        machine_name="dellserver",
        capabilities=["python"],
        repos=[],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/true"],
        repo_paths={},
    )
    return TestClient(build_app(server))


@pytest.fixture()
def lane(tmp_path, monkeypatch):
    """A packaged unit set and an installed one, both under tmp_path."""
    ref = tmp_path / "packaged"
    ref.mkdir()
    (ref / "coord-agent.service").write_text(
        "[Service]\nExecStart=coord agent --machine <MACHINE_NAME> --port <PORT>\n"
    )
    dest = tmp_path / "systemd-user"
    dest.mkdir()
    (dest / "coord-agent.service").write_text("[Service]\nExecStart=stale\n")

    real_install = du.install_units
    reloads: list[bool] = []

    def _install(**kwargs):
        kwargs.setdefault("reference_dir", ref)
        kwargs.setdefault("target_dir", dest)
        kwargs["reference_dir"] = ref
        kwargs["target_dir"] = dest
        return real_install(**kwargs)

    monkeypatch.setattr(du, "install_units", _install)
    monkeypatch.setattr(
        du, "daemon_reload",
        lambda **k: (reloads.append(True), (True, "daemon-reload ok"))[1],
    )
    return dest, reloads


def test_the_endpoint_deploys_and_reloads(client, lane):
    dest, reloads = lane
    resp = client.post("/deploy-units", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["reloaded"] is True
    # The machine name came from the *server*, so the template rendered for
    # this host rather than being copied verbatim (#1928).
    assert "--machine dellserver" in (dest / "coord-agent.service").read_text()
    assert reloads == [True]


def test_a_dry_run_writes_nothing_and_never_reloads(client, lane):
    dest, reloads = lane
    before = (dest / "coord-agent.service").read_text()
    resp = client.post("/deploy-units", json={"dry_run": True})
    assert resp.status_code == 200
    assert resp.json()["dry_run"] is True
    assert (dest / "coord-agent.service").read_text() == before
    assert reloads == []


def test_nothing_to_do_still_answers_200(client, lane):
    dest, reloads = lane
    client.post("/deploy-units", json={})
    reloads.clear()
    resp = client.post("/deploy-units", json={})
    assert resp.status_code == 200
    assert resp.json()["changed"] is False
    # No change => no reload. A reload is cheap but not free, and "we
    # reloaded" in the record should mean something actually moved.
    assert reloads == []


@pytest.fixture()
def lane_with_timer(tmp_path, monkeypatch):
    """Same shape as `lane`, plus a packaged+installed `.timer` unit — the
    lane `enable_timers` (#2082) actually acts on. `install_units` only
    reports units present in the PACKAGED reference, so the timer has to
    exist on both sides to appear in the report at all."""
    ref = tmp_path / "packaged"
    ref.mkdir()
    (ref / "coord-agent.service").write_text(
        "[Service]\nExecStart=coord agent --machine <MACHINE_NAME> --port <PORT>\n"
    )
    (ref / "coord-agent.timer").write_text("[Timer]\nOnUnitActiveSec=1min\n")
    dest = tmp_path / "systemd-user"
    dest.mkdir()
    (dest / "coord-agent.service").write_text("[Service]\nExecStart=stale\n")
    (dest / "coord-agent.timer").write_text("[Timer]\nOnUnitActiveSec=1min\n")

    real_install = du.install_units

    def _install(**kwargs):
        kwargs["reference_dir"] = ref
        kwargs["target_dir"] = dest
        return real_install(**kwargs)

    monkeypatch.setattr(du, "install_units", _install)
    monkeypatch.setattr(du, "daemon_reload", lambda **k: (True, "daemon-reload ok"))
    return dest


def test_the_endpoint_enables_installed_timers(client, lane_with_timer, monkeypatch):
    """#2082: refreshing a timer's content has never implied enabling it.
    The endpoint must assert enablement on every non-dry-run call, not just
    when content changed — `coord-agent.timer` is ACTION_UNCHANGED here."""
    calls: list = []

    def _fake_enable(report, **_kwargs):
        calls.append(sorted(u.name for u in report.units if u.name.endswith(".timer")))
        return {"coord-agent.timer": (True, True, "enabled")}

    monkeypatch.setattr(du, "enable_timers", _fake_enable)

    resp = client.post("/deploy-units", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["timers_enabled"] == {
        "coord-agent.timer": {"ok": True, "changed": True, "detail": "enabled"}
    }
    assert calls == [["coord-agent.timer"]]


def test_the_endpoint_reports_a_timer_it_left_alone(client, lane_with_timer, monkeypatch):
    """#2124: a timer `enable_timers` found already enabled (e.g. stopped by
    an operator on purpose) is reported with `changed: False` — the signal
    `coord release propagate`'s own output (`_roll_units`) uses to name it
    as held rather than started."""
    monkeypatch.setattr(
        du, "enable_timers",
        lambda report, **k: {
            "coord-agent.timer": (True, False, "already enabled (ActiveState=inactive)"),
        },
    )
    resp = client.post("/deploy-units", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["timers_enabled"] == {
        "coord-agent.timer": {
            "ok": True, "changed": False,
            "detail": "already enabled (ActiveState=inactive)",
        }
    }


def test_a_dry_run_never_enables_timers(client, lane_with_timer, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        du, "enable_timers", lambda report, **k: (calls.append(1), {})[1]
    )
    resp = client.post("/deploy-units", json={"dry_run": True})
    assert resp.status_code == 200
    assert resp.json()["timers_enabled"] == {}
    assert calls == []


def test_a_failed_timer_enable_fails_the_response(client, lane_with_timer, monkeypatch):
    monkeypatch.setattr(
        du, "enable_timers",
        lambda report, **k: {"coord-agent.timer": (False, False, "enable failed")},
    )
    resp = client.post("/deploy-units", json={})
    assert resp.status_code == 500
    body = resp.json()
    assert body["ok"] is False
    assert body["timers_enabled"]["coord-agent.timer"]["ok"] is False
    assert body["timers_enabled"]["coord-agent.timer"]["changed"] is False


def test_the_real_enable_timers_is_exercised_end_to_end(client, lane_with_timer, monkeypatch):
    """No monkeypatch of `enable_timers` itself here — only the subprocess
    boundary — so this exercises the real wiring from HTTP request through
    `install_units` to the actual `systemctl --user enable --now` argv,
    which is the thing that was silently never happening before #2082.
    `coord-agent.timer` has no prior state as far as this fake is
    concerned (empty `show` output), which `enable_timers` (#2124) must
    read the same way it reads genuinely never-enabled: go ahead and
    enable it."""
    calls: list = []

    def _fake_run(argv, **_kwargs):
        calls.append(list(argv))

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    resp = client.post("/deploy-units", json={})
    assert resp.status_code == 200
    assert resp.json()["timers_enabled"] == {
        "coord-agent.timer": {"ok": True, "changed": True, "detail": "enabled"}
    }
    assert calls[0][:3] == ["systemctl", "--user", "show"]
    assert calls[-1] == ["systemctl", "--user", "enable", "--now", "coord-agent.timer"]


def test_the_real_enable_timers_leaves_a_stopped_timer_stopped_end_to_end(
    client, lane_with_timer, monkeypatch
):
    """#2124's acceptance shape, exercised through the same real wiring as
    the test above: `coord-agent.timer` is already enabled but its
    `ActiveState` is `inactive` — exactly what `systemctl --user stop`
    leaves behind. No `enable`/`start` call may reach systemd for it."""
    calls: list = []

    def _fake_run(argv, **_kwargs):
        calls.append(list(argv))

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        if len(argv) > 2 and argv[2] == "show":
            _Proc.stdout = (
                "Id=coord-agent.timer\n"
                "UnitFileState=enabled\n"
                "ActiveState=inactive\n"
                "SubState=dead\n"
            )
        return _Proc()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    resp = client.post("/deploy-units", json={})
    assert resp.status_code == 200
    body = resp.json()["timers_enabled"]["coord-agent.timer"]
    assert body["ok"] is True
    assert body["changed"] is False
    assert "ActiveState=inactive" in body["detail"]
    assert all(argv[2] != "enable" for argv in calls)


def test_a_bodyless_post_is_accepted(client, lane):
    """The propagation shell POSTs `{}`; a timer retrying with no body at all
    must not 500."""
    assert client.post("/deploy-units").status_code == 200


def test_the_endpoint_is_in_the_served_openapi_spec(client):
    spec = client.get("/openapi.json").json()
    assert "/deploy-units" in spec["paths"]


def test_no_systemd_on_this_host_is_an_honest_nop_not_22_new_units(
    client, lane, monkeypatch
):
    """#3366: this lane is systemd-unit management. On a host with no
    `systemctl` at all (every macOS agent) every packaged unit reads as
    "new"/"not installed here" forever — that's the platform, not drift,
    and reporting it as noise on every propagation run trains the operator
    to ignore this lane. The endpoint must short-circuit BEFORE calling
    `du.install_units` at all, so the response never claims to have looked
    at units it structurally cannot manage here."""
    dest, reloads = lane
    monkeypatch.setattr("shutil.which", lambda name: None if name == "systemctl" else "/bin/true")
    resp = client.post("/deploy-units", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["units"] == []
    assert "n/a" in body["detail"]
    assert "no systemd" in body["detail"]
    # Never touched the filesystem lane at all.
    assert reloads == []
    assert (dest / "coord-agent.service").read_text() == "[Service]\nExecStart=stale\n"


def test_no_systemd_detail_names_the_known_supervisor(client, monkeypatch):
    """A launchd host's n/a response should say so, not just "unknown" —
    the whole point of #3366 is naming the actual supervisor everywhere."""
    monkeypatch.setattr("shutil.which", lambda name: None if name == "systemctl" else "/bin/true")
    monkeypatch.setattr("coord.restart_cmd.local_supervisor", lambda: "launchd")
    resp = client.post("/deploy-units", json={})
    assert resp.status_code == 200
    assert "launchd" in resp.json()["detail"]
