"""Outbound client for coord-portal's sync bridge (#2179).

**The gap this closes.** coord-portal has offered ``POST /api/bridge/push``
since ms-1, and the entire ms-3 mail pipeline (``docs/CUSTOMER_PORTAL.md``) is
live and waiting downstream of a queued outbox row — but nothing on this side
has ever called it. This module is that client: a thin wrapper over
coord-portal's bridge routes (``push``, ``pull``, ``heartbeat``, and — as of
PDR-3/#2508 — ``upload``), matching the wire contract coord-portal's
``src/bridge/*`` and ``src/routes/bridge.ts`` define. ``upload`` is the one
route that carries a bundle's raw content rather than D1 metadata — see
:meth:`PortalBridgeClient.upload_bundle`.

**What this module deliberately does NOT do**, per #2179's own framing —
those are separate, harder design questions and belong to follow-up issues:

* It does not decide **when** to push. Nothing here reads coord's board or
  watches assignment status transitions. Callers hand it
  ``(submission_id, revision, status)`` and it sends that; where those three
  values come from is out of scope here. The submission↔work association is
  now resolvable — :func:`coord.portal_store.get_milestone_link` reads the
  durable ``(repo, milestone_number) -> submission_id`` mapping an operator
  records with ``coord portal link`` (#2507) — but nothing in this module
  calls it: this client still just sends whatever ``submission_id`` a caller
  hands it. The mapping from coord's internal states to the portal's pinned
  customer vocabulary is a separate, still-unaddressed question.
* It does not persist a revision counter. The portal is idempotent by
  ``(submission_id, revision)`` and ignores anything at or below its stored
  watermark (see ``applyUpdate`` in coord-portal's ``src/bridge/updates.ts``),
  so a caller that loses track of "what revision did I last send" can safely
  resend a stale one — it will come back ``already_applied``, not corrupt
  anything. But *finding* the next revision to send is the caller's job.
* It does not run on a schedule. Wiring this into ``coord serve``'s tick
  loop, or any other cadence, is the caller's decision — see
  :class:`PortalBridgeClient` for the failure posture that makes it safe to
  do so.

**Failure posture.** The portal is a third party from coord's perspective
(``docs/CUSTOMER_PORTAL.md``, "The security posture"). A portal outage must
never block a merge or a dispatch. Every method here either returns a
value or raises :class:`PortalBridgeError` — it never raises anything else,
so a caller wiring this into a loop has exactly one exception type to catch.
Transport errors and 5xx responses are retried with backoff; a 401 (bad or
absent credentials) and 4xx responses are not — retrying a wrong credential
or a malformed request produces the same wrong answer forever.
"""

from __future__ import annotations

import datetime
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CLIENT_ID_HEADER = "CF-Access-Client-Id"
CLIENT_SECRET_HEADER = "CF-Access-Client-Secret"

# Mirrors coord-portal's MAX_PUSH_UPDATES (src/bridge/updates.ts) — a Worker
# limit made visible, not a contract term. Enforced here too so an oversized
# batch fails fast, locally, with a message that says why, instead of a
# generic 400 from the other side of the internet.
MAX_PUSH_UPDATES = 50

#: The pinned customer status vocabulary the portal accepts (Gate-A contract).
#: Keep in step with ``docs/CUSTOMER_PORTAL.md`` (§ Status vocabulary) and
#: coord-portal's ``src/submissions.ts`` ``SUBMISSION_STATUS_TEXT`` — a value
#: outside this set has no pill and no screen on the portal side, and
#: ``isSubmissionStatus`` rejects it as ``invalid_value:status``.
#:
#: ``post-shipped`` (#3106) is the one addition since ms-3: before it,
#: ``shipped`` was the terminal value in both directions
#: (:func:`coord.portal_sync._reentry_block_reason`), so a client whose
#: release had shipped could never be told about a later bug fix or small
#: enhancement — the fold either got stuck re-notifying ``shipped`` (a no-op,
#: #2588's churn guard) or was refused outright as an "un-ship" (#3096's
#: terminal-status guard). ``post-shipped`` covers exactly that: ongoing
#: maintenance against an already-shipped release, distinct from a full new
#: release cycle. This is a cross-repo change — coord owns the enum, the
#: fold (:func:`coord.portal_sync.fold_submission_status`) and the re-entry
#: guard, but coord-portal still has to learn to render and email this value
#: (``src/submissions.ts``'s ``SUBMISSION_STATUS_TEXT`` / ``isSubmissionStatus``)
#: before a push of it does anything visible on the customer's screen.
SUBMISSION_STATUSES = (
    "describing",
    "in-design",
    "awaiting-signoff",
    "planned",
    "in-progress",
    "quality-check",
    "needs-input",
    "on-hold",
    "shipped",
    "post-shipped",
)

