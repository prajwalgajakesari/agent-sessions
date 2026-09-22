---
name: push
description: Use when the user wants to share the current Claude Code session with teammates by committing a handoff summary and a redacted transcript under .claude/sessions/ on the current branch and pushing it. Triggers on "/sessions:push", "push this session", "share this session with the team", "save this session to the repo", "hand this off".
argument-hint: "[title or notes] [--yes] [--branch <name>] [--include-results]"
disable-model-invocation: true
---

# Push this session to the repo

You are about to publish this conversation to a shared git repository. Teammates, possibly using other coding agents, will read the summary first and the transcript second. Follow the steps in order and stop at any `STATUS: error`.

In the steps below, `<SCRIPT>` means `"${CLAUDE_PLUGIN_ROOT}/scripts/sessions.sh"` and `<TEMPLATE>` means `${CLAUDE_PLUGIN_ROOT}/agent_sessions/templates/summary.md`. Run every `<SCRIPT>` command with the Bash tool.

## Preflight

!`"${CLAUDE_PLUGIN_ROOT}/scripts/sessions.sh" locate --agent claude-code --session-id "${CLAUDE_SESSION_ID}" --project-dir "${CLAUDE_PROJECT_DIR}"`

If the block above says shell command execution is disabled by policy, run that same command with the Bash tool first and treat its output as the preflight.

## Arguments

`$ARGUMENTS`

Flags: `--yes` skips the confirmation. `--branch <name>` pushes to a new remote branch instead of the current one. `--include-results` keeps full tool output; by default read, web and MCP results are omitted and other output is truncated. Everything else in the arguments is the title or notes for the summary.

## Steps

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
