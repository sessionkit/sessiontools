# The status line script

Claude Code's status line, kept in sessiontools: a bash script that
reads Claude Code's session JSON and prints one line, and a verb that
installs it:

```text
[worker] | sessiontools | 421K/1.0M (42%) | $12 | 5h:12% wk:63% | Opus 5.5
```

Agent, repo, context used of the window, cost, plan usage, model.

## Install

- `uv tool install sessiontools` puts `session` on the PATH.
- `session statusline install --dry-run` prints the plan and writes
  nothing.
- `session statusline install` copies the script and sets `statusLine`.

Where it goes:

- It installs into the `claude-code` tool's host layer, in the `tools`
  package's settings folder, `~/.sessionkit/tools/settings/` (the kit
  dir, below). The script goes to `statusline.sh` there. `statusLine`
  goes into a drop-in of its own, `claude-code.d/50-statusline.json`.
  Install reads both back. The copy is yours to edit.
- `session start` passes that `statusLine` to claude in its
  `--settings` file, so a seat it launches shows the line
  ([the launcher][launcher]).
- **A bare `claude` does not show it.** To get it in a plain `claude`,
  alias it: `alias claude='session start -'`
  ([which status line a seat shows][which]).
- **It never writes `~/.claude`.** Nothing under `~/.claude`, or under
  `$CLAUDE_CONFIG_DIR`, is opened for writing. A status line you already
  have there is left as it is.
- **Already have a status line?** It is never replaced. Install says
  "you've already got one", gives its path or the inline command, and
  exits 3 with nothing written. `--force` backs up first.
- It needs `jq`.

## What it writes

- **On a bare machine, nothing.** No network, ever.
- A log, `~/.config/session/statusline-log.jsonl`, only if that dir
  already exists. It rotates at 25 MB. `SESSIONTOOLS_STATUSLINE_LOG=off`.
- The usage tap (`ccusage-limits-tap`, skillbox), only if it is on the
  PATH, writes its own limits file ([the usage tap][tap]).
  `SESSIONTOOLS_STATUSLINE_TAP=off`.

## A first install, as run

This is real output from a scratch `HOME`, with its path shortened to
`/tmp/demo-home`. A second run finds it current and writes nothing.

```console
$ session statusline install
session statusline install: /tmp/demo-home/.sessionkit/tools/settings
  state E: empty slot
plan:
  create /tmp/demo-home/.sessionkit/tools/settings/
  create /tmp/demo-home/.sessionkit/tools/settings/claude-code.d/
  create /tmp/demo-home/.sessionkit/tools/settings/statusline.sh (sessiontools 0.2.0)
  create /tmp/demo-home/.sessionkit/tools/settings/claude-code.d/50-statusline.json: "statusLine": {"type": "command", "command": "~/.sessionkit/tools/settings/statusline.sh", "padding": 1}
  note: /tmp/demo-home/.sessionkit/tools/settings/claude-code.d/50-statusline.json is created holding _about and statusLine
installed: /tmp/demo-home/.sessionkit/tools/settings/statusline.sh (sessiontools 0.2.0), statusLine runs ~/.sessionkit/tools/settings/statusline.sh; read back: both match; session start seats show it from their next launch
$ session statusline install
session statusline install: /tmp/demo-home/.sessionkit/tools/settings
  state C: installed and current
already installed (sessiontools 0.2.0); nothing to do
```

What the drop-in holds:

```json
{
  "_about": "Written by `session statusline install` (sessiontools). session start passes statusLine to claude as --settings. Edit or delete it; `session statusline check` says what is here.",
  "statusLine": {
    "type": "command",
    "command": "~/.sessionkit/tools/settings/statusline.sh",
    "padding": 1
  }
}
```

- The plan always prints first. `--dry-run` prints it and stops.
- The command is the `~` form. The shell claude runs it in expands `~`,
  and the form still works for a kit dir synced to a host with another
  home path.
- `_about` is never read as a setting, so it never reaches claude. Any
  other key you add to the drop-in is kept, and so is any other field of
  `statusLine` (`padding`, `refreshInterval`, an unknown key).
- Writes are atomic: a temp file, then a rename. The script lands first
  and the drop-in last, so a run cut short leaves a state the next run
  finishes. A drop-in that is a symlink (dotfiles) is written at its
  target, and the link stays.
