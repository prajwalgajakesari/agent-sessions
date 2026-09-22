---
name: agent-sessions
description: Share coding-agent sessions with teammates through the git repo you work in. Use when the user wants to push, publish, save or hand off the current session to the team; pull, load or read teammates' sessions or continue someone else's work; or set up (init) session sharing in a repository. Works from Codex, OpenCode, Gemini CLI, Cursor and Copilot; Claude Code users should install the sessions plugin instead.
license: MIT
compatibility: Requires git and Python 3.9+ (python3, python, or the Windows py launcher). Push reads the local session store of Codex CLI, OpenCode or Gemini CLI (experimental); pull and init work from any agent. Claude Code has its own plugin.
metadata:
  version: "0.2.0"
  homepage: https://github.com/prajwalgajakesari/agent-sessions
---

# agent-sessions

Three jobs, chosen by what the user asked for:

| The user wants to | Section |
|---|---|
| share, push, publish, save or hand off **this** session | Push |
| read, pull, load or continue **teammates'** sessions | Pull |
| enable session sharing in a repo | Init |

## Finding the script

This skill ships its code in its own folder. Resolve `<SCRIPT>` once, in this order, and reuse it in every step:

1. If `agent-sessions` is on PATH (installed with pip or uv), `<SCRIPT>` is `agent-sessions`.
2. Otherwise `<SCRIPT>` is `bash "<folder containing this SKILL.md>/scripts/sessions.sh"`. The default location is `~/.agents/skills/agent-sessions/`; inside a repo it may be `.agents/skills/agent-sessions/`.
3. On Windows without bash: `py -3 "<folder>/scripts/sessions.py"`.

`<TEMPLATE>` is `<folder containing this SKILL.md>/scripts/agent_sessions/templates/summary.md`. Every command prints `STATUS: ok` or `STATUS: error` as its first and last line, plus a `SCRIPT:` line with the absolute path that ran, so you can copy it. Run all commands through your shell tool from the repository you are working in.

## Push

Step 0: run `<SCRIPT> locate --agent auto` and treat its output as the preflight. It identifies this session by the shell call you just made (the call is still running when it looks, so it is unambiguous); if that fails it lists candidates. You may pass `--agent codex|opencode|gemini-cli` when you know which agent you are.

Arguments the user may give: a title or notes for the summary, `--yes` to skip the confirmation, `--branch <name>` to push to a new remote branch, `--include-results` to keep full tool output.

<!-- steps:push -->
1. **Check the preflight output.** If it ends in `STATUS: error`: when it lists `CANDIDATES:`, show them to the user, ask which one is this session, and re-run `<SCRIPT> locate --agent <agent> --session-id <id>`; otherwise explain the `REASON` and `WARNINGS` and stop (the usual fix is running `init` in this repo). Note `AGENT`, `SESSION_ID`, `SESSION_DIR`, `TITLE_DEFAULT`, `REPUSH` and `FOUND_BY`. If `FOUND_BY` is not "session id" or "environment", say so in the final report.

2. **Write the summary.** Read `<TEMPLATE>`, fill it in from the `OUTLINE` plus your own memory of this conversation, and write it through your shell tool with a quoted heredoc (file tools often refuse to write under `.claude/`):
   ```
   <SCRIPT> write-summary --out "<SESSION_DIR>" <<'SESSION_SUMMARY_EOF'
   ---
   title: "..."
   ...
   SESSION_SUMMARY_EOF
   ```
   Rules:
   - Hard cap of 120 lines and 6 KB. `write-summary` and export both reject anything larger; trim and write again.
   - `title`: from the arguments if given, otherwise a sharper version of `TITLE_DEFAULT`. `tags`: two to five short lowercase tags for area and kind of work. `outcome`: one sentence.
   - Leave `session_id`, `agent`, `author`, `handle`, `date` and `branch` as placeholders; export fills them.
   - Facts only. If something failed or was skipped, say so. Never paste secrets, connection strings, or query results into the summary.
   - If `REPUSH: yes`, read the existing `summary.md` first (`cat`) and revise it instead of starting over.

3. **Export.** Run:
   ```
   <SCRIPT> export --agent <AGENT> --session-id <SESSION_ID> --out "<SESSION_DIR>"
   ```
   Append `--include-results` only if the user asked for it. On `STATUS: error` show the `REASON` and stop. Export renames the directory to match the final title unless the session was pushed before, so **from here on use the `SESSION_DIR` printed by export**.

4. **Preview and confirm.** Show the user in a few lines: title, agent, branch, directory name, transcript size, message counts, files touched, `REDACTED_HIGH`, `REDACTED_REVIEW`, and the "Review these lines" list if present. A `NOTE:` line explaining an empty transcript is expected behaviour, not a bug to investigate; relay it and continue. Then ask one yes/no question: commit and push this? Skip the question only when `--yes` was passed. If the answer is no, stop and leave the files in place, uncommitted.

