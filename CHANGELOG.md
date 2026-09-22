# Changelog

## 0.2.0 — 2026-09-22

Works across coding agents. The repo is now `agent-sessions`; the Claude plugin keeps its name `sessions`.

- **Adapters**: Codex CLI (rollout JSONL, session index titles, `exec` command extraction, `apply_patch` files, compaction history) and OpenCode (SQLite, read-only, locked-db fallback, compaction parts, child sessions) join Claude Code. Gemini CLI ships as an experimental adapter built from its source schema. Copilot CLI and Cursor are detected and reported as not yet supported.
- **Agent-neutral model**: adapters emit events with canonical tool kinds (shell, read, edit, write, search, web, agent, mcp, ask); the renderer, result policy, redaction and files-touched logic no longer know agent tool names.
- **Live-session identification** without env vars: the locate call recognises itself as the shell call still running in exactly one session; otherwise it lists candidates and asks instead of guessing. Codex fork stubs are skipped.
- **Re-push fix**: the transcript now stops at the last push invocation, so pushing the same session twice keeps everything after the first push.
- **Portable skill** `agents-skills/agent-sessions/` (Agent Skills standard) with a bundled copy of the package, installed by `scripts/setup.sh --skills` into `~/.agents/skills/`, which Codex, OpenCode, Gemini CLI, Cursor and Copilot all read. `init --vendor-skill` copies it into a repo for teammates.
- **Python package** `agent-sessions-cli` (`pip install agent-sessions-cli`, console script `agent-sessions`), CI on Linux, macOS and Windows, release workflow with PyPI trusted publishing.
- `doctor` subcommand; `STATUS:` printed first and last; `SCRIPT:` line; `meta.json` gains `agent`, `agent_version`, `originator`, `parent_id`, `short_id`, `tokens`; commit trailer `Agent-Session: <agent>:<id>`; temp index inside the git dir for sandboxed agents.

## 0.1.0 — 2026-09-21

First release, Claude Code only.

- `/sessions:push` writes a handoff summary, renders and redacts the transcript, commits under `.claude/sessions/` through a temporary index, and pushes.
- `/sessions:pull` fetches, indexes sessions on the default branch and `HEAD`, and loads summaries into the chat.
- `/sessions:init` fixes `.gitignore`, drops a README and `.gitattributes`, and registers the plugin in `.claude/settings.json`.
- Renderer follows the active conversation path across rewinds and compaction, stubs MCP and file-read results, truncates other tool output, and collects files touched by subagents.
- Redaction covers cloud and VCS tokens, JWTs, private keys, URL credentials, Azure storage and Entra secrets, connection-string passwords and SAS signatures, with keyword hits flagged for review.
