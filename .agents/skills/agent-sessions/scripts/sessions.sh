#!/usr/bin/env bash
# claude-sessions shim: find a Python 3.8+ interpreter and run sessions.py.
#
# ALWAYS exits 0. A SKILL.md preflight block aborts the whole skill on a non-zero
# exit, so callers must read the trailing "STATUS: ok|error" line instead.
# Uses bash builtins only for the interpreter search so it still works when PATH
# is unusual (Git Bash on Windows, minimal CI shells).

# Resolve our own directory. The script may be invoked with a Windows path (D:\x\scripts\sessions.sh)
# from Git Bash, so accept either separator; cygpath normalises when it exists.
src="${BASH_SOURCE[0]}"
if command -v cygpath >/dev/null 2>&1; then
  src="$(cygpath -u "$src" 2>/dev/null || printf '%s' "$src")"
fi
case "$src" in
  */*)   here="${src%/*}" ;;
  *\\*)  here="${src%\\*}" ;;
  *)     here="." ;;
esac
here="$(cd "$here" 2>/dev/null && pwd)"

try_py() {
  "$@" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1
}

py=()
if command -v py >/dev/null 2>&1 && try_py py -3; then
  py=(py -3)
elif command -v python3 >/dev/null 2>&1 && try_py python3; then
  py=(python3)
elif command -v python >/dev/null 2>&1 && try_py python; then
  py=(python)
fi

if [ "${#py[@]}" -eq 0 ]; then
  printf '%s\n' \
    "Python 3.8+ was not found on PATH. claude-sessions needs it to render and redact transcripts." \
    "  macOS:          brew install python3" \
    "  Windows:        winget install Python.Python.3.12    (then open a new terminal)" \
    "  Ubuntu/Debian:  sudo apt install python3" \
    "STATUS: error" \
    "REASON: python-missing"
  exit 0
fi

# Git Bash on Windows: hand the native launcher a path it understands (D:/x/y instead of /d/x/y)
if command -v cygpath >/dev/null 2>&1; then
  here="$(cygpath -m "$here" 2>/dev/null || printf '%s' "$here")"
fi

"${py[@]}" "$here/sessions.py" "$@"
exit 0
