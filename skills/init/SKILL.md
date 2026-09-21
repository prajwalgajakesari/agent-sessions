---
name: init
description: Use when setting up a repository so teammates can share Claude Code sessions in it. Fixes .gitignore so .claude/sessions/ is tracked, adds a README and .gitattributes, and registers the claude-sessions plugin in .claude/settings.json so anyone who opens the repo is prompted to install it. Triggers on "/sessions:init", "enable shared sessions here", "set up session sharing in this repo".
argument-hint: "[--yes]"
disable-model-invocation: true
---

# Enable shared sessions in this repo

## Dry run

!`"${CLAUDE_PLUGIN_ROOT}/scripts/sessions.sh" init --dry-run --project-dir "${CLAUDE_PROJECT_DIR}"`

If the block above says shell command execution is disabled by policy, run that same command with the Bash tool first.

## Arguments

`$ARGUMENTS`

## Steps

1. **Check the dry run.** If it ends in `STATUS: error`, explain the `REASON`. The common case is an ignore rule that lives outside the repo's `.gitignore` (a global excludes file or `.git/info/exclude`); relay the exact lines the output says to add, then stop.

2. **Nothing to do?** If `CHANGES: 0`, tell the user the repo is already set up and stop.

3. **Confirm.** Show the `CHANGES` list. Ask one yes/no question unless `--yes` was passed: apply and commit these changes?

4. **Apply.** Run with the Bash tool:
   ```
   "${CLAUDE_PLUGIN_ROOT}/scripts/sessions.sh" init --commit --project-dir "${CLAUDE_PROJECT_DIR}"
   ```
   On `STATUS: error` show the `REASON` and stop.

5. **Report.** Give the `COMMIT` and the `NEXT` push command. Do not push yourself. Add two facts for the user to pass on: teammates who open this repo in Claude Code are prompted to install the `sessions` plugin, and anyone can share what they are working on with `/sessions:push` and read others' work with `/sessions:pull`. For teammates who prefer a terminal, the plugin repo ships `scripts/setup.sh` and `scripts/setup.ps1`.
