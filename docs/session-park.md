# Saying a seat has settled: `session park`

`session park` is the last step of a seat's settle. It runs five checks,
writes one record, `<session dir>/park.json`, and prints what it found.
`session park --check` reads that record back, for this seat or any live
seat: parked or not, clean or not, and why.

- **Why:** an idle seat that is waiting looks the same as one that is
  finished. A park is the seat's own statement that it has settled, with
  the evidence beside it.
- **Positive evidence only.** Each check reads PASS, FAIL or UNKNOWN, and
  UNKNOWN is never PASS. A seat is clean only when all five PASS.
- **It only records.** Nothing here kills a seat, sends mail, writes
  config or computes a deadline. Whatever acts on a park is somebody
  else's, and `session reap` does not read it.
- **Parking buys no time.** The record holds when the last real work
  ended (`work_ended_at`), never the time of the park. Parking again with
  no work since keeps the old value.
- **A park holds until the seat does something.** A turn that starts after
  it, or work done after it, means not parked.
- **Run `session park` as a call of its own,** and `--check` too: no
  chaining, no pipe, no redirection. Work chained after the park
  (`session park && git push`, `; echo $?`, `2>&1 | tail`) ran after the
  record was written, so it unparks it, and `--check` says so at once.
- **`--check` writes nothing,** for any seat. Only `session park` writes,
  and only the seat's own record.

The forms:

- `session park --no-promises`: settle. It runs the checks, writes the
  record and reports.
- `session park --promise "send the summary"`: an open promise, so
  `promises` FAILs.
- `session park --handed-on <Message-ID>`: the promises were handed on,
  in that mail.
- `session park --letter <REF>`: the handover, a Message-ID or a path.
- `session park --check`: this seat. Is it still parked, and clean?
- `session park --check 8d71f3b6 --json`: another live seat, as JSON.

A park can also carry a keep-until time and a reason:

```console
session park --no-promises \
  --keep-until 2030-06-09T17:00:00+00:00 --why "waiting on a verdict"
```

## The five checks

They run in this order, every one even after an earlier one fails. A check
that raises reads UNKNOWN. When a check finds both a FAIL and an UNKNOWN,
it is FAIL.

| check | PASS when | reads |
|---|---|---|
| `tree` | no unsaved work where the seat sits | a task seat: the task's `worktree/` and its store at the task dir, read as `session reap` reads a tail ([closing idle seats][reap]). No task: the cwd itself |
| `work` | nothing running but a listener or the idle timer | the processes under the seat, sorted as the [pane signal][pane-state] sorts them; a background agent with no completion seen; a turn that ended with children still running |
| `claim` | no claim held in the cwd's task | the task's `harness/.claim/*.json` and its slate, as plain JSON |
| `mail` | no unread mail in the seat's session box | the box under `$SESSION_MAIL_ROOT` (else `~/.mail/agents`), by the rule `message list --unread` keeps: a `read` mark in the seat's own marks file, drafts not counted, superseded mail hidden |
| `promises` | the park declared none, or handed them on | the park's own flags: nothing on disk records a promise, so this one measures nothing and says so |

- **`work`: a listener** is a command on the pane signal's listener list
  (`pager listen` by default). **The idle timer** is `tm task standdown
  ... --idle-wait` and nothing else: the same verb without `--idle-wait`
  is work. The tool shell running the park or `--check` itself is skipped.
- **`claim`**, and **`tree`**, read UNKNOWN when no cwd is known: with
  nowhere to look, "no task here" would be silence read as evidence. A
  seat whose name names a different task from its cwd's is UNKNOWN for
  `claim`, as `session reap` keeps it.
- **`mail` with subscriptions.** A seat that also hears other boxes
  (`SESSIONMAIL_SUBSCRIBE`) reads UNKNOWN: those boxes are not read here.
  Only Message-IDs are named, never a subject or a body.
- **`promises`:** `--no-promises` is PASS (`declared none`),
  `--handed-on REF` is PASS, `--promise TEXT` alone is FAIL (`open: ...`),
  and saying nothing is UNKNOWN. `--no-promises` with either of the others
  is a usage error.

## Parked, and what unparks it

`--check` reads the record, then the seat's transcript and its sidecar. The
record holds until one of these, and the first one is named:

- **a turn started after the park:** a typed prompt, a pager page, a
  background completion, a compaction's summary, or input queued while a
  turn ran;
- **work done after it:** any tool call that is not the seat reading or
  recording its own state and mail. Not work: `session park`, `session
  get`, `session list`, `message list`, `message read`, `message mark`,
  `pager status`, `pager listen` and the idle timer. A command with a
  substitution (`$(...)`, backticks) or a redirection is work, and so is
  anything not recognised;
- **the sidecar says a turn is running,** written after the park;
- **the transcript cannot be read:** nothing proves the seat is still
  parked.

So checking your own seat from a later turn reads not parked: that turn
unparked it. Within the park's own turn, `session park --check` as a call
of its own still reads parked.

