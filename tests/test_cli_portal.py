"""#2179: ``coord portal`` — status/heartbeat/push over the sync bridge client."""

from __future__ import annotations

import textwrap
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord.cli import main

CONFIG_YAML = textwrap.dedent("""
    repos:
      - name: coord
        github: owner/coord
    machines:
      - name: dellserver
        host: dellserver
        repos: [coord]
    portal:
      enabled: true
      base_url: https://intake.heurontech.com
      bridge_client_id: id-123
      bridge_client_secret: secret-456
""")

DISABLED_CONFIG_YAML = textwrap.dedent("""
    repos:
      - name: coord
        github: owner/coord
    machines:
      - name: dellserver
        host: dellserver
        repos: [coord]
""")

ENV_VAR_CONFIG_YAML = textwrap.dedent("""
    repos:
      - name: coord
        github: owner/coord
    machines:
      - name: dellserver
        host: dellserver
        repos: [coord]
    portal:
      enabled: true
      base_url: https://intake.heurontech.com
      bridge_client_id: ${BRIDGE_CLIENT_ID}
      bridge_client_secret: ${BRIDGE_CLIENT_SECRET}
""")


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "coordinator.yml"
    path.write_text(CONFIG_YAML)
    return str(path)


@pytest.fixture
def disabled_config_path(tmp_path):
    path = tmp_path / "coordinator.yml"
    path.write_text(DISABLED_CONFIG_YAML)
    return str(path)


@pytest.fixture
def env_var_config_path(tmp_path, monkeypatch):
    # #2336: BRIDGE_CLIENT_ID/BRIDGE_CLIENT_SECRET deliberately left unset in
    # this shell — mirrors a manual `ssh dellserver` session that never
    # sourced ~/.coord/coord-serve.env, unlike the coord-serve systemd unit's
    # EnvironmentFile=.
    monkeypatch.delenv("BRIDGE_CLIENT_ID", raising=False)
    monkeypatch.delenv("BRIDGE_CLIENT_SECRET", raising=False)
    path = tmp_path / "coordinator.yml"
    path.write_text(ENV_VAR_CONFIG_YAML)
    return str(path)


def run(*args):
    return CliRunner().invoke(main, list(args))


def test_portal_group_is_registered():
    result = run("portal", "--help")
    assert result.exit_code == 0
    for sub in ("status", "heartbeat", "push", "link"):
        assert sub in result.output


def test_sync_loop_commands_are_registered():
    """#1982: the loop's operator surface."""
    result = run("portal", "--help")
    assert result.exit_code == 0
    for sub in (
        "sync",
        "outbox",
        "events",
        "enqueue-status",
        "enqueue-design-round",
        "enqueue-preview",
        "remirror",
    ):
        assert sub in result.output


def test_status_reports_disabled_by_default(disabled_config_path):
    result = run("portal", "status", "--config", disabled_config_path)
    assert result.exit_code == 0
    assert "disabled" in result.output


def test_status_reports_enabled_and_credentials(config_path):
    result = run("portal", "status", "--config", config_path)
    assert result.exit_code == 0
    assert "ENABLED" in result.output
    assert "intake.heurontech.com" in result.output
    assert "credentials=set" in result.output


def test_status_reports_credentials_missing_for_unexpanded_env_var(env_var_config_path):
    """#2336: portal.bridge_client_id/secret are non-empty '${VAR}' strings,
    but the env var was never set in this shell — credentials_set must be
    false, not true just because the placeholder text itself is non-empty."""
    result = run("portal", "status", "--config", env_var_config_path, "--json")
    assert result.exit_code == 0, result.output
    assert '"credentials_set": false' in result.output


