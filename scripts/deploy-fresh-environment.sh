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
#   - Only ONE credential needs to be real before you run this:
#     debug-agent-litemaas. Fill in LITEMAAS_BASE_URL/API_KEY/MODEL_NAME
#     directly in manifests/secrets.local.env (it already ships under that
#     exact name - no copy/rename step), and this script applies it
#     automatically (via
#     scripts/apply-secrets-from-config.sh) BEFORE the orchestrator is ever
#     deployed - so it comes up healthy on the very first try, no
#     crash-loop-then-recover cycle. If that file doesn't exist, the script
#     still completes, but the orchestrator will crash-loop
#     (CreateContainerConfigError) until you create it and re-run.
#   - Every other credential is deliberately NOT read from a file at all -
#     set them after the app is up, through the Streamlit UI's Configure
#     popover (no cluster login or commands needed for any of these):
#       - Confluence: URL is hardcoded below (this deploy always points at
#         Red Hat's internal Confluence) - paste email + API token in the UI.
#       - Red Hat KCS: paste the SSO offline token in the UI.
#       - Slack / NotebookLM: upload the Secret YAML file in the UI
#         (generate it first with scripts/refresh-slack-creds.ps1 -Mode Export
#         / scripts/refresh-nbk-creds.ps1 -SkipLogin -Mode Export on your own
#         machine - that one local step is unavoidable, since both are
#         browser-session/OAuth credentials, not a value you can type).
#   - NotebookLM's image gets built and its Deployment applied on EVERY run,
#     regardless of whether real creds exist yet (unlike the old behavior,
#     which skipped it entirely). Its Secret is mounted as a volume with no
#     `optional: true`, so with no real nbk-mcp-creds yet the pod just sits
#     in ContainerCreating ("secret not found") - not a crash, not an error,
#     and this script does NOT wait on its rollout, so it can never make
#     this script fail or hang. The moment a real Secret is uploaded via the
#     UI, Kubernetes mounts it and the pod starts on its own.
#   - CFL/KCS/SLK all read their creds as plain env vars at request time (not
#     at process startup), so a bogus placeholder value still lets the pod
#     come up Ready (TCP-only readinessProbe) - this script seeds one for
#     any of the three that's missing, so all 3 Deployments ALWAYS come up
#     on a fresh cluster, real creds or not.
#   - Re-running this script is SAFE but not a pure no-op: Namespaces/RBAC/
#     BuildConfigs/Secrets/ConfigMap all use idempotent `oc apply` / existence
#     checks (nothing gets duplicated or silently overwritten - e.g. a real
#     token you've since pasted via the UI into confluence-mcp-creds /
#     redhat-kcs-mcp-creds / mcp-slack-creds / nbk-mcp-creds is never
#     touched). The 5 custom-built images (orchestrator, debug-agent-ui,
#     kcs-mcp, nbk-mcp) DO get rebuilt from current source on every run (a
#     real build, not a no-op) and their Deployments get an explicit
#     `oc rollout restart` right after, so a re-run always actually
#     redeploys whatever it just built - without that restart, the static
#     ":latest" image tag never changes text-wise, so `oc apply` alone would
#     see no diff and the already-running pod would silently keep running
#     the OLD image.
#
# Usage:
#   ./scripts/deploy-fresh-environment.sh
#   CONFLUENCE_BASE_URL=https://acme.atlassian.net SLACK_WORKSPACE_URL=https://acme.slack.com \
#     ./scripts/deploy-fresh-environment.sh
set -euo pipefail

# Resolve this script's own directory as an ABSOLUTE path, captured ONCE,
# before any `cd` below. Using a bare `$(dirname "$0")` a second time AFTER
# `cd`ing elsewhere silently re-evaluates that same relative string against
# the NEW working directory instead of the original one - e.g. invoking
# this as `sh debug-agent-hub-deploy/scripts/deploy-fresh-environment.sh`
# from one level above the bundle: the first `cd` moves into
# debug-agent-hub-deploy/, then a second raw `dirname "$0"` would resolve to
# the nonexistent debug-agent-hub-deploy/debug-agent-hub-deploy/scripts.
# That exact bug silently broke the chmod self-heal below (swallowed by
# `2>/dev/null || true`), causing "Permission denied" on
# apply-secrets-from-config.sh even though this self-heal line looked like
# it should have prevented it. Capturing SCRIPT_DIR up front avoids this
# class of bug entirely, regardless of how/from-where this script is
# invoked.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

