"""The LOUD end of a skipped ledger gate.

`test_ledger_support.requires_counter` skips the ledger's token-count tests
on a host with no cl100k_base vocabulary. That skip is legal (CI, a
contributor's laptop), but one line per test in a `-q` run is invisible: a
check-runner gate reported `66 skipped` where a normal run reports 2, and a
ledger regression would have passed it.

So when any of them skips, the terminal summary ends with ONE banner,
just above pytest's own count line: how many skipped, and why.
`tests/conftest.py` imports the hook from here, so it fires for every run.

Kept apart from `test_ledger_support` on purpose: importing that module runs
the vocabulary probe (a `uv` child), and conftest must stay cheap for runs
that collect no ledger test.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

#: Every `requires_counter` skip reason starts with this, and nothing else's does.
TAG = "LEDGER TOKEN-COUNT GATES NOT RUN"

_SKIPPED = "Skipped: "


def skip_reason(report) -> str | None:
    """A skipped report's reason text, or None when it carries none."""
    longrepr = getattr(report, "longrepr", None)
    if not (isinstance(longrepr, tuple) and len(longrepr) == 3):
        return None
    reason = str(longrepr[2])
    return reason.removeprefix(_SKIPPED)


def banner_lines(reports: Iterable) -> list[str]:
    """The banner for these skipped reports: [] when no ledger gate skipped,
    else the title line and one line per distinct reason (normally one)."""
    reasons = Counter(r for r in map(skip_reason, reports) if r and r.startswith(TAG))
    total = sum(reasons.values())
    if not total:
        return []
    noun = "test" if total == 1 else "tests"
    return [f"{TAG}: {total} ledger token-count {noun} SKIPPED"] + [
        f"{n} x {reason}" for reason, n in reasons.most_common()]


def pytest_terminal_summary(terminalreporter) -> None:
    """Write the banner, if any, at the end of the terminal summary."""
    lines = banner_lines(terminalreporter.stats.get("skipped", []))
    if not lines:
        return
    title, *reasons = lines
    terminalreporter.write_sep("!", title, red=True, bold=True)
    for line in reasons:
        terminalreporter.write_line(line, red=True)
    terminalreporter.write_sep("!", "a ledger regression would NOT fail this run",
                               red=True, bold=True)
