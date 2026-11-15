# Starting a session: `session start`

`session start [FLAGS] [AGENT] [WORDS...] [VENDOR FLAGS...]` launches a
coding-agent session from an agent file. The agent's front matter is
the launch configuration; the command line overrides it. Trailing words
are the first message.

- `session start`: no agent named; it takes [the default seat][seat]
  when it is free, and starts plain when it is not.
- `session start -`: always plain, with no agent.
- `session start --list-agents`: the agents it can start.
- `session start --dry-run AGENT`: what would run. It launches nothing.
- `session start --in-tmux AGENT`: in a new tmux window, not in place
  (from outside tmux, in session `agents`).
- `session start AGENT review the auth`: the words after AGENT are the
  first message.
- Flags go BEFORE the agent name.
- A launch in place needs a terminal: stdin and stdout must both be one.
  With no terminal (stdout piped, say) it is refused with exit 2 before
  anything happens. Run it from a terminal, or add `--in-tmux`, which
  gives the session a tmux window of its own. `--dry-run`, and a typed
  `-p`/`--print`, `-h`/`--help` or `-v`/`--version`, need no terminal.
- Exit codes: 0 ok, 1 launch failed (a [required context
  generator][required] that failed among them), 2 usage or a refusal, 3
  the agent is already held (see [the front door][door], and [the
  default seat][seat] for `session start claude`).

## Where agent files live

- `$AGENTS_DIR` when set.
- Else the `agent` leaf's `config/` (`session path agent --config`),
  once that folder exists. On a fresh host, one with no `~/.session/`
  of its own, it answers before it exists too.
- Else `~/.session/agents`, which keeps a symlink per agent after the
  move (an older host's folder; a fresh host has none).
- Two layouts, the directory one winning: `<root>/<name>/<name>.md` and
  the flat `<root>/<name>.md`. `--list-agents` answers by the same
  rules, so it never offers a name that will not start.
- Every session's directory is `<sid>/` in the `sessions` leaf
  (`session path sessions SID`), whatever its agent; `$SESSIONS_DIR`
  overrides.
- A named agent reaches Claude Code as a one-agent plugin and as an
  `--add-dir` copy at `.claude/agents/NAME.md`, which reads by the plain
  name. That copy leaves out `hooks`, `mcpServers` and `permissionMode`:
  the plugin copy never honoured them, so the seat behaves as it did before.
- With Claude Code's `project` setting source off
  (`--setting-sources ""`), a named agent keeps the scoped
  `session-agent-NAME:NAME` name, and only the shipped status line's
  strip shows it plain.

## Flags

- `--dry-run`: show what would run; launch nothing.
- `--idle`: boot self-directed; run no context generators. With an
  agent file, the first message is one line telling the agent to boot
  from its file. With no agent file, no context is injected. The user's
  CLAUDE.md is still carried when the sources leave it out: it is
  Claude Code's own file, not launcher context (see [the user's
  CLAUDE.md][user-memory]).
- `--converse`: tell the agent this session can speak. It is a
  capability, not an order to speak first.
- `--keyed [--key-label NAME]`: mint a fresh Ed25519 session key and
  derive the session id from it; `--key-label` labels the key instead
  of the agent name.
- `--seat SEAT`: the seat this session takes (for example
  `web+reviewer`), written to the session's settings at birth so
  `session settings` reads the seat's layer.
- `--directory DIR`: run the session in DIR. It beats the agent's
  `start: directory:`, which beats the directory you typed it in.
- `--first-message-file-over BYTES`: carry a first message longer than
  BYTES in a 0600 file and pass a pointer, keeping it out of `ps`
  (`0` = always).
- `--list-agents`: name the agents.
- `--allow-second-holder`: launch even though a live session holds the
  agent.

## tmux placement

- `--in-tmux` places the session in a new tmux window instead of
  exec-ing in place. (Not `--tmux`: claude owns that flag.)
- `--tmux-target SESSION[:WINDOW]` implies `--in-tmux`. SESSION is
  matched by exact name. The window is named exactly the agent unless
  WINDOW is given: `--tmux-target work reviewer` gives window
  `reviewer`.
- The old `--window-name` is gone (since 2030-12-08): to name the window
  yourself, write `--tmux-target SESSION:WINDOW`.
- `--switch` moves the tmux client to the new window; `--no-switch`
  never does, even from an interactive terminal inside tmux.
- Outside tmux, with no SESSION given, the window goes in session
  `agents`. It is created (detached) if it is missing and reused if it
  exists, and the agent's window is made its current one. The line it
  prints ends with how to reach it: `attach: tmux attach -t agents`.
  `--dry-run` says which session it would use and touches no tmux.
- **The window runs the line you typed.** It runs `session start` again,
  with `--in-tmux`, `--tmux-target`, `--switch` and `--no-switch` taken
  off, in the folder you typed it in. `tmux display -p
  '#{pane_start_command}'` shows it, and `tmux respawn-pane -k` starts a
  fresh session from it. Nothing is written to a launch script.
