# Changelog

## 0.1.0 — 2026-09-21

First release.

- `/sessions:push` writes a handoff summary, renders and redacts the transcript, commits under `.claude/sessions/` through a temporary index, and pushes.
- `/sessions:pull` fetches, indexes sessions on the default branch and `HEAD`, and loads summaries into the chat.
- `/sessions:init` fixes `.gitignore`, drops a README and `.gitattributes`, and registers the plugin in `.claude/settings.json`.
- Renderer follows the active conversation path across rewinds and compaction, stubs MCP and file-read results, truncates other tool output, and collects files touched by subagents.
- Redaction covers cloud and VCS tokens, JWTs, private keys, URL credentials, Azure storage and Entra secrets, connection-string passwords and SAS signatures, with keyword hits flagged for review.
