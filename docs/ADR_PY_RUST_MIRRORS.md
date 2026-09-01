# ADR: the Python↔Rust mirrors are known, measured, and deliberately unfixed

**Status:** accepted (#2900, Phase 4 of #2894)
**Related:** `docs/ADR_COORD_TUI_CI.md` (the generated-artifact gate),
`docs/ADR_COORD_WEB_CI.md` (the track-latest install decision it inherits),
`docs/STORE_SERVICE.md` (the resource-route milestone), #2899 (the split).

## Context

#2899 moved `coord-tui` into its own repository. Everything that used to be
guaranteed *for free* by "both halves are in one checkout, so one PR and one
CI run see both" now has to be guaranteed explicitly, or not at all.

#2900 closed the biggest hole — **the wire**. Both directions of the daemon
contract are now generated from the served OpenAPI document
(`coord.serve_app.openapi_spec()`) and byte-compared by coord-tui's CI:

| coord-tui file | generator | gate |
| --- | --- | --- |
| `src/app/types/generated.rs` | `scripts/codegen.py --rust` (#1941) | `--rust --check` |
| `src/app/types/generated_requests.rs` | `scripts/codegen.py --rust` (#2900) | `--rust --check` |
| `tests/fixtures/board_sample.json` | `scripts/gen_board_fixture.py` (#748) | `git diff --exit-code` |

That gate runs in coord-tui's `codegen-drift.yml` and is asserted *from this
repo* by `coord.health.checks.coord_tui_ci_pin`, so the cross-repo contract is
visible and gradeable from the side that can break it.

**What #2900 did not close is the rest of this document.** Underneath the
wire there is a second, older coupling: a set of *behavioural* rules
re-derived independently in Python and in Rust from the same raw `/board`
rows. Those are not wire shapes — no amount of schema generation reaches
them — and they were only ever kept in step by review discipline and by both
files being one `grep` apart.

## Decision

**Record the mirrors; do not build a second mirror to guard them.**

Specifically:

1. The inventory below is the complete, enumerated list, with file:line
   pairs on the Python side and symbol names on the Rust side. It is
   **kept complete mechanically**, not by good intentions:
   `tests/test_py_rust_mirror_inventory.py` fails if `coord/**.py` grows a
   reference to a coord-tui source file that this document does not list.

2. These mirrors are **unguarded**, and that is a stated position rather than
   an oversight. Nothing compares `stage_status_for` against its Rust twin;
   nothing can, cheaply, because the twin is a different language in a
   different repository and the thing being compared is a decision procedure,
   not a struct.

3. **If a mirror bites, the remedy is a wire-level fix, not a second mirror.**
   The correct response to "the TUI badged a stage differently from `coord
   status`" is to move that derivation onto the wire — have the daemon
   compute it once and ship the answer in the `/board` payload, where
   `scripts/codegen.py` then guards it like everything else — not to add a
   Rust-source-text scraper on the Python side.

   That is not hypothetical guidance. `coord/board_bool_guard.py` *was* the
   second-mirror answer: a Python check that read Rust source text to
   cross-reference INTEGER-backed booleans. It was retired in #2897 the
   moment the generated structs made it redundant (`docs/ADR_COORD_TUI_CI.md`).
   Reintroducing that shape for stage projection would repeat the mistake
   with a harder-to-parse subject.

## The inventory

Line numbers are Python-side, at the commit that introduced this document;
they drift, the symbol names do not — grep the symbol if the line has moved.

### Tier 1 — genuine logic mirrors (a divergence is a visible behaviour bug)

| Python | Rust (in the `coord-tui` repo) | What is mirrored |
| --- | --- | --- |
| `coord/stage_projection.py:3`, `:38` | `src/app/pipeline.rs` — `stage_status_for`, `merge_stage_status_for`, `test_stage_status_for`, `issue_has_any_approved_review`; `StageStatus` | The entire stage/gate derivation from raw `/board` rows, plus the four-value status vocabulary. The largest mirror by far. |
| `coord/drive_queue.py:828`, `:876` | `src/app/drive_queue.rs` — `summarize_drive_queue`, `DriveQueueSummary`, `DriveQueueLevel` | *"Ported field-for-field."* The ascending-severity ranking `empty < normal < stalled < held < blocked`, including the #2186 rule that blocked outranks a fleet-held gate. |
| `coord/drive.py:57`, `:873` | `src/app/pipeline.rs::gate_a_contract_exists_for` | Whether an issue's Gate-A acceptance contract exists. A third copy (`coord.milestone_dispatch.gate_a_status`) is in-repo; all three key on `coord.acceptance.gate_a_contract_path`. |
| `coord/notify.py:2010` | `src/app/pipeline.rs::test_stage_status_for` | Which test states collapse to the red FAILED badge. |
| `coord/drive_state.py:180` | `src/app/pipeline.rs::milestone_tracking_issue_for` | Resolving an issue to its milestone tracking issue (`None` for a plain issue). |
| `coord/state.py:1376` | `src/app/settings_ui.rs::record_test_verdict_conn` | The `test_state` → legacy `smoke_test` derivation (`passed`→`pass`, `failed`→`fail`, `skipped`→ leave alone). **Partly addressed** by #2900: the wire is now typed (`TestVerdictRequest`), but which value to *derive* remains a client-side policy decision on both sides. |
| `coord/reports.py:2645`, `:2648` | `src/app/pipeline.rs::completed_rows`, `src/app/reports.rs` | A report rendered as a Pipeline-local detail tab rather than a catalogue entry — the coupling `reports.py` explicitly calls out. |

### Tier 2 — string/key contracts (a divergence fails loudly, or is inert)

| Python | Rust (in the `coord-tui` repo) | What is mirrored |
| --- | --- | --- |
| `coord/approved_work.py:90`, `:136` | `src/app/types.rs::ApprovedSubmission` | The wire keys deserialized *without* `#[serde(default)]`, i.e. the ones Python must always emit. Not covered by codegen: `ApprovedSubmission` is not a `/board` projection. |
| `coord/ci_store.py:38` | `src/app/types.rs::CiCheckSummary` | The CI-rollup shape behind the "2✓ 1✗" badge. |
| `coord/decomposition_chat.py:51` | `src/app/dialogs.rs::maybe_bind_pending_decomposition_chat` | A literal briefing string the TUI pattern-matches on, because `Assignment` has no `submission_id` column. |
| `coord/board_wire.py:109` | `src/app/pipeline.rs::parse_allowed_globs_from_issue_body` | The issue-body marker parsed for acceptance globs. |
| `coord/board_schema.py:292` | `src/app/types.rs` + `types/generated.rs` | INTEGER-backed booleans. **Closed:** `INTEGER_BACKED_BOOLEANS` plus the generated structs are now the sole guard (#2897). Listed for completeness. |
| `coord/issue_store.py:243` | `src/app/data.rs` | `exit_code` / `failure_reason` column reads. **Largely closed** by #2895 (coord-tui reads no SQL) and #1941. |
| `coord/agent.py:6301` | `src/app.rs` health refresh | A 2 s probe timeout the two sides agree on by convention. |
| `coord/dashboard/server.py:1413`, `:1450` | `src/app/mod.rs` (`summarize_drive_queue(&self.data.drive_queue)`), `src/app/drive_queue.rs` | The web dashboard's drive-queue summary and its add-time guards, mirroring the TUI's. A *third* copy of the Tier-1 `drive_queue.py` rule. |
| `coord/tui_release.py:55`, `:321` | `src/main.rs::version_string` | The shape of the version banner `coord`'s release tooling parses out of a built coord-tui binary. Divergence fails loudly (an unparseable version), not silently. |
| `coord/commands/terminal.py:5` | `src/app/terminal.rs` | The embedded-terminal session contract. |
| `coord/commands/_common.py:308` | `src/app/pipeline.rs` | Which acceptance subcommand the TUI's Pipeline dispatch fires. |
| `coord/health/checks/deploy_lane_facts.py:115` | `src/app/data.rs` | Not a logic mirror: the *structural marker path* `resolve_coord_tui_checkout` uses to recognise a coord-tui checkout, and the pre-#2899 spelling it must NOT match. Listed because it is a hard-coded coord-tui path and would break on a layout change there. |
| `coord/machine_metrics.py:9` | `src/app/data.rs::spawn_machine_metrics` | The per-agent `GET :7433/metrics` polling contract (`coord.agent_app.metrics`: a `psutil` cpu/mem snapshot, `503` when psutil is absent). Two *independent* samplers now read that one endpoint — coord-tui's in-process `VecDeque`, live only while its panel is open, and the daemon-side ring buffer added by #3020. Not a logic mirror and not a divergence risk in itself: both sides consume the same wire, which is the endpoint's own contract, not a re-derived rule. Listed because it is a hard-coded reference to a coord-tui source symbol, and because a future change to the `/metrics` payload has to be made aware of *both* readers. |

## Consequences

**Accepted:** a Tier-1 divergence can ship and will be found by a human
noticing the TUI and `coord status` disagreeing, not by CI. That is a real
cost, taken knowingly: the alternative — a cross-repo behavioural conformance
harness — is a large, slow, permanently-maintained thing to guard rules that
have in practice changed rarely and always deliberately.

**Also accepted, and worth knowing:** every prose reference in the table
above still spells the Rust path `tui/src/app/...`, the pre-#2899 in-repo
layout. Those paths are now `<coord-tui>/src/app/...`. They were left
verbatim rather than rewritten in a sweep across a dozen modules, which would
have been a large diff touching files this story otherwise does not — this
document is the redirection. `tests/test_py_rust_mirror_inventory.py` matches
on the `tui/src/app` spelling for that reason.

**Bounded:** the inventory cannot silently shrink or grow. A new mirror added
to `coord/**.py` fails the inventory test until it is listed here, which is
the cheapest possible forcing function: it costs one table row, and it makes
adding a mirror a visible decision rather than an incidental comment.
