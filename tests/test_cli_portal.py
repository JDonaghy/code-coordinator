"""#2179: ``coord portal`` — status/heartbeat/push over the sync bridge client."""

from __future__ import annotations

import json
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
    for sub in ("status", "heartbeat", "push", "link", "ledger", "decision"):
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


def test_decompose_chat_names_the_pulled_status_that_disqualified_it(config_path):
    """#2996: the generic "is not a currently-approved portal submission"
    error named neither the cause nor the status that caused it. When the
    real cause is a `_PULLED_STATUSES` push on a submission with an
    `approved` sign-off — e.g. an operator's own `enqueue-status planned`
    pushed before the submission was actually decomposed — the failure now
    names that status and the `in-design` recovery, with no mock standing in
    for `dispatch_decomposition_chat` this time."""
    from coord import portal_store

    portal_store.record_events(
        [{"id": "e1", "submission_id": "sub_2f6a1c", "type": "signoff.approved"}],
        now=1.0,
    )
    row = portal_store.enqueue("sub_2f6a1c", "status", {"status": "planned"}, now=2.0)
    portal_store.mark_applied(row, now=2.0)

    result = run("portal", "decompose-chat", "--config", config_path, "sub_2f6a1c")
    assert result.exit_code != 0
    assert "'planned'" in result.output
    assert "in-design" in result.output


def test_decompose_chat_generic_message_when_never_approved_at_all(config_path):
    """No disqualifying status to name here — the submission simply has no
    approved sign-off on record, so the generic message stays generic.

    Matches the enrichment clause's own wording rather than the bare token
    "last_status" for the same reason as `WITHDRAWAL_WARNING` below: click
    `Result.output` interleaves stderr, so a loose token can be satisfied by
    unrelated CLI chatter that only appears in some environments.
    """
    result = run("portal", "decompose-chat", "--config", config_path, "sub_never_seen")
    assert result.exit_code != 0
    assert "is not a currently-approved portal submission" in result.output
    assert "its last_status is" not in result.output


# ── #2743: --wait blocks and prints the closing summary ────────────────────
#
# A CLI-dispatched decomposition-chat is issue_number=0 — no GitHub thread to
# post a completion comment to, and the assignment drops off `coord status`
# once it goes done. `--wait` polls the dispatch machine's own agent for
# completion, then fetches the log and prints the session's own closing
# report in full (previously only recoverable by hand-parsing `coord log
# --raw`'s NDJSON).


def _decompose_chat_ndjson(closing_text: str) -> bytes:
    """A minimal stream-json transcript ending in the closing assistant turn
    plus a terminal `result` event, mirroring a real decomposition-chat
    log."""
    lines = [
        json.dumps(
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"}
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "filing issues..."}]},
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": closing_text}]},
            }
        ),
        json.dumps(
            {
                "type": "result",
                "total_cost_usd": 0.5,
                "stop_reason": "end_turn",
                "num_turns": 2,
                "duration_ms": 1000,
            }
        ),
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_decompose_chat_wait_prints_completion_and_closing_summary(config_path):
    # #2743's motivating incident: the closing turn flagged a customer
    # round-trip need — the single most operationally important line the
    # session produced, and well over the old 100-char truncation.
    closing_text = (
        "Filed issue #501 and #502 under milestone ms-9, queued both via "
        "drive-queue, and recorded coord portal link — two of the six "
        "requested items reference a feature with zero references in the "
        "repo and need a customer round-trip before I can file them."
    )
    assert len(closing_text) > 100
    status_payload = {
        "completed": [
            {
                "id": "asg-789",
                "exit_code": 0,
                "started_at": 100,
                "finished_at": 142,
                "branch": "issue-0-decomposition-asg-789",
            }
        ],
        "active": [],
    }

    class _Resp:
        def json(self):
            return status_payload

    with (
        patch(
            "coord.decomposition_chat.dispatch_decomposition_chat",
            return_value=("asg-789", "dellserver"),
        ),
        patch("httpx.get", return_value=_Resp()),
        patch(
            "coord.network.fetch_log",
            return_value=(200, _decompose_chat_ndjson(closing_text)),
        ),
    ):
        result = run(
            "portal", "decompose-chat", "--config", config_path, "sub_2f6a1c", "--wait"
        )

    assert result.exit_code == 0, result.output
    assert "asg-789" in result.output
    assert "completed in 0m 42s" in result.output
    assert "closing summary" in result.output
    assert closing_text in result.output
    assert "…" not in result.output


def test_decompose_chat_wait_exits_nonzero_on_failed_session(config_path):
    # #2743 fix iteration 1: a FAILED session must fail the CLI's own exit
    # status, not just print "FAILED" — a script doing
    # `coord portal decompose-chat SUB --wait && next_step` has to be able
    # to see the failure without parsing stdout.
    status_payload = {
        "completed": [
            {
                "id": "asg-789",
                "exit_code": 1,
                "started_at": 100,
                "finished_at": 130,
                "branch": "issue-0-decomposition-asg-789",
                "error": "system prompt: coord portal link failed",
            }
        ],
        "active": [],
    }

    class _Resp:
        def json(self):
            return status_payload

    with (
        patch(
            "coord.decomposition_chat.dispatch_decomposition_chat",
            return_value=("asg-789", "dellserver"),
        ),
        patch("httpx.get", return_value=_Resp()),
        patch(
            "coord.network.fetch_log",
            return_value=(200, _decompose_chat_ndjson("could not link, aborting")),
        ),
    ):
        result = run(
            "portal", "decompose-chat", "--config", config_path, "sub_2f6a1c", "--wait"
        )

    assert result.exit_code == 1, result.output
    assert "FAILED (exit 1)" in result.output
    # The closing summary is still printed — failure isn't a reason to
    # withhold the one thing that explains *why*.
    assert "could not link, aborting" in result.output


def test_decompose_chat_wait_exits_nonzero_on_failed_session_even_if_log_fetch_fails(config_path):
    # Same as above, but the log fetch itself also fails — the exit status
    # must still reflect the FAILED session rather than silently returning 0
    # because the best-effort log-fetch fallback swallowed the exception.
    status_payload = {
        "completed": [
            {
                "id": "asg-789",
                "exit_code": 1,
                "started_at": 100,
                "finished_at": 130,
                "branch": "issue-0-decomposition-asg-789",
            }
        ],
        "active": [],
    }

    class _Resp:
        def json(self):
            return status_payload

    with (
        patch(
            "coord.decomposition_chat.dispatch_decomposition_chat",
            return_value=("asg-789", "dellserver"),
        ),
        patch("httpx.get", return_value=_Resp()),
        patch("coord.network.fetch_log", side_effect=OSError("connection refused")),
    ):
        result = run(
            "portal", "decompose-chat", "--config", config_path, "sub_2f6a1c", "--wait"
        )

    assert result.exit_code == 1, result.output
    assert "FAILED (exit 1)" in result.output


