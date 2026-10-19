# Changelog

All notable changes to sessiontools are documented here. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The one
version lives in `pyproject.toml`; `scripts/release.py` bumps it with
`uv version`, mirrors it into `.claude-plugin/*.json` when present, and
stamps this file.

## [Unreleased]

### Added

- **`session completion bash|zsh`: shell completion for `session`.** It
  prints a static script on stdout, built from the dispatcher's own verbs and
  each verb's argparse parser, never from a second list or from `--help`
  text. TAB completes verbs, sub-verbs (the hand-dispatched ones included,
  such as `sound engine` and `pane-state agent`), long flags, positional
  words from a verb's own table, and directories for directory values. TAB
  runs no command. The bash script works in bash 3.2 and later, with or
  without the bash-completion package. The zsh script registers with
  `compdef` after compinit. Any other shell is a usage error (exit 2). If
  PyYAML will not import, it prints no script and exits 1. Install is one
  line per shell; a drift test fails when the script and the dispatcher
  disagree. See `docs/shell-completion.md`.
- **`session park`: a seat says it has settled.** It runs five checks
  (`tree`, `work`, `claim`, `mail`, `promises`), each PASS, FAIL or UNKNOWN,
  writes one record, `<session dir>/park.json` (atomic, 0600), and prints
  what they found. The seat is clean only when all five PASS. The record
  holds when the last real work ended, never the park's time, so parking
  again buys no time. `--promise TEXT`, `--no-promises` and `--handed-on
  REF` declare promises; `--letter REF` names the handover; `--keep-until T
  --why R` records a request (absolute ISO 8601 with an offset) that nothing
  acts on yet. `session park --check [SEAT]` reads it back for this seat or
  any live seat and writes nothing: a turn or work after the park means not
  parked. Text and `--json` give one verdict. Exits: 0 parked and clean, 1
  parked and not clean, 2 usage, no such seat or no verdict, 3 not parked
  (`--check`), 4 the write failed. Nothing here kills, mails or writes
  config, and `session reap` is unchanged. Run it as a call of its own: work
  chained after it unparks it. See `docs/session-park.md`.
- **`session start` installs the mail schema before it reads the
  environment at birth** (ST-176). The birth reads only variables an
  installed schema names, and the mail schema used to be installed only
  after the session started. So the first session on a fresh home did not
  get `SESSIONMAIL_SUBSCRIBE`. When `pager` is on `PATH`, the launch
  script now runs `pager settings install` just before the birth. Its
  output goes to the session's `log/settings.jsonl`, never the terminal.
  A file it could not write is one stderr line naming that log. A `pager`
  without the verb is logged quietly. Nothing here stops a launch.
- **`session account status` reads the limits file when the exporter has
  no answer** (ST-172). The `session` setting `account.limits_file` names
  the file; it has no default, and unset changes nothing. Its `5h` and `7d`
  windows are shown with their reset times, by default as for the exporter,
  and with the file's age, `STALE` past 15 minutes. A window whose reset
  has passed shows no percent. A file that cannot be used, or
  another account's window, leaves usage unavailable with the reason.
  `--json` now carries that reason as `usage_unavailable` whenever `usage`
  is null.
- **`session account use` on a token-file host.** Some hosts sign in from
  `CLAUDE_CODE_OAUTH_TOKEN`, exported by the login shell from a file, not
  from the Keychain. Name that file in the `session` setting
  `account.token_file` (no default) and `use` swaps it: back it up
  (0600, `token-file-backups/`), stage the new token beside it, read the
  staged file back, rename it over the file, and read it back again. A
  failure restores the backup (exit 6), or prints the `install` line to
  run by hand (exit 7). A symlink is kept. On this route `use` never runs
  `security`. Live seats keep the token they launched with until they
  restart, so the plan names every live seat, and says whether this seat
  holds the old token. With the setting unset and no Keychain item, `use`
  refuses (exit 5) and names the setting. The ledger row gains `route`
  (`keychain` or `token-file`). `status` and `list` read the token file
  on such a host (`token file, fp match`), and label an env token
  `env token: from launch; moves at restart`.

- **`session pane-state agent on|off|status`: a launchd agent runs the
  pane signal's publisher, one per host** (ST-166). It is modelled on
  the sound engine's agent. Its plist names the install's own `session`
  script by absolute path (`pane-state watch`) and gives the job a PATH
  that holds tmux. It also passes through the variables that move where
  `watch` looks. `on` refuses while another publisher holds the watch
  lock, and exits 0 only once the job holds it. `--dry-run` prints the
  plist and changes nothing. One `LABEL` constant names the plist, the
  launchctl target and the plist's `Label`. On a real host, `on` IS the
  deploy. Tests run behind `SESSIONTOOLS_PANE_STATE_NO_LAUNCHCTL`.
- **Adding a Claude account in one verb: `session account new NAME --email
  E`** (ST-159). It does the three hand steps: `claude setup-token`, a save
  to passage at `PREFIX/E`, and `add`. PREFIX is the `session` setting
  `account.passage_prefix`, read through the settings cascade's template and
  host layers; unset, it is `claude/oauth-tokens`. Every cheap check runs
  first (terminal, NAME, E, registry, passage, the entry absent), and
  nothing is written before the token is proven: its shape, then a `claude
  -p` preflight whose child gets an allowlist of the environment. Then
  `passage insert -m` with the token on stdin, a read-back compared by
  SHA-256, and registration as `passage:ENTRY`, fp known. setup-token runs
  on a private pty: the person sees a masked copy of its screen on the
  alternate screen, no byte of the child reaches the terminal, and a frame
  that shows a token is never drawn. Ctrl-C twice is `new`'s own exit. It
  never overwrites or deletes a passage entry. Exit codes 0, 2, 4, 5, 8 (not
  saved) and 9 (saved, not registered). Tested against stubs only; not yet
  run live.
- **A `passage:` token source for accounts** (ST-159). `add --source
  passage:ENTRY` resolves through `passage show` (age), refuses a
  missing entry before decrypting, and lists as `passage (touch)`.
  `pass:` (gpg pass) is unchanged, and `session start` secrets do not
  take `passage:` yet.
- **The bare `session context --json` carries `band`** (ST-155), so
  the sidebar can paint the context colour without the `--parts` fold.
  It is the same `ok`, `near`, `over` or null that `--parts --json`
  carries, from the same helper (the settle threshold and the
  `session` `context.near_pct` cut), on the one-liner's own `used`
  (the peak with `--peak`); a synthetic-zero tail has none. The JSON
  only gains the field. The one-line text is unchanged, and the bare
  path still imports nothing from `context_parts`: `band` moved to
  `sessiontools.band` (no shim left behind).
- **What filled the window: `session context --parts | --startup | --boot |
  --hooks`, with `--tools` and `--json`** (ST-26). `--parts` splits the
  window into AT BOOT and SINCE BOOT, both exact, and splits since-boot
  growth by category with a per-session fit; what the fit misses prints as
  Unattributed. `--startup` is the boot alone; `--boot` runs to SETTLED and
  says which signal fired; `--hooks` is hook context by command, with its
  repeats. `--tools` splits Tool results by tool. Every option's help says
  what it does and gives an example; every figure names its unit, `~` marks
  a fitted one and `?` an unknown. `--parts --json` carries `band` (`ok`,
  `near`, `over`) against the settle threshold. The one-line default is
  unchanged and reads nothing new. `--startup` and `--boot` replace the
  retired session's `context-startup` and `context-boot` (the boot now reads
  a pointer brief as the brief); `--parts` and `--hooks` port the earlier
  `ctx-parts` and `hook-dupes` prototypes. `context-prom` and the
  `tool-debug` hook are dropped. The `session` skill does not teach the
  views yet (ST-26, with `ledger` and `account`).
- **`session start` sets the host's model and effort ahead of the user
  file** (ST-151). The `claude-code`
  settings tool, resolved through the cascade (host, agent, seat,
  session), is passed to claude as one 0600 `--settings` file under the
  session's `launch/`. Claude ranks it above `~/.claude/settings.json`,
  which `/model` and `/effort` rewrite, and below a typed `--model`. Only
  `model` and `effortLevel` in v1; any other key is one stderr line. The
  host file is read from `~/.sessionkit/tools/settings/`. A launch whose
  agent file's `claude:` section carries its own `--settings` is left
  exactly as it was, with one stderr line; one typed on the command line
  is folded in as the top layer. No `claude-code` file: nothing changes.
  `~/.claude/settings.json` is never written.
