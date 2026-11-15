# Restarting a seat: `session respawn`

`session respawn` restarts a seat in its own tmux pane, with the
`session start` line it was launched with. It prints a plan by default and
touches nothing. With `--go` it acts, on the pane you run it in.

- **Why:** a seat that has settled, or is stuck, is restarted by hand today:
  find the pane, find the start line, kill, retype. Typing the line wrong
  starts a different seat. This replays the line the seat was started with.
- **A plan unless `--go`.** With no `--go` it prints the plan and changes
  nothing: no file, no log line, no tmux call that writes.
- **`--go` acts on YOUR OWN pane only.** A SID that names another seat's
  pane is planned and never acted on: `session respawn SID --go` there
  refuses with *"acting on another seat is not built; see the plan"*. The
  only guard against killing a neighbour is the check that this process
  runs inside the pane it names, and that check cannot hold for someone
  else's pane. Whether to build it is not decided.
- **Only a seat started with `session start`.** The line it replays is the
  seat's launch record (`session get launch_command`), the line as it was
  entered. A seat with no record is refused: a seat that was started
  before the record existed, one not started by `session start`, and a
  keyed resume's fork. Kill its window and kick it instead.
- **It never reads tmux's `pane_start_command`.** tmux re-quotes that line,
  and nothing here parses it back.
- **`session` must be on your `PATH`.** The respawned pane gets this
  process's `PATH`, and without `session` on it the new pane would die with
  127. It refuses while the old seat is still alive.

The forms:

- `session respawn`: the plan for the seat in this pane.
- `session respawn --go`: log, then respawn this pane.
- `session respawn 8d71f3b6`: the plan for that seat, in whatever pane it is
  in. Run from outside tmux, this is the only form that works.
- `session respawn 8d71f3b6 --go`: acts only when `8d71f3b6` is the seat in
  your own pane; otherwise refused.

The SID is positional: a full session id, or a prefix of at least 8
characters (the `short` that `session list` prints). It is never a name:
`reviewer` matches `reviewer-2` too, and a guess is refused. A prefix that
matches two seats is refused, and the message lists the matches. There is
no `--session-id` flag.

## The plan

```text
session respawn: plan, nothing touched (--go acts)
  seat       0041271d  reviewer  (agent reviewer)
  pane       %3 on default, by $TMUX_PANE, confirmed by process ancestry
  record     ~/.session/sessions/0041271d-…/launch/command
  entered    session start --in-tmux reviewer
  dir        /Users/you/src/app
  pane line  session start reviewer
  message    none
  model      live model claude-opus-5-5; the line types no --model, so the respawn resolves it at launch, as a fresh `session start` does
  log        ~/.sessionkit/tools/log/respawn.log
  tmux       tmux -L default respawn-pane -k -t %3 -c /Users/you/src/app -e SESSION_START_ENTERED=… -e SESSION_START_ENTERED_DIR=… 'session start reviewer'
```

- **`entered`** is the recorded line. **`pane line`** is what the pane runs:
  the placement flags (`--in-tmux`, `--tmux-target`, `--switch`,
  `--no-switch`) are off, so the seat restarts IN PLACE, not in a new
  window.
- **`message`:** bare first-message words in the line move to a new 0600
  file, and the pane line names the file, so no word reaches
  `pane_start_command`, the log or the plan. The row shows the path
  `--go` would write.
