"""The ONE host rule (ST-258, mirroring sessionmail SM-941; D6).

`operator.short_host` is where sessiontools derives "this host" for a
writer: the derived operator address (`host_part`), start/policy.py, and the
launch-info Machine line.

SPEC_CASES pins the NAME rows N1-N12 of SM-941's table ROW FOR ROW, the row
ids kept as the test ids:

    repo sessionmail, commit f10c4899a18d186ebec83733fe05faa7b41011bf,
    openspec/changes/host-rule/proposal.md, "The table of cases" (NAME)

Its IS-ME rows (I1-I14) are the mail drop's "is this domain me?" set, not
a writer's host, so they are not pinned here. N6 is the D6 rename; N12 is
the empty case, where `host_part` returns None and the writer refuses.

BINDING_EXTRAS adds the rows of the binding a-e (from sessionmail's super)
that no N row covers on its own:

  (a) lowercased from every source: env, setting, live;
  (b) whitespace-stripped from every source, and a value empty after the
      strip is UNSET, so the next source is tried;
  (c) a DOTTED env or setting value is KEPT WHOLE; only the LIVE hostname
      is cut to its first label;
  (d) order: SESSIONMAIL_HOST, SESSION_MAIL_HOST, the `mail` tool's `host`
      setting, the live short hostname;
  (e) empty everywhere -> "" and the writer refuses (`host_part` -> None).

Every row is simulated: an env dict, a `mail` setting written under the
walled HOME conftest sets, and an injected hostname. Nothing reads the live
host or the live settings.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from settings_support import host

from sessiontools import operator as op_mod
from sessiontools.start import context as c

UNSET = object()  # no `mail` settings file at all


@pytest.fixture(autouse=True)
def _walled(monkeypatch):
    """The caller's own host variables never leak in, the live hostname is
    never consulted, and the FQDN never comes from the network."""
    assert "no-home" in os.environ["HOME"], "the walled HOME, never the live one"
    for name in op_mod.MAIL_HOST_ENVS:
        monkeypatch.delenv(name, raising=False)

    def _no_live_hostname():
        raise AssertionError("the live hostname was read")

    monkeypatch.setattr(op_mod, "_resolve_hostname", _no_live_hostname)
    monkeypatch.setattr(op_mod, "_resolve_fqdn", lambda: "")


def _set_mail_host(value) -> None:
    """Write the `mail` tool's host setting (or leave it unset)."""
    if value is UNSET:
        return
    mail_dir = host(Path(os.environ["HOME"]), op_mod.MAIL_SETTINGS_TOOL)
    mail_dir.mkdir(parents=True, exist_ok=True)
    (mail_dir / "mail.json").write_text(json.dumps({op_mod.MAIL_HOST_KEY: value}))


# (row id, env, mail `host` setting, live hostname, expected short host)
# SM-941 NAME rows, row for row:
# (row id, env, mail `host` setting, gethostname(), expected NAME)
SPEC_CASES = [
    ("N1", {"SESSIONMAIL_HOST": "pinned"}, "MacBook-Air", "live.example.invalid", "pinned"),
    ("N2", {"SESSIONMAIL_HOST": "Pinned"}, UNSET, "live.example.invalid", "pinned"),
    ("N3", {"SESSIONMAIL_HOST": "   ", "SESSION_MAIL_HOST": "Old"}, UNSET,
     "live.example.invalid", "old"),
    ("N4", {"SESSIONMAIL_HOST": "Foo.Local"}, UNSET, "live.example.invalid", "foo.local"),
    ("N5", {"SESSION_MAIL_HOST": "old"}, "MacBook-Air", "admins-Air.localdomain", "old"),
    ("N6", {}, "MacBook-Air", "admins-Air.localdomain", "macbook-air"),
    ("N7", {}, " host.lan ", "admins-Air.localdomain", "host.lan"),
    ("N8", {}, "  ", "admins-Air.localdomain", "admins-air"),
    ("N9", {}, 42, "admins-Air.localdomain", "admins-air"),
    ("N10", {}, UNSET, "MacBook-Air.local", "macbook-air"),
    ("N11", {}, UNSET, "ms9.example.invalid", "ms9"),
    ("N12", {}, UNSET, "", ""),
]


@pytest.mark.parametrize("env,setting,live,expected",
                         [row[1:] for row in SPEC_CASES],
                         ids=[row[0] for row in SPEC_CASES])
def test_the_spec_name_rows(env, setting, live, expected):
    _set_mail_host(setting)
    assert op_mod.short_host(env, lambda: live) == expected


def test_n12_the_writer_refuses():
    """N12: an empty hostname and nothing pinned derive no host part."""
    assert op_mod.host_part({}, resolve_fqdn=lambda: "",
                            resolve_hostname=lambda: "") is None


