"""Tests for #3295's client-side conditional GET.

``coord.client.fetch_board_payload`` now keeps a ``(etag, body)`` pair per
service URL and sends ``If-None-Match`` on every subsequent call, reusing the
cached body on a ``304`` instead of re-shipping/re-parsing the full board
(measured at 3.5MB) every time a thin-client dashboard reads it. The daemon
side of this (``ETag`` on ``GET /board``, honouring ``If-None-Match``) has
existed since #1336 — nothing server-side changes here, only this client now
uses it.

See ``tests/test_dashboard_server.py`` for #3295's other, independent half:
the per-process board memo in ``coord/dashboard/server.py`` that fixes the
non-thin-client (daemon-host) deployment.
"""

from __future__ import annotations

import httpx
import pytest

import coord.client as cc


@pytest.fixture(autouse=True)
def _clean_board_payload_cache():
    """Isolate each test from every other's cached ETag/body.

    Not relying solely on the (already autouse, repo-wide) conftest fixture
    of the same shape — this module's own tests are the direct spec for the
    cache, so they assert their pre/post state explicitly rather than
    trusting a fixture defined elsewhere.
    """
    cc.reset_board_payload_cache()
    yield
    cc.reset_board_payload_cache()


class _FakeResponse:
    """Minimal stand-in for ``httpx.Response`` — just what
    ``fetch_board_payload`` reads: ``status_code``, ``headers``,
    ``raise_for_status()``, ``json()``.
    """

    def __init__(self, *, status_code: int = 200, json_body=None, etag: str | None = None):
        self.status_code = status_code
        self._json_body = json_body
        self.headers: dict[str, str] = {"ETag": etag} if etag else {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)

    def json(self):
        return self._json_body


def _svc(url: str = "http://daemon:7435") -> cc.ServiceConfig:
    return cc.ServiceConfig(url=url)


