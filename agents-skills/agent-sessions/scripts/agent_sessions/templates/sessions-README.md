# Shared coding-agent sessions

This folder holds sessions that teammates chose to share, whichever coding agent they used. Each session is one directory:

```
YYYY-MM-DD_<slug>_<handle>_<short-id>/
├── summary.md      handoff written by the author's agent at push time: goal, outcome, decisions, files, gotchas, next steps, paste-ready handoff prompt
├── transcript.md   the conversation rendered to markdown; tool calls collapsed, secrets redacted, query results omitted
└── meta.json       machine-readable index entry (title, author, agent, branch, files touched, tags, outcome)
```

Start with `summary.md`. Open `transcript.md` only when you need to see exactly what was tried.

## How to push and pull, by agent

| Agent | Share this session | Read teammates' sessions |
|---|---|---|
| Claude Code | `/sessions:push [title] [--yes]` | `/sessions:pull [keyword]` |
| Codex CLI | `$agent-sessions push [title] --yes` | `$agent-sessions pull [keyword]` |
| OpenCode, Gemini CLI, Cursor, Copilot | ask the agent to *use the agent-sessions skill to push this session* | ask it to *use the agent-sessions skill to pull [keyword]* |
| Any terminal | `agent-sessions push` (after `pip install agent-sessions-cli`) | `agent-sessions list` |

Useful pull flags: `--full <id>` reads one full transcript, `--all-branches` includes sessions on unmerged branches, `--mine` filters to your own.

Install once: Claude Code users are prompted to install the `sessions` plugin when they open this repo. Everyone else runs `scripts/setup.sh --skills` (or `setup.ps1 -Skills`) from https://github.com/prajwalgajakesari/agent-sessions, which copies the skill into `~/.agents/skills/`. If `.agents/skills/agent-sessions/` exists in this repo, the skill is already vendored and nothing needs installing.

## What gets redacted

Structured secrets (cloud keys, tokens, JWTs, private keys, connection-string passwords, SAS signatures) are replaced with `[REDACTED:<kind>]` before anything is committed. Results from file reads, web fetches and MCP tools are omitted and other tool output is truncated, so query rows and file contents do not end up here. Keyword-based redactions (`password=`, `api_key=`) are flagged for the author to review at push time. Nothing here is guaranteed clean: treat a transcript as you would treat a chat log.

There is no generated index on purpose. Directories are per session and per author, so concurrent pushes never conflict. `agent-sessions list` builds the index from `meta.json` files on demand.
