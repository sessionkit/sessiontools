# The default model: pin what every new claude starts on

`session default-model` keeps Claude Code's global default model where
you put it. A bare `claude` starts on the top-level `"model"` in
`~/.claude/settings.json`. The in-app model picker rewrites that key for
every later session, and with no key at all the plan's default wins.
When the pin is set, `enforce` puts `"model"` back.

- **Off unless you set it.** The pin is the `session` setting
  `model.default`. Unset, nothing happens: no watcher, no read of
  settings.json, no backup, no log, no write.
- **It edits one member.** Only the top-level `"model"` changes. Every
  other byte of the file stays as it was, and the old file is backed up
  first.
- **Nothing installs itself.** `enforce` is one pass. To keep the pin
  standing, `unit` prints a launchd agent or systemd units that run
  `enforce` on every change, and you install them yourself (below).
- **Fable is never pinned,** whatever the environment says.
- **One session, not the default?** This page is about the default every
  new `claude` starts on. It never changes a running session.

```sh
session default-model show
```

## Turn it on

Name the model in the `session` tool's host settings,
`~/.sessionkit/tools/settings/session.json` or a drop-in in
`~/.sessionkit/tools/settings/session.d/`:

```json
{"model": {"default": "opus"}}
```

- **Template and host layers only.** One settings.json serves every
  session on the host, so an agent, a seat or a session cannot move the
  pin ([the keys][session-keys]).
- **Unset is anything but a non-empty string:** no key, `null`, `""`, a
  number, a block that is not an object, or a file that does not parse
  or cannot be read.
- **A value off the list is refused** (exit 3, nothing written), so a
  typo never becomes every new session's default. So is any value that
  contains `fable`, in any case.
- **There is no `set` verb.** Edit the file. To unpin, remove the key.

## The verbs

- `session default-model [show [--json]]`: the pin and why it reads as
  set or unset, the settings.json it guards, that file's `"model"`, and
  whether `enforce` would change it. Writes nothing, and exits with the
  code `enforce` would.
- `session default-model list [--json]`: the models a pin may name.
- `session default-model enforce [--dry-run]`: one pass. If the pin is
  set and `"model"` differs, put it back and print one line. `--dry-run`
  prints the planned edit and where the backup would go.
- `session default-model watch [--interval S]`: in the foreground until
  interrupted, enforce once, then poll every S seconds (default 1) and
  enforce again on each change. Unset, it says so and exits 0 without
  polling. For a host with neither launchd nor systemd, a Mac user
  with no login session (below), or to see it work.
- `session default-model unit launchd|systemd`: print the agent or the
  units on stdout. It installs and loads nothing.

```console
$ session default-model enforce
default-model: model "sonnet" -> "opus" in ~/.claude/settings.json (backup ~/.session/state/default-model/backups/settings.json.20300609-170000)
```

## The list is what this release knows

`list` prints the aliases `opus`, `sonnet` and `haiku`, then every model
id this release of sessiontools knows. Claude Code resolves an alias to
the newest model in its family, so an alias is the pin most people want.

- **It is not a live check** of what your account may use. No command
  prints that without credentials, and this verb touches none. A newer
  model is added in a later release.
- **Fable rows are listed** and marked `no: never pinned (Fable)`.
- `default` is not listed: to get the plan's default, unset the pin.

## Keep it pinned: install one unit

`enforce` runs once. Something has to run it again each time
settings.json is rewritten. `unit` prints that something, watching
settings.json (and its target, when it is a symlink) and the `session`
tool's host settings, so a change to the pin takes effect at once.

- **Render it from the shell whose settings it should guard.** It names
  the absolute `session` of this install, and carries
  `CLAUDE_CONFIG_DIR` and `SESSION_STATE_DIR` when they are set.
- **Run one pinner.** Unload any other tool that pins `"model"` before
  you load this one. Two pinners with different models put the key back
  over each other for ever.
