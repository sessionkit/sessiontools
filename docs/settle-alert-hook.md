# The settle alert (a plugin hook)

The settle alert is the one context warning a session hears. It has a
soft line and a hard line, each set by the `session` tool's `settle.*`
settings. The plugin's `hooks/settle-hook` (PostToolBatch) reads them
**live through the settings cascade on every tool batch**, and says at
most one line a batch. **With no `settle` set anywhere it is silent, and
python never starts.**

```text
You're past 250K (262K used). At a good stopping point, settle: save what matters, then hand over.
Now: 352K used, 73K before compaction. Settle: save what matters, then hand over.
```

- **Off by default.** No layer sets `settle`: nothing is said.
  `settle: null` in a layer turns it off until a layer below sets a
  `settle` key again. A host `_locked: ["settle"]` pins it for every
  layer below.
- **Live.** Each batch re-reads every layer, so an edited file is heard
  at the next tool batch, with no relaunch.
- **An agent overrides the host,** values and words alike, from its
  file's `settings:` front matter. A seat's file and a session's file
  come after the agent and win over it.
- **Birth-only.** Only a session `session start` launched is told. A
  session it did not launch has no session dir, so it gets no copy of
  `SESSION_SETTLE_AT` and no warning, even when the host layer sets
  `settle` ([who is told][who]).
- **A shell-rc `SESSION_SETTLE_AT` export lands in the session layer
  and beats every seat.** `session start` copies it once, at birth, into
  the newest layer there is, so it outranks the host, the agent and the
  seat. Only a `_locked` on `settle` in a layer above holds it off
  ([the environment][env]).
- **Its belt is a `PreCompact` guard.** It skips an auto compaction
  below the compaction point the launch set, so a seat is not compacted
  before its lines are said ([the guard][guard]).
- **`SESSIONTOOLS_HOOK_SETTLE` is retired.** Nothing reads it; `settle:
  null` is the off switch.
- **Every key, with its default and layer:** [the `session` keys][keys].
  **The compaction keys:** [the `claude-code` keys][cc-keys].

## A worked example

A soft line from 250K, once every 25K, and one hard line, "now", from
350K or 25K short of compaction, whichever comes first.

- In the host file, `~/.sessionkit/tools/settings/session.json`, or a
  seat's or session's `settings/session.json`:

  ```json
  {"settle": {"soft": {"at": 250000, "every": 25000},
              "hard": {"at": 350000, "before_compact": 25000}}}
  ```

- The same in an agent file's front matter
  (`<name>.md` in the agents folder `session path agent --config` prints):

  ```yaml
  settings:
    session:
      settle:
        soft: {at: 250000, every: 25000}
        hard: {at: 350000, before_compact: 25000}
    claude-code:
      idleCompaction: false
  ```

- What it says: the soft line at 250K, 275K, 300K and 325K, once each.
  The hard line on every batch from 350K, or from 25K short of the set
  compaction point when that is lower.
- `idleCompaction: false` is the `claude-code` tool's key: it stops
  Claude Code compacting the seat while it sits idle, between turns,
  when no tool batch runs and no line could be said.
- Token amounts are a number or a string: `250000`, `"250k"`, `"1.5M"`.

## When each line speaks

- **The soft line** speaks once per crossing of `soft.at + k × soft.every`
  (k = 0, 1, 2 ...). With `soft.every` unset, it speaks on every batch
  from `soft.at`. With `soft.at` unset there is no soft line.
- **The hard line** starts at the lower of `hard.at` and the set
  compaction point less `hard.before_compact`. With no point set,
  `hard.at` alone places it. It speaks on every batch from there, or
  once per crossing of each `hard.every`.
- **One line per batch, and the hard line wins.** Past the hard line the
  soft line is silent: "at a good stopping point" after "now" would undo
  it.
- **A crossing is said once.** It is a mark file under
  `<session dir>/settle/`, made with O_EXCL. A jump past several marks
  says one line. When the count falls, after a compaction, the marks
  above it are removed, so each crossing is said again.
- **The words** are `settle.soft.say` and `settle.hard.say`. On either
  line `{at}` (the mark crossed), `{used}` (the count), `{compact_at}`
  (the set compaction point) and `{left}` (tokens before it) are filled
  in. A stray brace is text, and a line break becomes a space.
- **`{compact_at}` and `{left}` come only from the point the launch
  set,** never from a guess or a status payload. With no point set they
  read `?`.

## The compaction point

The hard line and the guard need to know where Claude Code compacts this
seat. They read the point back from what the launch set, as Claude Code
does (`sessiontools.compact_point`):

- `CLAUDE_CODE_AUTO_COMPACT_WINDOW` in the environment, then
- `--autocompact` on the command line, then
- `autoCompactWindow` in the `--settings` file `session start` wrote,
  which the `claude-code` tool renders from its layers.
- `autoCompactEnabled: false`, there or in Claude Code's own settings
  files, means no point: `{left}` reads `?` and the guard passes.
- **Set `autoCompactWindow` to know the point.** Unset, Claude Code
  takes its window from the model or the server, which a hook cannot
  read, and the point is unknown.
- **Measured live** on Claude Code 2.1.316: `autoCompactWindow` in a
  `--settings` file moves the compaction point.
- **`idleCompaction: false` rests on static evidence only.** A live
  probe ran 12 seats idle, idle compaction on in 8 of them, positive
  controls included, and none compacted while idle. So a seat with it
  `false` that does not compact proves nothing yet.

## The PreCompact guard