def test_decompose_chat_wait_times_out(config_path):
    with (
        patch(
            "coord.decomposition_chat.dispatch_decomposition_chat",
            return_value=("asg-789", "dellserver"),
        ),
        # Deterministic timeout: the deadline check sees time already past
        # the deadline on its very first read, so the poll loop body never
        # runs (and httpx.get is never called) — no real sleeping needed.
        patch("time.monotonic", side_effect=[0, 100]),
    ):
        result = run(
            "portal", "decompose-chat", "--config", config_path, "sub_2f6a1c",
            "--wait", "--timeout", "5",
        )

    assert result.exit_code == 3
    assert "timed out" in result.output


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


# ── #2996: `enqueue-status` warns before a `_PULLED_STATUSES` push that ────
# would silently withdraw a submission from decomposition, on a submission
# with no linked milestone/issue and (so, by construction here) no
# decomposition on record. It must never refuse — only warn.

#: The #2996 warning's own distinctive opening clause.
#:
#: Assertions below match on THIS rather than on the bare token "warning",
#: because under click >= 8.2 ``Result.output`` is no longer a proxy for
#: stdout — it is an interleaved stdout+stderr stream (see
#: ``click.testing.Result.output``). A bare ``"warning" not in
#: result.output.lower()`` therefore also asserts the absence of every
#: *unrelated* advisory the CLI may write to stderr in some other
#: environment: ``coord/cli.py``'s "warning: coord CLI is running from a
#: non-editable install …" banner, ``coord/commands/portal.py``'s "warning:
#: backstop failed to record intake-session exit", a best-effort store
#: failure line, and so on. None of those are under test here, and none of
#: them fire on a developer box while several can fire on a CI runner — so
#: the loose form is green locally and red in CI for no product reason.
#: Matching the specific clause keeps the assertion about the behaviour the
#: test names.
WITHDRAWAL_WARNING = "has no linked milestone/issue on file"


@pytest.mark.parametrize("status", ["planned", "in-progress", "shipped"])
def test_enqueue_status_warns_before_withdrawing_an_unlinked_submission(status):
    result = run("portal", "enqueue-status", "sub_1", status)
    assert result.exit_code == 0, result.output
    assert WITHDRAWAL_WARNING in result.output
    assert "warning:" in result.output
    # Names the consequence in operator terms, not just the constant's name.
    assert "Approved work items" in result.output
    assert "decompose-chat" in result.output
    # Names the alternative for the "not started yet" case.
    assert "in-design" in result.output
    # And the push still went through.
    assert "queued:" in result.output


def test_enqueue_status_does_not_warn_for_a_non_pulled_status():
    result = run("portal", "enqueue-status", "sub_1", "in-design")
    assert result.exit_code == 0, result.output
    assert WITHDRAWAL_WARNING not in result.output


def test_enqueue_status_does_not_warn_once_a_link_is_on_file(config_path):
    """A linked submission has actually been pulled into decomposition — the
    #2996 warning exists for the case where it has NOT, so it must not fire
    once `coord portal link` is on record."""
    from coord import portal_store

    portal_store.link_milestone(
        repo_name="coord", milestone_number=1, submission_id="sub_linked", actor="tester"
    )
    result = run("portal", "enqueue-status", "sub_linked", "planned")
    assert result.exit_code == 0, result.output
    assert WITHDRAWAL_WARNING not in result.output


def test_enqueue_status_still_warns_even_when_the_push_itself_is_refused():
    """The warning is about the *consequence of applying* the status, which
    is independent of the #835 ordering guard's own separate refusal
    (`quality-check` with no preview queued yet) — both fire together
    rather than the ordering guard's exit code swallowing the warning."""
    result = run("portal", "enqueue-status", "sub_1", "quality-check")
    assert result.exit_code != 0
    assert WITHDRAWAL_WARNING in result.output
    assert "preview" in result.output


def test_outbox_is_empty_by_default():
    result = run("portal", "outbox")
    assert result.exit_code == 0
    assert "outbox: empty" in result.output


# ── #2749 (IL-3): the running-context ledger ────────────────────────────────


def test_ledger_reports_nothing_by_default():
    result = run("portal", "ledger", "sub_1")
    assert result.exit_code == 0
    assert "(none)" in result.output


def test_ledger_renders_paired_qa_and_decisions():
    from coord import portal_store

    row = portal_store.enqueue("sub_1", "question", {"question": "Offline-first?"})
    portal_store.mark_applied(row)
    portal_store.append_ledger_entry(
        "sub_1",
        portal_store.LEDGER_KIND_QUESTION_ANSWERED,
        question_revision=row.revision,
        text="Yes.",
        actor="jane",
        source_event_id="e1",
    )
    portal_store.propose_decision("sub_1", "Ship offline-first v1", actor="agent-1")
    rejected = portal_store.propose_decision("sub_1", "Native app", actor="agent-1")
    portal_store.reject_decision("sub_1", rejected.seq, "customer wants web-only")

    result = run("portal", "ledger", "sub_1")
    assert result.exit_code == 0
    assert "Offline-first?" in result.output
    assert "Yes." in result.output
    assert "(by jane)" in result.output
    assert "Ship offline-first v1" in result.output
    assert "REJECTED: customer wants web-only" in result.output


