# The session-info hooks (plugin hooks)

Two SessionStart hooks, one shim: `hooks/session-info` (`hooks/hooks.json`,
one entry per verb). **Both are off by default.** Each has its own switch,
and the shim checks it before python starts.

| variable | what it does |
| --- | --- |
| `SESSIONTOOLS_HOOK_TITLE=on` | the title hook runs (also `1`, `true`, `yes`) |
| `SESSIONTOOLS_HOOK_SESSION_INFO=on` | the context hook runs (also `1`, `true`, `yes`) |

Anything else, or unset, leaves a hook off. Set them in the session's
environment, or in a settings file's `env` block.

## The title hook

It sets the session title (the name `/rename` sets, shown in the resume
picker) to the first 8 characters of the session id, for example
`0f0e0d0c`.

- It runs on `startup`, `resume` and `fork`, and when the payload names
  no source. Claude Code does not apply a hook's title on `clear` or
  `compact`, so the hook says nothing there.
- **It never overwrites a title the session already has.** A name from
  `--name`, `/rename` or another hook arrives in the payload as
  `session_title`, and the hook stays silent. So a session that a
  launcher starts with a name keeps that name.

## The context hook

It adds a short block to the session's context before the first prompt:

```text
# Session Info

- **Session ID**: 0f0e0d0c-0b0a-4908-8706-050403020100
- **Working directory**: /path/to/project
- **Source**: startup
- **Transcript**: /path/to/transcript.jsonl
```

- It runs on every source: `startup`, `resume`, `clear`, `compact` and
  `fork`. After a clear, a resume or a fork the id or the transcript may
  have changed, and after a compact the session may need them restated.
- A line whose value is absent or empty is left out, never printed as
  empty. With no source in the payload, there is no Source line. A
  block left with no line at all is not emitted: the hook prints
  nothing rather than a bare heading.
- **On startup it does not repeat the session id that `session start`
  already injected.** The Session ID line is left out on `startup`, or
  when the payload names no source, if `SESSIONTOOLS_LAUNCH_INFO` equals
  the payload's session id. `session start` exports that variable when
  the launch-info it injected carries the session id, and unsets an
  inherited one otherwise: with no `launch-info` in the agent's context,
  on `--idle`, on a resume, when the id is minted in a tmux pane, and
  when `launch-info` failed. On every other source the line is printed,
  even when the values are equal: after a compact, launch-info may have
  been summarised away while the id stayed the same.
- The values are compared, not the presence of the variable. After
  `/clear` the session has a new id, and a `claude` started from the
  session's shell inherits the variable but has its own id. In both
  cases the values differ and the line is printed.
- launch-info and this block are complementary. launch-info carries the
  launcher's facts (date, machine, agent, voice); this block carries the
  harness's (working directory, source, transcript path), for any
  session, whoever launched it.

## Both hooks

- A subagent's payload (`agent_id`) is silent. A `claude --agent`
  session carries `agent_type` without `agent_id`, and the hooks run
  there.
- **They never break a session.** No python3, python older than 3.11,
  bad JSON on stdin or a crash all end with no output and exit 0.
- **The hooks' python** is the other hooks' (`hooks/pick-python.sh`):
  `$SESSIONTOOLS_PYTHON` when set, else the python in the shebang of
  `command -v session` (a bare path only), else `python3`.

## Known limitations

- **After a bare `/clear`, the title shows the previous short id.** A
  hook's title counts as a custom title, and a bare `/clear` starts a new
  session that keeps a custom title. Claude Code does not apply a hook's
  title on `clear`, so the hook cannot correct it there. The context
  block carries the new id. A later resume of that session keeps the old
  title too, because a title is never overwritten.
- **A fork may show its parent's short id.** Whether a fork inherits its
  parent's title is Claude Code's behaviour, and it is not measured here.
  If a fork inherits it, the title hook stays silent, by the same rule,
  and the fork shows its parent's short id, as after a `/clear`. If it
  does not, the fork gets its own short id.
- Claude Code ignores a plugin's title if the plugin finished installing
  after SessionStart ran. The next session gets it.

## Details

- The module is `sessiontools.session_info`, standard library only. Its
  pure functions, `title_output(payload, env)` and
  `context_output(payload, env)`, check the switches again, so they are
  safe to call directly.
- It ships unreleased: see [the plugin][plugin].

[plugin]: claude-code-plugin.md
