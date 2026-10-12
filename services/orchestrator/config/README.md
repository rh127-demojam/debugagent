`personas-slim.json`, `prompt-templates-slim.json`, and `team-config.json`
are intentionally **not** committed here - they're generated into this
folder at build time (never hand-authored here, never committed here):

- `scripts/build-deploy-bundle.ps1` copies them in when staging the
  deployment bundle.
- `scripts/deploy-fresh-environment.sh` (step 5) copies them in immediately
  before `oc start-build orchestrator`, so this also works if you run the
  script directly from a repo checkout.

**Edit only the repo-root copies** (`../../../personas-slim.json`, etc.) -
this folder is a build artifact, not a second place to edit.

`debugagent-hub.mdc` in this same folder is the one real, permanently
checked-in file - it's the orchestrator's own embedded rule file (not
regenerated from anywhere else in this repo).
