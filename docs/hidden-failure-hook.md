# The hidden-failure note (a plugin hook)

A test run piped into `tail` reports `tail`'s exit status, not the test
run's. So `pytest -q 2>&1 | tail -20` can exit 0 with `3 failed` on the
screen. On an agent seat, the plugin's `hooks/hidden-failure` says so in
one line, and does nothing else.

```text
[hidden-failure] pytest | tail exited 0 but its output shows a failure (pytest_failed): the pipe reported tail's status, not pytest's. Keep the gate's status: cmd > log 2>&1; s=$?; tail log; exit $s
```

- **It only advises.** It never blocks a call, never rewrites a command
  and changes no exit code. Its output has no `decision`,
  `permissionDecision` or `updatedInput`.
- **Agent seats only.** It is on when `TASKMASTER_ROLE` or
  `CLAUDE_CODE_AGENT` is set and not empty in the hook's environment.
  Without either, the shim exits before python starts, so a person's own
  session never sees it.
- **One switch** turns it off on a seat:

| variable | what it does |
| --- | --- |
| `SESSIONTOOLS_HOOK_HIDDEN_FAILURE=off` | switches it off (also `0`, `false`, `no`, any case). Anything else, unset included, leaves it on for a seat. |

## When it speaks

Two hooks in `hooks/hooks.json`, both with matcher `Bash`:

- **PostToolUse (`hidden-failure post`):** all of these must hold:
  - the call's command pipes a gate into `head` or `tail`;
  - the call exited 0;
  - its output (`tool_response` stdout and stderr) matches a failure
    marker.
- **PreToolUse (`hidden-failure pre`):** the command pipes a gate into
  `head`, which may also cut the failure off. It says the same kind of
  line before the call runs. A gate piped into `tail` gets no PreToolUse
  line: most of those runs hide nothing, so only a run that did gets the
  note.

```text
[hidden-failure] make | head reports head's status, not make's, and head may cut off the failure. Keep the gate's status: cmd > log 2>&1; s=$?; tail log; exit $s
```

## What counts

- **A gate** is a pipeline stage named `pytest`, `python -m pytest`,
  `uv run pytest`, `make`, `message send`, `git commit` or `git push`.
  Wrappers and settings in front of it (`nice`, `timeout`, `env`,
  `FOO=1`) are skipped. The set is `GATES` in
  `sessiontools.hidden_failure`.
- **The pipeline** is the one whose status reaches the call's exit: the
  last statement's, at top level, carried there by its `&&`/`||` list.
  - `pytest | tail -5 && echo ok` counts.
  - `pytest | tail -5; echo done` and `pytest | tail -5 || echo x` do not.
  - A pipe inside `$( )`, a loop or `if` does not count.
- **Not a pipe:** `||`, a `|` inside quotes, and anything in a
  heredoc body.
- **The failure markers** are ST-305's, each with a label: a pytest
  `N failed`/`N errors` line or a `FAILED`/`ERROR` line start
  (`pytest_failed`), `make: ***` (`make_error`), a Python traceback
  (`traceback`), a `fatal:`/`error:` line start
  (`fatal_or_error_prefix`), `command not found` or `No such file or
  directory` (`not_found`), `Permission denied` (`permission_denied`),
  and `nothing to commit` (`git_nothing_to_commit`). Their precision has
  not been audited; `not_found` can match a green run.
- **A command that names `pipefail`** gets nothing. There the pipe keeps
  the gate's status, so a note would be wrong.

## How it knows the call exited 0

Measured on Claude Code 2.1.325 (ST-319 do-001, by reading the shipped
code):

- PostToolUse fires only for a Bash call that Claude Code counts as a
  success. A failed call fires PostToolUseFailure instead. This hook
  never registers on PostToolUseFailure, because a failed call is
  already visible.
- The success path can still carry a non-zero exit, or no exit yet. The
  note is refused when any of these is true:
  - `tool_response` is not an object;
  - `interrupted` is true;
  - `returnCodeInterpretation` is set (the last command was, for
    example, a `grep` that exited 1);
  - `backgroundTaskId` is set.
- A key of an unexpected type counts as an unknown status, and unknown
  is not 0. No `Exit code N` line is read.
- If a later Claude Code changes this, the hook tests pin the 2.1.325
  shape with synthetic payloads, so the change shows up as a failing
  test.

## Privacy

- The line names the gate (its stage name), the reader (`head` or
  `tail`) and the marker's label. It quotes no output and no argument.
- Nothing is logged or written to disk.

## It never breaks a turn

- No python, a python older than 3.11, bad JSON or a crash all exit 0
  with no output. The shim's stderr goes to `/dev/null`.
- The python is the other hooks' (`hooks/pick-python.sh`):
  `$SESSIONTOOLS_PYTHON` when set, else the python in the shebang of
  `command -v session`, else `python3`.
- A subagent's call is not silenced. The note is about the call, and a
  subagent's Bash hides a failure in the same way.

## Speed

It runs on every Bash call on a seat, so it is cheap where it can be:

- **The shell decides most calls.** The shim reads the payload once
  (`cat`) and exits with no python unless the raw text contains a `|`,
  a gate word, `head` (pre) or `head`/`tail` (post), and, for post, a
  failure-marker word. So a green `pytest | tail` starts no python.
- **The module is loaded by file path,** never `import sessiontools`.
  The package's imports cost about 40 ms more, and it imports only the
  standard library.

Measured on ms2 (Apple M4 Max) on Sun 2026-11-15, at commit
`733dd64`. Each case is `sh hooks/hidden-failure` with a payload on
stdin, 51 runs after 3 warm-up runs. The host was under heavy load the
whole time (load average 42 to 46). CPU is the shim's user and system
time, children included. It depends less on the load than wall time
does.

| case | wall median | wall min | CPU median |
| --- | --- | --- | --- |
| post, 10 KB output, `gate \| tail` with failure text: the note | 94.5 ms | 56.8 ms | 40.0 ms |
| post, 10 KB output, `gate \| tail`, green: no python | 21.8 ms | 10.6 ms | 8.6 ms |
| post, no `head`/`tail` in the payload: no python | 22.7 ms | 14.0 ms | 8.1 ms |
| pre, no `head` in the payload: no python | 23.5 ms | 12.7 ms | 7.5 ms |
| post, no seat marker (a person's session) | 15.8 ms | 5.8 ms | 4.7 ms |

- **The 50 ms target** is met on CPU time (40.0 ms) for the one case
  that starts python. On wall time it is met only by the best runs:
  under this load the median is 94.5 ms. In an earlier run in the same
  hour, the best was 35.5 ms. Python's own start-up alone had a median
  of 27 ms under the same load (`python -I -c pass`).
- **The hook's own work** on that 10 KB payload, in process, has a
  median of 0.16 ms. The rest is the shell, python's start and its
  imports.
- The timeout in `hooks.json` is 5 s.

## Details

- It is in the plugin from the next release: see [the plugin][plugin].
- The parser is a port of ST-305's scanner, unchanged, with its 17
  tests (`tests/test_hidden_failure_scan.py`).
- The proposal is `openspec/changes/hidden-failure-note/proposal.md`
  (PROPOSED). It is in the source repository only.

[plugin]: claude-code-plugin.md
