"""Adapter contract shared by every agent."""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..model import Session


@dataclass
class SessionRef:
    agent: str
    session_id: str
    short_id: str
    source: str                      # file path or "sqlite:<db>#<id>"; "" when not on disk yet
    cwd: Optional[str] = None
    updated: Optional[dt.datetime] = None
    title: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @property
    def on_disk(self) -> bool:
        return bool(self.source)


class Adapter:
    name = "base"
    label = "Base"
    env_ids: Tuple[str, ...] = ()           # env vars carrying the session id
    env_presence: Tuple[str, ...] = ()      # env vars that merely prove we run inside this agent
    tested_version: Optional[Tuple[int, ...]] = None

    def data_dir(self) -> str:
        raise NotImplementedError

    def installed(self) -> bool:
        return os.path.exists(self.data_dir())

    def short_id(self, session_id: str) -> str:
        return session_id[:8]

    def locate_by_id(self, session_id: str) -> Optional[SessionRef]:
        raise NotImplementedError

    def candidates(self, repo_root: str, limit: int = 5) -> List[SessionRef]:
        raise NotImplementedError

    def load(self, ref: SessionRef) -> Session:
        raise NotImplementedError

    def ref_from_source(self, source: str) -> Optional[SessionRef]:
        """Build a ref from a SOURCE string this adapter printed earlier, or None if it is not ours."""
        return None

    def pending_ref(self, session_id: str) -> SessionRef:
        """A session we know the id of but that has no data on disk yet."""
        return SessionRef(self.name, session_id, self.short_id(session_id), "")

    def in_env(self) -> Optional[str]:
        """Session id from the environment, or "" if the environment proves the agent but not the id."""
        for var in self.env_ids:
            val = os.environ.get(var, "").strip()
            if val and not val.startswith("${"):
                return val
        for var in self.env_presence:
            if os.environ.get(var):
                return ""
        return None


def generic_label(name: str, kind: str, inp: object, paths: List[str]) -> str:
    """Default one-line label. Built from full strings; the renderer redacts then truncates."""
    d = inp if isinstance(inp, dict) else {}
    if kind == "shell":
        cmd = d.get("command") or d.get("cmd") or (inp if isinstance(inp, str) else "")
        if isinstance(cmd, list):
            cmd = "; ".join(str(c) for c in cmd)
        desc = str(d.get("description") or "").strip()
        return f"{name}: {desc or str(cmd).strip().split(chr(10))[0]}"
    if kind in ("read", "edit", "write"):
        target = paths[0] if paths else (d.get("filePath") or d.get("file_path") or d.get("path") or "")
        return f"{name}: {target}"
    if kind == "agent":
        return f"{name}: {str(d.get('description') or d.get('prompt') or '')}"
    if kind == "search":
        return f"{name}: {str(d.get('pattern') or d.get('query') or d.get('path') or '')}"
    if kind == "web":
        return f"{name}: {str(d.get('url') or d.get('query') or d.get('prompt') or '')}"
    if kind == "ask":
        return f"{name}"
    return name


def strip_wrappers(text: str, tags: Tuple[str, ...]) -> str:
    """Remove <tag ...>…</tag> blocks the harness injects around user text."""
    import re
    for tag in tags:
        text = re.sub(rf"<{tag}\b[^>]*>[\s\S]*?</{tag}>", "", text, flags=re.I)
    return text.strip()


def recent(ref: SessionRef, minutes: int) -> bool:
    if not ref.updated:
        return True
    return (dt.datetime.now(dt.timezone.utc) - ref.updated) <= dt.timedelta(minutes=minutes)
