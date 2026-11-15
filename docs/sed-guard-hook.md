# The sed guard (a plugin hook)

The plugin's `hooks/sed-guard` refuses a Bash call that runs `sed -i ''`,
BSD sed's in-place form, when the sed the call runs is GNU sed. The
refusal names the GNU form, so the agent can run it next.

- **Why:** GNU sed's `-i` takes no separate argument. GNU reads `''` as
  the script (or, after `-e` or `-f`, as a file named `''`), so
  `sed -i '' 's/a/b/' f` edits nothing it was meant to and exits 2.
- **On by default.** Its whole value is catching a seat that does not
  know which sed it has.
- **Only against GNU sed.** A host whose sed is BSD, or
  `/usr/bin/sed -i ''` on a host where GNU sed is first on PATH, is
  never refused.
- `SESSIONTOOLS_HOOK_SED=off` (also `0`, `false`, `no`, any case)
  switches it off, and the plugin stays loaded.
- It never breaks a turn: anything unexpected lets the call run, with
  no output.

## What it refuses

```text
sed -i '' 's/a/b/' f        refused: sed -i 's/a/b/' f
sed -i "" 's/a/b/' f        refused: sed -i 's/a/b/' f
cd d && sed -i '' -e x f    refused: cd d && sed -i -e x f
sed -Ei '' 's/a/b/' f       refused: sed -Ei 's/a/b/' f
```

- The flag is `-i`, a short-flag cluster ending in `i` (`-Ei`, `-ni`),
  or `--in-place`. The next word must be a SEPARATE empty argument.
- The sed is any word whose basename is `sed` or `gsed`, anywhere in a
  simple command, so `xargs sed`, `find ... -exec sed` and
  `/usr/local/bin/sed` count.
- **These pass:** `sed -i 's/a/b/' f`, `sed -i.bak ...`, `sed -i -e ...`,
  `sed -n '1p' f`, `sed -i'' ...` (the shell joins it to `-i`).

## It parses, it never greps

- **Heredoc bodies are cut first**, for `<<` and `<<-` with the
  delimiter in any quoting (`EOF`, `'EOF'`, `"EOF"`, `\EOF`). A mail
  body that mentions `sed -i ''` never refuses. `<<<` is a here-string,
  not a heredoc.
- **The rest is tokenised with Python's `shlex`** (posix, newline a
  separator) and split into simple commands at `;`, `&`, `|`, `(`, `)`
  and newline. A quoted string that holds the text (`echo "sed -i ''"`)
  is one word, and a comment is dropped: neither refuses.
- **An unquoted `echo sed -i '' x` is refused.** Every word is scanned,
  to catch `xargs sed`; the echo is rare, and its refusal explains
  itself.
- **Known misses, each letting the call run:** a `bash -c '...'` string
  is not looked into; `shlex` starts a comment at a `#` in the middle of
  a word (`$#`, `${#a[@]}`) and drops what follows; backticks and
  `$'...'` mean nothing to it. A miss costs one failed sed, as before.

## Which sed it asks

- **The sed the call names is asked `--version`**, capped at 1 s. Only
  `GNU sed` in the answer refuses.
- A word with a `/` is asked at that path; a relative one from the
  payload's `cwd`.
- A bare `sed` is the first `sed` on the hook's PATH. The hook's PATH
  stands in for the Bash tool's; on the host measured, both put GNU sed
  (homebrew's gnubin) first.
- `gsed` is GNU sed by name and is not asked.
- No sed found, a probe that fails, or one past its cap: the call runs.

## The GNU form

- **Cut from the original text:** the empty argument and the blanks
  before it are deleted in place, and every other byte is kept. Globs,
  `"$f"`, `~` and `$(...)` stay exactly as written.
- A one-line command is shown whole. A longer one shows only the lines
  that were cut, a backslash-continued line whole.

## The refusal contract, measured

The refusal is Claude Code's PreToolUse JSON on stdout, exit 0:

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse",
  "permissionDecision": "deny",
  "permissionDecisionReason": "sed -i '' is BSD sed's form. ..."}}
```

- **Measured on Claude Code 2.1.325**, macOS, 2026-11-15, in a
  live seat running `--dangerously-skip-permissions` (the payload read
  `permission_mode: bypassPermissions`). A PreToolUse Bash hook in a
  task-local `.claude/settings.local.json` answered a `touch` call:
  - with the JSON above: the file was not created, and the tool result
    was `PreToolUse:Bash hook error: <the reason>`;
  - with exit 2 and the reason on stderr: also not created, the result
    prefixed with the hook's path.
- So bypass mode does not skip the refusal. The JSON shape is used, as
  its reason reaches the agent without the path. The shim keeps its
  stderr at `/dev/null`.
- The measurement used a settings hook, not this plugin's, because a
  nested `claude -p` could not sign in from that seat. Plugin hooks and
  settings hooks run through the same PreToolUse handling.

## What it costs

- **A payload with no `sed`, or with no empty quote pair** (`''`, or
  `""`, which arrives as `\"\"`), starts no python: the shim reads it
  with `read` and tests it with `case`, both builtins. The whole payload
  is tested, the tool's description and the `cwd` included.
- Otherwise, one python start, and one `sed --version` only when a
  BSD-form call is found.
- **It fails open and silent:** no python, python older than 3.11, bad
  JSON, an unbalanced quote or a crash all exit 0 with no output.

## The hook

| event | matcher | command |
| --- | --- | --- |
| PreToolUse | `Bash` | `hooks/sed-guard` |

- Its own entry, after the busy sound's `*` entry. A refused call never
  runs. The busy sound's `arm` has already run by then; that its batch
  end still clears the sound after a refusal is not yet measured.
- The code is `src/sessiontools/sed_guard.py`; the tests are
  `tests/test_sed_guard.py`, through the real shim with a fake GNU and a
  fake BSD sed on PATH.
- It writes no settings file, and nothing outside the plugin.
