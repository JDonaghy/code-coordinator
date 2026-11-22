"""Tests for the merged-partial follow-through disposition."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from coord import board_doctor_followthrough as ft
from coord.config import ConfigError, _parse_board_doctor, _parse_board_doctor_repo_config
from coord.drive_queue import QueueEntry
from coord.models import BoardDoctorRepoConfig, Machine
from coord.milestone_order import parse_work_order

EPIC_BODY = """Epic body.

## Work order
- #100
- #101  {after: #100}

## Notes
keep me
"""


def _partial(**overrides) -> ft.PartialMerge:
    base = dict(
        repo_name="claude-coordinator",
        issue_number=101,
        title="Do A and B",
        body="Do A. Do B.",
        resolution_text="partial — B remains",
        review_findings="none",
        merged_diff="+a",
        pr_number=900,
        epic_number=50,
        epic_body=EPIC_BODY,
        milestone="M1",
        claude_md="rules",
    )
    base.update(overrides)
    return ft.PartialMerge(**base)


class FakeWriter:
    def __init__(self, epic_body: str = EPIC_BODY) -> None:
        self.next_number = 200
        self.created: list[dict] = []
        self.children: list[tuple[int, int, tuple[int, ...]]] = []
        self.bodies: dict[int, str] = {50: epic_body}
        self.closed: list[tuple[int, str]] = []
        self.comments: list[tuple[int, str]] = []

    def create_issue(self, repo_name, *, title, body, milestone):
        number = self.next_number
        self.next_number += 1
        self.created.append(dict(number=number, title=title, body=body, milestone=milestone))
        return number

    def add_child(self, repo_name, *, epic, child, after):
        self.children.append((epic, child, tuple(after)))

    def get_issue_body(self, repo_name, number):
        return self.bodies[number]

    def set_issue_body(self, repo_name, number, body):
        self.bodies[number] = body

    def close_issue(self, repo_name, number, *, comment):
        self.closed.append((number, comment))

    def comment_issue(self, repo_name, number, *, body):
        self.comments.append((number, body))

    @property
    def wrote_anything(self) -> bool:
        return (
            bool(self.created or self.children or self.closed or self.comments)
            or self.bodies[50] != EPIC_BODY
        )


class Sinks:
    def __init__(self) -> None:
        self.alerts: list[tuple[str, str]] = []
        self.pages: list[tuple[str, str]] = []
        self.records: list[dict] = []
        self.prompts: list[str] = []


def _plan_leg(sinks: Sinks, plan: dict | str):
    def leg(prompt: str) -> ft.PlanLegResult:
        sinks.prompts.append(prompt)
        out = plan if isinstance(plan, str) else f"Here is the plan:\n```json\n{json.dumps(plan)}\n```"
        return ft.PlanLegResult(assignment_id="plan-abc", output=out)
    return leg


def _run(partial, plan, writer=None, sinks=None, **kw):
    sinks = sinks or Sinks()
    writer = writer or FakeWriter()
    disp = ft.run_followthrough(
        partial,
        plan_leg=_plan_leg(sinks, plan),
        writer=writer,
        alert=lambda s, b: sinks.alerts.append((s, b)),
        page=lambda s, b: sinks.pages.append((s, b)),
        record=sinks.records.append,
        **kw,
    )
    return disp, writer, sinks


HAPPY_PLAN = {
    "items": [
        {"item": "A", "verified_still_missing": False, "evidence": "coord/a.py:10"},
        {"item": "B", "verified_still_missing": True, "evidence": "no b() anywhere"},
    ],
    "follow_ups": [
        {"key": "b1", "title": "Implement B core", "body": "Core of B.",
         "after": ["#100"], "files": ["coord/b.py"], "capability": "gtk"},
        {"key": "b2", "title": "Wire B into CLI", "body": "CLI.", "after": ["b1"],
         "milestone": "M2"},
    ],
}


def test_happy_path_writes_links_orders_and_closes() -> None:
    disp, writer, sinks = _run(_partial(), HAPPY_PLAN)

    assert disp.action == ft.ACTION_FILED
    assert disp.follow_ups == [200, 201]
    assert disp.plan_leg_assignment_id == "plan-abc"
    # Issues created with the follow-through marker and milestone fallback.
    assert [c["title"] for c in writer.created] == ["Implement B core", "Wire B into CLI"]
    assert writer.created[0]["milestone"] == "M1"
    assert writer.created[1]["milestone"] == "M2"
    assert all(ft.is_followthrough_product(c["body"]) for c in writer.created)
    assert "coord/b.py" in writer.created[0]["body"]
    assert "capability: gtk" in writer.created[0]["body"]
    # Linked under the epic with resolved `after` edges.
    assert writer.children == [(50, 200, (100,)), (50, 201, (200,))]
    # Work order regenerated, existing rows and other sections preserved.
    wo = parse_work_order(writer.bodies[50])
    assert wo.issue_numbers == (100, 101, 200, 201)
    assert wo.node(101).after == (100,)
    assert wo.node(201).after == (200,)
    assert "keep me" in writer.bodies[50]
    # Original closed with what landed and each successor.
    [(closed, comment)] = writer.closed
    assert closed == 101
    assert "A — coord/a.py:10" in comment
    assert "#200 — Implement B core" in comment and "#201 — Wire B into CLI" in comment
    # Recorded once, with the plan-leg id and follow-ups.
    [rec] = sinks.records
    assert rec["action"] == ft.ACTION_FILED
    assert rec["follow_ups"] == [200, 201]
    assert rec["plan_leg_assignment_id"] == "plan-abc"
    assert rec["key"] == "claude-coordinator#101"
    assert not sinks.alerts and not sinks.pages


def test_plan_leg_prompt_carries_every_input() -> None:
    _, _, sinks = _run(_partial(), HAPPY_PLAN)
    [prompt] = sinks.prompts
    for needle in ("Do A. Do B.", "partial — B remains", "+a", "Parent epic #50", "rules",
                   "READ-ONLY"):
        assert needle in prompt


def test_over_cap_plan_alerts_and_writes_nothing() -> None:
    plan = {
        "items": [{"item": "B", "verified_still_missing": True}],
        "follow_ups": [{"key": str(i), "title": f"t{i}", "body": ""} for i in range(3)],
    }
    disp, writer, sinks = _run(_partial(), plan, cap=2)
    assert disp.action == ft.ACTION_REJECTED
    assert any("over the per-issue cap of 2" in r for r in disp.reasons)
    assert not writer.wrote_anything
    assert len(sinks.alerts) == 1 and "rejected" in sinks.alerts[0][0]
    assert sinks.records[0]["action"] == ft.ACTION_REJECTED


def test_cross_backend_plan_alerts_and_writes_nothing() -> None:
    plan = {
        "items": [{"item": "B", "verified_still_missing": True}],
        "follow_ups": [{"key": "x", "title": "Port B to gtk and tui", "body": "both"}],
    }
    disp, writer, sinks = _run(_partial(), plan, backends=("gtk", "tui", "cocoa"))
    assert disp.action == ft.ACTION_REJECTED
    assert any("crosses backends (gtk, tui)" in r for r in disp.reasons)
    assert not writer.wrote_anything
    assert len(sinks.alerts) == 1


def test_after_declared_in_work_order_but_not_sub_issues_rejects_before_any_write() -> None:
    """#3945 review: `_splice_epic_checklists` would later splice the SAME
    `after` edge into `## Sub-issues` too, and `parse_sub_issues` requires
    it declared WITHIN that section — rejecting here, before any follow-up
    issue is created, beats hard-failing mid-write and orphaning one."""
    body = (
        "## Sub-issues\n"
        "- #100\n\n"
        "## Work order\n"
        "- #100\n"
        "- #101  {after: #100}\n"
    )
    plan = {
        "items": [{"item": "B", "verified_still_missing": True}],
        "follow_ups": [
            {"key": "a", "title": "New piece", "body": "Do it.", "after": ["#101"]},
        ],
    }
    # `issue_number` overridden away from the default 101 so the `after`
    # target under test (`#101`, declared in `## Work order` but not
    # `## Sub-issues`) doesn't also collide with the parent's own self-
    # pointing check (#4079a) — the two are deliberately different hazards.
    disp, writer, sinks = _run(
        _partial(issue_number=902, epic_body=body), plan, writer=FakeWriter(body),
    )

    assert disp.action == ft.ACTION_REJECTED
    assert any(
        "not its ## Sub-issues section" in r for r in disp.reasons
    ), disp.reasons
    # Nothing was created/linked/closed — rejected before any write, not
    # mid-write after orphaning a follow-up (`wrote_anything` isn't used
    # here: it compares against the module-level `EPIC_BODY` fixture,
    # which this test's custom *body* deliberately is not).
    assert writer.created == [] and writer.children == [] and writer.closed == []
    assert len(sinks.alerts) == 1


def test_after_declared_in_both_sections_is_accepted() -> None:
    body = (
        "## Sub-issues\n"
        "- #100\n\n"
        "## Work order\n"
        "- #100\n"
    )
    plan = {
        "items": [{"item": "B", "verified_still_missing": True}],
        "follow_ups": [
            {"key": "a", "title": "New piece", "body": "Do it.", "after": ["#100"]},
        ],
    }
    disp, writer, _ = _run(_partial(epic_body=body), plan, writer=FakeWriter(body))

    assert disp.action == ft.ACTION_FILED
    from coord.milestone_order import parse_sub_issues

    new_body = writer.bodies[50]
    assert parse_sub_issues(new_body).issue_numbers == (100, 200)
    assert parse_work_order(new_body).issue_numbers == (100, 200)


def test_cross_backend_check_inert_without_backend_rule() -> None:
    plan = {
        "items": [{"item": "B", "verified_still_missing": True}],
        "follow_ups": [{"key": "x", "title": "Port B to gtk and tui", "body": "both"}],
    }
    disp, _, _ = _run(_partial(), plan)
    assert disp.action == ft.ACTION_FILED


@pytest.mark.parametrize(
    ("follow_up", "needle"),
    [
        ({"key": "x", "title": "t", "body": "", "after": ["#999"]}, "not declared"),
        ({"key": "x", "title": "t", "body": "", "after": ["later"]}, "earlier follow-up key"),
        ({"key": "x", "title": "t", "body": "", "epic": 77}, "epic #77 does not exist"),
        ({"key": "x", "title": "t", "body": "", "milestone": "Nope"}, "milestone 'Nope'"),
    ],
)
def test_invalid_references_reject(follow_up, needle) -> None:
    plan = {"items": [{"item": "B", "verified_still_missing": True}], "follow_ups": [follow_up]}
    disp, writer, sinks = _run(
        _partial(), plan,
        epic_exists=lambda n: n == 50, milestone_exists=lambda m: m in {"M1", "M2"},
    )
    assert disp.action == ft.ACTION_REJECTED
    assert any(needle in r for r in disp.reasons), disp.reasons
    assert not writer.wrote_anything and len(sinks.alerts) == 1


def test_all_landed_closes_with_no_follow_ups() -> None:
    plan = {"items": [{"item": "B", "verified_still_missing": False, "evidence": "b.py:3"}],
            "follow_ups": []}
    disp, writer, sinks = _run(_partial(), plan)
    assert disp.action == ft.ACTION_ALL_LANDED
    assert writer.created == [] and writer.children == []
    assert writer.bodies[50] == EPIC_BODY
    [(closed, comment)] = writer.closed
    assert closed == 101 and "B — b.py:3" in comment
    assert sinks.records[0]["action"] == ft.ACTION_ALL_LANDED
    assert not sinks.alerts


def test_missing_items_without_follow_ups_reject() -> None:
    plan = {"items": [{"item": "B", "verified_still_missing": True}], "follow_ups": []}
    disp, writer, _ = _run(_partial(), plan)
    assert disp.action == ft.ACTION_REJECTED
    assert not writer.wrote_anything


def test_depth_limit_pages_and_never_dispatches_plan_leg() -> None:
    body = "Remainder.\n\n" + ft.FOLLOWTHROUGH_MARKER.format(issue=42)
    disp, writer, sinks = _run(_partial(body=body), HAPPY_PLAN)
    assert disp.action == ft.ACTION_DEPTH_PAGED
    assert sinks.prompts == []
    assert len(sinks.pages) == 1 and not sinks.alerts
    assert not writer.wrote_anything
    assert sinks.records[0]["action"] == ft.ACTION_DEPTH_PAGED


def test_dry_run_renders_plan_without_writing_or_recording() -> None:
    disp, writer, sinks = _run(_partial(), HAPPY_PLAN, dry_run=True)
    assert disp.dry_run and disp.action == ft.ACTION_FILED
    assert "[still missing] B" in disp.rendered_plan
    assert "[b2] Wire B into CLI" in disp.rendered_plan
    assert not writer.wrote_anything
    assert sinks.records == [] and sinks.alerts == [] and sinks.pages == []


def test_dry_run_rejected_plan_does_not_alert() -> None:
    plan = {"items": [{"item": "B", "verified_still_missing": True}],
            "follow_ups": [{"key": str(i), "title": f"t{i}", "body": ""} for i in range(3)]}
    disp, _, sinks = _run(_partial(), plan, cap=1, dry_run=True)
    assert disp.action == ft.ACTION_REJECTED
    assert sinks.alerts == [] and sinks.records == []


def test_unparseable_plan_alerts() -> None:
    disp, writer, sinks = _run(_partial(), "I could not decide.")
    assert disp.action == ft.ACTION_PLAN_FAILED
    assert not writer.wrote_anything and len(sinks.alerts) == 1


def test_malformed_epic_work_order_rejects() -> None:
    bad = "## Work order\n- #100 {bogus: 1}\n"
    disp, writer, _ = _run(_partial(epic_body=bad), HAPPY_PLAN, writer=FakeWriter(bad))
    assert disp.action == ft.ACTION_REJECTED
    assert any("does not parse" in r for r in disp.reasons)
    assert writer.created == []


def test_write_failure_alerts_with_partial_progress() -> None:
    class Boom(FakeWriter):
        def add_child(self, repo_name, *, epic, child, after):
            raise RuntimeError("gh down")

    disp, writer, sinks = _run(_partial(), HAPPY_PLAN, writer=Boom())
    assert disp.action == ft.ACTION_WRITE_FAILED
    assert disp.follow_ups == [200]
    assert writer.closed == []
    assert "#200" in sinks.alerts[0][1]


# ── Idempotent resume / dedupe (#4007) ──────────────────────────────────────

_SINGLE_PLAN = {
    "items": [{"item": "B", "verified_still_missing": True, "evidence": "no b()"}],
    "follow_ups": [
        {"key": "b1", "title": "Implement B core", "body": "Core of B.", "after": ["#100"]},
    ],
}


def test_write_failed_disposition_records_resumable_evidence() -> None:
    """The very scenario from the quadraui#2419 incident: `create_issue`
    confirms but `add_child`'s link never does. The write-failed
    disposition's recorded evidence must carry enough to resume without
    asking the plan leg again or re-creating #200."""
    class Boom(FakeWriter):
        def add_child(self, repo_name, *, epic, child, after):
            raise RuntimeError("add_sub_issue returned without error but parent reads None")

    disp, writer, sinks = _run(_partial(), _SINGLE_PLAN, writer=Boom())
    assert disp.action == ft.ACTION_WRITE_FAILED
    assert disp.follow_up_keys == {"b1": 200}
    [rec] = sinks.records
    [evidence_item] = [
        e for e in rec["evidence"] if e.startswith(ft.PENDING_FOLLOWTHROUGH_EVIDENCE_PREFIX)
    ]
    payload = json.loads(evidence_item[len(ft.PENDING_FOLLOWTHROUGH_EVIDENCE_PREFIX):])
    resumed_plan = ft.plan_from_jsonable(payload["plan"])
    assert [fu.key for fu in resumed_plan.follow_ups] == ["b1"]
    assert payload["follow_up_keys"] == {"b1": 200}


