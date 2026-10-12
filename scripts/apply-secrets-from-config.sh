#!/usr/bin/env bash
# Reads manifests/secrets.local.env (ships pre-named exactly this in the
# deploy bundle - just fill in real values directly, no copy/rename step)
# and creates/updates the real debug-agent-litemaas Secret + ConfigMap on the
# cluster - the one credential with no safe placeholder, so it has to be
# real before the orchestrator can start at all.
#
# Called automatically by scripts/deploy-fresh-environment.sh early in its
# run (before the orchestrator Deployment is first applied) if this config
# file exists, so a fresh deploy comes up with real values from the start -
# no separate manual step, no crash-loop-then-recover cycle. Also callable
# standalone to update an already-running cluster (e.g. rotating an expired
# LiteMaaS key): just edit the file and re-run this script.
#
# Idempotent + safe to re-run: uses
# `oc create ... --dry-run=client -o yaml | oc apply -f -`, which UPDATES an
# existing Secret/ConfigMap in place rather than erroring on "already
# exists". Only restarts the orchestrator Deployment if it already exists
# (on a brand-new cluster it won't yet - that's fine, it'll just pick up
# these values on its first-ever start a few steps later in deploy.sh).
#
# Every other source (Confluence, Red Hat KCS, Slack, NotebookLM) is
# UI-driven instead (paste-a-token Configure popover for Confluence/KCS,
# file-upload Configure popover for Slack/NotebookLM) - this script does
# NOT touch any of those.
#
# Usage:
#   # edit manifests/secrets.local.env with real values (ships pre-named,
#   # no copy step needed)
#   ./scripts/apply-secrets-from-config.sh [path-to-config]
set -euo pipefail
cd "$(dirname "$0")/.."

CONFIG_FILE="${1:-manifests/secrets.local.env}"
NS_HUB="debug-agent-hub"
NS_MCP="debug-agent-mcp"

log()  { echo "[apply-secrets] $*"; }
warn() { echo "[apply-secrets] WARNING: $*" >&2; }

# Restarts a Deployment only if it already exists. On a brand-new cluster,
# debug-agent-litemaas gets applied BEFORE the orchestrator Deployment
# itself is ever created - there's nothing to restart yet, and that's
# expected, not an error. If the Deployment DOES exist and the restart
# still fails, that's a real problem worth a loud warning.
restart_if_exists() {
  local ns="$1" name="$2"
  if oc get deploy "$name" -n "$ns" >/dev/null 2>&1; then
    if ! oc rollout restart "deploy/$name" -n "$ns"; then
      warn "Deployment $name exists but rollout restart FAILED - it may still be running the OLD value."
      warn "  Fix manually: oc rollout restart deploy/$name -n $ns"
    fi
  fi
}

if [ ! -f "$CONFIG_FILE" ]; then
  echo "Config file not found: $CONFIG_FILE"
  echo "Fill in real values in $CONFIG_FILE, then re-run."
  exit 1
fi

command -v oc >/dev/null || { echo "oc CLI not found on PATH"; exit 1; }
oc whoami >/dev/null 2>&1 || { echo "Not logged into an OpenShift cluster - run 'oc login' first"; exit 1; }
log "Logged in as $(oc whoami) against $(oc whoami --show-server)"
log "Reading config from $CONFIG_FILE"

# Real deployers fill this file in on a Windows machine (Notepad, VSCode
# without an LF line-ending setting, copy/paste from a chat, etc.), which
# silently reintroduces \r line endings even when the file this script
# ships started out as clean LF - confirmed in practice: `source`-ing a
# file with \r endings fails per-line with "$CONFIG_FILE: line N: $'\r':
# command not found" for every single VAR=value line, which aborts this
# whole script (set -euo pipefail) before the Secret/ConfigMap ever gets
# created, leaving the orchestrator to crash-loop with no real LiteMaaS
# credentials. Strip them unconditionally and silently here instead of
# hoping the file is already clean - this is the single, narrow point
# where the file is read, so fixing it here covers every call path
# (deploy-fresh-environment.sh's auto-call and any standalone re-run).
if grep -q $'\r' "$CONFIG_FILE" 2>/dev/null; then
  log "Detected Windows CRLF line endings in $CONFIG_FILE - stripping before sourcing..."
  sed -i 's/\r$//' "$CONFIG_FILE"
fi

set -a
# shellcheck disable=SC1090
source "$CONFIG_FILE"
set +a

if [ -z "${LITEMAAS_BASE_URL:-}" ] || [ -z "${LITEMAAS_API_KEY:-}" ] || [ -z "${MODEL_NAME:-}" ]; then
  echo "Nothing applied - LITEMAAS_BASE_URL / LITEMAAS_API_KEY / MODEL_NAME not all set in $CONFIG_FILE"
  exit 1
fi

log "Applying debug-agent-litemaas Secret + ConfigMap in $NS_HUB..."
oc create secret generic debug-agent-litemaas -n "$NS_HUB" \
  --from-literal=LITEMAAS_API_KEY="$LITEMAAS_API_KEY" \
  --from-literal=ATLASSIAN_OAUTH_CLIENT_ID="${LITEMAAS_ATLASSIAN_OAUTH_CLIENT_ID:-}" \
  --from-literal=ATLASSIAN_OAUTH_CLIENT_SECRET="${LITEMAAS_ATLASSIAN_OAUTH_CLIENT_SECRET:-}" \
  --from-literal=CONFLUENCE_EMAIL="${LITEMAAS_CONFLUENCE_EMAIL:-}" \
  --from-literal=CONFLUENCE_API_TOKEN="${LITEMAAS_CONFLUENCE_API_TOKEN:-}" \
  --dry-run=client -o yaml | oc apply -f -
oc create configmap debug-agent-litemaas -n "$NS_HUB" \
  --from-literal=LITEMAAS_BASE_URL="$LITEMAAS_BASE_URL" \
  --from-literal=MODEL_NAME="$MODEL_NAME" \
  --dry-run=client -o yaml | oc apply -f -
restart_if_exists "$NS_HUB" "debug-agent-orchestrator"

echo "== Applied: debug-agent-litemaas (Secret+ConfigMap) =="

# CONFLUENCE_URL is OPTIONAL - only acts if the user actually filled it in.
# Uses a merge PATCH (not create|apply, which would replace the whole
# Secret) so a real CONFLUENCE_USERNAME/CONFLUENCE_API_TOKEN already pasted
# in via the UI is never clobbered - only the URL key changes.
if [ -n "${CONFLUENCE_URL:-}" ]; then
  log "Applying CONFLUENCE_URL override in $NS_MCP..."
  if oc get secret confluence-mcp-creds -n "$NS_MCP" >/dev/null 2>&1; then
    oc patch secret confluence-mcp-creds -n "$NS_MCP" --type merge \
      -p "{\"stringData\":{\"CONFLUENCE_URL\":\"$CONFLUENCE_URL\"}}"
  else
    oc create secret generic confluence-mcp-creds -n "$NS_MCP" \
      --from-literal=CONFLUENCE_URL="$CONFLUENCE_URL" \
      --from-literal=CONFLUENCE_USERNAME="REPLACE_ME@example.com" \
      --from-literal=CONFLUENCE_API_TOKEN="REPLACE_VIA_UI"
  fi
  restart_if_exists "$NS_MCP" "mcp-confluence-server"
  echo "== Applied: confluence-mcp-creds CONFLUENCE_URL override =="
fi
