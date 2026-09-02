"""Systemd unit-file drift against `deploy/` (#1831).

`deploy/*.service`/`*.timer` is version-controlled, reviewed, and merged
like code. **Nothing ever installs it.** The release path is bump -> PR ->
merge -> tag push -> `publish.yml` -> PyPI, then `coord agent update` for the
venvs — no step copies `deploy/*.service` into `~/.config/systemd/user/`.
Unit files are hand-installed once at machine setup and drift forever after.

The 2026-08-04 incident this closes: dellserver's `coord-serve.service` was
three weeks stale, its `Environment=PATH=` still starting with an **editable**
checkout of this repo (`~/src/claude-coordinator/.venv/bin`). `coord_argv()`
(`coord/drive.py`) resolves subprocesses via `shutil.which("coord")` — i.e.
from that PATH — so the daemon itself ran the pinned release while
everything it spawned ran whatever stale branch that checkout happened to be
on. Two failure modes, one probe:

`unit_drift`
    Per deploy-lane unit: does the installed copy under
    `~/.config/systemd/user/` match `deploy/<name>`? Absence is the common
    case (most machines don't run every lane) and is reported OK, not a
    fault — same convention as `cli_venv`/`tui_binary`
    (:mod:`coord.health.checks.deploy_lane_facts`).

`_path_shadow_risk`
    Independent of content drift: does the installed unit's
    `Environment=PATH=` put an editable checkout's `.venv/bin` ahead of the
    release entry points (`~/.local/bin`, `~/.coord-venv/bin`)? This is what
    made the drift above *harmful* rather than merely untidy, and it can
    exist even on a unit whose content otherwise matches `deploy/` bit for
    bit if `deploy/` itself regresses — which is exactly what happened to
    `coord-serve.service`'s v0.4.105 cut (it dropped the #1117 PATH entry
    that had also fixed a real bug). CRIT regardless of the content-diff
    verdict — a shadowed release is the split-brain, not a cosmetic
    difference.

#3049 — a masked unit is not a stale one
-----------------------------------------
`systemctl --user mask` leaves an installed unit's file symlinked to
`/dev/null`, which reads back empty and therefore ALWAYS content-diffs
against `deploy/<name>` — a unit masked on purpose (a fleet that chose
manual release rolls masks the propagate/window lanes deliberately) reads
identically to one nobody has looked at in months. This probe still reports
the honest WARN — deciding whether that drift is *wanted* is policy, and
this probe has no policy context — but it also checks the same intent
sentinel the watchdog already honours (`~/.coord/watchdog-suppress.json`,
#2580) and publishes the verdict in `values["suppressed"]` (plus
`"suppress_reason"`/`"suppress_set"`) so a policy-aware consumer, notably
`coord release verify` (:mod:`coord.release_verify`), can render "masked by
policy" instead of a remedy that would re-arm the very thing the masking
exists to prevent.

#1927 — where the reference comes from
--------------------------------------
The original cut of this check diffed the installed unit against
``<checkout>/deploy/<name>``: a file in the host's own git working copy that
nothing verifies is at the released tag, or current at all. Installed units
and checkouts go stale for the *same* reason (nobody pulled), so they go
stale *together* — and when they do the comparison reports clean. The check
was least reliable in exactly the case it exists to catch, and the remedy it
printed (``cp <checkout>/deploy/... ~/.config/systemd/user/...``) sourced
from the same unverified working copy, cementing the stale unit.

So the reference is now the *packaged* unit set — ``coord/deploy/`` inside
the installed distribution (see :func:`packaged_unit_dir`). That is the
released artifact for the version this process is running, and it cannot
drift with the host. When the reference is NOT a released artifact (an
editable/source checkout, a configured directory, or an old wheel that ships
no units) the verdict is annotated and a *match* grades UNKNOWN rather than
OK: an un-annotated green from an unverified reference is worse than no
check at all.

#1928 — ``coord-agent.service`` is a template, not a plain file
-----------------------------------------------------------------
Unlike its siblings (byte-for-byte ``cp``-installed from ``deploy/``),
``deploy/coord-agent.service`` carries ``<MACHINE_NAME>``/``<PORT>``
placeholders that every real install fills in — via the documented manual
``sed`` (leaving systemd's ``%h`` specifier alone) or via
``install-agent.sh``'s inline heredoc (which expands ``%h`` to a literal
``$HOME`` and drops the ~76-line doc-comment header entirely). A byte-diff
against the raw template can never match either of those, so it warned on
every host, permanently — and printed a bare ``cp`` remedy that, followed
verbatim, installs the placeholders as literal text and takes the unit
down.

``_content_matches`` fixes this without weakening the check: it normalizes
away the two kinds of noise that are never a real difference — comment/blank
lines, and the ``%h`` vs. literal-``$HOME`` spelling — and then, only if the
(normalized) reference still contains placeholder tokens, accepts an
installed copy that fills them in *consistently* (the same placeholder
resolves to the same value everywhere it appears). A unit that is missing a
real property — e.g. elitebook's installed copy drops ``--machine``/
``--port`` from ``ExecStart`` entirely rather than filling them in — still
fails the match and still warns; that is a real defect, not template noise.
The remedy for a still-drifting *templated* unit is never the bare ``cp``:
see :func:`_templated_remedy`.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check, is_suppressed, load_suppressions
from coord.health.units import expand, human_hours

_UNIT_GLOBS = ("*.service", "*.timer")
_SYSTEMD_USER_DIR = "~/.config/systemd/user"

# Entry points that resolve to the pinned release. A `.venv/bin` entry ahead
# of ALL of these on a unit's PATH can shadow it (#1831's dellserver case).
_RELEASE_MARKERS = ("/.local/bin", "/.coord-venv/bin")

_PATH_LINE_RE = re.compile(r"^Environment\s*=\s*PATH=(.*)$", re.MULTILINE)

# #2683 (W3): this is the delimiter of the PATH *value written inside a
# systemd unit file*, not the delimiter of the current process's own PATH
# env var. systemd only runs on Linux, so an `Environment=PATH=...` line is
# unconditionally `:`-joined no matter what platform is running this health
# check (e.g. a dev box auditing a fleet member's unit file, or this
# module's own cross-platform test suite) -- `os.pathsep` would be wrong
# here on Windows, where it resolves to `;` and would silently fail to
# split content that is still colon-joined. Deliberately a named constant,
# not `os.pathsep`, and not the bare literal the #2683 audit flagged.
_SYSTEMD_PATH_SEP = ":"

# #1928: a template placeholder, e.g. `<MACHINE_NAME>` or `<PORT>` — see
# `deploy/coord-agent.service`. Uppercase-with-underscores by convention so
# it can never collide with a real systemd directive or value.
_PLACEHOLDER_RE = re.compile(r"<([A-Z][A-Z0-9_]*)>")

# Placeholders this module knows a real, safe substitution for — used to
# build a runnable remedy in `_templated_remedy` instead of a bare `cp`
# (#1928). Anything not listed here still gets a safe remedy, just not one
# that reaches for a value this module has no business guessing.
_KNOWN_PLACEHOLDER_VALUES = {
    "MACHINE_NAME": "$(hostname -s)",
    "PORT": "7433",
}

# Sentinel used to fold systemd's `%h` specifier and this host's literal
# $HOME into one token before comparing unit text (#1928) — install-agent.sh
# expands $HOME to a literal path in the unit it writes; deploy/*.service
# and a manual sed-install both keep `%h` literal. Both are correct, so
# neither spelling may count as drift. Chosen to be inert under re.escape()
# (see `_placeholder_pattern`) and never appear in a real unit file.
_HOME_TOKEN = "\x00HOME\x00"


def packaged_unit_dir():
    """`coord/deploy/` inside *this* installed distribution, or None.

    Shipped as package data (see `pyproject.toml`), so on a pip-installed
    host it is the unit set as of the installed version — the released
    artifact, which cannot drift with the host's git checkout (#1927).
    Returns None on a wheel old enough to predate #1927, which is why the
    working-copy fallbacks below still exist.
    """
    candidate = Path(__file__).resolve().parent.parent.parent / "deploy"
    if candidate.is_dir() and _unit_files(candidate):
        return candidate
    return None


def in_git_worktree(path: Path) -> bool:
    """Is `path` inside a git working copy?

    The discriminator between "released artifact" and "working copy" for
    :func:`packaged_unit_dir`: an editable/source install puts the package
    under a checkout, where `coord/deploy/` is as unverified as any other
    tracked file. A pip-installed wheel lands in `site-packages`, which has
    no `.git` above it.
    """
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover - resolve() is effectively total here
        return False
    for parent in (resolved, *resolved.parents):
        try:
            if (parent / ".git").exists():
                return True
        except OSError:  # pragma: no cover - unreadable ancestor
            continue
    return False


def installed_version() -> str | None:
    """The version of the installed coordinator distribution, or None.

    #2103/#2106: resolves via :func:`coord.dist_name.resolve_installed`
    instead of a hardcoded name, so this (feeding the unit-drift health
    check) doesn't need to know the distribution name to stay correct
    across a future rename.
    """
    try:
        from coord.dist_name import resolve_installed

        return resolve_installed().version
    except Exception:  # pragma: no cover - metadata missing in odd installs
        return None


@dataclass(frozen=True)
class UnitReference:
    """The thing installed units are diffed against, and how much it's worth.

    `verified` is the whole point of #1927: only a reference that is the
    released artifact for the installed version can turn a match into a
    green. Everything else is a working copy whose own currency is unknown,
    so its match is reported as UNKNOWN with `label` naming what was
    actually compared.
    """

    path: Path
    source: str  # "package" | "configured" | "checkout"
    verified: bool
    version: str | None = None

    @property
    def label(self) -> str:
        if self.source == "package":
            ver = f" {self.version}" if self.version else ""
            if self.verified:
                return f"the packaged units of installed coord{ver}"
            return f"coord{ver}'s packaged units (SOURCE CHECKOUT, unverified)"
        if self.source == "configured":
            return f"configured reference {self.path} (unverified working copy)"
        return f"{self.path} (unverified working copy)"

    @property
    def short_label(self) -> str:
        """The `headroom` half of :attr:`label` — one line, no path."""
        if self.source == "package" and self.verified:
            return f"packaged coord{' ' + self.version if self.version else ''}"
        return f"{self.path}"


def resolve_reference(ctx: HealthContext) -> UnitReference | None:
    """Where to read the reference units from, and whether it's trustworthy.

    Order (#1927): the packaged units of the running distribution first —
    they are the released artifact and are the only reference that cannot go
    stale with the host. `health.deploy_dir` and then the first local
    checkout's `deploy/` remain as fallbacks for wheels that predate #1927
    (and for operators who deliberately point the check elsewhere), but both
    are working copies and are flagged as such.
    """
    packaged = packaged_unit_dir()
    if packaged is not None:
        return UnitReference(
            path=packaged,
            source="package",
            verified=not in_git_worktree(packaged),
            version=installed_version(),
        )
    fallback = resolve_deploy_dir(ctx)
    if fallback is None:
        return None
    configured = getattr(ctx.thresholds, "deploy_dir", None)
    return UnitReference(
        path=fallback,
        source="configured" if configured else "checkout",
        verified=False,
    )


def resolve_deploy_dir(ctx: HealthContext):
    """The checked-in `deploy/` this machine can diff installed units against.

    The #1927 *fallback* reference, used only when the installed
    distribution ships no `coord/deploy/` of its own — see
    :func:`resolve_reference`, which is what the probe calls.

    Configured `health.deploy_dir` wins outright; otherwise the first local
    checkout (see `coord.health.context.local_checkouts`) that has one —
    normally the `claude-coordinator` entry in `repo_paths`.
    """
    configured = getattr(ctx.thresholds, "deploy_dir", None)
    if configured:
        return expand(configured, ctx.home)
    for checkout in ctx.checkouts:
        candidate = checkout.path / "deploy"
        if candidate.is_dir():
            return candidate
    return None


def resolve_systemd_user_dir(ctx: HealthContext):
    """Where installed systemd *user* units actually live on this machine."""
    configured = getattr(ctx.thresholds, "systemd_user_dir", None)
    if configured:
        return expand(configured, ctx.home)
    return expand(_SYSTEMD_USER_DIR, ctx.home)


def _unit_files(deploy_dir):
    seen = set()
    out = []
    for pattern in _UNIT_GLOBS:
        for path in sorted(deploy_dir.glob(pattern)):
            if path.name in seen:
                continue
            seen.add(path.name)
            out.append(path)
    return sorted(out, key=lambda p: p.name)


def _strip_noise_lines(text: str) -> str:
    """Drop comment and blank lines.

    Doc prose that never affects what systemd actually runs — and the
    biggest source of #1928's false drift: `deploy/coord-agent.service`
    carries ~76 lines of install documentation that `install-agent.sh`'s
    generated unit never includes, so a raw byte-diff always "differed" on
    that alone, template placeholders aside.
    """
    return "\n".join(
        line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")
    )


def _normalize_home(text: str, home: Path) -> str:
    """Fold systemd's `%h` specifier and this host's literal $HOME into one
    token (#1928), so a unit that spells its home directory either way
    compares equal. `install-agent.sh`'s heredoc expands `$HOME` to a
    literal path when it writes the unit; `deploy/coord-agent.service` and a
    manual sed-install both leave `%h` for systemd to resolve at run time.
    Both are correct — neither spelling is drift.
    """
    text = text.replace("%h", _HOME_TOKEN)
    home_str = str(home)
    if home_str and home_str != "/":
        text = text.replace(home_str, _HOME_TOKEN)
    return text


def _normalize_unit_text(text: str, home: Path) -> str:
    """Strip the noise `_content_matches` never treats as drift (#1928)."""
    return _strip_noise_lines(_normalize_home(text, home))


def _placeholder_pattern(normalized_deploy_text: str) -> re.Pattern[str]:
    """Compile *normalized_deploy_text* into a regex matching any rendering
    of its `<PLACEHOLDER>` tokens — each becomes a capture group on first
    use and a backreference on repeat, so the same placeholder must resolve
    to the same value everywhere it appears (#1928: `<PORT>` shows up in
    both `Description=` and `ExecStart=` in `deploy/coord-agent.service`).

    `re.escape()` leaves `<`, `>`, letters, digits and `_` untouched (see
    module-level note on `_HOME_TOKEN`), so the placeholder tokens and the
    home sentinel both survive escaping intact and are still findable.
    """
    seen: set[str] = set()

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in seen:
            return f"(?P={name})"
        seen.add(name)
        return f"(?P<{name}>.+?)"

    escaped = re.escape(normalized_deploy_text)
    return re.compile(_PLACEHOLDER_RE.sub(repl, escaped), re.DOTALL)


def _content_matches(deploy_text: str, installed_text: str, home: Path) -> bool:
    """Does *installed_text* satisfy *deploy_text* (#1928)?

    Exact, modulo the noise `_normalize_unit_text` strips, when
    *deploy_text* is a plain file (every lane but `coord-agent.service`
    today). When *deploy_text* is a template — it still contains
    `<PLACEHOLDER>` tokens after normalizing — an installed copy that fills
    them in consistently also counts as a match: the placeholder is the
    only thing allowed to differ, so a unit that's missing a real property
    (e.g. `ExecStart` dropping `--machine`/`--port` entirely rather than
    filling them in) still fails to match and still reports drift.
    """
    deploy_norm = _normalize_unit_text(deploy_text, home)
    installed_norm = _normalize_unit_text(installed_text, home)
    if deploy_norm == installed_norm:
        return True
    if not _PLACEHOLDER_RE.search(deploy_norm):
        return False
    return _placeholder_pattern(deploy_norm).fullmatch(installed_norm) is not None


def _is_templated(deploy_text: str) -> bool:
    """Does the *raw* reference text contain unfilled placeholders (#1928)?

    Checked against the raw text, not the normalized one, purely so this
    reads naturally at call sites that haven't normalized anything yet —
    normalization never introduces or removes a `<PLACEHOLDER>` token.
    """
    return _PLACEHOLDER_RE.search(deploy_text) is not None


def _templated_remedy(deploy_text: str, deploy_path: Path, installed_path: Path, service: str) -> str:
    """The remedy for a still-drifting *templated* unit (#1928).

    Never the bare `cp` the non-template branch prints: run verbatim, that
    installs `<MACHINE_NAME>`/`<PORT>` as literal text and the unit refuses
    to start — which is this issue's entire complaint. When every
    placeholder in the template has a known, safe substitution (today:
    `MACHINE_NAME`, `PORT`) the remedy is a real, runnable `sed` — copy-paste
    safe, per #1928's acceptance criteria. If some future placeholder has no
    known substitution, fall back to a pointer at the template's own
    documented install procedure rather than guess a value.
    """
    names = sorted(set(_PLACEHOLDER_RE.findall(deploy_text)))
    if names and all(n in _KNOWN_PLACEHOLDER_VALUES for n in names):
        sed_args = " ".join(f'-e "s/<{n}>/{_KNOWN_PLACEHOLDER_VALUES[n]}/"' for n in names)
        return (
            f"{deploy_path} is a TEMPLATE — do not cp it verbatim (#1928). Render "
            f"it for this host first: sed {sed_args} {deploy_path} > {installed_path} "
            f"&& systemctl --user daemon-reload && systemctl --user restart {service}"
        )
    return (
        f"{deploy_path} is a TEMPLATE ({', '.join(names)} placeholder(s)) — copying "
        "it verbatim installs those as literal text and the unit will not start. "
        f"See the install instructions at the top of {deploy_path} (sed substitution "
        "or install-agent.sh) to render it for this host before installing."
    )


def _diff_summary(installed_text: str, deploy_text: str) -> tuple[int, int | None]:
    """(changed line count, first differing line number in the installed
    file) between two unit files — a cheap stand-in for a full diff in a
    one-line `headroom` string."""
    diff = list(
        difflib.unified_diff(
            installed_text.splitlines(), deploy_text.splitlines(), lineterm=""
        )
    )
    changed = sum(
        1 for line in diff if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )
    first_line = None
    for line in diff:
        m = re.match(r"@@ -(\d+)", line)
        if m:
            first_line = int(m.group(1))
            break
    return changed, first_line


def find_path_shadow(installed_text: str) -> str | None:
    """The PATH entry that shadows the release, or None if the installed
    unit's PATH is safe (no editable checkout ahead of a release marker).

    A "release marker" is `~/.local/bin` or `~/.coord-venv/bin` — either
    resolves `coord` to the pinned install (`~/.local/bin/coord` is a
    symlink onto `~/.coord-venv/bin/coord`). An entry whose LAST path
    component is `.venv/bin` (a project-local dev venv, as opposed to the
    dot-prefixed-but-distinct `.coord-venv`/`.coord-cli-venv`) ahead of that
    marker is exactly the #1831 split-brain: `shutil.which("coord")`
    (`coord_argv()`, `coord/drive.py`) resolves it first.

    Only the LAST `Environment=PATH=` directive is read — systemd unit files
    may repeat `Environment=`, and later directives for the same key are
    what actually take effect.
    """
    matches = _PATH_LINE_RE.findall(installed_text)
    if not matches:
        return None
    entries = [e for e in matches[-1].split(_SYSTEMD_PATH_SEP) if e]

    release_idx = None
    for idx, entry in enumerate(entries):
        stripped = entry.rstrip("/")
        if any(stripped.endswith(marker) for marker in _RELEASE_MARKERS):
            release_idx = idx
            break

    for idx, entry in enumerate(entries):
        if release_idx is not None and idx >= release_idx:
            break
        if entry.rstrip("/").endswith("/.venv/bin"):
            return entry
    return None


@check(
    id="unit_drift",
    scope="machine",
    title="unit drift",
    order=44,
    description=(
        "Installed systemd user units (~/.config/systemd/user/) match the "
        "units packaged with the installed release, and no unit's PATH lets "
        "an editable checkout shadow that release (#1831, #1927)."
    ),
)
def probe_unit_drift(ctx: HealthContext) -> list[CheckResult]:
    reference = resolve_reference(ctx)
    if reference is None:
        return [
            CheckResult(
                check_id="unit_drift",
                scope="machine",
                severity=Severity.OK,
                headroom="no deploy/ checkout found on this machine",
                values={"deploy_dir": None, "reference_source": None},
            )
        ]

    deploy_dir = reference.path
    installed_dir = resolve_systemd_user_dir(ctx)
    # #3049: a unit deliberately masked on purpose (e.g. the release-roll
    # lanes on a fleet that chose manual releases) reads identically to a
    # genuinely-stale one here — masking a unit leaves its installed copy
    # empty, which diffs against `deploy/` exactly like neglect does. This
    # probe still reports the honest fact (WARN, "stale", the `cp`/`restart`
    # remedy) because severity is this probe's call alone and the drift is
    # real — but it also surfaces whether the SAME sentinel the watchdog
    # already honours (`~/.coord/watchdog-suppress.json`, #2580) covers this
    # unit, via `values["suppressed"]`. A downstream consumer that knows the
    # drift is intentional (`coord release verify`, #3049) can act on that
    # without this probe silently downgrading a fact it has no policy
    # context to judge.
    suppressions = load_suppressions(ctx.coord_dir)
    results: list[CheckResult] = []
    for deploy_path in _unit_files(deploy_dir):
        name = deploy_path.name
        installed_path = installed_dir / name
        values: dict = {
            "deploy_path": str(deploy_path),
            "installed_path": str(installed_path),
            # #1927: a green is only interpretable alongside what produced
            # it, so every result carries its reference — and whether that
            # reference is the released artifact or a working copy.
            "reference_dir": str(reference.path),
            "reference_source": reference.source,
            "reference_verified": reference.verified,
            "reference_version": reference.version,
        }

        if not installed_path.exists():
            results.append(
                CheckResult(
                    check_id="unit_drift",
                    scope="machine",
                    subject=name,
                    severity=Severity.OK,
                    headroom="not installed on this machine",
                    values={**values, "installed": False},
                )
            )
            continue

        try:
            deploy_text = deploy_path.read_text()
            installed_text = installed_path.read_text()
            installed_mtime = installed_path.stat().st_mtime
        except OSError as exc:
            results.append(
                CheckResult(
                    check_id="unit_drift",
                    scope="machine",
                    subject=name,
                    severity=Severity.UNKNOWN,
                    headroom=f"could not read unit: {exc}",
                    error=str(exc),
                    values={**values, "installed": True},
                )
            )
            continue

        values["installed"] = True
        values["installed_mtime"] = installed_mtime
        # #1928: not a bare `==` — `deploy_text` may be a template (today,
        # only coord-agent.service), and even for a plain file, comment/blank
        # lines and %h-vs-literal-$HOME spelling are never real drift.
        templated = _is_templated(deploy_text)
        matches = _content_matches(deploy_text, installed_text, ctx.home)
        values["matches"] = matches
        values["templated"] = templated
        shadow_entry = find_path_shadow(installed_text)
        values["shadow_entry"] = shadow_entry

        if shadow_entry:
            age = ctx.now - installed_mtime
            detail = (
                f"editable checkout '{shadow_entry}' precedes the release "
                "entry point on this unit's PATH — shutil.which(\"coord\") in "
                "subprocesses this unit spawns resolves the checkout instead "
                "of the pinned release (#1831). Reorder PATH= so ~/.local/bin "
                "or ~/.coord-venv/bin comes first."
            )
            results.append(
                CheckResult(
                    check_id="unit_drift",
                    scope="machine",
                    subject=name,
                    severity=Severity.CRIT,
                    headroom=f"PATH shadow risk ({human_hours(age)} since install)",
                    detail=detail,
                    threshold="crit when a .venv/bin entry precedes ~/.local/bin or ~/.coord-venv/bin",
                    values=values,
                )
            )
            continue

        if not matches:
            changed, first_line = _diff_summary(installed_text, deploy_text)
            age = ctx.now - installed_mtime
            values["diff_lines"] = changed
            values["first_diff_line"] = first_line
            # #3049: bare unit name is the key `scripts/fleet_watchdog.py`'s
            # own checks already suppress under (`suppress_keys=(unit,)` in
            # `check_disabled_timers`/`check_failed_units`) — an operator who
            # has already suppressed this unit for the watchdog does not
            # maintain a second key for this probe. `unit_drift:<name>` is
            # also accepted for symmetry with `_suppress_keys_for` above.
            suppressed, entry = is_suppressed(
                suppressions, (name, f"unit_drift:{name}"), now=ctx.now
            )
            values["suppressed"] = suppressed
            values["suppress_reason"] = (entry or {}).get("reason") if suppressed else None
            values["suppress_set"] = (entry or {}).get("set") if suppressed else None
            where = f", first differing at line {first_line}" if first_line else ""
            # #1928: a templated reference (coord-agent.service) never gets
            # the bare `cp` remedy below — followed verbatim it installs
            # `<MACHINE_NAME>`/`<PORT>` as literal text and takes the unit
            # down. See `_templated_remedy`.
            if templated:
                detail = (
                    f"{_templated_remedy(deploy_text, deploy_path, installed_path, name.rsplit('.', 1)[0])}"
                    f"   # reference: {reference.label}"
                )
            else:
                # The remedy sources from the SAME file the diff read
                # (#1927) — a `cp` out of an unverified checkout is how a
                # stale unit got cemented in the first place.
                detail = (
                    f"cp {deploy_path} {installed_path} && systemctl --user "
                    f"daemon-reload && systemctl --user restart "
                    f"{name.rsplit('.', 1)[0]}   # reference: {reference.label}"
                )
            results.append(
                CheckResult(
                    check_id="unit_drift",
                    scope="machine",
                    subject=name,
                    severity=Severity.WARN,
                    headroom=(
                        f"stale — installed {human_hours(age)} ago, {changed} "
                        f"line(s) differ from {reference.short_label}"
                        f"{where}"
                    ),
                    detail=detail,
                    threshold=f"warn when installed content != {reference.short_label}",
                    values=values,
                )
            )
            continue

        if not reference.verified:
            # Content matches, but the reference is a working copy nothing
            # verified is current (#1927). Reporting OK here is the exact
            # false green this check was rebuilt to stop emitting: a stale
            # checkout and a stale installed unit agree with each other.
            results.append(
                CheckResult(
                    check_id="unit_drift",
                    scope="machine",
                    subject=name,
                    severity=Severity.UNKNOWN,
                    headroom=(
                        f"matches {reference.short_label}, but that reference "
                        "is an unverified working copy — cannot confirm this "
                        "is the released unit"
                    ),
                    detail=(
                        f"diffed against {reference.label}. Nothing checks "
                        "that copy is at the released tag, and an installed "
                        "unit drifts for the same reason a checkout does, so "
                        "the two go stale together and agree (#1927). Install "
                        "a release wheel on this host (it ships coord/deploy/) "
                        "to make this comparison meaningful."
                    ),
                    values=values,
                )
            )
            continue

        results.append(
            CheckResult(
                check_id="unit_drift",
                scope="machine",
                subject=name,
                severity=Severity.OK,
                headroom=f"matches {reference.short_label}",
                values=values,
            )
        )

    return results
