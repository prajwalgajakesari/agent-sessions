"""Adapter tests: Codex CLI, OpenCode, Gemini CLI, and the cross-agent resolve() algorithm.

Fixtures are synthesised with the record shapes observed in Codex 0.148 rollouts,
OpenCode 1.18's SQLite schema and Gemini CLI's chatRecordingService source.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_sessions.adapters import REGISTRY, resolve  # noqa: E402
from agent_sessions.adapters.codex import CodexAdapter, extract_commands, extract_patches, output_text, patch_paths  # noqa: E402
from agent_sessions.adapters.gemini import GeminiAdapter  # noqa: E402
from agent_sessions.adapters.opencode import OpenCodeAdapter  # noqa: E402
from agent_sessions.cli import main as cli_main  # noqa: E402
from agent_sessions.core import Redactor, render_session  # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 9, 22, 6, 0, tzinfo=UTC)


def run(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli_main(argv)
    return code, buf.getvalue()


def git(args, cwd):
    p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {p.stderr}")
    return p.stdout.strip()


def init_repo(path):
    os.makedirs(path, exist_ok=True)
    git(["init", "-q", "-b", "main"], path)
    git(["config", "user.name", "Test User"], path)
    git(["config", "user.email", "test@example.com"], path)
    git(["config", "commit.gpgsign", "false"], path)
    Path(path, "README.md").write_text("# demo\n", encoding="utf-8")
    git(["add", "README.md"], path)
    git(["commit", "-q", "-m", "init"], path)
    return path


class Isolated:
    """Point every adapter at empty stores under a temp dir and hide the host agent's env."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.vars = {
            "CLAUDE_CONFIG_DIR": os.path.join(tmp, "claude"),
            "CODEX_HOME": os.path.join(tmp, "codex"),
            "OPENCODE_DB": os.path.join(tmp, "opencode", "opencode.db"),
            "GEMINI_CLI_HOME": os.path.join(tmp, "gemini"),
        }

    def __enter__(self):
        self.old = dict(os.environ)
        for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDECODE", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_PROJECT_DIR"):
            os.environ.pop(k, None)
        os.environ.update(self.vars)
        return self

    def __exit__(self, *exc):
        os.environ.clear()
        os.environ.update(self.old)


# --------------------------------------------------------------------------- Codex fixture


