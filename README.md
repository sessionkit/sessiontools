# sessiontools

`session`: one command for the coding-agent sessions on this machine.
Which are live, whether each is busy or idle, how full its context is,
and how to start one from an agent file. For anyone running several
Claude Code sessions side by side, and for the tools that watch them.

## Install

This puts `session` on the PATH:

```console
uv tool install sessiontools
```

Python 3.11 to 3.14. Optional: [the Claude Code plugin][plugin] adds
the `session` skill and four hooks.

Shell completion, one line per shell ([shell completion][completion]).
For zsh, in `~/.zshrc` after compinit:

```console
eval "$(session completion zsh)"
```

For bash with the bash-completion package (2.x), once:

```console
session completion bash > ~/.local/share/bash-completion/completions/session
```

For bash 3.2, or any bash without that package, in `~/.bashrc`:

```console
eval "$(session completion bash)"
```

## Quick start

- `session list`: the live sessions, with their panes, state and context.
- `session get state`: this session's state: busy, idle with children,
  or idle.
- `session context`: how full this session's context window is.
- `session context --parts`: what filled it: boot, then growth by
  category.
- `session ledger --short`: each file read and command run, by tokens.
- `session account status`: which Claude account this Mac is on.
- `session start --list-agents`: the agents it can start.
- `session reap`: seats idle past 1h, each CLOSE or KEEP, and why. A dry
  run.
- `session default-model`: the model every new `claude` starts on, and
  whether a pin keeps it there. Off until you set `model.default`.
- `session --help`: every verb.

## Requirements

- [x] macOS (tested on macOS 27, Apple silicon)
- [ ] Linux
- [ ] WSL
- [ ] Windows

What sessiontools depends on, what needs each one, and how to upgrade
it: [dependencies][dependencies].

## Switching accounts

`session account use NAME` moves this Mac to another Claude account from
a token you already hold: no browser, no restart, and it never prints a
token. On a Keychain host it moves **every** seat, not one:

| route | moves | cache cost | reversible by | gate |
|---|---|---|---|---|
| `account use` (Keychain) | every seat on the Mac with no env token, within ~50 s | the sum of their contexts (2.07M / 1.10M measured) | `use` back, or restore from the backup | witness, `--yes`, preflight, backup |
| `account use` (token file) | no live seat; each one that relaunches from a shell that reads the file | none now; each seat's context when it restarts | `use` back, or restore from the backup | witness, `--yes`, preflight, backup |

A host whose shell exports `CLAUDE_CODE_OAUTH_TOKEN` from a file names it
in the `session` setting `account.token_file`, and `use` swaps the file.
A seat `session start` launches does not follow the file unless its agent
file says `inherit: [CLAUDE_CODE_OAUTH_TOKEN]`.
Without `--yes`, `use` shows the plan and writes nothing. To add an
account, `session account new NAME --email E` signs in through `claude
setup-token`, saves the token to passage, reads it back and registers
it; the token is never shown. See [Switching Claude accounts][account].

If you sign in by token, `CLAUDE_CODE_OAUTH_TOKEN` in your shell, a
session `session start` launches does not get it by default. To carry
it, name it under `inherit:` in the agent file ([credentials are never
handed down][credentials]). A `/login` sign-in needs nothing:

```yaml
environment:
  inherit:
    - CLAUDE_CODE_OAUTH_TOKEN
```

## What an install changes

`uv tool install sessiontools` changes two places and fills uv's own
cache, and it touches no shell startup file and nothing in `~/.claude`.
Everything else below appears only when you run the verb named beside
it. Each line says what puts it there and what takes it away.

- **PATH:** `~/.local/bin/session`, the one command, or in
  `UV_TOOL_BIN_DIR` when that is set. uv says so if the folder is not
  on your PATH, and edits no shell file. `uv tool uninstall` removes it.
- **The program:** `~/.local/share/uv/tools/sessiontools/`, its own
  Python environment with PyYAML and jsonschema, or in `UV_TOOL_DIR`.
  `uv tool uninstall` removes it.
- **uv's cache:** the install fills it with what it downloaded and
  built. It is uv's, shared by everything uv installs, not sessiontools'.
  `uv cache dir` prints where it is (`~/.cache/uv` by default, or
  `UV_CACHE_DIR`). `uv tool uninstall` leaves it; `uv cache clean
  sessiontools` drops this package's entries, and `uv cache prune` drops
  the ones nothing uses.
