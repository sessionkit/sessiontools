"""Liveness: the pid is running AND it is the process the sidecar names."""

from __future__ import annotations

import pytest

from sessiontools import procs
from sessiontools.procs import Proc

T0 = procs.parse_ctime("Sun Jun  9 17:00:00 2030")


def test_ctime_is_utc_and_a_padded_day_parses():
    assert T0 == 1907254800.0
    assert procs.parse_ctime("Sun Jun  9 01:02:03 2030") == procs.parse_ctime("Sun Jun 9 01:02:03 2030")


@pytest.mark.parametrize("junk", [None, "", "yesterday", "Sun Jun  9 17:00:00"])
def test_what_is_not_ctime_is_none(junk):
    assert procs.parse_ctime(junk) is None


@pytest.mark.parametrize("started, live", [
    (T0, True), (T0 + 2, True), (T0 - 2, True),   # the 2 s tolerance
    (T0 + 3, False),                               # a reused pid
    (None, False),                                 # ps could not say
])
def test_same_process(started, live):
    table = {100: Proc(100, 1, started)}
    assert procs.same_process(100, "Sun Jun  9 17:00:00 2030", table) is live


def test_a_pid_ps_does_not_list_is_not_live():
    assert not procs.same_process(100, "Sun Jun  9 17:00:00 2030", {})
    assert not procs.same_process(None, "Sun Jun  9 17:00:00 2030", {1: Proc(1, 0, T0)})


def test_ancestors_walks_up_and_survives_a_cycle():
    table = {5: Proc(5, 4, T0), 4: Proc(4, 1, T0), 1: Proc(1, 0, T0)}
    assert list(procs.ancestors(5, table)) == [5, 4, 1]
    assert list(procs.ancestors(7, {7: Proc(7, 8, T0), 8: Proc(8, 7, T0)})) == [7, 8]


# --- Linux: procStart in clock ticks since boot -------------------------------------
# Claude Code on Linux writes procStart as field 22 of /proc/<pid>/stat, ticks
# since boot. procps prints lstart as btime + ticks // HZ, so that sum is the
# second the process table shows. These run anywhere: /proc/stat is a file of
# the test's own and the ticks rate is pinned.

LINUX_LSTART = "Sun Jun  9 17:00:00 2030"            # what ps prints for the process
LINUX_TICKS = 88300                                  # its start, 883 s after boot
LINUX_BOOT = int(procs.parse_ctime(LINUX_LSTART)) - LINUX_TICKS // 100


@pytest.fixture
def linux_boot(tmp_path, monkeypatch):
    """A Linux /proc/stat with a `btime` line, and 100 ticks a second.
    raising=False: the regression test must fail on its ASSERT, not on a
    missing name, against code from before the Linux encoding was read."""
    stat = tmp_path / "stat"
    stat.write_text(f"cpu  1 2 3 4\nintr 5\nbtime {LINUX_BOOT}\nprocesses 6\n")
    monkeypatch.setattr(procs, "PROC_STAT", str(stat), raising=False)
    monkeypatch.setattr(procs, "clock_ticks", lambda: 100, raising=False)
    return stat


def test_a_linux_sidecar_in_ticks_is_the_same_process(linux_boot):
    table = {5182: Proc(5182, 5154, procs.parse_ctime(LINUX_LSTART))}
    assert procs.same_process(5182, str(LINUX_TICKS), table)


@pytest.mark.parametrize("offset, live", [
    (0, True), (200, True), (-200, True),     # the 2 s tolerance, in ticks
    (300, False), (-300, False),              # a reused pid
])
def test_linux_ticks_keep_the_tolerance(linux_boot, offset, live):
    table = {5182: Proc(5182, 5154, procs.parse_ctime(LINUX_LSTART))}
    assert procs.same_process(5182, str(LINUX_TICKS + offset), table) is live


def test_linux_ticks_become_the_second_ps_prints(linux_boot):
    assert procs.parse_start(str(LINUX_TICKS)) == procs.parse_ctime(LINUX_LSTART)
    assert procs.parse_start(str(LINUX_TICKS + 99)) == procs.parse_ctime(LINUX_LSTART)  # ps truncates


