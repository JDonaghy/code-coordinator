"""#3371 Part A: the fleet's long-lived, SUBSCRIPTION-backed `claude` credential.

Every headless leg this fleet dispatches (`coord.providers.claude`) runs as a
`claude -p` subprocess that authenticates with whatever credential the host
happens to carry. Until this module, that was *always* the interactive
`claude login` OAuth **session** in `~/.claude/.credentials.json` (macOS:
the login Keychain) — a credential that dies after some weeks with no
warning. #3371's evidence: precision's session expired, `coord status`
still read `online • idle`, and four review dispatches failed at turn 1 for
$0.00 before a human noticed.

`claude setup-token` mints the same *family* of credential with a longer
life. Its own help is explicit — "Set up a long-lived authentication token
(requires Claude subscription)" — so it is subscription-backed OAuth,
**not** an API key. That distinction is the whole reason this module exists
and is enforced mechanically below (:func:`token_problem`).

  **`ANTHROPIC_API_KEY` remains explicitly unsupported, forever (#2462).**
  Switching this fleet to API-key auth moves billing and entitlements off
  the Max subscription; when #2462 accidentally disabled OAuth (via
  `claude --bare`) every dispatch on every machine failed within the hour.
  A token file whose contents look like an API key (`sk-ant-api…`) is
  therefore *refused*, loudly, rather than injected — see
  `coord/agent.py`'s `--setting-sources user` comment and
  `coord/providers/claude.py:268` for the scar tissue this protects.

Verified against `claude` 2.1.270 on 2026-09-17 (#3371's "open questions",
answered):

* **The channel.** `CLAUDE_CODE_OAUTH_TOKEN` is a first-class credential
  environment variable in the `claude` binary — it appears in its own
  auth-source list next to `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`, is
  redacted by its secret-scrubbing list, and `/login` prints
  "CLAUDE_CODE_OAUTH_TOKEN was set in your environment … this session will
  use your new credentials" — i.e. the env var is the credential a session
  authenticates with unless a fresh interactive login supersedes it. That
  is the channel a `setup-token` credential is consumed through.
* **Minting stays interactive.** `claude setup-token` produces no output
  and hangs indefinitely with stdin closed (exit via timeout only), so it
  cannot be scripted. Minting is a one-time, per-host operator step; this
  module only *consumes* what that step produced.
* **Lifetime is still not published anywhere.** The minted token carries no
  expiry we can read, so a setup-token-backed host reports
  `expires_at=None` ("no expiry known") rather than a fabricated date —
  never a green "does not expire" signal. The interactive-session probe
  keeps publishing `refreshTokenExpiresAt`, which is where forward expiry
  visibility actually comes from today.

**Opt-in, additive, per host.** With no token file present this module is a
byte-for-byte no-op: an interactive host keeps authenticating exactly as it
does today, which is the "alongside — not replacing" requirement in #3371.
An operator adopts it on one host by writing the minted token to
:func:`setup_token_path` (``~/.coord/claude-setup-token``, mode 600) — the
same `~/.coord/` fleet-secret handling as every other per-host secret. The
token is never logged, never echoed into a briefing, and never rendered by
`coord doctor`: :class:`SetupTokenStatus` redacts it in `repr()` so it
cannot leak through an exception traceback or a debug log line either.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from coord.platform_paths import default_coord_dir

#: The environment variable `claude` reads a long-lived OAuth token from.
#: NOT `ANTHROPIC_API_KEY` — see this module's docstring.
CLAUDE_OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

#: Filename under the coordinator state root (`~/.coord` on POSIX) holding
#: the output of a one-time `claude setup-token` run on THIS host.
SETUP_TOKEN_FILENAME = "claude-setup-token"

#: Prefix of the credential `claude setup-token` mints (a subscription OAuth
#: token). Used only as an *advisory* shape note — an unrecognised prefix is
#: still used, because Anthropic renaming it must not take a working host out
#: of the routing pool (this module's failure mode has to be permissive for
#: everything except the one case below).
SETUP_TOKEN_PREFIX = "sk-ant-oat"

#: Prefix of an Anthropic **API key**. Hard-refused: #3371's headline
#: constraint is that the fleet's credential stays subscription-backed.
API_KEY_PREFIX = "sk-ant-api"

#: What an operator is told to do when no credential can be found at all.
MINT_HINT = (
    f"run `claude setup-token` on this machine and write the token to "
    f"~/.coord/{SETUP_TOKEN_FILENAME} (chmod 600), or run `claude` "
    f"interactively to refresh the login session"
)


def setup_token_path() -> Path:
    """Where this host's long-lived `claude setup-token` credential lives.

    Resolved fresh on every call through
    :func:`coord.platform_paths.default_coord_dir`, so `$COORD_DIR` (the
    seam every other `~/.coord` state file already honours) redirects it
    for tests and for a non-POSIX host alike.
    """
    return default_coord_dir() / SETUP_TOKEN_FILENAME


def token_problem(token: str) -> str | None:
    """The one *disqualifying* problem with *token*, or `None`.

    Exactly one thing disqualifies a token: looking like an
    `ANTHROPIC_API_KEY`. Anything else — an unfamiliar prefix, an
    unexpected length — degrades to "assume fine" and is reported as an
    advisory note instead (:func:`token_note`), matching this package's
    standing "degrade to unknown, assume fine" probe contract
    (`coord.prereqs.ToolProbe.ok`). Fail-closed on shape would mean a
    renamed token prefix silently removing every adopting host from the
    routing pool, which is a worse failure than the one it would prevent.
    """
    if token.lower().startswith(API_KEY_PREFIX):
        return (
            f"{CLAUDE_OAUTH_TOKEN_ENV} holds what looks like an Anthropic "
            "API key (sk-ant-api…), not a subscription `claude setup-token` "
            "credential — API-key auth is deliberately unsupported on this "
            "fleet (billing/entitlements stay on the Max subscription; "
            "#2462, #3371). Mint the real thing with `claude setup-token`"
        )
    return None


def token_note(token: str, *, path: Path | None = None) -> str | None:
    """Advisory, non-disqualifying observation about *token* / its file.

    Surfaced by `coord doctor` next to a healthy credential; never blocks
    injection and never takes a host out of the routing pool.
    """
    if path is not None:
        try:
            mode = path.stat().st_mode
        except OSError:
            mode = 0
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            return f"{path} is group/world-accessible — chmod 600 it"
    if not token.lower().startswith(SETUP_TOKEN_PREFIX):
        return (
            f"does not look like a `claude setup-token` credential "
            f"(expected {SETUP_TOKEN_PREFIX}…) — used anyway"
        )
    return None


@dataclass(frozen=True, repr=False)
class SetupTokenStatus:
    """Which long-lived credential this host has, if any — never the value.

    `repr()` is redacted deliberately: this object is passed around the
    agent process and could otherwise end up in a traceback, a debug log,
    or a `/health` payload. `token` is only ever read by
    :func:`inject_setup_token`, which writes it straight into a subprocess
    environment.
    """

    #: `"env"` (inherited from the agent's own environment), `"file"`
    #: (this host's `~/.coord/claude-setup-token`), or `"absent"`.
    source: str
    token: str | None = None
    path: Path | None = None
    problem: str | None = None
    note: str | None = None

    @property
    def present(self) -> bool:
        """Is a long-lived credential configured on this host at all?"""
        return self.source != "absent"

    @property
    def usable(self) -> bool:
        """Is it configured AND not disqualified (see :func:`token_problem`)?

        "Usable" is a statement about shape, not liveness — exactly like
        `coord.prereqs._probe_claude_credentials_linux`'s content check of
        `~/.claude/.credentials.json`. Neither can prove a credential still
        authenticates without spending a billable turn, which `coord
        doctor` is documented not to do.
        """
        return self.present and self.token is not None and self.problem is None

    def describe(self) -> str:
        """One-line, redacted summary safe to print or log."""
        if not self.present:
            return "no long-lived claude setup-token configured"
        where = (
            f"{CLAUDE_OAUTH_TOKEN_ENV} (agent environment)"
            if self.source == "env" else str(self.path)
        )
        if self.problem:
            return f"long-lived claude credential from {where}: {self.problem}"
        suffix = f" ({self.note})" if self.note else ""
        return f"long-lived claude setup-token from {where}{suffix}"

    def __repr__(self) -> str:  # never leak the secret, #3371
        return (
            f"SetupTokenStatus(source={self.source!r}, token=<redacted>, "
            f"path={self.path!r}, problem={self.problem!r}, note={self.note!r})"
        )


_ABSENT = SetupTokenStatus(source="absent")


def load_setup_token(
    env: dict[str, str] | None = None, *, path: Path | None = None
) -> SetupTokenStatus:
    """Resolve this host's long-lived claude credential.

    Precedence deliberately mirrors what `claude` itself does, and what
    :func:`inject_setup_token` will do: an already-set
    `CLAUDE_CODE_OAUTH_TOKEN` in the agent's environment wins over the
    on-disk file, so an operator who exports it from a systemd unit gets
    the behaviour they asked for and `coord doctor` reports on the
    credential the next dispatch will ACTUALLY use — not a different one
    sitting next to it. That single-resolution-path property is the whole
    point (#2096, "one question, one answer"): a probe that disagreed with
    dispatch about which credential is live is worse than no probe.

    Never raises: an unreadable or empty file is `"absent"`, not an error.
    """
    environ = os.environ if env is None else env
    from_env = (environ.get(CLAUDE_OAUTH_TOKEN_ENV) or "").strip()
    if from_env:
        return SetupTokenStatus(
            source="env",
            token=from_env,
            problem=token_problem(from_env),
            note=token_note(from_env),
        )
    token_path = setup_token_path() if path is None else path
    try:
        raw = token_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return _ABSENT
    token = raw.strip()
    if not token:
        return _ABSENT
    return SetupTokenStatus(
        source="file",
        token=token,
        path=token_path,
        problem=token_problem(token),
        note=token_note(token, path=token_path),
    )


def inject_setup_token(
    env: dict[str, str], *, status: SetupTokenStatus | None = None
) -> str | None:
    """Put this host's long-lived credential into a worker's *env*, in place.

    Returns the source it used (`"env"` when the worker already inherits a
    token, `"file"` when one was injected from disk) or `None` when this
    host has no usable long-lived credential and the worker will
    authenticate with the interactive `claude login` session exactly as it
    always has.

    Three properties this must keep, in priority order:

    1. **No token configured → no mutation at all.** Every host that has
       not opted in must produce a byte-for-byte identical worker
       environment, so adopting this cannot regress the fleet the way
       #2462 did.
    2. **Never overwrite an inherited token.** If the agent process itself
       carries `CLAUDE_CODE_OAUTH_TOKEN`, that is the operator's explicit
       choice and the worker already inherits it.
    3. **Never inject a disqualified token** (an API key). The variable is
       left exactly as it was; `coord doctor`'s claude probe reports the
       problem, and the credential-health gate (#3371's Part B) takes the
       host out of the routing pool rather than letting it fail at turn 1.
    """
    st = load_setup_token(env) if status is None else status
    if not st.usable:
        return None
    if st.source == "env":
        return "env"
    env[CLAUDE_OAUTH_TOKEN_ENV] = st.token or ""
    return "file"
