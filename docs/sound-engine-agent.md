# The sound engine (a launchd agent)

`busy.player: engine` hands [the busy sound][busy] to one resident
Swift AVAudioEngine process, which holds the output open. The
hook to the first sample is about 20 ms instead of about 220 ms with
`afplay` (median of 20 runs).
It is macOS only, one per user, and **off until you switch it on**:

- `session sound engine on` builds the engine if it is stale, loads the
  agent and pings it.
- `session sound engine status` shows the binaries, the plist, the pid
  and the engine's answer.

How it behaves:

- A host-wide opt-in is one line in the host layer
  (`~/.sessionkit/tools/settings/sounds.json`):
  `{"busy": {"player": "engine"}}`.
- Not installed, off or dead is silent: the sound never falls back to
  `afplay`.

## `busy.player`

- `afplay` (unset is `afplay`): a detached `afplay` per sound, as
  before. See [the busy sound][busy].
- `engine`: the resident sound engine, below.
- Any other value, wrong case included, is silent in every mode.
- Read by the batch trigger only: `landed` keeps `afplay`.
- Switching away from `engine` unlinks the seat's records and tells the
  engine `clear`, so a sound it is counting cannot outlive the switch.

## How a hook reaches it

- **The first arm of a session** starts python once. It writes the
  engine's record, `<session dir>/sounds/busy/engine.<claude pid>` (the
  file, delay, claude pid and mouth dir), then sends `arm`.
- **Every later hook** sends ONE OSC datagram, `/sound/event ,ssd EVENT
  SID T`, to UDP `127.0.0.1:44100` with the native sender `sound-event`.
  No python, no reply, no wait.
- **The engine owns** the delay, the speech guard, the no-replay latch
  and the stop.
- **The sender** is `bin/sound-event` in the sound leaf (`session path
  sound`): `$SESSIONTOOLS_SOUND_HOME` when set, else the old
  `~/.local/share/sessiontools/sound/` while it is there, else
  `~/.sessionkit/tools/state/sound/`.
  Not executable counts as off: nothing is sent.
- **A resumed seat** (a new claude pid) resolves once and writes its own
  record.
- **From python:** `sessiontools.sound.send(event, sid)`, fire and
  forget.

## `session sound engine build|on|off|status`

The engine runs as the per-user launchd agent
`dev.sessionkit.tools.sound-engine`. The label lives in one constant in
the code; `status --json` prints it as `label`, and `--help` names it.

- `build [--force] [--dest DIR]` compiles `sound-engine` and
  `sound-event` from the packaged Swift sources with the system `swiftc`
  into `<sound home>/bin/`. All or nothing; no launchd.
- `on` builds if stale and refuses if `127.0.0.1:44100/udp` is held by
  anything but the loaded agent's own process. It writes the plist into
  `~/Library/LaunchAgents`, bootstraps it into `gui/<uid>`, and confirms
  with a ping. **`on` is a deploy:** the agent loads again at every login.
- `off` boots it out and removes the plist; `--purge` also removes the
  binaries.
- **The old label is retired.** An earlier release ran the job under
  another label. `on` and `off` boot out a job still loaded under it and
  remove its plist; the old engine holding the port is not a refusal.
  With nothing to retire they say so and do nothing.
- `status [--json]` shows the binaries, the plist, whether it is
  loaded, the pid and the engine's answer. It names a job or plist still
  there under the old label (`legacy` in `--json`).
- `on --dry-run` and `off --dry-run` say what they would do, the retire
  included, and change nothing.
- Exit 0 done, 1 tried and failed (bootstrap refused, no answer, or the
  service file not written), 2 refused before anything changed (no
  `swiftc`, a failed build, the port held, not macOS) or a usage error.
- The log is `~/.sessionkit/tools/log/sound-engine.log`.
- Before 0.3.2 the log was `~/Library/Logs/sessiontools/sound-engine.log`.
  That file is left in place, and `status` names it while it is there.

### The service file

`on` and `off` also write `~/.sessionkit/tools/services/sound-engine.json`.
It is the file `sessionkit service list` reads to find this job. The
folder sits beside `settings/`, never inside it, so `session settings`
never reads it.

- **`on`** writes it right after the plist, with `"enabled": true`.
- **`off`** rewrites it with `"enabled": false` and keeps the file.
  `--purge` removes the binaries and keeps the file too. An `off` with no
  file writes one.
- **A refused `on`** writes no file. **`--dry-run`** prints the file
  under `service would write PATH:` and writes nothing.
- **Each write is whole and atomic.** A temp file in the same folder is
  renamed over the old one, so a reader never sees half a file. Keys
  added by hand are not kept.
- **`port`** is the port the plist hands the engine (`--port`), so the
  two always agree. `protocol` is `udp` and `bind` is `127.0.0.1`.

The file carries these keys:

```json
{
  "service": 1,
  "name": "sound-engine",
  "label": "dev.sessionkit.tools.sound-engine",
  "enabled": true,
  "install": "session sound engine on",
  "uninstall": "session sound engine off",
  "description": "the busy sound's resident audio engine",
  "port": 44100,
  "protocol": "udp",
  "bind": "127.0.0.1",
  "note": "runs only in a logged-in GUI session (LimitLoadToSessionType Aqua)"
}
```

**If the write fails,** the engine is still loaded by `on` (or booted out
by `off`), as you asked. The verb prints this line, then exits 1:

```text
service could not write PATH: REASON; the previous file is unchanged
```

Run the same verb again once the folder is writable. Every step is
safe to repeat.

## Paths it will not touch

macOS guards some places with a privacy prompt, so the engine refuses
any path there, untouched, and the log says why.

- Desktop, Documents, Downloads, iCloud Drive
  (`~/Library/Mobile Documents`), `~/Library/CloudStorage`.
- Other apps' data: `~/Library/Containers`, `~/Library/Group Containers`.
- `~/Library/Mail`, `~/Library/Messages`.
- `/Volumes/*`, `/net`, `/Network`, and any mount the kernel calls
  non-local or removable.
- It checks the sound file, the mouth dir (`MOUTH_DIR`, or `mouth/` in
  the root sessionvoice gives), the sessions root and each seat's record
  dir.
- A seat whose sound or mouth is refused stays silent: keep the busy
  sound and the mouth elsewhere.

## Nothing read through a link

- After the check, the sound file, each record and `player.pid` are
  each read through ONE descriptor, opened with no link allowed anywhere
  on the checked path. The sound is decoded from that descriptor.
- A FIFO never blocks the open, and `playing.json` is only stat'd, the
  same way.
- So a link put on a file, or on a directory above it, after the check
  is refused, never followed.

[busy]: busy-sound-hook.md
