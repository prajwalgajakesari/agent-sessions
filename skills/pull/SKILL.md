---
name: pull
description: Use when the user wants to load teammates' shared Claude Code sessions from .claude/sessions/ into this conversation, browse what others did in this repo, or continue someone else's work. Triggers on "/sessions:pull", "pull team sessions", "what did others do here", "load the session about X", "continue from a teammate's session".
argument-hint: "[keyword] [--n 3] [--full <id>] [--all-branches] [--mine]"
disable-model-invocation: true
---

# Pull shared sessions into this chat

## Index

!`"${CLAUDE_PLUGIN_ROOT}/scripts/sessions.sh" list --project-dir "${CLAUDE_PROJECT_DIR}"`

If the block above says shell command execution is disabled by policy, run that same command with the Bash tool first and treat its output as the index. The index was built from `origin/<default branch>` and `HEAD` after a fetch. It never changes the working tree.

## Arguments

`$ARGUMENTS`

## Steps

1. **Check the index.** If it ends in `STATUS: error`, explain the `REASON` and stop. If `COUNT: 0`, tell the user nobody has pushed a session on this branch or the default branch yet, and mention `--all-branches` and `/sessions:push`.

2. **Select sessions.**
   - A keyword matches case-insensitively against title, tags, outcome and handle.
   - `--n N` sets how many to load. Default 3. Never load more than 8 without asking.
   - `--full <id>` selects the session whose id starts with `<id>` and also loads its transcript (step 4).
   - `--mine` keeps only sessions whose handle matches the current git user (`git config user.name`, lowercased, non-alphanumerics replaced by `-`).
   - `--all-branches` means: re-run the index command with `--all-branches` appended, via the Bash tool, then select from that output.
   - With no keyword, take the N most recent.

3. **Load summaries.** For each selected session run, read-only, with the Bash tool:
   ```
   git show <ref>:<path>/summary.md
   ```
   using the `<ref>:<path>` printed at the end of that session's index line. Read every loaded summary in full.

4. **Full transcript, only when asked.** Run `git show <ref>:<path>/meta.json` and read `transcript_files`. Check the size first with `git show <ref>:<path>/transcript.md | wc -l`. If it is over 1500 lines, read it in chunks with `sed -n 'A,Bp'` and tell the user how much you loaded. Ask before loading a second transcript in one turn.

5. **Report.** Say which sessions you loaded: title, author, date, branch. For each, two or three lines on outcome and open questions. Quote the `Handoff prompt` of the most relevant session verbatim in a fenced block so the user can act on it. End with how to get more: another keyword, `--full <id>`, or `--all-branches`.

Do not modify anything. Do not run `git pull`, `git checkout` or `git merge`; the index and `git show` are the only git access this skill needs.