- The read-back reads the drop-in's own bytes. Then it reads the layer
  back as `session start` does, merged key by key, and checks that the
  result holds every key the drop-in sets.

## Settings not moved

An earlier build kept the `claude-code` host layer in an older folder,
which is not read any more ([upgrading][upgrading]). While your
`claude-code` files are still only there, install refuses before it
looks at any state, and writes nothing. The second line names the old
folder, shown here as `<old folder>`:

```console
$ session statusline install
session statusline install: /tmp/demo-home/.sessionkit/tools/settings
sessiontools: the settings in <old folder> have not been moved, so they were not read and nothing was written. Move the files in <old folder> to ~/.sessionkit/tools/settings/, then re-run.
Nothing written: the claude-code settings have not been moved. --force does not override this.
```

- It exits 3. `--force` and `--dry-run` refuse the same way.
- Without the refusal, a first install would make the kit dir the host
  layer, and the files still in the old folder would never be named
  again.
- Move the files yourself, then run install again. Install never moves
  them for you.
- `check` says the same line on stderr. Its stdout is unchanged.

## "You've already got one"

A status line set inline in the drop-in:

```console
$ session statusline install
session statusline install: /tmp/demo-home/.sessionkit/tools/settings
  state O: another status line is set
you've already got one, inline in /tmp/demo-home/.sessionkit/tools/settings/claude-code.d/50-statusline.json: {"type": "command", "command": "echo \"$(whoami) | $(date +%H:%M)\""}
sessiontools' status line would show (sample payload):
  Claude | 421K/1.0M (42%) | $12 | 5h:12% wk:63% | Opus 5.5
Nothing written. `--force` replaces it, backing up first.
```

Install reads every file of the layer: `claude-code.template.json`,
`claude-code.json` and every drop-in. A `statusLine` of yours in any of
them counts. It matters which one is read first:

- **In the drop-in itself:** as above. `--force` backs the drop-in up,
  then sets ours.
- **In a file read before it** (the template, `claude-code.json`, or a
  drop-in named below `50-`): `--force` sets ours in the drop-in, which
  is read later and wins. Your file is not edited, not backed up and not
  touched. A sub-key ours does not set still comes from your file, and
  install says so:

  ```console
  $ session statusline install --force
  ...
  installed: /tmp/demo-home/.sessionkit/tools/settings/statusline.sh (sessiontools 0.2.0), statusLine runs ~/.sessionkit/tools/settings/statusline.sh; read back: both match; session start seats show it from their next launch
    statusLine.refreshInterval comes from /tmp/demo-home/.sessionkit/tools/settings/claude-code.json
  yours in /tmp/demo-home/.sessionkit/tools/settings/claude-code.json is still there; 50-statusline.json now wins over it (its refreshInterval still applies). Delete 50-statusline.json to get yours back.
  ```

- **In a drop-in read after it** (`51-` and up): that file wins, so
  install refuses, `--force` included, and writes nothing. Making ours
  win would mean editing your file.
- **Our own entry, typed into another file** (say `claude-code.json`):
  it is ours. Install says where it is, leaves it there, and creates no
  drop-in.

An earlier hand copy of this same line, from claude-code-tools'
`scripts/statusline.sh` (sha256 `68f55691…`), may already be the
script. Install names it:

```console
$ session statusline install
session statusline install: /tmp/demo-home/.sessionkit/tools/settings
  state D: the entry is ours, the script is edited or not ours
you've already got one: /tmp/demo-home/.sessionkit/tools/settings/statusline.sh (an earlier hand copy of this status line (claude-code-tools' scripts/statusline.sh))
sessiontools' status line would show (sample payload):
  Claude | 421K/1.0M (42%) | $12 | 5h:12% wk:63% | Opus 5.5
Nothing written. `--force` replaces it, backing up first.
```

- The preview renders sessiontools' line from the sample payload, with
  the tap and the log off, so it has no side effects. Your own command
  is never run: it may write files of its own.
- One other earlier hand copy (`a19e82a3…`) is named the same way.

## Every case install meets

Install merges the layer's files key by key, as `session start` does,
then sorts what it finds into one state and acts. "The entry" is the
`statusLine` that merge gives. "Ours" means it is `type: command` and
runs `~/.sessionkit/tools/settings/statusline.sh`.

