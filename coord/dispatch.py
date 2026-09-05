"""Dispatch approved assignments to agent servers and post briefings."""

from __future__ import annotations

import logging
import time
from typing import Iterable

import httpx

from coord import github_ops
from coord.comments import (
    format_advisory,
    format_briefing,
    format_completion,
    format_failure,
    format_refused_policy,
)
from coord.config import Config
from coord.models import EPIC_DECOMPOSE_TYPE, Proposal, Repo, coordinator_owned_docs

AGENT_PORT = 7433

_log = logging.getLogger(__name__)


class DispatchRefused(ValueError):
    """A pre-dispatch guard's refusal — deterministic, not transient (#1844).

    Raised by :func:`enforce_oracle_readiness` and
    :func:`enforce_epic_dispatch_guard` instead of a plain ``ValueError``:
    both refuse on a condition that CANNOT change between attempts (no
    acceptance slice exists yet; a tracking issue carries the epic label),
    unlike the other ``ValueError``s ``dispatch()`` can raise (an unresolved
    machine/repo_path, the #437 TOS gate, a provider/machine capability
    mismatch) which this deliberately leaves alone — those are refusals too,
    but reclassifying their retry-worthiness is outside what this issue
    covers, and several of them ARE operator-fixable in ways a running fleet
    can race (e.g. adding a capability to a machine's config while `coord
    drive-queue` is ticking).

    A subclass of ``ValueError``, not a new hierarchy: every existing
    ``except ValueError`` catch (CLI error handling, tests asserting the
    message) keeps working completely unchanged — `str(exc)` is still the
    plain refusal text. Only a caller that specifically wants to know "was
    this refusal deterministic" (``coord drive``'s subprocess boundary, via
    ``coord assign``/``coord approve-plan``/``coord fix`` mapping THIS
    exception — and no other — to ``coord.drive.EXIT_DISPATCH_REFUSED``)
    needs to catch it by name.
    """


def enforce_oracle_readiness(
    *, proposal_type: str, repo: Repo | None, config: Config, issue_number: int,
) -> None:
    """#1138: hard-gate a ``type="work"`` dispatch on the issue-level oracle
    gate (:func:`coord.milestone_dispatch.issue_oracle_ready`) — refuses an
    issue that belongs to an oracle-opted-in milestone (Gate A already
    satisfied) but has no JIT-authored acceptance slice yet, or whose repo
    declares a driver ``kind`` this install doesn't implement.

    Raises :class:`DispatchRefused` on refusal — a :class:`ValueError`
    subclass (#1844), so callers get "refuse cleanly" for free via their
    existing ``except ValueError`` handling (``coord approve``, ``coord
    assign``, ``coord milestone dispatch``) with zero CLI-layer changes, same
    as before this was split out from a plain ``ValueError``. Distinct from
    it specifically so `coord drive`'s subprocess boundary can tell THIS
    refusal — deterministic, no acceptance slice will appear on retry —
    apart from a transient one (missing ``repo_path``, the #437 TOS gate).

    Cheap no-op — no network call — for every dispatch outside #1138's
    scope: non-work proposal types (``plan``, ``review``, ``smoke``, ...),
    an unknown repo, or a repo with no ``acceptance.drivers`` entry
    configured (``has_driver`` is a local dict lookup). Shared by the
    headless ``dispatch()`` POST below and the ``--interactive``
    human-attended work launcher (``_dispatch_interactive_work``, which
    never calls ``dispatch()``) so both flavours of Work dispatch are
    covered, not just the unattended one.

    Fails OPEN (proceeds, doesn't gate) if the issue itself can't be
    fetched — mirroring the fail-soft posture the rest of the oracle-loop
    machinery already uses (``oracle_loop_contract_block``, #945: "never let
    a [...] read break dispatch"). By the time ``dispatch()`` runs, the
    caller has already successfully fetched this same issue once (for its
    title/briefing) moments earlier, so a failure here is a genuine
    transient blip, not a sign the issue doesn't exist — treating it as a
    hard stop would turn a GitHub hiccup into a fleet-wide outage for every
    oracle-configured repo, which is a worse failure mode than the gap
    #1138 closes.
    """
    if proposal_type != "work" or repo is None:
        return
    if not config.acceptance.has_driver(repo.name):
        return

    from coord import github_ops  # noqa: PLC0415
    from coord.milestone_dispatch import issue_oracle_ready  # noqa: PLC0415

    try:
        issue_data = github_ops.get_issue(repo.github, issue_number)
    except RuntimeError:
        return

    milestone_number = (issue_data.get("milestone") or {}).get("number")
    issue_labels = [lbl.get("name", "") for lbl in (issue_data.get("labels") or [])]

    readiness = issue_oracle_ready(
        repo, config, milestone_number, issue_number, issue_labels,
    )
    if readiness.reason is not None:
        raise DispatchRefused(readiness.reason)