def test_resumed_write_failure_alert_reports_reused_child_not_none() -> None:
    """A resumed attempt that fails writing AGAIN must not claim "Filed so
    far: none" — #200 is already confirmed via the recorded resume state,
    not freshly created THIS run, so the alert has to read it off
    `follow_up_keys`, not `follow_ups`."""
    class StillBoom(FakeWriter):
        def add_child(self, repo_name, *, epic, child, after):
            raise RuntimeError("gh down again")

    plan = ft.parse_plan(json.dumps(_SINGLE_PLAN))
    coverage = ft.FollowThroughCoverage(
        pending=ft.PendingFollowThrough(plan=plan, follow_up_keys={"b1": 200}),
    )
    disp, writer, sinks = _run(
        _partial(), _SINGLE_PLAN, writer=StillBoom(), coverage=coverage,
    )
    assert disp.action == ft.ACTION_WRITE_FAILED
    assert disp.follow_ups == []  # nothing freshly created this run
    assert disp.follow_up_keys == {"b1": 200}
    assert "#200" in sinks.alerts[0][1]
    assert "none" not in sinks.alerts[0][1]


def test_resume_reuses_recorded_child_retries_link_and_closes() -> None:
    """The next sweep, given the recorded state from the test above: no new
    plan leg dispatch, no new `create_issue` — only the link retried, then
    close (#4007 acceptance)."""
    plan = ft.parse_plan(json.dumps(_SINGLE_PLAN))
    coverage = ft.FollowThroughCoverage(
        pending=ft.PendingFollowThrough(plan=plan, follow_up_keys={"b1": 200}),
    )
    disp, writer, sinks = _run(_partial(), _SINGLE_PLAN, coverage=coverage)

    assert sinks.prompts == []  # plan leg never dispatched on resume
    assert writer.created == []  # #200 is reused, never re-created
    assert disp.action == ft.ACTION_FILED
    assert disp.follow_up_keys == {"b1": 200}
    assert writer.children == [(50, 200, (100,))]
    [(closed, comment)] = writer.closed
    assert closed == 101
    assert "#200 — Implement B core" in comment
    assert sinks.records[0]["action"] == ft.ACTION_FILED


