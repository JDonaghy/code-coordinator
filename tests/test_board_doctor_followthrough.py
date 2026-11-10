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

    @property
    def wrote_anything(self) -> bool:
        return bool(self.created or self.children or self.closed) or self.bodies[50] != EPIC_BODY


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
    disp, writer, sinks = _run(_partial(epic_body=body), plan, writer=FakeWriter(body))

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
        return ft.GitHubFollowThroughWriter(), fake

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
