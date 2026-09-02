#!/bin/bash
# SessionStart hook — Claude Code on the web.
#
# Both jobs below were done by hand during the onenote-mcp build. Doing them once
# per container instead is the difference between a cloud session that can deploy
# and one that hands the work back to a laptop.
#
#   1. AZURE CLI. Without it a cloud session cannot deploy onenote-mcp at all;
#      that single gap is why the deploy kept bouncing back to SKYNET.
#
#      Note the install route. Microsoft's official installer lives behind
#      aka.ms, which this environment's egress proxy BLOCKS -- curl returns 000,
#      not a redirect. PyPI is reachable, so azure-cli comes from there instead.
#      Do not "fix" this back to the aka.ms script; it cannot work here.
#
#      Installing the CLI grants NO Azure access on its own. A session still
#      needs credentials, which live in the environment config and never in this
#      repo -- see BUILD-NOTES for the scoped service principal. This hook
#      deliberately handles no secrets and performs no login.
#
#   2. onenote-mcp's RUNTIME DEPS, in a venv. The ast test suite needs nothing at
#      all by design, so this is not about running tests. It is about being able
#      to import and run the REAL module: that is what caught the defect the ast
#      suite is blind to by construction (@mcp.tool rebinds a tool's name to a
#      non-callable FunctionTool, so onenote_search could never call
#      onenote_list_sections). Fixtures cannot find that class of bug.
#
# Idempotent: safe to re-run, skips whatever is already present.

set -euo pipefail

# Local machines have their own toolchains; don't reach into them.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
VENV="$PROJECT_DIR/.venv"

# --- 1. Azure CLI -----------------------------------------------------------
if command -v az >/dev/null 2>&1 || [ -x "$HOME/.local/bin/az" ]; then
  echo "session-start: azure-cli already present, skipping"
else
  echo "session-start: installing azure-cli from PyPI (aka.ms is egress-blocked)"
  # --break-system-packages is required on PEP 668 images and harmless elsewhere;
  # fall back without it so a pip that rejects the flag can still succeed.
  python3 -m pip install --user --quiet --break-system-packages azure-cli \
    || python3 -m pip install --user --quiet azure-cli
fi

# --- 2. onenote-mcp runtime deps -------------------------------------------
if [ -f "$PROJECT_DIR/onenote-mcp/requirements.txt" ]; then
  if [ ! -x "$VENV/bin/python" ]; then
    echo "session-start: creating venv at .venv"
    python3 -m venv "$VENV"
  fi
  echo "session-start: installing onenote-mcp requirements"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r "$PROJECT_DIR/onenote-mcp/requirements.txt"
fi

# --- 3. Make both available for the rest of the session ---------------------
# SessionStart fires on startup AND on resume/clear/compact, so this runs several
# times in one session. Append only what is not already there, or the same exports
# stack up on every compaction.
_add_env() {
  [ -n "${CLAUDE_ENV_FILE:-}" ] || return 0
  grep -qxF "$1" "$CLAUDE_ENV_FILE" 2>/dev/null || echo "$1" >> "$CLAUDE_ENV_FILE"
}
_add_env "export PATH=\"$HOME/.local/bin:\$PATH\""
if [ -x "$VENV/bin/python" ]; then
  _add_env "export ONENOTE_VENV=\"$VENV\""
fi

# --- 4. Report deploy-readiness (names only, never values) -------------------
# A session that can run `az` but holds no credentials looks identical to one
# that can deploy, right up until the first command fails. Say which it is at
# startup. This only tests whether the variables are SET -- it never reads,
# prints, or logs a value, and it does not log in; that stays an explicit step.
if [ -n "${AZURE_CLIENT_ID:-}" ] && [ -n "${AZURE_CLIENT_SECRET:-}" ] && [ -n "${AZURE_TENANT_ID:-}" ]; then
  echo "session-start: Azure service-principal credentials present — this session can deploy"
  echo "session-start: log in with: az login --service-principal -u \"\$AZURE_CLIENT_ID\" -p \"\$AZURE_CLIENT_SECRET\" --tenant \"\$AZURE_TENANT_ID\""
else
  echo "session-start: no Azure credentials in this environment — az is installed but cannot reach a subscription"
  echo "session-start: see onenote-mcp/BUILD-NOTES-2026-08-22.md section 7a to set them"
fi

echo "session-start: done"
