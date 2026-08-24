"""#1800 acceptance: after a successful bake, epic-up.sh provisions from the
newly published image version with no manual edit.

This is the end-to-end claim the issue is about, spanning both scripts:
build-worker-image.sh publishes a version and must persist its sourceImageId
into $EPIC_ENV; epic-up.sh must then read that same file and deploy with it.
Neither script's full body can run for real under pytest (both drive real
`az`/`ssh` against a live Azure subscription), but both are structured so
that publishing the image version (build-worker-image.sh's `main()`, after
`az sig image-version create`) and consuming it (epic-up.sh's `main()`,
`source "$EPIC_ENV"`) are separated from the pure, sourceable function
bottom half:

  - build-worker-image.sh's update_epic_env() is exactly what main() calls
    right after publishing a version (see test_build_worker_image_env_update.py
    for that half in isolation).
  - epic-up.sh's main() does nothing to $SOURCE_IMAGE_ID beyond
    `source "$EPIC_ENV"` before using it -- there's no separate parsing
    step to fake, so sourcing $EPIC_ENV the same way main() does IS the
    real behaviour, not a mock of it.

So this test drives update_epic_env() for real against a scratch epic.env
(standing in for "a successful bake just happened"), then re-sources that
same file exactly as epic-up.sh's main() does, and asserts $SOURCE_IMAGE_ID
now equals the freshly published version -- with no edit in between.

Run against the pre-#1800 build-worker-image.sh (no update_epic_env
function at all), this fails outright: `update_epic_env: command not
found`. That is the bug -- a bake that completes while the next epic-up
keeps deploying the old image.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from .conftest import POSIX_BASH

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = REPO_ROOT / "scripts" / "azure-workers" / "build-worker-image.sh"
EPIC_UP_SCRIPT = REPO_ROOT / "scripts" / "azure-workers" / "epic-up.sh"

OLD_ID = (
    "/subscriptions/sub/resourceGroups/rg-coord-images/providers/"
    "Microsoft.Compute/galleries/sigcoord/images/coord-worker/"
    "versions/2026.0801.0"
)
NEW_ID = (
    "/subscriptions/sub/resourceGroups/rg-coord-images/providers/"
    "Microsoft.Compute/galleries/sigcoord/images/coord-worker/"
    "versions/2026.0804.0"
)


def test_bake_then_up_adopts_the_new_image_with_no_manual_edit(tmp_path: Path) -> None:
    epic_env = tmp_path / "epic.env"
    epic_env.write_text(f"SUBSCRIPTION_ID=abc123\nSOURCE_IMAGE_ID={OLD_ID}\n")

    # 1. "A successful bake just happened": build-worker-image.sh's main()
    #    would, at this point, have just run `az sig image-version create`
    #    and be about to call update_epic_env($VERSION, $IMAGE_ID) -- do
    #    exactly that, for real, against the scratch epic.env.
    bake = subprocess.run(
        [
            POSIX_BASH,
            "-c",
            f"""
set -euo pipefail
EPIC_ENV={shlex.quote(str(epic_env))}
UPDATE_ENV=1
source {shlex.quote(str(BUILD_SCRIPT))}
update_epic_env "2026.0804.0" {shlex.quote(NEW_ID)}
""",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert bake.returncode == 0, bake.stderr

    # 2. "The next epic-up.sh": read $SOURCE_IMAGE_ID out of $EPIC_ENV the
    #    exact way epic-up.sh's main() does -- `source "$EPIC_ENV"` -- with
    #    no edit in between.
    deploy = subprocess.run(
        [
            POSIX_BASH,
            "-c",
            f"""
set -euo pipefail
source {shlex.quote(str(epic_env))}
echo "$SOURCE_IMAGE_ID"
""",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert deploy.returncode == 0, deploy.stderr
    assert deploy.stdout.strip() == NEW_ID
    assert OLD_ID not in deploy.stdout


def test_no_update_env_bake_leaves_the_stale_pin_for_the_next_up(tmp_path: Path) -> None:
    """#1800 acceptance: --no-update-env preserves today's behaviour for a
    deliberate non-adoption -- the next epic-up.sh must still deploy the OLD
    image, unchanged, when the operator explicitly asked not to adopt."""
    epic_env = tmp_path / "epic.env"
    epic_env.write_text(f"SOURCE_IMAGE_ID={OLD_ID}\n")

    bake = subprocess.run(
        [
            POSIX_BASH,
            "-c",
            f"""
set -euo pipefail
EPIC_ENV={shlex.quote(str(epic_env))}
UPDATE_ENV=0
source {shlex.quote(str(BUILD_SCRIPT))}
update_epic_env "2026.0804.0" {shlex.quote(NEW_ID)}
""",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert bake.returncode == 0, bake.stderr

    deploy = subprocess.run(
        [
            POSIX_BASH,
            "-c",
            f"""
set -euo pipefail
source {shlex.quote(str(epic_env))}
echo "$SOURCE_IMAGE_ID"
""",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert deploy.returncode == 0, deploy.stderr
    assert deploy.stdout.strip() == OLD_ID