class CodexBuilder:
    def __init__(self, home, cwd, sid=None, title=None):
        self.home, self.cwd = home, cwd
        self.sid = sid or "01a0c7c0-0612-7860-8f15-" + uuid.uuid4().hex[:12]
        self.title = title
        self.t = T0
        self.records = [self._rec("session_meta", {
            "id": self.sid, "session_id": self.sid, "cwd": cwd, "cli_version": "0.148.0", "originator": "Codex CLI",
            "git": {"branch": "main"}, "parent_thread_id": None,
        })]

    def _rec(self, type_, payload):
        self.t += dt.timedelta(seconds=5)
        return {"timestamp": self.t.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "type": type_, "payload": payload}

    def _item(self, payload):
        self.records.append(self._rec("response_item", payload))

    def user(self, text, wrapped=False):
        content = []
        if wrapped:
            content.append({"type": "input_text", "text": "<recommended_plugins>\nplugins here\n</recommended_plugins>"})
            content.append({"type": "input_text", "text": "# AGENTS.md instructions\n\n<INSTRUCTIONS>\nSECRET RULES\n</INSTRUCTIONS>"})
            content.append({"type": "input_text", "text": f"<environment_context>\n  <cwd>{self.cwd}</cwd>\n</environment_context>"})
        content.append({"type": "input_text", "text": text})
        self._item({"type": "message", "role": "user", "content": content, "id": "msg_" + uuid.uuid4().hex[:8]})
        self.records.append(self._rec("event_msg", {"type": "user_message", "message": text}))

    def developer(self, text):
        self._item({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]})

    def assistant(self, text):
        self._item({"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": text}]})

    def reasoning(self, summary):
        self._item({"type": "reasoning", "summary": [{"type": "summary_text", "text": summary}], "encrypted_content": "gAAAA"})

    def exec(self, cmds=None, patch=None, output=None, batched=False):
        call_id = "call_" + uuid.uuid4().hex[:10]
        parts = []
        for c in cmds or []:
            parts.append("tools.exec_command({cmd:" + json.dumps(c) + ", yield_time_ms: 10000})")
        if patch:
            parts.append("tools.apply_patch(" + json.dumps(patch) + ")")
        snippet = "text(await " + parts[0] + ")" if len(parts) == 1 else "const r = await Promise.allSettled([" + ", ".join(parts) + "]);\ntext(JSON.stringify(r))"
        self._item({"type": "custom_tool_call", "name": "exec", "input": snippet, "call_id": call_id, "status": "completed"})
        if output is not None:
            if batched:
                body = "\n".join(json.dumps({"i": i, "status": "fulfilled", "value": {"chunk_id": "abc", "exit_code": 0, "output": o}}) for i, o in enumerate(output))
                text = "Output:\n" + body + "\nScript completed\nWall time 2.2 seconds"
            else:
                text = "Script completed\nWall time 0.2 seconds\nOutput:\n" + json.dumps({"chunk_id": "abc", "exit_code": 0, "output": output})
            self._item({"type": "custom_tool_call_output", "call_id": call_id, "output": [{"type": "input_text", "text": text}]})
        return call_id

    def function_call(self, name, args, output=None, namespace=None):
        call_id = "call_" + uuid.uuid4().hex[:10]
        payload = {"type": "function_call", "name": name, "arguments": json.dumps(args), "call_id": call_id}
        if namespace:
            payload["namespace"] = namespace
        self._item(payload)
        if output is not None:
            self._item({"type": "function_call_output", "call_id": call_id, "output": output})
        return call_id

    def compacted(self, texts):
        history = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": t}]} for t in texts]
        self.records.append(self._rec("compacted", {"message": "", "replacement_history": history}))

    def token_count(self, total):
        self.records.append(self._rec("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": total, "output_tokens": 10, "total_tokens": total + 10}}}))

    def write(self):
        day = os.path.join(self.home, "sessions", "2026", "09", "22")
        os.makedirs(day, exist_ok=True)
        path = os.path.join(day, f"rollout-2026-09-22T06-00-00-{self.sid}.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for r in self.records:
                fh.write(json.dumps(r) + "\n")
        if self.title:
            with open(os.path.join(self.home, "session_index.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"id": self.sid, "thread_name": self.title, "updated_at": self.t.isoformat()}) + "\n")
        return path


class TestCodex(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="as-"))
        self.repo = init_repo(os.path.join(self.tmp, "repo"))
        self.iso = Isolated(self.tmp).__enter__()
        self.home = os.environ["CODEX_HOME"]
        os.makedirs(os.path.join(self.home, "sessions"))
        self.adapter = CodexAdapter()

    def tearDown(self):
        self.iso.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_helpers(self):
        snippet = 'text(await tools.exec_command({cmd:"pwd; echo \\"hi\\"\\nls", yield_time_ms: 1}))'
        self.assertEqual(extract_commands(snippet), ['pwd; echo "hi"\nls'])
        patch = "*** Begin Patch\n*** Add File: /w/a.py\n+x\n*** Update File: b/c.md\n@@\n-a\n+b\n*** End Patch"
        self.assertEqual(patch_paths(patch), ["/w/a.py", "b/c.md"])
        self.assertEqual(extract_patches("text(await tools.apply_patch(" + json.dumps(patch) + "))"), [patch])
        self.assertEqual(output_text([{"type": "input_text", "text": "Script completed\nWall time 0.2 seconds\nOutput:\n" + json.dumps({"exit_code": 0, "output": "hello\nworld"})}]), "hello\nworld")
        batched = "Output:\n" + json.dumps({"i": 0, "value": {"output": "one"}}) + "\n" + json.dumps({"i": 1, "value": {"output": "two"}}) + "\nScript completed\nWall time 1 seconds"
        self.assertEqual(output_text(batched), "one\ntwo")
        self.assertEqual(output_text("plain text result"), "plain text result")

    def test_locate_load_and_render(self):
        b = CodexBuilder(self.home, self.repo, title="Fix the parser")
        b.user("please fix the parser", wrapped=True)
        b.developer("<image_resize_notice>ignored</image_resize_notice>")
        b.reasoning("Thinking about parsers")
        b.exec(cmds=["pwd", "ls -la"], output="/w\ntotal 0")
        b.exec(patch="*** Begin Patch\n*** Update File: " + os.path.join(self.repo, "src", "parser.py") + "\n@@\n-a\n+b\n*** End Patch", output="Done!")
        b.function_call("js", {"code": "1+1"}, output="2", namespace="mcp__cua_repl")
        b.function_call("spawn_agent", {"prompt": "review"}, output="ok", namespace="collaboration")
        b.function_call("request_user_input_async", {"questions": [{"title": "Which one?"}]}, output="a")
        b.assistant("Fixed it.")
        b.token_count(1234)
        path = b.write()
        ref = self.adapter.locate_by_id(b.sid)
        self.assertIsNotNone(ref)
        self.assertEqual(ref.title, "Fix the parser")
        self.assertEqual(ref.short_id, b.sid.replace("-", "")[-8:])
        self.assertEqual(ref.cwd, self.repo)
        ref.extra["repo_root"] = self.repo
        sess = self.adapter.load(ref)
        kinds = [e.kind for e in sess.events]
        self.assertEqual(kinds.count("user"), 1)
        self.assertEqual(sess.events[0].text, "please fix the parser")
        self.assertIn("thinking", kinds)
        tools = [e.tool for e in sess.events if e.tool]
        self.assertEqual([t.kind for t in tools], ["shell", "edit", "mcp", "agent", "ask"])
        self.assertEqual(tools[0].input["command"], "pwd\nls -la")
        self.assertEqual(tools[0].output, "/w\ntotal 0")
        self.assertEqual(tools[1].paths, ["src/parser.py"])
        self.assertEqual(sess.files_touched(), ["src/parser.py"])
        self.assertEqual(sess.tokens["input_tokens"], 1234)
        self.assertEqual(sess.agent_version, "0.148.0")
        segs, stats = render_session(sess, Redactor())
        text = "\n".join(segs)
        self.assertIn("please fix the parser", text)
        self.assertNotIn("SECRET RULES", text)
        self.assertNotIn("recommended_plugins", text)
        self.assertNotIn("image_resize_notice", text)
        self.assertIn("pwd\nls -la", text)
        self.assertIn("/w\ntotal 0", text)
        self.assertNotIn("chunk_id", text)
        self.assertIn("apply_patch: src/parser.py", text)
        self.assertIn("```diff", text)
        self.assertIn("result omitted", text)  # mcp stubbed
        self.assertEqual(stats["tool_calls"], 5)

    def test_compacted_and_push_marker(self):
        b = CodexBuilder(self.home, self.repo)
        b.user("first ask")
        b.assistant("FIRST REPLY")
        b.compacted(["summary of first ask", "<environment_context>x</environment_context>"])
        b.user("after compaction")
        b.assistant("SECOND REPLY")
        b.user("$agent-sessions push smoke --yes")
        b.assistant("WRITING SUMMARY")
        path = b.write()
        sess = self.adapter.load(self.adapter.ref_from_source(path))
        self.assertTrue(any(e.is_push_invocation for e in sess.events))
        segs, stats = render_session(sess, Redactor())
        text = "\n".join(segs)
        self.assertIn("Context compacted here", text)
        self.assertIn("summary of first ask", text)
        self.assertIn("SECOND REPLY", text)
        self.assertNotIn("WRITING SUMMARY", text)
        self.assertEqual(stats["compactions"], 1)
        self.assertEqual(stats["stopped_at_push"], 1)

    def test_candidates_skip_stubs_and_self_ref_wins(self):
        stub = CodexBuilder(self.home, self.repo)
        stub.compacted(["fork stub"])
        stub.write()
        other = CodexBuilder(self.home, self.repo, title="Other work")
        other.user("hello")
        other.assistant("hi")
        other.write()
        live = CodexBuilder(self.home, self.repo, title="Live work")
        live.user("working")
        pending_call = live.exec(cmds=['bash "/Users/x/.agents/skills/agent-sessions/scripts/sessions.sh" locate --agent auto'])  # no output yet
        live_path = live.write()
        ids = [c.session_id for c in self.adapter.candidates(self.repo, limit=10)]
        self.assertNotIn(stub.sid, ids)
        self.assertEqual(set(ids), {other.sid, live.sid})
        res = resolve("auto", None, self.repo)
        self.assertIsNotNone(res.ref, res.how)
        self.assertEqual(res.ref.session_id, live.sid)
        self.assertEqual(res.how, "self-referencing locate call")
        self.assertEqual(res.adapter.name, "codex")
        # without the pending call the two sessions are ambiguous
        with open(live_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"timestamp": "2026-09-22T07:00:00.000Z", "type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": pending_call, "output": "STATUS: ok"}}) + "\n")
        os.utime(live_path, None)
        live2 = CodexBuilder(self.home, self.repo, title="Third")
        live2.user("more")
        live2.assistant("ok")
        live2.write()
        res = resolve("auto", None, self.repo)
        self.assertIsNone(res.ref)
        self.assertEqual(res.how, "ambiguous")
        self.assertGreaterEqual(len(res.candidates), 2)

    def test_cli_locate_and_export(self):
        b = CodexBuilder(self.home, self.repo, title="Export me")
        b.user("do it")
        b.exec(cmds=["echo AKIAABCDEFGHIJKLMNOP"], output="AKIAABCDEFGHIJKLMNOP")
        b.assistant("done")
        b.write()
        code, text = run(["locate", "--agent", "codex", "--session-id", b.sid, "--project-dir", self.repo])
        self.assertEqual(code, 0, text)
        self.assertIn("AGENT: codex", text)
        self.assertIn("TITLE_DEFAULT: Export me", text)
        self.assertIn(f"SHORT_ID: {b.sid.replace('-', '')[-8:]}", text)
        out = Path(self.repo, ".claude", "sessions", "tmp-dir")
        out.mkdir(parents=True)
        (out / "summary.md").write_text("---\ntitle: \"Export me\"\ntags: [codex]\n---\n\n## Goal\nx\n", encoding="utf-8")
        code, text = run(["export", "--agent", "codex", "--session-id", b.sid, "--out", str(out)])
        self.assertEqual(code, 0, text)
        self.assertIn("RENAMED: yes", text)
        final = next(Path(self.repo, ".claude", "sessions").glob("*_export-me_*"))
        meta = json.loads((final / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["agent"], "codex")
        self.assertEqual(meta["agent_version"], "0.148.0")
        self.assertEqual(meta["originator"], "Codex CLI")
        body = (final / "transcript.md").read_text(encoding="utf-8")
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", body)
        self.assertIn("from codex 0.148.0", body)
        code, text = run(["commit", "--dir", str(final)])
        self.assertEqual(code, 0, text)
        self.assertIn(f"Agent-Session: codex:{b.sid}", git(["log", "-1", "--format=%B"], self.repo))


# --------------------------------------------------------------------------- OpenCode fixture


SCHEMA = """
CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT, vcs TEXT, name TEXT, time_created INTEGER, time_updated INTEGER);
CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, workspace_id TEXT, parent_id TEXT, slug TEXT, directory TEXT, path TEXT, title TEXT,
  version TEXT, share_url TEXT, summary_additions INTEGER, summary_deletions INTEGER, summary_files INTEGER, summary_diffs TEXT, metadata TEXT,
  cost REAL, tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER, tokens_cache_read INTEGER, tokens_cache_write INTEGER,
  revert TEXT, permission TEXT, agent TEXT, model TEXT, time_created INTEGER, time_updated INTEGER, time_compacting INTEGER, time_archived INTEGER);
CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT);
"""


class OpenCodeDb:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.executescript(SCHEMA)
        self.n = 0
        # recent timestamps: resolve() only trusts self-referencing calls in sessions updated within 15 minutes
        self.ms = int((dt.datetime.now(UTC) - dt.timedelta(minutes=5)).timestamp() * 1000)

    def tick(self):
        self.n += 1
        self.ms += 1000
        return self.ms

    def project(self, pid, worktree):
        self.con.execute("INSERT INTO project (id, worktree, vcs) VALUES (?,?,?)", (pid, worktree, "git"))

    def session(self, sid, directory, title="New session - 2026-09-22T06:00:00.000Z", project_id="global", parent_id=None, version="1.18.23", tokens=(0, 0, 0, 0, 0)):
        t = self.tick()
        self.con.execute(
            "INSERT INTO session (id, project_id, parent_id, directory, title, version, cost, tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write, agent, time_created, time_updated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (sid, project_id, parent_id, directory, title, version, 0.0, *tokens, "build", t, t))
        return sid

    def touch(self, sid):
        self.con.execute("UPDATE session SET time_updated=? WHERE id=?", (self.tick(), sid))

    def message(self, sid, role):
        mid = f"msg_{self.n:04d}" + uuid.uuid4().hex[:6]
        t = self.tick()
        self.con.execute("INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?,?,?,?,?)",
                         (mid, sid, t, t, json.dumps({"role": role, "time": {"created": t}, "agent": "build"})))
        return mid

    def part(self, sid, mid, data):
        pid = f"prt_{self.n:04d}" + uuid.uuid4().hex[:6]
        t = self.tick()
        self.con.execute("INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?,?,?,?,?,?)", (pid, mid, sid, t, t, json.dumps(data)))
        self.con.execute("UPDATE session SET time_updated=? WHERE id=?", (t, sid))
        return pid

    def tool(self, sid, mid, name, inp, output="", status="completed", error=None, title=None):
        state = {"status": status, "input": inp, "title": title or ""}
        if status == "completed":
            state["output"] = output
            state["metadata"] = {"output": output, "exit": 0}
        elif status == "error":
            state["error"] = error or "boom"
        return self.part(sid, mid, {"type": "tool", "tool": name, "callID": "call_" + uuid.uuid4().hex[:6], "state": state})

    def commit(self):
        self.con.commit()

    def close(self):
        self.con.commit()
        self.con.close()


class TestOpenCode(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="as-"))
        self.repo = init_repo(os.path.join(self.tmp, "repo"))
        self.iso = Isolated(self.tmp).__enter__()
        self.db = OpenCodeDb(os.environ["OPENCODE_DB"])
        self.adapter = OpenCodeAdapter()

    def tearDown(self):
        try:
            self.db.close()
        except sqlite3.ProgrammingError:
            pass
        self.iso.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _session(self, sid="ses_fbb7bd4bdffekPeUCuVdJWvg6w"):
        self.db.project("proj1", self.repo)
        self.db.session(sid, self.repo, project_id="proj1", tokens=(1000, 200, 30, 5000, 0))
        m = self.db.message(sid, "user")
        self.db.part(sid, m, {"type": "text", "text": "build the parser please"})
        a = self.db.message(sid, "assistant")
        self.db.part(sid, a, {"type": "step-start"})
        self.db.part(sid, a, {"type": "reasoning", "text": "PRIVATE THOUGHT"})
        self.db.tool(sid, a, "bash", {"command": "ls -la", "description": "list"}, output="total 0\nREADME.md")
        self.db.tool(sid, a, "read", {"filePath": os.path.join(self.repo, "data.csv")}, output="ROW,SECRETVALUE")
        self.db.tool(sid, a, "edit", {"filePath": os.path.join(self.repo, "src", "p.py"), "oldString": "a", "newString": "b"}, output="ok")
        self.db.tool(sid, a, "todowrite", {"todos": []}, output="ok")
        self.db.tool(sid, a, "bash", {"command": "false"}, status="error", error="exit 1")
        self.db.tool(sid, a, "fabric_query", {"sql": "select 1"}, output="QUERYROWS")
        self.db.part(sid, a, {"type": "text", "text": "Parser built."})
        self.db.part(sid, a, {"type": "step-finish", "tokens": {"total": 10}})
        u2 = self.db.message(sid, "user")
        self.db.part(sid, u2, {"type": "compaction", "auto": False, "tail_start_id": a})
        self.db.commit()
        return sid

    def test_load_and_render(self):
        sid = self._session()
        ref = self.adapter.locate_by_id(sid)
        self.assertIsNotNone(ref)
        self.assertEqual(ref.short_id, "djwvg6w".rjust(8, "v")[-8:].lower() if False else sid.split("_", 1)[1][-8:].lower())
        self.assertIsNone(ref.title, "default titles are untitled")
        ref.extra["repo_root"] = self.repo
        sess = self.adapter.load(ref)
        kinds = [e.kind for e in sess.events]
        self.assertEqual(kinds.count("user"), 1)
        self.assertIn("thinking", kinds)
        tools = [e.tool for e in sess.events if e.tool]
        self.assertEqual([t.kind for t in tools], ["shell", "read", "edit", "shell", "mcp"])
        self.assertTrue(tools[3].is_error)
        self.assertEqual(tools[3].output, "exit 1")
        self.assertEqual(sess.files_touched(), ["src/p.py"])
        self.assertEqual(sess.tokens["input"], 1000)
        self.assertEqual(sess.agent_version, "1.18.23")
        self.assertEqual(kinds.count("compaction"), 1)
        segs, stats = render_session(sess, Redactor())
        text = "\n".join(segs)
        self.assertIn("build the parser please", text)
        self.assertIn("total 0", text)
        self.assertNotIn("SECRETVALUE", text)
        self.assertNotIn("QUERYROWS", text)
        self.assertNotIn("PRIVATE THOUGHT", text)
        self.assertIn("**Error.**", text)
        self.assertIn("Context compacted here", text)
        self.assertEqual(stats["tool_calls"], 5)

    def test_child_session_paths_and_candidates(self):
        sid = self._session()
        self.db.session("ses_child000000000000000child1", self.repo, parent_id=sid, title="subtask")
        m = self.db.message("ses_child000000000000000child1", "assistant")
        self.db.tool("ses_child000000000000000child1", m, "write", {"filePath": os.path.join(self.repo, "docs", "x.md"), "content": "hi"}, output="ok")
        self.db.commit()
        cands = self.adapter.candidates(self.repo, limit=10)
        self.assertEqual([c.session_id for c in cands], [sid], "child sessions are not candidates")
        sess = self.adapter.load(cands[0])
        self.assertIn("docs/x.md", sess.files_touched())
        self.assertEqual(sess.subagent_files, 1)

    def test_self_ref_running_call_and_cli_export(self):
        sid = self._session()
        other = self.db.session("ses_other0000000000000000other1", self.repo)
        m = self.db.message(other, "user")
        self.db.part(other, m, {"type": "text", "text": "parallel work"})
        live = self.db.message(sid, "assistant")
        self.db.tool(sid, live, "bash", {"command": "bash ~/.agents/skills/agent-sessions/scripts/sessions.sh locate --agent auto"}, status="running")
        self.db.commit()
        res = resolve("auto", None, self.repo)
        self.assertEqual((res.ref.session_id if res.ref else None), sid, res.how)
        self.assertEqual(res.how, "self-referencing locate call")
        code, text = run(["locate", "--agent", "opencode", "--project-dir", self.repo])
        self.assertEqual(code, 0, text)
        self.assertIn("AGENT: opencode", text)
        self.assertIn("FOUND_BY: self-referencing locate call", text)
        out = Path(self.repo, ".claude", "sessions", "tmp")
        out.mkdir(parents=True)
        (out / "summary.md").write_text("---\ntitle: \"Parser via OpenCode\"\n---\n\n## Goal\nx\n", encoding="utf-8")
        code, text = run(["export", "--agent", "opencode", "--source", f"sqlite:{os.environ['OPENCODE_DB']}#{sid}", "--out", str(out)])
        self.assertEqual(code, 0, text)
        final = next(Path(self.repo, ".claude", "sessions").glob("*_parser-via-opencode_*"))
        meta = json.loads((final / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["agent"], "opencode")
        self.assertEqual(meta["short_id"], sid.split("_", 1)[1][-8:].lower())
        self.assertIn("still running", (final / "transcript.md").read_text(encoding="utf-8"))

    def test_locked_db_falls_back_to_copy(self):
        sid = self._session()
        self.db.close()
        locker = sqlite3.connect(os.environ["OPENCODE_DB"], isolation_level=None)
        locker.execute("BEGIN EXCLUSIVE")
        try:
            ref = self.adapter.locate_by_id(sid)
            self.assertIsNotNone(ref)
            sess = self.adapter.load(ref)
            self.assertGreater(len(sess.events), 3)
        finally:
            locker.execute("ROLLBACK")
            locker.close()

    def test_migration_warning(self):
        sid = self.db.session("ses_new00000000000000000000new1", self.repo)
        self.db.con.execute("INSERT INTO session_message (id, session_id, type, seq, time_created, time_updated, data) VALUES (?,?,?,?,?,?,?)",
                            ("sm1", sid, "user", 1, 1, 1, "{}"))
        self.db.commit()
        sess = self.adapter.load(self.adapter.locate_by_id(sid))
        self.assertTrue(any("session_message" in w for w in sess.warnings))


# --------------------------------------------------------------------------- Gemini fixture


class TestGemini(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="as-"))
        self.repo = init_repo(os.path.join(self.tmp, "repo"))
        self.iso = Isolated(self.tmp).__enter__()
        self.chats = os.path.join(os.environ["GEMINI_CLI_HOME"], "tmp", "abc123hash", "chats")
        os.makedirs(self.chats)
        self.adapter = GeminiAdapter()

    def tearDown(self):
        self.iso.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, sid, records):
        path = os.path.join(self.chats, f"session-2026-09-22T06-00-{sid[:8]}.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
        return path

    def test_load_rewind_and_render(self):
        sid = str(uuid.uuid4())
        ts = T0.isoformat()
        recs = [
            {"sessionId": sid, "projectHash": "abc123hash", "startTime": ts, "lastUpdated": ts, "kind": "main", "directories": [self.repo], "messages": []},
            {"id": "m1", "timestamp": ts, "type": "user", "content": [{"text": "hello gemini"}]},
            {"id": "m2", "timestamp": ts, "type": "gemini", "content": "Sure.", "thoughts": [{"subject": "Plan", "description": "think first", "timestamp": ts}],
             "tokens": {"input": 10, "output": 5, "total": 15},
             "toolCalls": [
                 {"id": "t1", "name": "run_shell_command", "args": {"command": "ls"}, "result": [{"text": "a.py"}], "status": "success"},
                 {"id": "t2", "name": "write_file", "args": {"file_path": os.path.join(self.repo, "notes.md"), "content": "x"}, "result": {"output": "ok"}, "status": "success"},
                 {"id": "t3", "name": "read_file", "args": {"path": "secret.txt"}, "result": [{"text": "HIDDEN CONTENT"}], "status": "success"},
             ]},
            {"id": "m3", "timestamp": ts, "type": "user", "content": "abandoned question"},
            {"id": "m4", "timestamp": ts, "type": "gemini", "content": "ABANDONED ANSWER", "tokens": {"input": 1, "output": 1, "total": 2}},
            {"$rewindTo": "m2"},
            {"$set": {"summary": "Gemini session about notes"}},
            {"id": "m5", "timestamp": ts, "type": "user", "content": "real follow up"},
            {"id": "m6", "timestamp": ts, "type": "gemini", "content": "REAL ANSWER", "tokens": {"input": 2, "output": 2, "total": 4}},
        ]
        self._write(sid, recs)
        ref = self.adapter.locate_by_id(sid)
        self.assertIsNotNone(ref)
        self.assertEqual(ref.short_id, sid.replace("-", "")[:8])
        ref.extra["repo_root"] = self.repo
        sess = self.adapter.load(ref)
        self.assertEqual(sess.title, "Gemini session about notes")
        kinds = [e.kind for e in sess.events]
        self.assertEqual(kinds.count("user"), 2)
        self.assertIn("thinking", kinds)
        tools = [e.tool for e in sess.events if e.tool]
        self.assertEqual([t.kind for t in tools], ["shell", "write", "read"])
        self.assertEqual(sess.files_touched(), ["notes.md"])
        self.assertEqual(sess.tokens["total"], 19)
        self.assertTrue(any("experimental" in w for w in sess.warnings))
        segs, _ = render_session(sess, Redactor())
        text = "\n".join(segs)
        self.assertIn("REAL ANSWER", text)
        self.assertNotIn("ABANDONED", text)
        self.assertNotIn("HIDDEN CONTENT", text)
        self.assertEqual([c.session_id for c in self.adapter.candidates(self.repo)], [sid])
        code, out = run(["locate", "--agent", "gemini-cli", "--session-id", sid, "--project-dir", self.repo])
        self.assertEqual(code, 0, out)
        self.assertIn("experimental", out)


class TestResolveAcrossAgents(unittest.TestCase):
    def test_no_stores_is_not_found(self):
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="as-"))
        try:
            repo = init_repo(os.path.join(tmp, "repo"))
            with Isolated(tmp):
                res = resolve("auto", None, repo)
                self.assertIsNone(res.ref)
                self.assertEqual(res.how, "not found")
                code, text = run(["locate", "--project-dir", repo])
                self.assertEqual(code, 1)
                self.assertIn("no session found", text)
                code, text = run(["doctor", "--project-dir", repo])
                self.assertEqual(code, 0, text)
                for name in ("Claude Code", "Codex CLI", "OpenCode", "Gemini CLI"):
                    self.assertIn(f"{name} (", text)
                    self.assertIn("not found", text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_registry(self):
        self.assertEqual(list(REGISTRY), ["claude-code", "codex", "opencode", "gemini-cli"])


if __name__ == "__main__":
    unittest.main()
