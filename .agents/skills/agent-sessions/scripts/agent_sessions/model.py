"""Agent-neutral session model. Adapters produce it, the renderer consumes it."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Canonical tool kinds. Adapters map their native tool names onto these; the
# renderer and the result policy never see agent-specific names.
KINDS = ("shell", "read", "edit", "write", "search", "web", "agent", "mcp", "ask", "other")
STUB_KINDS = {"read", "web", "mcp"}          # results replaced by a one-line stub
SNIPPET_KINDS = {"edit", "write"}            # inputs shown as short snippets
SENSITIVE_PROBE_KINDS = {"shell", "read", "edit", "write", "search"}

# A user turn that invoked push, in any agent's syntax.
PUSH_MARKER_RE = re.compile(
    r"<command-name>\s*/[\w-]*sessions[\w-]*:push\s*</command-name>"
    r"|/sessions:push\b"
    r"|\$agent-sessions\s+push\b"
    r"|\bagent-sessions\s+push\b"
    r"|\bsessions-push\b",
    re.I,
)

# The shell call that *is* the locate preflight. Used to recognise the live
# session: it is the one whose most recent shell call is this and has no output yet.
SELF_REF_RE = re.compile(r"sessions(?:\.sh|\.py)?\b[^\n]{0,200}\blocate\b|\bagent-sessions\b[^\n]{0,80}\blocate\b", re.I)


@dataclass
class ToolCall:
    kind: str                       # one of KINDS
    name: str                       # agent-native name
    label: str                      # one-line summary shown in <summary>
    input: object = None            # dict when structured, str when opaque
    output: str = ""
    is_error: bool = False
    paths: List[str] = field(default_factory=list)   # files this call touched (edit/write)
    pending: bool = False           # no output recorded yet (call in flight)


@dataclass
class Event:
    kind: str                       # user | assistant | thinking | tool | compaction | note
    text: str = ""
    ts: Optional[dt.datetime] = None
    is_push_invocation: bool = False
    command: str = ""               # slash command the user ran ("/compact"), if any
    tool: Optional[ToolCall] = None
    meta: dict = field(default_factory=dict)


@dataclass
class Session:
    agent: str                      # claude-code | codex | opencode | gemini-cli
    session_id: str
    short_id: str
    title: Optional[str]
    cwd: Optional[str]
    started: Optional[dt.datetime]
    ended: Optional[dt.datetime]
    events: List[Event]
    source: str                     # file path or "sqlite:<db>#<id>"
    agent_version: Optional[str] = None
    originator: Optional[str] = None
    parent_id: Optional[str] = None
    subagent_files: int = 0
    tokens: Optional[dict] = None
    tested_version: Optional[Tuple[int, ...]] = None
    warnings: List[str] = field(default_factory=list)
    record_count: int = 0
    bad_lines: int = 0

    def files_touched(self) -> List[str]:
        seen = {}
        for e in self.events:
            if e.tool:
                for p in e.tool.paths:
                    seen.setdefault(p, None)
        return list(seen.keys())

    def user_prompts(self, limit: int = 200) -> List[Tuple[Optional[dt.datetime], str]]:
        out = []
        for e in self.events:
            if e.kind != "user" or e.is_push_invocation:
                continue
            if e.command:
                out.append((e.ts, "/" + e.command.lstrip("/")))
            elif e.text:
                out.append((e.ts, re.sub(r"\s+", " ", e.text)[:limit]))
        return out

    def agent_tasks(self) -> List[str]:
        return [re.sub(r"\s+", " ", e.tool.label)[:120] for e in self.events if e.tool and e.tool.kind == "agent"]

    def last_user_text(self) -> str:
        for e in reversed(self.events):
            if e.kind == "user":
                return e.text or e.command
        return ""

    def has_pending_self_call(self) -> bool:
        """True when the most recent shell call is our own `locate` and has produced no output yet."""
        for e in reversed(self.events):
            if e.tool and e.tool.kind == "shell":
                return bool(e.tool.pending and SELF_REF_RE.search(_input_blob(e.tool.input)))
        return False


def _input_blob(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_input_blob(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_input_blob(v) for v in value)
    return "" if value is None else str(value)
