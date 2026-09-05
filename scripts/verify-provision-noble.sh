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
# question "do the package names in this script actually resolve on the OS it
# targets?" — a stubbed `apt-get` says yes to everything.
#
# This harness answers that second question WITHOUT a VM, hypervisor, docker,
# or root: it fetches the official Ubuntu 24.04 `ubuntu-base` root filesystem,
# unpacks it inside an unprivileged user namespace, and runs the script's real
# install surfaces against the real Ubuntu archive, the real github-cli apt
# source and real PyPI. Nothing is stubbed.
#
# It earns its keep: the first run of it found that `phase_toolchains`' browser
# capability was a silent false green on noble — `apt-get install
# chromium-browser` exits 0 and puts a stub on PATH that can never launch,
# while `chromium` has no candidate at all. See browser_works() in
# provision-machine.sh.
#
# WHAT IT DELIBERATELY CANNOT COVER
# ---------------------------------
# A chroot in a user namespace is not a machine. Out of reach here, and still
# needing the throwaway-VM run the issue asks for:
#
#   * systemd  — no PID 1, so `systemctl --user`, the coord-agent unit, the ten
#                daemon units, linger and `is-enabled` are all untestable.
#   * identity — `tailscale up`, `gh auth login` and the claude OAuth flow all
#                need a human and a browser.
#   * the live seams — a real coord-agent answering /health, a real coord serve
#                answering /board, a real dispatch landing.
#   * snap     — snapd needs systemd, so the browser INSTALL path is unverified
#                here even though the false-green it replaced is proven.
#
# Anything failing here fails on a real box too; passing here is necessary, not
# sufficient. The trailer is machine-readable on purpose (#2096: a report that
# only proves the request was issued is not a result).
#
# USAGE
#   scripts/verify-provision-noble.sh [--keep] [--rootfs DIR]
#
set -uo pipefail

REL="24.04"
POINT="${COORD_NOBLE_POINT:-24.04.4}"
ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
TARBALL_URL="https://cdimage.ubuntu.com/ubuntu-base/releases/${REL}/release/ubuntu-base-${POINT}-base-${ARCH}.tar.gz"
CACHE="${COORD_NOBLE_CACHE:-${TMPDIR:-/tmp}/ubuntu-base-${POINT}-${ARCH}.tar.gz}"
ROOTFS="${TMPDIR:-/tmp}/noble-verify-$$"
KEEP=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --keep)   KEEP=1; shift ;;
        --rootfs) [[ $# -ge 2 ]] || { echo "--rootfs needs a value" >&2; exit 2; }
                  ROOTFS="$2"; shift 2 ;;
        -h|--help) sed -n '2,50p' "$0"; exit 0 ;;
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

# The package/probe inventory is DERIVED from provision-machine.sh, not
# retyped, so a package rename there cannot silently stop being verified here.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROVISION="$SCRIPT_DIR/provision-machine.sh"
[[ -f "$PROVISION" ]] || fail "cannot find $PROVISION"
GH_MIN="$(sed -n 's/^GH_MIN_VERSION="\([0-9.]*\)".*/\1/p' "$PROVISION" | head -1)"
[[ -n "$GH_MIN" ]] || fail "could not read GH_MIN_VERSION out of $PROVISION"
mapfile -t BASE_ENTRIES < <(
    sed -n '/^BASE_REQUIREMENTS=(/,/^)/p' "$PROVISION" | sed -n 's/^ *"\(.*\)"$/\1/p'
)
[[ ${#BASE_ENTRIES[@]} -gt 0 ]] || fail "could not read BASE_REQUIREMENTS out of $PROVISION"

if [[ ! -s "$CACHE" ]]; then
    echo "fetching $TARBALL_URL"
    curl -fsSL -o "$CACHE" "$TARBALL_URL" || fail "download failed: $TARBALL_URL"
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
    printf 'BASE_ENTRIES=('
    printf '%q ' "${BASE_ENTRIES[@]}"
    printf ')\n'
} > "$ROOTFS/root/inventory.sh"

cat > "$ROOTFS/root/run.sh" <<'INNER'
#!/bin/bash
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
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

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  PASS  %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$*"; }
note() { printf '  note  %s\n' "$*"; }

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
if /root/.coord-venv/bin/pip install -q code-coordinator >/dev/null 2>&1; then
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

printf '\nNOBLE_VERIFY: ok=%s pass=%d fail=%d rootfs=%s\n' \
    "$([[ $FAIL -eq 0 ]] && echo true || echo false)" "$PASS" "$FAIL" "$VERSION_ID"
[[ $FAIL -eq 0 ]]
INNER

out=0
unshare -r --mount --pid --fork --mount-proc="$ROOTFS/proc" bash -c '
    mount --bind /dev "'"$ROOTFS"'/dev" 2>/dev/null
    "'"$CHROOT"'" "'"$ROOTFS"'" /bin/bash /root/run.sh
' || out=$?

[[ $KEEP -eq 1 ]] || rm -rf "$ROOTFS" 2>/dev/null || true
exit $out
