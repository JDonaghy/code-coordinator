#!/usr/bin/env bash
# Real-surface verification for scripts/provision-machine.sh (#3138).
#
# WHY THIS EXISTS
# ---------------
# tests/test_provision_machine.py drives the real script end to end, but it
# does so against a directory of stub binaries: `apt-get`, `sudo`, `snap`,
# `curl`, `gh` and friends are all one-liners that exit 0. That is the right
# shape for the behavioural invariants #3138 names (phase ordering, idempotency,
# credential hygiene, the gate's ability to fail) and the wrong shape for the
# question "does this actually work on the OS it targets?" — a stubbed
# `apt-get` says yes to everything and a stubbed `curl` invents a /health.
#
# This harness answers that second question WITHOUT a VM, hypervisor, docker,
# or root: it fetches the official Ubuntu 24.04 `ubuntu-base` root filesystem,
# verifies it against Ubuntu's published SHA256SUMS, unpacks it inside an
# unprivileged user namespace, and runs three tiers against it. Nothing is
# stubbed in any of them.
#
#   TIER 1  install surfaces (shared network)
#           Every BASE_REQUIREMENTS package, the github-cli apt source and its
#           version floor, restic, libgtk-4-dev, the browser packaging, and a
#           real `pip install code-coordinator` from real PyPI into a real
#           venv. Package names and the gh floor are PARSED OUT of
#           provision-machine.sh, never retyped, so they cannot drift.
#
#   TIER 2  the LIVE seams (private network namespace)
#           Re-enters the same rootfs with `unshare --net`, so 127.0.0.1 is
#           this rootfs's own loopback and the REAL default ports are free.
#           Starts the PyPI-installed `coord agent` on 7433 and `coord serve`
#           on 7435 as actual processes and drives them over actual HTTP:
#           GET /health, GET /healthz, GET /board. Then runs `coord status`
#           and `coord machine doctor` against that live pair and checks the
#           doctor's own machine-readable trailer — including that its `agent`
#           layer grades the live /health it just talked to. No systemd is
#           needed for any of this: the units only *supervise* these processes.
#           The private netns is not a detail — it means this tier can never
#           read (or disturb) the fleet agent on the host running the harness.
#
#   TIER 3  the daemon units, statically, under noble's OWN systemd
#           Installs `systemd` into the rootfs (for systemd-analyze only, not
#           to run it), renders every unit in `deploy_manifest.ROLE_UNITS`
#           through the same `render_unit()` call `phase_daemon_units` uses,
#           and runs `systemd-analyze verify` on each. That is what catches a
#           bad directive, a broken `[Install]` section, or an ExecStart that
#           does not resolve — the failures that otherwise only appear as a
#           unit sitting in `failed` after a rebuild.
#
# WHAT IT STILL DELIBERATELY CANNOT COVER
# ---------------------------------------
# A chroot in a user namespace is not a machine. Out of reach here, and still
# needing the throwaway-VM run the issue asks for:
#
#   * systemd as PID 1 — the units are parsed and their ExecStarts resolved,
#                but `systemctl --user enable --now`, linger, and the
#                is-enabled re-query in phase_daemon_units need a real
#                session bus and a real init.
#   * identity — `tailscale up`, `gh auth login` and the claude OAuth flow all
#                need a human and a browser.
#   * snap     — snapd needs systemd, so the browser INSTALL path is unverified
#                here even though the false-green it replaced is proven.
#   * a real dispatch landing on the agent (needs a reachable coordinator and
#                a real Claude subscription).
#
# Anything failing here fails on a real box too. Passing here is
# "necessary, not sufficient" — three tiers of real coverage make it MORE
# tempting to read a green here as the throwaway-VM run, not less. The trailer
# is machine-readable on purpose (#2096: a report that only proves the request
# was issued is not a result).
#
# USAGE
#   scripts/verify-provision-noble.sh [--keep] [--rootfs DIR] [--skip-live]
#                                     [--skip-units] [--no-checksum]
#
set -uo pipefail