def test_status_reports_credentials_set_once_env_var_resolves(env_var_config_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_CLIENT_ID", "id-from-env")
    monkeypatch.setenv("BRIDGE_CLIENT_SECRET", "secret-from-env")
    result = run("portal", "status", "--config", env_var_config_path, "--json")
    assert result.exit_code == 0, result.output
    assert '"credentials_set": true' in result.output


def test_heartbeat_refuses_when_disabled(disabled_config_path):
    result = run("portal", "heartbeat", "--config", disabled_config_path)
    assert result.exit_code != 0
    assert "not enabled" in result.output


def test_push_refuses_when_disabled(disabled_config_path):
    result = run("portal", "push", "--config", disabled_config_path, "sub_1", "1", "shipped")
    assert result.exit_code != 0
    assert "not enabled" in result.output


def test_push_rejects_an_unrecognised_status(config_path):
    result = run("portal", "push", "--config", config_path, "sub_1", "1", "not-a-status")
    assert result.exit_code != 0
    assert "Invalid value" in result.output or "invalid" in result.output.lower()


def test_heartbeat_sends_and_reports_success(config_path, monkeypatch):
    def _post(url, json=None, headers=None, timeout=None):
        class _R:
            status_code = 200

            def json(self):
                return {"ok": True}

        return _R()

    monkeypatch.setattr("httpx.post", _post)
    result = run("portal", "heartbeat", "--config", config_path)
    assert result.exit_code == 0
    assert "sent" in result.output


def test_push_sends_and_reports_applied(config_path, monkeypatch):
    seen = {}

    def _post(url, json=None, headers=None, timeout=None):
        seen["json"] = json

        class _R:
            status_code = 200

            def json(self):
                return {"results": [{"submission_id": "sub_1", "outcome": "applied"}]}

        return _R()

    monkeypatch.setattr("httpx.post", _post)
    result = run("portal", "push", "--config", config_path, "sub_1", "3", "shipped")
    assert result.exit_code == 0
    assert "applied" in result.output
    assert seen["json"] == {
        "updates": [{"submission_id": "sub_1", "revision": 3, "fields": {"status": "shipped"}}]
    }


def test_push_rejects_a_whitespace_only_submission_id_cleanly(config_path):
    """Regression for #2179 review: a caller-error submission_id must come
    back as a clean 'push failed: ...' message via PortalBridgeError, not an
    uncaught ValueError/traceback — portal_push only catches PortalBridgeError."""
    result = run("portal", "push", "--config", config_path, "   ", "1", "shipped")
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "push failed" in result.output
    assert "submission_id" in result.output


def test_push_reports_rejection_as_failure(config_path, monkeypatch):
    def _post(url, json=None, headers=None, timeout=None):
        class _R:
            status_code = 200

            def json(self):
                return {
                    "results": [
                        {"submission_id": "sub_1", "outcome": "rejected", "reason": "unknown_submission"}
                    ]
                }

        return _R()

    monkeypatch.setattr("httpx.post", _post)
    result = run("portal", "push", "--config", config_path, "sub_1", "3", "shipped")
    assert result.exit_code != 0
    assert "rejected" in result.output
    assert "unknown_submission" in result.output


# ── #2507: milestone ↔ portal submission linkage ────────────────────────────


def test_link_reports_unlinked_by_default(config_path):
    result = run("portal", "link", "--config", config_path, "coord", "3")
    assert result.exit_code != 0
    assert "not linked" in result.output


def test_link_writes_then_reads_back(config_path):
    write = run(
        "portal", "link", "--config", config_path, "coord", "3", "sub_abc123"
    )
    assert write.exit_code == 0, write.output
    assert "linked" in write.output
    assert "sub_abc123" in write.output

    read = run("portal", "link", "--config", config_path, "coord", "3")
    assert read.exit_code == 0, read.output
    assert "submission_id=sub_abc123" in read.output


def test_link_relink_overwrites_not_appends(config_path):
    run("portal", "link", "--config", config_path, "coord", "3", "sub_typo")
    run("portal", "link", "--config", config_path, "coord", "3", "sub_fixed")

    read = run("portal", "link", "--config", config_path, "coord", "3")
    assert read.exit_code == 0, read.output
    assert "submission_id=sub_fixed" in read.output
    assert "sub_typo" not in read.output


def test_link_is_scoped_to_milestone_number(config_path):
    """Same repo, different milestone — distinct links, no cross-talk."""
    run("portal", "link", "--config", config_path, "coord", "3", "sub_ms3")
    run("portal", "link", "--config", config_path, "coord", "9", "sub_ms9")

    ms3 = run("portal", "link", "--config", config_path, "coord", "3")
    ms9 = run("portal", "link", "--config", config_path, "coord", "9")
    assert "submission_id=sub_ms3" in ms3.output
    assert "submission_id=sub_ms9" in ms9.output


def test_link_rejects_unknown_repo(config_path):
    result = run("portal", "link", "--config", config_path, "nope", "3", "sub_1")
    assert result.exit_code != 0
    assert "unknown repo" in result.output


# ── #2665: one-off issue (no milestone) linkage ─────────────────────────────


def test_link_issue_reports_unlinked_by_default(config_path):
    result = run(
        "portal", "link", "--config", config_path, "coord", "--issue", "42"
    )
    assert result.exit_code != 0
    assert "not linked" in result.output
    assert "issue #42" in result.output


def test_link_issue_writes_then_reads_back(config_path):
    write = run(
        "portal", "link", "--config", config_path, "coord",
        "--issue", "42", "sub_abc123",
    )
    assert write.exit_code == 0, write.output
    assert "linked" in write.output
    assert "issue #42" in write.output
    assert "sub_abc123" in write.output

    read = run(
        "portal", "link", "--config", config_path, "coord", "--issue", "42"
    )
    assert read.exit_code == 0, read.output
    assert "submission_id=sub_abc123" in read.output


def test_link_issue_relink_overwrites_not_appends(config_path):
    run(
        "portal", "link", "--config", config_path, "coord",
        "--issue", "42", "sub_typo",
    )
    run(
        "portal", "link", "--config", config_path, "coord",
        "--issue", "42", "sub_fixed",
    )

    read = run(
        "portal", "link", "--config", config_path, "coord", "--issue", "42"
    )
    assert read.exit_code == 0, read.output
    assert "submission_id=sub_fixed" in read.output
    assert "sub_typo" not in read.output


def test_link_issue_does_not_collide_with_a_same_numbered_milestone(config_path):
    """Issue #3 and milestone 3 are different keys — no cross-talk (#2665)."""
    run("portal", "link", "--config", config_path, "coord", "3", "sub_ms3")
    run(
        "portal", "link", "--config", config_path, "coord",
        "--issue", "3", "sub_issue3",
    )

    ms = run("portal", "link", "--config", config_path, "coord", "3")
    issue = run(
        "portal", "link", "--config", config_path, "coord", "--issue", "3"
    )
    assert "submission_id=sub_ms3" in ms.output
    assert "submission_id=sub_issue3" in issue.output


def test_link_rejects_both_milestone_number_and_issue(config_path):
    result = run(
        "portal", "link", "--config", config_path, "coord",
        "3", "sub_1", "--issue", "42",
    )
    assert result.exit_code != 0
    assert "not both" in result.output


def test_link_rejects_neither_milestone_number_nor_issue(config_path):
    result = run("portal", "link", "--config", config_path, "coord")
    assert result.exit_code != 0
    assert "MILESTONE_NUMBER or --issue" in result.output


def test_link_rejects_a_non_integer_milestone_number(config_path):
    result = run(
        "portal", "link", "--config", config_path, "coord", "not-a-number"
    )
    assert result.exit_code != 0
    assert "must be an integer" in result.output


# ── #2533 (ms-67 PB-3): pull an approved submission into a decomposition chat ──


def test_decompose_chat_is_registered():
    result = run("portal", "--help")
    assert result.exit_code == 0
    assert "decompose-chat" in result.output


def test_decompose_chat_prints_assignment_id(config_path):
    with patch(
        "coord.decomposition_chat.dispatch_decomposition_chat",
        return_value=("asg-789", "dellserver"),
    ) as mock_dispatch:
        result = run(
            "portal", "decompose-chat", "--config", config_path, "sub_2f6a1c"
        )
    assert result.exit_code == 0, result.output
    assert "asg-789" in result.output
    assert mock_dispatch.call_count == 1
    assert mock_dispatch.call_args.args[0] == "sub_2f6a1c"


def test_decompose_chat_honours_machine_override(config_path):
    with patch(
        "coord.decomposition_chat.dispatch_decomposition_chat",
        return_value=("asg-789", "elitebook"),
    ) as mock_dispatch:
        result = run(
            "portal",
            "decompose-chat",
            "--config",
            config_path,
            "sub_2f6a1c",
            "--machine",
            "elitebook",
        )
    assert result.exit_code == 0, result.output
    assert mock_dispatch.call_args.kwargs["machine_override"] == "elitebook"


def test_decompose_chat_reports_a_dispatch_failure_cleanly(config_path):
    with patch(
        "coord.decomposition_chat.dispatch_decomposition_chat",
        side_effect=RuntimeError("no single machine claims every repo"),
    ):
        result = run(
            "portal", "decompose-chat", "--config", config_path, "sub_2f6a1c"
        )
    assert result.exit_code != 0
    assert "no single machine claims every repo" in result.output


# ── #1982: the sync loop's operator surface ─────────────────────────────────


def test_sync_refuses_when_disabled(disabled_config_path):
    result = run("portal", "sync", "--config", disabled_config_path)
    assert result.exit_code != 0
    assert "not enabled" in result.output


def test_sync_runs_a_pass_and_reports_it(config_path, monkeypatch):
    """One full pass over a stubbed portal: pull, push, heartbeat."""
    from coord.portal_sync import enqueue_status

    enqueue_status("sub_1", "in-progress")

    def _get(url, params=None, headers=None, timeout=None):
        class _R:
            status_code = 200

            def json(self):
                return {"events": [], "cursor": "c1", "has_more": False}

        return _R()

    def _post(url, json=None, headers=None, timeout=None):
        class _R:
            status_code = 200

            def json(self):
                if url.endswith("/heartbeat"):
                    return {"ok": True}
                return {"results": [{"submission_id": "sub_1", "outcome": "applied"}]}

        return _R()

    monkeypatch.setattr("httpx.get", _get)
    monkeypatch.setattr("httpx.post", _post)
    result = run("portal", "sync", "--config", config_path)
    assert result.exit_code == 0, result.output
    assert "applied=1" in result.output
    assert "heartbeat=ok" in result.output


def test_enqueue_status_refuses_an_announcement_with_nothing_to_announce():
    """#835: `awaiting-signoff` emails the customer — it must have content."""
    result = run("portal", "enqueue-status", "sub_1", "awaiting-signoff")
    assert result.exit_code != 0
    assert "design_round" in result.output


def test_enqueue_design_round_then_status_queues_both_in_order():
    ok = run(
        "portal", "enqueue-design-round", "sub_1", '{"round": 1, "outcome": "x"}'
    )
    assert ok.exit_code == 0, ok.output
    assert "seq=1" in ok.output

    status = run("portal", "enqueue-status", "sub_1", "awaiting-signoff")
    assert status.exit_code == 0, status.output
    assert "seq=2" in status.output

    listed = run("portal", "outbox")
    assert listed.exit_code == 0
    assert "design_round" in listed.output
    assert "HELD" in listed.output  # the announcement, until its round applies


def test_enqueue_design_round_rejects_invalid_json():
    result = run("portal", "enqueue-design-round", "sub_1", "{not json")
    assert result.exit_code != 0
    assert "not valid JSON" in result.output


def test_enqueue_status_refuses_quality_check_with_no_preview():
    """#2359: same #835 discipline, for the preview-approval gate."""
    result = run("portal", "enqueue-status", "sub_1", "quality-check")
    assert result.exit_code != 0
    assert "preview" in result.output


def test_enqueue_preview_then_status_queues_both_in_order():
    ok = run(
        "portal", "enqueue-preview", "sub_1", "https://pr-42.natal-chart.pages.dev"
    )
    assert ok.exit_code == 0, ok.output
    assert "seq=1" in ok.output

    status = run("portal", "enqueue-status", "sub_1", "quality-check")
    assert status.exit_code == 0, status.output
    assert "seq=2" in status.output

    listed = run("portal", "outbox")
    assert listed.exit_code == 0
    assert "preview" in listed.output
    assert "HELD" in listed.output  # the announcement, until its preview applies


def test_enqueue_preview_rejects_an_empty_url():
    result = run("portal", "enqueue-preview", "sub_1", "")
    assert result.exit_code != 0
    assert "non-empty" in result.output


def test_outbox_is_empty_by_default():
    result = run("portal", "outbox")
    assert result.exit_code == 0
    assert "outbox: empty" in result.output


def test_events_reports_nothing_by_default():
    result = run("portal", "events")
    assert result.exit_code == 0
    assert "no unhandled portal events" in result.output


def test_requeue_reports_an_unknown_row_cleanly():
    result = run("portal", "requeue", "sub_1", "1")
    assert result.exit_code != 0
    assert "no outbox row" in result.output


# ── #2659: backfill a mirror clobbered by the (since-fixed) #2585 bug ──────


def _seed_damaged_mirror(sub_id: str) -> None:
    """Reproduce the issue's exact evidence: `portal_events` intact, but
    `customer_json` folded through the pre-#2585 broken `_mirror_event` —
    every fact nested under `"payload"`, and the second event's envelope
    CLOBBERING the first's wholesale rather than merging into it."""
    from coord import portal_store

    portal_store.record_events(
        [
            {
                "id": f"{sub_id}-e1",
                "submission_id": sub_id,
                "type": "submission.created",
                "revision": 1,
                "payload": {
                    "outcome": "a stick figure website",
                    "audience": "my kid",
                    "done_definition": "it loads",
                },
            },
            {
                "id": f"{sub_id}-e2",
                "submission_id": sub_id,
                "type": "signoff.approved",
                "revision": 2,
                "payload": {"verdict": "approved", "round": 1, "comment": None},
            },
        ]
    )
    portal_store.replace_customer_json(
        sub_id, {"payload": {"verdict": "approved", "round": 1, "comment": None}}
    )


def test_remirror_rebuilds_a_clobbered_mirror():
    _seed_damaged_mirror("SUB-1")

    result = run("portal", "remirror", "SUB-1")
    assert result.exit_code == 0, result.output
    assert "remirrored 1 submission(s), 1 changed" in result.output

    from coord import portal_store

    record = portal_store.get_submission("SUB-1")
    assert record is not None
    assert record.customer["outcome"] == "a stick figure website"
    assert record.customer["audience"] == "my kid"
    assert record.customer["verdict"] == "approved"
    assert "payload" not in record.customer


def test_remirror_dry_run_writes_nothing():
    _seed_damaged_mirror("SUB-2")

    result = run("portal", "remirror", "--dry-run", "SUB-2")
    assert result.exit_code == 0, result.output
    assert "SUB-2: CHANGED" in result.output
    assert "dry-run" in result.output

    from coord import portal_store

    record = portal_store.get_submission("SUB-2")
    assert record is not None
    assert record.customer == {
        "payload": {"verdict": "approved", "round": 1, "comment": None}
    }


def test_remirror_with_no_arguments_covers_every_submission():
    _seed_damaged_mirror("SUB-A")
    _seed_damaged_mirror("SUB-B")

    result = run("portal", "remirror")
    assert result.exit_code == 0, result.output
    assert "remirrored 2 submission(s), 2 changed" in result.output

    from coord import portal_store

    for sub_id in ("SUB-A", "SUB-B"):
        record = portal_store.get_submission(sub_id)
        assert record is not None
        assert record.customer["outcome"] == "a stick figure website"


def test_remirror_reports_an_unknown_submission_cleanly():
    result = run("portal", "remirror", "SUB-NOPE")
    assert result.exit_code == 0
    assert "no events on file" in result.output


def test_remirror_is_idempotent_once_clean():
    """A second pass over an already-correct mirror reports 0 changed —
    remirror is a repair, not a perpetual toggle."""
    _seed_damaged_mirror("SUB-3")
    run("portal", "remirror", "SUB-3")

    result = run("portal", "remirror", "SUB-3")
    assert result.exit_code == 0, result.output
    assert "remirrored 1 submission(s), 0 changed" in result.output


# ── #2513 (PDR-5): manual "publish mocks to portal" ─────────────────────────


class _UploadResponse:
    """Minimal `httpx.Response` stand-in for `/api/bridge/upload` — mirrors
    `test_merge_queue.py`'s `_StubResponse`, needed here too since
    `publish-mocks` drives a real `PortalBridgeClient` over `httpx.post`."""

    def __init__(self, status_code=200, json_body=None, text="") -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = text or (str(json_body) if json_body is not None else "")

    def json(self):
        if self._json_body is None:
            raise ValueError("no body")
        return self._json_body


def _stub_get_issue(*, milestone_number: int | None = 9, title="Q3 push", body="Ship it."):
    def _get_issue(repo: str, number: int) -> dict:
        milestone = (
            {"number": milestone_number, "title": "ms title"}
            if milestone_number is not None
            else None
        )
        return {"title": title, "body": body, "milestone": milestone}

    return _get_issue


def _mock_bundle_dir(
    tmp_path,
    *,
    milestone_number: int | None = 9,
    issue_number: int | None = None,
    with_index: bool = False,
):
    """A local checkout at ``tmp_path/repo`` with a rendered Gate-A bundle
    on disk — the shape `publish-mocks` reads directly, no `gh` involved.

    Pass ``issue_number`` instead of ``milestone_number`` for a #2665
    one-off-issue bug-lane bundle (``tests/acceptance/issue-NN/``).
    """
    repo_dir = tmp_path / "repo"
    dirname = f"issue-{issue_number}" if issue_number is not None else f"ms-{milestone_number}"
    bundle_dir = repo_dir / "tests" / "acceptance" / dirname
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "contract.md").write_text("# contract\n")
    mocks_dir = bundle_dir / "mocks"
    mocks_dir.mkdir()
    (mocks_dir / "screen.html").write_text("<html>screen</html>")
    if with_index:
        # #2512: a master index page, if it exists, rides along automatically
        # — publish-mocks globs `mocks/*.html` rather than naming files.
        (mocks_dir / "index.html").write_text("<html>index</html>")
    return repo_dir


