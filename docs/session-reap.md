# Closing idle seats: `session reap`

`session reap` lists every seat idle past a limit (default 1h) and gives
each one a verdict, CLOSE or KEEP, with the reason. It is a dry run by
default and closes nothing. With `--yes` it closes the CLOSE seats, and it
re-checks each one at the act.

- **Why:** the prompt cache lasts an hour. A seat that idles past it and
  then wakes pays to rewrite its whole context. One seat measured idle for
  67 minutes had a cold cache, and its wake cost about 30 times as much.
  So: close, don't wait.
- **CLOSE needs positive evidence that the task is done.** reap never tries
  to tell "waiting on a human" from "done". A seat whose task is not in a
  terminal status is KEEP, and so is every seat it cannot read.
- **DONE means a terminal status:** `completed`, `archived`, `duplicate` or
  `todont`. A *hatched* task is `review` with every delivery landed. It
  does not close: hatched does not count as done yet ([v1
  choices](#v1-choices)), and its reason says so.
- **A dry-run CLOSE is advice.** Some checks run only at the act: what the
  pane holds, and whether it is its server's last pane. So `--yes` can
  still keep a seat the dry run marked CLOSE.
- **`--yes` closes the seat's PANE by id** (`kill-pane`, never
  `kill-window`). This stands in for taskmaster's stand-down path on
  purpose, for now ([why][why-kill-pane]).
- **A process that survives a close is named, never killed.** reap does not
  force.
- **No example here and no test runs `--yes` against real seats.** The
  tests use fake seats on a private `tmux -L` server.
- **Run it from the installed tool** (`uv tool install sessiontools`), not
  with `uv run` in a checkout ([limits][limits]).

```console
session reap                 # DRY RUN: every seat idle > 1h, CLOSE or KEEP, and why
session reap --idle 45m      # another limit: <n>s, <n>m or <n>h (m is minutes)
session reap --json          # the same rows as JSON
session reap 8d71f3b6        # only this seat: a session-id prefix of one listed seat
```

## What it lists

A seat is listed when it has been idle MORE than the limit. Idle is
`Session.idle_seconds`, the number `session get idle` prints. It is 0
while the seat is busy, and for `idle with children` it counts from the
turn's end. The longest idle comes first. The listing is the fresh read
`session list` makes, never the cache. Every row in this output is
invented:

```text
SHORT     NAME                          IDLE  VERDICT  WHY
6b0d4e91  web+web-153.lead.6b0d4e91    2h45m  KEEP     task WEB-153 is active (not done)
1f3a9c27  web+web-151.lead.1f3a9c27    1h27m  KEEP     task WEB-151 is review (not done)
c2e8a540  api+api-787.lead.c2e8a540    1h22m  KEEP     seat BUSY (work: tm task wait API-787)
8d71f3b6  api+api-144.worker.8d71f3b6  1h12m  CLOSE    task API-144 is completed; tail clean
e49a0c13  api+coordinator.e49a0c13     1h03m  KEEP     no task (a seat with no task is never closed)
dry run: 1 would close, 4 kept. Nothing was done.
```

A SHORT is matched on the session id only, never on the name: a fragment
such as `web-15` can be part of several seats' names. A SHORT that is not
the prefix of exactly one listed seat is a usage error, exit 2.

## CLOSE or KEEP: the gates, in order

Every gate can only KEEP. A seat reads CLOSE only when it passes every
gate, and then its reason is `task <ID> is <status>; tail clean`. When a
gate fails, its reason is the one shown:

| # | gate | KEEP reason |
|---|---|---|
| 1 | the seat has a tmux pane and socket | `no pane (nothing to close by id)` |
| 2 | no other live seat shares the pane | `pane shared with <short>` |
| 3 | the [pane signal][pane-state] reads FREE | `seat BUSY (turn)`, `seat BUSY (waiting)`, `seat BUSY (work: <command>)`, `seat UNKNOWN (<why>)` |
| 4 | the seat's cwd is inside a task | `no task (a seat with no task is never closed)`, `task README unreadable (<path>: <error>)` |
| 5 | the seat's name, if it has a task token, names that same task | `name says <token>, cwd says <ID>` |
| 6 | the task is DONE | `task <ID> is <status> (not done)`, or `(not done; hatched does not count as done yet)` when it is hatched; a missing status reads `no status` |
| 7 | the tail is clean: no unsaved work | `task <ID> is <status>; worktree dirty: <paths>`, `store dirty: <paths>`, `worktree unreadable (<why>)`, `store unreadable (<why>)` |

- **Gate 3.** BUSY `waiting` means a prompt is blocking the turn, so the
  seat is mid-turn and waiting on a human. BUSY `work` means a tool shell
  is running something: a `tm task wait`, a test run, a build. Only FREE
  `listener` or FREE `nothing live` goes on.
- **Gate 4.** reap walks up from the cwd to the first `README.md` whose
  front matter carries a `task_id`. A README that opens front matter but
  cannot be read or parsed stops the walk at KEEP: the seat is never
  resolved to a parent task. Seats that coordinate many tasks, and chat
  seats, sit at a store or project root, so they find no task.
- **Gate 5.** The token is the text between the name's first `+` and the
  next `.`, compared without case. A role word such as `coordinator` is a
  token too, so a seat named for a role and launched inside a task's
  directory is KEEP. A name with no `+`
  leaves the cwd to decide.
- **Gate 7.** reap runs `git --no-optional-locks status --porcelain
  --untracked-files=normal --ignore-submodules=none` in two places: the
  task's `worktree/`, if it exists, and the store, for the task directory.
  - `worktree/` must be a repo of its own. One with no `.git` reads as
    unreadable, because git would walk up to the store, which ignores it.
  - The status takes no lock, so a dry run can never make another seat's
    commit fail. Every inherited `GIT_*` variable is dropped, and each
    `git` gets 10 s.

## `--yes`: the act, in order

`--yes` acts on each CLOSE row one at a time. With SHORTs it acts on just
those seats, and a named KEEP row is refused (exit 1). It stops at the
first gate that fails for a seat, and prints `kept <short> (<name>):
<why>`:

1. **Re-read the seat**, fresh, by session id. It must still be there, with
   the same pid and process start, in the same socket and pane, and still
   idle past the limit. Reasons: `gone since the list`, `pid changed since
   the list`, `moved since the list`, `no longer idle past <limit>`.
2. **Run every dry-run gate again**, the tail included.
3. **Read the pane signal once more**, from the sidecar read again and a
   fresh `ps`. It must still read FREE, on the same pid: `sidecar gone or
   changed before the kill`, `turned <word> (<why>) before the kill`. This
   is the last reading before the kill, so the slow `git` runs never sit
   between it and `kill-pane`.
4. **The pane is the seat's.** The pane's own process, its *holder*, must
   be the seat or one of its ancestors: `pane <id> is held by <pid>, not
   this seat`.
5. **The seat's line is allowed.** `kill-pane` takes every process from
   the holder down to the seat, so only these may sit there:
   - **between the holder and the seat:** a launcher, and nothing else;
   - **the holder itself,** if it is not the seat: a launcher, or a login
     or interactive shell (`-bash`, `zsh`, `bash -l`). `sh -c <command>`
     does not count.

   A launcher is `tm task work` (or `taskmaster task work`) or `session
   start`. It must run from the script this PATH resolves for that name,
   matched by real path. A bare or relative name, or a copy of the script
   elsewhere, is not a launcher. Anything else on the line keeps the seat,
   and is named: `<pid> (<command>) sits between the pane and the seat,
   and is not a launcher`, or `pane held by <pid> (<command>): not a
   launcher or a login shell`. That covers a wrapper script with work left
   to do after the seat, a REPL, or an editor's terminal.
6. **The pane holds nothing but the seat.** Every process under the holder,
   and every process on the pane's terminal (`ps -t`), must be on the
   seat's line or under the seat. A job the shell forked and let go has
   parent pid 1, so it is in no process tree, but the hangup still
   reaches it. Reasons: `pane <id> also holds <pid> (<command>), not this
   seat's`, and `pane <id>'s terminal could not be read` when the terminal
   is unreadable.
7. **The pane is not its server's last.** Closing the last pane ends the
   server, and the server SIGTERMs its own `run-shell -b` jobs. Reasons:
   `pane <id> is the last on <socket>: closing it would end the server and
   its jobs`, `the panes on <socket> could not be read`, `pane <id> gone
   from <socket> before the kill`.
8. **Close:** `kill-pane -t <pane>` on the server the seat was read from
   (its `-L` name, from the seat's own row). It never sends `kill-window`.
   A `kill-pane` that tmux refuses is `failed <short> (<name>): kill-pane
   failed (exit <n>: <stderr>)`, exit 1, and nothing is retried.
9. **Verify** within 5 s. The pane must be gone. The seat, its line up to
   the holder, and every descendant must be dead; a reused pid does not
   count as the same process. Results:
   - a process left alive is named, never killed: `failed <short>
     (<name>): kill-pane sent, but not closed: survivors <pid> (<command>)`;
   - an empty `ps` table, or panes that cannot be read, is `kill-pane
     sent, but not verified`, exit 1. It is never read as "all gone".

What a close takes with it:

- The seat's line goes with the seat, the launcher and the login shell
  included.
- Killing a window's last pane closes the window. Killing a tmux session's
  last pane ends that session, and a human attached to it sees it end.
  (A server's last pane is KEEP, gate 7.)
- The seat loses its listener (its pager monitor, and any `pager listen`
  under a tool shell). That is the point. Verify checks that the listener
  is gone, rather than assuming it.
- Claude Code saves its transcript as it goes, so a done seat loses
  nothing else.

Each act is one tab-separated line in `reap.log`, in the state dir
(`$SESSION_STATE_DIR`, else `~/.session/state`). The fields are time,
short, name, task, `socket:pane`, outcome and why. The log is best effort:
if it cannot be written, the act is neither blocked nor undone.

## Why `kill-pane` stands in for the standdown path

The initiator's half of taskmaster's stand-down has four steps: mail the
seat to settle, wait for its reply, kill its pane, kick a successor.
`--yes` does only the kill, and that is deliberate **for now**:

- **The mail is skipped.** A mail WAKES the seat, which is the very cache
  rewrite reap exists to avoid.
- **There is no successor.** A done task gets none.
- **The seat's own half is assumed done** (settle, log, mail), because its
  task is done. The tail check stands in for it.

The close is one function, `close_seat`, so it can be swapped if that
choice changes.

## Exit codes and JSON

- **0:** a clean read, and every act closed and verified.
- **1:** a `--yes` act was refused at a gate, or a close did not verify.
- **2:** a usage error, including a bad `--idle` or a SHORT that does not
  name exactly one listed seat.
- **`--json`** prints one object per listed seat. The keys are
  `session_id`, `short`, `name`, `pid`, `cwd`, `tmux_socket`, `tmux_pane`,
  `idle_s`, `verdict`, `why`, `pane_signal` (`word`, `why`), `task_id`,
  `task_status` and `task_dir`. With `--yes`, each object also has `act`
  (`outcome`, `why`), which is null for a row that was not acted on.

## Limits: known, and accepted for v1

- **No check-and-kill is atomic.** After the last pane-signal read, three
  tmux calls and one `ps -t` run before the kill. A seat that wakes inside
  that window is closed mid-turn.
- **Files ignored by the worktree's own `.gitignore` are invisible to the
  tail.** This is accepted.
- **Processes are identified by their `ps` text.** A deliberate
  `exec -a` with the exact installed launcher path and its words passes
  as a launcher. A non-interactive `bash < script` holding the pane reads
  as an interactive shell. reap guards against closing something by
  accident; it is not built to resist an adversary.
- **The launcher pin reads the caller's PATH.** Under `uv run` in a
  checkout, `session` resolves to the checkout's `.venv`, so seats started
  by the installed `session start` read KEEP. That errs on the safe side,
  but run reap from the installed tool.
- **A launcher that takes more than 5 s to exit on its hangup** makes
  verify report `failed` and name it. This has not been measured on a real
  seat.
- **A process outside the session holding the pane's terminal open** has
  not been measured, and is out of scope.
- **The limit is per run (`--idle`).** There is no per-host or per-seat
  setting, and no nudge to settle: those are not built.
- **The `session` skill does not teach `reap`.** `tests/test_plugin.py`
  holds it in `HELD` until that is decided.

## v1 choices

Each choice is the side that closes less.

- **Hatched is not done.** A task in `review` with every delivery landed
  is KEEP; only terminal statuses close.
- **Idle seats on an open task are KEEP.** A seat waiting for its next
  piece of work is never closed.
- **Seats with no task are never closed** (coordinating seats, chat
  seats), and no flag closes them.
- **The close is `kill-pane` by pane id, never `kill-window`:** a window
  can hold a sibling seat.
- **The default limit is 1h.**

## Testing

Every test that touches tmux runs on a private `tmux -L st146-<rand>`
server under a scratch `TMUX_TMPDIR`, with fake seats. The fakes are a
process, a sidecar, a task README, and a fake `tm` launcher on the test's
PATH. Unit tests inject the listing and the process table, so they never
read the host's real seats. See [building and testing][building].

[why-kill-pane]: #why-kill-pane-stands-in-for-the-standdown-path
[limits]: #limits-known-and-accepted-for-v1
[pane-state]: pane-state-signal.md
[building]: building-and-testing.md
