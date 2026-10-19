"""The Fable gate and GIT-24. Ported from the old suite's SESH-838 sections
(unit and prepare level; the subprocess pass is in test_start_run.py) and
its GIT-24 section. Two deliberate changes: the ask is `message send`, and
GIT-24 keeps a seat's committer by TM-5594's commit_as marker, not the
retired whitelist (ST-62, by the 2030-07-09 ruling)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import _scanlist
import pytest
from git_support import hermetic_env
from settings_support import host, shown
from start_support import plan_for

from sessiontools.start import policy as p
from sessiontools.start.errors import EXIT_USAGE, LaunchError


def gate(section=None, cli=(), environ=None, declared=None):
    p.check_fable_gate("claude", section or {}, list(cli), environ or {}, declared or {})


# ---------- A. the gate itself ---------------------------------------------------

def test_fable_gate_refuses_cli_model_both_spellings():
    for cli in (["--model", "fable"], ["--model=fable"]):
        with pytest.raises(LaunchError) as excinfo:
            gate(cli=cli)
        assert excinfo.value.code == EXIT_USAGE


def test_fable_gate_refuses_front_matter_model_with_no_cli_flag():
    with pytest.raises(LaunchError):
        gate(section={"model": "fable"})


@pytest.mark.parametrize("value", ["fable", "Fable", "FABLE", "FaBlE", "claude-fable-5",
                                   "CLAUDE-FABLE-5", "Claude-Fable-5", "claude-fable-6"])
def test_fable_gate_alias_case_and_future_matrix(value):
    """Substring, case-insensitive, deliberately: a future claude-fable-6
    must be refused too. Do not tighten this into an equality test."""
    with pytest.raises(LaunchError):
        gate(cli=["--model", value])


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "Yes", "ON", " 1 "])
def test_fable_gate_override_values_enable(value):
    assert p.fable_allowed({p.AGENT_ALLOW_FABLE_ENV: value}) is True


@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "maybe", " ", "1 1",
                                   "yes please", "TRU"])
def test_fable_gate_fail_closed_values_deny(value):
    environ = {} if value is None else {p.AGENT_ALLOW_FABLE_ENV: value}
    assert p.fable_allowed(environ) is False
    with pytest.raises(LaunchError):
        gate(cli=["--model", "fable"], environ=environ)


@pytest.mark.parametrize("model", ["opus", "sonnet", "haiku", "gpt-nonsense"])
def test_fable_gate_allowed_models_are_not_over_blocked(model):
    gate(cli=["--model", model])


def test_fable_gate_refusal_names_all_four_things():
    with pytest.raises(LaunchError) as excinfo:
        gate(cli=["--model", "fable"])
    text = str(excinfo.value)
    for part in ("what   you asked for:", "rule   Fable is DENIED BY DEFAULT",
                 "how    override: AGENT_ALLOW_FABLE=1", "next   IF YOU ARE AN AGENT",
                 "--model fable"):
        assert part in text


@pytest.mark.parametrize("var", ["ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_MODEL",
                                 "ANTHROPIC_DEFAULT_OPUS_MODEL",
                                 "ANTHROPIC_DEFAULT_SONNET_MODEL",
                                 "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                                 "CLAUDE_CODE_SUBAGENT_MODEL"])
def test_fable_gate_refuses_each_named_launch_env_variable(var):
    with pytest.raises(LaunchError):
        gate(environ={var: "fable"})


def test_fable_gate_ignores_env_names_outside_the_model_pattern():
    gate(environ={"ANTHROPIC_API_KEY": "sk-fable-not-a-model"})


def test_fable_gate_refuses_the_bypass_env_plus_opus_cli():
    """--model opus with ANTHROPIC_DEFAULT_OPUS_MODEL=claude-fable-5 IS a
    Fable launch: an argv-only gate would report a clean pass."""
    with pytest.raises(LaunchError) as excinfo:
        gate(cli=["--model", "opus"], environ={"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-fable-5"})
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL=claude-fable-5" in str(excinfo.value)


def test_fable_gate_refuses_an_agent_declared_environment_variable():
    with pytest.raises(LaunchError):
        gate(declared={"ANTHROPIC_MODEL": "fable"})


def test_fable_gate_override_governs_every_door():
    allow = {p.AGENT_ALLOW_FABLE_ENV: "1"}
    gate(cli=["--model", "fable"], environ=allow)
    gate(cli=["--model", "opus"], environ=dict(allow, ANTHROPIC_DEFAULT_OPUS_MODEL="fable"))
    gate(environ=allow, declared={"ANTHROPIC_MODEL": "fable"})


def test_fable_gate_scans_every_model_occurrence_not_last_wins():
    with pytest.raises(LaunchError):
        gate(section={"model": "opus"}, cli=["--model", "fable"])


# ---------- the operator's ask address, now `message send` -------------------------
#
# ST-116 (SK-46 G22): the refusal speaks of "the operator", never a named
# person, and the address comes from AGENT_OPERATOR_ADDR, else the `session`
# tool's settings (`operator.address`, else `operator.mailbox` derived), else
# none. The old derivation keyed on a maildir named after a person; these fixtures
# name a neutral box instead.

def _local_mail_root(tmp_path, box="ops"):
    root = tmp_path / "mail-root"
    (root / box).mkdir(parents=True)
    return str(root)


def _host_settings(data, raw=None):
    """Write the `session` tool's host-layer file under the walled HOME."""
    d = host(Path(os.environ["HOME"]), "session")
    d.mkdir(parents=True, exist_ok=True)
    (d / "session.json").write_text(raw if raw is not None else json.dumps(data))


