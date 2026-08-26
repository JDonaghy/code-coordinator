# Architecture

How the coordinator, the agent servers, the CLI, the TUI, and the dashboard fit together.

## The big picture

```
                    ~/.coord/coord.db (SQLite)
                    coordinator.yml
                    GitHub (issues, PRs, comments)
                               ▲
                ┌──────────────┼──────────────┐
                │              │              │
            coord CLI     coord-tui       coord web
            (Python)      (Rust)          (Python, optional)
                │              │              │
                └──────────────┼──────────────┘
                               │  HTTP (port 7433)
                               ▼
                      ┌──────────────┐
                      │ coord agent  │  (one per machine)
                      │ Python HTTP  │
                      │ server       │
                      └──────┬───────┘
                             │ spawns
                             ▼
                      claude -p worker subprocess
                      (runs in isolated git worktree)
```

The system has three kinds of process:

1. **Coordinator clients** — the CLI (`coord`), the TUI (`coord-tui`), and the optional web dashboard (`coord web`). They read shared state from SQLite + `coordinator.yml` + GitHub, and dispatch work by calling agent HTTP endpoints.
2. **Agent servers** — one `coord agent` HTTP server per worker machine. Spawns and tracks `claude -p` subprocesses. Owns the worktrees, the log files, and per-worker lifecycle.
3. **Worker subprocesses** — `claude -p ...` invocations spawned by the agent. They run in isolated git worktrees and have no awareness of the coordinator beyond the briefing they received.

## Why this shape

The split-brain design has three goals:

- **Workers do one thing.** Each `claude -p` session has a single briefing, a single worktree, and no shared context with anything else. Fresh eyes every time.
- **The coordinator stays cheap.** The coordinator runs Opus by default (for triage and review); workers run Sonnet or Haiku. The coordinator's job is to *write good briefings and dispatch them*, not to do the work itself.
- **GitHub is the message bus.** Briefings, completions, failures, and reviews are posted as issue comments with `<!-- coord:event=... -->` markers. Persistent, linkable, parseable by any tool. Surviving a coordinator crash is a matter of reading the latest comments.

## The control-center daemon (`coord serve`, #584) and the "no orchestration daemon" choice