REL="24.04"
POINT="${COORD_NOBLE_POINT:-24.04.4}"
ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
BASE_URL="https://cdimage.ubuntu.com/ubuntu-base/releases/${REL}/release"
TARBALL="ubuntu-base-${POINT}-base-${ARCH}.tar.gz"
TARBALL_URL="${BASE_URL}/${TARBALL}"
SUMS_URL="${BASE_URL}/SHA256SUMS"
CACHE="${COORD_NOBLE_CACHE:-${TMPDIR:-/tmp}/${TARBALL}}"
ROOTFS="${TMPDIR:-/tmp}/noble-verify-$$"
KEEP=0
DO_LIVE=1
DO_UNITS=1
CHECKSUM=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --keep)   KEEP=1; shift ;;
        --rootfs) [[ $# -ge 2 ]] || { echo "--rootfs needs a value" >&2; exit 2; }
                  ROOTFS="$2"; shift 2 ;;
        --skip-live)   DO_LIVE=0; shift ;;
        --skip-units)  DO_UNITS=0; shift ;;
        --no-checksum) CHECKSUM=0; shift ;;
        -h|--help) sed -n '2,78p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

fail() { echo "verify-provision-noble: $*" >&2; echo "NOBLE_VERIFY: ok=false reason=harness"; exit 1; }

command -v unshare >/dev/null 2>&1 || fail "no unshare(1) — install util-linux"
unshare -r true >/dev/null 2>&1 \
    || fail "unprivileged user namespaces are disabled on this host; run this
where 'unshare -r true' works, or do the throwaway-VM run instead"
CHROOT="$(command -v chroot || echo /usr/sbin/chroot)"
[[ -x "$CHROOT" ]] || fail "no chroot(8) binary"

