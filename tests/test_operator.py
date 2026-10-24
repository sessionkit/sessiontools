"""The operator and the privileged seats, from one setting (ST-130 do-001,
designs/d01-operator-reader.md): `sessiontools.read_operator` and its CLI
twin `session operator`.

Every settings file here is written under the walled HOME conftest sets;
nothing is read from or written to the live machine's settings. The FQDN
lookup is patched wherever a derivation could reach it, so no test asks the
network."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from settings_support import host

import sessiontools
from sessiontools import cli
from sessiontools import operator as op_mod
from sessiontools.operator import Operator, read_operator
from sessiontools.start import policy

FQDN = "box7.example.invalid"
SHORT = "box7"
# The real seams, kept before any test patches them.
REAL_RESOLVE_FQDN = op_mod._resolve_fqdn
REAL_RESOLVE_HOSTNAME = op_mod._resolve_hostname
PTR = "1.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.ip6.arpa"


@pytest.fixture(autouse=True)
def _no_operator_from_the_caller(monkeypatch):
    """The caller's own AGENT_OPERATOR_ADDR or mail root never leaks in, and
    the FQDN never comes from the network."""
    monkeypatch.delenv(op_mod.AGENT_OPERATOR_ADDR_ENV, raising=False)
    monkeypatch.delenv(op_mod.SESSION_MAIL_ROOT_ENV, raising=False)
    for name in op_mod.MAIL_HOST_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(op_mod, "_resolve_fqdn", lambda: FQDN)
    monkeypatch.setattr(op_mod, "_resolve_hostname", lambda: SHORT)


def _settings_dir() -> Path:
    home = Path(os.environ["HOME"])
    assert "no-home" in str(home), "the walled HOME, never the live one"
    d = host(home, "session")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _host_settings(data=None, raw=None):
    (_settings_dir() / "session.json").write_text(
        raw if raw is not None else json.dumps(data))


def _template_settings(data):
    (_settings_dir() / "session.template.json").write_text(json.dumps(data))


def _mail_root(tmp_path, *boxes):
    root = tmp_path / "mail" / "agents"
    root.mkdir(parents=True)
    for box in boxes:
        (root / box).mkdir()
    return str(root)


def _cli_json(capsys, argv=("operator",)):
    assert cli.main(list(argv)) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "one line of JSON"
    return json.loads(out)


# ---------- nothing configured -----------------------------------------------

def test_nothing_configured_is_no_operator_and_nothing_privileged():
    op = read_operator({})
    assert op == Operator(address=None, mailbox=None, privileged=frozenset())
    assert not op.is_privileged("you")


def test_session_operator_with_no_settings_prints_nulls_and_exits_0(capsys):
    assert cli.main(["operator"]) == 0
    out = capsys.readouterr().out
    assert out == '{"operator": null, "mailbox": null, "privileged": []}\n'


# ---------- the privileged set -----------------------------------------------

def test_mailbox_and_privileged_list_with_a_local_maildir(tmp_path):
    """The success criterion: mailbox 'you' + privileged ['alice'] is
    {you, alice}, plus the derived address where it is deliverable."""
    _host_settings({"operator": {"mailbox": "you", "privileged": ["alice"]}})
    op = read_operator({op_mod.SESSION_MAIL_ROOT_ENV: _mail_root(tmp_path, "you")})
    assert op.address == f"you@{FQDN}"
    assert op.mailbox == "you"
    assert op.privileged == {"you", "alice", f"you@{FQDN}"}


def test_mailbox_not_local_is_privileged_by_name_but_has_no_address(tmp_path):
    _host_settings({"operator": {"mailbox": "you", "privileged": ["alice"]}})
    op = read_operator({op_mod.SESSION_MAIL_ROOT_ENV: _mail_root(tmp_path)})
    assert op.address is None
    assert op.privileged == {"you", "alice"}


@pytest.mark.parametrize("fqdn", ["", "box7", "   ", None])
def test_an_fqdn_with_no_domain_derives_the_short_host(tmp_path, fqdn):
    """The bare short host is the name the local mail drop delivers to, so
    it is a real address, not a look-alike."""
    _host_settings({"operator": {"mailbox": "you", "privileged": ["alice"]}})
    op = read_operator({op_mod.SESSION_MAIL_ROOT_ENV: _mail_root(tmp_path, "you")},
                       resolve_fqdn=lambda: fqdn)
    assert op.address == f"you@{SHORT}"
    assert op.privileged == {"you", "alice", f"you@{SHORT}"}


def test_no_short_host_and_no_fqdn_derives_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(op_mod, "_resolve_hostname", lambda: "")
    _host_settings({"operator": {"mailbox": "you", "privileged": ["alice"]}})
    op = read_operator({op_mod.SESSION_MAIL_ROOT_ENV: _mail_root(tmp_path, "you")},
                       resolve_fqdn=lambda: PTR)
    assert op.address is None
    assert op.privileged == {"you", "alice"}


# ---------- the host part: what the mail drop takes as this host -------------

@pytest.mark.parametrize("fqdn", [PTR, "1.0.0.127.in-addr.arpa", PTR.upper()])
def test_a_reverse_dns_fqdn_derives_the_short_host(tmp_path, fqdn):
    """getfqdn() on a host whose name has no forward record returns the
    loopback's PTR name; the address uses the short host instead."""
    _host_settings({"operator": {"mailbox": "admin"}})
    op = read_operator({op_mod.SESSION_MAIL_ROOT_ENV: _mail_root(tmp_path, "admin")},
                       resolve_fqdn=lambda: fqdn)
    assert op.address == f"admin@{SHORT}"
    assert "arpa" not in "".join(op.privileged)


