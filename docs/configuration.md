# Configuration

What sessiontools configures: which files, which environment variables,
and where each one lives. One section per part of the `session` command.

- **Settings files:** `session.json`, `sounds.json` and
  `claude-code.json`, side by side in `~/.sessionkit/tools/settings/`,
  the folder of the `tools` package. Every key, its default and the
  layers it is read from: [the settings page][settings]. Each package
  of the kit has its own, `~/.sessionkit/<package>/settings/`
  ([one folder per package][packages]); files from 0.1.0 are moved by
  hand ([upgrading][upgrading]).
- **Its own folders:** its state, moving from `~/.session/` (sessions,
  seats, agents, the state dir) to `~/.sessionkit/tools/state/`, where a
  fresh host starts the state dir and the sound engine
  ([where tools keeps its state][state-folders]), and
  `~/.sessionkit/tools/accounts/` (`session account`).
- **Environment variables:** each one belongs to one part, below. None
  is needed: unset, each part uses the default its table names.
- **The plugin's hooks** are switched by environment variables
  ([the hooks][hooks]).

Paths are shown under `~`, your home folder.

## `session list` and the live seats

`session list`, `session get` and `session context` read Claude Code's
own files. Nothing here needs setting.

| Name | Default | What it does |
| --- | --- | --- |
| `SESSION_SIDECAR_DIR` | `~/.claude/sessions` | Where Claude Code's per-session records are read from. |
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects` | Where the transcripts are read from. |
| `CONTEXT_MAX` | unset | A ceiling on the context window, in tokens. It only lowers a known model's window, never makes one. Set but not a positive number: the window is unknown. |
| `SESSIONS_DIR` | `~/.session/sessions` | The root of every session's folder. |
| `SESSION_STATE_DIR` | the `state` leaf ([below][state-folders]) | The state dir: the list snapshot, the pane signal's lock, the default model's backups and log, and each agent's claim and lock (`holders/`: the default seat's, and each hand-launched agent's), which `session list` reads for its SEAT column. Not `reap.log` since 0.3.2: it is in `~/.sessionkit/tools/log/`, which follows `HOME`, not this. |
| `SESSION_LIST_MAX_AGE` | `2` | Seconds a list snapshot may be served. Beats the `list.max_age` setting. `0` turns the cache off. |
| `SESSION_LIST_FRESH` | unset | A true word (`1`, `true`, `yes`, `on`) computes now, as `--fresh` does. |

- **The snapshot** is `list/snapshot.json` in the state dir
  ([the list cache][cache]).

## `session start`

`session start` reads agent files, writes each session's folder and
resolves two settings tools for it ([starting a session][start]).

- **Agent files:** `$AGENTS_DIR` when set. Else `~/.session/agent/config/`
  when it exists, else `~/.session/agents/`. An agent is
  `<name>/<name>.md` or `<name>.md`.
- **The session's folder:** `~/.session/sessions/<session-id>/`, or under
  `$SESSIONS_DIR`. Its `settings/` is the session layer, and its
  `launch/` holds the launch's private files.
- **An agent file's front matter** is the launch configuration:
  `start:`, `environment:` (`variables`, `files`, `secrets`, `inherit`)
  and `settings:`, the agent layer.
- **The session's home:** the `session` keys `home` and `home_dir`. It is
  handed to the session as `SESSION_HOME`, never `HOME`.
- **Model, effort, status line and renderer:** the `claude-code` tool,
  in `~/.sessionkit/tools/settings/claude-code.json`. It is passed to
  Claude Code as one `--settings` file, with `"tui": "default"` when
  nothing names `tui`. `~/.claude` is never written.
- **Credentials are never handed down.** `CLAUDE_CODE_OAUTH_TOKEN`,
  `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are dropped, unless the
  agent file names one under `environment: inherit:`. When one is set and
  dropped, `session start` says so on stderr and names the fix.

| Name | Default | What it does |
| --- | --- | --- |
| `AGENTS_DIR` | unset | Where agent files are read from. |
| `SESSIONS_DIR` | `~/.session/sessions` | The root of every session's folder. |
| `SESSION_KEY_INIT` | `~/.claude/agents/lib/session-keys/session-key-init` | The program `--keyed` runs to mint a session key. |
| `AGENT_ALLOW_FABLE` | unset | A true word (`1`, `true`, `yes`, `on`) lets a launch run on Fable. Unset, it is refused. |

## The operator