def enforce_epic_dispatch_guard(
    *, proposal_type: str, repo: Repo | None, config: Config, issue_number: int,
) -> None:
    """#1314: refuse a dispatch that would auto-close an epic/tracking issue
    on merge (``proposal_type`` in :data:`coord.models.CLOSES_ISSUE_TYPES`,
    e.g. ``"work"``) when *issue_number* itself carries the ``"epic"``
    label (:data:`coord.milestone_order.TRACKING_ISSUE_LABEL`).

    The #1077/#1142 ``CLOSES_ISSUE_TYPES`` split (see ``coord/models.py``)
    already assumes only ``mock-author``/``test-author``/``epic-decompose``
    (#3132) are ever dispatched directly against a tracking issue's own
    number — a small correction to an already-merged Gate-A contract, with
    no properly-typed tool for it yet, falls back to a plain ``coord
    assign`` (``type="work"``) instead. That silently breaks the same
    assumption a ``type="work"`` merge relies on everywhere else: that
    ``issue_number`` is real, resolvable work, not a milestone's tracking
    issue. Hit in practice against epic #1120's Gate A contract (PR #1312)
    — this is the dispatch-time half of the fix; ``coord/commands/
    plan_followup.py``'s ``pr()`` command independently checks the same
    label so the PR body never carries the closing keyword even for an
    already-dispatched assignment.

    This function's own scope is narrow — it only ever raises for
    *proposal_type* in :data:`coord.models.CLOSES_ISSUE_TYPES` (``"work"``).
    A ``type="epic-decompose"`` dispatch against the SAME epic is a no-op
    here (never enters the ``CLOSES_ISSUE_TYPES`` check at all) — that is
    the whole point of #3132: the properly-typed dispatch this docstring
    used to say didn't exist yet. See
    :data:`coord.models.EPIC_DECOMPOSE_TYPE` /
    :func:`epic_decompose_briefing` for what that dispatch actually briefs.

    Override: label the issue ``oracle:exempt`` (the existing "I know what
    I'm doing, let this bypass oracle-loop-specific gating" signal — see
    :func:`enforce_oracle_readiness`) to dispatch anyway — or, preferably
    since #3132, use ``type="epic-decompose"`` instead, which needs no
    override at all. Raises :class:`DispatchRefused` on refusal (#1844) —
    same deterministic-refusal reasoning as :func:`enforce_oracle_readiness`,
    and still a :class:`ValueError` under the hood, so callers get "refuse
    cleanly" for free via their existing ``except ValueError`` handling.

    Fails OPEN (proceeds) if the issue can't be fetched or *repo* is
    ``None`` — mirrors :func:`enforce_oracle_readiness`'s posture; a
    transient GitHub read failure must not turn into a fleet-wide dispatch
    outage.

    Scoped to repos with an ``acceptance.drivers`` entry configured (same
    cheap no-op test :func:`enforce_oracle_readiness` uses) — a local dict
    lookup, no network call, for every dispatch outside this scope. #1314's
    actual failure mode is inherent to the oracle loop's own convention of
    dispatching ``mock-author``/``test-author`` against a tracking issue's
    number in the first place; a repo with no acceptance driver has no such
    convention, so this intentionally does not add a `gh` round-trip to
    every "work" dispatch fleet-wide for a scenario that can't arise there.
    A plain (non-oracle) repo whose operator manually dispatches "work"
    against an epic's own number is a real but separate gap, same as the
    one #1138's oracle-readiness gate already accepts for the same reason.
    """
    from coord.models import CLOSES_ISSUE_TYPES  # noqa: PLC0415

    if proposal_type not in CLOSES_ISSUE_TYPES or repo is None:
        return
    if not config.acceptance.has_driver(repo.name):
        return

    from coord.milestone_order import TRACKING_ISSUE_LABEL  # noqa: PLC0415

    try:
        issue_data = github_ops.get_issue(repo.github, issue_number)
    except RuntimeError:
        return

    issue_labels = {lbl.get("name", "") for lbl in (issue_data.get("labels") or [])}
    if TRACKING_ISSUE_LABEL not in issue_labels or "oracle:exempt" in issue_labels:
        return

    raise DispatchRefused(
        f"refusing type={proposal_type!r} dispatch against #{issue_number}: it "
        f"carries the {TRACKING_ISSUE_LABEL!r} label (a milestone tracking/epic "
        "issue) — merging this would close the epic while its real sub-issues "
        "stay open/untouched (#1314). If this is a deliberate meta-level "
        "dispatch against the tracking issue's own number (e.g. a Gate-A "
        "contract correction), label the issue 'oracle:exempt' to override, "
        "or use a properly-typed dispatch instead — e.g. `coord acceptance "
        "mock <repo> <tracking_issue> --amend '<correction>'` for a "
        "targeted fix to an already-merged Gate-A contract (#1315), or "
        "`coord assign <machine> <repo> <tracking_issue> --type "
        "epic-decompose` to hand the epic to a worker for in-pickup "
        "decomposition (#3132) — that type never trips this guard."
    )


# #3132: the addendum appended to a ``type="epic-decompose"`` dispatch's
# briefing — see ``epic_decompose_briefing`` below. Kept as a module-level
# constant (not inlined in the function) so a test can assert against the
# exact contract text without re-deriving it, the same way
# ``coord.acceptance``'s oracle-loop contract block is a named, testable
# piece of text rather than an inline f-string.
EPIC_DECOMPOSE_CONTRACT = """\
## Epic decomposition contract (#3132)

This issue is an epic/tracking issue, dispatched with `type="epic-decompose"` \
specifically so it does NOT auto-close when your PR merges — the epic is the \
tracker; it closes when its own checklist is complete, not when this PR does. \
Decomposition happens now, with the checkout in hand, so you can re-verify \
any `file:line` citations in the epic body against the current code rather \
than trusting them as written.

Your job, in order:

1. **Decompose fully.** Read the epic's own decomposition/handoff \
instructions (if it carries them, follow those verbatim) and file every \
child issue this epic implies. Register each one against this epic with \
`coord milestone add-child <repo> <this epic's issue number> <new issue \
number>` (REPO EPIC ISSUE, all positional — no `--child` flag) so the \
epic's checklist and this epic's tracking stay in sync — never hand-edit \
the checklist directly.
2. **Queue the first batch.** At most 6 of the newly-filed children, \
chained serially so they land one at a time: `coord drive-queue add <repo> \
<child 1>`, then `coord drive-queue add <repo> <child 2> --after <repo>#\
<child 1>`, and so on.
3. **Re-queue this epic behind that batch** — `coord drive-queue add <repo> \
<this epic's issue number> --after <repo>#<child N>` (the last one queued) \
— so decomposition continues once the first batch lands, if more children \
remain.
4. **Implement only the first slice in this pickup.** Do not attempt the \
whole epic in one PR — that defeats the point of decomposing it.
5. **Leave this epic open.** Do not close it yourself and do not word your \
PR body as "Closes #N" — the coordinator already opens this PR with `Refs \
#N`, non-closing, for exactly this reason. The epic closes only when its \
checklist is complete.
"""


def epic_decompose_briefing(issue_number: int) -> str:
    """The full ``type="epic-decompose"`` briefing addendum for *issue_number*
    (#3132) — the contract described in :data:`EPIC_DECOMPOSE_CONTRACT`,
    naming the epic's own issue number so the worker doesn't have to infer
    which issue the ``add-child``/``drive-queue add`` commands below refer
    to.

    Appended (not prepended) to the proposal's own briefing by
    :func:`dispatch` — the epic's own issue body already carries whatever
    decomposition instructions its author wrote (per #3132's motivating
    issue, several epics wrote this out by hand); this is the coordinator's
    OWN durable restatement of the same contract, so a worker never has to
    rely solely on prose an operator might have gotten slightly wrong.
    """
    return (
        f"{EPIC_DECOMPOSE_CONTRACT}\n"
        f"(This epic's own issue number, for the commands above: #{issue_number}.)\n"
    )


