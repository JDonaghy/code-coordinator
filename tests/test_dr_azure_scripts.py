"""Tests for the #3130 DR **server** role in the azure-workers lane (rung D4).

The lane's existing scripts all build a *worker*, and a worker is defined by
*not* being a server: no ``coord serve``, no store, deliberately no board-daemon
token, and an ACL granting ``tag:coord-worker`` exactly one destination. D4 adds
the inverse role — ``dr-up.sh`` / ``dr-down.sh`` / ``provision-server.sh`` plus
the ``tag:coord-server`` ACL rules — for the case where dellserver is not merely
broken but *gone*, along with the building it was in.

Why these tests are shaped the way they are
-------------------------------------------
A real run creates billable Azure resources and cannot run in CI, so
``--dry-run`` is the primary interface and the mode under test. Two harnesses,
both already used in this repo:

* **Source the script** (its ``main()`` is behind a ``BASH_SOURCE[0] == $0``
  guard, so sourcing runs nothing) and call individual functions with ``az`` /
  ``tailscale`` / ``curl`` stubbed as bash functions — same shape as
  ``test_epic_up_capability_detection.py``.
* **Run the script end to end** with a ``$PATH`` full of recording stubs, which
  is how the "creates nothing" and "refuses before creating anything" criteria
  are actually *proved* rather than asserted about the source text: every ``az``
  invocation is logged, and the test fails if any create-family call appears.

The ACL is checked by parsing ``tailnet-acl.hujson`` (HuJSON: JSON plus
comments and trailing commas) — that file's ``tests:`` block is the only thing
standing between an edit and a tailnet you have locked yourself out of, so the
entries are asserted structurally rather than by grep.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LANE = REPO_ROOT / "scripts" / "azure-workers"
DR_UP = LANE / "dr-up.sh"
DR_DOWN = LANE / "dr-down.sh"
PROVISION_SERVER = LANE / "provision-server.sh"
PREFLIGHT = LANE / "preflight.sh"
ACL = LANE / "tailnet-acl.hujson"

#: A value that must never escape into stdout, stderr or an argv.
SECRET_SENTINEL = "s3cr3t-value-that-must-never-be-printed"

REQUIRED_SECRETS = [
    "github-token",
    "board-token",
    "restic-password",
    "backup-repository",
    "azure-account-name",
    "azure-account-key",
    "tailscale-oauth-secret",
]


# ---------------------------------------------------------------------------
# Harness 1: source a script, call one function, stub the world as bash funcs
# ---------------------------------------------------------------------------
def _source(script: Path, body: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    driver = f"""
set -euo pipefail
source {shlex.quote(str(script))}
{body}
"""
    full_env = dict(os.environ)
    full_env.update(env or {})
    return subprocess.run(
        ["bash", "-c", driver],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=full_env,
    )


# ---------------------------------------------------------------------------
# Harness 2: run a script for real against a $PATH of recording stubs
# ---------------------------------------------------------------------------
_AZ_STUB = r"""#!/usr/bin/env python3
# Recording `az` stand-in. Every invocation is appended to $AZ_LOG, one
# shell-quoted line each, so a test can prove no create-family call was made.
import json, os, sys

argv = sys.argv[1:]
with open(os.environ["AZ_LOG"], "a") as fh:
    fh.write(" ".join(argv) + "\n")
joined = " ".join(argv)

if "keyvault secret list" in joined:
    for name in os.environ.get("STUB_VAULT_SECRETS", "").split(","):
        if name:
            print(name)
    sys.exit(0 if os.environ.get("STUB_VAULT_READABLE", "1") == "1" else 1)

if "keyvault secret show" in joined:
    name = argv[argv.index("--name") + 1] if "--name" in argv else ""
    missing = os.environ.get("STUB_MISSING_SECRETS", "").split(",")
    if name in missing:
        sys.exit(1)
    print(os.environ.get("STUB_SECRET_VALUE", "value") + "-" + name)
    sys.exit(0)

if "vm list-usage" in joined:
    free = int(os.environ.get("STUB_QUOTA_FREE", "16"))
    limit = int(os.environ.get("STUB_QUOTA_LIMIT", "32"))
    rows = [
        {"name": {"value": "StandardDasv7Family"}, "currentValue": limit - free, "limit": limit},
        {"name": {"value": "cores"}, "currentValue": limit - free, "limit": limit},
    ]
    print(json.dumps(rows))
    sys.exit(0)

if "group show" in joined:
    if os.path.exists(os.environ.get("AZ_DELETED_MARKER", "/nonexistent")):
        print("Deleting")
    else:
        print(os.environ.get("STUB_GROUP_STATE", "Succeeded"))
    sys.exit(0)

if "group delete" in joined:
    # STUB_DELETE_NOOP models the real trap: the request is accepted (exit 0)
    # but the group never actually starts deleting.
    if os.environ.get("STUB_DELETE_NOOP", "0") != "1":
        open(os.environ["AZ_DELETED_MARKER"], "w").close()
    sys.exit(0)

sys.exit(0)
"""

_TAILSCALE_STUB = r"""#!/usr/bin/env python3
import os, sys
if len(sys.argv) > 1 and sys.argv[1] == "status":
    nodes = os.environ.get("STUB_TAILNET_NODES", "")
    for name in nodes.split(","):
        if name:
            print("100.64.0.1   %s   john@   linux   -" % name)
    sys.exit(0)