def _refusal(cli=("--model", "fable"), environ=None, declared=None):
    with pytest.raises(LaunchError) as excinfo:
        gate(cli=cli, environ=environ, declared=declared)
    assert excinfo.value.code == EXIT_USAGE
    return str(excinfo.value)


def _ask(text):
    return text.split("Your move is to ASK:", 1)[1]


def test_the_refusal_asks_through_message_send_not_the_retired_session_mail(
        monkeypatch, tmp_path):
    """DEVIATION, on purpose: `session mail send` retired with the session
    repo (2030-07-29); its successor is sessionmail's `message send`, whose
    sender is the environment (no --from)."""
    monkeypatch.setattr(p, "_resolve_operator_fqdn", lambda: "box7.example-tailnet.invalid")
    _host_settings({"operator": {"mailbox": "ops"}})
    text = _refusal(environ={p.SESSION_MAIL_ROOT_ENV: _local_mail_root(tmp_path)})
    assert "message send ops@box7.example-tailnet.invalid" in text
    assert "-s 'Fable request: <what for>' -f <file>" in text
    assert "session mail" not in text
    assert "mail your own initiator" in text


def test_the_refusal_honours_agent_operator_addr_override(monkeypatch):
    monkeypatch.setattr(p, "_resolve_operator_fqdn",
                        lambda: pytest.fail("override must short-circuit derivation"))
    text = _refusal(environ={p.AGENT_OPERATOR_ADDR_ENV: "operator@elsewhere.example"})
    assert "message send operator@elsewhere.example" in text


@pytest.mark.parametrize("fqdn", ["barehost", "", None, "   "])
def test_an_undeliverable_address_names_the_override_never_a_look_alike(
        monkeypatch, tmp_path, fqdn):
    monkeypatch.setattr(p, "_resolve_operator_fqdn", lambda: fqdn)
    _host_settings({"operator": {"mailbox": "ops"}})
    text = _refusal(environ={p.SESSION_MAIL_ROOT_ENV: _local_mail_root(tmp_path)})
    assert "message send" not in text
    assert "mail your own initiator" in text and "AGENT_OPERATOR_ADDR" in text
    assert _ask(text).splitlines()[0].strip() != ""


