"""Claude Code: ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl."""

from __future__ import annotations

import glob
import os
import re
from typing import Dict, List, Optional, Tuple

from ..core import config_dir, first_json_line, mtime, parse_ts, read_jsonl, rel_to_root, under, home_to_tilde
from ..model import Event, PUSH_MARKER_RE, Session, ToolCall
from .base import Adapter, SessionRef

KIND_BY_NAME = {
    "Bash": "shell", "Read": "read", "Edit": "edit", "MultiEdit": "edit", "NotebookEdit": "edit", "Write": "write",
    "Grep": "search", "Glob": "search", "WebFetch": "web", "WebSearch": "web", "Agent": "agent", "Task": "agent",
    "AskUserQuestion": "ask", "ReadMcpResourceTool": "mcp", "ReadMcpResourceDirTool": "mcp",
}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

SYSTEM_NOISE_RE = re.compile(
    r"<system-reminder>[\s\S]*?</system-reminder>|<local-command-caveat>[\s\S]*?</local-command-caveat>"
    r"|<local-command-stdout>[\s\S]*?</local-command-stdout>|<command-message>[\s\S]*?</command-message>",
    re.I,
)
COMMAND_NAME_RE = re.compile(r"<command-name>\s*([^<]*?)\s*</command-name>", re.I)
COMMAND_ARGS_RE = re.compile(r"<command-args>\s*([^<]*?)\s*</command-args>", re.I)


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


def clean_user_text(text: str) -> Tuple[str, Optional[str]]:
    m = COMMAND_NAME_RE.search(text)
    if m:
        name = m.group(1).strip()
        am = COMMAND_ARGS_RE.search(text)
        args = am.group(1).strip() if am else ""
        return "", (name + (" " + args if args else "")).strip()
    return SYSTEM_NOISE_RE.sub("", text).strip(), None


def tool_label(name: str, inp: dict) -> str:
    """Labels are built from full strings; the renderer redacts first and truncates last."""
    if name == "Bash":
        desc = str(inp.get("description") or "").strip()
        cmd = str(inp.get("command") or "").strip().split("\n")[0]
        return f"Bash: {desc or cmd}"
    if name == "Read":
        return f"Read: {home_to_tilde(str(inp.get('file_path', '')))}"
    if name in EDIT_TOOLS:
        return f"{name}: {home_to_tilde(str(inp.get('file_path') or inp.get('notebook_path') or ''))}"
    if name in ("Agent", "Task"):
        return f"Agent: {str(inp.get('description') or '')[:80]}"
    if name.startswith("mcp__"):
        parts = name.split("__")
        return f"MCP {parts[1] if len(parts) > 1 else '?'}: {parts[-1]}"
    if name in ("Grep", "Glob"):
        return f"{name}: {str(inp.get('pattern', ''))[:80]}"
    if name in ("WebFetch", "WebSearch"):
        return f"{name}: {str(inp.get('url') or inp.get('query') or '')[:80]}"
    return name


