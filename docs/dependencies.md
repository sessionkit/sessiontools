# Dependencies

This page lists sessiontools' direct dependencies and the tools a user
meets: what needs each one, whether you need it, how it gets onto your Mac,
and how you upgrade it. The README's [requirements][readme-requirements]
link here. The full resolved set, every package at the version this repo
is tested with, is in [`uv.lock`][uv-lock].

- **Required:** macOS, Claude Code, uv and Python 3.11 to 3.14.
  `uv tool install sessiontools` brings its two Python packages, PyYAML
  and jsonschema.
- **Optional:** Apple's Command Line Tools (`git` for `session reap`,
  `swiftc` for the sound engine), jq for the status line, passage or pass
  for secrets, tiktoken's vocabulary for `session ledger`, and a few tools
  single features use.
- **Pending a ruling:** Homebrew and tmux. Whether they are required is
  not decided yet; their entries say what uses each one today
  ([pending a ruling][pending]).
- **Upgrade:** `uv tool upgrade sessiontools` for the command and its
  Python packages, `claude plugin update sessiontools@sessiontools` for
  the plugin ([upgrade][upgrade]).

Required means a default command fails without it. Optional means a
feature you turn on needs it, or a command still works without it but
does less. sessiontools uses neither notmuch nor ffmpeg.

## Required

| Dependency | What needs it | How you get it | How you upgrade it |
|---|---|---|---|
| macOS | Everything. Tested on macOS 27 on Apple silicon. The pane signal's agent and the sound engine are macOS only. | Your Mac. | Software Update. |
| [Claude Code][claude-code] | `session start`, which runs `claude`; `session account new`, which runs `claude setup-token`; `session account use`, which checks a token with `claude -p` before it writes. | Its installer. | Claude Code's own update. |
| [uv][uv] | `uv tool install sessiontools`, the install; `session ledger`, which counts tokens through `uv run`. | The uv installer, or Homebrew. | `uv self update`; a Homebrew copy upgrades with Homebrew. |
| Python 3.11 to 3.14 | The `session` command runs on it. | `uv tool install sessiontools` picks one, and fetches it when the Mac has none. | It stays with the install: `uv tool upgrade sessiontools` keeps it. |
| PyYAML 6 or later | Agent files: `session start` and the agent layer of `session settings`; `session reap`, which reads a task's front matter. | `uv tool install sessiontools`. | `uv tool upgrade sessiontools`. |
| jsonschema 4 or later | Checking settings against each tool's schema: `session settings`, and `session start` as it reads them. | `uv tool install sessiontools`. | `uv tool upgrade sessiontools`. |

`import sessiontools`, the library, needs only the standard library: the
two packages load only inside the commands that use them.

## Optional

