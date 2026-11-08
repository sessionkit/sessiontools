"""`sessiontools.web.bind.check_bind`: loopback or a literal tailnet address,
nothing else (kit-web-shell design §5). No socket is opened by these tests."""

from __future__ import annotations

import socket

import pytest

from sessiontools.web import bind
from sessiontools.web.bind import BindRefused, check_bind


@pytest.mark.parametrize(("given", "bound"), [
    ("127.0.0.1", "127.0.0.1"),
    ("::1", "::1"),
    ("0:0:0:0:0:0:0:1", "::1"),
    ("100.64.0.1", "100.64.0.1"),
    ("100.101.102.103", "100.101.102.103"),
    ("100.127.255.254", "100.127.255.254"),
    ("fd7a:115c:a1e0::1", "fd7a:115c:a1e0::1"),
    ("fd7a:115c:a1e0:ab12:4843:cd96:6258:b240", "fd7a:115c:a1e0:ab12:4843:cd96:6258:b240"),
])
def test_loopback_and_a_literal_tailnet_address_are_accepted(given, bound):
    assert check_bind(given) == bound


def test_the_default_is_ipv4_loopback():
    assert check_bind(bind.DEFAULT_HOST) == "127.0.0.1"


@pytest.mark.parametrize("given", ["0.0.0.0", "::", "0:0:0:0:0:0:0:0"])
def test_an_any_address_is_refused_naming_it(given):
    with pytest.raises(BindRefused, match="every interface") as e:
        check_bind(given)
    assert repr(given) in str(e.value)


def test_the_empty_host_is_refused():
    with pytest.raises(BindRefused, match="every interface"):
        check_bind("")


@pytest.mark.parametrize("given", [
    "8.8.8.8",            # public
    "1.1.1.1",
    "2606:4700::1111",
    "192.168.1.10",       # private LAN: still another interface
    "10.0.0.1",
    "172.16.0.1",
    "100.63.255.255",     # just below 100.64.0.0/10
    "100.128.0.0",        # just above it
    "fd7a:115c:a1e1::1",  # just outside fd7a:115c:a1e0::/48
    "fe80::1",            # link-local
    "fe80::1%lo0",        # scoped
    "127.0.0.2",          # loopback range, but not the loopback address
    "::ffff:127.0.0.1",   # v4-mapped: not ::1
    "169.254.1.1",
    "255.255.255.255",
])
def test_any_other_address_is_refused_naming_it(given):
    with pytest.raises(BindRefused, match="only 127.0.0.1|scoped") as e:
        check_bind(given)
    assert repr(given) in str(e.value)


@pytest.mark.parametrize("given", [
    "localhost",
    "example.com",
    "host.tail1234.example.net",
    "somehost",
    " 127.0.0.1",          # not exactly a literal
    "127.0.0.1 ",
    "[::1]",               # brackets are a URL's, not an address
    "127.0.0.1:8080",      # no port here
    "2130706433",          # 127.0.0.1 as an integer
    "0x7f000001",
    "127.1",
    "127.000.000.001",
])
def test_a_name_or_anything_not_exactly_a_literal_is_refused(given):
    with pytest.raises(BindRefused, match="not a literal IP address") as e:
        check_bind(given)
    assert repr(given) in str(e.value)


def test_a_non_string_is_refused():
    with pytest.raises(BindRefused):
        check_bind(None)  # type: ignore[arg-type]


def test_a_refusal_is_a_value_error():
    assert issubclass(BindRefused, ValueError)


def test_a_name_is_never_resolved(monkeypatch):
    """The check reads the string; it never asks DNS (a name could resolve
    to 127.0.0.1 today and anywhere tomorrow)."""
    def boom(*a, **k):
        raise AssertionError("check_bind resolved a name")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket, "gethostbyname", boom)
    with pytest.raises(BindRefused):
        check_bind("localhost")