| state | what is there | install | `--force` |
| --- | --- | --- | --- |
| X | a settings file of the layer is not a JSON object | refuse, exit 5 | refuse too |
| E | no entry, no script | write both | same |
| F | no entry; a script | ours, unedited: write the entry (upgrading the script if it is another version). Otherwise refuse, exit 3 | back up the script, write both |
| C | entry ours, script ours and current | nothing to do | same |
| U | entry ours, script ours, another version | replace the script | same |
| M | entry ours, no script | write the script | same |
| D | entry ours, script edited or not ours | refuse, exit 3 | back up the script, replace it |
| O | any other `statusLine` | refuse, exit 3 | in the drop-in: back it up (and a script that is not ours), set the entry, write the script. Read before the drop-in: set ours in the drop-in, theirs untouched. Read after it: refuse too |
| L | no entry or ours; the script is a symlink | refuse, exit 3 | back up the link as a link, replace it with a file; with no entry, set it |

- A `statusline.sh` that is a directory, or anything else that is
  neither a file nor a link, is refused with exit 5, `--force` included.
- In state O, a script your entry ran from somewhere else is never
  touched. Only the kit's `statusline.sh` is replaced.
- Your other `statusLine` fields (`padding`, `refreshInterval`, any
  unknown key) in the drop-in are kept when install sets the entry. A
  fresh entry gets `padding: 1`.

Two more refusals come before any state, beside the one for
[settings not moved][not-moved]. Each exits 3, writes nothing, and
`--force` does not get past it.

**A symlink into `~/.claude`.** The kit dir, its `claude-code.d/`, the
drop-in or the script may be a symlink, say into a dotfiles repo. If one
of them resolves inside `~/.claude` (or `$CLAUDE_CONFIG_DIR`), install
refuses. Writing there would write the dir a bare `claude` reads:

```console
$ session statusline install --force
session statusline install: /tmp/demo-home/.sessionkit/tools/settings
  state E: empty slot
the kit dir /tmp/demo-home/.sessionkit/tools/settings resolves to /tmp/demo-home/.claude/kit, inside /tmp/demo-home/.claude, the dir a bare claude reads: install never writes there. Point it outside /tmp/demo-home/.claude, then run install again
Nothing written. --force does not override this.
```

**A lock that keeps ours out.** A `_locked` path in a file read before the
drop-in (the template, `claude-code.json`, or a drop-in below `50-`) makes
every later file's value for it ignored. Install merges the layer as if
the drop-in were already written. If the result would not hold every key
it sets, install refuses and names the lock:

```console
$ session statusline install --force
session statusline install: /tmp/demo-home/.sessionkit/tools/settings
  state O: another status line is set
/tmp/demo-home/.sessionkit/tools/settings/claude-code.template.json locks statusLine, and is read before 50-statusline.json, so the statusLine install would set there would not be read. Remove the lock first, then run install again
Nothing written. --force does not override this.
```

A lock read after the drop-in, or one on a value ours already has, does
not stop install.

## `--force` and the backups

```console
$ session statusline install --force
session statusline install: /tmp/demo-home/.sessionkit/tools/settings
  state O: another status line is set
plan:
  backup /tmp/demo-home/.sessionkit/tools/settings/claude-code.d/50-statusline.json → /tmp/demo-home/.sessionkit/tools/settings/claude-code.d/50-statusline.json.bak-20300609-170000
  create /tmp/demo-home/.sessionkit/tools/settings/statusline.sh (sessiontools 0.2.0)
  set statusLine in /tmp/demo-home/.sessionkit/tools/settings/claude-code.d/50-statusline.json: "statusLine": {"type": "command", "command": "~/.sessionkit/tools/settings/statusline.sh"}
    (was: {"type": "command", "command": "echo \"$(whoami) | $(date +%H:%M)\""})
backed up: /tmp/demo-home/.sessionkit/tools/settings/claude-code.d/50-statusline.json.bak-20300609-170000
installed: /tmp/demo-home/.sessionkit/tools/settings/statusline.sh (sessiontools 0.2.0), statusLine runs ~/.sessionkit/tools/settings/statusline.sh; read back: both match; session start seats show it from their next launch
```

- A backup sits beside its file, as `<name>.bak-<YYYYmmdd-HHMMSS>`. A name
  is never reused; a second backup in the same second gets `-2`. A
  backup does not end in `.json`, so it is never read as a drop-in.
