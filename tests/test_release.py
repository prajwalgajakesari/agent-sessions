"""Release hygiene: one version everywhere, skill step regions identical, bundle in sync."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agent_sessions  # noqa: E402

SPEC_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}


def frontmatter(text: str) -> dict:
    assert text.startswith("---\n"), "missing frontmatter"
    end = text.index("\n---", 4)
    fm = {}
    for line in text[4:end].split("\n"):
        m = re.match(r"^([A-Za-z-]+):", line)
        if m:
            fm[m.group(1)] = line.split(":", 1)[1].strip()
    return fm


def region(text: str, name: str) -> str:
    m = re.search(rf"<!-- steps:{name} -->\n(.*?)<!-- /steps:{name} -->", text, re.S)
    assert m, f"missing region steps:{name}"
    return m.group(1)


class TestVersionAgreement(unittest.TestCase):
    def test_versions_agree(self):
        v = agent_sessions.__version__
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["version"], v)
        self.assertEqual(market["plugins"][0]["version"], v)
        skill = (ROOT / ".agents" / "skills" / "agent-sessions" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(f'version: "{v}"', skill)
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## {v}", changelog)
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('dynamic = ["version"]', pyproject)
        self.assertIn('path = "agent_sessions/__init__.py"', pyproject)

    def test_names_agree(self):
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(market["name"], "agent-sessions")
        self.assertEqual(market["plugins"][0]["name"], "sessions")
        self.assertIn("agent-sessions.git", market["plugins"][0]["source"]["url"])
        self.assertIn('name = "agent-sessions-cli"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))


class TestSkillFiles(unittest.TestCase):
    def test_shared_step_regions_are_identical(self):
        portable = (ROOT / ".agents" / "skills" / "agent-sessions" / "SKILL.md").read_text(encoding="utf-8")
        for name in ("push", "pull", "init"):
            claude = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertEqual(region(claude, name), region(portable, name), f"steps:{name} drifted between the Claude skill and the portable skill")

    def test_portable_frontmatter_uses_spec_fields_only(self):
        text = (ROOT / ".agents" / "skills" / "agent-sessions" / "SKILL.md").read_text(encoding="utf-8")
        fm = frontmatter(text)
        self.assertEqual(fm["name"], "agent-sessions")
        self.assertTrue(set(fm) <= SPEC_FIELDS, f"non-spec frontmatter fields: {set(fm) - SPEC_FIELDS}")
        self.assertLessEqual(len(fm["description"]), 1024)
        self.assertNotIn("${CLAUDE_", text)
        self.assertNotIn("!`", text)
        self.assertNotIn("$ARGUMENTS", text)

    def test_claude_skills_are_user_invoked_only(self):
        for name in ("push", "pull", "init"):
            fm = frontmatter((ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8"))
            self.assertEqual(fm["name"], name)
            self.assertEqual(fm["disable-model-invocation"], "true")


class TestBundle(unittest.TestCase):
    def test_bundle_in_sync(self):
        p = subprocess.run([sys.executable, str(ROOT / "tools" / "sync_bundle.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_bundle_wrapper_runs_standalone(self):
        script = ROOT / ".agents" / "skills" / "agent-sessions" / "scripts" / "sessions.py"
        p = subprocess.run([sys.executable, str(script), "--version"], capture_output=True, text=True, cwd=str(ROOT.parent))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(agent_sessions.__version__, p.stdout)


if __name__ == "__main__":
    unittest.main()
