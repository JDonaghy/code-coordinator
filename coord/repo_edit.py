"""Comment-preserving text surgery on ``coordinator.yml`` (#2220).

``coord repo add`` has to write into the fleet's live config, and that file is
**mostly comments** — several hundred lines of them, each one the record of an
incident that produced the setting below it. A ``yaml.safe_load`` →
``yaml.safe_dump`` round trip would silently delete every one of them, which is
a worse outcome than not having the command at all. PyYAML is the only YAML
library in this project's base dependencies (see ``pyproject.toml``'s note on
keeping the client install small), and it cannot round-trip comments.

So these are line-level edits: find the block, insert into it, leave everything
else byte-identical. That is only safe because the caller
(``coord.commands.repo``) re-parses the result with :func:`coord.config.load`
and refuses to write when the parse fails or the repo did not actually land —
these functions are the edit, that check is the seatbelt.

Every function here is **pure** (``str`` in, ``str`` out) and therefore
testable without touching the operator's real config.
"""

from __future__ import annotations

import re

# A top-level key line: no leading whitespace, ends in a colon.
_TOP_LEVEL_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*:")


class RepoEditError(RuntimeError):
    """The config did not have the shape this edit needs. Raised instead of
    guessing — a wrong guess writes a plausible-looking config that loads fine
    and dispatches nothing."""


def _find_block(lines: list[str], key: str) -> tuple[int, int]:
    """``(start, end)`` line indices of the top-level ``key:`` block.

    ``start`` is the index of the ``key:`` line itself; ``end`` is the index
    one past the block's last *content* line — trailing blank lines and
    column-0 comments are excluded, because those belong to whatever section
    comes next (``# Concurrency settings`` sits above ``concurrency:``, not
    below the last repo).
    """
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == f"{key}:" or line.startswith(f"{key}:"):
            if _TOP_LEVEL_KEY.match(line) and line.split(":", 1)[0] == key:
                start = i
                break
    if start is None:
        raise RepoEditError(f"no top-level `{key}:` block found in coordinator.yml")

    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line[:1].isspace() and not line.lstrip().startswith("#"):
            end = i
            break
        if line[:1] == "#":  # a column-0 comment introduces the next section
            end = i
            break
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


def render_repo_entry(
    name: str,
    github: str,
    default_branch: str,
    *,
    depends_on: list[str] | None = None,
    build_command: str | None = None,
    test_command: str | None = None,
    uat_live_preview: bool = False,
) -> str:
    """The ``repos:`` list entry for a new repo, as a YAML fragment.

    Deliberately minimal: only the fields ``coord repo add`` can determine
    *correctly* without asking. Everything else (``ci_command``,
    ``worker_permissions``, ``housekeeping``, ``coordinator_only_files``,
    ``artifact_paths``, ``reference_repos``, ``provider``) is left to the
    operator and named in the command's printed residue — a wrong guess at
    ``coordinator_only_files`` is a security-relevant defect, and a guessed
    ``test_command`` produces a Test stage that runs the wrong suite and calls
    it green.

    ``uat_live_preview`` (#3092) is the one exception that is NOT a guess: it
    is written only when ``--with-preview`` provisioned the per-PR preview
    lane in the same command, so the flag and the workflow that makes it true
    land together. Setting it without that lane is exactly the silent stall
    ``coord repo doctor``'s #3073 check reports.
    """
    lines = [
        f"  - name: {name}",
        f"    github: {github}",
        f"    depends_on: [{', '.join(depends_on or [])}]",
        f"    default_branch: {default_branch}",
    ]
    if build_command:
        lines.append(f'    build_command: "{build_command}"')
    if test_command:
        lines.append(f'    test_command: "{test_command}"')
    if uat_live_preview:
        lines.append("    uat_live_preview: true")
    return "\n".join(lines) + "\n"


def insert_repo_entry(text: str, entry: str) -> str:
    """Append *entry* to the ``repos:`` list in *text*, preserving comments."""
    lines = text.splitlines(keepends=True)
    _, end = _find_block(lines, "repos")
    block = entry if entry.endswith("\n") else entry + "\n"
    # Keep the one-blank-line separation the existing entries use.
    prefix = "" if (end > 0 and not lines[end - 1].strip()) else "\n"
    lines[end:end] = [prefix + block]
    return "".join(lines)


# ── #2748 (IL-2): `acceptance.drivers.<repo>` ─────────────────────────────────


