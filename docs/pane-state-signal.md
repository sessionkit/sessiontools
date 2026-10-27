# The pane signal: is this seat safe to close by hand?

`session pane-state watch` writes each seat's state onto its tmux pane, as
the user option `@session_state`: `BUSY`, `FREE`, or unset (UNKNOWN). A
pane title, status line or sidebar shows it as `#{@session_state}`, read
from tmux's own memory, with no process spawned per redraw.

- **Why:** a seat waiting on a background test looks idle at its prompt.
  Two such seats were closed by hand in the middle of their work.
- **FREE only on the exact word `FREE`.** Unset means UNKNOWN, and so
  does a pane the publisher never wrote, or one it unset at stop.
- **It only reports.** The display (pane title, status line, sidebar)
  belongs to taskmaster or the tmux config, not to sessiontools.
- **A launchd agent starts the publisher**, one per host, like the
  sound engine. It is off until you switch it on:
  `session pane-state agent on` (below). **On a real host, `on` IS
  the deploy.**
- Works with `SESSIONTOOLS_HOOK_TURN` off; it never reads or sets it.

## The display contract

- `#{@session_state}`: `BUSY`, `FREE`, or empty (UNKNOWN).
- `#{@session_state_why}`: the reason, for a sidebar or for debugging.
- **The reason is raw command text.** It can hold `#[…]` or `#{…}`. Never
  put it into a status format unescaped, or a command line can restyle
  the bar.
- **Never use `#(…)` to read the state.** That runs a command every status
  interval: the cost this signal exists to avoid. Nothing sessiontools
  ships contains one (a test checks).

## The readings

| sidecar status | what is live | reads | why |
|---|---|---|---|
| none for the pane | | unset | |
| any | the process is dead, or `ps` failed | UNKNOWN | `no live process` |
| `busy` | a turn is running | BUSY | `turn` |
| `waiting` | a prompt blocks the turn | BUSY | `waiting` |
| `shell` / `idle` | a tool shell runs work | BUSY | `work: <command>` |
| `shell` / `idle` | only listeners, or nothing | FREE | `listener` / `nothing live` |
| anything else | | UNKNOWN | `status <word>` |