class ClaudeCodeAdapter(Adapter):
    name = "claude-code"
    label = "Claude Code"
    env_ids = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")
    env_presence = ("CLAUDECODE",)
    tested_version = (2, 1, 278)

    def data_dir(self) -> str:
        return os.path.join(config_dir(), "projects")

    def locate_by_id(self, session_id: str) -> Optional[SessionRef]:
        hits = glob.glob(os.path.join(self.data_dir(), "*", f"{session_id}.jsonl"))
        if not hits:
            return None
        hits.sort(key=os.path.getmtime, reverse=True)
        return self._ref(hits[0])

    def ref_from_source(self, source: str) -> Optional[SessionRef]:
        if source.endswith(".jsonl") and os.path.isfile(source):
            return self._ref(os.path.abspath(source))
        return None

    def _ref(self, path: str) -> SessionRef:
        sid = os.path.basename(path)[:-6]
        first = first_json_line(path)
        cwd = None
        if first and isinstance(first.get("cwd"), str):
            cwd = first["cwd"]
        else:
            # cwd lives on the first user/assistant record; peek a few lines
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    for _ in range(40):
                        line = fh.readline()
                        if not line:
                            break
                        if '"cwd"' in line:
                            import json
                            try:
                                obj = json.loads(line)
                                if isinstance(obj.get("cwd"), str):
                                    cwd = obj["cwd"]
                                    break
                            except ValueError:
                                continue
            except OSError:
                pass
        return SessionRef(self.name, sid, self.short_id(sid), path, cwd=cwd, updated=mtime(path))

    def candidates(self, repo_root: str, limit: int = 5) -> List[SessionRef]:
        paths = sorted(glob.glob(os.path.join(self.data_dir(), "*", "*.jsonl")), key=os.path.getmtime, reverse=True)[:80]
        out: List[SessionRef] = []
        for p in paths:
            ref = self._ref(p)
            if under(ref.cwd, repo_root):
                out.append(ref)
                if len(out) >= limit:
                    break
        return out

    # -- loading

    def load(self, ref: SessionRef) -> Session:
        records, bad = read_jsonl(ref.source)
        by_uuid: Dict[str, dict] = {r["uuid"]: r for r in records if isinstance(r.get("uuid"), str)}
        path = _active_path(records, by_uuid)
        results: Dict[str, Tuple[str, bool]] = {}
        for rec in path:
            if rec.get("type") == "user":
                for b in blocks_of(rec.get("message")):
                    if b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str):
                        results[b["tool_use_id"]] = (result_text(b.get("content")), bool(b.get("is_error")))
        events: List[Event] = []
        for rec in path:
            for ev in _events_from_record(rec, results, ref):
                # the compaction summary follows its boundary; fold it into one event
                if ev.kind == "compaction" and ev.meta.get("summary_only") and events and events[-1].kind == "compaction" and not events[-1].text:
                    events[-1].text = ev.text
                    continue
                events.append(ev)
        sidecars = self._subagent_files(ref)
        for sub in sidecars:
            sub_records, _ = read_jsonl(sub)
            for rec in sub_records:
                if rec.get("type") != "assistant":
                    continue
                for b in blocks_of(rec.get("message")):
                    if b.get("type") == "tool_use" and b.get("name") in EDIT_TOOLS:
                        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                        p = inp.get("file_path") or inp.get("notebook_path")
                        if isinstance(p, str) and p:
                            events.append(Event("tool", tool=ToolCall("edit", str(b.get("name")), f"{b.get('name')}: {home_to_tilde(p)} (subagent)",
                                                                     input={}, paths=[rel_to_root(p, _repo_hint(ref))]), meta={"subagent": True, "hidden": True}))
        title = None
        for rec in records:
            if rec.get("type") == "custom-title" and isinstance(rec.get("customTitle"), str):
                title = rec["customTitle"].strip()
        if not title:
            for rec in records:
                if rec.get("type") == "ai-title" and isinstance(rec.get("aiTitle"), str):
                    title = rec["aiTitle"].strip()
        stamps = [parse_ts(r.get("timestamp")) for r in records if r.get("type") in ("user", "assistant")]
        stamps = [s for s in stamps if s]
        version = next((r["version"] for r in records if isinstance(r.get("version"), str)), None)
        visible = [e for e in events if not e.meta.get("hidden")]
        hidden = [e for e in events if e.meta.get("hidden")]
        return Session(
            agent=self.name, session_id=ref.session_id, short_id=ref.short_id, title=title, cwd=ref.cwd,
            started=min(stamps) if stamps else None, ended=max(stamps) if stamps else None,
            events=visible + hidden, source=ref.source, agent_version=version, subagent_files=len(sidecars),
            tested_version=self.tested_version, record_count=len(records), bad_lines=bad,
        )

    def _subagent_files(self, ref: SessionRef) -> List[str]:
        side = os.path.join(os.path.dirname(ref.source), ref.session_id, "subagents")
        return sorted(glob.glob(os.path.join(side, "*.jsonl")))