def render_acceptance_driver_entry(
    repo_name: str,
    kind: str,
    run: str,
    *,
    setup: str = "",
    mock: str = "",
    capability: str = "",
    entrypoint: str = "",
) -> str:
    """The ``acceptance.drivers.<repo_name>:`` YAML fragment for a freshly
    created repo (#2748, IL-2) — mirrors :func:`render_repo_entry`'s "only
    what the caller can determine correctly" restraint, but every field a
    driver needs to actually RUN is knowable from the stack alone (see
    ``coord.commands.repo._ACCEPTANCE_DRIVER_TEMPLATES``), unlike
    ``ci_command``/``coordinator_only_files`` on the repo entry itself.

    4-space/6-space indentation matches the ``acceptance: / drivers: /
    <repo>: / <field>:`` nesting documented in docs/ORACLE_LOOP.md and used
    by every hand-authored fleet config today.
    """
    lines = [f"    {repo_name}:", f"      kind: {kind}"]
    if setup:
        lines.append(f'      setup: "{setup}"')
    lines.append(f'      run: "{run}"')
    if mock:
        lines.append(f'      mock: "{mock}"')
    if capability:
        lines.append(f"      capability: {capability}")
    if entrypoint:
        lines.append(f"      entrypoint: {entrypoint}")
    return "\n".join(lines) + "\n"


def insert_acceptance_driver_entry(text: str, entry: str) -> str:
    """Insert *entry* (one :func:`render_acceptance_driver_entry` block)
    under ``acceptance: / drivers:`` in *text*, preserving comments — same
    contract as :func:`insert_repo_entry`.

    Unlike ``repos:``/``machines:``, ``acceptance:`` is an ADVANCED,
    optional block: most of this project's own history predates it, and a
    fleet that has never touched the oracle loop has no ``acceptance:`` key
    at all (see ``tests/test_repo_add.py``'s fixture, which has neither).
    So this creates whatever is missing rather than requiring it exist
    first — a fresh ``acceptance:\\n  drivers:\\n`` block when there is no
    top-level ``acceptance:`` key yet, or just the ``drivers:`` child when
    ``acceptance:`` exists but is childless (hand-added for some other
    reason, or a future field lands there before ``drivers:`` does).
    """
    lines = text.splitlines(keepends=True)
    block = entry if entry.endswith("\n") else entry + "\n"

    try:
        start, end = _find_block(lines, "acceptance")
    except RepoEditError:
        # No `acceptance:` top-level block at all — append a fresh one.
        prefix = "" if (lines and not lines[-1].strip()) else "\n"
        lines.append(prefix + "acceptance:\n  drivers:\n" + block)
        return "".join(lines)

    drivers_line = None
    for i in range(start + 1, end):
        if re.match(r"^\s{2}drivers:\s*(\{\})?\s*$", lines[i]):
            drivers_line = i
            # An inline empty flow mapping (`drivers: {}`) — rewrite it to
            # block form so the entry can be inserted as a child on the
            # next line, rather than appending a SECOND `drivers:` key
            # below (a duplicate key that would silently override this one
            # in most YAML loaders and discard the entry being added).
            if lines[i].strip() != "drivers:":
                lines[i] = "  drivers:\n"
            break
    if drivers_line is None:
        # `acceptance:` exists but has no `drivers:` child yet.
        lines[start + 1:start + 1] = ["  drivers:\n"]
        drivers_line = start + 1
        end += 1

    # End of the `drivers:` mapping — the first line back at <=2-space
    # indent (a sibling of `drivers:` itself), or the end of the
    # `acceptance:` block.
    insert_at = drivers_line + 1
    while insert_at < end:
        line = lines[insert_at]
        if line.strip() and re.match(r"^\s{0,2}\S", line):
            break
        insert_at += 1
    lines[insert_at:insert_at] = [block]
    return "".join(lines)


# ── #2861: `portal.project_repos` ────────────────────────────────────────────


def render_portal_project_repo_entry(project_id: str, repos: list[str]) -> str:
    """The ``portal.project_repos`` list entry mapping *project_id* to *repos*.

    ``project_id`` is quoted because the portal's identifiers are opaque
    (``proj_67deaa6d1291`` today, but nothing promises the next one is not
    all-digits, ``yes``, or ``on`` — each of which YAML 1.1 would silently
    parse as a non-string and then fail ``_parse_portal_project_repos``'
    "must be a non-empty string" check for reasons an operator would have to
    reverse-engineer). Repo names are already validated against ``repos[]``
    at load, so they need no quoting.
    """
    return (
        f'    - project_id: "{project_id}"\n'
        f"      repos: [{', '.join(repos)}]\n"
    )


