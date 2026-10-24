# session

`session` is one command for the live coding-agent sessions on this
machine: it lists them, reads one seat's properties and how full its
context window is, launches a new one, and runs the per-host parts around
them (the busy sound, the status line, the pane signal, the account
switch, closing idle seats and saying a seat has settled).

- `session` alone, or `session --help`, lists the verbs.
- `session <verb> --help` has each verb's flags.
- sessiontools ships it; see [whose command this is][owner].

## The verbs

- `list`: the live sessions and their tmux panes
- `get`: one property of a live seat
- `context`: how full a seat's context window is, and what filled it
- `ledger`: every file a session read and every command it ran, with the
  tokens each put in the window
- `start`: launch a session from an agent file; see [session start][start]
- `settings`: a session's settings, each key with the file that set it
- `account`: which Claude account this Mac is on, and the switch; see
  [switching accounts][account]
- `sound`: the busy sound's signals, and the resident sound engine; see
  [the busy sound][busy] and [the sound engine][engine]
- `operator`: who seats ask and who is privileged, one line of JSON; see
  [operator and privileged seats][operator]
- `statusline`: Claude Code's status line, from one source in
  sessiontools; see [the status line][status-line]
- `pane-state`: each seat's BUSY / FREE / UNKNOWN on its tmux pane; see
  [the pane signal][pane-state]
- `default-model`: pin the default every NEW claude starts on, opt-in by
  the `model.default` setting; unset, it does nothing; see
  [the default model][default-model]
- `reap`: every seat idle past a limit, CLOSE or KEEP and why; see
  [closing idle seats][reap]
- `park`: say this seat has settled, or read that back; see
  [saying a seat has settled][park]
- `completion`: a completion script for bash or zsh; see
  [shell completion][completion]

## Each verb's form and one example

- `session list [--json] [--no-context] [--pane NAME] [--fresh]
  [--no-recap]`: the live sessions, one row each.

  ```sh
  session list --no-context
  ```

- `session get [PROP [SEAT]] [--json]`: one property of a live seat;
  bare `session get` lists the properties.

  ```sh
  session get model
  ```

- `session context [SEAT] [--peak] [--max N]
  [--parts|--startup|--boot|--hooks] [--tools] [--json]`: the tokens in
  the window now; `--parts`, `--startup`, `--boot` and `--hooks` say what
  filled it, and `--tools` splits `--parts` by tool.

  ```sh
  session context --parts --tools
  ```

- `session ledger [SEAT] [--summary|--short|--long|--json] [--width N]`:
  `--summary` is the default; `--short` is one line per item, largest
  first.

  ```sh
  session ledger --short
  ```

- `session start [AGENT] ...`: launch a session from an agent file, or a
  plain vendor session with no agent named. `--list-agents` prints the
  agent names it can resolve.

  ```sh
  session start --list-agents
  ```

- `session settings [SID] [TOOL] [--agent NAME] [--seat NAME] [--json]`:
  this session's settings by default.

  ```sh
  session settings --json
  ```

- `session account list | status | use NAME | new NAME | add NAME |
  remove NAME`: `use` moves every seat with no env token, and writes only
  with `--witness WHO` and `--yes`; `new` signs in through
  `claude setup-token` and saves the token to passage, never showing it.

  ```sh
  session account status
  ```

- `session sound landed|given --session SID`: the signals the pager sends
  for the busy sound. `session sound engine build|on|off|status` builds
  and switches the resident sound engine's launchd agent.

  ```sh
  session sound engine status
  ```

- `session operator`: prints `{"operator", "mailbox", "privileged"}`.

  ```sh
  session operator
  ```

- `session statusline install|check|sample`: `check` reads only, and says
  what the kit layer holds and what overrides it.

  ```sh
  session statusline check
  ```

- `session pane-state show [--json]`: each seat's signal, read once,
  nothing written. `session pane-state watch [--interval S] [--once]
  [--dry-run] [--stats]` is the publisher, one per host, writing
  `@session_state` on each pane. `session pane-state agent on|off|status`
  is the launchd agent that runs the publisher; `on` is the deploy.

  ```sh
  session pane-state show
  ```

- `session default-model [show [--json] | list [--json] | enforce
  [--dry-run] | watch [--interval S] | unit launchd|systemd]`: `show` (the
  default) reads only, and says whether `enforce` would change
  settings.json's `"model"`. `list` names the models a pin may use; Fable
  is listed and never pinned. `watch` enforces in the foreground on every
  change; `unit` prints a launchd agent or systemd units that do it, and
  installs nothing ([the default model][default-model]).

  ```sh
  session default-model show
  ```

- `session reap [--idle DURATION] [--json] [--yes] [SHORT ...]`: the
  default limit is 1h. A dry run unless `--yes`; CLOSE only when the
  seat's task is done.

  ```sh
  session reap --idle 2h
  ```

- `session park [--letter REF] [--no-promises | --promise TEXT ...
  [--handed-on REF] | --handed-on REF] [--keep-until T --why R] [--json]`:
  runs five checks, writes `park.json` and prints what they found; run it
  as a call of its own. `session park --check [SEAT] [--json]` reads it
  back for this seat or any live seat and writes nothing.

  ```sh
  session park --check
  ```

- `session completion bash|zsh`: the script on stdout, built from these
  verbs' own parsers. In `~/.zshrc`, after compinit:

  ```sh
  eval "$(session completion zsh)"
  ```

## Gone on purpose

- `session send`: disabled 2030-12-08 (it typed into an agent's pane by
  accident). It exits 2 and says to mail the agent instead
  (`message send`). The code stays in `send.py`, unwired.
- `session mail ...`: retired. It exits 2 and names where each verb
  went (`memory`, `message`, `pager`).

## Whose command this is

Since 2030-06-09 sessiontools owns `session`: it took the command over
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
- TOOL picks one tool (`mail`, `session`, `ears`, `mouth`, `sounds`,
  `claude-code`, `view`, `phone`); with none, every tool found.
- `--agent NAME` and `--seat NAME` read another layer than the
  session's own record.
- A tool whose files have not moved to the new paths reads "not moved".

[owner]: #whose-command-this-is
[start]: session-start-launcher.md
[account]: account.md
[busy]: busy-sound-hook.md
[engine]: sound-engine-agent.md
[operator]: operator-privileged-seats.md
[status-line]: status-line-script.md
[pane-state]: pane-state-signal.md
[default-model]: default-model.md
[cache]: list-snapshot-cache.md
[park]: session-park.md
[reap]: session-reap.md
[completion]: shell-completion.md
