# Epic authoring — how parent↔child linkage actually works

**Audience: the coordinator session.** Filing an epic is coordinator work, never a
worker task. This page is the reference behind the `create-epic` skill; the skill is
the checklist, this is the *why*.

Epic parentage in this fleet is **plain markdown, parsed with an exact grammar**. It is
not GitHub's native sub-issue links, not labels, and not milestone membership. Every
failure mode on this page is **silent** — the write succeeds, GitHub renders the issue
correctly, and the epic reports zero children.

## Why markdown and not the GitHub sub-issues API

The REST sub-issues API is live on these repos and `coord.parentage_github.GitHubParentage`
implements it. The board-payload publish step deliberately does **not** use it: a live
API call per pipeline row per poll is not affordable. `coord.parentage.MarkdownParentage`
— which runs `coord.milestone_order.parse_sub_issues` over the epic's own body — is the
cheap path that actually feeds the board. Consequence worth stating plainly:
**populating GitHub's native sub-issue links does not fix a blank Epic column.**

## The two independent systems

An epic has to satisfy both, and they fail differently.

| | `## Sub-issues` | `## Work order` |
|---|---|---|
| Parsed by | `parse_sub_issues` | `parse_work_order` |
| Feeds | Pipeline Epic column, nesting, `--lint-stale-epics` | **Plans panel counts** (ready / blocked / in-flight / done) |
| Written by | `coord milestone add-child` | `coord milestone write-order` |
| Requires a milestone? | no | yes — on the epic *and every node* |

They share one grammar and differ only in heading. An epic with a perfect
`## Sub-issues` block and no `## Work order` still reads `no_work_order`, 0/0, in the
Plans panel.

## The milestone requirement

`coord.plans.aggregate_repo_plans` emits **one row per open GitHub milestone**, and
`find_tracking_issue` selects the epic by matching `issue["milestone"]["number"]`. So:

- An epic with `milestone: null` **cannot appear in the Plans panel at all.** No label,
  body or checklist content rescues it. (claude-coordinator#3431 adds milestone-less
  epic rows; until it lands and deploys, this is absolute.)
- `find_tracking_issue` returns the **first** `epic`-labelled issue under a milestone.
  **Two epics under one milestone means one silently wins** and the other is invisible.
  Give each epic its own milestone.

## The grammar

```
- [ ] #1214 {after: #1213} — prose describing the child
```

Unforgiving in five specific ways, all silent:

1. **The heading is exactly `## Sub-issues`.** The regex is
   `^#{1,6}\s*Sub-issues\s*$`. `## 9. Sub-issues`, `## Sub-issues (phase 1)` and
   `## Children` all mean the parser finds **no section** and returns empty.
2. **The number comes immediately after the checkbox.** `- [ ] **#1213** — …` raises,
   and because `MarkdownParentage.parent()` wraps the parse in
   `except Exception: continue`, **one bad row voids every child of that epic** — the
   blast radius is the epic, not the line.
3. **Annotations come immediately after `#N`, before the prose.** `- [ ] #1214 — text
   {after: #1213}` does not error; the annotation is simply **ignored** and the
   dependency edge disappears. Formatting *after* the number is fine:
   `- [ ] #1206 — **tranche 2**` parses.
4. **Every `-` bullet in the section must be an issue row.** Prose lists, housekeeping
   checkboxes and unfiled-phase notes all raise. Put them under their own heading — a
   heading ends the section.
5. **Every `{after: #M}` target must be declared in the same section.**

Blockquotes and non-bullet lines are skipped safely, so explanatory notes are fine as
long as they are not `-` bullets.

## The sequence

```bash
coord milestone create <repo> --title "<plan name>" --description "<paragraph>"   # -> M
coord issue create <repo> --title "EPIC: <...>" --body-file epic.md --label epic  # -> E
coord issue create <repo> --title "<...>" --body-file child.md                    # -> C1…
coord milestone assign <repo> E M          # the epic AND every child
coord milestone assign <repo> C1 M
coord milestone add-child <repo> E C1      # splices `## Sub-issues`, validates first
coord milestone add-child <repo> E C2 --after C1
printf -- '- #C1\n- #C2  {after: #C1}\n' | coord milestone write-order <repo> E
```

`coord issue create` has no `--milestone` flag yet (claude-coordinator#3432), which is
why step 4 is separate — and why it gets skipped.

`coord milestone capture` composes create + create + assign for a lightweight plan stub,
but the issue it creates is deliberately **not** `epic`-labelled, so it shows as
`epic:—` until promoted.

## Verifying — the write path is not the check

Every failure above returns exit 0. Confirm the effect:

```bash
coord plans --repo <repo>                       # real counts, not `epic:—`/`no_work_order`
coord plans --lint-stale-epics --repo <repo>    # the epic must NOT be listed
```

`--lint-stale-epics` reporting `children: 0 open / 0 closed (of 0)` is ambiguous: it
means "declared scope already shipped" **or** "the body doesn't parse". Only parsing the
body tells them apart:

```bash
python3 -c "
import json,subprocess,sys; sys.path.insert(0,'.')
from coord.milestone_order import parse_sub_issues
b=json.loads(subprocess.run(['coord','issue','view','<repo>','<E>','--no-comments','--json'],
    capture_output=True,text=True).stdout)['body']
try: print([(n.issue_number,n.checked,n.after) for n in parse_sub_issues(b).nodes]
           or 'EMPTY — heading did not match')
except Exception as e: print('RAISED:', e)
"
```

**`coord issue list --json` carries no `body`.** Use `coord issue view` for anything
that parses a body, or you will conclude every epic is broken.

A deliberately-empty `## Sub-issues` section is legitimate and parses to zero nodes —
different from an unparseable one, and only body-level parsing distinguishes them.

## Worked failure: vimcode#1212

Filed 2026-09-20 with the `epic` label, a thorough body, and two real children. Invisible
in the Plans panel; children unparented. Four independent defects, each sufficient alone:
no milestone; `## 9. Sub-issues`; `- [ ] **#1213** — …`; and prose bullets inside the
section. Fixed by creating milestone #10, assigning all three issues to it, renaming the
heading, unbolding the rows, moving the prose lists under their own headings, and writing
a `## Work order`.
