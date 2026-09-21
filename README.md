# claude-sessions

Share Claude Code sessions with your team through the git repo you already work in.

Claude Code keeps every conversation in `~/.claude/projects/` as an undocumented JSONL file that is pruned after 30 days and cannot be shared. This plugin turns a session into three committed files under `.claude/sessions/` and gives teammates one command to load them into their own chat.

```
/sessions:push "Reconcile fabric tables"     you, at the end of a session
/sessions:pull reconcile                     a teammate, next morning
```

## What a pushed session looks like

```
.claude/sessions/2026-09-21_reconcile-fabric-tables_prajwal-p_6f6df382/
├── summary.md      handoff written by your Claude: goal, outcome, decisions and why, files changed,
│                   commands and gotchas, open questions, and a paste-ready handoff prompt (max 120 lines)
├── transcript.md   the conversation rendered to markdown along the active path (rewinds and
│                   compaction handled), tool calls collapsed, secrets redacted, query results omitted
└── meta.json       title, author, branch, dates, files touched (subagents included), tags, outcome
```

Pull reads `summary.md` first and loads a transcript only when you ask for it, so a teammate gets the reasoning without burning their whole context window.

## Install

Inside Claude Code:

```
/plugin marketplace add prajwalgajakesari/claude-sessions
/plugin install sessions@claude-sessions
```

Or from a terminal with `scripts/setup.sh` (macOS, Linux, WSL, Git Bash) or `scripts/setup.ps1` (Windows PowerShell). Both also accept a repo path to run `init` on.

Requirements: Claude Code 2.1.2xx or newer, git, and Python 3.8+ on PATH (`python3`, `python`, or the Windows `py` launcher). No Python packages.

## Set up a repo once

```
/sessions:init
```

In the repo you want to share sessions in. It:

- rewrites a blanket `.claude/` ignore rule to `.claude/*` and re-includes `.claude/sessions/` and `.claude/settings.json`, so only those are tracked
- creates `.claude/sessions/README.md` and a `.gitattributes` that keeps transcripts LF on every OS
- adds the marketplace and plugin to `.claude/settings.json`, so anyone who opens the repo in Claude Code is prompted to install the plugin
- commits exactly those files and prints the push command

## Skills

### `/sessions:push [title or notes] [--yes] [--branch <name>] [--include-results]`

1. Locates the live transcript by session id, checks the repo (ignore rules, detached HEAD, merges in progress, missing upstream) and prints an outline of the conversation.
2. Your Claude writes `summary.md` from the template, using the outline plus its own memory of the session.
3. The script renders and redacts the transcript, writes `meta.json`, and prints a redaction report.
4. You see a short preview and confirm once. `--yes` skips the question.
5. The commit is built on a temporary index and pushed to your current branch. Your staged changes, working tree and any in-progress merge are untouched. `--branch` pushes to a new remote branch instead without checking it out.

Pushing the same session again updates the same directory.

### `/sessions:pull [keyword] [--n 3] [--full <id>] [--all-branches] [--mine]`

Fetches, indexes every session on `origin/<default branch>` and `HEAD` by reading `meta.json` through `git show`, and loads the three most relevant summaries into the chat. Nothing is checked out or merged. `--all-branches` includes sessions on unmerged branches. `--full` reads one transcript.

### `/sessions:init [--yes]`

Described above. Safe to re-run.

## What is and is not redacted

Before anything is written, the transcript goes through three layers:

| Layer | What it does |
|---|---|
| Result policy | Results of MCP tools and file reads are replaced by a one-line stub. Other tool output is cut to 30 lines or 2 KB. `--include-results` keeps up to 20 KB per result. |
| Sensitive files | Any tool call that reads or edits `.env*`, `local.settings.json`, `appsettings*.json`, `secrets.json`, key and certificate files, Terraform state, `.netrc`, `.pypirc`, `.databrickscfg` or anything under `.azure/` has both input and result withheld. |
| Pattern redaction | AWS keys, GitHub, Slack, OpenAI and Anthropic tokens, JWTs, private keys, credentials in URLs, Azure storage account keys, shared access keys and SAS signatures, Azure DevOps PATs, Entra client secrets, bearer tokens. Keyword hits such as `password=`, `client_secret=`, `-P` after `sqlcmd` are redacted too and listed for you to review. |

GUIDs are never redacted. In Fabric and Azure everything is a GUID and none of them are secrets. Placeholders like `${DB_PASSWORD}` and `<your-key>` are left alone.

If a high-confidence pattern still matches after redaction, export deletes the transcript and refuses. Even so, treat a pushed transcript the way you treat a chat log: read the preview before you say yes.

## How it survives Claude Code changes

The JSONL format is internal to Claude Code and changes between versions. The renderer only trusts `user`, `assistant` and `compact_boundary` records, walks `parentUuid` from the newest message so abandoned branches and pre-compaction duplicates are skipped, tolerates a truncated last line, counts unknown record types instead of failing, and warns when a transcript was written by a newer Claude Code than the one it was tested against. Nothing in the repo format depends on the JSONL.

## Layout of this repo

```
.claude-plugin/plugin.json       plugin manifest (name: sessions)
.claude-plugin/marketplace.json  this repo is its own marketplace
skills/{push,pull,init}/SKILL.md the three skills
scripts/sessions.py              locate | export | commit | list | init (stdlib only)
scripts/sessions.sh              interpreter shim, always exits 0, prints STATUS
scripts/setup.sh, setup.ps1      teammate bootstrap
templates/                       summary template, repo README, .gitattributes
tests/                           python3 -m unittest discover tests
```

Test locally without installing:

```
claude --plugin-dir /path/to/claude-sessions
```

## License

MIT
