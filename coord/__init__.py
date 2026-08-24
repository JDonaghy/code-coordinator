"""code-coordinator: multi-agent coordinator for Claude Code workers."""

from __future__ import annotations

from importlib.metadata import Distribution
from typing import TYPE_CHECKING

from coord.dist_name import CANDIDATE_NAMES, DistributionNotFoundError, resolve_installed

if TYPE_CHECKING:
    from pathlib import Path

# NOTE on the `# noqa: PLC0415` (import-outside-toplevel) markers below:
# `json`/`pathlib.Path`/`urllib.parse`/`subprocess`/`setuptools_scm` are all
# deferred into the function bodies that use them rather than imported at
# module scope, so the common (non-editable-install) path through
# `_resolve_version` — every `coord` invocation except a developer/operator
# checkout — pays zero import cost for machinery it never touches. Each
# import is module-level-safe on its own; this is purely to keep the hot
# path cheap, not a cycle-avoidance workaround. The `TYPE_CHECKING`-only
# import above gives `_editable_source_root`'s return annotation a real
# name for type checkers without paying that cost at runtime either (`from
# __future__ import annotations` already defers evaluating the annotation
# itself).


def _editable_source_root(dist_name: str) -> Path | None:
    """Return the source checkout root when *dist_name* is installed
    editable (PEP 660, ``pip install -e .``), else ``None``.

    #2010: an editable install's ``.dist-info`` is written once at install
    time and never refreshed — reading ``__version__`` straight off it goes
    stale the moment ``git pull`` moves the checkout's HEAD past whatever
    tag was current at install time, misreporting the *operator's own* CLI
    as drifted rather than the fleet it's inspecting.

    pip records editable installs via a ``direct_url.json`` with
    ``dir_info.editable: true`` and a ``file://`` URL pointing at the live
    source tree — that's the reliable signal, not a ``site-packages``
    substring match on ``__file__`` (a non-editable install into a venv
    satisfies that too, and its metadata IS trustworthy since it's a
    frozen snapshot rather than a claim about live source).

    #2728: the URL's path component must go through
    ``urllib.request.url2pathname`` (platform-dispatched), not a bare
    ``urllib.parse.unquote``. pip writes this URL via
    ``pathname2url``/``path_to_url``, which on Windows produces
    ``file:///C:/Users/...`` — ``urlparse`` yields a path of
    ``/C:/Users/...``, and handing that straight to ``pathlib.Path`` does
    NOT recover ``C:\\Users\\...``: Windows path parsing only recognises a
    drive letter at the very start of the string, so a leading slash before
    ``C:`` makes the whole thing parse as a driveless, rooted path (a
    literal top-level folder named ``C:``) that can never exist. ``root
    .is_dir()`` then reads False on every Windows editable install, no
    exception raised, and ``_resolve_version`` silently falls through to
    the frozen ``.dist-info`` snapshot — the exact pre-#2010 symptom,
    reintroduced Windows-only. ``url2pathname`` is ``unquote`` verbatim on
    POSIX (no behaviour change there) and is ``nturl2path.url2pathname`` on
    Windows, which strips that leading slash before the drive letter.
    """
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415
    from urllib.parse import urlparse  # noqa: PLC0415
    from urllib.request import url2pathname  # noqa: PLC0415

    try:
        raw = Distribution.from_name(dist_name).read_text("direct_url.json")
        info = json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001 — best-effort, never break __version__
        return None
    if not isinstance(info, dict) or not info.get("dir_info", {}).get("editable"):
        return None
    parsed = urlparse(info.get("url", ""))
    if parsed.scheme != "file" or not parsed.path:
        return None
    root = Path(url2pathname(parsed.path))
    return root if root.is_dir() else None


