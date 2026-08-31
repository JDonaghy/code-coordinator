"""#2179: coord.portal_bridge — the outbound client for coord-portal's sync bridge."""

from __future__ import annotations

import httpx
import pytest

from coord.config import PortalConfig
from coord.portal_bridge import (
    BridgeUpdate,
    PortalBridgeClient,
    PortalBridgeError,
    SUBMISSION_STATUSES,
    client_from_config,
)


def _client(**overrides):
    defaults = dict(
        base_url="https://intake.heurontech.com",
        client_id="id-123",
        client_secret="secret-456",
        max_retries=0,
        retry_backoff_secs=0.0,
    )
    defaults.update(overrides)
    return PortalBridgeClient(**defaults)


class _Response:
    def __init__(self, status_code=200, json_body=None, text="") -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = text or (str(json_body) if json_body is not None else "")

    def json(self):
        if self._json_body is None:
            raise ValueError("no body")
        return self._json_body


# ── push ─────────────────────────────────────────────────────────────────


def test_push_sends_the_documented_wire_shape(monkeypatch):
    seen = {}

    def _post(url, json=None, headers=None, timeout=None):
        seen["url"] = url
        seen["json"] = json
        seen["headers"] = headers
        return _Response(200, {"results": [{"submission_id": "sub_1", "outcome": "applied"}]})

    monkeypatch.setattr("httpx.post", _post)
    client = _client()
    results = client.push([BridgeUpdate(submission_id="sub_1", revision=3, fields={"status": "shipped"})])

    assert seen["url"] == "https://intake.heurontech.com/api/bridge/push"
    assert seen["json"] == {
        "updates": [{"submission_id": "sub_1", "revision": 3, "fields": {"status": "shipped"}}]
    }
    assert seen["headers"]["CF-Access-Client-Id"] == "id-123"
    assert seen["headers"]["CF-Access-Client-Secret"] == "secret-456"
    assert results[0].submission_id == "sub_1"
    assert results[0].outcome == "applied"
    assert results[0].ok is True


def test_push_status_rejects_a_status_outside_the_pinned_vocabulary():
    client = _client()
    with pytest.raises(PortalBridgeError, match="pinned portal status vocabulary"):
        client.push_status("sub_1", 1, "not-a-real-status")


def test_push_status_accepts_every_pinned_status(monkeypatch):
    def _post(url, json=None, headers=None, timeout=None):
        fields = json["updates"][0]["fields"]
        return _Response(200, {"results": [{"submission_id": "sub_1", "outcome": "applied"}]})

    monkeypatch.setattr("httpx.post", _post)
    client = _client()
    for status in SUBMISSION_STATUSES:
        result = client.push_status("sub_1", 1, status)
        assert result.ok


def test_push_empty_list_is_a_noop_no_request_sent(monkeypatch):
    def _post(*a, **k):
        raise AssertionError("push([]) must not make a request")

    monkeypatch.setattr("httpx.post", _post)
    assert _client().push([]) == []


def test_push_over_the_batch_cap_is_refused_locally_without_a_request(monkeypatch):
    def _post(*a, **k):
        raise AssertionError("an oversized batch must be refused before any request")

    monkeypatch.setattr("httpx.post", _post)
    updates = [
        BridgeUpdate(submission_id=f"sub_{i}", revision=1, fields={"status": "shipped"})
        for i in range(51)
    ]
    with pytest.raises(PortalBridgeError, match="caps a batch at 50"):
        _client().push(updates)


def test_rejected_outcome_is_not_an_exception(monkeypatch):
    """A per-item `rejected` is a real answer from the portal (bad field,
    invalid status value, ...), not a transport failure — the caller decides
    what to do with it, so push() must not raise for this."""

    def _post(url, json=None, headers=None, timeout=None):
        return _Response(
            200,
            {"results": [{"submission_id": "sub_1", "outcome": "rejected", "reason": "invalid_value:status"}]},
        )

    monkeypatch.setattr("httpx.post", _post)
    result = _client().push_status("sub_1", 1, "shipped")
    assert result.outcome == "rejected"
    assert result.reason == "invalid_value:status"
    assert result.ok is False