The operator is the human other sessions ask. `session operator` prints
who it is, and who is privileged ([operator and privileged seats][operator]).

- **The file:** the `operator` block of
  `~/.sessionkit/tools/settings/session.json`, or a drop-in in
  `~/.sessionkit/tools/settings/session.d/`.
- **Its keys:** `operator.address`, `operator.mailbox` and
  `operator.privileged` ([the keys][session-keys]).
- **Read from** the template and host layers only.

| Name | Default | What it does |
| --- | --- | --- |
| `AGENT_OPERATOR_ADDR` | unset | Changes where an ask goes. It grants no privilege. |
| `SESSION_MAIL_ROOT` | `~/.mail/agents` | Where `operator.mailbox` must exist as a folder before an address is made from it. |

## `session account`

`session account` keeps its own files, apart from the settings cascade
([switching Claude accounts][account]).

- **Its folder:** `~/.sessionkit/tools/accounts/`, beside the
  package's `settings/` folder and not inside it. The folder is 0700 and
  every file in it 0600: `accounts.json` (the registry),
  `account-config.json` (optional), `host-ledger.jsonl`, and the backups
  `use` makes. The old `~/.config/sessiontools/` is not read
  ([moving the old folder][account-move]).
- **`account-config.json`** has two keys: `host_read` (true or false,
  default true) and `usage_url` (default
  `http://localhost:9103/metrics`).
- **Three `session` settings:** `account.token_file`,
  `account.limits_file` and `account.passage_prefix`, read from the
  template and host layers ([the keys][session-keys]).

| Name | Default | What it does |
| --- | --- | --- |
| `PASSAGE_DIR` | `~/.passage/store` | The passage store `session account` reads, and `new` saves a token to. |
| `PASSAGE_IDENTITIES_FILE` | `~/.passage/identities` | The passage identities file. |

## The plugin hooks

The Claude Code plugin's hooks read the session's environment. A
settings file's `env` block reaches them too ([the plugin][plugin]).

| Name | Default | What it does |
| --- | --- | --- |
| `SESSION_SETTLE_AT` | unset: silent | The settle alert's threshold in tokens: `250000`, `250k`, `1.5M`. `session context` uses it too. |
| `SESSION_SETTLE_SAY` | the built-in line | The settle alert's line. `{at}` and `{used}` are filled in. |
| `SESSIONTOOLS_HOOK_SETTLE` | on | `off` (or `0`, `false`, `no`) switches the settle alert off. |
| `SESSIONTOOLS_HOOK_BUSY` | on | `off` (or `0`, `false`, `no`) switches the busy sound hook off: the busy sound and the stop sound. |
| `SESSIONTOOLS_HOOK_CLOCK` | off | `on` (or `1`, `true`, `yes`) says the clock line on every tool batch: the time and the context as used/max. |
| `SESSION_CLOCK_WARN_AT` | unset: no warn line | The clock line's warn mark: tokens (`250k`) or a percent of the window (`80%`). |
| `SESSIONTOOLS_HOOK_TURN` | off | `on` (or `1`, `true`, `yes`) sets the turn-ended mark at Stop. |
| `SESSIONTOOLS_HOOK_TITLE` | off | `on` (or `1`, `true`, `yes`) sets the session title to the short session id at SessionStart. |
| `SESSIONTOOLS_HOOK_SESSION_INFO` | off | `on` (or `1`, `true`, `yes`) adds the session-info block to the context at SessionStart. |
| `SESSIONTOOLS_HOOK_IDENTITY` | on | `off` (or `0`, `false`, `no`) stops the identity block being added to the context at SessionStart. A session with no block is silent either way. |
| `SESSIONTOOLS_LAUNCH_INFO` | unset | Set by `session start` to the session id its launch-info carries. The session-info block then leaves that id out on startup. |
| `SESSIONTOOLS_PYTHON` | the `session` command's python | The python the busy sound, settle alert, clock line and session-info hooks run. |

- **The settle alert:** [the settle alert][settle].
- **The clock line:** [the clock line][clock]. It and the settle alert
  are separate hooks with separate switches.
- **The busy sound** and **the stop sound** read the `sounds` tool:
  `~/.sessionkit/tools/settings/sounds.json` and every lower layer. Each
  is silent until set ([the busy sound][busy]).
- **The session-info hooks:** [the session-info hooks][session-info].
  The identity hook's block: [the identity block][identity].

## The busy sound and the sound engine