def enforce_model_provider_compatibility(
    *, wire_model: str | None, effective_provider_name: str, config: Config,
) -> None:
    """#1798: STRUCTURAL MODEL/PROVIDER GATE — refuse to dispatch a *wire_model*
    that cannot plausibly belong to the resolved provider's backend type
    (:func:`coord.config.model_plausible_for_provider_type`), e.g. a Claude
    alias (``"sonnet"``) handed to an ``opencode``-type provider.

    :func:`resolve_dispatch_model_alias`'s precedence fix (provider pin wins
    over label routing) closes the common path into this failure, but
    doesn't close every one — an operator can still pass an explicit
    ``--model`` that mismatches the resolved provider, or configure a
    non-claude provider with no pin at all (so a namespace-mismatched
    *label_model* falls through unchecked). Before this gate, a mismatch
    like that was only discovered when the backend itself rejected the
    argument mid-run, minutes into a worker, after the assignment row
    existed and the worktree was built — see the #1798 issue's #1708 proof
    run, where an explicit ``--model opencode/glm-5.2`` override against a
    silently-``claude``-falling-back agent made a masked bug visible only
    because the mismatch was loud. Placed alongside the other structural
    gates in :func:`dispatch` (oracle readiness, epic-target, TOS,
    provider-availability), before any worktree/HTTP work happens.

    A ``None`` *wire_model* (``--model`` omitted, provider's own default
    applies) is always a no-op — there's nothing to validate.

    Raises:
        ValueError: When *wire_model* is set but implausible for the
            resolved provider's type, naming both.
    """
    if wire_model is None:
        return
    from coord.providers import provider_type_for  # noqa: PLC0415
    from coord.config import model_plausible_for_provider_type  # noqa: PLC0415

    provider_type = provider_type_for(effective_provider_name, config.providers)
    if model_plausible_for_provider_type(wire_model, provider_type):
        return
    raise ValueError(
        f"refusing dispatch: model {wire_model!r} is not valid for provider "
        f"{effective_provider_name!r} (type {provider_type!r}) — the two "
        "belong to different model namespaces (#1798). Pass an explicit "
        "--model in the resolved provider's own namespace, or leave "
        "--model unset and pin providers.definitions"
        f"[{effective_provider_name!r}].model in coordinator.yml instead."
    )


def _reraise_with_body(
    exc: httpx.HTTPStatusError, machine_name: str,
) -> httpx.HTTPStatusError:
    """#1527: fold the agent's rejection reason into the raised exception.

    ``AgentServer.assign`` (coord/agent.py) raises a precise ``ValueError``
    for every dispatch-time rejection — unhandled repo, missing
    ``repo_path``, unknown ``pull_repos``, the #425/#324 provider
    capability gates — and ``agent_app.py``'s ``assign`` route faithfully
    returns each as ``{"error": "<reason>"}`` with a 400. Plain
    ``resp.raise_for_status()`` discards that body: every existing caller
    that catches ``httpx.HTTPError`` and renders ``str(e)`` (``coord/
    commands/dispatch.py``, ``milestone_dispatch.py``, ``plan_followup.py``,
    ``dispatch_workers.py``, ...) only ever saw the generic status line
    ("400 Bad Request"), never the agent's own reason.

    Returns a **new** ``httpx.HTTPStatusError`` — never raises one — so
    ``dispatch()`` can ``raise ... from e`` at the call site. Carries the
    same ``request``/``response`` as *exc* so ``classify_error``/
    ``is_retryable`` (coord/network.py), which inspect
    ``exc.response.status_code``, keep working unchanged; only the message
    gains the detail.

    Degrades gracefully rather than raising a *different* error out of an
    error handler: a non-JSON or JSON-but-not-``{"error": ...}`` body falls
    back to the raw response text (truncated); an empty body falls back to
    the original status-line message.
    """
    detail = ""
    try:
        parsed = exc.response.json()
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        detail = str(parsed.get("error") or "")
    if not detail:
        detail = exc.response.text[:500].strip()
    message = (
        f"{machine_name} rejected the assignment: {detail}" if detail else str(exc)
    )
    return httpx.HTTPStatusError(message, request=exc.request, response=exc.response)


def resolve_dispatch_model_alias(
    *,
    explicit_model: str | None,
    label_model: str | None,
    config: Config,
    effective_provider_name: str,
) -> str | None:
    """Resolve the dispatch model *alias* (before ``models.resolve()``).

    Precedence: *explicit_model* → the effective provider's own pinned
    ``ProviderDef.model`` (for non-claude/claude-pty backends only) →
    *label_model* → ``models.default``. The board/DB stores this alias for
    legibility; the caller is responsible for the final
    ``config.models.resolve()`` translation to an exact model id (typically
    deferred to :func:`dispatch`/:func:`resolve_dispatch_model`, which run
    at actual-wire-payload time — see the module docstring of each CLI
    entry point that calls this function for why: re-resolving here would
    bake an exact id into ``Proposal.model``/board bookkeeping instead of
    the human-legible alias).

    #1798: *label_model* comes from ``models.labels``, which maps to
    Anthropic aliases (``tier:small -> haiku``) — a namespace that means
    nothing to a non-claude/claude-pty backend. Letting it win over a
    pinned non-claude provider's own ``ProviderDef.model`` (the OLD
    precedence, `explicit_model or label_model` unconditionally) dispatched
    e.g. ``opencode`` with ``--model sonnet``, a Claude alias the opencode
    binary cannot serve — silently, with no validation at dispatch time.
    Now the provider's own pin wins over label routing whenever it applies
    (*provider_pins_model*); only an *explicit_model* — a human being
    specific — still overrides the pin. Claude/claude-pty backends are
    unaffected: *provider_pins_model* is always ``False`` for them (their
    ``ProviderDef.model``, if any, is folded into ``models.default`` via a
    different path — see :attr:`coord.config.ProviderDef.model`'s
    docstring), so label routing there is unchanged.

    #1430: label routing (*label_model*) is gated to ``type="work"`` by
    every caller — plan workers are read-only/cheap and must not inherit a
    ``tier:large`` -> opus routing meant for the eventual work dispatch.
    Callers for which label routing doesn't apply (plan-only, review,
    smoke, chat, etc.) pass ``label_model=None``.

    #1706 review fix: ``config.models.default`` (e.g. ``"sonnet"``) is a
    Claude model alias and means nothing to a non-Claude backend — passing
    it through as ``--model sonnet`` to ``opencode run`` (or any future
    non-Claude provider) is nonsensical. When *explicit_model* didn't supply
    a model, and the effective provider's backend *type* is NOT
    ``claude``/``claude-pty``, check whether that provider's own
    ``ProviderDef.model`` (coordinator.yml ``providers.
    definitions.<name>.model``) is pinned; if so, return ``None`` instead
    of *label_model*/``models.default`` so the eventual wire payload omits
    ``--model`` entirely and the provider instance's own ``build_command``
    falls back to its ``self._model`` (threaded in from ``ProviderDef.model``
    at construction — see ``coord/providers/opencode.py`` and
    ``coord/providers/claude.py``). This makes ``providers.definitions.
    <name>.model`` actually reachable for a normal ``coord assign``/``coord
    approve``/``coord milestone dispatch`` with no explicit ``--model``,
    instead of being permanently shadowed by ``config.models.default`` OR
    (#1798) by a namespace-mismatched *label_model*.

    Only an explicit override (``--model``) wins over the provider's pin —
    matching the precedence already implemented in each provider's
    ``build_command``: resolved_model > spec.model > definition.model.
    Label routing does NOT win over the pin (#1798 fix): ``models.labels``
    resolves to Claude aliases, which are meaningless to a pinned non-claude
    provider, so the pin — the only value in the correct namespace — wins
    instead.

    This function is called from every site that used to inline the
    ``explicit or label or config.models.default`` rule so the
    provider-aware exception lives in exactly one place:
    ``coord.dispatch.dispatch`` (the fallback, for callers that pass
    ``proposal.model=None``), ``coord.commands.dispatch_workers.
    _dispatch_headless`` (``coord assign``), the ``approve`` command's
    per-proposal loop in ``coord.commands.dispatch``, and
    ``coord.milestone_dispatch.dispatch_entry`` (``coord milestone
    dispatch``).

    Deliberately keyed off ``ProviderDef.type``, not the definition's name:
    an operator can register a ``claude``/``claude-pty`` backend under an
    arbitrary name (e.g. ``fast-claude`` in ``coordinator.example.yml``),
    and that backend's ``--model`` must still flow through
    ``config.models.resolve()``'s alias -> exact-id translation
    (``models.versions``) — bypassing ``models.default`` for it here would
    let an unresolved alias leak through instead (see ``ProviderDef.model``'s
    docstring for the caveat).

    Args:
        explicit_model: The explicit per-dispatch override (``--model``
            flag, or an already-resolved ``proposal.model`` from an earlier
            stage), if any.
        label_model: The issue-label-routed model (``type="work"`` only),
            or ``None`` when label routing doesn't apply.
        config: The coordinator config (``models`` and ``providers``).
        effective_provider_name: The already-resolved effective provider
            name (spec > repo > ``providers.default``), as returned by
            :func:`coord.providers.resolve_provider_name` or
            :func:`coord.providers.guard_unattended_dispatch`.

    Returns:
        The model alias (NOT yet passed through ``config.models.resolve()``),
        or ``None`` to omit ``--model``.
    """
    provider_def = config.providers.definitions.get(effective_provider_name)
    provider_pins_model = (
        provider_def is not None
        and provider_def.type not in ("claude", "claude-pty")
        and provider_def.model is not None
    )
    if explicit_model:
        return explicit_model
    if provider_pins_model:
        return None
    if label_model:
        return label_model
    return config.models.default


