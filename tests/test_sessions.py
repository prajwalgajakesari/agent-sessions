"""Tests for scripts/sessions.py. Standard library only: python3 -m unittest discover tests"""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sessions.py"
SHIM = ROOT / "scripts" / "sessions.sh"

spec = importlib.util.spec_from_file_location("sessions", SCRIPT)
S = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(S)

UTC = dt.timezone.utc


def run(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = S.main(argv)
    return code, buf.getvalue()


def git(args, cwd, check=True, stdin=None):
    p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, input=stdin)
    if check and p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {p.stderr}")
    return p.stdout.strip()


def init_repo(path, initial=True):
    os.makedirs(path, exist_ok=True)
    git(["init", "-q", "-b", "main"], path)
    git(["config", "user.name", "Test User"], path)
    git(["config", "user.email", "test@example.com"], path)
    git(["config", "commit.gpgsign", "false"], path)
    if initial:
        Path(path, "README.md").write_text("# demo\n", encoding="utf-8")
        git(["add", "README.md"], path)
        git(["commit", "-q", "-m", "init"], path)
    return path


def write_session_files(repo, name="2026-09-21_demo_test-user_abcdef12", sid="abcdef12-0000-0000-0000-000000000000", title="Demo"):
    d = Path(repo, S.SESSIONS_REL, name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.md").write_text(f"---\ntitle: \"{title}\"\n---\n\n## Goal\nx\n", encoding="utf-8")
    (d / "transcript.md").write_text("# Demo\n", encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({
        "session_id": sid, "title": title, "handle": "test-user", "branch": "main",
        "started": "2026-09-21T10:00:00+00:00", "pushed_at": "2026-09-21T11:00:00+00:00",
        "outcome": "done", "tags": ["demo"], "transcript_files": ["transcript.md"],
    }, indent=2), encoding="utf-8")
    return str(d)


class Builder:
    """Synthesises Claude Code JSONL with the record shapes seen in v2.1.259."""

    def __init__(self, sid=None, cwd="/work/repo"):
        self.sid = sid or str(uuid.uuid4())
        self.cwd = cwd
        self.records = []
        self.last = None
        self.t = dt.datetime(2026, 9, 21, 10, 0, tzinfo=UTC)

    def _base(self, type_, parent):
        self.t += dt.timedelta(seconds=7)
        return {
            "type": type_, "uuid": str(uuid.uuid4()), "parentUuid": parent, "isSidechain": False,
            "timestamp": self.t.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "sessionId": self.sid,
            "cwd": self.cwd, "version": "2.1.259", "gitBranch": "main",
        }

    def _add(self, rec):
        self.records.append(rec)
        self.last = rec["uuid"]
        return rec

    def user(self, text, parent=..., **extra):
        rec = self._base("user", self.last if parent is ... else parent)
        rec["message"] = {"role": "user", "content": text}
        rec.update(extra)
        return self._add(rec)

    def assistant(self, blocks, parent=...):
        rec = self._base("assistant", self.last if parent is ... else parent)
        rec["message"] = {"role": "assistant", "content": blocks, "model": "claude-test"}
        return self._add(rec)

    def text(self, t, parent=...):
        return self.assistant([{"type": "text", "text": t}], parent)

    def thinking(self, t):
        return self.assistant([{"type": "thinking", "thinking": t, "signature": "x"}])

    def tool_use(self, name, inp, tool_id=None):
        tid = tool_id or "toolu_" + uuid.uuid4().hex[:12]
        self.assistant([{"type": "tool_use", "id": tid, "name": name, "input": inp}])
        return tid

    def tool_result(self, tid, content, is_error=False, tur=None):
        rec = self._base("user", self.last)
        rec["message"] = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": is_error}]}
        if tur is not None:
            rec["toolUseResult"] = tur
        return self._add(rec)

    def compact(self, summary="Summary of the earlier work for the model"):
        boundary = self._base("system", None)
        boundary.update({"subtype": "compact_boundary", "logicalParentUuid": self.last, "content": "Conversation compacted",
                         "compactMetadata": {"trigger": "manual", "preTokens": 100000, "postTokens": 5000}})
        self._add(boundary)
        rec = self._base("user", boundary["uuid"])
        rec["message"] = {"role": "user", "content": summary}
        rec["isCompactSummary"] = True
        return self._add(rec)

    def title(self, t):
        self.records.append({"type": "ai-title", "aiTitle": t, "sessionId": self.sid})

    def noise(self):
        self.records.append({"type": "attachment", "uuid": str(uuid.uuid4()), "parentUuid": self.last, "attachment": {"type": "total_tokens_reminder"}})
        self.records.append({"type": "last-prompt", "lastPrompt": "x", "leafUuid": self.last, "sessionId": self.sid})

    def write(self, dirpath):
        path = Path(dirpath) / f"{self.sid}.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for rec in self.records:
                fh.write(json.dumps(rec) + "\n")
        return str(path)

    def write_subagent(self, dirpath, records):
        sub = Path(dirpath) / self.sid / "subagents"
        sub.mkdir(parents=True, exist_ok=True)
        with open(sub / "agent-a1b2c3.jsonl", "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")


