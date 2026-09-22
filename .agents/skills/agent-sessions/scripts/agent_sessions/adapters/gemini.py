"""Gemini CLI (experimental): ~/.gemini/tmp/<project_hash>/chats/session-<ts>-<id8>.jsonl.

Built from the chatRecordingService source, not from a real sample. The first line is
session metadata; later lines are message records or update records ($set, $rewindTo).
"""

from __future__ import annotations

import datetime as dt
import glob
import os
from typing import List, Optional

from ..core import first_json_line, mtime, parse_ts, read_jsonl, rel_to_root, under
from ..model import Event, PUSH_MARKER_RE, Session, ToolCall
from .base import Adapter, SessionRef, generic_label

KIND_BY_TOOL = {
    "run_shell_command": "shell", "shell": "shell",
    "read_file": "read", "read_many_files": "read",
    "write_file": "write", "replace": "edit", "edit": "edit",
    "glob": "search", "grep_search": "search", "search_file_content": "search", "list_directory": "search",
    "web_fetch": "web", "google_web_search": "web",
    "activate_skill": "other", "save_memory": "other",
}


def _parts_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return ""


class GeminiAdapter(Adapter):
    name = "gemini-cli"
    label = "Gemini CLI"
    tested_version = None

    def home(self) -> str:
        return os.path.abspath(os.path.expanduser(os.environ.get("GEMINI_CLI_HOME") or "~/.gemini"))

    def data_dir(self) -> str:
        return os.path.join(self.home(), "tmp")

    def installed(self) -> bool:
        return bool(glob.glob(os.path.join(self.data_dir(), "*", "chats")))

    def short_id(self, session_id: str) -> str:
        return session_id.replace("-", "")[:8]

    def _ref(self, path: str) -> SessionRef:
        meta = first_json_line(path) or {}
        sid = meta.get("sessionId") if isinstance(meta.get("sessionId"), str) else os.path.basename(path)[:-6]
        dirs = meta.get("directories") if isinstance(meta.get("directories"), list) else []
        cwd = next((d for d in dirs if isinstance(d, str)), None)
        return SessionRef(self.name, sid, self.short_id(sid), path, cwd=cwd, updated=mtime(path),
                          title=meta.get("summary") if isinstance(meta.get("summary"), str) else None, extra={"directories": dirs})

    def _paths(self) -> List[str]:
        return sorted(glob.glob(os.path.join(self.data_dir(), "*", "chats", "session-*.jsonl")), key=os.path.getmtime, reverse=True)

    def locate_by_id(self, session_id: str) -> Optional[SessionRef]:
        for p in glob.glob(os.path.join(self.data_dir(), "*", "chats", f"session-*-{session_id[:8]}.jsonl")):
            ref = self._ref(p)
            if ref.session_id == session_id:
                return ref
        return None

    def candidates(self, repo_root: str, limit: int = 5) -> List[SessionRef]:
        out: List[SessionRef] = []
        for p in self._paths()[:200]:
            ref = self._ref(p)
            if any(under(d, repo_root) for d in ref.extra.get("directories", [])) or under(ref.cwd, repo_root):
                out.append(ref)
                if len(out) >= limit:
                    break
        return out

    def ref_from_source(self, source: str) -> Optional[SessionRef]:
        if os.path.basename(source).startswith("session-") and source.endswith(".jsonl") and os.path.isfile(source):
            return self._ref(os.path.abspath(source))
        return None

    def load(self, ref: SessionRef) -> Session:
        records, bad = read_jsonl(ref.source)
        root_hint = ref.extra.get("repo_root") or ref.cwd
        meta: dict = {}
        messages: List[dict] = []
        for rec in records:
            if "sessionId" in rec and "messages" in rec or ("sessionId" in rec and not meta):
                meta = dict(rec)
                if isinstance(rec.get("messages"), list):
                    messages.extend(m for m in rec["messages"] if isinstance(m, dict))
                continue
            if "$set" in rec and isinstance(rec["$set"], dict):
                meta.update(rec["$set"])
                continue
            if "$rewindTo" in rec:
                target = rec["$rewindTo"]
                idx = next((i for i, m in enumerate(messages) if m.get("id") == target), None)
                if idx is not None:
                    messages = messages[: idx + 1]
                continue
            if rec.get("type") in ("user", "gemini", "info", "error", "warning"):
                messages.append(rec)

        events: List[Event] = []
        stamps: List[dt.datetime] = []
        tokens: Optional[dict] = None
        for m in messages:
            ts = parse_ts(m.get("timestamp"))
            if ts:
                stamps.append(ts)
            mtype = m.get("type")
            if mtype == "user":
                text = _parts_text(m.get("displayContent") or m.get("content")).strip()
                if not text:
                    continue
                if PUSH_MARKER_RE.search(text):
                    events.append(Event("user", text="", ts=ts, is_push_invocation=True))
                else:
                    events.append(Event("user", text=text, ts=ts))
            elif mtype == "gemini":
                for th in m.get("thoughts") or []:
                    if isinstance(th, dict):
                        t = " ".join(str(th.get(k, "")) for k in ("subject", "description") if th.get(k))
                        if t.strip():
                            events.append(Event("thinking", text=t, ts=ts))
                text = _parts_text(m.get("content")).strip()
                if text:
                    events.append(Event("assistant", text=text, ts=ts))
                for tc in m.get("toolCalls") or []:
                    if not isinstance(tc, dict):
                        continue
                    name = str(tc.get("name") or "tool")
                    args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                    kind = KIND_BY_TOOL.get(name, "mcp" if "__" in name else "other")
                    paths: List[str] = []
                    if kind in ("edit", "write"):
                        p = args.get("file_path") or args.get("path")
                        if isinstance(p, str) and p:
                            paths.append(rel_to_root(p, root_hint))
                    result = tc.get("result")
                    output = _parts_text(result) if not isinstance(result, str) else result
                    if not output and isinstance(result, dict):
                        output = str(result.get("output") or result.get("llmContent") or "")
                    status = str(tc.get("status") or "")
                    events.append(Event("tool", ts=ts, tool=ToolCall(kind, name, generic_label(name, kind, args, paths), input=args, output=output,
                                                                     is_error=status.lower() in ("error", "failed"), paths=paths,
                                                                     pending=status.lower() in ("executing", "scheduled", "running"))))
                tk = m.get("tokens")
                if isinstance(tk, dict):
                    tokens = tokens or {}
                    for k in ("input", "output", "cached", "thoughts", "tool", "total"):
                        if isinstance(tk.get(k), (int, float)):
                            tokens[k] = tokens.get(k, 0) + tk[k]
            elif mtype in ("info", "error", "warning"):
                text = _parts_text(m.get("content")).strip()
                if text:
                    events.append(Event("note", text=f"{mtype}: {text}", ts=ts))
        started = parse_ts(meta.get("startTime")) or (min(stamps) if stamps else None)
        ended = parse_ts(meta.get("lastUpdated")) or (max(stamps) if stamps else None)
        sub = 0
        parent_dir = os.path.join(os.path.dirname(ref.source), ref.session_id.replace("/", "_"))
        if os.path.isdir(parent_dir):
            sub = len(glob.glob(os.path.join(parent_dir, "*.jsonl")))
        return Session(
            agent=self.name, session_id=ref.session_id, short_id=ref.short_id, title=ref.title or (meta.get("summary") if isinstance(meta.get("summary"), str) else None),
            cwd=ref.cwd, started=started, ended=ended, events=events, source=ref.source, subagent_files=sub, tokens=tokens,
            tested_version=None, warnings=["Gemini CLI adapter is experimental: built from the source schema without a real sample"],
            record_count=len(records), bad_lines=bad,
        )