def resolve_dispatch_model(
    proposal: Proposal, config: Config, effective_provider_name: str,
) -> str | None:
    """Resolve the wire-payload ``model`` for a :func:`dispatch` call.

    Thin wrapper around :func:`resolve_dispatch_model_alias`: computes
    *label_model* from ``proposal.issue_labels`` (gated to ``type="work"``,
    per #1430 — see that function's docstring), then translates the
    resulting alias to an exact model id via ``config.models.resolve()``
    (``models.versions``, when configured) for the actual wire payload.

    #1430: most callers (``coord approve``, ``coord assign``, ``coord
    milestone dispatch``) already pre-resolve ``proposal.model`` via
    :func:`resolve_dispatch_model_alias` themselves before calling
    :func:`dispatch` — for bookkeeping, so the dispatched record/board
    reflect what actually ran. This function is the fallback for any
    caller that doesn't: it repeats the same rule so :func:`dispatch` is
    correct on its own, not just when every caller remembers to do the
    work upfront.

    Args:
        proposal: The dispatch proposal (``proposal.model`` is the explicit
            per-dispatch override, if any — already resolved by an earlier
            stage for most callers).
        config: The coordinator config (``models`` and ``providers``).
        effective_provider_name: The already-resolved effective provider
            name (spec > repo > ``providers.default``), as returned by
            :func:`coord.providers.guard_unattended_dispatch`.

    Returns:
        The wire-ready model id/alias (already passed through
        ``config.models.resolve()``), or ``None`` to omit ``--model``.
    """
    label_model = (
        config.models.model_for_labels(proposal.issue_labels)
        if proposal.type == "work"
        else None
    )
    alias = resolve_dispatch_model_alias(
        explicit_model=proposal.model,
        label_model=label_model,
        config=config,
        effective_provider_name=effective_provider_name,
    )
    return config.models.resolve(alias)


def _wire_payload_needs_provider_field(
    effective_provider_name: str, config: Config,
) -> bool:
    """Whether :func:`dispatch`'s wire payload must include ``"provider"``
    (#1711 review of #324's payload-omission gap).

    The historical rule (send ``provider`` only when the effective name is
    not ``"claude"``) was deliberate old-agent compatibility, but it hid a
    reachable divergence: the ``"claude"`` entry in
    ``providers.definitions`` can be **customized** (a redefined
    ``binary``, ``env``, or ``extra_args`` — legal per ``ProviderDef``'s
    docstring, e.g. to point ``claude`` at a wrapped binary) without ever
    changing its NAME. Omitting the field for name ``"claude"``
    unconditionally sends the agent down its hardcoded legacy spawn path
    (``coord.agent.default_worker_command``, always the bare ``"claude"``
    binary with no env/extra_args) instead of through the provider seam
    that would apply those customizations — so the coordinator's recorded
    ``provider_name="claude"`` would silently stop matching what actually
    ran on the agent, corrupting exactly the kind of comparison this
    provider-plumbing epic (#1709) exists to make trustworthy.

    Fix: still omit the field for the vanilla (uncustomized) ``"claude"``
    definition — preserving byte-identical payloads for every no-
    ``providers:``-block deployment and genuine old-agent compatibility —
    but include it the moment that definition carries ANY customization,
    so the agent is told to route through the provider seam and actually
    apply it. A non-``"claude"`` effective name always needs the field
    regardless (unchanged from #324).
    """
    if effective_provider_name != "claude":
        return True
    definition = config.providers.definitions.get("claude")
    if definition is None:
        return False
    return bool(
        definition.binary or definition.model or definition.env or definition.extra_args
    )


