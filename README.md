# Debug Agent Hub - Fresh OpenShift Environment Deployment

This is the OpenShift deployment package for **Debug Agent Hub** (orchestrator +
Streamlit UI in namespace `debug-agent-hub`; four MCP servers - Confluence,
NotebookLM, Red Hat KCS, Slack - in namespace `debug-agent-mcp`).

This is unrelated to this repo's root `README.md`, which documents a
different thing entirely (the Cursor IDE canvas/agent tooling used to query
Confluence/NotebookLM/Slack from inside Cursor). Nothing in this bundle
touches Cursor.

## Contents

```
manifests/           OpenShift YAML (Deployments, Services, Routes, RBAC,
                      BuildConfigs/ImageStreams, NetworkPolicies, example
                      Secret/ConfigMap templates)
services/             Application source + Dockerfiles for the 4 custom-built
                      images (orchestrator, debug-agent-ui, kcs-mcp, nbk-mcp)
personas-slim.json,
prompt-templates-slim.json,
team-config.json      Seed data for the debug-agent-config ConfigMap.
                      Single source of truth - edit ONLY these. The copies
                      under services/orchestrator/config/ are a build
                      artifact (the image's baked-in default), kept in sync
                      automatically by build-deploy-bundle.ps1 and step 5 of
                      deploy-fresh-environment.sh. Never edit them directly.
scripts/
  deploy-fresh-environment.sh   The automated single-script path (below)
  DEPLOY-README.md              This file
```

## Prerequisites

- `oc` CLI installed, logged in (`oc login ...`) to the **target** cluster,
  with permissions to create Namespaces/RBAC (cluster-admin or equivalent).
- This whole extracted folder present on a machine that has that `oc` access
  (your bastion, laptop, wherever `oc` works against the new cluster).

---

## Option A: Automated (recommended)

Before running, open `manifests/secrets.local.env` (it's already there under
that exact name - no copy/rename step) and fill in the 3 required values:

```bash
LITEMAAS_BASE_URL=...   # no trailing /v1 - the code appends it
LITEMAAS_API_KEY=...
MODEL_NAME=...
```

This is the ONLY credential that must be real before deploying - every
other source (Confluence, Red Hat KCS, Slack, NotebookLM) gets a safe
placeholder automatically and is set for real afterward through the UI (see
"Setting credentials via the UI" below). Skipping this file is not fatal -
the script still completes - but the orchestrator will crash-loop
(`CreateContainerConfigError`) until it's filled in and
`./scripts/apply-secrets-from-config.sh` is (re-)run.

```bash
cd debug-agent-hub-deploy
chmod +x scripts/deploy-fresh-environment.sh
./scripts/deploy-fresh-environment.sh
```

Optional env vars (org-specific, both default to a placeholder you can fix
later with `oc set env`):

```bash
CONFLUENCE_BASE_URL=https://acme.atlassian.net \
SLACK_WORKSPACE_URL=https://acme.slack.com \
  ./scripts/deploy-fresh-environment.sh
```

This does everything below in order, is idempotent, and is safe to re-run.
Read on only if you need to do any of it by hand (debugging a failed step,
air-gapped step-by-step execution, or you just want to see what it does).

---

## Option B: Manual step-by-step

### 1. Namespaces

```bash
oc apply -f manifests/namespaces.yaml
```

### 2. RBAC

```bash
oc apply -f manifests/orchestrator-credentials-rbac.yaml
oc apply -f manifests/orchestrator-team-config-rbac.yaml
```

### 3. `debug-agent-config` ConfigMap

Generated directly from the 3 JSON files at the bundle root (not hand-authored
YAML - see `manifests/debug-agent-config.example.yaml` for the shape only):

```bash
oc create configmap debug-agent-config -n debug-agent-hub \
  --from-file=personas-slim.json \
  --from-file=prompt-templates-slim.json \
  --from-file=team-config.json \
  --dry-run=client -o yaml | oc apply -f -
```

