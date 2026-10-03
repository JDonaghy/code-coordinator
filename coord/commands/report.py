"""`coord report` — CLI surface over the report engine (#1742).

Sibling of ``coord audit``: same ``--json`` convention, same
``coord.state`` seam discipline (``list_reports`` / ``run_report`` route to
the daemon when ``board_service`` is set, local registry otherwise), so a
thin client never opens ``~/.coord/coord.db`` and never folds the audit
trail itself.

``coord report list`` prints the catalogue with each report's parameters,
their allowed values and their defaults.  ``coord report run <id>`` renders
one report as a plain table plus its notes block; ``--format json`` emits
the ``ReportResult`` verbatim — the same bytes the daemon's
``GET /report/{id}`` returns for the same window.

``--format csv|ndjson|md|xlsx`` (#1765, extended by #3472) write a
machine/human-readable export to stdout, so it pipes and redirects normally.
Each one dispatches through :data:`coord.reports.EXPORT_FORMATS` on the
**raw wire result** — the same table, on the same input, that
``GET /report/{id}?format=<fmt>`` dispatches through — rather than
re-serialising the human table above.  That is deliberate and load-bearing
twice over: it is why the CLI and the daemon emit identical bytes, and it is
why ``started_at`` exports as an epoch (or a real Excel datetime cell for
``xlsx``) instead of the ``13h ago`` that ``_format_cell`` renders.
``--format xlsx`` needs the optional ``reports-xlsx`` extra
(``openpyxl``); without it, this exits ``2`` with a message naming the
extra rather than a crash.  ``--json`` survives as a hidden alias for
``--format json``.

``coord report export <id> --public`` (#3474) runs a report the same way
``report run`` does (``coord.state.run_report`` — daemon when
``board_service`` is set, local registry otherwise) and then redacts it for
an audience OUTSIDE the fleet, via :func:`coord.reports.redact_report_for_public`:
every repo not named in ``coordinator.yml``'s ``reporting.public.allowlist_
repos`` is dropped and folded into one aggregate ``"private repo"`` row —
no name, no issue number, no title. The result is written as a
self-contained ``.public.html`` (data inlined, chart on a pinned ECharts
CDN) plus the same redacted ``.public.csv`` into ``--out`` (default: the
current directory). A report whose dollar figure has no stated cost basis
left standing after redaction is refused outright (``PublicExportError``) —
"a public number with no stated basis is not produced" (#3474). The
redaction itself runs **client-side**, over the already-fetched result —
same precedent as every ``--format`` above, which also serialises
client-side over ``run_report``'s own return value — so a thin client never
needs a second, DB-backed export path. The allowlist that drives it,
though, is resolved via :func:`_resolve_public_allowlist` — the same
thin-client-first ``board_service``/``fetch_remote_config`` seam
``coord.commands._common._load_config`` uses for every other config read
(#1080/#947/#2824), never a raw local ``coordinator.yml`` load: the
allowlist is the one input here that is explicitly security-sensitive, so
a thin client with a stale local copy must never under-redact against it.

Exit codes: ``2`` for a bad request (unknown report, unknown parameter, bad
value, ``--format xlsx`` without the extra installed, or a public export
refused over an unstated cost basis — the message names what was
allowed/needed), ``1`` for a read/transport failure.  Never a traceback for
either.
"""

from __future__ import annotations

import json as _json
import time
from datetime import datetime, timezone
from pathlib import Path

import click

from coord.commands._common import _CONFIG_OPTION


def _resolve_public_allowlist(config_path) -> frozenset[str]:  # noqa: ANN001
    """The thin-client-safe ``reporting.public.allowlist_repos`` for
    ``coord report export --public`` (#3474 review).

    Mirrors :func:`coord.commands._common._load_config`'s "thin client
    first" resolution — a machine with ``board_service``/``client.toml``
    configured fetches the DAEMON's live config (``fetch_remote_config``)
    rather than trusting whatever ``coordinator.yml`` happens to sit on
    local disk, the exact bug class #1080/#947/#2824 already paid for
    (a stale/stray local file silently diverging from the daemon's real
    config with no signal anything is wrong). ``_load_config`` itself
    ``sys.exit(2)``s on a bad config, which is right for every OTHER
    command but wrong here: the allowlist is the one input this feature
    treats as security-sensitive, so a failure to resolve it (daemon
    unreachable, local file malformed/absent) must fail SOFT — the caller
    catches whatever this raises and falls back to the empty allowlist,
    i.e. "redact everything" (same posture as the daemon-side sibling,
    :func:`coord.reports.load_public_allowlist`).
    """
    from coord.client import fetch_remote_config, resolve_board_service  # noqa: PLC0415
    from coord.config import load as load_config  # noqa: PLC0415

    path = config_path
    svc = resolve_board_service()
    if svc is not None:
        path = fetch_remote_config(svc)
    return frozenset(load_config(path).reporting.public.allowlist_repos)


