# The clock line (a plugin hook)

Switched on, the plugin's `hooks/clock-hook` (PostToolBatch,
`hooks/hooks.json`) says the time and how full the context window is,
once per tool batch. **Off by default**: unset, the shim exits before
python starts.

```text
[clock] 12:34 UTC | ctx 182K/1.00M (18%)
```

At or past a warn mark the session set, a second line follows:

```text
[clock] 12:34 UTC | ctx 262K/1.00M (26%)
ctx past 250K: 262K of 1.00M used.
```

| variable | what it does |
| --- | --- |
| `SESSIONTOOLS_HOOK_CLOCK=on` | switches it on (also `1`, `true`, `yes`, any case). Anything else, unset included, is off. |
| `SESSION_CLOCK_WARN_AT` | the warn mark: tokens (`250000`, `250k`, `1.5M`) or a percent of the window (`80%`, `12.5%`). Unset means no warn line. |

- **The clock** is the host's local time, `HH:MM` and the zone name, as
  `date '+%H:%M %Z'` prints it.
- **The context** is `used/max (pct%)`. `used` is the last usage row,
  the same number `session context` prints, read with
  `sessiontools.window.scan` on the transcript the hook payload names.
  `max` is the model's window, and `CONTEXT_MAX` tightens it as it does
  for `session context`.
- **It never guesses.** An unknown window renders `ctx 182K/?`. No
  transcript or no usage row renders `ctx ?`, and so does a transcript
  whose last row reads 0 after a real count (a synthetic tail, not a
  reading). The clock is still said. `ctx ?` never warns; `ctx 182K/?`
  warns only at a token mark (below).
- **The warn line** is said at `used >= mark`: reaching the mark counts.
  A percent mark names both figures, `ctx past 80% (800K): 812K of 1.00M
  used.` With the window unknown, a token mark drops the `of`
  (`ctx past 250K: 262K used.`) and a percent mark says nothing, since
  it cannot be worked out. An empty, unparsable or zero mark means no
  warn line.
- **There is no mail count.** Mail is not this hook's.
- **The count is this batch's own**, as for the settle alert: the hook
  waits for the batch's own assistant row to reach the transcript, 1 s
  at most ([the settle alert][settle] says how).
- A subagent's batch (payload `agent_id`) is silent. The session hears
  the line on its own next batch.
- **It never breaks a turn.** No python, python older than 3.11, or a
  crash all end with no output and exit 0. A missing or unreadable
  transcript is not a failure: the line is still said, as `ctx ?`. It
  never writes to stderr.
- **The hook's python** is the settle alert's (`hooks/pick-python.sh`):
  `$SESSIONTOOLS_PYTHON` when set, else the python in the shebang of
  `command -v session`, else `python3`.

## The clock line and the settle alert

Both are PostToolBatch hooks in the same `hooks.json`. Claude Code runs
both on each batch and shows each line. They share no state, and each
has its own switch:

- **The clock line is a gauge.** It is said on every batch once switched
  on. Its warn line is a figure, "past X", and gives no instruction.
- **The settle alert is an instruction**: settle, save, hand over. It is
  said only past the session's own `SESSION_SETTLE_AT`
  ([the settle alert][settle]).
- **The warn mark does not fall back to `SESSION_SETTLE_AT`.** With both
  on one value, the session would hear one crossing twice. Set
  `SESSION_CLOCK_WARN_AT` below `SESSION_SETTLE_AT` for an early
  warning, or leave it unset and let the settle alert speak at the mark.
- With both on, each batch starts python twice and reads the transcript
  twice. The settle alert runs only when a threshold is set.

## Speed

It runs on every batch, so it was timed: `sh hooks/clock-hook` with a
payload on stdin, on a synthetic 20.0 MB transcript (4,986 rows, the
batch's own row last), 51 runs per case after 3 warm-up runs, on an
Apple Silicon Mac under heavy load.

| case | median | min | max |
| --- | --- | --- | --- |
| off (unset) | 9.3 ms | 5.3 ms | 45.1 ms |
| on | 133.7 ms | 104.3 ms | 285.5 ms |
| on, with the warn line | 144.4 ms | 106.5 ms | 347.6 ms |

- Off is the shell alone: no python starts.
- On, python's start and the imports took about 53 ms of that, and the
  rest is reading the transcript.
- The hook's timeout in `hooks.json` is 5 s.

## Details

- The variables come from the Claude Code process environment. A
  settings `env` block reaches the hook too, as for the settle alert.
- It is in the plugin from the next release: see [the plugin][plugin].

[settle]: settle-alert-hook.md
[plugin]: claude-code-plugin.md