- **Closing idle seats: `session reap [--idle DURATION] [--json] [--yes]
  [SHORT ...]`** (ST-146). It lists every seat idle past a limit (default 1h,
  `--idle 45m` to change it), each with CLOSE or KEEP and the reason. It is
  a DRY RUN by default and closes nothing. CLOSE needs positive evidence
  that the work is done, and all of it must hold:
  - the seat has a pane of its own, and its pane signal reads FREE;
  - its cwd is in a task whose status is terminal (`completed`, `archived`,
    `duplicate`, `todont`);
  - its name's task token, if it has one, names that task;
  - the task's worktree and store directory have no unsaved work (a
    lock-free `git status`).

  Everything else is KEEP, every unknown included, so a seat waiting on a
  person is never closed. "Hatched" (`review`, every delivery landed) does
  not count as done yet. A dry-run CLOSE is advice. `--yes` re-reads every
  gate at the act, then adds checks that run only there:
  - the pane signal once more, just before the kill;
  - the pane's holder must be the seat or on its line;
  - only an installed launcher (`tm task work`, `session start`, matched by
    real path) or a login shell may sit on that line;
  - the pane must hold nothing else, in its process tree or on its
    terminal;
  - it must not be its server's last pane.

  Then it closes the seat's PANE by id (`kill-pane`, never `kill-window`),
  a deliberate stand-in for the standdown path until TM-5310. It verifies
  the close, and a process left alive is named, never killed. A read that
  fails is "not verified", exit 1. Each act is one line in `reap.log` in
  the state dir. No real `--yes` has run yet. Every test runs on a
  private `tmux -L` server with fake seats. The `session` skill does not
  teach it yet: `tests/test_plugin.py` holds it in `HELD`. See
  `docs/session-reap.md`.
- **Switching Claude accounts: `session account list | status | use NAME
  | add NAME | remove NAME`** (ST-22). `use` moves the
  HOST: it rewrites the one Keychain item, `Claude Code-credentials`,
  from a token already held in `pass` or a private 0600 file. No
  browser, no `/login`, no restart. Every seat on the Mac with no env
  token follows within about 50 s, and each one's cache cold-starts, so
  `use` shows that cost and writes nothing without `--yes`. It needs a
  `--witness`, preflights the token with one `claude -p` before any
  write, backs the item up (0600), verifies the write by fingerprint,
  and restores the backup when that fails. `status` names the host's
  account by fingerprint, says whether this seat runs on an env token,
  and reads plan usage from the exporter. `list` resolves no token.
  `add` registers sources that already exist and stores no token;
  `remove` unregisters and deletes nothing. The registry
  (`~/.sessionkit/tools/accounts/accounts.json`) holds names and
  fingerprints, never a token, and no verb prints one;
  `python -m sessiontools.account.leakscan` finds a leaked one by
  place, never by value. The env-token (one seat) route is out of v1.
  No live `use` has run yet. The `session` skill
  does not teach it yet (ST-26, as `ledger`): `tests/test_plugin.py`
  holds it in `HELD`. See `docs/account.md`.
- **The token ledger: `session ledger [SEAT] [--summary | --short | --long |
  --json] [--width N]`** (ST-2). One session's transcript, item by item:
  every `Read`, every nested-memory and boot-memory load, every `Bash`
  command and every other tool result, each with the tokens of the text that
  entered the window (line-number prefixes and persisted-output previews
  included). A persisted output's full size shows beside it, marked not
  read. Four views: `--summary` (the default: counts and totals by kind, the
  ten largest, every repeat), `--short` (one line per item, largest first,
  commands cut to `--width` characters, default 16), `--long` (one row per
  item, every figure) and `--json` (one record per item, which concatenates
  across sessions). Counts are tiktoken `cl100k_base`, a ±10% estimate of
  Claude's tokenizer, labelled on every view and record. The counter runs
  offline through `uv` and never downloads its vocabulary: with no
  vocabulary the verb exits 2 and says to run `tokens` once. SEAT resolves
  as `session context`'s does. The `session` skill does not teach it yet:
  that waits on ST-26. `tests/test_plugin.py` holds it in `HELD`; when that
  is settled, strike that entry and add the skill line.
- **The pane signal: `session pane-state show | watch`** (ST-134). Each
  seat's state goes onto its tmux pane as the user option `@session_state`:
  `BUSY`, `FREE`, or unset (UNKNOWN), with `@session_state_why` beside it. A
  display reads `#{@session_state}` with no spawn per redraw (measured: none
  in 20 s; the `#(date)` control spawns one a second). It works with
  `SESSIONTOOLS_HOOK_TURN` off. A background test under a seat whose turn
  has ended reads BUSY, and a seat whose only child is its own `pager
  listen` reads FREE. `watch` is one publisher per host (an flock, and a
  second one exits 0 naming the holder). It writes on change only, reads
  `ps` only when something moved, work is live, or 30 s have passed, and
  unsets everything it wrote on SIGTERM. `--dry-run` writes nothing and
  takes no lock; `--once` leaves its writes standing. `show` reads once and
  writes nothing. New settings: `pane_state.listeners` (a trust list,
  default `pager listen`) and `pane_state.interval`. Nothing starts the
  publisher on a real host yet. See `docs/pane-state-signal.md`.
- **The status line, one source: `session statusline install | check |
  sample`** (ST-136). Claude Code's status line now ships in the package as
  `sessiontools/statusline/statusline.sh`: the live line in daily use,
  reconciled with the claude-code-tools copy. It has an off switch for the
  usage tap and for the log, and the log now rotates at 25 MB. The script is
  fail-soft (exit 0 and a line, whatever a helper does) and makes no network
  call. `install` copies a stamped, editable copy into the config dir
  (ST-165 moves it to the kit layer: see Changed), sets `statusLine`, and
  reads both back. It never replaces an existing status line without
  `--force`, which backs up first. `check [--diff]` says pristine, edited or
  not ours, and what overrides it. `sample` prints the sample payload. See
  `docs/status-line-script.md`.