def test_a_mailbox_setting_whose_maildir_is_not_local_derives_no_address(
        monkeypatch, tmp_path):
    monkeypatch.setattr(p, "_resolve_operator_fqdn", lambda: "otherbox.example.invalid")
    _host_settings({"operator": {"mailbox": "ops"}})
    (tmp_path / "no-ops").mkdir()
    assert p.operator_ask_address({p.SESSION_MAIL_ROOT_ENV: str(tmp_path / "no-ops")}) is None


def test_operator_ask_address_derives_and_overrides(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "_resolve_operator_fqdn", lambda: "box7.example.invalid")
    _host_settings({"operator": {"mailbox": "ops"}})
    local = {p.SESSION_MAIL_ROOT_ENV: _local_mail_root(tmp_path)}
    assert p.operator_ask_address(local) == "ops@box7.example.invalid"
    assert p.operator_ask_address(dict(local, AGENT_OPERATOR_ADDR="  a@b.c  ")) == "a@b.c"
    assert p.operator_ask_address(dict(local, AGENT_OPERATOR_ADDR=" ")) \
        == "ops@box7.example.invalid"


# ---------- ST-116: a stranger's box, the setting, the fail-safe ------------------

STRANGER = {"HOME": "/nonexistent", p.SESSION_MAIL_ROOT_ENV: "/nonexistent/.mail/agents"}


#: The three ways a Fable model reaches the gate.
DOORS = [
    {"cli": ["--model", "fable"]},
    {"cli": [], "environ": {"ANTHROPIC_MODEL": "claude-fable-5"}},
    {"cli": [], "declared": {"ANTHROPIC_MODEL": "fable"}},
]


def _strangers_refusal(door) -> str:
    environ = dict(STRANGER, **door.get("environ", {}))
    return _refusal(cli=door["cli"], environ=environ, declared=door.get("declared"))


@pytest.mark.parametrize("door", DOORS)
def test_a_strangers_refusal_names_no_person_and_still_refuses(door):
    """The README's reproduction: no settings, no maildir. Every door still
    refuses with exit 2; the text says "the operator", a role, and names no
    one (the private words themselves are the scan list's, below)."""
    text = _strangers_refusal(door)
    assert "the operator's standing approval" in text
    assert "say you need the operator's\n         approval for Fable" in text
    assert '"' not in text.split("next", 1)[1], "the refusal quotes no one"
    ask = _ask(text)
    assert "AGENT_OPERATOR_ADDR" in ask and "operator.address" in ask
    assert "operator.mailbox" in ask and shown("session") in ask
    assert "message send" not in text


def test_a_maildir_called_alice_is_no_longer_an_address(monkeypatch, tmp_path):
    """The old trigger: on one host a local `alice` maildir derived `alice@<FQDN>`.
    With no setting, a maildir's name is nobody's address."""
    monkeypatch.setattr(p, "_resolve_operator_fqdn", lambda: "box7.example.invalid")
    root = _local_mail_root(tmp_path, box="alice")
    assert p.operator_ask_address({p.SESSION_MAIL_ROOT_ENV: root}) is None
    text = _refusal(environ={p.SESSION_MAIL_ROOT_ENV: root})
    assert "alice" not in text.lower()


def test_the_address_setting_is_used_as_written(monkeypatch):
    monkeypatch.setattr(p, "_resolve_operator_fqdn",
                        lambda: pytest.fail("an address setting needs no derivation"))
    _host_settings({"operator": {"address": "  boss@ops.example  "}})
    assert p.operator_ask_address({}) == "boss@ops.example"
    assert "message send boss@ops.example" in _refusal()


def test_the_address_setting_beats_the_mailbox_setting(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "_resolve_operator_fqdn", lambda: "box7.example.invalid")
    _host_settings({"operator": {"address": "boss@ops.example", "mailbox": "ops"}})
    local = {p.SESSION_MAIL_ROOT_ENV: _local_mail_root(tmp_path)}
    assert p.operator_ask_address(local) == "boss@ops.example"


def test_the_environment_override_beats_every_setting(tmp_path):
    _host_settings({"operator": {"address": "boss@ops.example", "mailbox": "ops"}})
    environ = {p.AGENT_OPERATOR_ADDR_ENV: "env@wins.example",
               p.SESSION_MAIL_ROOT_ENV: _local_mail_root(tmp_path)}
    assert p.operator_ask_address(environ) == "env@wins.example"
    assert "message send env@wins.example" in _refusal(environ=environ)