def test_ledger_json_shape():
    from coord import portal_store

    row = portal_store.enqueue("sub_1", "question", {"question": "Offline-first?"})
    portal_store.mark_applied(row)

    result = run("portal", "ledger", "sub_1", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["submission_id"] == "sub_1"
    [qa] = payload["qa"]
    assert qa["question"] == "Offline-first?"
    assert qa["answers"] == []


def test_ledger_shows_an_unanswered_question_as_open():
    from coord import portal_store

    row = portal_store.enqueue("sub_1", "question", {"question": "SQLite or Postgres?"})
    portal_store.mark_applied(row)

    result = run("portal", "ledger", "sub_1")
    assert result.exit_code == 0
    assert "unanswered" in result.output


# ── #2749 (IL-3): decision propose/confirm/reject/supersede ────────────────


def test_decision_propose_confirm_reject_supersede_round_trip():
    proposed = run("portal", "decision", "propose", "sub_1", "Use SQLite")
    assert proposed.exit_code == 0
    assert "proposed: sub_1 #1" in proposed.output

    confirmed = run("portal", "decision", "confirm", "sub_1", "1")
    assert confirmed.exit_code == 0
    assert "confirmed: sub_1 #1" in confirmed.output

    proposed2 = run("portal", "decision", "propose", "sub_1", "Use Postgres instead")
    assert proposed2.exit_code == 0

    superseded = run("portal", "decision", "supersede", "sub_1", "1", "2")
    assert superseded.exit_code == 0
    assert "superseded: sub_1 #1 -> #2" in superseded.output

    rejected_no_reason = run("portal", "decision", "reject", "sub_1", "2", "")
    assert rejected_no_reason.exit_code != 0
    assert "reason" in rejected_no_reason.output

    rejected = run("portal", "decision", "reject", "sub_1", "2", "overkill for v1")
    assert rejected.exit_code == 0
    assert "rejected: sub_1 #2" in rejected.output
    assert "overkill for v1" in rejected.output


def test_decision_confirm_unknown_seq_reports_cleanly():
    result = run("portal", "decision", "confirm", "sub_1", "999")
    assert result.exit_code != 0
    assert "no decision" in result.output


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
        ("portal", "enqueue-design-round", "sub_1", "{}"),
        ("portal", "enqueue-preview", "sub_1", "https://pr-1.example.pages.dev"),
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


# ── #2995: `enqueue-question`/`enqueue-status` — the Ask move's exit ───────
#
# Unlike every command in the parametrized test above, these two no longer
# call `_refuse_if_thin_client` at all — they route through the daemon on a
# thin client that claims SUBMISSION_ID's mapped repo(s)
# (`_refuse_unless_claiming_machine`), and only refuse outright when that
# machine does NOT claim them (still exercised below — this widens the
# routed set, it does not remove the guard).


def test_enqueue_status_refuses_on_a_non_claiming_thin_client(thin_client, monkeypatch):
    import coord.commands.portal as portal_mod

    monkeypatch.setattr(portal_mod, "_load_config", lambda *a, **k: object())
    monkeypatch.setattr(
        "coord.decomposition_chat.resolve_approved_submission",
        lambda cfg, sid: {"submission_id": sid, "repos": ["web"]},
    )
    monkeypatch.setattr("coord.test_orchestrator.local_machine", lambda cfg: None)

    result = run("portal", "enqueue-status", "sub_1", "in-design")
    assert result.exit_code != 0
    assert "does not claim" in result.output or "unconfigured" in result.output
    assert "dellserver" in result.output


def test_enqueue_status_refuses_when_submission_not_approved_on_a_thin_client(
    thin_client, monkeypatch
):
    import coord.commands.portal as portal_mod

    monkeypatch.setattr(portal_mod, "_load_config", lambda *a, **k: object())
    monkeypatch.setattr(
        "coord.decomposition_chat.resolve_approved_submission",
        lambda cfg, sid: None,
    )

    result = run("portal", "enqueue-status", "sub_1", "in-design")
    assert result.exit_code != 0
    assert "not a currently-approved" in result.output


def test_enqueue_status_does_not_call_the_thin_client_guard(thin_client, monkeypatch):
    """`enqueue-status` must never call `_refuse_if_thin_client` — only the
    narrower `_refuse_unless_claiming_machine` (#2995)."""
    import coord.commands.portal as portal_mod

    calls = []
    real_guard = portal_mod._refuse_if_thin_client

    def _tracking_guard(cmd_name):
        calls.append(cmd_name)
        return real_guard(cmd_name)

    portal_mod._refuse_if_thin_client = _tracking_guard
    monkeypatch.setattr(portal_mod, "_load_config", lambda *a, **k: object())
    monkeypatch.setattr(
        "coord.decomposition_chat.resolve_approved_submission",
        lambda cfg, sid: None,
    )
    try:
        result = run("portal", "enqueue-status", "sub_1", "in-design")
    finally:
        portal_mod._refuse_if_thin_client = real_guard
    assert result.exit_code != 0  # refused for an unrelated reason (not approved)
    assert calls == []


def test_enqueue_status_routes_through_the_daemon_on_a_claiming_thin_client(
    thin_client, monkeypatch
):
    """End-to-end: a claiming thin client's `enqueue-status` reaches
    `/portal-enqueue-status` instead of writing a local (wrong, empty) DB —
    the same shape as `test_decision_propose_routes_through_the_daemon_on_a_
    thin_client` above."""
    import coord.commands.portal as portal_mod
    from coord import client as cc

    monkeypatch.setattr(portal_mod, "_load_config", lambda *a, **k: object())
    monkeypatch.setattr(
        "coord.decomposition_chat.resolve_approved_submission",
        lambda cfg, sid: {"submission_id": sid, "repos": ["coord"]},
    )

    class _Machine:
        name = "elitebook"

        def can_work_on(self, repo_name):
            return repo_name == "coord"

    monkeypatch.setattr(
        "coord.test_orchestrator.local_machine", lambda cfg: _Machine()
    )

    calls = []

    def _fake_post_record(svc, path, payload, **kw):
        calls.append((path, payload))
        return {
            "row": {
                "id": 1, "submission_id": payload["submission_id"], "seq": 1,
                "revision": 1, "kind": "status",
                "fields_json": f'{{"status": "{payload["status"]}"}}',
                "announces": "", "requires_kind": "", "state": "pending",
                "reason": "", "attempts": 0, "enqueued_at": 100.0, "sent_at": None,
            }
        }

    monkeypatch.setattr(cc, "post_record", _fake_post_record)

    result = run("portal", "enqueue-status", "sub_1", "in-design")
    assert result.exit_code == 0, result.output
    assert "status=in-design" in result.output
    assert len(calls) == 1
    assert calls[0][0] == "/portal-enqueue-status"
    assert calls[0][1]["submission_id"] == "sub_1"
    assert calls[0][1]["status"] == "in-design"


def test_enqueue_question_refuses_on_a_non_claiming_thin_client(thin_client, monkeypatch):
    import coord.commands.portal as portal_mod

    monkeypatch.setattr(portal_mod, "_load_config", lambda *a, **k: object())
    monkeypatch.setattr(
        "coord.decomposition_chat.resolve_approved_submission",
        lambda cfg, sid: {"submission_id": sid, "repos": ["web"]},
    )
    monkeypatch.setattr("coord.test_orchestrator.local_machine", lambda cfg: None)

    result = run("portal", "enqueue-question", "sub_1", "why?")
    assert result.exit_code != 0
    assert "does not claim" in result.output or "unconfigured" in result.output
    assert "dellserver" in result.output


def test_enqueue_question_does_not_call_the_thin_client_guard(thin_client, monkeypatch):
    import coord.commands.portal as portal_mod

    calls = []
    real_guard = portal_mod._refuse_if_thin_client

    def _tracking_guard(cmd_name):
        calls.append(cmd_name)
        return real_guard(cmd_name)

    portal_mod._refuse_if_thin_client = _tracking_guard
    monkeypatch.setattr(portal_mod, "_load_config", lambda *a, **k: object())
    monkeypatch.setattr(
        "coord.decomposition_chat.resolve_approved_submission",
        lambda cfg, sid: None,
    )
    try:
        result = run("portal", "enqueue-question", "sub_1", "why?")
    finally:
        portal_mod._refuse_if_thin_client = real_guard
    assert result.exit_code != 0  # refused for an unrelated reason (not approved)
    assert calls == []


def test_enqueue_question_routes_through_the_daemon_atomically_on_a_claiming_thin_client(
    thin_client, monkeypatch
):
    """End-to-end: a claiming thin client's `enqueue-question` reaches
    `/portal-enqueue-question` exactly ONCE — the #2995 design-note
    requirement that the question row and its `needs-input` announcement
    (#2901) are applied atomically across the seam, not as two separate
    routed calls a crash (or a thin client) could observe apart."""
    import coord.commands.portal as portal_mod
    from coord import client as cc

    monkeypatch.setattr(portal_mod, "_load_config", lambda *a, **k: object())
    monkeypatch.setattr(
        "coord.decomposition_chat.resolve_approved_submission",
        lambda cfg, sid: {"submission_id": sid, "repos": ["coord"]},
    )

    class _Machine:
        name = "elitebook"

        def can_work_on(self, repo_name):
            return repo_name == "coord"

    monkeypatch.setattr(
        "coord.test_orchestrator.local_machine", lambda cfg: _Machine()
    )

    calls = []

    def _fake_post_record(svc, path, payload, **kw):
        calls.append((path, payload))
        assert path == "/portal-enqueue-question"
        return {
            "question_row": {
                "id": 1, "submission_id": payload["submission_id"], "seq": 1,
                "revision": 1, "kind": "question",
                "fields_json": f'{{"question": "{payload["question"]}"}}',
                "announces": "", "requires_kind": "", "state": "draft",
                "reason": "", "attempts": 0, "enqueued_at": 100.0, "sent_at": None,
            },
            "status_row": {
                "id": 2, "submission_id": payload["submission_id"], "seq": 2,
                "revision": 2, "kind": "status",
                "fields_json": '{"status": "needs-input"}',
                "announces": "needs-input", "requires_kind": "question",
                "state": "pending", "reason": "", "attempts": 0,
                "enqueued_at": 100.0, "sent_at": None,
            },
        }

    monkeypatch.setattr(cc, "post_record", _fake_post_record)

    result = run("portal", "enqueue-question", "sub_1", "which blue?")
    assert result.exit_code == 0, result.output
    assert "question" in result.output
    assert "status=needs-input" in result.output
    # Exactly ONE request carried both rows — never two separate round trips.
    assert len(calls) == 1
    assert calls[0][0] == "/portal-enqueue-question"
    assert calls[0][1] == {"submission_id": "sub_1", "question": "which blue?"}


def test_enqueue_status_shows_a_clean_error_when_the_daemon_rejects_the_write(
    thin_client, monkeypatch
):
    """#2995 fix-round: the daemon answers a routed rejection (e.g. an
    announcing status with nothing queued — the exact `PortalSyncError` case
    a daemon-host caller sees) with a 400, which `client.post_record` raises
    as `httpx.HTTPStatusError`, not `PortalSyncError`. Before this fix the
    CLI's `except PortalSyncError` let it through as a raw traceback; a
    claiming thin client must see the same clean red message + exit 1 a
    daemon-host caller gets for the identical mistake."""
    import httpx

    import coord.commands.portal as portal_mod
    from coord import client as cc

    monkeypatch.setattr(portal_mod, "_load_config", lambda *a, **k: object())
    monkeypatch.setattr(
        "coord.decomposition_chat.resolve_approved_submission",
        lambda cfg, sid: {"submission_id": sid, "repos": ["coord"]},
    )

    class _Machine:
        name = "elitebook"

        def can_work_on(self, repo_name):
            return repo_name == "coord"

    monkeypatch.setattr("coord.test_orchestrator.local_machine", lambda cfg: _Machine())

    # Go through the real `client.post_record` (rather than faking it
    # outright) so `_reraise_with_detail` (#2907) actually runs and folds
    # the daemon's `{"error": ...}` body into the exception's message,
    # exactly like the real daemon response this fix has to survive.
    def _fake_httpx_post(url, *, json, headers, timeout):
        request = httpx.Request("POST", url)
        return httpx.Response(
            400,
            json={
                "error": "bad portal-enqueue-status: refusing to queue "
                "status 'needs-input' for sub_1: it emails the customer "
                "about a question and none has been queued",
            },
            request=request,
        )

    monkeypatch.setattr(cc.httpx, "post", _fake_httpx_post)

    result = run("portal", "enqueue-status", "sub_1", "needs-input")
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "refusing to queue status" in result.output


def test_enqueue_question_shows_a_clean_error_when_the_daemon_rejects_the_write(
    thin_client, monkeypatch
):
    """Same fix, `enqueue-question` half — see the matching `enqueue-status`
    test right above for the full rationale."""
    import httpx

    import coord.commands.portal as portal_mod
    from coord import client as cc

    monkeypatch.setattr(portal_mod, "_load_config", lambda *a, **k: object())
    monkeypatch.setattr(
        "coord.decomposition_chat.resolve_approved_submission",
        lambda cfg, sid: {"submission_id": sid, "repos": ["coord"]},
    )

    class _Machine:
        name = "elitebook"

        def can_work_on(self, repo_name):
            return repo_name == "coord"

    monkeypatch.setattr("coord.test_orchestrator.local_machine", lambda cfg: _Machine())

    # See the matching `enqueue-status` test above: go through the real
    # `client.post_record` so `_reraise_with_detail` (#2907) folds the
    # daemon's error body into the raised exception's message.
    def _fake_httpx_post(url, *, json, headers, timeout):
        request = httpx.Request("POST", url)
        return httpx.Response(
            400,
            json={"error": "bad portal-enqueue-question: sub_1 is not approved"},
            request=request,
        )

    monkeypatch.setattr(cc.httpx, "post", _fake_httpx_post)

    result = run("portal", "enqueue-question", "sub_1", "which blue?")
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "is not approved" in result.output


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


# ── #2751: `link` routes through the daemon instead of refusing ────────────


def test_link_does_not_call_the_thin_client_guard(thin_client, monkeypatch):
    """Unlike sync/outbox/events/enqueue-*/requeue/publish-mocks, `link` now
    routes its read/write through `/portal-link` instead of refusing — it
    must never call `_refuse_if_thin_client` at all."""
    import coord.commands.portal as portal_mod

    calls = []
    real_guard = portal_mod._refuse_if_thin_client

    def _tracking_guard(cmd_name):
        calls.append(cmd_name)
        return real_guard(cmd_name)

    portal_mod._refuse_if_thin_client = _tracking_guard
    try:
        # Fails for an unrelated reason (COORD_SERVICE_URL points nowhere
        # reachable) — the point is only that the guard itself is never hit.
        result = run("portal", "link", "coord", "3", "--config", "/does/not/exist.yml")
    finally:
        portal_mod._refuse_if_thin_client = real_guard
    assert result.exit_code != 0
    assert calls == []


def test_link_write_and_read_route_through_the_daemon_on_a_thin_client(
    config_path, monkeypatch
):
    """End-to-end: a `coord portal link` write, then read, both succeed from
    a simulated thin client — the ms-67 gap this issue fixes (a
    `type="decomposition-chat"` session dispatched to precision/elitebook
    could file issues but could not record the mandatory portal link)."""
    from coord import client as cc

    monkeypatch.setattr(
        cc,
        "resolve_board_service",
        lambda *a, **k: cc.ServiceConfig("http://dellserver:7435"),
    )
    monkeypatch.setattr(cc, "fetch_remote_config", lambda svc, **k: config_path)

    store: dict = {}

    def _fake_post_record(svc, path, payload, **kw):
        assert path == "/portal-link"
        store["link"] = payload["record"]
        return {"ok": True}

    def _fake_get(url, *, params, headers, timeout):
        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                link = store.get("link")
                matches = link is not None and all(
                    link.get(k) == v for k, v in params.items()
                )
                return {"link": link if matches else None}

        return _Resp()

    monkeypatch.setattr(cc, "post_record", _fake_post_record)
    monkeypatch.setattr(cc.httpx, "get", _fake_get)

    write = run("portal", "link", "coord", "3", "sub_abc123")
    assert write.exit_code == 0, write.output
    assert "linked" in write.output
    assert "sub_abc123" in write.output

    read = run("portal", "link", "coord", "3")
    assert read.exit_code == 0, read.output
    assert "submission_id=sub_abc123" in read.output


# ── #2749 (IL-3): `decision` routes through the daemon instead of refusing ──


def test_decision_propose_does_not_call_the_thin_client_guard(thin_client, monkeypatch):
    """Same #2751 exception as `link` above: an agent session's decision can
    land on any machine, so `decision propose` must never call
    `_refuse_if_thin_client` at all."""
    import coord.commands.portal as portal_mod
    from coord import client as cc

    calls = []
    real_guard = portal_mod._refuse_if_thin_client

    def _tracking_guard(cmd_name):
        calls.append(cmd_name)
        return real_guard(cmd_name)

    portal_mod._refuse_if_thin_client = _tracking_guard

    def _boom_post_record(svc, path, payload, **kw):
        raise RuntimeError("simulated unreachable daemon")

    monkeypatch.setattr(cc, "post_record", _boom_post_record)
    try:
        # Fails for an unrelated reason (the daemon POST above is stubbed to
        # raise) — the point is only that the guard itself is never hit.
        result = run("portal", "decision", "propose", "sub_1", "Use SQLite")
    finally:
        portal_mod._refuse_if_thin_client = real_guard
    assert result.exit_code != 0
    assert calls == []


def test_ledger_does_not_call_the_thin_client_guard(thin_client, monkeypatch):
    """`ledger` is explicitly NOT thin-client-refused (#2749's "any machine"
    requirement) — it routes a GET to `/portal-ledger` instead."""
    import coord.commands.portal as portal_mod
    from coord import client as cc

    calls = []
    real_guard = portal_mod._refuse_if_thin_client

    def _tracking_guard(cmd_name):
        calls.append(cmd_name)
        return real_guard(cmd_name)

    portal_mod._refuse_if_thin_client = _tracking_guard

    def _boom_get(url, *, params, headers, timeout):
        raise RuntimeError("simulated unreachable daemon")

    monkeypatch.setattr(cc.httpx, "get", _boom_get)
    try:
        result = run("portal", "ledger", "sub_1")
    finally:
        portal_mod._refuse_if_thin_client = real_guard
    assert result.exit_code != 0
    assert calls == []


def test_ledger_routes_through_the_daemon_on_a_thin_client(thin_client, monkeypatch):
    """End-to-end: `coord portal ledger` from a simulated thin client reads
    `/portal-ledger` and renders the exact payload the daemon returns."""
    from coord import client as cc

    payload = {
        "submission_id": "sub_1",
        "qa": [{"question_revision": 1, "question": "Offline-first?", "answers": []}],
        "unpaired_answers": [],
        "decisions": [],
        "archived_decisions": [],
        "narrative": "",
    }

    def _fake_get(url, *, params, headers, timeout):
        assert url.endswith("/portal-ledger")
        assert params == {"submission_id": "sub_1"}

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"payload": payload}

        return _Resp()

    monkeypatch.setattr(cc.httpx, "get", _fake_get)

    result = run("portal", "ledger", "sub_1")
    assert result.exit_code == 0, result.output
    assert "Offline-first?" in result.output
    assert "(unanswered — needs-input)" in result.output

    as_json = run("portal", "ledger", "sub_1", "--json")
    assert as_json.exit_code == 0
    assert json.loads(as_json.output) == payload


