"""Adapter registry and the live-session identification algorithm."""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from ..model import PUSH_MARKER_RE
from .base import Adapter, SessionRef, recent
from .claude_code import ClaudeCodeAdapter

REGISTRY: Dict[str, Adapter] = {}


def register(adapter: Adapter) -> None:
    REGISTRY[adapter.name] = adapter


register(ClaudeCodeAdapter())

try:  # optional adapters are added as they land; a broken import must not take the tool down
    from .codex import CodexAdapter
    register(CodexAdapter())
except ImportError:  # pragma: no cover
    pass
try:
    from .opencode import OpenCodeAdapter
    register(OpenCodeAdapter())
except ImportError:  # pragma: no cover
    pass
try:
    from .gemini import GeminiAdapter
    register(GeminiAdapter())
except ImportError:  # pragma: no cover
    pass

AGENT_CHOICES = ["auto"] + list(REGISTRY.keys())
RECENT_MINUTES = 15


class Resolution:
    def __init__(self, ref: Optional[SessionRef], how: str, candidates: List[SessionRef], adapter: Optional[Adapter]) -> None:
        self.ref, self.how, self.candidates, self.adapter = ref, how, candidates, adapter


def installed_adapters(agent: str = "auto") -> List[Adapter]:
    if agent != "auto":
        if agent not in REGISTRY:
            raise KeyError(agent)
        return [REGISTRY[agent]]
    return [a for a in REGISTRY.values() if a.installed()]


def resolve(agent: str, session_id: Optional[str], repo_root: str) -> Resolution:
    """Find the live session. Never picks silently between several plausible ones."""
    adapters = installed_adapters(agent)
    if session_id:
        for a in adapters:
            ref = a.locate_by_id(session_id)
            if ref:
                return Resolution(ref, "session id", [], a)
        # known id, no data yet: fresh session. Pick the adapter the environment proves, else the explicit one.
        owner = REGISTRY[agent] if agent != "auto" else _env_agent(adapters)
        if owner:
            return Resolution(owner.pending_ref(session_id), "not written yet", [], owner)
        return Resolution(None, "not found", [], None)

    for a in adapters:
        env_id = a.in_env()
        if env_id:
            ref = a.locate_by_id(env_id)
            if ref:
                return Resolution(ref, f"environment ({a.name})", [], a)
            return Resolution(a.pending_ref(env_id), "not written yet", [], a)

    all_cands: List[Tuple[Adapter, SessionRef]] = []
    for a in adapters:
        try:
            for ref in a.candidates(repo_root, limit=5):
                all_cands.append((a, ref))
        except Exception:  # noqa: BLE001 - one broken store must not hide the others
            continue
    all_cands.sort(key=lambda ar: ar[1].updated.timestamp() if ar[1].updated else 0, reverse=True)

    # self-referencing locate call still in flight
    hits = []
    for a, ref in all_cands:
        if not recent(ref, RECENT_MINUTES):
            continue
        try:
            sess = a.load(ref)
        except Exception:  # noqa: BLE001
            continue
        ref.extra["session"] = sess
        ref.title = ref.title or sess.title
        if sess.has_pending_self_call():
            hits.append((a, ref))
    if len(hits) == 1:
        return Resolution(hits[0][1], "self-referencing locate call", [], hits[0][0])

    # push marker in the latest user turn
    marks = []
    for a, ref in all_cands:
        sess = ref.extra.get("session")
        if sess is None:
            try:
                sess = a.load(ref)
                ref.extra["session"] = sess
                ref.title = ref.title or sess.title
            except Exception:  # noqa: BLE001
                continue
        if PUSH_MARKER_RE.search(sess.last_user_text() or "") or any(e.is_push_invocation for e in sess.events[-3:]):
            marks.append((a, ref))
    if len(marks) == 1:
        return Resolution(marks[0][1], "push marker in latest user turn", [], marks[0][0])

    if len(all_cands) == 1:
        a, ref = all_cands[0]
        how = "only recent session for this repo" if recent(ref, RECENT_MINUTES) else "only session for this repo (not recent; check it is the right one)"
        return Resolution(ref, how, [], a)
    if not all_cands:
        return Resolution(None, "not found", [], None)
    return Resolution(None, "ambiguous", [ref for _, ref in all_cands], None)


def _env_agent(adapters: List[Adapter]) -> Optional[Adapter]:
    for a in adapters:
        if a.in_env() is not None:
            return a
    return None


def adapter_for(agent: str) -> Adapter:
    if agent not in REGISTRY:
        raise KeyError(agent)
    return REGISTRY[agent]
