"""`commands.shell`: a Bash command cut down to the programs it runs (ST-320).

Each case is a kind of command the reader met in a week of four seats' Bash
calls (36,904 of them), where it was measured against bashlex: the two named
the same programs in 99.80% of the calls bashlex could read, and the cases
marked MEASURED are where they differed and the reader was right. The
commands here are made up; no transcript's text is in this file.
"""

from __future__ import annotations

import pytest

from sessiontools.commands import shell

CASES = [
    # the settings in front, a redirection, a pipe
    ("FOO=1 BAR=2 git status 2>&1 | head -3", ["git status", "head"]),
    ("echo hi>out; echo 2>&1 a >>log &>all <in", ["echo", "echo"]),
    # a here-document's body is input: a quoted one, a plain one, two on a line
    ("cat <<'EOF'\n$(rm -rf /) `x` ; not a command\nEOF\necho done", ["cat", "echo"]),
    ("cat >>a <<EOF || true\nEOF\ncat >>b <<EOF\n- a line; ls\nEOF", ["cat", "true", "cat"]),
    ("python3 - <<'PY'\nprint(1)\nPY", ["python3 (inline)"]),
    ('git commit -m "$(cat <<\'EOF\'\nfix: it\'s (done)\nEOF\n)" && git push',
     ["git commit", "cat", "git push"]),
    # $VAR as the program, set in the same call; never one that holds a substitution
    ('g="git -C /x"; $g rev-parse HEAD', ["git rev-parse"]),
    ("s=$(ls d | head -1); bash \"$s\"", ["ls", "head", "$s"]),
    # a function the call defines: its body once, its calls never
    ("f() { echo hi; }; f; f", ["echo"]),
    ("function nm1 { notmuch search x | head -1; }\nj=$(nm1 a); nm1 b", ["notmuch search", "head"]),
    # the three wrappers that are followed, and the ones stepped over
    ("ssh -o X=y ms1 'tail -1 f | jq .'", ["ssh", "tail", "jq"]),
    ("uv run --quiet --with bashlex python3 script.py a", ["script.py"]),
    ("uvx --from pkg tool run", ["tool"]),
    ("bash -c 'cd /x && make test'", ["cd", "make test"]),
    ("ssh h 'bash -lc \"tmux ls; pgrep -lf x | head -3\"'", ["ssh", "tmux ls", "pgrep", "head"]),
    ("timeout 5 env A=1 nohup git log", ["git log"]),
    # a wrapper's option takes a value: the value is not the program (the egg review, M1)
    ("sudo -u root ls", ["ls"]),
    ("sudo -iu root ls; sudo -uroot ls; sudo --user=root ls", ["ls", "ls", "ls"]),
    ("timeout -s KILL 30 make", ["make"]),
    ("timeout --kill-after 5 30s make test; timeout 30s make test", ["make test", "make test"]),
    ("env -u FOO python3 x.py", ["x.py"]),
    ("env -i PATH=/bin nice -n 5 stdbuf -o L git log", ["git log"]),
    ("/usr/bin/time -f %e -o t.out ./tool a; exec -a name ./tool", ["tool", "tool"]),
    ("sudo -n true", ["true"]),
    # `command -v` looks a name up; plain `command` runs it
    ("command -v git >/dev/null && command git status", ["command", "git status"]),
    # ... and so does -v in a cluster or after -p (ST-324, D2); plain -p still runs it
    ("command -pv git", ["command"]),
    ("command -p -V git; command -Vp git", ["command", "command"]),
    ("command -p git status; command -- git status", ["git status", "git status"]),
    # a substitution: inside double quotes, beside a quote (MEASURED), nested, backticks
    ('echo "n: $(grep -c x f | wc -l)" 2>&1', ["echo", "grep", "wc"]),
    ("notmuch search 'date:@'$(date +%s)'..' | head", ["notmuch search", "date", "head"]),
    ("ls $(dirname $(readlink -f $(which x)))/..", ["ls", "dirname", "readlink", "which"]),
    ("echo `date` \"`whoami`\"", ["echo", "date", "whoami"]),
    ("GH_TOKEN=$(gh auth token) gh run list", ["gh run list", "gh auth token"]),
    ("message mark cora $(seq 1 3) 42751 done | tail -2", ["message mark", "seq", "tail"]),
    ("diff <(sort a) <(sort b) | wc -l", ["diff", "sort", "sort", "wc"]),
    # what a single quote holds is text (MEASURED: `x` in a quoted sentence)
    ("S='see `git config x` and $(date)'; echo \"$S\"", ["echo"]),
    # the shell's own words
    ("for s in a b; do echo \"$s: $(session get $s model)\"; done", ["echo", "session get"]),
    ("for ((i=0;i<3;i++)); do x=$((i+1)); done; a=(1 2 $(id -u)); (cd /t && ls) > o",
     ["id", "cd", "ls"]),
    ("case $x in a|b) echo one ;; *) ls ;; esac; date", ["echo", "ls", "date"]),
    ("x=$(case $y in a) echo 1;; esac); echo $x", ["echo", "echo"]),
    ("if [[ -f x && -n \"$(cat x)\" ]]; then git log; elif ! true; then :; fi",
     ["[[", "cat", "git log", "true", ":"]),
    ("[[ $a =~ ^(x|y)$ ]] && echo yes || echo no", ["[[", "echo", "echo"]),
    ("time (for s in a b; do ./tool $s; done)", ["tool"]),
    ("while read -r l; do printf '%s\\n' \"$l\"; done < f | sort -u", ["read", "printf", "sort"]),
    # a comment, a continued line, an export
    ("echo hi # a comment; rm x\nls", ["echo", "ls"]),
    ("git log \\\n  --oneline | head", ["git log", "head"]),
    ("export PATH=/x:$PATH; python3 -m pytest -q", ["export", "python3 -m pytest"]),
    # verbs: a known program's first plain word, through its own options
    ("git -C /r -c a=b worktree add ../w", ["git worktree add"]),
    # ... and an option's value is not the verb (M1): the tables in shell.py, by program
    ("tmux -L srv ls", ["tmux ls"]),
    ("tmux -L failmode send-keys -t x Enter", ["tmux send-keys"]),
    ("tmux -S /tmp/sock -f /dev/null new-session -d", ["tmux new-session"]),
    ("kubectl -n ns get pods", ["kubectl get"]),
    ("kubectl --context=prod --namespace ns get pods", ["kubectl get"]),
    ("make -f x.mk all", ["make all"]),
    ("make -C dir test", ["make test"]),
    ("make -j 8 -C dir test; make -j test; make -j8 test", ["make test", "make test", "make test"]),
    # ... a number option that ends a cluster takes the next word too (ST-324, D1)
    ("make -sj 4 test", ["make test"]),
    ("make -ksj 8 -C dir all; make -sl 2.5 test", ["make all", "make test"]),
    ("make -sj test; make -sj4 test; make -sj", ["make test", "make test", "make"]),
    # ... but not when an earlier letter takes the rest of the word: -f's makefile is `j`
    ("make -fj 4", ["make 4"]),
    ("npm --prefix x run build", ["npm run"]),
    ("docker -H h ps", ["docker ps"]),
    ("notmuch --config /x/cfg search tag:inbox", ["notmuch search"]),
    ("uv --directory /x pip install y", ["uv pip install"]),
    ("atom --store kin find --type rule", ["atom find"]),
    ("systemctl --user -H box status x.service", ["systemctl status"]),
    ("make -n test", ["make test"]),
    ("grep -rn pattern dir", ["grep"]),
]