def test_decision_propose_routes_through_the_daemon_on_a_thin_client(
    thin_client, monkeypatch
):
    """End-to-end: `coord portal decision propose` from a simulated thin
    client reaches `/portal-decision` instead of writing a local (wrong)
    DB — the same shape as `test_link_write_and_read_route_through_the_daemon
    _on_a_thin_client` above."""
    from coord import client as cc

    calls = []

    def _fake_post_record(svc, path, payload, **kw):
        calls.append((path, payload))
        assert path == "/portal-decision"
        return {
            "entry": {
                "id": 1, "submission_id": payload["submission_id"], "seq": 1,
                "text": payload["text"], "state": "proposed", "reason": "",
                "superseded_by_seq": None, "actor": payload.get("actor", ""),
                "recorded_at": 100.0, "updated_at": 100.0,
            }
        }

    monkeypatch.setattr(cc, "post_record", _fake_post_record)

    result = run("portal", "decision", "propose", "sub_1", "Use SQLite")
    assert result.exit_code == 0, result.output
    assert "proposed: sub_1 #1" in result.output
    assert len(calls) == 1
    assert calls[0][1]["action"] == "propose"


# ── #2903 (phase 1 of #2902): the draft gate ────────────────────────────────


@pytest.fixture(autouse=True)
def default_approval_policy(tmp_path, monkeypatch):
    """Pin `portal.approval` to ABSENT for this module (#2903).

    The `enqueue-*` / `drafts` / `draft *` commands take no `--config`; they
    read whatever `config.load()` resolves, which on a real fleet machine is
    `~/.coord/coordinator.yml`. Without this, "does the default policy gate a
    design round?" would be answered by whatever the operator has configured
    that day — a test that passes here and fails on `dellserver`. Pointing
    `$COORD_CONFIG` at a config with a `portal:` block and deliberately NO
    `approval:` key makes the answer the built-in default, everywhere.

    Autouse but harmless to the rest of the module: every pre-existing test
    either passes `--config` explicitly (which wins) or never loads a config
    at all.
    """
    path = tmp_path / "policy-coordinator.yml"
    path.write_text(DISABLED_CONFIG_YAML)
    monkeypatch.setenv("COORD_CONFIG", str(path))
    return str(path)



