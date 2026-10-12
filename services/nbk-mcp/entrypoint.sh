#!/bin/sh
set -eu

SEED_DIR="/var/nlm-auth-seed"
WORK_DIR="/var/nlm-auth"

mkdir -p "$WORK_DIR/profiles/default"

if [ ! -f "$WORK_DIR/profiles/default/cookies.json" ]; then
  echo "Seeding NotebookLM auth from secret into writable volume..."
  cp "$SEED_DIR/auth.json" "$WORK_DIR/auth.json"
  cp "$SEED_DIR/profiles/default/cookies.json" "$WORK_DIR/profiles/default/cookies.json"
  cp "$SEED_DIR/profiles/default/metadata.json" "$WORK_DIR/profiles/default/metadata.json"
else
  echo "Reusing existing writable NotebookLM auth copy (already seeded)."
fi

export NOTEBOOKLM_MCP_CLI_PATH="$WORK_DIR"

echo "Starting notebooklm-mcp (http transport, cross-namespace Service)..."
exec notebooklm-mcp --transport http --host 0.0.0.0 --port 8000 --path /mcp