- Every backup is read back before anything is replaced. If one does
  not land, install exits 4 with "nothing replaced".
- The drop-in is backed up whole, as a plain copy of its bytes, even
  when it is a symlink. To undo, `mv` the backup back. The old
  `statusLine` is also printed, after `was:`.
- An unedited copy of ours is replaced with no backup, because nothing
  of yours is in it.

## `check` and `check --diff`

`check` reads and never writes. It reads the kit layer as `session start`
does. Here, after one edit to the kit copy, on a host where an earlier
install had put a copy into `~/.claude`:

```console
$ session statusline check --diff
session statusline check: /tmp/demo-home/.sessionkit/tools/settings
  state D: the entry is ours, the script is edited or not ours
  settings: statusLine = {"type": "command", "command": "~/.sessionkit/tools/settings/statusline.sh", "padding": 1} (ours), set in /tmp/demo-home/.sessionkit/tools/settings/claude-code.d/50-statusline.json
  script: /tmp/demo-home/.sessionkit/tools/settings/statusline.sh: edited (from sessiontools 0.2.0); `session statusline check --diff` shows the edits
  claude: /tmp/demo-home/.claude/settings.json sets statusLine {"type": "command", "command": "~/.claude/statusline.sh", "padding": 1} (sessiontools' own, from an install before the kit layer): a session start seat shows the kit's; a bare claude shows this one. Left as it is.
  claude: /tmp/demo-home/.claude/statusline.sh: pristine sessiontools 0.2.0, from an install before the kit layer. Left as it is.
  a --settings typed on a launch's command line is folded on top of this (its keys win, except a key a lower file locks with _locked); one in the agent file's claude: section gets none of this; an agent, seat or session layer may override it: session settings SID claude-code shows a seat's
  not visible here: MDM or server-managed settings; the line also needs workspace trust
--- sessiontools 0.2.0 statusline.sh
+++ /tmp/demo-home/.sessionkit/tools/settings/statusline.sh (stamp line dropped)
@@ -149,7 +149,7 @@
 if [ -n "$AGENT" ]; then
   agent_part="[${AGENT}]"
 else
-  agent_part="Claude"
+  agent_part="me"
 fi
```

- The kit copy carries a stamp on line 2:
  `# sessiontools-statusline <version> sha256:<hex>`. `check` drops it,
  hashes the rest and says *pristine*, *edited* or *not ours*.
- The diff is how an edit worth keeping travels back to sessiontools.
- It names the file each part of `statusLine` comes from, when the
  layer's files share it, and a file whose `statusLine` none of applies.
  A drop-in read after `50-statusline.json` that sets its own wins, and
  `check` says so.
- It reports a project `.claude/settings.json` or
  `.claude/settings.local.json` in the cwd that sets `statusLine`: it
  wins over `~/.claude`'s for a bare `claude` there, and a `session start`
  seat still shows the kit's. One that sets `disableAllHooks` stops the
  line, and `check` exits 1.
- It reports managed settings (`managed-settings.json` and
  `managed-settings.d/`) that set `statusLine`, `allowManagedHooksOnly`
  or `disableAllHooks`. Each of those exits 1.
- Exit 0: ours is what a `session start` seat runs (pristine or edited)
  and nothing it can see blocks it. Exit 1: it is not, or something
  does. Exit 5: a settings file of the kit layer is unreadable.

## What is already in `~/.claude`

`install` and `check` both read the dir a bare `claude` reads:
`$CLAUDE_CONFIG_DIR` if it is set, else `~/.claude`. They report what is
there, one line each, and write nothing there, ever:

- **A `statusLine` in its `settings.json`**, with its value: a `session
  start` seat shows the kit's, and a bare `claude` shows this one. An
  earlier `session statusline install` put one there; it is named as
  sessiontools' own.
- **Its `statusline.sh`**, judged as the kit copy is: pristine (from an
  install before the kit layer), edited, an earlier hand copy (named), or
  not ours.
- **`disableAllHooks: true`** in it: no status line runs, a `session
  start` seat's included, because the `--settings` file does not set
  that key. `check` exits 1 for it.
- **An unreadable `settings.json`** is said, never fixed. It never stops
  install.

Each line ends `Left as it is.` A status line there never makes `check`
fail. Those files are yours: remove them, or keep them for a bare
`claude`, as you like.

## Editing your copy