def test_draft_gate_commands_are_registered():
    result = run("portal", "--help")
    assert result.exit_code == 0
    assert "drafts" in result.output
    assert "draft" in result.output

    sub = run("portal", "draft", "--help")
    assert sub.exit_code == 0
    for verb in ("edit", "approve", "reject"):
        assert verb in sub.output


def test_drafts_is_empty_by_default():
    result = run("portal", "drafts")
    assert result.exit_code == 0
    assert "none awaiting approval" in result.output


def test_an_enqueued_design_round_lands_in_draft_under_the_default_policy():
    """No config block anywhere: the built-in default must gate it."""
    ok = run(
        "portal", "enqueue-design-round", "sub_1",
        '{"round": 1, "outcome_definition": "a booking form"}',
    )
    assert ok.exit_code == 0, ok.output
    assert "drafted (awaiting approval)" in ok.output

    from coord import portal_store

    assert portal_store.pending_outbox() == []
    assert [r.seq for r in portal_store.draft_outbox()] == [1]


def test_an_enqueued_status_still_queues_exactly_as_before():
    ok = run("portal", "enqueue-status", "sub_1", "in-design")
    assert ok.exit_code == 0, ok.output
    assert "queued:" in ok.output
    assert "drafted" not in ok.output

    from coord import portal_store

    assert [r.seq for r in portal_store.pending_outbox()] == [1]