- **A refused `unit` prints nothing** and exits 3: a pin off the list,
  or no `session` found. So the launchd lines render in the current
  folder and move the plist only when that worked; a redirect straight
  into `~/Library/LaunchAgents/` would leave an empty plist there.

On macOS, a launchd agent (`WatchPaths`), with its log in
`~/Library/Logs/sessiontools/default-model.log`:

```sh
session default-model unit launchd > dev.sessionkit.tools.default-model.plist &&
    mv dev.sessionkit.tools.default-model.plist ~/Library/LaunchAgents/
mkdir -p ~/Library/Logs/sessiontools
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.sessionkit.tools.default-model.plist
```

- **The launchd agent needs a login session.** `launchctl bootstrap
  gui/$(id -u)` loads it into the domain of your login session at the
  Mac's screen. A user who only reaches the Mac over SSH has no such
  domain, and `bootstrap` exits 125 ("Domain does not support specified
  action"). The user domain is no way round it: an agent loaded there
  runs once at load, and its `WatchPaths` never fires again. On such a
  host, run `session default-model watch` in the foreground instead, in a
  terminal that stays open. It puts `"model"` back within seconds of each
  change.

On Linux, a systemd user `.path` unit (`PathChanged=`) and the `.service`
it starts, printed as one stream with a `# file:` line before each. Its
output goes to the user journal:

```sh
mkdir -p ~/.config/systemd/user
session default-model unit systemd | awk -v d="$HOME/.config/systemd/user" \
    '/^# file: /{f = d "/" $3} {print > f}'
systemctl --user daemon-reload
systemctl --user enable --now sessiontools-default-model.path
systemctl --user start sessiontools-default-model.service
```

- **A drop-in edited in place, under launchd:** launchd sees a file
  added to, removed from or renamed in `session.d/`, not one rewritten
  where it stands. After such an edit, run `session default-model
  enforce` once. `watch` sees it.
- **Our own write fires the unit once more.** That pass finds the file in
  sync and writes nothing. launchd waits at least 10 seconds between
  runs.

## Turn it off

Remove `model.default`, and the next pass does nothing. To remove the
unit too:

```sh
launchctl bootout gui/$(id -u)/dev.sessionkit.tools.default-model
rm ~/Library/LaunchAgents/dev.sessionkit.tools.default-model.plist

systemctl --user disable --now sessiontools-default-model.path
rm ~/.config/systemd/user/sessiontools-default-model.path \
  ~/.config/systemd/user/sessiontools-default-model.service
systemctl --user daemon-reload
```

## What a pass writes

- **The file:** `$CLAUDE_CONFIG_DIR/settings.json`, else
  `~/.claude/settings.json`. A symlinked file is written at its target,
  so the link survives. A missing `"model"` key is inserted as the first
  member. A missing file is created when the config folder exists. A
  missing config folder is refused: it is Claude Code's to create.
- **Refused, nothing written:** a file that does not parse, is not
  UTF-8 or starts with a byte-order mark; a duplicate top-level
  `"model"`; a `"model"` whose value is an object or an array. A
  `"model"` nested inside another key is never touched.
- **Changed while we read it:** the pass starts again, up to three
  times, and then refuses. The next change retries.
- **Backups:** `~/.session/state/default-model/backups/settings.json.<stamp>`,
  each read back after it is written, the newest 10 kept, mode `0600`.
- **The log:** `~/.session/state/default-model/log.jsonl`, one line per
  write: the file, the old and new value, the backup.
- `SESSION_STATE_DIR` moves both, in place of `~/.session/state`.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | done, already in sync, or unset |
| `2` | usage |
| `3` | refused, nothing written |
| `4` | a backup, write, read-back or log line failed |
| `5` | settings.json could not be read |

`show` exits with the code `enforce` would, so a script can ask first.
Under a unit, a non-zero exit is a line in its log, and the next change
retries.

[session-keys]: settings.md#the-session-tools-keys