### 4. Secrets

**Required for the orchestrator to classify/synthesize at all:**

```bash
oc create secret generic debug-agent-litemaas -n debug-agent-hub \
  --from-literal=LITEMAAS_API_KEY=<your-key> \
  --from-literal=ATLASSIAN_OAUTH_CLIENT_ID=<your-client-id> \
  --from-literal=ATLASSIAN_OAUTH_CLIENT_SECRET=<your-client-secret> \
  --from-literal=CONFLUENCE_EMAIL=<your-email> \
  --from-literal=CONFLUENCE_API_TOKEN=<your-token>

oc create configmap debug-agent-litemaas -n debug-agent-hub \
  --from-literal=LITEMAAS_BASE_URL=<your-openai-compatible-endpoint> \
  --from-literal=MODEL_NAME=<your-model-name>
```

`LITEMAAS_BASE_URL` can be any OpenAI-API-compatible endpoint (a LiteLLM/
LiteMaaS gateway, OpenAI itself, Azure OpenAI's compatible endpoint, a local
vLLM/Ollama server, etc.) - **do not include a trailing `/v1`**, the
orchestrator appends it itself (`app.py`: `f"{LITEMAAS_BASE_URL}/v1"`). For
OpenAI directly, use `https://api.openai.com`. `MODEL_NAME` must be a model
your chosen endpoint actually serves. These 3 values are required at Python
import time - the orchestrator pod crash-loops without them, so there's no
safe placeholder to auto-seed here (unlike Confluence/KCS).

**Confluence (CFL) and Red Hat KCS - two ways to provide the real token:**

- *Either* create the Secret with the real value now:
  ```bash
  oc create secret generic confluence-mcp-creds -n debug-agent-mcp \
    --from-literal=CONFLUENCE_URL="https://your-domain.atlassian.net/wiki" \
    --from-literal=CONFLUENCE_USERNAME="you@example.com" \
    --from-literal=CONFLUENCE_API_TOKEN="<your-token>"

  oc create secret generic redhat-kcs-mcp-creds -n debug-agent-mcp \
    --from-literal=RHKB_OFFLINE_TOKEN="<your-offline-token>"
  ```
- *Or* skip this and let `deploy-fresh-environment.sh` seed a
  `REPLACE_VIA_UI` placeholder, then set the real token later through the
  Streamlit UI's **Connect** popover (no `oc` needed at that point - see
  "Setting credentials via the UI" below). The manual steps below assume
  you're doing it the `oc create secret` way.

**Slack and NotebookLM - no placeholder path, must be provisioned out-of-band**
(live browser session / interactive Google OAuth, not a pasteable static
token). See `manifests/secrets.example.yaml` for the exact shape, and the
`refresh-slack-creds.ps1` / `refresh-nbk-creds.ps1` scripts in the main repo
(not part of this bundle) for how to produce them. If you don't have these
yet, skip Slack/NBK for now - deploy everything else first, add them later.

### 5. BuildConfigs + ImageStreams, then build the 4 custom images

```bash
oc apply -f manifests/buildconfigs.yaml

oc -n debug-agent-hub start-build orchestrator      --from-dir=services/orchestrator --follow
oc -n debug-agent-hub start-build debug-agent-ui    --from-dir=services/streamlit-ui  --follow
oc -n debug-agent-mcp  start-build mcp-redhat-kcs-server --from-dir=services/kcs-mcp  --follow
oc -n debug-agent-mcp  start-build nbk-mcp          --from-dir=services/nbk-mcp       --follow
```

(Confluence and Slack use off-the-shelf public images - no build needed.)

### 6. Deploy everything

