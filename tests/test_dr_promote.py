"""`coord dr promote` — restore onto a standby and serve the board (#3129).

Rung D3 of epic #3117. Black-box where it counts, and for the same reason
`tests/test_dr_verify.py` is: the failure modes this rung has to catch are all
*integration* failures — a refusal that never fires, a unit start that reports
success without the unit running, a credential check that grades presence
instead of capability — and every one of those survives a mock of the boundary
it lives on.

So the external world here is made of **real subprocesses**:

* a fake ``restic`` on ``$PATH`` (a trimmed sibling of `test_dr_verify.py`'s)
  so the restore runs through the real `subprocess` boundary with the real
  argv and the real environment plumbing;
* a fake ``systemctl`` on ``$PATH`` backed by a JSON state file, so unit state
  is *queried* through the same batched ``systemctl show`` production uses, a
  masked unit is genuinely masked, and the ordered list of units actually
  started is observable rather than asserted against a mock's call log;
* a fake ``gh`` on ``$PATH`` that answers ``.permissions.push`` per repo, so
  "can this token merge?" is a real capability answer this test can flip;
* **real git**, against a real bare remote on disk — dirty, behind and
  up-to-date are actual repository states, not a table of canned
  ``subprocess.run`` answers, because "resolve the symlink and ask about the
  right directory" is the property most likely to break;
* **real HTTP servers** for the incumbent's ``/healthz`` and the promoted
  daemon's ``/board``, so the refusal names something that genuinely answered
  and the verification step reads a genuine response.

The store the tests restore is the real coord schema (`_ensure_schema` + the
#748 board fixture), because the parity assertion is an assertion *about* that
schema.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sqlite3
import subprocess
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from click.testing import CliRunner

from coord import deploy_manifest, dr_promote
from coord.cli import main
from coord.db import _ensure_schema
from coord.gen_board_fixture import build_fixture_db

DAEMON_UNITS = deploy_manifest.ROLE_UNITS[deploy_manifest.ROLE_DAEMON]

# --------------------------------------------------------------------------
# Shims: real binaries, real argv, real exit codes.
# --------------------------------------------------------------------------

_RESTIC_SHIM = r'''#!/usr/bin/env python3
"""Stand-in restic: enough of the CLI for `snapshots` and `restore`."""
import json, os, shutil, sys
from pathlib import Path

args = sys.argv[1:]
repo = os.environ.get("RESTIC_REPOSITORY", "")
if not repo:
    sys.stderr.write("Fatal: no repository specified\n"); sys.exit(1)
if not os.environ.get("RESTIC_PASSWORD"):
    # The real restic refuses too. This is what proves the password reached
    # the child through the environment and not through argv.
    sys.stderr.write("Fatal: no repository password\n"); sys.exit(1)
root = Path(repo)
if args[:1] == ["snapshots"]:
    sys.stdout.write(json.dumps([
        {"id": "0123456789abcdef0123", "time": "2026-09-05T04:00:00Z",
         "tags": ["coord-store"]},
    ]))
    sys.exit(0)
if args[:1] == ["restore"]:
    target = Path(args[args.index("--target") + 1])
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(str(root / "coord-store.db"), str(target / "coord-store.db"))
    sys.exit(0)
sys.stderr.write("fake restic: unhandled %s\n" % (args,)); sys.exit(2)
'''

_SYSTEMCTL_SHIM = r'''#!/usr/bin/env python3
"""Stand-in systemctl --user: `show` reads state, `enable --now` mutates it."""
import json, os, sys

path = os.environ["FAKE_SYSTEMCTL_STATE"]
state = json.loads(open(path).read())
args = [a for a in sys.argv[1:] if a != "--user"]
units = state["units"]

if args[:1] == ["show"]:
    names = [a for a in args[1:] if not a.startswith("--")]
    blocks = []
    for name in names:
        unit = units.get(name)
        if unit is None:
            blocks.append(
                "Id=%s\nLoadState=not-found\nUnitFileState=\n"
                "ActiveState=inactive\nSubState=dead" % name
            )
        else:
            blocks.append(
                "Id=%s\nLoadState=loaded\nUnitFileState=%s\n"
                "ActiveState=%s\nSubState=running"
                % (name, unit["file_state"], unit["active"])
            )
    sys.stdout.write("\n\n".join(blocks) + "\n")
    sys.exit(0)

if args[:1] == ["enable"]:
    name = args[-1]
    state.setdefault("started", []).append(name)
    unit = units.get(name)
    if unit is None:
        open(path, "w").write(json.dumps(state))
        sys.stderr.write("Failed to enable unit: Unit %s not found.\n" % name)
        sys.exit(1)
    rc = int(unit.get("enable_rc", 0))
    if rc == 0:
        unit["active"] = unit.get("becomes", "active")
    open(path, "w").write(json.dumps(state))
    sys.exit(rc)

sys.stderr.write("fake systemctl: unhandled %s\n" % (args,)); sys.exit(2)
'''

_GH_SHIM = r'''#!/usr/bin/env python3
"""Stand-in gh: answers repo permissions and an issues read.