@pytest.mark.parametrize("src,want", CASES, ids=[c[0][:40] for c in CASES])
def test_the_programs_of_a_command(src, want):
    assert [p.label for p in shell.programs(src)] == want


def test_via_says_how_a_program_was_reached():
    got = {p.name: p.via for p in shell.programs(
        "ssh ms1 'uptime'; bash -c 'ls'; uv run pytest; uvx ruff check")}
    assert got == {"ssh": "", "uptime": "ssh:ms1", "ls": "bash -c", "pytest": "uv run",
                   "ruff": "uvx"}


def test_split_keeps_the_words_and_drops_the_settings():
    assert shell.split("A=1 B='x y' cmd --flag 'two words' > out") == \
        [["cmd", "--flag", "two words"]]


@pytest.mark.parametrize("src", ["", "   ", "'", '"', "$(", "`", "((", "case x in", ")", "esac",
                                 "<<EOF", "a=(", "${", "[[ x", "f() {", ";;", "|&", "\\"])
def test_broken_shell_never_raises_or_hangs(src):
    assert isinstance(shell.programs(src), list)


def test_show_quotes_what_a_shell_would_and_cuts_long_arguments():
    assert shell.show(["git", "commit", "-m", "two words"]) == "git commit -m 'two words'"
    assert shell.show(["python3", "-c", "x" * 100], argmax=10) == "python3 -c " + "x" * 9 + "…"
    assert shell.show(["echo", "a" * 30], width=12) == "echo aaaaaa…"