def _live_scm_version(root: Path) -> str | None:
    """Best-effort live version for an editable checkout at *root*, so
    ``__version__`` doesn't trust a ``.dist-info`` stamp frozen at ``pip
    install -e .`` time (#2010). Two tiers, most-accurate first:

    1. ``setuptools_scm.get_version()`` — the exact scheme a wheel build
       would stamp from the same commit. Only importable when the
       environment kept the build-system requirement around (e.g. ``pip
       install -e ".[dev]"``); a plain ``pip install -e .`` discards it
       once PEP 517 build isolation tears down its throwaway venv.
    2. ``git describe --tags --dirty --always`` — always available
       wherever git is, and identical to the wheel's version string on a
       clean tagged commit (the common case); slightly less precise
       (raw git-describe form, not PEP 440) when HEAD has moved past the
       last tag, but still honest and never a stale number.

       KNOWN TRADE-OFF: this tier's output (e.g. ``3.4.5-dirty``, hyphen
       separated) is not the same string ``setuptools_scm`` or a wheel
       build would produce for that same commit (PEP 440,
       ``3.4.5.dev0+g<sha>``-style). If ``coord status``'s drift check
       ever does plain string equality between an operator's and an
       agent's version, an operator that falls all the way to this tier
       can show spurious drift against an otherwise-matching agent — a
       strict improvement over pre-#2010 (never a *stale* number) but not
       a guarantee of an *exact-format* match. Every real agent/daemon
       install is a wheel, never editable (see the INVARIANT in
       CLAUDE.md's release section), so this only affects
       operator-vs-operator comparisons, and only when tier 1
       (``setuptools_scm``) is unavailable.

    Returns ``None`` — never raises — when neither works; callers fall
    back to the (possibly stale) package metadata.
    """
    try:
        from setuptools_scm import get_version  # noqa: PLC0415

        return get_version(root=str(root), fallback_version="0.0.0.dev0")
    except Exception:  # noqa: BLE001 — setuptools-scm may not be importable
        pass

    import subprocess  # noqa: PLC0415

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "describe", "--tags", "--dirty", "--always"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        described = result.stdout.strip()
        if not described:
            return None
        return described[1:] if described.startswith("v") else described
    except Exception:  # noqa: BLE001 — best-effort, never break __version__
        return None


def _resolve_version(names: tuple[str, ...] = CANDIDATE_NAMES) -> str:
    """#1238: single-sourced from the installed package's metadata, which
    setuptools-scm stamps from the git tag at build time (see
    ``[tool.setuptools_scm]`` in pyproject.toml) — this is the ONLY place
    ``__version__`` is computed. No other source file may hardcode a
    version literal; a release is just ``git tag vX.Y.Z && git push origin
    vX.Y.Z``.

    #2103/#2106: resolves through :func:`coord.dist_name.resolve_installed`
    rather than a hardcoded distribution name literal — ``__version__`` is
    what ``/health``'s ``"version"`` field reports, which is the *only*
    field ``coord agent update``'s polling loop keys off of to decide a
    machine "came back". A hardcoded literal here would need editing at the
    next rename and would degrade to ``"0+unknown"`` on every machine in
    between, faking a fleet-wide "did not come back".

    #2010: for a wheel install that metadata is always correct — it's a
    frozen snapshot of a build that just happened. For an *editable*
    install it is a snapshot written once at ``pip install -e .`` time and
    never refreshed; ``git pull`` can move the checkout well past it with
    nothing updating ``.dist-info``, so the operator's own CLI ends up
    reporting itself as ancient. When the install is editable, prefer a
    live git-derived version over the frozen metadata so ``coord
    --version`` (and the ``coord status`` drift check that compares agent
    versions against it) reflects the code actually running, rather than
    accusing every agent of drift from a number that was wrong about the
    operator, not them.

    COST: this runs once at module-import time — i.e. for every ``coord``
    invocation, not just ``--version``/``status``. On a non-editable
    install (every agent/daemon; see the wheel-only INVARIANT in
    CLAUDE.md's release section) it's just the one ``importlib.metadata``
    lookup already required by #1238. On an editable install without
    ``setuptools_scm`` importable, it additionally spawns one ``git
    describe`` subprocess (bounded by a 5s timeout — see
    ``_live_scm_version``) per invocation. That's an operator/developer
    checkout only, and negligible next to a `claude -p` subprocess launch
    or a board read; revisit only if something starts shelling out to
    ``coord`` in a tight loop from such a checkout.
    """
    try:
        resolved = resolve_installed(names)
    except DistributionNotFoundError:
        # Neither candidate name is an installed package at all (e.g.
        # `python -c "import coord"` run directly against a source
        # checkout that was never `pip install`'d) — degrade to an
        # obviously-not-a-release string rather than raising.
        return "0+unknown"

    root = _editable_source_root(resolved.name)
    if root is not None:
        scm_version = _live_scm_version(root)
        if scm_version is not None:
            return scm_version
    return resolved.version


__version__ = _resolve_version()
