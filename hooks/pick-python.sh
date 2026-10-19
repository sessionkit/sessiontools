# shellcheck shell=sh
# pick-python.sh - the hooks' one python picker. Not a hook and not run:
# busy-sound and settle-hook source it with `.`, by path from their own $0,
# so it costs one file read and no fork. It defines `pick` and empties $py.
#
#   pick    sets $py to the python to run, once; later calls return at once.
#           Returns 1, with $py empty, when there is no python at all.
#
# The order:
#   1. $SESSIONTOOLS_PYTHON, when it names an executable (tests, odd hosts);
#   2. else the interpreter in the shebang of `command -v session`, when that
#      is a bare path to a python (no space: `#!/usr/bin/env python3` and
#      wrappers do not count). `uv tool install` writes that shebang: it is
#      the tool venv's python, which has PyYAML and is 3.11+, while uv keeps
#      that python off PATH;
#   3. else python3 from PATH. On a clean Mac that is Apple's 3.9.6, so each
#      hook's own `< (3, 11)` guard exits 0 there.
#
# Picking runs `command -v` and reads one line, so a hook calls `pick` only
# after its fast paths: an off switch never pays for it.
py=
pick() {   # the python, picked once and only when needed; 1 when there is none
  [ -n "$py" ] && return 0
  py=${SESSIONTOOLS_PYTHON:-}
  if [ -z "$py" ] && st=$(command -v session) && IFS= read -r l < "$st"; then
    case "$l" in '#!'*[[:space:]]*) ;; '#!'*python*) py=${l#\#!} ;; esac   # a bare path only
  fi
  [ -n "$py" ] && command -v "$py" >/dev/null || py=python3
  command -v "$py" >/dev/null || { py=; return 1; }
}
