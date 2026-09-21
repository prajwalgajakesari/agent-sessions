---
name: push
description: Use when the user wants to share the current Claude Code session with teammates by committing a handoff summary and a redacted transcript under .claude/sessions/ on the current branch and pushing it. Triggers on "/sessions:push", "push this session", "share this session with the team", "save this session to the repo", "hand this off".
argument-hint: "[title or notes] [--yes] [--branch <name>] [--include-results]"
disable-model-invocation: true
---

# Push this session to the repo

You are about to publish this conversation to a shared git repository. Teammates will read the summary first and the transcript second. Follow the steps in order and stop at any `STATUS: error`.

## Preflight

!`"${CLAUDE_PLUGIN_ROOT}/scripts/sessions.sh" locate --session-id "${CLAUDE_SESSION_ID}" --project-dir "${CLAUDE_PROJECT_DIR}"`

If the block above says shell command execution is disabled by policy, run that same command with the Bash tool first and treat its output as the preflight.

## Arguments

`$ARGUMENTS`

Flags: `--yes` skips the confirmation. `--branch <name>` pushes to a new remote branch instead of the current one. `--include-results` keeps full tool output; by default MCP and file-read results are omitted and other output is truncated. Everything else in the arguments is the title or notes for the summary.

## Steps

1. **Check the preflight.** If it ends in `STATUS: error`, explain the `REASON` and `WARNINGS` to the user and stop. The usual fix is running `/sessions:init` in this repo. Otherwise note `SESSION_DIR`, `TRANSCRIPT`, `TITLE_DEFAULT` and `REPUSH`. If `FOUND_BY` is not "session id", say so in the final report; export re-checks the file by id anyway.

2. **Write the summary.** Read `${CLAUDE_PLUGIN_ROOT}/templates/summary.md`, fill it in from the `OUTLINE` plus your own memory of this conversation, and write it with the Bash tool through a quoted heredoc (the Write tool treats `.claude/` as protected):
   ```
   "${CLAUDE_PLUGIN_ROOT}/scripts/sessions.sh" write-summary --out "<SESSION_DIR>" <<'SESSION_SUMMARY_EOF'
   ---
   title: "..."
   ...
   SESSION_SUMMARY_EOF
   ```
   Rules:
   - Hard cap of 120 lines and 6 KB. `write-summary` and export both reject anything larger; trim and write again.
   - `title`: from the arguments if given, otherwise a sharper version of `TITLE_DEFAULT`. `tags`: two to five short lowercase tags for area and kind of work. `outcome`: one sentence.
   - Leave `session_id`, `author`, `handle`, `date` and `branch` as placeholders; export fills them.
   - Facts only. If something failed or was skipped, say so. Never paste secrets, connection strings, or query results into the summary.
   - If `REPUSH: yes`, read the existing `summary.md` first (`cat`) and revise it instead of starting over.

3. **Export.** Run with the Bash tool:
   ```
   "${CLAUDE_PLUGIN_ROOT}/scripts/sessions.sh" export --transcript "<TRANSCRIPT>" --out "<SESSION_DIR>" --session-id "${CLAUDE_SESSION_ID}"
   ```
   Append `--include-results` only if the user asked for it. On `STATUS: error` show the `REASON` and stop. Export renames the directory to match the final title unless the session was pushed before, so **from here on use the `SESSION_DIR` printed by export**.

4. **Preview and confirm.** Show the user in a few lines: title, branch, directory name, transcript size, message counts, files touched, `REDACTED_HIGH`, `REDACTED_REVIEW`, and the "Review these lines" list if present. Then ask one yes/no question: commit and push this? Skip the question only when `--yes` was passed. If the answer is no, stop and leave the files in place, uncommitted.

5. **Commit and push.** Run with the Bash tool, using the `SESSION_DIR` from the export output:
   ```
   "${CLAUDE_PLUGIN_ROOT}/scripts/sessions.sh" commit --dir "<SESSION_DIR>" --push
   ```
   Append `--branch <name>` if the user passed it.

6. **Report.** One short message: commit sha, branch, path, and how a teammate reads it (`/sessions:pull <keyword>`). If the push failed, show the `MANUAL` command exactly as printed so the user can run it in their own terminal.

Do not run other git commands. Do not stage or commit anything outside `.claude/sessions/`. The commit is built on a temporary index, so the user's staged changes and working tree are untouched.