The busy sound is set in the `sounds` tool ([the keys][sounds-keys]). The
sound engine is an optional player, a per-user launchd agent, macOS only
([the sound engine][engine]).

- **Settings:** `~/.sessionkit/tools/settings/sounds.json`, or an agent,
  seat or session layer.
- **The engine's files:** its binaries in `bin/` of the `sound` leaf
  (`~/.sessionkit/tools/state/sound/` on a fresh host, else the old
  `~/.local/share/sessiontools/sound/`), its plist
  `~/Library/LaunchAgents/dev.sessionkit.tools.sound-engine.plist`, its
  log `~/.sessionkit/tools/log/sound-engine.log` (0.3.1 and before:
  `~/Library/Logs/sessiontools/`, left in place).
- **The mouth:** the busy sound never plays while the mouth speaks; the
  stop sound is queued behind it (`mouth play`, when `mouth` is on
  `PATH`). Its folder
  is found by sessionvoice's rule: `$MOUTH_DIR`, else `<home>/mouth`. The
  home is `$SESSIONVOICE_HOME`, else `~/.sessionvoice` if it holds a heard
  log, else `~/.voicemode` if it holds one, else `~/.sessionvoice`. A heard
  log is a `heard.lock` or a `heard_*.jsonl` in `<home>/logs/conversations`.

| Name | Default | What it does |
| --- | --- | --- |
| `SESSIONTOOLS_SOUND_HOME` | the `sound` leaf ([below][state-folders]) | Where the engine's binaries are built and found. |
| `SESSIONTOOLS_SOUND_PORT` | `44100` | The local UDP port the engine listens on. |
| `MOUTH_DIR` | unset | The mouth's folder, read to know when it speaks. |
| `SESSIONVOICE_HOME` | unset | sessionvoice's home; the mouth's folder is `mouth/` inside it. |
| `VOICEMODE_MOUTH_DIR` | unset | The old name of `MOUTH_DIR`, read only when `MOUTH_DIR` is not set. |
| `VOICEMODE_BASE_DIR` | unset | The old name of `SESSIONVOICE_HOME`, read when that is unset or empty. |

## The status line

`session statusline install` puts a status line in the `claude-code`
tool's host layer. A seat `session start` launches shows it
([the status line][status-line]).

- **The script:** `~/.sessionkit/tools/settings/statusline.sh`.
- **The setting:** `statusLine`, in its own drop-in,
  `~/.sessionkit/tools/settings/claude-code.d/50-statusline.json`.
- **Its log:** `~/.config/session/statusline-log.jsonl`, only when
  `~/.config/session/` already exists.

| Name | Default | What it does |
| --- | --- | --- |
| `SESSIONTOOLS_STATUSLINE_LOG` | on | `off` stops the status line's log. |
| `SESSIONTOOLS_STATUSLINE_LOG_MAX` | `26214400` (25 MB) | Bytes the log may reach. Past it, the next render moves it to `statusline-log.jsonl.1`; one old file is kept. |
| `SESSIONTOOLS_STATUSLINE_TAP` | on | `off` stops the usage tap. |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | The Claude Code folder `install` and `check` report on. Nothing in it is written. `session default-model` guards the `settings.json` in it ([below][model-section]). |

## The pane signal

`session pane-state` publishes BUSY, FREE or UNKNOWN on each seat's tmux
pane. Its publisher can run as a per-user launchd agent, macOS only
([the pane signal][pane-state]).

- **Settings:** `pane_state.listeners` and `pane_state.interval`, in
  `~/.sessionkit/tools/settings/session.json`, template and host layers only
  ([the keys][session-keys]).
- **The lock:** `pane-state/watch.lock` in the state dir.
- **The agent's files:** its plist
  `~/Library/LaunchAgents/dev.sessionkit.tools.pane-state.plist`, its log
  `~/.sessionkit/tools/log/pane-state.log` (0.3.1 and before:
  `~/Library/Logs/sessiontools/`, left in place).
- **The agent passes on** `TMUX_TMPDIR`, `SESSION_STATE_DIR`,
  `SESSION_SIDECAR_DIR` and `SESSIONS_DIR`, each when it is set at `on`.

## The default model

`session default-model` pins the model every new `claude` starts on. It
is off until `model.default` is set, and nothing installs its unit for
you ([the default model][default-model]).

- **Setting:** `model.default`, in
  `~/.sessionkit/tools/settings/session.json`, template and host layers
  only ([the keys][session-keys]). Unset, nothing is read or written.