There is **no autonomous orchestration daemon** — nothing drives work on its own; a human (or a periodic `coord notify`) always advances the loop. What *has* been added (#584, the "portable control center") is an optional **board-serving daemon**, `coord serve` (port 7435): it fronts the one canonical SQLite DB + `coordinator.yml` on an always-on host (e.g. dellserver) and serves the board (`GET /board`) + config (`GET /config`) and records results (`POST /result`, `/completion`) over Tailscale, so `coord` and `coord-tui` on *any* machine render and drive the **same** board as bearer-token thin clients instead of each owning a local DB. Consequences:

- **State lives in SQLite + GitHub.** `~/.coord/coord.db` (owned by the daemon host when `coord serve` runs, else local) is the cache; GitHub issue comments are the durable source of truth. Either reconstructs the other.
- **`coord notify` still has to be fired periodically.** The daemon serves state; it does not drive the loop. `notify` polls each agent for completion, posts the GH comments, and triggers the auto-loop (review-on-completion, fix-on-request-changes, re-review-on-fix-completion). Without it, the pipeline visibly freezes — agents finish work but no one notices. Run it on a cron, a `watch`, a TUI timer, or by hand.
  - **For thin-client setups specifically, the sanctioned driver is a systemd user timer on the daemon host** (`deploy/coord-notify.service` + `deploy/coord-notify.timer`, #1311 — install steps in [`AGENT_OPERATIONS.md`](AGENT_OPERATIONS.md#periodic-coord-notify-coord-notify-timer-1311)), firing every few minutes. It exists because the in-process 30s auto-notify that a full `coord-tui` normally runs is **deliberately suppressed** for thin clients (`is_remote_board_service()`, `tui/src/app/data.rs`) — a thin client must not shell out `coord notify` locally against the wrong/absent DB — which otherwise left thin-client-dispatched work with **zero** drivers instead of the intended one.
  - **This timer is meant to be the *only* driver for a thin-client setup — do not layer a second one on top.** A hand-rolled `while`/`watch` loop calling `coord notify` alongside it reintroduces the exact 2026-06-07 incident this architecture guards against: two drivers raced each other and auto-bounced a `request-changes` verdict into redundant fix-2/fix-3 workers, duplicating work a human had already done by hand. That incident is why #476 (cap auto-fix at `pipeline.max_review_iterations`, default 5, before every auto-fix dispatch — `coord/auto_loop.py`) and #477 ("the TUI owns the loop — single driver, visible, killable" — the reasoning behind the thin-client suppression above) exist. If a timer-based `coord notify` ever proves insufficient, the fix is to extend *this one driver*, not add a competing one.
  - **Known residual gap, deliberately deferred rather than blocking #1311:** nothing in the auto-loop checks "is there a live, human-attended `--interactive`/`--chat`/`--troubleshoot`/`--fix-of`/`--merge-of` pane open on this issue right now" before this timer's `coord notify` fires an auto-fix — #602 fixed that class of race only for the TUI's own auto-offer popup, not for `notify`'s auto-loop dispatch path. Low risk while flat headless-only dispatch is the common case; revisit if an auto-fix ever collides with a live interactive session on the same issue.
- **`coord` and `coord-tui` are peers, not nested layers.** The TUI does not shell out to `coord`. Both are independent clients of the same state — directly against SQLite, or (with `coord serve`) against the daemon's HTTP API. (See the [Divergence risk](#divergence-risk) section.)

## The web dashboard (`coord web`, port 7434) and Phone Control Center

`coord web` is an optional **web dashboard** that serves two things from the same port:

1. **The React PWA** (Phone Control Center) — a mobile-optimised single-page app for reviewing pipeline status and triggering gate actions from a phone over Tailscale. Built separately from source (`npm run build` in `coord/dashboard/webapp/`) and served from `dist/`.
2. **The JSON REST API** — `GET /api/pipeline`, `POST /api/pipeline/action`, `GET /api/board`, etc. — called by the React app, and also directly curl-able.

Like `coord-tui`, `coord web` is a **peer client** of the same shared state (`~/.coord/coord.db`). Run it on the always-on host so phones can reach it via Tailscale at `http://<hostname>:7434`.

**Full runbook** (build → serve → phone access → API reference → ToS posture): **[docs/PHONE_WEBAPP.md](PHONE_WEBAPP.md)**.

## The agent HTTP API

The `coord agent` server exposes a small, JSON-only HTTP API on port 7433. Any client — `coord`, the TUI, `curl`, Postman — can call it. There's no authentication; Tailscale is the auth boundary.

| Method + Path | Purpose |
|---|---|
| `POST /assign` | Dispatch a worker on this machine. Body is the `AssignmentSpec` JSON. Returns `{"id": ..., "status": "running"}` |
| `GET  /status` | Active + completed assignments, with progress updates, exit codes, cost data |
| `GET  /health` | Version, uptime, last_update result, machine info |
| `GET  /logs/<id>` | Full worker log |
| `GET  /stream/<id>` | Server-sent events: tails the worker log |
| `POST /cancel/<id>` | Send SIGTERM to a running worker |
| `POST /restart` | Restart the agent process (waits for active workers up to `cancel_timeout`) |
| `POST /update` | `pip install --upgrade code-coordinator` then re-exec |
| `POST /worktree-clean` | Prune stale worktrees for completed assignments |

Example: dispatch a worker with curl.

```bash
curl -X POST http://laptop.tailnet:7433/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_name": "myrepo",
    "repo_path": "~/src/myrepo",
    "issue_number": 42,
    "issue_title": "Fix the auth bug",
    "briefing": "Read src/auth.py and fix the timeout issue.",
    "files_allowed": [],
    "files_forbidden": [],
    "pull_repos": [],
    "type": "work"
  }'
```

That's all the agent does. Everything else — the merge queue, the plan brain, the notify pipeline, the review dispatch logic — lives in the coordinator clients and runs *on the machine where you invoke them*.

## Where each `coord` subcommand actually runs

Concretely: when you run `coord X` from your laptop, what happens?

| Subcommand | Reads | Writes / Calls |
|---|---|---|
| `coord status` | DB, agent `/status`, `/health` | Stdout |
| `coord assign` | DB, `coordinator.yml` | Agent `/assign`, DB |
| `coord plan` | DB, GitHub issues | Spawns `claude -p` for the brain, DB |
| `coord approve` | DB, agent `/status`, GitHub | Agent `/assign`, DB |
| `coord merge` | DB (merge_queue, board), `gh pr ...` | `gh pr create/merge`, DB |
| `coord notify` | Agent `/status`, DB | `gh issue comment`, agent `/logs/<id>`, DB, **triggers auto-loop** |
| `coord bounce` | DB | Agent `/assign` (fix worker), DB |
| `coord pr` | DB | Agent `/assign` (PR worker + review worker), DB |
| `coord agent update` | `coordinator.yml` | Agent `/update`, `/health` |
| `coord agent` | n/a (this **is** the agent server) | HTTP listener on port 7433 |

Notice that `coord agent` is the odd one out — it's the only subcommand that's a server, not a client. All others are short-lived clients.

## The auto-loop, end to end

The most coordinated path through the system is the review/fix/re-review loop. It's worth walking through in one place because the logic is spread across `coord/notify.py`, `coord/auto_loop.py`, `coord/review.py`, and `coord/merge_queue.py`.

1. `coord assign laptop myrepo 42` posts to `POST /assign` on laptop. Agent spawns `claude -p`.
2. Worker finishes, pushes branch, exits. Agent records `status=done`.
3. `coord notify` (run periodically) polls laptop, sees the completion, posts a GH comment. **Review auto-dispatch is gated on the Test stage:** the default pipeline order is `Work → Test → Review → Merge`, and when `default_gates` orders `"test"` *before* `"review"` (the default — `PipelineConfig.test_precedes_review()`), `dispatch_pending_reviews` only fires once the work has a `passed`/`skipped` Test verdict — recorded with `coord test <work_id> --passed|--skipped` (or the **P/S** keys on the Test stage in the TUI). A work assignment left at *Pending Test* gets no review, so it never merges and the TUI "Go" does nothing — this is the single most common reason a story silently stops progressing. (A `failed` test routes to a fix, not a review.) The explicit `coord review`/`coord pr` paths bypass this gate so a human can always force a review. With the gate satisfied (and `reviews.enabled`), `dispatch_review` sends a `type="review"` assignment to a *different* machine.
4. Reviewer reads the diff, runs tests, posts `gh pr review` with `--approve` / `--request-changes` / `--comment`. The reviewer's log carries a machine-parseable verdict header.
5. `coord notify` runs again. Sees the review completion, parses the verdict, persists `review_verdict` and `review_findings` to the DB.
6. **If `request-changes`**: `run_for_review_transition` calls `_dispatch_fix`, which posts a `type="work"` `[fix-N]` assignment with `target_branch=<original work's branch>` so the fix lands on the same branch (not an orphan).
7. **If `approve`**: nothing happens immediately — the merge gate (`has_approved_review`) will allow the merge next time `coord merge` runs.
8. Fix worker finishes. `coord notify` detects the completion (via the `fix_completions` classifier added in #278), calls `run_for_fix_transition`, which dispatches a fresh review against the fix-1 assignment.
9. Re-review approves → merge gate passes. `coord merge` rebases and merges. Conflict on rebase → conflict-fix worker auto-dispatched (`#241`), pinned to the original branch via `target_branch` (`#277`), pushes the rebase, merge re-enqueues, succeeds.

If any single link is broken — most often `coord notify` not running — the whole loop stalls. The TUI helps spot this because completed assignments without comments are visible in the pipeline view, but the fix is always "run `coord notify`."

> **Updated (2026-07-20):** the flat, stage-serial loop above is still what runs for a standalone
> issue. What has since **shipped on top of it** is the milestone tier of
> **[`PIPELINE_V2.md`](PIPELINE_V2.md)** — this issue loop now nests inside a milestone pipeline with
> Gates A–D (`coord acceptance mock`, `coord milestone gate-b`/`gate-c`/`ship`), and the develop +
> feature-branch model (#934) that routes each milestone's issues onto a `feature/ms-NN` branch. See
> [The milestone tier and the branch model](#the-milestone-tier-and-the-branch-model-934) below.
> **[`ORACLE_LOOP.md`](ORACLE_LOOP.md)** — the tight in-session loop where the worker iterates against
> a sealed, independently-authored acceptance oracle and the coordinator re-runs it externally as the
> trust gate — is available (`coord acceptance author`/`run`/`record`) but is **opt-in**, not the
> default path for new work.

## The milestone tier and the branch model (#934)

The auto-loop above sequences a *single* issue. For work that spans many issues, that loop **nests inside a milestone pipeline** ([`PIPELINE_V2.md`](PIPELINE_V2.md)) so the expensive gates (architecture review, acceptance authoring) are paid **once per milestone**, not once per issue.

**What an epic is.** A GitHub tracking issue carrying the `epic` label (`TRACKING_ISSUE_LABEL`, `coord/milestone_order.py`) whose body holds a `## Work order` block — a DAG of children written as `- #762 {group: A, after: #761}` (`group` = parallel cohort, `after` = hard dependency). Membership is backed by GitHub's native **sub-issues API** (`coord/parentage_github.py`; the older `## Sub-issues` checklist is migrated by `coord milestone sync`, #1061). The `coord milestone` group (`coord/commands/milestone.py`) drives it: `chat` (steward session to draft the order), `write-order`/`order` (write/read the DAG), `dispatch` (promote the ready frontier into the pipeline, draining as `after` deps clear), and the gate commands below.

**The four gates** wrap the whole milestone (distinct from the per-issue Work → Test → Review → Merge stages):

| Gate | Command | Enforces |
|---|---|---|
| **A — contract** | `coord acceptance mock` | A mock-first black-box `contract.md` exists on the default branch before any issue dispatches — checked by `gate_a_status` (`coord/milestone_dispatch.py`) for repos with an `acceptance.drivers` entry; repos without one skip the gate. |
| **B — architecture** | `coord milestone gate-b` (#933) | An independent `type=review` confirms the *assembled* milestone was built to the Gate-A contract. `request-changes` = bounce, not ship. |
| **C — acceptance** | `coord milestone gate-c` (#932) | The full accumulated acceptance suite is green (integration gaps *between* issues that per-issue runs miss). |
| **D — ship** | `coord milestone ship` (#934) | Merges `feature/ms-NN → develop`, gated on Gate B (`approve`) + Gate C (re-run live), then closes the tracking issue on success — the completion signal Gate D itself watches for (below). |

**Driving the four gates as one machine (#1929, `coord/milestone_gate.py`).** The four commands above are each a manual operator step. `coord milestone drive <repo> <epic>` puts a milestone under **gate control**: a durable `GateRecord` per `(repo_name, tracking_issue)` — current gate, `entered_at`, `waiting_on`, and the ordered list of gates already `cleared` — persisted under the `milestone_gates` `board_meta` key, deliberately in the same seam as `milestone_drains` so **one board read answers "what is this milestone doing"**. `coord.serve_app._milestone_gate_tick` advances it one step per daemon tick through `gate_a → work → gate_b → gate_c → gate_d → done`. Because the position lives on the board and not in daemon memory, a restart mid-milestone resumes at the recorded gate and never re-runs a cleared one — the same resumability `drive-issue.sh` gets from re-reading the board.

**Gate D's completion signal.** `probe_milestone`'s `shipped` probe reads the tracking issue's own GitHub state (`state == "CLOSED"`), mirroring the "closed epics" convention already used elsewhere (`github_ops.get_closed_epics`). Concretely, that means `coord milestone ship` closes the tracking issue as its last step once the merge succeeds — it does not merely open/merge the PR. Gate D never ships anything itself (`ship` stays an explicit operator action); it only observes that closure to advance to `done` and deregister. A milestone whose tracking issue is closed by any other means (an operator closing it by hand, a differently-shaped ship path) satisfies the gate identically — the check is the issue state, not who or what closed it.

Layering is deliberate: `evaluate_gate(gate, probes)` is **pure** (gate name + a `GateProbes` value in, one `GateStep` out — no board, no GitHub, no clock), so every edge in the machine is decided in exactly one place and any edge is testable without I/O; `probe_milestone()` is the I/O half and reuses the *existing* readers (`milestone_dispatch.gate_a_status`, `gate_b.latest_gate_b_verdict`) so the gate walk and the manual CLI can never disagree about whether a gate is satisfied. `work` is the only state with a side effect, and it delegates to `plan_dispatch` + `dispatch_entry` rather than adding a second dispatch path. Every other edge is currently an explicit, logged **hold** — the machine reports why it cannot advance and stays put. There is no silent fall-through anywhere in the module; that is the failure mode that produced the 240-minute advisory spin in the per-issue driver.

**`drive` vs. `milestone.auto_dispatch` — mutually exclusive per milestone, gate driving wins.** `milestone.auto_dispatch` gates the *legacy standalone drain* (`_milestone_drain_tick`): a milestone registered by a pre-#2335 `coord milestone dispatch` whose frontier the daemon re-drains with no gate walk around it. (Since #2335 the bulk `coord milestone dispatch` path enqueues the whole `## Work order` DAG into the drive-queue — `after=` edges carry the dependencies, the DQ-4 tick launches, and nothing new registers for this drain.) A milestone with a gate record is driven by `_milestone_gate_tick` instead, which owns the drain as its `work` state — so `_milestone_drain_tick` **skips** any `(repo_name, tracking_issue)` that has a gate record. Without that exclusion a milestone parked at Gate A could still have its frontier dispatched by the independently-gated drain path. Gate driving is consequently its own per-milestone opt-in (`coord milestone drive`) and `_milestone_gate_tick` is *not* behind the global flag — hiding it there would make `drive` silently no-op.

**Exactly one overseer, structurally (#1930).** The gate tick is the sole owner of a gate-controlled milestone's `work` drain. `coord milestone drive` only ever writes/re-persists the `GateRecord` — it never calls `apply_step` or dispatches, so running it twice (any operator, any machine) is inert, not a race. `coord milestone dispatch`, the manual non-gate CLI, now refuses outright against a milestone with a gate record — before #1930 it had no way to know one existed and would dispatch the same frontier the gate tick's `work` state owns, the same "two gates disagreeing about whether work may start" shape #1870 fixed for the drive queue, reached from an operator's keyboard instead of a second host's timer.

**The branch model** (`coord/branch_model.py`, opt-in via a repo's `develop_branch:` config, `coord/config.py`). When `develop_branch` is set on a repo *and* an issue belongs to a GitHub Milestone:

- each milestone gets one `feature/ms-NN` integration branch off `develop` (`feature_branch_name` → `feature/ms-{n}`; created idempotently by `ensure_feature_branch_exists` before dispatch);
- that milestone's issues branch off `feature/ms-NN` and their PRs merge **back into it**, not into `main`;
- `feature/ms-NN → develop` happens only via Gate D (`coord milestone ship`); `develop → main` is a separate, un-automated release cut.

`resolve_base_branch(repo, milestone_number)` is the single resolver — it returns `feature/ms-NN` only when `develop_branch` is set and a milestone number is present, else `repo.default_branch or "main"`. It is **fail-open**: a repo that never sets `develop_branch`, or an issue with no milestone, resolves to exactly today's single-branch `main` flow, with no extra `gh` call. The resolver is threaded through the five branch-deciding seams — Work dispatch (`coord/dispatch.py`), review/diff base (`coord/review.py`), merge target (`coord/merge_queue.py`, `coord/commands/merge.py`), reconcile (`coord/reconcile.py`), and the auto-loop (`coord/auto_loop.py`) — each guarded on `repo.develop_branch` so the default path is untouched. (The interactive `--review-of`/`--merge-of` surfaces are deliberately **not** yet wired into the milestone base — a documented follow-up.)

## Observability: `coord usage`, `coord audit` and `coord report`

Three read-only commands surface what the fleet did, all routed through the same board seam as `coord status` (daemon when `coord serve` is up, local DB otherwise) so a thin client never opens `~/.coord/coord.db` directly.

- **`coord usage`** (`coord/commands/status.py`, aggregation in `coord/usage_rollup.py`) — per-assignment/model/issue **cost, tokens, and wall-clock time**, over a time window (`--today`/`--week`/`--month`/`--since`) and grouped by issue, repo, or time bucket (`--by-issue`/`--issue N`/`--by repo|week|month`/`--by-time`). Cost is the captured `cost_usd` when real, else estimated from token counts × `PricingConfig` rates.
- **`coord audit`** (query surface `coord/commands/audit.py`, store `coord/audit.py`) — a durable, append-only, keyset-paginated **event log**: dispatch, verdicts, merges, notifications. `record_audit()` is invoked at the `state._*_local` / `issue_store` **write choke points** (e.g. `_record_test_verdict_local`), so there is one row per real transition regardless of topology, and the write is best-effort — it never raises into the caller (a board mutation must succeed even if the audit write fails). Event names reuse the `coord:event=` vocabulary from `coord/comments.py` so the audit log and the GitHub message bus agree.
- **`coord report`** (`coord/reports.py`, CLI `coord/commands/report.py`, daemon `GET /report` + `GET /report/{id}`) — the **fold** on top of `coord audit`: where `coord audit` is the event *stream*, a report answers "what moved in this window and where did it end up". One report ships today, `issue-activity`: one row per `(repo, issue)` with `started_at`, machines, fix iterations, ordered Test/Review verdicts, `merged_at`, `drive_exit` and a derived `outcome`, plus a `notes` block of anomalies (the load-bearing one: a driver that exited non-zero on an issue that merged anyway — a merge given up on while still converging). The engine is **server-side** because the audit trail lives on the daemon host; `fold_issue_activity()` is pure (entries + window in, `ReportResult` out — no DB, no clock) and the pagination that feeds it walks the audit keyset cursor until the window is covered, adding an explicit truncation note rather than silently shipping a short answer. Adding a report costs one `ReportDef` in `REPORTS`. Running one is strictly read-only — no board write, no reconcile side effect. **CSV export (#1765) is server-side, once**: `coord.reports.result_to_csv()` is the single serializer behind `coord report run --format csv`, `GET /report/{id}?format=csv` (`text/csv` + a `Content-Disposition` filename) and the coord-tui Reports panel's per-section Export action, so all three emit identical bytes. It has to live here rather than in a client because the wire values are **raw** (`started_at` is an epoch float, `machines` is a list) and every renderer turns them into display strings via `column_meta` — a client-side CSV would export the formatting instead of the data, and would depend on when Export was clicked. `notes` ride along as leading `#` comment lines rather than being dropped.

## When a merge isn't happening

*Mirrored as a skill: `coord/skills/merge-stuck-triage/SKILL.md` — keep both in sync.*

A story that won't merge — the TUI "Go" does nothing, `coord merge` skips it, the box stays grey/pending — almost always traces to one of these gates. Check in order:

1. **Test gate (the #1 cause).** No review is dispatched until the work's Test stage has a verdict (see step 3 of the auto-loop above). **Symptom:** work `done`, but no `type="review"` assignment exists and `review_state` is null. **Fix:** `coord test <work_assignment_id> --passed` (`--skipped` for trivial, `--fail --reason "…"` for broken), then `coord pr <id>` opens/reuses the PR and dispatches the review. In the TUI: **P / S / F** on the Test stage.
2. **Review not approved.** The merge gate is `has_approved_review` — a `type="review"` assignment with `review_verdict="approve"` for the work behind the queue entry. No review or `request-changes` → merge refuses with *"review required but not approved"*.
3. **CI red.** Merge is gated on `gh pr checks` (#240). Failing/pending checks block it (surfaced in the queue entry's `error`). `coord merge --force-merge` overrides.
4. **PR conflicts.** `mergeable=CONFLICTING` → `coord merge` auto-dispatches a conflict-fix worker (#241) to rebase; on success it re-enqueues and merges, on a semantic conflict it marks the entry `HUMAN_REQUIRED`. This worker runs invisibly — check for a `type="conflict-fix"` running assignment before assuming nothing happened.
5. **Queue clog / group halt.** `coord merge` processes each `(repo, target_branch)` group together; pre-#292 it `break`s on the first blocked entry (now skip-and-`continue`). A queue full of stale entries (for already-closed issues) can stall everything behind them. To merge one issue past a clog: `coord merge --repo <r> --order <assignment_id>` jumps it to the front. To declog: delete `merge_queue` rows whose GitHub issue is already closed — they are never auto-pruned (the closed-issue filter only blocks *new* enqueues).
6. **Post-bounce keying (#292).** After a review bounce (request-changes → fix → approve), the queue entry can be keyed to the *original* (request-changes) work while the approval sits on the *fix* assignment, so `has_approved_review` fails. Fixed in #292; the pre-fix manual workaround was re-keying `merge_queue.assignment_id` to the approved fix.

**Live-on-pull vs needs-release:** the merge/review/auto-loop logic (`merge_queue.py`, `auto_loop.py`, `reconcile.py`, `cli.py`) runs in fresh `coord` CLI invocations, so a `git pull` of the coordinator clone makes fixes live immediately. Only agent-side code (`agent.py` / `agent_app.py`, the long-running `coord agent` service) needs a release + `coord agent update` — see [AGENT_OPERATIONS.md](AGENT_OPERATIONS.md).

## When an issue is sitting in the pipeline you never dispatched

*Mirrored as a skill: `coord/skills/pipeline-limbo-triage/SKILL.md` — keep both in sync.*

Board vs Pipeline membership is **label-driven, not assignment-driven**. An open issue with *zero* assignments can still show up in the Pipeline — because it carries a `status:ready` label. The lifecycle (defined in `coord/cli.py`'s `refine`/`ready`/`backlog` commands and mirrored in `tui/src/app.rs`) is:

| State | Signal | Where it shows |
|---|---|---|
| **Backlog** | `coord` label, **no** `status:*` label, no assignments | Board sidebar |
| **Refining** | `status:refining` | Board sidebar |
| **Refined / Ready** | `status:ready`, no assignments | **Pipeline** (a "pending / ready-to-dispatch" card) |
| **In-progress** | has a `type="work"` assignment | Pipeline |
| **Done** | merged | Pipeline (Done group) |

So the "ready-but-not-started" state — `coord` + `status:ready`, no work assignment — is a Pipeline card that looks dispatched but isn't. **The refinement chat and new-issue chat flows finalize by flipping `status:refining → status:ready` (via `coord ready`), which silently parks the issue in this limbo.** That's how issues "appear in the pipeline" without anyone dispatching them.

**To drop one back to the Board:** `coord backlog <repo> <issue>` strips `status:refining`/`status:ready`, returning it to unscoped Backlog. It's symmetric with `coord refine` / `coord ready`, and writes through to the local `issues` cache so the TUI reflects it on the next refresh. (The TUI's right-click *Drop to Backlog* fires the same command — #266.)

**Known gap (#359):** refinement/plan-only issues get stranded in the Pipeline this way; the desired fix is for the chat dialogs to route straight to **Plan** or **Work** instead of leaving an issue in the `status:ready` limbo stage.

## Divergence risk

The CLI and the TUI are peer clients of the same state. They re-implement the same business logic in two languages. There is no compiler check keeping them in sync.

**What stays in sync naturally:**

- Anything that reads SQLite directly and renders it (status, queue rows, assignment metadata).
- Anything that POSTs to agent HTTP endpoints (`/assign`, `/cancel`, `/status`).
- Anything that hits GitHub via `gh` (the TUI shells out for these).

**What needs manual mirroring:**

- State-machine classification (e.g. `PipelineMergeState`: NotApplicable / NoQueue / Merged / BlockedOnReview / BlockedOnCi / Ready). The Python `coord/merge_queue.py` and the Rust `tui/src/app.rs` both have to know the same rules.
- Conflict classifier signal strings (`_REBASEABLE_SIGNALS`, `_HUMAN_SIGNALS`).
- Review gate predicate (`has_approved_review`).

**Symptom of divergence:** the TUI paints a stage as `BlockedOnReview` when in fact the Python side would proceed, or vice versa. Today (May 2026) the TUI mostly delegates to queue rows, so most gate logic is in Python — but the TUI is growing, and the more it implements directly, the more divergence opportunity.

**That next step has since shipped in part:** `coord serve` (#584) makes the daemon the canonical board holder with the CLI and TUI as thin clients — but the *gate logic* still lives in both languages (the daemon serves rows, it doesn't yet centralise the state-machine rules). Closing the remaining drift is now tracked as explicit tech debt: a `BoardService` facade (#749) and generating the wire types from one schema so the Rust/TS mirrors can't diverge — #748 hardens the `/board` parse (the blank-board class), #750 removes the hand-mirror. See the Tech Debt milestone (epic #751).

## Design decisions — the settled rationale

Moved out of `CLAUDE.md` by #2195. These are *settled* explanations: they document why the
system has the shape it has. Nothing here changes what a worker editing this repo must do,
which is why it lives here rather than in the file every worker leg loads. The one-line
statements of these rules remain in `CLAUDE.md`'s "Key Design Decisions"; the reasoning is
here.

### Agent servers are dumb dispatchers

They spawn `claude -p` and track the subprocess. All intelligence is in the coordinator
brain. This is deliberate: the agent is a long-running daemon on someone else's machine, so
every rule it enforces is a rule that needs a release plus `coord agent update` to change.
Keeping it thin keeps the deploy lane cold.

### Conflict rules are inferred, not configured — and the inference runs on ONE path only

The deliberate choice is that there is **no DSL for conflict zones**. A configuration
language for conflicts would need maintaining in lockstep with the code it describes;
inference degrades gracefully instead.

What that inference actually is, precisely: a line in the planning brain's **prompt**
(`coord/brain.py:34`) —

> If two issues would touch overlapping files in the same repo, do NOT assign them
> simultaneously — flag the conflict and pick the higher-priority one.

Two consequences worth stating plainly, because both have cost real work:

- **It is a prompt instruction, not a mechanism.** Nothing computes a file set or compares
  one; the planning model is asked to notice. There is no code path that can refuse a
  dispatch for file overlap.
- **It only runs on the `coord plan` → `coord approve` path.** The drive queue dispatches
  through `coord drive` → `coord assign` and never consults the brain, so unattended work —
  which is most work — got no overlap check at all. Both same-file collisions on
  2026-08-14 (quadraui #306/#309 against #307/#308, and code-coordinator #2234 against
  #2230, all four appending to a single file) arrived through the queue.

`claim.py` does not close this gap: it is issue-level only (an active board assignment, or
an existing `issue-{N}-*` remote branch), with no file awareness.

**What DOES cover the queue now (#2247): `coord/overlap_predict.py`.** At `coord
drive-queue add`, the candidate issue's own declared file list — an explicit `## Files` /
`files:` block in its body, never an inference from prose — is compared against the **real
diffs** of every in-flight branch in that repo (`GhOps.get_compare_files`, the same compare
call #1720's advisory fence uses). Only one side of that comparison is a guess, which is
what makes it worth doing. Three properties are load-bearing and should not be "improved"
away:

- **It ORDERS, never refuses.** An overlap chains the newcomer `--after` the incumbent (a
  flag the queue already had) and records why. A false-positive prediction that blocked a
  dispatch would be worse than the conflict it prevents; a false negative merely returns
  you to the pre-#2247 behaviour. `--no-predict-overlap` opts out per add.
- **No prediction is a valid answer.** No `## Files` block, an unreachable board, a branch
  whose compare fails — each degrades to exactly today's enqueue, never to a bad guess.
  Two *queued* entries with no branches yet are compared declaration-to-declaration only,
  because that is two authors' statements rather than two guesses.
- **Its accuracy is measured.** Every auto-`--after` is a checkable claim ("these two file
  sets will intersect"), recorded in the audit log with both sides' file lists. `coord
  drive-queue overlap-report` scores those claims against the branches' real diffs and
  records each verdict, so a **false positive** — an entry serialized for nothing — is a
  number. Nobody can safely loosen an unmeasured predictor.

Known gap, deliberate: the prediction runs at ENQUEUE, not at tick-launch time. Work that
goes in flight after an entry was queued does not retroactively reorder it — #2246's
post-merge sibling sweep is the exact, zero-prediction backstop for those.

**`file_groups` and `exclusive_files` do not exist.** Earlier revisions of this note offered
them as optional power-user config; no such keys are read anywhere in `coord/`, and none
appear in any `coordinator.yml`. Treat any reference to them as stale.

The exact, zero-prediction signal that *is* available is GitHub's own `mergeable` field
(`GhOps.check_pr_mergeable`). #2246 put it on the post-merge sweep, where it catches the
collisions prediction cannot foresee; #2231 covers the merge-time reading that still
short-circuits before the merge is attempted. Prediction reduces how often that sweep has
to fire; it does not replace it.

### `coordinator.yml` lives in `~/.coord/`, not the repo checkout

Config-path resolution (`coord.config.resolve_config_path`) is `$COORD_CONFIG` →
`~/.coord/coordinator.yml` → `./coordinator.yml` (first existing wins). The canonical home is
`~/.coord/coordinator.yml`, mirroring `~/.coord/coord.db` + `~/.coord/client.toml`, so the
tool runs on a machine with **no repo checkout**. `./coordinator.yml` is a development
fallback only — relying on it makes the loaded file depend on your CWD (this bit us: a
near-empty `~/src/<repo>/coordinator.yml` stub shadowed the real `~/coordinator.yml`).
`coord config` and `coord serve` both print the resolved path so it is never ambiguous which
file is loaded.

**On a thin client, that resolved path is a CACHE, not the config** —
`~/.coord/coordinator.remote.yml` (`coord.client.REMOTE_CONFIG_CACHE`) is re-fetched from the
daemon's `GET /config` on essentially every command and overwritten wholesale, so edits to it
silently revert, including from the `coord config` you run to check them.

**Fleet config is never edited at the daemon host's `~/.coord/coordinator.yml` path
directly** — that path is a symlink into the `coord-settings` checkout, and `sed -i`/most
editors write-and-rename over it, silently replacing the symlink with a disconnected regular
file that stops being version-controlled (#1832). Edit
`~/src/coord-settings/coord/coordinator.yml` (commit + push there, then `git pull` on the
daemon host), then restart `coord-serve` (with `coord sessions --remote` empty first).
`coord diagnose --config-provenance` detects a broken symlink. See
[`OPERATING_GOTCHAS.md`](OPERATING_GOTCHAS.md) (#8 and #14).

### Merge is gated on CI checks (#240), and an empty check list is not a pass (#1904)

Before merging a PR, `coord merge` calls `gh pr checks` via `coord.ci_store.CiStore` and
refuses when any check has failed or is still running. Pass `--force-merge` to override (the
failures are surfaced in the TUI and CLI output so the override is intentional).
`ci_store: { type: none }` in `coordinator.yml` disables the gate entirely.

**A PR with zero reported checks is not automatically clear to merge.** `checks == []` is
ambiguous — "no CI configured" vs. "CI exists but never triggered" (a throttled webhook, a
wedged run, a path-filtered-out workflow) — so the gate calls
`CiStore.expects_checks(repo, pr)` to tell them apart: `NoOpCi` (the `type: none` opt-out)
always answers `False`; `GitHubCi` answers based on whether the repo declares any GitHub
Actions workflows at all, failing closed (`True`) on a read error. When checks are expected
but absent, the entry blocks with a `checks_absent` event / `CI never ran:`-prefixed reason —
distinct from `checks_pending`/`checks_stale` — at all three surfaces that read CI status
(`--plan`, `--dry-run`, and the real merge), so they can never disagree with each other
again.

### Mechanical merge conflicts auto-rebase (#241)

When `coord merge` fails because the worker's branch is out of date on a rebaseable conflict,
the coordinator dispatches a `type="conflict-fix"` worker that rebases, resolves obvious
additive merges, runs tests, and `git push --force-with-lease`. On success the merge
re-enqueues automatically; on failure the entry is marked `HUMAN_REQUIRED` and surfaced in
the TUI. Semantic conflicts (same function modified two ways) are not attempted — the worker
exits and posts a comment for manual resolution. `gh` is denied for `conflict-fix` workers;
only the coordinator drives merge retries.

### Why Test precedes Review (the #520 reversal)

The pipeline order is `Work → Test → Review → Merge`: the smoke test runs *before* the
PR/review (the natural order — smoke before PR). This reversed the #520-era "get the review
over with first" workaround, which existed only because the Testing stage used to be painful;
once the agent-assisted Testing stage became smooth, the workaround cost more than it saved
(review cycles were being burned on untested code).

It is enforced in two places that must stay in sync:

1. The **displayed** stage order comes from `pipeline.default_gates = ["test","review","merge"]`
   (`coord/config.py`). An old DB carrying the #520-era `["review","test","merge"]` is
   migrated in `coord/db.py`.
2. The **headless** auto-loop holds review dispatch until the work has a `passed`/`skipped`
   test verdict whenever `default_gates` orders test before review —
   `PipelineConfig.test_precedes_review()` drives `dispatch_pending_reviews`
   (`coord/review.py`).

The explicit `coord review` / `coord pr` paths stay ungated so a human can always force a
review. The merge gate already required a test verdict (`requires_smoke`), so the human test
touchpoint just moved earlier.

### Interactive testing + merge agents (leg 3c / A3, #350/#581/#306/#606)

From a Pipeline row's right-click menu, the TUI routes **board-driven** verdicts (never
TTY-scraped, ToS §3.7) along `Work → Test → Review → Merge`:

- **Start testing** = `coord assign --interactive --smoke-of <work_aid>` — a read-only testing
  agent in the live checkout, recording its verdict via `coord test --passed|--fail`.
- A `passed`/`skipped` test → **pass→review**, which launches the interactive review.
- An approved review → **Start merge** = `--merge-of <work_aid>`, which worktrees and
  proactively rebases onto the default branch (#306), resolves mechanical conflicts (semantic
  ones with the operator), runs tests, `git push --force-with-lease`, then runs
  `coord verify-merge` and — if clean — `coord merge` itself to complete it (#606).
- A `failed` test **or** a request-changes review → one-key **`--fix-of`** on the same branch.
  A test-fail takes the identical action as a request-changes; `--fix-of` accepts a review id
  **or** a test-failed work id (the #581 front door).

The merge agent is still gated on CI/review/smoke (a gate failure is reported, not forced) and
`verify-merge` (#604) runs first.

### The notifier tells you when NOBODY IS COMING — and nothing else (#1632)

`coord/notifier/` pushes to a self-hosted ntfy on the daemon host (over Tailscale, so no event
text leaves the tailnet) when the pipeline **has stopped or is stalled and will not advance
without a human**: a halted drive, a gate parked `HUMAN_REQUIRED`, a worker's `STUCK:`, a
stall that survived `drive`'s nudge (#1593), a leg running far past comparable work, a fleet
CRIT that invalidates in-flight work.

It is **not** an error channel and **not** a progress feed — a failed test, a request-changes
review and a mechanical merge conflict are all things the auto-loop handles, and pushing them
is what trains an operator to mute the channel. In normal operation this fires approximately
never.

"Far too long" is **learned, never a fixed timeout**: p90 of a `(repo, type, tier)`-stratified
population built from the durations milestone #37 already records, with a cold-start state
(<5 samples → a generous absolute ceiling, said out loud in the message) so it never fires off
a population of one.

Quiet hours (22:00–08:00 daemon-local, `notifications.quiet_hours`) are a **deferral window,
not a filter** — events are held and delivered as one 08:00 digest, nothing is discarded, and
**no severity pierces them**. The only exception is `coord drive --urgent`, which is a
deadline rather than a severity and expires with the drive.

The whole subsystem is **advisory and isolated** (#1485's lesson): `tick()` never raises,
every collector source fails open, state lives in its own `~/.coord/notifier.json`, and an
unreachable ntfy server cannot affect dispatch, routing, the board or any verdict. It rides
#1616's daemon clock rather than shipping a second one, and reads `drive`'s stall decision
rather than defining "stalled" again. Off by default. Full rationale:
[`NOTIFIER.md`](NOTIFIER.md).

### Recovery and freshness

- **Failure reassignment.** Failed assignments can be retried on a different machine via
  `coord retry`. With `concurrency.auto_reassign: true`, reconciliation auto-retries on a
  different machine.
- **Dependency freshness checks.** Before dispatching, `coord approve` checks whether upstream
  repos are up-to-date on the target machine. Stale dependencies trigger warnings, or an
  auto-pull with `--auto-pull`.

## File map

| Path | What lives there |
|---|---|
| `coord/agent.py`, `coord/agent_app.py` | The HTTP server (`coord agent` subcommand). Subprocess management for `claude -p`. |
| `coord/cli.py` | All other `coord X` subcommands. Click entry points. |
| `coord/brain.py` | The planning brain: gathers context, calls `claude -p`, parses proposals. |
| `coord/merge_queue.py` | The merge state machine: enqueue, sequence, process, gate. |
| `coord/auto_loop.py` | Review → fix dispatch (#243), fix → review dispatch (#278). |
| `coord/notify.py` | Polls agents, posts GH comments, triggers the auto-loop. |
| `coord/conflict_fix.py` | Rebase-on-merge-failure worker dispatch (#241). |
| `coord/branch_model.py` | #934 develop + feature-branch resolver (`resolve_base_branch`, `ensure_feature_branch_exists`); opt-in via a repo's `develop_branch`. |
| `coord/commands/milestone.py`, `coord/milestone_order.py`, `coord/milestone_dispatch.py`, `coord/milestone_chat.py` | The milestone tier: `## Work order` DAG, frontier dispatch, Gates A–D, steward chat. |
| `coord/usage_rollup.py`, `coord/usage.py` | `coord usage` cost/token/time aggregation over the board. |
| `coord/audit.py`, `coord/commands/audit.py` | Durable event log: `record_audit()` at the state write-waist; `coord audit` query surface. |
| `coord/reports.py`, `coord/commands/report.py` | Report engine (#1742): `REPORTS` registry, pure `fold_issue_activity()`, paginated audit fetch; `coord report` CLI + `GET /report`, `GET /report/{id}`; #1765 CSV export (`result_to_csv`, `--format csv`, `?format=csv`). |
| `coord/review.py` | Adversarial review dispatch + verdict parsing. `dispatch_pending_reviews()` is the **bulk** path used by `reconcile()` and `coord notify`: it bounds dispatch with a per-pass cap (`reviews.max_auto_dispatch_per_pass`, default 5) and a **surge gate** (`reviews.flood_threshold`, default 12 — above it, refuse all and require `reviews.allow_review_flood: true` / `COORD_ALLOW_REVIEW_FLOOD=1`). This is the flood guard: a backlog "unmasking" (e.g. dropping a gate that had suppressed reviews) can't fire hundreds of metered reviews at once. See the 2026-06-08 incident. |
| `coord/state.py`, `coord/db.py` | SQLite schema and access helpers. |
| `coord/dashboard/` | The web dashboard (`coord web`). Optional. |
| `tui/src/app.rs` | The Rust TUI (uses quadraui primitives). Historically one ~48k-line file; being decomposed into an `app/` module — see the Tech Debt milestone (epic #751 / #742–#745). |
| `coordinator.yml` | Single source of truth for repos, machines, dependencies, policies (incl. `pipeline.default_gates`). Canonical location `~/.coord/coordinator.yml`; resolved `$COORD_CONFIG` → `~/.coord/coordinator.yml` → `./coordinator.yml`, so a machine needs no repo checkout. `coord config` / `coord serve` print the resolved path. |
| `~/.coord/coord.db` | Local state cache. Survives across sessions; rebuilt from GitHub on `coord resume`. |
| `~/.coord/logs/<id>.log` | Per-assignment worker log (on the agent machine that ran it). |
| `~/.coord/worktrees/<id>/` | Per-assignment git worktree (on the agent machine). Cleaned up by `coord-tui` press `c` or `POST /worktree-clean`. |
