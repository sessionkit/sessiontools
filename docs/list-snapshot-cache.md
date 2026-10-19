# The `session list` cache: one computation, many readers

`session list` keeps its last result and serves it while it is young (2 s
by default), so K pollers running it together cost ONE computation. The
output is the same as before, byte for byte. Use `--fresh` when a
live/idle decision cannot take an answer up to 2 s old.

- **Why:** several pollers run `session list` every 2-3 s, and each run
  used to pay for the whole listing.
- **Default on:** caching is the default.
- **Only the clocks move:** `runtime_s` and `idle_s` are still counted
  to the moment of printing.

## Max age

- 2 s by default. A snapshot older than that is recomputed.
- `SESSION_LIST_MAX_AGE` (seconds), else `list.max_age` in
  `~/.sessionkit/tools/settings/session.json` (template and host layers).
- `0` turns the cache off. A value that cannot be read is passed over.

## Single flight

- A caller that finds no young snapshot takes a lock (flock) and
  computes.
- Callers that arrive meanwhile wait on the lock and print that one's
  result.
- A lock held past 15 s (on the monotonic clock) is given up, and that
  caller computes on its own.
- A holder that dies, even by SIGKILL, drops the lock with it, so a
  waiter takes it at once and computes.

## `--fresh`

- `--fresh`, or `SESSION_LIST_FRESH=1`, computes now, waits for no one,
  and still refreshes the snapshot.
- **Who needs it:** a caller that decides with what it reads: whether to
  start a seat ("is X running?"), kill a pid, or treat a seat as dead.
  Display and metrics pollers keep the cache.

## Where it lives

- `$SESSION_STATE_DIR/list/snapshot.json`, else
  `~/.session/state/list/snapshot.json`, beside `list.lock`.
- The folder is 0700 and the file 0600 (it names every seat's cwd).
- It is written to a temp file and renamed, so a reader never sees half
  of one.

## Never served

- A snapshot that is missing, corrupt, stale or stamped in the future.
- One from another snapshot schema or sessiontools version.
- One made under other `HOME` / `SESSION_SIDECAR_DIR` / `TMUX_TMPDIR` /
  `CLAUDE_PROJECTS_DIR` / `CONTEXT_MAX` values.
- One without CONTEXT when CONTEXT is asked for.
- Each of these recomputes. A state dir that cannot be written costs
  only the caller's own computation.

## Who reads it, and who never does

- `--no-context` and `--pane` are served from any young snapshot
  (CONTEXT is dropped for `--no-context`).
- `session get`, `session context` and `session start`'s front door (it
  refuses a second holder of an agent) never use the snapshot. They read
  the live sessions every call, so sessionmail's idle check
  (`session get idle`, or the library) is never 2 s old. Tests pin this.