```bash
oc apply -f manifests/orchestrator.yaml      # Deployment + Service + Route
oc apply -f manifests/streamlit-ui.yaml      # Deployment + Service + Route
oc apply -f manifests/confluence-mcp.yaml
oc apply -f manifests/kcs-mcp.yaml
oc apply -f manifests/slack-mcp.yaml         # only if mcp-slack-creds exists
oc apply -f manifests/nbk-mcp.yaml           # only if nbk-mcp-creds exists (+ NetworkPolicy)
```

### 7. Wire up the two Route hostnames

`orchestrator.yaml` ships with `__PUBLIC_BASE_URL__` / `__UI_URL__`
placeholders (the real hostnames don't exist until the Routes above are
created). Resolve and patch them in:

```bash
ORCH_HOST=$(oc get route debug-agent-orchestrator -n debug-agent-hub -o jsonpath='{.spec.host}')
UI_HOST=$(oc get route debug-agent-ui -n debug-agent-hub -o jsonpath='{.spec.host}')

oc set env deploy/debug-agent-orchestrator -n debug-agent-hub \
  PUBLIC_BASE_URL="https://${ORCH_HOST}" \
  UI_URL="https://${UI_HOST}" \
  CONFLUENCE_BASE_URL="https://your-domain.atlassian.net" \
  SLACK_WORKSPACE_URL="https://your-workspace.slack.com"
```

### 8. Wait for readiness

```bash
oc rollout status deploy/debug-agent-orchestrator -n debug-agent-hub --timeout=180s
oc rollout status deploy/debug-agent-ui -n debug-agent-hub --timeout=180s
oc rollout status deploy/mcp-confluence-server -n debug-agent-mcp --timeout=120s
oc rollout status deploy/mcp-redhat-kcs-server -n debug-agent-mcp --timeout=120s
```

---

## Setting Confluence/KCS credentials via the UI (instead of `oc`)

Once `mcp-confluence-server` / `mcp-redhat-kcs-server` exist (real token or
placeholder), open the Streamlit UI's **Connect** popover and paste the real
token there. Under the hood this calls `POST /credentials/{CFL|KCS}` on the
orchestrator, which `PATCH`es the Secret and restarts that one Deployment for
you - no cluster access required for whoever is setting it up. This only
works as an *update* (the Secret/Deployment must already exist first), which
is exactly what `deploy-fresh-environment.sh`'s placeholder-seeding step is
for.

## Verification

```bash
oc get pods -n debug-agent-hub -n debug-agent-mcp
curl -sk "https://$(oc get route debug-agent-orchestrator -n debug-agent-hub -o jsonpath='{.spec.host}')/health" | jq
```

`/health` reports per-source status (`confluence`, `kcs`, `slack`,
`notebooklm`) - it's normal for a freshly-deployed source with a placeholder
token to show as degraded/error until the real credential is set.

## Known gotchas (learned the hard way on the source environment)

- **NotebookLM credential updates require a pod replacement, not just a
  rollout restart annotation bump**, if the pod has ever started once before -
  `services/nbk-mcp/entrypoint.sh` only seeds its writable auth volume
  once per pod lifetime. If a Secret update to `nbk-mcp-creds` doesn't seem
  to take effect, force-delete the pod: `oc -n debug-agent-mcp delete pod -l app=nbk-mcp-server`.
- **NotebookLM sessions are short-lived.** `nlm login --check` / the
  `auth_status` field is a shallow probe and can report healthy on a session
  that's actually already dead - the only reliable check is a real
  `notebook_list` call (or `/health`'s `notebooklm_auth_status`).
- **The orchestrator Route needs a long timeout** (`haproxy.router.openshift.io/timeout: 300s`,
  already set in `orchestrator.yaml`) - RCA synthesis can chain up to 3 LLM
  calls.
- **Secret-sourced env vars do not hot-reload.** Only `team-config.json`
  (mounted as a ConfigMap *volume*, not an env var) picks up changes without
  a pod restart - that's why both the credentials-paste endpoint and NBK
  credential refreshes always force a pod restart/replacement afterward.