def test_the_template_layer_supplies_the_address_and_host_overrides_it():
    d = host(Path(os.environ["HOME"]), "session")
    d.mkdir(parents=True)
    (d / "session.template.json").write_text(
        json.dumps({"operator": {"address": "shipped@ops.example"}}))
    assert p.operator_ask_address({}) == "shipped@ops.example"
    _host_settings({"operator": {"address": "host@ops.example"}})
    assert p.operator_ask_address({}) == "host@ops.example"


@pytest.mark.parametrize("data, raw", [
    (None, "{not json"),
    (None, "[1, 2]"),
    ({"_locked": "operator"}, None),
    ({"operator": "boss@ops.example"}, None),
    ({"operator": {"address": 7}}, None),
    ({"operator": {"address": ["boss@ops.example"]}}, None),
    ({"operator": {"address": "   "}}, None),
    ({"operator": {"mailbox": None}}, None),
])
def test_a_bad_settings_file_never_changes_whether_the_gate_refuses(data, raw):
    """FAIL SAFE: at worst the address is None; the refusal, its exit code
    and its "name one" sentence are untouched."""
    _host_settings(data, raw)
    assert p.operator_ask_address({}) is None
    text = _refusal()
    assert "AGENT_OPERATOR_ADDR" in _ask(text)
    gate(cli=["--model", "opus"])                      # still not over-blocked
    gate(cli=["--model", "fable"], environ={p.AGENT_ALLOW_FABLE_ENV: "1"})


def test_an_unreadable_settings_file_never_changes_whether_the_gate_refuses():
    _host_settings({"operator": {"address": "boss@ops.example"}})
    path = host(Path(os.environ["HOME"]), "session") / "session.json"
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running as a user who can read a mode-0 file")
        assert p.operator_ask_address({}) is None
        assert "AGENT_OPERATOR_ADDR" in _ask(_refusal())
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize("mailbox", ["..", ".", "../ops", "a/ops", "ops@box", ""])
def test_a_mailbox_setting_must_be_one_plain_local_name(monkeypatch, tmp_path, mailbox):
    monkeypatch.setattr(p, "_resolve_operator_fqdn", lambda: "box7.example.invalid")
    root = tmp_path / "mail" / "agents"
    (root / "ops").mkdir(parents=True)
    _host_settings({"operator": {"mailbox": mailbox}})
    assert p.operator_ask_address({p.SESSION_MAIL_ROOT_ENV: str(root)}) is None


@pytest.mark.parametrize("door", DOORS)
def test_a_strangers_refusal_hits_no_private_scan_row(door):
    """No private word in any door's refusal, by the private scan list
    (tests/_scanlist.py; skips loudly without it). Row names only."""
    rows = _scanlist.rows()
    found = _scanlist.hits(_strangers_refusal(door), rows)
    assert found == [], f"private scan rows hit the refusal: {', '.join(found)}"


def _fable_half() -> str:
    """policy.py's Fable half: up to the committer identity, without the
    committer paragraph of the module docstring."""
    source = Path(p.__file__).read_text(encoding="utf-8")
    fable_half = source.split("# ---------- the committer identity", 1)[0]
    head, rest = fable_half.split("THE COMMITTER. When the launch", 1)
    return head + rest.split("This module parses no STORE.md", 1)[1]


def test_no_person_is_named_in_the_policy_source():
    """No named approver anywhere in the Fable half of policy.py: the
    operator is a role, read from settings. The words are the private scan
    list's (skips loudly without it); a failure names rows, never text."""
    rows = _scanlist.rows()
    found = _scanlist.hits(_fable_half(), rows)
    assert found == [], f"private scan rows hit policy.py's Fable half: {', '.join(found)}"


