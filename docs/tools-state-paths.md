# Where tools keeps its state: `session path`

tools' state is moving from `~/.session/` (and, for the sound engine,
`~/.local/share/sessiontools/sound/`) to `~/.sessionkit/tools/state/`.
One resolver in `sessiontools.settings` decides each folder, and
`session path` prints its answer, so nothing else spells a path.

- **Nothing moves by itself.** An existing folder stays where it is
  until `session path migrate` moves it ([moving tools' state][migrate]).
- **A fresh host starts `state` and `sound` in the new folder.** The other
  leaves start in the old one until they move, later, with every seat
  stopped.
- **Upgrade every copy of sessiontools on a host together.** An older
  copy writes the old `state` folder; `session path --json` shows
  `stranded` when one lags ([why][together]).
- **A moved folder is followed.** Once a folder has moved and its old
  path is a link to the new one, every reader and writer uses the new one.
- **Ask, never copy.** A script or another package runs `session path`, or
  imports the resolver, rather than spell a folder of its own.

```console
session path sessions
```

## Each leaf, old and new

| Leaf | Old folder | New folder | Its variable |
| --- | --- | --- | --- |
| `state` | `~/.session/state/` | `~/.sessionkit/tools/state/` | `SESSION_STATE_DIR` |
| `sessions` | `~/.session/sessions/` | `~/.sessionkit/tools/state/sessions/` | `SESSIONS_DIR` |
| `seats` | `~/.session/seats/` | `~/.sessionkit/tools/state/seats/` | none |
| `agent` | `~/.session/agent/` | `~/.sessionkit/tools/state/agent/` | none |
| `backups` | `~/.session/backups/` | `~/.sessionkit/tools/state/backups/` | none |
| `sound` | `~/.local/share/sessiontools/sound/` | `~/.sessionkit/tools/state/sound/` | `SESSIONTOOLS_SOUND_HOME` |
| `log` | none read | `~/.sessionkit/tools/log/` | none |

- **`state`** holds the list snapshot, each agent's claim (`holders/`:
  the default seat's, and each hand-launched agent's), the default
  model's backups and log, and the pane signal's lock.
- **`agent`** holds agent files (`config/`) and each agent's home
  (`home/<agent>/`). `AGENTS_DIR` moves only where `session start` looks
  for an agent file. It never moves this leaf or an agent's home.
- **`log`** moved in 0.3.2 with no fallback: an old log is left in place
  and never read.

## The rule

For each leaf, the first that holds:

1. **Its variable is set:** that path, with no fallback.
2. **The old folder is real:** the old folder. It is a directory, and it
   is not a link to the new one.
3. **The new folder exists:** the new folder. That is the case after a
   move, when the old path is a link to it.
4. **Neither exists:** where a first write goes. That is the new folder
   for `state` and `sound`, and the old one for `sessions`, `seats`,
   `agent` and `backups`.

The old folder wins while it is real because the leaves nest: a first
write to `tools/state/sound/` makes `tools/state/`. Under "new wins", that
would strand a live `~/.session/state/`.

## `session path`

- `session path LEAF`: one absolute path and a newline.
- `session path sessions ID` and `session path seats SEAT`: the folder of
  that session or seat. The join is literal: no short-id lookup, and the
  folder need not exist.
- `session path --json`: every leaf as `{"path", "source"}`.
  `session path LEAF --json` gives that leaf alone, and
  `session path sessions ID --json` gives that session's folder with the
  leaf's `source`.
- `session path` alone: one line per leaf, for a person.
- An unknown leaf, or a NAME on a leaf that takes none, exits 2 with the
  usage.
- `session path migrate [--dry-run] [--json]`: moves `state` and `sound`
  to the new folder, once, leaving a link at each old path
  ([moving tools' state][migrate]).

`source` is exactly one of these words:

- `override`: its variable is set.
- `old`: the old folder, real.
- `new`: the new folder.
- `fresh-old`: neither exists, so a first write goes to the old folder.
- `fresh-new`: neither exists, so a first write goes to the new folder.

`stranded` is added when the old folder is used while the new one holds
something of its own. That something is never read or merged: a person
decides. A `tools/state/` that holds only the other leaves' folders
(`sound/`, `sessions/` and the rest) is not stranded state.

## For peers: mail, voice, taskmaster, the kit

- **Not Python:** run `session path LEAF`, or `session path --json` and
  read `.LEAF.path`. A session's folder is `session path sessions ID`.
- **Python:** import the resolver, the same one the verb reads:
  `from sessiontools import settings`, then `settings.sessions_root()`,
  `state_root()`, `seats_root()`, `agent_root()`, `backups_root()` or
  `sound_root()`. Each takes an optional `env` mapping. For the source too,
  call `settings.leaf_path(leaf)`, which returns `(path, source)`.
- **A copy you cannot avoid** (a hook with no Python on its hot path) must
  follow the rule above, with no rule of its own. tools' own copy is
  `hooks/busy-sound`, and a test holds it to the resolver.

See also: [moving tools' state][migrate], [configuration][configuration],
[the `session` command][session].

[migrate]: tools-state-migrate.md
[together]: tools-state-migrate.md#upgrade-every-copy-together
[configuration]: configuration.md
[session]: session.md
