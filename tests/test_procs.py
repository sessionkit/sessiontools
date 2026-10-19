"""Liveness: the pid is running AND it is the process the sidecar names."""

from __future__ import annotations

import pytest

from sessiontools import procs
from sessiontools.procs import Proc

T0 = procs.parse_ctime("Wed Jan  2 17:00:00 2030")


def test_ctime_is_utc_and_a_padded_day_parses():
    assert T0 == 1893603600.0
    assert procs.parse_ctime("Tue Jun  4 01:02:03 2030") == procs.parse_ctime("Tue Jun 4 01:02:03 2030")


@pytest.mark.parametrize("junk", [None, "", "yesterday", "Wed Jan  2 17:00:00"])
def test_what_is_not_ctime_is_none(junk):
    assert procs.parse_ctime(junk) is None


@pytest.mark.parametrize("started, live", [
    (T0, True), (T0 + 2, True), (T0 - 2, True),   # the 2 s tolerance
    (T0 + 3, False),                               # a reused pid
    (None, False),                                 # ps could not say
])
def test_same_process(started, live):
    table = {100: Proc(100, 1, started)}
    assert procs.same_process(100, "Wed Jan  2 17:00:00 2030", table) is live


def test_a_pid_ps_does_not_list_is_not_live():
    assert not procs.same_process(100, "Wed Jan  2 17:00:00 2030", {})
    assert not procs.same_process(None, "Wed Jan  2 17:00:00 2030", {1: Proc(1, 0, T0)})


def test_ancestors_walks_up_and_survives_a_cycle():
    table = {5: Proc(5, 4, T0), 4: Proc(4, 1, T0), 1: Proc(1, 0, T0)}
    assert list(procs.ancestors(5, table)) == [5, 4, 1]
    assert list(procs.ancestors(7, {7: Proc(7, 8, T0), 8: Proc(8, 7, T0)})) == [7, 8]


def test_the_real_ps_lists_this_process_with_a_start():
    import os
    table = procs.table()
    assert os.getpid() in table and table[os.getpid()].started is not None


# --- the command column (ST-134) ------------------------------------------------------

PS_WITH_COMMANDS = """\
    1     0 Wed Jul 10 01:00:00 2030     /sbin/launchd
 5000     1 Tue Jun  4 01:02:03 2030     claude --session-id x  --two  spaces
 5001  5000 Wed Jul 10 01:00:00 2030
junk line
"""


def test_a_table_with_commands_splits_the_five_words_of_lstart():
    table = procs.parse_table(PS_WITH_COMMANDS, commands=True)
    assert table[1] == Proc(1, 0, procs.parse_ctime("Wed Jul 10 01:00:00 2030"), "/sbin/launchd")
    assert table[5000].started == procs.parse_ctime("Tue Jun 4 01:02:03 2030")
    assert table[5000].command == "claude --session-id x  --two  spaces"
    assert table[5001].command == ""        # ps printed no command
    assert set(table) == {1, 5000, 5001}


def test_a_table_without_commands_is_the_one_callers_always_read():
    out = "  42     1 Wed Jan  2 17:00:00 2030\n"
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