def test_the_policy_source_guard_catches_a_planted_word():
    """The positive control: each row's own word, planted in the Fable half,
    is caught by that row."""
    rows = _scanlist.rows()
    half = _fable_half()
    missed = [name for name, word in _scanlist.controls(rows)
              if name not in _scanlist.hits(f"{half}\nAPPROVER = \" {word} \"\n", rows)]
    assert missed == [], f"the guard missed rows: {', '.join(missed)}"


# ---------- B. through prepare: refused before any side effect -----------------------

@pytest.mark.parametrize("front_matter, tail, env", [
    ("claude:\n  effort: high\n", ["--model", "fable"], None),
    ("claude:\n  effort: high\n", ["--model=fable"], None),
    ("claude:\n  model: fable\n", [], None),
    ("environment:\n  variables:\n    ANTHROPIC_MODEL: fable\n", [], None),
    ("claude:\n  effort: high\n", [], {"ANTHROPIC_MODEL": "fable"}),
    ("claude:\n  effort: high\n", ["--model", "opus"],
     {"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-fable-5"}),
    ("claude:\n  effort: high\n", ["--model", "fable"], {"AGENT_ALLOW_FABLE": "tru"}),
])
def test_prepare_refuses_fable_before_any_side_effect(tmp_path, front_matter, tail, env):
    with pytest.raises(LaunchError) as excinfo:
        plan_for(tmp_path, front_matter, argv_tail=tail, extra_env=env)
    assert excinfo.value.code == EXIT_USAGE
    sessions = tmp_path / "agents" / "sessions"
    assert not sessions.exists() or list(sessions.iterdir()) == []


def test_prepare_dry_run_also_refuses_a_fable_launch(tmp_path):
    with pytest.raises(LaunchError):
        plan_for(tmp_path, "claude:\n  effort: high\n", argv_tail=["--model", "fable"],
                 flags=["--dry-run"])


def test_the_override_lets_the_launch_plan(tmp_path):
    plan = plan_for(tmp_path, "claude:\n  effort: high\n", argv_tail=["--model", "fable"],
                    extra_env={"AGENT_ALLOW_FABLE": "1"}, flags=["--dry-run"])
    assert plan.argv[plan.argv.index("--model") + 1] == "fable"


def test_a_message_containing_the_word_fable_is_not_refused(tmp_path):
    plan = plan_for(tmp_path, "claude:\n  effort: high\n",
                    argv_tail=["ask", "about", "fable", "creatures"], flags=["--dry-run"])
    assert "fable" in plan.injected.initial and "--model" not in plan.argv


# ---------- GIT-24 -----------------------------------------------------------------

def git_repo(tmp_path, name=None, email=None, subdir="repo"):
    repo = tmp_path / subdir
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=hermetic_env())  # ST-161
    for key, value in (("user.name", name), ("user.email", email)):
        if value is not None:
            subprocess.run(["git", "-C", str(repo), "config", key, value], check=True,
                           env=hermetic_env())
    return str(repo)


AGENT = {"GIT_COMMITTER_NAME": "An Agent", "GIT_COMMITTER_EMAIL": "agent@example.test"}


def seat_env(root, **extra):
    """An AI committer plus TM-5594's marker naming `root` as the store."""
    return {**AGENT, "TM_COMMIT_AS": "seat", "TM_COMMIT_AS_ROOT": str(root), **extra}


def store_with_repo(tmp_path, name="store"):
    """A disposable store dir holding a repo whose human is resolvable."""
    store = tmp_path / name
    store.mkdir()
    repo = git_repo(store, "A Human", "human@example.test")
    return store, repo


HUMAN = ("override", "A Human", "human@example.test")


def test_git24_nothing_to_override_keeps(tmp_path):
    """(i) No AI committer in the env: nothing to override, marker or not."""
    assert p.resolve_committer_identity(str(tmp_path), environ={}).mode == "keep"
    assert p.resolve_committer_identity(
        str(tmp_path), environ={"TM_COMMIT_AS": "human"}).mode == "keep"