def test_covering_open_pr_short_circuits_before_planning() -> None:
    """An open PR already carrying a closing keyword for the parent: file
    nothing, don't even dispatch a plan leg (#4007 acceptance)."""
    coverage = ft.FollowThroughCoverage(covering_pr=2439)
    disp, writer, sinks = _run(_partial(), HAPPY_PLAN, coverage=coverage)

    assert disp.action == ft.ACTION_ALREADY_COVERED
    assert sinks.prompts == []
    assert not writer.wrote_anything
    assert any("#2439" in r for r in disp.reasons)
    assert sinks.records[0]["action"] == ft.ACTION_ALREADY_COVERED
    assert not sinks.alerts and not sinks.pages


def test_covering_pr_takes_priority_over_pending() -> None:
    """A covering PR wins even when a pending resume state also exists —
    remediation already in flight beats resuming a stale attempt."""
    plan = ft.parse_plan(json.dumps(_SINGLE_PLAN))
    coverage = ft.FollowThroughCoverage(
        covering_pr=2439,
        pending=ft.PendingFollowThrough(plan=plan, follow_up_keys={"b1": 200}),
    )
    disp, writer, sinks = _run(_partial(), _SINGLE_PLAN, coverage=coverage)
    assert disp.action == ft.ACTION_ALREADY_COVERED
    assert not writer.wrote_anything


def test_already_covered_by_dedupes_without_creating() -> None:
    """The plan leg naming an existing issue as already covering a slice
    (quadraui#2443's own body said so in prose; this is the structured
    counterpart the writer actually honors) links/orders that issue
    instead of filing a duplicate."""
    plan = {
        "items": [{"item": "B", "verified_still_missing": True}],
        "follow_ups": [
            {"key": "a", "title": "Implement B core", "body": "Core of B.",
             "after": ["#100"], "already_covered_by": "#2440"},
        ],
    }
    disp, writer, sinks = _run(_partial(), plan)

    assert disp.action == ft.ACTION_FILED
    assert writer.created == []
    assert disp.deduped == {"a": 2440}
    assert disp.follow_up_keys == {"a": 2440}
    assert writer.children == [(50, 2440, (100,))]
    [(closed, comment)] = writer.closed
    assert "#2440 — Implement B core" in comment
    assert not sinks.alerts


def test_malformed_already_covered_by_rejects_before_any_write() -> None:
    plan = {
        "items": [{"item": "B", "verified_still_missing": True}],
        "follow_ups": [{"key": "a", "title": "t", "body": "", "already_covered_by": "nope"}],
    }
    disp, writer, sinks = _run(_partial(), plan)
    assert disp.action == ft.ACTION_REJECTED
    assert any("already_covered_by" in r for r in disp.reasons)
    assert writer.created == []


def test_already_covered_by_int_is_accepted() -> None:
    """The plan leg's own schema documents `epic` as a bare int; a plan
    leg spelling `already_covered_by` the same way must not hard-fail the
    whole plan into `ACTION_PLAN_FAILED`."""
    plan = {
        "items": [{"item": "B", "verified_still_missing": True}],
        "follow_ups": [{"key": "a", "title": "t", "body": "", "already_covered_by": 2440}],
    }
    disp, writer, sinks = _run(_partial(), plan)
    assert disp.action == ft.ACTION_FILED
    assert disp.deduped == {"a": 2440}


def test_already_covered_by_closed_issue_rejects_before_any_write() -> None:
    """A syntactically valid `already_covered_by` reference is not honoured
    on the plan leg's word alone — the referenced issue must independently
    read as open (quadraui#2440/#2443's own failure mode: a stale or wrong
    reference must not silently drop the remainder on the floor)."""
    plan = {
        "items": [{"item": "B", "verified_still_missing": True}],
        "follow_ups": [{"key": "a", "title": "t", "body": "", "already_covered_by": "#2440"}],
    }
    disp, writer, sinks = _run(_partial(), plan, covering_issue_open=lambda _n: False)
    assert disp.action == ft.ACTION_REJECTED
    assert any(
        "already_covered_by" in r and "open" in r for r in disp.reasons
    )
    assert writer.created == []


def test_fully_deduped_plan_filed_with_explicit_reason() -> None:
    """When every follow-up resolves via `already_covered_by`, nothing new
    is filed — `ACTION_FILED` still closes the parent (the remainder has
    somewhere to live), but the decision log must say so explicitly rather
    than reading like a bare "something was filed" claim."""
    plan = {
        "items": [{"item": "B", "verified_still_missing": True}],
        "follow_ups": [
            {"key": "a", "title": "t", "body": "", "already_covered_by": "#2440"},
        ],
    }
    disp, writer, sinks = _run(_partial(), plan)
    assert disp.action == ft.ACTION_FILED
    assert disp.follow_ups == []
    assert any("already covered" in r for r in disp.reasons)


