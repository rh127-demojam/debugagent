#!/usr/bin/env bash
# Single end-to-end deploy for a brand-new, empty OpenShift cluster (same
# platform as the source environment - BuildConfigs/Routes/ImageStreams all
# apply as-is).
#
# Prerequisites:
#   - `oc` installed and logged in (`oc login ...`) with cluster-admin or
#     equivalent (creates Namespaces, RBAC, NetworkPolicies).
#   - Run from the repo root, on a machine that has both `oc` and this repo
#     checked out (e.g. scp/clone the whole repo onto your bastion first).
#   - Secrets CANNOT be safely automated for Slack/NotebookLM (live third-
#     party browser sessions / interactive OAuth - no safe placeholder).
#     Their Deployments are SKIPPED if the Secret is missing; re-run this
#     script after applying it (see manifests/secrets.example.yaml and
#     scripts/refresh-{slack,nbk}-creds.ps1).
#   - Confluence/KCS use static tokens that DO have a UI paste-a-token flow
#     (orchestrator's /credentials/{source}, wired to the Streamlit Connect
#     popover) - but that's an UPDATE (HTTP PATCH), not a create, so this
#     script seeds a REPLACE_VIA_UI placeholder Secret for either one if
#     missing, so their Deployments come up immediately and the real token
#     can be set entirely through the UI afterwards, with no oc command.
#   - debug-agent-litemaas (LiteLLM/LiteMaaS gateway creds) has no UI flow
#     either - orchestrator deploys but classification/RCA will fail until
#     it's applied manually (see manifests/secrets.example.yaml).
#   - Every step here is idempotent (`oc apply` / `oc create --dry-run=client
#     -o yaml | oc apply -f -` / `oc create secret` guarded by an existence
#     check) - safe to re-run after fixing any of the above.
#
# Usage:
#   ./scripts/deploy-fresh-environment.sh
#   CONFLUENCE_BASE_URL=https://acme.atlassian.net SLACK_WORKSPACE_URL=https://acme.slack.com \
#     ./scripts/deploy-fresh-environment.sh
set -euo pipefail
cd "$(dirname "$0")/.."

NS_HUB="debug-agent-hub"
NS_MCP="debug-agent-mcp"
CONFLUENCE_BASE_URL="${CONFLUENCE_BASE_URL:-__CONFLUENCE_BASE_URL__}"
SLACK_WORKSPACE_URL="${SLACK_WORKSPACE_URL:-__SLACK_WORKSPACE_URL__}"

log()  { echo "[deploy] $*"; }
warn() { echo "[deploy] WARNING: $*" >&2; }

secret_exists() { oc -n "$1" get secret "$2" >/dev/null 2>&1; }
cm_exists()     { oc -n "$1" get configmap "$2" >/dev/null 2>&1; }

echo "== Debug Agent Hub: fresh-environment deploy =="

# --- 0. sanity -------------------------------------------------------------
command -v oc >/dev/null || { echo "oc CLI not found on PATH"; exit 1; }
oc whoami >/dev/null 2>&1 || { echo "Not logged into an OpenShift cluster - run 'oc login' first"; exit 1; }
log "Logged in as $(oc whoami) against $(oc whoami --show-server)"

# --- 1. namespaces -----------------------------------------------------------
log "Applying namespaces..."
oc apply -f manifests/namespaces.yaml

# --- 2. RBAC -----------------------------------------------------------------
log "Applying RBAC (credentials editor, team-config editor)..."
oc apply -f manifests/orchestrator-credentials-rbac.yaml
oc apply -f manifests/orchestrator-team-config-rbac.yaml

# --- 3. debug-agent-config ConfigMap (generated from the real repo files) ---
log "Generating debug-agent-config ConfigMap from personas-slim.json / prompt-templates-slim.json / team-config.json..."
oc create configmap debug-agent-config -n "$NS_HUB" \
  --from-file=personas-slim.json \
  --from-file=prompt-templates-slim.json \
  --from-file=team-config.json \
  --dry-run=client -o yaml | oc apply -f -