# ── failure posture: 401 never retries, 5xx/transport errors do ──────────


def test_401_raises_immediately_without_retrying(monkeypatch):
    calls = []

    def _post(url, json=None, headers=None, timeout=None):
        calls.append(1)
        return _Response(401)

    monkeypatch.setattr("httpx.post", _post)
    client = _client(max_retries=3)
    with pytest.raises(PortalBridgeError, match="401"):
        client.push_status("sub_1", 1, "shipped")
    assert len(calls) == 1


def test_5xx_is_retried_then_raises_if_still_failing(monkeypatch):
    calls = []

    def _post(url, json=None, headers=None, timeout=None):
        calls.append(1)
        return _Response(503, text="upstream down")

    monkeypatch.setattr("httpx.post", _post)
    client = _client(max_retries=2, retry_backoff_secs=0.0)
    with pytest.raises(PortalBridgeError, match="503"):
        client.push_status("sub_1", 1, "shipped")
    assert len(calls) == 3  # initial attempt + 2 retries


def test_5xx_succeeds_after_a_retry(monkeypatch):
    calls = []

    def _post(url, json=None, headers=None, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            return _Response(503, text="upstream down")
        return _Response(200, {"results": [{"submission_id": "sub_1", "outcome": "applied"}]})

    monkeypatch.setattr("httpx.post", _post)
    client = _client(max_retries=2, retry_backoff_secs=0.0)
    result = client.push_status("sub_1", 1, "shipped")
    assert result.ok
    assert len(calls) == 2


def test_transport_error_is_retried_then_raises(monkeypatch):
    def _post(url, json=None, headers=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("httpx.post", _post)
    client = _client(max_retries=1, retry_backoff_secs=0.0)
    with pytest.raises(PortalBridgeError, match="transport error"):
        client.push_status("sub_1", 1, "shipped")


def test_other_4xx_raises_without_retrying(monkeypatch):
    calls = []

    def _post(url, json=None, headers=None, timeout=None):
        calls.append(1)
        return _Response(400, text="invalid_request")

    monkeypatch.setattr("httpx.post", _post)
    client = _client(max_retries=3)
    with pytest.raises(PortalBridgeError, match="400"):
        client.push_status("sub_1", 1, "shipped")
    assert len(calls) == 1


def test_non_json_response_raises(monkeypatch):
    def _post(url, json=None, headers=None, timeout=None):
        return _Response(200, json_body=None)

    monkeypatch.setattr("httpx.post", _post)
    with pytest.raises(PortalBridgeError, match="non-JSON"):
        _client().push_status("sub_1", 1, "shipped")


# ── heartbeat ────────────────────────────────────────────────────────────


def test_heartbeat_sends_an_iso_timestamp_and_reports_ok(monkeypatch):
    seen = {}

    def _post(url, json=None, headers=None, timeout=None):
        seen["url"] = url
        seen["json"] = json
        return _Response(200, {"ok": True})

    monkeypatch.setattr("httpx.post", _post)
    client = _client()
    assert client.heartbeat() is True
    assert seen["url"] == "https://intake.heurontech.com/api/bridge/heartbeat"
    at = seen["json"]["at"]
    assert at.endswith("Z")
    assert "T" in at


def test_heartbeat_accepts_an_explicit_timestamp(monkeypatch):
    seen = {}

    def _post(url, json=None, headers=None, timeout=None):
        seen["json"] = json
        return _Response(200, {"ok": True})

    monkeypatch.setattr("httpx.post", _post)
    client = _client()
    client.heartbeat(at="2026-08-13T12:00:00Z")
    assert seen["json"] == {"at": "2026-08-13T12:00:00Z"}


# ── pull ─────────────────────────────────────────────────────────────────


def test_pull_sends_cursor_and_limit_as_query_params(monkeypatch):
    seen = {}

    def _get(url, params=None, headers=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        return _Response(200, {"events": [], "cursor": "abc", "has_more": False})

    monkeypatch.setattr("httpx.get", _get)
    client = _client()
    data = client.pull(cursor="abc", limit=10)
    assert seen["url"] == "https://intake.heurontech.com/api/bridge/pull"
    assert seen["params"] == {"cursor": "abc", "limit": 10}
    assert data == {"events": [], "cursor": "abc", "has_more": False}


# ── upload_bundle (PDR-3, #2508) ────────────────────────────────────────


def test_upload_bundle_sends_files_and_returns_bundle_key(monkeypatch):
    seen = {}

    def _post(url, json=None, headers=None, timeout=None):
        seen["url"] = url
        seen["json"] = json
        return _Response(200, {"bundle_key": "bundles/sub_1/r1.tar"})

    monkeypatch.setattr("httpx.post", _post)
    client = _client()
    key = client.upload_bundle(
        "sub_1", {"contract.md": "# contract", "mocks/index.html": "<html></html>"}
    )

    assert seen["url"] == "https://intake.heurontech.com/api/bridge/upload"
    assert seen["json"] == {
        "submission_id": "sub_1",
        "files": {"contract.md": "# contract", "mocks/index.html": "<html></html>"},
    }
    assert key == "bundles/sub_1/r1.tar"


def test_upload_bundle_rejects_empty_files():
    client = _client()
    with pytest.raises(PortalBridgeError, match="empty files"):
        client.upload_bundle("sub_1", {})


def test_upload_bundle_raises_when_response_has_no_bundle_key(monkeypatch):
    monkeypatch.setattr("httpx.post", lambda *a, **k: _Response(200, {}))
    client = _client()
    with pytest.raises(PortalBridgeError, match="bundle_key"):
        client.upload_bundle("sub_1", {"contract.md": "# contract"})


# ── client_from_config ──────────────────────────────────────────────────


def test_client_from_config_returns_none_when_disabled():
    assert client_from_config(PortalConfig(enabled=False)) is None


def test_client_from_config_builds_a_client_when_enabled():
    cfg = PortalConfig(
        enabled=True,
        base_url="https://intake.heurontech.com",
        bridge_client_id="id-123",
        bridge_client_secret="secret-456",
        timeout_secs=7.0,
        max_retries=1,
    )
    client = client_from_config(cfg)
    assert isinstance(client, PortalBridgeClient)
    assert client.base_url == "https://intake.heurontech.com"
    assert client.timeout_secs == 7.0
    assert client.max_retries == 1


# ── BridgeUpdate validation ────────────────────────────────────────────


def test_bridge_update_rejects_empty_submission_id():
    with pytest.raises(PortalBridgeError, match="submission_id"):
        BridgeUpdate(submission_id="", revision=1, fields={"status": "shipped"})


def test_bridge_update_rejects_negative_revision():
    with pytest.raises(PortalBridgeError, match="revision"):
        BridgeUpdate(submission_id="sub_1", revision=-1, fields={"status": "shipped"})


def test_bridge_update_rejects_empty_fields():
    with pytest.raises(PortalBridgeError, match="fields"):
        BridgeUpdate(submission_id="sub_1", revision=1, fields={})


# ── COORD_OWNED_FIELDS (#2987) ──────────────────────────────────────────────


def test_relayed_answer_is_a_coord_owned_field():
    """#2987: a relayed answer (#2986) is pushed OUT under the top-level
    `relayed_answer` key (`coord.portal_sync.KIND_RELAYED_ANSWER`) — it must
    be in the ownership allowlist or every push of it degrades to
    `rejected:not_owned:relayed_answer` (see `_facts_from_payload`, which
    also relies on this set to keep coord's own field out of the customer
    mirror)."""
    from coord.portal_bridge import COORD_OWNED_FIELDS

    assert "relayed_answer" in COORD_OWNED_FIELDS
