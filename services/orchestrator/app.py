import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
import yaml
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from openai import OpenAI
from pydantic import BaseModel

logger = logging.getLogger("debug-agent-orchestrator")
logging.basicConfig(level=logging.INFO)

CONFIG_DIR = Path("/app/config")
RULE_FILE = "debugagent-hub.mdc"
PERSONA_FILE = "personas-slim.json"
TEMPLATE_FILE = "prompt-templates-slim.json"
TEAM_CONFIG_FILE = "team-config.json"

AUTH_SERVER = "https://auth.atlassian.com/VCeDsk8ZHncYF1g234fKtc4lNipbBhu3"
REGISTER_URL = AUTH_SERVER + "/dcr/register"
AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
MCP_URL = os.environ.get("ATLASSIAN_MCP_URL", "https://mcp.atlassian.com/v2/mcp")
RESOURCE = "https://mcp.atlassian.com/v2/mcp"
SCOPES = "read:confluence:agent-interface search:confluence:agent-interface offline_access"

CFL_MCP_URL = os.environ.get(
    "CONFLUENCE_MCP_URL",
    "http://mcp-confluence-service.debug-agent-mcp.svc.cluster.local:8080/mcp",
)

NBK_URL = os.environ.get("NOTEBOOKLM_MCP_URL", "http://127.0.0.1:8000/mcp")
NBK_MAX_NOTEBOOKS = int(os.environ.get("NOTEBOOKLM_MAX_NOTEBOOKS", "2"))
NBK_POLL_INTERVAL = float(os.environ.get("NOTEBOOKLM_POLL_INTERVAL", "5"))
NBK_MAX_POLLS = int(os.environ.get("NOTEBOOKLM_MAX_POLLS", "36"))  # ~3 min ceiling per notebook
# Firing every selected notebook at the nbk-mcp sidecar simultaneously has
# OOM-killed it (fixed memory limit) when NOTEBOOKLM_MAX_NOTEBOOKS allows
# more than a couple - bound actual parallelism independently of that cap.
NBK_MAX_PARALLEL = int(os.environ.get("NOTEBOOKLM_MAX_PARALLEL", "2"))

# Red Hat Knowledgebase (KCS) - bespoke MCP server (mcp-redhat-kcs-server),
# cross-namespace Service in debug-agent-mcp, same pattern as mcp-confluence-server.
# It owns the offline-token->access-token exchange against Red Hat SSO; the
# orchestrator only ever speaks MCP to it, never touches access.redhat.com directly.
KCS_MCP_URL = os.environ.get(
    "KCS_MCP_URL",
    "http://mcp-redhat-kcs-service.debug-agent-mcp.svc.cluster.local:8080/mcp",
)

# Slack - bespoke MCP server (redhat-community-ai-tools/slack-mcp), same
# cross-namespace Service pattern as the other three. See manifests/slack-mcp.yaml
# for why MCP_TRANSPORT=streamable-http is used instead of the upstream
# template's "sse" (that's the older two-endpoint transport, incompatible
# with _call_mcp()'s single-URL sequential-POST client).
SLK_MCP_URL = os.environ.get(
    "SLACK_MCP_URL",
    "http://mcp-slack-service.debug-agent-mcp.svc.cluster.local:8000/mcp",
)
# Optional - only used to build clickable Slack permalinks (e.g.
# https://redhat-internal.slack.com). Without it, Slack matches still work,
# just without a clickable URL (same graceful degradation as a CFL match
# with no url field).
SLK_WORKSPACE_URL = os.environ.get("SLACK_WORKSPACE_URL", "").rstrip("/")
# scripts/deploy-fresh-environment.sh defaults this env var to the literal
# sentinel string "__SLACK_WORKSPACE_URL__" when nobody running the deploy
# passed a real one - that string is non-empty, so it used to slip past the
# `if not SLK_WORKSPACE_URL` check in _slack_permalink() below and get used
# as-is, producing a schemeless "URL" like
# "__SLACK_WORKSPACE_URL__/archives/C123/p456". A browser treats a
# schemeless link as relative to whatever page it's clicked from, so this
# silently rendered as a broken, misleading link back into the Streamlit UI
# itself instead of Slack. Anything that isn't a real http(s) URL - the
# sentinel included - is treated as "not configured" here, same as empty.
if not SLK_WORKSPACE_URL.startswith(("http://", "https://")):
    SLK_WORKSPACE_URL = ""

app = FastAPI(title="Debug Agent Orchestrator")

def _clean_env(name: str) -> str:
    """Strip whitespace AND embedded \\r/\\n from an env var before use.
    Confirmed live on ocpv01: LITEMAAS_API_KEY picked up a trailing '\\r'
    (sourced from a Windows-edited manifests/secrets.local.env with CRLF
    line endings, applied before the CRLF-stripping guard existed in
    apply-secrets-from-config.sh), which made httpcore reject the
    Authorization header outright with "Illegal header value b'Bearer
    ...\\r'" - every RCA call failed, 3/3 retries, with ranked matches
    still rendering fine since that path never touches this client. A
    plain .strip() only catches leading/trailing whitespace including the
    outer \\r, but doesn't help if \\r ends up embedded mid-string for some
    other reason, so also strip \\r/\\n explicitly anywhere in the value."""
    return os.environ[name].replace("\r", "").replace("\n", "").strip()


def _litemaas_base_url() -> str:
    """Append /v1 to LITEMAAS_BASE_URL, but don't double it if whoever
    filled in secrets.local.env already pasted the full API endpoint
    (including /v1) instead of just the host - confirmed live on ocpv01:
    LITEMAAS_BASE_URL was set to '.../maas.redhatworkshops.io/v1', so
    blindly appending '/v1' produced '.../v1/v1/chat/completions', which
    the gateway 404s on for every single call (classification, notebook
    selection, AND final RCA synthesis all failed identically). The
    secrets.local.env.example comment already says "no trailing /v1 on
    the base URL" but nothing enforced it, so make this tolerant of
    either convention instead of relying on every deployer reading and
    following that comment correctly."""
    base = _clean_env("LITEMAAS_BASE_URL").rstrip("/")
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


client = OpenAI(
    base_url=_litemaas_base_url(),
    api_key=_clean_env("LITEMAAS_API_KEY"),
)
MODEL = _clean_env("MODEL_NAME")

CONF_BASE = os.environ.get("CONFLUENCE_BASE_URL", "").rstrip("/")
SPACES = [
    s.strip()
    for s in os.environ.get("CONFLUENCE_SPACES", "").split(",")
    if s.strip()
]

PENDING: dict[str, dict] = {}


def load_text(name: str) -> str:
    path = CONFIG_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"Missing agent config: {path}")
    return path.read_text(encoding="utf-8")


# /app/config is a ConfigMap volume mount (see manifests/ for the Deployment) -
# Kubernetes always mounts ConfigMap volumes read-only, so this process can
# NEVER write team-config.json directly to that path. Persisting a team-config
# edit instead means PATCHing the backing ConfigMap via the in-cluster K8s API
# (save_team_config() below); kubelet then re-syncs the mounted file within
# ~60-90s on its own. _TEAM_CONFIG_CACHE closes that gap for THIS process: once
# set, every load_team_*() call serves the just-written data immediately,
# rather than waiting on the kubelet sync lag. A pod restart before the next
# sync simply falls back to the file, which the API call already updated
# anyway - there is no window where data is lost, only a window where a
# *second*, independent replica would still see the old value (fine for the
# current single-replica deployment).
_TEAM_CONFIG_CACHE: dict | None = None

_TEAM_CONFIG_DEFAULT = {
    "version": "1.0",
    "confluence": {"spaces": []},
    "notebook": {"notebooks": []},
    "slack": {"channels": []},
}

# Maps the canvas/API's source id to (top-level key, list key) in team-config.json.
_TEAM_SECTION = {
    "CFL": ("confluence", "spaces"),
    "NBK": ("notebook", "notebooks"),
    "SLK": ("slack", "channels"),
}


def _team_config_snapshot() -> dict:
    if _TEAM_CONFIG_CACHE is not None:
        return _TEAM_CONFIG_CACHE
    path = CONFIG_DIR / TEAM_CONFIG_FILE
    if not path.is_file():
        return dict(_TEAM_CONFIG_DEFAULT)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(_TEAM_CONFIG_DEFAULT)


_SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


def _k8s_auth() -> tuple[str, str, str]:
    """This pod's own in-cluster ServiceAccount identity: (bearer token, CA
    cert path, this pod's own namespace). Shared by every K8s API call this
    process makes (ConfigMap patch for team-config, Secret patch + Deployment
    restart for credentials) - each caller still needs its own Role granting
    the specific verb/resource/resourceName it uses; this just reads the
    token/cert once per call."""
    token = (_SA_DIR / "token").read_text().strip()
    namespace = (_SA_DIR / "namespace").read_text().strip()
    ca_path = str(_SA_DIR / "ca.crt")
    return token, ca_path, namespace


def _k8s_patch(url: str, body: dict) -> None:
    token, ca_path, _ = _k8s_auth()
    with httpx.Client(verify=ca_path, timeout=15.0) as http:
        resp = http.patch(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/merge-patch+json",
            },
            json=body,
        )
    resp.raise_for_status()


def _k8s_patch_or_create_secret(namespace: str, name: str, string_data: dict) -> None:
    """PATCH the named Secret's stringData; if it doesn't exist yet, create it.

    A bare PATCH 404s on any environment where this Secret was never
    pre-created - e.g. nbk-mcp-creds and mcp-slack-creds ship with no
    manifest of their own (manifests/nbk-mcp.yaml / slack-mcp.yaml just
    reference the Secret name; nothing creates a placeholder for them the
    way secrets.example.yaml does for CFL/KCS), so on a genuinely fresh
    deploy the very first credential upload always hits an empty Secret.
    Previously that 404 propagated as an unhandled httpx.HTTPStatusError and
    got mapped to a generic 502 Bad Gateway with no indication of the real
    cause. Falling back to create here requires the `create` verb on
    secrets in this namespace - see
    manifests/orchestrator-credentials-rbac.yaml."""
    token, ca_path, _ = _k8s_auth()
    secret_url = f"https://kubernetes.default.svc/api/v1/namespaces/{namespace}/secrets/{name}"
    with httpx.Client(verify=ca_path, timeout=15.0) as http:
        headers = {"Authorization": f"Bearer {token}"}
        resp = http.patch(
            secret_url,
            headers={**headers, "Content-Type": "application/merge-patch+json"},
            json={"stringData": string_data},
        )
        if resp.status_code == 404:
            create_url = f"https://kubernetes.default.svc/api/v1/namespaces/{namespace}/secrets"
            resp = http.post(
                create_url,
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": name, "namespace": namespace},
                    "stringData": string_data,
                },
            )
    resp.raise_for_status()


def save_team_config(data: dict) -> None:
    """PATCH the debug-agent-config ConfigMap's team-config.json key via the
    in-cluster K8s API, using this pod's own ServiceAccount token. Requires a
    Role granting get/patch/update on that one ConfigMap (see
    manifests/orchestrator-team-config-rbac.yaml) - bound to the 'default'
    ServiceAccount this Deployment actually runs as (it sets no
    serviceAccountName of its own). Updates _TEAM_CONFIG_CACHE first so
    load_team_*() reflects the change immediately regardless of kubelet's
    ConfigMap-volume sync lag."""
    global _TEAM_CONFIG_CACHE
    _, _, namespace = _k8s_auth()
    cm_name = os.environ.get("TEAM_CONFIG_CONFIGMAP", "debug-agent-config")
    url = f"https://kubernetes.default.svc/api/v1/namespaces/{namespace}/configmaps/{cm_name}"
    _k8s_patch(url, {"data": {TEAM_CONFIG_FILE: json.dumps(data, indent=2)}})
    _TEAM_CONFIG_CACHE = data


def load_team_notebooks() -> list[dict]:
    return (_team_config_snapshot().get("notebook") or {}).get("notebooks") or []


def load_team_spaces() -> list[dict]:
    return (_team_config_snapshot().get("confluence") or {}).get("spaces") or []


def load_team_channels() -> list[dict]:
    return (_team_config_snapshot().get("slack") or {}).get("channels") or []


# ---------------------------------------------------------------------------
# Query history persistence - stored as a second key inside the SAME
# debug-agent-config ConfigMap that team-config.json already lives in, so
# the existing RBAC Role (resourceNames: ["debug-agent-config"]) covers it
# with no new grant needed, and it rides the same kubelet ConfigMap-volume
# auto-sync (~60-90s, no pod restart) that team-config.json relies on. This
# makes history survive a browser refresh (new Streamlit session) AND
# outlive the orchestrator pod itself (image rebuilds, rollout restarts),
# unlike the old st.session_state-only list which lost everything on either.
HISTORY_FILE = "query-history.json"
HISTORY_RETENTION_DAYS = float(os.environ.get("HISTORY_RETENTION_DAYS", "3"))
_HISTORY_MAX_ENTRIES = 50
_HISTORY_MAX_BYTES = 900_000  # stay comfortably under k8s's ~1MiB ConfigMap/etcd object cap
_HISTORY_CACHE: list[dict] | None = None


