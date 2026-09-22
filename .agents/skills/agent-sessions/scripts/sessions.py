#!/usr/bin/env python3
"""Entry point for the Claude plugin, the portable skill bundle and a bare checkout.

Finds the agent_sessions package next to this script (bundle layout) or one level up
(repository layout) before falling back to an installed copy.
"""

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
for _cand in (_here, os.path.dirname(_here)):
    if os.path.isdir(os.path.join(_cand, "agent_sessions")):
        sys.path.insert(0, _cand)
        break

from agent_sessions.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