def _config_with_repo_path(tmp_path, repo_dir) -> str:
    path = tmp_path / "coordinator.yml"
    path.write_text(textwrap.dedent(f"""
        repos:
          - name: coord
            github: owner/coord
        machines:
          - name: dellserver
            host: dellserver
            repos: [coord]
            repo_paths:
              coord: {repo_dir}
        portal:
          enabled: true
          base_url: https://intake.heurontech.com
          bridge_client_id: id-123
          bridge_client_secret: secret-456
    """))
    return str(path)


def test_publish_mocks_is_registered():
    result = run("portal", "--help")
    assert result.exit_code == 0
    assert "publish-mocks" in result.output


def test_publish_mocks_refuses_when_disabled(disabled_config_path):
    result = run(
        "portal", "publish-mocks", "--config", disabled_config_path, "coord", "3"
    )
    assert result.exit_code != 0
    assert "not enabled" in result.output


def test_publish_mocks_rejects_unknown_repo(config_path):
    result = run("portal", "publish-mocks", "--config", config_path, "nope", "3")
    assert result.exit_code != 0
    assert "unknown repo" in result.output


def test_publish_mocks_errors_when_no_portal_link(config_path, monkeypatch):
    """Unlike PDR-3's merge-triggered push (fail-open — a no-op), a manually
    invoked command must say why it did nothing, naming the fix."""
    monkeypatch.setattr("coord.github_ops.get_issue", _stub_get_issue())
    result = run("portal", "publish-mocks", "--config", config_path, "coord", "3")
    assert result.exit_code != 0
    assert "coord portal link" in result.output