# Row keys whose values are epoch timestamps — rendered as a relative age in
# the human table (absolute in --json, which is the machine contract).
# Fallback only: used when a row column is not covered by the report's
# `column_meta` (#1760) — a report that ships full metadata never reaches
# this.
_TS_COLUMNS = frozenset(
    {"started_at", "merged_at", "first_event_at", "last_event_at"}
)

# Per-column display cap for the human table. Long free text (titles, drive
# exit reasons) is truncated here and never in --json.
_MAX_CELL = 44

# Shorter headers for the human table only — `columns` itself is the wire
# contract (#1741 renders against it) and is never rewritten. Fallback only
# (#1760): a report's `column_meta[].label` takes precedence when present,
# so this list only matters for a report that ships none.
_HEADER_ALIASES = {
    "started_at": "started",
    "merged_at": "merged",
    "fix_iterations": "fixes",
    "test_verdicts": "tests",
    "review_verdicts": "reviews",
    "drive_exit": "drive exit",
    "first_event_at": "first",
    "last_event_at": "last",
}


def _relative_time(ts: float | None, *, now: float | None = None) -> str:
    if ts is None:
        return "-"
    delta = (time.time() if now is None else now) - float(ts)
    if delta < 0:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%m-%d %H:%M")
    for seconds, unit in ((60, "s"), (3600, "m"), (86400, "h"), (86400 * 30, "d")):
        if delta < seconds:
            prior = {60: 1, 3600: 60, 86400: 3600, 86400 * 30: 86400}[seconds]
            return f"{int(delta // prior)}{unit} ago"
    return f"{int(delta // (86400 * 30))}mo ago"


def _truncate(s: str, width: int) -> str:
    return s if len(s) <= width else s[: max(1, width - 1)] + "…"


def _format_cell(column: str, row: dict, meta: dict | None = None) -> str:
    """Render one row value for the human table.

    Dispatches on the column's declared ``kind`` from ``column_meta``
    (#1760) when the report supplies one — that is what lets the CLI stop
    hardcoding per-field knowledge (``_TS_COLUMNS`` et al) and the panel and
    the CLI format the same column the same way without drifting.  Falls
    back to dispatching on the value's *shape* (timestamp column by name,
    list, dict, None) when no metadata is present, so a report that ships
    none still gets a table.
    """
    value = row.get(column)
    kind = (meta or {}).get("kind")
    is_timestamp = kind == "timestamp" if kind else column in _TS_COLUMNS
    if column == "started_at" and value is None and row.get("started_before_window"):
        # Not "unknown" — "began before this window". The distinction is the
        # whole point of the started_before_window flag. Scoped to
        # started_at specifically: a None merged_at just means "not merged
        # yet" and must not also read as "before window".
        return "<window"
    if value is None:
        return "-"
    if is_timestamp:
        return _relative_time(value)
    if kind == "money":
        # Four decimals, matching `coord usage`'s rollup views — a leg can
        # genuinely cost $0.0032 and "$0.00" would read as free.
        try:
            return f"${float(value):.4f}"
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        if not value:
            return "-"
        if all(isinstance(v, dict) for v in value):
            # The `decisions` report's `options` column (#2369): a list of
            # `{label, command_or_action, ...}` dicts, not the scalar
            # strings every other `kind: list` column holds. Rendered
            # through the same rule the CSV export and the coord-tui
            # Reports panel use, so a dict-shaped list item never falls
            # through to a raw Python dict repr here.
            from coord.reports import format_option_cell  # noqa: PLC0415

            return " | ".join(format_option_cell(v) for v in value)
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        # drive_exit and friends: the two fields that matter, inline.
        if "exit_code" in value:
            reason = value.get("reason")
            code = value.get("exit_code")
            code_s = "crash" if code is None else str(code)
            return f"exit {code_s}" + (f": {reason}" if reason else "")
        return ",".join(f"{k}={v}" for k, v in value.items())
    return str(value)