def dispatch(
    proposal: Proposal,
    config: Config,
    *,
    pull_repos: Iterable[str] = (),
    fresh_branch: bool = False,
) -> dict:
    """POST an assignment to the agent server on the target machine.

    Returns the response JSON from the agent server (which includes the
    server-assigned `id`).
    """
    machine = next(
        (m for m in config.machines if m.name == proposal.machine_name), None
    )
    if machine is None:
        raise ValueError(f"Unknown machine: {proposal.machine_name!r}")

    repo_path = machine.repo_path(proposal.repo_name)
    if repo_path is None:
        raise ValueError(
            f"No repo_path configured for {proposal.repo_name!r} on machine {machine.name!r}. "
            f"Add it to coordinator.yml under machines[].repo_paths."
        )

    # Resolve deny-list from the repo's worker_permissions config.
    repo = config.repo(proposal.repo_name)

    # #1138: STRUCTURAL ORACLE-LOOP GATE — refuse a `type="work"` dispatch
    # for an issue inside an oracle-opted-in milestone (Gate A satisfied)
    # that has no JIT-authored acceptance slice yet, or whose repo declares
    # a driver kind this install doesn't implement. Placed early / before
    # the TOS gate below so a refusal never depends on provider resolution
    # succeeding first.
    enforce_oracle_readiness(
        proposal_type=proposal.type, repo=repo, config=config,
        issue_number=proposal.issue_number,
    )

    # #1314: STRUCTURAL EPIC-TARGET GATE — refuse a dispatch that would
    # auto-close a tracking/epic issue on merge (see
    # `enforce_epic_dispatch_guard`'s docstring). Placed alongside the
    # oracle-readiness gate above, before the TOS gate, for the same reason.
    enforce_epic_dispatch_guard(
        proposal_type=proposal.type, repo=repo, config=config,
        issue_number=proposal.issue_number,
    )

    # #437: STRUCTURAL TOS-COMPLIANCE GATE — refuse to route an
    # unattended dispatch through a provider whose capabilities mark it
    # ``human_attended_only`` (subscription-billed interactive Claude
    # Code).  Precedence: per-proposal override (if the brain ever sets
    # one) → ``providers.labels`` match (#1889) → per-repo
    # ``Repo.provider`` → ``config.providers.default``.  Deferred import so
    # the unattended dispatch surface stays free of a module-level cycle
    # with the provider registry.
    from coord.providers import guard_unattended_dispatch  # noqa: PLC0415
    spec_provider = getattr(proposal, "provider", None)
    # #1889: providers.labels routes work dispatches by the issue's
    # harness-eval label (e.g. `harness:opencode`); plan/review/smoke
    # proposals deliberately stay off it, same as `models.labels` (#1430) —
    # a label meant for the eventual work dispatch must not leak into a
    # cheap/read-only stage.
    provider_issue_labels = proposal.issue_labels if proposal.type == "work" else None
    # #324: resolve the effective provider name (spec > label > repo >
    # default) so the coordinator DB always records the winning provider
    # regardless of which level supplied it, and the wire payload carries
    # the exact name the agent should look up in its registry.
    effective_provider_name: str = guard_unattended_dispatch(
        spec_provider=spec_provider,
        repo_provider=repo.provider if repo is not None else None,
        providers_cfg=config.providers,
        models_cfg=config.models,
        where="coord approve / dispatch",
        issue_labels=provider_issue_labels,
    )

    # #1711: STRUCTURAL PROVIDER-AVAILABILITY GATE — refuse to route a
    # dispatch to a machine that hasn't declared it can run the resolved
    # provider (e.g. an `opencode` assignment landing on a machine with no
    # `provider:opencode` capability). Without this, the failure only
    # surfaced at spawn time inside the agent process as an ENOENT-shaped
    # subprocess error, after the assignment row existed and the worktree
    # was built. Placed right after the TOS gate above, for the same
    # reason: both are structural refusals keyed off the same resolved
    # provider name, before any repo/worktree/HTTP work happens.
    from coord.providers import guard_provider_machine_capability  # noqa: PLC0415

    guard_provider_machine_capability(
        provider_name=effective_provider_name,
        machine=machine,
        all_machines=config.machines,
        providers_cfg=config.providers,
        where="coord approve / dispatch",
    )
    deny_commands: list[str] = []
    if repo is not None and repo.worker_permissions is not None:
        deny_commands = repo.worker_permissions.deny

    # Resolve coordinator-only files (workers must not read or modify these).
    # #2966: coordinator_only_files was set by zero repos fleet-wide, so this
    # started empty for every work dispatch and only ever grew sealed
    # acceptance paths below — never a doc. coordinator_owned_docs() unions
    # in a fleet-wide default (the repo's own CLAUDE.md) regardless of
    # whether coordinator_only_files is configured, so "only the coordinator
    # writes docs" is enforced (advisory, like every non-oracle
    # files_forbidden entry) without depending on 10 repos' worth of config.
    files_forbidden: list[str] = coordinator_owned_docs(repo)

    # #944 sealing v1 (docs/ORACLE_LOOP.md): the acceptance oracle is
    # read-only/run-only for the worker — it's authored by an independent
    # test-author, not the worker under test. Auto-forbid it for any repo
    # with an acceptance driver configured, so sealing doesn't depend on an
    # operator remembering to also list it under coordinator_only_files.
    # #930: exempt `mock-author` — the one type whose entire job IS writing
    # under tests/acceptance/ms-NN/ (Gate A). A future `test-author` (#931)
    # gets the same exemption when it lands.
    # #1552: forbid the driver's declared `entrypoint:` alongside the tree.
    # It is the oracle's crate root — a `type="work"` worker editing
    # `tui/tests/acceptance.rs` can unwire (or re-point) the very slice it is
    # being graded against without ever touching `tests/acceptance/**`.
    if (
        proposal.type != "mock-author"
        and config.acceptance.has_driver(proposal.repo_name)
    ):
        for sealed in config.acceptance.sealed_paths(proposal.repo_name):
            if sealed not in files_forbidden:
                files_forbidden.append(sealed)

    # Resolve model: proposal override → provider-definition pin
    # (non-claude/claude-pty only) → models.labels (type="work" only) →
    # config default. See resolve_dispatch_model()'s docstring for the full
    # precedence rationale (#1430, #1706, #1798).
    wire_model = resolve_dispatch_model(proposal, config, effective_provider_name)

    # #1798: STRUCTURAL MODEL/PROVIDER GATE — refuse a resolved model that
    # cannot plausibly belong to the resolved provider's backend type (e.g.
    # a Claude alias handed to an opencode-type provider) before any
    # worktree/HTTP work happens. See enforce_model_provider_compatibility's
    # docstring for why this is still needed even after the precedence fix
    # above (an explicit --model, or an unpinned non-claude provider, can
    # still produce a mismatch).
    enforce_model_provider_compatibility(
        wire_model=wire_model, effective_provider_name=effective_provider_name,
        config=config,
    )

    # #255: pin the worker's branch base to the repo's configured default
    # branch.  Without this the agent fell back to a hardcoded "main", which
    # silently routed around `default_branch: develop` repos like quadraui
    # and let local-only commits on the default branch slip into worker
    # branches.
    #
    # #934: when the target issue belongs to a milestone (`proposal.
    # milestone_number`, set by callers like `coord.milestone_dispatch.
    # dispatch_entry` that already fetched the issue) and the repo has
    # opted into the develop + feature-branch-per-milestone git model
    # (`repo.develop_branch` set), branch off `feature/ms-NN` instead —
    # `coord.branch_model.resolve_base_branch` falls back to today's flat
    # `default_branch` behavior for every other repo/proposal.
    from coord.branch_model import resolve_base_branch  # noqa: PLC0415

    if repo is not None:
        default_branch = resolve_base_branch(repo, proposal.milestone_number)
    else:
        default_branch = "main"

    # #305: artifact_paths are only relevant for work assignments.  Skip for
    # review, smoke, refinement, and other non-work types.
    artifact_paths: list[str] = []
    if proposal.type == "work" and repo is not None:
        artifact_paths = list(repo.artifact_paths)

    # #352: resolve new-issue guidance for new-issue-chat assignments.
    # Only resolve when the repo *explicitly configured* new_issue_guidance —
    # the resolver always returns a non-empty _DEFAULT, so checking
    # `if new_issue_guidance:` below would always send the field, causing
    # agents that predate #352 to reject the payload with a 400.  Gating on
    # the raw config field lets repos without guidance dispatch to any agent
    # (the agent's built-in NEW_ISSUE_CHAT_SYSTEM_PROMPT is fine without it).
    new_issue_guidance: str = ""
    if proposal.type == "new-issue-chat" and repo is not None and repo.new_issue_guidance:
        from pathlib import Path
        new_issue_guidance = repo.resolve_new_issue_guidance(Path(repo_path).expanduser())

    # #603: prepend the per-issue context digest to the TOP of a -p WORK
    # briefing (cross-repo deps / prior-attempt findings) so the worker reads
    # them first.  Only `work` (chat/refinement/conflict-fix carry no issue
    # context); the interactive and auto-loop fix/review paths inject at their
    # own sites, so this is the single -p work chokepoint (no double injection).
    #
    # #945 (docs/ORACLE_LOOP.md "The worker briefing contract"): right after
    # the #603 digest, prepend the oracle-loop contract when this repo has an
    # acceptance driver configured (the oracle-loop proxy — #944 never landed
    # a milestone-level flag, so "driver configured for this repo" is the
    # signal, mirroring the tests/acceptance/ auto-seal above) AND this issue
    # already has an authored slice (oracle_loop_contract_block returns ""
    # otherwise, e.g. before Gate A/#931 has run for it).
    briefing_text = proposal.briefing
    if proposal.type == "work" and proposal.issue_number:
        from pathlib import Path  # noqa: PLC0415

        from coord.state import issue_context_block  # noqa: PLC0415

        oracle_contract = ""
        if config.acceptance.has_driver(proposal.repo_name):
            from coord.acceptance import oracle_loop_contract_block  # noqa: PLC0415

            # #2896: the issue's slice may live under the shared repo-root
            # tree (a directory-discovered driver, e.g. ms-37's cli-pytest
            # suite) OR under an entrypoint-linked driver's own sibling
            # `acceptance/` dir (e.g. ms-65's tui-tuidriver suite, relocated
            # out of the repo root) — this dispatch call has no single path
            # in hand to pick a route ahead of time (proposal.issue_number
            # alone doesn't say which), so it tries every search root this
            # repo declares and uses whichever one actually has the slice.
            # A local checkout scan (no `gh`), so trying more than one root
            # costs nothing but a stat/read against a directory that
            # usually doesn't exist.
            repo_root = Path(repo_path).expanduser()
            for search_root in config.acceptance.acceptance_search_roots(
                proposal.repo_name
            ):
                oracle_contract = oracle_loop_contract_block(
                    repo_root / search_root,
                    proposal.repo_name,
                    proposal.issue_number,
                    acceptance_dirname=search_root,
                )
                if oracle_contract:
                    break

        # #1720: dispatch-time file-overlap fence — the union of file
        # footprints of every OTHER currently-running work-like assignment
        # in this repo, derived from live branch diffs (not the brain.py
        # prompt-only heuristic, which only covers `coord plan` and guesses
        # from issue body text). Advisory only; never blocks this dispatch —
        # see coord.overlap_fence.compute_overlap_fence's docstring for the
        # fail-open contract. "" when there's nothing running (or nothing
        # with a pushed branch), same no-op-prefix shape as issue_context_block.
        overlap_fence = ""
        if repo is not None and repo.github:
            from coord.overlap_fence import compute_overlap_fence  # noqa: PLC0415

            overlap_fence = compute_overlap_fence(
                proposal.repo_name,
                repo.github,
                default_branch,
                exclude_issue_number=proposal.issue_number,
            )

        briefing_text = (
            issue_context_block(proposal.repo_name, proposal.issue_number)
            + overlap_fence
            + oracle_contract
            + briefing_text
        )

        # #3112: append the reviewer's own repo-specific grading rules
        # (`reviews.repo_overrides`) so the worker can read the exact
        # criteria it will be reviewed against — before this, the override
        # rules were read in coord/review.py alone, so a worker could be
        # graded on (and request-changes'd for) a rule it was never shown.
        # `repo_focus_lines` is the SAME builder `build_review_briefing`
        # calls for the reviewer's copy, so the two can never drift.
        from coord.review import repo_focus_lines  # noqa: PLC0415

        focus_lines = repo_focus_lines(config.reviews, proposal.repo_name)
        if focus_lines:
            briefing_text = (
                briefing_text
                + "\n\n## What the reviewer will grade you against\n"
                + "\n".join(focus_lines)
                + "\n"
            )
    elif proposal.type == EPIC_DECOMPOSE_TYPE and proposal.issue_number:
        # #3132 review: an epic-decompose worker gets a real worktree + branch
        # and implements the first slice (see WRITE_CAPABLE_SPEC_TYPES'
        # comment in coord/agent.py) — the same file-overlap exposure as a
        # `work` dispatch on this repo, so it gets the same #1720 dispatch-
        # time fence against other in-flight work-like assignments. Advisory
        # only; never blocks this dispatch (see
        # coord.overlap_fence.compute_overlap_fence's fail-open contract).
        overlap_fence = ""
        if repo is not None and repo.github:
            from coord.overlap_fence import compute_overlap_fence  # noqa: PLC0415

            overlap_fence = compute_overlap_fence(
                proposal.repo_name,
                repo.github,
                default_branch,
                exclude_issue_number=proposal.issue_number,
            )

        # #3132: append (not prepend — unlike the #603/#945 work-stage
        # blocks above, there is no per-issue context digest or oracle-loop
        # contract to lead with here) the decompose-and-queue contract so a
        # worker sees it durably from the coordinator itself, not only from
        # whatever the epic's own body happens to say.
        briefing_text = (
            briefing_text
            + overlap_fence
            + "\n\n"
            + epic_decompose_briefing(proposal.issue_number)
        )

    url = f"http://{machine.host}:{AGENT_PORT}/assign"
    payload: dict = {
        "repo_name": proposal.repo_name,
        "repo_path": repo_path,
        "issue_number": proposal.issue_number,
        "issue_title": proposal.issue_title,
        "briefing": briefing_text,
        "files_allowed": proposal.files_likely,
        "files_forbidden": files_forbidden,
        "pull_repos": list(pull_repos),
        "deny_commands": deny_commands,
        "model": wire_model,
        "type": proposal.type,
        "branch": default_branch,
    }
    # #351: only send artifact_paths when non-empty — older agents reject
    # unknown payload keys with a 400.  When absent the agent falls back to
    # self.artifact_paths (startup config).
    if artifact_paths:
        payload["artifact_paths"] = artifact_paths
    # #352: only send new_issue_guidance when non-empty — older agents don't
    # have this field and will reject the payload with a 400.
    if new_issue_guidance:
        payload["new_issue_guidance"] = new_issue_guidance
    # Only send fresh_branch when True — older agents don't have this field
    # and will reject the payload with a 400.
    if fresh_branch:
        payload["fresh_branch"] = True
    # Only send target_branch when set — agents predating #target_branch
    # (and the AssignmentSpec(**body) kwargs check) reject unknown fields.
    if proposal.target_branch:
        payload["target_branch"] = proposal.target_branch
    # #315: only send resume_session_id when set — older agents without the
    # field reject unknown payload keys with a 400.
    if getattr(proposal, "resume_session_id", None):
        payload["resume_session_id"] = proposal.resume_session_id
    # #2188: the issue's GitHub labels, so the agent's own reap can see
    # `coord.models.DELIVERABLE_ANALYSIS_LABEL` without a DB/GitHub round
    # trip (config-free agent — docs/EPHEMERAL_WORKERS.md). Only sent when
    # non-empty, same discipline as every optional wire field above — an
    # agent predating `AssignmentSpec.issue_labels` 400s on an unrecognized
    # kwarg.
    if proposal.issue_labels:
        payload["issue_labels"] = list(proposal.issue_labels)
    # #2131: per-leg spend ceiling, resolved here (CLI/daemon lane) and
    # carried on the wire so a config-free agent is covered too. Sent ONLY
    # when the operator has actually configured one — with no `budget:` block
    # `ceiling_for` returns None, the key is omitted, and the payload stays
    # byte-identical to pre-#2131 (so an agent predating the field, which
    # 400s on any unrecognized kwarg, is unaffected until someone opts in).
    _budget = getattr(config, "budget", None)
    _cost_ceiling = _budget.ceiling_for(proposal.type) if _budget is not None else None
    if _cost_ceiling:
        payload["cost_ceiling_usd"] = _cost_ceiling
    # #324/#1711: send the resolved provider name unless it's the vanilla
    # (uncustomized) implicit "claude" default — see
    # _wire_payload_needs_provider_field's docstring for why a customized
    # "claude" definition must NOT be silently omitted (#1711 review of the
    # #324 payload-omission gap). Older agents that predate #425's
    # spec.provider field would reject an unknown payload key; omitting the
    # field for the untouched default keeps every no-providers.-block
    # deployment's wire payload byte-identical to pre-#324 (no-config
    # parity requirement).
    if effective_provider_name and _wire_payload_needs_provider_field(
        effective_provider_name, config,
    ):
        payload["provider"] = effective_provider_name

    resp = httpx.post(url, json=payload, timeout=15)
    if (
        resp.status_code == 400
        and "provider" in payload
        and "provider_def" not in payload
        and config.providers.definitions.get(effective_provider_name) is not None
    ):
        # #1796 fix iteration 1 (review finding, blocking): the first
        # attempt above deliberately never carries "provider_def" — the
        # original #1796 patch attached it unconditionally to every
        # named-provider dispatch whenever the coordinator's own config had
        # a matching definition, which is a strictly wider condition than
        # "the agent actually needs it". "provider_def" is a field ONLY an
        # agent already updated PAST this release understands —
        # AssignmentSpec(**body) 400s on any unrecognized kwarg
        # (coord/agent_app.py) — so an agent that already supports
        # "provider" (#324) but hasn't yet received #1796's release would
        # get a brand-new hard 400 on every named-provider dispatch,
        # including ones that were already working correctly against its
        # own local providers.definitions (coordinator.yml). That's a
        # regression of an already-working path, not just a failure to fix
        # the config-free case #1796 targets — see docs/EPHEMERAL_WORKERS.md
        # and coord.agent.AgentServer._resolve_provider's docstring for what
        # "provider_def" is actually for.
        #
        # So "provider_def" is attached ONLY as a one-shot retry, fired
        # only once the agent has ALREADY refused the bare "provider"
        # payload with a 400. That 400 can only come from one of two agent
        # generations: (a) one old enough to predate #1796's refusal logic
        # never 400s here at all — it either resolves the name from its own
        # local registry (unchanged, no regression) or silently falls back
        # to the legacy claude path (the pre-#1796 bug, unaffected either
        # way by this change and unaffected by whether provider_def would
        # have helped); or (b) one new enough to run #1796's
        # `_resolve_provider` (coord/agent.py) — which means it is ALSO new
        # enough to accept "provider_def" — could not resolve the name from
        # its own local registry (a genuinely config-free agent, or a local
        # registry that's missing/stale relative to the coordinator's own)
        # and explicitly refused rather than guess. Retrying is safe to do
        # unconditionally on any 400 here because every ValueError
        # `AgentServer.assign` can raise is checked before it ever creates
        # an AgentAssignment or touches git/worktree state (coord/agent.py)
        # — so a retry can never double-spawn a worker or leak a worktree.
        # An unrelated 400 (e.g. a bad repo_path) just fails identically on
        # the retry too, and that message is what the caller ultimately
        # sees via _reraise_with_body below.
        from coord.providers import provider_def_to_wire  # noqa: PLC0415

        definition = config.providers.definitions[effective_provider_name]
        retry_payload = dict(payload, provider_def=provider_def_to_wire(definition))
        resp = httpx.post(url, json=retry_payload, timeout=15)

    if resp.status_code == 400 and "cost_ceiling_usd" in payload:
        # #2131: the agent lane lags the CLI/daemon lane (a `coord/agent.py`
        # change reaches agents only after a PyPI release plus `coord agent
        # update` — docs/AGENT_OPERATIONS.md). An agent that predates
        # `AssignmentSpec.cost_ceiling_usd` 400s on the unrecognized kwarg,
        # so without this fallback the first operator to enable `budget:`
        # would take the WHOLE FLEET's dispatch down until every agent was
        # updated. Retry once without the ceiling: an uncapped leg is exactly
        # today's behaviour, and is unambiguously better than no leg at all.
        # Safe to retry unconditionally — `AgentServer.assign` validates
        # every ValueError case before creating an assignment or touching
        # git/worktree state, so this can never double-spawn a worker.
        degraded_payload = {
            k: v for k, v in payload.items() if k != "cost_ceiling_usd"
        }
        retried = httpx.post(url, json=degraded_payload, timeout=15)
        if retried.status_code != 400:
            _log.warning(
                "agent %s rejected cost_ceiling_usd (#2131) — dispatched "
                "UNCAPPED; run `coord agent update` on that machine",
                machine.name,
            )
            resp = retried

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise _reraise_with_body(e, machine.name) from e
    result = resp.json()
    # #324: attach the resolved provider name to the response dict so callers
    # that record the dispatched assignment (cli.py, dashboard/server.py) can
    # persist it without re-resolving the config precedence chain.
    result["_provider_name"] = effective_provider_name
    return result