class TestFetchBoardPayloadConditionalGet:
    def test_first_call_for_a_url_sends_no_if_none_match(self, monkeypatch) -> None:
        captured: dict = {}

        def fake_get(url, *, headers=None, **kw):
            captured["headers"] = headers
            return _FakeResponse(json_body={"round_number": 1}, etag='"v1"')

        monkeypatch.setattr(cc.httpx, "get", fake_get)

        body = cc.fetch_board_payload(_svc())

        assert body == {"round_number": 1}
        assert "If-None-Match" not in captured["headers"]

    def test_second_call_sends_the_etag_cached_from_the_first(self, monkeypatch) -> None:
        headers_sent: list[dict] = []

        def fake_get(url, *, headers=None, **kw):
            headers_sent.append(headers)
            return _FakeResponse(json_body={"round_number": 1}, etag='"v1"')

        monkeypatch.setattr(cc.httpx, "get", fake_get)
        svc = _svc()

        cc.fetch_board_payload(svc)
        cc.fetch_board_payload(svc)

        assert len(headers_sent) == 2
        assert "If-None-Match" not in headers_sent[0]
        assert headers_sent[1]["If-None-Match"] == '"v1"'

    def test_304_reuses_the_cached_body_and_never_touches_json(self, monkeypatch) -> None:
        """The whole point: a validated-unchanged response must not pay for
        parsing a (potentially multi-MB) body it doesn't have."""
        headers_sent: list[dict] = []
        first_body = {"round_number": 1, "assignments": ["x"] * 50}

        def fake_get(url, *, headers=None, **kw):
            headers_sent.append(headers)
            if len(headers_sent) == 1:
                return _FakeResponse(json_body=first_body, etag='"v1"')

            def _must_not_be_called():
                raise AssertionError("json() called on a 304 — body was refetched")

            resp = _FakeResponse(status_code=304)
            resp.json = _must_not_be_called
            return resp

        monkeypatch.setattr(cc.httpx, "get", fake_get)
        svc = _svc()

        first = cc.fetch_board_payload(svc)
        second = cc.fetch_board_payload(svc)

        assert first == first_body
        assert second == first_body
        assert headers_sent[1]["If-None-Match"] == '"v1"'

    def test_a_new_etag_on_200_replaces_the_cached_one(self, monkeypatch) -> None:
        responses = [
            _FakeResponse(json_body={"round_number": 1}, etag='"v1"'),
            _FakeResponse(json_body={"round_number": 2}, etag='"v2"'),
        ]
        headers_sent: list[dict] = []

        def fake_get(url, *, headers=None, **kw):
            headers_sent.append(headers)
            return responses.pop(0)

        monkeypatch.setattr(cc.httpx, "get", fake_get)
        svc = _svc()

        first = cc.fetch_board_payload(svc)
        second = cc.fetch_board_payload(svc)

        assert first == {"round_number": 1}
        assert second == {"round_number": 2}
        assert headers_sent[1]["If-None-Match"] == '"v1"'

        # A third call must send the NEW etag ("v2"), never the stale "v1" —
        # proves the cache was actually replaced, not just appended to.
        def fake_get_third(url, *, headers=None, **kw):
            headers_sent.append(headers)
            return _FakeResponse(json_body={"round_number": 2}, etag='"v2"')

        monkeypatch.setattr(cc.httpx, "get", fake_get_third)
        cc.fetch_board_payload(svc)
        assert headers_sent[2]["If-None-Match"] == '"v2"'

    def test_cache_is_scoped_per_service_url(self, monkeypatch) -> None:
        """Two different daemons (or a test's two fake ones) must never
        share a cache entry, or a client pointed at daemon B could send
        daemon A's ETag and silently trust a body that was never daemon B's."""

        def fake_get(url, *, headers=None, **kw):
            etag = '"a-1"' if url.startswith("http://a:") else '"b-1"'
            return _FakeResponse(json_body={"url": url}, etag=etag)

        monkeypatch.setattr(cc.httpx, "get", fake_get)

        cc.fetch_board_payload(_svc("http://a:7435"))
        cc.fetch_board_payload(_svc("http://b:7435"))

        assert cc._board_payload_cache["http://a:7435"][0] == '"a-1"'
        assert cc._board_payload_cache["http://b:7435"][0] == '"b-1"'

    def test_response_without_an_etag_is_not_cached(self, monkeypatch) -> None:
        def fake_get(url, *, headers=None, **kw):
            return _FakeResponse(json_body={"round_number": 1})  # no ETag

        monkeypatch.setattr(cc.httpx, "get", fake_get)

        cc.fetch_board_payload(_svc())

        assert "http://daemon:7435" not in cc._board_payload_cache

    def test_a_304_with_no_prior_cache_raises_instead_of_guessing(self, monkeypatch) -> None:
        """A #2096 "gate can fail" check on the cache itself: without this
        guard, an unconditional GET answered 304 (a daemon bug — this client
        never sends `If-None-Match` with nothing cached) would fall through
        to `resp.json()` on a bodyless response and silently return `None`
        — indistinguishable from "board successfully fetched, contents:
        nothing". Refusing outright is the honest failure."""

        def fake_get(url, *, headers=None, **kw):
            assert "If-None-Match" not in (headers or {})
            return _FakeResponse(status_code=304)

        monkeypatch.setattr(cc.httpx, "get", fake_get)

        with pytest.raises(RuntimeError):
            cc.fetch_board_payload(_svc())

    def test_reset_board_payload_cache_clears_every_entry(self, monkeypatch) -> None:
        def fake_get(url, *, headers=None, **kw):
            return _FakeResponse(json_body={"round_number": 1}, etag='"v1"')

        monkeypatch.setattr(cc.httpx, "get", fake_get)
        cc.fetch_board_payload(_svc())
        assert cc._board_payload_cache

        cc.reset_board_payload_cache()

        assert cc._board_payload_cache == {}