class ExportBase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cs-"))
        self.repo = init_repo(os.path.join(self.tmp, "repo"))
        self.transcripts = os.path.join(self.tmp, "transcripts")
        os.makedirs(self.transcripts)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def export(self, builder, summary=None, extra=None, name="2026-09-21_demo_test-user_x"):
        path = builder.write(self.transcripts)
        out = Path(self.repo, S.SESSIONS_REL, name)
        out.mkdir(parents=True, exist_ok=True)
        (out / "summary.md").write_text(summary or (ROOT / "templates" / "summary.md").read_text(encoding="utf-8"), encoding="utf-8")
        code, text = run(["export", "--transcript", path, "--out", str(out)] + (extra or []))
        transcript = ""
        for f in sorted(out.glob("transcript*.md")):
            transcript += f.read_text(encoding="utf-8")
        meta = json.loads((out / "meta.json").read_text(encoding="utf-8")) if (out / "meta.json").exists() else None
        return code, text, transcript, meta, out


class TestRendering(ExportBase):
    def test_active_path_skips_abandoned_branch(self):
        b = Builder(cwd=self.repo)
        b.title("Branchy session")
        b.user("hello")
        fork = b.text("hi there")["uuid"]
        b.user("abandoned question", parent=fork)
        b.text("ABANDONED ANSWER")
        b.user("real question", parent=fork)
        b.text("REAL ANSWER")
        b.noise()
        code, text, transcript, meta, _ = self.export(b)
        self.assertEqual(code, 0, text)
        self.assertIn("REAL ANSWER", transcript)
        self.assertNotIn("ABANDONED", transcript)
        self.assertIn("# Branchy session", transcript)

    def test_compaction_rendered_once_with_marker(self):
        b = Builder(cwd=self.repo)
        b.user("first ask")
        b.text("FIRST REPLY")
        b.compact("MODEL SUMMARY TEXT")
        b.user("after compaction")
        b.text("SECOND REPLY")
        code, text, transcript, meta, _ = self.export(b)
        self.assertEqual(code, 0, text)
        self.assertEqual(transcript.count("FIRST REPLY"), 1)
        self.assertIn("Context compacted here", transcript)
        self.assertIn("MODEL SUMMARY TEXT", transcript)
        self.assertIn("SECOND REPLY", transcript)
        self.assertEqual(meta["stats"]["compactions"], 1)

    def test_stops_at_push_command(self):
        b = Builder(cwd=self.repo)
        b.user("do the work")
        b.text("WORK DONE")
        b.user("<command-name>/sessions:push</command-name>\n<command-message>push</command-message>\n<command-args>--yes</command-args>")
        b.text("WRITING SUMMARY NOW")
        code, text, transcript, meta, _ = self.export(b)
        self.assertEqual(code, 0, text)
        self.assertIn("WORK DONE", transcript)
        self.assertNotIn("WRITING SUMMARY NOW", transcript)
        self.assertEqual(meta["stats"]["stopped_at_push"], 1)

    def test_other_commands_and_reminders(self):
        b = Builder(cwd=self.repo)
        b.user("<command-name>/compact</command-name>\n<command-message>compact</command-message>\n<command-args></command-args>")
        b.user("<system-reminder>SECRET REMINDER</system-reminder>visible ask")
        b.user("meta thing", isMeta=True)
        b.text("ok")
        code, text, transcript, meta, _ = self.export(b)
        self.assertEqual(code, 0, text)
        self.assertIn("Ran `/compact`", transcript)
        self.assertIn("visible ask", transcript)
        self.assertNotIn("SECRET REMINDER", transcript)
        self.assertNotIn("meta thing", transcript)

    def test_result_policy(self):
        b = Builder(cwd=self.repo)
        b.user("run things")
        big = "\n".join(f"line {i}" for i in range(1, 101))
        tid = b.tool_use("Bash", {"command": "seq 1 100", "description": "count"})
        b.tool_result(tid, big)
        tid2 = b.tool_use("Read", {"file_path": "/work/repo/data.csv"})
        b.tool_result(tid2, "ROW1,SECRETVALUE\nROW2,X")
        tid3 = b.tool_use("mcp__fabric__read_data", {"query": "select 1"})
        b.tool_result(tid3, "QUERYROWS 1 2 3")
        b.text("done")
        code, text, transcript, meta, out = self.export(b)
        self.assertEqual(code, 0, text)
        self.assertIn("line 1\n", transcript)
        self.assertNotIn("line 90", transcript)
        self.assertIn("more lines omitted", transcript)
        self.assertNotIn("SECRETVALUE", transcript)
        self.assertNotIn("QUERYROWS", transcript)
        self.assertIn("result omitted", transcript)
        self.assertIn("MCP fabric: read_data", transcript)
        shutil.rmtree(out)
        code, text, transcript, meta, _ = self.export(b, extra=["--include-results"])
        self.assertEqual(code, 0, text)
        self.assertIn("line 90", transcript)
        self.assertIn("QUERYROWS", transcript)

    def test_sensitive_file_withheld(self):
        b = Builder(cwd=self.repo)
        b.user("check env")
        tid = b.tool_use("Read", {"file_path": "/work/repo/.env"})
        b.tool_result(tid, "DB_PASSWORD=plaintextvalue")
        tid2 = b.tool_use("Bash", {"command": "cat config/local.settings.json"})
        b.tool_result(tid2, '{"ConnectionString": "Server=x;Password=Zzz99999!"}')
        b.text("done")
        code, text, transcript, meta, _ = self.export(b)
        self.assertEqual(code, 0, text)
        self.assertNotIn("plaintextvalue", transcript)
        self.assertNotIn("Zzz99999", transcript)
        self.assertIn("withheld", transcript)
        self.assertEqual(meta["stats"]["withheld_results"], 2)

    def test_thinking_flag(self):
        b = Builder(cwd=self.repo)
        b.user("q")
        b.thinking("PRIVATE THOUGHTS")
        b.text("a")
        code, text, transcript, _, out = self.export(b)
        self.assertNotIn("PRIVATE THOUGHTS", transcript)
        shutil.rmtree(out)
        code, text, transcript, _, _ = self.export(b, extra=["--include-thinking"])
        self.assertIn("PRIVATE THOUGHTS", transcript)

    def test_files_touched_includes_subagents(self):
        b = Builder(cwd=self.repo)
        b.user("delegate")
        tid = b.tool_use("Agent", {"description": "Fix the parser", "prompt": "long prompt", "subagent_type": "general-purpose"})
        b.tool_result(tid, "done by agent")
        b.text("delegated")
        sub = Builder(sid="agent-sub", cwd=self.repo)
        sub.user("subagent start")
        stid = sub.tool_use("Edit", {"file_path": os.path.join(self.repo, "src", "parser.py"), "old_string": "a", "new_string": "b"})
        sub.tool_result(stid, "ok", tur={"filePath": os.path.join(self.repo, "src", "parser.py"), "structuredPatch": []})
        b.write_subagent(self.transcripts, sub.records)
        code, text, transcript, meta, _ = self.export(b)
        self.assertEqual(code, 0, text)
        self.assertIn("src/parser.py", meta["files_touched"])
        self.assertIn("Agent: Fix the parser", transcript)
        self.assertNotIn("subagent start", transcript)

    def test_frontmatter_filled_and_tags(self):
        b = Builder(cwd=self.repo)
        b.title("Reconcile fabric tables")
        b.user("q")
        b.text("a")
        summary = (ROOT / "templates" / "summary.md").read_text(encoding="utf-8")
        summary = summary.replace("[<area>, <kind-of-work>]", "[fabric, Data Modelling]").replace(
            '"<one sentence: what is true now that was not before>"', '"Tables reconciled"')
        code, text, transcript, meta, out = self.export(b, summary=summary)
        self.assertEqual(code, 0, text)
        fm, _ = S.parse_frontmatter((out / "summary.md").read_text(encoding="utf-8"))
        self.assertEqual(fm["title"], "Reconcile fabric tables")
        self.assertEqual(fm["session_id"], b.sid)
        self.assertEqual(fm["handle"], "test-user")
        self.assertEqual(fm["branch"], "main")
        self.assertEqual(fm["tags"], ["fabric", "data-modelling"])
        self.assertEqual(meta["outcome"], "Tables reconciled")
        self.assertEqual(meta["title"], "Reconcile fabric tables")

    def test_summary_cap(self):
        b = Builder(cwd=self.repo)
        b.user("q")
        b.text("a")
        summary = "---\ntitle: \"Big\"\n---\n" + "\n".join(f"line {i}" for i in range(200))
        code, text, *_ = self.export(b, summary=summary)
        self.assertEqual(code, 1)
        self.assertIn("exceeds the cap", text)

    def test_zero_messages_is_error(self):
        b = Builder(cwd=self.repo)
        b.noise()
        code, text, *_ = self.export(b)
        self.assertEqual(code, 1)
        self.assertIn("STATUS: error", text)

    def test_large_transcript_splits(self):
        b = Builder(cwd=self.repo)
        blob = ("lorem ipsum " * 5000).strip()  # ~60 KB
        for i in range(24):
            b.user(f"chunk {i} " + blob)
            b.text(f"ack {i}")
        code, text, transcript, meta, out = self.export(b)
        self.assertEqual(code, 0, text)
        parts = sorted(p.name for p in out.glob("transcript*.md"))
        self.assertGreaterEqual(len(parts), 2, parts)
        self.assertNotIn("transcript.md", parts)
        self.assertEqual(meta["transcript_files"], parts)
        self.assertIn("ack 23", transcript)

    def test_redaction_in_transcript_and_report(self):
        b = Builder(cwd=self.repo)
        b.user("here is the key AKIAABCDEFGHIJKLMNOP and token ghp_abcdefghijklmnopqrstuvwxyz0123456789ABCD")
        tid = b.tool_use("Bash", {"command": "curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz' https://user:p4ssw0rd@host/x"})
        b.tool_result(tid, "ok")
        b.text("Stored under password=SuperSecret123 for now")
        code, text, transcript, meta, _ = self.export(b)
        self.assertEqual(code, 0, text)
        for leaked in ("AKIAABCDEFGHIJKLMNOP", "ghp_abcdefghijklmnopqrstuvwxyz0123456789ABCD", "abcdefghijklmnopqrstuvwxyz", "p4ssw0rd", "SuperSecret123"):
            self.assertNotIn(leaked, transcript, leaked)
        self.assertIn("[REDACTED:aws-access-key]", transcript)
        self.assertIn("REDACTED_HIGH: 4", text)
        self.assertIn("REDACTED_REVIEW: 1", text)
        self.assertIn("Review these lines", text)


