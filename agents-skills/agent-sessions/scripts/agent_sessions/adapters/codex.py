"""OpenAI Codex CLI: $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl."""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
from typing import Dict, List, Optional, Tuple

from ..core import first_json_line, mtime, parse_ts, read_jsonl, rel_to_root, under
from ..model import Event, PUSH_MARKER_RE, Session, ToolCall
from .base import Adapter, SessionRef, generic_label, strip_wrappers

WRAPPER_TAGS = ("environment_context", "recommended_plugins", "user_instructions", "permissions instructions",
                "permissions_instructions", "turn_aborted", "send_user_message_question_reply", "INSTRUCTIONS")
DROP_PREFIXES = ("# AGENTS.md instructions", "<INSTRUCTIONS>", "<environment_context>", "<recommended_plugins>")
SHELL_NAMES = {"exec", "shell", "exec_command", "container.exec", "local_shell", "shell_command"}
READ_NAMES = {"read_file", "view_image", "read_files"}
WEB_NAMES = {"web_search", "web.run", "browser_search", "web_fetch"}
JS_STR = r"""(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|`((?:[^`\\]|\\.)*)`)"""
CMD_RE = re.compile(r"exec_command\(\s*\{[^}]*?\bcmd\s*:\s*" + JS_STR, re.S)
PATCH_CALL_RE = re.compile(r"apply_patch\(\s*" + JS_STR, re.S)
PATCH_HEADER_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)
LEAD_RE = re.compile(r"^(?:Script completed\s*\n(?:Wall time [^\n]*\n)?)?(?:Output:\s*\n?)?", re.S)
TRAIL_RE = re.compile(r"\n?Script completed\s*(?:\nWall time [^\n]*)?\s*$", re.S)
ROLLOUT_ID_RE = re.compile(r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
AGENT_NAMESPACES = {"collaboration", "agents"}


def _unescape(s: str) -> str:
    """Body of a JS string literal (escapes intact) -> the string it denotes."""
    try:
        return json.loads('"' + s + '"')
    except ValueError:
        return s.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\'", "'")


def _js_strings(regex: "re.Pattern[str]", snippet: str) -> List[str]:
    out = []
    for m in regex.finditer(snippet):
        raw = next((g for g in m.groups() if g is not None), "")
        out.append(_unescape(raw))
    return out


def extract_commands(snippet: str) -> List[str]:
    return _js_strings(CMD_RE, snippet)


def extract_patches(snippet: str) -> List[str]:
    return _js_strings(PATCH_CALL_RE, snippet)


def output_text(payload_output: object) -> str:
    """Codex outputs are strings or [{type: input_text, text}] with a 'Script completed' preamble and JSON body."""
    if isinstance(payload_output, list):
        text = "\n".join(str(b.get("text", "")) for b in payload_output if isinstance(b, dict))
    else:
        text = str(payload_output or "")
    text = TRAIL_RE.sub("", LEAD_RE.sub("", text.strip(), count=1)).strip()
    body = text
    if not (body.startswith("{") or body.startswith("[")):
        return text
    # one JSON document, or one JSON object per line for batched exec_command calls
    docs: List[object] = []
    try:
        docs.append(json.loads(body))
    except ValueError:
        for line in body.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                docs.append(json.loads(line))
            except ValueError:
                return text
    outs: List[str] = []

    def walk(v: object) -> None:
        if isinstance(v, dict):
            if isinstance(v.get("output"), str):
                outs.append(v["output"])
            for k, x in v.items():
                if k != "output":
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(docs)
    return "\n".join(outs) if outs else text


def patch_paths(patch: str) -> List[str]:
    out = []
    for m in PATCH_HEADER_RE.finditer(patch):
        p = m.group(1) or m.group(2)
        if p:
            out.append(p.strip())
    return out


class CodexAdapter(Adapter):
    name = "codex"
    label = "Codex CLI"
    env_ids = ("CODEX_THREAD_ID", "CODEX_SESSION_ID")
    tested_version = (0, 148, 0)

    def home(self) -> str:
        return os.path.abspath(os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex"))

    def data_dir(self) -> str:
        return os.path.join(self.home(), "sessions")

    def short_id(self, session_id: str) -> str:
        return session_id.replace("-", "")[-8:]

    # -- discovery

    def _rollout_paths(self, cap: int = 400) -> List[str]:
        base = self.data_dir()
        today = dt.date.today()
        ordered: List[str] = []
        seen = set()
        for day in (today, today - dt.timedelta(days=1)):
            for p in sorted(glob.glob(os.path.join(base, day.strftime("%Y"), day.strftime("%m"), day.strftime("%d"), "rollout-*.jsonl")),
                            key=os.path.getmtime, reverse=True):
                ordered.append(p)
                seen.add(p)
        rest = [p for p in glob.glob(os.path.join(base, "*", "*", "*", "rollout-*.jsonl")) if p not in seen]
        rest += glob.glob(os.path.join(self.home(), "archived_sessions", "**", "rollout-*.jsonl"), recursive=True)
        rest.sort(key=os.path.getmtime, reverse=True)
        return (ordered + rest)[:cap]

    def _titles(self) -> Dict[str, str]:
        titles: Dict[str, str] = {}
        idx = os.path.join(self.home(), "session_index.jsonl")
        if os.path.exists(idx):
            recs, _ = read_jsonl(idx)
            for r in recs:
                if isinstance(r.get("id"), str) and isinstance(r.get("thread_name"), str):
                    titles[r["id"]] = r["thread_name"]
        return titles

    def _ref(self, path: str, titles: Optional[Dict[str, str]] = None) -> SessionRef:
        first = first_json_line(path) or {}
        payload = first.get("payload") if first.get("type") == "session_meta" and isinstance(first.get("payload"), dict) else {}
        sid = payload.get("id") or payload.get("session_id")
        if not isinstance(sid, str):
            m = ROLLOUT_ID_RE.search(os.path.basename(path))
            sid = m.group(1) if m else os.path.basename(path)[:-6]
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
        titles = titles if titles is not None else self._titles()
        return SessionRef(self.name, sid, self.short_id(sid), path, cwd=cwd, updated=mtime(path), title=titles.get(sid),
                          extra={"parent_id": payload.get("parent_thread_id"), "cli_version": payload.get("cli_version"),
                                 "originator": payload.get("originator")})

    def locate_by_id(self, session_id: str) -> Optional[SessionRef]:
        hits = glob.glob(os.path.join(self.data_dir(), "*", "*", "*", f"rollout-*-{session_id}.jsonl"))
        hits += glob.glob(os.path.join(self.home(), "archived_sessions", "**", f"rollout-*-{session_id}.jsonl"), recursive=True)
        if not hits:
            return None
        hits.sort(key=os.path.getmtime, reverse=True)
        return self._ref(hits[0])

    def candidates(self, repo_root: str, limit: int = 5) -> List[SessionRef]:
        titles = self._titles()
        out: List[SessionRef] = []
        for p in self._rollout_paths():
            ref = self._ref(p, titles)
            if under(ref.cwd, repo_root) and _has_content(p):
                out.append(ref)
                if len(out) >= limit:
                    break
        return out

    def ref_from_source(self, source: str) -> Optional[SessionRef]:
        if os.path.basename(source).startswith("rollout-") and source.endswith(".jsonl") and os.path.isfile(source):
            return self._ref(os.path.abspath(source))
        return None

    # -- loading

    def load(self, ref: SessionRef) -> Session:
        records, bad = read_jsonl(ref.source)
        root_hint = ref.extra.get("repo_root") or ref.cwd
        outputs: Dict[str, str] = {}
        for rec in records:
            if rec.get("type") != "response_item":
                continue
            p = rec.get("payload") or {}
            if p.get("type") in ("custom_tool_call_output", "function_call_output") and isinstance(p.get("call_id"), str):
                outputs[p["call_id"]] = output_text(p.get("output"))

        events: List[Event] = []
        tokens: Optional[dict] = None
        stamps: List[dt.datetime] = []
        version = ref.extra.get("cli_version")
        originator = ref.extra.get("originator")
        parent_id = ref.extra.get("parent_id")
        for rec in records:
            rtype = rec.get("type")
            ts = parse_ts(rec.get("timestamp"))
            p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            if rtype == "session_meta":
                version = p.get("cli_version") or version
                originator = p.get("originator") or originator
                parent_id = p.get("parent_thread_id") or parent_id
                continue
            if rtype == "compacted":
                summary = _history_summary(p.get("replacement_history"))
                events.append(Event("compaction", text=summary, ts=ts))
                continue
            if rtype == "event_msg" and p.get("type") == "token_count":
                info = p.get("info") if isinstance(p.get("info"), dict) else p
                usage = info.get("total_token_usage") or info.get("last_token_usage") or info
                if isinstance(usage, dict):
                    tokens = {k: usage.get(k) for k in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens", "total_tokens") if k in usage}
                continue
            if rtype != "response_item":
                continue
            if ts:
                stamps.append(ts)
            ptype = p.get("type")
            if ptype == "message":
                role = p.get("role")
                if role == "developer":
                    continue
                texts = [str(c.get("text", "")) for c in p.get("content", []) if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text")]
                if role == "user":
                    kept = []
                    for t in texts:
                        if any(t.lstrip().startswith(pref) for pref in DROP_PREFIXES):
                            t = strip_wrappers(t, WRAPPER_TAGS)
                            if t.startswith("# AGENTS.md") or t.startswith("<INSTRUCTIONS>"):
                                continue
                        t = strip_wrappers(t, WRAPPER_TAGS)
                        if t:
                            kept.append(t)
                    text = "\n\n".join(kept).strip()
                    if not text:
                        continue
                    if PUSH_MARKER_RE.search(text):
                        events.append(Event("user", text="", ts=ts, is_push_invocation=True))
                    else:
                        events.append(Event("user", text=text, ts=ts))
                elif role == "assistant":
                    text = "\n\n".join(t for t in texts if t.strip())
                    if text:
                        events.append(Event("assistant", text=text, ts=ts))
            elif ptype == "agent_message":
                text = str(p.get("text") or p.get("message") or "")
                if text:
                    events.append(Event("assistant", text=text, ts=ts, meta={"agent_name": str(p.get("agent") or p.get("from") or "agent")}))
            elif ptype == "reasoning":
                parts = [str(s.get("text", "")) for s in p.get("summary", []) if isinstance(s, dict)]
                if any(parts):
                    events.append(Event("thinking", text="\n".join(x for x in parts if x), ts=ts))
            elif ptype == "custom_tool_call":
                name = str(p.get("name") or "tool")
                raw = str(p.get("input") or "")
                call_id = p.get("call_id") if isinstance(p.get("call_id"), str) else None
                cmds = extract_commands(raw)
                patches = extract_patches(raw) if "apply_patch" in raw else []
                paths: List[str] = []
                if patches:
                    # Codex Desktop edits files through tools.apply_patch(...) inside the exec snippet
                    for patch in patches:
                        paths.extend(rel_to_root(x, root_hint) if os.path.isabs(x) else x for x in patch_paths(patch))
                    inp: object = {"patch": "\n\n".join(patches)}
                    if cmds:
                        inp["command"] = "\n".join(cmds)  # type: ignore[index]
                    kind = "edit"
                    label = f"apply_patch: {paths[0] if paths else ''}"
                elif name in SHELL_NAMES or cmds:
                    inp = {"command": "\n".join(cmds)} if cmds else raw
                    kind = "shell"
                    label = generic_label(name, kind, inp, [])
                else:
                    inp, kind = raw, "other"
                    label = generic_label(name, kind, inp, [])
                out = outputs.get(call_id, "") if call_id else ""
                pending = bool(call_id) and call_id not in outputs
                events.append(Event("tool", ts=ts, tool=ToolCall(kind, name, label, input=inp, output=out, paths=paths, pending=pending)))
            elif ptype == "function_call":
                name = str(p.get("name") or "tool")
                ns = str(p.get("namespace") or "")
                call_id = p.get("call_id") if isinstance(p.get("call_id"), str) else None
                try:
                    args = json.loads(p.get("arguments") or "{}")
                except ValueError:
                    args = {"arguments": p.get("arguments")}
                if not isinstance(args, dict):
                    args = {"arguments": args}
                paths: List[str] = []
                if ns.startswith("mcp__") or name.startswith("mcp__"):
                    kind = "mcp"
                elif name in SHELL_NAMES:
                    kind = "shell"
                elif name == "apply_patch":
                    kind = "edit"
                    patch = str(args.get("patch") or args.get("input") or "")
                    args = {"patch": patch}
                    paths = [rel_to_root(x, root_hint) if os.path.isabs(x) else x for x in patch_paths(patch)]
                elif name in READ_NAMES:
                    kind = "read"
                elif name in WEB_NAMES:
                    kind = "web"
                elif name.startswith("request_user_input"):
                    kind = "ask"
                elif ns in AGENT_NAMESPACES or name in ("spawn_agent", "send_message", "wait_for_agent", "wait_agent", "followup_task"):
                    kind = "agent"
                else:
                    kind = "other"
                label = generic_label(f"{ns + '.' if ns else ''}{name}", kind, args, paths)
                out = outputs.get(call_id, "") if call_id else ""
                pending = bool(call_id) and call_id not in outputs
                events.append(Event("tool", ts=ts, tool=ToolCall(kind, name, label, input=args, output=out, paths=paths, pending=pending)))
            elif ptype == "compaction":
                events.append(Event("compaction", ts=ts))
        return Session(
            agent=self.name, session_id=ref.session_id, short_id=ref.short_id, title=ref.title or self._titles().get(ref.session_id),
            cwd=ref.cwd, started=min(stamps) if stamps else None, ended=max(stamps) if stamps else None, events=events,
            source=ref.source, agent_version=version if isinstance(version, str) else None, originator=originator if isinstance(originator, str) else None,
            parent_id=parent_id if isinstance(parent_id, str) else None, tokens=tokens, tested_version=self.tested_version,
            record_count=len(records), bad_lines=bad,
        )


def _has_content(path: str, lines: int = 40) -> bool:
    """Codex writes 3-record stubs when it forks or compacts; only rollouts with turns are candidates."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for _ in range(lines):
                line = fh.readline()
                if not line:
                    return False
                if '"type":"response_item"' in line or '"type": "response_item"' in line:
                    return True
    except OSError:
        return False
    return False


def _history_summary(history: object) -> str:
    """The messages Codex substitutes for everything before a compaction, as plain text."""
    if not isinstance(history, list):
        return ""
    parts = []
    for item in history:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        role = item.get("role", "?")
        texts = [str(c.get("text", "")) for c in item.get("content", []) if isinstance(c, dict)]
        text = strip_wrappers("\n".join(texts), WRAPPER_TAGS)
        if text:
            parts.append(f"**{role}:** {text}")
    return "\n\n".join(parts)