- **Message words stay out of `ps`.** Words typed after the agent name go
  to a 0600 file, `<state dir>/launches/<token>/first-message.md`, and the
  window's line names that file instead. A respawn reads it again; if it
  is gone, the respawn refuses and says so. These files are not cleaned up
  yet.
- **The session id comes from the window.** The command that places it
  mints nothing, so its line says `session id minted in the pane`. The
  window's own launch records the line you typed (`session get
  launch_command`). The line reaches the record through the launch
  script's environment, never its command line, so it is not in `ps`
  either, and the session does not inherit it.
- **A placed window resolves in tmux's environment**, not yours. Its
  variables are the tmux server's global environment, with the target
  session's own on top. So `AGENTS_DIR=/x session start --in-tmux pip`
  finds `pip` in tmux's `AGENTS_DIR`, not in `/x`. When you have set
  `AGENTS_DIR`, `SESSION_STATE_DIR` or `SESSIONS_DIR` to something tmux's
  environment does not hold, the placing command prints one warning line
  per variable, naming it. The launch still goes ahead.
- To give the window your value, set it in tmux first:

  ```bash
  tmux set-environment -g AGENTS_DIR /x
  ```
- **A window that fails stays open** with its error and tmux's `Pane is
  dead (status N)`: a launch refused in the window (exit 3), or a shell
  that cannot find `session` (status 127). A window whose session exits
  cleanly closes. The window's PATH is the PATH of the command that
  placed it (tmux 3.8), so `session` must be on that PATH.

## The terminal title

- Just before the vendor starts, the launch sets the terminal's title to
  a short name: the seat (`--seat`), else the agent's name, else the
  vendor's (`claude` for a plain session). A terminal that titles its tab
  from the running command's arguments would otherwise show the whole
  launch line until the vendor sets a title of its own.
- It is one title escape (OSC 0) on stdout, written only when stdout is
  a terminal. Control characters are taken out of the name first.
- `CLAUDE_CODE_DISABLE_TERMINAL_TITLE`, set to anything but empty, turns
  it off, as it turns off Claude Code's own title.
- Nothing of the launch runs after the vendor exits, so it does not put
  the old title back itself. Claude Code clears the title as it exits,
  which removes this one too. A vendor that is killed before it can
  clear the title leaves this one until the shell or the next program
  sets its own.
- Under tmux it sets the pane's title. The window's name is set as
  described above.
- `--dry-run` shows the title line in the launch script it prints.

## The front door: one holder

- A hand launch (no claude `-n`/`--name`) of an agent that a live Claude
  Code session already holds is refused, exit 3, naming the holder and
  its age.
- `--allow-second-holder` launches anyway, with one stderr line naming
  whom it starts beside.
- **A holder** is a live Claude Code session of that agent, or a live
  claim on it (below): a launch still starting up holds the agent too.
- **Your own pane:** a direct start typed in the holder's own tmux pane
  replaces it (`respawn-pane -k`), unless the holder is the launcher or
  one of its ancestors. A start nested in the holder's own shell is
  refused, and so is a start with `--in-tmux`: a tmux placement never
  replaces anything.
- **The claim:** a hand launch of a free agent takes the agent's lock
  (`holders/<agent>.lock` in the state dir) and writes
  `holders/<agent>.json` before it execs, the same claim the default
  seat writes. Two launches of one agent started together give one
  holder; the second is refused, exit 3. With `--in-tmux` the command
  that places the window only checks for a holder; the launch inside the
  window takes the claim. Two placements started together can both get
  past the check: the second window's launch is then refused, and the
  window stays open showing why. A dry run, a
  spawner's `-n` launch and a launch beside a holder with
  `--allow-second-holder` take no claim.
- **Fail open, loudly:** when the process table or the claim cannot be
  read, the lock stays busy for 10 s, or the claim cannot be written,
  the launch goes ahead with one stderr line saying so. Only a measured
  live holder refuses.
- The front door reads the live sessions every call, never the
  [`session list` cache][cache].

## The default seat: one per host

A bare `session start` takes the default seat, the one session per host
named for the vendor (`claude` today), when it can hear and no live
session holds it. Otherwise it starts plain with one stderr line. What
it means for you, shadowing included, is in
[the default seat][seat-user]. This section covers how it is decided.

- **The order:**
  1. The gates, before anything is locked or written: macOS (sessionmail
     cannot yet read Linux start times), then `message box path claude`,
     given 5 s.
     Exit 2 means the box is missing; no `message`, a timeout or any
     other exit means it could not be checked.
  2. Under the seat's lock, who holds it: a live claim first, then a
     live Claude Code session named `claude`, from one process-table read.
  3. Free: the claim is written before the launch execs, and the launch
     goes on as the seat.
  4. With the lock let go, [the home-folder offer](#the-offer-start-in-work-instead)
     when it applies, then the mint. A question waits on a person, so it
     is never asked under the lock; the claim already holds the seat.
- **The claim:** `holders/claude.json` in the state dir
  (`session path state`), beside its lock
  `holders/claude.lock`. It records the session id, the pid, that pid's
  start time, the tmux pane and how it was taken. Nothing releases it.
  It goes stale when the pid ends or the pid's start time differs (a
  reused pid), and the next start takes it over. A claim that does not
  parse is stale too; one that exists and cannot be read fails closed.
- **Fail closed:** when the process table or the claim cannot be read,
  the lock stays busy for 10 s or the claim cannot be written, a bare
  start runs plain
  with the `UNSURE` line. A wrong "free" would be a second session
  hearing the seat's mail.
- **Your own pane:** a direct start typed in the holder's own tmux pane
  replaces it (`respawn-pane -k`), unless the holder is the launcher or
  one of its ancestors (a start nested inside the seat is held). A start
  with `--in-tmux` never replaces anything.
- **tmux:** with `--in-tmux` the command that places the window takes
  no claim. The launch inside the window takes the seat, as a start
  typed there would. A claim left by an older launcher with no id says
  `id minted in the pane` in the held line.
- **The definition goes inline.** Claude Code keeps the name `claude` for
  its own built-in agent, so the seat is passed as
  `--agents '{"claude": {"description": ..., "prompt": ""}}' --agent
  claude`, with no plugin dir. The prompt is empty, and the description
  is always the shipped one.
- **The agent word is never inherited.** `CLAUDE_CODE_AGENT` is part of
  the session identity the launch script's first line unsets, and a
  tmux server `session start` creates never holds it. The seat, and
  every named launch, gets it from `--agent`, which Claude Code turns
  back into the variable. A plain launch has none, so its sidecar names
  no agent and it does not hear box `claude`. A server born inside a
  seat before this fix keeps the word until an operator removes it:
  see [the default seat][seat-user].
- **Reads as bare:** the built-in counts as no agent file for `--idle`,
  `--converse` and the settings layer. The session's birth settings
  record `agent: claude` and no agent file. A shadowing file counts as an
  agent file.
- **A bare `--keyed`** is still refused, exit 2, before any gate runs.
  `--allow-second-holder` does nothing on a bare start.
- **The dry run** takes no lock and writes nothing. It prints one `seat:`
  line after `mode:` saying what a live launch would do.

### A named launch of the seat word

`session start claude`, fresh or with `--resume ID` or `--continue`,
meets the seat's own check, never [the front door][door] alone. The
front door misses a holder that has claimed the seat but not yet
started.

- The gates decide only the line. A failed gate is followed by a
  check that reads and writes nothing.
- **Free:** it takes the seat under the lock, gate or no gate.
- **Held:** a hand launch is refused, exit 3, with the front door's line.
  `--allow-second-holder` starts beside it with the front door's line
  and takes nothing. A spawner's `-n` starts plain with `HELD`. Only the
  holder's line prints, never the gate's too.
- **Not checkable:** it launches as named with the named `UNSURE` line,
  and takes nothing. Beside a failed gate, both lines print.

### The lines

Each is one stderr line after `session start: `; the exit code does not
change. HOLDER reads like `session 37f09460-1111-2222-3333-444455556666
(pid 78996, tmux %4, live 23m)`, or `a session (id not known yet)` before
the id is.

| name | when | the line |
| --- | --- | --- |
| `TAKEN` | it took the seat | `this session is claude, the default seat on this host (box claude)` |
| `HELD` | a live session holds it | `starting plain: the default seat claude is held by HOLDER, so this session will NOT hear Delta messages or calls; that one does` |
| `NO_BOX` | box `claude` is missing | `starting plain: box claude is missing, so this session will NOT hear Delta messages or calls; repair: message setup --box claude` |
| `UNBOXED` | the box could not be checked | `starting plain: could not check box claude (WHY), so this session will NOT hear Delta messages or calls` |
| `LINUX` | not macOS | `starting plain: the default seat claude is macOS-only for now: sessionmail cannot yet read Linux start times, so this session will NOT hear Delta messages or calls` |
| `UNSURE` | the holder could not be checked | `starting plain: could not check who holds the default seat claude (WHY), so this session will NOT hear Delta messages or calls` |
| `RESUMED` | `session start - --resume ID` of a session born as the seat | `resuming plain: session 37f09460 was started as claude, the default seat, and a resume through '-' does not take it, so this session will NOT hear Delta messages or calls; 'session start claude --resume ID' takes it back while it is free`; when the seat is held, the tail is `the default seat claude is held now by HOLDER` |
| named, cannot hear | a named launch took the seat but a gate failed | `this session is claude and holds the default seat, but cannot hear yet:` then the gate's reason (`may not hear yet` when the box only could not be checked) |
| named `UNSURE` | a named launch could not check the holder | `this session is claude: could not check who holds the default seat claude (WHY), so it takes no claim and may be a second holder` |
| moved | the tmux claim could not move to the pane | `placed, but the default seat claude's claim could not move to the new pane (WHY); the seat reads free until that session writes its sidecar` |

