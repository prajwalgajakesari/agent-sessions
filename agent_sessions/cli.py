"""Command-line interface: locate | write-summary | export | commit | list | init | doctor.

Every subcommand prints "STATUS: ok|error" as its first and last line. Exit code is
0 on ok and 1 on error; scripts/sessions.sh swallows the code so skill preflights
never abort.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import platform
import shutil
import sys
from collections import OrderedDict
from typing import List, Optional, Tuple

from . import __version__
from .adapters import AGENT_CHOICES, REGISTRY, adapter_for, installed_adapters, resolve
from .adapters.base import SessionRef
from .core import (
    OUTLINE_MAX_PROMPTS, PACKAGE_DIR, SESSIONS_REL, SUMMARY_MAX_BYTES, SUMMARY_MAX_LINES, TOOL_VERSION,
    TRANSCRIPT_MAX_BYTES, UNSUPPORTED, PUSH_TIMEOUT, Redactor, Report, SessionsError, current_branch,
    default_remote_branch, dump_frontmatter, existing_session_dir, git_dir, git_out, handle_from_name, head_exists,
    home_to_tilde, in_progress_operation, load_template, parse_frontmatter, path_is_ignored, render_session,
    repo_root, run_git, script_path, session_dir_name, slugify, upstream_of, version_tuple, write_text,
)
from .model import Session


# --------------------------------------------------------------------------- helpers


def clean_arg(value: Optional[str]) -> Optional[str]:
    if value and value.strip() and not value.strip().startswith("${"):
        return value.strip()
    return None


def resolve_project_dir(explicit: Optional[str]) -> str:
    for cand in (explicit, os.environ.get("CLAUDE_PROJECT_DIR")):
        if cand and cand.strip() and not cand.startswith("${"):
            return os.path.abspath(cand.strip())
    return os.getcwd()


def detected_unsupported() -> List[Tuple[str, str, str]]:
    out = []
    for key, (label, paths) in UNSUPPORTED.items():
        for p in paths:
            full = os.path.expanduser(p)
            if glob.glob(full):
                out.append((key, label, p))
                break
    return out


def portable_skill_dir() -> Optional[str]:
    """The portable skill folder, from a checkout or from inside a bundle."""
    for cand in (PACKAGE_DIR.parent / "agents-skills" / "agent-sessions", PACKAGE_DIR.parent.parent):
        if (cand / "SKILL.md").exists() and (cand / "scripts").is_dir():
            return str(cand)
    return None


def fmt_when(ts: Optional[dt.datetime]) -> str:
    if not ts:
        return "?"
    delta = dt.datetime.now(dt.timezone.utc) - ts
    mins = int(delta.total_seconds() // 60)
    if mins < 60:
        return f"{mins} min ago"
    if mins < 60 * 48:
        return f"{mins // 60} h ago"
    return ts.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- locate


def cmd_locate(args: argparse.Namespace) -> Tuple[str, bool]:
    rep = Report()
    rep.kv("SCRIPT", script_path())
    project_dir = resolve_project_dir(args.project_dir)
    root = repo_root(project_dir)
    agent = args.agent or "auto"
    session_id = clean_arg(args.session_id)
    try:
        res = resolve(agent, session_id, root)
    except KeyError:
        return rep.finish(False, f"unknown agent {agent}; choose from {', '.join(AGENT_CHOICES)}"), False

    if res.ref is None:
        rep.kv("REPO_ROOT", root)
        rep.kv("AGENT", agent)
        for key, label, p in detected_unsupported():
            rep.add(f"  note: {label} detected ({p}); transcript export for it is not supported yet")
        if res.candidates:
            rep.add("CANDIDATES:")
            for c in res.candidates:
                rep.add(f"  - {c.agent} | {c.session_id} | {c.title or '(untitled)'} | {fmt_when(c.updated)} | {home_to_tilde(c.cwd or '?')}")
            rep.add("Ask the user which one, then re-run:  locate --agent <agent> --session-id <id>")
            return rep.finish(False, "several recent sessions match this repo; pick one"), False
        return rep.finish(False, "no session found for this repo in any supported agent store"
                          " (Claude Code, Codex, OpenCode, Gemini CLI). Pass --agent and --session-id, or start from a session that has at least one turn."), False

    adapter, ref = res.adapter, res.ref
    assert adapter is not None
    sess: Optional[Session] = ref.extra.get("session")
    if sess is None and ref.on_disk:
        sess = adapter.load(ref)
    title = args.title or (sess.title if sess else None) or ref.title or "untitled session"
    name = git_out(["config", "user.name"], root) or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    email = git_out(["config", "user.email"], root)
    handle = handle_from_name(name)
    branch = current_branch(root)
    existing = existing_session_dir(root, ref.session_id, ref.short_id)
    dir_name = os.path.basename(existing) if existing else session_dir_name(sess.started if sess else None, title, handle, ref.short_id)

    rep.kv("REPO_ROOT", root)
    rep.kv("AGENT", adapter.name)
    rep.kv("AGENT_LABEL", adapter.label)
    rep.kv("SOURCE", ref.source or "(not written to disk yet; export will find it by session id)")
    rep.kv("TRANSCRIPT", ref.source or "(not written to disk yet)")
    rep.kv("FOUND_BY", res.how)
    rep.kv("SESSION_ID", ref.session_id)
    rep.kv("SHORT_ID", ref.short_id)
    rep.kv("TITLE_DEFAULT", title)
    rep.kv("AUTHOR", f"{name} <{email}>" if email else name)
    rep.kv("HANDLE", handle)
    rep.kv("BRANCH", branch or "(detached HEAD)")
    rep.kv("SESSION_DIR", os.path.join(root, SESSIONS_REL, dir_name))
    rep.kv("REPUSH", "yes" if existing else "no")
    rep.kv("STARTED", sess.started.isoformat() if sess and sess.started else "?")
    rep.kv("RECORDS", f"{sess.record_count} ({sess.bad_lines} unparsable lines skipped)" if sess else "0")
    rep.kv("SUBAGENT_FILES", sess.subagent_files if sess else 0)

    warnings: List[str] = []
    if not sess:
        warnings.append("this session's transcript is not on disk yet (nothing persisted before this command); the outline below is empty")
    if sess:
        warnings.extend(sess.warnings)
    if sess and sess.cwd and not os.path.realpath(sess.cwd).startswith(os.path.realpath(root)):
        warnings.append(f"session cwd {home_to_tilde(sess.cwd)} is outside this repo")
    ignored = path_is_ignored(root, f"{SESSIONS_REL}/probe.md")
    if ignored:
        warnings.append(f"{SESSIONS_REL}/ is gitignored by {ignored}. Run /sessions:init (or `agent-sessions init`) in this repo first.")
    op = in_progress_operation(root)
    if op:
        warnings.append(f"git operation in progress ({op}); the commit is still safe because it uses a temporary index")
    if not head_exists(root):
        warnings.append("repository has no commits yet; make an initial commit before pushing a session")
    if not branch:
        warnings.append("detached HEAD; pass --branch sessions/<handle>/<date> to push to a new branch")
    elif not upstream_of(root, branch):
        warnings.append(f"branch {branch} has no upstream; push will run `git push -u origin {branch}`")
    if not email:
        warnings.append("git user.email is not set; commits will use a default identity")
    if git_out(["config", "--bool", "commit.gpgsign"], root) == "true":
        warnings.append("commit.gpgsign is enabled; commit-tree will try to sign and may fail without an agent")
    if sess and sess.agent_version and adapter.tested_version:
        v = version_tuple(sess.agent_version)
        if v and v > adapter.tested_version:
            warnings.append(f"session written by {adapter.label} {sess.agent_version}, newer than the tested {'.'.join(map(str, adapter.tested_version))}")
    if adapter.name == "gemini-cli":
        warnings.append("Gemini CLI support is experimental (built from the source schema, no real sample yet); check the transcript before confirming")
    for key, label, p in detected_unsupported():
        warnings.append(f"{label} detected ({p}); transcript export for it is not supported yet")
    rep.kv("WARNINGS", len(warnings))
    for w in warnings:
        rep.add("  -", w)

    prompts = sess.user_prompts() if sess else []
    files = sess.files_touched() if sess else []
    tasks = sess.agent_tasks() if sess else []
    rep.blank()
    rep.add("OUTLINE")
    rep.add(f"Prompts ({len(prompts)}):")
    shown = prompts if len(prompts) <= OUTLINE_MAX_PROMPTS else prompts[: OUTLINE_MAX_PROMPTS // 2] + [(None, "…")] + prompts[-OUTLINE_MAX_PROMPTS // 2:]
    for i, (ts, text) in enumerate(shown, 1):
        stamp = ts.strftime("%H:%M") if ts else "--:--"
        rep.add(f"  {i:>3}. [{stamp}] {home_to_tilde(text)}")
    rep.add(f"Files touched ({len(files)}, including subagents):")
    for f in files[:60]:
        rep.add("  -", f)
    if len(files) > 60:
        rep.add(f"  … {len(files) - 60} more")
    if tasks:
        rep.add(f"Subagent tasks ({len(tasks)}):")
        for t in tasks[:20]:
            rep.add("  -", t)
    blocking = ignored is not None or not head_exists(root)
    return rep.finish(not blocking, "preflight blocked: see WARNINGS" if blocking else ""), not blocking


# --------------------------------------------------------------------------- write-summary


def cmd_write_summary(args: argparse.Namespace) -> Tuple[str, bool]:
    rep = Report()
    out_dir = os.path.abspath(args.out)
    text = sys.stdin.read().strip("\n") + "\n"
    if not text.strip() or not text.lstrip().startswith("---"):
        return rep.finish(False, "summary must start with a YAML frontmatter block (---)"), False
    lines = text.count("\n")
    nbytes = len(text.encode("utf-8"))
    rep.kv("SESSION_DIR", out_dir)
    rep.kv("SUMMARY_LINES", lines)
    rep.kv("SUMMARY_BYTES", nbytes)
    if lines > SUMMARY_MAX_LINES or nbytes > SUMMARY_MAX_BYTES:
        return rep.finish(False, f"summary exceeds the cap ({SUMMARY_MAX_LINES} lines / {SUMMARY_MAX_BYTES} bytes); trim it and write again"), False
    write_text(os.path.join(out_dir, "summary.md"), text)
    return rep.finish(True), True


# --------------------------------------------------------------------------- export


def _ref_for_export(agent: str, session_id: Optional[str], source: Optional[str], rep: Report) -> Tuple[Optional[SessionRef], Optional[object]]:
    """Pick the session to export: explicit id through the named adapter first, then the given source."""
    adapters = installed_adapters(agent) if agent == "auto" else [adapter_for(agent)]
    if session_id:
        for a in adapters:
            ref = a.locate_by_id(session_id)
            if ref:
                if source and os.path.abspath(source) != os.path.abspath(ref.source) and not ref.source.startswith("sqlite:"):
                    rep.kv("TRANSCRIPT_SWITCHED", f"{home_to_tilde(source)} -> {home_to_tilde(ref.source)} (matched by session id)")
                elif source and source != ref.source:
                    rep.kv("TRANSCRIPT_SWITCHED", f"{source} -> {ref.source} (matched by session id)")
                return ref, a
        rep.kv("TRANSCRIPT_WARNING", f"no session {session_id} found in the {agent} store; exporting the given source")
    if source:
        # a source path is self-describing: ask every adapter, even ones whose store is absent on this machine
        pool = adapters if agent != "auto" else list(REGISTRY.values())
        for a in pool:
            ref = a.ref_from_source(source)
            if ref:
                return ref, a
    return None, None


def cmd_export(args: argparse.Namespace) -> Tuple[str, bool]:
    rep = Report()
    out_dir = os.path.abspath(args.out)
    root = repo_root(os.path.dirname(out_dir) if os.path.isdir(os.path.dirname(out_dir)) else os.getcwd())
    agent = args.agent or "auto"
    session_id = clean_arg(args.session_id)
    source = clean_arg(args.source) or clean_arg(args.transcript)
    if not session_id and not source:
        return rep.finish(False, "pass --session-id (from locate) or --source"), False
    try:
        ref, adapter = _ref_for_export(agent, session_id, source, rep)
    except KeyError:
        return rep.finish(False, f"unknown agent {agent}"), False
    if ref is None or adapter is None:
        return rep.finish(False, f"could not resolve a session from --session-id {session_id or '-'} / --source {source or '-'}"), False
    ref.extra["repo_root"] = root
    sess: Session = adapter.load(ref)  # type: ignore[union-attr]
    rep.kv("AGENT", sess.agent)
    rep.kv("SOURCE", ref.source)
    rep.kv("TRANSCRIPT", ref.source)
    if not sess.events and sess.record_count == 0:
        return rep.finish(False, f"no records could be read from {ref.source}"), False

    summary_path = os.path.join(out_dir, "summary.md")
    if not os.path.exists(summary_path):
        return rep.finish(False, f"summary.md not found in {out_dir}; write it first"), False
    with open(summary_path, "r", encoding="utf-8", errors="replace") as fh:
        summary_raw = fh.read()
    fm, body = parse_frontmatter(summary_raw)

    name = git_out(["config", "user.name"], root) or "unknown"
    handle = handle_from_name(name)
    branch = current_branch(root) or "(detached)"
    title = str(args.title or fm.get("title") or sess.title or "untitled session").strip()
    if title.startswith("<") or not title:
        title = sess.title or "untitled session"
    tags = fm.get("tags") if isinstance(fm.get("tags"), list) else []
    tags = [slugify(str(t), 30) for t in tags if str(t).strip() and not str(t).startswith("<")]
    outcome = str(fm.get("outcome") or "").strip()
    if outcome.startswith("<"):
        outcome = ""

    desired = session_dir_name(sess.started, title, handle, sess.short_id)
    renamed = False
    if os.path.basename(out_dir) != desired:
        rel_out = os.path.relpath(out_dir, root).replace(os.sep, "/")
        tracked = head_exists(root) and run_git(["cat-file", "-e", f"HEAD:{rel_out}/meta.json"], root).ok
        new_dir = os.path.join(os.path.dirname(out_dir), desired)
        if not tracked and not os.path.exists(new_dir):
            os.rename(out_dir, new_dir)
            out_dir = new_dir
            summary_path = os.path.join(out_dir, "summary.md")
            renamed = True
    # printed before any later failure so the caller never loses track of a renamed dir
    rep.kv("SESSION_DIR", out_dir)
    rep.kv("RENAMED", "yes (use this SESSION_DIR from now on)" if renamed else "no")

    fm["title"] = title
    fm["session_id"] = sess.session_id
    fm["agent"] = sess.agent
    fm["author"] = name
    fm["handle"] = handle
    fm["date"] = (sess.started or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d")
    fm["branch"] = branch
    fm["tags"] = tags
    fm["outcome"] = outcome or "(not stated)"
    summary_text = dump_frontmatter(fm) + "\n" + body.lstrip("\n")

    redactor = Redactor()
    summary_text = redactor.redact(home_to_tilde(summary_text), "summary.md")
    s_lines = summary_text.count("\n") + 1
    s_bytes = len(summary_text.encode("utf-8"))
    if s_lines > SUMMARY_MAX_LINES or s_bytes > SUMMARY_MAX_BYTES:
        rep.kv("SUMMARY_LINES", s_lines)
        rep.kv("SUMMARY_BYTES", s_bytes)
        return rep.finish(False, f"summary.md exceeds the cap ({SUMMARY_MAX_LINES} lines / {SUMMARY_MAX_BYTES} bytes); trim it"), False

    mode = "full" if args.include_results else "policy"
    segments, stats = render_session(sess, redactor, mode, args.include_thinking, title)
    transcript_text = "\n".join(segments)
    if len(transcript_text.encode("utf-8")) > TRANSCRIPT_MAX_BYTES:
        redactor2 = Redactor()
        segments, stats = render_session(sess, redactor2, "stub", args.include_thinking, title)
        redactor.hits.update(redactor2.hits)
        redactor.review.extend(redactor2.review)
        transcript_text = "\n".join(segments)
        stats["degraded_to_stubs"] = 1
    if stats.get("messages", 0) == 0 and not stats.get("stopped_at_push") and not stats.get("commands") and not stats.get("tool_calls"):
        return rep.finish(False, "zero messages rendered; the session format may have changed"), False

    files_written: List[str] = []
    for old in glob.glob(os.path.join(out_dir, "transcript*.md")):
        os.remove(old)
    if len(transcript_text.encode("utf-8")) > TRANSCRIPT_MAX_BYTES:
        chunk: List[str] = []
        size = 0
        part = 1
        for seg in segments:
            b = len(seg.encode("utf-8")) + 1
            if chunk and size + b > TRANSCRIPT_MAX_BYTES * 0.9:
                p = os.path.join(out_dir, f"transcript-{part}.md")
                write_text(p, "\n".join(chunk))
                files_written.append(p)
                part += 1
                chunk, size = [], 0
            chunk.append(seg)
            size += b
        if chunk:
            p = os.path.join(out_dir, f"transcript-{part}.md")
            write_text(p, "\n".join(chunk))
            files_written.append(p)
    else:
        p = os.path.join(out_dir, "transcript.md")
        write_text(p, transcript_text)
        files_written.append(p)

    files_touched = sess.files_touched()
    meta = OrderedDict([
        ("session_id", sess.session_id),
        ("short_id", sess.short_id),
        ("agent", sess.agent),
        ("agent_version", sess.agent_version),
        ("originator", sess.originator),
        ("parent_id", sess.parent_id),
        ("title", title),
        ("slug", slugify(title)),
        ("dir", os.path.basename(out_dir)),
        ("author", name),
        ("handle", handle),
        ("started", sess.started.isoformat() if sess.started else None),
        ("ended", sess.ended.isoformat() if sess.ended else None),
        ("branch", branch),
        ("files_touched", files_touched),
        ("tags", tags),
        ("outcome", outcome or "(not stated)"),
        ("tokens", sess.tokens),
        ("pushed_at", dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()),
        ("tool_version", TOOL_VERSION),
        ("transcript_files", [os.path.basename(f) for f in files_written]),
        ("stats", {k: v for k, v in stats.items() if not k.startswith("ignored:")}),
    ])
    meta_text = redactor.redact(json.dumps(meta, indent=2, ensure_ascii=False), "meta.json") + "\n"
    write_text(summary_path, summary_text)
    write_text(os.path.join(out_dir, "meta.json"), meta_text)

    residual = []
    for p in files_written + [summary_path, os.path.join(out_dir, "meta.json")]:
        with open(p, "r", encoding="utf-8") as fh:
            for pat in Redactor.residual_high(fh.read()):
                residual.append(f"{os.path.basename(p)}:{pat}")

    rep.kv("SHORT_ID", sess.short_id)
    rep.kv("TITLE", title)
    rep.kv("BRANCH", branch)
    rep.kv("FILES", ", ".join(os.path.basename(f) for f in files_written) + ", summary.md, meta.json")
    rep.kv("TRANSCRIPT_BYTES", sum(os.path.getsize(f) for f in files_written))
    rep.kv("SUMMARY_LINES", s_lines)
    rep.kv("MESSAGES", f"{stats.get('user_messages', 0)} user / {stats.get('commands', 0)} slash commands / "
                       f"{stats.get('assistant_messages', 0)} assistant / {stats.get('tool_calls', 0)} tool calls")
    rep.kv("FILES_TOUCHED", len(files_touched))
    rep.kv("COMPACTIONS", stats.get("compactions", 0))
    rep.kv("WITHHELD_RESULTS", stats.get("withheld_results", 0))
    rep.kv("RESULT_MODE", "full (opt-in)" if mode == "full" else ("stubs (size budget)" if stats.get("degraded_to_stubs") else "policy: read/web/MCP results stubbed, others truncated"))
    rep.kv("REDACTED_HIGH", redactor.high_count())
    rep.kv("REDACTED_REVIEW", redactor.review_count())
    if stats.get("messages", 0) == 0 and stats.get("stopped_at_push"):
        rep.kv("NOTE", "transcript is empty by design: the push command was the first turn of this session, and everything from the push onward is never exported. This is not an exporter fault; continue.")
    if sess.agent == "gemini-cli":
        rep.kv("EXPERIMENTAL", "Gemini CLI adapter is built from the source schema without a real sample; review the transcript")
    if redactor.hits:
        rep.add("Redactions by pattern:")
        for k, v in sorted(redactor.hits.items(), key=lambda kv: -kv[1]):
            rep.add(f"  - {k}: {v}")
    if redactor.review:
        rep.add("Review these lines (keyword-based redactions, may be false positives):")
        for label, line, pat in redactor.review[:20]:
            rep.add(f"  - {label}:{line} {pat}")
        if len(redactor.review) > 20:
            rep.add(f"  … {len(redactor.review) - 20} more")
    if residual:
        for p in files_written:
            os.remove(p)
        return rep.finish(False, "high-confidence secret survived redaction: " + ", ".join(residual) + ". Transcript files removed; fix and re-run."), False
    return rep.finish(True), True


# --------------------------------------------------------------------------- commit


def cmd_commit(args: argparse.Namespace) -> Tuple[str, bool]:
    rep = Report()
    session_dir = os.path.abspath(args.dir)
    if not os.path.isdir(session_dir):
        return rep.finish(False, f"{session_dir} does not exist"), False
    root = repo_root(session_dir)
    rel = os.path.relpath(session_dir, root).replace(os.sep, "/")
    if not rel.startswith(SESSIONS_REL + "/"):
        return rep.finish(False, f"{rel} is not under {SESSIONS_REL}/"), False
    try:
        with open(os.path.join(session_dir, "meta.json"), "r", encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return rep.finish(False, "meta.json missing or invalid; run export first"), False
    title = str(meta.get("title") or "session")
    session_id = str(meta.get("session_id") or "")
    agent = str(meta.get("agent") or "claude-code")
    if not head_exists(root):
        return rep.finish(False, "repository has no commits yet"), False
    head = git_out(["rev-parse", "HEAD"], root)
    branch = current_branch(root)
    target_branch = args.branch
    if not branch and not target_branch:
        return rep.finish(False, "detached HEAD; pass --branch <name>"), False
    is_update = run_git(["cat-file", "-e", f"HEAD:{rel}/meta.json"], root).ok

    # temp index inside the git dir: writable even in sandboxes that block /tmp
    tmp_index = os.path.join(git_dir(root), f"sessions-index.{os.getpid()}")
    try:
        env = {"GIT_INDEX_FILE": tmp_index}
        r = run_git(["read-tree", "HEAD"], root, env_extra=env)
        if not r.ok:
            return rep.finish(False, "read-tree failed: " + r.err.strip()), False
        r = run_git(["add", "-f", "--", rel], root, env_extra=env)
        if not r.ok:
            return rep.finish(False, "add failed: " + r.err.strip()), False
        r = run_git(["write-tree"], root, env_extra=env)
        if not r.ok:
            return rep.finish(False, "write-tree failed: " + r.err.strip()), False
        tree = r.out.strip()
    finally:
        try:
            os.remove(tmp_index)
        except OSError:
            pass
    if tree == git_out(["rev-parse", "HEAD^{tree}"], root):
        return rep.finish(False, "nothing to commit: session files are identical to HEAD"), False

    verb = "update" if is_update else "add"
    trailers = [f"Agent-Session: {agent}:{session_id}", f"Agent-Session-Path: {rel}"]
    if agent == "claude-code":
        trailers.insert(0, f"Claude-Session: {session_id}")
    message = f"docs(sessions): {verb} {title}\n\n" + "\n".join(trailers) + "\n"
    r = run_git(["commit-tree", tree, "-p", head, "-F", "-"], root, stdin=message)
    if not r.ok:
        return rep.finish(False, "commit-tree failed: " + r.err.strip()), False
    commit = r.out.strip()

    if target_branch:
        pushed_ref = target_branch
        run_git(["update-ref", f"refs/heads/{target_branch}", commit], root)
    else:
        r = run_git(["update-ref", "-m", "sessions: push", f"refs/heads/{branch}", commit, head], root)
        if not r.ok:
            return rep.finish(False, "update-ref failed (HEAD moved during commit?): " + r.err.strip()), False
        run_git(["add", "-f", "--", rel], root)  # keep the user's real index in sync for these paths
        pushed_ref = branch

    rep.kv("COMMIT", commit)
    rep.kv("BRANCH", pushed_ref)
    rep.kv("ACTION", verb)
    rep.kv("PATH", rel)
    if not args.push:
        rep.kv("PUSHED", "no (--push not given)")
        return rep.finish(True), True

    remote = args.remote
    if target_branch:
        push_args = ["push", remote, f"{commit}:refs/heads/{target_branch}"]
        manual = f"git push {remote} {commit}:refs/heads/{target_branch}"
    else:
        up = upstream_of(root, branch or "")
        push_args = ["push", remote, branch] if up else ["push", "-u", remote, branch]
        manual = " ".join(["git"] + push_args)
    r = run_git(push_args, root, timeout=PUSH_TIMEOUT)
    if r.ok:
        rep.kv("PUSHED", f"yes -> {remote}/{pushed_ref}")
        return rep.finish(True), True
    rep.kv("PUSHED", "no")
    rep.add("Push output:")
    for line in (r.err or r.out).strip().split("\n")[-8:]:
        rep.add("  ", line)
    rep.kv("MANUAL", manual)
    hint = " If this is a sandboxed agent (Codex), approve network access or run MANUAL in your own terminal." \
        if any(w in (r.err or "").lower() for w in ("could not resolve", "network", "timed out", "connection")) else ""
    return rep.finish(False, "commit created locally but push failed; run the MANUAL command in your terminal." + hint), False


# --------------------------------------------------------------------------- list


def cmd_list(args: argparse.Namespace) -> Tuple[str, bool]:
    rep = Report()
    project_dir = resolve_project_dir(args.project_dir)
    root = repo_root(project_dir)
    remote = args.remote
    has_remote = bool(git_out(["remote", "get-url", remote], root))
    if has_remote and not args.no_fetch:
        r = run_git(["fetch", remote, "--quiet"], root, timeout=45)
        rep.kv("FETCH", "ok" if r.ok else f"failed ({(r.err or r.out).strip().split(chr(10))[-1][:120]}); using local refs")
    else:
        rep.kv("FETCH", "skipped" if has_remote else f"no remote named {remote}")
    refs: List[str] = []
    default = default_remote_branch(root, remote) if has_remote else None
    if default:
        refs.append(f"{remote}/{default}")
    if head_exists(root):
        refs.append("HEAD")
    if args.all_branches and has_remote:
        r = run_git(["for-each-ref", "--format=%(refname:short)", "--sort=-committerdate", f"refs/remotes/{remote}"], root)
        for line in r.out.split("\n"):
            line = line.strip()
            if line and line != f"{remote}/HEAD" and line not in refs and len(refs) < 52:
                refs.append(line)
    rep.kv("REFS", ", ".join(refs) or "(none)")

    sessions = {}
    for ref in refs:
        r = run_git(["ls-tree", "-r", "--name-only", ref, "--", SESSIONS_REL], root)
        if not r.ok:
            continue
        for p in r.out.split("\n"):
            p = p.strip()
            if not p.endswith("/meta.json"):
                continue
            show = run_git(["show", f"{ref}:{p}"], root)
            if not show.ok:
                continue
            try:
                meta = json.loads(show.out)
            except ValueError:
                continue
            sid = str(meta.get("session_id") or p)
            entry = {"ref": ref, "path": p[: -len("/meta.json")], "meta": meta}
            prev = sessions.get(sid)
            if prev is None or str(meta.get("pushed_at") or "") > str(prev["meta"].get("pushed_at") or ""):
                sessions[sid] = entry
    ordered = sorted(sessions.values(), key=lambda e: str(e["meta"].get("pushed_at") or e["meta"].get("started") or ""), reverse=True)
    rep.kv("COUNT", len(ordered))
    rep.add("Read a summary with:  git show <ref>:<path>/summary.md    (transcript: <path>/transcript.md)")
    rep.blank()
    for e in ordered:
        m = e["meta"]
        sid = str(m.get("short_id") or str(m.get("session_id") or "")[:8])
        date = str(m.get("started") or m.get("pushed_at") or "")[:10]
        tags = ",".join(m.get("tags") or []) if isinstance(m.get("tags"), list) else ""
        agent = m.get("agent") or "claude-code"
        rep.add(f"- {sid} | {date} | {m.get('handle', '?')} | {agent} | {m.get('branch', '?')} | {m.get('title', '?')} | {m.get('outcome', '')}"
                f"{' | #' + tags if tags else ''} | {e['ref']}:{e['path']}")
    if args.json:
        rep.blank()
        rep.add("JSON: " + json.dumps([{"ref": e["ref"], "path": e["path"], **{k: e["meta"].get(k) for k in
                                        ("session_id", "short_id", "agent", "title", "handle", "branch", "started", "pushed_at", "outcome", "tags")}} for e in ordered]))
    return rep.finish(True), True


# --------------------------------------------------------------------------- init


def cmd_init(args: argparse.Namespace) -> Tuple[str, bool]:
    rep = Report()
    project_dir = resolve_project_dir(args.project_dir)
    root = repo_root(project_dir)
    changes: List[str] = []
    touched: List[str] = []
    gitignore = os.path.join(root, ".gitignore")

    for probe in (f"{SESSIONS_REL}/probe.md", ".claude/settings.json"):
        ignored = path_is_ignored(root, probe)
        if not ignored:
            continue
        src, _, pattern = ignored.split(":", 2)
        src_path = os.path.normpath(os.path.join(root, src))
        if src_path != os.path.normpath(gitignore):
            rep.add(f"{probe} is ignored by {src} (pattern {pattern}), which is outside the repo's .gitignore.")
            rep.add(f"Add these lines to {src} or to {gitignore} manually:  !{SESSIONS_REL}/  and  !.claude/settings.json")
            return rep.finish(False, "ignore rule lives outside .gitignore; fix it manually then re-run"), False
        with open(gitignore, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().split("\n")
        new_lines = []
        rewrote = False
        for line in lines:
            if line.strip() in (".claude", ".claude/", "/.claude", "/.claude/") and not rewrote:
                new_lines.append(".claude/*")
                rewrote = True
                changes.append(f".gitignore: rewrote '{line.strip()}' to '.claude/*' so sub-paths can be re-included")
            else:
                new_lines.append(line)
        for neg in (f"!{SESSIONS_REL}/", "!.claude/settings.json"):
            if neg not in [l.strip() for l in new_lines]:
                new_lines.append(neg)
                changes.append(f".gitignore: added '{neg}'")
        if not args.dry_run:
            write_text(gitignore, "\n".join(new_lines))
        touched.append(".gitignore")
        if path_is_ignored(root, probe) and not args.dry_run:
            return rep.finish(False, f"{probe} is still ignored after rewriting .gitignore (pattern {pattern}); edit .gitignore manually"), False
        break

    sessions_dir = os.path.join(root, SESSIONS_REL)
    for fname, template in (("README.md", "sessions-README.md"), (".gitattributes", "gitattributes")):
        dest = os.path.join(sessions_dir, fname)
        if not os.path.exists(dest):
            changes.append(f"create {SESSIONS_REL}/{fname}")
            if not args.dry_run:
                write_text(dest, load_template(template))
            touched.append(f"{SESSIONS_REL}/{fname}")

    settings_path = os.path.join(root, ".claude", "settings.json")
    settings: dict = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as fh:
                settings = json.load(fh, object_pairs_hook=OrderedDict)
        except ValueError:
            return rep.finish(False, f"{settings_path} is not valid JSON; fix it before running init"), False
    market_name, plugin_name, repo = args.marketplace_name, args.plugin_name, args.marketplace_repo
    markets = settings.setdefault("extraKnownMarketplaces", OrderedDict())
    if market_name not in markets:
        markets[market_name] = OrderedDict([("source", OrderedDict([("source", "github"), ("repo", repo)]))])
        changes.append(f"settings.json: registered marketplace {market_name} -> github:{repo}")
    key = f"{plugin_name}@{market_name}"
    enabled = settings.get("enabledPlugins")
    if isinstance(enabled, list):
        if key not in enabled:
            enabled.append(key)
            changes.append(f"settings.json: enabled plugin {key}")
    else:
        if not isinstance(enabled, dict):
            enabled = OrderedDict()
            settings["enabledPlugins"] = enabled
        if not enabled.get(key):
            enabled[key] = True
            changes.append(f"settings.json: enabled plugin {key}")
    note = f"agent-sessions: {SESSIONS_REL}/ holds shared session handoffs from any coding agent. Claude: /{plugin_name}:push and /{plugin_name}:pull. Others: the agent-sessions skill."
    comment = settings.get("_comment")
    if isinstance(comment, list):
        if not any("agent-sessions:" in str(c) or "claude-sessions:" in str(c) for c in comment):
            comment.append(note)
            changes.append("settings.json: added _comment entry")
    elif comment is None:
        settings["_comment"] = [note]
        changes.append("settings.json: added _comment entry")
    if any(c.startswith("settings.json") for c in changes):
        if not args.dry_run:
            write_text(settings_path, json.dumps(settings, indent=2, ensure_ascii=False))
        touched.append(".claude/settings.json")

    if args.vendor_skill:
        src = portable_skill_dir()
        dest = os.path.join(root, ".agents", "skills", "agent-sessions")
        if not src:
            return rep.finish(False, "portable skill folder not found next to this installation; run from a checkout or ~/.agents/skills/agent-sessions"), False
        changes.append(f"vendor the agent-sessions skill into .agents/skills/agent-sessions (from {home_to_tilde(src)})")
        if not args.dry_run:
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        touched.append(".agents/skills/agent-sessions")

    rep.kv("REPO_ROOT", root)
    rep.kv("MODE", "dry-run" if args.dry_run else "applied")
    rep.kv("CHANGES", len(changes))
    for c in changes:
        rep.add("  -", c)
    if not changes:
        rep.add("Repo already set up; nothing to do.")
        return rep.finish(True), True
    if args.dry_run:
        return rep.finish(True), True
    if args.commit:
        if not head_exists(root):
            return rep.finish(False, "repository has no commits yet; commit the setup manually"), False
        r = run_git(["add", "--"] + touched, root)
        if not r.ok:
            return rep.finish(False, "git add failed: " + r.err.strip()), False
        r = run_git(["commit", "--only", "-m", "chore(agents): enable shared coding-agent sessions under .claude/sessions", "--"] + touched, root)
        if not r.ok:
            return rep.finish(False, "git commit failed: " + (r.err or r.out).strip()), False
        rep.kv("COMMIT", git_out(["rev-parse", "--short", "HEAD"], root))
        branch = current_branch(root) or "HEAD"
        rep.kv("NEXT", f"git push origin {branch}")
    else:
        rep.kv("NEXT", "review the changes, then: git add " + " ".join(touched) + " && git commit -m 'chore(agents): enable shared coding-agent sessions'")
    return rep.finish(True), True


# --------------------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace) -> Tuple[str, bool]:
    rep = Report()
    rep.kv("TOOL", f"agent-sessions {TOOL_VERSION}")
    rep.kv("SCRIPT", script_path())
    rep.kv("PYTHON", f"{platform.python_version()} ({sys.executable})")
    rep.kv("GIT", git_out(["--version"], os.getcwd()) or "(not found)")
    rep.kv("PLATFORM", platform.platform())
    root = None
    try:
        root = repo_root(resolve_project_dir(args.project_dir))
        rep.kv("REPO_ROOT", root)
    except SessionsError:
        rep.kv("REPO_ROOT", "(not inside a git repo)")
    rep.blank()
    rep.add("Agents:")
    for a in REGISTRY.values():
        status = "installed" if a.installed() else "not found"
        line = f"  - {a.label} ({a.name}): {status}; store {home_to_tilde(a.data_dir())}"
        env_id = a.in_env()
        if env_id:
            line += f"; running inside it (session {env_id[:12]}…)"
        elif env_id == "":
            line += "; running inside it"
        rep.add(line)
        if root and a.installed():
            try:
                cands = a.candidates(root, limit=1)
            except Exception as e:  # noqa: BLE001
                cands = []
                rep.add(f"      could not scan: {type(e).__name__}: {e}")
            if cands:
                c = cands[0]
                rep.add(f"      newest session for this repo: {c.session_id} ({fmt_when(c.updated)})")
    for key, label, p in detected_unsupported():
        rep.add(f"  - {label} ({key}): detected at {p}; pull and init work, push is not supported yet")
    rep.blank()
    skill = os.path.expanduser("~/.agents/skills/agent-sessions/SKILL.md")
    rep.kv("PORTABLE_SKILL", "installed at ~/.agents/skills/agent-sessions" if os.path.exists(skill) else "not installed (scripts/setup.sh --skills)")
    plugins = os.path.join(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"), "plugins", "installed_plugins.json")
    claude_plugin = "unknown"
    try:
        with open(plugins, "r", encoding="utf-8") as fh:
            claude_plugin = "installed" if "sessions@" in fh.read() else "not installed"
    except OSError:
        claude_plugin = "Claude Code not found"
    rep.kv("CLAUDE_PLUGIN", claude_plugin)
    return rep.finish(True), True


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-sessions", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"agent-sessions {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("locate", help="find the live session and report git facts")
    s.add_argument("--agent", default="auto", choices=AGENT_CHOICES)
    s.add_argument("--session-id")
    s.add_argument("--project-dir")
    s.add_argument("--title")
    s.set_defaults(func=cmd_locate)

    s = sub.add_parser("write-summary", help="write summary.md into a session dir from stdin")
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_write_summary)

    s = sub.add_parser("export", help="render + redact a session into a session dir (summary.md must exist)")
    s.add_argument("--out", required=True)
    s.add_argument("--agent", default="auto", choices=AGENT_CHOICES)
    s.add_argument("--session-id")
    s.add_argument("--source", help="SOURCE printed by locate (file path or sqlite ref)")
    s.add_argument("--transcript", help="alias of --source")
    s.add_argument("--title")
    s.add_argument("--include-results", action="store_true", help="keep full tool results (capped at 20 KB each)")
    s.add_argument("--include-thinking", action="store_true")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("commit", help="commit the session dir via a temporary index and push")
    s.add_argument("--dir", required=True)
    s.add_argument("--push", action="store_true")
    s.add_argument("--branch")
    s.add_argument("--remote", default="origin")
    s.set_defaults(func=cmd_commit)

    s = sub.add_parser("list", help="list sessions on origin/<default> and HEAD")
    s.add_argument("--project-dir")
    s.add_argument("--remote", default="origin")
    s.add_argument("--all-branches", action="store_true")
    s.add_argument("--no-fetch", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("init", help="prepare a repo for shared sessions")
    s.add_argument("--project-dir")
    s.add_argument("--marketplace-repo", default="prajwalgajakesari/agent-sessions")
    s.add_argument("--marketplace-name", default="agent-sessions")
    s.add_argument("--plugin-name", default="sessions")
    s.add_argument("--vendor-skill", action="store_true", help="also copy the portable skill into .agents/skills/")
    s.add_argument("--commit", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("doctor", help="show detected agents, stores and installation state")
    s.add_argument("--project-dir")
    s.set_defaults(func=cmd_doctor)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        text, ok = args.func(args)
    except SessionsError as e:
        text, ok = Report().finish(False, str(e)), False
    except Exception as e:  # noqa: BLE001 - never leave the caller without a STATUS line
        text, ok = Report().finish(False, f"unexpected {type(e).__name__}: {e}"), False
    sys.stdout.write(text)
    sys.stdout.flush()
    return 0 if ok else 1


def entry() -> None:
    sys.exit(main())
