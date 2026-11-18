"""Integration tests for the opt-in SearXNG-compatible facade.

Covers:
- Route table untouched when `providers.searxng.enabled` is false
- Client-token auth, SearXNG response shape, upstream Firecrawl contract
- Upstream error passthrough and non-JSON handling
- Optional bare /search mount
- Audit endpoint inference for the /searxng/ prefix
- Existing /v1/search keeps working with the facade enabled
"""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from app.core.security import derive_master_key_bytes, hmac_sha256_hex
from app.middleware import _infer_api_endpoint

FIRECRAWL_PAYLOAD = {
    "success": True,
    "data": [
        {"title": "Hob", "url": "https://example.com/hob", "description": "self-hosted assistant"},
        {"title": "", "url": "https://example.com/blank", "description": ""},
        {"title": "no-url", "description": "should be skipped"},
    ],
}


def _enable(limit: int = 10, root: bool = False):
    def _mutate(config) -> None:
        config.providers.searxng.enabled = True
        config.providers.searxng.result_limit = limit
        config.providers.searxng.expose_root_search = root

    return _mutate


def _seed(app, seed_client, seed_api_key, *, api_key_plain: str = "fc-test-key"):
    db = app.state.db_session_factory()
    client, token = seed_client(db, master_key="master")
    mk = derive_master_key_bytes("master")
    seed_api_key(
        db,
        master_key="master",
        api_key_plain=api_key_plain,
        api_key_hash=hmac_sha256_hex(mk, api_key_plain),
        last4=api_key_plain[-4:],
        client_id=client.id,
    )
    db.close()
    return token


class TestInferApiEndpoint:
    def test_searxng_search(self) -> None:
        assert _infer_api_endpoint("/searxng/search") == "searxng_search"

    def test_firecrawl_and_exa_unchanged(self) -> None:
        assert _infer_api_endpoint("/v1/search") == "search"
        assert _infer_api_endpoint("/exa/search") == "exa_search"

    def test_unknown(self) -> None:
        assert _infer_api_endpoint("/search") is None


class TestDisabledByDefault:
    def test_prefixed_route_absent(self, tmp_path, make_app) -> None:
        app, _cfg, _sec = make_app(tmp_path)
        tc = TestClient(app, raise_server_exceptions=False)
        assert tc.get("/searxng/search", params={"q": "hob"}).status_code == 404

    def test_root_route_absent(self, tmp_path, make_app) -> None:
        app, _cfg, _sec = make_app(tmp_path)
        tc = TestClient(app, raise_server_exceptions=False)
        assert tc.get("/search", params={"q": "hob"}).status_code == 404