- **The file it guards:** `~/.claude/settings.json`, only its top-level
  `"model"`; under `$CLAUDE_CONFIG_DIR` when set, the same folder
  the status line reports on.
- **Its own files:** backups in `default-model/backups/` in the state
  dir (the newest 10), and one line per write in
  `default-model/log.jsonl` beside them.
- **A unit carries** `CLAUDE_CONFIG_DIR` and `SESSION_STATE_DIR`, each
  when it is set at `unit`.
- **The unit's files,** once you install one: the plist
  `~/Library/LaunchAgents/dev.sessionkit.tools.default-model.plist` and
  its log `~/.sessionkit/tools/log/default-model.log` (0.3.1 and before:
  `~/Library/Logs/sessiontools/`, left in place), or the
  systemd user units `sessiontools-default-model.path` and `.service`.

## `session context` and `session ledger`

- **The band:** `session context` says `ok`, `near` or `over` against
  `SESSION_SETTLE_AT`. `near` is within `context.near_pct` percent of it,
  read from all five layers ([the keys][session-keys]).
- **The ledger's token counts** use tiktoken's cache:
  `$TIKTOKEN_CACHE_DIR`, else `~/.cache/tiktoken`.

## Where tools keeps its state

Each folder of tools' state is a leaf with an old and a new path. One
resolver in `sessiontools.settings` picks one for each leaf, and
`session path LEAF` prints it ([the page][state-paths]).

- **The rule:** the leaf's variable when it is set; else the old folder
  while it is a real directory; else the new folder when it exists, as
  after a move that left the old path a link to it; else where a first
  write goes: the new folder for `state` and `sound`, the old one for the
  rest.
- **Nothing moves by itself.** `session path migrate` moves `state` and
  `sound`, once per host, with that host's go, and leaves a link at each
  old path ([moving tools' state][migrate]).
- **Upgrade every copy of sessiontools on a host together:** an older copy
  writes the old `state` folder.

| Leaf | Old | New | Variable |
| --- | --- | --- | --- |
| `state` | `~/.session/state` | `~/.sessionkit/tools/state` | `SESSION_STATE_DIR` |
| `sessions` | `~/.session/sessions` | `~/.sessionkit/tools/state/sessions` | `SESSIONS_DIR` |
| `seats` | `~/.session/seats` | `~/.sessionkit/tools/state/seats` | none |
| `agent` | `~/.session/agent` | `~/.sessionkit/tools/state/agent` | none; `AGENTS_DIR` moves only the agent-file lookup |
| `backups` | `~/.session/backups` | `~/.sessionkit/tools/state/backups` | none |
| `sound` | `~/.local/share/sessiontools/sound` | `~/.sessionkit/tools/state/sound` | `SESSIONTOOLS_SOUND_HOME` |
| `log` | none read | `~/.sessionkit/tools/log` | none |

## Settings

sessiontools keeps its settings in JSON files that stack in layers:
template, host, agent, seat and session. The most specific layer wins.

- Every key sessiontools reads, its type, its default and which layers
  it is read from today: [docs/settings.md][settings].
- How the family's layers stack, and where the files live: the Session
  Kit settings guide, [sessionkit's docs/settings.md][kit-settings].
- What applies to one session: `session settings <session-id> [tool]`.

[settings]: settings.md
[state-folders]: #where-tools-keeps-its-state
[state-paths]: tools-state-paths.md
[migrate]: tools-state-migrate.md
[packages]: settings.md#one-folder-per-package
[upgrading]: settings.md#upgrading-from-010
[session-keys]: settings.md#the-session-tools-keys
[sounds-keys]: settings.md#the-sounds-tools-keys
[kit-settings]: https://github.com/sessionkit/sessionkit/blob/HEAD/docs/settings.md
[cache]: list-snapshot-cache.md
[start]: session-start-launcher.md
[operator]: operator-privileged-seats.md
[account]: account.md
[account-move]: account.md#moving-the-old-folder
[plugin]: claude-code-plugin.md
[settle]: settle-alert-hook.md
[clock]: clock-line-hook.md
[busy]: busy-sound-hook.md
[session-info]: session-info-hook.md
[identity]: identity-block.md
[engine]: sound-engine-agent.md
[status-line]: status-line-script.md
[pane-state]: pane-state-signal.md
[default-model]: default-model.md
[hooks]: #the-plugin-hooks
[model-section]: #the-default-model
