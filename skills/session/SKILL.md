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

Several agents in one seat? See the sessionkit skill's card, "Several agents in one seat".

- `list`: live sessions, their panes, STATE and CONTEXT; `--pane NAME` for one pane id.
- `get PROP [SEAT]`: one property. The props are id, short, pid, name,
  agent, cwd, status, state, idle, version, pane and model. Bare `get` lists them.
- `context [SEAT]`: how full a window is; never guess yours, run this.
  [What filled it](references/context.md).
- `start [FLAGS] [AGENT] [WORDS...]`: launch from `~/.session/agents/<name>.md`,
  flags BEFORE the agent; `--dry-run` launches nothing, `--list-agents` lists them.
- `settings [SID] [TOOL]`: merged down the cascade, each key with its file.
- `sound landed|given`: the pager's busy-sound signal.
- `operator`: who seats ask and who is privileged.
- `statusline`: the status line.
- `pane-state`: each pane's BUSY / FREE.

These are not here, on purpose:

- Agent mail: `message`, `pager`, `memory` (sessionmail).
- `session send` is disabled: mail the agent.
- `session mail` is retired; it says where each verb went.