sys.exit(0)
"""

_SSH_STUB = r"""#!/usr/bin/env python3
import os, sys
with open(os.environ["SSH_LOG"], "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
sys.exit(int(os.environ.get("STUB_SSH_RC", "0")))
"""

_CURL_STUB = r"""#!/usr/bin/env python3
import os, sys
body = os.environ.get("STUB_HEALTH_BODY", "")
if not body:
    sys.exit(7)
print(body)
sys.exit(0)
"""

_COORD_STUB = r"""#!/usr/bin/env python3
import os, sys
if "sessions" in sys.argv:
    # `coord sessions --json`'s real shape (coord/commands/sessions.py,
    # sessions_cmd): a top-level OBJECT keyed "sessions", not a bare array --
    # see dr_live_sessions in dr-down.sh for why that distinction matters.
    print(os.environ.get("STUB_SESSIONS_JSON", '{"sessions": []}'))
sys.exit(0)
"""


def _write_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def lane(tmp_path: Path) -> dict:
    """A stubbed azure-workers environment: recording binaries + an env file."""
    # The lane parses `az` JSON with jq (a documented preflight prereq, and what
    # epic-up.sh/epic-down.sh already use). Guarded narrowly rather than at
    # module level: the ACL, secret-table and readiness tests below need no jq
    # and must keep running even on a host without it.
    if shutil.which("jq") is None:
        pytest.skip("jq is not installed; the azure-workers scripts require it (preflight section A)")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _write_stub(bindir / "az", _AZ_STUB)
    _write_stub(bindir / "tailscale", _TAILSCALE_STUB)
    _write_stub(bindir / "ssh", _SSH_STUB)
    _write_stub(bindir / "scp", _SSH_STUB)
    _write_stub(bindir / "curl", _CURL_STUB)
    _write_stub(bindir / "coord", _COORD_STUB)

    easy_azure = tmp_path / "easy-azure"
    (easy_azure / "modules" / "coord-server-vm").mkdir(parents=True)
    (easy_azure / "modules" / "coord-server-vm" / "main.bicep").write_text("// stub\n")

    env_file = tmp_path / "epic.env"
    env_file.write_text(
        "\n".join(
            [
                f'EASY_AZURE_DIR="{easy_azure}"',
                'KEY_VAULT_NAME="kv-coord-test"',
                'SUBSCRIPTION_ID="sub-test"',
                'KEY_VAULT_URI="https://kv-coord-test.vault.azure.net/"',
                'KEY_VAULT_RESOURCE_ID="/subscriptions/sub-test/kv"',
                'IDENTITY_RESOURCE_ID="/subscriptions/sub-test/id"',
                'IDENTITY_CLIENT_ID="client-id"',
                'PRIVATE_DNS_ZONE_ID="/subscriptions/sub-test/dns"',
                "SOURCE_IMAGE_ID=/subscriptions/sub-test/resourceGroups/rg-coord-images/"
                "providers/Microsoft.Compute/galleries/sigcoord/images/coord-worker/versions/2026.0801.0",
                'LOCATION="eastus"',
            ]
        )
        + "\n"
    )

    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "AZ_LOG": str(tmp_path / "az.log"),
            "SSH_LOG": str(tmp_path / "ssh.log"),
            "AZ_DELETED_MARKER": str(tmp_path / "deleted.marker"),
            "DR_ENV": str(env_file),
            "STUB_VAULT_SECRETS": ",".join(REQUIRED_SECRETS),
            "STUB_TAILNET_NODES": "precision,elitebook",
            "STUB_SECRET_VALUE": SECRET_SENTINEL,
        }
    )
    env.pop("TAILSCALE_API_KEY", None)
    return {"env": env, "tmp": tmp_path, "az_log": tmp_path / "az.log", "easy_azure": easy_azure}


def _run_dr_up(lane: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(DR_UP), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=lane["env"],
        cwd=str(LANE),
    )


def _az_calls(lane: dict) -> list[str]:
    log = lane["az_log"]
    return log.read_text().splitlines() if log.exists() else []


#: Every `az` subcommand that would bring a billable resource into existence.
_CREATE_FAMILY = re.compile(
    r"\b(group|deployment|vm|network|identity|keyvault|sig|disk|storage)\b[^\n]*\bcreate\b"
)


def _assert_created_nothing(lane: dict) -> None:
    offenders = [c for c in _az_calls(lane) if _CREATE_FAMILY.search(c)]
    assert not offenders, f"--dry-run issued create-family az calls: {offenders}"


# ===========================================================================
# 1. --dry-run prints a complete ordered plan and creates NOTHING
# ===========================================================================


def test_sourcing_dr_up_runs_nothing(lane: dict) -> None:
    """Guard for the harness itself: main() is behind the BASH_SOURCE guard, so
    sourcing dr-up.sh (which also sources provision-server.sh) must touch
    neither Azure nor the network."""
    result = _source(DR_UP, "echo sourced-ok", env=lane["env"])
    assert result.returncode == 0, result.stderr
    assert "sourced-ok" in result.stdout
    _assert_created_nothing(lane)


def test_dry_run_prints_a_complete_ordered_plan(lane: dict) -> None:
    result = _run_dr_up(lane, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    # The ordered plan: resource group, deployment/module, sku, image, tag,
    # ACL requirements, secrets it will fetch, teardown.
    for fragment in (
        "1. resource group",
        "rg-coord-dr-coord-dr",
        "2. deployment",
        "coord-server-vm/main.bicep",
        "Standard_D4as_v7",
        "versions/2026.0801.0",
        "3. ACL requirements",
        "4. boot path",
        "secrets to fetch",
        "5. teardown",
        "./dr-down.sh",
    ):
        assert fragment in out, f"plan is missing {fragment!r}:\n{out}"
    # Ordered, not just present.
    assert out.index("1. resource group") < out.index("2. deployment") < out.index("3. ACL requirements")
    assert out.index("3. ACL requirements") < out.index("4. boot path") < out.index("5. teardown")


def test_dry_run_creates_nothing(lane: dict) -> None:
    """The acceptance criterion, proved rather than asserted: every `az`
    invocation is recorded and none of them is a create."""
    result = _run_dr_up(lane, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _az_calls(lane)
    assert calls, "expected the dry run to make read-only az calls (quota, vault)"
    _assert_created_nothing(lane)


def test_dry_run_plan_names_the_server_tag_not_the_worker_tag(lane: dict) -> None:
    """The smoke-test tell from #3130: a plan naming tag:coord-worker means the
    ACL half was never done, and a real run would produce a VM that can reach
    the board but that nothing can reach."""
    result = _run_dr_up(lane, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tag:coord-server" in result.stdout
    plan = result.stdout.split("3. ACL requirements", 1)[1].split("4. boot path", 1)[0]
    assert "tag:coord-server <- autogroup:member" in plan
    assert "tag:coord-server <- tag:coord-worker" in plan


# ===========================================================================
# 2. It refuses, before creating anything — one named test per cause
# ===========================================================================


def test_refuses_when_quota_is_insufficient(lane: dict) -> None:
    lane["env"]["STUB_QUOTA_FREE"] = "2"  # need 4
    result = _run_dr_up(lane, "--dry-run")
    assert result.returncode != 0, result.stdout
    assert "REFUSED" in result.stderr
    assert "vCPU quota" in result.stderr
    _assert_created_nothing(lane)


def test_refuses_when_the_quota_row_is_unreadable(lane: dict) -> None:
    """"We could not check" is not a pass (#2096): an unreported quota is
    treated as insufficient, because QuotaExceeded otherwise surfaces minutes
    into a real deployment with a VM already half-created."""
    result = _source(
        DR_UP,
        'az() { echo "[]"; }\n'
        'check_quota eastus Standard_D4as_v7 4 && echo PASSED || echo BLOCKED\n',
        env=lane["env"],
    )
    assert result.returncode == 0, result.stderr
    assert "BLOCKED" in result.stdout
    assert "cannot verify" in result.stdout


def test_refuses_when_the_server_tag_is_absent_from_the_tailnet_policy(lane: dict, tmp_path: Path) -> None:
    acl = tmp_path / "worker-only-acl.hujson"
    acl.write_text('{\n  "tagOwners": { "tag:coord-worker": ["autogroup:admin"] },\n}\n')
    result = _run_dr_up(lane, "--dry-run", "--acl-file", str(acl))
    assert result.returncode != 0, result.stdout
    assert "REFUSED" in result.stderr
    assert "tag:coord-server" in result.stderr
    _assert_created_nothing(lane)


def test_refuses_when_the_server_tag_exists_but_nothing_grants_it_7435(lane: dict, tmp_path: Path) -> None:
    """A declared-but-ungranted tag produces a board no worker can reach —
    which looks like a successful provision and is not one."""
    acl = tmp_path / "tagged-but-ungranted.hujson"
    acl.write_text(
        '{\n  "tagOwners": { "tag:coord-server": ["autogroup:admin"] },\n'
        '  "acls": [ { "src": ["autogroup:member"], "dst": ["tag:coord-server:22"] } ],\n}\n'
    )
    result = _run_dr_up(lane, "--dry-run", "--acl-file", str(acl))
    assert result.returncode != 0, result.stdout
    assert "nothing grants :7435" in result.stderr or "nothing grants :7435" in result.stdout
    _assert_created_nothing(lane)


def test_a_multi_port_grant_still_satisfies_the_7435_check(lane: dict, tmp_path: Path) -> None:
    """A destination written as `tag:coord-server:7433,7435` grants exactly what
    a single-port `tag:coord-server:7435` would, and must not be treated as
    ungranted merely because the port list has more than one entry -- the
    shipped tailnet-acl.hujson itself uses this multi-port form for the
    operator grant (rule with dst `tag:coord-server:22,7433,7434,7435`)."""
    acl = tmp_path / "multi-port-acl.hujson"
    acl.write_text(
        '{\n  "tagOwners": { "tag:coord-server": ["autogroup:admin"] },\n'
        '  "acls": [ { "src": ["tag:coord-worker"], "dst": ["tag:coord-server:7433,7435"] } ],\n}\n'
    )
    result = _run_dr_up(lane, "--dry-run", "--acl-file", str(acl))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing grants :7435" not in (result.stdout + result.stderr)


def test_refuses_when_the_key_vault_lacks_a_required_secret(lane: dict) -> None:
    lane["env"]["STUB_VAULT_SECRETS"] = ",".join(
        s for s in REQUIRED_SECRETS if s != "restic-password"
    )
    result = _run_dr_up(lane, "--dry-run")
    assert result.returncode != 0, result.stdout
    assert "REFUSED" in result.stderr
    assert "restic-password" in result.stdout + result.stderr
    _assert_created_nothing(lane)


def test_refuses_when_the_easy_azure_server_module_is_absent(lane: dict) -> None:
    """The cross-repo boundary: the coord-server-vm Bicep module is easy-azure's
    half of #3130. Absent, this must fail clearly rather than half-booting a
    server with no secrets — the #1777 failure mode."""
    (lane["easy_azure"] / "modules" / "coord-server-vm" / "main.bicep").unlink()
    result = _run_dr_up(lane, "--dry-run")
    assert result.returncode != 0, result.stdout
    assert "REFUSED" in result.stderr
    assert "easy-azure server module" in result.stderr
    assert "coord-server-vm/main.bicep" in result.stdout
    _assert_created_nothing(lane)


def test_refuses_when_a_shared_resource_id_is_still_a_placeholder(lane: dict) -> None:
    """A placeholder left unfilled otherwise surfaces as an opaque Azure error
    several minutes and one running VM later — the same guard epic-up.sh opens
    with."""
    env_file = Path(lane["env"]["DR_ENV"])
    env_file.write_text(
        env_file.read_text().replace('IDENTITY_CLIENT_ID="client-id"', 'IDENTITY_CLIENT_ID="<fill-me>"')
    )
    result = _run_dr_up(lane, "--dry-run")
    assert result.returncode != 0, result.stdout
    assert "still a placeholder" in result.stdout
    assert "IDENTITY_CLIENT_ID" in result.stdout
    _assert_created_nothing(lane)


def test_refuses_on_a_tailnet_hostname_collision_without_polling(lane: dict) -> None:
    """This cost 15 minutes in the worker lane: a node already holding the name
    makes Tailscale assign `<name>-1` and the readiness poll then waits the full
    timeout on the STALE node. epic-up.sh fails fast; so does this."""
    lane["env"]["STUB_TAILNET_NODES"] = "precision,coord-dr"
    result = _run_dr_up(lane, "--dry-run")
    assert result.returncode != 0, result.stdout
    assert "REFUSED" in result.stderr
    assert "already exists" in result.stdout + result.stderr
    assert "coord-dr-1" in result.stdout or "'coord-dr'" in result.stdout
    _assert_created_nothing(lane)


def test_hostname_collision_is_detected_by_exact_name_not_substring(lane: dict) -> None:
    """`coord-dr-old` is a different node; matching it as a collision would
    refuse a perfectly good name."""
    lane["env"]["STUB_TAILNET_NODES"] = "coord-dr-old,precision"
    result = _run_dr_up(lane, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr


def test_all_gates_run_in_one_pass_rather_than_one_refusal_per_rerun(lane: dict) -> None:
    """Every gate runs even after one blocks, so an operator fixes N problems in
    N fixes rather than N runs — the same reason preflight.sh is not `set -e`."""
    lane["env"]["STUB_QUOTA_FREE"] = "0"
    lane["env"]["STUB_VAULT_SECRETS"] = ""
    lane["env"]["STUB_TAILNET_NODES"] = "coord-dr"
    result = _run_dr_up(lane, "--dry-run")
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "vCPU quota" in combined
    assert "Key Vault" in combined
    assert "hostname" in combined
    _assert_created_nothing(lane)


# ===========================================================================
# 3. The boot path: which secrets it obtained, and the readiness gate
# ===========================================================================


def test_sourcing_provision_server_runs_nothing() -> None:
    result = _source(PROVISION_SERVER, "echo sourced-ok")
    assert result.returncode == 0, result.stderr
    assert "sourced-ok" in result.stdout


def test_the_secret_table_is_the_single_source_of_truth() -> None:
    """dr-up.sh's vault gate, preflight's server section and the boot-time fetch
    all read this one table — two lists that agree today are a split brain
    waiting to happen (#2085)."""
    result = _source(PROVISION_SERVER, "dr_secret_names required")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == REQUIRED_SECRETS
    # dr-up.sh does not carry its own copy.
    up = DR_UP.read_text()
    assert "dr_secret_names required" in up
    for name in REQUIRED_SECRETS:
        assert f'"{name}"' not in up, f"dr-up.sh hardcodes the secret name {name!r}"


def test_the_board_token_secret_exports_the_name_its_real_consumer_reads() -> None:
    """`coord.dr_promote.check_board_token_credential()` resolves the daemon's
    bearer token via `coord.serve_app.resolve_serve_token()`, which only ever
    reads `$COORD_SERVE_TOKEN` (or ~/.coord/serve_token) -- never a
    `COORD_BOARD_TOKEN` of this script's own invention. A mismatch here means
    the credential can never report anything but "missing", no matter how
    successfully it was fetched from Key Vault, and `dr_advertise_ready` can
    then never pass in a real run (#2085)."""
    result = _source(PROVISION_SERVER, "dr_secret_env board-token")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "COORD_SERVE_TOKEN"


def test_persist_board_token_writes_the_file_resolve_serve_token_reads(tmp_path: Path) -> None:
    """The env var alone only reaches THIS process; `coord serve` is started as
    a separate systemd --user unit that does not inherit it, and survives
    every future restart on its own. `resolve_serve_token()`'s only source
    that survives that is the token FILE, so that is what must be written."""
    target = tmp_path / "coord-home" / ".coord" / "serve_token"
    result = _source(
        PROVISION_SERVER, f'dr_persist_board_token "{SECRET_SENTINEL}" "{target}"'
    )
    assert result.returncode == 0, result.stderr
    assert target.read_text() == SECRET_SENTINEL
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert SECRET_SENTINEL not in result.stdout
    assert SECRET_SENTINEL not in result.stderr


def test_persist_board_token_is_a_no_op_when_nothing_was_obtained(tmp_path: Path) -> None:
    """The fetch step already reports a missing board-token as a blocker; this
    function must not paper over that with an empty (or stale) token file."""
    target = tmp_path / "serve_token"
    result = _source(PROVISION_SERVER, f'dr_persist_board_token "" "{target}"')
    assert result.returncode == 0, result.stderr
    assert not target.exists()


def test_boot_path_reports_which_secrets_it_obtained(tmp_path: Path) -> None:
    out = tmp_path / "secrets.env"
    result = _source(
        PROVISION_SERVER,
        _az_bash_stub(missing=["git-push-identity"])
        + f'lines="$(dr_fetch_secrets kv-test {out})"\n'
        'dr_report_secrets "$lines"\n',
    )
    assert result.returncode == 0, result.stderr
    for name in REQUIRED_SECRETS:
        assert f"[ok]      {name}" in result.stdout
    assert "[MISSING] git-push-identity" in result.stdout


def test_refuses_to_advertise_ready_without_the_github_token(tmp_path: Path) -> None:
    obtained = ",".join(s for s in REQUIRED_SECRETS if s != "github-token")
    result = _source(
        PROVISION_SERVER,
        f'dr_advertise_ready "{obtained}" ok ok && echo READY-CLAIMED || echo REFUSED\n',
    )
    assert "REFUSED" in result.stdout
    assert "READY-CLAIMED" not in result.stdout
    assert "github-token" in result.stderr


def test_refuses_to_advertise_ready_without_the_restic_credentials(tmp_path: Path) -> None:
    """A restore that reproduces the database is worth nothing if restic could
    never have read the repository in the first place."""
    for missing in ("restic-password", "backup-repository", "azure-account-key"):
        obtained = ",".join(s for s in REQUIRED_SECRETS if s != missing)
        result = _source(
            PROVISION_SERVER,
            f'dr_advertise_ready "{obtained}" ok ok && echo READY-CLAIMED || echo REFUSED\n',
        )
        assert "REFUSED" in result.stdout, missing
        assert "READY-CLAIMED" not in result.stdout, missing
        assert missing in result.stderr


def test_refuses_to_advertise_ready_when_the_github_token_cannot_merge() -> None:
    """Pin 4 of #3117: a board that serves /board and cannot merge a PR is not a
    recovered fleet. The verdict comes from `coord dr promote`'s own probe, not
    from the token file existing."""
    obtained = ",".join(REQUIRED_SECRETS)
    result = _source(
        PROVISION_SERVER,
        f'dr_advertise_ready "{obtained}" incapable ok && echo READY-CLAIMED || echo REFUSED\n',
    )
    assert "REFUSED" in result.stdout
    assert "cannot merge" in result.stderr


def test_an_unprobed_github_token_is_a_blocker_not_a_pass() -> None:
    """#2096: a missing verdict defaulting to the permissive branch is exactly
    the unreachable-failure shape. `unknown` blocks."""
    obtained = ",".join(REQUIRED_SECRETS)
    for verdict in ("", "unknown"):
        result = _source(
            PROVISION_SERVER,
            f'dr_advertise_ready "{obtained}" "{verdict}" ok && echo READY-CLAIMED || echo REFUSED\n',
        )
        assert "REFUSED" in result.stdout, verdict
        assert "never established" in result.stderr, verdict


def test_ready_requires_a_board_that_actually_answered() -> None:
    """The ready verdict is derived from `coord dr promote`'s record, which sets
    outcome=ok only after a GET /board against the started daemon — never from
    the absence of an exception."""
    obtained = ",".join(REQUIRED_SECRETS)
    result = _source(
        PROVISION_SERVER,
        f'dr_advertise_ready "{obtained}" ok failed && echo READY-CLAIMED || echo REFUSED\n',
    )
    assert "REFUSED" in result.stdout
    assert "did not report a serving board" in result.stderr


def test_ready_is_reachable_when_everything_is_in_place() -> None:
    """The passing verdict must be reachable too, or the gate above proves
    nothing about the gate and only about the refusal."""
    obtained = ",".join(REQUIRED_SECRETS)
    result = _source(
        PROVISION_SERVER,
        f'dr_advertise_ready "{obtained}" ok ok && echo READY-CLAIMED || echo REFUSED\n',
    )
    assert result.returncode == 0, result.stderr
    assert "READY-CLAIMED" in result.stdout
    assert "REFUSED" not in result.stdout


def test_the_ready_gate_reuses_coord_dr_promote_rather_than_reimplementing_it() -> None:
    """One question, one answer (#2085): the restore, the unit ordering, the
    credential probes and the board verification all live in `coord dr promote`
    (rung D3). provision-server.sh must call it, not re-derive any of them."""
    text = PROVISION_SERVER.read_text()
    assert "dr promote" in text
    assert '.credentials["github-token"]' in text
    # No second implementation of the restore or the unit set.
    assert "restic restore" not in text
    assert "systemctl start coord-serve" not in text


# ===========================================================================
# 4. No secret in argv, in a log line, or in an image artifact
# ===========================================================================


def _az_bash_stub(missing: list[str] | None = None) -> str:
    """A bash `az` stand-in for dr_fetch_secrets' one call shape."""
    missing_csv = " ".join(missing or [])
    return f"""
az() {{
    local name="" prev=""
    for a in "$@"; do
        [[ "$prev" == "--name" ]] && name="$a"
        prev="$a"
    done
    for m in {missing_csv or '""'}; do
        [[ "$m" == "$name" ]] && return 1
    done
    printf '%s\\n' "{SECRET_SENTINEL}-$name"
}}
"""


def test_no_secret_value_reaches_stdout_stderr_or_the_report(tmp_path: Path) -> None:
    out = tmp_path / "secrets.env"
    result = _source(
        PROVISION_SERVER,
        _az_bash_stub()
        + f'lines="$(dr_fetch_secrets kv-test {out})"\n'
        'dr_report_secrets "$lines"\n'
        'dr_obtained_csv "$lines"\n',
    )
    assert result.returncode == 0, result.stderr
    assert SECRET_SENTINEL not in result.stdout
    assert SECRET_SENTINEL not in result.stderr
    # ...but it did actually get fetched, or this test would pass vacuously.
    assert SECRET_SENTINEL in out.read_text()


def test_fetched_secrets_land_0600_on_tmpfs_so_they_cannot_reach_an_image(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "secrets.env"
    result = _source(PROVISION_SERVER, _az_bash_stub() + f'dr_fetch_secrets kv-test {out} >/dev/null')
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert stat.S_IMODE(out.parent.stat().st_mode) == 0o700
    # The default target is /run: tmpfs, so nothing written there survives into
    # a disk image, a snapshot or a backup.
    assert 'DR_SECRETS_FILE_DEFAULT="/run/' in PROVISION_SERVER.read_text()


def _code_lines(script: Path) -> str:
    """The script with whole-line `#` comments removed — the comments talk
    *about* the anti-patterns below, so a naive substring check on the raw text
    would fail on its own documentation."""
    return "\n".join(
        line for line in script.read_text().splitlines() if not line.lstrip().startswith("#")
    )


def test_no_script_passes_a_secret_value_as_an_argv_element() -> None:
    """`az keyvault secret set --value "$v"` and friends put the value in
    /proc/<pid>/cmdline, readable by any local user. bootstrap-shared.sh prompts
    for secrets precisely to avoid that; the server role must not reintroduce
    it."""
    for script in (DR_UP, DR_DOWN, PROVISION_SERVER):
        code = _code_lines(script)
        assert "--value" not in code, f"{script.name} passes a value on a command line"
        assert "set -x" not in code, f"{script.name} would echo every expanded argv"


def test_only_the_boot_path_ever_reads_a_secret_value() -> None:
    """dr-up.sh and dr-down.sh run on the operator's laptop and have no reason
    to hold a credential at all; only the boot path, on the DR VM itself, reads
    values — and it writes them straight to a 0600 file."""
    assert "--query value" in _code_lines(PROVISION_SERVER)
    for script in (DR_UP, DR_DOWN):
        assert "--query value" not in _code_lines(script)
        assert "keyvault secret show" not in _code_lines(script)


def test_the_vault_gate_never_asks_for_a_secret_value(lane: dict) -> None:
    """dr-up.sh checks the vault is complete by listing names. If it read values
    they would be in this process, and one `echo` from a log line."""
    result = _run_dr_up(lane, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _az_calls(lane)
    assert any("keyvault secret list" in c for c in calls)
    assert not [c for c in calls if "keyvault secret show" in c]
    assert SECRET_SENTINEL not in result.stdout + result.stderr


# ===========================================================================
# 5. dr-down.sh drains before deleting
# ===========================================================================


def _run_dr_down(lane: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(DR_DOWN), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=lane["env"],
        cwd=str(LANE),
    )


def test_dr_down_refuses_to_delete_while_work_is_still_running(lane: dict) -> None:
    """The drain is the whole point of the ordering: `az group delete` under a
    running host loses anything unpushed."""
    lane["env"]["STUB_HEALTH_BODY"] = json.dumps({"machine": "coord-dr", "active": 2})
    result = _run_dr_down(lane, "--machine", "coord-dr", "--drain-timeout", "0")
    assert result.returncode != 0, result.stdout
    assert "still 2 assignment(s) running" in result.stderr
    assert not [c for c in _az_calls(lane) if "group delete" in c], "deleted despite active work"


def test_dr_down_deletes_once_the_agent_reports_idle(lane: dict) -> None:
    lane["env"]["STUB_HEALTH_BODY"] = json.dumps({"machine": "coord-dr", "active": 0})
    result = _run_dr_down(lane, "--machine", "coord-dr")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "idle (active=0)" in result.stdout
    assert any("group delete" in c for c in _az_calls(lane))


def test_dr_down_refuses_when_a_live_interactive_session_is_on_the_machine(lane: dict) -> None:
    """Interactive tmux sessions are invisible to /health's assignment count, so
    a merge agent someone is driving by hand would otherwise be killed
    silently."""
    lane["env"]["STUB_HEALTH_BODY"] = json.dumps({"machine": "coord-dr", "active": 0})
    lane["env"]["STUB_SESSIONS_JSON"] = json.dumps(
        {"sessions": [{"machine": "coord-dr", "session_name": "merge-3130"}]}
    )
    result = _run_dr_down(lane, "--machine", "coord-dr")
    assert result.returncode != 0, result.stdout
    assert "merge-3130" in result.stderr
    assert not [c for c in _az_calls(lane) if "group delete" in c]


def test_dr_down_force_skips_the_drain_and_the_session_check(lane: dict) -> None:
    lane["env"]["STUB_HEALTH_BODY"] = json.dumps({"machine": "coord-dr", "active": 5})
    lane["env"]["STUB_SESSIONS_JSON"] = json.dumps(
        {"sessions": [{"machine": "coord-dr", "session_name": "merge-3130"}]}
    )
    result = _run_dr_down(lane, "--machine", "coord-dr", "--force")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "will be LOST" in result.stdout
    assert any("group delete" in c for c in _az_calls(lane))


def test_dr_down_pauses_before_it_drains(lane: dict) -> None:
    """`coord pause` explicitly does not cancel in-flight assignments, which is
    what makes it the right first step — but it has to come first, or new work
    lands on a machine that is already draining."""
    lane["env"]["STUB_HEALTH_BODY"] = json.dumps({"machine": "coord-dr", "active": 0})
    result = _run_dr_down(lane, "--machine", "coord-dr")
    assert result.returncode == 0, result.stdout + result.stderr
    ssh_log = Path(lane["env"]["SSH_LOG"]).read_text()
    assert "coord-dr" in ssh_log
    assert result.stdout.index("stop routing new work") < result.stdout.index("drain in-flight work")


def test_dr_down_confirms_deletion_started_rather_than_assuming_it(lane: dict) -> None:
    """#2096: `az group delete --no-wait` returning 0 only proves the request was
    accepted. The line an operator remembers must be an observation."""
    lane["env"]["STUB_HEALTH_BODY"] = json.dumps({"machine": "coord-dr", "active": 0})
    result = _run_dr_down(lane, "--machine", "coord-dr")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "deletion is under way" in result.stdout
    calls = _az_calls(lane)
    delete_at = next(i for i, c in enumerate(calls) if "group delete" in c)
    assert any("group show" in c for c in calls[delete_at + 1 :]), (
        "no state re-read after the delete request"
    )


def test_dr_down_fails_loudly_when_the_group_is_not_deleting_afterwards(lane: dict) -> None:
    """And the failing branch is reachable: a group that still reports
    `Succeeded` after the delete request must not print a reassuring line."""
    lane["env"]["STUB_HEALTH_BODY"] = json.dumps({"machine": "coord-dr", "active": 0})
    lane["env"]["STUB_DELETE_NOOP"] = "1"
    result = _run_dr_down(lane, "--machine", "coord-dr")
    assert result.returncode != 0, result.stdout
    assert "still reports provisioningState='Succeeded'" in result.stderr
    assert "deletion is under way" not in result.stdout


def test_dr_down_on_an_absent_group_is_a_no_op(lane: dict) -> None:
    lane["env"]["STUB_GROUP_STATE"] = ""
    result = _run_dr_down(lane, "--machine", "coord-dr")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing to do" in result.stdout
    assert not [c for c in _az_calls(lane) if "group delete" in c]


# ===========================================================================
# 6. The tailnet ACL
# ===========================================================================


def _load_hujson(path: Path) -> dict:
    """Parse HuJSON (JSON + `//` comments + trailing commas) without a dep.

    Scans character by character so a `//` inside a string literal is not
    mistaken for a comment.
    """
    text = path.read_text()
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\":
                if i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    stripped = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    return json.loads(stripped)


@pytest.fixture(scope="module")
def acl() -> dict:
    return _load_hujson(ACL)


def test_acl_is_parseable_after_the_edit(acl: dict) -> None:
    """The file is applied verbatim to the tailnet; a syntax error there is an
    outage, not a lint failure."""
    assert set(acl) >= {"tagOwners", "acls", "tests"}


def test_acl_declares_the_server_tag(acl: dict) -> None:
    assert "tag:coord-server" in acl["tagOwners"]
    assert acl["tagOwners"]["tag:coord-server"] == ["autogroup:admin"]


def test_acl_grants_operators_the_server_ports(acl: dict) -> None:
    rules = [
        r
        for r in acl["acls"]
        if r.get("src") == ["autogroup:member"]
        and any(d.startswith("tag:coord-server:") for d in r.get("dst", []))
    ]
    assert rules, "no operator -> tag:coord-server rule"
    dst = rules[0]["dst"][0]
    # rsplit: the destination itself contains a colon ("tag:coord-server").
    ports = dst.rsplit(":", 1)[1].split(",")
    assert set(ports) == {"22", "7433", "7434", "7435"}


def test_acl_lets_a_worker_reach_the_server_board_and_only_that(acl: dict) -> None:
    rules = [
        r
        for r in acl["acls"]
        if r.get("src") == ["tag:coord-worker"]
        and any(d.startswith("tag:coord-server") for d in r.get("dst", []))
    ]
    assert rules, "no tag:coord-worker -> tag:coord-server rule"
    for rule in rules:
        for dst in rule["dst"]:
            if dst.startswith("tag:coord-server"):
                assert dst == "tag:coord-server:7435", f"worker granted more than 7435: {dst}"


def test_acl_lets_the_server_dial_agents_because_the_daemon_calls_out(acl: dict) -> None:
    """The daemon calls OUT to each agent on 7433; the agent never calls back.
    Without this rule the DR board serves /board and dispatches nothing."""
    rules = [r for r in acl["acls"] if r.get("src") == ["tag:coord-server"]]
    assert rules, "no tag:coord-server egress rule — the board could not dispatch"
    dsts = {d for r in rules for d in r["dst"]}
    assert "tag:coord-worker:7433" in dsts
    assert "autogroup:member:7433" in dsts
    for dst in dsts:
        assert dst.endswith(":7433"), f"server egress is wider than 7433: {dst}"


def test_acl_did_not_widen_the_worker_tag(acl: dict) -> None:
    """Out of scope for #3130 and the containment rule the worker lane exists
    for: adding the server role must not have given workers anything new."""
    worker_dsts = {
        d
        for r in acl["acls"]
        if r.get("src") == ["tag:coord-worker"]
        for d in r["dst"]
    }
    assert worker_dsts == {"dellserver:7435", "tag:coord-server:7435"}


def test_acl_tests_assert_a_worker_can_reach_the_server_board(acl: dict) -> None:
    accepts = [
        t
        for t in acl["tests"]
        if t.get("src") == "tag:coord-worker" and "tag:coord-server:7435" in t.get("accept", [])
    ]
    assert accepts, "no ACL test asserting worker -> tag:coord-server:7435"


def test_acl_tests_assert_a_worker_still_cannot_reach_an_operator_machine(acl: dict) -> None:
    denies = {d for t in acl["tests"] if t.get("src") == "tag:coord-worker" for d in t.get("deny", [])}
    # precision and elitebook, by IP, on a concrete port (ACL tests reject "*").
    assert "100.116.209.7:22" in denies
    assert any(d.startswith("100.85.221.28:") for d in denies)
    assert "tag:coord-server:22" in denies


def test_acl_tests_assert_the_server_is_contained_too(acl: dict) -> None:
    """A compromised DR server must be contained the same way a compromised
    worker is: it dials agents, it does not SSH into your machines."""
    accepts = {d for t in acl["tests"] if t.get("src") == "tag:coord-server" for d in t.get("accept", [])}
    denies = {d for t in acl["tests"] if t.get("src") == "tag:coord-server" for d in t.get("deny", [])}
    assert "tag:coord-worker:7433" in accepts
    assert "100.116.209.7:22" in denies
    assert "tag:coord-worker:22" in denies


def test_acl_kept_the_operator_ssh_path_that_stops_a_lockout(acl: dict) -> None:
    """elitebook -> precision. If a future edit would break it, the tailnet
    rejects the save rather than cutting the live session."""
    live = [
        t
        for t in acl["tests"]
        if t.get("src") == "100.85.221.28" and "100.116.209.7:22" in t.get("accept", [])
    ]
    assert live, "the operator SSH lockout guard was dropped"


# ===========================================================================
# 7. preflight.sh --role server
# ===========================================================================


def _run_preflight(lane: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PREFLIGHT), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=lane["env"],
        cwd=str(LANE),
    )


def test_preflight_rejects_an_unknown_role(lane: dict) -> None:
    result = _run_preflight(lane, "--role", "banana")
    assert result.returncode == 2
    assert "unknown --role" in result.stderr


def test_preflight_server_role_runs_the_same_gates_dr_up_does(lane: dict) -> None:
    """One question, one answer (#2085): a preflight that says "clear" and a
    dry-run that then refuses would be two implementations of the same gate.
    preflight sources dr-up.sh and calls its functions."""
    text = PREFLIGHT.read_text()
    assert 'source "$HERE/dr-up.sh"' in text
    for fn in ("check_server_module", "check_acl_tag", "check_vault_secrets", "check_quota",
               "check_hostname_collision"):
        assert fn in text, f"preflight does not call dr-up.sh's {fn}"


def test_preflight_server_role_reports_the_module_gate(lane: dict) -> None:
    (lane["easy_azure"] / "modules" / "coord-server-vm" / "main.bicep").unlink()
    result = _run_preflight(lane, "--role", "server", "--vault", "kv-coord-test")
    assert "I. DR board server role" in result.stdout
    assert "easy-azure server module" in result.stdout
    assert result.returncode == 1


def test_preflight_worker_role_does_not_run_the_server_section(lane: dict) -> None:
    """Out of scope for #3130: the worker lane's behaviour is unchanged."""
    result = _run_preflight(lane, "--vault", "kv-coord-test")
    assert "I. DR board server role" not in result.stdout
    assert "D. Compute quota" in result.stdout


def test_preflight_server_role_does_not_fail_on_a_dead_daemon_host(lane: dict) -> None:
    """The premise of --role server is that dellserver is gone. Failing on its
    absence would make the DR preflight unusable in the one scenario it is
    for."""
    result = _run_preflight(lane, "--role", "server", "--vault", "kv-coord-test")
    assert "H. Daemon host" in result.stdout
    assert "presumes" in result.stdout
    assert "cannot ssh to dellserver" not in result.stdout


# ===========================================================================
# 8. Cross-repo boundary
# ===========================================================================


def test_the_easy_azure_half_is_named_and_not_attempted() -> None:
    """#1777's precedent: the opencode-api-key wiring sat half-done for months
    because the two halves were never scoped separately. The server Bicep module
    is easy-azure's; this repo must name it and fail clearly, not vendor it."""
    text = DR_UP.read_text()
    assert "modules/coord-server-vm/main.bicep" in text
    assert "#1777" in text
    assert not list(LANE.glob("*.bicep")), "a Bicep module was vendored into this repo"