class TestSearxngSearch:
    def test_requires_client_token(self, tmp_path, make_app) -> None:
        app, _cfg, _sec = make_app(
            tmp_path,
            handler=lambda req: httpx.Response(200, json=FIRECRAWL_PAYLOAD),
            config_mutate=_enable(),
        )
        tc = TestClient(app, raise_server_exceptions=False)
        assert tc.get("/searxng/search", params={"q": "hob"}).status_code == 401

    def test_returns_searxng_shape(self, tmp_path, make_app, seed_client, seed_api_key) -> None:
        seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(200, json=FIRECRAWL_PAYLOAD)

        app, _cfg, _sec = make_app(tmp_path, handler=handler, config_mutate=_enable(limit=7))
        token = _seed(app, seed_client, seed_api_key)
        tc = TestClient(app, raise_server_exceptions=False)

        resp = tc.get(
            "/searxng/search",
            params={"q": "hob", "format": "json", "safesearch": "0"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert list(body.keys()) == ["results"]
        assert body["results"] == [
            {"title": "Hob", "url": "https://example.com/hob", "content": "self-hosted assistant"},
            {"title": "", "url": "https://example.com/blank", "content": ""},
        ]
        assert resp.headers.get("cache-control") == "no-store"

        assert seen, "upstream not called"
        assert seen[0].url.path.endswith("/search")
        assert json.loads(seen[0].content) == {"query": "hob", "limit": 7}

    def test_limit_is_clamped(self, tmp_path, make_app, seed_client, seed_api_key) -> None:
        seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(200, json={"success": True, "data": []})

        app, cfg, _sec = make_app(tmp_path, handler=handler, config_mutate=_enable())
        cfg.providers.searxng.result_limit = 999
        token = _seed(app, seed_client, seed_api_key)
        tc = TestClient(app, raise_server_exceptions=False)
        resp = tc.get(
            "/searxng/search",
            params={"q": "hob"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert json.loads(seen[0].content)["limit"] == 50

    def test_upstream_error_passthrough(self, tmp_path, make_app, seed_client, seed_api_key) -> None:
        app, _cfg, _sec = make_app(
            tmp_path,
            handler=lambda req: httpx.Response(402, json={"success": False, "error": "payment required"}),
            config_mutate=_enable(),
        )
        token = _seed(app, seed_client, seed_api_key)
        tc = TestClient(app, raise_server_exceptions=False)
        resp = tc.get(
            "/searxng/search",
            params={"q": "hob"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code >= 400
        assert resp.headers.get("content-type", "").startswith("application/json")

    def test_upstream_non_json_yields_502(self, tmp_path, make_app, seed_client, seed_api_key) -> None:
        app, _cfg, _sec = make_app(
            tmp_path,
            handler=lambda req: httpx.Response(200, text="<html>not json</html>"),
            config_mutate=_enable(),
        )
        token = _seed(app, seed_client, seed_api_key)
        tc = TestClient(app, raise_server_exceptions=False)
        resp = tc.get(
            "/searxng/search",
            params={"q": "hob"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 502
        assert resp.json()["results"] == []

    def test_missing_query_is_422(self, tmp_path, make_app, seed_client, seed_api_key) -> None:
        app, _cfg, _sec = make_app(
            tmp_path,
            handler=lambda req: httpx.Response(200, json={"data": []}),
            config_mutate=_enable(),
        )
        token = _seed(app, seed_client, seed_api_key)
        tc = TestClient(app, raise_server_exceptions=False)
        resp = tc.get("/searxng/search", headers={"Authorization": f"Bearer {token}"})
        # FCAM maps RequestValidationError to 400 VALIDATION_ERROR (not 422).
        assert resp.status_code == 400
        assert resp.headers.get("content-type", "").startswith("application/json")


class TestRootMount:
    def test_root_search_when_enabled(self, tmp_path, make_app, seed_client, seed_api_key) -> None:
        app, _cfg, _sec = make_app(
            tmp_path,
            handler=lambda req: httpx.Response(200, json=FIRECRAWL_PAYLOAD),
            config_mutate=_enable(root=True),
        )
        token = _seed(app, seed_client, seed_api_key)
        tc = TestClient(app, raise_server_exceptions=False)
        resp = tc.get("/search", params={"q": "hob"}, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert len(resp.json()["results"]) == 2

    def test_prefixed_route_still_works(self, tmp_path, make_app, seed_client, seed_api_key) -> None:
        app, _cfg, _sec = make_app(
            tmp_path,
            handler=lambda req: httpx.Response(200, json=FIRECRAWL_PAYLOAD),
            config_mutate=_enable(root=True),
        )
        token = _seed(app, seed_client, seed_api_key)
        tc = TestClient(app, raise_server_exceptions=False)
        resp = tc.get("/searxng/search", params={"q": "hob"}, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200


class TestFirecrawlRegression:
    def test_v1_search_still_works(self, tmp_path, make_app, seed_client, seed_api_key) -> None:
        app, _cfg, _sec = make_app(
            tmp_path,
            handler=lambda req: httpx.Response(200, json=FIRECRAWL_PAYLOAD),
            config_mutate=_enable(),
        )
        token = _seed(app, seed_client, seed_api_key)
        tc = TestClient(app, raise_server_exceptions=False)
        resp = tc.post(
            "/v1/search",
            json={"query": "hob"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