def test_the_default_lookup_patched_to_a_ptr_name_derives_the_short_host(
        tmp_path, monkeypatch, capsys):
    """socket.getfqdn itself returns the PTR name: the reader and
    `session operator` both say <mailbox>@<short host>."""
    monkeypatch.setattr(op_mod, "_resolve_fqdn", REAL_RESOLVE_FQDN)
    monkeypatch.setattr(op_mod, "_resolve_hostname", REAL_RESOLVE_HOSTNAME)
    monkeypatch.setattr(op_mod.socket, "getfqdn", lambda *a: PTR)
    monkeypatch.setattr(op_mod.socket, "gethostname", lambda: "Box7.local")
    monkeypatch.setenv(op_mod.SESSION_MAIL_ROOT_ENV, _mail_root(tmp_path, "admin"))
    _host_settings({"operator": {"mailbox": "admin"}})
    assert read_operator().address == "admin@box7"
    assert _cli_json(capsys)["operator"] == "admin@box7"
    assert policy.operator_ask_address(os.environ) == "admin@box7"


@pytest.mark.parametrize("fqdn", ["otherbox.example.invalid", "box70.example.invalid",
                                  "box.example.invalid", "example.invalid"])
def test_an_fqdn_not_under_the_short_host_derives_the_short_host(tmp_path, fqdn):
    _host_settings({"operator": {"mailbox": "you"}})
    op = read_operator({op_mod.SESSION_MAIL_ROOT_ENV: _mail_root(tmp_path, "you")},
                       resolve_fqdn=lambda: fqdn)
    assert op.address == f"you@{SHORT}"


@pytest.mark.parametrize("fqdn", ["box7.example-tailnet.invalid", "BOX7.Example.Invalid"])
def test_an_fqdn_under_the_short_host_is_kept(tmp_path, fqdn):
    """A tailnet name stays the host part, so a privileged match against a
    drop-qualified sender does not change."""
    _host_settings({"operator": {"mailbox": "you"}})
    op = read_operator({op_mod.SESSION_MAIL_ROOT_ENV: _mail_root(tmp_path, "you")},
                       resolve_fqdn=lambda: fqdn)
    assert op.address == f"you@{fqdn.lower()}"