# Self-healing: tarballs built on Windows (see scripts/build-deploy-bundle.ps1)
# always lose the Unix execute bit on *.sh files - NTFS has no such concept
# to begin with, so there's nothing for `tar` to preserve. Fix it here,
# unconditionally, every run, rather than relying on whoever extracted the
# tarball to remember `chmod +x`. This script itself is invoked via `sh`, so
# it doesn't need +x on itself to reach this line.
chmod +x "$SCRIPT_DIR"/*.sh 2>/dev/null || true

NS_HUB="debug-agent-hub"
NS_MCP="debug-agent-mcp"
CONFLUENCE_BASE_URL="${CONFLUENCE_BASE_URL:-__CONFLUENCE_BASE_URL__}"
# Red Hat's internal Slack workspace - this deploy always points here, same
# "not configurable per-deployment, always correct out of the box" pattern
# as CFL_HARDCODED_URL below. This only feeds the orchestrator's permalink
# builder (_slack_permalink() in app.py) so ranked Slack matches get a
# clickable "Open" link - it has nothing to do with the mcp-slack-creds
# Secret (that's XOXC/XOXD session cookies, set separately via the UI).
# Still overridable via the env var for any other team's workspace.
SLACK_WORKSPACE_URL="${SLACK_WORKSPACE_URL:-https://redhat.enterprise.slack.com}"

# Red Hat's internal Confluence instance - this deploy always points here,
# not configurable per-deployment. Only email + API token (pasted via the
# UI's Configure popover afterward) vary per user. /wiki suffix is required
# by the mcp-atlassian image this Secret feeds.
CFL_HARDCODED_URL="https://stage-redhat.atlassian.net/wiki"

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

# --- 4. seed placeholder Secrets for CFL/KCS/SLK, then check what we have --
#
# Whether a source gets a placeholder (always deployed, fixed up later)
# or needs special handling depends on ONE question: can the container
# tolerate a placeholder value at startup without crash-looping?
#
#   - CFL/KCS: a plain env-var static token, read lazily per-request.
#     readinessProbe is a plain TCP check, not an auth check, so a bogus
#     token still comes up Ready - just fails real API calls until fixed.
#     Fixed up via the UI's Configure popover (POST /credentials/{source} in
#     app.py does an HTTP PATCH on the Secret + restarts the Deployment -
#     both already have to exist for a PATCH to succeed, which is exactly
#     why the placeholder has to be seeded BEFORE that first UI paste).
#     CFL's URL is always the hardcoded Red Hat Confluence instance above -
#     the UI no longer has a URL field at all, only email + token.
#   - SLK: same shape as CFL/KCS (plain env vars, bare-TCP readinessProbe),
#     but fixed up via the UI's file-upload Configure popover instead of a
#     paste-a-token form (a live browser session isn't something you can
#     safely type into a text box) - upload the Secret YAML from
#     scripts/refresh-slack-creds.ps1 -Mode Export.
#   - NBK is handled in step 5/6 below, not here - it has no plain-env-var
#     placeholder story at all (see the file-mount note there).
SEEDED_PLACEHOLDER=()
seed_placeholder_secret() {
  local ns="$1" name="$2" hint="$3"; shift 3
  if ! secret_exists "$ns" "$name"; then
    log "Seeding placeholder Secret $name in $ns ($hint)..."
    oc create secret generic "$name" -n "$ns" "$@"
    SEEDED_PLACEHOLDER+=("$name|$hint")
  fi
}
seed_placeholder_secret "$NS_MCP" confluence-mcp-creds \
  "replace email + API token via the UI's Configure popover" \
  --from-literal=CONFLUENCE_URL="$CFL_HARDCODED_URL" \
  --from-literal=CONFLUENCE_USERNAME="REPLACE_ME@example.com" \
  --from-literal=CONFLUENCE_API_TOKEN="REPLACE_VIA_UI"
seed_placeholder_secret "$NS_MCP" redhat-kcs-mcp-creds \
  "replace the real value via the UI's Configure popover" \
  --from-literal=RHKB_OFFLINE_TOKEN="REPLACE_VIA_UI"
seed_placeholder_secret "$NS_MCP" mcp-slack-creds \
  "replace via the UI's Configure popover - upload the Secret YAML from scripts/refresh-slack-creds.ps1 -Mode Export" \
  --from-literal=SLACK_XOXC_TOKEN="REPLACE_VIA_UI" \
  --from-literal=SLACK_XOXD_TOKEN="REPLACE_VIA_UI"

# --- 4b. apply the one credential with no safe placeholder, if provided ----
#
# debug-agent-litemaas is read at Python import time - the orchestrator
# CrashLoopBackOffs on startup if it's missing, unlike everything else
# above which just comes up Ready with a bad value. Applying it HERE, before
# step 6 ever deploys the orchestrator for the first time, means a fresh
# cluster comes up healthy on the very first rollout instead of
# crash-looping once and recovering after a manual fix-up later.
if [ -f manifests/secrets.local.env ]; then
  log "Found manifests/secrets.local.env - applying debug-agent-litemaas..."
  if ! ./scripts/apply-secrets-from-config.sh manifests/secrets.local.env; then
    warn "apply-secrets-from-config.sh reported an issue - see output above."
  fi
else
  warn "manifests/secrets.local.env not found - the orchestrator WILL crash-loop until you"
  warn "  fill in LITEMAAS_BASE_URL/API_KEY/MODEL_NAME in that file and run"
  warn "  ./scripts/apply-secrets-from-config.sh"
fi

declare -A HAVE
HAVE[litemaas_secret]=0;  secret_exists "$NS_HUB" debug-agent-litemaas  && HAVE[litemaas_secret]=1
HAVE[litemaas_cm]=0;      cm_exists     "$NS_HUB" debug-agent-litemaas  && HAVE[litemaas_cm]=1
HAVE[nbk]=0;              secret_exists "$NS_MCP" nbk-mcp-creds         && HAVE[nbk]=1

if [ "${HAVE[litemaas_secret]}" = 0 ] || [ "${HAVE[litemaas_cm]}" = 0 ]; then
  warn "debug-agent-litemaas Secret/ConfigMap still missing in $NS_HUB - orchestrator WILL crash-loop."
fi
[ "${HAVE[nbk]}" = 1 ] || log "nbk-mcp-creds not set yet - nbk-mcp-server will deploy but sit idle (ContainerCreating) until it's uploaded via the UI."

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

log "Building mcp-redhat-kcs-server image..."
oc -n "$NS_MCP" start-build mcp-redhat-kcs-server --from-dir=services/kcs-mcp --follow

# NBK's image always gets built now, regardless of whether real creds exist
# yet - building doesn't need any credentials at all, it's just compiling
# the Dockerfile. This guarantees the ImageStream has a pullable image by
# the time step 6 applies its Deployment, so the pod can at least schedule
# (it'll still sit in ContainerCreating until a real Secret exists - see the
# header comment above - but that's a volume-mount wait, not an image pull
# failure).
log "Building nbk-mcp image..."
oc -n "$NS_MCP" start-build nbk-mcp --from-dir=services/nbk-mcp --follow

# --- 6. Deployments ----------------------------------------------------------
#
# The 5 custom-built images use a static ":latest" tag in the Deployment
# spec, which never changes text-wise between runs - so `oc apply` alone
# sees no diff and does nothing on a re-run, even right after step 5 just
# built a brand-new image. An already-running pod does NOT re-pull just
# because a new image landed on the tag; it has to be told to restart. So
# every custom-built Deployment gets an explicit rollout restart right
# after its apply, guaranteeing a re-run of this script always actually
# deploys whatever it just built (harmless no-op extra churn on a first run,
# where the pod was starting fresh anyway).
log "Deploying orchestrator (Deployment+Service+Route)..."
oc apply -f manifests/orchestrator.yaml
oc rollout restart deploy/debug-agent-orchestrator -n "$NS_HUB"

log "Deploying debug-agent-ui (Deployment+Service+Route)..."
oc apply -f manifests/streamlit-ui.yaml
oc rollout restart deploy/debug-agent-ui -n "$NS_HUB"

log "Deploying mcp-confluence-server..."
oc apply -f manifests/confluence-mcp.yaml

log "Deploying mcp-redhat-kcs-server..."
oc apply -f manifests/kcs-mcp.yaml
oc rollout restart deploy/mcp-redhat-kcs-server -n "$NS_MCP"

log "Deploying mcp-slack-server (+ NetworkPolicy)..."
oc apply -f manifests/slack-mcp.yaml

# NBK's Deployment is always applied now too (see step 5 note above). No
# rollout restart needed on a first-ever apply (nothing was running before),
# and we deliberately do NOT wait on its readiness in step 7 - with no real
# Secret yet it would just sit ContainerCreating until the --timeout
# expires, which would fail this whole script for a condition that isn't
# actually an error.
log "Deploying nbk-mcp-server (+ NetworkPolicy)..."
oc apply -f manifests/nbk-mcp.yaml
if [ "${HAVE[nbk]}" = 1 ]; then
  oc rollout restart deploy/nbk-mcp-server -n "$NS_MCP"
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
# nbk-mcp-server is deliberately NOT waited on here - see step 6 note.
log "Waiting for rollouts..."
oc rollout status deploy/debug-agent-orchestrator -n "$NS_HUB" --timeout=180s
oc rollout status deploy/debug-agent-ui -n "$NS_HUB" --timeout=180s
oc rollout status deploy/mcp-confluence-server -n "$NS_MCP" --timeout=120s
oc rollout status deploy/mcp-redhat-kcs-server -n "$NS_MCP" --timeout=120s
oc rollout status deploy/mcp-slack-server -n "$NS_MCP" --timeout=120s
[ "${HAVE[nbk]}" = 1 ] && oc rollout status deploy/nbk-mcp-server -n "$NS_MCP" --timeout=120s

echo ""
echo "== Deploy complete =="
echo "  Orchestrator: https://${ORCH_HOST}"
echo "  UI:           https://${UI_HOST}"
if [ "${HAVE[litemaas_secret]}" = 0 ] || [ "${HAVE[litemaas_cm]}" = 0 ]; then
  echo "  ACTION NEEDED: debug-agent-litemaas is missing - the orchestrator is CrashLoopBackOff."
  echo "    Fix: fill in LITEMAAS_BASE_URL/API_KEY/MODEL_NAME in manifests/secrets.local.env,"
  echo "    then run ./scripts/apply-secrets-from-config.sh"
fi
if [ ${#SEEDED_PLACEHOLDER[@]} -gt 0 ]; then
  echo "  ACTION NEEDED (set via the UI's Configure popover, not a command - app is already up):"
  for entry in "${SEEDED_PLACEHOLDER[@]}"; do
    name="${entry%%|*}"
    hint="${entry#*|}"
    echo "    - $name ($hint)"
  done
fi
if [ -z "$SLACK_WORKSPACE_URL" ] || [ "$SLACK_WORKSPACE_URL" = "__SLACK_WORKSPACE_URL__" ]; then
  echo "  ACTION NEEDED: SLACK_WORKSPACE_URL ended up empty/unset - Slack results will work fine"
  echo "    but their links will not be clickable (app.py degrades this gracefully, no crash)."
  echo "    Fix: oc set env deploy/debug-agent-orchestrator -n $NS_HUB \\"
  echo "           SLACK_WORKSPACE_URL=\"https://your-workspace.slack.com\" && \\"
  echo "         oc rollout restart deploy/debug-agent-orchestrator -n $NS_HUB"
fi
if [ "${HAVE[nbk]}" = 0 ]; then
  echo "  nbk-mcp-server is deployed but idle (ContainerCreating) - upload nbk-mcp-creds via the UI's"
  echo "    Configure popover (generate it first with scripts/refresh-nbk-creds.ps1 -SkipLogin -Mode Export)."
fi
