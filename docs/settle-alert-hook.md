# The settle alert (a plugin hook)

Once a session's context reaches the threshold it set for itself, the
plugin's `hooks/settle-hook` (PostToolBatch, `hooks/hooks.json`) says
one line each turn. **Unset means silent, always**, and python never
starts.

```text
You're past 250K. At a good stopping point, settle: save what matters, then hand over.
```

| variable | what it does |
| --- | --- |
| `SESSION_SETTLE_AT` | the threshold in tokens: `250000`, `250k`, `1.5M` |
| `SESSION_SETTLE_SAY` | replaces the line; `{at}` and `{used}` are filled in |
| `SESSIONTOOLS_HOOK_SETTLE=off` | switches it off (also `0`, `false`, `no`) |

- **Any session that sets a threshold is told.** The hook never looks
  at who the session is.
- It fires at `used >= threshold`. `used` is the last usage row, the
  same number `session context` prints, read with
  `sessiontools.window.scan` on the transcript the hook payload names.
  It took 0.08 s on a 21.6 MB transcript.
- It fires on **turns that call a tool**. PostToolBatch runs once per
  model response that made tool calls, so a reply with no tool call is
  not told. The next batch is.
- A subagent's batch (payload `agent_id`) is silent. The session hears
  the line on its own next batch.
- **It never breaks a turn.** No python3, python older than 3.11, a
  missing or unreadable transcript, or a crash all end with no output
  and exit 0.
- `SESSIONTOOLS_HOOK_SETTLE=off` keeps the plugin loaded.
- **The hook's python** is the busy sound's (`hooks/pick-python.sh`):
  `$SESSIONTOOLS_PYTHON` when set, else the python in the shebang of
  `command -v session` (a bare path only), else `python3`. `uv tool
  install` keeps its python off `PATH`, and a clean Mac's `python3` is
  older than 3.11, so `PATH` alone would leave the alert silent.

## Details

- The variables come from the Claude Code process environment. A
  settings `env` block reaches the hook too. This was measured on Claude
  Code 2.1.284 with
  `claude -p --plugin-dir . --settings '{"env": {...}}'`: the line came
  back with the session's live count in it.
- The default names no next-step verb. It gains `session respawn` once
  that verb is built. Until then, `SESSION_SETTLE_SAY` is how a session
  names its own next step.
- It ships from plugin 0.0.4: see [the plugin][plugin].

[plugin]: claude-code-plugin.md