- **Folders, each made by a verb you run:**
  - `~/.session/state/list/`: the list cache, `snapshot.json` and
    `list.lock`, at the first `session list`.
  - `~/.session/sessions/<session-id>/`: one for each `session start`,
    holding `launch/`, `log/settings.jsonl` and `session-id`. It also
    gets `settings/<tool>.d/50-env.json`, one for each tool whose
    settings name a variable that is set when the session starts. Each
    is written once, so a resumed session keeps its file, and
    `log/settings.jsonl` names each variable it read, never its value.
    The plugin's hooks and `session park` write only inside a folder
    that is already there.
  - `~/.session/state/reap.log`: `session reap --yes`.
  - `~/.session/state/pane-state/watch.lock`: the pane signal's service.
  - `~/.session/state/default-model/`: `backups/` and `log.jsonl`, the
    first time `session default-model enforce` changes settings.json.
  - `~/.sessionkit/tools/accounts/`: `session account new` and `use`.
  - `~/.local/share/sessiontools/sound/bin/`: `session sound engine on`
    builds the sound engine there with `swiftc`.
  - `~/Library/Logs/sessiontools/`: the two services' logs.
  - `SESSIONS_DIR`, `SESSION_STATE_DIR` and `SESSIONTOOLS_SOUND_HOME`
    move these folders when set.
- **Services (launchd agents):** none until you turn one on. Each `on`
  writes a plist in `~/Library/LaunchAgents/` and loads it, and it
  starts again at every login. Each `off` unloads it and deletes the
  plist. Both `on` and `off` also retire a job an older release set up
  under its old label: they unload it and delete its plist. `status`
  names one that is still there, and `--dry-run` lists the retire and
  changes nothing.
  - `dev.sessionkit.tools.sound-engine`: `session sound engine on`.
    `off --purge` also deletes the engine it built.
  - `dev.sessionkit.tools.pane-state`: `session pane-state agent on`.
    It sets `@session_state` and `@session_state_why` on each seat's
    tmux pane.
  - `dev.sessionkit.tools.default-model`: no verb writes or loads it.
    `session default-model unit launchd` prints it, and it is there only
    if you save and load it yourself
    ([the default model][default-model]).
- **Plugin and hooks, optional:** `claude plugin marketplace add` and
  `claude plugin install` ([the Claude Code plugin][plugin]). Claude
  Code names the marketplace and the plugin in `~/.claude/settings.json`
  (`extraKnownMarketplaces`, `enabledPlugins`), lists them in
  `~/.claude/plugins/`, and keeps a copy in
  `~/.claude/plugins/cache/sessiontools/`. The plugin brings the
  `session` skill and nine hook entries: SessionStart (two),
  PreToolUse, PostToolBatch (three), Stop, UserPromptSubmit and
  SessionEnd. Each is silent unless set.
  Uninstalling the plugin takes it out of the settings and leaves the
  cached copy.
- **Settings:** the install writes none, and none are needed.
  `session statusline install` writes `statusline.sh` and
  `claude-code.d/50-statusline.json` in `~/.sessionkit/tools/settings/`;
  with `--force` it first keeps a `.bak-<stamp>` copy of each file it
  replaces. Any other file there is one you wrote.
- **Outside these folders, only on a verb:** `session account use NAME
  --yes` replaces the Keychain item `Claude Code-credentials`, or the
  token file named by `account.token_file`, after a backup in the
  accounts folder. `session account new` saves a token to passage.
  With `model.default` set, `session default-model enforce` (and the
  unit or `watch` that runs it) rewrites the top-level `"model"` in
  `~/.claude/settings.json`, after a backup.

## Uninstall

Turn off any service you turned on first: the pane signal's service
and the default model's agent run the `session` command that the last
line removes. Then remove the
plugin, if you installed it, and the command. Skip the lines for what
you never installed:

```console
session pane-state agent off
session sound engine off --purge
launchctl bootout gui/$(id -u)/dev.sessionkit.tools.default-model
rm ~/Library/LaunchAgents/dev.sessionkit.tools.default-model.plist
claude plugin uninstall sessiontools@sessiontools
claude plugin marketplace remove sessiontools
uv tool uninstall sessiontools
```

That leaves what the verbs made, listed above. The plugin's cached
copy, the services' logs and the engine's folder are sessiontools'
alone. To delete them:

```bash
rm -rf "${HOME:?}/.claude/plugins/cache/sessiontools"
rm -rf "${HOME:?}/Library/Logs/sessiontools"
rm -rf "${HOME:?}/.local/share/sessiontools"
```