def _pad(cell: str, width: int, align: str) -> str:
    return cell.rjust(width) if align == "right" else cell.ljust(width)


def _render_table(result: dict) -> list[str]:
    columns = list(result.get("columns") or [])
    rows = list(result.get("rows") or [])
    if not columns or not rows:
        return []
    meta_by_id = {
        m.get("id"): m for m in (result.get("column_meta") or []) if isinstance(m, dict)
    }
    headers = [
        (meta_by_id.get(c, {}).get("label") or _HEADER_ALIASES.get(c, c)).upper()
        for c in columns
    ]
    aligns = [meta_by_id.get(c, {}).get("align") or "left" for c in columns]
    cells = [
        [_truncate(_format_cell(c, r, meta_by_id.get(c)), _MAX_CELL) for c in columns]
        for r in rows
    ]
    # #1763: an optional pinned grand-total row. Rendered through the same
    # per-column formatter as any other row — the only special-casing is the
    # `Σ` marker in the first column, which the wire deliberately leaves
    # blank so each renderer picks its own.
    totals = result.get("totals")
    footer: list[str] | None = None
    if isinstance(totals, dict):
        footer = [
            _truncate(_format_cell(c, totals, meta_by_id.get(c)), _MAX_CELL)
            for c in columns
        ]
        if footer and footer[0] == "-":
            footer[0] = "Σ"
    widths = [
        max(len(header), *(len(row[i]) for row in cells + ([footer] if footer else [])))
        for i, header in enumerate(headers)
    ]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    for row in cells:
        lines.append(
            "  ".join(_pad(cell, widths[i], aligns[i]) for i, cell in enumerate(row)).rstrip()
        )
    if footer:
        lines.append("  ".join("-" * widths[i] for i in range(len(headers))).rstrip())
        lines.append(
            "  ".join(_pad(cell, widths[i], aligns[i]) for i, cell in enumerate(footer)).rstrip()
        )
    return lines


def _abs_time(ts: float | None) -> str:
    if ts is None:
        return "?"
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%SZ"
    )


@click.group("report", help="Run reports over the coordinator's own history (#1742).")
def report_group() -> None:
    """Report engine — a small registry of named, parameterised reports."""


@report_group.command("list", help="List the available reports and their parameters.")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output the raw catalogue JSON.")
@_CONFIG_OPTION
def report_list(
    output_json: bool,
    config_path: Path,  # noqa: ARG001 — accepted for --config-flag consistency; reports need no coordinator.yml
) -> None:
    from coord.state import list_reports  # noqa: PLC0415

    try:
        cat = list_reports()
    except Exception as e:  # noqa: BLE001 — a clean CLI error, not a traceback
        click.echo(f"error: report catalogue read failed: {e}", err=True)
        raise SystemExit(1) from e

    if output_json:
        click.echo(_json.dumps(cat, indent=2, default=str))
        return

    reports = cat.get("reports") or []
    if not reports:
        click.echo("(no reports available)")
        return
    for i, rep in enumerate(reports):
        if i:
            click.echo("")
        click.echo(f"{rep.get('id')}  —  {rep.get('title')}")
        desc = (rep.get("description") or "").strip()
        if desc:
            click.echo(f"  {desc}")
        params = rep.get("params") or []
        if not params:
            click.echo("  params: (none)")
            continue
        click.echo("  params:")
        for p in params:
            default = p.get("default")
            default_s = repr(default) if default else "(none)"
            bits = [f"kind={p.get('kind')}", f"default={default_s}"]
            choices = p.get("choices") or []
            if choices:
                allowed = ", ".join(str(c) for c in choices)
                if p.get("free_form"):
                    allowed += ", or any duration (e.g. 13h)"
                bits.append(f"allowed={allowed}")
            click.echo(f"    {p.get('id')}  ({p.get('label')})")
            click.echo(f"      {'; '.join(bits)}")
            help_text = (p.get("help") or "").strip()
            if help_text:
                click.echo(f"      {help_text}")