def test_deduped_follow_up_is_not_auto_queued(monkeypatch) -> None:
    monkeypatch.delenv(ft.NO_FOLLOWTHROUGH_ENV_VAR, raising=False)
    enqueue = Enqueue()
    ctx = ft.AutoQueueContext(machines=FLEET, enqueue=enqueue)
    plan = {
        "items": [{"item": "X", "verified_still_missing": True}],
        "follow_ups": [{"key": "a", "title": "t", "body": "", "already_covered_by": "#2440"}],
    }
    disp, writer, sinks = _run(_partial(), plan, auto_queue=ctx)
    assert disp.action == ft.ACTION_FILED
    assert enqueue.calls == []


def test_enabled_requires_env_fleet_and_repo(monkeypatch) -> None:
    repo = SimpleNamespace(board_doctor=SimpleNamespace(followthrough=True))
    cfg = SimpleNamespace(
        board_doctor=SimpleNamespace(followthrough=True), repo=lambda name: repo,
    )
    monkeypatch.delenv(ft.NO_FOLLOWTHROUGH_ENV_VAR, raising=False)
    assert ft.followthrough_enabled("r", cfg)
    monkeypatch.setenv(ft.NO_FOLLOWTHROUGH_ENV_VAR, "1")
    assert not ft.followthrough_enabled("r", cfg)
    monkeypatch.delenv(ft.NO_FOLLOWTHROUGH_ENV_VAR)
    cfg.board_doctor.followthrough = False
    assert not ft.followthrough_enabled("r", cfg)
    cfg.board_doctor.followthrough = True
    repo.board_doctor.followthrough = False
    assert not ft.followthrough_enabled("r", cfg)


def test_config_parses_followthrough_switches() -> None:
    assert _parse_board_doctor(None).followthrough is True
    assert _parse_board_doctor(None).followthrough_cap == 5
    cfg = _parse_board_doctor({"followthrough": False, "followthrough_cap": 2})
    assert cfg.followthrough is False and cfg.followthrough_cap == 2
    with pytest.raises(ConfigError):
        _parse_board_doctor({"followthrough_cap": 0})
    with pytest.raises(ConfigError):
        _parse_board_doctor({"followthrough": "yes"})
    assert _parse_board_doctor_repo_config(None, 0).followthrough is False
    assert _parse_board_doctor_repo_config({"followthrough": True}, 0).followthrough is True
    with pytest.raises(ConfigError):
        _parse_board_doctor_repo_config({"followthrough": 1}, 0)


# ── Auto-queue ───────────────────────────────────────────────────────────────

REPO = "claude-coordinator"


def _machine(name: str, caps: list[str], *, repo_path: bool = True) -> Machine:
    return Machine(
        name=name, host=name, capabilities=caps, repos=[REPO],
        repo_paths={REPO: f"/src/{name}"} if repo_path else {},
    )


FLEET = [
    _machine("linuxbox", ["python", "rust"]),
    _machine("gtkbox", ["python", "gtk"]),
    _machine("macmini", ["python", "macos"]),
    _machine("winbox", ["python", "windows"]),
    _machine("macnopath", ["macos"], repo_path=False),
]


class Enqueue:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, repo_name, issue_number, **kwargs):
        self.calls.append(dict(repo=repo_name, issue=issue_number, **kwargs))
        return len(self.calls)


def _cfg(*, auto_queue: bool, followthrough: bool = True):
    repo = SimpleNamespace(board_doctor=BoardDoctorRepoConfig(
        followthrough=followthrough, followthrough_auto_queue=auto_queue,
    ))
    return SimpleNamespace(
        board_doctor=SimpleNamespace(followthrough=True),
        repo=lambda name: repo, machines=FLEET,
    )


def _single_follow_up_plan(capability: str | None, **extra) -> dict:
    fu = {"key": "a", "title": "Platform piece", "body": "Do it.", "after": ["#100"]}
    if capability is not None:
        fu["capability"] = capability
    fu.update(extra)
    return {
        "items": [{"item": "X", "verified_still_missing": True, "evidence": "none"}],
        "follow_ups": [fu],
    }


@pytest.mark.parametrize("capability, machine", [
    ("[gtk]", "gtkbox"), ("macos", "macmini"), ("[win]", "winbox"), ("windows", "winbox"),
])
def test_platform_follow_up_is_pinned_to_capable_machine(
    monkeypatch, capability, machine,
) -> None:
    monkeypatch.delenv(ft.NO_FOLLOWTHROUGH_ENV_VAR, raising=False)
    enqueue = Enqueue()
    ctx = ft.build_auto_queue_context(REPO, _cfg(auto_queue=True), enqueue=enqueue)
    assert ctx is not None
    disp, writer, sinks = _run(_partial(), _single_follow_up_plan(capability), auto_queue=ctx)
    assert disp.action == ft.ACTION_FILED
    assert enqueue.calls == [dict(
        repo=REPO, issue=200, machine=machine, after=[f"{REPO}#100"], position=None,
    )]
    assert disp.queued == {200: machine}
    assert sinks.records[-1]["queued"] == {"200": machine}
    assert not sinks.alerts


def test_unroutable_capability_alerts_and_is_not_queued(monkeypatch) -> None:
    monkeypatch.delenv(ft.NO_FOLLOWTHROUGH_ENV_VAR, raising=False)
    enqueue = Enqueue()
    ctx = ft.build_auto_queue_context(REPO, _cfg(auto_queue=True), enqueue=enqueue)
    disp, writer, sinks = _run(_partial(), _single_follow_up_plan("fpga"), auto_queue=ctx)
    # Still filed and the original still closed — only the queueing is refused.
    assert [c["number"] for c in writer.created] == [200]
    assert writer.closed and writer.closed[0][0] == 101
    assert enqueue.calls == []
    assert len(sinks.alerts) == 1
    assert "unroutable" in sinks.alerts[0][0]
    assert "'fpga'" in sinks.alerts[0][1]
    assert any("not queued" in r for r in sinks.records[-1]["reasons"])


def test_capability_machine_without_repo_checkout_is_not_capable() -> None:
    assert ft.capable_machines("macos", REPO, FLEET) == ["macmini"]
    assert ft.capable_machines("macos", "other-repo", FLEET) == []


def test_auto_queue_off_files_but_never_queues(monkeypatch) -> None:
    monkeypatch.delenv(ft.NO_FOLLOWTHROUGH_ENV_VAR, raising=False)
    enqueue = Enqueue()
    ctx = ft.build_auto_queue_context(REPO, _cfg(auto_queue=False), enqueue=enqueue)
    assert ctx is None
    disp, writer, sinks = _run(_partial(), HAPPY_PLAN, auto_queue=ctx)
    assert [c["number"] for c in writer.created] == [200, 201]
    assert enqueue.calls == []
    assert disp.queued == {}


def test_auto_queue_requires_followthrough_itself(monkeypatch) -> None:
    monkeypatch.delenv(ft.NO_FOLLOWTHROUGH_ENV_VAR, raising=False)
    cfg = _cfg(auto_queue=True, followthrough=False)
    assert ft.build_auto_queue_context(REPO, cfg) is None


def test_after_edges_follow_the_plan_and_tail_append(monkeypatch) -> None:
    monkeypatch.delenv(ft.NO_FOLLOWTHROUGH_ENV_VAR, raising=False)
    enqueue = Enqueue()
    entries = [QueueEntry(repo=REPO, issue=101, position=3)]
    # The original's epic (50) is not the one draining → tail.
    ctx = ft.build_auto_queue_context(
        REPO, _cfg(auto_queue=True), entries=entries, draining_epic=77, enqueue=enqueue,
    )
    _run(_partial(), HAPPY_PLAN, auto_queue=ctx)
    assert enqueue.calls == [
        dict(repo=REPO, issue=200, machine="gtkbox", after=[f"{REPO}#100"], position=None),
        dict(repo=REPO, issue=201, machine=None, after=[f"{REPO}#200"], position=None),
    ]


