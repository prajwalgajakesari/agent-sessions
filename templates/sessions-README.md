# Shared Claude Code sessions

This folder holds Claude Code sessions that teammates chose to share. Each session is one directory:

```
YYYY-MM-DD_<slug>_<handle>_<session-id-prefix>/
├── summary.md      handoff written by the author's Claude at push time: goal, outcome, decisions, files, gotchas, next steps, paste-ready handoff prompt
├── transcript.md   the conversation rendered to markdown; tool calls collapsed, secrets redacted, query results omitted
└── meta.json       machine-readable index entry (title, author, branch, files touched, tags, outcome)
```

Start with `summary.md`. Open `transcript.md` only when you need to see exactly what was tried.

## Using the plugin

| Want to | Run inside Claude Code |
|---|---|
| Share the session you are in | `/sessions:push [title] [--yes]` |
| See what teammates did here | `/sessions:pull` |
| Load a specific topic | `/sessions:pull reconciliation` |
| Read one full transcript | `/sessions:pull --full <id-prefix>` |
| Include sessions on unmerged branches | `/sessions:pull --all-branches` |

Plugin: [claude-sessions](https://github.com/prajwalgajakesari/claude-sessions). Opening this repo in Claude Code prompts you to install it.

## What gets redacted

Structured secrets (cloud keys, tokens, JWTs, private keys, connection-string passwords, SAS signatures) are replaced with `[REDACTED:<kind>]` before anything is committed. Results from MCP tools and file reads are omitted and other tool output is truncated, so query rows and file contents do not end up here. Keyword-based redactions (`password=`, `api_key=`) are flagged for the author to review at push time. Nothing here is guaranteed clean: treat a transcript as you would treat a chat log.

There is no generated index on purpose. Directories are per session and per author, so concurrent pushes never conflict.