@report_group.command("run", help="Run a report by id.")
@click.argument("report_id")
@click.option(
    "--param", "raw_params", multiple=True, metavar="KEY=VALUE",
    help="Report parameter, repeatable (e.g. --param since=13h).",
)
@click.option(
    "--format", "output_format",
    type=click.Choice(["table", "json", "csv", "ndjson", "md", "xlsx"]),
    default="table",
    show_default=True,
    help=(
        "Output encoding: human table, raw ReportResult JSON, CSV, "
        "newline-delimited JSON, a markdown table, or an .xlsx workbook "
        "(#3472; needs the 'reports-xlsx' extra)."
    ),
)
@click.option(
    "--json", "legacy_json", is_flag=True, default=False, hidden=True,
    help="Deprecated alias for --format json.",
)
@_CONFIG_OPTION
def report_run(
    report_id: str,
    raw_params: tuple[str, ...],
    output_format: str,
    legacy_json: bool,
    config_path: Path,  # noqa: ARG001 — accepted for --config-flag consistency; reports need no coordinator.yml
) -> None:
    # #1765: `--json` predates `--format` and is kept as a hidden alias so
    # existing scripts and the #1742 smoke commands keep working verbatim.
    # Hidden, not removed: it is absent from --help (there is one documented
    # way to ask for JSON) while still being accepted.
    if legacy_json:
        output_format = "json"
    from coord.state import run_report  # noqa: PLC0415

    params: dict[str, str] = {}
    for raw in raw_params:
        if "=" not in raw:
            click.echo(
                f"error: --param expects KEY=VALUE, got {raw!r} "
                "(e.g. --param since=13h)",
                err=True,
            )
            raise SystemExit(2)
        key, _, value = raw.partition("=")
        params[key.strip()] = value

    try:
        result = run_report(report_id, params)
    except ValueError as e:
        # ReportError (local) or the daemon's 400/404 body, re-raised as a
        # ValueError by coord.client.fetch_report — either way it is a user
        # error whose message already names what was allowed.
        click.echo(f"error: {e}", err=True)
        raise SystemExit(2) from e
    except Exception as e:  # noqa: BLE001 — transport/DB failure
        click.echo(f"error: report run failed: {e}", err=True)
        raise SystemExit(1) from e

    if output_format == "json":
        click.echo(_json.dumps(result, indent=2, default=str))
        return

    if output_format in ("csv", "ndjson", "md", "xlsx"):
        # #1765 / #3472: the *server's* serializer, applied to the raw wire
        # result — never a re-serialisation of the human table. That is what
        # makes these bytes identical to `GET /report/{id}?format=<fmt>`, and
        # what keeps `started_at` an epoch instead of the `13h ago` this
        # module renders two functions up. `EXPORT_FORMATS` is the single
        # table every surface (CLI, daemon, dashboard) dispatches through —
        # see `coord.reports.EXPORT_FORMATS`.
        from coord.reports import EXPORT_FORMATS, XlsxExtraMissingError  # noqa: PLC0415

        export = EXPORT_FORMATS[output_format]
        try:
            body = export.serialize(result)
        except XlsxExtraMissingError as e:
            click.echo(f"error: {e}", err=True)
            raise SystemExit(2) from e
        # nl=False: the serializer already terminates every line (text
        # formats) or is a self-contained binary blob (xlsx), and click
        # would otherwise append a stray blank line/byte that the daemon's
        # bytes do not have. click.echo writes bytes straight to the binary
        # stream when given bytes, so xlsx round-trips undamaged even on a
        # platform whose stdout does newline translation.
        click.echo(body, nl=False)
        return

    window = result.get("window") or [None, None]
    click.echo(
        f"{result.get('report_id')}  —  window {_abs_time(window[0])} → "
        f"{_abs_time(window[1])}"
    )
    rows = result.get("rows") or []
    click.echo("")
    if not rows:
        click.echo("(no activity in this window)")
    else:
        for line in _render_table(result):
            click.echo(line)

    notes = result.get("notes") or []
    if notes:
        click.echo("")
        click.echo(f"notes ({len(notes)}):")
        for note in notes:
            click.echo(f"  • {note}")


