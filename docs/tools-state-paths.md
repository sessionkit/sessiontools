# Where tools keeps its state: `session path`

tools' state is moving from `~/.session/` (and, for the sound engine,
`~/.local/share/sessiontools/sound/`) to `~/.sessionkit/tools/state/`.
One resolver in `sessiontools.settings` decides each folder, and
`session path` prints its answer, so nothing else spells a path.

- **Nothing moves by itself.** An existing folder stays where it is
  until `session path migrate` moves it ([moving tools' state][migrate]).
- **A fresh host starts every leaf in the new folder**, so it makes
  nothing under `~/.session/`. A fresh host is one with no `~/.session/`
  of its own.
- **A host that has `~/.session/` keeps its old folders**, even for a
  `sessions`, `seats`, `agent` or `backups` folder it has not made yet.
  Only `state` and `sound` start new there. Those four leaves move later,
  host by host, with every seat stopped.
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
- `session path agent NAME`: that agent's home, `<agent leaf>/home/NAME`
  ([an agent's home and folder][agent]).
- `session path agent [NAME] --config`: the agents folder the launcher
  looks in, or NAME's own folder in it ([an agent's home and
  folder][agent]).
- `session path --json`: every leaf as `{"path", "source"}`.
  `session path LEAF --json` gives that leaf alone, and
  `session path sessions ID --json` gives that session's folder with the
  leaf's `source`. `session path agent NAME --json` is the one answer
  with another shape ([the agent contract][contract]).
- `session path` alone: one line per leaf, for a person.
- An unknown leaf, a NAME on a leaf that takes none, or `--config` on any
  leaf but `agent` exits 2 with the usage.
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

## An agent's home and folder: `session path agent`

An agent has two folders, and `session path agent` answers both:

- **Its home** is its SESSION_HOME, `<agent leaf>/home/NAME`, from
  `settings.agent_home`. It is the same string as
  `$(session path agent)/home/NAME` on the same host.
- **Its folder** holds its agent file beside its extras, such as
  `avatar.jpg`. It comes from the launcher's own lookup, `start.agents`,
  so it is the folder the agent starts from.

| Command | Prints | Exit |
| --- | --- | --- |
| `session path agent` | the agent leaf | 0 |
| `session path agent NAME` | NAME's home, whether or not it exists | 0 |
| `session path agent --config` | the agents folder | 0 |
| `session path agent NAME --config` | NAME's folder, or nothing | 0; 2 when the launcher refuses NAME |
| `session path agent NAME --json` | [the agent contract][contract] | 0 |
| `session path agent --config --json` | the agents folder as `{"path", "source", "rung"}` | 0 |
| a bad NAME, with any flags | the usage, on stderr | 2 |

- **The home need not exist.** An agent with no `home/NAME/` still gets
  its path, exit 0, and nothing is made. This is the rule `sessions ID`
  and `seats SEAT` follow. A script that cares tests it:

  ```sh
  test -d "$(session path agent NAME)"
  ```

- **NAME need not be an agent for its home.** The join never reads the
  agents folder, so a seat with a home and no agent file still gets one.
  `AGENTS_DIR` never moves a home.
- **The agents folder** is the first of these that holds, as the launcher
  looks:
  1. `$AGENTS_DIR`, when it is set.
  2. `config/` in the agent leaf, when it is a directory.
  3. On a fresh host (no `~/.session/` of its own), `config/` in the
     agent leaf before it exists: `~/.sessionkit/tools/state/agent/config/`,
     `fresh-new`. That is where a first agent file goes, so writing it
     makes nothing under `~/.session/`.
  4. Else `~/.session/agents/`, the pre-4.2 flat folder, read only.
- **NAME's folder** is `<agents folder>/NAME/`, when it holds
  `NAME.md`. A flat agent (`<agents folder>/NAME.md`) and the built-in
  default seat have no folder: `--config` prints nothing, exit 0.
- **The launcher refuses NAME** when there is no agent of that name, or
  a `NAME/` folder without its `NAME.md`. `--config` then exits 2 with
  the launcher's own message.
- **A bad NAME** is empty, `.` or `..`, or holds a `/` or a NUL. It
  exits 2 with the usage whatever the flags, and nothing is made.

### The agent contract: `agent NAME --json`

`session path agent NAME --json` prints one document, so a peer gets the
home and the folder in one call. `--config` does not change it.

```json
{
  "version": "<sessiontools version>",
  "root": {"path": "<agent leaf>", "source": "old"},
  "items": {
    "home": {"path": "<agent leaf>/home/NAME", "source": "old"},
    "agents": {"path": "<agents folder>", "source": "old", "rung": "agents"},
    "config": {"path": "<agents folder>/NAME", "source": "old",
               "file": "<agents folder>/NAME/NAME.md", "builtin": false}
  }
}
```

- **`version`**: sessiontools' version, or `0+unknown` when its metadata
  cannot be read. Cache on it.
- **`root`**: the agent leaf, exactly as `session path agent --json`
  prints it, with `stranded` when set.
- **`items.home`**: NAME's home, with the agent leaf's `source`.
- **`items.agents`**: the agents folder. `rung` names the step that
  answered: `AGENTS_DIR`, `config` or `agents` (a fresh host's `config/`
  before it exists is `config`). `source` is `override` for
  `AGENTS_DIR`, the agent leaf's source for `config` (`fresh-new` on a
  fresh host, until the folder is made), and `old` or `fresh-old` for
  `agents`.
- **`items.config`**: NAME's folder as the launcher finds it. `file` is
  its agent file, and `builtin` is true for the default seat, whose file
  is the package's own.
- **`items.config.path` is `""` when NAME has no folder**: a flat agent
  or the built-in seat. Test it before joining `avatar.jpg`: an empty
  path joined to it names no agent's face.
- **A refused NAME does not fail `--json`.** It exits 0, and
  `items.config` carries `"path": ""`, `"file": ""`, `"builtin": false`
  and `refused`, the launcher's message. `items.home` is still a true
  answer: a seat need not be an agent.
- **Every other `--json` keeps its shape.** `session path --json`,
  `LEAF --json`, `sessions ID --json` and `seats SEAT --json` are
  unchanged; only `agent NAME --json` is a contract.

## For peers: mail, voice, taskmaster, the kit

- **Not Python:** run `session path LEAF`, or `session path --json` and
  read `.LEAF.path`. A session's folder is `session path sessions ID`.
- **Python:** import the resolver, the same one the verb reads:
  `from sessiontools import settings`, then `settings.sessions_root()`,
  `state_root()`, `seats_root()`, `agent_root()`, `backups_root()` or
  `sound_root()`. Each takes an optional `env` mapping. For the source too,
  call `settings.leaf_path(leaf)`, which returns `(path, source)`.
- **An agent's home and folder:** run `session path agent NAME --json`.
  From Python, `paths.agent_report(name, env)` returns the same dict.
  It reads `start.agents`, the launcher's lookup; `settings.agent_config`
  reads only `config/` in the agent leaf, so it can miss a face.
- **The kit's agent file:** run `session path agent --config --json`,
  make its `path`, and write the file there. On a fresh host that is
  `~/.sessionkit/tools/state/agent/config/`, so no leaf turns old.
- **A copy you cannot avoid** (a hook with no Python on its hot path) must
  follow the rule above, with no rule of its own. tools' own copy is
  `hooks/busy-sound`, and a test holds it to the resolver.

See also: [moving tools' state][migrate], [configuration][configuration],
[the `session` command][session].

[migrate]: tools-state-migrate.md
[agent]: #an-agents-home-and-folder-session-path-agent
[contract]: #the-agent-contract-agent-name---json
[together]: tools-state-migrate.md#upgrade-every-copy-together
[configuration]: configuration.md
[session]: session.md