def test_drafts_lists_the_full_prose_per_submission():
    run("portal", "enqueue-question", "sub_1", "which shade of blue?")
    result = run("portal", "drafts")
    assert result.exit_code == 0
    assert "sub_1" in result.output
    assert "seq=1" in result.output
    assert "which shade of blue?" in result.output
    assert "(editable)" in result.output


def test_drafts_can_be_scoped_to_one_submission():
    run("portal", "enqueue-question", "sub_1", "blue?")
    run("portal", "enqueue-question", "sub_2", "green?")
    result = run("portal", "drafts", "sub_2")
    assert result.exit_code == 0
    assert "green?" in result.output
    assert "blue?" not in result.output


def test_drafts_json_carries_the_fields():
    run("portal", "enqueue-question", "sub_1", "which blue?")
    result = run("portal", "drafts", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["drafts"][0]["kind"] == "question"
    assert payload["drafts"][0]["fields"]["question"] == "which blue?"


def test_outbox_surfaces_a_draft_so_the_queue_never_looks_idle():
    run("portal", "enqueue-question", "sub_1", "which blue?")
    result = run("portal", "outbox")
    assert result.exit_code == 0
    assert "DRAFT" in result.output


def test_draft_edit_rewrites_the_prose_and_ledgers_both_versions():
    run("portal", "enqueue-question", "sub_1", "Whomst shall we ask re: blue?")
    result = run("portal", "draft", "edit", "sub_1", "1", "--text", "Which blue?")
    assert result.exit_code == 0, result.output
    assert "still draft" in result.output

    from coord import portal_store

    row = portal_store.get_outbox_row("sub_1", 1)
    assert row.fields["question"] == "Which blue?"
    assert row.state == portal_store.STATE_DRAFT
    entry = [
        e
        for e in portal_store.ledger_for_submission("sub_1")
        if e.kind == portal_store.LEDGER_KIND_DRAFT_EDITED
    ][0]
    assert entry.payload["agent_text"] == "Whomst shall we ask re: blue?"
    assert entry.payload["operator_text"] == "Which blue?"


def test_draft_edit_refuses_a_non_editable_field():
    run(
        "portal", "enqueue-design-round", "sub_1",
        '{"outcome_definition": "a form", "bundle_key": "r2://b/1"}',
    )
    result = run(
        "portal", "draft", "edit", "sub_1", "1",
        "--field", "design_round.bundle_key", "--text", "r2://elsewhere",
    )
    assert result.exit_code != 0
    assert "not editable" in result.output
    assert "design_round.outcome_definition" in result.output


def test_draft_edit_refuses_a_row_that_has_already_left_the_gate():
    run("portal", "enqueue-question", "sub_1", "which blue?")
    run("portal", "draft", "approve", "sub_1", "1")
    result = run("portal", "draft", "edit", "sub_1", "1", "--text", "too late")
    assert result.exit_code != 0
    assert "not draft" in result.output


def test_draft_edit_refuses_an_unknown_row():
    result = run("portal", "draft", "edit", "sub_1", "9", "--text", "nope")
    assert result.exit_code != 0
    assert "no outbox row" in result.output


def test_draft_edit_opens_the_editor_when_no_text_is_given(monkeypatch):
    run("portal", "enqueue-question", "sub_1", "agent wording")
    seen = {}

    def _fake_edit(text):
        seen["seeded"] = text
        return "operator wording\n"

    monkeypatch.setattr("click.edit", _fake_edit)
    result = run("portal", "draft", "edit", "sub_1", "1")
    assert result.exit_code == 0, result.output
    assert seen["seeded"] == "agent wording"

    from coord import portal_store

    assert portal_store.get_outbox_row("sub_1", 1).fields["question"] == (
        "operator wording"
    )


def test_draft_edit_leaving_the_editor_unchanged_writes_nothing(monkeypatch):
    run("portal", "enqueue-question", "sub_1", "agent wording")
    monkeypatch.setattr("click.edit", lambda text: None)
    result = run("portal", "draft", "edit", "sub_1", "1")
    assert result.exit_code == 0
    assert "unchanged" in result.output

    from coord import portal_store

    assert portal_store.get_outbox_row("sub_1", 1).fields["question"] == "agent wording"


def test_draft_approve_flips_the_row_to_pending():
    run("portal", "enqueue-question", "sub_1", "which blue?")
    result = run("portal", "draft", "approve", "sub_1", "1")
    assert result.exit_code == 0, result.output
    assert "approved" in result.output

    from coord import portal_store

    assert [r.seq for r in portal_store.pending_outbox()] == [1, 2]
    assert portal_store.draft_outbox() == []


def test_draft_approve_refuses_a_row_that_is_not_a_draft():
    run("portal", "enqueue-status", "sub_1", "in-design")
    result = run("portal", "draft", "approve", "sub_1", "1")
    assert result.exit_code != 0
    assert "not draft" in result.output


def test_draft_reject_requires_a_reason():
    run("portal", "enqueue-question", "sub_1", "which blue?")
    result = run("portal", "draft", "reject", "sub_1", "1")
    assert result.exit_code != 0
    assert "reason" in result.output.lower()


def test_draft_reject_also_rejects_the_announcement_behind_it():
    run("portal", "enqueue-question", "sub_1", "which blue?")
    result = run(
        "portal", "draft", "reject", "sub_1", "1", "--reason", "answered at intake"
    )
    assert result.exit_code == 0, result.output
    assert "rejected" in result.output
    assert "also rejected: seq=2" in result.output

    from coord import portal_store

    assert [r.state for r in portal_store.outbox_for_submission("sub_1")] == [
        "rejected",
        "rejected",
    ]


def test_draft_reject_no_cascade_refuses_and_names_the_row():
    run("portal", "enqueue-question", "sub_1", "which blue?")
    result = run(
        "portal", "draft", "reject", "sub_1", "1",
        "--reason", "answered at intake", "--no-cascade",
    )
    assert result.exit_code != 0
    assert "seq=2" in result.output

    from coord import portal_store

    assert portal_store.draft_outbox()[0].seq == 1


@pytest.mark.parametrize(
    "args",
    [
        ("portal", "drafts"),
        ("portal", "draft", "edit", "sub_1", "1", "--text", "x"),
        ("portal", "draft", "approve", "sub_1", "1"),
        ("portal", "draft", "reject", "sub_1", "1", "--reason", "no"),
    ],
)
def test_draft_gate_commands_refuse_on_a_thin_client(thin_client, args):
    """Every outbox-touching command is a daemon-host command (#2336)."""
    result = run(*args)
    assert result.exit_code != 0
    assert "must run on the daemon host" in result.output
    assert "dellserver" in result.output


def test_a_configured_ungated_kind_queues_straight_through(tmp_path, monkeypatch):
    """#2903 end to end: coordinator.yml -> policy -> outbox state."""
    path = tmp_path / "ungated.yml"
    path.write_text(
        DISABLED_CONFIG_YAML
        + "portal:\n  approval:\n    question: false\n"
    )
    monkeypatch.setenv("COORD_CONFIG", str(path))

    ok = run("portal", "enqueue-question", "sub_1", "which blue?")
    assert ok.exit_code == 0, ok.output
    assert "queued:" in ok.output
    assert "drafted" not in ok.output

    from coord import portal_store

    assert [r.seq for r in portal_store.pending_outbox()] == [1, 2]
    assert portal_store.draft_outbox() == []


# ── #2867: `coord portal note` — the ledger's operator-context layer ─────────


def test_note_is_registered_on_the_portal_group():
    result = run("portal", "--help")
    assert result.exit_code == 0
    assert "note" in result.output


def test_note_records_operator_background_verbatim_and_attributed(monkeypatch):
    """The headline #2867 behaviour: what the operator knows gets a durable
    home on the ledger, stored exactly as typed and attributed to a human."""
    from coord import portal_store

    portal_store.seed_revision("sub_1", 1)
    monkeypatch.setattr("coord.commands.portal._actor", lambda: "jane")

    text = (
        "Spoke to client 2026-08-28: household of two, no logins needed; "
        "calendar is a nice-to-have."
    )
    result = run("portal", "note", "sub_1", text)
    assert result.exit_code == 0, result.output
    assert "noted: sub_1 #1" in result.output

    [entry] = portal_store.operator_notes_for_submission("sub_1")
    assert entry.text == text  # verbatim — not summarized, not reworded
    assert entry.actor == "operator:jane"
    assert entry.kind == portal_store.LEDGER_KIND_OPERATOR_NOTE


def test_note_rejects_an_unknown_submission_with_a_clear_error():
    from coord import portal_store

    result = run("portal", "note", "sub_nope", "background")
    assert result.exit_code == 2
    assert "unknown submission" in result.output
    assert "sub_nope" in result.output
    assert portal_store.ledger_for_submission("sub_nope") == []


def test_note_rejects_empty_text():
    from coord import portal_store

    portal_store.seed_revision("sub_1", 1)
    result = run("portal", "note", "sub_1", "   ")
    assert result.exit_code == 2
    assert "non-empty" in result.output
    assert portal_store.ledger_for_submission("sub_1") == []


def test_ledger_renders_operator_notes_attributed_and_in_seq_order(monkeypatch):
    """#2867's acceptance bar for the read side: `coord portal ledger` shows
    the note verbatim, under its own heading, attributed — and never as a
    decision."""
    from coord import portal_store

    portal_store.seed_revision("sub_1", 1)
    monkeypatch.setattr("coord.commands.portal._actor", lambda: "jane")
    run("portal", "note", "sub_1", "Household of two.")
    run("portal", "note", "sub_1", "Calendar is a nice-to-have.")

    result = run("portal", "ledger", "sub_1")
    assert result.exit_code == 0, result.output
    assert "## Operator notes" in result.output
    assert "[1] Household of two.  (by operator:jane)" in result.output
    assert "[2] Calendar is a nice-to-have.  (by operator:jane)" in result.output
    # Order is ledger seq order, notes never leak into the Decisions layer.
    assert result.output.index("Household of two.") < result.output.index(
        "Calendar is a nice-to-have."
    )
    decisions_section = result.output.split("## Decisions", 1)[1]
    assert "Household of two." not in decisions_section

    as_json = run("portal", "ledger", "sub_1", "--json")
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.output)
    assert [n["text"] for n in payload["operator_notes"]] == [
        "Household of two.",
        "Calendar is a nice-to-have.",
    ]
    assert payload["decisions"] == []
    assert payload["narrative"] == ""