def dispatch_with_retry(
    proposal: Proposal,
    config: Config,
    *,
    max_retries: int = 3,
    backoff_base: float = 60.0,
    pull_repos: Iterable[str] = (),
    fresh_branch: bool = False,
    on_retry: callable | None = None,
) -> dict:
    """Dispatch with exponential backoff on transient failures."""
    from coord.network import classify_error, is_retryable

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return dispatch(proposal, config, pull_repos=pull_repos, fresh_branch=fresh_branch)
        except httpx.HTTPError as exc:
            state, reason = classify_error(exc)
            if not is_retryable(state) or attempt == max_retries:
                raise
            wait = backoff_base * (2 ** attempt)
            if on_retry:
                on_retry(attempt + 1, max_retries, state, reason, wait)
            time.sleep(wait)
            last_exc = exc
        except ValueError:
            raise
    raise last_exc  # unreachable, but satisfies type checker


def compute_do_not_touch(
    proposal: Proposal,
    peers: Iterable[Proposal],
    in_flight: Iterable[dict] = (),
) -> list[tuple[str, str]]:
    """Compute (file, reason) pairs for other work touching `proposal.repo_name`.

    `peers` are other proposals being dispatched in the same batch.
    `in_flight` are records loaded from ~/.coord/dispatched.json (each with
    keys: machine_name, repo_name, files_likely).
    """
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(machine_name: str, files: Iterable[str]) -> None:
        for f in files:
            key = (machine_name, f)
            if key in seen:
                continue
            seen.add(key)
            pairs.append((f, f"{machine_name} is working there"))

    for peer in peers:
        if peer is proposal:
            continue
        if peer.repo_name != proposal.repo_name:
            continue
        _add(peer.machine_name, peer.files_likely)

    for record in in_flight:
        if record.get("repo_name") != proposal.repo_name:
            continue
        if record.get("machine_name") == proposal.machine_name:
            continue
        _add(record.get("machine_name", "?"), record.get("files_likely", []))

    return pairs