def insert_portal_project_repo_entry(text: str, entry: str) -> str:
    """Insert *entry* under ``portal: / project_repos:`` in *text*, preserving
    comments — same contract and same "create whatever is missing" posture as
    :func:`insert_acceptance_driver_entry`.

    ``portal:`` is optional and absent on any fleet that has never talked to
    coord-portal, so this creates the block when there is none. A created
    block has no ``enabled:`` key, which parses as ``enabled: false`` — i.e.
    identical to having no block at all, so writing a mapping can never
    accidentally switch the portal client ON.
    """
    lines = text.splitlines(keepends=True)
    block = entry if entry.endswith("\n") else entry + "\n"

    try:
        start, end = _find_block(lines, "portal")
    except RepoEditError:
        prefix = "" if (lines and not lines[-1].strip()) else "\n"
        lines.append(prefix + "portal:\n  project_repos:\n" + block)
        return "".join(lines)

    list_line = None
    for i in range(start + 1, end):
        if re.match(r"^\s{2}project_repos:\s*(\[\])?\s*$", lines[i]):
            list_line = i
            # An inline empty flow list (`project_repos: []`) — rewrite to
            # block form, or the entry below it would be a sibling key rather
            # than a list item (and a second `project_repos:` further down
            # would silently override the first).
            if lines[i].strip() != "project_repos:":
                lines[i] = "  project_repos:\n"
            break
    if list_line is None:
        lines[start + 1:start + 1] = ["  project_repos:\n"]
        list_line = start + 1
        end += 1

    # End of the `project_repos:` list — the first line back at <=2-space
    # indent (a sibling of `project_repos:` itself), or the end of `portal:`.
    insert_at = list_line + 1
    while insert_at < end:
        line = lines[insert_at]
        if line.strip() and re.match(r"^\s{0,2}\S", line):
            break
        insert_at += 1
    lines[insert_at:insert_at] = [block]
    return "".join(lines)


# ── #2915: a whole `machines:` entry ─────────────────────────────────────────


def render_machine_entry(
    name: str,
    host: str,
    *,
    capabilities: list[str] | None = None,
    repo_paths: dict[str, str] | None = None,
    max_workers: int | None = None,
) -> str:
    """The ``machines:`` list entry for a new machine, as a YAML fragment.

    Same restraint as :func:`render_repo_entry` — only the fields ``coord
    machine add`` can determine *correctly*. ``quiet_hours`` is deliberately
    omitted (a guessed timezone silently suppresses dispatch for hours).

    ``repos:`` is rendered from ``repo_paths``' KEYS rather than taking a
    separate list, so the two can never disagree. That disagreement is
    exactly incident item 4 of #2915: a ``repo_paths`` key naming the
    checkout's *directory* (``claude-coordinator``) instead of the fleet's
    *repo name* (``code-coordinator``) makes ``_parse_machines`` raise, and
    the ENTIRE ``coordinator.yml`` then fails to load — for every machine,
    not just the new one. The caller validates those keys against the
    config's own ``repos:`` names before calling this.
    """
    caps = capabilities or []
    paths = repo_paths or {}
    lines = [
        f"  - name: {name}",
        f"    host: {host}",
        f"    capabilities: [{', '.join(caps)}]",
        f"    repos: [{', '.join(paths)}]",
    ]
    if max_workers is not None:
        lines.append(f"    max_workers: {max_workers}")
    if paths:
        lines.append("    repo_paths:")
        lines.extend(f"      {repo}: {path}" for repo, path in paths.items())
    return "\n".join(lines) + "\n"


def insert_machine_entry(text: str, entry: str) -> str:
    """Append *entry* to the ``machines:`` list in *text*, preserving comments.

    Mirrors :func:`insert_repo_entry` exactly — same block finder, same
    one-blank-line separation — because the two blocks have the same shape
    and a second, subtly-different implementation is how the pair drifts.
    """
    lines = text.splitlines(keepends=True)
    _, end = _find_block(lines, "machines")
    block = entry if entry.endswith("\n") else entry + "\n"
    prefix = "" if (end > 0 and not lines[end - 1].strip()) else "\n"
    lines[end:end] = [prefix + block]
    return "".join(lines)