| Dependency | What needs it | Without it | How you get it | How you upgrade it |
|---|---|---|---|---|
| `git`, from Apple's Command Line Tools | `session reap`, which reads each seat's unsaved work with `git status`. `session start`, which checks that its launch record is ignored by any repo it lands in. | `session reap` reads the work as unreadable and keeps every seat. | `xcode-select --install`, or Homebrew's installer, which brings them. | Software Update. |
| `swiftc`, from Apple's Command Line Tools | `session sound engine on` and `build`, which build the sound engine. | They refuse, "no swiftc on PATH", and change nothing; the busy sound plays with `afplay`. | The same. | Software Update. |
| jq | The status line (`session statusline install`). | The line reads `statusline: jq not found`. | macOS 27 ships `/usr/bin/jq`. | Software Update. |
| passage | `session account new`, which saves the token there; an agent file's `secrets:` with the `passage` backend. | `account new` refuses before any sign-in, "passage is not installed"; a `passage` secret aborts the launch. | Your package manager, such as Homebrew. | The same package manager. |
| pass, with `gpgconf` | An agent file's `secrets:` with the `pass` backend. | The launch aborts, "`pass` is not installed". | Your package manager, such as Homebrew; `gpgconf` comes with GnuPG. | The same package manager. |
| tiktoken and its `cl100k_base` vocabulary | `session ledger`, which counts tokens with them. | `session ledger` exits 2 and says what is missing: it never downloads. | Run the `tokens` command once on the Mac: it fills uv's and tiktoken's caches. Or set `TIKTOKEN_CACHE_DIR` to a directory that holds the vocabulary. | No version is pinned: the counter takes the tiktoken uv's cache holds. Run `tokens` again to refresh it. The vocabulary does not change. |
| `ccusage-limits-tap` | The status line's plan-usage gauge ([the usage tap][tap]). | The line skips it. | skillbox's `ccusage` skill. | Its own update. |
| `mouth`, from [sessionvoice][pkg-voice] | The stop sound, which it queues behind any line still speaking. | `afplay` plays the sound. | `uv tool install sessionvoice`, or the kit. | `uv tool upgrade sessionvoice`. |
| `pager`, from [sessionmail][pkg-mail] | `session start`, which runs `pager settings install` first, so a new session reads mail's settings schema. | The step is skipped; the launch goes on. | `uv tool install sessionmail`, or the kit. | `uv tool upgrade sessionmail`. |
| `python3` 3.11 or later | The plugin's hooks, but only when they cannot find the interpreter `uv tool install` gave `session`. | Each hook exits 0 and does nothing, as it does on Apple's 3.9. | The tool's own Python comes first; else `python3` on PATH. | With `session`: `uv tool upgrade sessiontools`. |

macOS itself brings the rest: `security` for `session account use` on a
Keychain host, `afplay` for the sounds, `launchctl` for the two launch
agents, `lsof` for the sound engine's port, and `ps` for reading the
processes behind each session.

## Pending a ruling

Whether these two are required is not decided yet, so this page gives
them no class and no install step. Each entry says what uses it today,
what happens without it, and where it comes from today.

### [Homebrew][brew]

Status: pending a ruling. It hangs on the ruling for tmux below.

- **What uses it:** no sessiontools command runs `brew`. It is where tmux
  comes from today, and its installer brings Apple's Command Line Tools.
- **Without it:** the Command Line Tools come from `xcode-select
  --install` instead.
- **Where it comes from today:** its installer, from its own site.

### [tmux][tmux]

Status: pending a ruling.

- **What uses it:** `session start --in-tmux` and `session start
  --tmux-target`; `session reap --yes`, which closes a seat by closing its
  tmux pane; `session pane-state agent on`, the pane signal's agent,
  which sets BUSY and FREE on each seat's pane.
- **Without it:** `session start --in-tmux` stops with "tmux is not
  installed (or not on PATH)"; `session reap --yes` closes nothing: a
  seat with no pane is kept, and so is one whose panes cannot be read;
  `session pane-state agent on` refuses, "no
  tmux on PATH", and changes nothing. `session list` still works, with no
  pane for each session.
- **Where it comes from today:** Homebrew.

## Upgrade

```bash
uv tool upgrade sessiontools
claude plugin update sessiontools@sessiontools
```

- **The first line** upgrades `session` and its Python packages, and keeps
  the Python the install chose. Installed through Session Kit, `uv tool
  upgrade sessionkit` does it instead.
- **The second line** upgrades the plugin, if you installed it. A plugin
  installed from a checkout is read from that folder: pull the checkout,
  then start a new session ([the plugin][plugin]).

[readme-requirements]: ../README.md#requirements
[uv-lock]: ../uv.lock
[upgrade]: #upgrade
[pending]: #pending-a-ruling
[tap]: status-line-script.md#the-usage-tap-what-where-how-long
[plugin]: claude-code-plugin.md#which-release-has-what
[brew]: https://brew.sh
[claude-code]: https://claude.ai/code
[uv]: https://docs.astral.sh/uv/
[tmux]: https://github.com/tmux/tmux
[pkg-mail]: https://github.com/sessionkit/sessionmail
[pkg-voice]: https://github.com/sessionkit/sessionvoice