## The declared environment

Nothing reaches a session's environment that its agent file did not
declare. `environment:` has four keys:

```yaml
environment:
  variables:            # NAME: value, set in the session only
    VOICEMODE_TTS: kokoro
  files:                # sourced in order, later wins; ./ is the session's dir
    - ~/.voicemode.env
    - ./.voicemode.env
  secrets:              # NAME: backend:ref, resolved at launch (`pass`, `passage`)
    GITHUB_TOKEN: pass:github/token
  inherit:              # names taken from the launcher's own environment
    - CLAUDE_CODE_OAUTH_TOKEN
```

- Order: `variables:`, then `files:`, then the secret file (`secrets:`
  and `inherit:`) last, so a stray `.env` line cannot shadow a
  credential.
- `secrets:` values and `inherit:` values reach the session in a 0600
  file the launch script sources and removes before the vendor runs.
  Neither is ever on argv, in the script, or in the tmux server. Both
  are reported by name, never by value, and masked in the launch record.
- `pass:REF` is the first line of `pass show REF` (gpg). `passage:REF`
  is the first line of `passage show REF` (age; the store is
  `$PASSAGE_DIR`, else `~/.passage/store`). Each says on stderr, before
  it waits, that it may ask for a PIN or a touch. A `passage:` entry with
  no `.age` file fails before anything decrypts, so a typo costs no touch.