def _repo_hint(ref: SessionRef) -> Optional[str]:
    return ref.extra.get("repo_root") or ref.cwd


def _active_path(records: List[dict], by_uuid: Dict[str, dict]) -> List[dict]:
    leaf = None
    for rec in reversed(records):
        if rec.get("type") in ("user", "assistant") and not rec.get("isSidechain") and rec.get("uuid") in by_uuid:
            leaf = rec
            break
    if leaf is None:
        return [r for r in records if r.get("type") in ("user", "assistant", "system") and not r.get("isSidechain")]
    path: List[dict] = []
    seen = set()
    cur: Optional[dict] = leaf
    while cur is not None and cur.get("uuid") not in seen:
        seen.add(cur.get("uuid"))
        path.append(cur)
        parent = cur.get("parentUuid")
        if parent is None and cur.get("type") == "system" and cur.get("subtype") == "compact_boundary":
            parent = cur.get("logicalParentUuid")
        cur = by_uuid.get(parent) if isinstance(parent, str) else None
    path.reverse()
    return path


def _events_from_record(rec: dict, results: Dict[str, Tuple[str, bool]], ref: SessionRef) -> List[Event]:
    rtype = rec.get("type")
    ts = parse_ts(rec.get("timestamp"))
    out: List[Event] = []
    if rtype == "system":
        if rec.get("subtype") == "compact_boundary":
            meta = rec.get("compactMetadata") if isinstance(rec.get("compactMetadata"), dict) else {}
            out.append(Event("compaction", ts=ts, meta={"pre_tokens": meta.get("preTokens"), "post_tokens": meta.get("postTokens")}))
        return out
    if rtype == "user":
        if rec.get("isMeta") or rec.get("isSidechain"):
            return out
        if rec.get("isCompactSummary"):
            # attach the summary text to the preceding compaction event when possible
            out.append(Event("compaction", text=result_text(rec.get("message", {}).get("content")), ts=ts, meta={"summary_only": True}))
            return out
        for b in blocks_of(rec.get("message")):
            btype = b.get("type")
            if btype == "text":
                raw = str(b.get("text", ""))
                if PUSH_MARKER_RE.search(raw):
                    out.append(Event("user", text="", ts=ts, is_push_invocation=True))
                    continue
                text, command = clean_user_text(raw)
                if command:
                    out.append(Event("user", ts=ts, command=command))
                elif text:
                    out.append(Event("user", text=text, ts=ts))
            elif btype == "image":
                out.append(Event("note", text="[image omitted]", ts=ts))
        return out
    if rtype == "assistant":
        if rec.get("isSidechain"):
            return out
        if rec.get("isApiErrorMessage"):
            out.append(Event("note", text="API error message omitted", ts=ts))
            return out
        for b in blocks_of(rec.get("message")):
            btype = b.get("type")
            if btype == "text":
                out.append(Event("assistant", text=str(b.get("text", "")), ts=ts))
            elif btype == "thinking":
                out.append(Event("thinking", text=str(b.get("thinking", "")), ts=ts))
            elif btype == "tool_use":
                name = str(b.get("name") or "tool")
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                kind = KIND_BY_NAME.get(name, "mcp" if name.startswith("mcp__") else "other")
                paths: List[str] = []
                if name in EDIT_TOOLS:
                    p = inp.get("file_path") or inp.get("notebook_path")
                    if isinstance(p, str) and p:
                        paths.append(rel_to_root(p, _repo_hint(ref)))
                tool_id = b.get("id") if isinstance(b.get("id"), str) else None
                output, is_error = results.get(tool_id, ("", False)) if tool_id else ("", False)
                pending = bool(tool_id) and tool_id not in results
                out.append(Event("tool", ts=ts, tool=ToolCall(kind, name, tool_label(name, inp), input=inp, output=output,
                                                              is_error=is_error, paths=paths, pending=pending)))
            else:
                out.append(Event("note", text=f"unsupported block: {btype}", ts=ts))
        return out
    return out
