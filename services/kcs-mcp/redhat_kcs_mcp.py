#!/usr/bin/env python3
"""
Red Hat KCS MCP server.

Exposes Red Hat Knowledgebase (KCS Solutions/Articles) search as MCP tools
over streamable-HTTP, so the orchestrator can reach it the same way it
reaches mcp-confluence-server and nbk-mcp - via _call_mcp()'s
initialize -> notifications/initialized -> tools/call HTTP handshake,
not a direct REST call in-process.

Auth: a Red Hat SSO offline/refresh token (RHKB_OFFLINE_TOKEN) is exchanged
for a short-lived access token against sso.redhat.com (grant_type=refresh_token,
client_id=rhsm-api), cached in-process until shortly before it expires. The
offline token itself is never sent anywhere except that SSO token endpoint.
Search hits the Hydra Solr API (access.redhat.com/hydra/rest/search/v2/kcs).
"""

import json
import os
import time

import httpx
from mcp.server.fastmcp import FastMCP

RH_API_BASE_URL = os.environ.get("RH_API_BASE_URL", "https://access.redhat.com").rstrip("/")
RH_SSO_URL = os.environ.get(
    "RH_SSO_URL",
    "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token",
)
RHKB_OFFLINE_TOKEN = os.environ.get("RHKB_OFFLINE_TOKEN", "")
if not RHKB_OFFLINE_TOKEN:
    raise RuntimeError("RHKB_OFFLINE_TOKEN environment variable is required")

_TOKEN_CACHE: dict = {"access_token": None, "expiry": 0.0}

mcp = FastMCP(
    "Red Hat KCS",
    instructions="Search and retrieve Red Hat Knowledgebase (KCS) Solutions and Articles.",
    host="0.0.0.0",
    port=8080,
)


def _get_access_token() -> str:
    now = time.time()
    if _TOKEN_CACHE["access_token"] and now < _TOKEN_CACHE["expiry"]:
        return _TOKEN_CACHE["access_token"]
    resp = httpx.post(
        RH_SSO_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": "rhsm-api",
            "refresh_token": RHKB_OFFLINE_TOKEN,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    access_token = data.get("access_token")
    if not access_token:
        raise RuntimeError("SSO token response had no access_token")
    _TOKEN_CACHE["access_token"] = access_token
    _TOKEN_CACHE["expiry"] = now + max(30, int(data.get("expires_in", 300)) - 60)
    return access_token


def _request(path: str, body: dict, timeout: float = 20.0) -> dict:
    token = _get_access_token()
    resp = httpx.post(
        f"{RH_API_BASE_URL}{path}",
        json=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


_SEARCH_EXPRESSION = (
    "sort=score%20DESC"
    "&fq=documentKind%3A(%22Article%22%20OR%20%22Solution%22)"
    "%20AND%20accessState%3A(%22active%22%20OR%20%22private%22)"
    "&fl=allTitle%2CcaseCount%2CdocumentKind%2C%5Belevated%5D"
    "%2ChasPublishedRevision%2Cid%2Clanguage%2ClastModifiedDate"
    "%2CModerationState%2Cscore%2Curi%2Cresource_uri%2Cview_uri%2CcreatedDate"
    "&showRetired=false"
)


def _as_text(value) -> str:
    """Issue/Resolution/RootCause come back as Solr multi-value fields - a
    list of paragraph strings, not a single string. Join them into plain
    text so MCP clients get clean prose, not a stringified Python list."""
    if isinstance(value, list):
        return " ".join(str(v) for v in value if v)
    return str(value) if value else ""


@mcp.tool()
def search_kcs(query: str, rows: int = 10, start: int = 0) -> str:
    """Search Red Hat KCS Solutions and Articles. Returns a JSON array string
    of {id, title, score, view_uri} objects - call get_kcs(id) for full
    content. (Returns a plain string, not a typed list, so this comes back
    as a single MCP content block instead of being split into one block per
    list item.)"""
    result = _request(
        "/hydra/rest/search/v2/kcs",
        {
            "q": query,
            "rows": rows,
            "start": start,
            "clientName": "debug-agent-kcs-mcp",
            "expression": _SEARCH_EXPRESSION,
        },
    )
    docs = (result.get("response") or {}).get("docs") or []
    items = [
        {
            "id": doc.get("id"),
            "title": doc.get("allTitle"),
            "score": doc.get("score"),
            "view_uri": doc.get("view_uri"),
        }
        for doc in docs
    ]
    return json.dumps(items)


@mcp.tool()
def get_kcs(solution_id: str) -> str:
    """Get a specific KCS solution by id and return a JSON object string with
    title, issue, resolution, root_cause, view_uri."""
    result = _request("/hydra/rest/search/v2/kcs", {"q": f"id:{solution_id}"})
    docs = (result.get("response") or {}).get("docs") or []
    if not docs:
        return json.dumps({"title": "", "issue": "", "resolution": "", "root_cause": "", "view_uri": ""})
    doc = docs[0]
    return json.dumps({
        "title": doc.get("publishedTitle") or doc.get("allTitle") or "",
        "issue": _as_text(doc.get("issue")),
        "resolution": _as_text(doc.get("solution_resolution")),
        "root_cause": _as_text(doc.get("solution_rootcause")),
        "view_uri": doc.get("view_uri") or "",
    })


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
