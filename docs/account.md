# Switching Claude accounts: `session account`

`session account` says which of your Claude accounts this Mac is on, and
moves it to another one from a token you already hold. There is no
browser, no `/login` and no restart. It never prints a token.

- **`use` moves EVERY seat on the Mac.** By default the host's login is
  one Keychain item, `Claude Code-credentials`. Every session with no env
  token of its own reads it, and follows a change within about 50 s.
- **Each seat that moves cold-starts its cache.** Measured on one busy
  host: 2.07M and 1.10M tokens for all its seats, one swap each. `use`
  shows that cost and stops, unless you pass `--yes`.
- **A token-file host is switched by its file.** If the login shell
  exports `CLAUDE_CODE_OAUTH_TOKEN` from a file, name that file in the
  `session` setting `account.token_file`. `use` then swaps the file and
  never touches the Keychain. Live seats keep their token until they
  restart. A seat `session start` launches does NOT follow the file
  unless its agent file says `inherit: [CLAUDE_CODE_OAUTH_TOKEN]`; without
  it, the seat signs in from Claude's stored credentials. See
  [Token-file hosts][token-file-hosts].
- **The plan comes first, then the gates:** a named `--witness`, `--yes`,
  a token preflight before any write, and a 0600 backup. A failed write
  restores the backup.
- **`new` adds an account.** It runs `claude setup-token` in a masked
  copy of its screen, saves the token to passage, reads it back, and
  registers it. The token is never on screen or on argv. See `new` below.

The verbs:

- `session account list`: names, emails and sources, and which one the
  host is on.
- `session account status [--json] [--no-resets]`: the host account,
  this seat's route, plan usage and resets.
- `session account use NAME --witness WHO`: the plan and its cost. It
  writes nothing (exit 3).
- `session account use NAME --witness WHO --yes`: move the host.
- `session account new NAME --email E`: sign in, save to passage,
  register.
- `session account add NAME --email E --source BACKEND:REF [--source ...] [--no-fp]`:
  register token sources that already exist.
- `session account remove NAME`: unregister. It deletes no token.

## Blast radius

| route | moves | cache cost | reversible by | gate |
|---|---|---|---|---|
| `account use` (Keychain) | every seat on the Mac with no env token, within ~50 s | the sum of their contexts (2.07M / 1.10M measured) | `use` back, or restore from the backup | witness, `--yes`, preflight, backup |
| `account use` (token file) | no live seat; each one that relaunches from a shell that reads the file | none now; each seat's context when it restarts | `use` back, or restore from the backup | witness, `--yes`, preflight, backup |

- **On a Keychain host, a seat with an env token is immune.**
  `CLAUDE_CODE_OAUTH_TOKEN` beats the Keychain, so `use` never moves it.
  `status` says which route this seat is on.
- **On a token-file host, a live seat keeps the token it launched with.**
  The shell read the file at launch, so the swap reaches a seat only when
  it restarts. `use` names every live seat in its plan, and moves none.
- **On a token-file host, a seat with no env token is NOT on the file.**
  `session start` strips `CLAUDE_CODE_OAUTH_TOKEN` from the seats it
  launches, unless the agent file says `inherit:
  [CLAUDE_CODE_OAUTH_TOKEN]`. A seat without it signs in from Claude's
  stored credentials, so `use` does not move it, even at a restart.
  `status` says `NOT on the token file` for it, never `follows the host`.
- **Moving one running seat by itself is not built.** It is a relaunch,
  which needs a respawn verb that does not exist.

## `use NAME`: in the order that makes failure cheap

Each step passes or exits. Nothing is written, backed up or run before
the plan is shown and every gate has passed.

1. Pick the route: the Keychain item, or the token file that
   `account.token_file` names (see [Token-file hosts][token-file-hosts]).
   A bad setting or file: exit 5.
2. Look NAME up in the registry. Unknown: exit 2, naming the known ones.
3. Resolve NAME's token in memory, through its sources in order. A `pass`
   source costs a PIN and a key touch. All fail: exit 4.
4. Read the host's current sign-in: the item, or the file. Already on
   NAME: say so, exit 0.
5. Print the plan: from, to, the item or file, the token's source, and
   the cost (how many seats move and the tokens their caches cold-start)
   or, for a token file, the restart report. Then where the backup goes,
   and the witness, or `MISSING`.