Deliberately as strict about argv as the real `gh` is about semantics. The
push probe is only answered `true`/`false` when `--jq .permissions.push` is
actually present: without it real gh returns the repo's whole JSON body, which
the caller would then fail to parse. A shim that ignored the flag would answer
`true` either way and hide a dropped `--jq` — the one thing #3129's move of
this argv into coord/github_ops.py could plausibly get wrong.
"""
import json, os, sys

path = os.environ["FAKE_GH_STATE"]
state = json.loads(open(path).read())
args = sys.argv[1:]
state.setdefault("calls", []).append(args)
open(path, "w").write(json.dumps(state))

if args[:1] == ["api"] and len(args) >= 2:
    target = args[1]
    if "/issues" in target:
        if state.get("issues_ok", True):
            sys.stdout.write("[]\n"); sys.exit(0)
        sys.stderr.write("gh: Not Found (HTTP 404)\n"); sys.exit(1)
    slug = target[len("repos/"):]
    answer = state.get("push", {}).get(slug, state.get("push_default", True))
    if answer is None:
        sys.stderr.write("gh: HTTP 401 Bad credentials\n"); sys.exit(1)
    if args[2:] != ["--jq", ".permissions.push"]:
        # What real gh does without the filter: the full repo object.
        sys.stdout.write(json.dumps({"full_name": slug,
                                     "permissions": {"push": answer}}) + "\n")
        sys.exit(0)
    sys.stdout.write("true\n" if answer else "false\n"); sys.exit(0)

sys.stderr.write("fake gh: unhandled %s\n" % (args,)); sys.exit(2)
'''


def _write_shim(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


# --------------------------------------------------------------------------
# Real HTTP, so "something answered" means something answered.
# --------------------------------------------------------------------------


@contextlib.contextmanager
def http_server(routes: dict[str, dict], *, token: str | None = None):
    """Serve *routes* (path → JSON body) on an ephemeral port.

    When *token* is set, every path except ``/healthz`` demands
    ``Authorization: Bearer <token>`` and answers 401 otherwise — the daemon's
    own contract (``coord.serve_app``), so the bearer-token gate is a real one.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
            body = routes.get(self.path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            if token and self.path != "/healthz":
                if self.headers.get("Authorization") != f"Bearer {token}":
                    self.send_response(401)
                    self.end_headers()
                    return
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:  # noqa: ANN002 — silence the console
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def dead_url() -> str:
    """A URL nothing is listening on — the state a real promotion runs in."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


# --------------------------------------------------------------------------
# The standby
# --------------------------------------------------------------------------

CONFIG_YAML = """\
repos:
  - name: claude-coordinator
    github: JDonaghy/claude-coordinator
machines:
  - name: standby
    host: standby
    repos:
      - claude-coordinator
    repo_paths:
      claude-coordinator: {checkout}
"""

TOKEN = "board-token-for-tests"


@dataclass
class Standby:
    tmp: Path
    coord_dir: Path
    settings: Path
    origin: Path
    gh_state: Path
    systemctl_state: Path
    fixture_db: Path

    def systemd(self) -> dict:
        return json.loads(self.systemctl_state.read_text())

    def started(self) -> list[str]:
        return self.systemd().get("started", [])

    def set_gh(self, **kwargs) -> None:
        state = json.loads(self.gh_state.read_text())
        state.update(kwargs)
        self.gh_state.write_text(json.dumps(state))

    def mask(self, unit: str) -> None:
        state = self.systemd()
        state["units"][unit]["file_state"] = "masked"
        self.systemctl_state.write_text(json.dumps(state))

    def uninstall(self, unit: str) -> None:
        state = self.systemd()
        state["units"].pop(unit, None)
        self.systemctl_state.write_text(json.dumps(state))

    @property
    def live_db(self) -> Path:
        return self.coord_dir / "coord.db"

    @property
    def role_file(self) -> Path:
        return self.coord_dir / "role"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return proc.stdout


@pytest.fixture
def standby(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Standby:
    coord_dir = tmp_path / "coord"
    coord_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    # --- git identity, isolated from whatever this machine has configured ---
    global_cfg = tmp_path / "gitconfig"
    global_cfg.write_text(
        "[user]\n\tname = Standby Operator\n\temail = standby@example.invalid\n"
        "[init]\n\tdefaultBranch = main\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    # --- the coord-settings checkout, with a real remote ---
    origin = tmp_path / "coord-settings.git"
    subprocess.run(
        ["git", "init", "--bare", str(origin)], check=True, capture_output=True
    )
    settings = tmp_path / "coord-settings"
    subprocess.run(
        ["git", "init", str(settings)], check=True, capture_output=True
    )
    (settings / "coordinator.yml").write_text(
        CONFIG_YAML.format(checkout=settings)
    )
    _git(settings, "add", "coordinator.yml")
    _git(settings, "commit", "-m", "config")
    _git(settings, "branch", "-M", "main")
    _git(settings, "remote", "add", "origin", str(origin))
    _git(settings, "push", "-u", "origin", "main")

    # The real fleet shape: ~/.coord/coordinator.yml is a SYMLINK into the
    # checkout. Resolving the link rather than stat-ing it is the property
    # #3120 calls out as fragile, so the fixture keeps it honest.
    (coord_dir / "coordinator.yml").symlink_to(settings / "coordinator.yml")

    # --- the snapshot that will be restored ---
    restic_repo = tmp_path / "restic-repo"
    restic_repo.mkdir()
    fixture_db = restic_repo / "coord-store.db"
    conn = sqlite3.connect(fixture_db)
    try:
        _ensure_schema(conn)
        build_fixture_db(conn)
        conn.commit()
    finally:
        conn.close()

    # --- shims ---
    _write_shim(bin_dir / "restic", _RESTIC_SHIM)
    _write_shim(bin_dir / "systemctl", _SYSTEMCTL_SHIM)
    _write_shim(bin_dir / "gh", _GH_SHIM)

    systemctl_state = tmp_path / "systemctl.json"
    systemctl_state.write_text(
        json.dumps(
            {
                "units": {
                    name: {"file_state": "enabled", "active": "inactive"}
                    for name in DAEMON_UNITS
                },
                "started": [],
            }
        )
    )
    gh_state = tmp_path / "gh.json"
    gh_state.write_text(json.dumps({"push_default": True, "issues_ok": True}))

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SYSTEMCTL_STATE", str(systemctl_state))
    monkeypatch.setenv("FAKE_GH_STATE", str(gh_state))
    monkeypatch.setenv("COORD_DIR", str(coord_dir))
    monkeypatch.delenv("COORD_CONFIG", raising=False)
    monkeypatch.setenv("COORD_BACKUP_REPOSITORY", str(restic_repo))
    monkeypatch.setenv("RESTIC_PASSWORD", "hunter2-not-in-argv")
    monkeypatch.setenv("COORD_SERVE_TOKEN", TOKEN)
    monkeypatch.setenv("COORD_SERVICE_URL", dead_url())

    # ~/.coord/serve_token is a module constant bound at import from the real
    # HOME; point it somewhere absent so only this fixture's env var counts.
    import coord.serve_app as serve_app

    monkeypatch.setattr(serve_app, "SERVE_TOKEN_FILE", tmp_path / "absent-token")

    return Standby(
        tmp=tmp_path,
        coord_dir=coord_dir,
        settings=settings,
        origin=origin,
        gh_state=gh_state,
        systemctl_state=systemctl_state,
        fixture_db=fixture_db,
    )


def run_promote(*args: str):
    return CliRunner().invoke(main, ["dr", "promote", *args])


def board_payload(assignments: int) -> dict:
    return {
        "schema_version": 1,
        "assignments": [{"assignment_id": f"a{i}"} for i in range(assignments)],
    }


def store_assignments(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0])
    finally:
        conn.close()


# --------------------------------------------------------------------------
# --dry-run: the primary interface
# --------------------------------------------------------------------------


def test_dry_run_prints_an_ordered_plan_exits_zero_and_mutates_nothing(standby):
    before = _git(standby.settings, "rev-parse", "HEAD")

    result = run_promote("--dry-run")

    assert result.exit_code == 0, result.output
    # The plan is ordered, and the order is the one the module documents.
    positions = [
        result.output.index(f"{n}. {step}")
        for n, step in enumerate(
            [
                dr_promote.STEP_INCUMBENT,
                dr_promote.STEP_RESTORE,
                dr_promote.STEP_CONFIG,
                dr_promote.STEP_CREDENTIALS,
                dr_promote.STEP_UNITS,
                dr_promote.STEP_VERIFY,
            ],
            start=1,
        )
    ]
    assert positions == sorted(positions)
    assert "REFUSALS: none" in result.output

    # …and nothing moved.
    assert not standby.live_db.exists()
    assert not standby.role_file.exists()
    assert standby.started() == []
    assert _git(standby.settings, "rev-parse", "HEAD") == before
    assert _git(standby.settings, "status", "--porcelain") == ""


def test_dry_run_still_reports_a_live_incumbent_and_still_exits_zero(standby):
    """A rehearsal that says "dellserver is still serving" has rehearsed fine."""
    with http_server({"/healthz": {"status": "ok"}}) as url:
        result = run_promote("--dry-run", "--board-url", url)

    assert result.exit_code == 0, result.output
    assert "is serving a live board" in result.output
    assert not standby.live_db.exists()
    assert standby.started() == []


def test_dry_run_lists_every_daemon_unit_in_manifest_order(standby):
    result = run_promote("--dry-run")

    listed = [
        line.split(". ", 1)[1].split(" ", 1)[0]
        for line in result.output.splitlines()
        if line.strip().split(". ", 1)[-1].split(" ")[0] in DAEMON_UNITS
        and line.startswith("   ")
    ]
    assert listed == list(DAEMON_UNITS)


# --------------------------------------------------------------------------
# The refusal that matters most: a live incumbent
# --------------------------------------------------------------------------


def test_refuses_when_the_incumbent_answers_health_and_names_the_responder(standby):
    with http_server({"/healthz": {"status": "ok", "store_backend": "sqlite"}}) as url:
        result = run_promote("--board-url", url)

    assert result.exit_code == 1
    combined = result.output + str(result.exception or "")
    host = url.split("://", 1)[1]
    assert host in combined
    assert "is serving a live board" in combined
    # Never runs against a machine that currently holds a live board.
    assert not standby.live_db.exists()
    assert standby.started() == []


def test_force_waives_a_live_incumbent_but_nothing_else(standby):
    """`--force` is the operator asserting the incumbent is gone — nothing more."""
    standby.set_gh(push={"JDonaghy/claude-coordinator": False})

    with http_server({"/healthz": {"status": "ok"}}) as url:
        result = run_promote("--force", "--board-url", url)

    assert result.exit_code == 1
    combined = result.output + str(result.exception or "")
    # The plan still *lists* the waived refusal; what --force changes is which
    # of them the run actually stops on, which is the FAILED line's list.
    stopped_on = combined.split("FAILED after")[-1]
    assert "is serving a live board" not in stopped_on
    assert "github-token" in stopped_on
    assert standby.started() == []


# --------------------------------------------------------------------------
# The coord-settings checkout: absent, dirty, behind
# --------------------------------------------------------------------------


def test_refuses_when_the_settings_checkout_is_absent(standby):
    (standby.coord_dir / "coordinator.yml").unlink()

    result = run_promote("--force")

    assert result.exit_code == 1
    assert "no coordinator.yml at" in result.output
    assert not standby.live_db.exists()
    assert standby.started() == []


def test_refuses_when_the_settings_checkout_is_dirty(standby):
    (standby.settings / "coordinator.yml").write_text(
        (standby.settings / "coordinator.yml").read_text() + "\n# half-finished edit\n"
    )

    result = run_promote("--force")

    assert result.exit_code == 1
    assert "uncommitted change" in result.output
    assert standby.started() == []


def test_refuses_when_the_settings_checkout_is_behind_its_remote(standby):
    # Someone else pushed a config change this standby has never pulled.
    other = standby.tmp / "other-clone"
    subprocess.run(
        ["git", "clone", str(standby.origin), str(other)],
        check=True,
        capture_output=True,
    )
    (other / "coordinator.yml").write_text(
        (other / "coordinator.yml").read_text() + "\n# a change made elsewhere\n"
    )
    _git(other, "add", "coordinator.yml")
    _git(other, "commit", "-m", "elsewhere")
    _git(other, "push", "origin", "main")

    result = run_promote("--force")

    assert result.exit_code == 1
    assert "is behind" in result.output
    assert standby.started() == []
    assert not standby.live_db.exists()


def test_an_unreachable_remote_reads_as_unknown_not_as_up_to_date(standby):
    """The permissive default is the failure mode, so it must not exist."""
    _git(standby.settings, "remote", "set-url", "origin", str(standby.tmp / "gone.git"))

    result = run_promote("--force")

    assert result.exit_code == 1
    assert "could not determine whether" in result.output
    assert standby.started() == []


def test_an_ahead_checkout_is_a_note_not_a_refusal(standby):
    """#3120's condition, reported — but a local-only commit cannot stale the fleet."""
    (standby.settings / "extra.txt").write_text("local\n")
    _git(standby.settings, "add", "extra.txt")
    _git(standby.settings, "commit", "-m", "local only")

    result = run_promote("--dry-run")

    assert result.exit_code == 0
    assert "unpushed commit(s)" in result.output
    assert "REFUSALS: none" in result.output


# --------------------------------------------------------------------------
# Credentials: capability, reported before anything starts
# --------------------------------------------------------------------------


def test_missing_credentials_are_named_individually_before_any_unit_starts(
    standby, monkeypatch
):
    monkeypatch.delenv("COORD_SERVE_TOKEN")
    standby.set_gh(push={"JDonaghy/claude-coordinator": False})

    result = run_promote("--force")

    assert result.exit_code == 1
    assert "github-token" in result.output
    assert "board-token" in result.output
    # Each named on its own line, not lumped into one "credentials missing".
    assert "no push permission on JDonaghy/claude-coordinator" in result.output
    assert "no bearer token" in result.output
    # Reported BEFORE anything is started, and nothing was.
    assert standby.started() == []
    assert not standby.live_db.exists()


def test_a_token_that_cannot_merge_is_not_reported_as_present(standby):
    """Capability, not presence: `gh` answers, the token just cannot push."""
    standby.set_gh(push={"JDonaghy/claude-coordinator": False})

    result = run_promote("--dry-run")

    assert "[MISS] github-token: incapable" in result.output
    assert "no push permission on JDonaghy/claude-coordinator" in result.output


def test_a_token_that_cannot_read_issues_is_not_reported_as_present(standby):
    standby.set_gh(issues_ok=False)

    result = run_promote("--dry-run")

    assert "CANNOT read issues on JDonaghy/claude-coordinator" in result.output
    assert "[MISS] github-token" in result.output


def test_an_unprobeable_token_blocks_rather_than_defaulting_to_ok(standby):
    """`unknown` is a blocker: "we could not check" must never render "fine"."""
    standby.set_gh(push={"JDonaghy/claude-coordinator": None})

    result = run_promote("--force")

    assert result.exit_code == 1
    assert "github-token: unknown" in result.output
    assert standby.started() == []


def test_a_missing_git_identity_is_reported_as_missing():
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        if argv[:3] == ["git", "config", "--get"]:
            return subprocess.CompletedProcess(argv, 1, "", "")
        raise AssertionError(f"unexpected probe after a missing identity: {argv}")

    cred = dr_promote.check_git_push_credential(None, runner=runner)

    assert cred.verdict == dr_promote.CRED_MISSING
    assert "user.name" in cred.detail and "user.email" in cred.detail
    # It never went on to probe a push with an identity it does not have.
    assert not any("push" in c for c in calls)


def test_a_refused_dry_run_push_is_incapable_not_unknown(tmp_path):
    def runner(argv, **kwargs):
        if argv[:3] == ["git", "config", "--get"]:
            value = "Someone" if argv[-1] == "user.name" else "someone@example.invalid"
            return subprocess.CompletedProcess(argv, 0, value + "\n", "")
        return subprocess.CompletedProcess(
            argv, 128, "", "remote: Permission to x/y.git denied to nobody.\n"
        )

    cred = dr_promote.check_git_push_credential(
        None, runner=runner, checkout=tmp_path
    )

    assert cred.verdict == dr_promote.CRED_INCAPABLE
    assert "refused a dry-run push" in cred.detail


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------


def test_units_are_started_in_role_units_order(standby):
    with http_server(
        {"/board": board_payload(store_assignments(standby.fixture_db))}, token=TOKEN
    ) as url:
        result = run_promote("--local-board-url", url, "--verify-timeout", "20")

    assert result.exit_code == 0, result.output
    assert standby.started() == list(DAEMON_UNITS)


def test_a_masked_unit_is_skipped_rather_than_failing_the_run(standby):
    standby.mask("coord-web.service")

    with http_server(
        {"/board": board_payload(store_assignments(standby.fixture_db))}, token=TOKEN
    ) as url:
        result = run_promote("--local-board-url", url, "--verify-timeout", "20")

    assert result.exit_code == 0, result.output
    assert "coord-web.service" not in standby.started()
    assert [u for u in DAEMON_UNITS if u != "coord-web.service"] == standby.started()
    assert "skip (masked)" in result.output


def test_a_unit_systemd_does_not_know_about_blocks_the_run(standby):
    standby.uninstall("coord-serve.service")

    result = run_promote("--force")

    assert result.exit_code == 1
    assert "coord-serve.service" in result.output
    assert "does not know about" in result.output
    assert standby.started() == []


def test_no_systemd_session_says_so_rather_than_naming_ten_forgotten_units(standby):
    """Every unit missing usually means the query failed, not ten oversights."""
    # No systemd user session: `systemctl show` answers nothing at all.
    _write_shim(
        standby.tmp / "bin" / "systemctl",
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stderr.write('Failed to connect to user scope bus\\n')\n"
        "sys.exit(1)\n",
    )

    result = run_promote("--force")

    assert result.exit_code == 1
    assert "no state for ANY of them" in result.output
    assert not standby.live_db.exists()


def test_a_unit_that_exits_zero_but_never_becomes_active_fails_the_run(standby):
    """Success derived from an observation after the action, never from exit 0."""
    state = standby.systemd()
    # `enable --now` succeeds; the unit lands inactive a moment later.
    state["units"]["coord-notify.timer"]["becomes"] = "failed"
    standby.systemctl_state.write_text(json.dumps(state))

    with http_server(
        {"/board": board_payload(store_assignments(standby.fixture_db))}, token=TOKEN
    ) as url:
        result = run_promote("--local-board-url", url, "--verify-timeout", "20")

    assert result.exit_code == 1
    combined = result.output + str(result.exception or "")
    assert "coord-notify.timer" in combined
    assert "ActiveState=failed" in combined


# --------------------------------------------------------------------------
# Verification, and the report
# --------------------------------------------------------------------------


def test_success_prints_the_elapsed_time_and_the_remaining_manual_steps(standby):
    rows = store_assignments(standby.fixture_db)
    with http_server({"/board": board_payload(rows)}, token=TOKEN) as url:
        result = run_promote("--local-board-url", url, "--verify-timeout", "20")

    assert result.exit_code == 0, result.output
    assert "coord dr promote: OK in " in result.output
    assert "elapsed" in result.output
    assert "REMAINING MANUAL STEPS" in result.output
    assert "tailnet-acl.hujson" in result.output
    assert "client.toml" in result.output
    # The restore actually landed, and the host now declares itself the daemon.
    assert standby.live_db.exists()
    assert store_assignments(standby.live_db) == rows
    assert standby.role_file.read_text().strip() == deploy_manifest.ROLE_DAEMON


def test_the_record_carries_the_measured_rto(standby, tmp_path):
    rows = store_assignments(standby.fixture_db)
    record = tmp_path / "promote.json"
    with http_server({"/board": board_payload(rows)}, token=TOKEN) as url:
        result = run_promote(
            "--local-board-url", url, "--verify-timeout", "20", "--record", str(record)
        )

    assert result.exit_code == 0, result.output
    body = json.loads(record.read_text())
    assert body["outcome"] == "ok"
    assert body["elapsed_seconds"] > 0
    assert body["remaining_manual_steps"]
    assert body["units"]["coord-serve.service"] == "start"


def test_a_wrong_board_token_fails_the_run_rather_than_reporting_recovered(standby):
    """The board-token gate is deferred to step 6 — and it can fail."""
    rows = store_assignments(standby.fixture_db)
    with http_server(
        {"/board": board_payload(rows)}, token="a-completely-different-token"
    ) as url:
        result = run_promote("--local-board-url", url, "--verify-timeout", "20")

    assert result.exit_code == 1
    combined = result.output + str(result.exception or "")
    assert "rejected the daemon's own bearer token" in combined


def test_a_board_serving_nothing_from_a_populated_store_is_a_failure(standby):
    rows = store_assignments(standby.fixture_db)
    assert rows > 0
    with http_server({"/board": board_payload(0)}, token=TOKEN) as url:
        result = run_promote("--local-board-url", url, "--verify-timeout", "20")

    assert result.exit_code == 1
    combined = result.output + str(result.exception or "")
    assert "served 0 assignments" in combined


def test_a_board_that_never_answers_is_a_failure_not_a_shrug(standby):
    result = run_promote("--local-board-url", dead_url(), "--verify-timeout", "2")

    assert result.exit_code == 1
    combined = result.output + str(result.exception or "")
    assert "did not serve GET /board" in combined


def test_verify_rejects_a_payload_that_is_not_a_board(standby, tmp_path):
    step = dr_promote.verify_board(
        db_path=standby.fixture_db,
        url="http://board.invalid",
        token=None,
        fetcher=lambda svc, **kw: {"totally": "not a board"},
    )
    assert not step.ok
    assert "not a board payload" in step.detail


def test_verify_rejects_a_board_serving_more_than_the_store_holds(standby):
    rows = store_assignments(standby.fixture_db)
    step = dr_promote.verify_board(
        db_path=standby.fixture_db,
        url="http://board.invalid",
        token=None,
        fetcher=lambda svc, **kw: board_payload(rows + 5),
    )
    assert not step.ok
    assert "not serving the store that was just restored" in step.detail


# --------------------------------------------------------------------------
# Secret hygiene
# --------------------------------------------------------------------------


def test_no_credential_reaches_argv_or_a_log_line(standby, caplog):
    caplog.set_level("DEBUG")
    rows = store_assignments(standby.fixture_db)
    with http_server({"/board": board_payload(rows)}, token=TOKEN) as url:
        result = run_promote("--local-board-url", url, "--verify-timeout", "20")

    assert result.exit_code == 0, result.output
    haystack = result.output + "\n".join(r.getMessage() for r in caplog.records)
    assert "hunter2-not-in-argv" not in haystack
    assert TOKEN not in haystack
    # The restic password reached the child through the environment — the shim
    # exits non-zero without it, so a green run is the proof it arrived.
    assert standby.live_db.exists()


def test_the_shims_prove_no_secret_was_passed_as_an_argument(standby):
    """Whatever argv the probes built, none of it carried a credential."""
    run_promote("--dry-run")

    calls = json.loads(standby.gh_state.read_text())["calls"]
    flattened = " ".join(" ".join(c) for c in calls)
    assert TOKEN not in flattened
    assert "hunter2-not-in-argv" not in flattened
