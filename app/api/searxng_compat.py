"""SearXNG-compatible facade for Hob / other SearXNG clients.

Exposes a read-only GET endpoint that speaks the SearXNG search API
(`GET /search?q=...&format=json&safesearch=0`) while internally reusing the
Firecrawl search forwarder (key pool, client governance, quota, audit).

Design notes
------------
* Opt-in: routes are only mounted when `providers.searxng.enabled` is true,
  so existing deployments keep an unchanged route table.
* No new upstream client: requests reuse `Forwarder.forward(provider="firecrawl")`
  and therefore inherit key rotation, retries, rate limiting and audit logging.
* Auth: the same client token the Firecrawl data plane already requires
  (`Authorization: Bearer <client token>`).
* Response: `{"results": [{"title", "url", "content"}]}` — SearXNG's JSON shape.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.api.deps import enforce_client_governance, get_db, require_client
from app.db.models import Client

logger = logging.getLogger(__name__)

# Primary mount point; the SearXNG client's base_url should end with /searxng.
router = APIRouter(prefix="/searxng", tags=["searxng-compat"])

# Optional bare /search mount for clients that insist on a root path.
root_router = APIRouter(tags=["searxng-compat"])

MIN_RESULTS = 1
MAX_RESULTS = 50
MAX_QUERY_LENGTH = 2000


def _result_limit(request: Request) -> int:
    configured = getattr(request.app.state.config.providers.searxng, "result_limit", 10)
    try:
        value = int(configured)
    except (TypeError, ValueError):
        value = 10
    return max(MIN_RESULTS, min(value, MAX_RESULTS))


def to_searxng_payload(payload: Any) -> dict[str, list[dict[str, str]]]:
    """Convert a Firecrawl search payload into the SearXNG JSON shape."""
    items = payload.get("data") if isinstance(payload, dict) else None
    results: list[dict[str, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or ""
        if not url:
            continue
        results.append(
            {
                "title": item.get("title") or "",
                "url": url,
                "content": item.get("description") or item.get("snippet") or "",
            }
        )
    return {"results": results}


def _searxng_search(
    request: Request,
    query: str,
    response_format: str,
    safesearch: str,
    client: Client,
    db: Session,
) -> Response:
    limit = _result_limit(request)

    result = request.app.state.forwarder.forward(
        db=db,
        request_id=request.state.request_id,
        client=client,
        method="POST",
        upstream_path="/v1/search",
        json_body={"query": query, "limit": limit},
        inbound_headers=dict(request.headers),
    )
    request.state.api_key_id = result.api_key_id
    request.state.retry_count = result.retry_count
    request.state.endpoint = "searxng_search"

    upstream = result.response
    if upstream.status_code >= 400:
        # Keep FCAM's error envelope (always JSON) so clients surface the reason.
        return upstream

    try:
        payload = json.loads(upstream.body.decode("utf-8")) if upstream.body else {}
    except (ValueError, UnicodeDecodeError):
        logger.warning(
            "searxng.upstream_not_json",
            extra={"fields": {"request_id": request.state.request_id}},
        )
        return JSONResponse(
            {"results": [], "error": "upstream returned a non-JSON body"},
            status_code=502,
        )

    body = to_searxng_payload(payload)
    logger.info(
        "searxng.search_ok",
        extra={
            "fields": {
                "request_id": request.state.request_id,
                "limit": limit,
                "results": len(body["results"]),
                "format": response_format,
                "safesearch": safesearch,
            }
        },
    )
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@router.get("/search", dependencies=[Depends(enforce_client_governance)])
def searxng_search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=MAX_QUERY_LENGTH, description="Search query"),
    response_format: str = Query("json", alias="format", description="SearXNG output format"),
    safesearch: str = Query("0", description="SearXNG safesearch level (accepted as-is)"),
    client: Client = Depends(require_client),
    db: Session = Depends(get_db),
) -> Response:
    """SearXNG-compatible search backed by the Firecrawl key pool."""
    return _searxng_search(request, q, response_format, safesearch, client, db)


@root_router.get(
    "/search",
    dependencies=[Depends(enforce_client_governance)],
    include_in_schema=False,
)
def searxng_search_root(
    request: Request,
    q: str = Query(..., min_length=1, max_length=MAX_QUERY_LENGTH, description="Search query"),
    response_format: str = Query("json", alias="format", description="SearXNG output format"),
    safesearch: str = Query("0", description="SearXNG safesearch level (accepted as-is)"),
    client: Client = Depends(require_client),
    db: Session = Depends(get_db),
) -> Response:
    """Same handler mounted at /search for clients that require a root path."""
    return _searxng_search(request, q, response_format, safesearch, client, db)
