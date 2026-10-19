# session context

How full a seat's window is, and what filled it. `session context --help`
has every flag. Never guess your context: run this.

## The one-liner (the default)

```bash
session context            # this session
session context c3a7       # a live seat, or a session id prefix
session context --peak     # the high-water mark instead of now
```

It prints one line: `Context: 45K / 1.00M (4%) · 954K free`. It is cheap,
and it never reads the transcript a second time. Reach for it first.

The window is the model's. When the model's window is unknown or exceeded,
the line says `?`, never a guess. `--max N` asserts the window size in
tokens when you know it.

## What filled it: the views

Each view reads the transcript a second time, so ask for one only when the
number alone is not enough. Pick one view at a time.

- `--parts`: the window by category. First AT BOOT (the core prompt and
  tools, memory files, the skills index, the brief, hook context), then
  SINCE BOOT (your own output, tool results, hook context, mail). The
  Unattributed row is what the split does not explain.
- `--parts --tools`: also splits "Tool results" by tool and names the 5
  largest single results. `--tools` works only with `--parts`.
- `--startup`: only the boot, what was in the window before the first
  reply.
- `--boot`: the boot plus the reads up to SETTLED. For an agent, SETTLED is
  the last call before its first act on work (its first mail read or slice
  claim); for a human session, the last call before the first typed input.
- `--hooks`: hook context by hook command, in chars: each hook's share,
  and how much of it repeats.

How to read the rows: every number is tokens (tok) unless it says chars.
`~` marks a fitted figure, an exact total split by a fit or counted at a
rate. `?` is unknown. A row with no mark is exact.

## Machine-readable: `--json`

```bash
session context --json
session context --parts --json | jq '.rows[] | select(.fitted)'
```

`--json` prints the same numbers as JSON. The rows are in the views, not in
the bare `--json`: `--parts`, `--startup`, `--boot` and `--hooks` with
`--json` carry `.rows[]`, with `exact` or `fitted` on each row, and the
bare `session context --json` has no rows. An unknown window is `null`,
never a guess.

The one-liner's JSON and `--parts --json` carry `band`: how close the
window is to the seat's settle threshold (`SESSION_SETTLE_AT`).

- `over`: at or past the threshold.
- `near`: inside the near band, which starts `context.near_pct` percent
  (default 20) below the threshold.
- `ok`: below the near band.
- `null`: no threshold is set, or the count is unknown.

So with a threshold of 250K and the default 20, `near` starts at 200K.
`context.near_pct` is a setting of the `session` tool, read down the
settings cascade; `session settings` shows where it was set. To change it
for one session:

```json
{"context": {"near_pct": 10}}
```

in `~/.session/sessions/<sid>/settings/session.json`.