def _machine_entry_range(lines: list[str], machine: str) -> tuple[int, int]:
    """``(start, end)`` line indices of one machine's entry inside ``machines:``."""
    m_start, m_end = _find_block(lines, "machines")
    entry_starts: list[int] = []
    for i in range(m_start + 1, m_end):
        if re.match(r"^\s{0,4}- ", lines[i]):
            entry_starts.append(i)
    for idx, s in enumerate(entry_starts):
        e = entry_starts[idx + 1] if idx + 1 < len(entry_starts) else m_end
        for j in range(s, e):
            if re.match(rf"^\s*-?\s*name:\s*{re.escape(machine)}\s*$", lines[j]):
                return s, e
    raise RepoEditError(f"no machine named {machine!r} in coordinator.yml `machines:`")


def add_repo_to_machine(
    text: str, machine: str, repo_name: str, repo_path: str
) -> str:
    """Add *repo_name* to *machine*'s ``repos:`` list and ``repo_paths:`` map.

    Handles both spellings the fleet's own config uses: an inline
    ``repos: [a, b]`` flow list and a block list. Idempotent — a repo already
    listed is left alone rather than duplicated (a duplicate is not a parse
    error, so nothing downstream would ever have told the operator).
    """
    lines = text.splitlines(keepends=True)
    start, end = _machine_entry_range(lines, machine)

    # ── `repos:` list ────────────────────────────────────────────────────
    repos_line = None
    for i in range(start, end):
        if re.match(r"^\s*repos:\s*(\[.*\])?\s*$", lines[i]):
            repos_line = i
            break
    if repos_line is None:
        raise RepoEditError(
            f"machine {machine!r} has no `repos:` key — refusing to guess where "
            "to put one"
        )

    flow = re.match(r"^(\s*repos:\s*)\[(.*)\]\s*$", lines[repos_line])
    if flow:
        head, inner = flow.group(1), flow.group(2).strip()
        existing = [p.strip() for p in inner.split(",") if p.strip()]
        if repo_name not in existing:
            existing.append(repo_name)
            lines[repos_line] = f"{head}[{', '.join(existing)}]\n"
    else:
        # Block list: `repos:` followed by `    - name` items.
        item_end = repos_line + 1
        existing = []
        while item_end < end and re.match(r"^\s+- ", lines[item_end]):
            existing.append(lines[item_end].strip()[2:].strip())
            item_end += 1
        if repo_name not in existing:
            indent = re.match(r"^(\s*)", lines[repos_line]).group(1) + "  "
            lines[item_end:item_end] = [f"{indent}- {repo_name}\n"]
            end += 1

    # ── `repo_paths:` map ────────────────────────────────────────────────
    lines = _add_repo_path(lines, start, end, machine, repo_name, repo_path)
    return "".join(lines)


def _add_repo_path(
    lines: list[str], start: int, end: int, machine: str, repo_name: str, repo_path: str
) -> list[str]:
    paths_line = None
    for i in range(start, end):
        if re.match(r"^\s*repo_paths:\s*$", lines[i]):
            paths_line = i
            break

    if paths_line is None:
        # No `repo_paths:` at all — append one at the end of the machine's
        # entry.  A machine with `repos:` but no `repo_paths:` is exactly the
        # #1801 "declared but has no path" dispatch blocker, so adding the key
        # is a fix, not an assumption.
        indent = "    "
        for i in range(start, end):
            m = re.match(r"^(\s*)(?:- )?name:", lines[i])
            if m:
                indent = m.group(1) + ("  " if lines[i].lstrip().startswith("- ") else "")
                break
        insert_at = end
        while insert_at > start and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines[insert_at:insert_at] = [
            f"{indent}repo_paths:\n",
            f"{indent}  {repo_name}: {repo_path}\n",
        ]
        return lines

    child_indent = re.match(r"^(\s*)", lines[paths_line]).group(1) + "  "
    i = paths_line + 1
    while i < end and lines[i].startswith(child_indent) and lines[i].strip():
        if re.match(rf"^\s*{re.escape(repo_name)}:\s*", lines[i]):
            return lines  # already mapped — leave the operator's value alone
        i += 1
    lines[i:i] = [f"{child_indent}{repo_name}: {repo_path}\n"]
    return lines
