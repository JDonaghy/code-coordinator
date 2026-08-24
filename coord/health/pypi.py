"""Poll PyPI's **simple index** for a project's released versions (#1628).

Why the simple index and not the JSON API (``/pypi/<name>/json``): they flip
independently, in both directions.  The JSON API's ``info.version`` can lead
the index (a release is visible there before the files are servable) *and*
lag it (cached separately).  Only the simple index is what ``pip`` actually
resolves against, so "am I behind?" answered from the JSON API can disagree
with what an operator sees when they run ``pip install -U`` — which turns
the check into a source of confusion instead of a signal.

The index is HTML (PEP 503) whose anchors are distribution filenames::

    <a href="...">code_coordinator-0.4.91-py3-none-any.whl</a>
    <a href="...">code_coordinator-0.4.91.tar.gz</a>

Parsing is filename-based and defensive: anything unrecognised is skipped
rather than raising.  Yanked releases carry ``data-yanked`` on the anchor and
are excluded — pip won't resolve to them, so counting one as "you're behind"
would be a lie.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from coord.dist_name import CANDIDATE_NAMES

# PEP 503 normalisation: runs of -_. collapse to a single "-", lowercased.
_NORMALIZE_RE = re.compile(r"[-_.]+")

# One anchor.  Non-greedy attribute soup, then the filename as anchor text.
_ANCHOR_RE = re.compile(r"<a\s+([^>]*)>\s*([^<]+?)\s*</a>", re.IGNORECASE | re.DOTALL)

_SDIST_SUFFIXES = (".tar.gz", ".zip", ".tar.bz2")


def split_distribution_filename(filename: str) -> tuple[str, str] | None:
    """``(project_name, version)`` from a distribution filename, or ``None``.

    Wheels (PEP 427) are ``name-version(-build)?-pytag-abi-platform.whl`` with
    every ``-`` inside the name escaped to ``_``, so splitting on ``-`` is
    exact — no regex needed and no way to mis-split a hyphenated project.
    Sdists are ``name-version.tar.gz`` where the name may legitimately keep
    its hyphens, so those split from the right.
    """
    lower = filename.lower()
    if lower.endswith(".whl"):
        parts = filename[: -len(".whl")].split("-")
        # 5 components without a build tag, 6 with one.
        if len(parts) < 5 or not parts[0] or not parts[1]:
            return None
        return parts[0], parts[1]
    for suffix in _SDIST_SUFFIXES:
        if lower.endswith(suffix):
            stem = filename[: -len(suffix)]
            name, sep, version = stem.rpartition("-")
            if not sep or not name or not version:
                return None
            return name, version
    return None


# Numeric-ish release segments plus an optional pre/post/dev suffix.  This is
# a deliberate PEP 440 *subset*: enough to order 0.4.9 < 0.4.10 < 0.5.0 and to
# recognise 1.0.0rc1 as a pre-release, without taking a `packaging` dependency
# the package doesn't currently declare.
_VERSION_RE = re.compile(
    r"^(?P<release>\d+(?:\.\d+)*)"
    r"(?:(?P<pre_kind>a|b|c|rc|alpha|beta|pre|preview)(?P<pre_num>\d*))?"
    r"(?:\.?(?:post|rev|r)(?P<post>\d*))?"
    r"(?:\.?dev(?P<dev>\d*))?$",
    re.IGNORECASE,
)


def normalize_name(name: str) -> str:
    """PEP 503 normalised project name (``code_coordinator`` → ``code-coordinator``)."""
    return _NORMALIZE_RE.sub("-", name).lower()


@dataclass(frozen=True, order=True)
class Version:
    """A comparable subset of PEP 440.  ``is_prerelease`` drives filtering.

    Only ``sort_key`` participates in comparison: ``1.0`` and ``1.0.0`` are
    the same release however they were spelled on the index, and the counting
    of "how many releases am I behind" must not double-count a project that
    changed its spelling.
    """

    sort_key: tuple
    raw: str = field(default="", compare=False)
    is_prerelease: bool = field(default=False, compare=False)

    def __str__(self) -> str:  # pragma: no cover — trivial
        return self.raw


def parse_version(raw: str) -> Version | None:
    """Parse *raw* into a comparable :class:`Version`, or ``None`` if it isn't
    a shape we can order confidently.

    Refusing to guess matters: mis-ordering versions would make the check
    report "2 behind" on a machine that is current, and an alert that cries
    wolf gets muted.
    """
    m = _VERSION_RE.match(raw.strip())
    if not m:
        return None
    release = tuple(int(part) for part in m.group("release").split("."))
    # Pad so 1.0 and 1.0.0 compare equal-ish (1.0 sorts just below 1.0.0 only
    # if we don't pad; padding is what a human means by "same release").
    release = release + (0,) * (4 - len(release)) if len(release) < 4 else release

    pre_kind = m.group("pre_kind")
    is_pre = pre_kind is not None or m.group("dev") is not None
    # Ordering within a release: dev < pre < final < post.
    if m.group("dev") is not None:
        stage, stage_num = 0, int(m.group("dev") or 0)
    elif pre_kind is not None:
        stage, stage_num = 1, int(m.group("pre_num") or 0)
    elif m.group("post") is not None:
        stage, stage_num = 3, int(m.group("post") or 0)
    else:
        stage, stage_num = 2, 0
    return Version(sort_key=(release, stage, stage_num), raw=raw.strip(), is_prerelease=is_pre)


def parse_simple_index(html: str, project: str) -> list[Version]:
    """Every non-yanked, parseable release version in a PEP 503 index page.

    Sorted ascending.  Pre-releases are included here and filtered by the
    caller — ``pip install -U`` won't pick one by default, so the "how far
    behind am I" check excludes them, but a caller that wants them can.
    """
    wanted = normalize_name(project)
    out: list[Version] = []
    seen: set[str] = set()
    for attrs, filename in _ANCHOR_RE.findall(html or ""):
        if "data-yanked" in attrs.lower():
            continue
        parts = split_distribution_filename(filename)
        if parts is None:
            continue
        name, raw_version = parts
        if normalize_name(name) != wanted:
            continue
        version = parse_version(raw_version)
        if version is None or version.raw in seen:
            continue
        seen.add(version.raw)
        out.append(version)
    return sorted(out)


def fetch_simple_index(
    project: str,
    *,
    index_url: str = "https://pypi.org/simple",
    timeout: float = 3.0,
) -> str:
    """GET the simple-index page for *project*.  Raises on failure — the
    caller (a probe) is the thing that has to fail soft."""
    import httpx  # noqa: PLC0415 — keep import cost off the non-network path

    url = f"{index_url.rstrip('/')}/{normalize_name(project)}/"
    response = httpx.get(
        url,
        timeout=timeout,
        follow_redirects=True,
        headers={"Accept": "text/html"},
    )
    response.raise_for_status()
    return response.text


def latest_release(
    project: str,
    *,
    index_url: str = "https://pypi.org/simple",
    timeout: float = 3.0,
) -> tuple[Version | None, list[Version]]:
    """``(latest_final_release, all_final_releases_ascending)``."""
    html = fetch_simple_index(project, index_url=index_url, timeout=timeout)
    finals = [v for v in parse_simple_index(html, project) if not v.is_prerelease]
    return (finals[-1] if finals else None), finals


def latest_release_any(
    names: tuple[str, ...] = CANDIDATE_NAMES,
    *,
    index_url: str = "https://pypi.org/simple",
    timeout: float = 3.0,
) -> tuple[str, Version | None, list[Version]]:
    """:func:`latest_release`, tried against each of *names* in turn
    (#2103/#2106).

    A rename (like #2096's `claude-coordinator` -> `code-coordinator`)
    publishes under a new, separate PyPI project — a caller resolving
    "what's the latest fleet-wide release" must not hardcode whichever name
    happened to be current when the code was written, or it silently stops
    seeing new releases the moment a future rename ships.

    Returns ``(project_name_used, latest_final_release,
    all_final_releases_ascending)`` for the first name in *names* that has
    any final release on the index — ``code-coordinator`` wins once it has
    releases, mirroring :func:`coord.dist_name.resolve_installed`'s
    preference. A name with no releases yet (including a 404 for "this
    project doesn't exist on the index at all", which is the *expected*
    state for ``code-coordinator`` until #2096 ships) is treated as "try
    the next name", not as an error.

    Raises only when every name in *names* fails to even resolve (network
    down, index unreachable, ...) — re-raising the last error, since at
    that point there is no meaningful "no releases" to fall back to and the
    caller needs to know the lookup itself failed.
    """
    last_exc: Exception | None = None
    for project in names:
        try:
            latest, finals = latest_release(project, index_url=index_url, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — try the next candidate name
            last_exc = exc
            continue
        if finals:
            return project, latest, finals
    if last_exc is not None:
        raise last_exc
    # Every name resolved (no network/index error) but none has a single
    # final release — an honest "nothing here", not a failure.
    return names[-1], None, []


def releases_behind(installed: Version, finals: list[Version]) -> int:
    """How many entries in *finals* are strictly newer than *installed*.

    The ONE place "how far behind is this install" gets computed — shared by
    ``coord.health.checks.agent_install``'s ``agent_version`` check and
    ``coord release propagate``/``coord release nightly-window``'s #2583
    min-releases-behind auto-roll gate, so there is exactly one answer to
    "how many releases behind" rather than two that can quietly disagree
    (``coord.release_cordon.version_drift`` is a deliberately DIFFERENT,
    network-free patch-arithmetic *estimate* used elsewhere for cheap,
    every-tick decisions — not a second implementation of this count; see
    its own docstring).

    Takes already-parsed :class:`Version` objects, not raw strings: both
    existing call sites parse ``installed`` themselves already (to grade an
    unparseable version as UNKNOWN rather than 0), so accepting a
    :class:`Version` here keeps that judgement call with the caller instead
    of duplicating it.
    """
    return sum(1 for v in finals if v > installed)
