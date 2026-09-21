#!/usr/bin/env python3
"""
claude-sessions: push Claude Code sessions into a git repo, pull them back into chat.

Subcommands
  locate   find the live transcript for a session, report git facts, emit an outline
  export   render + redact a transcript into a session dir (summary.md must exist)
  commit   commit the session dir through a temporary index, then push
  list     enumerate sessions on origin/<default> and HEAD without touching the worktree
  init     prepare a repo: .gitignore, README, .gitattributes, .claude/settings.json

Standard library only, Python 3.8+. Every subcommand ends its output with
"STATUS: ok" or "STATUS: error" followed by "REASON: ...". The process exit code
is 0 on ok and 1 on error; scripts/sessions.sh swallows the exit code so a
SKILL.md preflight block never aborts.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

PLUGIN_VERSION = "0.1.0"
TESTED_CLAUDE_VERSION = (2, 1, 259)
SESSIONS_REL = ".claude/sessions"

SUMMARY_MAX_LINES = 120
SUMMARY_MAX_BYTES = 6 * 1024
TRANSCRIPT_MAX_BYTES = 1024 * 1024
RESULT_MAX_LINES = 30
RESULT_MAX_BYTES = 2048
FULL_RESULT_MAX_BYTES = 20 * 1024
INPUT_MAX_BYTES = 2048
OUTLINE_MAX_PROMPTS = 40

GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_OPTIONAL_LOCKS": "0"}
GIT_TIMEOUT = 30
PUSH_TIMEOUT = 60

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
STUB_TOOLS = {"Read", "WebFetch", "WebSearch", "ReadMcpResourceTool", "ReadMcpResourceDirTool"}
AGENT_TOOLS = {"Agent", "Task"}
PUSH_COMMAND_RE = re.compile(r"<command-name>\s*/[\w-]*sessions[\w-]*:push\s*</command-name>")

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE.parent / "templates"


class SessionsError(Exception):
    """Raised for user-facing failures. The message becomes REASON."""


# --------------------------------------------------------------------------- output


class Report:
    """Collects human-readable lines and a final STATUS."""

    def __init__(self) -> None:
        self.lines: List[str] = []

    def add(self, *parts: object) -> None:
        self.lines.append(" ".join(str(p) for p in parts))

    def kv(self, key: str, value: object) -> None:
        self.lines.append(f"{key}: {value}")

    def blank(self) -> None:
        self.lines.append("")

    def finish(self, ok: bool, reason: str = "") -> str:
        out = list(self.lines)
        out.append("STATUS: ok" if ok else "STATUS: error")
        if reason:
            out.append(f"REASON: {reason}")
        return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- git helpers


class GitResult:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err

    @property
    def ok(self) -> bool:
        return self.code == 0


def run_git(args: List[str], cwd: str, timeout: int = GIT_TIMEOUT,
            stdin: Optional[str] = None, env_extra: Optional[Dict[str, str]] = None) -> GitResult:
    env = dict(os.environ)
    env.update(GIT_ENV)
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, env=env, input=stdin)
    except subprocess.TimeoutExpired:
        return GitResult(124, "", f"git {' '.join(args[:2])} timed out after {timeout}s")
    except FileNotFoundError:
        raise SessionsError("git is not on PATH")
    return GitResult(proc.returncode, proc.stdout, proc.stderr)


def git_out(args: List[str], cwd: str, default: str = "") -> str:
    r = run_git(args, cwd)
    return r.out.strip() if r.ok else default


def repo_root(project_dir: str) -> str:
    r = run_git(["rev-parse", "--show-toplevel"], project_dir)
    if not r.ok:
        raise SessionsError(f"{project_dir} is not inside a git repository")
    return os.path.normpath(r.out.strip())


def git_dir(root: str) -> str:
    return os.path.normpath(os.path.join(root, git_out(["rev-parse", "--git-dir"], root, ".git")))


def current_branch(root: str) -> Optional[str]:
    r = run_git(["symbolic-ref", "-q", "--short", "HEAD"], root)
    return r.out.strip() if r.ok and r.out.strip() else None


def head_exists(root: str) -> bool:
    return run_git(["rev-parse", "-q", "--verify", "HEAD"], root).ok


def in_progress_operation(root: str) -> Optional[str]:
    gd = git_dir(root)
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply", "BISECT_LOG"):
        if os.path.exists(os.path.join(gd, marker)):
            return marker
    return None


def upstream_of(root: str, branch: str) -> Optional[str]:
    r = run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{branch}@{{upstream}}"], root)
    return r.out.strip() if r.ok and r.out.strip() else None


def default_remote_branch(root: str, remote: str = "origin") -> Optional[str]:
    r = run_git(["symbolic-ref", "-q", "--short", f"refs/remotes/{remote}/HEAD"], root)
    if r.ok and r.out.strip():
        return r.out.strip().split("/", 1)[1]
    r = run_git(["ls-remote", "--symref", remote, "HEAD"], root, timeout=GIT_TIMEOUT)
    if r.ok:
        m = re.search(r"ref: refs/heads/(\S+)\s+HEAD", r.out)
        if m:
            return m.group(1)
    for cand in ("main", "master"):
        if run_git(["rev-parse", "-q", "--verify", f"refs/remotes/{remote}/{cand}"], root).ok:
            return cand
    return None


def path_is_ignored(root: str, rel: str) -> Optional[str]:
    """Return 'source:line:pattern' if rel is ignored, else None."""
    r = run_git(["check-ignore", "-v", "--no-index", "--", rel], root)
    if r.code == 0 and r.out.strip():
        match = r.out.strip().split("\t")[0]
        # -v also prints the negated pattern that re-included a path; that means NOT ignored
        pattern = match.split(":", 2)[-1]
        if pattern.startswith("!"):
            return None
        return match
    return None


def handle_from_name(name: str) -> str:
    h = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return h or "unknown"


def slugify(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    s = s[:limit].rstrip("-")
    return s or "session"


def home_to_tilde(text: str) -> str:
    home = str(Path.home())
    if home and home != "/" and home in text:
        text = text.replace(home, "~")
    return text


# --------------------------------------------------------------------------- transcript reading


def read_records(path: str) -> Tuple[List[dict], int]:
    """Read JSONL tolerantly. A truncated final line (file is live) is skipped."""
    records: List[dict] = []
    bad = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if isinstance(obj, dict):
                records.append(obj)
    return records, bad


def blocks_of(message: Optional[dict]) -> List[dict]:
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                parts.append(str(b.get("text", "")))
            elif t == "tool_reference":
                parts.append(f"[tool reference: {b.get('tool_name', '?')}]")
            elif t == "image":
                parts.append("[image omitted]")
            else:
                parts.append(f"[unsupported block: {t}]")
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


SYSTEM_NOISE_RE = re.compile(
    r"<system-reminder>[\s\S]*?</system-reminder>|<local-command-caveat>[\s\S]*?</local-command-caveat>"
    r"|<local-command-stdout>[\s\S]*?</local-command-stdout>|<command-message>[\s\S]*?</command-message>",
    re.I,
)
COMMAND_NAME_RE = re.compile(r"<command-name>\s*([^<]*?)\s*</command-name>", re.I)
COMMAND_ARGS_RE = re.compile(r"<command-args>\s*([^<]*?)\s*</command-args>", re.I)


def clean_user_text(text: str) -> Tuple[str, Optional[str]]:
    """Strip harness markup. Returns (text, command) where command is '/name args' if this was a slash command."""
    command = None
    m = COMMAND_NAME_RE.search(text)
    if m:
        name = m.group(1).strip()
        am = COMMAND_ARGS_RE.search(text)
        args = am.group(1).strip() if am else ""
        command = (name + (" " + args if args else "")).strip()
        return "", command
    text = SYSTEM_NOISE_RE.sub("", text)
    return text.strip(), None


def parse_ts(ts: object) -> Optional[dt.datetime]:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def version_tuple(v: object) -> Optional[Tuple[int, ...]]:
    if not isinstance(v, str):
        return None
    try:
        return tuple(int(x) for x in v.split(".")[:3])
    except ValueError:
        return None


class Transcript:
    """Loaded transcript plus derived facts."""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        self.records, self.bad_lines = read_records(self.path)
        self.session_id = self._session_id()
        self.sidecar_dir = os.path.join(os.path.dirname(self.path), self.session_id) if self.session_id else None
        self.by_uuid: Dict[str, dict] = {}
        for rec in self.records:
            u = rec.get("uuid")
            if isinstance(u, str):
                self.by_uuid[u] = rec

    def _session_id(self) -> str:
        base = os.path.basename(self.path)
        if base.endswith(".jsonl"):
            return base[:-6]
        for rec in self.records:
            if isinstance(rec.get("sessionId"), str):
                return rec["sessionId"]
        return ""

    # -- facts

    def title(self) -> Optional[str]:
        title = None
        for rec in self.records:
            if rec.get("type") == "custom-title" and isinstance(rec.get("customTitle"), str):
                title = rec["customTitle"]
        if title:
            return title.strip()
        for rec in self.records:
            if rec.get("type") == "ai-title" and isinstance(rec.get("aiTitle"), str):
                title = rec["aiTitle"]
        return title.strip() if title else None

    def cwd(self) -> Optional[str]:
        for rec in self.records:
            if rec.get("type") in ("user", "assistant") and isinstance(rec.get("cwd"), str):
                return rec["cwd"]
        return None

    def claude_version(self) -> Optional[str]:
        for rec in self.records:
            if isinstance(rec.get("version"), str):
                return rec["version"]
        return None

    def time_range(self) -> Tuple[Optional[dt.datetime], Optional[dt.datetime]]:
        stamps = [parse_ts(r.get("timestamp")) for r in self.records if r.get("type") in ("user", "assistant")]
        stamps = [s for s in stamps if s]
        if not stamps:
            return None, None
        return min(stamps), max(stamps)

    def active_path(self) -> List[dict]:
        """Walk parentUuid from the leaf, crossing compaction boundaries. Chronological order."""
        leaf = None
        for rec in reversed(self.records):
            if rec.get("type") in ("user", "assistant") and not rec.get("isSidechain") and rec.get("uuid") in self.by_uuid:
                leaf = rec
                break
        if leaf is None:
            return [r for r in self.records if r.get("type") in ("user", "assistant", "system") and not r.get("isSidechain")]
        path: List[dict] = []
        seen = set()
        cur: Optional[dict] = leaf
        while cur is not None and cur.get("uuid") not in seen:
            seen.add(cur.get("uuid"))
            path.append(cur)
            parent = cur.get("parentUuid")
            if parent is None and cur.get("type") == "system" and cur.get("subtype") == "compact_boundary":
                parent = cur.get("logicalParentUuid")
            cur = self.by_uuid.get(parent) if isinstance(parent, str) else None
        path.reverse()
        return path

    # -- outline helpers

    def user_prompts(self, path: Optional[List[dict]] = None, limit: int = 200) -> List[Tuple[Optional[dt.datetime], str]]:
        out = []
        for rec in path if path is not None else self.active_path():
            if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isCompactSummary"):
                continue
            for b in blocks_of(rec.get("message")):
                if b.get("type") != "text":
                    continue
                text, command = clean_user_text(str(b.get("text", "")))
                if command:
                    out.append((parse_ts(rec.get("timestamp")), f"/{command.lstrip('/')}"))
                elif text:
                    one = re.sub(r"\s+", " ", text)[:limit]
                    out.append((parse_ts(rec.get("timestamp")), one))
        return out

    def agent_tasks(self, path: Optional[List[dict]] = None) -> List[str]:
        out = []
        for rec in path if path is not None else self.active_path():
            if rec.get("type") != "assistant":
                continue
            for b in blocks_of(rec.get("message")):
                if b.get("type") == "tool_use" and b.get("name") in AGENT_TOOLS:
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    desc = inp.get("description") or inp.get("prompt") or ""
                    out.append(re.sub(r"\s+", " ", str(desc))[:120])
        return out

    def subagent_files(self) -> List[str]:
        if not self.sidecar_dir:
            return []
        return sorted(glob.glob(os.path.join(self.sidecar_dir, "subagents", "*.jsonl")))

    def files_touched(self, root: Optional[str]) -> List[str]:
        found: "OrderedDict[str, None]" = OrderedDict()

        def add(p: object) -> None:
            if not isinstance(p, str) or not p:
                return
            p = os.path.normpath(p)
            if root:
                try:
                    rel = os.path.relpath(p, root)
                    if not rel.startswith(".."):
                        p = rel.replace(os.sep, "/")
                except ValueError:
                    pass
            found[home_to_tilde(p)] = None

        def scan(records: Iterable[dict]) -> None:
            for rec in records:
                t = rec.get("type")
                if t == "assistant":
                    for b in blocks_of(rec.get("message")):
                        if b.get("type") == "tool_use" and b.get("name") in EDIT_TOOLS:
                            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                            add(inp.get("file_path") or inp.get("notebook_path"))
                elif t == "user":
                    tur = rec.get("toolUseResult")
                    if isinstance(tur, dict) and isinstance(tur.get("filePath"), str) and (
                        "structuredPatch" in tur or tur.get("type") in ("create", "update")
                    ):
                        add(tur.get("filePath"))

        scan(self.records)
        for sub in self.subagent_files():
            recs, _ = read_records(sub)
            scan(recs)
        return list(found.keys())

    def links(self) -> List[str]:
        out = []
        for rec in self.records:
            if rec.get("type") == "pr-link":
                for v in rec.values():
                    if isinstance(v, str) and v.startswith("http"):
                        out.append(v)
        return out


# --------------------------------------------------------------------------- redaction


class Pattern:
    def __init__(self, name: str, regex: str, level: str, group: Optional[int] = None,
                 context: Optional[str] = None, flags: int = 0) -> None:
        self.name = name
        self.regex = re.compile(regex, flags)
        self.level = level
        self.group = group
        self.context = re.compile(context, re.I) if context else None


PATTERNS: List[Pattern] = [
    # high confidence, structured formats. Whole match replaced unless group given.
    Pattern("aws-access-key", r"\bAKIA[0-9A-Z]{16}\b", "high"),
    Pattern("github-token", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,255}\b|\bgithub_pat_[A-Za-z0-9_]{22,255}\b", "high"),
    Pattern("slack-token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b", "high"),
    Pattern("sk-api-key", r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b", "high"),
    Pattern("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b", "high"),
    Pattern("private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*", "high"),
    Pattern("url-userinfo", r"(?<=://)[^/\s:@]{1,64}:[^/\s@]{1,256}(?=@)", "high"),
    Pattern("entra-client-secret", r"\b[A-Za-z0-9_~.-]{3}8Q~[A-Za-z0-9_~.-]{30,40}\b", "high"),
    Pattern("azure-account-key", r"(AccountKey=)([A-Za-z0-9+/]{80,90}={0,2})", "high", group=2, flags=re.I),
    Pattern("shared-access-key", r"(SharedAccessKey=|SharedAccessSignature=)([^;\s\"']{8,})", "high", group=2, flags=re.I),
    Pattern("sas-signature", r"([?&;]sig=)([A-Za-z0-9%+/=_-]{20,})", "high", group=2, flags=re.I),
    Pattern("azure-devops-pat", r"\b(AZURE_DEVOPS_EXT_PAT|System\.AccessToken|SYSTEM_ACCESSTOKEN)(\s*[:=]\s*[\"']?)([A-Za-z0-9]{20,})", "high", group=3, flags=re.I),
    Pattern("oauth-basic-token", r"([A-Za-z0-9_-]{20,})(:x-oauth-basic)", "high", group=1),
    Pattern("bearer-token", r"\b(Bearer\s+)([A-Za-z0-9._~+/=-]{16,})", "high", group=2),
    # review level, keyword based. Value only.
    Pattern("password-kv", r"\b(password|passwd|pwd)(\s*[:=]\s*[\"']?)([^\s\"';&,]{8,})", "review", group=3, flags=re.I),
    Pattern("secret-kv",
            r"\b(client[_-]?secret|azure[_-]?client[_-]?secret|api[_-]?key|apikey|secret[_-]?key|access[_-]?token"
            r"|auth[_-]?token|refresh[_-]?token|token)(\s*[:=]\s*[\"']?)([^\s\"';&,]{8,})", "review", group=3, flags=re.I),
    Pattern("cli-secret-flag", r"(--client-secret|--password|--pat|--token)(\s+|=)([\"']?)([^\s\"']{8,})", "review", group=4),
    Pattern("sqlcmd-password", r"(\s-P\s*)([\"']?)([^\s\"']{6,})", "review", group=3, context=r"\b(sqlcmd|bcp|mssql-cli)\b"),
    Pattern("az-login-password", r"(\s-p\s+)([\"']?)([^\s\"']{8,})", "review", group=3, context=r"--service-principal|az\s+login"),
]

PEM_BLOCK_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")
PLACEHOLDER_RE = re.compile(r"^(<|\$\{|\$\(|\{\{|\*\*\*|your[-_]|xxx|\[REDACTED|example|changeme|placeholder|redacted)", re.I)
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SENSITIVE_FILE_RE = re.compile(
    r"(^|[\\/])(\.env(\.[^\\/]*)?|local\.settings\.json|appsettings(\.[^\\/]*)?\.json|secrets?\.json|\.netrc|\.pypirc"
    r"|\.databrickscfg|id_(rsa|dsa|ecdsa|ed25519)|credentials|\.git-credentials)$"
    r"|\.(pfx|pem|key|p12|publishsettings|tfvars|tfstate|kdbx)$|[\\/]\.azure[\\/]",
    re.I,
)


def is_placeholder(value: str) -> bool:
    v = value.strip("\"'")
    return bool(PLACEHOLDER_RE.match(v)) or bool(GUID_RE.match(v)) or "os.environ" in v or "getenv" in v


class Redactor:
    def __init__(self) -> None:
        self.hits: Counter = Counter()
        self.review: List[Tuple[str, int, str]] = []  # (file, line, pattern)

    def redact(self, text: str, label: str = "") -> str:
        text = PEM_BLOCK_RE.sub(lambda m: self._count("private-key", "high", label, 0) or "[REDACTED:private-key]", text)
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if not line:
                continue
            for p in PATTERNS:
                if p.context and not p.context.search(line):
                    continue
                line = p.regex.sub(lambda m, p=p, n=i + 1: self._replace(m, p, label, n), line)
            lines[i] = line
        return "\n".join(lines)

    def _count(self, name: str, level: str, label: str, line: int) -> None:
        self.hits[name] += 1
        if level == "review":
            self.review.append((label, line, name))

    def _replace(self, m: "re.Match[str]", p: Pattern, label: str, line: int) -> str:
        whole = m.group(0)
        if p.group:
            value = m.group(p.group)
            if value is None or is_placeholder(value):
                return whole
            self._count(p.name, p.level, label, line)
            start = m.start(p.group) - m.start(0)
            end = m.end(p.group) - m.start(0)
            return whole[:start] + f"[REDACTED:{p.name}]" + whole[end:]
        if is_placeholder(whole):
            return whole
        self._count(p.name, p.level, label, line)
        return f"[REDACTED:{p.name}]"

    @staticmethod
    def residual_high(text: str) -> List[str]:
        found = []
        if PEM_BLOCK_RE.search(text):
            found.append("private-key")
        for p in PATTERNS:
            if p.level != "high":
                continue
            for m in p.regex.finditer(text):
                value = m.group(p.group) if p.group else m.group(0)
                if value and not is_placeholder(value) and "[REDACTED:" not in m.group(0):
                    found.append(p.name)
                    break
        return found


def redact_value(value: object, redactor: "Redactor") -> object:
    """Redact every string inside a JSON-like value, preserving structure."""
    if isinstance(value, str):
        return redactor.redact(value, "transcript")
    if isinstance(value, dict):
        return {k: redact_value(v, redactor) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v, redactor) for v in value]
    return value


def mentions_sensitive_file(text: str) -> Optional[str]:
    for tok in re.split(r"[\s\"'`;|&<>()]+", text):
        tok = tok.strip()
        if tok and SENSITIVE_FILE_RE.search(tok):
            return tok
    return None


# --------------------------------------------------------------------------- rendering


def fence(text: str, lang: str = "") -> str:
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{ticks}{lang}\n{text.rstrip()}\n{ticks}"


def truncate(text: str, max_lines: int, max_bytes: int) -> Tuple[str, int]:
    lines = text.split("\n")
    omitted = 0
    if len(lines) > max_lines:
        omitted = len(lines) - max_lines
        lines = lines[:max_lines]
    out = "\n".join(lines)
    if len(out.encode("utf-8", "replace")) > max_bytes:
        out = out.encode("utf-8", "replace")[:max_bytes].decode("utf-8", "ignore")
        omitted = max(omitted, 1)
    return out, omitted


PERSISTED_PATH_RE = re.compile(r"(Full output saved to:\s*)(\S+)")


def scrub_persisted(text: str) -> str:
    return PERSISTED_PATH_RE.sub(r"\1[local path]", text)


def tool_label(name: str, inp: dict) -> str:
    if name == "Bash":
        desc = str(inp.get("description") or "").strip()
        cmd = str(inp.get("command") or "").strip().split("\n")[0]
        return f"Bash: {desc or cmd[:80]}"
    if name in ("Read",):
        return f"Read: {home_to_tilde(str(inp.get('file_path', '')))}"
    if name in EDIT_TOOLS:
        return f"{name}: {home_to_tilde(str(inp.get('file_path') or inp.get('notebook_path') or ''))}"
    if name in AGENT_TOOLS:
        return f"Agent: {str(inp.get('description') or '')[:80]}"
    if name.startswith("mcp__"):
        parts = name.split("__")
        return f"MCP {parts[1] if len(parts) > 1 else '?'}: {parts[-1]}"
    if name in ("Grep", "Glob"):
        return f"{name}: {str(inp.get('pattern', ''))[:80]}"
    if name in ("WebFetch", "WebSearch"):
        return f"{name}: {str(inp.get('url') or inp.get('query') or '')[:80]}"
    return name


def render_input(name: str, inp: dict, withheld: bool) -> str:
    if withheld:
        return "_input withheld: references a sensitive file_"
    if name == "Bash":
        cmd = str(inp.get("command") or "")
        cmd, _ = truncate(cmd, 40, INPUT_MAX_BYTES)
        return fence(cmd, "bash")
    if name in ("Write",):
        content = str(inp.get("content") or "")
        snippet, omitted = truncate(content, 20, INPUT_MAX_BYTES)
        tail = f"\n_… {omitted} more lines_" if omitted else ""
        return fence(snippet) + tail
    if name in ("Edit", "MultiEdit"):
        parts = []
        edits = inp.get("edits") if isinstance(inp.get("edits"), list) else [inp]
        for e in edits[:5]:
            if not isinstance(e, dict):
                continue
            old, _ = truncate(str(e.get("old_string") or ""), 12, 1024)
            new, _ = truncate(str(e.get("new_string") or ""), 12, 1024)
            parts.append("- old:\n" + fence(old) + "\n- new:\n" + fence(new))
        return "\n".join(parts) if parts else "_no edit payload_"
    if name in AGENT_TOOLS:
        prompt = str(inp.get("prompt") or "")
        prompt, omitted = truncate(prompt, 15, INPUT_MAX_BYTES)
        return fence(prompt) + (f"\n_… {omitted} more lines_" if omitted else "")
    try:
        dumped = json.dumps(inp, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        dumped = str(inp)
    dumped, omitted = truncate(dumped, 30, INPUT_MAX_BYTES)
    return fence(dumped, "json") + (f"\n_… truncated_" if omitted else "")


def render_result(name: str, text: str, is_error: bool, mode: str, withheld: Optional[str]) -> str:
    """mode: 'policy' (default), 'stub' (everything stubbed), 'full' (opt-in)."""
    if withheld:
        return f"_result withheld: reads a sensitive file ({withheld})_"
    text = scrub_persisted(text)
    nlines = text.count("\n") + (1 if text else 0)
    nbytes = len(text.encode("utf-8", "replace"))
    prefix = "**Error.** " if is_error else ""
    if not text.strip():
        return prefix + "_no output_"
    stub = name.startswith("mcp__") or name in STUB_TOOLS
    if mode == "stub" or (mode == "policy" and stub):
        return f"{prefix}_result omitted: {nlines} lines, {nbytes} bytes_"
    if mode == "full":
        body, omitted = truncate(text, 10_000, FULL_RESULT_MAX_BYTES)
    else:
        body, omitted = truncate(text, RESULT_MAX_LINES, RESULT_MAX_BYTES)
    out = prefix + fence(body)
    if omitted:
        out += f"\n_… {omitted} more lines omitted ({nlines} lines, {nbytes} bytes total)_"
    return out


def render_transcript(tr: Transcript, redactor: Redactor, result_mode: str = "policy",
                      include_thinking: bool = False, title: str = "") -> Tuple[List[str], dict]:
    """Return (segments, stats). Segments are markdown chunks that can be split across files."""
    path = tr.active_path()
    # pre-pass: tool results by tool_use_id
    results: Dict[str, Tuple[str, bool]] = {}
    for rec in path:
        if rec.get("type") != "user":
            continue
        for b in blocks_of(rec.get("message")):
            if b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str):
                results[b["tool_use_id"]] = (result_text(b.get("content")), bool(b.get("is_error")))

    segments: List[str] = []
    stats = Counter()
    last_role = None
    last_date = None
    stopped = False
    header = [f"# {title or tr.title() or 'Claude Code session'}", ""]
    header.append(f"_Session `{tr.session_id}` · rendered by claude-sessions {PLUGIN_VERSION} · "
                  f"tool results {'included' if result_mode == 'full' else 'truncated or omitted'}; thinking {'included' if include_thinking else 'omitted'}._")
    header.append("")
    segments.append("\n".join(header))

    def emit(text: str) -> None:
        segments.append(text.rstrip() + "\n")

    def role_header(role: str, ts: Optional[dt.datetime]) -> str:
        nonlocal last_role, last_date
        out = []
        if ts and ts.date() != last_date:
            last_date = ts.date()
            out.append(f"\n## {last_date.isoformat()} (UTC)\n")
            last_role = None
        if role != last_role:
            last_role = role
            stamp = f" · {ts.strftime('%H:%M')}" if ts else ""
            out.append(f"\n### {role}{stamp}\n")
        return "\n".join(out)

    for rec in path:
        rtype = rec.get("type")
        ts = parse_ts(rec.get("timestamp"))
        if rtype == "system":
            if rec.get("subtype") == "compact_boundary":
                meta = rec.get("compactMetadata") if isinstance(rec.get("compactMetadata"), dict) else {}
                pre, post = meta.get("preTokens"), meta.get("postTokens")
                detail = f" ({pre} → {post} tokens)" if pre and post else ""
                emit(f"\n> **Context compacted here**{detail}. Everything above was summarised for the model.\n")
                last_role = None
                stats["compactions"] += 1
            continue
        if rtype == "user":
            if rec.get("isMeta") or rec.get("isSidechain"):
                stats["skipped_meta"] += 1
                continue
            if rec.get("isCompactSummary"):
                body = redactor.redact(result_text(rec.get("message", {}).get("content")), "transcript")
                body, _ = truncate(body, 400, 16 * 1024)
                emit("<details>\n<summary>Compaction summary given to the model</summary>\n\n" + body + "\n\n</details>\n")
                stats["compaction_summaries"] += 1
                continue
            for b in blocks_of(rec.get("message")):
                btype = b.get("type")
                if btype == "text":
                    raw = str(b.get("text", ""))
                    if PUSH_COMMAND_RE.search(raw):
                        stopped = True
                        break
                    text, command = clean_user_text(raw)
                    if command:
                        emit(role_header("User", ts) + f"\n> Ran `/{command.lstrip('/')}`\n")
                        stats["commands"] += 1
                    elif text:
                        emit(role_header("User", ts) + "\n" + redactor.redact(home_to_tilde(text), "transcript") + "\n")
                        stats["user_messages"] += 1
                elif btype == "tool_result":
                    continue  # rendered with its tool_use
                elif btype == "image":
                    emit(role_header("User", ts) + "\n_[image omitted]_\n")
                else:
                    emit(f"_[unsupported block: {btype}]_")
            if stopped:
                break
            continue
        if rtype == "assistant":
            if rec.get("isSidechain"):
                continue
            if rec.get("isApiErrorMessage"):
                emit(f"\n> _API error message omitted_\n")
                continue
            for b in blocks_of(rec.get("message")):
                btype = b.get("type")
                if btype == "text":
                    text = str(b.get("text", "")).strip()
                    if text:
                        emit(role_header("Assistant", ts) + "\n" + redactor.redact(home_to_tilde(text), "transcript") + "\n")
                        stats["assistant_messages"] += 1
                elif btype == "thinking":
                    if include_thinking:
                        think = redactor.redact(home_to_tilde(str(b.get("thinking", ""))), "transcript")
                        think, _ = truncate(think, 80, 6 * 1024)
                        emit(role_header("Assistant", ts) + "\n<details>\n<summary>Thinking</summary>\n\n" + think + "\n\n</details>\n")
                    stats["thinking_blocks"] += 1
                elif btype == "tool_use":
                    name = str(b.get("name") or "tool")
                    raw_inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    tool_id = b.get("id") if isinstance(b.get("id"), str) else None
                    probe = " ".join(str(v) for v in raw_inp.values() if isinstance(v, str))
                    withheld = mentions_sensitive_file(probe) if name in ({"Bash", "Read", "Grep", "Glob"} | EDIT_TOOLS) else None
                    # redact the full input once, before any label truncation can split a secret
                    inp = redact_value(raw_inp, redactor)
                    label = home_to_tilde(tool_label(name, inp))
                    body = [f"<details>\n<summary>🔧 {label}</summary>\n"]
                    body.append(home_to_tilde(render_input(name, inp, bool(withheld))))
                    if tool_id and tool_id in results:
                        text, is_error = results[tool_id]
                        text = redactor.redact(home_to_tilde(text), "transcript")
                        body.append("\n**Result**\n\n" + redactor.redact(render_result(name, text, is_error, result_mode, withheld), "transcript"))
                    else:
                        body.append("\n_no result recorded_")
                    body.append("\n</details>\n")
                    emit(role_header("Assistant", ts) + "\n" + "\n".join(body))
                    stats["tool_calls"] += 1
                    if withheld:
                        stats["withheld_results"] += 1
                else:
                    emit(f"_[unsupported block: {btype}]_")
            continue
        stats[f"ignored:{rtype}"] += 1

    stats["messages"] = stats["user_messages"] + stats["assistant_messages"]
    stats["stopped_at_push"] = 1 if stopped else 0
    return segments, dict(stats)


# --------------------------------------------------------------------------- frontmatter


def parse_frontmatter(text: str) -> Tuple["OrderedDict[str, object]", str]:
    """Minimal YAML subset: key: value, key: [a, b], key:\n  - a."""
    fm: "OrderedDict[str, object]" = OrderedDict()
    if not text.startswith("---"):
        return fm, text
    lines = text.split("\n")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return fm, text
    key = None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m:
            key, raw = m.group(1), m.group(2).strip()
            if raw == "":
                fm[key] = []
            elif raw.startswith("[") and raw.endswith("]"):
                fm[key] = [x.strip().strip("\"'") for x in raw[1:-1].split(",") if x.strip()]
            else:
                fm[key] = raw.strip("\"'")
        elif key and line.lstrip().startswith("- ") and isinstance(fm.get(key), list):
            fm[key].append(line.lstrip()[2:].strip().strip("\"'"))  # type: ignore[union-attr]
    body = "\n".join(lines[end + 1:])
    return fm, body


def dump_frontmatter(fm: "OrderedDict[str, object]") -> str:
    out = ["---"]
    for k, v in fm.items():
        if isinstance(v, list):
            out.append(f"{k}: [{', '.join(str(x) for x in v)}]")
        else:
            s = str(v).replace('"', "'")
            out.append(f'{k}: "{s}"')
    out.append("---")
    return "\n".join(out)


# --------------------------------------------------------------------------- subcommands


def resolve_session_id(explicit: Optional[str]) -> Optional[str]:
    for cand in (explicit, os.environ.get("CLAUDE_CODE_SESSION_ID"), os.environ.get("CLAUDE_SESSION_ID")):
        if cand and cand.strip() and not cand.startswith("${"):
            return cand.strip()
    return None


def resolve_project_dir(explicit: Optional[str]) -> str:
    for cand in (explicit, os.environ.get("CLAUDE_PROJECT_DIR")):
        if cand and cand.strip() and not cand.startswith("${"):
            return os.path.abspath(cand.strip())
    return os.getcwd()


def config_dir() -> str:
    return os.path.abspath(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))


def find_transcript(session_id: Optional[str], root: str) -> Tuple[Optional[str], str]:
    """Return (path, how)."""
    projects = os.path.join(config_dir(), "projects")
    if session_id:
        hits = glob.glob(os.path.join(projects, "*", f"{session_id}.jsonl"))
        if hits:
            hits.sort(key=os.path.getmtime, reverse=True)
            return hits[0], "session id"
    # fallback: newest transcript whose cwd matches the repo
    real_root = os.path.realpath(root)
    candidates = sorted(glob.glob(os.path.join(projects, "*", "*.jsonl")), key=os.path.getmtime, reverse=True)[:60]
    for cand in candidates:
        try:
            with open(cand, "r", encoding="utf-8", errors="replace") as fh:
                for _ in range(40):
                    line = fh.readline()
                    if not line:
                        break
                    if '"cwd"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = obj.get("cwd")
                    if isinstance(cwd, str) and os.path.realpath(cwd).startswith(real_root):
                        return cand, "newest transcript for this repo (session id unavailable)"
                    break
        except OSError:
            continue
    return None, "not found"


def session_dir_name(started: Optional[dt.datetime], title: str, handle: str, session_id: str) -> str:
    date = (started or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d")
    return f"{date}_{slugify(title)}_{handle}_{session_id[:8]}"


def existing_session_dir(root: str, session_id: str) -> Optional[str]:
    base = os.path.join(root, SESSIONS_REL)
    for cand in glob.glob(os.path.join(base, f"*_{session_id[:8]}")):
        meta = os.path.join(cand, "meta.json")
        try:
            with open(meta, "r", encoding="utf-8") as fh:
                if json.load(fh).get("session_id") == session_id:
                    return cand
        except (OSError, ValueError):
            if os.path.isdir(cand):
                return cand
    return None


def cmd_locate(args: argparse.Namespace) -> Tuple[str, bool]:
    rep = Report()
    project_dir = resolve_project_dir(args.project_dir)
    root = repo_root(project_dir)
    session_id = resolve_session_id(args.session_id)
    path, how = find_transcript(session_id, root)
    if not path:
        rep.kv("REPO_ROOT", root)
        rep.kv("SESSION_ID", session_id or "(unknown)")
        return rep.finish(False, "transcript not found under " + os.path.join(config_dir(), "projects")
                          + ". Session persistence may be disabled, or the session id is not available."), False
    tr = Transcript(path)
    started, ended = tr.time_range()
    title = args.title or tr.title() or "untitled session"
    name = git_out(["config", "user.name"], root) or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    email = git_out(["config", "user.email"], root)
    handle = handle_from_name(name)
    branch = current_branch(root)
    existing = existing_session_dir(root, tr.session_id)
    dir_name = os.path.basename(existing) if existing else session_dir_name(started, title, handle, tr.session_id)

    rep.kv("REPO_ROOT", root)
    rep.kv("TRANSCRIPT", tr.path)
    rep.kv("FOUND_BY", how)
    rep.kv("SESSION_ID", tr.session_id)
    rep.kv("TITLE_DEFAULT", title)
    rep.kv("AUTHOR", f"{name} <{email}>" if email else name)
    rep.kv("HANDLE", handle)
    rep.kv("BRANCH", branch or "(detached HEAD)")
    rep.kv("SESSION_DIR", os.path.join(root, SESSIONS_REL, dir_name))
    rep.kv("REPUSH", "yes" if existing else "no")
    rep.kv("STARTED", started.isoformat() if started else "?")
    rep.kv("RECORDS", f"{len(tr.records)} ({tr.bad_lines} unparsable lines skipped)")
    rep.kv("SUBAGENT_FILES", len(tr.subagent_files()))

    warnings = []
    cwd = tr.cwd()
    if cwd and not os.path.realpath(cwd).startswith(os.path.realpath(root)):
        warnings.append(f"transcript cwd {home_to_tilde(cwd)} is outside this repo")
    ignored = path_is_ignored(root, f"{SESSIONS_REL}/probe.md")
    if ignored:
        warnings.append(f"{SESSIONS_REL}/ is gitignored by {ignored}. Run /sessions:init in this repo first.")
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
    v = version_tuple(tr.claude_version())
    if v and v > TESTED_CLAUDE_VERSION:
        warnings.append(f"transcript written by Claude Code {tr.claude_version()}, newer than the tested {'.'.join(map(str, TESTED_CLAUDE_VERSION))}")
    rep.kv("WARNINGS", len(warnings))
    for w in warnings:
        rep.add("  -", w)

    path_recs = tr.active_path()
    prompts = tr.user_prompts(path_recs)
    files = tr.files_touched(root)
    tasks = tr.agent_tasks(path_recs)
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
    links = tr.links()
    if links:
        rep.add("Links:")
        for l in links[:10]:
            rep.add("  -", l)
    blocking = ignored is not None or not head_exists(root)
    return rep.finish(not blocking, "preflight blocked: see WARNINGS" if blocking else ""), not blocking


def cmd_export(args: argparse.Namespace) -> Tuple[str, bool]:
    rep = Report()
    out_dir = os.path.abspath(args.out)
    root = repo_root(os.path.dirname(out_dir) if os.path.isdir(os.path.dirname(out_dir)) else os.getcwd())
    tr = Transcript(args.transcript)
    if not tr.records:
        return rep.finish(False, f"no records could be read from {args.transcript}"), False
    summary_path = os.path.join(out_dir, "summary.md")
    if not os.path.exists(summary_path):
        return rep.finish(False, f"summary.md not found in {out_dir}; write it first"), False
    with open(summary_path, "r", encoding="utf-8", errors="replace") as fh:
        summary_raw = fh.read()
    fm, body = parse_frontmatter(summary_raw)

    started, ended = tr.time_range()
    name = git_out(["config", "user.name"], root) or "unknown"
    handle = handle_from_name(name)
    branch = current_branch(root) or "(detached)"
    title = str(args.title or fm.get("title") or tr.title() or "untitled session").strip()
    if title.startswith("<") or not title:
        title = tr.title() or "untitled session"
    tags = fm.get("tags") if isinstance(fm.get("tags"), list) else []
    tags = [slugify(str(t), 30) for t in tags if str(t).strip() and not str(t).startswith("<")]
    outcome = str(fm.get("outcome") or "").strip()
    if outcome.startswith("<"):
        outcome = ""

    # authoritative frontmatter fields
    fm["title"] = title
    fm["session_id"] = tr.session_id
    fm["author"] = name
    fm["handle"] = handle
    fm["date"] = (started or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d")
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
    segments, stats = render_transcript(tr, redactor, mode, args.include_thinking, title)
    transcript_text = "\n".join(segments)
    if len(transcript_text.encode("utf-8")) > TRANSCRIPT_MAX_BYTES and mode != "stub":
        redactor2 = Redactor()
        segments, stats = render_transcript(tr, redactor2, "stub", args.include_thinking, title)
        redactor.hits.update(redactor2.hits)
        redactor.review.extend(redactor2.review)
        transcript_text = "\n".join(segments)
        stats["degraded_to_stubs"] = 1
    if stats.get("messages", 0) == 0:
        return rep.finish(False, "zero messages rendered; transcript format may have changed"), False

    # split if still too large
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

    files_touched = tr.files_touched(root)
    meta = OrderedDict([
        ("session_id", tr.session_id),
        ("title", title),
        ("slug", slugify(title)),
        ("dir", os.path.basename(out_dir)),
        ("author", name),
        ("handle", handle),
        ("started", started.isoformat() if started else None),
        ("ended", ended.isoformat() if ended else None),
        ("branch", branch),
        ("files_touched", files_touched),
        ("tags", tags),
        ("outcome", outcome or "(not stated)"),
        ("pushed_at", dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()),
        ("plugin_version", PLUGIN_VERSION),
        ("claude_code_version", tr.claude_version()),
        ("transcript_files", [os.path.basename(f) for f in files_written]),
        ("stats", {k: v for k, v in stats.items() if not k.startswith("ignored:")}),
    ])
    meta_text = redactor.redact(json.dumps(meta, indent=2, ensure_ascii=False), "meta.json") + "\n"
    write_text(summary_path, summary_text)
    write_text(os.path.join(out_dir, "meta.json"), meta_text)

    # residual scan is the safety net
    residual = []
    for p in files_written + [summary_path, os.path.join(out_dir, "meta.json")]:
        with open(p, "r", encoding="utf-8") as fh:
            for pat in Redactor.residual_high(fh.read()):
                residual.append(f"{os.path.basename(p)}:{pat}")
    high = sum(v for k, v in redactor.hits.items() if any(p.name == k and p.level == "high" for p in PATTERNS) or k == "private-key")
    review = sum(v for k, v in redactor.hits.items() if any(p.name == k and p.level == "review" for p in PATTERNS))

    rep.kv("SESSION_DIR", out_dir)
    rep.kv("TITLE", title)
    rep.kv("BRANCH", branch)
    rep.kv("FILES", ", ".join(os.path.basename(f) for f in files_written) + ", summary.md, meta.json")
    rep.kv("TRANSCRIPT_BYTES", sum(os.path.getsize(f) for f in files_written))
    rep.kv("SUMMARY_LINES", s_lines)
    rep.kv("MESSAGES", f"{stats.get('user_messages', 0)} user / {stats.get('assistant_messages', 0)} assistant / {stats.get('tool_calls', 0)} tool calls")
    rep.kv("FILES_TOUCHED", len(files_touched))
    rep.kv("COMPACTIONS", stats.get("compactions", 0))
    rep.kv("WITHHELD_RESULTS", stats.get("withheld_results", 0))
    rep.kv("RESULT_MODE", "full (opt-in)" if mode == "full" else ("stubs (size budget)" if stats.get("degraded_to_stubs") else "policy: MCP and Read stubbed, others truncated"))
    rep.kv("REDACTED_HIGH", high)
    rep.kv("REDACTED_REVIEW", review)
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


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")


def cmd_commit(args: argparse.Namespace) -> Tuple[str, bool]:
    rep = Report()
    session_dir = os.path.abspath(args.dir)
    if not os.path.isdir(session_dir):
        return rep.finish(False, f"{session_dir} does not exist"), False
    root = repo_root(session_dir)
    rel = os.path.relpath(session_dir, root).replace(os.sep, "/")
    if not rel.startswith(SESSIONS_REL + "/"):
        return rep.finish(False, f"{rel} is not under {SESSIONS_REL}/"), False
    meta_path = os.path.join(session_dir, "meta.json")
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return rep.finish(False, "meta.json missing or invalid; run export first"), False
    title = str(meta.get("title") or "session")
    session_id = str(meta.get("session_id") or "")
    if not head_exists(root):
        return rep.finish(False, "repository has no commits yet"), False
    head = git_out(["rev-parse", "HEAD"], root)
    branch = current_branch(root)
    target_branch = args.branch
    if not branch and not target_branch:
        return rep.finish(False, "detached HEAD; pass --branch <name>"), False
    is_update = run_git(["cat-file", "-e", f"HEAD:{rel}/meta.json"], root).ok

    tmp = tempfile.NamedTemporaryFile(prefix="sessions-index-", delete=False)
    tmp.close()
    try:
        env = {"GIT_INDEX_FILE": tmp.name}
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
            os.remove(tmp.name)
        except OSError:
            pass
    if tree == git_out(["rev-parse", "HEAD^{tree}"], root):
        return rep.finish(False, "nothing to commit: session files are identical to HEAD"), False

    verb = "update" if is_update else "add"
    message = f"docs(sessions): {verb} {title}\n\nClaude-Session: {session_id}\nClaude-Session-Path: {rel}\n"
    r = run_git(["commit-tree", tree, "-p", head, "-F", "-"], root, stdin=message)
    if not r.ok:
        return rep.finish(False, "commit-tree failed: " + r.err.strip()), False
    commit = r.out.strip()

    pushed_ref = None
    if target_branch:
        pushed_ref = target_branch
        run_git(["update-ref", f"refs/heads/{target_branch}", commit], root)
    else:
        r = run_git(["update-ref", "-m", "sessions: push", f"refs/heads/{branch}", commit, head], root)
        if not r.ok:
            return rep.finish(False, "update-ref failed (HEAD moved during commit?): " + r.err.strip()), False
        # keep the user's real index in sync for these paths so status stays clean
        run_git(["add", "-f", "--", rel], root)
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
    return rep.finish(False, "commit created locally but push failed; run the MANUAL command in your terminal"), False


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

    sessions: Dict[str, dict] = {}
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
        sid = str(m.get("session_id") or "")[:8]
        date = str(m.get("started") or m.get("pushed_at") or "")[:10]
        tags = ",".join(m.get("tags") or []) if isinstance(m.get("tags"), list) else ""
        rep.add(f"- {sid} | {date} | {m.get('handle', '?')} | {m.get('branch', '?')} | {m.get('title', '?')} | {m.get('outcome', '')}"
                f"{' | #' + tags if tags else ''} | {e['ref']}:{e['path']}")
    if args.json:
        rep.blank()
        rep.add("JSON: " + json.dumps([{"ref": e["ref"], "path": e["path"], **{k: e["meta"].get(k) for k in ("session_id", "title", "handle", "branch", "started", "pushed_at", "outcome", "tags")}} for e in ordered]))
    return rep.finish(True), True


def load_template(name: str) -> str:
    p = TEMPLATES / name
    with open(p, "r", encoding="utf-8") as fh:
        return fh.read()


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
        text = "\n".join(new_lines)
        if not args.dry_run:
            write_text(gitignore, text)
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
    note = f"claude-sessions: {SESSIONS_REL}/ holds shared session handoffs. Push with /{plugin_name}:push, read with /{plugin_name}:pull."
    comment = settings.get("_comment")
    if isinstance(comment, list):
        if note not in comment:
            comment.append(note)
            changes.append("settings.json: added _comment entry")
    elif comment is None:
        settings["_comment"] = [note]
        changes.append("settings.json: added _comment entry")
    if any(c.startswith("settings.json") for c in changes):
        if not args.dry_run:
            write_text(settings_path, json.dumps(settings, indent=2, ensure_ascii=False))
        touched.append(".claude/settings.json")

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
        r = run_git(["commit", "--only", "-m", "chore(claude): enable shared Claude Code sessions under .claude/sessions", "--"] + touched, root)
        if not r.ok:
            return rep.finish(False, "git commit failed: " + (r.err or r.out).strip()), False
        rep.kv("COMMIT", git_out(["rev-parse", "--short", "HEAD"], root))
        branch = current_branch(root) or "HEAD"
        rep.kv("NEXT", f"git push origin {branch}")
    else:
        rep.kv("NEXT", "review the changes, then: git add " + " ".join(touched) + " && git commit -m 'chore(claude): enable shared Claude Code sessions'")
    return rep.finish(True), True


# --------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sessions", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"claude-sessions {PLUGIN_VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("locate", help="find the live transcript and report git facts")
    s.add_argument("--session-id")
    s.add_argument("--project-dir")
    s.add_argument("--title")
    s.set_defaults(func=cmd_locate)

    s = sub.add_parser("export", help="render + redact a transcript into a session dir")
    s.add_argument("--transcript", required=True)
    s.add_argument("--out", required=True, help="session directory (must already contain summary.md)")
    s.add_argument("--title")
    s.add_argument("--include-results", action="store_true", help="keep full tool results (capped at 20 KB each)")
    s.add_argument("--include-thinking", action="store_true")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("commit", help="commit the session dir via a temporary index and push")
    s.add_argument("--dir", required=True)
    s.add_argument("--push", action="store_true")
    s.add_argument("--branch", help="push to this new remote branch instead of the current one")
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
    s.add_argument("--marketplace-repo", default="prajwalgajakesari/claude-sessions")
    s.add_argument("--marketplace-name", default="claude-sessions")
    s.add_argument("--plugin-name", default="sessions")
    s.add_argument("--commit", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_init)
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


if __name__ == "__main__":
    sys.exit(main())