Feed it the sample payload and look at the line:

```console
$ session statusline sample | ~/.sessionkit/tools/settings/statusline.sh
Claude | 421K/1.0M (42%) | $12 | 5h:12% wk:63% | Opus 5.5
```

- `session statusline sample` prints the sample payload, which has no
  `cwd`, so the line has no repo segment.
- Set `SESSIONTOOLS_STATUSLINE_TAP=off` while you try things, so the
  sample's plan usage never reaches the tap's file.
- Once edited, a plain `install` refuses to replace your copy (state D).
  After an upgrade, `check` says which version yours came from.

## The switches

The first lines of the script:

```bash
TAP=${SESSIONTOOLS_STATUSLINE_TAP:-on}
LOG=${SESSIONTOOLS_STATUSLINE_LOG:-on}
LOG_MAX=${SESSIONTOOLS_STATUSLINE_LOG_MAX:-26214400}
```

- Set them in the environment Claude Code runs in, or edit the defaults
  in your copy.
- `off`, `0`, `false` and `no` (any case for the words) mean off.
- `LOG_MAX` is in bytes. The default is 25 MB.

## The log: what, where, how long

- **Path:** `~/.config/session/statusline-log.jsonl`.
- **When:** every render, but only if `~/.config/session` already exists.
  The script never creates that directory, and nothing in sessiontools
  does either.
- **What:** one JSON line per render. This is the output from the
  sample payload, with a made-up time in `t`:

  ```json
  {"t":1907254800,"sid":"00000000-0000-0000-0000-000000000000","model":"claude-opus-5-5","cost":12.345,"ctx":{"input_tokens":1200,"cache_creation_input_tokens":20000,"cache_read_input_tokens":400000},"pct":42.7,"size":1000000,"tin":2400000,"tout":61000}
  ```

- **Why it exists:** other tools read it for a session's running model,
  by `sid`. The record's keys are fixed.
- **Retention:** past `LOG_MAX` (25 MB), the next render moves it to
  `statusline-log.jsonl.1` first. One generation is kept, so it peaks at
  about 50 MB. At a busy host's measured rate of about 4.7 MB a day, that
  is roughly 5 days live and 5 more in `.1`.
- **The race:** two sessions rotating at once can replace `.1` with a
  near-empty file. Nobody archives this log, so that is accepted.
- **A big existing log** (one host had 245 MB) moves to `.1` at the first
  render after install. It is freed at the next rotation.
- **Off:** `SESSIONTOOLS_STATUSLINE_LOG=off`, or remove the directory.
- The write runs in the background with every stream silenced. A slow
  `jq` never holds the line.

## The usage tap: what, where, how long

`ccusage-limits-tap` comes from skillbox's `ccusage` skill, not
sessiontools. It feeds the plan-usage gauge.

- **When:** only if it is on the PATH and the payload carries
  `.rate_limits`. Claude Code sends that to subscribers, after the
  first API response.
- **Path:** the tap's own default file, a `claude-limits.json` in its
  data directory.
  - A session with its own OAuth token writes
    `claude-limits.<account>.json` beside it instead.
  - `CCUSAGE_CLAUDE_LIMITS_FILE` overrides the path.
- **What:** the payload's `rate_limits` (`five_hour` and `seven_day`, each
  with `used_percentage` and `resets_at`), plus `written_at` and `account`.
- **How it writes:** atomically, to a temp file and then a rename, mode
  0600. It overwrites in place.
- **Retention:** one small file per account, which never grows.
- **Network:** none, by its own contract. It is Python stdlib only, with
  no socket or HTTP code.
- **Off:** `SESSIONTOOLS_STATUSLINE_TAP=off`, or leave it uninstalled.
  A missing, failing or slow tap never touches the line: it runs in the
  background with every stream silenced.

## When the kit dir is a git repo

A kit dir kept in dotfiles is left modified, never committed. Install
never commits, stages or resets, and says so:

```console
$ session statusline install --force
...
installed: /tmp/demo-home/.sessionkit/tools/settings/statusline.sh (sessiontools 0.2.0), statusLine runs ~/.sessionkit/tools/settings/statusline.sh; read back: both match; session start seats show it from their next launch
/tmp/demo-home/.sessionkit/tools/settings is a git repo: statusline.sh is now modified there (and 1 backup untracked). Nothing was committed.
```

