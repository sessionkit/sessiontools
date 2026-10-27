# The Claude Code plugin

The repo is also a Claude Code plugin. It adds the `session` skill and
its hooks; the `session` command works without it.

- **The `session` skill** (`skills/session/SKILL.md`): teaches an agent
  the verbs, so it runs `session context` instead of guessing.
- **The settle alert** (`hooks/settle-hook`, PostToolBatch): one line
  once a session passes the context threshold it set. Silent unless
  set. See [the settle alert][settle].
- **The clock line** (`hooks/clock-hook`, PostToolBatch): the time and
  the context as used/max on every tool batch, and a warn line at a
  mark. Off unless `SESSIONTOOLS_HOOK_CLOCK=on`. See [the clock
  line][clock].
- **The busy sound** (`hooks/busy-sound`): a sound while a tool batch
  runs long. Silent unless set. See [the busy sound][busy].
- **The stop sound** (`hooks/busy-sound`, Stop): a sound once when the
  seat's turn ends, queued behind any line the mouth is still speaking.
  Silent unless set. See [the busy sound][busy].
- **The turn-ended mark** (`hooks/busy-sound`): lets
  `session list` say `idle with children`. Off unless
  `SESSIONTOOLS_HOOK_TURN=on`. See [the Python library][library].
- **The session title** (`hooks/session-info title`, SessionStart): sets
  the session title to the short session id. Off unless
  `SESSIONTOOLS_HOOK_TITLE=on`. See [the session-info hooks][session-info].
- **The session-info block** (`hooks/session-info context`,
  SessionStart): adds the session id, working directory, source and
  transcript path to the context. Off unless
  `SESSIONTOOLS_HOOK_SESSION_INFO=on`. See
  [the session-info hooks][session-info].
- **The identity block** (`hooks/session-info identity`, SessionStart):
  adds the block `session start` wrote (who this session is, at launch)
  to the context. On; a session with no block is silent;
  `SESSIONTOOLS_HOOK_IDENTITY=off` turns it off. See
  [the identity block][identity].

## Install

The repo carries its own marketplace (`.claude-plugin/marketplace.json`,
marketplace and plugin both named `sessiontools`). From a checkout:

```console
claude plugin marketplace add /path/to/sessiontools
claude plugin install sessiontools@sessiontools
```

- Run as written on Claude Code 2.1.296, with an empty
  `CLAUDE_CONFIG_DIR` and no login, these lines and the ones below
  each exit 0. The plugin verbs ask for no auth (tested 2030-12-08).
- `claude plugin list` then shows `sessiontools@sessiontools`, its
  version and `Read from: /path/to/sessiontools`.

## Update and uninstall

```console
claude plugin update sessiontools@sessiontools
claude plugin uninstall sessiontools@sessiontools
claude plugin marketplace remove sessiontools
```

## Which release has what

- A checkout is a folder marketplace. `claude plugin update` finds
  nothing to update in it: Claude Code says the plugin is read from the
  folder, and an edit there takes effect at the next session start or
  `/reload-plugins`. So pull the checkout, then start a new session. The
  install also keeps a copy in the plugin cache. A live session's load
  has not been tested.
- 0.0.3 and older: no hooks.
- 0.0.4: the settle alert.
- 0.1.0: the busy sound.
- Unreleased: the turn-ended mark, the clock line and the session-info
  hooks.
- `claude plugin list` shows the version you run.

[settle]: settle-alert-hook.md
[clock]: clock-line-hook.md
[busy]: busy-sound-hook.md
[library]: python-library-api.md
[session-info]: session-info-hook.md
[identity]: identity-block.md