# --- 4. seed placeholder Secrets for CFL/KCS, then check what we actually have
#
# CFL and KCS are the only two sources with a UI paste-a-token flow
# (POST /credentials/{source} in app.py, wired to the Streamlit "Connect"
# popover) - but that flow does an HTTP PATCH, which 404s on a Secret/
# Deployment that doesn't exist yet. So unlike Slack/NBK (live browser
# sessions / interactive OAuth - no safe placeholder, no UI paste
# equivalent, must be provisioned out-of-band - see manifests/secrets.example.yaml),
# CFL/KCS get an initial placeholder Secret here if missing. This makes
# their Deployments come up immediately (readinessProbe is a plain TCP
# check, not an auth check) so a user can set the REAL token entirely
# through the UI on a brand-new cluster, with zero oc commands.
SEEDED_PLACEHOLDER=()
seed_placeholder_secret() {
  local ns="$1" name="$2"; shift 2
  if ! secret_exists "$ns" "$name"; then
    log "Seeding placeholder Secret $name in $ns (replace the real value via the UI's Connect popover, or 'oc patch secret')..."
    oc create secret generic "$name" -n "$ns" "$@"
    SEEDED_PLACEHOLDER+=("$name")
  fi
}
seed_placeholder_secret "$NS_MCP" confluence-mcp-creds \
  --from-literal=CONFLUENCE_URL="https://REPLACE_ME.atlassian.net/wiki" \
  --from-literal=CONFLUENCE_USERNAME="REPLACE_ME@example.com" \
  --from-literal=CONFLUENCE_API_TOKEN="REPLACE_VIA_UI"
seed_placeholder_secret "$NS_MCP" redhat-kcs-mcp-creds \
  --from-literal=RHKB_OFFLINE_TOKEN="REPLACE_VIA_UI"

declare -A HAVE
HAVE[litemaas_secret]=0;  secret_exists "$NS_HUB" debug-agent-litemaas  && HAVE[litemaas_secret]=1
HAVE[litemaas_cm]=0;      cm_exists     "$NS_HUB" debug-agent-litemaas  && HAVE[litemaas_cm]=1
HAVE[confluence]=0;       secret_exists "$NS_MCP" confluence-mcp-creds  && HAVE[confluence]=1
HAVE[kcs]=0;              secret_exists "$NS_MCP" redhat-kcs-mcp-creds  && HAVE[kcs]=1
HAVE[slack]=0;            secret_exists "$NS_MCP" mcp-slack-creds       && HAVE[slack]=1
HAVE[nbk]=0;              secret_exists "$NS_MCP" nbk-mcp-creds         && HAVE[nbk]=1

if [ "${HAVE[litemaas_secret]}" = 0 ] || [ "${HAVE[litemaas_cm]}" = 0 ]; then
  warn "debug-agent-litemaas Secret/ConfigMap missing in $NS_HUB - orchestrator will deploy but LLM"
  warn "  classification/RCA + direct Confluence REST calls will fail. See manifests/secrets.example.yaml."
fi
[ "${HAVE[slack]}" = 1 ]      || warn "mcp-slack-creds missing in $NS_MCP - skipping mcp-slack-server (see scripts/refresh-slack-creds.ps1)."
[ "${HAVE[nbk]}" = 1 ]        || warn "nbk-mcp-creds missing in $NS_MCP - skipping nbk-mcp-server (see scripts/refresh-nbk-creds.ps1)."

# --- 5. BuildConfigs + ImageStreams, then trigger builds --------------------
log "Applying BuildConfigs/ImageStreams..."
oc apply -f manifests/buildconfigs.yaml

# services/orchestrator/config/{personas-slim,prompt-templates-slim,team-config}.json
# are a build artifact, not a second place to edit - always resync from the
# repo-root copies (the same ones step 3 used for the ConfigMap) right
# before building, so the baked-in image default can never silently drift
# from what's actually deployed.
log "Syncing personas/prompt-templates/team-config into services/orchestrator/config/..."
cp personas-slim.json prompt-templates-slim.json team-config.json services/orchestrator/config/

log "Building orchestrator image..."
oc -n "$NS_HUB" start-build orchestrator --from-dir=services/orchestrator --follow

