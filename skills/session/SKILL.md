---
name: session
description: >-
  The live coding-agent sessions on this machine: list them, read a
  seat's properties, start one from an agent file. Use when you
  need to know who is running, find a seat's pane or session id, check
  your own context, read a session's settings, or launch an agent.
  `session` IS on the PATH; mail between agents is `message`.
---

# session

`session <verb> --help` has the flags.

- `list`: the live sessions, their panes, STATE and CONTEXT;
  `--pane NAME` for one pane id.
- `get PROP [SEAT]`: one property. The props are id, short, pid, name,
  agent, cwd, status, state, idle, version, pane and model. Bare `get` lists them.
- `context [SEAT]`: how full a window is. Never guess yours: run this.
  - What filled it: [references/context.md](references/context.md).
- `start [FLAGS] [AGENT] [WORDS...]`: launch from
  `~/.session/agents/<name>.md`. Flags BEFORE the agent name. `--dry-run`
  launches nothing; `--list-agents` lists them.
- `settings [SID] [TOOL]`: settings merged down the cascade, each key with
  the file that set it.
- `sound landed|given`: the pager's busy-sound signal.
- `operator`: who seats ask and who is privileged.
- `statusline`: the status line.
- `pane-state`: each pane's BUSY / FREE.

These are not here, on purpose:

- Mail between agents is `message`, `pager` and `memory` (sessionmail).
- `session send` is disabled; mail the agent instead.
- `session mail` is retired, and says where each verb went.