def _history_snapshot() -> list[dict]:
    if _HISTORY_CACHE is not None:
        return _HISTORY_CACHE
    path = CONFIG_DIR / HISTORY_FILE
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _prune_history(entries: list[dict]) -> list[dict]:
    """Drop entries older than HISTORY_RETENTION_DAYS (default 3), newest
    first, then cap by count and by total encoded size so this never grows
    into the ConfigMap size limit."""
    cutoff = datetime.now(timezone.utc).timestamp() - HISTORY_RETENTION_DAYS * 86400
    kept = [e for e in entries if e.get("ts_epoch", 0) >= cutoff]
    kept.sort(key=lambda e: e.get("ts_epoch", 0), reverse=True)
    kept = kept[:_HISTORY_MAX_ENTRIES]
    while kept and len(json.dumps(kept)) > _HISTORY_MAX_BYTES:
        kept.pop()  # list is newest-first, so this drops the oldest surviving entry
    return kept


def save_history(entries: list[dict]) -> None:
    """PATCH the query-history.json key of the debug-agent-config ConfigMap.
    Mirrors save_team_config() exactly, just a different key in the same
    object."""
    global _HISTORY_CACHE
    _, _, namespace = _k8s_auth()
    cm_name = os.environ.get("TEAM_CONFIG_CONFIGMAP", "debug-agent-config")
    url = f"https://kubernetes.default.svc/api/v1/namespaces/{namespace}/configmaps/{cm_name}"
    _k8s_patch(url, {"data": {HISTORY_FILE: json.dumps(entries, indent=2)}})
    _HISTORY_CACHE = entries


class HistoryItem(BaseModel):
    query: str
    sources: list[str] = []
    result: dict
    mode: str = "troubleshoot"


@app.get("/history")
def get_history():
    items = _prune_history(_history_snapshot())
    return {"items": items, "retention_days": HISTORY_RETENTION_DAYS}


@app.post("/history")
def add_history(item: HistoryItem):
    now = datetime.now(timezone.utc)
    entries = list(_history_snapshot())
    entries.insert(0, {
        "ts_epoch": now.timestamp(),
        "ts": now.isoformat(),
        "query": item.query,
        "sources": item.sources,
        "result": item.result,
        "mode": item.mode,
    })
    pruned = _prune_history(entries)
    try:
        save_history(pruned)
    except Exception as exc:  # noqa: BLE001 - history persistence is best-effort, never block the UI
        raise HTTPException(status_code=502, detail=f"Failed to save history: {str(exc)[:200]}")
    return {"status": "ok", "count": len(pruned)}


@app.delete("/history")
def clear_history():
    save_history([])
    return {"status": "ok"}


def _effective_cfl_spaces() -> list[str]:
    """team-config.json is authoritative when it has spaces configured (it's
    the thing the user actually maintains via [ADD_TEAM_ITEM]/the canvas).
    CONFLUENCE_SPACES env var is only a fallback for when team-config has
    none - it previously silently overrode team-config with a stale value
    (CONFLUENCE_SPACES=Automotive) that didn't match any real content for
    this team's queries, filtering out every genuinely relevant result."""
    team_keys = [s.get("key") for s in load_team_spaces() if s.get("key")]
    return team_keys or SPACES