5. **Commit and push.** Run, using the `SESSION_DIR` from the export output:
   ```
   <SCRIPT> commit --dir "<SESSION_DIR>" --push
   ```
   Append `--branch <name>` if the user passed it. If the push fails with a network or DNS error you are probably in a sandbox: approve network access if your agent offers it, otherwise hand the user the `MANUAL` command exactly as printed.

6. **Report.** One short message: commit sha, branch, path, and how a teammate reads it: Claude Code `/sessions:pull <keyword>`, Codex `$agent-sessions pull <keyword>`, OpenCode, Gemini CLI, Cursor and Copilot "use the agent-sessions skill to pull <keyword>".

Do not run other git commands. Do not stage or commit anything outside `.claude/sessions/`. The commit is built on a temporary index, so the user's staged changes and working tree are untouched.
<!-- /steps:push -->

## Pull

Step 0: run `<SCRIPT> list` and treat its output as the index. It fetches, then reads `origin/<default branch>` and `HEAD` without touching the working tree. Arguments the user may give: a keyword, `--n N`, `--full <id>`, `--all-branches`, `--mine`.

<!-- steps:pull -->
1. **Check the index.** If it ends in `STATUS: error`, explain the `REASON` and stop. If `COUNT: 0`, tell the user nobody has pushed a session on this branch or the default branch yet, and mention `--all-branches` and pushing a session.

2. **Select sessions.** Each index line is `short id | date | author | agent | branch | title | outcome | #tags | <ref>:<path>`.
   - A keyword matches case-insensitively against title, tags, outcome, author and agent.
   - `--n N` sets how many to load. Default 3. Never load more than 8 without asking.
   - `--full <id>` selects the session whose short id or id starts with `<id>` and also loads its transcript (step 4).
   - `--mine` keeps only sessions whose author handle matches the current git user (`git config user.name`, lowercased, non-alphanumerics replaced by `-`).
   - `--all-branches` means: re-run `<SCRIPT> list --all-branches` through your shell tool, then select from that output.
   - With no keyword, take the N most recent.

3. **Load summaries.** For each selected session run, read-only:
   ```
   git show <ref>:<path>/summary.md
   ```
   using the `<ref>:<path>` printed at the end of that session's index line. Read every loaded summary in full.

4. **Full transcript, only when asked.** Run `git show <ref>:<path>/meta.json` and read `transcript_files`. Check the size first with `git show <ref>:<path>/transcript.md | wc -l`. If it is over 1500 lines, read it in chunks with `sed -n 'A,Bp'` and tell the user how much you loaded. Ask before loading a second transcript in one turn.

5. **Report.** Say which sessions you loaded: title, author, agent, date, branch. For each, two or three lines on outcome and open questions. Quote the `Handoff prompt` of the most relevant session verbatim in a fenced block so the user can act on it. End with how to get more: another keyword, `--full <id>`, or `--all-branches`.

Do not modify anything. Do not run `git pull`, `git checkout` or `git merge`; the index and `git show` are the only git access this needs.
<!-- /steps:pull -->

## Init

Step 0: run `<SCRIPT> init --dry-run` and treat its output as the dry run. Arguments the user may give: `--yes`, `--vendor-skill`.

<!-- steps:init -->
1. **Check the dry run.** If it ends in `STATUS: error`, explain the `REASON`. The common case is an ignore rule that lives outside the repo's `.gitignore` (a global excludes file or `.git/info/exclude`); relay the exact lines the output says to add, then stop.

2. **Nothing to do?** If `CHANGES: 0`, tell the user the repo is already set up and stop.

3. **Confirm.** Show the `CHANGES` list. Ask one yes/no question unless `--yes` was passed: apply and commit these changes? If the user passed `--vendor-skill`, or asks for teammates on Codex, OpenCode, Gemini CLI, Cursor or Copilot to get the skill automatically on clone, add `--vendor-skill` to the command in the next step; it copies the portable skill into `.agents/skills/agent-sessions/`.

4. **Apply.** Run:
   ```
   <SCRIPT> init --commit
   ```
   with `--vendor-skill` appended when chosen. On `STATUS: error` show the `REASON` and stop.

5. **Report.** Give the `COMMIT` and the `NEXT` push command. Do not push yourself. Add three facts for the user to pass on: teammates who open this repo in Claude Code are prompted to install the `sessions` plugin; teammates on other agents install the skill once with `scripts/setup.sh --skills` (or `setup.ps1`) from the agent-sessions repo, or get it on clone if the skill was vendored; anyone can share what they are working on with push and read others' work with pull.
<!-- /steps:init -->

## If something is off

`<SCRIPT> doctor` prints which agent stores were found, whether this skill and the Claude plugin are installed, and the Python and git versions. Gemini CLI support is experimental; Copilot CLI and Cursor sessions cannot be exported yet, but pull and init work there.
