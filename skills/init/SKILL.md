---
name: init
description: Use when setting up a repository so teammates can share coding-agent sessions in it. Fixes .gitignore so .claude/sessions/ is tracked, adds a README and .gitattributes, and registers the sessions plugin in .claude/settings.json so anyone who opens the repo in Claude Code is prompted to install it. Triggers on "/sessions:init", "enable shared sessions here", "set up session sharing in this repo".
argument-hint: "[--yes] [--vendor-skill]"
disable-model-invocation: true
---

# Enable shared sessions in this repo

In the steps below, `<SCRIPT>` means `"${CLAUDE_PLUGIN_ROOT}/scripts/sessions.sh"`. Run every `<SCRIPT>` command with the Bash tool.

## Dry run

!`"${CLAUDE_PLUGIN_ROOT}/scripts/sessions.sh" init --dry-run --project-dir "${CLAUDE_PROJECT_DIR}"`

If the block above says shell command execution is disabled by policy, run that same command with the Bash tool first.

## Arguments

`$ARGUMENTS`

## Steps

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
