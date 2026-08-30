"""Fleet ``coordinator.yml`` provenance: is the live config still the one that
was reviewed? (#1779)

``coordinator.yml`` used to be a plain file at ``~/.coord/coordinator.yml`` on
the daemon host, hand-copied from a tracked copy elsewhere — real, and
undetectable, content drift. That copy has since been replaced by a
**symlink** into the ``JDonaghy/coord-settings`` checkout
(``~/src/coord-settings/coord/coordinator.yml`` by default), so the live file
*is* the tracked file and content drift is structurally impossible. That
closes one hole and opens three narrower ones, none visible from a running
fleet:

1. **The symlink gets replaced by a regular file.** ``coord init`` offers to
   overwrite ``coordinator.yml`` (``coord/commands/setup.py``), and any
   ``scp``, ``cp``, or editor that writes-and-renames breaks the link. The
   fleet is then silently back to running an untracked file — this is the
   highest-value check here, and the loudest finding.
2. **The checkout is dirty.** A direct edit to the live path writes *through*
   the symlink into the checkout's working tree — recoverable, but the
   running config now has uncommitted changes nobody has reviewed.
3. **The checkout is behind (or ahead of) ``origin``.** Someone pushed a
   config change that was never pulled onto the daemon host (or committed
   locally but never pushed), so the reviewed intent and the running fleet
   disagree.

A fourth state is *not* a problem: **no checkout at all.** The coord-settings
checkout is deliberately absent from every machine except the daemon host and
the operator's box (agents, thin clients, and every ephemeral Azure worker
have no reason to carry it, and — see #1779's cross-repo constraint — a
dispatched worker must never be able to edit the file governing its own
concurrency limits and review gates). :func:`config_provenance` reports that
as a neutral skip, never a warning.

Mirrors :mod:`coord.graph_health`'s shape (``coord diagnose --graph``'s "is
the artifact current?" check): a single read-only, best-effort probe function
plus a renderer, wired into ``coord diagnose`` as another local-machine sweep
alongside ``--graph``/``--orphan-worktrees``. No network access is required —
sync-vs-``origin`` is judged against the existing remote-tracking ref, never a
fresh ``git fetch``.

**A fourth failure mode lives here too, of the same shape (#2953):** the
config can be exactly the reviewed one — symlinked, clean, in sync — and
still be silently inert, because ``smoke_tests.capability_rules[].files`` is
a plain-prefix match (``coord/smoke.py:204``, ``str.startswith``, NOT a
glob) with nothing validating that a prefix matches anything real. A rule
with a stray ``**`` suffix (#1072) or a prefix missing a directory level
(#2953's own ``src/gtk/`` vs. ``quadraui/src/gtk/``) is syntactically fine,
passes review, and contributes nothing at dispatch time — routing silently
falls through to "any repo-capable machine" instead of a capability-matched
one. :func:`capability_rule_health` walks every configured repo's *locally
available* git-tracked files and reports a prefix as dead (matches nowhere)
or partial (matches in some repos but a deeper occurrence elsewhere suggests
it should reach further) — the same "no checkout here is not a problem"
precedent as :func:`config_provenance` applies per repo.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coord.config import Config

# The tracked config's path inside the coord-settings checkout.
TRACKED_CONFIG_REL = Path("coord") / "coordinator.yml"


def default_settings_dir() -> Path:
    """``$COORD_SETTINGS_DIR``, defaulting to ``~/src/coord-settings``.

    Computed fresh on every call (not a module-level constant) so a test can
    override it via ``monkeypatch.setenv`` without also having to fight a
    value baked in at import time.
    """
    env = os.environ.get("COORD_SETTINGS_DIR")
    return Path(env).expanduser() if env else Path.home() / "src" / "coord-settings"


def default_live_config_path() -> Path:
    """Where the daemon's live ``coordinator.yml`` lives.

    Mirrors the first two steps of :func:`coord.config.resolve_config_path`
    (``$COORD_CONFIG``, then ``~/.coord/coordinator.yml``), computed fresh at
    call time rather than reusing ``coord.config.USER_CONFIG_PATH`` — that is
    a module-level constant fixed at import time against whatever ``$HOME``
    was then, so it would not follow a test's environment override.

    Deliberately excludes ``resolve_config_path``'s third step
    (``./coordinator.yml`` in cwd): that is a repo-checkout dev convenience
    that the running daemon never uses, and folding it in here would let a
    stray dev ``coordinator.yml`` in whatever directory this check happens to
    run from masquerade as the fleet's live config.
    """
    env = os.environ.get("COORD_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".coord" / "coordinator.yml"


@dataclass
class ConfigProvenance:
    """Provenance of one machine's live ``coordinator.yml``."""

    live_path: Path
    checkout_dir: Path
    # False on every machine with no coord-settings checkout — the expected,
    # neutral state everywhere except the daemon host / operator box.
    checkout_present: bool = False
    live_exists: bool = False
    is_symlink: bool = False
    resolved_target: Path | None = None
    # True only when the symlink resolves to THIS checkout's tracked
    # coord/coordinator.yml — not just "somewhere inside the checkout".
    in_checkout: bool = False
    dirty: bool = False
    dirty_files: list[str] = field(default_factory=list)
    ahead: int = 0
    behind: int = 0
    upstream: str | None = None
    sync_unknown_reason: str | None = None

    @property
    def skip(self) -> bool:
        """No coord-settings checkout here — nothing to check, not a problem."""
        return not self.checkout_present

    @property
    def regression(self) -> bool:
        """The highest-value finding: the live config is no longer a symlink
        into the reviewed checkout (#1779's central failure mode)."""
        return self.checkout_present and not self.in_checkout

    @property
    def in_sync(self) -> bool:
        return self.sync_unknown_reason is None and self.ahead == 0 and self.behind == 0

    @property
    def healthy(self) -> bool:
        return (
            self.checkout_present
            and self.in_checkout
            and not self.dirty
            and self.sync_unknown_reason is None
            and self.behind == 0
        )


