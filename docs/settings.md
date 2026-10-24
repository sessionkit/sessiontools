# Settings: the keys sessiontools reads

sessiontools reads three settings tools of its own: `session`, `sounds` and
`claude-code`. Each is JSON files in layers. This page lists every key:
its type, its default, what it does, its file, and which layers it is
read from today.

- **The files:** `~/.sessionkit/tools/settings/session.json`,
  `~/.sessionkit/tools/settings/sounds.json` and
  `~/.sessionkit/tools/settings/claude-code.json`, each with a
  `.d/` folder of drop-ins beside it ([the files][files]).
- **Not every key follows the cascade.** Most `session` keys are read
  from the template and host layers only, so an agent, a seat or a
  session cannot change them. The `sounds` keys, the `claude-code` keys,
  `context.near_pct`, `home` and `home_dir` are read from all five layers
  ([who reads which layers][layers]).
- **See what applies:** `session settings <session-id> [tool]` prints each
  key with the file that set it.
- **How the layers stack**, for the whole family: the Session Kit
  settings guide, [sessionkit's docs/settings.md][kit-settings].

Settings are JSON. Paths are shown under `~`, your home folder.

## The files

| Tool | Host file, then drop-ins | Template |
| --- | --- | --- |
| `session` | `~/.sessionkit/tools/settings/session.json`, then `~/.sessionkit/tools/settings/session.d/*.json` | `~/.sessionkit/tools/settings/session.template.json` |
| `sounds` | `~/.sessionkit/tools/settings/sounds.json`, then `~/.sessionkit/tools/settings/sounds.d/*.json` | `~/.sessionkit/tools/settings/sounds.template.json` |
| `claude-code` | `~/.sessionkit/tools/settings/claude-code.json`, then `~/.sessionkit/tools/settings/claude-code.d/*.json` | `~/.sessionkit/tools/settings/claude-code.template.json` |

- **One folder holds all three.** They ship in one package, `tools`, so
  their files sit side by side in `~/.sessionkit/tools/settings/`. No
  other folder is read for them.
- **The lower layers** are the same for every tool: an agent file's
  `settings:` front matter (`~/.session/agent/config/<name>.md`), then
  `~/.session/seats/<seat>/settings/<tool>.json`, then
  `~/.session/sessions/<session-id>/settings/<tool>.json`. Each of the
  last two may have a `<tool>.d/` folder too.
- **In one layer,** `<tool>.json` is read first, then `<tool>.d/*.json` in
  name order. A later file wins.
- **sessiontools installs no template.** A template file is read when one
  is there.

## One folder per package

Every Session Kit command keeps its host settings in the folder of the
package that ships it: `~/.sessionkit/<package>/settings/<command>.json`,
with its `<command>.d/` drop-ins, template and schema beside it. One map
says which package each command belongs to:

| Command | Package | Its folder |
| --- | --- | --- |
| `session`, `claude-code`, `sounds` | `tools` | `~/.sessionkit/tools/settings/` |
| `mail` | `mail` | `~/.sessionkit/mail/settings/` |
| `ears`, `mouth` | `voice` | `~/.sessionkit/voice/settings/` |
| `view` | `view` | `~/.sessionkit/view/settings/` |
| `phone` | `phone` | `~/.sessionkit/phone/settings/` |

- **There is no other host folder.** No environment variable or flag
  moves it, and no older folder is read as a fallback.
- **A command the map does not name** has no host folder. It is never
  guessed from a folder's name.
- **From Python:** `from sessiontools.settings import PACKAGES,
  package_dir, host_dir`. `PACKAGES` is the map above,
  `package_dir("tools")` is `~/.sessionkit/tools/settings/`, and
  `host_dir("sounds")[0]` is the folder a command's files are read from
  (the second item is always `None`).

### Upgrading from 0.1.0

0.1.0 kept these files in one folder per command, under `~/.settings/`:
`session/` and `sounds/`. A later build added `claude-code/` beside them.

- **They are not read any more.** Move them to
  `~/.sessionkit/tools/settings/`. Each command's files are named for
  it (`session.json`, `sounds.d/`, `claude-code.template.json`), so
  the folders' files never collide in the one folder.
- **Until you move them,** each `session` verb that reads a command
  whose files are still only in its old folder says so once on stderr,
  naming both folders. It reads nothing from the old folder and writes
  nothing. Its stdout is unchanged, so `--json` output and the status
  line stay clean.
- **The plugin's hooks say nothing.** The busy sound stays silent, as it
  is with no settings.
- **`session statusline install` refuses** while the `claude-code` files
  have not been moved: exit 3, nothing written, `--force` included.

## Who reads which layers

The cascade has five layers: template, host, agent, seat and session.
A reader that reads only the first two ignores a key set in an agent's
front matter, a seat's file or a session's file. This was measured in
the code, reader by reader.

| Keys | Layers read today | Read by |
| --- | --- | --- |
| `session`: `operator.*`, `list.max_age`, `pane_state.*`, `account.*`, `model.default` | template and host only | `session operator`, `session list`, `session pane-state`, `session account`, `session default-model` |
| `session`: `context.near_pct` | all five | `session context` |
| `session`: `home`, `home_dir` | all five (session only once its id is known at launch) | `session start` |
| `sounds`: every key | all five | the busy sound hook (the busy sound and the stop sound), `session sound` |
| `claude-code`: every key | all five (session only once its id is known at launch) | `session start` |
| `claude-code`: `statusLine` | template and host only | `session statusline install` and `check` |

- **The agent and the seat** come from the session's own record, which
  `session start` writes at birth ([written for you][written]).
  A session started some other way has no agent or seat layer.
- **The agent layer needs PyYAML.** The `session` command has it. The busy
  sound hook may run a bare `python3`; without PyYAML it skips the agent
  layer and reads the rest.
- **`_locked` is honoured by every reader**, in the layers that reader
  reads.

## The `session` tool's keys

File: `~/.sessionkit/tools/settings/session.json` (the host layer), or a
drop-in in `~/.sessionkit/tools/settings/session.d/`.

| Key | Type | Default | What it does | Layers read today |
| --- | --- | --- | --- | --- |
| `operator.address` | string | none | The operator's full mail address, used as written. `AGENT_OPERATOR_ADDR` wins over it, and grants no privilege. | template and host |
| `operator.mailbox` | string | none | The operator's local mail box name. With no `address`, the address is `<mailbox>@<this host>`, when that box exists under `~/.mail/agents` (`SESSION_MAIL_ROOT` overrides). `<this host>` is the full name when it is under the short host, else the short host; never a reverse-DNS name. | template and host |
| `operator.privileged` | list of strings | empty: the operator alone | Seat names or addresses that may act as administrators. The operator is always one. A list that is not a list is ignored. | template and host |
| `list.max_age` | number (seconds) | `2` | How old a `session list` snapshot may be and still be served. `0` turns the cache off. `SESSION_LIST_MAX_AGE` wins over it. | template and host |
| `pane_state.listeners` | list of strings | `["pager listen"]` | Commands that do not count as work, so a seat running only these reads FREE. An empty list: every tool shell is work. | template and host |
| `pane_state.interval` | number (seconds) | `1.0` | Seconds between the pane signal's ticks. Anything but a number above 0 reads as the default. | template and host |
| `context.near_pct` | number, 0 to 100 | `20` | How close to the settle threshold, in percent, counts as `near` in `session context`. Anything else reads as the default. | all five |
| `account.token_file` | string (a path) | none | On a host that signs in from a token file, the file `session account use` swaps. Unset, the route is the Keychain. | template and host |
| `account.limits_file` | string (a path) | none | The limits file `session account status` reads when no usage exporter answers. | template and host |
| `account.passage_prefix` | string (a folder) | `claude/oauth-tokens` | The passage folder `session account new` saves a token under. | template and host |
| `model.default` | string, a model `session default-model list` names | none: nothing is pinned | The model every new `claude` starts on. When set, `session default-model enforce` puts it back as the top-level `"model"` in Claude Code's settings.json if something rewrote it. Unset, or anything but a non-empty string, does nothing at all. A value off the list, or any Fable model, is refused and nothing is written ([the default model][default-model]). | template and host |
| `home` | boolean | `false` | Give the session a home directory, exported to it as `SESSION_HOME`. Where depends on the layer that set it: the agent's, the seat's, or the session's own. | all five |
| `home_dir` | string (a path) | none | The home directory itself. Setting it turns `home` on. `home: false` lower down turns it off again. | all five |

- **Nothing is a default person.** With no `operator` block, no one is the
  operator and no one is privileged.
- **A bad file removes, never grants.** For the operator, a file that
  cannot be read, is not JSON or has the wrong shape reads as nothing
  configured.
- **The account keys never guess.** A bad `token_file` or
  `passage_prefix` is refused with exit 5, naming the key and the file. A
  bad `limits_file` leaves the usage unavailable, and the exit is still 0.

## The `sounds` tool's keys

File: `~/.sessionkit/tools/settings/sounds.json` (the host layer), or
any lower layer. The schema is
[`settings/sounds.schema.json`][sounds-schema] in the source repo.
sessiontools does not install it; copy it to
`~/.sessionkit/tools/settings/sounds.schema.json` and `session settings`
checks the keys against it.

| Key | Type | Default | What it does | Layers read today |
| --- | --- | --- | --- | --- |
| `enabled` | boolean | off | The main switch for every sound. Only `true` turns any sound on. | all five |
| `busy.trigger` | `batch`, `landed` or `finished` | `batch` | When the busy sound plays: a tool batch still running after `delay` (`batch`), or a message waiting unread (`landed`). `finished`, the user's speech ended, is not built yet. Any other value is silent. | all five |
| `busy.player` | `afplay` or `engine` | `afplay` | Who plays it: a detached `afplay`, or the resident sound engine. Read by the `batch` trigger only: `landed` plays with `afplay`. Any other value is silent. | all five |
| `busy.auto_arm` | boolean | off | The batch trigger's switch: arm on every tool batch. Only `true` arms it. | all five |
| `busy.file` | string (a path) | none | The sound file, absolute or under `~/`. Missing or unreadable is silent. | all five |
| `busy.delay` | number (seconds), 0 or more | `batch`: none, so silent; `landed`, `finished`: `0` | Seconds before the sound starts. | all five |
| `stop.file` | string (a path) | none | The stop sound: played once when the seat's turn ends, queued through the mouth (`afplay` when the mouth is absent or refuses). Absolute or under `~/`. Missing, unreadable or relative is silent. | all five |

- **Unset is silent.** A seat with no `sounds` keys plays nothing.
- No `sounds` key is read from the environment.

## The `claude-code` tool's keys

File: `~/.sessionkit/tools/settings/claude-code.json` (the host
layer), or a drop-in in its `claude-code.d/`. `session start` hands the
result to Claude Code as one `--settings` file, under Claude Code's own
key names. Any other key is named on stderr and dropped.

| Key | Type | Default | What it does | Layers read today |
| --- | --- | --- | --- | --- |
| `model` | string | none | The model every session `session start` launches runs on. A `--model` typed on the command line still wins. | all five |
| `effortLevel` | string | none | The effort level, the same way. A typed `--effort` still wins. | all five |
| `statusLine.type` | string, `command` | none | Must be `command`, or no status line is passed on. | all five |
| `statusLine.command` | string | none | The status line script. `session statusline install` sets it, in `claude-code.d/50-statusline.json`. | all five |
| `statusLine.padding` | integer, 0 or more | none | Claude Code's status line padding. | all five |
| `statusLine.refreshInterval` | integer, 1 or more | none | Claude Code's status line refresh interval. | all five |
| `statusLine.hideVimModeIndicator` | boolean | none | Claude Code's vim mode indicator switch. | all five |

- **With no `claude-code` file anywhere**, `session start` adds no
  `--settings` of its own.
- **A `--settings` typed on the command line** is folded in on top:
  its keys win over every layer, except a key a lower file locks with
  `_locked`. Claude Code still gets one `--settings` file.
- **A `--settings` in an agent file's `claude:` section** gets nothing
  from this tool, and one stderr line says so.
- **`~/.claude/settings.json` is never written.**

## Written for you

`session start` writes these into the session layer at birth. They are not
settings for you to set:

- `~/.session/sessions/<session-id>/settings/session.json`: `agent`,
  `agent_file` and `seat`, each when there is one. The readers take a
  session's agent and seat layers from here.
- `~/.session/sessions/<session-id>/settings/<tool>.d/50-env.json`: each
  environment variable an installed schema names (`x-env`), read once. The
  environment is not a layer after that. sessiontools' own schema names no
  variable.

## Other tools

`session settings` also reads `mail`, `ears`, `mouth` and `phone`.
Their keys belong to sessionmail, sessionvoice and sessionphone, and
their own settings pages list them. The cascade itself is written
down as a specification, the session
settings specification. Where the family's files live and how the layers
stack: the Session Kit settings guide,
[sessionkit's docs/settings.md][kit-settings].

[kit-settings]: https://github.com/sessionkit/sessionkit/blob/HEAD/docs/settings.md
[sounds-schema]: ../settings/sounds.schema.json
[files]: #the-files
[layers]: #who-reads-which-layers
[written]: #written-for-you
[default-model]: default-model.md