- HEAD and the index are left byte-identical, staged work included.
  A tracked `statusline.sh` that was `MM` stays `MM`.
- Committing is yours to do, or not.

## Which status line a seat shows

Highest first ([the launcher][launcher] has the detail):

1. A managed `statusLine` (or managed `disableAllHooks`).
2. The `--settings` file `session start` writes: the session, seat and
   agent layers, then the kit host layer that install writes.
3. The project's `.claude/settings.local.json`, then its
   `.claude/settings.json`.
4. `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`).

Its limits:

- **A bare `claude` does not show it.** It reads `~/.claude` and the
  project files only, so it shows `~/.claude`'s status line, if one is
  set, or none. To get the kit's line in a plain `claude`, alias it:

  ```console
  alias claude='session start -'
  ```

  The `-` means no agent, so the words and flags after it pass through to
  claude. The alias is not seen by `session start`'s own launch of
  `claude`, so it does not loop.
- **A `--settings` typed on the command line is folded on top.** A
  `statusLine` in it wins over the kit layer's.
- **A `--settings` in an agent file's `claude:` section gets nothing
  from the kit layer.** It shows that file's status line. `session
  start` says so on stderr.
- **Workspace trust still gates the line**, wherever it comes from. The
  command runs only in a folder whose trust dialog has been accepted
  ([status line docs][cc-statusline]).
- An agent, seat or session layer can override any part of it, or turn
  it off with `"statusLine": null`. `session settings SID claude-code`
  shows what a seat gets.
- **Never edit `~/.claude` to get the kit's line.** Install never does,
  and nothing here asks you to.

## A blank line, and what `check` cannot see

Claude Code shows the line only after the script exits 0 with output.
The script always exits 0. When the line is blank anyway, check:

- **Workspace trust**, above.
- **`disableAllHooks: true`** in `~/.claude/settings.json` or a project's
  settings turns custom status lines off. `check` reports both.
- **`allowManagedHooksOnly`** in managed settings hides a custom status
  line without warning. That is the likely case on a work computer.
  `check` reports it when the managed file sets it.
- **The seat was not launched by `session start`**, or its agent file
  opens its own `--settings`. See the limits above.
- **MDM or server-managed settings** are invisible to `check`. Run
  `/status` in Claude Code to see the setting sources.

A Claude Code plugin cannot set `statusLine`. Plugin settings keep only
`agent` and `subagentStatusLine` ([plugin settings][cc-plugin-settings]).
That is why this is a verb and not a plugin default.

## Exit codes

| code | install | check |
| --- | --- | --- |
| 0 | installed, upgraded or nothing to do (also `--dry-run`) | ours is what a `session start` seat runs, and nothing visible blocks it |
| 1 | | not ours, missing, overridden by a later kit file, or blocked (managed, `disableAllHooks`) |
| 2 | usage | usage |
| 3 | refused: you already have one, the `claude-code` settings have not been moved, a path resolves inside the config dir, or a lock keeps ours out; nothing written | |
| 4 | a write, a backup or the read-back failed | |
| 5 | a settings file of the layer unreadable, or `statusline.sh` not a file or link | a settings file of the layer unreadable |

A `--dry-run` of a case install would refuse exits 3 (or 5), as the real
run would.

## Removing it

There is no verb for it. Remove the two files install wrote:

```console
rm ~/.sessionkit/tools/settings/claude-code.d/50-statusline.json ~/.sessionkit/tools/settings/statusline.sh
```

`check` then shows state E. Restore a backup with
`mv <file>.bak-<stamp> <file>`.

- If you added keys of your own to the drop-in, move them first.

## Upgrading

A new sessiontools may ship a changed script. Then `install` finds your
pristine kit copy in state U and replaces it, with no backup, since
nothing of yours is in it. An edited copy is never replaced without
`--force`, which backs it up first.

An install from before the kit layer left its copy in `~/.claude`. That
copy is reported, never moved or removed: install writes a fresh copy
into the kit layer.

[tap]: #the-usage-tap-what-where-how-long
[cc-statusline]: https://code.claude.com/docs/en/statusline
[cc-plugin-settings]: https://code.claude.com/docs/en/plugins-reference
[launcher]: session-start-launcher.md#the-status-line
[which]: #which-status-line-a-seat-shows
[not-moved]: #settings-not-moved
[upgrading]: settings.md#upgrading-from-010