# The package/probe inventory is DERIVED, not retyped, so a package rename
# cannot silently stop being verified here. Since #3139 it lives in the SHARED
# provisioning core, which both lanes source — so this verifies what the image
# lane installs too, not only what the bare-metal lane probes. Sourced rather
# than sed'd: the core is a pure declaration block plus functions, so reading
# it the way its callers do is the real value, not a guess at it.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROVISION="$SCRIPT_DIR/provision-machine.sh"
[[ -f "$PROVISION" ]] || fail "cannot find $PROVISION"
CORE="$SCRIPT_DIR/lib/provision-core.sh"
[[ -f "$CORE" ]] || fail "cannot find the shared provisioning core at $CORE"
# shellcheck source=lib/provision-core.sh
. "$CORE"
GH_MIN="$COORD_GH_MIN_VERSION"
[[ -n "$GH_MIN" ]] || fail "the shared core defines no COORD_GH_MIN_VERSION"
BASE_ENTRIES=("${COORD_BASE_REQUIREMENTS[@]}")
[[ ${#BASE_ENTRIES[@]} -gt 0 ]] || fail "the shared core defines no COORD_BASE_REQUIREMENTS"

if [[ ! -s "$CACHE" ]]; then
    echo "fetching $TARBALL_URL"
    curl -fsSL -o "$CACHE" "$TARBALL_URL" || fail "download failed: $TARBALL_URL"
fi

# #3138 review: verify the rootfs against Ubuntu's published SHA256SUMS before
# unpacking and running code out of it. The tarball is the trust root of every
# result below, so "it downloaded over https" is not enough — a stale/corrupt
# cache file (this thing is cached across runs, deliberately) would otherwise
# read as a mysterious package failure rather than as a bad download.
if [[ $CHECKSUM -eq 1 ]]; then
    sums="$(curl -fsSL "$SUMS_URL" 2>/dev/null || true)"
    want="$(printf '%s\n' "$sums" | sed -n "s/^\([0-9a-f]\{64\}\) \*\?${TARBALL}\$/\1/p" | head -1)"
    if [[ -z "$want" ]]; then
        fail "could not read a SHA256 for $TARBALL out of $SUMS_URL
(pass --no-checksum to run anyway, knowingly)"
    fi
    got="$(sha256sum "$CACHE" | cut -d' ' -f1)"
    if [[ "$got" != "$want" ]]; then
        rm -f "$CACHE"
        fail "SHA256 mismatch for $TARBALL
  published: $want
  on disk:   $got
The cached copy has been deleted; re-run to fetch it again."
    fi
    echo "rootfs sha256 verified against $SUMS_URL"
fi

mkdir -p "$ROOTFS" || fail "cannot create $ROOTFS"
# Extraction warnings about chown/chgrp are expected: a single-uid user
# namespace cannot map the utmp/adm/ssh gids some packages want. They are
# harness artifacts, never package-name problems, and every check below is
# written to be insensitive to them (it probes behaviour, not apt's exit code).
unshare -r tar -xzf "$CACHE" -C "$ROOTFS" 2>/dev/null
[[ -x "$ROOTFS/bin/bash" ]] || fail "rootfs did not extract"
cp /etc/resolv.conf "$ROOTFS/etc/resolv.conf" 2>/dev/null || true
mkdir -p "$ROOTFS/root"

{
    printf '%s\n' "GH_MIN=$GH_MIN"
    printf '%s\n' "DO_UNITS=$DO_UNITS"
    printf 'BASE_ENTRIES=('
    printf '%q ' "${BASE_ENTRIES[@]}"
    printf ')\n'
} > "$ROOTFS/root/inventory.sh"

# The shared PASS/FAIL bookkeeping both tiers use, sourced into each.
cat > "$ROOTFS/root/tally.sh" <<'TALLY'
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  PASS  %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$*"; }
note() { printf '  note  %s\n' "$*"; }
tally_out() { printf '%d %d\n' "$PASS" "$FAIL" > "$1"; }
TALLY

# ── Tier 1 + 3: install surfaces, then the units ─────────────────────────────

cat > "$ROOTFS/root/run.sh" <<'INNER'
#!/bin/bash
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
# chroot(8) also hands the HOST's HOME through, and systemd's `%h` specifier
# expands to exactly that — so without this every rendered unit's ExecStart
# points at a path OUTSIDE the rootfs and tier 3 grades the host's venv, not
# this one. Pin it to the rootfs's own root home.
export HOME=/root
# chroot(8) hands the HOST's PATH through, which on a dev box routinely lacks
# /usr/sbin — and dpkg refuses to configure ANYTHING when it cannot find
# ldconfig and start-stop-daemon, which then reads downstream as "restic does
# not exist on noble". A wrong PATH here manufactures fake failures, so pin it.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# apt drops privileges to _apt to fetch, which a single-uid namespace cannot do.
printf 'APT::Sandbox::User "root";\nAcquire::Retries "3";\n' > /etc/apt/apt.conf.d/99userns

# THE ONE ORDERING CONCESSION, AND WHY IT DOES NOT WEAKEN THE RESULT.
# A single-uid user namespace maps only gid 0, so libutempter0's postinst
# (dpkg-statoverride ... root utmp) fails with EINVAL, dpkg leaves tmux
# half-configured, and EVERY later apt-get aborts on the wedged queue —
# turning one namespace artifact into a cascade of fake "package does not
# exist" results. tmux is therefore installed LAST, alone, after every other
# surface has been checked. It is not skipped and not stubbed; only reordered.
NS_HOSTILE="tmux"

. /root/inventory.sh
. /root/tally.sh

. /etc/os-release
printf '### rootfs: %s (%s) %s\n\n' "$PRETTY_NAME" "$VERSION_CODENAME" "$(uname -m)"
[[ "$VERSION_ID" == "24.04" ]] && ok "the rootfs really is 24.04" || bad "not 24.04: $VERSION_ID"

printf '\n### apt-get update against the stock noble sources\n'
apt-get update -qq >/dev/null 2>&1 && ok "apt-get update" || bad "apt-get update"

probe_entry() {
    local probe="$1" pkg="$2"
    if [[ "$probe" == *" "* ]]; then
        # shellcheck disable=SC2086
        if $probe >/dev/null 2>&1; then ok "$probe"; else bad "$probe (pkg $pkg)"; fi
    else
        if command -v "$probe" >/dev/null 2>&1; then ok "$probe"; else bad "$probe (pkg $pkg)"; fi
    fi
}

printf '\n### BASE_REQUIREMENTS: every probe/package pair, as declared\n'
pkgs=(); deferred=()
for entry in "${BASE_ENTRIES[@]}"; do
    pkg="${entry##*|}"
    case " $NS_HOSTILE " in *" $pkg "*) list=deferred ;; *) list=pkgs ;; esac
    if [[ $list == deferred ]]; then
        case " ${deferred[*]-} " in *" $pkg "*) ;; *) deferred+=("$pkg") ;; esac
    else
        case " ${pkgs[*]-} " in *" $pkg "*) ;; *) pkgs+=("$pkg") ;; esac
    fi
