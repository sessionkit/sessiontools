# shellcheck shell=sh
# session-dir.sh - the hooks' one way to this session's dir. Not a hook and
# not run: busy-sound and settle-hook source it with `.`, by path from their
# own $0, so it costs one file read and no fork. It defines `leaf`, `fresh`
# and `session_dir`.
#
# THE ONE BASH COPY of settings.leaf_path, for the sessions and sound
# leaves, with builtins only: the override; else OLD while it is a directory
# that is not NEW's; else NEW when it is a directory; else the fresh default,
# the third word: new for sound (settings.FRESH_NEW); for sessions, fresh()'s
# answer: new only on a fresh host, one with no ~/.session of its own
# (settings.old_root_real). tests/test_leaf_paths.py pins both
# against python on old-only, fresh, migrated, new-only and partial-old HOMEs.
leaf() {   # leaf OLD NEW old|new: sets $at to the rule's answer, no override; no fork
  if [ -d "$1" ] && ! [ "$1" -ef "$2" ]; then at=$1
  elif [ -d "$2" ]; then at=$2
  elif [ "$3" = new ]; then at=$2
  else at=$1
  fi
}
fresh() {  # fresh ROOT STATE: sets $fw to old when ROOT is a dir that is not STATE, else new; no fork
  if [ -d "$1" ] && ! [ "$1" -ef "$2" ]; then fw=old; else fw=new; fi
}

# session_dir: sets $sid from CLAUDE_CODE_SESSION_ID (claude sets it in every
# hook) and $dir to <sessions>/<sid>, the sessions leaf as settings resolves
# it ($SESSIONS_DIR, a literal ~/ expanded as python expanduser()s it, else
# the rule above). Returns 1 when the id is missing or not a bare name
# (busy.is_sid), or when the dir does not exist: no identity, nothing to read.
session_dir() {
  sid=${CLAUDE_CODE_SESSION_ID:-}
  case "$sid" in '' | */* | . | ..) return 1 ;; esac
  if [ -n "${SESSIONS_DIR:-}" ]; then
    root=$SESSIONS_DIR
    case "$root" in \~/*) root=$HOME/${root#\~/} ;; esac
  else
    fresh "$HOME/.session" "$HOME/.sessionkit/tools/state"
    leaf "$HOME/.session/sessions" "$HOME/.sessionkit/tools/state/sessions" "$fw"
    root=$at
  fi
  dir=$root/$sid
  [ -d "$dir" ]
}