class TestRedactor(unittest.TestCase):
    def redact(self, s):
        r = S.Redactor()
        return r.redact(s), r

    def test_high_patterns(self):
        cases = {
            "AKIAABCDEFGHIJKLMNOP": "aws-access-key",
            "xoxb-1234567890-abcdefghij": "slack-token",
            "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789": "sk-api-key",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijklmnop": "jwt",
            "abc8Q~" + "x" * 34: "entra-client-secret",
            "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=" + "A" * 86 + "==;EndpointSuffix=core.windows.net": "azure-account-key",
            "https://acct.blob.core.windows.net/c/f.parquet?sv=2020&sig=" + "Q" * 44 + "%3D": "sas-signature",
            "export AZURE_DEVOPS_EXT_PAT=abcdefghijklmnopqrstuvwxyz1234": "azure-devops-pat",
            "git clone https://alice:hunter2pw@dev.azure.com/org/_git/repo": "url-userinfo",
        }
        for text, name in cases.items():
            out, r = self.redact(text)
            self.assertIn(f"[REDACTED:{name}]", out, text)
            self.assertEqual(r.hits[name], 1, text)
        self.assertNotIn("A" * 86, self.redact(list(cases)[5])[0])

    def test_pem_block(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nABCD\n-----END RSA PRIVATE KEY-----\nafter"
        out, r = self.redact(pem)
        self.assertEqual(out, "[REDACTED:private-key]\nafter")
        self.assertEqual(r.hits["private-key"], 1)

    def test_review_patterns_value_only(self):
        out, r = self.redact("Server=db;Database=x;User Id=u;Password=Abc12345!;Encrypt=true")
        self.assertIn("Password=[REDACTED:password-kv];Encrypt=true", out)
        out, r = self.redact("sqlcmd -S host -U sa -P Pa55word!")
        self.assertIn("-P [REDACTED:sqlcmd-password]", out)
        out, r = self.redact("az login --service-principal -u app -p SuperSecretValue123 --tenant t")
        self.assertIn("-p [REDACTED:az-login-password]", out)
        self.assertEqual(len(r.review), 1)

    def test_false_positives_untouched(self):
        for text in (
            "mkdir -p some/folder/path",
            "client_secret=123e4567-e89b-12d3-a456-426614174000",
            "tenant id 72f988bf-86f1-41af-91ab-2d7cd011db47",
            "password=${DB_PASSWORD}",
            "api_key=<your-api-key>",
            "max_tokens=4096",
            "the token count was 12345678",
        ):
            out, r = self.redact(text)
            self.assertEqual(out, text)
            self.assertEqual(sum(r.hits.values()), 0, text)

    def test_residual_scan(self):
        self.assertEqual(S.Redactor.residual_high("nothing here"), [])
        self.assertIn("aws-access-key", S.Redactor.residual_high("x AKIAABCDEFGHIJKLMNOP y"))
        out, _ = self.redact("x AKIAABCDEFGHIJKLMNOP y Bearer abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(S.Redactor.residual_high(out), [])

    def test_sensitive_file_detection(self):
        self.assertTrue(S.mentions_sensitive_file("cat .env.local"))
        self.assertTrue(S.mentions_sensitive_file("/home/u/.azure/accessTokens.json"))
        self.assertTrue(S.mentions_sensitive_file("Read C:\\src\\app\\appsettings.Development.json"))
        self.assertFalse(S.mentions_sensitive_file("cat README.md src/main.py"))
        self.assertFalse(S.mentions_sensitive_file("environment.yml"))


class TestLocate(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cs-"))
        self.repo = init_repo(os.path.join(self.tmp, "repo"))
        self.cfg = os.path.join(self.tmp, "cfg")
        self.proj = os.path.join(self.cfg, "projects", "-work-repo")
        os.makedirs(self.proj)
        self.old = dict(os.environ)
        os.environ["CLAUDE_CONFIG_DIR"] = self.cfg
        for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_PROJECT_DIR"):
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_locate_by_id_and_outline(self):
        b = Builder(cwd=self.repo)
        b.title("Locate me")
        b.user("first prompt about parsers")
        tid = b.tool_use("Edit", {"file_path": os.path.join(self.repo, "a.py"), "old_string": "x", "new_string": "y"})
        b.tool_result(tid, "ok")
        b.text("done")
        b.write(self.proj)
        code, text = run(["locate", "--session-id", b.sid, "--project-dir", self.repo])
        self.assertEqual(code, 0, text)
        self.assertIn("FOUND_BY: session id", text)
        self.assertIn("TITLE_DEFAULT: Locate me", text)
        self.assertIn("HANDLE: test-user", text)
        self.assertIn("first prompt about parsers", text)
        self.assertIn("  - a.py", text)
        self.assertIn(f"{S.SESSIONS_REL}/2026-09-21_locate-me_test-user_{b.sid[:8]}", text)

    def test_locate_falls_back_to_cwd_match(self):
        other = Builder(cwd="/elsewhere")
        other.user("x")
        other.text("y")
        other.write(self.proj)
        mine = Builder(cwd=self.repo)
        mine.user("x")
        mine.text("y")
        path = mine.write(self.proj)
        os.utime(path, None)
        code, text = run(["locate", "--project-dir", self.repo])
        self.assertEqual(code, 0, text)
        self.assertIn("newest transcript for this repo", text)
        self.assertIn(f"SESSION_ID: {mine.sid}", text)

    def test_locate_blocked_when_ignored(self):
        Path(self.repo, ".gitignore").write_text(".claude/\n", encoding="utf-8")
        b = Builder(cwd=self.repo)
        b.user("x")
        b.text("y")
        b.write(self.proj)
        code, text = run(["locate", "--session-id", b.sid, "--project-dir", self.repo])
        self.assertEqual(code, 1)
        self.assertIn("/sessions:init", text)
        self.assertIn("STATUS: error", text)

    def test_locate_missing_transcript(self):
        code, text = run(["locate", "--session-id", "00000000-0000-0000-0000-000000000000", "--project-dir", self.repo])
        self.assertEqual(code, 1)
        self.assertIn("transcript not found", text)


class TestCommit(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cs-"))
        self.repo = init_repo(os.path.join(self.tmp, "repo"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_commit_leaves_index_and_worktree_alone(self):
        d = write_session_files(self.repo)
        Path(self.repo, "other.txt").write_text("staged\n", encoding="utf-8")
        git(["add", "other.txt"], self.repo)
        Path(self.repo, "README.md").write_text("# demo\nmodified\n", encoding="utf-8")
        head_before = git(["rev-parse", "HEAD"], self.repo)
        code, text = run(["commit", "--dir", d])
        self.assertEqual(code, 0, text)
        self.assertIn("ACTION: add", text)
        head = git(["rev-parse", "HEAD"], self.repo)
        self.assertNotEqual(head, head_before)
        tree = git(["ls-tree", "-r", "--name-only", "HEAD"], self.repo)
        self.assertIn(".claude/sessions/2026-09-21_demo_test-user_abcdef12/summary.md", tree)
        self.assertNotIn("other.txt", tree)
        self.assertEqual(git(["diff", "--cached", "--name-only"], self.repo), "other.txt")
        self.assertEqual(git(["diff", "--name-only"], self.repo), "README.md")
        status = git(["status", "--porcelain"], self.repo)
        self.assertNotIn(".claude/sessions", status)
        msg = git(["log", "-1", "--format=%B"], self.repo)
        self.assertIn("Claude-Session: abcdef12-0000-0000-0000-000000000000", msg)
        code, text = run(["commit", "--dir", d])
        self.assertEqual(code, 1)
        self.assertIn("nothing to commit", text)
        Path(d, "summary.md").write_text("---\ntitle: \"Demo\"\n---\nrevised\n", encoding="utf-8")
        code, text = run(["commit", "--dir", d])
        self.assertEqual(code, 0, text)
        self.assertIn("ACTION: update", text)

    def test_commit_during_merge_conflict(self):
        git(["checkout", "-q", "-b", "feature"], self.repo)
        Path(self.repo, "README.md").write_text("feature\n", encoding="utf-8")
        git(["commit", "-q", "-am", "feature"], self.repo)
        git(["checkout", "-q", "main"], self.repo)
        Path(self.repo, "README.md").write_text("main\n", encoding="utf-8")
        git(["commit", "-q", "-am", "main"], self.repo)
        git(["merge", "feature"], self.repo, check=False)
        self.assertTrue(Path(self.repo, ".git", "MERGE_HEAD").exists())
        d = write_session_files(self.repo)
        code, text = run(["commit", "--dir", d])
        self.assertEqual(code, 0, text)
        self.assertTrue(Path(self.repo, ".git", "MERGE_HEAD").exists(), "merge state must survive")
        self.assertIn("summary.md", git(["ls-tree", "-r", "--name-only", "HEAD"], self.repo))

    def test_push_to_bare_and_to_new_branch(self):
        bare = os.path.join(self.tmp, "remote.git")
        git(["init", "-q", "--bare", "-b", "main", bare], self.tmp)
        git(["remote", "add", "origin", bare], self.repo)
        git(["push", "-q", "-u", "origin", "main"], self.repo)
        d = write_session_files(self.repo)
        code, text = run(["commit", "--dir", d, "--push"])
        self.assertEqual(code, 0, text)
        self.assertIn("PUSHED: yes", text)
        self.assertEqual(git(["rev-parse", "main"], bare), git(["rev-parse", "HEAD"], self.repo))
        Path(d, "summary.md").write_text("---\ntitle: \"Demo\"\n---\nv2\n", encoding="utf-8")
        code, text = run(["commit", "--dir", d, "--push", "--branch", "sessions/test-user/2026-09-21"])
        self.assertEqual(code, 0, text)
        self.assertEqual(git(["rev-parse", "HEAD"], self.repo), git(["rev-parse", "main"], bare), "current branch must not move")
        self.assertTrue(git(["rev-parse", "--verify", "refs/heads/sessions/test-user/2026-09-21"], bare))

    def test_detached_head_needs_branch(self):
        git(["checkout", "-q", "--detach"], self.repo)
        d = write_session_files(self.repo)
        code, text = run(["commit", "--dir", d])
        self.assertEqual(code, 1)
        self.assertIn("detached HEAD", text)


class TestList(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cs-"))
        self.bare = os.path.join(self.tmp, "remote.git")
        git(["init", "-q", "--bare", "-b", "main", self.bare], self.tmp)
        self.a = init_repo(os.path.join(self.tmp, "a"))
        git(["remote", "add", "origin", self.bare], self.a)
        git(["push", "-q", "-u", "origin", "main"], self.a)
        git(["clone", "-q", self.bare, "b"], self.tmp)
        self.b = os.path.join(self.tmp, "b")
        git(["config", "user.name", "Other"], self.b)
        git(["config", "user.email", "o@example.com"], self.b)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_sees_default_branch_after_fetch(self):
        d = write_session_files(self.a, title="Shared thing")
        code, text = run(["commit", "--dir", d, "--push"])
        self.assertEqual(code, 0, text)
        code, text = run(["list", "--project-dir", self.b])
        self.assertEqual(code, 0, text)
        self.assertIn("FETCH: ok", text)
        self.assertIn("COUNT: 1", text)
        self.assertIn("Shared thing", text)
        self.assertIn("origin/main:.claude/sessions/2026-09-21_demo_test-user_abcdef12", text)
        self.assertEqual(git(["status", "--porcelain"], self.b), "")

    def test_all_branches_flag(self):
        git(["checkout", "-q", "-b", "feature/x"], self.a)
        d = write_session_files(self.a, name="2026-09-22_feat_test-user_feedfeed", sid="feedfeed-0000-0000-0000-000000000000", title="Feature work")
        code, text = run(["commit", "--dir", d, "--push"])
        self.assertEqual(code, 0, text)
        code, text = run(["list", "--project-dir", self.b])
        self.assertIn("COUNT: 0", text)
        code, text = run(["list", "--project-dir", self.b, "--all-branches"])
        self.assertIn("COUNT: 1", text)
        self.assertIn("origin/feature/x:", text)

    def test_list_without_remote(self):
        solo = init_repo(os.path.join(self.tmp, "solo"))
        write_session_files(solo)
        git(["add", "."], solo)
        git(["commit", "-q", "-m", "s"], solo)
        code, text = run(["list", "--project-dir", solo])
        self.assertEqual(code, 0, text)
        self.assertIn("no remote", text)
        self.assertIn("COUNT: 1", text)


class TestInit(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cs-"))
        self.repo = init_repo(os.path.join(self.tmp, "repo"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_init_rewrites_gitignore_and_merges_settings(self):
        Path(self.repo, ".gitignore").write_text(".claude/\nnode_modules/\n", encoding="utf-8")
        Path(self.repo, ".claude").mkdir()
        Path(self.repo, ".claude", "settings.json").write_text(json.dumps({
            "_comment": ["existing note"],
            "permissions": {"allow": ["Read"]},
            "enabledPlugins": {"other@market": True},
        }, indent=2), encoding="utf-8")
        git(["add", ".gitignore"], self.repo)
        git(["commit", "-q", "-m", "ignore"], self.repo)
        code, text = run(["init", "--dry-run", "--project-dir", self.repo])
        self.assertEqual(code, 0, text)
        self.assertIn("MODE: dry-run", text)
        self.assertEqual(Path(self.repo, ".gitignore").read_text(encoding="utf-8"), ".claude/\nnode_modules/\n")
        code, text = run(["init", "--commit", "--project-dir", self.repo])
        self.assertEqual(code, 0, text)
        gi = Path(self.repo, ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".claude/*", gi)
        self.assertIn("!.claude/sessions/", gi)
        self.assertIn("!.claude/settings.json", gi)
        self.assertNotIn("\n.claude/\n", "\n" + gi)
        self.assertIsNone(S.path_is_ignored(self.repo, ".claude/sessions/x/summary.md"))
        self.assertIsNone(S.path_is_ignored(self.repo, ".claude/settings.json"))
        self.assertIsNotNone(S.path_is_ignored(self.repo, ".claude/other.json"))
        settings = json.loads(Path(self.repo, ".claude", "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["extraKnownMarketplaces"]["claude-sessions"]["source"]["repo"], "prajwalgajakesari/claude-sessions")
        self.assertTrue(settings["enabledPlugins"]["sessions@claude-sessions"])
        self.assertTrue(settings["enabledPlugins"]["other@market"])
        self.assertEqual(settings["permissions"], {"allow": ["Read"]})
        self.assertEqual(len(settings["_comment"]), 2)
        self.assertTrue(Path(self.repo, ".claude", "sessions", "README.md").exists())
        self.assertTrue(Path(self.repo, ".claude", "sessions", ".gitattributes").exists())
        tree = git(["ls-tree", "-r", "--name-only", "HEAD"], self.repo)
        for p in (".gitignore", ".claude/settings.json", ".claude/sessions/README.md", ".claude/sessions/.gitattributes"):
            self.assertIn(p, tree)
        self.assertEqual(git(["status", "--porcelain"], self.repo), "")
        code, text = run(["init", "--commit", "--project-dir", self.repo])
        self.assertEqual(code, 0, text)
        self.assertIn("CHANGES: 0", text)

    def test_init_list_style_enabled_plugins(self):
        Path(self.repo, ".claude").mkdir()
        Path(self.repo, ".claude", "settings.json").write_text(json.dumps({"enabledPlugins": ["x@y"]}), encoding="utf-8")
        code, text = run(["init", "--project-dir", self.repo])
        self.assertEqual(code, 0, text)
        settings = json.loads(Path(self.repo, ".claude", "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["enabledPlugins"], ["x@y", "sessions@claude-sessions"])
        self.assertIn("NEXT: review", text)

    def test_init_refuses_rule_outside_gitignore(self):
        Path(self.repo, ".git", "info").mkdir(exist_ok=True)
        Path(self.repo, ".git", "info", "exclude").write_text(".claude/\n", encoding="utf-8")
        code, text = run(["init", "--project-dir", self.repo])
        self.assertEqual(code, 1)
        self.assertIn("outside", text)
        self.assertIn("!.claude/sessions/", text)


@unittest.skipUnless(shutil.which("bash"), "bash not available")
class TestShim(unittest.TestCase):
    def test_shim_runs_python(self):
        p = subprocess.run(["bash", str(SHIM), "--version"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertIn(f"claude-sessions {S.PLUGIN_VERSION}", p.stdout + p.stderr)

    def test_shim_without_python_reports_status_and_exits_zero(self):
        bash = shutil.which("bash")
        empty = tempfile.mkdtemp(prefix="cs-empty-")
        try:
            p = subprocess.run([bash, str(SHIM), "locate"], capture_output=True, text=True, env={"PATH": empty, "HOME": empty})
        finally:
            shutil.rmtree(empty, ignore_errors=True)
        self.assertEqual(p.returncode, 0)
        self.assertIn("STATUS: error", p.stdout)
        self.assertIn("REASON: python-missing", p.stdout)
        self.assertIn("winget", p.stdout)


if __name__ == "__main__":
    unittest.main()