# The binding a-e, the rows no N row covers alone:
# (row id, env, mail `host` setting, live hostname, expected short host)
BINDING_EXTRAS = [
    ("a-env-older", {"SESSION_MAIL_HOST": "MacBook-Air"}, UNSET, "x", "macbook-air"),
    ("a-setting", {}, "MacBook-Air", "x", "macbook-air"),
    ("b-env-older", {"SESSION_MAIL_HOST": "\tBox7 "}, UNSET, "x", "box7"),
    ("b-live", {}, UNSET, "  Box7  ", "box7"),
    ("b-env-blank-tries-setting", {"SESSIONMAIL_HOST": " ",
                                   "SESSION_MAIL_HOST": "\t"}, "Studio", "x", "studio"),
    ("b-setting-empty-tries-live", {}, "", "Box7", "box7"),
    ("c-env-older-dotted", {"SESSION_MAIL_HOST": "Foo.local"}, UNSET, "x", "foo.local"),
    ("c-live-cut-deep", {}, UNSET, "Box7.tail1234.ts.net", "box7"),
    ("d-newer-beats-older", {"SESSIONMAIL_HOST": "Newer",
                             "SESSION_MAIL_HOST": "Older"}, "Setting", "live", "newer"),
    ("d6-env-pin", {"SESSIONMAIL_HOST": "MacBook-Air"}, UNSET,
     "admins-Air.localdomain", "macbook-air"),
    ("e-blank", {"SESSIONMAIL_HOST": " ", "SESSION_MAIL_HOST": ""}, "  ", "  ", ""),
]


@pytest.mark.parametrize("env,setting,live,expected",
                         [row[1:] for row in BINDING_EXTRAS],
                         ids=[row[0] for row in BINDING_EXTRAS])
def test_the_binding_extras(env, setting, live, expected):
    _set_mail_host(setting)
    assert op_mod.short_host(env, lambda: live) == expected


def test_blank_everywhere_the_writer_refuses():
    """(e), all sources blank after the strip: no host part."""
    _set_mail_host("  ")
    assert op_mod.host_part({"SESSIONMAIL_HOST": " ", "SESSION_MAIL_HOST": ""},
                            resolve_fqdn=lambda: "",
                            resolve_hostname=lambda: "  ") is None


def test_the_pin_is_the_writers_host_after_a_live_rename():
    """D6: the address a writer derives keeps the pinned host when the live
    hostname changes mid-session."""
    env = {"SESSIONMAIL_HOST": "MacBook-Air"}
    assert op_mod.host_part(env, resolve_fqdn=lambda: "admins-Air.localdomain",
                            resolve_hostname=lambda: "admins-Air.localdomain") == "macbook-air"


# The launch-info Machine line uses the same rule (rca-001: it printed the raw
# hostname, so it and the derived address could disagree).

def test_the_machine_line_is_the_pinned_host_not_the_live_name(monkeypatch):
    monkeypatch.setenv("SESSIONMAIL_HOST", "MacBook-Air")
    monkeypatch.setattr(op_mod, "_resolve_hostname", lambda: "admins-Air.localdomain")
    block = c.generate_launch_info(now="now")
    assert "- **Machine**: macbook-air\n" in block
    assert "admins-Air" not in block


def test_the_machine_line_unpinned_is_the_live_short_host(monkeypatch):
    monkeypatch.setattr(op_mod, "_resolve_hostname", lambda: "admins-Air.localdomain")
    assert "- **Machine**: admins-air\n" in c.generate_launch_info(now="now")


def test_the_machine_line_follows_the_mail_setting(monkeypatch):
    _set_mail_host("Studio")
    monkeypatch.setattr(op_mod, "_resolve_hostname", lambda: "admins-Air.localdomain")
    assert "- **Machine**: studio\n" in c.generate_launch_info(now="now")


def test_the_machine_line_agrees_with_the_derived_host(monkeypatch):
    monkeypatch.setenv("SESSION_MAIL_HOST", " Foo.local ")
    monkeypatch.setattr(op_mod, "_resolve_hostname", lambda: "admins-Air.localdomain")
    expected = op_mod.short_host(os.environ)
    assert expected == "foo.local"
    assert f"- **Machine**: {expected}\n" in c.generate_launch_info(now="now")


def test_the_machine_line_is_omitted_when_no_host_is_derived(monkeypatch):
    """A field not supplied is omitted: "Machine: " would be a fabricated fact."""
    monkeypatch.setattr(op_mod, "_resolve_hostname", lambda: "")
    assert "Machine" not in c.generate_launch_info(now="now")


def test_an_injected_host_is_still_printed_as_given():
    assert "- **Machine**: TestHost\n" in c.generate_launch_info(now="now", host="TestHost")
