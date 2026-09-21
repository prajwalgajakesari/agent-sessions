#!/usr/bin/env bash
# One-time bootstrap for a teammate on macOS, Linux, WSL or Git Bash:
#   1. register the claude-sessions marketplace
#   2. install the sessions plugin
#   3. optionally prepare a repo:  ./setup.sh /path/to/repo
#
# Everything here is also doable from inside Claude Code with
#   /plugin marketplace add prajwalgajakesari/claude-sessions
#   /plugin install sessions@claude-sessions
#   /sessions:init
set -uo pipefail

MARKET="prajwalgajakesari/claude-sessions"
PLUGIN="sessions@claude-sessions"
REPO_DIR="${1:-}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v claude >/dev/null 2>&1; then
  echo "The 'claude' CLI is not on PATH. Install Claude Code first: https://code.claude.com/docs/en/quickstart"
  exit 1
fi

echo "==> Registering marketplace $MARKET"
if claude plugin marketplace add "$MARKET" >/dev/null 2>&1; then
  echo "    ok"
else
  echo "    could not add it from the CLI (already present, or this Claude Code version lacks the subcommand)."
  echo "    Inside Claude Code run:  /plugin marketplace add $MARKET"
fi

echo "==> Installing plugin $PLUGIN"
if claude plugin install "$PLUGIN" >/dev/null 2>&1; then
  echo "    ok"
else
  echo "    could not install it from the CLI (already installed, or this Claude Code version lacks the subcommand)."
  echo "    Inside Claude Code run:  /plugin install $PLUGIN"
fi

if [ -n "$REPO_DIR" ]; then
  echo "==> Preparing repo $REPO_DIR"
  "$here/sessions.sh" init --project-dir "$REPO_DIR"
  echo "    Review the changes above, commit them, and push."
fi

echo
echo "Done. Open Claude Code in a repo and run /sessions:push to share a session or /sessions:pull to read your teammates'."