6. The gates, after the plan. No `--witness WHO`: exit 2. No `--yes`:
   exit 3. Neither writes a file or runs `claude`.
7. Preflight the token: one `claude -p` in a tmp directory, with an empty
   `CLAUDE_CONFIG_DIR`, a short allowlist of your environment and the
   token only in that child's environment. It must answer `accountok`,
   or exit 4. Nothing is on disk yet.
8. Back up the current item to `keychain-backups/<UTC>.json`, or the
   file to `token-file-backups/<UTC>.token` (0600, read back). A failure:
   exit 5.
9. Write the item (only `claudeAiOauth` is replaced; other keys are
   kept), or swap the file. Read it back and compare. A mismatch or
   failed write restores the backup (exit 6). A failed restore is exit 7,
   loud, naming the backup and the manual restore line.
10. Append `{at, from, to, by, witness, backup, route}` to
    `host-ledger.jsonl`. `route` is `keychain` or `token-file`.

A plan without `--yes` or `--witness` costs a key touch for a `pass`
source and one read of the item or file. It makes no `claude` call and
leaves no backup.

Exit codes: 0 done or already there; 2 usage, unknown name, no witness
(after the plan); 3 plan shown, no `--yes`; 4 token unresolvable or
preflight failed; 5 a bad setting or token file, or a backup, registry
or ledger failure; 6 write failed, restored; 7 restore failed.

## Token-file hosts

Some hosts sign in from an env token, not the Keychain: the login shell
exports `CLAUDE_CODE_OAUTH_TOKEN`, read from a file, and every seat
launched from that shell carries it. On such a host `use` swaps the file.

Name the file in the `session` setting `account.token_file`, in
`~/.sessionkit/tools/settings/session.json` or a file in
`~/.sessionkit/tools/settings/session.d/` (`session settings` shows what a
host has). The path below is a placeholder:

```json
{"account": {"token_file": "~/path/to/oauth-token"}}
```

- **There is no default.** Unset, the host's route is the Keychain item.
  `use` never guesses the route from `CLAUDE_CODE_OAUTH_TOKEN` in its
  own environment, since `session start` strips that variable from the
  seats it launches. A seat that needs it names it under `inherit:`
  ([credentials are never handed down][credentials]).
- **Unset on a token-file host,** `use` finds no Keychain item and
  refuses (exit 5, nothing written). The message names the setting and
  where it lives, and says when this shell exports the variable.
- **The value** is an absolute path or one starting with `~`. The file
  must exist, be a private (0600) regular file of yours, and hold one
  token line. Anything else is exit 5, naming the path and why, never
  the content. `use` does not create a host's sign-in.
- **A symlink is followed.** The swap happens in the link's target
  directory, so the link, and the profile that reads it, keep working.
- **`security` is never run** on this route, not even to look. The plan
  says the Keychain is not read.

The swap, after the gates and the preflight: back the file up, stage the
new token in a temp file beside it (0600, fsynced), read the staged file
back and compare, then rename it over the file. Then read the file back
once more. A failure before the rename removes the temp file and leaves
the file as it was; a failure after it restores the backup. Both are
exit 6. A restore that fails is exit 7, with the `install -m 600 BACKUP FILE` line
to run by hand.

**The restart report.** `use` cannot see which token a running seat
holds (reading another process's environment would show it). So it
names every live seat, and says whether THIS seat holds the file's
current token, compared in memory:

```text
use home (home@example.com): the host moves from work to home
  file:    token file ~/path/to/oauth-token
  keychain: not read: a seat that signs in from it does not move
  token:   passage:claude/oauth-tokens/home@example.com (preflight: runs before the write)
  seats:   2 live seats keep the token they launched with until they restart: alpha, beta; new launches from a shell that reads the file read the new file
  session start: its seats do NOT follow the file unless their agent file has inherit: [CLAUDE_CODE_OAUTH_TOKEN]; without it they sign in from Claude's stored credentials
  this seat holds the old token: yes
  backup:  ~/.sessionkit/tools/accounts/token-file-backups/ (written before the write)
  witness: sam
plan only: re-run with --yes to swap the file
```

The `seats:`, `session start:` and `this seat` lines are printed again
after `done:`. A
seat scan that fails is reported, never a gate.

**The re-mint trap.** A new setup token for the same account has a new
fingerprint. If the file holds a token the registry does not know,
`status` says `token file: unknown (not in registry)`, not the account's
name. To be named again, register the token the file now holds, or `use`
back to an account whose source the registry knows.

## `new NAME --email E`: a token made, saved and registered

The three hand steps of adding an account, as one verb: `claude
setup-token`, a save to passage at `PREFIX/E`, and `add`. Every cheap
check comes first, and nothing is written before the token is proven.

PREFIX is the passage folder the token goes in. It is the `session`
tool's setting `account.passage_prefix`, read through the settings
cascade's template and host layers (`session settings` shows what a host
has). Unset, it is `claude/oauth-tokens`. To save into a store that
already keeps tokens elsewhere, set it to that folder:

```json
{"account": {"passage_prefix": "claude/oauth-tokens"}}
```

A value that is not a folder path inside the store (absolute, empty, a
`.` or `..` part, a space, a leading `-`, a token's shape), or a settings
file that cannot be read, stops `new` before the sign-in (exit 5), naming
the key and the file.

1. stdin and stdout are a terminal, at least 40 columns wide (exit 2).
   A terminal too short for setup-token's screens gets a warning here,
   before the browser opens: about 25 rows at 40 columns, 21 at 80.
2. PREFIX is read (exit 5), NAME and E are valid, E is safe as an entry
   name, the registry loads (exit 5), and NAME is not taken (exit 2).
3. passage is on PATH, with a store and an identities file (exit 5),
   and the entry does not exist (exit 2). A `setup-token-*` folder left
   by an earlier run that was killed is scanned for a token's shape and
   removed; a hit is named by path, never by value.
4. `claude setup-token` runs on a private pty. Sign in **as E** in the
   browser that opens; `new` cannot check which account a token is for.
5. The token has the setup-token shape (`sk-ant-oat01-...`), or exit 4.
6. A preflight, as `use` runs one: one `claude -p` that must answer
   `accountok` (exit 4). Its child gets a short allowlist of your
   environment, so no other credential or provider setting can answer
   for the token.
7. `passage insert -m ENTRY`, the token on stdin (exit 8).
8. A read-back through the `passage:` source, compared by SHA-256 (exit
   8). It runs even after an insert that failed or timed out, if the
   entry was written: a commit hook that hangs can leave a good entry.
9. `add`'s own registration, with the fingerprint already known, so the
   token is decrypted once in all (exit 9 if this step fails).

What you see while it runs:

- **A masked copy of setup-token's screen**, drawn by `new` on the
  alternate screen. No byte from setup-token reaches your terminal, and
  a screen that shows a token is never drawn at all: you see `setup-token
  showed a token: captured, not shown` instead.
- **The pasted sign-in code shows as asterisks.** Claude Code 2.1.299
  masks its paste field (read from its code; a version that echoes it
  would show the code, which is single-use).
- **If its screen is taller than your terminal**, nothing more is drawn
  once it reaches the last row, and `new` ends the run (exit 4). Make the
  terminal taller and run `new` again.
- **To stop:** press Ctrl-C twice within a second. That is `new`'s own
  exit: it ends setup-token, restores your terminal, saves nothing and
  exits 4. A single Ctrl-C goes to setup-token, which is not known to
  stop on it (or on Esc) while it waits for the sign-in.
- **When it ends with no token** (cancelled, a policy refusal, an
  account on hold), `new` prints the last screen it drew, masked, so you
  can see why.

```text
new e (e@example.com): sign in AS e@example.com in the browser that opens
  token:  goes to passage:claude/oauth-tokens/e@example.com, on this host
  screen: setup-token runs in a masked copy of its screen; the token is never shown
  stop:   Ctrl-C twice ends the run; nothing is saved
token captured (not shown); preflight: accountok
saved: passage:claude/oauth-tokens/e@example.com (read back: digests match)
added e (e@example.com): passage (touch); fp known
  registry: /Users/you/.sessionkit/tools/accounts/accounts.json
```

- **`new` never overwrites or deletes a passage entry.** After exit 8,
  check the entry it names, or remove it by hand (`passage rm ENTRY`),
  then run `new` again.
- **After exit 9** the token is saved and verified. `new` prints the
  `session account add ... --source passage:ENTRY` line that finishes
  it (under another NAME, if NAME was taken during the sign-in).
- **passage only.** There is no fallback to `pass` or to a file. The
  read-back may cost one Touch ID or key touch, as any `passage:` source
  does.
- **A token that `new` drops** (a run ended after the token appeared, a
  failed preflight, exit 8) was never shown and is not registered, but
  it is still valid for its year. Revoking it, if claude.ai offers that,
  is yours to do.
- **Tested against stubs only.** No live sign-in has been run through
  it yet.

Exit codes: 0 saved, read back and registered; 2 usage (a bad NAME or
email, NAME taken, the entry exists, no terminal); 4 no token, or its
preflight failed; 5 the registry or passage refused, before the sign-in;
8 not saved, or not read back (the entry, if any, is named); 9 saved and
verified, not registered. A signal during the sign-in is 128 + its
number.

## The other verbs

- **`list`:** registry order, one row per account: name, email, source
  kinds and their cost (`file (free)`, `pass (key touch)`), whether its
  fingerprint is known, and `<- host` on the host's account. A
  `passage` source shows as `passage (touch)`. It resolves
  no token.
- **`status`:** the host's account, by fingerprint of the Keychain item
  (`Keychain, fp match`), or of the token file on a token-file host
  (`token file, fp match`). No match is `unknown (not in registry)` or
  `token file: unknown (not in registry)`, never a guess. A token file
  that is refused is `token file: unreadable (WHY)`. This seat's route:
  `env token: immune to use` on a Keychain host, `env token: from launch;
  moves at restart` on a token-file host. With no env token: `follows
  the host` on a Keychain host; on a token-file host, `NOT on the token
  file`, since such a seat signs in from Claude's stored credentials
  (`--json`: `seat.via` is `no env token: not on the token file`,
  `seat.name` null). That line names `inherit:
  [CLAUDE_CODE_OAUTH_TOKEN]` as the way to carry the token, and notes
  that a tool shell may strip the variable from a seat that has it. A
  host whose `account.token_file` is set to a value that is refused is
  still a token-file host for this line. Plan
  usage of the live account, from the exporter at
  `http://localhost:9103/metrics`, or `usage: unavailable`. When the
  exporter has no answer, the limits file, if the host names one (below).
  Each usage window shows when it resets, in text and in `--json`
  (`resets_at`). `--no-resets` hides them (and drops `resets_at` from
  `--json`); `--resets` is still accepted and does nothing. A window the
  exporter gives no reset time for shows none in text, and `resets_at:
  null` in `--json`.
- **`add`:** registers token sources that already exist (`file:PATH`, a
  private 0600 file of yours; `pass:ENTRY`, gpg pass; `passage:ENTRY`,
  passage and age), cheapest first. It resolves one source to fingerprint
  the token, unless `--no-fp`. It stores no token anywhere: `new` is the
  verb that does. A `passage:` entry that does not exist is refused
  before anything is decrypted. `passage:` is for accounts only;
  `session start` secrets still take `pass:`.
- **`remove`:** deletes the registry entry only, never a `pass` entry or
  a file. It refuses (exit 3) the account the host is on.

## The limits file: usage with no exporter

A host with no usage exporter can still show its plan usage from the
limits file a status-line tap writes. `status` reads it only when the
exporter has no answer, and only if the host names it. The `session`
setting `account.limits_file` holds its path, set in the template or host
layer of the settings cascade (`session settings` shows it). There is no
default. Unset, `status` prints what it always has.

```json
{"account": {"limits_file": "~/path/to/limits.json"}}
```

```console
$ session account status
usage: 5h 3% (resets 2030-06-09T17:00+00:00), 7d 1% (resets 2030-12-08T17:00+00:00)  (usage of the host account, limits file, 2m old)
```

- `--json` gives `usage.source` `limits_file`, the `file`, `written_at`,
  `age_seconds` and `stale`, and the same `windows` (`5h`, `7d`) the
  exporter gives, each with `expired`.
- **The age is always said.** Past 15 minutes the file is `STALE`. Its
  numbers are still shown, and the reader decides.
- **A window whose reset has passed shows no percent**: `5h reset since
  written`, with no reset clause in text, and `percent: null`,
  `expired: true` in `--json`. It is never guessed to be 0.
- Resets follow the same flag as the exporter's windows: shown by
  default, hidden by `--no-resets`.
- **Only the host's window.** The file's `account` must be absent or
  `default`. Another account's window is refused.
- **Never a guess.** If the setting is refused, or the file is missing,
  unreadable or malformed, usage stays unavailable and the reason follows
  the exporter's (`usage_unavailable` in `--json`). The exit code is still 0.

## Files

All under `~/.sessionkit/tools/accounts/`, 0600, never committed. That
folder is 0700, and so is each missing folder above it when `session
account` makes it; a folder that already exists keeps its mode. It sits
beside `~/.sessionkit/tools/settings/`, never inside it: `session
settings` reads that tree, and the backups hold tokens.

- `accounts.json`: the registry. Names, emails, source refs and
  fingerprints (`sha256(token)[:16]`). Never a token.
- `account-config.json`: optional. `host_read` (default `true`; `false`
  makes `status` and `list` read the ledger instead of the Keychain or
  the token file) and `usage_url`. The limits file is not set here: it is
  the `session` setting `account.limits_file`.
- `host-ledger.jsonl`: one row per `use`, with its `route`. No token, no
  fingerprint, no path to the token file.
- `setup-token-XXXXXX/`: `new`'s config folder for setup-token, for the
  length of one run. It is scanned for the token, by path, and removed.
- `keychain-backups/<UTC>.json`: restore points. **They hold the item,
  token included**: never print, copy or commit one.
- `token-file-backups/<UTC>.token`: a token-file host's restore points,
  in a 0700 folder. **Each holds the old token**: never print, copy or
  commit one.

### Moving the old folder

Builds before this one kept these files in `~/.config/sessiontools/`.
That folder is not read any more, and nothing is written to it.

- **Until it is moved,** while it holds files and
  `~/.sessionkit/tools/accounts/` does not exist, `list` and `status` say
  so once on stderr, naming both folders and the command that moves them.
  They then read the new folder only, so `list` says no accounts are
  registered. Their stdout is unchanged.
- **`add`, `new`, `use` and `remove` refuse** with exit 5 and write
  nothing. A write would make the new folder and leave both.
- **A folder that cannot be looked at** (no search permission) is exit 5,
  naming it. It is never taken as absent.

The move is `python -m sessiontools.account.movehome`, run by the same
Python that runs `session`. The stderr line prints that full command,
because under `uv tool install` that Python is not on `PATH`. The first
line is a dry run, the second makes the move:

```console
$ /path/to/python -m sessiontools.account.movehome
$ /path/to/python -m sessiontools.account.movehome --apply
```

- **A dry run by default.** It lists each entry as `path mode size`, then
  the file count and the byte total. It never prints a digest or any
  content. A file that is not 0600, or a folder that is not 0700, is a
  warning: it is moved as it is.
- **`--apply`** makes the missing folders above the new one (0700), then
  renames the old folder in one step, so the modes and times are carried.
  It then reads the new folder back and checks every path, mode, size and
  sha256 against the dry run's list: `verified: N file(s), B byte(s),
  sha256 equal`.
- **It refuses** (exit 3, nothing moved) when both folders exist, when
  either path is a symlink, when a symlink or a special file is inside,
  and when the rename fails (another filesystem: nothing is copied).
- **A failed verify** renames the folder back, exit 4, naming only the
  differing paths. A path it cannot look at, list or read is exit 6,
  naming it.
- **`--home H`** moves the folder under H instead of `$HOME`. It touches
  nothing else: not `~/.config` itself, not the settings, not mail.

## Never a token

- A token lives in memory only, between resolve and hash, preflight or
  write. Never in argv, output, the ledger, the registry or an error.
- The Keychain write goes to `security -i` on stdin, hex-encoded, never in
  argv (`ps` is world-readable). The token-file swap writes the token
  only to the temp file it renames over the host's file. `new` gives passage the token on stdin
  too, and its preflight gets it in the child's environment, as `use`'s
  does.
- `python -m sessiontools.account.leakscan PATH...` finds token-shaped
  strings and reports each by path and line, never by value.

## Not in v1

- **Storing a new token anywhere but passage** (a `pass` entry, or a
  0600 file for a host with no passage): an open question. `new` writes
  to passage only.
- **The `session` skill does not teach the verb yet.** It waits on a
  decision about the skill's size.

[token-file-hosts]: #token-file-hosts
[credentials]: session-start-launcher.md#credentials-are-never-handed-down
