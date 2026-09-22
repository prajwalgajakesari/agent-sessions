"""Shared machinery: git plumbing, redaction, rendering, storage, frontmatter."""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from . import __version__
from .model import Event, Session, STUB_KINDS, SENSITIVE_PROBE_KINDS, ToolCall

TOOL_VERSION = __version__
SESSIONS_REL = ".claude/sessions"

SUMMARY_MAX_LINES = 120
SUMMARY_MAX_BYTES = 6 * 1024
TRANSCRIPT_MAX_BYTES = 1024 * 1024
RESULT_MAX_LINES = 30
RESULT_MAX_BYTES = 2048
FULL_RESULT_MAX_BYTES = 20 * 1024
INPUT_MAX_BYTES = 2048
OUTLINE_MAX_PROMPTS = 25

GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_OPTIONAL_LOCKS": "0"}
GIT_TIMEOUT = 30
PUSH_TIMEOUT = 60

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES = PACKAGE_DIR / "templates"

# Agents we recognise but cannot export yet. locate/doctor name them when present.
UNSUPPORTED = OrderedDict([
    ("copilot-cli", ("GitHub Copilot CLI", ["~/.copilot/session-state"])),
    ("cursor", ("Cursor", ["~/.cursor/projects", "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb",
                          "~/.config/Cursor/User/globalStorage/state.vscdb"])),
])


class SessionsError(Exception):
    """User-facing failure. The message becomes REASON."""


# --------------------------------------------------------------------------- report


class Report:
    """Collects lines. STATUS is printed first and last so truncated output still shows it."""

    def __init__(self) -> None:
        self.lines: List[str] = []

    def add(self, *parts: object) -> None:
        self.lines.append(" ".join(str(p) for p in parts))

    def kv(self, key: str, value: object) -> None:
        self.lines.append(f"{key}: {value}")

    def blank(self) -> None:
        self.lines.append("")

    def finish(self, ok: bool, reason: str = "") -> str:
        status = "STATUS: ok" if ok else "STATUS: error"
        out = [status]
        if reason:
            out.append(f"REASON: {reason}")
        out.extend(self.lines)
        out.append(status)
        if reason:
            out.append(f"REASON: {reason}")
        return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- git


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


def under(path: Optional[str], root: str) -> bool:
    if not path:
        return False
    try:
        return os.path.realpath(path).startswith(os.path.realpath(root))
    except (OSError, ValueError):
        return False


def rel_to_root(path: str, root: Optional[str]) -> str:
    p = os.path.normpath(path)
    if root:
        try:
            rel = os.path.relpath(p, root)
            if not rel.startswith(".."):
                return rel.replace(os.sep, "/")
        except ValueError:
            pass
    return home_to_tilde(p)


# --------------------------------------------------------------------------- reading helpers


def read_jsonl(path: str) -> Tuple[List[dict], int]:
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