log "Building debug-agent-ui image..."
oc -n "$NS_HUB" start-build debug-agent-ui --from-dir=services/streamlit-ui --follow

if [ "${HAVE[kcs]}" = 1 ]; then
  log "Building mcp-redhat-kcs-server image..."
  oc -n "$NS_MCP" start-build mcp-redhat-kcs-server --from-dir=services/kcs-mcp --follow
fi

if [ "${HAVE[nbk]}" = 1 ]; then
  log "Building nbk-mcp image..."
  oc -n "$NS_MCP" start-build nbk-mcp --from-dir=services/nbk-mcp --follow
fi

# --- 6. Deployments ----------------------------------------------------------
log "Deploying orchestrator (Deployment+Service+Route)..."
oc apply -f manifests/orchestrator.yaml

log "Deploying debug-agent-ui (Deployment+Service+Route)..."
oc apply -f manifests/streamlit-ui.yaml

if [ "${HAVE[confluence]}" = 1 ]; then
  log "Deploying mcp-confluence-server..."
  oc apply -f manifests/confluence-mcp.yaml
fi

if [ "${HAVE[kcs]}" = 1 ]; then
  log "Deploying mcp-redhat-kcs-server..."
  oc apply -f manifests/kcs-mcp.yaml
fi

if [ "${HAVE[slack]}" = 1 ]; then
  log "Deploying mcp-slack-server (+ NetworkPolicy)..."
  oc apply -f manifests/slack-mcp.yaml
fi

if [ "${HAVE[nbk]}" = 1 ]; then
  log "Deploying nbk-mcp-server (+ NetworkPolicy)..."
  oc apply -f manifests/nbk-mcp.yaml
fi

# --- 7. resolve Route hosts, patch orchestrator env -------------------------
log "Resolving Route hostnames and patching orchestrator env..."
ORCH_HOST="$(oc get route debug-agent-orchestrator -n "$NS_HUB" -o jsonpath='{.spec.host}')"
UI_HOST="$(oc get route debug-agent-ui -n "$NS_HUB" -o jsonpath='{.spec.host}')"
oc set env deploy/debug-agent-orchestrator -n "$NS_HUB" \
  PUBLIC_BASE_URL="https://${ORCH_HOST}" \
  UI_URL="https://${UI_HOST}" \
  CONFLUENCE_BASE_URL="${CONFLUENCE_BASE_URL}" \
  SLACK_WORKSPACE_URL="${SLACK_WORKSPACE_URL}"

# --- 8. wait for readiness ---------------------------------------------------
log "Waiting for rollouts..."
oc rollout status deploy/debug-agent-orchestrator -n "$NS_HUB" --timeout=180s
oc rollout status deploy/debug-agent-ui -n "$NS_HUB" --timeout=180s
[ "${HAVE[confluence]}" = 1 ] && oc rollout status deploy/mcp-confluence-server -n "$NS_MCP" --timeout=120s
[ "${HAVE[kcs]}" = 1 ]        && oc rollout status deploy/mcp-redhat-kcs-server -n "$NS_MCP" --timeout=120s
[ "${HAVE[slack]}" = 1 ]      && oc rollout status deploy/mcp-slack-server -n "$NS_MCP" --timeout=120s
[ "${HAVE[nbk]}" = 1 ]        && oc rollout status deploy/nbk-mcp-server -n "$NS_MCP" --timeout=120s

echo ""
echo "== Deploy complete =="
echo "  Orchestrator: https://${ORCH_HOST}"
echo "  UI:           https://${UI_HOST}"
if [ ${#SEEDED_PLACEHOLDER[@]} -gt 0 ]; then
  echo "  ACTION NEEDED: these Secrets were seeded with a REPLACE_VIA_UI placeholder token:"
  for s in "${SEEDED_PLACEHOLDER[@]}"; do
    echo "    - $s (set the real value via the UI's Connect popover, or 'oc patch secret $s ...')"
  done
fi
for key in confluence kcs slack nbk; do
  if [ "${HAVE[$key]}" = 0 ]; then
    echo "  SKIPPED (missing creds): $key - see manifests/secrets.example.yaml, then re-run this script."
  fi
done
