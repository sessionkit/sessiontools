# The Python library

`sessiontools` is also a library: which sessions are live, whether each
is busy, and how long it has been idle, in our words, never a
harness's. The `session` command is built on it. Add it to a project
that needs it (sessionmail does):

```console
uv add sessiontools
```

```python
from sessiontools import sessions, idle_seconds

for s in sessions():                       # newest status first
    print(s.id, s.agent, "busy" if s.busy else f"idle {s.idle_seconds():.0f}s")

idle_seconds("22d54b86-...")               # 0.0 busy, seconds otherwise, None unknown
```

- Every answer is in our words: busy, not busy, unknown.
- **Unknown is `None`, never zero:** a caller decides what unknown
  means.
- `import sessiontools` loads nothing outside the standard library.

## The functions

- `sessions()`: every session with a readable sidecar, newest status
  first.
- `live()`: the sessions whose process is running now; `sessions()`
  minus every sidecar a crash left behind or whose pid was reused.
- `find(session_id)`: one `Session`, or `None`.
- `session_id(env=None)`: this session's id, from the environment only
  (never the disk): the first non-blank of `SESSION_ID_VARS`, stripped,
  or `None`.
- `current(env=None)`: this session's `Session`,
  `find(session_id())`, or `None` when there is no id or no sidecar.
- `SESSION_ID_VARS`: `("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")`,
  in the order they are read.
- `idle_seconds(session_id, now=None)`: `0.0` while busy, else seconds
  since the state began, `None` when nothing can be said.
- `read_operator()`, `Operator`: see
  [operator and privileged seats][operator].
- `sidecar_dir()`, `SIDECAR_DIR_ENV`: where the sidecars are read from.

## `Session`

- Fields: `id`, `harness`, `busy`, `updated_at`, `turn_ended_at`,
  `pid`, `cwd`, `name`, `agent`, `started_at`, `version`, `tmux`, and
  `raw_status` (the harness's own word) for the curious.
- `Session.idle_seconds(now=None)`: 0 while busy, else seconds since
  the state began.
- `Session.state`: one of `STATES`, exported as `BUSY`,
  `IDLE_WITH_CHILDREN` and `IDLE`.

## The three states

- `busy`: the harness says busy.
- `idle with children`: the harness says busy, but the turn is over and
  a background agent runs.
- `idle`: neither.
- `idle with children` comes from a `turn-ended` mark in the session
  dir, written by the plugin's hook only when
  `SESSIONTOOLS_HOOK_TURN=on` (`sessiontools.turn`). With no mark every
  reading is the sidecar's alone.
- Consumers: page an `idle with children` seat at once, never reap one
  (that kills its children), and wake a `busy` seat no more than before.

## The driver

- The first driver, `claude_code`, reads Claude Code's own per-session
  sidecar, `~/.claude/sessions/<pid>.json`, or `$SESSION_SIDECAR_DIR`.
- It replaced the private readers three of our tools each had for that
  file.
- The day a harness changes a status word, its driver changes and
  nothing above it does.

## Dependencies

- Two runtime dependencies, each imported by one feature alone:
  PyYAML for `session start` (agent front matter is YAML), and
  jsonschema for `session settings`.
- `tests/test_cli.py` pins that `import sessiontools` reaches neither.

[operator]: operator-privileged-seats.md