- A **tool shell** is a direct `<shell> -c` child of the seat (Claude
  Code's Bash tool). Everything under it is sorted: a listener, a pipe
  helper (`tail`, `head`, `tee`, `cat`), or **work**, which is anything
  else.
- An unknown process is work. So is a tool shell with nothing under it,
  and a nested shell with no child at that moment.
- The pager monitor, MCP servers and `caffeinate` are the harness's own
  direct children. They never count, either way.
- A taskmaster `tm task wait` reads BUSY. That is the default, not a
  settled answer: whether such a wait counts as work is still open.

## The settings

In `~/.sessionkit/tools/settings/session.json` (template and host layers):

```json
{"pane_state": {"listeners": ["pager listen"], "interval": 1.0}}
```

- `listeners` is a **trust list**: anything on it can make a working seat
  read FREE. A file that cannot be read gives the default, `pager listen`
  only. An empty list means no listeners, so every tool shell is work.
- `interval` is the seconds between ticks. Anything but a number above
  0 reads as 1.0.

## `session pane-state show [--json]`

- Reads every seat once: its pane, the reading, the reason, and what the
  pane holds now (`PUBLISHED`; `-` is unset).
- Writes nothing: no tmux option, no lock, no file. It is safe on a real
  host, beside a live publisher. It costs one `ps` and two `list-panes`
  per server.
- Lists every seat with a sidecar, a dead one included: it reads UNKNOWN
  `no live process`. The publisher skips a dead seat
  and unsets any pane it wrote for it.

## `session pane-state watch`

- `--interval S`: seconds between ticks (default: the setting, else 1).
- `--once`: one tick, then exit. **Its writes stay standing:** nothing
  keeps them current, and nothing unsets them.
- `--dry-run`: logs every transition and never writes tmux. It **takes no
  lock**, so it runs beside a live publisher and creates no file.
- `--stats`: on exit, prints `ticks= ps= tmux= writes= mismatches=`.
- **One per host:** an flock on `pane-state/watch.lock` in the state dir
  (`session path state`). A second `watch` names the holder and exits 0.
- **On SIGTERM or SIGINT** it unsets every option it wrote. A stopped
  publisher leaves UNKNOWN, never a stale FREE.
- **At start** it rewrites whatever the panes hold, so a FREE left by a
  killed predecessor is corrected on the first tick.

## `session pane-state agent on|off|status`

The publisher runs as a per-user launchd agent,
`dev.sessionkit.tools.pane-state`. It is macOS only. Its label lives in
one constant in the code; `session pane-state agent status --json`
prints it as `label`, and `agent --help` names it.

**The old label is retired.** An earlier release ran the job under
another label. `on` and `off` boot out a job still loaded under it and
remove its plist, and the old job holding the watch lock is not a
refusal. `status` names one that is still there (`legacy` in `--json`).
With nothing to retire, they say so and do nothing; `--dry-run` lists
the retire and changes nothing.

- `session pane-state agent on --dry-run` prints the plist it would
  write, and changes nothing.
- `session pane-state agent on` writes the plist, loads it and proves it.
- `session pane-state agent status` shows the plist, the pid and who
  holds the watch lock.
- `session pane-state agent off` boots it out and removes the plist.

How the job runs:

- **What it runs:** `<this install's session> pane-state watch`, by
  absolute path, because launchd does no PATH lookup. On an editable
  install the job runs the checkout as it was at its last start, so run
  `on` again to restart it.
- **Its PATH** is the directory of the tmux `on` finds, then
  `/usr/bin:/bin:/usr/sbin:/sbin`. launchd's own PATH has no Homebrew
  tmux.
- **It passes through** `TMUX_TMPDIR`, `SESSION_STATE_DIR`,
  `SESSION_SIDECAR_DIR` and `SESSIONS_DIR`, each only when it is set at
  `on` time. So the job reads the same sockets, lock, sidecars and turn
  marks as the shell that ran `on`.
- **`on` refuses** (exit 2, nothing changed) off macOS, with no
  `session` script or no tmux, and while the watch lock is held by
  anything but the loaded agent. It names the holder: stop that
  publisher first.
- **`on` proves it:** it waits up to 10 s for the lock to name the
  job's pid. If it never does, `on` exits 1 and names the log,
  `~/.sessionkit/tools/log/pane-state.log`. Before 0.3.2 the agent logged
  to `~/Library/Logs/sessiontools/pane-state.log`; that file is left in
  place, and `status` names it while it is there.
- **KeepAlive:** a crashed publisher is back within 30 s
  (`ThrottleInterval`). If a hand-run `watch` holds the lock, the job
  exits at each start and launchd tries again every 30 s; `status`
  says so.
- **`off`** boots the job out, so the publisher unsets every option it
  wrote, and removes the plist.
- **No GUI login, no agent:** it loads into `gui/<uid>`, like the
  sound engine. On a host with no one logged in at the console, the
  bootstrap fails, `on` exits 1 with launchctl's words, and nothing
  publishes.
- Exit codes: 0 done, 1 tried and failed, 2 refused before anything
  changed or a usage error.

## What it costs

- Each tick stats the sidecar files, in-process, with no spawn.
- It reads the process table (one `ps`) only when a sidecar changed, a
  seat was BUSY with work last tick, or 30 s have passed.
- Panes are found once per seat and cached. The pane index is read again
  only for a new seat or after a failed write.
- tmux options are written only on change: one `tmux` call per server per
  tick that has a change.
- Measured on a private server: an idle minute cost 0 tmux runs and two
  `ps` reads (the 30 s safety reads), and a status line on
  `#{@session_state}` spawned nothing in 20 s. The `#(date)` control
  spawned one child a second. Tests: `tests/test_pane_state_redraw.py`,
  `tests/test_pane_state_tmux.py`.

## Risks

- **R1, a stale FREE after a SIGKILL.** The panes keep their last word.
  The next start rewrites them; until then nothing does.
- **R2, up to one tick (1 s) late going FREE → BUSY** when a seat starts a
  turn, because the sidecar is polled.
- **R3, the tool-shell shape is Claude Code's.** If tool calls stop being
  a direct `<shell> -c` child, a seat with a background test reads FREE.
  Re-check `show` against a live seat when Claude Code's version moves.
- **R4, the listener list is a trust list** (see the settings above).
- **R5, a crash with no sidecar write is seen only at the 30 s safety
  read.** The pane keeps its last word until then, which is never a wrong
  FREE.
- **A seat in no pane.** A seat outside tmux is never written. If a
  server answered but its pane list came back empty (a timeout), the seat
  is looked for again at the next safety read, not every tick. Meanwhile
  it reads UNKNOWN, never FREE.

The other verbs: [the `session` verbs][verbs].

[verbs]: session.md
