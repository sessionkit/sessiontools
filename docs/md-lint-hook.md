# The Markdown lint (a plugin hook)

After a `Write`, `Edit` or `MultiEdit` of a `.md` file, the plugin's
`hooks/md-lint` (PostToolUse, `hooks/hooks.json`) names the lines that
break two rules. It answers and never blocks: the write has already
happened, and the agent reads the lines as context.

```text
md-lint /home/me/project/docs/guide.md:
line 14 is 134 columns; a table line stays under 76 columns, wider lines wrap
line 9 says 'shape': use the precise term
```

It is on by default, only in a session `session start` launched.

| variable | what it does |
| --- | --- |
| `SESSIONTOOLS_HOOK_MD_LINT=off` | off (or `0`, `false`, `no`) |

Anything else, unset included, is on. Off exits before python starts.

## The two rules

- **A wide table line.** A line whose text, leading spaces stripped,
  starts with `|`, and is 76 or more display columns wide. An editor
  window of 80 columns wraps it, and a wrapped table is hard to read.
  Width is display columns, not bytes: East Asian wide and fullwidth
  characters count 2, combining marks 0, everything else 1. A tab
  counts 1; tables rarely carry tabs.
- **The word `shape`.** The whole word `shape` or `shapes`, in any case,
  in prose. It is a vague word; the hook asks for the precise one.
  `reshape`, `shaped`, `shaping` and `misshapen` are other words and
  are not named. Table cells are prose.

## What is skipped

For both rules:

- YAML front matter: a `---` line at line 1, to the next `---` or
  `...`. With no closing line there is no front matter.
- Fenced code, with ```` ``` ```` or `~~~`, closed by a run of the same
  character at least as long. An unclosed fence runs to the end.
- HTML comments, on one line or many.

For the `shape` rule, also:

- inline code spans;
- link text and link targets: `[text](target)`, `![alt](src)`,
  `[text][ref]`, `[text][]`, `<https://...>`, and bare
  `http://` or `https://` URLs;
- reference definitions, `[ref]: url`, the whole line;
- quoted text: `"..."`, curly double quotes and curly single quotes, on
  one line. Straight single quotes are not skipped: they are
  apostrophes too often;
- blockquote lines, `>` after up to three spaces.

## What is reported

- **A `Write`** reports every finding in the file.
- **An `Edit` or `MultiEdit`** lints the whole file as it is on disk
  after the edit, so it knows which lines sit in a fence or in front
  matter. It reports only lines where an edit's `new_string` now sits:
  every place that text appears, which covers `replace_all`. An edit
  that deletes (an empty `new_string`) reports nothing. Lines the edit
  did not touch are not named, so an old wide table is not repeated on
  every edit.
- One line per rule per line: two `shape`s on one line are one finding.
- One header, `md-lint <path>:`, where the path is the tool's
  `file_path` exactly as given (usually absolute). Then at most 10
  finding lines, then `... and N more` when there are more.
- No finding, no output.

## It never breaks a turn

- It never blocks: no `decision`, never exit 2. The output is only
  `hookSpecificOutput.additionalContext`.
- No python, python older than 3.11, bad JSON, a file that cannot be
  read, or a crash: exit 0, no output, nothing on stderr.
- **Birth-only**, as [the settle alert][settle]: the shim needs
  `CLAUDE_CODE_SESSION_ID` and that session's folder under the
  sessions leaf. A session `session start` did not launch is silent.
- **Cheap.** A payload with no `.md"` in it (any case) exits in the
  shell, so a `Write` of any other file starts no python. Python makes
  the exact decision.
- **The hook's python** is the other hooks' (`hooks/pick-python.sh`):
  `$SESSIONTOOLS_PYTHON` when set, else the python in the shebang of
  `command -v session`, else `python3`.

## Speed

`sh hooks/md-lint` with a payload on stdin, 20 runs per case after 3
warm-up runs, on an Apple Silicon Mac under load.

| case | median | min | max |
| --- | --- | --- | --- |
| off | 11.0 ms | 8.5 ms | 19.5 ms |
| not a `.md` file | 25.7 ms | 13.7 ms | 40.7 ms |
| a `.md` file of 2,001 lines | 135.1 ms | 90.0 ms | 210.6 ms |

The hook's timeout in `hooks.json` is 5 s.

## Details

- The variable comes from the Claude Code process environment. A
  settings `env` block reaches the hook too.
- `.markdown` and `.mdx` files are not linted.
- It is in the plugin from the next release: see [the plugin][plugin].
- The code: `sessiontools.md_lint`. `lint(text)` is pure and returns
  each finding with its line, rule and message.

[settle]: settle-alert-hook.md
[plugin]: claude-code-plugin.md