def test_the_short_host_follows_the_mail_drops_order(tmp_path):
    """The variables (first set wins), then the mail tool's `host` setting,
    then the hostname's first label; each lowercased."""
    home = Path(os.environ["HOME"])
    mail_dir = host(home, op_mod.MAIL_SETTINGS_TOOL)
    mail_dir.mkdir(parents=True, exist_ok=True)
    assert op_mod.short_host({}, lambda: "Box7.lan") == "box7"
    (mail_dir / "mail.json").write_text(json.dumps({"host": " Studio "}))
    assert op_mod.short_host({}, lambda: "box7") == "studio"
    assert op_mod.short_host({"SESSION_MAIL_HOST": "Older"}) == "older"
    assert op_mod.short_host({"SESSIONMAIL_HOST": "Newer",
                              "SESSION_MAIL_HOST": "older"}) == "newer"
    assert op_mod.short_host({"SESSIONMAIL_HOST": "  "}, lambda: "x") == "studio"


@pytest.mark.parametrize("raw", ["{not json", '{"host": 7}', '{"host": ""}', "[]"])
def test_a_bad_mail_host_setting_falls_back_to_the_hostname(raw):
    mail_dir = host(Path(os.environ["HOME"]), op_mod.MAIL_SETTINGS_TOOL)
    mail_dir.mkdir(parents=True, exist_ok=True)
    (mail_dir / "mail.json").write_text(raw)
    assert op_mod.short_host({}, lambda: "box7.lan") == "box7"


def _drop_on_this_host(address: str, host: str) -> bool:
    """The local mail drop's on-this-host predicate, copied (not imported)
    from its qualify step: a domain is this host when it IS the short host
    or a name under it."""
    local, at, domain = address.strip().lower().partition("@")
    if not local:
        return False
    return not at or domain == host or domain.startswith(host + ".")


#: (short host, FQDN): short, short under a tailnet, PTR, unrelated, case,
#: a prefix that is not a label, nothing.
DROP_TABLE = [
    ("box7", "box7"),
    ("box7", "box7.example-tailnet.invalid"),
    ("box7", "BOX7.Example-Tailnet.Invalid"),
    ("box7", PTR),
    ("box7", "1.0.0.127.in-addr.arpa"),
    ("box7", "otherbox.example.invalid"),
    ("box7", "box70.example.invalid"),
    ("box7", "example.invalid"),
    ("box7", ""),
    ("fresh-vm", PTR),
    ("fresh-vm", "fresh-vm.local"),
]


@pytest.mark.parametrize(("short", "fqdn"), DROP_TABLE)
def test_the_host_part_is_always_one_the_drop_takes_as_this_host(short, fqdn):
    got = op_mod.host_part({}, resolve_fqdn=lambda: fqdn, resolve_hostname=lambda: short)
    assert _drop_on_this_host(f"you@{got}", short)
    assert not got.endswith(".arpa")


@pytest.mark.parametrize(("short", "fqdn"), DROP_TABLE)
def test_is_this_host_equals_the_drops_predicate(short, fqdn):
    """On every non-reverse-DNS name the two tests agree; on a PTR name the
    drop's would say no as well, unless the short host were a PTR label."""
    assert op_mod.is_this_host(fqdn, short) == (
        bool(fqdn) and _drop_on_this_host(f"you@{fqdn}", short))


def test_a_reverse_dns_name_is_never_this_host_even_under_the_short_host():
    """The one place the helper is stricter than the drop: a short host of
    "1" would put the IPv6 loopback's PTR name under it."""
    assert _drop_on_this_host(f"you@{PTR}", "1")
    assert not op_mod.is_this_host(PTR, "1")
    assert op_mod.host_part({}, resolve_fqdn=lambda: PTR,
                            resolve_hostname=lambda: "1") == "1"


def test_the_address_setting_is_the_operator_and_is_privileged(tmp_path):
    _host_settings({"operator": {"address": "  you@host.example  ", "mailbox": "you",
                                 "privileged": ["alice", "bob"]}})
    op = read_operator({op_mod.SESSION_MAIL_ROOT_ENV: _mail_root(tmp_path, "you")})
    assert op.address == "you@host.example"
    assert op.privileged == {"you@host.example", "you", "alice", "bob"}


def test_the_operator_is_privileged_with_no_privileged_list():
    _host_settings({"operator": {"address": "you@host.example"}})
    op = read_operator({})
    assert op.privileged == {"you@host.example"}
    assert op.mailbox is None