def test_publish_mocks_errors_when_milestone_less_issue_has_no_link(
    config_path, monkeypatch
):
    """#2665: a milestone-less tracking issue no longer fails outright —
    it falls back to an issue-scoped `coord portal link --issue` lookup,
    which (with none recorded here) reports the fix to run, naming
    `--issue`."""
    monkeypatch.setattr(
        "coord.github_ops.get_issue", _stub_get_issue(milestone_number=None)
    )
    result = run("portal", "publish-mocks", "--config", config_path, "coord", "3")
    assert result.exit_code != 0
    assert "issue #3" in result.output
    assert "--issue 3" in result.output


def test_publish_mocks_uploads_and_enqueues_for_an_issue_scoped_link(
    tmp_path, monkeypatch
):
    """#2665: the one-off-issue counterpart of
    `test_publish_mocks_uploads_and_enqueues` below — a milestone-less
    tracking issue, linked via `--issue`, publishes its
    `tests/acceptance/issue-NN/` bug-lane bundle."""
    from coord import portal_store

    repo_dir = _mock_bundle_dir(tmp_path, issue_number=3, with_index=True)
    cfg_path = _config_with_repo_path(tmp_path, repo_dir)
    portal_store.link_issue(repo_name="coord", issue_number=3, submission_id="sub_1")
    monkeypatch.setattr(
        "coord.github_ops.get_issue", _stub_get_issue(milestone_number=None)
    )

    seen_upload: dict = {}

    def _post(url, json=None, headers=None, timeout=None):
        seen_upload["files"] = (json or {}).get("files")
        return _UploadResponse(200, {"bundle_key": "bundles/sub_1/r1.tar"})

    monkeypatch.setattr("httpx.post", _post)

    result = run("portal", "publish-mocks", "--config", cfg_path, "coord", "3")
    assert result.exit_code == 0, result.output
    assert "published" in result.output
    assert "issue #3" in result.output
    assert "sub_1" in result.output
    assert set(seen_upload["files"]) == {
        "contract.md", "mocks/screen.html", "mocks/index.html",
    }

    rows = portal_store.outbox_for_submission("sub_1")
    assert len(rows) == 1
    assert rows[0].kind == "design_round"