def test_draining_epic_queues_at_original_position(monkeypatch) -> None:
    monkeypatch.delenv(ft.NO_FOLLOWTHROUGH_ENV_VAR, raising=False)
    enqueue = Enqueue()
    entries = [QueueEntry(repo=REPO, issue=101, position=3)]
    ctx = ft.build_auto_queue_context(
        REPO, _cfg(auto_queue=True), entries=entries, draining_epic=50, enqueue=enqueue,
    )
    _run(_partial(), HAPPY_PLAN, auto_queue=ctx)
    assert [c["position"] for c in enqueue.calls] == [3, 4]


def test_declared_capable_machine_is_preferred(monkeypatch) -> None:
    monkeypatch.delenv(ft.NO_FOLLOWTHROUGH_ENV_VAR, raising=False)
    fleet = FLEET + [_machine("gtkbox2", ["gtk"])]
    enqueue = Enqueue()
    ctx = ft.AutoQueueContext(machines=fleet, enqueue=enqueue)
    _run(_partial(), _single_follow_up_plan("gtk", machine="gtkbox2"), auto_queue=ctx)
    assert enqueue.calls[0]["machine"] == "gtkbox2"
    # A declared machine lacking the capability is overridden.
    enqueue2 = Enqueue()
    ctx2 = ft.AutoQueueContext(machines=fleet, enqueue=enqueue2)
    _run(_partial(), _single_follow_up_plan("gtk", machine="macmini"), auto_queue=ctx2)
    assert enqueue2.calls[0]["machine"] == "gtkbox"