`hooks/settle-hook precompact` runs at Claude Code's `PreCompact` (matcher
`auto`). It is the belt under `idleCompaction: false`: a hook is the
only thing that sees the moment itself.

- **It skips an auto compaction** (exit 2, one reason line on stderr)
  when all of these hold: settle is on, the compaction point the launch
  set is known, the count is the whole context (no message after the
  last usage row, as when the turn is over), and the count is below the
  point. Claude Code shows `Compaction blocked by PreCompact hook`.
- **It passes everything else:** an auto compaction at or above the
  point, a compaction mid-turn (new prompt or tool results after the
  last usage row), a manual `/compact`, a subagent's payload, settle
  off, an unknown point, no reading, or any failure. A pass prints
  nothing, since a PreCompact hook's stdout joins the summary's
  instructions.
- **The veto is measured live on the threshold path only.** On Claude
  Code 2.1.316 an exit 2 blocked a threshold compaction, and blocked it
  again on every later turn. That it holds on the idle path is static
  reading, **unmeasured**: no idle compaction started in the probe.
- With no `settle` set it shares the alert's OFF stamp, so it starts no
  python.

## Who is told

- **A session `session start` launched.** The hook needs
  `CLAUDE_CODE_SESSION_ID` (Claude Code sets it in every hook) and that
  session's dir, `<sessions>/<session-id>`. With either missing it exits
  0 before python starts, whatever any layer sets.
- **That is birth-only, on purpose.** A `claude` started by hand has no
  session dir: it gets no birth copy of `SESSION_SETTLE_AT`, no warning
  and no guard, even when the host file sets `settle`.
- **It never asks who the session is.** Any launched session whose
  settings set a line is told.
- **It fires on turns that call a tool.** PostToolBatch runs once per
  model response with tool calls. A reply with no tool call is not told;
  the next batch is.
- **A subagent's batch is silent** (payload `agent_id`). The session
  hears the line on its own next batch.
- **The count is this batch's own:** the last usage row, the number
  `session context` prints, read with `sessiontools.window.scan` on the
  payload's transcript. When the payload names its tool calls the hook
  waits for that batch's own assistant row, every 10 ms, 1 s at most,
  then counts what is on disk. A scan took 0.08 s on a 21.6 MB
  transcript.

## The environment

The environment is not a layer. The hook reads no variable to decide
whether to speak.

- **`SESSION_SETTLE_AT`** at launch is copied once, at birth, into
  `<session dir>/settings/session.d/50-env.json` as `settle.soft.at`.
  `SESSION_SETTLE_SAY` lands the same way as `settle.soft.say`.
- **So a shell-rc export beats every seat.** The copy is the session
  layer, the last one read: it outranks the host, the agent and the
  seat, and every session started from that shell gets it. Only a
  `_locked` on `settle` (or `settle.soft.at`) in a layer above holds it
  off: a locked path ignores that key in every layer below.
- To set a seat's lines, use its settings, not an export.
- To stop an export, unset it before `session start`.
- A running session keeps its copy until
  `<session dir>/settings/session.d/50-env.json` is edited.
- **Today's behaviour stands:** a launched seat that exports only
  `SESSION_SETTLE_AT` hears one line, every batch, from its mark.

## Cheap when off

- **The OFF stamp,** `<session dir>/settle/off`, lets the shim exit 0 by
  shell builtins, with no python. It lists every layer path the cascade
  read or looked for: each layer's file, the agent file, each `.d`
  folder and the files in it.
- `session start` writes it at birth; if the birth did not, the first
  batch does. A settle that turns on removes it.
- **A newer listed path wakes it.** A layer made later, or an edited
  one, is newer than the stamp, so python runs and reads it.
- **A file moved into a `.d` folder is seen,** since the folder's mtime
  changes.
- **A top-level layer file put in place with an OLD mtime is not seen**
  (`mv` of an old file, `cp -p`) until it is touched:

  ```sh
  touch ~/.sessionkit/tools/settings/session.json
  ```

- **On:** each batch reads the cascade. The agent layer's `session`
  section is cached in `<session dir>/settle/agent.json`, keyed on the
  agent file's mtime, size and inode, so a batch does not import the
  YAML parser while the file is unchanged.

## Details

- **`session context`'s band reads `settle.soft.at` only,** through the
  same cascade. With no soft line set there is no band, even when a hard
  line is set ([the band][band]).
- **It never breaks a turn.** No python3, python older than 3.11, a
  missing transcript, an unreadable settings file or a crash all end
  with no output and exit 0.
- **The hook's python** is the busy sound's (`hooks/pick-python.sh`):
  `$SESSIONTOOLS_PYTHON` when set, else the python in the shebang of
  `command -v session` (a bare path only), else `python3`. `uv tool
  install` keeps its python off `PATH`, and a clean Mac's `python3` is
  older than 3.11, so `PATH` alone would leave the alert silent.
- **The default words name no next-step verb.** Set `settle.soft.say`
  or `settle.hard.say` to name a session's own next step.
- The clock line says a figure, never a warning: a session hears one
  warning, this one ([the clock line][clock]).
- It ships from plugin 0.0.4: see [the plugin][plugin].

[who]: #who-is-told
[env]: #the-environment
[guard]: #the-precompact-guard
[keys]: settings.md#the-session-tools-keys
[cc-keys]: settings.md#the-claude-code-tools-keys
[band]: configuration.md#session-context-and-session-ledger
[clock]: clock-line-hook.md
[plugin]: claude-code-plugin.md