def test_publish_mocks_errors_when_no_local_checkout(config_path, monkeypatch):
    from coord import portal_store

    portal_store.link_milestone(
        repo_name="coord", milestone_number=9, submission_id="sub_1"
    )
    monkeypatch.setattr("coord.github_ops.get_issue", _stub_get_issue())
    result = run("portal", "publish-mocks", "--config", config_path, "coord", "3")
    assert result.exit_code != 0
    assert "no local repo checkout" in result.output


def test_publish_mocks_errors_when_bundle_is_empty(tmp_path, monkeypatch):
    from coord import portal_store

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    cfg_path = _config_with_repo_path(tmp_path, repo_dir)
    portal_store.link_milestone(
        repo_name="coord", milestone_number=9, submission_id="sub_1"
    )
    monkeypatch.setattr("coord.github_ops.get_issue", _stub_get_issue())

    result = run("portal", "publish-mocks", "--config", cfg_path, "coord", "3")
    assert result.exit_code != 0
    assert "nothing to publish" in result.output


def test_publish_mocks_errors_on_non_utf8_mock_file(tmp_path, monkeypatch):
    """A stray non-UTF-8 file under ``mocks/`` must surface as a named CLI
    error (#2513 review follow-up), not a raw ``UnicodeDecodeError``
    traceback."""
    from coord import portal_store

    repo_dir = _mock_bundle_dir(tmp_path, milestone_number=9)
    (repo_dir / "tests" / "acceptance" / "ms-9" / "mocks" / "binary.html").write_bytes(
        b"\xff\xfe not valid utf-8"
    )
    cfg_path = _config_with_repo_path(tmp_path, repo_dir)
    portal_store.link_milestone(
        repo_name="coord", milestone_number=9, submission_id="sub_1"
    )
    monkeypatch.setattr("coord.github_ops.get_issue", _stub_get_issue())

    result = run("portal", "publish-mocks", "--config", cfg_path, "coord", "3")
    assert result.exit_code != 0
    assert "could not read local mock bundle" in result.output
    assert "binary.html" in result.output