- **The busy sound can play through the resident sound engine** (ST-132,
  the hook side). New `busy.player` in
  the `sounds` settings: `afplay` (unset: today's path, unchanged) or
  `engine`. Any other value is silent in every mode. With `engine`, the
  session's first arm starts python once: it writes the engine's record,
  `<session dir>/sounds/busy/engine.<claude pid>` (0600, atomic, back-dated
  1 s as `off` is), unlinks any other, and sends `arm`. After that,
  `hooks/busy-sound` sends every arm, end, stop and clear as ONE OSC
  datagram (`/sound/event ,ssd EVENT SID T`, 76 bytes for a uuid sid) to UDP
  `127.0.0.1:44100`, with the native sender `sound-event`
  (`src/sessiontools/sound/event.swift`, Darwin only) and no python. The
  sender reads the hook payload and sends nothing for a subagent's event
  (a top-level truthy `agent_id`, python's rule). New
  `sessiontools.sound`: `PORT`, `encode()`, `send()` (python's client),
  `home()`, `sender()`, `installed()`. The sender's home is
  `$SESSIONTOOLS_SOUND_HOME`, else `~/.local/share/sessiontools/sound`; not
  installed counts as off, and an engine that is off is silent. A resumed
  seat (a new claude pid) resolves once and writes its own record. A
  resolve that does not come out `engine` unlinks the records and tells the
  engine `clear`. Read by the batch trigger only: `landed` keeps afplay. The
  ST-98 turn-ended mark is unchanged and still runs first. The engine itself,
  its build and its launchd agent land separately; see the entries below.
- **The sound engine plays** (ST-132, the engine's audio).
  `engine.swift` gains its output: an AVAudioEngine with one
  AVAudioSourceNode into the main mixer, a fixed table of 8 voices, and a
  render block that never allocates, locks or retains (raw memory and
  `Synchronization.Atomic`s; a test scans it). A stop or a mouth cut fades
  over 20 ms. Sounds are decoded at first use, held as 48 kHz stereo Int16
  (the mixer resamples to the device), and evicted after 10 minutes unused;
  a file changed on disk is decoded again. Files in Desktop, Documents,
  Downloads or iCloud Drive are refused unopened, symlinks followed, so no
  privacy prompt reaches the screen; `status` counts refusals and names the
  last. It follows the default output device (configuration changes and the
  Core Audio default-device listener), rebuilding with backoff, and exits 3
  for launchd if it cannot restart within 10 s. It never touches the
  microphone or the device's rate or buffer size. New flags: `--volume
  0..1`, `--trace FILE` (JSONL per datagram and per voice start, with
  CLOCK_UPTIME_RAW host times, for the measurement), `--script ...
  --offline OUT.wav` (the real graph in manual rendering mode, frame-exact,
  no device) and `--evict-after S` (tests). Bare, it opens the device;
  `SESSIONTOOLS_SOUND_NO_DEVICE` (set for every test by `tests/conftest.py`)
  refuses that. The no-record latch is now pruned after ten minutes.
- **The sound engine's TCC tail** (ST-132).
  - Each seat's record dir (`<sessions>/<sid>/sounds/busy`) is classified
    before it is listed. So is a record that is itself a link. A session dir,
    `sounds/busy` or record linked into a protected place is refused:
    `refused` with `what: "records"`, once per sid, and counted in `status`.
    Refusals are cached per sid; an allowed dir is classified again at every
    arm.
  - A mouth that turns refused after its arm is now counted in
    `status.refused` and shown as `last_refused`, with its sid, like one
    refused at arm. It is counted once while it stays refused, and again if
    it comes back and is refused anew (before, only once per path for the
    engine's life).
  - New refused places: `~/Library/Containers` and
    `~/Library/Group Containers` (class `appdata`, macOS 14's App Data
    prompt), and `~/Library/Mail` and `~/Library/Messages` (class
    `private`).
  - The mouth and the records are read at the path the check resolved,
    never through their links again (the sound file too: see the next
    entry). Before, a link moved between the check and the read could lead
    the read into a protected place. A live test caught it as a hang, about
    1 run in 12.
- **The sound file and the records are read at the checked path** (ST-132).
  - The busy sound is decoded from the path its TCC check resolved: a link
    moved between the check and the decode no longer leads it somewhere
    unchecked. A checked file swapped for a link before the decode is
    refused as `unreadable`, not followed. (Until the change below, the
    decode then opened that path again by name, a window of microseconds;
    the next entry closes it.) The decode cache
    stays keyed by the record's `file`, which the log lines still name; it
    keeps the checked path and decodes again once the name leads elsewhere.
  - A record is read through `open(O_NOFOLLOW | O_NONBLOCK)` (now
    `O_NOFOLLOW_ANY | O_NONBLOCK`, see below), then `fstat`
    and `read` on that descriptor: a record swapped for a link after `find`
    checked it is never followed, and a FIFO cannot hang the engine.
  - `--script` has a `relink` step (`{"t": T, "relink": PATH, "to": TARGET}`)
    that moves a link at exactly the instant between a path's check and its
    read, so the tests make the race every run.
- **The sound file is opened once and decoded from that descriptor** (ST-132).
  - `decode()` opens the checked path with `O_NOFOLLOW_ANY | O_NONBLOCK`,
    checks the descriptor with `fstat` (a regular file), and hands
    AVAudioFile `/dev/fd/N`, which dup()s it. The decode reads the inode
    that open reached and never looks the name up again, so a link renamed
    over the file after the open is never read. A link there before the open
    is refused as `unreadable`. The null output opens it the same way.
  - **The intermediate-directory swap is closed too**, for every read after
    a check: `O_NOFOLLOW_ANY` (open) and `AT_SYMLINK_NOFOLLOW_ANY` (stat)
    refuse a link ANYWHERE on the path (ELOOP), and the checked path has
    none, so a directory swapped for a link since the check is never
    crossed. That covers the sound file, its decode cache's stat, each
    record and its directory listing (now read through a descriptor, not
    `FileManager`), the mouth's `playing.json` and `player.pid`, and the TCC
    walk's own stats. No `openat` walk is needed. (`O_NOFOLLOW_ANY` replaces
    `O_NOFOLLOW`: the kernel refuses the two together, EINVAL.)
  - `relink` takes `"at"`: `check` (the default, as before), `open` (just
    before the sound's open) or `decode` (after it), and may relink a
    directory the path is under. The tests decode WAV, AIFF, CAF and AAC
    from the descriptor offline, with no device.
- **A `player.pid` that is not a regular file never blocks the tick**
  (ST-132).
  - The mouth's `player.pid` is opened like every other checked read
    (`O_NOFOLLOW_ANY | O_NONBLOCK`), then `fstat`ed. A FIFO there used to
    block the open with no writer, which stalled the one tick every seat
    shares; only a REFUSED mouth was safe from it. Now a FIFO, a directory
    or any other non-regular file is not live, so it does not hold the
    seat. busy's `_pid_live` now does the same (ST-135, under Fixed).
- **`session sound engine on` keeps its port refusal** (ST-132). A loaded
  job no longer makes the port's holder ours: `on` replaces the engine only
  when every process `lsof` names as BOUND to the port is the job's own pid
  (`launchctl print`). A foreign holder, a job that is loaded but not
  running, or a holder `lsof` cannot name is refused (exit 2, nothing
  changed), and a socket merely connected to the port is a client, not a
  holder. Before, a foreign holder beside our loaded job made `on` write the
  plist, reload and exit 1.
- **The sound engine's TCC refusal, widened** (ST-132). Every path the
  engine would touch is classified from its string before anything stats or
  opens it: the sound file, the mouth's `playing.json` and `player.pid`
  (which `VOICEMODE_MOUTH_DIR` or `VOICEMODE_BASE_DIR` can point anywhere),
  and the sessions root.
  - Refused places: Desktop, Documents, Downloads, iCloud Drive, and now
    `~/Library/CloudStorage`, `/Volumes/*` (removable or not: that cannot be
    told without asking the volume), `/net`, `/Network`, and any mount the
    kernel's table (`getfsstat`, `MNT_NOWAIT`) flags non-local or removable.
  - The log line keeps `reason: "tcc"` and adds `class` (`folder`, `icloud`,
    `cloud`, `volume`, `network`, `removable`); a mouth or sessions refusal
    also says `what` and `path`.
  - A refused mouth latches its seat at arm, before any tick reads it: that
    seat is silent until `end`/`stop`/`clear`, and every other seat goes on.
    A mouth that becomes refused after its arm (a moved symlink, a new mount)
    counts as speaking, so it stays silent, and is logged once.
  - A sessions root in a refused place reads no record: it is logged at
    start, and every arm says `sessions-refused`.
  - `sound-engine --classify PATH` prints the verdict and every path the
    walk lstat()ed; `--mounts FILE` declares test mounts. The tests use only
    strings, temp dirs and conftest's HOME, plus a FIFO `player.pid` that an
    engine opening it would block on.
- **`session sound engine build|on|off|status`** (ST-132). New
  `sessiontools.sound.service`.
  - `build [--dest DIR] [--force]` compiles `sound-engine` and `sound-event`
    from the packaged Swift sources with the system `swiftc`, into
    `~/.local/share/sessiontools/sound/bin` (or `$SESSIONTOOLS_SOUND_HOME`).
    A build is all or nothing: a failed compile leaves the old binaries in
    place. Each binary has a `<name>.source-sha256` beside it, and it is
    rebuilt when that hash differs from the packaged source's hash.
  - `on [--dry-run]` builds if stale and probes `127.0.0.1:44100/udp`. It
    refuses (exit 2, nothing changed) when the port is held by anything but
    its own loaded agent, naming the holder from `lsof`. Then it writes
    `~/Library/LaunchAgents/dev.sessionkit.tools.sound-engine.plist`,
    boots out the old job and waits for it to go, and bootstraps into
    `gui/<uid>`, with one retry. Last, it confirms with `/sound/ping`
    within 2 s, re-sent every 250 ms.
  - **On a real host, `on` is the deploy.**
  - `off [--dry-run] [--purge]` boots the agent out and removes the plist.
    The binaries stay unless `--purge`.
  - `status [--json]` reports the binaries and the hash each was built
    from, whether the plist is present, whether the job is loaded, its pid,
    and the engine's pong.
  - Exit codes: 0 done; 1 tried and failed (bootstrap refused, or no pong);
    2 refused (no swiftc, the build failed, the port is held) or a usage
    error.
  - `ThrottleInterval` is 30, not 10. The
    engine exits 3 when it cannot start the output within 10 s, so on a
    host with no output device KeepAlive would otherwise respawn it every
    ~10 s.
  - Tests never run launchctl: `SESSIONTOOLS_SOUND_NO_LAUNCHCTL`, set by
    `tests/conftest.py`, makes the real `_launchctl` raise, and the tests
    use a double.
  - The wheel ships the `.swift` sources, and a test pins that.
- **Operator and privileged seats from one setting** (ST-130). New
  `operator.privileged` (a list of seat names or addresses) beside
  ST-116's `operator.address` / `operator.mailbox` in
  `~/.sessionkit/tools/settings/session.json`. One public reader,
  `sessiontools.read_operator() -> Operator(address, mailbox, privileged)`
  with `is_privileged(who)`, and its CLI twin `session operator`, which
  prints `{"operator", "mailbox", "privileged"}` as one line of JSON. The
  operator is always privileged. `AGENT_OPERATOR_ADDR` moves the address but
  grants no privilege. Nothing configured, or a bad file, means nothing
  privileged. The Fable gate's `operator_ask_address` now reads through it.
- **`idle with children`: a seat whose turn is over while a background agent
  runs no longer reads busy** (ST-98). `Session.state` is `busy`, `idle with
  children` or `idle` (exported as `BUSY`, `IDLE_WITH_CHILDREN`, `IDLE`,
  `STATES`), with `Session.turn_ended_at`. Claude Code keeps its sidecar
  `busy` while a background agent lives; a `turn-ended` mark in the session
  dir (contract v1, `sessiontools.turn`) says the turn is over. A busy
  sidecar reads `idle with children`, idle since the mark's `at`, only when
  the mark is a valid v1 mark, no older than the sidecar's `updatedAt`, and
  the sidecar's process is live; with no mark every reading is exactly as
  before. `session list` gains a STATE column and a `state` JSON key (STATUS
  and `status` keep the harness's word) and counts IDLE from the turn's end;
  `session get state` is new and `session get idle` counts from the turn's
  end. The `session list` snapshot is held to the marks on disk, so the
  state shows as soon as the hook writes (snapshot schema 2). The writer
  rides `hooks/busy-sound`: Stop (now `busy-sound stop`) sets the mark,
  PreToolUse, UserPromptSubmit and SessionEnd clear it, a subagent's event
  (`agent_id`) changes nothing. **DEFAULT OFF**: nothing is written unless
  `SESSIONTOOLS_HOOK_TURN=on`; switching it on is a separate step.
- **`session list` computes once; concurrent callers share one snapshot**
  (ST-111). The result is kept in `$SESSION_STATE_DIR/list/snapshot.json`
  (else `~/.session/state/`), written atomically, and served while it is
  younger than 2 s (`SESSION_LIST_MAX_AGE`, or `list.max_age` in the
  `session` settings; `0` is off). Callers that arrive together block on one
  flock and read the one computation; a holder that dies drops the lock, and
  a wait is given up after 15 s. `--fresh` (or `SESSION_LIST_FRESH=1`)
  computes now and refreshes the snapshot; `session get`, `session context`
  and `session start`'s front door never read it (tested). A missing,
  corrupt, stale, other-schema, other-version or other-environment snapshot
  is recomputed. Output is unchanged. On one busy Mac (42 seats, 3 live
  servers, 1034 dead socket files) a served run forks nothing.
- **A context generator's exit can be declared information, not failure**
  (ST-114, from SM-706). A `start: context:` entry may be
  `{run: CMD, informational-exit: [N, ...]}`. When CMD exits with a listed
  code, its block is headed ``CONTEXT GENERATOR NOTE: `CMD` exited N[ --
  LAST STDERR LINE]. This agent file declares exit N informational for this
  generator, so this is its answer, not a failure.``, with its stdout in
  full and no stderr "failed" line. Example: `{run: pager status,
  informational-exit: [3]}`, the pager's documented quiet. Undeclared
  codes, timeouts and broken built-ins keep the FAILED block byte for byte,
  and string entries render exactly as before. Codes are integers from 1 to
  255; `0`, a bool, a non-list or an unknown key is refused, exit 2.
- **`make lint` fails when uv.lock disagrees with pyproject.toml** (ST-122,
  from ST-121). A new `make lock-check` target, run first by `make lint`,
  runs `uv lock --check --offline`. A version or dependency change made
  without a re-lock now fails lint, naming uv.lock and the fix
  (`uv lock`, then commit uv.lock), instead of letting every `uv run`
  rewrite uv.lock in each checkout. The check needs no network and never
  writes the lockfile. `tests/test_lock_check.py` runs it on bumped copies.

### Changed

- **The launchd labels are `dev.sessionkit.tools.<job>`** (ST-213): the
  sound engine is `dev.sessionkit.tools.sound-engine` and the pane-state
  publisher `dev.sessionkit.tools.pane-state`, the kit's one scheme. Each
  is still one constant; the plist's name, its `Label`, the launchctl
  target and the help derive from it. **Upgrading:** nothing to do by
  hand. The next `session sound engine on` or `session pane-state agent
  on` boots out the job under the old label and removes its plist by
  itself, and `off` removes either form. `status` names an old job or
  plist still there (`legacy` in `--json`), and `--dry-run` lists the
  retire and changes nothing.
- **`session account` keeps its files in `~/.sessionkit/tools/accounts/`**
  (ST-212), beside the package's `settings/` and not inside it: the
  registry, `account-config.json`, `host-ledger.jsonl` and the backups.
  A missing folder is made 0700, and the files are 0600. The old
  `~/.config/sessiontools/` is not read. While it holds files and the new
  folder does not exist, `list` and `status` say so once on stderr,
  naming both folders and the move command. `add`, `new`, `use` and
  `remove` refuse, exit 5. `python -m sessiontools.account.movehome` moves
  the folder: a dry run unless `--apply`, sha256-verified, refused when
  both folders exist, and it never prints a token. The leakscan's default
  `--registry` follows. See `docs/account.md`.
- **Settings live in `~/.sessionkit/<package>/settings/`, one file per
  command.** This package's are in `~/.sessionkit/tools/settings/`
  (`session.json`, `claude-code.json`, `sounds.json` and their `.d/`
  folders). The old `~/.settings/<command>/` folders are not read; while
  a command's files are only there, one stderr line names both folders.
  A `--settings` typed on the `session start` command line is folded in
  as the top layer of the computed one, so a key a lower file locks
  with `_locked` still holds; one that cannot be read, or two, refuse
  the launch before a session is made. See `docs/settings.md`.
- **Supported Python is 3.11 to 3.14.** `requires-python` is now
  `>=3.11,<3.15`.
- **Help text and the dry-run launch header no longer cite internal
  references** (ST-189). Wording only; flags, exit codes and behaviour are
  unchanged. The header line is now `# launch record: ...`.
- **`session account status` shows each usage window's reset time by
  default** (ST-168). The `5h` and `7d` lines read `(resets TIME)`, and
  `--json` carries `resets_at`; `--no-resets` hides them. A window with no
  reset time shows no reset clause and a null `resets_at`; a time is never
  guessed.
- **The status line lives in the sessionkit `claude-code` layer, never
  `~/.claude`** (ST-165).
  `session statusline install` now copies the script to
  `~/.sessionkit/tools/settings/statusline.sh` and sets `statusLine`
  in a drop-in it owns, `claude-code.d/50-statusline.json`. Nothing under
  `~/.claude` or `$CLAUDE_CONFIG_DIR` is opened for writing. `session
  start` renders `statusLine` into its `--settings` file beside `model`
  and `effortLevel`, checked and merged key by key like every map. Install
  reads the whole kit layer: it refuses (exit 3, `--force` included)
  while the `claude-code` settings have not been moved there, and names
  the move. A `statusLine` of yours read after the drop-in is refused;
  one read before it is overridden under `--force` and never touched. It
  also refuses,
  `--force` included, when the kit dir, the drop-in or the script resolves
  inside `~/.claude` by a symlink, and when a `_locked` path read before
  the drop-in would keep ours from applying. `check` reads the
  same layer. Both verbs report what `~/.claude` holds, read only. A bare
  `claude` does not show the kit's line: `alias claude='session start -'`.
  The shipped script's header no longer names `~/.claude`, so its stamp
  moves: an existing pristine kit copy reads as U and `install` replaces
  it. See `docs/status-line-script.md`.

- **`session account use` shows the plan first.** The plan, with its cost
  or restart report, now comes before every gate: a missing `--witness`
  is exit 2 after the plan, no `--yes` is exit 3. The preflight and the
  backup run only after both gates, so a plan-only run makes no `claude`
  call and leaves no backup. The usage line shows `--witness WHO`.
- **`use`'s preflight child gets an allowlisted environment.** It used to
  get everything but five names, so another credential or provider
  variable Claude Code honours could answer the preflight for a bad
  token. It now gets the same short allowlist as `new`'s, plus
  `CLAUDE_CONFIG_DIR` and the token under test.
- **`session list` forks no git** (ST-111). `repo_name` finds
  the repo in-process (a `.git` dir with a HEAD, or a `gitdir:` file whose
  target exists), as `git rev-parse --show-toplevel` would. An empty cwd now
  gives `""`, not the lister's own repo.
- **Dead tmux socket files cost no fork** (ST-111, ST-82).
  `tmux.sockets()` knocks on each socket in-process and skips the ones that
  refuse. One `session list` now forks tmux once per LIVE server and `ps`
  once.
- **`hooks/busy-sound` forks no `dirname`** (ST-132). It found
  python's `src/` with `$(dirname "$0")` on every hook call, before any fast
  path: about 3 ms each. It
  now uses builtins, with the same result (a bare `$0` still gives `.`). A
  test runs the engine's fast path and the stat fast paths with every
  command on PATH replaced by a tripwire, and none may fire.

### Fixed

- **The docs link this repo relatively, and other repos on `HEAD`**
  (ST-215). The README and the configuration and settings pages linked
  their own files by full `/blob/main/` URLs, which open a browser from a
  checkout and would not resolve on a repo whose one branch is `master`.
  A link to a file in this repo is now relative to its page. A link to
  another sessionkit repo is a full URL on `/blob/HEAD/`, which GitHub
  resolves to the default branch, and its text names the package and path
  (sessionkit's docs/settings.md). `tests/test_doc_links_relative.py` (it
  replaces `test_readme_links_absolute.py`) checks that every link in the
  README and in docs/ into this repo is relative and that its path and
  anchor exist. `tests/test_shipped_links.py` fails if a shipped file links
  a sessionkit repo by a branch name (only `HEAD` or a `v` tag), links a
  file in this repo by its full URL, or passes `-b main` / `--branch main`
  for a sessionkit repo.
- **The settings reader refuses a folder it cannot search, on every
  Python** (ST-210). From CPython 3.14, `Path.is_file()` and `is_dir()`
  return False for a path they cannot look at; up to 3.13 they raised. A
  settings folder that lists but cannot be searched (mode `r--`) therefore
  read as one with no file, and a value set there fell to its default
  without a word. That held for a host file, a `.d` entry, a template, and
  the agent, seat and session layers. Every check in the reader now stats
  the path itself: a path that is not there is absent, and one it cannot
  look at raises `SettingsDenied`, naming the file. `SettingsDenied` is a
  `SettingsError` and also the `PermissionError` that 3.13 raised, so
  every caller's catch still catches it. `session settings` now refuses,
  naming the file, with exit 2 where 3.13 printed a traceback, and
  `session statusline check` and `install` refuse with exit 5. Readable
  and absent files read as before.
- **The settle alert runs on the `session` command's python** (ST-208).
  `hooks/settle-hook` ran `python3` from `PATH` and exited 0 below 3.11.
  `uv tool install` keeps its python off `PATH`, and a clean Mac's
  `python3` is Apple's 3.9.6, so after the documented install the alert
  never fired and nothing said why. It now picks its python as the busy
  sound does: `$SESSIONTOOLS_PYTHON`, else the bare-path python in the
  shebang of `command -v session`, else `python3`. Both hooks source one
  picker, `hooks/pick-python.sh`. The busy sound behaves as before, and
  its fast paths still read no extra file.
- **The launch record's `start:` names the venv's interpreter on a macOS
  framework Python** (ST-207). A framework build (Homebrew's, for one)
  re-execs its `Python.app` binary, and `sys.orig_argv[0]` names that
  binary, which has none of the venv's packages: the recorded command
  failed with `ModuleNotFoundError` when re-run. The record now names
  `sys.executable`, then the rest of `sys.orig_argv`, flags included, so
  it re-runs as written. A `python -m` start typed with a bare `python`
  now records the absolute interpreter path too.
- **`session settings` reports each leaf under its own path when two
  dotted keys join alike** (ST-152). A map key may hold a dot (a Claude
  Code permission rule such as `Read(~/.zshrc)`), and the agent's dotted
  front matter shorthand splits at every dot. The report keyed each leaf
  by its joined path, so a host rule and an agent's split copy of it
  shared one row, and the report could print the agent's value and file
  while the host's value resolved. Now:
  - The report, `--json` and `Resolved.origins` / `Resolved.leaves`
    spell a path segment that holds a `.` or a `"`, or is empty, as a
    JSON string: `permissions.rules."Read(~/.zshrc)"`,
    `subscriptions."super.sessionmail"`. No two paths share a spelling.
    Keys without a dot are spelled as before.
  - Two surviving leaves whose paths join alike get a NOTE naming both
    paths and both files. When the agent layer is one side, it ends with
    the nested spelling that keeps the key whole.
  - `_locked` still matches the joined path, so a lock covers both
    spellings. `merge()` still returns joined keys, unchanged.
- **An agent key that the dotted shorthand cuts inside brackets or quotes
  gets a NOTE** (ST-173). Front matter
  `claude-code.permissions.rules.Read(~/.zshrc): deny` splits at the dot in
  the rule, so the rule `Read(~/.zshrc)` is not what is set. With no other
  key to collide with, the report printed the split row with no NOTE, and
  the row reads like the rule. Now a NOTE names the key, its file and the
  pieces it was cut into, and ends with the nested spelling that keeps the
  key whole: `claude-code.permissions.rules: {"Read(~/.zshrc)": ...}`.
  It shows in the report, in `--json`'s `notes` and on `session start`'s
  stderr. For a tool that is not moved (no host, seat or session file),
  the report prints it under the `not moved` line. It is never a refusal.
  A key copied from the report's own quoted spelling
  (`rules."Read(~/.zshrc)"`) is cut the same way and gets the same hint,
  not the same NOTE: the pieces the NOTE names differ. Only agent front
  matter is checked, since a JSON key is whole as written. Ordinary
  shorthand such as `claude-code.model` gets no NOTE.
- **A tool's schema is found under its dot name and beside its settings
  file** (ST-147). The settings reader looked only at `<tool>.schema.json`
  in the tool's own settings folder. sessionvoice's installer
  writes the mouth's and the ears' schemas as `.<tool>.schema.json`,
  beside the file `$VOICEMODE_MOUTH_SETTINGS` or `$EARS_CONF` names when
  set, so `session settings` never checked them and the session's birth
  never read the variables they name. Now one lookup, `find_schema`,
  serves listing, checking and birth. It tries, in order: the dot name,
  then the visible name, first in the directory of the tool's settings
  file (`SETTINGS_FILE_ENV`, which names only those two tools), then in
  the tool's folder, `~/.sessionkit/<package>/settings/`. The first file
  found is read, and no two are merged. Each other file found is
  reported: as a `NOTE` in
  `session settings`, and as a `schema-shadowed` line in the session's
  `log/settings.jsonl` at birth.
- **`session settings` reads the ears' and the mouth's host values from the
  file they actually read** (ST-174). When `$EARS_CONF` or
  `$VOICEMODE_MOUTH_SETTINGS` pointed elsewhere, the schema was found
  beside that file, but the host layer's values still came from the
  tool's default settings folder, which the tool never read. Now, with the
  variable set and not empty, the host layer is the file it names (any
  file name), then `<tool>.d/*.json` beside it, and the template is
  `<tool>.template.json` beside it. The tool's default folder,
  `~/.sessionkit/<package>/settings/`, is not read for that tool. A `NOTE`
  names the default folder when it still holds files, and another says
  so when the named file does not exist. `session
  settings` lists the tool when only the named file exists. A tool that is
  not moved now prints its NOTE lines under its `not moved` line; before,
  that report was one line. The schema lookup is unchanged.
- **A launcher's credentials no longer reach the sessions it starts**
  (ST-143; SK-80's one SHOULD-FIX). `host_environ()` handed every child
  the launcher's whole environment, so a seat started holding
  `CLAUDE_CODE_OAUTH_TOKEN` passed it to the in-place exec, the context
  generators, and any tmux server `session start` birthed, which kept it
  for every pane it ever made. Now:
  - `host_environ()` drops `CREDENTIAL_VARS` (`CLAUDE_CODE_OAUTH_TOKEN`,
    `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`), so no child and no
    server birth holds one.
  - The launch script's first line unsets the same names beside the
    identity names, for a pane on a server something else birthed.
  - A session that needs its launcher's value says so:
    `environment: inherit: [NAME, ...]` (any valid name). On a live
    launch the value rides the 0600 secret file, sourced last and
    removed: never argv, never the tmux server. The banner names each
    name (`carried`, or `not set -- not carried` on stderr; not an
    abort), the launch record masks it by name, and a dry run names it
    and reads nothing. A name under both `inherit:` and `variables:` or
    `secrets:` is refused.
  - `tm kick` building its child's environment is taskmaster's half and
    not changed here; the first-line unset limits it meanwhile.
- **A `player.pid` that is not a regular file never hangs the busy-sound
  hook** (ST-135, from ST-132). `busy._pid_live` read
  the mouth's `player.pid` with `read_text()`, a blocking open: a FIFO there
  with no writer hung the seat's hook, and its turn with it, and a directory
  raised an error that was read as "unreadable", which held the seat. It now
  opens with `O_RDONLY | O_NONBLOCK`, `fstat`s the descriptor and reads from
  it. A FIFO or a directory, reached directly or through a link, is not
  live. A link to a regular pid file is followed, as the engine's
  `pidFileLive` does (engine parity). A regular
  `player.pid` reads exactly as before: missing, empty or stale is not live,
  unreadable fails toward silence.
- **`session context` finds the transcript from a cwd over 200 characters**
  (ST-125). Claude Code cuts a long project directory name to 200
  characters and appends `-<hash>`. The hash is Java's `String.hashCode`
  over the raw cwd, taken as an absolute value and written in base 36. It
  reproduces the one capped entry on one Mac exactly. `window.encode_dir` now
  produces that name. If no directory has it (another build may use a
  different hash), the lookup falls back to any directory named
  `<first 200 characters>-*`. Shorter cwds encode exactly as before.
- **`session context` finds the transcript from a cwd holding a space, `+`,
  `@` or any other character outside `[a-zA-Z0-9]`** (ST-126). Claude Code
  turns every such character into `-`, not only `/`, `.` and `_`, and it
  counts UTF-16 units, so an emoji becomes two dashes. Measured on one Mac
  with claude 2.1.296: `st126 a+b@c~d=e,f` became `st126-a-b-c-d-e-f`.
  `window.encode_dir` now does the same. A cwd made only of letters, digits,
  `/`, `.`, `_` and `-` encodes exactly as before.
- **Two user-visible texts no longer carry internal notes** (ST-164).
  `session reap`, keeping a hatched task, now gives the reason
  `(not done; hatched does not count as done yet)`; it used to point at
  an internal open question. `session statusline`, naming the first known
  earlier hand copy of the status line, now calls it
  `(an older revision of this script)`; it used to name internal hosts and
  a date. Nothing else in either output changed.

## [0.1.0] - 2026-10-10

The session-settings work that landed between `v0.0.4` and `v0.1.0` (Sat 3
to Sun 4 Aug) had no entry here. ST-138 (2030-08-14) added it from `git log
v0.0.4..v0.1.0`. From the same log it added the licence, the scrub to
neutral names and `make scan`. Those entries are marked *(from git log,
ST-138)*. The commits that only touched `openspec/` (the paper trail, not
the package) have none, and neither does a lint-only commit.

### Added

- **`busy.trigger`, the busy sound's ONE mode** (ST-90). `batch` (unset is
  `batch`, so ST-66 is unchanged), `landed` or `finished`. Any other value
  is silent in every mode. The batch trigger's arm is OFF unless the mode is
  `batch`, so a `landed` or `finished` seat stamps `sounds/busy/off` and its
  shim starts no python. Outside `batch`, `delay` is optional and defaults
  to 0. The schema (`settings/sounds.schema.json`) gains the enum.
- **The busy-sound hook runs on the `session` tool's python** (ST-90).
  The shim picks `$SESSIONTOOLS_PYTHON`, else the bare-path python
  in the shebang of `command -v session`, else `python3`, and only when
  python has to start, so the off path is unchanged. That venv has PyYAML,
  so the hook now reads the agent layer (`settings:` in the agent's front
  matter) with the same reader as `session settings`.
- **The busy sound never plays over the mouth** (ST-90). The waiter
  counts its delay only while the mouth is silent, starts nothing while it
  speaks, and cuts a playing sound within 0.1 s of the mouth starting (the
  slot stays armed: no replay). Any session's speech counts. The mouth is
  speaking while `<mouth dir>/playing.json` exists and `player.pid` names a
  live process, so a killed player's leftover file does not silence it.
  The mouth dir follows voicemode's rule. `playing.json` is not yet a
  documented voicemode interface, and is read only through
  `busy.mouth_speaking()`.
- **`session sound landed|given`, the busy sound's signals** (ST-90;
  `busy.signal(event, sid, ref)` in python). `landed` plays the sound on
  `<session dir>/sounds/unread/` at its delay (unset: 0), only when the
  session's `busy.trigger` resolves to `landed` and its seat is live; one
  sound per run of lines. `given` stops it. Exit 0 always, 2 for a usage
  error; `--json` prints the decision and why. `finished` is held.
- **The busy sound: a sound file plays once a tool batch has run past a
  delay, and stops the moment the batch returns** (ST-66). Plugin 0.0.5.
  - `hooks/busy-sound arm|end|clear`, a POSIX shim, is wired to PreToolUse,
    PostToolBatch (beside settle-hook), Stop, UserPromptSubmit and
    SessionEnd. Silent unless the `sounds` settings set `enabled` and
    `busy.{auto_arm,file,delay}`. `SESSIONTOOLS_HOOK_BUSY=off` switches it
    off.
  - `sessiontools.busy`:
    - `config()` reads the cascade.
    - `play(slot, file, delay)` and `stop(slot)` are the seam a second
      trigger (ST-90) reuses.
    - A detached waiter is its own process group, and `afplay` is its child.
    - The `arm`, `end` and `clear` hook verbs.
  - Cost on a seat without the setting: one python run per session, then
    `stat` calls (the `off` stamp, back-dated a second because `/bin/sh -nt`
    compares whole seconds).
  - `settings/sounds.schema.json`.
- **A session home, `SESSION_HOME`, from the `session` tool's settings**
  *(from git log, ST-138)*. There is no home by default. `home_dir: PATH` is
  the place and turns a home on. `home: true` puts it by the layer that set
  it: agent `agent/home/<agent>/`, seat `seats/<seat>/home/`, session, host
  or template `sessions/<sid>/home/`. `home` is the switch: a `home: false`
  in a lower file than the `home_dir` turns the home off, a lower `home_dir`
  turns it back on, and a lower `home: true` keeps the `home_dir` as the
  place. `home: false` and `home_dir` in one file are refused, naming the
  file. `session start` resolves it before the launch, exports
  `SESSION_HOME` and makes the dir after the pane mint. `HOME` is untouched.
  The dry run prints a `home:` line, and a layer file the reader refuses
  refuses the launch.
- **Settings from the environment, at birth** *(from git log, ST-138)*. A
  new session's script calls `settings.birth_main` once, after the prelude;
  a resume does not. For each installed
  `~/.settings/<tool>/<tool>.schema.json`, every variable a key names with
  `x-env` is read from the environment into the session's
  `settings/<tool>.d/50-env.json` (0600, in 0700 dirs). A file already there
  is kept. A variable no schema names is not read. The key's type says how
  the string is read: an object of booleans from a list split on commas and
  whitespace (any other object is refused), an array from a comma list, an
  integer or a number parsed, a boolean from `1`/`true`/`yes`/`on` or
  `0`/`false`/`no`/`off`, and a string as written. Each variable is logged
  by name, never by value, to the session's `log/settings.jsonl`:
  `env-at-birth`, `env-empty` (set but empty, not read), `env-refused` (a
  value it cannot read; the rest is still written) or `env-kept`. It always
  exits 0.
- **The agent's file under `agent/config/`** *(from git log, ST-138)*. An
  agent with extras is `agent/config/<name>/<name>.md` beside them, one with
  none the flat `agent/config/<name>.md`, and the folder wins. The
  launcher's agents folder is `$AGENTS_DIR`, else `~/.session/agent/config`
  once it exists, else `~/.session/agents`; both fallbacks read the same
  `HOME`.
- **`make scan` (and its other name, `make scrub`), the pre-publish scrub
  check** *(from git log, ST-138)*. It runs the release-scan tool that
  `$SESSIONKIT_SCAN` names over HEAD's tree and fails on any P0 term. The
  scanner and its word list are private and never live in this repo. With
  `SESSIONKIT_SCAN` unset it says so and exits 2.

### Changed

- **Licensed AGPL-3.0-or-later** (ST-102, SK-24) *(from git log, ST-138)*.
  `LICENSE` is the unmodified FSF text, and `pyproject.toml` declares
  `license = "AGPL-3.0-or-later"` with `license-files = ["LICENSE"]`, so the
  wheel and sdist carry both. `v0.0.4` and earlier shipped with no licence.
- **The busy-sound hook's `end` and `clear` stop every slot** (`busy`,
  `unread`, `finished`), in python and in the shim's `[ -e ]` fast path, so
  a signalled sound stops at the batch end, a prompt, Stop and SessionEnd.
  The off path still starts no python.
- **The agent layer is read from the file the launch used** *(from git log,
  ST-138)*. The birth records `agent_file`, resolved through links, beside
  the agent and seat in `settings/session.json`. The settings reader takes
  the agent layer from the launcher's file, else the recorded one, else
  `agent/config/`. A launch whose `AGENTS_DIR` names another folder (a
  plugin's own agents) now takes its agent settings from that file. A
  recorded file that is gone falls back to `agent/config/` and says so:
  `session settings` prints a `NOTE` line and `--json` carries `notes`. The
  exit code is unchanged.
- **Neutral example names** (ST-102, SK-24) *(from git log, ST-138)*. A
  docstring in `tmux.py`, a path in `tests/test_window.py` and the pass-path
  example in the `Makefile` now use neutral names. Text only: no default and
  no behaviour changed.

### Removed

- **The oracle, from the release tree** (ST-102, SK-24 scrub). The old
  launcher's copy (`tests/oracle/session-start`), the 93 tests marked
  `oracle`, the `oracle` marker, `-m 'not oracle'` in `addopts` and
  `make oracle` are gone; `--strict-markers` stays. The resume dry-run
  tests still run the port, under the same node ids. Of the default run,
  only the five tests whose subject was the oracle itself went with it;
  their one pin on the package (no old launcher ships in it) moved to
  `tests/test_cli.py`.

### Fixed

- **A session id of `.` or `..` names no session** (ST-90). Each
  would resolve to the sessions dir or its parent. `busy.is_sid` now
  refuses them in `session sound` (a usage error, exit 2), in the hook's
  `_sid()`, and in the busy-sound shim's `case` (exit 0, no python).
  A valid session id behaves as before.
- **`session settings` reads the session dir the launcher made** *(from git
  log, ST-138)*. `settings.sessions_root` now honours `$SESSIONS_DIR`, and
  the launcher's root is the same function, so a setup whose `SESSIONS_DIR`
  moves its sessions no longer has its reader and its launcher naming
  different dirs.
- `tests/test_start_plan.py` expects the launcher's door files under
  `launch/` *(from git log, ST-138)*.
  `v0.0.4` was tagged with those three tests red; the code was right.

## [0.0.4] - 2026-10-03

Tag `v0.0.4` was cut without a section here: its changelog still had
everything under `[Unreleased]`, and `0.1.0` took it all. This section was
put back by ST-138 (2030-08-14). Its body is that `[Unreleased]` block as
`git show v0.0.4:CHANGELOG.md` has it, with one later lint fix. The entries
marked *(from git log, ST-138)* were added from `git log v0.0.3..v0.0.4`,
for commits that had no entry anywhere, and one note is marked *(note:
ST-138)*.

### Added

- **`session start` writes a launch record into the session dir** (ST-18).
  - `<session dir>/launch/` (0700) holds `env` (the full environment,
    sorted, with secret values masked to their length), `command` (the
    vendor argv, the `session start` command line, the cwd) and
    `startup-files` (`TAG<TAB>PATH`: agent file, doors, sourced env files,
    the CLAUDE.md chain, settings, MCP). All files are 0600.
  - The launch script writes it on the line before the vendor starts, so
    it records the env the vendor actually inherits. A keyed tmux launch
    writes it into the dir its pane mints.
  - A `.gitignore` of `*` goes in first. `git check-ignore` must then
    agree, or no repo may enclose the dir, before any other byte is
    written. A sessions root can sit inside a repo that does not ignore
    session dirs, and this guard covers it.
  - The record is written once, never read back and never fatal: a
    failure prints one stderr line and the vendor still starts. A resume
    writes none. The dry-run report names the record's dir.
  - Masking (v1): names with TOKEN, KEY, SECRET, PASSWORD, PASSWD, AUTH or
    CREDENTIAL, the word PAT, any `AWS_` name, and every declared secret.
    A long, high-entropy value is masked as a backstop. Identity names
    stay clear.

- **The settle alert: a PostToolBatch hook that is silent unless the
  session sets its threshold** (ST-10). It covers any session that sets a
  limit, not seats only.
  - `hooks/hooks.json` wires `hooks/settle-hook`, a POSIX shim, to run
    `sessiontools.settle`. It says one line per tool-calling turn once
    `used >= SESSION_SETTLE_AT`.
  - `SESSION_SETTLE_SAY` overrides the line, with `{at}` and `{used}`
    filled in. `SESSIONTOOLS_HOOK_SETTLE=off` switches it off with the
    plugin still loaded.
  - With no threshold it is silent and never starts python. Nothing in it
    checks who the session is. A subagent's batch is silent. Every failure
    is silent, exit 0.
  - `tests/test_settle.py` runs the alert's acceptance checks through the
    real shim.
  - Inert until the next release updates the plugin cache. No version bump
    here.

- **The `session` skill, shipped as a Claude Code plugin**.
  - `.claude-plugin/` holds a self-hosting `sessiontools` marketplace.
  - `skills/session/SKILL.md` teaches `list`, `get`, `context` and
    `start`, within the 40-line, 400-token fold. It says what is not here
    (mail, the disabled `send`, the retired `session mail`).
  - `tests/test_plugin.py` pins the skill to `cli.VERBS` and `get.PROPS`,
    and pins the plugin's version to the package's.
- `session start`, ported (`sessiontools.start`). Direct launches and tm's
  spawns - the parser and its position trap, agent resolution, front matter,
  the vendor render rule and door guard, the declared environment, context
  generators, the 0600 prompt and first-message files, the agent plugin dir,
  the uuid and keyed mint, `{shortsid}`, `SESSIONS_DIR`, the Fable gate,
  GIT-24, the dry-run report - byte-equal to the old launcher's dry runs.
  Library first: `prepare(args)` -> `LaunchPlan` -> `execute(plan)`.
- `session start --in-tmux` / `--tmux-target SESSION[:WINDOW]`, ported
  (`sessiontools.start.placement`): a detached window running the
  plan's own script as a 0700 trampoline; every target `=`-anchored
  (TM-1658); the client switches only for a human at a terminal in tmux
  (TM-2414); a keyed launch mints inside the pane, reaped with the pane
  (CA-152); `new-window` is the handover. The tmux dry run is byte-equal
  to the old launcher's golden. Five smaller fixes ship with it:
  - The trampoline path reaches tmux shell-quoted, so a space or `$` in
    AGENTS_DIR or TMPDIR no longer breaks the launch. The old launcher
    had this flaw as well.
  - A keyed tmux dry run says `{shortsid}` is "filled in the pane from
    the minted id", as the old launcher did.
  - The Fable-gate tests stub placement as well as exec.
  - The test wall kills its private tmux servers by socket, with every
    TMUX* variable stripped. A socket counts only if the server behind it,
    asked for its own `#{socket_path}`, names a path inside the wall. So a
    symlink or a hard link to another server's socket is left alone, and
    our own server is killed even when something outside links to it. An
    earlier version checked the file (inside the wall, one link), which a
    symlink to an in-wall hard link got past. A file the server cannot be
    asked about stops nothing. The wall's own tests start servers where
    tmux really puts them, `<dir>/tmux-<uid>/<name>`.
  - The keyed pane test gives the real HOME to the real key-init primitive
    alone, inside its wrapper. The private tmux server keeps the test's
    HOME, so it reads no user `~/.tmux.conf`, and the test checks that.
  - The wall unsets SESSIONS_DIR and points SESSION_KEY_INIT at a path
    that does not exist. That path now reaches in-process tests,
    `plan_for` and `clean_env`'s subprocesses, so a keyed test that
    forgets its stub refuses. Before, `clean_env` dropped it and a
    subprocess fell back to the real primitive, which records births in
    the agent registry. An environment a test builds from nothing still
    reaches the real one.
- `session start ... --resume <id>` / `-r` / `--continue` / `-c`, ported
  (`sessiontools.start.resume`). The resume runs the port, no
  longer the bridge. The dry run is byte-equal to the old launcher's on
  21 inputs, stdout and stderr, checked against the bundled bridge. Two
  of them are what `tm session resume` sends (`- --resume ID
  --name X`), and a Fable resume under `AGENT_ALLOW_FABLE=1`.
  - CA-62: the session's recorded cwd wins, read from the transcript
    under the launch's own HOME.
  - The restart message carries the date, then the voice signal, then the
    user's words.
  - A keyed resume forks into a fresh keyed identity (`--fork-session`,
    added once). An unkeyed resume of a keyed session says it is resuming
    UNKEYED.
  - Refused: a resume under tmux, in the old launcher's words, and a keyed
    bare `--continue`.
  - A resume reads neither `--directory` nor `start: directory:`, as
    before.
  - Three differences from the old launcher, each a refusal:
    - `--key-label` on an unkeyed resume is refused, where the old one
      ignored it.
    - A door opened twice on a keyed resume (`--session-id` after the
      agent name) is refused with exit 2, before the fork is minted. The
      old launcher printed its FORK line, then died in a traceback, exit 1.
    - An agent that will not resolve is refused by the port, where the old
      launcher refused it itself. The words and the exit code are the same.

    A codex agent is refused whatever its flags, as before; the old
    launcher ran its `-c`.
- A live launch of an agent that declares `environment: secrets:`,
  ported (`sessiontools.start.secrets`), fresh or resumed:
  - Each secret is resolved through `pass` (120 s) and reported by name,
    never by value. `pass` says it is waiting for a PIN or a key touch
    before it waits. A "Card error" gets one scdaemon restart and one
    retry, never a loop.
  - Any failure aborts the launch and writes nothing.
  - The values reach the vendor in a 0600 file, written first and
    refused unless it is 0600. The launch script sources it last and
    removes it before the vendor runs; the shred trap is the backstop.
    A value never appears in argv, the script, stdout or stderr.
  - Anything that fails between writing the file and the exec takes the
    file with it: a failed exec, a placement that fails before the
    handover, and an OS error while naming the
    window, which the old launcher left on disk. An exec that returns
    (only a stand-in can) is a failure too. Naming the window is
    best-effort against an OS refusal, on both of its tmux calls.
  - A `pass` that will not run (not executable) is a failure reported by
    name, like one that is missing. It used to crash with an
    AttributeError.
  - Two changes from the old launcher: a result's repr never shows its
    value, and neither does a planned file's (path and size only). The
    file is created 0600 and is 0600 at its first byte; a test pins
    both.
  - The test wall puts refusing `pass` and `gpgconf` stubs first on PATH
    for every test, so no test can raise a PIN dialog or kill the host's
    gpg-agent. A test that builds its own PATH (the tmux e2e) takes the
    wall from `start_support.walled_bin()`, and `private_env` refuses to
    start tmux with a PATH that finds the host's `pass` or `gpgconf`,
    compared by real path.
- One stderr line per context generator (`session start: context: running
  `...``), so a slow dry run never looks hung.
- `session list` CONTEXT, ported (`sessiontools.window`): the table's last
  column and the five `context_*` JSON keys sessionview's sidebar reads,
  from `session-context`'s fold and window map (SESH-377): 0 differences
  from the old `scan()` over all 4,211 transcripts on one Mac.
  `--no-context` skips the transcript read; `get`, `send` and `--pane` never
  pay it.
- `session context [SEAT] [--peak] [--max N] [--json]`, ported: the old
  `session-context` report, same text and JSON keys (plus `model`). The
  mail rules' R06 ("run `session context`") works again. BEHAVIOUR CHANGES
  from the old CLI (also in `--help`):
  - the window is the model's, never a guessed 1M: unknown or exceeded
    prints `?` (saying which) and the JSON has `max`/`pct`/`free` null;
    `$CONTEXT_MAX` still tightens a known window but is no longer `--max`'s
    default; `--max` must be positive (else exit 2).
  - a transcript with no usage rows is exit 1, not `0 / 1.00M (0%)`.
  - a `<synthetic>` zero reading on a seat that reached its peak is `?`, as
    in `session list`; `--peak` shows the peak.
  - `--dir` is gone: with no SEAT and no session in the environment, the
    newest transcript of the current directory's project is read.
  - SEAT: a live seat's id prefix, then the `session get` name rule, then
    any session id prefix among the transcripts.
- `session get model`: the transcript's last real model id.
- **`session settings [SID] [TOOL]`, the settings cascade reader** *(from
  git log, ST-138)*. `sessiontools.settings` merges five layers: template,
  host, agent (`settings:` in the agent's front matter), seat and session.
  In each layer it reads `<tool>.json`, then `<tool>.d/*.json`. A map merges
  key by key and any other value replaces the one above it. `_locked` names
  dotted paths, matched at dot boundaries. Each key is shown with the file
  that set it, a jsonschema check names the failing file, and a tool whose
  files have not moved reads "not moved". `--agent`, `--seat`, `--json`.
  `mail` lists `subscriptions` per layer, never merged. The new runtime
  dependency jsonschema does the schema check, and no config library is
  taken. Plugin version 0.0.4.
- `settings.ensure_private_dir(path)`: a 0700 dir with a `.gitignore` of
  `*`, for state that must never reach a repo *(from git log, ST-138)*.
- **The front door: `session start` refuses a second live holder of an
  agent** (ST-46, SESH-497) *(from git log, ST-138)*. A hand launch (no
  `-n`/`--name`) of an agent that a live Claude Code session already holds
  prints one stderr line naming the newest holder and exits 3 (`EXIT_HELD`,
  new). Nothing is minted, written or placed. `--allow-second-holder`
  launches anyway, with one warning line, and `--dry-run` reports the
  holder and exits 0. A holder in the launcher's own pane (`$TMUX_PANE`) is
  replaced, not doubled (ST-46).
- `session start --seat SEAT`: the seat a spawner names (project+role), for
  the settings reader's seat layer. The birth writes
  `settings/session.json` with the agent and the seat *(from git log,
  ST-138)*.

### Changed

- `sessiontools.context` is `sessiontools.window` (`Context` is `Fullness`):
  `sessiontools.start.context` is the launch's prompt, a different thing.
- One tmux runner, `sessiontools.tmux.run(args, socket=None)`; `start`
  uses it.
- One runtime dependency: PyYAML, imported only by `sessiontools.start`.
  Written on 09-29;
  by the tag, `session settings` had added jsonschema, so 0.0.4 shipped
  two (see Added) *(note: ST-138)*.
- The default agents folder is `~/.session/agents` (was `~/.claude/agents`);
  `AGENTS_DIR` still overrides.
- The Fable refusal asks through `message send`, not `session mail send`.
- A malformed agent file is a usage refusal (exit 2) naming the file, not
  a traceback.
- The converse signal states a fact, not an order: `Voice: AVAILABLE --
  this session can speak`, and whether to speak first is the seat's
  letter's call. It said "speak your
  first response aloud. Do NOT type it.", which overrode a letter that
  said stay silent. The ears/mouth routing clause is kept.
- Every session dir is under `$SESSIONS_DIR`, else
  `~/.session/sessions/`, whatever the agent. It was `<agents dir>/sessions`
  or `<agent>/home/sessions` *(from git log, ST-138)*.
- The launcher's prompt file, first message, agent plugin and secret file
  go under the session's `launch/` (0700, `.gitignore` of `*`), beside the
  launch record. This holds for new births only *(from git log, ST-138)*.
- GIT-24: `session start` keeps an AI committer only when `TM_COMMIT_AS` is
  exactly `seat` and the work dir is under `TM_COMMIT_AS_ROOT`. Anything
  else gets the human committer. The `ai-committer-allowed` whitelist is
  gone (ST-62) *(from git log, ST-138)*.
- `--tmux-target` help says the window is named after the agent (ST-46,
  SESH-1114) *(from git log, ST-138)*.

### Removed

- `--identity-from-env` and `--window-name`. tm's
  `-n <seat>+{shortsid}` still names seats.
- The road to the bundled bridge. Every launch path is ported, so nothing
  hands an invocation to the old launcher any more, and no stderr line says
  "using the bridge". Gone: `bridge()`, `bridge_environ`,
  `refuse_before_bridging`, `bridge_unless_refused`, `NotPorted`,
  `args.not_ported` and `agentfile.refuse_unshipped_vendor`. The refusals
  that road carried now hold on the port's own roads (direct, tmux,
  resume, declared secrets), each in the port's words:
  - a vendor the port does not ship
  - a file it cannot parse
  - an agent it cannot find
  - the Fable gate, which asks through `message send`. The bridge's own
    copy of the gate asked through the retired `session mail send`, a
    dead end.
- The old launcher's FILE, from the package. The wheel no longer
  carries `sessiontools/bridge/`, and `sessiontools.start.SCRIPT` is gone.
  - The file moved, byte for byte and still sha256-pinned, to
    `tests/oracle/session-start`.
  - The 93 tests that run it are marked `oracle`. `addopts` deselects
    them, so `make check` and plain `pytest` leave them out, and the new
    `make oracle` runs them.
  - The resume dry-run tests still check the port in the default run; only
    their byte-equal comparison is manual.
  - An unmarked test that calls the oracle fails, and `--strict-markers`
    makes a misspelt mark an error.
  - Measured: the default run went from 81.7 s to 78.7 s. The oracle tests
    take 3.4 s.

## [0.0.3] - 2026-09-23

### Added

- One session-id resolver, the one place a tool gets this session's id
  (ST-6). `SESSION_ID_VARS`
  (`CLAUDE_CODE_SESSION_ID`, then `CLAUDE_SESSION_ID`); `session_id()`,
  the first non-blank of them, stripped, from the environment only and
  never the disk, else `None`; and `current()`, which is
  `find(session_id())`: this session's `Session`, or `None`.

## [0.0.2] - 2026-09-17

### Fixed

- `requires-python` is `>=3.11`, matching sessionmail, the first consumer;
  0.0.1 declared `>=3.13` (the `new package` default) and made sessionmail's
  `uv sync` unsatisfiable for the 3.11/3.12 splits it allows. Suite measured
  green on 3.11, 3.12 and 3.13.

## [0.0.1] - 2026-09-17

### Added

- sessiontools, born Thu 2030-07-18 with `new repo` and `new
  package`.
- The Claude Code driver: `sessions()`, `find()`, `idle_seconds()` and
  `Session`, reading the harness's own `~/.claude/sessions/<pid>.json`
  with the verdict rule every earlier reader used (busy -> 0; otherwise
  seconds since `updatedAt`; no sidecar -> `None`).
- `openspec/specs/library` - three ratified requirements;
  `changes/the-session-command` - the proposal for a future `session` command.
- A pin that no console script is declared and no runtime dependency is
  taken (`tests/test_cli.py`).