def _git(checkout_dir: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(checkout_dir),
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (subprocess.SubprocessError, OSError):
        return None


def config_provenance(
    *, live_path: Path | None = None, checkout_dir: Path | None = None
) -> ConfigProvenance:
    """Provenance of *live_path* (default: this machine's live
    ``coordinator.yml``) against *checkout_dir* (default: the coord-settings
    checkout).

    Read-only and best-effort: git failures, a missing checkout, or a missing
    live file all return a populated :class:`ConfigProvenance` rather than
    raising. Never touches the network — sync-vs-origin is read from the
    existing remote-tracking ref, never a fresh ``git fetch``.
    """
    live_path = live_path or default_live_config_path()
    checkout_dir = checkout_dir or default_settings_dir()
    prov = ConfigProvenance(live_path=live_path, checkout_dir=checkout_dir)

    # #1779: the checkout's mere presence is the whole "is this machine in
    # scope" signal — absent on every machine except the daemon host and the
    # operator box. Anything past this point only runs where it's present.
    if not (checkout_dir / ".git").exists():
        return prov
    prov.checkout_present = True

    prov.is_symlink = live_path.is_symlink()
    prov.live_exists = live_path.exists()  # follows the symlink; False if dangling/absent

    tracked_path = checkout_dir / TRACKED_CONFIG_REL
    if prov.is_symlink:
        try:
            prov.resolved_target = live_path.resolve()
        except OSError:
            prov.resolved_target = None
        if prov.resolved_target is not None:
            prov.in_checkout = prov.resolved_target == tracked_path.resolve()

    if not prov.in_checkout:
        # Regression case (not a symlink at all, or a symlink pointing
        # somewhere else) — nothing downstream (dirty/sync) is meaningful
        # against a file that isn't even the tracked one.
        return prov

    # ── Checkout clean? ──────────────────────────────────────────────────
    status = _git(checkout_dir, "status", "--porcelain", "--", str(TRACKED_CONFIG_REL))
    if status is not None and status.returncode == 0:
        out = status.stdout.strip()
        if out:
            prov.dirty = True
            prov.dirty_files = [line for line in out.splitlines() if line.strip()]

    # ── In sync with origin? (no fetch — existing remote-tracking ref only) ─
    upstream = _git(checkout_dir, "rev-parse", "--abbrev-ref", "@{upstream}")
    if upstream is None or upstream.returncode != 0:
        prov.sync_unknown_reason = (
            "no upstream tracking ref configured for the coord-settings checkout"
        )
        return prov
    prov.upstream = upstream.stdout.strip()

    counts = _git(
        checkout_dir, "rev-list", "--left-right", "--count", f"{prov.upstream}...HEAD"
    )
    if counts is None or counts.returncode != 0:
        prov.sync_unknown_reason = f"could not compare HEAD to {prov.upstream}"
        return prov
    parts = counts.stdout.split()
    if len(parts) != 2:
        prov.sync_unknown_reason = (
            f"unexpected `git rev-list` output comparing HEAD to {prov.upstream}"
        )
        return prov
    try:
        prov.behind, prov.ahead = int(parts[0]), int(parts[1])
    except ValueError:
        prov.sync_unknown_reason = (
            f"unexpected `git rev-list` output comparing HEAD to {prov.upstream}"
        )
    return prov


def format_provenance_lines(prov: ConfigProvenance) -> list[str]:
    """Human-readable report lines for *prov* (used by ``coord diagnose
    --config-provenance``). Each of the three failure modes gets its own,
    distinctly-worded line — never collapsed into one generic "drift" line."""
    lines: list[str] = []

    if not prov.checkout_present:
        lines.append(
            f"· no coord-settings checkout at {prov.checkout_dir} — skipping "
            "config provenance (expected on every machine except the daemon "
            "host and the operator box)"
        )
        return lines

    fix = f"ln -sf {prov.checkout_dir / TRACKED_CONFIG_REL} {prov.live_path}"
    if not prov.is_symlink:
        exists_note = "exists as a REGULAR FILE" if prov.live_exists else "does not exist"
        lines.append(
            f"✗ REGRESSION: {prov.live_path} is NOT a symlink into "
            f"{prov.checkout_dir} — it {exists_note}. The fleet is running "
            f"an untracked config again. Fix: {fix}"
        )
        return lines
    if not prov.in_checkout:
        lines.append(
            f"✗ REGRESSION: {prov.live_path} is a symlink, but resolves to "
            f"{prov.resolved_target} — not {prov.checkout_dir / TRACKED_CONFIG_REL}. "
            f"Fix: {fix}"
        )
        return lines

    lines.append(f"✓ {prov.live_path} → {prov.resolved_target} (symlinked into the checkout)")

    if prov.dirty:
        lines.append(
            f"⚠ uncommitted changes to {TRACKED_CONFIG_REL} in {prov.checkout_dir} "
            f"({len(prov.dirty_files)} line(s) of `git status --porcelain`) — the "
            "running config has changes nobody has reviewed"
        )
    else:
        lines.append(f"✓ {prov.checkout_dir}: checkout is clean (no uncommitted config changes)")

    if prov.sync_unknown_reason:
        lines.append(f"? sync vs origin unknown — {prov.sync_unknown_reason}")
    elif prov.behind and prov.ahead:
        lines.append(
            f"⚠ diverged from {prov.upstream}: {prov.behind} commit(s) behind, "
            f"{prov.ahead} ahead — reconcile on the daemon host"
        )
    elif prov.behind:
        lines.append(
            f"⚠ {prov.behind} commit(s) behind {prov.upstream} — reviewed config "
            f"not yet deployed. Fix: git -C {prov.checkout_dir} pull"
        )
    elif prov.ahead:
        lines.append(
            f"⚠ {prov.ahead} commit(s) ahead of {prov.upstream} — local commit(s) "
            "not yet pushed"
        )
    else:
        lines.append(f"✓ in sync with {prov.upstream}")

    return lines


def summary_line(prov: ConfigProvenance) -> str:
    """The machine-readable trailer ``coord diagnose --config-provenance``
    prints, mirroring ``GRAPH_HEALTH:``/``DIAGNOSE_RESULT:``."""
    if not prov.checkout_present:
        return "CONFIG_PROVENANCE: checkout=absent skip=true"
    return (
        "CONFIG_PROVENANCE: checkout=present "
        f"symlinked={'true' if prov.in_checkout else 'false'} "
        f"dirty={'true' if prov.dirty else 'false'} "
        f"behind={prov.behind} ahead={prov.ahead}"
    )


# ── Dead capability_rules prefixes (#2953) ──────────────────────────────────
#
# `smoke_tests.capability_rules[].files` prefixes are matched with plain
# `str.startswith` against the paths a PR diff touches (`coord/smoke.py`,
# `match_rules`, line ~204) — not a glob, and not repo-scoped: nothing in
# `coordinator.yml` says which repo(s) a rule is "for" (see
# `coord/repo_onboard.py`'s `contents.capability_rules_present` note), so a
# rule that stops matching in one repo produces no error anywhere — routing
# just silently falls through to "any repo-capable machine" instead of a
# capability-matched one. #1072 shipped a `**`-suffixed prefix that matched
# nothing, anywhere, for almost a month; #2953 is a prefix that matches one
# repo (vimcode's `src/gtk/`) but not another whose GTK backend lives one
# directory level deeper (quadraui's `quadraui/src/gtk/`) — same silent-inert
# shape, different cause, and a bare "matches somewhere" check would have
# called it healthy.


@dataclass
class CapabilityRuleFinding:
    """Live-repo health of one ``smoke_tests.capability_rules[].files``
    prefix, checked against every repo's *locally available* git-tracked
    files (#2953).

    Reported per ``(rule index, prefix)`` pair rather than per rule, since a
    rule can list several prefixes with entirely different health.

    Each repo lands in exactly one of four buckets:

    * ``matched_repos`` — at least one tracked file starts with the prefix.
    * ``suspect_repos`` — nothing starts with the prefix, but the identical
      directory shape (``/<prefix>``) occurs somewhere deeper in the repo's
      tracked-file paths — e.g. ``src/gtk/`` matching nothing at the root
      while ``quadraui/src/gtk/foo.rs`` is tracked. This is #2953's own
      shape: the rule's *intent* clearly reaches this repo, but the exact
      prefix does not.
    * ``clean_miss_repos`` — neither of the above: no signal either way.
      Most rules are legitimately single-repo (a path that only exists in
      one repo's own layout), so a plain miss elsewhere is expected, not a
      finding.
    * ``skipped_repos`` — no local checkout available to check at all
      (#1779's precedent: absence is not a problem, never reported as dead).
    """

    rule_index: int
    prefix: str
    requires: tuple[str, ...] = ()
    matched_repos: tuple[str, ...] = ()
    suspect_repos: tuple[str, ...] = ()
    clean_miss_repos: tuple[str, ...] = ()
    skipped_repos: tuple[str, ...] = ()

    @property
    def checked_repos(self) -> tuple[str, ...]:
        """Every repo a local checkout let us actually test the prefix
        against — the denominator for ``dead``."""
        return self.matched_repos + self.suspect_repos + self.clean_miss_repos

    @property
    def dead(self) -> bool:
        """Matches nothing in ANY repo checked — #1072's `**`-suffix shape.

        False (not "dead", just "unknown") when no repo had a checkout to
        check at all — the module's existing no-checkout-is-not-a-problem
        precedent, applied per prefix instead of per config file.
        """
        return bool(self.checked_repos) and not self.matched_repos

    @property
    def partial(self) -> bool:
        """Matches in some repos, but a deeper occurrence elsewhere suggests
        it should reach further — #2953's `src/gtk/` shape. A bare "matches
        somewhere" test reports this prefix healthy; this catches it."""
        return bool(self.matched_repos) and bool(self.suspect_repos)

    @property
    def healthy(self) -> bool:
        return bool(self.matched_repos) and not self.suspect_repos


def local_repo_checkouts(config: "Config") -> dict[str, Path]:
    """Every repo in *config* with a git checkout that actually exists on
    THIS machine (first path wins when more than one machine names the same
    repo) — mirrors ``coord diagnose --graph``'s own checkout-discovery loop
    (``coord/commands/status.py:_diagnose_graph_health``).

    A repo with no local checkout is simply absent from the returned dict;
    callers treat that as a skip, never as a finding (#1779's precedent).
    """
    found: dict[str, Path] = {}
    for machine in config.machines:
        for repo_cfg in config.repos:
            if repo_cfg.name in found:
                continue
            raw = machine.repo_path(repo_cfg.name)
            if not raw:
                continue
            path = Path(raw).expanduser()
            if (path / ".git").exists():
                found[repo_cfg.name] = path
    return found


def _tracked_files(checkout: Path) -> list[str] | None:
    """Git-tracked file paths (relative, forward-slash) in *checkout*, or
    ``None`` on any git failure — treated as "can't check" (skipped), never
    as "dead"."""
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=str(checkout),
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


def _classify_prefix(prefix: str, tracked: list[str]) -> str:
    """``"matched"`` / ``"suspect"`` / ``"miss"`` for *prefix* against one
    repo's *tracked* file list — see :class:`CapabilityRuleFinding` for what
    each means."""
    if any(path.startswith(prefix) for path in tracked):
        return "matched"
    # #2953: does the identical directory shape appear deeper in the tree —
    # e.g. `src/gtk/` inert at the root but `quadraui/src/gtk/x.rs` tracked?
    # Compared with a leading slash on both sides so a prefix without one
    # (`src/gtk`) doesn't spuriously match a path that merely CONTAINS the
    # letters `src/gtk` mid-component (e.g. `mysrc/gtk_thing`).
    needle = prefix if prefix.startswith("/") else f"/{prefix}"
    if any(needle in f"/{path}" for path in tracked):
        return "suspect"
    return "miss"


def capability_rule_health(
    config: "Config", *, repo_checkouts: dict[str, Path] | None = None
) -> list[CapabilityRuleFinding]:
    """Dead/partial ``smoke_tests.capability_rules[].files`` prefixes (#2953).

    For each configured prefix, every repo in *config* is checked (using
    *repo_checkouts*, or :func:`local_repo_checkouts` when not given) and
    bucketed per :class:`CapabilityRuleFinding`. Read-only and best-effort:
    a repo with no local checkout, or a checkout `git ls-files` fails
    against, is skipped rather than counted toward "dead".

    Returns one finding per ``(rule, prefix)`` pair, in file order. Empty
    when no ``capability_rules`` are configured at all.
    """
    rules = list(getattr(config.smoke_tests, "capability_rules", None) or [])
    if not rules:
        return []

    checkouts = (
        repo_checkouts if repo_checkouts is not None else local_repo_checkouts(config)
    )
    all_repo_names = sorted({r.name for r in config.repos})

    tracked_cache: dict[str, list[str] | None] = {}

    def _tracked(repo_name: str) -> list[str] | None:
        if repo_name not in tracked_cache:
            tracked_cache[repo_name] = _tracked_files(checkouts[repo_name])
        return tracked_cache[repo_name]

    findings: list[CapabilityRuleFinding] = []
    for idx, rule in enumerate(rules):
        for prefix in rule.files:
            matched: list[str] = []
            suspect: list[str] = []
            miss: list[str] = []
            skipped: list[str] = []
            for repo_name in all_repo_names:
                if repo_name not in checkouts:
                    skipped.append(repo_name)
                    continue
                tracked = _tracked(repo_name)
                if tracked is None:
                    skipped.append(repo_name)
                    continue
                status = _classify_prefix(prefix, tracked)
                if status == "matched":
                    matched.append(repo_name)
                elif status == "suspect":
                    suspect.append(repo_name)
                else:
                    miss.append(repo_name)
            findings.append(
                CapabilityRuleFinding(
                    rule_index=idx,
                    prefix=prefix,
                    requires=tuple(rule.requires),
                    matched_repos=tuple(matched),
                    suspect_repos=tuple(suspect),
                    clean_miss_repos=tuple(miss),
                    skipped_repos=tuple(skipped),
                )
            )
    return findings


def unclaimed_capability_requirements(config: "Config") -> list[str]:
    """``requires:`` capabilities that no machine in *config* declares at
    all (#2953) — the same "config looks right in review but routes
    nothing" shape as a dead ``files`` prefix, on the other half of the
    rule.

    Distinct from claude-coordinator#2952 (a capability machines *declare*
    but nothing *probes*): this only asks whether the fleet has any machine
    claiming the capability in the first place — cheap and free of overlap
    with #2952's probe-coverage question.

    Returns capability names in first-seen order across
    ``capability_rules``, deduplicated.
    """
    declared = {cap for m in config.machines for cap in m.capabilities}
    seen: dict[str, None] = {}
    for rule in getattr(config.smoke_tests, "capability_rules", None) or []:
        for cap in rule.requires:
            if cap not in declared:
                seen.setdefault(cap, None)
    return list(seen.keys())


def format_capability_rule_lines(
    findings: list[CapabilityRuleFinding], unclaimed: list[str]
) -> list[str]:
    """Human-readable report lines for *findings*/*unclaimed* (used by
    ``coord diagnose --capability-rules``)."""
    lines: list[str] = []

    if not findings and not unclaimed:
        lines.append(
            "· no smoke_tests.capability_rules configured — nothing to check"
        )
        return lines

    for f in findings:
        req = ", ".join(f.requires) if f.requires else "(none)"
        if f.dead:
            lines.append(
                f"✗ DEAD: files=[{f.prefix!r}] requires=[{req}] — matches no "
                f"tracked file in ANY repo checked ({', '.join(f.checked_repos)}). "
                "This rule contributes nothing at dispatch time — routing "
                "silently falls through to any repo-capable machine (#2953)."
            )
        elif f.partial:
            lines.append(
                f"⚠ PARTIAL: files=[{f.prefix!r}] requires=[{req}] — matches "
                f"in: {', '.join(f.matched_repos)}. Looks dead but plausibly "
                f"should also match in: {', '.join(f.suspect_repos)} — the "
                "identical directory shape exists deeper in that repo's tree "
                "(#2953)."
            )
        elif f.matched_repos:
            lines.append(
                f"✓ files=[{f.prefix!r}] requires=[{req}] — matches in: "
                f"{', '.join(f.matched_repos)}"
            )
        else:
            # Nothing checked at all (every repo skipped) — neither healthy
            # nor dead, just unverifiable from this machine.
            lines.append(
                f"? files=[{f.prefix!r}] requires=[{req}] — no local checkout "
                "available for any repo; could not check"
            )
        if f.clean_miss_repos:
            lines.append(f"    (no match, no signal in: {', '.join(f.clean_miss_repos)})")
        if f.skipped_repos:
            lines.append(f"    (no local checkout — skipped: {', '.join(f.skipped_repos)})")

    for cap in unclaimed:
        lines.append(
            f"✗ UNCLAIMED CAPABILITY: {cap!r} is required by a "
            "capability_rules entry but no machine in coordinator.yml "
            "declares it — that rule can never route anywhere (#2953)."
        )

    return lines


def capability_rule_summary_line(
    findings: list[CapabilityRuleFinding], unclaimed: list[str]
) -> str:
    """The machine-readable trailer ``coord diagnose --capability-rules``
    prints, mirroring ``GRAPH_HEALTH:``/``CONFIG_PROVENANCE:``."""
    dead = sum(1 for f in findings if f.dead)
    partial = sum(1 for f in findings if f.partial)
    return (
        "CAPABILITY_RULES: "
        f"prefixes={len(findings)} dead={dead} partial={partial} "
        f"unclaimed_caps={len(unclaimed)}"
    )


# ── Test-command feature-flag coverage vs build_command (#2967) ─────────────
#
# quadraui is configured `test_command: cargo test --features tui` against
# `build_command: cargo build --features tui --features gtk --features
# terminal` — the Test gate never compiles `src/gtk/**` or the terminal
# backend, so a `passed` verdict on a change confined to either carries no
# information at all. Nothing previously compared the two commands; this is
# the same "config looks right in review but silently does less than it
# claims" shape as #2953's dead capability_rules prefixes, one seam over:
# there it was a `files` prefix matching nothing, here it's a `--features`
# flag the build enables that the effective Test-stage command never turns
# on. Pure string comparison against the already-loaded config — no local
# checkout required, so (unlike `capability_rule_health`) this runs the same
# everywhere, including a thin client with no repo checkouts at all.


def _cargo_features(command: str | None) -> set[str]:
    """Every value named by a `--features`/`-F`/`--features=...` token in
    *command*, comma- or space-separated, deduped.

    Returns the empty set for a falsy, unparseable (unbalanced quoting), or
    feature-less command — treated as "no feature-flag signal to compare",
    never as a gap by itself. That is what keeps a plain `cargo build` /
    `cargo test` pair (vimcode, coord-tui) and non-cargo commands (npm/pnpm
    repos) out of the findings entirely: both sides parse to an empty set,
    which is equal, not missing.
    """
    if not command:
        return set()
    try:
        tokens = shlex.split(command)
    except ValueError:
        return set()
    features: set[str] = set()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        value: str | None = None
        if tok in ("--features", "-F"):
            if i + 1 < len(tokens):
                value = tokens[i + 1]
                i += 1
        elif tok.startswith("--features="):
            value = tok[len("--features=") :]
        if value is not None:
            features.update(part for part in value.replace(",", " ").split() if part)
        i += 1
    return features


@dataclass
class FeatureCoverageFinding:
    """Feature-flag coverage of one repo's effective Test-stage command
    against its ``build_command`` (#2967).

    ``effective_test_command``/``test_command_source``/``ci_equivalent``
    mirror ``coord.smoke.resolve_smoke_command`` — the SAME precedence
    (``ci_command`` > ``smoke_tests.default_command`` > ``test_command``)
    the Test stage itself uses, so this reports on the command that will
    actually gate the repo, not always the raw ``repos[].test_command``.
    """

    repo: str
    build_command: str
    effective_test_command: str | None
    test_command_source: str
    ci_equivalent: bool
    build_features: tuple[str, ...] = ()
    test_features: tuple[str, ...] = ()
    missing_features: tuple[str, ...] = ()

    @property
    def gap(self) -> bool:
        """``build_command`` enables a feature the effective test command
        never turns on — a passed verdict didn't compile that code."""
        return bool(self.missing_features)

    @property
    def healthy(self) -> bool:
        return not self.gap


def feature_coverage_findings(config: "Config") -> list[FeatureCoverageFinding]:
    """Cargo `--features` coverage of every configured repo's effective
    Test-stage command against its ``build_command`` (#2967).

    Only repos whose ``build_command`` names at least one `--features` value
    are considered — a repo with no explicit features (or a non-cargo build,
    e.g. npm/pnpm) parses to an empty set on both sides and is silently
    omitted, matching :func:`capability_rule_health`'s "no signal, no
    finding" precedent. The effective test command is resolved with
    ``coord.smoke.resolve_smoke_command`` (deferred import: `coord.smoke`
    pulls in ``httpx``/``github_ops``, which this otherwise-light,
    network-free module avoids at import time), so a repo that already
    covers the gap via ``ci_command`` or ``smoke_tests.default_command``
    correctly reports healthy rather than flagging the raw
    ``test_command`` alone.
    """
    from coord.smoke import resolve_smoke_command  # noqa: PLC0415

    findings: list[FeatureCoverageFinding] = []
    for repo_cfg in config.repos:
        build_command = repo_cfg.build_command
        if not build_command:
            continue
        build_features = _cargo_features(build_command)
        if not build_features:
            continue
        smoke_cmd = resolve_smoke_command(repo_cfg, config.smoke_tests)
        test_features = _cargo_features(smoke_cmd.command)
        missing = tuple(sorted(build_features - test_features))
        findings.append(
            FeatureCoverageFinding(
                repo=repo_cfg.name,
                build_command=build_command,
                effective_test_command=smoke_cmd.command,
                test_command_source=smoke_cmd.source,
                ci_equivalent=smoke_cmd.ci_equivalent,
                build_features=tuple(sorted(build_features)),
                test_features=tuple(sorted(test_features)),
                missing_features=missing,
            )
        )
    return findings


def format_feature_coverage_lines(findings: list[FeatureCoverageFinding]) -> list[str]:
    """Human-readable report lines for *findings* (used by ``coord diagnose
    --test-coverage``)."""
    if not findings:
        return [
            "· no repo's build_command names an explicit `--features` value "
            "— nothing to check (this only applies to cargo feature-flagged "
            "builds)"
        ]

    lines: list[str] = []
    for f in findings:
        if f.gap:
            lines.append(
                f"✗ GAP: {f.repo} — build_command (`{f.build_command}`) enables "
                f"[{', '.join(f.build_features)}], but the effective Test-stage "
                f"command ({f.test_command_source}: "
                f"`{f.effective_test_command or '(unconfigured)'}`) enables "
                f"[{', '.join(f.test_features) or '(none)'}]. Missing: "
                f"{', '.join(f.missing_features)} — a `passed` verdict never "
                "compiled that code (#2967)."
            )
        else:
            lines.append(
                f"✓ {f.repo} — {f.test_command_source} covers every feature "
                f"build_command enables ([{', '.join(f.build_features)}])"
            )
    return lines


def feature_coverage_summary_line(findings: list[FeatureCoverageFinding]) -> str:
    """The machine-readable trailer ``coord diagnose --test-coverage``
    prints, mirroring ``GRAPH_HEALTH:``/``CAPABILITY_RULES:``."""
    gap = sum(1 for f in findings if f.gap)
    return f"TEST_COMMAND_COVERAGE: repos_checked={len(findings)} gap={gap}"