def test_publish_mocks_uploads_and_enqueues(tmp_path, monkeypatch):
    from coord import portal_store

    repo_dir = _mock_bundle_dir(tmp_path, milestone_number=9, with_index=True)
    cfg_path = _config_with_repo_path(tmp_path, repo_dir)
    portal_store.link_milestone(
        repo_name="coord", milestone_number=9, submission_id="sub_1"
    )
    monkeypatch.setattr("coord.github_ops.get_issue", _stub_get_issue())

    seen_upload: dict = {}

    def _post(url, json=None, headers=None, timeout=None):
        seen_upload["url"] = url
        seen_upload["files"] = (json or {}).get("files")
        return _UploadResponse(200, {"bundle_key": "bundles/sub_1/r1.tar"})

    monkeypatch.setattr("httpx.post", _post)

    result = run("portal", "publish-mocks", "--config", cfg_path, "coord", "3")
    assert result.exit_code == 0, result.output
    assert "published" in result.output
    assert "sub_1" in result.output
    assert seen_upload["url"] == "https://intake.heurontech.com/api/bridge/upload"
    assert set(seen_upload["files"]) == {
        "contract.md", "mocks/screen.html", "mocks/index.html",
    }

    rows = portal_store.outbox_for_submission("sub_1")
    assert len(rows) == 1
    assert rows[0].kind == "design_round"
    assert rows[0].fields["design_round"]["bundle_key"] == "bundles/sub_1/r1.tar"


