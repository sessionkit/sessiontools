# The busy sound (a plugin hook)

The plugin's `hooks/busy-sound` plays a sound file once a tool batch has
run past a delay, and stops it the moment the batch returns.
A batch that ends sooner makes no sound. **It is silent unless set**,
through the settings cascade (`session settings <sid> sounds`):

```json
{"enabled": true,
 "busy": {"auto_arm": true, "file": "~/sounds/busy.wav", "delay": 8}}
```

- `SESSIONTOOLS_HOOK_BUSY=off` (also `0`, `false`, `no`) switches it
  off, and the plugin stays loaded.
- It never breaks a turn: no python, a crash or a bad setting all end
  with no output and exit 0.
- It never plays over the mouth (voicemode speaking).

## Where the setting goes

- `~/.sessionkit/tools/settings/sounds.json` (host).
- An agent's front matter: `settings: {sounds.busy.delay: 8, ...}`.
- `seats/<seat>/settings/sounds.json`.
- `sessions/<sid>/settings/sounds.json`.
- The schema is `settings/sounds.schema.json`.

## The hooks

| event | verb | what it does |
| --- | --- | --- |
| PreToolUse | `arm` | first tool of a batch: arm the sound in a detached waiter, return at once |
| PostToolBatch | `end` | stop the sound in every slot (a subagent's batch end is skipped) |
| Stop | `stop` | stop anything left, in every slot; set the turn-ended mark when it is on |
| UserPromptSubmit, SessionEnd | `clear` | stop anything left, in every slot |

## The settings

- `enabled` and `busy.auto_arm` count only as the boolean `true`. `file`
  must be a readable file (absolute or `~/`), and `delay` a number
  `>= 0` in seconds. Anything else is silent.
- **`busy.trigger` picks ONE mode**: `batch` (unset is `batch`:
  the hooks above), `landed` (the user's message is queued, unread; the
  pager signals it) or `finished` (voice input has closed the user's
  utterance; the voice input signals it).
- Outside `batch`, `delay` is optional and defaults to 0.
- Any other trigger, a typo included, is silent in every mode, never a
  fallback to `batch`.
- A seat whose trigger is not `batch` is OFF for these hooks and pays
  what a seat without the setting pays.
- **`busy.player` picks who plays it**: `afplay` (unset is
  `afplay`) or `engine`, [the resident sound engine][engine]. Any other
  value, wrong case included, is silent in every mode. The batch
  trigger only: `landed` keeps `afplay`.
- The agent layer sets the default and a session layer overrides it.

## What it costs

- **A seat without the setting** starts python once per session
  (~40 ms), which stamps `<session dir>/sounds/busy/off`. After that it
  pays a few `stat` calls per tool.
- A newer host- or session-layer `sounds` file is re-read at once.
- A seat- or agent-layer switch-on takes effect at the next launch, or
  after `rm <session dir>/sounds/busy/off`.

## Playing

- The player is `afplay` unless `busy.player` is `engine`. It plays
  once, never loops, and runs in its own process group, which the batch
  end kills.
- With `engine`, one resident process plays it instead, and the hooks
  send it one datagram each with no python: see [the sound
  engine][engine].
- A subagent's tool arms nothing. A foreground Agent call keeps the
  parent's sound going until it returns; a background one does not.

## Never over the mouth

The speech guard lives in the waiter, so every trigger has it.

- The delay counts only silence, and nothing starts while the mouth
  speaks.
- A sound that is playing is cut within 0.1 s of the mouth starting.
  The slot stays armed, so it does not replay in that batch.
- Any session's speech counts.
- "Speaking" is `playing.json` in the mouth dir
  (`$VOICEMODE_MOUTH_DIR`, else `$VOICEMODE_BASE_DIR/mouth`, else
  `~/.voicemode/mouth`), counted only while `player.pid` there names a
  live process.
- That file is voicemode's internal state, not yet a blessed contract,
  so it is read only through `busy.mouth_speaking()`.
- No mouth dir: the guard does nothing.

## The signals: `session sound`

Another tool (the pager) says the moment; sessiontools decides.

- `session sound landed --session SID [--ref ID]`, or
  `busy.signal(event, sid, ref)` in python.
- `landed` plays the sound on `<session dir>/sounds/unread/` at its
  `delay` (unset: 0) only when the session's trigger resolves to
  `landed` and the seat's sidecar names a live pid. A second `landed`
  while it is armed is ignored.
- `session sound given --session SID` stops it, and so do `end` and
  `clear`, which stop every slot (`busy`, `unread`, `finished`).
- Exit 0 always, 2 for a usage error. Nothing on stdout unless
  `--json`, which says the decision and why.
- `finished` is held and not built.

## Inside

- **The seam:** `sessiontools.busy.play(slot, file, delay=0)` and
  `stop(slot)` know nothing of batches. Another trigger (`busy.trigger`,
  above) uses them on its own slot dir.
- **The hook's python**: `$SESSIONTOOLS_PYTHON` when set, else
  the python in the shebang of `command -v session` (a bare path only),
  else `python3`. The settle alert picks the same way, with the same
  `hooks/pick-python.sh`.
- The `session` tool's venv has PyYAML, so the hook reads the agent
  layer exactly as `session settings` does. On `python3` without PyYAML
  the agent layer is dark and the other layers still count.
- The choice is made only when python has to start, so the off path is
  unchanged.
- It ships from plugin 0.1.0: see [the plugin][plugin].

[engine]: sound-engine-agent.md
[plugin]: claude-code-plugin.md