- **`tmux`:** every `-e` value is shown as `…`; the names stay. They carry the entered line
  (see [what the new seat records](#what-the-new-seat-records)).

## The model: shown, not refused

The line is the authority. A `--model` typed in it rides along as typed.
A model set by the agent file or settings is resolved at the new launch,
as a fresh `session start` resolves it. A model you switched to with
`/model` after the launch is NOT carried, because it is not in the line.

The plan's `model` row says so in one line, when the two differ:
`live model X, the respawn will start with Y`. That is advice. It never
refuses.

## What it refuses

Exit 2, one line saying why, before anything is written:

- **The pane.** `$TMUX_PANE` is unset or empty, `$TMUX` is empty, or the
  one ancestry check fails. `$TMUX_PANE` is the answer and is never
  inferred: an untargeted tmux query answers with the ACTIVE pane, which is
  yours. The check runs once, on the server `$TMUX` names: the first
  ancestor of this process that is a pane's own process must be
  `$TMUX_PANE`.
- **The seat.** No live seat in the pane, or two. Seats are read fresh,
  never from the [list cache][cache], and matched on the (socket, pane)
  pair: `%3` on another tmux server is not this `%3`.
- **The SID.** Not a session id, shorter than 8 characters, unknown,
  ambiguous, or a seat in no pane.
- **The record.** No `launch_command`; a line that is not `session start`;
  one carrying `--session-id` or a resume (the new seat would start on the
  old seat's id, or resume it); a `launch_dir` that is gone; `session`
  missing from `PATH`; a recorded `--first-message-file` that is gone or
  not ours and 0600.
- **Another seat's pane under `--go`.** See above.
- **The log.** `respawn.log` cannot be opened or written, or the new
  first-message file cannot: `--go` refuses and the pane is untouched.

## What `--go` does

In this order:

1. Opens `respawn.log` (below). It cannot be opened: refused, and nothing
   else is touched.
2. Writes the new first-message file, when the line carries bare words.
3. Writes the log line, BEFORE the kill. A failed write takes the message
   file back and refuses.
4. `tmux set-option -p remain-on-exit failed` on the pane, as a call of its
   own. A tmux without `failed` prints one stderr line, and the respawn
   still runs. A respawn that fails then leaves the pane open with its
   reason; a clean exit closes it.
5. `exec`s into `tmux respawn-pane -k -t PANE -c LAUNCH_DIR ...`. Exec,
   because the pane is about to be killed with this process in it. It
   starts the pane line in `launch_dir`. It does not return.

If your `AGENTS_DIR`, `SESSION_STATE_DIR` or `SESSIONS_DIR` differs from
tmux's, one line per variable says so: the respawned pane resolves in
tmux's environment, as a [placed window][launcher] does.

Exit codes: 0 the plan was printed (an own-pane `--go` does not return: it
becomes tmux); 2 refused; 1 tmux could not be run.

## What the new seat records

`--go` passes the entered line to the new pane by the environment, so the
respawned seat records the line as it was entered, `--in-tmux` and words
included, not the pane line. A second respawn replays the same line, even
after the first message file is deleted. While `tmux respawn-pane` runs (a
moment, and then it is killed with the pane) its command line holds the
entered words: the same window as a [placed window][launcher].

## The log

`~/.sessionkit/tools/log/respawn.log` (the tools log dir, beside `reap.log`;
`session path log` prints it). One tab-separated line for each `--go`:

```text
TIME  SHORT  NAME  AGENT  SOCKET:PANE  LAUNCH_DIR  PANE_LINE  BY
```

`BY` is `own pane`. The pane line is logged, never the entered one, so no
bare first-message word reaches the log. The file is 0600.

**It differs from reap's log on purpose.** reap's is best effort. This is
the only trace an act leaves once it has killed its own caller, so a log
that cannot be written refuses the `--go`.

## The claim

`respawn-pane -k` kills the old seat before the new launcher runs, and the
new process gets the same `$TMUX_PANE`. The default seat's claim moves to
the new pane as it does for a fresh launch ([one holder][front-door]): the
old holder is dead, or it is replaced.

## Not here

- **No `--dry-run`, `--explain`, `--trust-env` or `--session-id`.** The
  plan is the default; a SID without `--go` is the read-only look at
  another seat.
- **No `--go` on another seat.** Refused for now.
- **No carrying a live `/model` flip.**
- **Retiring a hand-run respawn script** is its owner's call; this does
  not touch one.

[cache]: list-snapshot-cache.md
[launcher]: session-start-launcher.md
[front-door]: session-start-launcher.md#the-front-door-one-holder