- A secret that fails to resolve aborts the launch. An `inherit:` name
  the launcher does not hold does not: the banner says
  `inherit: NAME not set in the launcher's environment -- not carried`
  on stderr, and the session starts without it. A carried name prints
  `inherit: NAME carried from the launcher's environment`.
- A dry run calls no backend and reads no `inherit:` value; it lists
  both by name.
- A name may have one source: one under `inherit:` and also under
  `variables:` or `secrets:` is refused, exit 2.

### Credentials are never handed down

A session started from inside another does not inherit its account.
`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` and
`ANTHROPIC_AUTH_TOKEN` are dropped from every child of the launch,
including a tmux server `session start` creates, and the launch script's
first line unsets them. To pass one on, name it under `inherit:`.

**A login by token needs this.** If Claude Code signs in from
`CLAUDE_CODE_OAUTH_TOKEN` in the launcher's environment (a token from
`claude setup-token`, exported by your shell), a session `session start`
launches starts without that token unless its agent file carries:

```yaml
environment:
  inherit:
    - CLAUDE_CODE_OAUTH_TOKEN
```

The same goes for a login by `ANTHROPIC_API_KEY` or
`ANTHROPIC_AUTH_TOKEN`: name that variable instead. A `/login` login,
kept in the Keychain, is not affected: nothing here touches it.

The launcher says when it drops one. If one of these names is set in its
environment and the agent file does not declare it under `inherit:`,
`secrets:` or `variables:`, it prints one line on stderr before the
launch, a dry run included, naming the variable and the fix:

```text
session start: CLAUDE_CODE_OAUTH_TOKEN is set here but not handed down; to carry it, add `environment: inherit: [CLAUDE_CODE_OAUTH_TOKEN]` to /path/to/agent.md
```

The line never shows the value. The token is still dropped, and the exit
code does not change.

The default drops the token on purpose. A launcher's credential once
reached every session it started, and every pane of a tmux server it
created. Carrying one is opt-in, and by name. The dry run lists the
name, `CLAUDE_CODE_OAUTH_TOKEN  <- the launcher's environment`, and
reads no value.

## The host's model, effort and status line

A host can pin the model and effort every session it starts runs on,
ahead of `~/.claude/settings.json`, which a typed `/model` or `/effort`
rewrites for every later session. It can give those sessions a
status line the same way, without touching `~/.claude`. Write
them in the `claude-code` settings tool:

```json
{"model": "sonnet", "effortLevel": "high"}
```

- Put that in `~/.sessionkit/tools/settings/claude-code.json`, the
  `tools` package's settings folder. No other folder is read for it.
  Files in an older folder are not read until you move them; stderr
  says so once ([upgrading][upgrading]).
- An agent's `settings:` front matter (`claude-code.model: haiku`), a
  seat's `settings/claude-code.json` and a session's override the host,
  as for every tool (`session settings SID claude-code` shows the result
  and the file behind each key).
- The launcher writes the result to a 0600 file in the session's
  `launch/` directory and passes it as `--settings`. The dry run's
  command lists it with the layer and file of each value; the launch
  record names the file.