def test_git24_unmarked_dir_overrides_with_the_human_identity(tmp_path):
    """(d) No marker at all (a hand launch): the safe default, the human."""
    repo = git_repo(tmp_path, "A Human", "human@example.test")
    assert p.resolve_committer_identity(repo, environ=dict(AGENT)) == HUMAN


def test_git24_ignores_the_agents_own_config_override(tmp_path):
    repo = git_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[user]\n\tname = A Human\n\temail = human@example.test\n")
    agent_cfg = tmp_path / "agent-gitconfig"
    agent_cfg.write_text("[user]\n\tname = An Agent\n\temail = agent@example.test\n")
    identity = p.resolve_committer_identity(
        repo, environ={"GIT_COMMITTER_NAME": "An Agent", "HOME": str(home),
                       "GIT_CONFIG_GLOBAL": str(agent_cfg)})
    assert (identity.name, identity.email) == ("A Human", "human@example.test")


def test_git24_a_half_resolvable_human_is_unset_not_half_a_guess(tmp_path):
    repo = git_repo(tmp_path, "A Human", email=None)
    identity = p.resolve_committer_identity(
        repo, environ={"GIT_COMMITTER_NAME": "An Agent", "HOME": str(tmp_path / "no")})
    assert identity == p.COMMITTER_UNSET


def test_git24_no_resolvable_human_means_unset_never_a_guess(tmp_path):
    """(h) Outside an opted-in store, with no human to find: unset."""
    (tmp_path / "not-a-repo").mkdir()
    assert p.resolve_committer_identity(
        str(tmp_path / "not-a-repo"),
        environ={"GIT_COMMITTER_NAME": "An Agent", "HOME": str(tmp_path / "no")}).mode == "unset"


# ---------- GIT-24: the commit_as marker (ST-62, d01) ---------------------------

def test_the_marker_names_are_tm_5594s():
    assert (p.COMMIT_AS_ENV, p.COMMIT_AS_ROOT_ENV, p.COMMIT_AS_SEAT) == (
        "TM_COMMIT_AS", "TM_COMMIT_AS_ROOT", "seat")


def test_the_retired_whitelist_is_gone():
    """The 2030-07-09 ruling replaced it; nothing here reads it."""
    for name in ("COMMITTER_WHITELIST", "read_committer_whitelist"):
        assert not hasattr(p, name)
    assert "whitelist_path" not in p.resolve_committer_identity.__code__.co_varnames


def test_seat_marker_inside_the_store_keeps_the_seat_committer(tmp_path):
    """(a) The store opted in and the work dir is inside it: keep."""
    store, repo = store_with_repo(tmp_path)
    assert p.resolve_committer_identity(repo, environ=seat_env(store)) == p.COMMITTER_KEEP


def test_seat_marker_at_the_store_root_itself_keeps(tmp_path):
    store, _repo = store_with_repo(tmp_path)
    assert p.resolve_committer_identity(str(store), environ=seat_env(store)).mode == "keep"


def test_a_work_dir_outside_the_root_gets_the_human(tmp_path):
    """(b) The marker names another store: this dir never opted in."""
    store, _ = store_with_repo(tmp_path, "store")
    other = tmp_path / "elsewhere"
    other.mkdir()
    repo = git_repo(other, "A Human", "human@example.test")
    assert p.resolve_committer_identity(repo, environ=seat_env(store)) == HUMAN


@pytest.mark.parametrize("verdict", ["human", "Seat", "SEAT", " seat", "seat ", "", "yes"])
def test_only_exactly_seat_keeps(tmp_path, verdict):
    """(c) `human`, and every near miss, is the human."""
    store, repo = store_with_repo(tmp_path)
    environ = seat_env(store, TM_COMMIT_AS=verdict)
    assert p.resolve_committer_identity(repo, environ=environ) == HUMAN


def test_a_seat_marker_with_no_root_var_gets_the_human(tmp_path):
    store, repo = store_with_repo(tmp_path)
    environ = seat_env(store)
    del environ["TM_COMMIT_AS_ROOT"]
    assert p.resolve_committer_identity(repo, environ=environ) == HUMAN