def test_an_empty_privileged_list_means_only_the_operator():
    _host_settings({"operator": {"address": "you@host.example", "privileged": []}})
    assert read_operator({}).privileged == {"you@host.example"}


def test_a_privileged_list_without_an_operator_is_just_that_list():
    _host_settings({"operator": {"privileged": ["alice"]}})
    op = read_operator({})
    assert op.address is None
    assert op.privileged == {"alice"}


def test_members_are_stripped_and_empties_dropped():
    _host_settings({"operator": {"privileged": ["  alice ", "", "   ", "bob"]}})
    assert read_operator({}).privileged == {"alice", "bob"}


def test_the_template_layer_supplies_privileged_and_host_replaces_it():
    """A list is a leaf: the host's list REPLACES the template's."""
    _template_settings({"operator": {"privileged": ["shipped"]}})
    assert read_operator({}).privileged == {"shipped"}
    _host_settings({"operator": {"privileged": ["host-seat"]}})
    assert read_operator({}).privileged == {"host-seat"}


def test_a_template_lock_keeps_the_shipped_privileged_list():
    _template_settings({"_locked": ["operator.privileged"],
                        "operator": {"privileged": ["shipped"]}})
    _host_settings({"operator": {"privileged": ["host-seat"]}})
    assert read_operator({}).privileged == {"shipped"}


# ---------- AGENT_OPERATOR_ADDR: moves the address, grants nothing ----------

def test_agent_operator_addr_alone_sets_the_address_and_grants_no_privilege():
    op = read_operator({op_mod.AGENT_OPERATOR_ADDR_ENV: "  me@agent.example "})
    assert op.address == "me@agent.example"
    assert op.privileged == frozenset()
    assert not op.is_privileged("me@agent.example")


def test_agent_operator_addr_beside_settings_wins_the_address_not_privilege():
    _host_settings({"operator": {"address": "you@host.example", "privileged": ["alice"]}})
    op = read_operator({op_mod.AGENT_OPERATOR_ADDR_ENV: "me@agent.example"})
    assert op.address == "me@agent.example"
    assert op.privileged == {"you@host.example", "alice"}
    assert not op.is_privileged("me@agent.example")


def test_session_operator_honours_the_env_address_without_privilege(capsys, monkeypatch):
    monkeypatch.setenv(op_mod.AGENT_OPERATOR_ADDR_ENV, "me@agent.example")
    assert _cli_json(capsys) == {"operator": "me@agent.example", "mailbox": None,
                                 "privileged": []}


# ---------- fail safe: a bad file never grants ------------------------------

@pytest.mark.parametrize("data, raw", [
    (None, "{not json"),
    (None, "[1, 2]"),
    (None, ""),
    ({"_locked": "operator"}, None),
    ({"_locked": [7]}, None),
    ({"operator": "you@host.example"}, None),
    ({"operator": ["you"]}, None),
    ({"operator": {"privileged": "alice"}}, None),
    ({"operator": {"privileged": {"alice": True}}}, None),
    ({"operator": {"privileged": 7}}, None),
    ({"operator": {"privileged": None}}, None),
    ({"operator": {"privileged": True}}, None),
    ({"operator": {"privileged": [7, None, ["alice"], {"alice": 1}, True]}}, None),
    ({"operator": {"address": 7, "mailbox": ["you"], "privileged": [1.5]}}, None),
])
def test_a_bad_settings_file_reads_as_nothing_configured(data, raw):
    _host_settings(data, raw)
    op = read_operator({})
    assert op == Operator(None, None, frozenset())
    assert not op.is_privileged("alice")


def test_a_privileged_string_is_ignored_but_the_operator_still_counts():
    """Not a list: ignored as if absent. Never split into characters, never
    read as one seat."""
    _host_settings({"operator": {"address": "you@host.example", "privileged": "alice"}})
    op = read_operator({})
    assert op.privileged == {"you@host.example"}
    assert not op.is_privileged("alice")
    assert not op.is_privileged("c")


def test_non_string_members_are_dropped_and_strings_kept():
    _host_settings({"operator": {"privileged": [7, "alice", None, {"bob": 1}, ["x"]]}})
    assert read_operator({}).privileged == {"alice"}