done
note "packages: ${pkgs[*]}"
[[ ${#deferred[@]} -eq 0 ]] || note "deferred to the end (see NS_HOSTILE): ${deferred[*]}"
apt-get install -y -qq --no-install-recommends ca-certificates >/dev/null 2>&1
apt-get install -y -qq --no-install-recommends "${pkgs[@]}" >/dev/null 2>&1
for entry in "${BASE_ENTRIES[@]}"; do
    pkg="${entry##*|}"
    case " $NS_HOSTILE " in *" $pkg "*) continue ;; esac
    probe_entry "${entry%%|*}" "$pkg"
done
py="$(python3 --version 2>&1)"
if python3 --version 2>&1 | grep -qE '3\.(1[2-9]|[2-9][0-9])'; then
    ok "$py clears the 3.12 floor install-agent.sh enforces"
else
    bad "$py is below the 3.12 floor"
fi
# iproute2 is the HARNESS's own dependency, not provision-machine.sh's: tier 2
# re-enters this rootfs in a private network namespace, where loopback starts
# DOWN and `ip link set lo up` is what makes 127.0.0.1 answer at all.
apt-get install -y -qq --no-install-recommends iproute2 >/dev/null 2>&1
command -v ip >/dev/null 2>&1 \
    && note "iproute2 present (harness scaffolding for the tier-2 netns)" \
    || bad "iproute2 did not install — tier 2 will have no loopback"

printf '\n### gh from the official apt source (phase_cred_tools, verbatim)\n'
install -d -m 0755 /usr/share/keyrings
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg status=none
chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
printf 'deb [arch=%s signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main\n' \
    "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/github-cli.list
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq gh >/dev/null 2>&1
gh_ver="$(gh --version 2>/dev/null | sed -n 's/^gh version \([0-9.]*\).*/\1/p')"
if [[ -n "$gh_ver" ]] \
    && [[ "$(printf '%s\n%s\n' "$GH_MIN" "$gh_ver" | sort -V | head -1)" == "$GH_MIN" ]]; then
    ok "gh $gh_ver clears the $GH_MIN floor"
else
    bad "gh '${gh_ver:-<absent>}' does not clear the $GH_MIN floor"
fi

printf '\n### restic (server role, phase_credentials)\n'
apt-get install -y -qq restic >/dev/null 2>&1
v="$(restic version 2>/dev/null | head -1)"
[[ -n "$v" ]] && ok "restic -> $v" || bad "restic did not install"

printf '\n### libgtk-4-dev (capability: gtk, phase_toolchains)\n'
apt-get install -y -qq --no-install-recommends libgtk-4-dev >/dev/null 2>&1
if pkg-config --exists gtk4 2>/dev/null; then
    ok "pkg-config --exists gtk4 -> $(pkg-config --modversion gtk4)"
else
    bad "libgtk-4-dev did not make gtk4 visible to pkg-config"
fi

printf '\n### browser (capability: browser) — the #1678 false green, proven\n'
# Asserted on the PACKAGES rather than on an install, deliberately: snapd needs
# systemd, so the install path itself is out of a chroot's reach. What IS in
# reach is the fact that made the old code wrong, and it is checkable exactly.
cand="$(apt-cache policy chromium 2>/dev/null | sed -n 's/^ *Candidate: //p')"
if [[ "$cand" == "(none)" || -z "$cand" ]]; then
    ok "'chromium' has no candidate on noble (policy: ${cand:-<no such package>}) —"
    note "      so the old 'chromium-browser || chromium' fallback pair could never"
    note "      have produced a browser on the one OS this script targets"
else
    bad "'chromium' now HAS a candidate ($cand) — re-check browser_works()'s premise"
fi
mkdir -p /tmp/chromedeb && cd /tmp/chromedeb || exit 1
if apt-get download chromium-browser >/dev/null 2>&1 && dpkg-deb -x chromium-browser_*.deb x 2>/dev/null; then
    if grep -q 'requires the chromium snap to be installed' x/usr/bin/chromium-browser 2>/dev/null; then
        ok "the 'chromium-browser' deb's /usr/bin/chromium-browser is a stub that"
        note "      exits 1 unless /snap/bin/chromium exists, and its postinst installs"
        note "      no snap — so 'command -v chromium-browser' is a FALSE GREEN and"
        note "      browser_works() is right to ask for --version instead"
    else
        bad "the chromium-browser deb no longer looks like the snap stub — re-check"
    fi
else
    bad "could not fetch/unpack the chromium-browser deb to inspect it"
fi
cd / || exit 1

printf '\n### the coord venv: PyPI, never editable\n'
python3 -m venv /root/.coord-venv >/dev/null 2>&1
/root/.coord-venv/bin/pip install -q --upgrade pip >/dev/null 2>&1
# [server] is the extra the daemon host needs — `coord serve`'s uvicorn/starlette
# stack lives there, and tier 2 starts the real daemon, so install what a real
# --role server box installs rather than the bare CLI.
if /root/.coord-venv/bin/pip install -q 'code-coordinator[server]' >/dev/null 2>&1 \
   || /root/.coord-venv/bin/pip install -q code-coordinator >/dev/null 2>&1; then
    ok "pip install code-coordinator from real PyPI"
else
    bad "pip install code-coordinator failed"
fi
v="$(/root/.coord-venv/bin/coord version 2>/dev/null | head -1)"
[[ -n "$v" ]] && ok "the venv's coord runs -> $v" || bad "the venv produced no runnable coord"
if /root/.coord-venv/bin/pip list --editable --format=freeze 2>/dev/null | grep -q .; then
    bad "an editable install is present in the venv"
else
    ok "no editable install in the venv"
fi

printf '\n### the deferred namespace-hostile packages, installed last and alone\n'
if [[ ${#deferred[@]} -gt 0 ]]; then
    apt-get install -y -qq --no-install-recommends "${deferred[@]}" >/dev/null 2>&1
    note "apt rc=$? (non-zero here is the libutempter0 statoverride artifact)"
    for entry in "${BASE_ENTRIES[@]}"; do
        pkg="${entry##*|}"
        case " $NS_HOSTILE " in *" $pkg "*) probe_entry "${entry%%|*}" "$pkg" ;; esac
    done
fi

# ── Tier 3: the daemon units, parsed by noble's own systemd ──────────────────
# systemd goes in LAST for the same reason tmux does: it is a large install
# with postinsts a single-uid namespace cannot fully satisfy, and a wedged
# dpkg queue would turn every earlier surface into a fake failure. Nothing is
# STARTED here — `systemd-analyze verify` is a static parser, and it is the
# thing that would have caught #1928's literal `<MACHINE_NAME>` in a unit file.
if [[ "${DO_UNITS:-1}" == "1" ]]; then
printf '\n### the daemon units, rendered and parsed by noble systemd\n'
apt-get install -y -qq --no-install-recommends systemd >/dev/null 2>&1
if ! command -v systemd-analyze >/dev/null 2>&1; then
    bad "systemd-analyze did not install — the units were not parsed"
else
    note "systemd-analyze $(systemd-analyze --version 2>/dev/null | head -1)"
    mkdir -p /root/units /root/.local/bin
    # install-agent.sh creates ~/.local/bin/coord as a shim onto the venv
    # (#2936), and half the packaged units ExecStart it rather than the venv
    # path directly. Mirror that here or every one of them reads as a dead
    # ExecStart for a reason that has nothing to do with the units.
    ln -sf /root/.coord-venv/bin/coord /root/.local/bin/coord
    # Rendered through the SAME call phase_daemon_units uses, from the SAME
    # packaged unit dir, so what is parsed here is byte-for-byte what a real
    # --role server run would drop into ~/.config/systemd/user. The packaged
    # *.sh helpers are staged into ~/.local/bin for the same reason: that is
    # where the units' ExecStart= lines say they live.
    render_out="$(/root/.coord-venv/bin/python - /root/units noble-verify 7433 <<'PY' 2>&1
import sys
from pathlib import Path
try:
    from coord.deploy_manifest import ROLE_DAEMON, units_for_role
    from coord.deploy_units import render_unit
    from coord.health.checks.unit_drift import packaged_unit_dir
except Exception as exc:                     # pragma: no cover - version skew
    print(f"UNAVAILABLE|{exc}")
    raise SystemExit(0)
dest = Path(sys.argv[1]); machine, port = sys.argv[2], sys.argv[3]
ref = packaged_unit_dir()
if ref is None:
    print("UNAVAILABLE|this install ships no coord/deploy/")
    raise SystemExit(0)
dest.mkdir(parents=True, exist_ok=True)
wanted = list(units_for_role(ROLE_DAEMON))
# A `.timer` on its own has no ExecStart — the unit that does the work is the
# `.service` it fires, which ROLE_UNITS deliberately does not list (enabling
# the timer is what matters). Verify those too: a broken ExecStart in
# coord-notify.service is invisible from coord-notify.timer.
for unit in list(wanted):
    if unit.endswith(".timer"):
        companion = unit[: -len(".timer")] + ".service"
        if (ref / companion).exists() and companion not in wanted:
            wanted.append(companion)
for unit in wanted:
    src = ref / unit
    if not src.exists():
        print(f"MISSING|{unit}")
        continue
    text, note = render_unit(src.read_text(encoding="utf-8"), machine_name=machine, port=port)
    if text is None:
        print(f"UNRENDERABLE|{unit}: {note}")
        continue
    (dest / unit).write_text(text, encoding="utf-8")
    print(f"OK|{unit}")
helpers = Path("/root/.local/bin")
helpers.mkdir(parents=True, exist_ok=True)
for helper in sorted(ref.glob("*.sh")):
    target = helpers / helper.name
    target.write_text(helper.read_text(encoding="utf-8"))
    target.chmod(0o755)
    print(f"HELPER|{helper.name}")
PY
)"
    if printf '%s\n' "$render_out" | grep -q '^UNAVAILABLE|'; then
        bad "could not render the daemon units: $(printf '%s\n' "$render_out" | sed -n 's/^UNAVAILABLE|//p')"
    else
        rendered=0 packaged_helpers=""
        while IFS= read -r line; do
            [[ -n "$line" ]] || continue
            case "$line" in
                OK\|*)           rendered=$((rendered + 1)) ;;
                HELPER\|*)       packaged_helpers="$packaged_helpers ${line#*|}" ;;
                MISSING\|*)      bad "in ROLE_UNITS but not packaged: ${line#*|}" ;;
                UNRENDERABLE\|*) bad "unit did not render: ${line#*|}" ;;
            esac
        done <<< "$render_out"
        note "packaged deploy helpers staged into ~/.local/bin:${packaged_helpers:- <none>}"
        if [[ $rendered -eq 0 ]]; then
            bad "ROLE_UNITS rendered zero units — that is a broken install"
        else
            ok "$rendered daemon unit(s) rendered from the packaged deploy dir"
        fi
        # ExecStart resolution is checked explicitly: %h expands to /root here
        # and the venv really is at /root/.coord-venv, so this is a genuine
        # "would this unit have anything to run?" answer, not a tautology.
        missing_exec=0 checked_exec=0
        for f in /root/units/*.service; do
            [[ -e "$f" ]] || continue
            # A here-string, not `< <(...)`: process substitution needs
            # /dev/fd, which a chroot does not reliably have — and a loop that
            # silently reads nothing would "pass" this check vacuously.
            while IFS= read -r execline; do
                [[ -n "$execline" ]] || continue
                bin="${execline#ExecStart=}"; bin="${bin#[-+!@:]}"; bin="${bin%% *}"
                bin="${bin//%h/$HOME}"      # systemd's own specifier expansion
                [[ -n "$bin" ]] || continue
                checked_exec=$((checked_exec + 1))
                if [[ ! -x "$bin" ]]; then
                    case "$bin" in
                        *.sh)
                            # A helper the units reference but this coord
                            # release does not package (the repo's own
                            # deploy/ has several coord/deploy/ does not).
                            # This tier grades the RELEASE, from a rootfs with
                            # no checkout in it, so it is the honest picture
                            # of a wheel-only install. On a real rebuild
                            # provision-machine.sh stages this file from the
                            # deploy/ of the checkout it is run from, and only
                            # warns by name (DEADEXEC, counted in its final
                            # dead_exec= trailer) when even that is absent —
                            # a release-packaging gap, not an installer bug.
                            note "$(basename "$f"): ExecStart '$(basename "$bin")' is not"
                            note "        packaged in this coord release — provision-machine.sh"
                            note "        stages it from the checkout's deploy/, else DEADEXEC" ;;
                        *)
                            bad "$(basename "$f"): ExecStart '$bin' is not executable in the rootfs"
                            missing_exec=$((missing_exec + 1)) ;;
                    esac
                fi
            done <<< "$(grep '^ExecStart=' "$f" 2>/dev/null)"
        done
        if [[ $checked_exec -eq 0 ]]; then
            bad "no ExecStart= line was read from any rendered unit — the check
          did not run, which is not the same as passing"
        elif [[ $missing_exec -eq 0 ]]; then
            ok "all $checked_exec rendered ExecStart command(s) resolve to a real executable"
        fi
        # systemd-analyze verify writes diagnostics to stderr and exits
        # non-zero on a genuine parse/assignment error. Unknown-unit warnings
        # for targets a chroot has no unit tree for are expected and filtered.
        verify_bad=0
        for f in /root/units/*; do
            [[ -e "$f" ]] || continue
            case "$f" in *.sh) continue ;; esac
            vout="$(systemd-analyze verify --user "$f" 2>&1)"
            # "Failed to connect to system bus" is the chroot itself (no dbus,
            # no PID 1) and says nothing about the unit; unknown-unit warnings
            # for targets a chroot has no unit tree for are likewise expected.
            # Everything else — a bad directive, an unparsable value, a
            # missing ExecStart — is a real defect in the shipped unit.
            # "is not executable" is covered above, more precisely and with
            # the release-packaging distinction systemd cannot make.
            vout="$(printf '%s\n' "$vout" \
                | grep -v -e "Failed to connect to system bus" \
                        -e "is not executable" \
                        -e "Unit .* not found" \
                        -e "not found in the unit search path" \
                        -e "^$" || true)"
            if [[ -n "$vout" ]]; then
                bad "systemd-analyze verify $(basename "$f"):"
                printf '%s\n' "$vout" | sed 's/^/          /'
                verify_bad=$((verify_bad + 1))
            fi
        done
        [[ $verify_bad -eq 0 ]] \
            && ok "systemd-analyze verify --user is clean for every rendered unit"
    fi
fi
fi

tally_out /root/verdict.pkg
printf '\nTIER_1_3: pass=%d fail=%d rootfs=%s\n' "$PASS" "$FAIL" "$VERSION_ID"
[[ $FAIL -eq 0 ]]
INNER

# ── Tier 2: the live seams, in a private network namespace ───────────────────

cat > "$ROOTFS/root/run-live.sh" <<'LIVE'
#!/bin/bash
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root
. /root/tally.sh

COORD=/root/.coord-venv/bin/coord
CFG=/root/.coord/coordinator.yml
MACHINE=noble-verify

printf '### the live seams: real processes, real HTTP, private loopback\n'
if [[ ! -x "$COORD" ]]; then
    bad "no coord in the venv — tier 1 must pass before tier 2 can run"
    tally_out /root/verdict.live
    exit 1
fi

# A private netns starts with loopback DOWN, so 127.0.0.1 answers nothing
# until this runs. It is also why the REAL default ports (7433/7435) are
# free here even when the host running this harness is a live fleet member —
# and why nothing below can reach, or be confused by, that host's agent.
ip link set lo up 2>/dev/null || true
if ip -o addr show lo 2>/dev/null | grep -q '127\.0\.0\.1'; then
    ok "private loopback is up (this netns cannot see the host's fleet agent)"
else
    bad "loopback is down in the netns — nothing below can be trusted"
    tally_out /root/verdict.live
    exit 1
fi

mkdir -p /root/.coord
cat > "$CFG" <<YAML
repos:
  - name: code-coordinator
    github: JDonaghy/code-coordinator
machines:
  - name: $MACHINE
    host: 127.0.0.1
    capabilities: []
YAML
export COORD_CONFIG="$CFG"

wait_http() {   # url, seconds
    local url="$1" limit="$2" i=0
    while [[ $i -lt $limit ]]; do
        if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then return 0; fi
        i=$((i + 1)); sleep 1
    done
    return 1
}

json_keys() {   # reads stdin, prints sorted top-level keys or nothing
    python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
print(" ".join(sorted(d)) if isinstance(d, dict) else "<not-an-object>")' 2>/dev/null
}

printf '\n### coord agent, started for real, answering GET /health on :7433\n'
"$COORD" agent --config "$CFG" --machine "$MACHINE" --host 127.0.0.1 --port 7433 \
    > /root/agent.log 2>&1 &
AGENT_PID=$!
if wait_http http://127.0.0.1:7433/health 45; then
    body="$(curl -fsS --max-time 5 http://127.0.0.1:7433/health)"
    keys="$(printf '%s' "$body" | json_keys)"
    if [[ -n "$keys" ]]; then
        ok "GET /health -> 200, JSON object with keys: $keys"
    else
        bad "GET /health answered but the body is not a JSON object: ${body:0:200}"
    fi
else
    bad "coord agent never answered /health within 45s"
    note "      agent log tail:"
    tail -20 /root/agent.log 2>/dev/null | sed 's/^/          /'
fi

printf '\n### coord serve, started for real, answering GET /board on :7435\n'
"$COORD" serve --config "$CFG" --host 127.0.0.1 --port 7435 \
    > /root/serve.log 2>&1 &
SERVE_PID=$!
if wait_http http://127.0.0.1:7435/healthz 45; then
    ok "GET /healthz -> 200 (the daemon booted on a fresh ~/.coord/coord.db)"
    body="$(curl -fsS --max-time 10 http://127.0.0.1:7435/board)"
    keys="$(printf '%s' "$body" | json_keys)"
    if [[ -n "$keys" ]]; then
        ok "GET /board -> 200, JSON object with keys: $keys"
    else
        bad "GET /board did not return a JSON object: ${body:0:200}"
        tail -20 /root/serve.log 2>/dev/null | sed 's/^/          /'
    fi
else
    bad "coord serve never answered /healthz within 45s"
    note "      serve log tail:"
    tail -20 /root/serve.log 2>/dev/null | sed 's/^/          /'
fi

printf '\n### coord status against that live pair\n'
sout="$("$COORD" status --config "$CFG" 2>&1)"
if printf '%s\n' "$sout" | grep -q "$MACHINE"; then
    ok "coord status renders the board and names the machine"
    printf '%s\n' "$sout" | head -12 | sed 's/^/        | /'
else
    bad "coord status did not render the machine"
    printf '%s\n' "$sout" | head -20 | sed 's/^/          /'
fi

printf '\n### coord machine doctor, graded against the LIVE agent above\n'
dout="$("$COORD" machine doctor "$MACHINE" --config "$CFG" -v 2>&1)"
trailer="$(printf '%s\n' "$dout" | grep -E '^MACHINE_DOCTOR: ' | tail -1 || true)"
if [[ -n "$trailer" ]]; then
    ok "the doctor produced its machine-readable trailer: $trailer"
else
    bad "coord machine doctor produced no MACHINE_DOCTOR: trailer — phase_gate
          treats an absent trailer as a failure, and so does this"
    printf '%s\n' "$dout" | tail -25 | sed 's/^/          /'
fi
# The agent layer is the one the live /health above exists to grade. A chroot
# legitimately cannot make the WHOLE doctor clean (no tailscale, no gh auth,
# no ssh), so this asserts the one layer the tier can honestly earn: at least
# one agent check passed, and NONE of them is a CRIT. "Some agent line is a ✓"
# on its own would pass while sitting next to a ✗ two lines down.
agent_lines="$(printf '%s\n' "$dout" | grep -E '\[agent\.' || true)"
agent_ok="$(printf '%s\n' "$agent_lines" | grep -c '✓' || true)"
agent_crit="$(printf '%s\n' "$agent_lines" | grep -c 'CRIT' || true)"
if [[ -n "$agent_lines" && "$agent_ok" -gt 0 && "$agent_crit" -eq 0 ]]; then
    ok "every [agent.*] check ($agent_ok) grades OK against the live /health"
    printf '%s\n' "$agent_lines" | sed 's/^/        | /'
else
    bad "the doctor's agent layer did not cleanly pass against a live agent
          (ok=$agent_ok crit=$agent_crit)"
    printf '%s\n' "${agent_lines:-<no [agent.*] lines at all>}" | sed 's/^/          /'
fi
# The network layer is the other one a live listener earns: `host:` must reach
# THIS machine, which 127.0.0.1 in a private netns does, exactly.
if printf '%s\n' "$dout" | grep -E '\[network\.' | grep -q '✓'; then
    ok "the doctor's [network.*] layer confirms host: reaches this machine"
fi
note "the remaining layers (identity, toolchain, linger) read UNKNOWN/CRIT here"
note "by design: tailscale up, gh auth login and systemd are the VM's job."

kill "$AGENT_PID" "$SERVE_PID" 2>/dev/null || true
wait "$AGENT_PID" "$SERVE_PID" 2>/dev/null || true

tally_out /root/verdict.live
printf '\nTIER_2: pass=%d fail=%d\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
LIVE

enter() {   # extra unshare flags..., last arg = inner script path
    local script="${!#}"
    local flags=("${@:1:$#-1}")
    unshare -r --mount --pid --fork --mount-proc="$ROOTFS/proc" "${flags[@]}" bash -c '
        mount --bind /dev "'"$ROOTFS"'/dev" 2>/dev/null
        "'"$CHROOT"'" "'"$ROOTFS"'" /bin/bash '"$script"'
    '
}

out=0
enter /root/run.sh || out=$?

live=0
if [[ $DO_LIVE -eq 1 ]]; then
    printf '\n'
    enter --net /root/run-live.sh || live=$?
else
    printf '\n  note  tier 2 (live seams) skipped by --skip-live\n'
    printf '0 0\n' > "$ROOTFS/root/verdict.live"
fi

read -r p1 f1 < "$ROOTFS/root/verdict.pkg" 2>/dev/null || { p1=0; f1=1; }
read -r p2 f2 < "$ROOTFS/root/verdict.live" 2>/dev/null || { p2=0; f2=1; }
total_pass=$((p1 + p2)); total_fail=$((f1 + f2))

printf '\nNOBLE_VERIFY: ok=%s pass=%d fail=%d rootfs=%s tiers=%s\n' \
    "$([[ $total_fail -eq 0 && $out -eq 0 && $live -eq 0 ]] && echo true || echo false)" \
    "$total_pass" "$total_fail" "$REL" \
    "$([[ $DO_LIVE -eq 1 ]] && echo 1,2,3 || echo 1,3)"

[[ $KEEP -eq 1 ]] || rm -rf "$ROOTFS" 2>/dev/null || true
[[ $total_fail -eq 0 && $out -eq 0 && $live -eq 0 ]]
