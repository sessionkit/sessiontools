# Starting a session: `session start`

`session start [FLAGS] [AGENT] [WORDS...] [VENDOR FLAGS...]` launches a
coding-agent session from an agent file. The agent's front matter is
the launch configuration; the command line overrides it. Trailing words
are the first message.

- `session start --list-agents`: the agents it can start.
- `session start --dry-run AGENT`: what would run. It launches nothing.
- `session start --in-tmux AGENT`: in a new tmux window, not in place.
- `session start AGENT review the auth`: the words after AGENT are the
  first message.
- Flags go BEFORE the agent name.
- Exit codes: 0 ok, 1 launch failed, 2 usage or a refusal, 3 the agent
  is already held (see [the front door][door]).

## Where agent files live

- `$AGENTS_DIR` when set.
- Else `~/.session/agent/config`, once that folder exists.
- Else `~/.session/agents`, which keeps a symlink per agent after the
  move.
- Two layouts, the directory one winning: `<root>/<name>/<name>.md` and
  the flat `<root>/<name>.md`. `--list-agents` answers by the same
  rules, so it never offers a name that will not start.
- Every session's directory is `~/.session/sessions/<sid>/`, whatever
  its agent; `$SESSIONS_DIR` overrides.

## Flags

- `--dry-run`: show what would run; launch nothing.
- `--idle`: boot self-directed; run no context generators. With an
  agent file, the first message is one line telling the agent to boot
  from its file. With no agent file, nothing is injected.
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

## The front door: one holder

- A hand launch (no claude `-n`/`--name`) of an agent that a live Claude
  Code session already holds is refused, exit 3, naming the holder and
  its age.
- `--allow-second-holder` launches anyway, with one stderr line naming
  whom it starts beside.
- The front door reads the live sessions every call, never the
  [`session list` cache][cache].

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
  secrets:              # NAME: backend:ref, resolved at launch (`pass` today)
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
- Only `model`, `effortLevel` and `statusLine` are passed on. Any other
  key gets one stderr line, `claude-code: KEY (from FILE) not rendered in
  v1`, and is dropped.
- A `--model` or `--effort` typed on the command line still wins:
  claude ranks its own flags above `--settings`.
- With no `claude-code` file anywhere, nothing changes: no `--settings`.
  With no `statusLine` in any layer, the file is what it was before.
- A `--settings` typed on the command line is folded in as the top
  layer, above the session's. Its keys win over every other layer,
  except a key a lower file locks with `_locked`, and they are passed
  on as given. The launch still carries one `--settings`, the
  computed file, and the dry run names the folded file. It may be a
  file or inline JSON (`--settings '{"model": "opus"}'`); `~` is the
  launch's home, and a relative path is read under the session's
  working directory. One the launcher cannot read, one that is not a
  JSON object, or a second `--settings`, refuses the launch with exit 2,
  naming it. With no `claude-code` file anywhere, it is not read and
  passes to claude unchanged.
- A `--settings` in the agent file's `claude:` section gets none from
  the launcher: no model, no effort, no status line. Its argv is
  unchanged, and one stderr line names the host file not applied and
  the launch's own file.
- A resume carries the file too.
- `~/.claude/settings.json` is never written.

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
  `CONTEXT GENERATOR FAILED` block.
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
[upgrading]: settings.md#upgrading-from-010
