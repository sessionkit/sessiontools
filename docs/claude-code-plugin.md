# The Claude Code plugin

The repo is also a Claude Code plugin. It adds the `session` skill and
two hooks; the `session` command works without it.

- **The `session` skill** (`skills/session/SKILL.md`): teaches an agent
  the verbs, so it runs `session context` instead of guessing.
- **The settle alert** (`hooks/settle-hook`, PostToolBatch): one line
  once a session passes the context threshold it set. Silent unless
  set. See [the settle alert][settle].
- **The busy sound** (`hooks/busy-sound`): a sound while a tool batch
  runs long. Silent unless set. See [the busy sound][busy].
- **The turn-ended mark** (`hooks/busy-sound`): lets
  `session list` say `idle with children`. Off unless
  `SESSIONTOOLS_HOOK_TURN=on`. See [the Python library][library].

## Install

The repo carries its own marketplace (`.claude-plugin/marketplace.json`,
marketplace and plugin both named `sessiontools`). From a checkout:

```console
claude plugin marketplace add /path/to/sessiontools
claude plugin install sessiontools@sessiontools
```

- Run as written on Claude Code 2.1.296, with an empty
  `CLAUDE_CONFIG_DIR` and no login, these lines and the ones below
  each exit 0. The plugin verbs ask for no auth (tested 2030-08-14).
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
- Unreleased: the turn-ended mark.
- `claude plugin list` shows the version you run.

[settle]: settle-alert-hook.md
[busy]: busy-sound-hook.md
[library]: python-library-api.md