def load_json_config(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _persona_catalog() -> list[str]:
    return list((load_json_config(PERSONA_FILE).get("personas") or {}).keys())


def _template_catalog() -> list[dict]:
    """Flatten the tier/category/template tree in prompt-templates-slim.json
    into a flat list of {tier, category, name, template, placeholders, chain}."""
    data = load_json_config(TEMPLATE_FILE)
    out = []
    for tier, tier_body in (data.get("tiers") or {}).items():
        for category, cat_body in (tier_body.get("categories") or {}).items():
            for name, tmpl in cat_body.items():
                out.append({
                    "tier": tier,
                    "category": category,
                    "name": name,
                    "template": tmpl.get("template", ""),
                    "placeholders": tmpl.get("placeholders", []),
                    "chain": tmpl.get("chain", []),
                })
    return out


def _find_template(name: str) -> dict | None:
    if not name:
        return None
    for t in _template_catalog():
        if t["name"] == name:
            return t
    return None


_MDC_GOVERNANCE_CACHE: str | None = None


def _mdc_governance_text() -> str:
    """debugagent-hub.mdc (RULE_FILE) is baked into this image at
    /app/config/debugagent-hub.mdc - it is the exact same file that governs
    the Cursor-canvas agent. Most of it (MCP tool allowlists, OAuth setup
    modes, sidecar write paths) only makes sense inside Cursor's own agent
    loop and has no equivalent here (this service calls its own in-cluster
    MCP servers with service credentials, not Cursor's personal-OAuth
    MCPs - that's *why* this is a separate FastAPI service in the first
    place, not a portability gap to fix).

    But the "## Response Rules" / "## Source-Grounded Constraint (STRICT)"
    sections ARE output-governance, not transport-specific, and previously
    only reached this service's LLM calls via the standalone /rca endpoint's
    system_message() - never the main /troubleshoot pipeline's RCA synthesis
    in run_prompt_chain(), which is what the Streamlit UI actually calls.
    This extracts just that portable slice so the production RCA-synthesis
    prompt is explicitly governed by the same rules text as the Cursor
    agent, instead of only app.py's own (potentially drifting) paraphrase
    of them."""
    global _MDC_GOVERNANCE_CACHE
    if _MDC_GOVERNANCE_CACHE is not None:
        return _MDC_GOVERNANCE_CACHE
    try:
        text = load_text(RULE_FILE)
        match = re.search(
            r"## Response Rules.*?(?=\n## Data Sensitivity)", text, re.DOTALL
        )
        _MDC_GOVERNANCE_CACHE = match.group(0).strip() if match else ""
    except FileNotFoundError:
        _MDC_GOVERNANCE_CACHE = ""
    return _MDC_GOVERNANCE_CACHE


def system_message() -> str:
    return (
        "You are the Debug Agent Hub running in OpenShift. "
        "Follow the rules, personas, and prompt templates below. "
        "Classify the user request with one persona and one template. "
        "Ground every resolution step in the Confluence and NotebookLM evidence supplied. "
        "If that evidence is empty, reply that no matching content was found "
        "and do not invent diagnostic steps.\n\n"
        "## Rules\n"
        f"{load_text(RULE_FILE)}\n\n"
        "## Personas\n"
        f"{load_text(PERSONA_FILE)}\n\n"
        "## Prompt templates\n"
        f"{load_text(TEMPLATE_FILE)}\n"
    )


class RcaRequest(BaseModel):
    findings: str


class TroubleshootRequest(BaseModel):
    query: str
    sources: list[str] | None = None  # subset of ["CFL","NBK","KCS","SLK"]; None/empty = all four


def public_base(request: Request) -> str:
    configured = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if configured:
        return configured
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def redirect_uri(request: Request) -> str:
    return public_base(request) + "/auth/atlassian/callback"


def store_path() -> Path:
    preferred = Path("/var/oauth/atlassian.json")
    try:
        preferred.parent.mkdir(parents=True, exist_ok=True)
        probe = preferred.parent / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return preferred
    except OSError:
        fallback = Path("/tmp/atlassian-oauth.json")
        return fallback


def load_store() -> dict:
    path = store_path()
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_store(data: dict) -> None:
    path = store_path()
    path.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def register_client(redirect: str) -> dict:
    store = load_store()
    if store.get("client_id") and redirect in (store.get("redirect_uris") or []):
        return store
    resp = httpx.post(
        REGISTER_URL,
        json={
            "client_name": "debug-agent-orchestrator",
            "redirect_uris": [redirect],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": SCOPES,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    reg = resp.json()
    store["client_id"] = reg["client_id"]
    store["client_secret"] = reg.get("client_secret", "")
    store["redirect_uris"] = reg.get("redirect_uris") or [redirect]
    store["token_endpoint_auth_method"] = reg.get("token_endpoint_auth_method", "none")
    save_store(store)
    return store


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def token_request(form: dict) -> dict:
    resp = httpx.post(TOKEN_URL, data=form, timeout=30.0)
    if resp.status_code >= 400:
        raise RuntimeError(f"Token request failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def ensure_access_token() -> str:
    store = load_store()
    access = store.get("access_token")
    if access and store.get("expires_at", 0) > time.time():
        return access
    refresh = store.get("refresh_token")
    if not refresh or not store.get("client_id"):
        raise RuntimeError("Not signed in to Confluence. Open /auth/atlassian/login")
    form = {
        "grant_type": "refresh_token",
        "client_id": store["client_id"],
        "refresh_token": refresh,
        "resource": RESOURCE,
    }
    if store.get("client_secret"):
        form["client_secret"] = store["client_secret"]
    tok = token_request(form)
    store["access_token"] = tok["access_token"]
    if tok.get("refresh_token"):
        store["refresh_token"] = tok["refresh_token"]
    store["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    save_store(store)
    return store["access_token"]


def _parse_mcp_response(response: httpx.Response) -> dict | None:
    if response.status_code == 202 or not response.content:
        return None
    text = response.text
    if "text/event-stream" in response.headers.get("content-type", "") or text.lstrip().startswith("event:"):
        data = None
        for line in text.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk and chunk != "[DONE]":
                    data = json.loads(chunk)
        if data is None:
            raise RuntimeError(f"Empty MCP stream ({response.status_code})")
        return data
    return response.json()


def _call_mcp(url: str, name: str, arguments: dict, headers: dict, timeout: float = 60.0) -> str:
    headers = dict(headers)
    with httpx.Client(timeout=timeout) as http:
        init = http.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "debug-agent-orchestrator", "version": "1.0"},
                },
            },
        )
        init.raise_for_status()
        session = init.headers.get("mcp-session-id")
        if session:
            headers["Mcp-Session-Id"] = session
        note = http.post(
            url,
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        if note.status_code not in (200, 202):
            note.raise_for_status()
        resp = http.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        resp.raise_for_status()
        body = _parse_mcp_response(resp) or {}
    if body.get("error"):
        raise RuntimeError(json.dumps(body["error"])[:500])
    result = body.get("result") or {}
    content = result.get("content") or []
    if result.get("isError"):
        text = content[0].get("text", "") if content else ""
        raise RuntimeError(text[:500] or "MCP tool returned an error")
    if len(content) > 1:
        # Some MCP servers (confirmed for mcp-slack-server's list-returning
        # tools, e.g. search_messages/search_channel_messages, by inspecting
        # a raw tools/call response directly) emit ONE content block PER
        # LIST ITEM instead of a single block holding the whole JSON array.
        # Reassemble them into one JSON array string so callers' json.loads()
        # behaves the same regardless of which style the upstream server
        # chose - CFL/NBK/KCS have only ever returned a single block, so
        # this is a no-op for them.
        parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        return "[" + ",".join(p for p in parts if p) + "]"
    return content[0].get("text", "") if content else ""


def mcp_tool(name: str, arguments: dict) -> str:
    token = ensure_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    return _call_mcp(MCP_URL, name, arguments, headers)


def nbk_tool(name: str, arguments: dict, timeout: float = 60.0) -> str:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    return _call_mcp(NBK_URL, name, arguments, headers, timeout=timeout)


def cfl_tool(name: str, arguments: dict, timeout: float = 30.0) -> str:
    """Call the local mcp-atlassian server (mcp-confluence-service, namespace
    debug-agent-mcp) over streamable-HTTP. Same _call_mcp() client used for
    nbk_tool() - this is a cross-namespace Service call, not a sidecar or
    subprocess, so no Dockerfile/image change was needed to add this."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    return _call_mcp(CFL_MCP_URL, name, arguments, headers, timeout=timeout)


def kcs_tool(name: str, arguments: dict, timeout: float = 20.0) -> str:
    """Call mcp-redhat-kcs-server (namespace debug-agent-mcp) over streamable-HTTP.
    Same _call_mcp() client used for cfl_tool()/nbk_tool() - another
    cross-namespace Service call, no subprocess, no direct SSO/Hydra calls
    from this process."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    return _call_mcp(KCS_MCP_URL, name, arguments, headers, timeout=timeout)


def slk_tool(name: str, arguments: dict, timeout: float = 20.0) -> str:
    """Call mcp-slack-server (namespace debug-agent-mcp) over streamable-HTTP.
    Same _call_mcp() client used for cfl_tool()/nbk_tool()/kcs_tool() - no
    per-request auth headers needed, since SLACK_XOXC_TOKEN/SLACK_XOXD_TOKEN
    set as server-wide env vars (see manifests/slack-mcp.yaml) take priority
    over per-request headers in every transport mode (confirmed by reading
    slack_mcp_server.py's make_request())."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    return _call_mcp(SLK_MCP_URL, name, arguments, headers, timeout=timeout)


@app.get("/healthz")
def healthz():
    """Liveness probe target. Process-local only - no MCP/network calls -
    so a slow or down Confluence/NotebookLM/KCS dependency never causes
    kubelet to kill and restart this container. Deep dependency status
    lives in /health, which backs the readiness probe instead."""
    return {"status": "ok"}


# /health backs the readiness probe, which has its own timeoutSeconds
# budget (live cluster config: 6s as of this writing). Since each
# dependency's health check can be called with its own timeout in
# seconds, this per-check timeout -- not just their parallel dispatch --
# must stay comfortably under that budget. A single dead/hanging dependency
# (e.g. an expired Slack session, where the upstream call blocks for its
# full timeout instead of failing fast) must never be able to push /health's
# total wall-clock latency past the probe's timeoutSeconds, or that ONE
# degraded source takes the entire orchestrator's readiness down for every
# source, not just the dead one. 4.0s per check, run in parallel via the
# ThreadPoolExecutor below, bounds total /health latency to ~4s even in the
# worst case (one dependency fully hung) -- safely under the probe budget,
# with the dependency correctly reported as "unreachable" rather than the
# whole endpoint timing out.
_HEALTH_CHECK_TIMEOUT = 4.0


def _cfl_health_check() -> tuple[str, str | None]:
    try:
        cfl_tool("confluence_search", {"query": "healthcheck", "limit": 1}, timeout=_HEALTH_CHECK_TIMEOUT)
        return "ok", None
    except Exception as exc:  # noqa: BLE001 - health check must not crash
        return "unreachable", str(exc)[:200]


def _nbk_health_check() -> tuple[str, str | None]:
    try:
        info_text = nbk_tool("server_info", {}, timeout=_HEALTH_CHECK_TIMEOUT)
        info = json.loads(info_text)
        return info.get("auth_status", "unknown"), None
    except Exception as exc:  # noqa: BLE001 - health check must not crash
        return "unreachable", str(exc)[:200]


def _kcs_health_check() -> tuple[str, str | None]:
    try:
        kcs_tool("search_kcs", {"query": "healthcheck", "rows": 1}, timeout=_HEALTH_CHECK_TIMEOUT)
        return "ok", None
    except Exception as exc:  # noqa: BLE001 - health check must not crash
        return "unreachable", str(exc)[:200]


def _slk_health_check() -> tuple[str, str | None]:
    try:
        slk_tool("whoami", {}, timeout=_HEALTH_CHECK_TIMEOUT)
        return "ok", None
    except Exception as exc:  # noqa: BLE001 - health check must not crash
        return "unreachable", str(exc)[:200]


@app.get("/health")
def health():
    """Deep dependency check - backs the readiness probe. Each of the four
    MCP sources does a full initialize/notifications/tools-call handshake
    (3 network round-trips), so these run in parallel rather than in
    sequence - sequential execution was pushing total latency past the
    probe's timeoutSeconds once a third cross-namespace hop (KCS) was
    added, which caused a readiness/liveness crash loop."""
    files = [RULE_FILE, PERSONA_FILE, TEMPLATE_FILE, TEAM_CONFIG_FILE]

    with ThreadPoolExecutor(max_workers=4) as pool:
        cfl_future = pool.submit(_cfl_health_check)
        nbk_future = pool.submit(_nbk_health_check)
        kcs_future = pool.submit(_kcs_health_check)
        slk_future = pool.submit(_slk_health_check)
        cfl_status, cfl_error = cfl_future.result()
        nbk_status, nbk_error = nbk_future.result()
        kcs_status, kcs_error = kcs_future.result()
        slk_status, slk_error = slk_future.result()

    return {
        "status": "ok",
        "model": MODEL,
        "config": {name: (CONFIG_DIR / name).is_file() for name in files},
        "confluence_mcp": CFL_MCP_URL,
        "confluence_status": cfl_status,
        "confluence_error": cfl_error,
        "notebooklm_mcp": NBK_URL,
        "notebooklm_auth_status": nbk_status,
        "notebooklm_error": nbk_error,
        "notebooklm_notebooks_configured": len(load_team_notebooks()),
        "kcs_mcp": KCS_MCP_URL,
        "kcs_status": kcs_status,
        "kcs_error": kcs_error,
        "slack_mcp": SLK_MCP_URL,
        "slack_status": slk_status,
        "slack_error": slk_error,
        "slack_channels_configured": len(load_team_channels()),
    }


# ---------------------------------------------------------------------------
# In-UI credential paste for CFL/KCS - both use a static token (Confluence
# API token, Red Hat SSO offline token) that's safe to type into a text
# field. Slack (browser session cookies) and NotebookLM (interactive Google
# OAuth via `nlm login`) are NOT safe to type into a text field, so they use
# a separate file-upload flow instead (see /credentials/{source}/file,
# _FILE_CRED_CONFIG below) - both converge on the same underlying mechanism:
# UI submits credentials -> orchestrator PATCHes the backing Secret in
# debug-agent-mcp via the in-cluster K8s API -> restarts that one MCP
# Deployment (Secret-sourced env vars/volume mounts do NOT hot-reload like
# team-config.json's ConfigMap-volume mount does - only a new pod picks up
# the new value) -> caller re-polls /health to see the status flip. The
# credential value is never echoed back in any response or log line.
_CRED_CONFIG = {
    "CFL": {"secret": "confluence-mcp-creds", "deployment": "mcp-confluence-server", "token_key": "CONFLUENCE_API_TOKEN"},
    "KCS": {"secret": "redhat-kcs-mcp-creds", "deployment": "mcp-redhat-kcs-server", "token_key": "RHKB_OFFLINE_TOKEN"},
}


class CredentialUpdate(BaseModel):
    token: str
    username: str | None = None  # CFL only - CONFLUENCE_USERNAME (email)
    url: str | None = None  # CFL only - optional override of the hardcoded CONFLUENCE_URL


# CFL's CONFLUENCE_URL is intentionally NOT settable through this endpoint -
# this deploy always points at Red Hat's internal Confluence instance
# (hardcoded in scripts/deploy-fresh-environment.sh's placeholder seed), so
# there is no URL field in the UI anymore and no way to override it via the
# API either. Only email + API token vary per user.


def _mcp_namespace() -> str:
    return os.environ.get("MCP_NAMESPACE", "debug-agent-mcp")


def _k8s_restart_deployment(namespace: str, name: str) -> None:
    """Force a rollout restart the same way `oc rollout restart` does under
    the hood - patch the pod template's restartedAt annotation so kubelet
    replaces the pod and the container picks up the just-updated Secret."""
    from datetime import datetime, timezone

    url = f"https://kubernetes.default.svc/apis/apps/v1/namespaces/{namespace}/deployments/{name}"
    restarted_at = datetime.now(timezone.utc).isoformat()
    _k8s_patch(url, {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": restarted_at}}}}})


@app.post("/credentials/{source}")
def update_credentials(source: str, body: CredentialUpdate):
    source = source.strip().upper()
    if source not in _CRED_CONFIG:
        raise HTTPException(status_code=400, detail="source must be CFL or KCS")
    token = (body.token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="token is required")
    cfg = _CRED_CONFIG[source]
    namespace = _mcp_namespace()
    string_data = {cfg["token_key"]: token}
    if source == "CFL":
        if body.username and body.username.strip():
            string_data["CONFLUENCE_USERNAME"] = body.username.strip()
    try:
        _k8s_patch_or_create_secret(namespace, cfg["secret"], string_data)
        _k8s_restart_deployment(namespace, cfg["deployment"])
    except Exception as exc:  # noqa: BLE001 - never let a raw exception (or the token) leak into the response
        raise HTTPException(status_code=502, detail=f"Failed to apply credentials: {str(exc)[:200]}")
    return {"status": "ok", "restarted": True}


# Slack (browser-session cookies) and NotebookLM (interactive Google OAuth)
# can't be pasted into a text field the way CFL/KCS tokens can - the UI has
# the user upload the ready-made Secret YAML produced by
# scripts/refresh-slack-creds.ps1 -Mode Export /
# scripts/refresh-nbk-creds.ps1 -SkipLogin -Mode Export instead. Once parsed,
# this converges on the exact same PATCH-secret-then-restart-deployment
# mechanism as the paste-a-token flow above.
_FILE_CRED_CONFIG = {
    "SLK": {
        "secret": "mcp-slack-creds",
        "deployment": "mcp-slack-server",
        "required_keys": {"SLACK_XOXC_TOKEN", "SLACK_XOXD_TOKEN"},
    },
    "NBK": {
        "secret": "nbk-mcp-creds",
        "deployment": "nbk-mcp-server",
        # Must match the volume's `items:` source keys in manifests/nbk-mcp.yaml
        # exactly - that's what notebooklm-mcp-cli expects on disk.
        "required_keys": {"auth.json", "cookies.json", "metadata.json"},
    },
}


@app.post("/credentials/{source}/file")
async def update_credentials_from_file(source: str, file: UploadFile = File(...)):
    source = source.strip().upper()
    if source not in _FILE_CRED_CONFIG:
        raise HTTPException(status_code=400, detail="source must be SLK or NBK")
    cfg = _FILE_CRED_CONFIG[source]

    raw = await file.read()
    try:
        doc = yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001 - never let a raw exception (or file content) leak into the response
        raise HTTPException(status_code=400, detail=f"Could not parse uploaded file as YAML: {str(exc)[:200]}")
    if not isinstance(doc, dict) or doc.get("kind") != "Secret":
        raise HTTPException(status_code=400, detail="Uploaded file must be a Kubernetes Secret YAML (kind: Secret)")

    # Accept either plain `stringData` (what refresh-slack-creds.ps1 writes)
    # or base64 `data` (what refresh-nbk-creds.ps1 writes) - normalize both
    # to a single plain-string dict so the outgoing PATCH is always stringData.
    string_data: dict[str, str] = {}
    for k, v in (doc.get("stringData") or {}).items():
        string_data[str(k)] = str(v)
    for k, v in (doc.get("data") or {}).items():
        try:
            string_data[str(k)] = base64.b64decode(str(v)).decode("utf-8")
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Could not base64-decode data key '{k}' in uploaded file")

    missing = cfg["required_keys"] - string_data.keys()
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Uploaded file is missing required key(s): {', '.join(sorted(missing))}",
        )

    namespace = _mcp_namespace()
    try:
        _k8s_patch_or_create_secret(namespace, cfg["secret"], string_data)
        _k8s_restart_deployment(namespace, cfg["deployment"])
    except Exception as exc:  # noqa: BLE001 - never let a raw exception (or the secret content) leak into the response
        raise HTTPException(status_code=502, detail=f"Failed to apply credentials: {str(exc)[:200]}")
    return {"status": "ok", "restarted": True}


@app.get("/auth/status")
def auth_status():
    store = load_store()
    return {
        "signed_in": bool(store.get("refresh_token")),
        "login_url": "/auth/atlassian/login",
    }


@app.get("/auth/atlassian/login")
def atlassian_login(request: Request):
    redirect = redirect_uri(request)
    store = register_client(redirect)
    state = secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    PENDING[state] = {"verifier": verifier, "redirect": redirect, "exp": time.time() + 600}
    params = {
        "response_type": "code",
        "client_id": store["client_id"],
        "redirect_uri": redirect,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": RESOURCE,
        "prompt": "consent",
    }
    return RedirectResponse(AUTHORIZE_URL + "?" + urlencode(params), status_code=302)


@app.get("/auth/atlassian/callback")
def atlassian_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
):
    if error:
        return HTMLResponse(
            f"<p>Atlassian login failed: {error} {error_description}</p><p><a href='/auth/atlassian/login'>Try again</a></p>",
            status_code=400,
        )
    pending = PENDING.pop(state, None)
    if not pending or pending["exp"] < time.time():
        return HTMLResponse(
            "<p>Login session expired. <a href='/auth/atlassian/login'>Start again</a></p>",
            status_code=400,
        )
    store = load_store()
    form = {
        "grant_type": "authorization_code",
        "client_id": store.get("client_id", ""),
        "code": code,
        "redirect_uri": pending["redirect"],
        "code_verifier": pending["verifier"],
        "resource": RESOURCE,
    }
    if store.get("client_secret"):
        form["client_secret"] = store["client_secret"]
    try:
        tok = token_request(form)
    except Exception as exc:
        return HTMLResponse(
            f"<p>Could not save the Confluence login.</p><pre>{exc}</pre><p><a href='/auth/atlassian/login'>Try again</a></p>",
            status_code=502,
        )
    store["access_token"] = tok.get("access_token", "")
    if tok.get("refresh_token"):
        store["refresh_token"] = tok["refresh_token"]
    store["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    save_store(store)
    return HTMLResponse(
        "<p>Confluence sign-in saved for this orchestrator.</p>"
        "<p><a href='/'>Return to Debug Agent Hub</a></p>"
    )


@app.get("/")
def index():
    """API-only now - the UI moved to a standalone Streamlit app
    (debug-agent-ui) that calls this backend's /health and /troubleshoot
    over its in-cluster Service. See UI_URL for the current UI Route."""
    return {
        "service": "debug-agent-orchestrator",
        "message": "This is the API backend only. Use the Debug Agent Hub UI instead.",
        "ui_url": os.environ.get("UI_URL", ""),
        "endpoints": [
            "/health", "/healthz", "/troubleshoot", "/find-matches", "/rca",
            "/team-config", "/team-config/search", "/team-config/items",
            "/credentials/{source}", "/credentials/{source}/file", "/history",
        ],
    }


@app.post("/rca")
def rca(req: RcaRequest):
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_message()},
            {"role": "user", "content": f"Summarize root cause from these findings:\n{req.findings}"},
        ],
    )
    return {"rca": resp.choices[0].message.content}


def _safe_cql_text(query: str) -> str:
    return re.sub(r'["\\]', " ", query).strip()


def _matches_from_text(text: str) -> list[dict]:
    payload = None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    items = []
    if isinstance(payload, dict):
        items = payload.get("results") or payload.get("data") or []
    elif isinstance(payload, list):
        items = payload
    matches = []
    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        content = item.get("content") if isinstance(item.get("content"), dict) else item
        title = content.get("title") or item.get("title") or "Confluence page"
        url = item.get("url") or content.get("url") or ""
        excerpt = item.get("excerpt") or content.get("excerpt") or ""
        excerpt = re.sub(r"<[^>]+>", " ", str(excerpt))
        excerpt = re.sub(r"\s+", " ", excerpt).strip()[:2000]
        matches.append(
            {
                "source": "CFL",
                "title": title,
                "url": url,
                "excerpt": excerpt,
                "score": max(40, 100 - (i - 1) * 10),
            }
        )
    if matches:
        return matches
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []
    return [
        {
            "source": "CFL",
            "title": "Confluence search",
            "url": "",
            "excerpt": cleaned[:2000],
            "score": 50,
        }
    ]


def _strip_cfl_highlight_markup(text: str) -> str:
    return re.sub(r"@@@hl@@@|@@@endhl@@@", "", text or "")


def search_confluence(query: str, limit: int = 10) -> list[dict]:
    """Search Confluence via the local mcp-atlassian server (cfl_tool),
    replacing the old Atlassian Rovo OAuth path (mcp_tool/MCP_URL), whose
    agent-interface OAuth scope never completes consent. This server uses a
    plain Basic-auth API token (confluence-mcp-creds secret) - no OAuth.

    IMPORTANT: confluence_search's "query" argument is fed straight to
    Confluence's CQL endpoint by the upstream mcp-atlassian server, which
    only heuristically detects plain text vs. CQL. Free-text troubleshooting
    queries frequently contain the bare words "AND"/"OR" or embedded double
    quotes (e.g. quoting an error message), which get misdetected as raw CQL
    and rejected with "Unexpected error during search: The query cannot be
    parsed". To make this robust for any natural-language input, always wrap
    the query ourselves in an explicit, escaped `text ~ "..."` CQL clause
    instead of relying on that heuristic.

    IMPORTANT: without a `type = page` restriction, Confluence's CQL search
    also matches attachments, comments, and blogposts whose filename/body
    happens to contain the query text ever so loosely (confirmed in
    practice - a troubleshooting query surfaced an unrelated attachment
    whose link was not a readable page at all). Restricting to `type = page`
    keeps results to actual wiki pages, which is what the ranked-match UI
    and RCA synthesis assume they're citing.

    IMPORTANT: the raw query text used to be dropped into `text ~ "..."`
    VERBATIM - fine for a short sentence, but a very common real workflow
    is pasting an entire raw error/log blob (multi-line, full of IPs,
    ports, JSON punctuation, timestamps) as the query. Confluence's CQL
    `text ~` is a tokenized "contains" match, not semantic search - feeding
    it a huge, noisy, unnatural blob lets Confluence's OWN internal
    relevance ranking bury (or entirely exclude from the top `limit`
    results) the one page that actually contains the real error text,
    under noise matches from the IPs/ports/timestamps (confirmed in
    practice: a page verified to contain this exact error didn't appear in
    results at all). Searching on the same cleaned, de-noised keyword set
    that scoring already uses (_extract_score_keywords) keeps the CQL
    search focused on the handful of terms that could actually appear in a
    real document, instead of the whole blob.

    IMPORTANT: confluence_search's `text ~ "w1 w2 w3 ..."` behaves as an
    implicit AND of every single word supplied - confirmed empirically: a
    3-term search ("grafana password cbis-ansible-security") found the
    one real target page as the ONLY hit, but the exact same query's full
    ~24-term cleaned keyword set returned ZERO hits, because requiring
    ALL 24 of them to literally co-occur in one page is practically
    impossible even for the genuinely-matching page (no human-written KB
    article repeats an entire raw log's incidental vocabulary verbatim).
    More cleaned keywords therefore makes recall WORSE past a fairly low
    threshold, not better - the opposite of the usual "add more search
    terms" intuition. Try a small, specificity-ranked core set first
    (hyphenated/compound technical identifiers ranked above short common
    words - those are what separated "cbis-ansible-security" from "api"/
    "http" in testing), and relax to an even smaller core if that still
    returns nothing, instead of ever sending the full keyword set as one
    AND clause.

    IMPORTANT: even the small-core AND tiers can still fail on a term our
    own _term_specificity heuristic ranks highly but Confluence's own
    analyzer doesn't index as a matchable unit - confirmed in practice: a
    URL-embedded hostname-like token ("k8s-apiserver.ncs", inside a
    rendered `<https://k8s-apiserver.ncs:8443/...>` smart-link) matched
    ZERO pages even searched completely alone, which drags every AND
    combination that includes it down to zero too - even though the page
    was verified (by fetching its real content directly) to contain this
    exact error log verbatim, and OTHER single terms from the very same
    query ("memcache.go", "masterbm") each independently found it when
    searched alone. We can't reliably predict which specific term
    Confluence's analyzer will treat this way, so as a last resort, issue
    a few independent single-term searches instead of ever betting
    everything on one AND combination, and merge whatever distinct pages
    any of them find."""
    spaces = _effective_cfl_spaces()
    search_keywords = _extract_score_keywords(query)
    ordered_keywords = sorted(search_keywords, key=_term_specificity, reverse=True)
    term_tiers = [ordered_keywords[:10], ordered_keywords[:4]] if ordered_keywords else [[]]

    def _run_cql(cql: str) -> list | None:
        args: dict = {"query": cql, "limit": limit}
        if spaces:
            args["spaces_filter"] = ",".join(spaces)
        raw = cfl_tool("confluence_search", args)
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, list) else None

    items: list | None = None
    for tier in term_tiers:
        search_text = " ".join(tier) if tier else query
        parsed = _run_cql(f'type = page AND text ~ "{_safe_cql_text(search_text)}"')
        if parsed:
            items = parsed
            break
        if items is None:
            items = parsed  # keep the last attempt's shape for the fallback below

    if not items and ordered_keywords:
        merged: dict[str, dict] = {}
        for term in ordered_keywords[:3]:
            single = _run_cql(f'type = page AND text ~ "{_safe_cql_text(term)}"')
            for it in single or []:
                key = it.get("url") or it.get("id") or ""
                if key and key not in merged:
                    merged[key] = it
        if merged:
            # Don't truncate here - the normal per-item scoring/sort below
            # (and the top-5 full-page-fetch enrichment after it) already
            # decides what actually surfaces; cutting the merged pool down
            # to `limit` at THIS point, before anything is scored, risked
            # dropping the one real match in favor of whatever happened to
            # get inserted first.
            items = list(merged.values())

    if not isinstance(items, list):
        # Unexpected shape (e.g. a plain-text error from the server) - fall
        # back to the generic text matcher instead of dropping the result.
        return _matches_from_text(text)

    matches = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # Defense-in-depth: some confluence_search responses include a
        # "type" field (page/attachment/comment/blogpost) even though the
        # CQL above already asked for type=page - skip anything that isn't
        # a page if the field is present, rather than trusting the upstream
        # filter alone.
        item_type = str(item.get("type") or "").lower()
        if item_type and item_type != "page":
            continue
        content = item.get("content") or {}
        raw_excerpt = content.get("value") if isinstance(content, dict) else ""
        excerpt = _strip_cfl_highlight_markup(str(raw_excerpt or ""))
        excerpt = re.sub(r"\s+", " ", excerpt).strip()[:2000]
        space = item.get("space") if isinstance(item.get("space"), dict) else {}
        matches.append(
            {
                "source": "CFL",
                "title": item.get("title") or "Confluence page",
                "url": item.get("url") or "",
                "excerpt": excerpt,
                "score": score_answer_text(query, excerpt, base=55),
                "space": space.get("key", ""),
                "updated": item.get("updated", ""),
            }
        )

    # Fetch full page body for the most promising hits, same "cheap search,
    # fetch full content for the top few" pattern search_kcs() already uses
    # (get_kcs for its top 3). confluence_search only ever returns a short
    # highlight excerpt centered on ONE matched phrase - scoring against
    # that alone can badly undersell, or even hide, a page whose full body
    # is a genuine, complete, near word-for-word match for the query
    # (confirmed in practice: a page containing the user's exact pasted
    # error log verbatim still only scored in the 60s because the snippet
    # confluence_search returned barely overlapped the query). Re-score the
    # top candidates against the real content via confluence_get_page (page
    # ID/title lookup not needed - it accepts the full page URL directly)
    # so an exact/near-exact match is unambiguous (score near 95-100)
    # rather than indistinguishable from a loosely-related hit.
    matches.sort(key=lambda m: m["score"], reverse=True)
    for m in matches[: min(5, len(matches))]:
        full_body = _fetch_cfl_full_page(m["url"])
        if full_body:
            m["excerpt"] = full_body[:4000]
            m["score"] = score_answer_text(query, full_body, base=55)
    return matches


def _fetch_cfl_full_page(page_ref: str) -> str | None:
    """Fetch a Confluence page's full body text via confluence_get_page.
    Returns None on any failure (missing ref, tool error, empty body) so
    the caller falls back to keeping the original search-excerpt score
    rather than losing the match entirely."""
    if not page_ref:
        return None
    try:
        text = cfl_tool("confluence_get_page", {"page_id": page_ref}, timeout=20.0)
        payload = json.loads(text)
        content = (payload.get("metadata") or {}).get("content") or {}
        body = content.get("value") if isinstance(content, dict) else None
        if not body:
            return None
        body = _strip_cfl_highlight_markup(str(body))
        return re.sub(r"\s+", " ", body).strip() or None
    except Exception:  # noqa: BLE001 - one bad page fetch shouldn't drop the match
        return None


_SCORE_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "and", "or", "of", "to", "for",
    "in", "on", "at", "by", "with", "about", "related", "content", "please",
    "show", "find", "get", "me", "what", "how", "does", "do", "can", "i", "we",
    "this", "that", "it", "be", "not", "no", "any", "all", "us", "you", "your",
    # Ansible/JSON task-failure scaffolding - a user pasting a raw log/error
    # blob as their query (a very common real workflow, not just a short
    # sentence) gets these in every single failure output regardless of
    # root cause. They'd never appear verbatim in a KB article, but used to
    # still count toward the overlap-ratio denominator, permanently
    # capping the achievable score for any log-paste query (confirmed in
    # practice: a 35-keyword extraction from one Ansible failure blob was
    # ~20 scaffolding/noise tokens, dragging a would-be ~95 down to 64).
    "changed", "false", "true", "failed", "failure", "fatal", "info", "msg",
    "module", "module_stderr", "module_stdout", "stdout", "stderr", "see",
    "exact",
    # Ansible module result/invocation JSON schema - FIXED, documented
    # field names from Ansible's own module-result contract (cmd, delta,
    # start, end, rc, invocation, deprecations, exception, stdin, plus the
    # literal boilerplate phrase "non-zero return code" every failed
    # command prints). These describe the SHAPE of ANY ansible task
    # failure, never its topic/root-cause - they appear in essentially
    # every ansible-log-paste query in this environment (confirmed against
    # a second, unrelated real query - a Grafana password-change failure -
    # which still hit this same dilution: 44 extracted keywords, only
    # ~10 of which were topically real). This is a bounded, reusable
    # category fix, not a one-off word added for a single query.
    "cmd", "delta", "start", "end", "rc", "invocation", "deprecations",
    "exception", "stdin", "non-zero", "return", "code",
    # Red Hat / OpenShift product-brand boilerplate - this deployment's
    # entire corpus (Confluence spaces, KCS articles, NBK notebooks) is Red
    # Hat OpenShift content by definition, so "Red Hat", "OpenShift",
    # "Container", "Platform" appear in nearly every single document
    # regardless of topic. Counting them toward keyword overlap let any
    # page about literally anything OpenShift-related satisfy the
    # "relevant" gate (hits >= min(2, len(q_kw)) - just 2 matches even for
    # a long, specific query) purely by matching the product name, never
    # the actual topic. Confirmed in practice: a generic "Deployment"
    # runbook ranked #2 for "How to disable compromised Comodo AAA Services
    # / AAA Certificate Services CA root certificates on Red Hat OpenShift
    # Container Platform" - it matched nothing but "red"/"hat"/"openshift"/
    # "container"/"platform", none of which distinguish it from any other
    # page in this corpus. "certificate(s)"/"root"/"services"/"ca" etc are
    # deliberately NOT added here - those genuinely discriminate topic.
    "red", "hat", "openshift", "container", "platform",
    # Python logging / urllib3 boilerplate - a small, fixed vocabulary
    # from Python's own logging module and the urllib3 HTTP library that
    # shows up in almost any Python network-call failure regardless of
    # what the call was actually for: log-level names, urllib3's own
    # module name, and its own hardcoded deprecation-warning text
    # ("Advanced Usage ... documentation", "warnings.warn(").
    "debug", "warning", "warn", "critical", "urllib3", "connectionpool",
    "documentation", "advanced", "usage", "starting", "warnings.warn",
}

_SCORE_NEGATIVE_PHRASES = (
    "no information", "not found", "does not contain", "doesn't contain",
    "no results", "not covered", "no relevant", "cannot find", "could not find",
    "i don't have", "i do not have", "not mentioned", "no mention",
    "unable to find", "not available in", "no content",
)


def _is_noise_token(word: str) -> bool:
    """Drop tokens that can never generalize to a KB article: pure
    numbers/timestamps (e.g. "922", "2026-04-30" from a log line) and
    hostname/identifier-looking tokens (multiple hyphens mixed with
    digits, e.g. "ncs-mqv221-nccmqv221-masterbm-0") - these are unique to
    one environment/run and were never going to appear verbatim in any
    general-purpose documentation, yet were counting as 'must-match'
    keywords and silently capping the overlap ratio for log-paste queries."""
    if re.fullmatch(r"[\d.,:-]+", word):
        return True
    if word.count("-") >= 2 and any(c.isdigit() for c in word):
        return True
    # Redaction placeholders ("xxxxxxxxxxxxxx", a masked password/secret in
    # a pasted log) are a single character repeated N times - zero
    # information, yet long enough to look "distinctive" by length alone
    # and dominate term-selection if not caught here.
    if len(word) >= 4 and len(set(word)) == 1:
        return True
    return False


def _term_specificity(word: str) -> int:
    """Rough proxy for 'how likely is this term to be a genuinely
    distinctive product/task/component identifier, rather than a short
    common word' - used only to pick which few keywords to lead a CQL
    search with (see search_confluence). Hyphenated multi-word compounds
    ("cbis-ansible-security") are exactly the kind of specific identifier
    that let a 3-term search find the one real target page as its ONLY
    hit in testing, so they're weighted up; plain word length alone is a
    weaker but still useful signal for everything else."""
    score = len(word)
    if "-" in word:
        score += 6
    return score


def _extract_score_keywords(text: str) -> set[str]:
    # Ansible renders multi-line command output as a single JSON string,
    # so a pasted failure log often contains a LITERAL two-character
    # escape sequence ("\" + "n"/"r"/"t"), not a real newline/tab, glued
    # directly against the next word with no real whitespace in between
    # (e.g. "...'xxxxxx'\nDEBUG:urllib3..."). Left alone, that corrupts
    # tokenization - the trailing "n" fuses onto "DEBUG" producing a bogus
    # token like "ndebug" instead of the real, stoppable word "debug"
    # (confirmed in practice: "\nDEBUG"/"\nINFO"/"\nInvalid" became
    # "ndebug"/"ninfo"/"ninvalid"). Normalize to real whitespace first -
    # this is a tokenizer correctness fix, not specific to any one query.
    text = re.sub(r"\\[nrt]", " ", text)
    # Python's logging module conventionally prints "LEVEL:logger.name:" -
    # e.g. "DEBUG:urllib3.connectionpool:Starting new HTTPS connection" or
    # "INFO:cbis_common.CbisLogger:Invalid username or password". The
    # logger name is an internal module/class path, never meaningful
    # search content, but because it's a long dotted/underscored token
    # (e.g. "urllib3.connectionpool", "cbis_common.cbislogger") it looked
    # maximally "distinctive" by length and dominated keyword selection -
    # strip the whole "LEVEL:logger.name:" prefix structurally (a fixed,
    # well-known logging-output SHAPE, not any one specific logger name),
    # so it never becomes a token in the first place.
    text = re.sub(
        r"\b(?:DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL):[\w.]+:",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    words = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9._-]{2,}", text.lower())
    # Trailing punctuation cleanup: the token regex allows '.'/'-' mid-word
    # on purpose (needed for "my.txt", "sudoers.d", "ncc-admin"), but a log
    # paste's sentence-ending/ellipsis periods ("connected.", "response...")
    # rode along as part of the token too, producing near-duplicate
    # keywords that will never literally match real document text. Strip
    # trailing '.'/'-' after extraction, not during it, so "my.txt" keeps
    # its internal period but "connected." becomes "connected".
    cleaned = {w.rstrip(".-") for w in words}
    cleaned = {w for w in cleaned if len(w) >= 3}
    return {w for w in cleaned if w not in _SCORE_STOPWORDS and not _is_noise_token(w)}


def score_answer_text(query: str, answer: str, base: int = 55) -> int:
    """Heuristic 0-100 relevance score for a source match, since none of the
    sources return their own ranked score the way a search engine would (a
    flat hardcoded score there made every match look equally relevant,
    which is misleading - see the score=60/55 constants this originally
    replaced). The score is driven almost entirely by query/answer keyword
    overlap - actual topical relevance - plus a floor penalty for content
    that's too thin to evaluate at all.

    Deliberately NOT scored: formatting. Earlier versions of this function
    rewarded long content (+10 for >800 chars) and bulleted/numbered
    structure (+10) regardless of topic - which let a long, well-formatted
    KCS article about an unrelated subject outscore a short, genuinely
    on-topic Confluence page (observed in practice: a Satellite bugfix list
    outranked the actual answer). That's backwards for this tool's
    purpose: pages are written by many different people with very
    different formatting discipline, and find-matches/troubleshoot must
    surface whatever is topically relevant across all connected sources
    regardless of how well (or badly) any individual author formatted it.
    Judging the COLLECTED evidence - weighing caveats, picking the best
    procedure, noticing when something looks like a runbook vs. a stub -
    is exactly what the LLM-driven RCA/prompt-chain synthesis step
    (run_prompt_chain) is for. This function's only job is to get the
    topically-relevant matches into that evidence pool in the first place,
    not to pre-judge their quality by their formatting.

    Thin-content floor: a stub/near-empty excerpt (e.g. a Confluence
    attachment or a one-line placeholder) still needs to be penalized hard
    regardless of relevance, since there's no real content to judge
    relevance OF - that's a substance check, not a style one."""
    if not answer or not answer.strip():
        return 15
    lower = answer.lower()
    if any(phrase in lower for phrase in _SCORE_NEGATIVE_PHRASES):
        return 20

    score = base
    q_kw = _extract_score_keywords(query)
    hits = 0
    overlap_ratio = 1.0  # no extractable query keywords (e.g. 1-2 word query) - don't penalize
    if q_kw:
        hits = sum(1 for kw in q_kw if kw in lower)
        overlap_ratio = hits / len(q_kw)
        score += round(overlap_ratio * 40)  # relevance is the only positive driver now

    length = len(answer.strip())
    if length < 40:
        score -= 30  # near-empty stub (e.g. a bare title/filename echo) - push toward 'low'
    elif length < 120:
        score -= 20

    # Gate on BOTH the ratio and the absolute hit count: a long, detailed
    # query can make "1 keyword matched" look like a fine ratio just by
    # having a huge denominator. Matching only a single generic term out of
    # a multi-keyword query is not enough to call something relevant - but
    # if the query itself only ever had 1-2 extractable keywords, require
    # all of them (min(2, len(q_kw))) rather than demanding 2 out of a
    # pool that small.
    relevant_enough = overlap_ratio >= 0.15 and hits >= min(2, len(q_kw) or 1)

    # Quoted phrases in the query (e.g. "Comodo AAA Services") are a
    # deliberate, load-bearing signal - the user named a specific
    # vendor/cert-chain/error string, not just incidental vocabulary.
    # Scattered keyword-overlap can't tell "mentions a certificate" apart
    # from "is about this named certificate": confirmed in practice, a
    # generic "Deployment" page and later an unrelated "alarms not
    # appearing ... CMPv2" page BOTH cleared the ratio/hit-count gate above
    # for a query quoting "Comodo AAA Services"/"AAA Certificate Services",
    # by matching only "certificate"/"root"/"services" - common PKI words
    # that are also part of those exact names - without either page ever
    # mentioning the actual named thing. When the query quotes a phrase,
    # require that phrase to appear (normalized whitespace/case) literally
    # in the content - this OVERRIDES the generic gate in both directions:
    # a verbatim phrase hit is strong enough evidence on its own even if
    # the scattered-keyword ratio was borderline, and its total absence
    # means scattered overlap should not be trusted even if it otherwise
    # cleared the bar above.
    quoted = re.findall(r'"([^"]{3,})"', query)
    if quoted:
        norm_answer = re.sub(r"\s+", " ", lower)
        relevant_enough = any(re.sub(r"\s+", " ", p.lower()) in norm_answer for p in quoted)

    if q_kw and not relevant_enough:
        # Near-zero topical overlap (e.g. a single generic word matched):
        # never read as a credible match for this specific query.
        score = min(score, 38)

    return max(0, min(100, score))


def query_notebook(notebook_id: str, notebook_name: str, notebook_url: str, query: str) -> dict:
    """Run one notebook query via the start+poll async pattern and return a match dict."""
    start_text = nbk_tool(
        "notebook_query_start",
        {"notebook_id": notebook_id, "query": query, "timeout": 180},
        timeout=30.0,
    )
    start_payload = json.loads(start_text)
    if start_payload.get("status") == "error":
        raise RuntimeError(start_payload.get("error", "notebook_query_start failed"))
    query_id = start_payload.get("query_id")
    if not query_id:
        raise RuntimeError(f"No query_id returned: {start_text[:200]}")

    for _ in range(NBK_MAX_POLLS):
        time.sleep(NBK_POLL_INTERVAL)
        status_text = nbk_tool(
            "notebook_query_status",
            {"query_id": query_id},
            timeout=30.0,
        )
        status_payload = json.loads(status_text)
        status = status_payload.get("status")
        if status == "completed":
            result = status_payload.get("result") or {}
            answer = re.sub(r"\s+", " ", str(result.get("answer", ""))).strip()
            return {
                "source": "NBK",
                "title": notebook_name,
                "url": notebook_url,
                "excerpt": answer[:4000],
                "score": score_answer_text(query, answer, base=55),
            }
        if status == "error":
            raise RuntimeError(status_payload.get("error", "notebook_query_status reported an error"))
    raise RuntimeError(f"Timed out waiting for notebook '{notebook_name}'")


def select_relevant_notebooks(query: str, notebooks: list[dict], max_n: int) -> list[dict]:
    """Ask the LLM which configured team notebooks are topically relevant to
    this query, so /troubleshoot only fans out to notebooks that actually
    cover the topic instead of blindly querying the first N configured ones.

    IMPORTANT: an empty [] from the LLM is a *valid, authoritative* answer
    ("nothing in the catalog is relevant to this query") and must be honored
    as-is - it must NOT trigger a fallback to other notebooks. Only a genuine
    failure of the LLM call itself (timeout, malformed response, etc.) falls
    back to a keyword-overlap heuristic, and only as a last resort to naive
    notebooks[:max_n]. Conflating "LLM said no" with "LLM call failed" is
    exactly what caused unrelated notebooks to show up for off-topic queries."""
    catalog = [
        {"key": nb.get("key", ""), "name": nb.get("name", ""), "category": nb.get("category", "")}
        for nb in notebooks
        if nb.get("key")
    ]
    if not catalog:
        return []
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You select which knowledge notebooks are relevant to a support "
                        "query, based only on their name/category. Respond with ONLY a "
                        'JSON array of notebook "key" values (copied exactly from the '
                        "provided list) that are topically relevant to the query. If none "
                        "are clearly relevant, respond with an empty array []. No prose, "
                        "no markdown, just the JSON array."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Query: {query}\n\nNotebooks:\n{json.dumps(catalog, indent=2)}",
                },
            ],
            temperature=0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        selected_keys_raw = json.loads(raw)
        if not isinstance(selected_keys_raw, list):
            raise ValueError("LLM notebook selection did not return a list")
        valid_keys = {nb["key"] for nb in catalog}
        selected_keys = [k for k in selected_keys_raw if k in valid_keys][:max_n]
        by_key = {nb.get("key"): nb for nb in notebooks}
        selected = [by_key[k] for k in selected_keys if k in by_key]
        if not selected:
            logger.info(
                "select_relevant_notebooks: LLM judged no configured notebooks "
                "relevant to query=%r - returning zero NBK matches (this is correct "
                "behaviour, not a failure)",
                query,
            )
        # The LLM call succeeded and returned a structurally valid list - that
        # is authoritative, even when empty. Do NOT fall through to a
        # heuristic fallback here.
        return selected
    except Exception:
        logger.exception(
            "select_relevant_notebooks: LLM classification call failed for query=%r "
            "- using keyword fallback instead of naive notebooks[:%d]",
            query, max_n,
        )

    # Only reachable when the LLM call itself failed (network/timeout/parse
    # error) above. Fall back to a cheap keyword-overlap match against
    # notebook name/category (NOT naive notebooks[:max_n] in config order,
    # which silently favors whichever notebooks happen to be first in
    # team-config.json and could exclude the actually-relevant one, e.g. a
    # newly-added notebook appended at the end).
    q_kw = _extract_score_keywords(query)
    scored = []
    for nb in notebooks:
        label = f"{nb.get('name', '')} {nb.get('category', '')}".lower()
        hits = sum(1 for kw in q_kw if kw in label)
        scored.append((hits, nb))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    relevant = [nb for hits, nb in scored if hits > 0][:max_n]
    if relevant:
        return relevant
    # Still nothing matched by keyword either -- fall back to first max_n so a
    # query still gets *some* NBK coverage rather than none at all.
    return notebooks[:max_n]


def search_notebooks(query: str) -> tuple[list[dict], list[str]]:
    """Query only the team notebooks the LLM judges relevant to this query
    (falls back to the first NBK_MAX_NOTEBOOKS configured notebooks if
    selection fails). Returns (matches, warnings)."""
    all_notebooks = load_team_notebooks()
    notebooks = select_relevant_notebooks(query, all_notebooks, NBK_MAX_NOTEBOOKS)
    jobs = []
    for nb in notebooks:
        notebook_id = nb.get("key", "")
        notebook_name = nb.get("name", notebook_id)
        notebook_url = f"https://notebooklm.google.com/notebook/{notebook_id}" if notebook_id else ""
        if notebook_id:
            jobs.append((notebook_id, notebook_name, notebook_url))

    matches: list[dict] = []
    warnings: list[str] = []
    if not jobs:
        if all_notebooks:
            warnings.append("NBK: no configured notebooks matched this query's topic")
        return matches, warnings

    # Each notebook query is an independent blocking HTTP call (own httpx.Client),
    # so fan them out in parallel instead of waiting on them one at a time -
    # total wait becomes max(notebook_times) instead of sum(notebook_times).
    # Cap concurrency (not len(jobs)) - the nbk-mcp sidecar has a fixed memory
    # limit and firing every selected notebook at once has OOM-killed it
    # (exit 137) when NOTEBOOKLM_MAX_NOTEBOOKS allows more than a couple.
    with ThreadPoolExecutor(max_workers=min(len(jobs), NBK_MAX_PARALLEL)) as pool:
        future_to_name = {
            pool.submit(query_notebook, nid, nname, nurl, query): nname
            for nid, nname, nurl in jobs
        }
        for future in future_to_name:
            notebook_name = future_to_name[future]
            try:
                matches.append(future.result())
            except Exception as exc:  # noqa: BLE001 - one notebook failing shouldn't kill the rest
                warnings.append(f"NBK '{notebook_name}': {str(exc)[:200]}")
    return matches, warnings


def search_kcs(query: str, limit: int = 10) -> tuple[list[dict], list[str]]:
    """Search Red Hat Knowledgebase (KCS Solutions/Articles) via
    mcp-redhat-kcs-server's search_kcs/get_kcs MCP tools. Returns (matches, warnings)."""
    try:
        text = kcs_tool("search_kcs", {"query": query, "rows": limit})
        items = json.loads(text)
    except Exception as exc:  # noqa: BLE001 - one source failing shouldn't kill the others
        return [], [f"KCS: {str(exc)[:300]}"]

    if not isinstance(items, list):
        return [], []

    matches: list[dict] = []
    # Fetch full Issue/Resolution/Root-Cause text for only the top few hits
    # (same "cheap search, fetch full content for the promising ones"
    # pattern used for Confluence) - search_kcs itself has no body text,
    # only id/title/score.
    for item in items[:3]:
        if not isinstance(item, dict):
            continue
        solution_id = item.get("id")
        if not solution_id:
            continue
        try:
            detail_text = kcs_tool("get_kcs", {"solution_id": str(solution_id)})
            detail = json.loads(detail_text)
        except Exception:  # noqa: BLE001 - one bad detail fetch shouldn't drop the rest
            detail = {}
        # Give each section its own budget instead of concatenating-then-
        # blindly-cutting-at-2500: confirmed empirically (KCS solution
        # 6458661, "How to redeploy/renew an expired default ingress
        # certificate in RHOCP?") that Issue+Root Cause text alone can
        # exceed 2500 chars by itself, meaning NONE of Resolution's actual
        # actionable commands (e.g. `oc delete secret router-ca`) ever
        # reached the RCA prompt - the model could only paraphrase vaguely
        # ("follow the KCS article") because the real procedure, which
        # KCS articles consistently put at the END of a long Resolution
        # field, was truncated away before it ever got there. Resolution
        # is the section the RCA synthesis actually needs steps from, so
        # it gets by far the largest share.
        parts = []
        if detail.get("issue"):
            parts.append(f"Issue: {detail['issue'][:700]}")
        if detail.get("root_cause"):
            parts.append(f"Root Cause: {detail['root_cause'][:700]}")
        if detail.get("resolution"):
            parts.append(f"Resolution: {detail['resolution'][:5500]}")
        excerpt = re.sub(r"\s+", " ", " ".join(parts)).strip()[:7500]
        title = detail.get("title") or item.get("title") or "Red Hat KCS solution"
        matches.append({
            "source": "KCS",
            "title": title,
            "url": detail.get("view_uri") or item.get("view_uri") or "",
            "excerpt": excerpt,
            "score": score_answer_text(query, excerpt, base=55) if excerpt else 30,
        })
    return matches, []


def _slack_permalink(channel_id: str, ts: str) -> str:
    if not SLK_WORKSPACE_URL or not channel_id or not ts:
        return ""
    return f"{SLK_WORKSPACE_URL}/archives/{channel_id}/p{ts.replace('.', '')}"


def _parse_slack_matches(query: str, items: list) -> list[dict]:
    """Normalize search_messages()/search_channel_messages() results into the
    shared match shape used by CFL/NBK/KCS. mcp-slack-server's OUTPUT_FORMAT
    env var controls the item shape - manifests/slack-mcp.yaml sets
    OUTPUT_FORMAT=json so items are dicts, but fall back to parsing the
    compact "[ts] @handle: text" string format too, in case that ever
    changes server-side."""
    matches = []
    for item in items:
        if isinstance(item, dict):
            ts = item.get("ts", "")
            text = item.get("text", "")
            channel_id = item.get("channel_id", "")
            channel_name = item.get("channel_name", "")
        elif isinstance(item, str):
            m = re.match(r"^\[(?P<ts>[\d.]+)\]\s*@\S+:\s*(?P<text>.*)$", item, re.S)
            ts = m.group("ts") if m else ""
            text = m.group("text") if m else item
            channel_id = ""
            channel_name = ""
        else:
            continue
        excerpt = re.sub(r"\s+", " ", str(text)).strip()[:2000]
        if not excerpt:
            continue
        matches.append({
            "source": "SLK",
            "title": f"#{channel_name}" if channel_name else "Slack message",
            "url": _slack_permalink(channel_id, ts),
            "excerpt": excerpt,
            "score": score_answer_text(query, excerpt, base=50),
        })
    return matches


def search_slack(query: str, limit: int = 15) -> tuple[list[dict], list[str]]:
    """Search Slack via mcp-slack-server. Scopes to configured team channels
    (team-config.json slack.channels, same [ADD_TEAM_ITEM] pattern as CFL
    spaces/NBK notebooks) when set, else searches the whole workspace.
    Returns (matches, warnings), same contract as search_notebooks()/search_kcs()."""
    channels = [c for c in load_team_channels() if c.get("key")]

    if not channels:
        try:
            text = slk_tool("search_messages", {"query": query, "sort": "score", "limit": limit})
            # Empty text is a legitimate "zero results" response (zero
            # list items means zero content blocks after the _call_mcp()
            # reassembly above) - not a malformed payload. json.loads("")
            # would otherwise raise "Expecting value: line 1 column 1".
            items = json.loads(text) if text.strip() else []
        except Exception as exc:  # noqa: BLE001 - one source failing shouldn't kill the others
            return [], [f"SLK: {str(exc)[:300]}"]
        if not isinstance(items, list):
            return [], []
        return _parse_slack_matches(query, items), []

    # Team channels configured: fan out per-channel like search_notebooks(),
    # capped to a handful of channels rather than the whole configured list.
    jobs = channels[:3]
    per_channel_limit = max(5, limit // len(jobs))
    matches: list[dict] = []
    warnings: list[str] = []

    def _search_one(channel: dict) -> list[dict]:
        text = slk_tool(
            "search_channel_messages",
            {"channel_id": channel["key"], "query": query, "sort": "score", "limit": per_channel_limit},
        )
        items = json.loads(text) if text.strip() else []
        return _parse_slack_matches(query, items) if isinstance(items, list) else []

    with ThreadPoolExecutor(max_workers=min(len(jobs), 3)) as pool:
        future_to_name = {pool.submit(_search_one, ch): ch.get("name", ch["key"]) for ch in jobs}
        for future in future_to_name:
            channel_name = future_to_name[future]
            try:
                matches.extend(future.result())
            except Exception as exc:  # noqa: BLE001 - one channel failing shouldn't kill the rest
                warnings.append(f"SLK '{channel_name}': {str(exc)[:200]}")
    return matches, warnings


# ---------------------------------------------------------------------------
# Team config search/add/remove - lets the UI search each source for spaces/
# notebooks/channels to add to team-config.json, mirroring the canvas's
# [ADD_TEAM_ITEM]/[REMOVE_TEAM_ITEM]/[SEARCH_SPACES]/[REFRESH_*] prompts.

def search_cfl_spaces(query: str, limit: int = 10) -> list[dict]:
    """mcp-confluence-server (this bespoke, API-token-based deployment) has no
    list-spaces tool (confirmed via tools/list - only confluence_search and
    page/attachment/comment tools exist). Approximate 'space search' by
    running a content search and surfacing the unique spaces referenced by
    the hits, same shape as team-config's confluence.spaces entries."""
    query = query.strip()
    if not query:
        return []
    cql = f'type = page AND text ~ "{_safe_cql_text(query)}"'
    try:
        text = cfl_tool("confluence_search", {"query": cql, "limit": limit})
        items = json.loads(text)
    except Exception:  # noqa: BLE001 - search failing shouldn't 500 the endpoint
        return []
    if not isinstance(items, list):
        return []
    seen: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        space = item.get("space") if isinstance(item.get("space"), dict) else {}
        key = space.get("key")
        if key and key not in seen:
            seen[key] = {"key": key, "name": space.get("name") or key, "category": "other"}
    return list(seen.values())


def search_nbk_notebooks(query: str) -> list[dict]:
    """notebook_list returns every notebook the authenticated NotebookLM
    account can see (not just team-config's configured subset) - this is the
    one source that genuinely supports a real 'browse everything' tool, same
    one the canvas's [REFRESH_NOTEBOOKS] uses."""
    text = nbk_tool("notebook_list", {}, timeout=30.0)
    payload = json.loads(text) if text.strip() else []
    if isinstance(payload, dict) and payload.get("status") == "error":
        raise RuntimeError(payload.get("error", "notebook_list failed")[:200])
    items = payload.get("notebooks") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    q = query.strip().lower()
    out = []
    for nb in items:
        if not isinstance(nb, dict):
            continue
        nb_id = nb.get("notebook_id") or nb.get("id") or nb.get("key")
        title = nb.get("title") or nb.get("name") or nb_id
        if not nb_id:
            continue
        if q and q not in str(title).lower():
            continue
        out.append({"key": nb_id, "name": title, "category": "other"})
    return out


def search_slk_channels(query: str, limit: int = 20) -> list[dict]:
    """This mcp-slack-server deployment's list_joined_channels and
    get_channel_id_by_name both return empty for this cookie-session account
    (confirmed live - no channels resolve even for channels known to exist
    from search results), so there is no working channel-browse tool here
    either. Approximate it the same way as CFL spaces: run a message search
    and surface the unique channels referenced by the hits."""
    query = query.strip()
    if not query:
        return []
    try:
        text = slk_tool("search_messages", {"query": query, "sort": "score", "limit": limit})
        items = json.loads(text) if text.strip() else []
    except Exception as exc:  # noqa: BLE001 - search failing shouldn't 500 the endpoint
        raise RuntimeError(f"Slack: {str(exc)[:200]}")
    if not isinstance(items, list):
        return []
    seen: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        channel_id = item.get("channel_id")
        channel_name = item.get("channel_name") or channel_id
        if channel_id and channel_id not in seen:
            seen[channel_id] = {"key": channel_id, "name": channel_name, "category": "other"}
    return list(seen.values())


class TeamConfigItem(BaseModel):
    source: str  # "CFL" | "NBK" | "SLK"
    key: str
    name: str
    category: str = "other"


@app.get("/team-config")
def get_team_config():
    return {
        "spaces": load_team_spaces(),
        "notebooks": load_team_notebooks(),
        "channels": load_team_channels(),
    }


@app.get("/team-config/search")
def team_config_search(source: str, query: str = ""):
    source = source.strip().upper()
    if source not in _TEAM_SECTION:
        raise HTTPException(status_code=400, detail="source must be CFL, NBK, or SLK")
    try:
        if source == "CFL":
            return {"results": search_cfl_spaces(query)}
        if source == "NBK":
            return {"results": search_nbk_notebooks(query)}
        return {"results": search_slk_channels(query)}
    except Exception as exc:  # noqa: BLE001 - surface as a clean 502, not a stack trace
        raise HTTPException(status_code=502, detail=str(exc)[:300])


@app.post("/team-config/items")
def add_team_config_item(item: TeamConfigItem):
    source = item.source.strip().upper()
    if source not in _TEAM_SECTION:
        raise HTTPException(status_code=400, detail="source must be CFL, NBK, or SLK")
    section, list_key = _TEAM_SECTION[source]
    data = json.loads(json.dumps(_team_config_snapshot()))  # deep copy - don't mutate the cache before save succeeds
    existing = data.setdefault(section, {}).setdefault(list_key, [])
    if any(e.get("key") == item.key for e in existing):
        return {"status": "exists", "items": existing}
    existing.append({"key": item.key, "name": item.name, "category": item.category or "other"})
    try:
        save_team_config(data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Failed to save team-config: {str(exc)[:300]}")
    return {"status": "ok", "items": existing}


@app.delete("/team-config/items")
def remove_team_config_item(source: str, key: str):
    source = source.strip().upper()
    if source not in _TEAM_SECTION:
        raise HTTPException(status_code=400, detail="source must be CFL, NBK, or SLK")
    section, list_key = _TEAM_SECTION[source]
    data = json.loads(json.dumps(_team_config_snapshot()))
    existing = data.setdefault(section, {}).setdefault(list_key, [])
    new_list = [e for e in existing if e.get("key") != key]
    data[section][list_key] = new_list
    try:
        save_team_config(data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Failed to save team-config: {str(exc)[:300]}")
    return {"status": "ok", "items": new_list}


_DEFAULT_CLASSIFICATION = {
    "persona": "Site Reliability Engineer (SRE)",
    "template": None,
    "templateName": None,
    "templateChain": [],
    "rationale": "Default fallback - classification unavailable.",
}


def classify_query(query: str) -> dict:
    """Pick the best-matching persona + prompt template for this query, mirroring
    the canvas troubleshoot pipeline's Classify step. Falls back to a sane
    default (SRE persona, no template) if the LLM call fails - classification
    failing should never block search/RCA."""
    personas = _persona_catalog()
    templates = _template_catalog()
    if not personas or not templates:
        return dict(_DEFAULT_CLASSIFICATION)
    catalog = [{"tier": t["tier"], "category": t["category"], "name": t["name"]} for t in templates]
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Classify this support query. Pick exactly one persona from the "
                        "list and exactly one template (by tier+category+name) from the "
                        "list that best matches the query's INTENT. If the query is a "
                        "general informational/exploratory request ('tell me about X', "
                        "'what covers Y', 'show me content on Z') rather than an actual "
                        "incident/error report, prefer a Tier 1 'Fact & Definition "
                        "Retrieval' template (e.g. 'Get component function', 'Get "
                        "standard procedure') over incident-lookup or RCA templates - "
                        "those are only for queries that describe an actual problem, "
                        "error, or outage. Respond with ONLY JSON: {\"persona\": "
                        '"<persona>", "tier": "<tier>", "category": "<category>", '
                        '"name": "<template name>", "rationale": "<one sentence>"}. '
                        "No prose, no markdown."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Query: {query}\n\nPersonas:\n{json.dumps(personas)}\n\n"
                        f"Templates:\n{json.dumps(catalog, indent=2)}"
                    ),
                },
            ],
            temperature=0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        parsed = json.loads(raw)
        persona = parsed.get("persona") or _DEFAULT_CLASSIFICATION["persona"]
        if persona not in personas:
            persona = _DEFAULT_CLASSIFICATION["persona"]
        tmpl = _find_template(parsed.get("name"))
        if not tmpl:
            return {**_DEFAULT_CLASSIFICATION, "persona": persona}
        return {
            "persona": persona,
            "template": f"{tmpl['tier']} | {tmpl['category']} | {tmpl['name']}",
            "templateName": tmpl["name"],
            "templateChain": tmpl.get("chain", []),
            "templateBody": tmpl.get("template", ""),
            "placeholders": tmpl.get("placeholders", []),
            "rationale": parsed.get("rationale", ""),
        }
    except Exception:
        logger.exception("classify_query: LLM classification failed for query=%r", query)
        return dict(_DEFAULT_CLASSIFICATION)


_RUNBOOK_SIGNAL_WORDS = ("runbook", "procedure", "step-by-step", "how to resolve", "workaround")
_RESOLUTION_SIGNAL_WORDS = ("fix", "resolved by", "solution:", "resolve this by", "workaround:")


def _contains_any_word(lower_text: str, words: tuple[str, ...]) -> bool:
    """Word-boundary match instead of plain substring 'in' checks. A naive
    `"fix" in lower` matches "bugfix"/"prefix"/"suffix" too - which is
    exactly how a Red Hat Satellite 'Full Bugfix & RFE Inclusion List'
    article ended up falsely tagged with a 'resolution steps' signal (and
    the score bump that implies) despite having nothing to do with the
    query. \\b boundaries treat "bugfix" as its own token, distinct from
    "fix"."""
    return any(re.search(rf"\b{re.escape(w)}\b", lower_text) for w in words)


def _detect_signals(excerpt: str) -> list[str]:
    signals = []
    lower = excerpt.lower()
    if re.search(r"(^|\n)\s*(\d+\.\s|[-*]\s)", excerpt):
        signals.append("structured steps")
    if _contains_any_word(lower, _RUNBOOK_SIGNAL_WORDS):
        signals.append("runbook")
    if _contains_any_word(lower, _RESOLUTION_SIGNAL_WORDS):
        signals.append("resolution steps")
    if any(p in lower for p in _SCORE_NEGATIVE_PHRASES):
        signals.append("stub/no-answer")
    return signals


def _probable_answer(excerpt: str) -> str:
    if not excerpt or not excerpt.strip():
        return "no clear resolution"
    lower = excerpt.lower()
    if any(p in lower for p in _SCORE_NEGATIVE_PHRASES):
        return "no clear resolution"
    snippet = re.split(r"(?<=[.!?])\s+", excerpt.strip())
    return (snippet[0] if snippet else excerpt)[:280]


def rank_matches(query: str, matches: list[dict]) -> list[dict]:
    """Turn raw per-source matches into the unified RankedMatch structure:
    adds signals, confidence bucket, probableAnswer, and a cross-source
    corroboration bonus when 2+ DIFFERENT sources share strong keyword
    overlap in their excerpts (proxy for 'same resolution appears in
    multiple sources'). Sorts descending and caps at top 8 (single-source)
    or 10 (multi-source), per the source-grounded ranking spec.

    The cap is a CEILING, not a target to pad up to - matches with zero
    query-keyword overlap are dropped entirely rather than kept around to
    fill out the list. Confirmed in practice: two unrelated Slack messages
    (an eMudhra CVE advisory and an unrelated KCS resolution-script PSA)
    both showed "0 query keyword(s) matched" yet still surfaced at Medium
    confidence and took up ranked-match slots, purely because their
    excerpts happened to resemble EACH OTHER closely enough to trigger the
    +15 cross-source corroboration bonus below. Corroboration is meant to
    amplify a match that's already independently relevant to the query,
    not manufacture relevance out of nothing - a match with 0 hits can
    neither give nor receive that bonus, and is excluded from the final
    list regardless of score."""
    if not matches:
        return []
    q_kw = _extract_score_keywords(query)
    enriched = []
    keyword_sets = []
    hit_counts = []
    for m in matches:
        excerpt = m.get("excerpt", "") or ""
        keyword_sets.append(_extract_score_keywords(excerpt))
        hit_counts.append(sum(1 for kw in q_kw if kw in excerpt.lower()) if q_kw else 1)
        enriched.append({
            **m,
            "signals": _detect_signals(excerpt),
            "probableAnswer": _probable_answer(excerpt),
            "sourceRef": m.get("title", ""),
            "sourceUrl": m.get("url", ""),
        })

    n = len(enriched)
    bonus = [0] * n
    for i in range(n):
        if q_kw and hit_counts[i] == 0:
            continue  # can't give or receive corroboration with 0 query relevance
        for j in range(i + 1, n):
            if q_kw and hit_counts[j] == 0:
                continue
            if enriched[i]["source"] == enriched[j]["source"]:
                continue
            a, b = keyword_sets[i], keyword_sets[j]
            if not a or not b:
                continue
            overlap = len(a & b) / max(1, min(len(a), len(b)))
            if overlap >= 0.35:
                bonus[i] = max(bonus[i], 15)
                bonus[j] = max(bonus[j], 15)

    for i, m in enumerate(enriched):
        final_score = max(0, min(100, m.get("score", 50) + bonus[i]))
        m["score"] = final_score
        # Tie-breaker only, stripped before returning below. Two matches
        # can both independently clip to the scoring ceiling (100) while
        # having materially different underlying relevance - confirmed in
        # practice: a CFL match (8 query keywords matched) outranked a KCS
        # match (12 matched) purely because CFL results are appended to
        # the matches list before KCS's in _fanout_and_rank(), and Python's
        # stable sort keeps that insertion order on an exact score tie.
        # That has nothing to do with actual relevance - break ties by the
        # real keyword-hit count instead.
        m["_hits"] = hit_counts[i]
        if bonus[i]:
            m["signals"].append("cross-source corroboration")
        m["confidence"] = "high" if final_score >= 70 else "medium" if final_score >= 40 else "low"
        m["why"] = (f"{hit_counts[i]} query keyword(s) matched; " if q_kw else "") + (
            ", ".join(m["signals"]) if m["signals"] else "general match"
        )

    # Drop zero-overlap matches entirely rather than padding the list up to
    # the cap with them - see docstring. Fewer, grounded matches are
    # strictly better than a full list diluted with noise.
    if q_kw:
        enriched = [m for i, m in enumerate(enriched) if hit_counts[i] > 0]

    enriched.sort(key=lambda x: (x["score"], x.get("_hits", 0)), reverse=True)
    cap = 10 if len({m["source"] for m in enriched}) > 1 else 8
    top = enriched[:cap]
    for idx, m in enumerate(top, start=1):
        m["rank"] = idx
        m.pop("_hits", None)
    return top


def _evidence_block(ranked_matches: list[dict], limit: int = 5) -> str:
    # 1500, then 6000, both still re-truncated every match's already-
    # curated excerpt before the RCA prompt ever saw all of it (see
    # search_kcs()'s own per-section truncation comment for the concrete
    # example that surfaced this - confirmed live that 6000 still cut off
    # 2 of 3 numbered steps in a real KCS article's Resolution section,
    # since search_kcs()'s own excerpt for a content-rich solution can
    # legitimately run close to its 7500-char backstop). 8000 clears that
    # backstop entirely, so a well-curated per-source excerpt is never
    # re-cut at this second layer. To keep total evidence volume (and thus
    # RCA call latency/reliability) in check after raising the per-match
    # cap this much, `limit` was lowered from 8 to 5 - confirmed live that
    # 8 matches x up to 8000 chars each pushed deepseek-r1-distill-qwen-14b
    # (the configured MODEL) to consistently exhaust its whole max_tokens
    # budget on reasoning and return unparsable/empty JSON after all 3
    # retry attempts - a real regression, not a one-off. Fewer, still
    # full-fidelity matches is a better tradeoff than many truncated ones.
    lines = [
        f"[{m['source']}] (score {m['score']}, {m['confidence']}) {m['title']}\n"
        f"URL: {m.get('sourceUrl', '')}\n{(m.get('excerpt', '') or '')[:8000]}"
        for m in ranked_matches[:limit]
    ]
    return "\n\n".join(lines) or "No matches from Confluence, NotebookLM, Red Hat KCS, or Slack."


def _chat_completion_with_retry(
    messages: list[dict], temperature: float, retries: int = 1, max_tokens: int | None = None
):
    """client.chat.completions.create() with one short retry on a
    TRANSIENT connection failure (confirmed in practice:
    openai.APIConnectionError / httpx RemoteProtocolError, "Server
    disconnected without sending a response", from the LiteMaaS gateway -
    not a malformed request, just a dropped connection) instead of
    treating every such blip as a hard failure. Chain-step and RCA-
    synthesis calls previously had no retry at all for this case (only
    the final RCA call retried on an EMPTY completion, a different,
    unrelated edge case for deepseek-r1-distill) - a single transient
    gateway disconnect was silently turning into a visible '(step failed)'
    placeholder or the generic 'no structured answer' RCA fallback, for a
    failure that a second attempt usually clears on its own."""
    last_exc: Exception | None = None
    extra: dict = {"max_tokens": max_tokens} if max_tokens else {}
    for attempt in range(retries + 1):
        try:
            return client.chat.completions.create(
                model=MODEL, messages=messages, temperature=temperature, **extra
            )
        except Exception as exc:  # noqa: BLE001 - decide retry vs. re-raise below
            last_exc = exc
            if attempt < retries:
                logger.warning(
                    "run_prompt_chain: LLM call failed (attempt %d/%d), retrying: %s",
                    attempt + 1, retries + 1, str(exc)[:200],
                )
                time.sleep(1.5)
                continue
            raise
    raise last_exc  # pragma: no cover - loop always returns or raises above


def run_prompt_chain(query: str, classification: dict, ranked_matches: list[dict]) -> tuple[list[dict], dict]:
    """Runs up to 3 local synthesis steps over the ranked evidence, mirroring
    the canvas pipeline's Classify -> Prompt Chain -> Synthesize RCA steps:
    1) apply the classified template to interpret the query against evidence
    2) follow that template's configured `chain` hop if one exists (e.g. turn
       a diagnosis into a runbook)
    3) always finish with a structured RCA synthesis grounded in the same
       ranked evidence - capped at 3 steps total."""
    evidence = _evidence_block(ranked_matches)
    persona = classification.get("persona") or _DEFAULT_CLASSIFICATION["persona"]
    persona_data = (load_json_config(PERSONA_FILE).get("personas") or {}).get(persona, {})
    chain_steps: list[dict] = []

    step_names = []
    if classification.get("templateName"):
        step_names.append(classification["templateName"])
        next_names = classification.get("templateChain") or []
        if next_names:
            step_names.append(next_names[0])
    step_names = step_names[:2]  # 3rd step below is always the RCA synthesis

    running_context = ""
    for step_name in step_names:
        tmpl = _find_template(step_name)
        if not tmpl:
            continue
        try:
            resp = _chat_completion_with_retry(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            f"You are acting as: {persona} ({persona_data.get('role', '')}). "
                            f"{persona_data.get('constraints', '')} "
                            "Ground every claim in the evidence provided. If evidence is "
                            "empty or weak, say so plainly."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Template: {tmpl['name']}\n{tmpl['template']}\n\n"
                            f"Query: {query}\n\n{running_context}\n\n## Evidence\n{evidence}"
                        ),
                    },
                ],
                temperature=0.2,
            )
            summary = (resp.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001 - one chain step failing shouldn't kill the rest
            logger.exception("run_prompt_chain: step %r failed for query=%r", step_name, query)
            summary = f"(step failed: {str(exc)[:150]})"
        # NOTE: the 800-char cap used to be applied to the SAME string that
        # got stored for display, hard-truncating mid-sentence/mid-word
        # (e.g. a multi-section runbook cut off at "...Edit t"). Keep the
        # cap only for running_context (bounding prompt growth across
        # chain steps) - the user-visible chain_steps entry keeps the full,
        # untruncated model output.
        chain_steps.append({"step": len(chain_steps) + 1, "template": step_name, "summary": summary})
        running_context += f"\n\nPrior step ({step_name}) result:\n{summary[:800]}"

    governance = _mdc_governance_text()
    rca_system = (
        f"You are acting as: {persona} ({persona_data.get('role', '')}). "
        f"{persona_data.get('constraints', '')} "
        + (f"\n\n{governance}\n\n" if governance else " ")
        + "Respond with ONLY strict JSON (no markdown fences, no prose outside "
        'the JSON) matching this shape: {"summary": str, "rootCause": str, '
        '"steps": [{"action": str, "detail": str, "sources": [str]}], '
        '"verification": str}. Every steps[].detail must cite its source tag '
        "([CFL]/[NBK]/[KCS]/[SLK]) inline. "
        "CRITICAL: when the Evidence contains literal CLI commands, YAML/JSON "
        "snippets, file paths, or flag names (e.g. `oc delete secret "
        "router-ca`), REPRODUCE THEM VERBATIM, character-for-character, "
        "inside the relevant steps[].detail - do not paraphrase a concrete, "
        "copy-pasteable command into generic prose like 'remove the secret' "
        "or 'follow the documented procedure'. Users need commands they can "
        "run, not a description of what a command does. If the evidence has "
        "multiple numbered steps with their own commands, each should "
        "usually become its own steps[] entry carrying its own command(s), "
        "not one step that vaguely summarizes all of them. "
        "IMPORTANT: base your judgment entirely on the Evidence section below, "
        "NOT on whether the user's query itself contains an explicit error code "
        "or incident report. Many valid queries are informational/exploratory "
        "('tell me about X', 'what covers Y') rather than incident reports - for "
        "those, use 'rootCause' to mean 'key explanation' and 'steps' to mean "
        "'key procedures or facts from the evidence', still grounded in and "
        "citing the evidence. The evidence comes from many different human "
        "authors across Confluence/NotebookLM/KCS/Slack with wildly different "
        "formatting discipline - a short, plainly-written, badly-formatted "
        "note can be the correct answer, and a long, polished, bulleted "
        "article can be irrelevant to this specific query. Judge each piece "
        "of evidence purely on whether its CONTENT actually answers the "
        "query, never on how well-formatted, long, or official-looking it "
        "is. Only say no matching content was found in "
        '"summary"/"rootCause" (and leave "steps" as []) if the Evidence '
        "section itself is genuinely empty, irrelevant, or contradictory - "
        "never simply because the query lacks a literal error code."
    )
    rca_user = f"Query: {query}\n\n{running_context}\n\n## Evidence\n{evidence}"

    def _call_rca() -> tuple[str, str, str]:
        """Returns (content, reasoning_content, finish_reason). Confirmed
        live on the cluster that this RCA call can come back with an
        EMPTY message.content three times in a row for the exact same
        prompt (not a one-off flake) while reasoning_content is long and
        actually contains the worked-out answer - deepseek-r1-distill on
        this gateway will sometimes spend its whole token budget on
        chain-of-thought and never emit the final content. An explicit,
        generous max_tokens gives it enough room to finish reasoning AND
        still write the content. Bumped 6000 -> 9000 after confirming live
        that growing the evidence block (to stop truncating real KCS
        procedure steps) made this run out of budget mid-reasoning more
        often, not less - more context to reason over means more
        chain-of-thought tokens burned before it even starts the answer."""
        resp = _chat_completion_with_retry(
            messages=[
                {"role": "system", "content": rca_system},
                {"role": "user", "content": rca_user},
            ],
            temperature=0,
            max_tokens=9000,
        )
        msg = resp.choices[0].message
        content = (msg.content or "").strip()
        reasoning = (getattr(msg, "reasoning_content", "") or "").strip()
        finish_reason = resp.choices[0].finish_reason or ""
        return content, reasoning, finish_reason

    def _extract_json_object(text: str) -> str:
        """Best-effort extraction of a {...} JSON object out of a larger
        blob of text (e.g. a reasoning trace that ends with the answer but
        isn't strictly just the JSON) - takes the outermost balanced-brace
        span, since reasoning_content commonly reads like '...so the
        answer is: {"summary": ...}' with narration before/after it."""
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return text
        return text[start : end + 1]

    try:
        # Up to 3 attempts total before giving up - confirmed empty/
        # unparsable completions can repeat across multiple attempts for
        # the same large evidence block, not just a single transient blip.
        def _clean_json_candidate(text: str) -> str:
            text = text.strip()
            if text.startswith("```"):
                text = text.strip("`")
                text = text.split("\n", 1)[1] if "\n" in text else text
            return _extract_json_object(text).strip()

        structured = None
        for attempt in range(3):
            content, reasoning, finish_reason = _call_rca()
            # Confirmed live: `content` is sometimes non-empty but still
            # unparsable as-is (e.g. "Here is the analysis:\n```json\n{...}"
            # - the model wraps the JSON in prose/fences despite the
            # "ONLY strict JSON" instruction). Try content first, and if
            # that doesn't parse, try the reasoning trace before treating
            # this attempt as a failure and retrying.
            for candidate_name, candidate_text in (("content", content), ("reasoning", reasoning)):
                if not candidate_text:
                    continue
                try:
                    structured = json.loads(_clean_json_candidate(candidate_text))
                    raw = candidate_text
                    break
                except (json.JSONDecodeError, ValueError):
                    continue
            if structured is not None:
                break
            logger.info(
                "run_prompt_chain: RCA attempt %d/3 produced no parsable JSON for "
                "query=%r (finish_reason=%s, content_len=%d, reasoning_len=%d), retrying",
                attempt + 1, query, finish_reason, len(content), len(reasoning),
            )
        if structured is None:
            raise ValueError("no parsable RCA JSON after 3 attempts")
    except Exception:
        logger.exception("run_prompt_chain: structured RCA synthesis failed for query=%r", query)
        structured = {
            "summary": (
                "Automated RCA synthesis did not return a structured answer for this query. "
                "The ranked matches and Sources below are the raw evidence that was retrieved."
            ),
            "rootCause": "",
            "steps": [],
            "verification": "",
        }

    final_template = "Perform a guided RCA" if _find_template("Perform a guided RCA") else "Synthesize RCA"
    chain_steps.append({
        "step": len(chain_steps) + 1,
        "template": final_template,
        "summary": structured.get("summary") or "",
    })
    structured["sources"] = [
        {
            "source": m["source"],
            "title": m["title"],
            "url": m.get("sourceUrl", ""),
            "excerpt": (m.get("excerpt", "") or "")[:500],
        }
        for m in ranked_matches[:8]
    ]
    return chain_steps, structured


def _dedupe_matches(matches: list[dict]) -> list[dict]:
    """Defensive guard against any single source returning the same item
    twice (backend search pagination overlap, a repeated KCS solution ID, a
    Slack message double-indexed under more than one highlight, etc.) -
    verified empirically with no duplicates in production, but nothing in
    search_confluence/search_kcs/search_slack/search_notebooks actually
    guards against it themselves, so this closes that gap for every source
    uniformly instead of patching each parser. Keeps the first occurrence,
    deduping on (source, url) when a URL exists, falling back to
    (source, title, excerpt-prefix) when it doesn't."""
    seen: set[tuple] = set()
    out = []
    for m in matches:
        url = m.get("url") or ""
        if url:
            key = (m.get("source"), url)
        else:
            key = (m.get("source"), m.get("title", ""), (m.get("excerpt") or "")[:200])
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def _fanout_and_rank(query: str, selected: set[str]) -> tuple[list[dict], list[str], dict, list[dict]]:
    """Steps 1-5 of the troubleshoot pipeline (parse sources -> classify ->
    fan-out search -> rank matches), shared by /troubleshoot and
    /find-matches. The only difference between those two endpoints is
    whether run_prompt_chain() (RCA synthesis) runs afterwards - this
    function stops right before that step, same split as the canvas's
    Troubleshoot Mode vs. Find Matches Mode in debugagent-hub.mdc."""
    matches: list[dict] = []
    warnings: list[str] = []
    classification = dict(_DEFAULT_CLASSIFICATION)

    # Confluence, NotebookLM, Red Hat KCS, Slack, and classification are
    # independent - run them all in parallel rather than waiting on each in
    # sequence. Only the sources the caller actually selected get submitted at all.
    with ThreadPoolExecutor(max_workers=5) as pool:
        cfl_future = pool.submit(search_confluence, query) if "CFL" in selected else None
        nbk_future = pool.submit(search_notebooks, query) if "NBK" in selected else None
        kcs_future = pool.submit(search_kcs, query) if "KCS" in selected else None
        slk_future = pool.submit(search_slack, query) if "SLK" in selected else None
        classify_future = pool.submit(classify_query, query)

        if cfl_future is not None:
            try:
                matches.extend(cfl_future.result())
            except Exception as e:  # noqa: BLE001 - partial failure across sources is expected
                warnings.append(f"CFL: {str(e)[:300]}")

        if nbk_future is not None:
            try:
                nbk_matches, nbk_warnings = nbk_future.result()
                matches.extend(nbk_matches)
                warnings.extend(nbk_warnings)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"NBK: {str(e)[:300]}")

        if kcs_future is not None:
            try:
                kcs_matches, kcs_warnings = kcs_future.result()
                matches.extend(kcs_matches)
                warnings.extend(kcs_warnings)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"KCS: {str(e)[:300]}")

        if slk_future is not None:
            try:
                slk_matches, slk_warnings = slk_future.result()
                matches.extend(slk_matches)
                warnings.extend(slk_warnings)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"SLK: {str(e)[:300]}")

        try:
            classification = classify_future.result()
        except Exception:  # noqa: BLE001 - classification failing shouldn't block the rest
            logger.exception("_fanout_and_rank: classify_query raised for query=%r", query)

    matches = _dedupe_matches(matches)
    ranked_matches = rank_matches(query, matches)
    return matches, warnings, classification, ranked_matches