@report_group.command(
    "export",
    help="Export a report for sharing outside the fleet (#3474; --public is required today).",
)
@click.argument("report_id")
@click.option(
    "--param", "raw_params", multiple=True, metavar="KEY=VALUE",
    help="Report parameter, repeatable (e.g. --param since=13h).",
)
@click.option(
    "--public", is_flag=True, default=False,
    help=(
        "Redact every repo not in coordinator.yml's "
        "reporting.public.allowlist_repos before exporting — everything "
        "else is aggregated into one 'private repo' row with no name, "
        "issue number or title. The only export mode supported today; "
        "omitting this flag is a usage error rather than silently "
        "producing an un-redacted export."
    ),
)
@click.option(
    "--out", "out_dir", type=click.Path(path_type=Path), default=Path("."),
    show_default=True,
    help="Directory to write the exported <report>.public.html/.csv pair into.",
)
@_CONFIG_OPTION
def report_export(
    report_id: str,
    raw_params: tuple[str, ...],
    public: bool,
    out_dir: Path,
    config_path: Path,
) -> None:
    if not public:
        click.echo(
            "error: `coord report export` needs --public today — a "
            "self-contained, redacted HTML+CSV pair for sharing outside the "
            "fleet (#3474). There is no un-redacted export mode.",
            err=True,
        )
        raise SystemExit(2)

    from coord.reports import (  # noqa: PLC0415
        REPORTS,
        PublicExportError,
        assert_public_export_allowed,
        public_csv_filename,
        public_html_filename,
        redact_report_for_public,
        result_to_csv,
        result_to_public_html,
    )
    from coord.state import run_report  # noqa: PLC0415

    params: dict[str, str] = {}
    for raw in raw_params:
        if "=" not in raw:
            click.echo(
                f"error: --param expects KEY=VALUE, got {raw!r} "
                "(e.g. --param since=13h)",
                err=True,
            )
            raise SystemExit(2)
        key, _, value = raw.partition("=")
        params[key.strip()] = value

    try:
        result = run_report(report_id, params)
    except ValueError as e:
        # ReportError (local) or the daemon's 400/404 body, re-raised as a
        # ValueError by coord.client.fetch_report — mirrors `report run`.
        click.echo(f"error: {e}", err=True)
        raise SystemExit(2) from e
    except Exception as e:  # noqa: BLE001 — transport/DB failure
        click.echo(f"error: report run failed: {e}", err=True)
        raise SystemExit(1) from e

    # #3474: redaction runs HERE, client-side, over the already-fetched
    # `result` — same precedent `--format csv|xlsx|...` above already sets
    # (those also serialise client-side over `run_report`'s own return
    # value), so a thin client never needs a second, DB-backed export path.
    # The allowlist itself goes through `_resolve_public_allowlist` — the
    # SAME thin-client-first resolution every other config read uses
    # (#1080/#947/#2824), not a raw local-file load — so a thin client whose
    # daemon has since tightened `reporting.public.allowlist_repos` can
    # never under-redact from a stale local copy. Any failure to resolve it
    # (daemon unreachable, local file malformed/absent — reports need no
    # config at all for `list`/`run`) is NOT a usage error here: it falls
    # back to the empty allowlist, i.e. redact everything, the only safe
    # default for an export leaving the fleet.
    try:
        allowed = _resolve_public_allowlist(config_path)
    except Exception as e:  # noqa: BLE001 — fail CLOSED: unreadable/unreachable config => redact everything
        click.echo(
            f"warning: could not resolve reporting.public.allowlist_repos "
            f"({e}) — redacting every repo.",
            err=True,
        )
        allowed = frozenset()

    report_def = REPORTS.get(report_id)
    redacted = redact_report_for_public(result, allowed_repos=allowed, report=report_def)
    try:
        assert_public_export_allowed(redacted, report_id)
    except PublicExportError as e:
        click.echo(f"error: {e}", err=True)
        raise SystemExit(2) from e

    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / public_html_filename(redacted)
    csv_path = out_dir / public_csv_filename(redacted)
    html_path.write_text(result_to_public_html(redacted), encoding="utf-8")
    csv_path.write_text(result_to_csv(redacted), encoding="utf-8")
    click.echo(f"wrote {html_path}")
    click.echo(f"wrote {csv_path}")
