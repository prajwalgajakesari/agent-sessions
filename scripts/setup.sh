#!/usr/bin/env bash
# One-time bootstrap for a teammate on macOS, Linux, WSL or Git Bash.
#
#   ./setup.sh                 install the Claude Code plugin AND the portable skill (default)
#   ./setup.sh --claude        Claude Code plugin only (marketplace add + plugin install)
#   ./setup.sh --skills        portable skill only, into ~/.agents/skills/agent-sessions
#                              (read by Codex, OpenCode, Gemini CLI, Cursor and Copilot)
#   ./setup.sh --repo <path>   also run `init` in that repository
#
# Everything here is also doable from inside an agent:
#   Claude Code:  /plugin marketplace add prajwalgajakesari/agent-sessions
#                 /plugin install sessions@agent-sessions
#   Others:       copy agents-skills/agent-sessions/ to ~/.agents/skills/agent-sessions/
set -uo pipefail

MARKET="prajwalgajakesari/agent-sessions"
PLUGIN="sessions@agent-sessions"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/.." && pwd)"
skill_src="$repo_root/agents-skills/agent-sessions"
skill_dst="${AGENT_SKILLS_DIR:-$HOME/.agents/skills}/agent-sessions"

do_claude=0; do_skills=0; repo_dir=""
if [ $# -eq 0 ]; then do_claude=1; do_skills=1; fi
while [ $# -gt 0 ]; do
  case "$1" in
    --claude) do_claude=1 ;;
    --skills) do_skills=1 ;;
    --repo) shift; repo_dir="${1:-}" ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1"; exit 2 ;;
  esac
  shift
done
[ -n "$repo_dir" ] && [ $do_claude -eq 0 ] && [ $do_skills -eq 0 ] && { do_claude=1; do_skills=1; }

if [ $do_claude -eq 1 ]; then
  if command -v claude >/dev/null 2>&1; then
    echo "==> Claude Code: registering marketplace $MARKET"
    if claude plugin marketplace add "$MARKET" >/dev/null 2>&1; then echo "    ok"; else echo "    already present or unsupported CLI; inside Claude Code run: /plugin marketplace add $MARKET"; fi
    echo "==> Claude Code: installing $PLUGIN"
    if claude plugin install "$PLUGIN" >/dev/null 2>&1; then echo "    ok"; else echo "    already installed or unsupported CLI; inside Claude Code run: /plugin install $PLUGIN"; fi
  else
    echo "==> Claude Code CLI not found; skipping the plugin (install Claude Code, then re-run with --claude)"
  fi
fi

if [ $do_skills -eq 1 ]; then
  if [ ! -f "$skill_src/SKILL.md" ]; then
    echo "==> portable skill not found at $skill_src (run from a checkout of the agent-sessions repo)"; exit 1
  fi
  echo "==> Installing the portable skill into $skill_dst"
  mkdir -p "$(dirname "$skill_dst")"
  rm -rf "$skill_dst"
  cp -R "$skill_src" "$skill_dst"
  find "$skill_dst" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null
  chmod +x "$skill_dst/scripts/sessions.sh" 2>/dev/null
  echo '    ok: Codex (invoke with $agent-sessions), OpenCode, Gemini CLI, Cursor and Copilot read ~/.agents/skills'
fi

if [ -n "$repo_dir" ]; then
  echo "==> Preparing repo $repo_dir"
  "$here/sessions.sh" init --project-dir "$repo_dir"
  echo "    Review the changes above, commit them, and push."
fi

echo
"$here/sessions.sh" doctor 2>/dev/null | sed -n '/^Agents:/,/^STATUS/p' | grep -v "^STATUS"
echo
echo "Done. In Claude Code use /sessions:push and /sessions:pull; in Codex type \$agent-sessions push; elsewhere ask your agent to use the agent-sessions skill."
