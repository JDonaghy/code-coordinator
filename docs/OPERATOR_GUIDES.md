# Operational guides — operator-facing

**Not needed to work on this repo.** These are runbooks for driving the fleet; a worker or
reviewer never needs this file.

This index used to live at the bottom of [`CLAUDE.md`](../CLAUDE.md), where it was loaded into
every worker leg, every review leg, and every coordinator session — and re-read on every turn of
each — despite opening with "a worker or reviewer can stop reading here" (#2787). It moved here
under the rule that file states for itself: *if it does not change what a worker does, it belongs
in `docs/`.*

## Read before operating the fleet

- [`OPERATING_GOTCHAS.md`](OPERATING_GOTCHAS.md) — **READ BEFORE OPERATING.** Traps that each cost a real dispatch, real money, or real lost work, and are invisible from the code. The headline: **a merged fix is not a live fix.**
- [`AGENT_OPERATIONS.md`](AGENT_OPERATIONS.md) — releases, propagation, the deploy lanes, agent installs, and the **`~/.coord-venv` must-be-PyPI invariant**. **Read end-to-end before touching any agent install** — don't re-derive it.
- [`DRIVE_QUEUE.md`](DRIVE_QUEUE.md) — the durable, board-backed driver (`coord drive-queue`, #1750). **Read the top section before queuing more than ~2 issues on one repo.**

## Reference

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — how it all fits together, plus the settled design rationale. Two diagnostic entry points: [why a merge/review isn't happening](ARCHITECTURE.md#when-a-merge-isnt-happening) (**check here first when "Go does nothing"**) and [why an issue is in the Pipeline you never dispatched](ARCHITECTURE.md#when-an-issue-is-sitting-in-the-pipeline-you-never-dispatched).
- [`COST_DISCIPLINE.md`](COST_DISCIPLINE.md) — dispatch economics; what to send to a worker versus keep in the coordinator session. Also holds the **"dispatch, don't do"** rule and the coordinator session's own **`coord`-seam-over-`gh`** rule.
- [`NOTIFIER.md`](NOTIFIER.md) — the "nobody is coming" push channel (`coord notifier`).
- [`CUSTOMER_PORTAL.md`](CUSTOMER_PORTAL.md) — the coord-portal bridge. **Its
  [operator runbook](CUSTOMER_PORTAL.md#running-one-end-to-end--the-operator-runbook) is
  the one to read when a customer request needs turning into coordinator work** — repo
  creation, the project↔repo mapping, the decomposition session, Gate-A mocks, and how a mock
  gets back to the client for sign-off, in the order you actually do them.
- [`CUSTOMER_FACING_APPS.md`](CUSTOMER_FACING_APPS.md) — shipping a repo whose
  **external** customer judges it by looking at it (natal-chart today). Why `Work → Test →
  Review → Merge → auto-deploy` makes that customer the first human to see the change, the
  per-PR preview / `uat` gate / visual-baseline pattern that fixes it, and the manual runbook
  to use until the gate exists.
- [`ORACLE_LOOP.md`](ORACLE_LOOP.md) — sealed acceptance suites, Gate-A contracts, the test-author agent.
- [`DATA_STORE_SELECTION.md`](DATA_STORE_SELECTION.md) — choosing between D1, Supabase
  and managed Postgres for a new app. **A reasoning guide, not an approved-vendor list** —
  `house_stack_context()` already derives what the fleet runs mechanically (#2997), and a
  hand-maintained blessed-stack list would rot and suppress the reasoning. Holds the
  Hyperdrive fact that stops D1 being a one-way door.
- [`PHONE_WEBAPP.md`](PHONE_WEBAPP.md) — Phone Control Center v1 runbook and the `/api/pipeline` surface.
- [`GRAPHIFY_SETUP.md`](GRAPHIFY_SETUP.md) — installing the knowledge graph on a new machine (four layers, all of which fail *silently*).
- [`EPHEMERAL_WORKERS.md`](EPHEMERAL_WORKERS.md) — on-demand Azure worker VMs per epic. **The tailnet ACL is the security boundary** — `agent_app.py` has no authentication.
- [`MAC_MINI.md`](MAC_MINI.md) — adding a Mac mini; sizing, provisioning, and what non-macOS work routes there. The port itself is [`CROSS_PLATFORM.md`](CROSS_PLATFORM.md) (milestone #39).
- [`WSL_WINDOWS_WORKER.md`](WSL_WINDOWS_WORKER.md) — using a Tailscale-connected WSL2 box as the Windows worker for quadraui/vimcode's Win-GUI ports. Not the coord-itself Windows port.
- [`FORGE_MIGRATION.md`](FORGE_MIGRATION.md) — surviving a forge outage (cheap) versus leaving a forge (expensive); milestone #58 / epic #1902.

## Two deploy facts that bite most often

- **`coord-tui` ships as a locally-built binary, not via PyPI.** After a `tui/` PR merges, rebuild and reinstall locally: `cd tui && cargo build && cp target/debug/coord-tui ~/.local/bin/coord-tui`. Workers should not bump versions for tui-only changes.
- **Turn off Claude Code's Agent View when driving the fleet** — set `"disableAgentView": true` in `~/.claude/settings.json` (the Claude-Code *client* config, not `~/.coord/`; restart to apply), or its cross-session roster pops up mid-type when another session changes stage. Full note in [`AGENT_OPERATIONS.md`](AGENT_OPERATIONS.md#operator-claude-code-settings-driving-the-fleet).

## The webapp v1 milestone, for the record

The Phone Control Center **v1 milestone (#700–#703) is shipped**: `coord web` binds
`0.0.0.0:7434` on the always-on host, reachable from a phone via the Tailscale MagicDNS name
(`http://dellserver:7434`) and installable as a PWA via "Add to Home Screen" in Safari / Chrome.
The full runbook is [`PHONE_WEBAPP.md`](PHONE_WEBAPP.md); what a worker needs is the short
section in [`CLAUDE.md`](../CLAUDE.md#coord-web-phone-control-center).

Acceptance status, which is operator context rather than a worker rule:

- **Shipped:** `playwright.config.ts` + specs in `coord/dashboard/webapp/e2e/`, and
  `smoke_tests.capability_rules` routes `coord/dashboard/webapp/**` → the `browser` capability,
  so the Test stage lands on a browser-capable machine.
- **Shipped:** the `web-playwright` acceptance driver (#1539) — it is in `SUPPORTED_KINDS`, so
  `coord acceptance run/record` can drive the webapp.
- **Not shipped:** the seeded-board fixture server (#1538), so today's specs are a smoke net,
  not a deterministic oracle. Milestone #51 / epic #1537
  ([`WEB_CONTROL_CENTER.md`](WEB_CONTROL_CENTER.md)).
- **`browser` is advertised by one machine (elitebook)** until #1541 adds it to dellserver, so
  all web testing funnels through the dev box — and since 2026-08-01 that machine's probe reads
  UNMET (#1678).
