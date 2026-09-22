"""OpenCode: SQLite at ~/.local/share/opencode/opencode.db (tables session, message, part)."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import tempfile
from typing import Dict, List, Optional, Tuple

from ..core import rel_to_root, under
from ..model import Event, PUSH_MARKER_RE, Session, ToolCall
from .base import Adapter, SessionRef, generic_label

KIND_BY_TOOL = {
    "bash": "shell", "read": "read", "edit": "edit", "write": "write", "patch": "edit", "multiedit": "edit",
    "glob": "search", "grep": "search", "list": "search", "webfetch": "web", "websearch": "web",
    "task": "agent", "question": "ask",
}
SKIP_TOOLS = {"todowrite", "todoread", "skill", "step-start", "step-finish"}
DEFAULT_TITLE_RE = re.compile(r"^New session - \d{4}-\d{2}-\d{2}T", re.I)


def _ms(value: object) -> Optional[dt.datetime]:
    if isinstance(value, (int, float)) and value > 0:
        return dt.datetime.fromtimestamp(value / 1000.0, tz=dt.timezone.utc)
    return None


class OpenCodeAdapter(Adapter):
    name = "opencode"
    label = "OpenCode"
    tested_version = (1, 18, 23)

    def db_path(self) -> str:
        if os.environ.get("OPENCODE_DB"):
            return os.path.abspath(os.path.expanduser(os.environ["OPENCODE_DB"]))
        base = os.environ.get("OPENCODE_DATA_DIR") or os.path.join(os.environ.get("XDG_DATA_HOME") or "~/.local/share", "opencode")
        return os.path.abspath(os.path.expanduser(os.path.join(base, "opencode.db")))

    def data_dir(self) -> str:
        return self.db_path()

    def short_id(self, session_id: str) -> str:
        # ids look like ses_fbb7bd4bdffekPeUCuVdJWvg6w; the tail is the random part
        return session_id.split("_", 1)[-1][-8:].lower()

    # -- db access

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path()
        con = sqlite3.connect(path, timeout=5)
        try:
            con.execute("PRAGMA query_only=1")
            con.execute("SELECT 1 FROM session LIMIT 1")
            return con
        except sqlite3.OperationalError as e:
            con.close()
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
        # copy db + wal + shm and read the copy
        tmp = tempfile.mkdtemp(prefix="agent-sessions-oc-")
        for suffix in ("", "-wal", "-shm"):
            src = path + suffix
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(tmp, "opencode.db" + suffix))
        con = sqlite3.connect(os.path.join(tmp, "opencode.db"), timeout=5)
        con.execute("PRAGMA query_only=1")
        return con

    def _ref_from_row(self, row: Tuple) -> SessionRef:
        sid, directory, title, time_updated, parent_id, version = row
        return SessionRef(self.name, sid, self.short_id(sid), f"sqlite:{self.db_path()}#{sid}", cwd=directory, updated=_ms(time_updated),
                          title=None if not title or DEFAULT_TITLE_RE.match(title) else title,
                          extra={"parent_id": parent_id, "version": version})

    def locate_by_id(self, session_id: str) -> Optional[SessionRef]:
        if not self.installed():
            return None
        con = self._connect()
        try:
            row = con.execute("SELECT id, directory, title, time_updated, parent_id, version FROM session WHERE id=?", (session_id,)).fetchone()
        finally:
            con.close()
        return self._ref_from_row(row) if row else None

    def candidates(self, repo_root: str, limit: int = 5) -> List[SessionRef]:
        if not self.installed():
            return []
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT s.id, s.directory, s.title, s.time_updated, s.parent_id, s.version, p.worktree "
                "FROM session s LEFT JOIN project p ON p.id = s.project_id "
                "WHERE s.parent_id IS NULL ORDER BY s.time_updated DESC LIMIT 200").fetchall()
        finally:
            con.close()
        out: List[SessionRef] = []
        for row in rows:
            directory, worktree = row[1], row[6]
            if under(directory, repo_root) or (worktree and worktree != "/" and under(worktree, repo_root)):
                out.append(self._ref_from_row(row[:6]))
                if len(out) >= limit:
                    break
        return out

    def ref_from_source(self, source: str) -> Optional[SessionRef]:
        if source.startswith("sqlite:") and "#" in source:
            sid = source.rsplit("#", 1)[1]
            return self.locate_by_id(sid)
        return None

    # -- loading

    def load(self, ref: SessionRef) -> Session:
        root_hint = ref.extra.get("repo_root") or ref.cwd
        con = self._connect()
        try:
            srow = con.execute(
                "SELECT title, version, time_created, time_updated, tokens_input, tokens_output, tokens_reasoning, "
                "tokens_cache_read, tokens_cache_write, cost, parent_id FROM session WHERE id=?", (ref.session_id,)).fetchone()
            messages = con.execute("SELECT id, data, time_created FROM message WHERE session_id=? ORDER BY time_created, id", (ref.session_id,)).fetchall()
            parts = con.execute("SELECT message_id, data, time_created FROM part WHERE session_id=? ORDER BY time_created, id", (ref.session_id,)).fetchall()
            children = con.execute("SELECT id FROM session WHERE parent_id=?", (ref.session_id,)).fetchall()
            child_parts = []
            for (cid,) in children:
                child_parts += con.execute("SELECT data FROM part WHERE session_id=?", (cid,)).fetchall()
            warnings: List[str] = []
            if not parts:
                try:
                    n = con.execute("SELECT COUNT(*) FROM session_message WHERE session_id=?", (ref.session_id,)).fetchone()[0]
                    if n:
                        warnings.append("OpenCode stored this session in the new session_message table, which this version cannot read yet")
                except sqlite3.OperationalError:
                    pass
        finally:
            con.close()

        by_message: Dict[str, List[dict]] = {}
        for message_id, data, _ in parts:
            try:
                by_message.setdefault(message_id, []).append(json.loads(data))
            except ValueError:
                continue

        events: List[Event] = []
        stamps: List[dt.datetime] = []
        for mid, data, time_created in messages:
            try:
                m = json.loads(data)
            except ValueError:
                continue
            role = m.get("role") or "assistant"
            ts = _ms((m.get("time") or {}).get("created")) or _ms(time_created)
            if ts:
                stamps.append(ts)
            for part in by_message.get(mid, []):
                events.extend(_events_from_part(part, role, ts, root_hint))

        hidden: List[Event] = []
        for (data,) in child_parts:
            try:
                part = json.loads(data)
            except ValueError:
                continue
            if part.get("type") == "tool" and KIND_BY_TOOL.get(str(part.get("tool"))) in ("edit", "write"):
                inp = (part.get("state") or {}).get("input") or {}
                p = inp.get("filePath") if isinstance(inp, dict) else None
                if isinstance(p, str) and p:
                    hidden.append(Event("tool", tool=ToolCall("edit", str(part.get("tool")), f"{part.get('tool')}: {p} (subagent)", input={},
                                                             paths=[rel_to_root(p, root_hint)]), meta={"hidden": True, "subagent": True}))

        title, version, t_created, t_updated, tin, tout, treason, tcr, tcw, cost, parent_id = srow if srow else (None,) * 11
        tokens = None
        if any(x for x in (tin, tout, treason, tcr, tcw)):
            tokens = {"input": tin, "output": tout, "reasoning": treason, "cache_read": tcr, "cache_write": tcw, "cost": cost}
        return Session(
            agent=self.name, session_id=ref.session_id, short_id=ref.short_id,
            title=None if not title or DEFAULT_TITLE_RE.match(str(title)) else str(title), cwd=ref.cwd,
            started=min(stamps) if stamps else _ms(t_created), ended=max(stamps) if stamps else _ms(t_updated),
            events=events + hidden, source=ref.source, agent_version=str(version) if version else None,
            parent_id=parent_id if isinstance(parent_id, str) else None, subagent_files=len(children), tokens=tokens,
            tested_version=self.tested_version, warnings=warnings, record_count=len(messages) + len(parts),
        )


def _events_from_part(part: dict, role: str, ts: Optional[dt.datetime], root_hint: Optional[str]) -> List[Event]:
    ptype = part.get("type")
    if ptype == "text":
        text = str(part.get("text") or "").strip()
        if not text:
            return []
        if role == "user":
            if PUSH_MARKER_RE.search(text):
                return [Event("user", text="", ts=ts, is_push_invocation=True)]
            return [Event("user", text=text, ts=ts)]
        return [Event("assistant", text=text, ts=ts)]
    if ptype == "reasoning":
        text = str(part.get("text") or "")
        return [Event("thinking", text=text, ts=ts)] if text.strip() else []
    if ptype == "compaction":
        return [Event("compaction", ts=ts, meta={"auto": part.get("auto")})]
    if ptype == "tool":
        name = str(part.get("tool") or "tool")
        if name in SKIP_TOOLS:
            return []
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = str(state.get("status") or "")
        inp = state.get("input") if isinstance(state.get("input"), dict) else {}
        kind = KIND_BY_TOOL.get(name, "mcp")
        paths: List[str] = []
        if kind in ("edit", "write"):
            p = inp.get("filePath") or inp.get("path")
            if isinstance(p, str) and p:
                paths.append(rel_to_root(p, root_hint))
        output = state.get("output")
        if not isinstance(output, str) or not output:
            meta_out = (state.get("metadata") or {}).get("output") if isinstance(state.get("metadata"), dict) else None
            output = meta_out if isinstance(meta_out, str) else ""
        is_error = status == "error"
        if is_error and not output:
            output = str(state.get("error") or "")
        label = generic_label(name, kind, inp, paths)
        if not label.split(": ", 1)[-1].strip() and isinstance(state.get("title"), str):
            label = f"{name}: {state['title']}"
        return [Event("tool", ts=ts, tool=ToolCall(kind, name, label, input=inp, output=output, is_error=is_error, paths=paths,
                                                    pending=(status == "running")))]
    return []