#: Fields coord is the sole writer of (coord-portal's ``COORD_OWNED_FIELDS``,
#: ``src/bridge/ownership.ts``). Pushing anything outside this set is a
#: ``rejected:not_owned:<field>`` or ``rejected:unknown_field:<field>``
#: outcome, never a write — the portal enforces sole-writer ownership on its
#: side regardless of what this module lets through.
COORD_OWNED_FIELDS = (
    "status",
    "decomposition",
    "question",
    "design_round",
    "artifacts",
    "onhold_since",
    "preview_url",
    # #2987: a relayed answer coord recorded on the client's behalf
    # (`coord portal answer`, #2986), pushed OUT so the client can see and
    # confirm/correct it — the wire shape coord-portal#159 specifies.
    "relayed_answer",
)


class PortalBridgeError(RuntimeError):
    """A bridge call failed after retries, or was rejected outright.

    The one exception type every :class:`PortalBridgeClient` method raises.
    Callers on a schedule (a tick loop, a cron drain) should catch this,
    log/surface it, and move on — per #2179's failure posture, a portal
    outage must never be fatal to anything else coord does.
    """


@dataclass(frozen=True)
class BridgeUpdate:
    """One coord-owned fact for one submission, at one revision.

    Mirrors the wire shape of a single entry in ``POST /api/bridge/push``'s
    ``updates`` array (``src/bridge/updates.ts`` ``parseUpdate``).
    """

    submission_id: str
    revision: int
    fields: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.submission_id or not self.submission_id.strip():
            raise PortalBridgeError("BridgeUpdate.submission_id must be non-empty")
        if self.revision < 0:
            raise PortalBridgeError("BridgeUpdate.revision must be >= 0")
        if not self.fields:
            raise PortalBridgeError("BridgeUpdate.fields must be non-empty")

    def to_wire(self) -> dict[str, Any]:
        return {
            "submission_id": self.submission_id,
            "revision": self.revision,
            "fields": self.fields,
        }


@dataclass(frozen=True)
class PushResult:
    """One entry of ``POST /api/bridge/push``'s per-item ``results``."""

    submission_id: str
    outcome: str  # "applied" | "already_applied" | "rejected"
    reason: str | None = None

    @property
    def ok(self) -> bool:
        """True for both `applied` and `already_applied` — both mean the
        fleet's intent is now reflected on the portal (updates.ts's own
        framing of `already_applied` as "a success, not a no-op")."""
        return self.outcome in ("applied", "already_applied")