@app.post("/troubleshoot")
def troubleshoot(req: TroubleshootRequest):
    selected = {s.strip().upper() for s in (req.sources or [])} or {"CFL", "NBK", "KCS", "SLK"}
    matches, warnings, classification, ranked_matches = _fanout_and_rank(req.query, selected)

    if not ranked_matches:
        return {
            "matches": matches,
            "warnings": warnings,
            "classification": classification,
            "rankedMatches": [],
            "promptChain": [],
            "rca": {
                "summary": "No documentation found for this query across selected sources.",
                "rootCause": "",
                "steps": [],
                "verification": "",
                "sources": [],
            },
            "noResults": True,
        }

    prompt_chain, structured_rca = run_prompt_chain(req.query, classification, ranked_matches)

    return {
        "matches": matches,
        "warnings": warnings,
        "classification": classification,
        "rankedMatches": ranked_matches,
        "promptChain": prompt_chain,
        "rca": structured_rca,
    }


@app.post("/find-matches")
def find_matches(req: TroubleshootRequest):
    """Fast 'what docs/threads exist about this?' pass - Steps 1-5 of the
    troubleshoot pipeline (classify + fan-out search + rank) with NO RCA
    synthesis, mirroring the canvas's [FIND_MATCHES] mode. Much cheaper/faster
    than /troubleshoot since it skips run_prompt_chain()'s LLM calls entirely."""
    selected = {s.strip().upper() for s in (req.sources or [])} or {"CFL", "NBK", "KCS", "SLK"}
    matches, warnings, classification, ranked_matches = _fanout_and_rank(req.query, selected)

    return {
        "matches": matches,
        "warnings": warnings,
        "classification": classification,
        "rankedMatches": ranked_matches,
        "noResults": not ranked_matches,
    }
