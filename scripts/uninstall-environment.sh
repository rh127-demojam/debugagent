#!/usr/bin/env bash
# Tears down everything scripts/deploy-fresh-environment.sh installs, so
# the SAME OpenShift cluster can be re-tested with a completely fresh
# install right afterward.
#
# Every resource this project creates lives inside exactly 2 Namespaces -
# debug-agent-hub and debug-agent-mcp. Confirmed by grepping every file in
# manifests/ for `^kind:` - the only kinds that ever appear are Deployment,
# Service, Route, NetworkPolicy, ConfigMap, Secret, BuildConfig,
# ImageStream, Role, RoleBinding - all namespaced. There is no
# ClusterRole/ClusterRoleBinding/PersistentVolume or anything else
# cluster-scoped anywhere in this project. So deleting both Namespaces
# cascades and removes literally everything:
#   - All 6 Deployments + Services + Routes (orchestrator, debug-agent-ui,
#     mcp-confluence-server, mcp-redhat-kcs-server, mcp-slack-server,
#     nbk-mcp-server)
#   - Every Secret, including any REAL credentials you've since pasted or
#     uploaded via the UI's Configure popovers (Confluence/KCS tokens,
#     Slack/NotebookLM session files) - see note below if you want to keep
#     those for next time instead of re-entering them.
#   - debug-agent-litemaas Secret + ConfigMap
#   - All BuildConfigs + their ImageStreams + every Build's history
#   - Both NetworkPolicies (mcp-slack-restrict-ingress, nbk-mcp-restrict-ingress)
#   - The cross-namespace RBAC Role/RoleBinding pair
#     (orchestrator-credentials-rbac.yaml, orchestrator-team-config-rbac.yaml)
#
# NOT touched (by design - these aren't cluster resources at all):
#   - manifests/secrets.local.env (your real LiteMaaS values) - stays put
#     on disk for the next ./scripts/deploy-fresh-environment.sh run.
#   - Any local files in this repo/bundle.
#   - Anything on the cluster outside these 2 namespaces.
#
# Usage:
#   ./scripts/uninstall-environment.sh
set -euo pipefail
cd "$(dirname "$0")/.."

NS_HUB="debug-agent-hub"
NS_MCP="debug-agent-mcp"

log()  { echo "[uninstall] $*"; }
warn() { echo "[uninstall] WARNING: $*" >&2; }

command -v oc >/dev/null || { echo "oc CLI not found on PATH"; exit 1; }
oc whoami >/dev/null 2>&1 || { echo "Not logged into an OpenShift cluster - run 'oc login' first"; exit 1; }
log "Logged in as $(oc whoami) against $(oc whoami --show-server)"

echo ""
echo "About to delete namespaces: $NS_HUB, $NS_MCP"
echo "This removes EVERY Deployment/Service/Route/Secret/ConfigMap/BuildConfig/"
echo "ImageStream/Role/RoleBinding inside them - including any real credentials"
echo "pasted/uploaded via the UI since the last install."
echo ""

for ns in "$NS_HUB" "$NS_MCP"; do
  if oc get namespace "$ns" >/dev/null 2>&1; then
    log "Deleting namespace $ns..."
    oc delete namespace "$ns" --wait=false
  else
    log "Namespace $ns already gone - nothing to do."
  fi
done

log "Waiting for both namespaces to fully terminate (can take a minute or two)..."
for ns in "$NS_HUB" "$NS_MCP"; do
  deadline=$((SECONDS + 180))
  while oc get namespace "$ns" >/dev/null 2>&1; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      warn "$ns is still Terminating after 180s - it may be stuck on a finalizer."
      warn "  Check:    oc get namespace $ns -o yaml | grep -A5 finalizers"
      warn "  Re-running this script, or just running"
      warn "  ./scripts/deploy-fresh-environment.sh directly, is still safe -"
      warn "  'oc apply' simply waits/retries until a Terminating namespace is gone."
      break
    fi
    sleep 3
  done
  oc get namespace "$ns" >/dev/null 2>&1 || log "$ns fully deleted."
done

echo ""
echo "== Uninstall complete =="
echo "  $NS_HUB and $NS_MCP (and everything inside them) are gone."
echo "  manifests/secrets.local.env was left untouched - your LiteMaaS credentials"
echo "  are still there for the next install."
echo ""
echo "  To reinstall: ./scripts/deploy-fresh-environment.sh"