- Only `model`, `effortLevel`, `statusLine`, `autoCompactWindow`,
  `idleCompaction` and `tui` are passed on. Any other key gets one stderr
  line, `claude-code: KEY (from FILE) not rendered in v1`, and is dropped.
- `autoCompactWindow` (100000 to 1000000 tokens) and `idleCompaction`
  (`true` or `false`) are passed on only when a layer sets them; unset,
  Claude Code's own default stands. A value Claude Code would refuse or
  drop gets one stderr line naming the file and what the key needs.
  `"auto"` is one of those: Claude Code takes it only per model, under
  `modelSettings`, so leave the key unset instead. The settle alert reads
  the window back from this same file (`sessiontools.compact_point`) to
  count down to compaction. It also reads `autoCompactEnabled` there and
  in Claude Code's own settings files: off anywhere Claude honours it
  means no point, and the alert prints `?`.
- A `--model` or `--effort` typed on the command line still wins:
  claude ranks its own flags above `--settings`.
- The file also carries `"tui": "default"` when nothing names `tui`
  ([Claude Code's first-run screens](#claude-codes-first-run-screens)),
  so there is a `--settings` even with no `claude-code` file anywhere.
  With no `claude-code` file and a `tui` in your own Claude settings,
  nothing changes: no `--settings`. With no `statusLine` in any layer,
  the file has none.
- A `--settings` typed on the command line is folded in as the top
  layer, above the session's. Its keys win over every other layer,
  except a key a lower file locks with `_locked`, and they are passed
  on as given. The launch still carries one `--settings`, the
  computed file, and the dry run names the folded file. It may be a
  file or inline JSON (`--settings '{"model": "opus"}'`); `~` is the
  launch's home, and a relative path is read under the session's
  working directory. One the launcher cannot read, one that is not a
  JSON object, or a second `--settings`, refuses the launch with exit 2,
  naming it. When there is no file to fold it into (no `claude-code`
  file anywhere, and a `tui` in your own Claude settings), it is not
  read and passes to claude unchanged.
- A `--settings` in the agent file's `claude:` section gets none from
  the launcher: no model, no effort, no status line. Its argv is
  unchanged, and one stderr line names the host file not applied and
  the launch's own file.
- A resume carries the file too.
- `~/.claude/settings.json` is never written.

### Claude Code's first-run screens

- **The fullscreen-renderer offer** greets a session while no setting
  names Claude Code's `tui`. A session `session start` launches is not
  shown it: its `--settings` file carries `"tui": "default"`, the
  renderer it already runs. Your own files are read, never written.
- The launcher sets it only when nothing else names `tui`: a
  `claude-code` layer, a typed `--settings`, or Claude's own settings
  (`~/.claude/settings.json`, or `$CLAUDE_CONFIG_DIR/settings.json`
  when that is set, and the working directory's `.claude/settings.json`
  and `.claude/settings.local.json`). A file that is missing or not
  JSON names nothing.
- `--settings` outranks your own files, so the launcher never sets
  `tui` over them. To use the fullscreen renderer, run `/tui fullscreen`
  once in any session: it writes your own settings, and from then on
  the launcher leaves `tui` alone. To pick one for every launched
  session, put `"tui"` in `claude-code.json`.
- **The theme picker and the folder-trust question are still asked.**
  They are not settings: Claude Code keeps the answers in its own
  `~/.claude.json`, and no `--settings` key or environment variable
  answers them. The launcher does not write that file. The theme is
  asked once for each user, and trust once for each folder.

### The status line

It goes in the same `claude-code` layer. The kit keeps it in a drop-in
of its own, `~/.sessionkit/tools/settings/claude-code.d/50-statusline.json`,
so a person's `claude-code.json` is never edited for it.
`session statusline install` writes that drop-in and the script beside
it ([the status line script](status-line-script.md)):

```json
{"statusLine": {"type": "command",
                "command": "~/.sessionkit/tools/settings/statusline.sh",
                "padding": 1}}
```

Install also writes an `_about` key, which says what wrote the file. It
is never passed on.

- It is passed on as one object. It needs `type: "command"` and a
  non-empty `command`; without them none of it is passed on, and one
  stderr line names the file of its `command`.
- `padding` (an int, 0 or more), `refreshInterval` (an int, 1 or more)
  and `hideVimModeIndicator` (true or false) are kept. Any other field,
  or one of the wrong type, gets its own `not rendered in v1` line and
  is dropped alone.
- It merges field by field, like every map. A seat that sets only
  `statusLine.command` runs its own script and keeps the host's
  `padding`. `"statusLine": null` in an agent, seat or session layer
  removes it; that seat then shows `~/.claude`'s line, if there is one.
- The dry run names the file of its `command`.

Which status line a seat shows, highest first:

1. A managed `statusLine` (or managed `disableAllHooks`).
2. The `--settings` file: session, then seat, then agent, then the kit
   host layer.
3. The project's `.claude/settings.local.json`, then its
   `.claude/settings.json`.
4. `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`).

Limits:

- **Only a seat launched by `session start` gets the kit's line.** A
  bare `claude` reads `~/.claude` and the project files only. To get it
  in a plain `claude`, alias it: `alias claude='session start -'` (the
  `-` means no agent).
- A `--settings` typed on the command line is folded on top (above),
  so a `statusLine` in it wins. One in the agent file's `claude:`
  section gets nothing from the kit layer, and shows that file's status
  line.
- Never edit `~/.claude` to get the kit's line. A `statusLine` already
  there is left as it is; a `session start` seat shows the kit's over it.

## Context generators: a declared informational exit

A `start: context:` entry may be a mapping instead of a command string,
to declare which non-zero exits are information rather than failure.
The launcher never guesses; it honours only the codes listed:

```yaml
start:
  context:
    initial:
      - git status --short             # a string: as before
      - run: pager status
        informational-exit: [3]        # the pager's documented quiet
```

- When `pager status` exits 3 (claude is not running yet, so nothing
  can be listening, which is not deafness), its block is headed
  ``CONTEXT GENERATOR NOTE: `pager status` exited 3. This agent file
  declares exit 3 informational for this generator, so this is its
  answer, not a failure.``, and its stdout follows in full.
- When stderr has a last line, the header adds it after the code
  (`exited 3 -- LINE.`).
- Any other code, a timeout, or a broken built-in keeps the
  `CONTEXT GENERATOR FAILED` block. For an entry marked
  [`required: true`][required], it refuses the seat instead.
- `informational-exit:` is a list of integers from 1 to 255. Anything
  else is refused, exit 2.

### The launch-info marker

When the `launch-info` built-in renders with a session id, the launch
script exports `SESSIONTOOLS_LAUNCH_INFO=<that id>`, so the
[session-info hook](session-info-hook.md) does not print the id again.
Every other launch only unsets an inherited one, on the script's first
line beside the session's identity: no `launch-info` entry, `--idle`, a
resume, a session id minted in the tmux pane, or a `launch-info` that
failed. The export comes before the declared environment, so an agent
file that sets the variable under `variables:` wins. A dry run shows the
line in the script it prints.

## The user's CLAUDE.md

Every seat reads the user's own `CLAUDE.md`. Claude Code loads it only
through its `user` setting source, and that one switch loads the user
`settings.json` too (hooks, plugins, permissions, env). So when a fresh
launch's `--setting-sources` leaves `user` out, `session start` reads the
file itself and puts it first in the system-prompt file it already
writes. The user settings stay out.

- **The file:** `$CLAUDE_CONFIG_DIR/CLAUDE.md` when that variable is set,
  else `$HOME/.claude/CLAUDE.md`. Both are read from the environment
  Claude Code will get: the launcher's own, with the agent file's
  `environment: variables:` laid over it. A value that only an
  `environment: files:` or `secrets:` entry sets is not known before the
  launch, so that case uses the launcher's own value.
- **Your `--setting-sources` is never changed.** Nothing adds `user` to
  it, and the launcher opens no `settings.json` to carry the memory.
- **Where it goes:** the 0600 system-prompt file
  (`--append-system-prompt-file`), first, then the default seat's file
  body, then the `system-prompt:` generators' output.
- **The block's header** uses Claude Code's own label for the file and
  says why it was carried:

```text
<!-- source: user memory: /Users/x/.claude/CLAUDE.md -->
Contents of /Users/x/.claude/CLAUDE.md (user's private global instructions for all projects; carried by session start because this launch's --setting-sources leaves out `user`):
```

- **The dry run** names it in the system prompt's provenance:
  `computed: user memory (/Users/x/.claude/CLAUDE.md), then the default
  seat's file body, then injected context (system prompt)`. Each part
  appears only when it is there.

### When it is carried

| launch | carried? | why |
| --- | --- | --- |
| fresh, `--setting-sources ''` | yes | Claude Code drops it |
| fresh, `--setting-sources project,local` (`tm kick`, `tm task work`) | yes | Claude Code drops it |
| fresh, `--setting-sources user`, `user,project` or `=user` | no | Claude Code loads it: never twice |
| fresh, no `--setting-sources` | no | Claude Code loads all three sources |
| fresh, two `--setting-sources`, the last without `user` | yes | Claude Code keeps the last |
| `--idle` | yes | it is Claude Code's file, not launcher context |
| `--bare`, `--safe-mode`, `--restricted` | no | no customisations were asked for |
| no file, unreadable, not UTF-8, or empty | no | a quiet no-op: the argv is unchanged |
| resume (`--resume`, keyed or not) | no | a resume keeps the prompt it was born with |
| respawn (`session respawn`) | yes | it replays a fresh `session start` |
| a vendor other than claude | no | only the claude row has a `user_memory` field |

- The sources are read from the argv Claude Code will see: the agent
  file's vendor section, then the command line. The last flag wins, as
  it does in Claude Code.

### It yields your own system-prompt file

Claude Code keeps only one `--append-system-prompt-file`. When nothing
else on the launch writes that file (`--idle`, or no `system-prompt:`
generators, and no default seat body) and your agent file or command
line passes its own, the user memory is not carried. The launch runs as
it did before, and stderr (and the dry run's plan) says one line:

```text
user memory not carried: your own --append-system-prompt-file owns the system-prompt door (put /Users/x/.claude/CLAUDE.md's text in it if you want it)
```

- When the launcher writes the file anyway (generators, or the default
  seat's body), your own file is still refused, exit 2, as before.
- An inline `--append-system-prompt` is a different door: the memory
  rides beside it and never yields to it.

### What it does not do

- **No `@` imports.** Claude Code expands `@path` imports only on its own
  `user` route. The block names the imports it finds (an `@` starting a
  word, outside code spans) in one line under the header, and reads none
  of them.
- **No `~/.claude/rules/`.** Only the one `CLAUDE.md` is carried.
- **The launch record is not a load.** The launch script's record call
  lists `~/.claude/settings.json` and `~/.claude/CLAUDE.md` among the
  vendor's startup files. That is an audit of what Claude Code may read;
  it hands neither file to the seat.

## Context generators: a required entry

A `start: context:` entry marked `required: true` must work, or the
seat does not start. If it fails, `session start` prints why, writes it
down, and exits 1. The vendor never starts.

```yaml
start:
  context:
    system-prompt:
      - launch-info
      - run: memory wake
        required: true                 # no wake, no seat
      - run: pager status
        informational-exit: [3]        # both keys can be used together
        required: true
```

- **Why:** an unmarked generator that fails leaves a `CONTEXT GENERATOR
  FAILED` block and the launch goes ahead. That is right for a nice-to-have
  briefing. For a wake it is wrong: the seat starts without knowing it
  missed its wake. A truncated letter, or a `(failed rc=1)` that nobody
  saw for a week, looks the same as a good wake.
- **Where:** the key sits in the entry's own mapping, beside `run:`. It
  is not a `# required` comment, because YAML drops comments. It is not a
  separate list, because a separate list drifts away from the entries it
  names. Both `system-prompt:` and `initial:` accept it, and it means the
  same in each.
- **Opt-in:** an entry with no `required:`, or `required: false`, behaves
  exactly as it did before. An agent file with no marking launches
  exactly as before.

### What counts as a failure

- A non-zero exit that the entry does not list under
  `informational-exit:`. A listed code is the generator's answer, not a
  failure, so it does not refuse the seat.
- A timeout (the cap is 60 s). Whatever the generator wrote before it
  was stopped is kept and shown, marked `partial: timed out`. An
  unmarked timeout still shows no output, as before. A timeout kills the
  generator and everything it started, marked or not: each generator
  runs in its own process group, and the whole group is killed.
- A built-in that raises. `launch-info` can be marked, but in practice it
  does not fail.

### The value

| written | means |
|---|---|
| `true`, `yes`, `on` | required |
| `false`, `no`, `off` | not required |
| key absent | not required |
| `1`, `"true"`, `yes-please`, a list, or nothing after the colon | refused, exit 2 |

- `yes` and `on` are YAML booleans. Every other switch in an agent file
  reads them the same way.
- A misspelt key (`requred:`) is refused, exit 2, like every unknown
  key. A marking that cannot be read is never treated as "not required".

### The first required failure stops the run

- Generators run in order. After a required entry fails, nothing else
  runs: not the rest of its own list, and not `initial:` when the
  failure was in `system-prompt:`.
- Why: a wake acts as well as reads (it runs mail-scripts and marks mail
  read), so later generators should not act for a seat that is about to
  be refused. You also do not wait up to 60 s for each of them.
- So one launch reports one required failure. If a second required entry
  is also broken, the next attempt shows it.

### What you see

The refusal goes to stderr. A live launch and a dry run print the same
block:

```text
session start: REFUSED: required context generator `memory wake` exited 1 -- mail tree not found
  list:    system-prompt, entry 2 of 3 (nothing after it ran)
  output:  (16 bytes)
    wake: 3 letters
  stderr:  (20 bytes)
    mail tree not found
  record:  ~/.session/sessions/<id>/launch/refused
  the vendor was not started.
```

- The first line names the command and gives the exit code, or
  `timed out after 60s`.
- Before it, each earlier unmarked generator that failed gets its usual
  `context generator failed` line.
- `output:` and `stderr:` are printed in full, never cut. An empty
  stream reads `(none)`.
- Exit 1, "launch failed". Not 2: a generator that fails is the
  environment failing at launch time (a mail tree, a network), not a
  mistake in the agent file.
- Nothing is printed on stdout.

### The record: `launch/refused`

- A live launch writes `<session dir>/launch/refused`: 0600, written
  once, in the same folder and behind the same git check as the launch
  record (`launch/env`, `launch/command`). It opens with `key: value`
  lines: `refused`, `at`, `door`, `entry`, `run`, `reason` and
  `start`, then `launch_command` and `cwd` when the launch knows them.
  A blank line follows, then the block printed on stderr, without its
  `record:` line.
- It writes no `env` or `command` file. No vendor ran, and with a
  `command` file `session respawn` would take a seat that never started
  for one it can bring back.
- A dry run writes nothing. Its record line ends `(dry run: not
  written)`.
- If the file cannot be written, the record line says why. The seat is
  still refused and the exit is still 1.

### Where it does not apply

- **`--idle` and a resume** run no generators, so a required entry can
  never refuse them.
- **`--in-tmux`:** the command that places the window exits 0 with
  "placed". The launch inside the window is the one that refuses it. That
  window stays open with the refusal and `Pane is dead (status 1, ...)`. The
  placing command's exit code cannot carry a verdict reached later, in
  another process. `--in-tmux --dry-run` plans in the caller, so it
  refuses and exits 1 directly.

## Starting in your home folder

On macOS, a session that starts in your home folder gets one line on
stderr before the launch:

```text
session start: starting in your home folder; macOS may ask for access to Desktop, Downloads, Photos and Media Library. Start in a project folder to avoid this.
```

- Why: the agent reads the folder it starts in. Desktop, Downloads,
  Photos and Media Library sit directly in the home folder, and macOS
  guards each one with a privacy prompt. In a project folder there are
  none.
- What to do: start in a project folder (`cd` there first, or pass
  `--directory DIR`).
- The folder checked is the one the session runs in: `--directory`, the
  agent's `start: directory:`, or a resume's recorded folder, not the
  one you typed the command in. Symlinks are followed. A folder inside
  the home folder is silent.
- Other systems are silent, as is a launch with `HOME` unset. A dry run
  prints the line too.

### The offer: start in `~/work` instead

At a terminal, the line is followed by one question. If `~/work` is a
folder already:

```text
session start: start in ~/work instead? [Y/n]
```

If nothing is there yet:

```text
session start: make ~/work and start there? [Y/n]
```

- Yes is the default: press Enter, or type `y` or `yes`. The session
  starts in `~/work`, exactly as if `--directory ~/work` had named it,
  and one line says so (`starting in ~/work`, or `made ~/work; starting
  there`). A folder it makes gets the usual permissions for a new
  folder (your umask), like `mkdir`.
- Anything else is a no: `n`, `no`, a typo, or the end of input. The
  session starts in the home folder, and nothing more is printed.
- Ctrl-C at the question stops the launch with exit code 130, before
  any session or folder is created. A launch that was taking the default
  seat leaves the seat's claim behind: it names the stopped launcher, so it
  is stale at once and the next start takes the seat.
- If `~/work` exists but is not a folder (a file, or a link to nothing),
  there is no question: one line says
  `~/work is not a folder; starting in your home folder`. If the folder
  cannot be made, one line says why, and the session starts in the home
  folder. The exit code is unchanged either way.
- It never moves, renames or deletes anything. The only thing it ever
  creates is `~/work`, and only after a yes.

The question is asked only when all of these hold:

- the session would start in the home folder itself, on macOS, with
  `HOME` set;
- the folder is the one you typed the command in: not from
  `--directory` or the agent's `start: directory:`, and not a resume;
- it is not a dry run;
- both stdin and stderr are a terminal. Scripts, pipes and launches
  from another program get the warning line only, and start in the home
  folder as before.

A bare `session start` is asked too, after the default seat is decided
(taken, or held and starting plain), and so is a named launch of the
seat word. A launch refused for the seat (exit 3) is asked nothing,
like a launch the front door refuses.

To start in the home folder on purpose, without the question, name it:
`session start --directory ~ <agent>`.

## From Python

`sessiontools.start` is library first: `prepare(args)` returns a
`LaunchPlan`, and `execute(plan)` launches it.

```python
from sessiontools.start import parse_args, prepare, execute

plan = prepare(parse_args(["--dry-run", "AGENT"]))
```

- It is a port of the old launcher (the retired session repo's
  `session-start`). Every part is ported (tmux placement, resume,
  declared secrets), and the package no longer carries the old launcher.
- PyYAML is imported by `sessiontools.start.agentfile` alone: agent
  front matter is YAML.

[cache]: list-snapshot-cache.md
[door]: #the-front-door-one-holder
[required]: #context-generators-a-required-entry
[seat]: #the-default-seat-one-per-host
[seat-user]: session.md#the-default-seat
[upgrading]: settings.md#upgrading-from-010
[user-memory]: #the-users-claudemd
