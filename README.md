# agent-sessions

Share coding-agent sessions with your team through the git repo you already work in. One folder, `.claude/sessions/`, holds handoffs from **Claude Code, Codex CLI, OpenCode** and (experimentally) **Gemini CLI**; anyone on any agent can pull them back into their own chat.

```
/sessions:push "Reconcile fabric tables"        Claude Code, end of a session
$agent-sessions pull reconcile                  a teammate in Codex, next morning
```

Coding agents keep sessions in private, undocumented, short-lived local stores. This tool turns one into three committed files and gives teammates one command to load them.

## What a pushed session looks like

```
.claude/sessions/2026-09-22_reconcile-fabric-tables_prajwal-p_7dcecef6/
├── summary.md      handoff the author's agent wrote at push time: goal, outcome, decisions and why,
│                   files changed, commands and gotchas, open questions, a paste-ready handoff prompt (≤120 lines)
├── transcript.md   the conversation as markdown along the active path (rewinds and compaction handled),
│                   tool calls collapsed, secrets redacted, file reads and query results omitted
└── meta.json       title, author, agent and version, branch, dates, files touched (subagents included), tags, outcome, tokens
```

Pull reads `summary.md` first and loads a transcript only when asked, so a teammate gets the reasoning without burning their context window.

## Install

| You use | Do this once |
|---|---|
| Claude Code | `/plugin marketplace add prajwalgajakesari/agent-sessions` then `/plugin install sessions@agent-sessions`. Repos set up with `init` prompt you automatically. |
| Codex CLI, OpenCode, Gemini CLI, Cursor, Copilot | `npx skills add prajwalgajakesari/agent-sessions --skill agent-sessions -g`, or `scripts/setup.sh --skills` (or `scripts\setup.ps1 -Skills`) from a checkout. Either puts the `agent-sessions` skill into `~/.agents/skills/`, which all of them read. The three other skills the CLI lists (`push`, `pull`, `init`) are the Claude plugin's and only work inside Claude Code. |
| Any terminal | `pip install agent-sessions-cli` or `uvx agent-sessions-cli`, then `agent-sessions doctor`. |

Requirements: git and Python 3.9+ (`python3`, `python`, or the Windows `py` launcher). No Python packages.

Then, in each repo you want to share sessions in: `/sessions:init` in Claude Code, or `$agent-sessions init` in Codex, or `agent-sessions init` in a terminal. It fixes a blanket `.claude/` ignore rule so only `.claude/sessions/` and `.claude/settings.json` are tracked, drops a README and `.gitattributes`, registers the Claude plugin in `.claude/settings.json`, and commits exactly those files. Add `--vendor-skill` to also copy the portable skill into `.agents/skills/` so every teammate's agent discovers it on clone.

## Using it

| Agent | Share this session | Read teammates' sessions |
|---|---|---|
| Claude Code | `/sessions:push [title] [--yes]` | `/sessions:pull [keyword]` |
| Codex CLI | `$agent-sessions push [title] --yes` | `$agent-sessions pull [keyword]` |
| OpenCode, Gemini CLI, Cursor, Copilot | "use the agent-sessions skill to push this session" | "use the agent-sessions skill to pull [keyword]" |
| Terminal | `agent-sessions locate` → write `summary.md` → `export` → `commit --push` | `agent-sessions list` |

Push flow, whichever agent runs it:

1. `locate` finds this session in the agent's store, checks the repo (ignore rules, detached HEAD, merges in progress, missing upstream) and prints an outline of the conversation.
2. The agent writes `summary.md` from the template, using the outline plus its own memory, through `write-summary` (agents' file tools tend to refuse `.claude/`).
3. `export` renders and redacts the transcript, writes `meta.json`, renames the folder to the final title, and prints a redaction report.
4. You see a short preview and confirm once. `--yes` skips it.
5. `commit --push` builds the commit on a temporary index inside `.git` and pushes to your current branch. Your staged changes, working tree and any in-progress merge are untouched. `--branch` pushes to a new remote branch instead.

Pushing the same session again updates the same folder and keeps everything after the first push.

Pull flags: `--n N` how many summaries (default 3), `--full <id>` one full transcript, `--all-branches` sessions on unmerged branches, `--mine` your own.

## How the live session is found

No agent tells a shell command which session it belongs to, so `locate` never guesses:

| Signal | Used by |
|---|---|
| `--session-id` passed by the skill | Claude Code (`${CLAUDE_SESSION_ID}` is substituted into the preflight) |
| Environment variable | Claude Code (`CLAUDE_CODE_SESSION_ID`); Codex if `CODEX_THREAD_ID` reaches child shells |
| **Self-reference**: the `locate` call itself is the newest shell call in exactly one session and has no output yet | Codex, OpenCode, Claude Code |
| A push invocation in the latest user turn | any |
| Exactly one session for this repo | any |

If more than one session still matches, `locate` prints `CANDIDATES:` and the skill asks you which one, then re-runs with `--agent` and `--session-id`. Codex fork and compaction stubs are skipped.

## Where each agent keeps sessions

| Agent | Store | Notes |
|---|---|---|
| Claude Code | `~/.claude/projects/<encoded-cwd>/<id>.jsonl` (+ `subagents/`) | active path via `parentUuid`, compaction boundaries, subagent edits counted |
| Codex CLI | `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl`, `session_index.jsonl` for titles | `exec` snippets parsed for `exec_command` and `apply_patch`; `compacted` history rendered once; developer messages and environment wrappers dropped |
| OpenCode | `~/.local/share/opencode/opencode.db` (SQLite, WAL) | opened read-only, copied if locked; `compaction` parts; child sessions feed files touched; tokens and cost from the session row |
| Gemini CLI | `~/.gemini/tmp/<hash>/chats/session-*.jsonl` | **experimental**: built from the source schema without a real sample; `$set`/`$rewindTo` applied |
| Copilot CLI, Cursor | detected, not exported | pull and init work; push says so. Cursor's plain-text agent transcripts are the next adapter once a sample exists |

## What is and is not redacted

| Layer | What it does |
|---|---|
| Result policy | Results of file reads, web fetches and MCP tools are replaced by a one-line stub. Other tool output is cut to 30 lines or 2 KB. `--include-results` keeps up to 20 KB per result. |
| Sensitive files | Any tool call that reads or edits `.env*`, `local.settings.json`, `appsettings*.json`, `secrets.json`, key and certificate files, Terraform state, `.netrc`, `.pypirc`, `.databrickscfg` or anything under `.azure/` has both input and result withheld. |
| Pattern redaction | AWS keys, GitHub, Slack, OpenAI and Anthropic tokens, JWTs, private keys, credentials in URLs, Azure storage account keys, shared access keys and SAS signatures, Azure DevOps PATs, Entra client secrets, bearer tokens. Keyword hits such as `password=`, `client_secret=`, `-P` after `sqlcmd` are redacted too and listed for you to review. |

GUIDs are never redacted. Placeholders like `${DB_PASSWORD}` and `<your-key>` are left alone. If a high-confidence pattern still matches after redaction, export deletes the transcript and refuses. Treat a pushed transcript the way you treat a chat log: read the preview before you say yes.

## Sandboxes and permissions

Codex's default sandbox blocks network access, so `commit --push` may fail; approve the escalation when Codex offers it, or run the printed `MANUAL` command in your own terminal. The temporary index lives inside `.git`, which stays writable. In OpenCode, allow the skill's script in `opencode.json` permissions to avoid a prompt per step.

## Layout of this repo

```
agent_sessions/                   the package (stdlib only): cli.py, core.py, model.py, adapters/, templates/
scripts/sessions.py, sessions.sh  entry point + shim (finds Python, always exits 0, prints STATUS)
scripts/setup.sh, setup.ps1       teammate bootstrap: --claude, --skills, --repo <path>
skills/{push,pull,init}/          the Claude Code plugin skills
.agents/skills/agent-sessions/     the portable Agent-Skills skill, with a bundled copy of the package
tools/sync_bundle.py              keeps that copy identical (`--check` in CI)
tests/                            python3 -m unittest discover -s tests
.claude-plugin/                   plugin.json (name: sessions) and marketplace.json (name: agent-sessions)
```

Test the Claude plugin from a checkout with `claude --plugin-dir /path/to/agent-sessions`. Test the portable skill by copying `.agents/skills/agent-sessions` into `~/.agents/skills/`.

## Contributing an adapter

An adapter is one file in `agent_sessions/adapters/` that turns a native store into `Session` events with canonical tool kinds (`shell`, `read`, `edit`, `write`, `search`, `web`, `agent`, `mcp`, `ask`). The renderer, result policy, redaction and files-touched logic never see agent names. Start from `gemini.py` (small) or `codex.py` (complete). A real session sample from your agent, with secrets removed, is the most useful contribution.

## License

MIT