def test_publish_mocks_includes_uppercase_html_suffix(tmp_path, monkeypatch):
    """The `.html` suffix match is case-insensitive, matching the TUI's
    `gate_a_mocks_dir_exists_for` enablement gate (#2513 review follow-up).

    A `mocks/` dir holding only `SCREEN.HTML` enables the TUI menu item; if
    this command's glob were case-sensitive it would then die with "nothing
    to publish" — the exact enabled-button-does-nothing mismatch the gate
    tightening was written to close.
    """
    from coord import portal_store

    repo_dir = tmp_path / "repo"
    mocks_dir = repo_dir / "tests" / "acceptance" / "ms-9" / "mocks"
    mocks_dir.mkdir(parents=True)
    (mocks_dir / "SCREEN.HTML").write_text("<html>shouty</html>")
    cfg_path = _config_with_repo_path(tmp_path, repo_dir)
    portal_store.link_milestone(
        repo_name="coord", milestone_number=9, submission_id="sub_1"
    )
    monkeypatch.setattr("coord.github_ops.get_issue", _stub_get_issue())

    seen_upload: dict = {}

    def _post(url, json=None, headers=None, timeout=None):
        seen_upload["files"] = (json or {}).get("files")
        return _UploadResponse(200, {"bundle_key": "bundles/sub_1/r1.tar"})

    monkeypatch.setattr("httpx.post", _post)

    result = run("portal", "publish-mocks", "--config", cfg_path, "coord", "3")
    assert result.exit_code == 0, result.output
    assert set(seen_upload["files"]) == {"mocks/SCREEN.HTML"}