def post_briefing(
    proposal: Proposal,
    config: Config,
    *,
    assignment_id: str = "pending",
    do_not_touch: Iterable[tuple[str, str]] = (),
) -> None:
    """Post the assignment briefing as a GitHub issue comment."""
    repo = config.repo(proposal.repo_name)
    if repo is None:
        raise ValueError(f"Unknown repo: {proposal.repo_name!r}")

    body = format_briefing(
        assignment_id=assignment_id,
        machine_name=proposal.machine_name,
        repo_name=proposal.repo_name,
        issue_number=proposal.issue_number,
        briefing=proposal.briefing,
        files_likely=proposal.files_likely,
        do_not_touch=do_not_touch,
    )
    github_ops.post_issue_comment(repo.github, proposal.issue_number, body)

    # Auto-tag the issue with pipeline_tracked_labels so the TUI's Pipeline
    # panel picks it up on the next `gh search issues` poll.  Without this,
    # manually filed issues stay invisible until the user remembers to
    # label them (we hit this filing quadraui#263).  Best-effort — never
    # fail the briefing post on a labeling error.
    tracked = config.pipeline.tracked_labels()
    if tracked:
        try:
            github_ops.add_issue_labels(repo.github, proposal.issue_number, tracked)
        except (RuntimeError, OSError):
            pass


