#!/usr/bin/env python3
"""Keep the portable skill's bundled copy of the package identical to the source.

    python3 tools/sync_bundle.py          # copy agent_sessions/ + scripts into agents-skills/agent-sessions/scripts/
    python3 tools/sync_bundle.py --check  # exit 1 if the bundle differs (CI)
"""

from __future__ import annotations

import filecmp
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_PKG = ROOT / "agent_sessions"
SRC_SCRIPTS = [ROOT / "scripts" / "sessions.py", ROOT / "scripts" / "sessions.sh"]
BUNDLE = ROOT / "agents-skills" / "agent-sessions" / "scripts"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


def files_under(base: Path):
    for p in sorted(base.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc" and p.name != ".DS_Store":
            yield p.relative_to(base)


def sync() -> None:
    dest_pkg = BUNDLE / "agent_sessions"
    if dest_pkg.exists():
        shutil.rmtree(dest_pkg)
    shutil.copytree(SRC_PKG, dest_pkg, ignore=IGNORE)
    BUNDLE.mkdir(parents=True, exist_ok=True)
    for s in SRC_SCRIPTS:
        shutil.copy2(s, BUNDLE / s.name)
    os.chmod(BUNDLE / "sessions.sh", 0o755)
    print(f"synced {sum(1 for _ in files_under(dest_pkg)) + len(SRC_SCRIPTS)} files into {BUNDLE.relative_to(ROOT)}")


def check() -> int:
    problems = []
    dest_pkg = BUNDLE / "agent_sessions"
    src_files = set(files_under(SRC_PKG))
    dst_files = set(files_under(dest_pkg)) if dest_pkg.exists() else set()
    for rel in sorted(src_files - dst_files):
        problems.append(f"missing in bundle: agent_sessions/{rel}")
    for rel in sorted(dst_files - src_files):
        problems.append(f"stale in bundle: agent_sessions/{rel}")
    for rel in sorted(src_files & dst_files):
        if not filecmp.cmp(SRC_PKG / rel, dest_pkg / rel, shallow=False):
            problems.append(f"differs: agent_sessions/{rel}")
    for s in SRC_SCRIPTS:
        d = BUNDLE / s.name
        if not d.exists():
            problems.append(f"missing in bundle: scripts/{s.name}")
        elif not filecmp.cmp(s, d, shallow=False):
            problems.append(f"differs: scripts/{s.name}")
    if problems:
        print("bundle out of sync; run: python3 tools/sync_bundle.py")
        for p in problems:
            print("  -", p)
        return 1
    print("bundle in sync")
    return 0


if __name__ == "__main__":
    sys.exit(check() if "--check" in sys.argv[1:] else (sync() or 0))