@dataclass(frozen=True)
class PortalBridgeClient:
    """Thin HTTP client for coord-portal's ``/api/bridge/*`` routes.

    Construct via :func:`client_from_config` rather than directly, so the
    "disabled means no client" rule lives in one place.
    """

    base_url: str
    client_id: str
    client_secret: str
    timeout_secs: float = 10.0
    max_retries: int = 2
    retry_backoff_secs: float = 1.0

    def _headers(self) -> dict[str, str]:
        return {
            CLIENT_ID_HEADER: self.client_id,
            CLIENT_SECRET_HEADER: self.client_secret,
            "Content-Type": "application/json",
        }

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = self.base_url.rstrip("/") + path
        attempts = self.max_retries + 1
        last_error: str | None = None
        for attempt in range(attempts):
            try:
                response = httpx.post(
                    url, json=body, headers=self._headers(), timeout=self.timeout_secs
                )
            except httpx.HTTPError as exc:
                last_error = f"transport error: {exc}"
                if attempt + 1 < attempts:
                    logger.warning(
                        "portal bridge %s: %s (attempt %d/%d, retrying)",
                        path, last_error, attempt + 1, attempts,
                    )
                    time.sleep(self.retry_backoff_secs * (attempt + 1))
                    continue
                raise PortalBridgeError(
                    f"POST {path} failed after {attempts} attempt(s): {last_error}"
                ) from exc

            if response.status_code == 401:
                # Never retried: a bad/absent credential pair is not a
                # transient condition. See coord-portal's
                # isBridgeAuthorized — it fails closed on exactly this.
                raise PortalBridgeError(
                    f"POST {path}: 401 unauthorized — check "
                    f"portal.bridge_client_id/bridge_client_secret"
                )
            if 500 <= response.status_code < 600:
                last_error = f"{response.status_code} {response.text[:200]!r}"
                if attempt + 1 < attempts:
                    logger.warning(
                        "portal bridge %s: %s (attempt %d/%d, retrying)",
                        path, last_error, attempt + 1, attempts,
                    )
                    time.sleep(self.retry_backoff_secs * (attempt + 1))
                    continue
                raise PortalBridgeError(
                    f"POST {path} failed after {attempts} attempt(s): {last_error}"
                )
            if response.status_code >= 400:
                raise PortalBridgeError(
                    f"POST {path}: {response.status_code} {response.text[:200]!r}"
                )

            try:
                return response.json()
            except ValueError as exc:
                raise PortalBridgeError(f"POST {path}: non-JSON response: {exc}") from exc

        # Unreachable: the loop above always either returns or raises.
        raise PortalBridgeError(f"POST {path}: exhausted retries: {last_error}")

    def push(self, updates: list[BridgeUpdate]) -> list[PushResult]:
        """``POST /api/bridge/push`` — send a batch of coord-owned facts.

        Returns one :class:`PushResult` per update, in request order. A
        per-item ``rejected`` outcome is NOT an exception — it is a real
        answer from the portal (e.g. an unowned field, or a status outside
        the pinned vocabulary) and the caller decides what to do with it.
        Only transport failures, 401s, and malformed responses raise
        :class:`PortalBridgeError`.
        """
        if not updates:
            return []
        if len(updates) > MAX_PUSH_UPDATES:
            raise PortalBridgeError(
                f"push() got {len(updates)} updates; the portal caps a batch "
                f"at {MAX_PUSH_UPDATES} (src/bridge/updates.ts MAX_PUSH_UPDATES) "
                f"— split it into multiple calls"
            )
        data = self._post("/api/bridge/push", {"updates": [u.to_wire() for u in updates]})
        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            raise PortalBridgeError(
                f"POST /api/bridge/push: response had no 'results' list: {data!r}"
            )
        return [
            PushResult(
                submission_id=str(r.get("submission_id", "")),
                outcome=str(r.get("outcome", "rejected")),
                reason=r.get("reason"),
            )
            for r in raw_results
        ]

    def push_status(self, submission_id: str, revision: int, status: str) -> PushResult:
        """Convenience wrapper: push a single ``status`` field update.

        This is the mail-pipeline path #2179 exists for — a status update is
        what ``src/notifications.ts`` (portal-side) turns into a queued
        outbox row.
        """
        if status not in SUBMISSION_STATUSES:
            raise PortalBridgeError(
                f"{status!r} is not in the pinned portal status vocabulary: "
                f"{SUBMISSION_STATUSES}"
            )
        results = self.push(
            [BridgeUpdate(submission_id=submission_id, revision=revision, fields={"status": status})]
        )
        return results[0]

    def heartbeat(self, at: str | None = None) -> bool:
        """``POST /api/bridge/heartbeat`` — say the daemon is alive.

        Without this the portal cannot distinguish a dead daemon from a slow
        one (``docs/CUSTOMER_PORTAL.md``). *at* defaults to now (UTC); pass
        it explicitly only in tests.
        """
        data = self._post("/api/bridge/heartbeat", {"at": at or _utcnow_iso()})
        return bool(data.get("ok"))

    def pull(self, cursor: str | None = None, limit: int | None = None) -> dict[str, Any]:
        """``GET /api/bridge/pull`` — customer-authored events since *cursor*.

        Included for completeness of the wire contract; #2179 is push-only
        (the mail pipeline needs status out, not events in). Raises
        :class:`PortalBridgeError` the same way the push/heartbeat paths do.
        """
        url = self.base_url.rstrip("/") + "/api/bridge/pull"
        params: dict[str, Any] = {}
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        try:
            response = httpx.get(
                url, params=params, headers=self._headers(), timeout=self.timeout_secs
            )
        except httpx.HTTPError as exc:
            raise PortalBridgeError(f"GET /api/bridge/pull failed: {exc}") from exc
        if response.status_code == 401:
            raise PortalBridgeError(
                "GET /api/bridge/pull: 401 unauthorized — check "
                "portal.bridge_client_id/bridge_client_secret"
            )
        if response.status_code >= 400:
            raise PortalBridgeError(
                f"GET /api/bridge/pull: {response.status_code} {response.text[:200]!r}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise PortalBridgeError(f"GET /api/bridge/pull: non-JSON response: {exc}") from exc

    def upload_bundle(self, submission_id: str, files: dict[str, str]) -> str:
        """``POST /api/bridge/upload`` — store a design round's rendered
        mock bundle + contract in the portal's R2 (coord-portal#120, PDR-2).

        Deliberately a fourth route, not folded into :meth:`push`: the
        three ``/api/bridge/*`` routes this module otherwise speaks
        (``push``/``pull``/``heartbeat``) carry only D1 metadata —
        :func:`coord.portal_sync.enqueue_design_round`'s own docstring
        already says the mock bundle itself "is not uploaded here". This is
        that upload, kept separate so a bundle (arbitrarily large HTML) is
        never accidentally routed through :data:`MAX_PUSH_UPDATES`-bounded
        ``push`` batching.

        *files* maps a path relative to the bundle root — e.g.
        ``"contract.md"``, ``"mocks/index.html"`` — to its text content; see
        :func:`coord.mock_author.collect_mock_bundle_files`, which builds
        exactly this mapping by reading a merged Gate-A branch back off
        GitHub. Returns the R2 object key the portal assigns the bundle —
        callers thread that straight into the ``design_round`` payload
        :func:`coord.portal_sync.enqueue_design_round` queues, so the
        customer's browser has something to fetch. Raises
        :class:`PortalBridgeError` the same way every other method here
        does: a transport failure, a 401, a 4xx/5xx, or a response with no
        usable ``bundle_key``.
        """
        if not files:
            raise PortalBridgeError("upload_bundle() got an empty files mapping")
        data = self._post(
            "/api/bridge/upload",
            {"submission_id": submission_id, "files": files},
        )
        bundle_key = data.get("bundle_key")
        if not isinstance(bundle_key, str) or not bundle_key:
            raise PortalBridgeError(
                f"POST /api/bridge/upload: response had no 'bundle_key' "
                f"string: {data!r}"
            )
        return bundle_key


def _utcnow_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def client_from_config(cfg: Any) -> PortalBridgeClient | None:
    """Build a :class:`PortalBridgeClient` from a :class:`coord.config.PortalConfig`.

    Returns ``None`` when the block is absent/disabled — the single place
    "not configured" turns into "there is no client", so every caller gets
    the same no-op behaviour rather than each re-deriving it from
    ``cfg.enabled``.
    """
    if not getattr(cfg, "enabled", False):
        return None
    # Config parsing (coord.config._parse_portal) already refuses an enabled
    # block missing any of these, so this is a defensive re-check, not the
    # primary guard.
    if not (cfg.base_url and cfg.bridge_client_id and cfg.bridge_client_secret):
        raise PortalBridgeError(
            "portal.enabled is true but base_url/bridge_client_id/"
            "bridge_client_secret are not all set"
        )
    return PortalBridgeClient(
        base_url=cfg.base_url,
        client_id=cfg.bridge_client_id,
        client_secret=cfg.bridge_client_secret,
        timeout_secs=cfg.timeout_secs,
        max_retries=cfg.max_retries,
    )