def test_note_routes_through_the_daemon_on_a_thin_client(thin_client, monkeypatch):
    """Same #2751 exception `decision`/`ledger`/`link` get: the operator may
    be sitting anywhere in the fleet, so this must POST `/portal-note`
    rather than write a thin client's own (empty, wrong) local DB."""
    from coord import client as cc

    calls = []

    def _fake_post_record(svc, path, payload, **kw):
        calls.append((path, payload))
        return {
            "entry": {
                "id": 1,
                "submission_id": payload["submission_id"],
                "seq": 7,
                "kind": "operator_note",
                "question_revision": None,
                "text": payload["text"],
                "actor": "operator:jane",
                "source_event_id": None,
                "payload_json": "{}",
                "recorded_at": 100.0,
            }
        }

    monkeypatch.setattr(cc, "post_record", _fake_post_record)

    result = run("portal", "note", "sub_1", "Client prefers web-only")
    assert result.exit_code == 0, result.output
    assert "noted: sub_1 #7" in result.output
    assert len(calls) == 1
    assert calls[0][0] == "/portal-note"
    assert calls[0][1]["submission_id"] == "sub_1"
    assert calls[0][1]["text"] == "Client prefers web-only"


# ── #2986: `coord portal answer` — an out-of-band answer ────────────────────


