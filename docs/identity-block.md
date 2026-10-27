# The identity block: who a launched session is

A session that `session start` launches knows on its first turn, with no
tool call, who it is: its id, its seat and nickname, its tmux pane,
whether it holds the seat's calls, its seat-mates, and whether it has a
signing key. One file says so, and two readers hand it on.

## The block

`session start` writes it once, in the pane, after the launch record and
before the agent starts:

```text
# Who you are (at launch)

- **Session ID**: 3f9c2a1e-0b0a-4908-8706-050403020100
- **Seat**: myproject+build.foreman
- **Nickname**: myproject+build.foreman.3f9c2a1e
- **Pane**: %271
- **Call holder** (at launch): yes
- **Seat-mates** (at launch): none
- **Key**: unknown (signing-key default pending a decision)

Seat-mates and call holder change after launch: re-check with `session list`.
```

- It lives at `identity.md` in the session's dir, under the sessions
  leaf (`$SESSIONS_DIR`; else `~/.session/sessions` while it is real;
  else `~/.sessionkit/tools/state/sessions` when it exists; else
  `~/.session/sessions`; `session path sessions` prints it). It is written atomically, mode 0600. Read it through
  `session identity`, not by its path: the root can move.
- A line that is not known says so and why, for example
  `none (not in tmux)`. It never guesses.
- The block never goes on a command line: `ps` is world-readable.
- A failed write never stops a launch. A resumed session gets no new
  block.

## `session identity [SID]`

Prints the block for SID, byte for byte. With no SID it reads
`$CLAUDE_SESSION_ID`. A unique prefix of an id is enough.

```sh
session identity 3f9c2a1e
```

| exit | stdout | stderr |
| --- | --- | --- |
| 0 | the block, unchanged | nothing |
| 3 | nothing | one line: the session has no block (`session start` did not launch it, or launched it before blocks were written) |
| 2 | nothing | one line: no SID and no `$CLAUDE_SESSION_ID`, a SID that is not a session id, or a bad option |
| 1 | nothing | one line: any other fault, such as an ambiguous prefix or an unreadable file |

It reads one file: no network and no subprocess. A file larger than
16 KiB is a fault, never a partial block. Scripts should treat any exit
other than 0 and 3 as a fault.

sessionmail's `memory wake` runs `session identity <sid>` and prints its
stdout as it is.

## The identity hook

The plugin's SessionStart hook `hooks/session-info identity` adds the
block to the session's context before the first prompt, with the same
bytes `session identity` prints. See [the session-info hooks][hook].

- **It is on, and its switch is the file.** A session with no block says
  nothing. `SESSIONTOOLS_HOOK_IDENTITY=off` (or `0`, `false`, `no`)
  turns it off before python starts.
- It runs on every source. After `resume` or `compact` the block is
  added again; its changeable lines say "at launch". After `/clear` the
  session has a new id with no block, so the hook says nothing.
- A subagent's payload is silent, and the hook never breaks a session.

[hook]: session-info-hook.md
