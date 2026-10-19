# The `session` verbs

`session <verb>`: one command for the live coding-agent sessions on this
machine. `session` alone, or `session --help`, lists the verbs;
`session <verb> --help` has each verb's flags.

- `session list [--json]`: the live sessions and their tmux panes
- `session get [PROP [SEAT]] [--json]`: one property of a live seat
- `session context [SEAT] [--peak] [--max N]
  [--parts|--startup|--boot|--hooks] [--tools] [--json]`: how full a
  seat's context window is; `--parts`, `--startup`, `--boot` and
  `--hooks` say what filled it, and `--tools` splits `--parts` by tool
- `session ledger [SEAT] [--summary|--short|--long|--json] [--width N]`:
  every file a session read and every command it ran, each with the
  tokens it put in the window; `--summary` is the default
- `session account list | status | use NAME | new NAME | add NAME |
  remove NAME`: which Claude account this Mac is on, and the switch.
  `use` moves every seat with no env token; `new` signs in through
  `claude setup-token` and saves the token to passage, never showing it;
  see [switching accounts][account]
- `session start ...`: launch a session from an agent file; see
  [session start][start]
- `session settings [SID] [TOOL] [--json]`: a session's settings, each
  key with the file that set it
- `session sound landed|given --session SID`: the busy sound's signals;
  see [the busy sound][busy]
- `session sound engine build|on|off|status`: the resident sound
  engine's launchd agent; see [the sound engine][engine]
- `session operator`: who seats ask and who is privileged, one line of
  JSON; see [operator and privileged seats][operator]
- `session statusline install|check|sample`: Claude Code's status line,
  one source in sessiontools; see [the status line][status-line]
- `session pane-state show [--json]`: each seat's BUSY / FREE / UNKNOWN
  and its pane, read once, nothing written; see [the pane signal][pane-state]
- `session pane-state watch [--interval S] [--once] [--dry-run] [--stats]`:
  the publisher, one per host, writing `@session_state` on each pane
- `session pane-state agent on|off|status`: the launchd agent that runs
  the publisher, one per host; `on` is the deploy. See
  [the pane signal][pane-state]
- `session reap [--idle DURATION] [--json] [--yes] [SHORT ...]`: every
  seat idle past a limit (default 1h), CLOSE or KEEP and why. A dry run
  unless `--yes`; CLOSE only when the seat's task is done; see
  [closing idle seats][reap]
- `session park [--letter REF] [--no-promises | --promise TEXT ...
  [--handed-on REF] | --handed-on REF] [--keep-until T --why R] [--json]`:
  this seat has settled. Runs five checks, writes `park.json` and prints
  what they found; run it as a call of its own. `session park --check
  [SEAT] [--json]` reads it back for this seat or any live seat and writes
  nothing; see [saying a seat has settled][park]
- `session completion bash|zsh`: a completion script for that shell, on
  stdout, built from these verbs' own parsers; see [shell completion][completion]

Gone on purpose:

- `session send`: disabled 2030-07-30 (it typed into an agent's pane by
  accident). It exits 2 and says to mail the agent instead
  (`message send`). The code stays in `send.py`, unwired.
- `session mail ...`: retired. It exits 2 and names where each verb
  went (`memory`, `message`, `pager`).

## Whose command this is

Since 2030-07-29 sessiontools owns `session`: it took the command over
when the session repo was retired. The verbs are ported onto the
library, not copied, and `tests/test_cli.py` pins it.

## `session list`

- The live sessions and their tmux panes, with the JSON keys and table
  columns its callers were written against, CONTEXT included
  (`sessiontools.window`).
- A STATE column and a `state` key: `busy`, `idle with children` or
  `idle`. STATUS and `status` keep the harness's own word.
- `--no-context` skips CONTEXT (a transcript read per seat): `-` cells,
  null keys.
- `--pane NAME` prints the pane id of the session matching NAME, or
  exits 1 when none does.
- `--fresh` computes now instead of reading the shared snapshot.
- `--no-recap` is accepted. The /recap sub-line is not ported yet, and
  its key is null.
- It computes once for every caller: see [the list cache][cache].

## `session get`

- Bare `session get` lists the properties: `id`, `short`, `pid`,
  `name`, `agent`, `cwd`, `status`, `state`, `idle`, `version`, `pane`
  and `model`.
- `model` reads the transcript (the last real model id). The rest come
  from the live listing, read fresh each call, never from the cache.
- No SEAT means this session, from the environment.
- Exit 0 found; 1 unknown PROP; 2 no such seat, or no value for PROP.

## `session context`

- The tokens in the window now, of the model's window. `--peak` gives
  the session's high-water mark instead.
- `--max N` asserts the window size. `$CONTEXT_MAX` still tightens a
  known window.
- An unknown or exceeded window prints `?`, and the JSON carries `max`,
  `pct` and `free` as null.
- A transcript with no usage rows is exit 1.
- SEAT is a live seat's id prefix, then the `session get` name rule,
  then any session id prefix among the transcripts. With no SEAT and no
  session in the environment, it reads the newest transcript of the
  current directory's project.
- The views say what filled the window. Each reads the transcript a
  second time; the one-line default never does. At most one per run:
  - `--parts`: by category. AT BOOT (the first call's window, exact),
    then SINCE BOOT (every call's growth, exact), split into own output
    (exact), tool results, hook context and the rest (fitted per
    session), and Unattributed, what the fit misses. `--tools` adds
    Tool results by tool, with the 5 largest results.
  - `--startup`: only the boot, before the first reply.
  - `--boot`: the boot plus the reads up to SETTLED: for an agent, the
    last call before its first act on work; for a human session, the
    last call before the first typed input. It prints which signal
    fired.
  - `--hooks`: hook context by hook command, in chars, with how much
    of it repeats.
- Every figure names its unit (`tok`, `chars`). `~` marks a fitted
  figure; `?` is unknown, never a guess.
- `--json` with a view prints that view as one JSON document, each row
  with `exact` and `fitted`; `--parts --json` carries `band` (`ok`,
  `near`, `over`, or null with no settle threshold).
- The bare `--json` carries `band` too, without the `--parts` fold.
- `--peak` belongs to the one-liner and `--max` to the one-liner and
  `--parts`; with another view either is refused (exit 2).

## `session settings`

- The settings of session SID (default: this session), merged down the
  cascade (template, host, agent, seat, session), each key with the
  file that set it.
- TOOL picks one tool (`mail`, `session`, `ears`, `mouth`, `sounds`);
  with none, every tool found.
- `--agent NAME` and `--seat NAME` read another layer than the
  session's own record.
- A tool whose files have not moved to the new paths reads "not moved".

[start]: session-start-launcher.md
[account]: account.md
[busy]: busy-sound-hook.md
[engine]: sound-engine-agent.md
[operator]: operator-privileged-seats.md
[status-line]: status-line-script.md
[pane-state]: pane-state-signal.md
[cache]: list-snapshot-cache.md
[park]: session-park.md
[reap]: session-reap.md
[completion]: shell-completion.md
