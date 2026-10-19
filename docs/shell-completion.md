# Shell completion: `session completion bash|zsh`

`session completion SHELL` prints a completion script for bash or zsh on
stdout. Install it with one line, and TAB completes `session`'s verbs,
sub-verbs, long flags and the positional words a verb takes from a fixed
table.

```console
# zsh, in ~/.zshrc after compinit:
eval "$(session completion zsh)"

# bash with the bash-completion package (2.x), once; it loads by command name:
session completion bash > ~/.local/share/bash-completion/completions/session

# any bash, 3.2 included, without that package, in ~/.bashrc:
eval "$(session completion bash)"
```

- **One source.** The script is built from the dispatcher's own tables:
  the verbs `session` runs, and each verb's argparse parser. It is never
  built from a second list or from `--help` text. A test fails when the
  two drift apart (`tests/test_completion_drift.py`).
- **Static.** The script holds the whole tree. At TAB it runs no command:
  no Python, no tmux, no transcript read, no network.
- **Regenerate after an upgrade.** The `eval` form regenerates at every
  shell start. The file form is a copy, so rerun it after
  `uv tool upgrade sessiontools`.
- **zsh needs compinit first.** Without `compdef` the script defines
  nothing and says so on stderr. It never runs `compinit` itself.
- Any other shell is a usage error (exit 2). fish is not written yet.

## What TAB offers

- **Verbs:** every verb `session` runs, and `--help`. `mail` and the
  disabled verbs are never offered.
- **Sub-verbs**, including the ones dispatched by hand:
  `sound engine build|on|off|status`, `pane-state show|watch|agent`,
  `pane-state agent on|off|status`, `statusline install|check|sample`
  and `account list|status|use|new|add|remove`.
- **Long flags** at each verb and sub-verb. Short flags are left out.
- **Positional words from a table:** `sound landed|given`, the property
  names after `session get`, the tool names after `session settings SID`
  and the shells after `session completion`. Each is the table the verb
  itself checks against.
- **Directories** after a flag whose value is a directory
  (`session start --directory`, `session sound engine build --dest`).
- **Nothing**, where the word is free-form (a seat, a session id, a name,
  a number), after a flag's value is expected, and after `session start
  AGENT`, where every word is passed on to the agent.

## A broken install

Building the script builds every verb's parser, and `account`'s and
`start`'s packages load PyYAML. If PyYAML will not import,
`session completion` prints no script, names the module on stderr and
exits 1. A script that silently lacked those verbs would be worse than
none.

## Known limits

- **Word candidates can look like files in bash.** The script registers
  with `complete -o filenames`, so directory values get a trailing `/`.
  bash 3.2 has no `compopt` to turn that on only for values. So a verb
  that has the same name as a directory in the current directory
  completes as `name/`.
- **A colon splits a word in bash.** bash's default `COMP_WORDBREAKS`
  splits on `:`. A value such as `12:30` arrives as three words, and
  the words after it may get no candidates.
- **zsh, cursor in the middle of a word:** the script reads the whole
  word, including the part after the cursor. It uses that word only to
  spot `--flag=value`, so little changes.
- **Constraints between flags are not encoded.** Every member of a
  mutually exclusive group is offered, and so is a flag that another
  flag requires, such as `ledger --width` without `--short`. The verb
  refuses the line when it runs.
- **What the tests prove about generation.** Generating the script
  starts no process, opens no socket and writes no file under HOME or
  the working directory. No test blocks file reads; the parsers read no
  files today.
- The script's own variables are local to each TAB, and none stays set
  in your shell. Its functions are all named `_session*`.