def test_publish_mocks_reports_upload_failure(tmp_path, monkeypatch):
    from coord import portal_store

    repo_dir = _mock_bundle_dir(tmp_path)
    cfg_path = _config_with_repo_path(tmp_path, repo_dir)
    portal_store.link_milestone(
        repo_name="coord", milestone_number=9, submission_id="sub_1"
    )
    monkeypatch.setattr("coord.github_ops.get_issue", _stub_get_issue())
    monkeypatch.setattr(
        "httpx.post", lambda *a, **k: _UploadResponse(401, {}, text="unauthorized")
    )

    result = run("portal", "publish-mocks", "--config", cfg_path, "coord", "3")
    assert result.exit_code != 0
    assert "upload failed" in result.output


# ── #2336: state-touching commands refuse to run on a thin client ──────────


@pytest.fixture
def thin_client(monkeypatch):
    """Simulate running on a machine with board_service configured — i.e. a
    thin client, per coord/client.py's resolve_board_service() bootstrap
    contract (flag > env > ~/.coord/client.toml)."""
    monkeypatch.setenv("COORD_SERVICE_URL", "http://dellserver:7435")


@pytest.mark.parametrize(
    "args",
    [
        ("portal", "sync"),
        ("portal", "outbox"),
        ("portal", "events"),
        ("portal", "link", "coord", "3"),
        ("portal", "link", "coord", "3", "sub_1"),
        ("portal", "enqueue-status", "sub_1", "shipped"),
        ("portal", "enqueue-design-round", "sub_1", "{}"),
        ("portal", "enqueue-preview", "sub_1", "https://pr-1.example.pages.dev"),
        ("portal", "enqueue-question", "sub_1", "why?"),
        ("portal", "requeue", "sub_1", "1"),
        ("portal", "remirror"),
        ("portal", "remirror", "sub_1"),
        ("portal", "publish-mocks", "coord", "3"),
        ("portal", "decompose-chat", "sub_1"),
    ],
)
def test_state_touching_commands_refuse_on_a_thin_client(thin_client, args):
    result = run(*args)
    assert result.exit_code != 0
    assert "must run on the daemon host" in result.output
    assert "dellserver" in result.output


def test_status_heartbeat_and_push_do_not_call_the_thin_client_guard(thin_client):
    """status/heartbeat/push never touch the local DB, so they must not be
    gated by the #2336 guard — only sync/outbox/events/enqueue-*/requeue call
    _refuse_if_thin_client. (A full CLI invocation isn't used here: setting
    COORD_SERVICE_URL also reroutes _load_config's own config fetch to the
    daemon per #1080, which is unrelated to this guard and would make the
    assertion about a network error rather than about this guard.)"""
    import coord.commands.portal as portal_mod

    calls = []
    real_guard = portal_mod._refuse_if_thin_client

    def _tracking_guard(cmd_name):
        calls.append(cmd_name)
        return real_guard(cmd_name)

    portal_mod._refuse_if_thin_client = _tracking_guard
    try:
        result = run("portal", "heartbeat", "--config", "/does/not/exist.yml")
    finally:
        portal_mod._refuse_if_thin_client = real_guard
    assert result.exit_code != 0  # fails for an unrelated reason (bad --config)
    assert calls == []