`work_ended_at` is the end of the last work call before the park. With no
work since the seat's previous park, that park's value is carried. With no
work at all, it is the session's start. The park's own call never counts,
whatever it chains, so a chained re-park cannot move it.

## Reading another seat: `--check SEAT`

- **SEAT** is a live seat's name, repo or session-id prefix, matched over
  the fresh listing `session list` makes. A SEAT that matches no live seat
  is exit 2. A SEAT without `--check` is a usage error: a park writes this
  seat only.
- `--check`, `--json` and SEAT go in any order.
- **Read-only:** every input is a file read, `ps`, or `git
  --no-optional-locks`, so it starts no turn in that seat and can never
  make its commits fail.
- `tree`, `work`, `claim` and `mail` run again now; `promises` comes from
  the record.
- **Its `mail` PASS needs proof.** Another seat's subscriptions live in its
  own environment, which cannot be read from here. Its own park read them,
  so a park-time `mail` PASS proves it had none. Without that, a live
  `mail` PASS reads UNKNOWN.

## `--keep-until T --why R`

- Records a request to be kept until T, with its reason. T is absolute:
  ISO 8601 with an offset (`2030-06-09T17:00:00+00:00`) or `Z`, and in
  the future.
- **Refused, exit 2, nothing written:** no offset, a date alone, a past T,
  a relative form (`+2h`), prose, no `--why` or a blank one, and `--why`
  without `--keep-until`.
- **Nothing acts on it.** No time is computed from it, and it never moves
  `work_ended_at`.
- **It is not carried over.** A park without `--keep-until` clears it.
- `--check` shows it on a `keep until:` line while the seat is parked, and
  in `record` in the JSON.

## Output: text and `--json`

Both forms come from one verdict, so they always agree. Every row here is
invented:

```text
parked: yes, at 17:01:00 (work ended 17:00:00: last work: Bash git commit)
clean: no
  tree      PASS     task WEB-153: worktree and store clean
  work      FAIL     work: make test
  claim     PASS     no claim held in WEB-153
  mail      FAIL     2 unread: <1792...@host.session-mail>, <1792...@host.session-mail>
  promises  UNKNOWN  not declared (--no-promises, --promise TEXT, --handed-on REF)
keep until: 2030-06-09T17:00:00+00:00 (why: waiting on a verdict)
```

Not parked reads `parked: no (never parked)`, `parked: no (a turn started
at 17:20:00 (...) after the park at 17:01:00)`, `parked: no (work after
the park at 17:01:00: ...)` or `parked: no (park.json unreadable: ...)`.
The live checks are still printed, so you see what a park would find.

`--json` prints one object: `v`, `session_id`, `name`, `parked`,
`why_not_parked` (null while parked), `turn` (what unparked it: `kind`,
which is `turn`, `work`, `busy` or `transcript`, with `at` and `origin`;
null while parked), `clean`, `checks` (each `name`, `word`, `why`) and
`record`, the park as read, or null.

**The record, `park.json`** (contract v1), written atomically, `0600`, in
the session dir (`$SESSIONS_DIR`, else `~/.session/sessions`), which must
already exist: `v`, `session_id`, `parked_at`, `work_ended_at`,
`work_ended_why`, `letter`, `keep_until`, `keep_why`, `promises`
(`declared`, `open`, `handed_on`), `checks` and `clean`. Times are epoch
seconds. There is no deadline field. A record that is not a whole v1
record for this session is unreadable, and unreadable means not parked.

## Exit codes

| verb | 0 | 1 | 2 | 3 | 4 |
|---|---|---|---|---|---|
| `session park` | parked, clean | parked, not clean: the record is written; fix and park again | usage, no session id, or no session dir: nothing written | - | the write failed: nothing written |
| `session park --check` | parked, clean | parked, not clean | usage, no SEAT and no session id, or no such live seat | not parked: never parked, unparked, or the record unreadable | - |

- **An internal error also exits 2,** never 0 or 1, so a caller never reads
  a crash as a park. It prints `internal error (...): no verdict`. Exit 2
  then means no verdict, not always nothing written: a crash after the
  write leaves the record on disk.
- `session park --help` lists the same codes.

## Limits

- **A seat cannot read itself as parked from a later turn:** that turn
  unparked it. Ask from another seat, or in the park's own turn.
- **Promises are declared, not measured.** Nothing on disk records one.
- **The listener list and the idle timer are matched by command text,** as
  the pane signal matches them.
- **The `session` skill does not teach `park`.** `tests/test_plugin.py`
  holds it in `HELD`, beside `reap`.

## Testing

The tests use a temp HOME, temp `$SESSIONS_DIR` and mail root, fake
listings and process tables, and a real git store and worktree in a temp
dir. No test reads the host's seats, sends mail or writes live config. See
[building and testing][building].

[reap]: session-reap.md
[pane-state]: pane-state-signal.md
[building]: building-and-testing.md