def test_answer_is_registered_on_the_portal_group():
    result = run("portal", "--help")
    assert result.exit_code == 0
    assert "answer" in result.output


def test_answer_pairs_to_the_current_open_question_and_is_attributed(monkeypatch):
    from coord import portal_store

    monkeypatch.setattr("coord.commands.portal._actor", lambda: "jane")
    row = portal_store.enqueue("sub_1", "question", {"question": "Offline-first?"})
    portal_store.mark_applied(row)

    result = run("portal", "answer", "sub_1", "Yes, offline-first.")
    assert result.exit_code == 0, result.output
    assert f"answered: sub_1 Q[{row.revision}]" in result.output
    assert "via verbal" in result.output
    assert "operator:jane" in result.output

    ledger_result = run("portal", "ledger", "sub_1")
    assert ledger_result.exit_code == 0, ledger_result.output
    assert "RELAYED via verbal" in ledger_result.output
    assert "operator:jane" in ledger_result.output
    assert "Yes, offline-first." in ledger_result.output
    assert "unanswered" not in ledger_result.output


def test_answer_defaults_source_to_verbal_and_validates_choice():
    from coord import portal_store

    row = portal_store.enqueue("sub_1", "question", {"question": "SQLite?"})
    portal_store.mark_applied(row)

    bad = run("portal", "answer", "sub_1", "Yes.", "--source", "carrier-pigeon")
    assert bad.exit_code != 0
    assert "Invalid value" in bad.output or "invalid choice" in bad.output.lower()

    ok = run("portal", "answer", "sub_1", "Yes.", "--source", "email")
    assert ok.exit_code == 0, ok.output
    assert "via email" in ok.output


def test_answer_rejects_an_unknown_submission_with_a_clear_error():
    from coord import portal_store

    result = run("portal", "answer", "sub_nope", "Yes.")
    assert result.exit_code == 2
    assert "unknown submission" in result.output
    assert portal_store.ledger_for_submission("sub_nope") == []


def test_answer_rejects_empty_text():
    from coord import portal_store

    row = portal_store.enqueue("sub_1", "question", {"question": "Offline-first?"})
    portal_store.mark_applied(row)
    result = run("portal", "answer", "sub_1", "   ")
    assert result.exit_code == 2
    assert "non-empty" in result.output


def test_answer_with_no_open_question_reports_a_clear_error():
    from coord import portal_store

    portal_store.seed_revision("sub_1", 1)
    result = run("portal", "answer", "sub_1", "Yes.")
    assert result.exit_code == 2
    assert "no open question" in result.output


def test_answer_revision_backfills_an_older_reasked_question():
    """SUB-1EA1D3's fixture case (#2986): Q[11] answered verbally after
    Q[13] was already re-asked — `--revision` must land on Q[11], not
    whatever is currently open."""
    from coord import portal_store

    q11 = portal_store.enqueue(
        "sub_1", "question", {"question": "Who will use this, and how?"}
    )
    portal_store.mark_applied(q11)
    q13 = portal_store.enqueue("sub_1", "question", {"question": "And the rest?"})
    portal_store.mark_applied(q13)

    result = run(
        "portal", "answer", "sub_1", "Household of two.",
        "--revision", str(q11.revision),
    )
    assert result.exit_code == 0, result.output
    assert f"Q[{q11.revision}]" in result.output

    ledger_result = run("portal", "ledger", "sub_1")
    assert ledger_result.exit_code == 0, ledger_result.output
    assert "Household of two." in ledger_result.output
    # Q[13] is still open — the backfill must not touch it.
    q13_section = ledger_result.output.split(f"Q[{q13.revision}]", 1)[1]
    assert "(unanswered — needs-input)" in q13_section.split("Q[", 1)[0]


def test_answer_routes_through_the_daemon_on_a_thin_client(thin_client, monkeypatch):
    """Same #2751 exception `note`/`decision`/`link`/`ledger` get: the
    operator may be relaying this from any machine in the fleet, so this
    must POST `/portal-answer` rather than write a thin client's own
    (empty, wrong) local DB."""
    from coord import client as cc

    calls = []

    def _fake_post_record(svc, path, payload, **kw):
        calls.append((path, payload))
        return {
            "entry": {
                "id": 1,
                "submission_id": payload["submission_id"],
                "seq": 5,
                "kind": "question_answered",
                "question_revision": 1,
                "text": payload["text"],
                "actor": "operator:jane",
                "source_event_id": None,
                "payload_json": '{"relayed": true, "source": "phone"}',
                "recorded_at": 100.0,
            }
        }

    monkeypatch.setattr(cc, "post_record", _fake_post_record)

    result = run(
        "portal", "answer", "sub_1", "Client said yes", "--source", "phone",
    )
    assert result.exit_code == 0, result.output
    assert "answered: sub_1 Q[1]" in result.output
    assert len(calls) == 1
    assert calls[0][0] == "/portal-answer"
    assert calls[0][1]["submission_id"] == "sub_1"
    assert calls[0][1]["text"] == "Client said yes"
    assert calls[0][1]["source"] == "phone"


def test_answer_does_not_call_the_thin_client_guard(thin_client, monkeypatch):
    import coord.commands.portal as portal_mod
    from coord import client as cc

    calls = []
    real_guard = portal_mod._refuse_if_thin_client

    def _tracking_guard(cmd_name):
        calls.append(cmd_name)
        return real_guard(cmd_name)

    portal_mod._refuse_if_thin_client = _tracking_guard

    def _boom_post_record(svc, path, payload, **kw):
        raise RuntimeError("simulated unreachable daemon")

    monkeypatch.setattr(cc, "post_record", _boom_post_record)
    try:
        result = run("portal", "answer", "sub_1", "Yes.")
    finally:
        portal_mod._refuse_if_thin_client = real_guard
    assert result.exit_code != 0
    assert calls == []