@pytest.mark.parametrize("root", ["", "store", "./store", "relative/store"])
def test_an_empty_or_relative_root_gets_the_human(tmp_path, monkeypatch, root):
    """(e) Even when the relative root would resolve to the right store
    from this cwd: kick always writes an absolute root, so anything else is
    not kick's and fails closed."""
    _store, repo = store_with_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert p.resolve_committer_identity(repo, environ=seat_env(root)) == HUMAN


def test_a_mismatched_root_gets_the_human(tmp_path):
    """(e) An absolute root that is not this work dir's store (here: a
    sub-directory of it, so the store itself lies OUTSIDE the root)."""
    store, repo = store_with_repo(tmp_path)
    (store / "sub").mkdir()
    assert p.resolve_committer_identity(repo, environ=seat_env(store / "sub")) == HUMAN


def test_a_sibling_prefix_is_not_inside(tmp_path):
    """(f) /x/store-evil starts with the string /x/store but is not under it:
    containment is by path component, never a string prefix."""
    store, _ = store_with_repo(tmp_path, "store")
    _evil, evil_repo = store_with_repo(tmp_path, "store-evil")
    assert p.resolve_committer_identity(evil_repo, environ=seat_env(store)) == HUMAN
    assert not p.seat_committer_allowed(evil_repo, seat_env(store))


def test_a_symlinked_root_resolves_and_keeps(tmp_path):
    """(g) On a real host ~/.taskmaster can be a symlink to the store: the marker may
    name either spelling."""
    store, repo = store_with_repo(tmp_path)
    link = tmp_path / "taskmaster-link"
    link.symlink_to(store, target_is_directory=True)
    assert p.resolve_committer_identity(repo, environ=seat_env(link)).mode == "keep"


def test_a_symlinked_work_dir_resolves_and_keeps(tmp_path):
    """(g) The work dir reached through a link that lives OUTSIDE the store."""
    store, repo = store_with_repo(tmp_path)
    link = tmp_path / "repo-link"
    link.symlink_to(repo, target_is_directory=True)
    assert p.resolve_committer_identity(str(link), environ=seat_env(store)).mode == "keep"


def test_a_symlink_inside_the_store_pointing_out_is_outside(tmp_path):
    """realpath, not the spelling: a link under the store whose target is
    elsewhere is judged where it lands."""
    store, _ = store_with_repo(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    repo = git_repo(other, "A Human", "human@example.test")
    (store / "escape").symlink_to(repo, target_is_directory=True)
    assert p.resolve_committer_identity(
        str(store / "escape"), environ=seat_env(store)) == HUMAN


def test_outside_the_store_with_no_human_is_unset(tmp_path):
    """(h) Fail closed all the way down: no human resolvable means unset."""
    store, _ = store_with_repo(tmp_path)
    (tmp_path / "bare").mkdir()
    environ = seat_env(store, HOME=str(tmp_path / "no"))
    assert p.resolve_committer_identity(
        str(tmp_path / "bare"), environ=environ) == p.COMMITTER_UNSET


# ---------- GIT-24 through the plan path --------------------------------------

def test_the_plan_renders_no_override_inside_an_opted_in_store(tmp_path):
    """A launch whose dir (plan_for's `work`) is inside an opted-in store
    keeps the spawner's seat committer: the script never touches it."""
    plan = plan_for(tmp_path, "claude:\n  effort: high\n", flags=["--dry-run"],
                    extra_env=seat_env(tmp_path))
    assert "GIT_COMMITTER" not in plan.script


def test_the_plan_renders_the_human_outside_the_opted_in_store(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[user]\n\tname = A Human\n\temail = human@example.test\n")
    store = tmp_path / "store"
    store.mkdir()
    plan = plan_for(tmp_path, "claude:\n  effort: high\n", flags=["--dry-run"],
                    extra_env=seat_env(store, HOME=str(home)))
    assert "export GIT_COMMITTER_NAME='A Human'" in plan.script
    assert "export GIT_COMMITTER_EMAIL=human@example.test" in plan.script
