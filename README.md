# sessiontools

`session`: one command for the coding-agent sessions on this machine.
Which are live, whether each is busy or idle, how full its context is,
and how to start one from an agent file. For anyone running several
Claude Code sessions side by side, and for the tools that watch them.

## Install

```console
uv tool install sessiontools     # puts `session` on the PATH
```

Python 3.11 to 3.14. Optional: [the Claude Code plugin][plugin] adds
the `session` skill and two hooks.

Shell completion, one line per shell ([shell completion][completion]):

```console
eval "$(session completion zsh)"     # zsh: in ~/.zshrc, after compinit
session completion bash > ~/.local/share/bash-completion/completions/session  # bash with bash-completion 2
eval "$(session completion bash)"    # bash 3.2 or any bash without it: in ~/.bashrc
```

## Quick start

```console
session list                 # the live sessions: panes, state, context
session get state            # this session: busy, idle with children, idle
session context              # how full this session's context window is
session context --parts      # what filled it: boot, then growth by category
session ledger --short       # each file read and command run, by tokens
session account status       # which Claude account this Mac is on
session start --list-agents  # the agents it can start
session reap                 # seats idle past 1h: CLOSE or KEEP, and why (a dry run)
session --help               # every verb
```

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
Without `--yes`, `use` shows the plan and writes nothing. To add an
account, `session account new NAME --email E` signs in through `claude
setup-token`, saves the token to passage, reads it back and registers
it; the token is never shown. See [Switching Claude accounts][account].

## Uninstall

```console
uv tool uninstall sessiontools
```

It leaves `~/.session/` (session dirs, the list cache) and
`~/.sessionkit/tools/settings/`; remove them by hand to leave nothing
behind.

## Docs

- [The `session` verbs][verbs]: list, get, context, ledger, account,
  settings, operator, reap
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
- [The busy sound][busy]: a sound while a tool batch runs long
- [The sound engine][engine]: the busy sound from one resident process
- [The status line][status-line]: `session statusline install` puts it in the
  kit's claude-code layer, never `~/.claude`. `session start` seats show it;
  a bare `claude` does not (`alias claude='session start -'`)
- [The pane signal][pane-state]: BUSY / FREE on each seat's tmux pane,
  and `session pane-state agent`, the launchd agent that publishes it
- [The Python library][library]: `from sessiontools import sessions`
- [Building and testing][building]: make targets, specs, releases
- [CHANGELOG][changelog]; licence AGPL-3.0-or-later ([LICENSE][license])

## Configuration

Nothing needs configuring: unset, every part uses its default. What can
be set, and where each one lives ([configuration][configuration]):

- **Settings files**, JSON in layers: `~/.sessionkit/tools/settings/session.json`
  (the operator, the list cache, the pane signal, accounts, a session's
  home), `~/.sessionkit/tools/settings/sounds.json` (the busy sound), and
  `~/.sessionkit/tools/settings/claude-code.json` (the model, effort
  and status line `session start` passes on). Every key, its default and
  which layers it is read from: [settings][settings].
- **Folders of its own:** `~/.session/` (sessions, seats, agent files, the
  state dir) and `~/.sessionkit/tools/accounts/` (`session account`).
- **Environment variables:** the settle alert's `SESSION_SETTLE_AT`, the
  hooks' `SESSIONTOOLS_HOOK_*` switches, and the paths it reads, such as
  `SESSIONS_DIR` and `SESSION_STATE_DIR`. The configuration page lists
  each one with its default, under the part that reads it.

## Logs

- **The sound engine:** `~/Library/Logs/sessiontools/sound-engine.log`,
  once `session sound engine on` has run.
- **The pane signal's agent:** `~/Library/Logs/sessiontools/pane-state.log`,
  once `session pane-state agent on` has run.
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

[verbs]: docs/session-command-verbs.md
[start]: docs/session-start-launcher.md
[account]: docs/account.md
[cache]: docs/list-snapshot-cache.md
[operator]: docs/operator-privileged-seats.md
[reap]: docs/session-reap.md
[completion]: docs/shell-completion.md
[plugin]: docs/claude-code-plugin.md
[settle]: docs/settle-alert-hook.md
[busy]: docs/busy-sound-hook.md
[engine]: docs/sound-engine-agent.md
[status-line]: docs/status-line-script.md
[pane-state]: docs/pane-state-signal.md
[library]: docs/python-library-api.md
[building]: docs/building-and-testing.md
[changelog]: CHANGELOG.md
[license]: LICENSE
[configuration]: docs/configuration.md
[settings]: docs/settings.md