def test_ctime_still_reads_as_ctime(linux_boot):
    assert procs.parse_start(LINUX_LSTART) == procs.parse_ctime(LINUX_LSTART)


@pytest.mark.parametrize("junk", [None, "", "12ab", "-88300", "88300 ", "\uff18\uff18"])
def test_what_is_neither_encoding_is_none(linux_boot, junk):
    assert procs.parse_start(junk) is None


@pytest.mark.parametrize("digits", [21, 400, 5000])
def test_digits_too_long_for_a_start_are_none_not_a_crash(linux_boot, digits):
    """The kernel's starttime is a u64, at most 20 digits. A longer run (a
    damaged sidecar) is not live; it must not raise out of every liveness
    check and take `session list` down with it."""
    table = {5182: Proc(5182, 5154, procs.parse_ctime(LINUX_LSTART))}
    assert procs.parse_start("9" * digits) is None
    assert not procs.same_process(5182, "9" * digits, table)
    assert procs.parse_start("1" * 20) is not None   # the u64 width still reads


@pytest.mark.parametrize("stat", [None, "cpu  1 2 3 4\nprocesses 6\n", "btime soon\n"])
def test_ticks_without_a_boot_time_are_not_live(tmp_path, monkeypatch, stat):
    path = tmp_path / "stat"
    if stat is not None:
        path.write_text(stat)
    monkeypatch.setattr(procs, "PROC_STAT", str(path))
    assert procs.boot_time() is None
    assert procs.parse_start(str(LINUX_TICKS)) is None
    table = {5182: Proc(5182, 5154, procs.parse_ctime(LINUX_LSTART))}
    assert not procs.same_process(5182, str(LINUX_TICKS), table)


@pytest.mark.parametrize("sysconf", [lambda name: -1, lambda name: (_ for _ in ()).throw(ValueError(name))])
def test_clock_ticks_falls_back_when_the_system_cannot_say(monkeypatch, sysconf):
    monkeypatch.setattr(procs.os, "sysconf", sysconf)
    assert procs.clock_ticks() == procs.DEFAULT_CLOCK_TICKS


def test_the_real_ps_lists_this_process_with_a_start():
    import os
    table = procs.table()
    assert os.getpid() in table and table[os.getpid()].started is not None


# --- the command column (ST-134) ------------------------------------------------------

PS_WITH_COMMANDS = """\
    1     0 Sun Dec  8 01:00:00 2030     /sbin/launchd
 5000     1 Sun Jun  9 01:02:03 2030     claude --session-id x  --two  spaces
 5001  5000 Sun Dec  8 01:00:00 2030
junk line
"""


def test_a_table_with_commands_splits_the_five_words_of_lstart():
    table = procs.parse_table(PS_WITH_COMMANDS, commands=True)
    assert table[1] == Proc(1, 0, procs.parse_ctime("Sun Dec  8 01:00:00 2030"), "/sbin/launchd")
    assert table[5000].started == procs.parse_ctime("Sun Jun 9 01:02:03 2030")
    assert table[5000].command == "claude --session-id x  --two  spaces"
    assert table[5001].command == ""        # ps printed no command
    assert set(table) == {1, 5000, 5001}


def test_a_table_without_commands_is_the_one_callers_always_read():
    out = "  42     1 Sun Jun  9 17:00:00 2030\n"
    assert procs.parse_table(out) == {42: Proc(42, 1, T0)}
    assert procs.parse_table(out)[42].command is None


def test_children_index_is_in_pid_order():
    table = {9: Proc(9, 1, T0), 3: Proc(3, 1, T0), 4: Proc(4, 3, T0), 1: Proc(1, 0, T0)}
    assert procs.children(table) == {0: [1], 1: [3, 9], 3: [4]}


def test_the_real_ps_with_commands_names_this_process():
    import os
    table = procs.table(commands=True)
    me = table[os.getpid()]
    assert me.started is not None and me.command and "python" in me.command.lower()
