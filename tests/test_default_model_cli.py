"""`session default-model show|list|enforce`: the verb over the pin's core.

Every test runs through `cli.main` under the conftest's temporary HOME and
state dir, with CLAUDE_CONFIG_DIR scrubbed; a guard asserts the target
settings.json lies under the test's temp tree, so no test reaches a real one.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from settings_support import write_session_settings

from sessiontools import cli, completion, default_model_cli, verbs
from sessiontools import default_model as dm

SAMPLE = '{\n  "env": {"ANTHROPIC_MODEL": "x"},\n  "model": "sonnet",\n  "theme": "dark"\n}\n'


@pytest.fixture(autouse=True)
def _under_tmp(tmp_path_factory):
    base = tmp_path_factory.getbasetemp().resolve()
    assert dm.settings_path().resolve().is_relative_to(base)
    assert dm.state_dir().resolve().is_relative_to(base)


@pytest.fixture
def home() -> Path:
    return Path(os.environ["HOME"])


@pytest.fixture
def claude(home) -> Path:
    d = home / ".claude"
    d.mkdir()
    return d


def pin(home: Path, value) -> None:
    write_session_settings(home, {"model": {"default": value}})


def put(claude: Path, text: str) -> Path:
    p = claude / "settings.json"
    p.write_text(text)
    return p


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(["default-model", *argv])
    out, err = capsys.readouterr()
    return code, out, err


# --- the tables -----------------------------------------------------------------------------

def test_the_verb_is_in_every_table():
    assert "default-model" in dict(cli.VERBS)
    assert "the default every NEW claude starts on" in dict(cli.VERBS)["default-model"]
    assert cli.HANDLERS["default-model"] is cli._default_model
    assert verbs.resolve(cli.PARSERS["default-model"]) is default_model_cli.build_parser


def test_the_usage_lists_it(capsys):
    assert cli.main([]) == 0
    assert re.search(r"^  default-model\s+pin the default", capsys.readouterr().out,
                     re.MULTILINE)


def test_the_sub_verbs_are_the_table_and_completion_offers_them():
    p = default_model_cli.build_parser()
    sub = next(a for a in p._actions if a.dest == "verb")
    assert list(sub.choices) == [n for n, _ in default_model_cli.SUBVERBS]
    assert "'session default-model') _session_r='show list enforce watch unit" in completion.script("bash")


def test_help_says_new_claude_and_the_exit_codes(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["default-model", "--help"])
    assert e.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "the default every NEW claude starts on" in out
    assert "3 refused, nothing written" in out and "5 settings.json unreadable" in out


@pytest.mark.parametrize("argv", [["bogus"], ["enforce", "--json"], ["list", "--dry-run"],
                                  ["show", "extra"]])
def test_a_usage_error_is_exit_2(capsys, argv):
    with pytest.raises(SystemExit) as e:
        cli.main(["default-model", *argv])
    assert e.value.code == 2


# --- show -----------------------------------------------------------------------------------

def test_bare_is_show(home, claude, capsys):
    pin(home, "opus")
    put(claude, SAMPLE)
    assert run(capsys) == run(capsys, "show")


def test_show_unset_writes_nothing_and_says_so(home, claude, capsys):
    p = put(claude, SAMPLE)
    old = (p.read_bytes(), p.stat().st_mtime_ns)
    code, out, _ = run(capsys, "show")
    assert code == 0
    assert "pin:     unset" in out and "model.default is not set" in out
    assert 'model:   "sonnet"' in out and "unset: nothing is pinned" in out
    assert (p.read_bytes(), p.stat().st_mtime_ns) == old
    assert not dm.state_dir().exists()


def test_show_out_of_sync_plans_and_writes_nothing(home, claude, capsys):
    pin(home, "opus")
    p = put(claude, SAMPLE)
    old = p.read_bytes()
    code, out, _ = run(capsys, "show")
    assert code == 0
    assert 'pin:     "opus"' in out and f"file:    {p}" in out
    assert "out of sync" in out and 'would set model "sonnet" -> "opus"' in out
    assert p.read_bytes() == old
    assert not dm.state_dir().exists()


def test_show_in_sync(home, claude, capsys):
    pin(home, "opus")
    put(claude, SAMPLE.replace('"sonnet"', '"opus"'))
    code, out, _ = run(capsys)
    assert code == 0 and "state:   in sync" in out


def test_show_a_missing_key_says_so(home, claude, capsys):
    pin(home, "opus")
    put(claude, '{"theme": "dark"}')
    code, out, _ = run(capsys)
    assert code == 0 and 'model:   no "model" key' in out and "out of sync" in out


def test_show_json(home, claude, capsys):
    pin(home, "opus")
    p = put(claude, SAMPLE)
    code, out, _ = run(capsys, "show", "--json")
    got = json.loads(out)
    assert code == 0
    assert {k: got[k] for k in ("pin", "file", "present", "model", "state", "code")} == {
        "pin": "opus", "file": str(p), "present": True, "model": "sonnet",
        "state": "planned", "code": 0}
    assert set(got) == {"pin", "setting", "file", "present", "model", "note", "state",
                        "code", "message"}


def test_show_json_unset(home, capsys):
    code, out, _ = run(capsys, "show", "--json")
    got = json.loads(out)
    assert (code, got["pin"], got["state"], got["present"]) == (0, None, "unset", False)
    assert got["note"] == "no settings.json"


@pytest.mark.parametrize("value", ["fable", "claude-fable-5-1"])
def test_show_a_fable_pin_is_exit_3(home, claude, capsys, value):
    pin(home, value)
    p = put(claude, SAMPLE)
    old = p.read_bytes()
    code, out, _ = run(capsys, "show")
    assert code == 3 and "refused" in out and "Fable is never pinned" in out
    assert p.read_bytes() == old


def test_show_an_unreadable_file_is_exit_5(home, claude, capsys):
    pin(home, "opus")
    (claude / "settings.json").mkdir()
    code, out, _ = run(capsys, "show")
    assert code == 5 and "unreadable" in out


def test_show_no_config_dir_is_exit_3_and_creates_nothing(home, capsys):
    pin(home, "opus")
    code, out, _ = run(capsys, "show")
    assert code == 3 and "no Claude Code config dir" in out
    assert not (home / ".claude").exists()


def test_show_an_unparseable_file_is_exit_3(home, claude, capsys):
    pin(home, "opus")
    put(claude, '{"model": ')
    code, out, _ = run(capsys, "show")
    assert code == 3 and "does not parse as JSON" in out


# --- list -----------------------------------------------------------------------------------

def test_list_prints_every_model_and_marks_fable(capsys):
    code, out, _ = run(capsys, "list")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split() == ["NAME", "KIND", "PINNABLE"]
    for m in dm.models():
        row = next(x for x in lines[1:] if x.split()[:1] == [m.name])
        assert row.split()[1] == m.kind
        if m.pinnable:
            assert row.endswith("yes")
        else:
            assert row.endswith("no: never pinned (Fable)")
    assert any(not m.pinnable for m in dm.models())


def test_list_says_it_is_not_a_live_entitlement_check(capsys):
    _code, out, _ = run(capsys, "list")
    assert out.rstrip().endswith(default_model_cli.NOT_LIVE)
    assert "not a live check" in default_model_cli.NOT_LIVE


def test_list_json(capsys):
    code, out, _ = run(capsys, "list", "--json")
    got = json.loads(out)
    assert code == 0
    assert got == [{"name": m.name, "kind": m.kind, "pinnable": m.pinnable, "why": m.why}
                   for m in dm.models()]


def test_list_reads_no_setting_and_writes_nothing(home, capsys, monkeypatch):
    monkeypatch.setattr(dm, "setting", lambda *a, **k: pytest.fail("list read the setting"))
    assert run(capsys, "list")[0] == 0
    assert not dm.state_dir().exists()


# --- enforce --------------------------------------------------------------------------------

def test_enforce_unset_leaves_a_rewritten_model_alone(home, claude, capsys):
    p = put(claude, SAMPLE.replace('"sonnet"', '"claude-fable-5-1"'))
    old = (p.read_bytes(), p.stat().st_mtime_ns)
    code, out, err = run(capsys, "enforce")
    assert (code, err) == (0, "")
    assert "model.default is not set: nothing to do" in out
    assert (p.read_bytes(), p.stat().st_mtime_ns) == old
    assert not dm.state_dir().exists()


def test_enforce_puts_model_back_and_prints_the_log_line(home, claude, capsys):
    pin(home, "opus")
    p = put(claude, SAMPLE)
    old = p.read_bytes()
    code, out, err = run(capsys, "enforce")
    assert (code, err) == (0, "")
    [backup] = dm.backups()
    assert out == f'default-model: model "sonnet" -> "opus" in {p} (backup {backup})\n'
    assert p.read_text() == SAMPLE.replace('"sonnet"', '"opus"')
    assert backup.read_bytes() == old


def test_enforce_again_is_in_sync_and_writes_nothing(home, claude, capsys):
    pin(home, "opus")
    p = put(claude, SAMPLE)
    run(capsys, "enforce")
    mtime = p.stat().st_mtime_ns
    code, out, _ = run(capsys, "enforce")
    assert code == 0 and "in sync" in out
    assert p.stat().st_mtime_ns == mtime and len(dm.backups()) == 1


def test_enforce_inserts_a_missing_key(home, claude, capsys):
    pin(home, "opus")
    p = put(claude, '{\n  "theme": "dark"\n}\n')
    code, out, _ = run(capsys, "enforce")
    assert code == 0 and 'model absent -> "opus"' in out
    assert p.read_text() == '{\n  "model": "opus",\n  "theme": "dark"\n}\n'


def test_enforce_dry_run_prints_the_plan_and_writes_nothing(home, claude, capsys):
    pin(home, "opus")
    p = put(claude, SAMPLE)
    old = p.read_bytes()
    code, out, err = run(capsys, "enforce", "--dry-run")
    assert (code, err) == (0, "")
    assert f'would set model "sonnet" -> "opus" in {p} (replaced)' in out
    assert f"the backup would go in {dm.state_dir() / 'backups'}" in out
    assert p.read_bytes() == old
    assert not dm.state_dir().exists()


def test_enforce_dry_run_of_a_new_file_names_no_backup(home, claude, capsys):
    pin(home, "opus")
    code, out, _ = run(capsys, "enforce", "--dry-run")
    assert code == 0 and "(created)" in out and "backup" not in out
    assert not (claude / "settings.json").exists()


@pytest.mark.parametrize("value", ["fable", "Fable", "claude-fable-5-1"])
def test_enforce_a_fable_pin_is_exit_3_on_stderr(home, claude, capsys, value):
    pin(home, value)
    p = put(claude, SAMPLE)
    old = p.read_bytes()
    code, out, err = run(capsys, "enforce")
    assert (code, out) == (3, "")
    assert "Fable is never pinned" in err and "nothing written" in err
    assert p.read_bytes() == old and not dm.state_dir().exists()


def test_enforce_a_pin_off_the_list_is_exit_3(home, claude, capsys):
    pin(home, "opsu")
    put(claude, SAMPLE)
    code, _out, err = run(capsys, "enforce")
    assert code == 3 and "session default-model list" in err


def test_enforce_unreadable_is_exit_5(home, claude, capsys):
    pin(home, "opus")
    (claude / "settings.json").mkdir()
    code, _out, err = run(capsys, "enforce")
    assert code == 5 and "unreadable" in err


def test_enforce_no_config_dir_is_exit_3(home, capsys):
    pin(home, "opus")
    code, _out, err = run(capsys, "enforce")
    assert code == 3 and "not ours to create" in err
    assert not (home / ".claude").exists()


def test_enforce_a_failed_write_is_exit_4_and_names_the_backup(home, claude, capsys,
                                                               monkeypatch):
    from sessiontools import statusline

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(statusline, "_write_bytes", boom)
    pin(home, "opus")
    p = put(claude, SAMPLE)
    old = p.read_bytes()
    code, _out, err = run(capsys, "enforce")
    [backup] = dm.backups()
    assert code == 4 and "failed (disk full)" in err and str(backup) in err
    assert p.read_bytes() == old


# --- the CLI path loads no yaml ---------------------------------------------------------------

def test_the_verb_path_loads_no_yaml(tmp_path):
    """`session default-model show|list|enforce|unit|watch`, in a fresh interpreter on a
    temp HOME with a pin set: neither runtime dependency nor `start` loads."""
    probe = (
        "import sys, json, os, pathlib, contextlib, io\n"
        "h = pathlib.Path(os.environ['HOME'])\n"
        "s = h / '.sessionkit' / 'tools' / 'settings'; s.mkdir(parents=True)\n"
        "(s / 'session.json').write_text(json.dumps({'model': {'default': 'opus'}}))\n"
        "(h / '.claude').mkdir(); (h / '.claude' / 'settings.json').write_text('{\"model\": \"x\"}')\n"
        "from sessiontools import cli\n"
        "codes = []\n"
        "with contextlib.redirect_stdout(io.StringIO()):\n"
        "    for argv in (['show'], ['show', '--json'], ['list'], ['enforce', '--dry-run'], ['enforce'],\n"
        "                 ['unit', 'launchd'], ['unit', 'systemd'], ['watch', '--interval', '0.01']):\n"
        "        if argv[0] == 'watch':\n"
        "            from sessiontools import default_model_watch as w\n"
        "            real = w.watch\n"
        "            w.watch = lambda **k: real(ticks=1, **k)\n"
        "        codes.append(cli.main(['default-model', *argv]))\n"
        "print(codes, json.loads((h / '.claude' / 'settings.json').read_text())['model'],\n"
        "      'yaml' in sys.modules, 'jsonschema' in sys.modules, 'sessiontools.start' in sys.modules)\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    env.update(HOME=str(tmp_path), SESSION_STATE_DIR=str(tmp_path / "state"))
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                            check=True, env=env)
    assert result.stdout.strip() == "[0, 0, 0, 0, 0, 0, 0, 0] opus False False False"