uv's cache stays too, with the other things uv installed; `uv cache
clean sessiontools` drops this package's entries.

The rest is your data: delete it by hand only if you want it gone.
`~/.sessionkit/tools/` holds your settings and accounts, and the
accounts folder keeps the backups of every token `session account use`
replaced. `~/.session/` holds the session folders and the list cache,
and the kit's other packages keep their files in it too.

## Docs

- [The `session` command][verbs]: what it does, every verb, and an
  example of each
- [Starting a session][start]: agent files, flags, tmux, one holder
- [Switching Claude accounts][account]: `session account`, its blast
  radius, and `new`, which adds an account
- [The `session list` cache][cache]: one computation, many readers
- [Operator and privileged seats][operator]: who seats ask
- [Closing idle seats][reap]: `session reap`, a dry run by default
- [Shell completion][completion]: `session completion bash|zsh`, a static
  script built from the verbs' own parsers
- [The Claude Code plugin][plugin]: the skill and the hooks
- [The settle alert][settle]: one line past a context threshold
- [The clock line][clock]: the time and the context on every tool batch
- [The busy sound][busy]: a sound while a tool batch runs long
- [The session-info hooks][session-info]: the short session id as the
  session title, and the session's facts in context, both off by default
- [The sound engine][engine]: the busy sound from one resident process
- [The status line][status-line]: `session statusline install` puts it in the
  kit's claude-code layer, never `~/.claude`. `session start` seats show it;
  a bare `claude` does not (`alias claude='session start -'`)
- [The pane signal][pane-state]: BUSY / FREE on each seat's tmux pane,
  and `session pane-state agent`, the launchd agent that publishes it
- [The default model][default-model]: `session default-model`, opt-in,
  puts settings.json's `"model"` back when something rewrites it
- [The Python library][library]: `from sessiontools import sessions`
- [Dependencies][dependencies]: what sessiontools needs, what needs each
  one, and how to upgrade it
- [Building and testing][building]: make targets, specs, releases
- [CHANGELOG][changelog]; licence AGPL-3.0-or-later ([LICENSE][license])

## Configuration

Nothing needs configuring: unset, every part uses its default. What can
be set, and where each one lives ([configuration][configuration]):

- **Settings files**, JSON in layers: `~/.sessionkit/tools/settings/session.json`
  (the operator, the list cache, the pane signal, accounts, a session's
  home, the default model), `~/.sessionkit/tools/settings/sounds.json` (the busy sound), and
  `~/.sessionkit/tools/settings/claude-code.json` (the model, effort
  and status line `session start` passes on). Every key, its default and
  which layers it is read from: [settings][settings].
- **Folders of its own:** `~/.session/` (sessions, seats, agent files, the
  state dir) and `~/.sessionkit/tools/accounts/` (`session account`).
- **Environment variables:** the settle alert's `SESSION_SETTLE_AT`, the
  clock line's `SESSION_CLOCK_WARN_AT`, the hooks' `SESSIONTOOLS_HOOK_*`
  switches (`SESSIONTOOLS_HOOK_CLOCK=on` switches the clock line on), and
  the paths it reads, such as `SESSIONS_DIR` and `SESSION_STATE_DIR`.
  The configuration page lists each one with its default, under the part
  that reads it.

## Logs

- **The sound engine:** `~/Library/Logs/sessiontools/sound-engine.log`,
  once `session sound engine on` has run.
- **The pane signal's agent:** `~/Library/Logs/sessiontools/pane-state.log`,
  once `session pane-state agent on` has run.
- **The default model:** `~/.session/state/default-model/log.jsonl`, one
  line per write to settings.json, and
  `~/Library/Logs/sessiontools/default-model.log`, once you have loaded
  the launchd agent `session default-model unit launchd` prints.
- **`session reap --yes`:** `~/.session/state/reap.log`, one line for each
  seat it closed or failed to close.
- **Each session's settings at birth:**
  `~/.session/sessions/<session-id>/log/settings.jsonl`, what `session
  start` read into that session's settings: each environment variable
  by name, never its value.
- **The status line:** `~/.config/session/statusline-log.jsonl`, one line
  per render, only when `~/.config/session/` already exists.

Follow one as it grows with `tail -F`, for example
`tail -F ~/Library/Logs/sessiontools/pane-state.log`.

- **Health:** `session list` prints a row for each live session, with its
  state and context. A session you know is running that is not listed is
  one sessiontools cannot see.

[verbs]: docs/session.md
[start]: docs/session-start-launcher.md
[credentials]: docs/session-start-launcher.md#credentials-are-never-handed-down
[account]: docs/account.md
[cache]: docs/list-snapshot-cache.md
[operator]: docs/operator-privileged-seats.md
[reap]: docs/session-reap.md
[completion]: docs/shell-completion.md
[plugin]: docs/claude-code-plugin.md
[settle]: docs/settle-alert-hook.md
[clock]: docs/clock-line-hook.md
[busy]: docs/busy-sound-hook.md
[session-info]: docs/session-info-hook.md
[engine]: docs/sound-engine-agent.md
[status-line]: docs/status-line-script.md
[pane-state]: docs/pane-state-signal.md
[default-model]: docs/default-model.md
[library]: docs/python-library-api.md
[building]: docs/building-and-testing.md
[dependencies]: docs/dependencies.md
[changelog]: CHANGELOG.md
[license]: LICENSE
[configuration]: docs/configuration.md
[settings]: docs/settings.md