def first_json_line(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for _ in range(5):
                line = fh.readline()
                if not line:
                    return None
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None
    return None


def parse_ts(ts: object) -> Optional[dt.datetime]:
    if isinstance(ts, (int, float)):
        try:
            secs = ts / 1000.0 if ts > 1e11 else float(ts)
            return dt.datetime.fromtimestamp(secs, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
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
        return tuple(int(x) for x in re.split(r"[.-]", v)[:3] if x.isdigit())
    except ValueError:
        return None


def mtime(path: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.fromtimestamp(os.path.getmtime(path), tz=dt.timezone.utc)
    except OSError:
        return None


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
        self.review: List[Tuple[str, int, str]] = []

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

    def high_count(self) -> int:
        high = {p.name for p in PATTERNS if p.level == "high"} | {"private-key"}
        return sum(v for k, v in self.hits.items() if k in high)

    def review_count(self) -> int:
        rev = {p.name for p in PATTERNS if p.level == "review"}
        return sum(v for k, v in self.hits.items() if k in rev)


def redact_value(value: object, redactor: Redactor) -> object:
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


def flatten_strings(value: object, limit: int = 200) -> List[str]:
    out: List[str] = []

    def walk(v: object) -> None:
        if len(out) >= limit:
            return
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(value)
    return out


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


def render_input(kind: str, inp: object, withheld: bool) -> str:
    if withheld:
        return "_input withheld: references a sensitive file_"
    if kind == "shell":
        if isinstance(inp, dict):
            cmd = inp.get("command") or inp.get("cmd") or inp.get("commands")
            if isinstance(cmd, list):
                cmd = "\n".join(str(c) for c in cmd)
            cmd = str(cmd) if cmd else json.dumps(inp, ensure_ascii=False)
        else:
            cmd = str(inp or "")
        cmd, omitted = truncate(cmd, 40, INPUT_MAX_BYTES)
        return fence(cmd, "bash") + (f"\n_… {omitted} more lines_" if omitted else "")
    if kind == "write":
        content = str((inp or {}).get("content") or "") if isinstance(inp, dict) else str(inp or "")
        snippet, omitted = truncate(content, 20, INPUT_MAX_BYTES)
        return fence(snippet) + (f"\n_… {omitted} more lines_" if omitted else "")
    if kind == "edit":
        if isinstance(inp, dict):
            if isinstance(inp.get("patch"), str):
                patch, omitted = truncate(inp["patch"], 40, INPUT_MAX_BYTES)
                return fence(patch, "diff") + (f"\n_… {omitted} more lines_" if omitted else "")
            edits = inp.get("edits") if isinstance(inp.get("edits"), list) else [inp]
            parts = []
            for e in edits[:5]:
                if not isinstance(e, dict):
                    continue
                old, _ = truncate(str(e.get("old_string") or e.get("oldString") or e.get("old_str") or ""), 12, 1024)
                new, _ = truncate(str(e.get("new_string") or e.get("newString") or e.get("new_str") or ""), 12, 1024)
                if old or new:
                    parts.append("- old:\n" + fence(old) + "\n- new:\n" + fence(new))
            if parts:
                return "\n".join(parts)
        text, omitted = truncate(str(inp or ""), 30, INPUT_MAX_BYTES)
        return fence(text) + (f"\n_… truncated_" if omitted else "")
    if kind == "agent":
        prompt = str((inp or {}).get("prompt") or "") if isinstance(inp, dict) else str(inp or "")
        prompt, omitted = truncate(prompt, 15, INPUT_MAX_BYTES)
        return fence(prompt) + (f"\n_… {omitted} more lines_" if omitted else "")
    if kind == "ask":
        q = json.dumps(inp, ensure_ascii=False, indent=2) if isinstance(inp, (dict, list)) else str(inp or "")
        q, _ = truncate(q, 20, INPUT_MAX_BYTES)
        return fence(q)
    try:
        dumped = json.dumps(inp, indent=2, ensure_ascii=False) if isinstance(inp, (dict, list)) else str(inp or "")
    except (TypeError, ValueError):
        dumped = str(inp)
    dumped, omitted = truncate(dumped, 30, INPUT_MAX_BYTES)
    return fence(dumped, "json" if isinstance(inp, (dict, list)) else "") + (f"\n_… truncated_" if omitted else "")


def render_result(kind: str, text: str, is_error: bool, mode: str, withheld: Optional[str]) -> str:
    """mode: 'policy' (default), 'stub' (everything stubbed), 'full' (opt-in)."""
    if withheld:
        return f"_result withheld: reads a sensitive file ({withheld})_"
    text = scrub_persisted(text)
    nlines = text.count("\n") + (1 if text else 0)
    nbytes = len(text.encode("utf-8", "replace"))
    prefix = "**Error.** " if is_error else ""
    if not text.strip():
        return prefix + "_no output_"
    if mode == "stub" or (mode == "policy" and kind in STUB_KINDS):
        return f"{prefix}_result omitted: {nlines} lines, {nbytes} bytes_"
    if mode == "full":
        body, omitted = truncate(text, 10_000, FULL_RESULT_MAX_BYTES)
    else:
        body, omitted = truncate(text, RESULT_MAX_LINES, RESULT_MAX_BYTES)
    out = prefix + fence(body)
    if omitted:
        out += f"\n_… {omitted} more lines omitted ({nlines} lines, {nbytes} bytes total)_"
    return out


def render_tool(tc: ToolCall, redactor: Redactor, result_mode: str) -> Tuple[str, Optional[str]]:
    withheld = None
    if tc.kind in SENSITIVE_PROBE_KINDS:
        withheld = mentions_sensitive_file(" ".join([tc.label] + tc.paths + flatten_strings(tc.input)))
    inp = redact_value(tc.input, redactor)
    # redact the full label first, truncate last: cutting first could split a secret.
    # The label repeats the input, so count its hits with a throwaway redactor.
    label = Redactor().redact(home_to_tilde(tc.label), "transcript").replace("\n", " ")
    if len(label) > 120:
        label = label[:117] + "…"
    body = [f"<details>\n<summary>🔧 {label}</summary>\n", home_to_tilde(render_input(tc.kind, inp, bool(withheld)))]
    if tc.output or tc.is_error:
        out = redactor.redact(home_to_tilde(tc.output), "transcript")
        body.append("\n**Result**\n\n" + redactor.redact(render_result(tc.kind, out, tc.is_error, result_mode, withheld), "transcript"))
    elif tc.pending:
        body.append("\n_call still running when the transcript was exported_")
    else:
        body.append("\n_no result recorded_")
    body.append("\n</details>\n")
    return "\n".join(body), withheld


def render_session(sess: Session, redactor: Redactor, result_mode: str = "policy",
                   include_thinking: bool = False, title: str = "") -> Tuple[List[str], dict]:
    """Return (segments, stats). Segments can be split across files."""
    events = sess.events
    last_push = -1
    for i, e in enumerate(events):
        if e.is_push_invocation:
            last_push = i
    if last_push >= 0:
        events = events[:last_push]
    stats: Counter = Counter()
    stats["stopped_at_push"] = 1 if last_push >= 0 else 0

    segments: List[str] = []
    last_role = None
    last_date = None
    header = [f"# {title or sess.title or 'Coding agent session'}", ""]
    header.append(f"_Session `{sess.session_id}` · rendered by agent-sessions {TOOL_VERSION} from {sess.agent}"
                  f"{' ' + sess.agent_version if sess.agent_version else ''} · tool results "
                  f"{'included' if result_mode == 'full' else 'truncated or omitted'}; thinking {'included' if include_thinking else 'omitted'}._")
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

    for e in events:
        if e.meta.get("hidden"):
            continue  # bookkeeping events (subagent edits) feed files_touched only
        if e.kind == "compaction":
            pre, post = e.meta.get("pre_tokens"), e.meta.get("post_tokens")
            detail = f" ({pre} → {post} tokens)" if pre and post else ""
            emit(f"\n> **Context compacted here**{detail}. Everything above was summarised for the model.\n")
            if e.text.strip():
                body = redactor.redact(home_to_tilde(e.text), "transcript")
                body, _ = truncate(body, 400, 16 * 1024)
                emit("<details>\n<summary>Compaction summary given to the model</summary>\n\n" + body + "\n\n</details>\n")
                stats["compaction_summaries"] += 1
            last_role = None
            stats["compactions"] += 1
        elif e.kind == "note":
            if e.text.strip():
                emit(f"\n> _{redactor.redact(home_to_tilde(e.text), 'transcript').strip()}_\n")
                stats["notes"] += 1
        elif e.kind == "user":
            if e.is_push_invocation:
                continue
            if e.command:
                emit(role_header("User", e.ts) + f"\n> Ran `/{e.command.lstrip('/')}`\n")
                stats["commands"] += 1
            elif e.text.strip():
                emit(role_header("User", e.ts) + "\n" + redactor.redact(home_to_tilde(e.text), "transcript") + "\n")
                stats["user_messages"] += 1
        elif e.kind == "assistant":
            if e.text.strip():
                who = "Assistant" + (f" ({e.meta['agent_name']})" if e.meta.get("agent_name") else "")
                emit(role_header(who, e.ts) + "\n" + redactor.redact(home_to_tilde(e.text), "transcript") + "\n")
                stats["assistant_messages"] += 1
        elif e.kind == "thinking":
            stats["thinking_blocks"] += 1
            if include_thinking and e.text.strip():
                think = redactor.redact(home_to_tilde(e.text), "transcript")
                think, _ = truncate(think, 80, 6 * 1024)
                emit(role_header("Assistant", e.ts) + "\n<details>\n<summary>Thinking</summary>\n\n" + think + "\n\n</details>\n")
        elif e.kind == "tool" and e.tool:
            body, withheld = render_tool(e.tool, redactor, result_mode)
            emit(role_header("Assistant", e.ts) + "\n" + body)
            stats["tool_calls"] += 1
            if withheld:
                stats["withheld_results"] += 1
        else:
            stats[f"ignored:{e.kind}"] += 1

    stats["messages"] = stats["user_messages"] + stats["assistant_messages"]
    return segments, dict(stats)


# --------------------------------------------------------------------------- frontmatter


def parse_frontmatter(text: str) -> Tuple["OrderedDict[str, object]", str]:
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


# --------------------------------------------------------------------------- storage


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")


def load_template(name: str) -> str:
    with open(TEMPLATES / name, "r", encoding="utf-8") as fh:
        return fh.read()


def session_dir_name(started: Optional[dt.datetime], title: str, handle: str, short_id: str) -> str:
    date = (started or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d")
    return f"{date}_{slugify(title)}_{handle}_{short_id}"


def existing_session_dir(root: str, session_id: str, short_id: str) -> Optional[str]:
    base = os.path.join(root, SESSIONS_REL)
    for cand in glob.glob(os.path.join(base, f"*_{short_id}")):
        meta = os.path.join(cand, "meta.json")
        try:
            with open(meta, "r", encoding="utf-8") as fh:
                if json.load(fh).get("session_id") == session_id:
                    return cand
        except (OSError, ValueError):
            if os.path.isdir(cand):
                return cand
    return None


def config_dir() -> str:
    return os.path.abspath(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))


def script_path() -> str:
    """Absolute path of the entry script the user invoked, for the SCRIPT: line.

    The shim runs sessions.py, but the shim itself is what skills should call, so report it when present.
    """
    if not (sys.argv and sys.argv[0]):
        return "agent-sessions"
    p = os.path.abspath(sys.argv[0])
    if os.path.basename(p) == "sessions.py":
        sh = os.path.join(os.path.dirname(p), "sessions.sh")
        if os.path.exists(sh):
            return sh
    return p