def post_completion(
    *,
    assignment_id: str,
    machine_name: str,
    repo_github: str,
    repo_name: str,
    issue_number: int,
    exit_code: int,
    duration_seconds: float | None = None,
    log_path: str | None = None,
    summary: str = "",
) -> None:
    body = format_completion(
        assignment_id=assignment_id,
        machine_name=machine_name,
        repo_name=repo_name,
        issue_number=issue_number,
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        log_path=log_path,
        summary=summary,
    )
    github_ops.post_issue_comment(repo_github, issue_number, body)


def post_failure(
    *,
    assignment_id: str,
    machine_name: str,
    repo_github: str,
    repo_name: str,
    issue_number: int,
    exit_code: int | None,
    duration_seconds: float | None = None,
    log_path: str | None = None,
    error: str = "",
) -> None:
    body = format_failure(
        assignment_id=assignment_id,
        machine_name=machine_name,
        repo_name=repo_name,
        issue_number=issue_number,
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        log_path=log_path,
        error=error,
    )
    github_ops.post_issue_comment(repo_github, issue_number, body)


def post_advisory(
    *,
    assignment_id: str,
    machine_name: str,
    repo_github: str,
    repo_name: str,
    issue_number: int,
    duration_seconds: float | None = None,
    log_path: str | None = None,
    reason: str = "",
) -> None:
    body = format_advisory(
        assignment_id=assignment_id,
        machine_name=machine_name,
        repo_name=repo_name,
        issue_number=issue_number,
        duration_seconds=duration_seconds,
        log_path=log_path,
        reason=reason,
    )
    github_ops.post_issue_comment(repo_github, issue_number, body)


def post_refused_policy(
    *,
    assignment_id: str,
    machine_name: str,
    repo_github: str,
    repo_name: str,
    issue_number: int,
    duration_seconds: float | None = None,
    log_path: str | None = None,
    reason: str = "",
) -> None:
    body = format_refused_policy(
        assignment_id=assignment_id,
        machine_name=machine_name,
        repo_name=repo_name,
        issue_number=issue_number,
        duration_seconds=duration_seconds,
        log_path=log_path,
        reason=reason,
    )
    github_ops.post_issue_comment(repo_github, issue_number, body)
