"""Tests for the merged-partial follow-through disposition."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from coord import board_doctor_followthrough as ft
from coord.config import ConfigError, _parse_board_doctor, _parse_board_doctor_repo_config
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