def test_enqueue_failure_alerts_without_failing_disposition(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("daemon down")

    ctx = ft.AutoQueueContext(machines=FLEET, enqueue=boom)
    disp, writer, sinks = _run(_partial(), HAPPY_PLAN, auto_queue=ctx)
    assert disp.action == ft.ACTION_FILED
    assert writer.closed
    assert any("auto-queue failed" in s for s, _ in sinks.alerts)


def test_config_parses_followthrough_auto_queue() -> None:
    assert _parse_board_doctor_repo_config(None, 0).followthrough_auto_queue is False
    parsed = _parse_board_doctor_repo_config({"followthrough_auto_queue": True}, 0)
    assert parsed.followthrough_auto_queue is True
    with pytest.raises(ConfigError):
        _parse_board_doctor_repo_config({"followthrough_auto_queue": "yes"}, 0)


# ── Prose-preserving epic splice (#3945) ────────────────────────────────────


def test_splice_preserves_existing_row_prose_in_work_order() -> None:
    body = (
        "Epic body.\n\n"
        "## Work order\n"
        "- #100 — tranche 1 of #99: do the first half\n"
        "- #101  {after: #100} — tranche 2: do the rest\n\n"
        "## Notes\nkeep me\n"
    )
    disp, writer, _ = _run(_partial(epic_body=body), HAPPY_PLAN, writer=FakeWriter(body))

    assert disp.action == ft.ACTION_FILED
    new_body = writer.bodies[50]
    # Every pre-existing line survives byte-for-byte.
    assert "- #100 — tranche 1 of #99: do the first half" in new_body
    assert "- #101  {after: #100} — tranche 2: do the rest" in new_body
    assert "keep me" in new_body
    # The new rows are still structurally present and parse correctly.
    wo = parse_work_order(new_body)
    assert wo.issue_numbers == (100, 101, 200, 201)
    assert wo.node(201).after == (200,)


def test_splice_preserves_existing_checkbox_row_prose_in_work_order() -> None:
    """Same as the checkbox-free variant above, but for the `- [ ] #N —
    prose` shape `coord milestone add-child` actually writes today
    (#1061's checkbox is optional, not retired) — the splice must not
    require the epic to already be in the checkbox-free grammar."""
    body = (
        "Epic body.\n\n"
        "## Work order\n"
        "- [ ] #100 — tranche 1 of #99: do the first half\n"
        "- [x] #101  {after: #100} — tranche 2: do the rest\n\n"
        "## Notes\nkeep me\n"
    )
    disp, writer, _ = _run(_partial(epic_body=body), HAPPY_PLAN, writer=FakeWriter(body))

    assert disp.action == ft.ACTION_FILED
    new_body = writer.bodies[50]
    # Every pre-existing line survives byte-for-byte.
    assert "- [ ] #100 — tranche 1 of #99: do the first half" in new_body
    assert "- [x] #101  {after: #100} — tranche 2: do the rest" in new_body
    assert "keep me" in new_body
    wo = parse_work_order(new_body)
    assert wo.issue_numbers == (100, 101, 200, 201)
    assert wo.node(201).after == (200,)


def test_splice_preserves_prose_in_both_work_order_and_sub_issues() -> None:
    body = (
        "## Sub-issues\n"
        "- #100 — prose for the sub-issues list\n\n"
        "## Work order\n"
        "- #100 — different prose for the work order\n\n"
        "## Notes\nkeep me\n"
    )
    plan = {
        "items": [{"item": "B", "verified_still_missing": True}],
        "follow_ups": [{"key": "a", "title": "New piece", "body": "Do it.", "after": ["#100"]}],
    }
    disp, writer, _ = _run(_partial(epic_body=body), plan, writer=FakeWriter(body))

    assert disp.action == ft.ACTION_FILED
    new_body = writer.bodies[50]
    assert "- #100 — prose for the sub-issues list" in new_body
    assert "- #100 — different prose for the work order" in new_body
    assert "keep me" in new_body
    from coord.milestone_order import parse_sub_issues

    assert parse_sub_issues(new_body).issue_numbers == (100, 200)
    assert parse_work_order(new_body).issue_numbers == (100, 200)


def test_splice_is_idempotent_for_an_already_linked_node() -> None:
    body = "## Work order\n- #100 — keep this exact row\n- #200\n"
    disp, writer, _ = _run(_partial(epic_body=body), HAPPY_PLAN, writer=FakeWriter(body))
    assert disp.action == ft.ACTION_FILED
    new_body = writer.bodies[50]
    assert new_body.count("- #200") == 1
    assert "- #100 — keep this exact row" in new_body


def test_splice_appends_fresh_work_order_when_epic_has_neither_section() -> None:
    body = "Epic body with no checklist at all.\n"
    plan = {
        "items": [{"item": "B", "verified_still_missing": True}],
        "follow_ups": [{"key": "a", "title": "New piece", "body": "Do it."}],
    }
    disp, writer, _ = _run(_partial(epic_body=body), plan, writer=FakeWriter(body))
    assert disp.action == ft.ACTION_FILED
    wo = parse_work_order(writer.bodies[50])
    assert wo.issue_numbers == (200,)


def test_splice_preserves_trailing_newline() -> None:
    """`body.splitlines()` drops whether *body* ended in `\\n` — the splice
    must restore it rather than silently trimming a trailing newline on
    every write (#3945 review)."""
    from coord.board_doctor_followthrough import _splice_epic_checklists
    from coord.milestone_order import WorkOrderNode

    with_trailing = "## Work order\n- #100\n"
    result = _splice_epic_checklists(with_trailing, [WorkOrderNode(issue_number=200)])
    assert result.endswith("\n")

    without_trailing = "## Work order\n- #100"
    result = _splice_epic_checklists(without_trailing, [WorkOrderNode(issue_number=200)])
    assert not result.endswith("\n")


def test_splice_raises_on_a_cycle_introduced_by_the_new_rows() -> None:
    from coord.board_doctor_followthrough import _splice_epic_checklists
    from coord.milestone_order import WorkOrderError, WorkOrderNode

    # Two new rows that mutually depend on each other — the existing
    # section (just #100, no `after` at all) is fine on its own; the
    # cycle is entirely in the two spliced-in nodes.
    with pytest.raises(WorkOrderError):
        _splice_epic_checklists(
            "## Work order\n- #100\n",
            [
                WorkOrderNode(issue_number=300, after=(400,)),
                WorkOrderNode(issue_number=400, after=(300,)),
            ],
        )


# ── Real GitHub-backed writer (#3945) ───────────────────────────────────────


class _FakeGithubOps:
    """Records every call a :class:`ft.GitHubFollowThroughWriter` makes,
    standing in for :mod:`coord.github_ops` (never a real `gh` call)."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.sub_issues: list[tuple[str, int, int]] = []
        self.parents: dict[int, int] = {}
        self.bodies: dict[int, str] = {}
        self.closed: list[tuple[int, str]] = []
        self.states: dict[int, str] = {}
        self.next_number = 500

    def create_issue(self, repo, title, body, labels=None, milestone=None):
        number = self.next_number
        self.next_number += 1
        self.created.append(dict(
            repo=repo, number=number, title=title, body=body, milestone=milestone,
        ))
        self.bodies[number] = body
        self.states[number] = "open"
        return {"number": number, "url": f"https://x/{number}"}

    def add_sub_issue(self, repo, parent_number, child_number):
        self.sub_issues.append((repo, parent_number, child_number))
        self.parents[child_number] = parent_number

    def get_issue(self, repo, number):
        return {"number": number, "body": self.bodies.get(number, "")}

    def get_issue_parent(self, repo, number):
        parent = self.parents.get(number)
        return {"number": parent} if parent is not None else None

    def get_issue_state(self, repo, number):
        return self.states.get(number, "open")

    def update_issue_body(self, repo, number, body):
        self.bodies[number] = body

    def close_issue(self, repo, number, comment=None, force=False):
        self.closed.append((number, comment))
        self.states[number] = "closed"


def test_github_writer_routes_every_method_through_github_ops(monkeypatch) -> None:
    fake = _FakeGithubOps()
    monkeypatch.setattr("coord.github_ops.create_issue", fake.create_issue)
    monkeypatch.setattr("coord.github_ops.add_sub_issue", fake.add_sub_issue)
    monkeypatch.setattr("coord.github_ops.get_issue", fake.get_issue)
    monkeypatch.setattr("coord.github_ops.get_issue_parent", fake.get_issue_parent)
    monkeypatch.setattr("coord.github_ops.get_issue_state", fake.get_issue_state)
    monkeypatch.setattr("coord.github_ops.update_issue_body", fake.update_issue_body)
    monkeypatch.setattr("coord.github_ops.close_issue", fake.close_issue)

    writer = ft.GitHubFollowThroughWriter()
    fake.bodies[50] = "## Work order\n- #100\n"

    number = writer.create_issue(
        "acme/api", title="New piece", body="Do it.", milestone=None,
    )
    assert number == 500
    assert fake.created[0]["title"] == "New piece"

    writer.add_child("acme/api", epic=50, child=number, after=(100,))
    assert fake.sub_issues == [("acme/api", 50, 500)]

    assert writer.get_issue_body("acme/api", 50) == "## Work order\n- #100\n"
    writer.set_issue_body("acme/api", 50, "## Work order\n- #100\n- #500\n")
    assert fake.bodies[50] == "## Work order\n- #100\n- #500\n"

    writer.close_issue("acme/api", 101, comment="done")
    assert fake.closed == [(101, "done")]


class TestGitHubFollowThroughWriterReadBack:
    """Every write on :class:`ft.GitHubFollowThroughWriter` confirms its own
    result by reading it back and raises when the read disagrees — a clean
    `gh`/REST return only proves the request was issued, not that it
    landed (#3945 review)."""

    @staticmethod
    def _writer_and_fake(monkeypatch) -> tuple["ft.GitHubFollowThroughWriter", _FakeGithubOps]:
        fake = _FakeGithubOps()
        monkeypatch.setattr("coord.github_ops.add_sub_issue", fake.add_sub_issue)
        monkeypatch.setattr("coord.github_ops.get_issue", fake.get_issue)
        monkeypatch.setattr("coord.github_ops.get_issue_parent", fake.get_issue_parent)
        monkeypatch.setattr("coord.github_ops.get_issue_state", fake.get_issue_state)
        monkeypatch.setattr("coord.github_ops.update_issue_body", fake.update_issue_body)
        monkeypatch.setattr("coord.github_ops.close_issue", fake.close_issue)
        # No-op sleep: these tests aren't about the backoff timing itself
        # (see the dedicated retry tests below), just the confirm/raise
        # contract, so a real `time.sleep` must not slow every run of them.
        return ft.GitHubFollowThroughWriter(sleep=lambda _seconds: None), fake

    def test_add_child_confirmed_does_not_raise(self, monkeypatch) -> None:
        writer, fake = self._writer_and_fake(monkeypatch)
        writer.add_child("acme/api", epic=50, child=500, after=())
        assert fake.parents[500] == 50

    def test_add_child_unconfirmed_raises(self, monkeypatch) -> None:
        writer, fake = self._writer_and_fake(monkeypatch)
        # `add_sub_issue` "succeeds" but the parent never actually took.
        monkeypatch.setattr(
            "coord.github_ops.add_sub_issue", lambda repo, parent_number, child_number: None,
        )
        with pytest.raises(RuntimeError, match="not 50"):
            writer.add_child("acme/api", epic=50, child=500, after=())

    def test_add_child_readback_retries_a_lagging_read_before_succeeding(
        self, monkeypatch,
    ) -> None:
        """#4007 incident: `add_sub_issue` returned without error but the
        immediate read-back saw the parent still unset — read-after-write
        lag, not a real failure. A read that lags once and then confirms
        within the retry window must not be reported as `write-failed`."""
        fake = _FakeGithubOps()
        monkeypatch.setattr("coord.github_ops.add_sub_issue", fake.add_sub_issue)
        calls = {"n": 0}

        def lagging_get_parent(repo, number):
            calls["n"] += 1
            if calls["n"] < 2:
                return None
            return fake.get_issue_parent(repo, number)

        monkeypatch.setattr("coord.github_ops.get_issue_parent", lagging_get_parent)
        sleeps: list[float] = []
        writer = ft.GitHubFollowThroughWriter(sleep=sleeps.append)

        writer.add_child("acme/api", epic=50, child=500, after=())

        assert fake.parents[500] == 50
        assert sleeps == [1.0]  # one backoff, then the second read confirmed

    def test_add_child_readback_exhausts_retries_then_raises(self, monkeypatch) -> None:
        fake = _FakeGithubOps()
        monkeypatch.setattr("coord.github_ops.add_sub_issue", fake.add_sub_issue)
        monkeypatch.setattr("coord.github_ops.get_issue_parent", lambda repo, number: None)
        sleeps: list[float] = []
        writer = ft.GitHubFollowThroughWriter(sleep=sleeps.append)

        with pytest.raises(RuntimeError, match="after 3 read-back attempts"):
            writer.add_child("acme/api", epic=50, child=500, after=())

        assert sleeps == [1.0, 2.0]

    def test_set_issue_body_confirmed_does_not_raise(self, monkeypatch) -> None:
        writer, fake = self._writer_and_fake(monkeypatch)
        writer.set_issue_body("acme/api", 50, "new body")
        assert fake.bodies[50] == "new body"

    def test_set_issue_body_unconfirmed_raises(self, monkeypatch) -> None:
        writer, fake = self._writer_and_fake(monkeypatch)
        # `update_issue_body` "succeeds" but never actually writes.
        monkeypatch.setattr(
            "coord.github_ops.update_issue_body", lambda repo, number, body: None,
        )
        with pytest.raises(RuntimeError, match="does not match"):
            writer.set_issue_body("acme/api", 50, "new body")

    def test_close_issue_confirmed_does_not_raise(self, monkeypatch) -> None:
        writer, fake = self._writer_and_fake(monkeypatch)
        writer.close_issue("acme/api", 101, comment="done")
        assert fake.closed == [(101, "done")]

    def test_close_issue_unconfirmed_raises(self, monkeypatch) -> None:
        writer, fake = self._writer_and_fake(monkeypatch)
        # `close_issue` "succeeds" but the issue still reads open.
        monkeypatch.setattr(
            "coord.github_ops.close_issue", lambda repo, number, comment=None, force=False: None,
        )
        with pytest.raises(RuntimeError, match="not confirmed closed"):
            writer.close_issue("acme/api", 101, comment="done")


# ── #4079: self-pointing remainder / coordinator-owned remainder ───────────


def test_already_covered_by_pointing_at_parent_rejects_without_closing() -> None:
    """quadraui#2806's own failure mode: the plan leg named the follow-up
    `already_covered_by` the PARENT issue itself. `covering_issue_open`
    alone can't catch this — the parent is still open at validation time —
    so this must be checked directly."""
    plan = {
        "items": [{"item": "B", "verified_still_missing": True}],
        "follow_ups": [
            {"key": "a", "title": "Rewrite CLAUDE.md", "body": "...",
             "already_covered_by": "#101"},
        ],
    }
    disp, writer, sinks = _run(_partial(), plan)  # _partial()'s issue_number is 101

    assert disp.action == ft.ACTION_REJECTED
    assert any(
        "already_covered_by" in r and "parent issue #101" in r for r in disp.reasons
    )
    assert not writer.wrote_anything


def test_follow_up_naming_coordinator_owned_doc_rejects_without_closing() -> None:
    """A follow-up whose own `files` names a coordinator-owned doc
    (CLAUDE.md) can never actually be finished by an ordinary worker
    dispatch — writing it and closing the parent on the strength of it
    would silently lose the remainder (quadraui#2806)."""
    plan = {
        "items": [{"item": "B", "verified_still_missing": True}],
        "follow_ups": [
            {"key": "a", "title": "Rewrite CLAUDE.md", "body": "...",
             "files": ["CLAUDE.md"]},
        ],
    }
    disp, writer, sinks = _run(
        _partial(), plan, coordinator_owned_docs=("CLAUDE.md",),
    )

    assert disp.action == ft.ACTION_REJECTED
    assert any("CLAUDE.md" in r and "coordinator-owned" in r for r in disp.reasons)
    assert not writer.wrote_anything


def test_coordinator_owned_doc_check_inert_without_configured_paths() -> None:
    """Without any `coordinator_owned_docs` configured (the default), a
    follow-up naming CLAUDE.md in `files` is not rejected on that basis —
    mirrors `detect_cross_backend_scope`'s own "inert without configured
    backends" contract."""
    plan = {
        "items": [{"item": "B", "verified_still_missing": True}],
        "follow_ups": [
            {"key": "a", "title": "t", "body": "...", "files": ["CLAUDE.md"]},
        ],
    }
    disp, writer, sinks = _run(_partial(), plan)
    assert disp.action == ft.ACTION_FILED


def test_follow_up_naming_file_under_coordinator_owned_directory_rejects() -> None:
    """A `coordinator_only_files` entry ending in `/` is a directory prefix
    (`coord.models.path_is_sealed_entry`) — a follow-up naming a file
    beneath it must be caught the same way a single named file is, via the
    shared `coord.models.coordinator_doc_overlap` matcher."""
    plan = {
        "items": [{"item": "B", "verified_still_missing": True}],
        "follow_ups": [
            {"key": "a", "title": "Rewrite the operator guide", "body": "...",
             "files": ["docs/OPERATOR_GUIDES.md"]},
        ],
    }
    disp, writer, sinks = _run(
        _partial(), plan, coordinator_owned_docs=("docs/",),
    )

    assert disp.action == ft.ACTION_REJECTED
    assert any("docs/" in r and "coordinator-owned" in r for r in disp.reasons)
    assert not writer.wrote_anything


def test_after_target_pointing_at_parent_rejects() -> None:
    """Same self-pointing hazard as `already_covered_by` (#4079a review):
    an `after` edge naming the parent would splice a dependency on the very
    issue this plan leg is meant to replace. The parent is normally
    declared in the epic's own `## Work order`, so the ordinary
    `declared`-membership check alone would pass this silently."""
    plan = {
        "items": [{"item": "B", "verified_still_missing": True}],
        "follow_ups": [
            {"key": "a", "title": "t", "body": "...", "after": ["#101"]},
        ],
    }
    disp, writer, sinks = _run(_partial(), plan)  # _partial()'s issue_number is 101

    assert disp.action == ft.ACTION_REJECTED
    assert any(
        "after" in r and "parent issue #101" in r for r in disp.reasons
    )
    assert not writer.wrote_anything


# ── #4079b: every named leftover must be addressed, not just claimed ───────


_TWO_LEFTOVER_RESOLUTION = (
    "ISSUE_RESOLUTION: partial — the menu-bar scenario needs covering; "
    "and the cut leg of copy/cut/paste is not actually exercised."
)


def test_resolution_leftover_silently_dropped_rejects() -> None:
    """quadracalc#27's own shape: the resolution text names TWO leftovers
    but the plan only has an item for one of them — `validate_plan` must
    not let the other vanish silently."""
    plan = {
        "items": [
            {"item": "menu-bar scenario", "verified_still_missing": False,
             "evidence": "tests/test_menu.py:42 drives File > Open via the menu bar"},
        ],
        "follow_ups": [],
    }
    disp, writer, sinks = _run(
        _partial(resolution_text=_TWO_LEFTOVER_RESOLUTION), plan,
    )
    assert disp.action == ft.ACTION_REJECTED
    assert any(
        "leftover" in r and "copy/cut/paste" in r for r in disp.reasons
    )
    assert not writer.wrote_anything


def test_resolution_leftover_verified_by_name_only_rejects() -> None:
    """Same shape, but BOTH leftovers get a plan item — the second one's
    "evidence" is just the scenario's name, not a quoted line showing the
    `Ctrl+X` keypress actually ran. A name existing is not evidence."""
    plan = {
        "items": [
            {"item": "menu-bar scenario", "verified_still_missing": False,
             "evidence": "tests/test_menu.py:42 drives File > Open via the menu bar"},
            {"item": "cut leg of copy/cut/paste", "verified_still_missing": False,
             "evidence": "copy_cut_paste"},
        ],
        "follow_ups": [],
    }
    disp, writer, sinks = _run(
        _partial(resolution_text=_TWO_LEFTOVER_RESOLUTION), plan,
    )
    assert disp.action == ft.ACTION_REJECTED
    assert any(
        "copy_cut_paste" in r and "not a quoted line" in r for r in disp.reasons
    )
    assert not writer.wrote_anything
    # The leftover-coverage check itself is satisfied (there IS an item
    # naming the cut leg) — only the bare-symbol-evidence check fires.
    assert not any("leftover" in r for r in disp.reasons)


def test_resolution_leftover_with_quoted_behaviour_passes() -> None:
    """The positive case: both leftovers get an item, and the second's
    evidence actually quotes the behaviour — nothing to reject."""
    plan = {
        "items": [
            {"item": "menu-bar scenario", "verified_still_missing": False,
             "evidence": "tests/test_menu.py:42 drives File > Open via the menu bar"},
            {"item": "cut leg of copy/cut/paste", "verified_still_missing": False,
             "evidence": "tests/test_clipboard.py:88 presses Ctrl+X and asserts "
                         "the selection is removed and placed on the clipboard"},
        ],
        "follow_ups": [],
    }
    disp, writer, sinks = _run(
        _partial(resolution_text=_TWO_LEFTOVER_RESOLUTION), plan,
    )
    assert disp.action == ft.ACTION_ALL_LANDED
    assert len(writer.closed) == 1


def test_single_short_resolution_text_leftover_check_inert() -> None:
    """A single, short resolution ("partial — B remains") has nothing for
    the leftover-coverage check to usefully compare — it must not reject a
    plan on the strength of a weak one-phrase match."""
    disp, writer, sinks = _run(_partial(), HAPPY_PLAN)
    assert disp.action == ft.ACTION_FILED


# ── #4079c: an upstream-blocked partial is filed/linked, not ignored ───────


_UPSTREAM_RESOLUTION = """\
ISSUE_RESOLUTION: partial — blocked on upstream (BLOCKED_ON_UPSTREAM above)

BLOCKED_ON_UPSTREAM: quadraui: Expose per-pane scroll offsets
quadracalc's minimap needs the f32 scroll offset of a pane; today only the
integer line index is public.
"""


def _fake_upstream_outcome(repo="quadraui", title="Expose per-pane scroll offsets",
                            number=9001, status="filed", detail=""):
    gap = SimpleNamespace(repo=repo, title=title)
    return SimpleNamespace(gap=gap, status=status, number=number, detail=detail)


def test_upstream_blocked_partial_parks_without_closing() -> None:
    """quadracalc#48's own shape: the worker's final message carries a
    `BLOCKED_ON_UPSTREAM` marker. Follow-through must not plan or file an
    ordinary follow-up for this — it files/links the upstream gap and
    leaves the parent open as the tracker."""
    calls: list[tuple[str, str, int]] = []

    def fake_process(final_message, *, repo_name, issue_number):
        calls.append((final_message, repo_name, issue_number))
        return [_fake_upstream_outcome()]

    disp, writer, sinks = _run(
        _partial(resolution_text=_UPSTREAM_RESOLUTION),
        plan={},  # never read: the plan leg is never even dispatched
        process_upstream_gaps=fake_process,
    )

    assert disp.action == ft.ACTION_UPSTREAM_PARKED
    assert writer.closed == []
    assert writer.created == []
    assert len(calls) == 1
    assert calls[0][1:] == ("claude-coordinator", 101)
    [(number, comment)] = writer.comments
    assert number == 101
    assert "quadraui#9001" in comment
    assert not sinks.prompts  # the plan leg was never dispatched


def test_upstream_blocked_partial_dry_run_does_not_write() -> None:
    disp, writer, sinks = _run(
        _partial(resolution_text=_UPSTREAM_RESOLUTION),
        plan={},
        process_upstream_gaps=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not be called in dry_run")
        ),
        dry_run=True,
    )
    assert disp.action == ft.ACTION_UPSTREAM_PARKED
    assert not writer.wrote_anything
    assert not sinks.records


def test_upstream_blocked_partial_write_failure_alerts() -> None:
    def fake_process(final_message, *, repo_name, issue_number):
        return [_fake_upstream_outcome()]

    class BrokenCommentWriter(FakeWriter):
        def comment_issue(self, repo_name, number, *, body):
            raise RuntimeError("boom")

    disp, writer, sinks = _run(
        _partial(resolution_text=_UPSTREAM_RESOLUTION),
        plan={},
        writer=BrokenCommentWriter(),
        process_upstream_gaps=fake_process,
    )
    assert disp.action == ft.ACTION_WRITE_FAILED
    assert sinks.alerts


def test_upstream_already_parked_does_not_re_comment() -> None:
    """Re-entry guard (#4079c review): once a key is already parked, a
    repeat sweep must write nothing, not even a second comment."""
    def must_not_be_called(final_message, *, repo_name, issue_number):
        raise AssertionError("must not file/link again once already parked")

    disp, writer, sinks = _run(
        _partial(resolution_text=_UPSTREAM_RESOLUTION),
        plan={},
        process_upstream_gaps=must_not_be_called,
        already_upstream_parked=True,
    )
    assert disp.action == ft.ACTION_UPSTREAM_ALREADY_PARKED
    assert not writer.wrote_anything
    # Never recorded — recording would overwrite the original
    # `ACTION_UPSTREAM_PARKED` entry `upstream_parked_keys` reads back.
    assert not sinks.records


def test_upstream_gap_unresolved_is_not_claimed_parked() -> None:
    """A per-gap failure (`status="unknown-repo"`, no exception raised) must
    not be reported as filed/linked, must alert, and must not be recorded
    as `ACTION_UPSTREAM_PARKED` — the next sweep needs to retry it."""
    def fake_process(final_message, *, repo_name, issue_number):
        return [_fake_upstream_outcome(status="unknown-repo", number=None)]

    disp, writer, sinks = _run(
        _partial(resolution_text=_UPSTREAM_RESOLUTION),
        plan={},
        process_upstream_gaps=fake_process,
    )
    assert disp.action == ft.ACTION_UPSTREAM_PARK_FAILED
    assert writer.comments == []
    assert writer.closed == []
    assert sinks.alerts
    assert sinks.records and sinks.records[0]["action"] == ft.ACTION_UPSTREAM_PARK_FAILED


def test_upstream_gap_not_a_gap_parked_counts_as_resolved() -> None:
    """A `"not-a-gap"` outcome that parked the downstream row directly
    (#3930/#3977) has no upstream issue number and is still a legitimate
    resolution — not a failure."""
    gap = SimpleNamespace(repo="quadraui", title="Expose per-pane scroll offsets")
    outcome = SimpleNamespace(gap=gap, status="not-a-gap", number=None, detail="", parked=True)

    def fake_process(final_message, *, repo_name, issue_number):
        return [outcome]

    disp, writer, sinks = _run(
        _partial(resolution_text=_UPSTREAM_RESOLUTION),
        plan={},
        process_upstream_gaps=fake_process,
    )
    assert disp.action == ft.ACTION_UPSTREAM_PARKED
    [(number, comment)] = writer.comments
    assert number == 101
    assert "not-a-gap" in comment


class TestUpstreamParkedKeys:
    """:func:`coord.board_doctor_followthrough.upstream_parked_keys`."""

    def test_latest_parked_entry_is_a_key(self) -> None:
        decisions = [
            {"key": "quadraui#48", "action": ft.ACTION_UPSTREAM_PARKED},
        ]
        assert ft.upstream_parked_keys(decisions) == {"quadraui#48"}

    def test_a_later_non_parked_entry_for_the_same_key_wins(self) -> None:
        """An operator reopening the row with a fresh resolution records a
        different action afterward — that key must no longer read as
        parked."""
        decisions = [
            {"key": "quadraui#48", "action": ft.ACTION_UPSTREAM_PARKED},
            {"key": "quadraui#48", "action": ft.ACTION_FILED},
        ]
        assert ft.upstream_parked_keys(decisions) == set()

    def test_unrelated_action_is_not_a_key(self) -> None:
        decisions = [{"key": "quadraui#48", "action": ft.ACTION_ALREADY_COVERED}]
        assert ft.upstream_parked_keys(decisions) == set()