def test_an_unreadable_settings_file_grants_nothing():
    _host_settings({"operator": {"address": "you@host.example", "privileged": ["alice"]}})
    path = _settings_dir() / "session.json"
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running as a user who can read a mode-0 file")
        assert read_operator({}) == Operator(None, None, frozenset())
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize("mailbox", ["..", ".", "../you", "a/you", "you@box", ""])
def test_a_mailbox_that_is_not_one_plain_local_name_derives_nothing(tmp_path, mailbox):
    root = tmp_path / "mail" / "agents"
    (root / "you").mkdir(parents=True)
    _host_settings({"operator": {"mailbox": mailbox}})
    op = read_operator({op_mod.SESSION_MAIL_ROOT_ENV: str(root)})
    assert op.address is None
    assert not any("@" + FQDN in member for member in op.privileged)


# ---------- is_privileged and the value ---------------------------------------

def test_is_privileged_is_exact_after_strip_and_case_insensitive():
    _host_settings({"operator": {"address": "You@Host.Example", "privileged": ["Alice"]}})
    op = read_operator({})
    assert op.is_privileged("alice") and op.is_privileged("  ALICE ")
    assert op.is_privileged("you@host.example")
    assert not op.is_privileged("alic") and not op.is_privileged("alice2")
    assert not op.is_privileged("you")            # the box was not configured
    for who in ("", "   ", None, 7, ["alice"]):
        assert not op.is_privileged(who)


def test_operator_is_a_frozen_value():
    op = read_operator({})
    with pytest.raises(AttributeError):
        op.address = "you@host.example"
    assert isinstance(op.privileged, frozenset)


def test_read_operator_defaults_to_os_environ(monkeypatch):
    monkeypatch.setenv(op_mod.AGENT_OPERATOR_ADDR_ENV, "me@agent.example")
    assert read_operator().address == "me@agent.example"


def test_the_reader_is_re_exported_from_sessiontools():
    assert sessiontools.read_operator is read_operator
    assert sessiontools.Operator is Operator
    assert {"read_operator", "Operator"} <= set(sessiontools.__all__)


# ---------- the Fable gate reads through it ----------------------------------

def test_policy_operator_ask_address_is_the_readers_address(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "_resolve_operator_fqdn", lambda: "box7.gate.example.invalid")
    _host_settings({"operator": {"mailbox": "you", "privileged": ["alice"]}})
    environ = {op_mod.SESSION_MAIL_ROOT_ENV: _mail_root(tmp_path, "you")}
    assert policy.operator_ask_address(environ) == "you@box7.gate.example.invalid"
    _host_settings({"operator": {"privileged": ["alice"]}})
    assert policy.operator_ask_address(environ) is None       # privilege is not an address


# ---------- the CLI contract ---------------------------------------------------

def test_session_operator_prints_the_three_keys_privileged_sorted(capsys, monkeypatch,
                                                                  tmp_path):
    monkeypatch.setenv(op_mod.SESSION_MAIL_ROOT_ENV, _mail_root(tmp_path, "you"))
    _host_settings({"operator": {"mailbox": "you", "privileged": ["bob", "alice"]}})
    got = _cli_json(capsys)
    assert list(got) == ["operator", "mailbox", "privileged"]
    assert got == {"operator": f"you@{FQDN}", "mailbox": "you",
                   "privileged": ["alice", "bob", "you", f"you@{FQDN}"]}


def test_session_operator_with_a_bad_file_prints_nulls_and_exits_0(capsys):
    _host_settings(raw="{not json")
    assert _cli_json(capsys) == {"operator": None, "mailbox": None, "privileged": []}


def test_session_operator_refuses_an_unknown_flag_with_exit_2(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["operator", "--bogus"])
    assert excinfo.value.code == 2


def test_session_operator_help_exits_0(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["operator", "--help"])
    assert excinfo.value.code == 0
    assert "privileged" in capsys.readouterr().out


def test_session_help_lists_the_operator_verb(capsys):
    assert cli.main([]) == 0
    assert "  operator " in capsys.readouterr().out
