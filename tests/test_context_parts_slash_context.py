"""Reading a recorded `/context` run back out of a transcript (ST-26 do-002,
design section 3.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from sessiontools.context_parts import slash_context as S

REPORT = """## Context Usage

**Model:** claude-opus-5-5
**Tokens:** 19.5k / 1m (2%)

### Estimated usage by category

| Category | Tokens | Percentage |
|----------|--------|------------|
| System prompt | 2.1k | 0.2% |
| System tools | 11.9k | 1.2% |
| System tools (deferred) | 19.9k | 2.0% |
| Custom agents | 54 | 0.0% |
| Free space | 947.5k | 94.8% |
| Big | 1.25m | 0% |

### Custom Agents

| Agent Type | Source | Tokens |
|------------|--------|--------|
| worker | user | 54 |
"""


@pytest.mark.parametrize("text, suffix, want", [
    ("2.1", "k", 2100), ("54", "", 54), ("1,234", "", 1234), ("1.25", "m", 1_250_000),
    ("947.5", "K", 947_500), ("x", "", None),
])
def test_parse_tokens(text, suffix, want):
    assert S.parse_tokens(text, suffix) == want


def test_parse_report_reads_the_category_table_only():
    rows = S.parse_report(REPORT)
    assert rows == {"System prompt": 2100, "System tools": 11900,
                    "System tools (deferred)": 19900, "Custom agents": 54,
                    "Free space": 947500, "Big": 1_250_000}
    assert "worker" not in rows          # the per-item table after it is not read


@pytest.mark.parametrize("text", ["", "no report here", "## Context Usage\nno table",
                                  "## Context Usage\n### Estimated usage by category\n"])
def test_parse_report_is_none_when_not_a_report(text):
    assert S.parse_report(text) is None


def meta(content, **extra) -> dict:
    return {"type": "user", "isMeta": True, "message": {"role": "user", "content": content},
            **extra}


def test_from_record_needs_an_is_meta_user_record():
    assert S.from_record(meta(REPORT)) is not None
    assert S.from_record({**meta(REPORT), "isMeta": False}) is None
    assert S.from_record({**meta(REPORT), "type": "assistant"}) is None
    assert S.from_record(meta("hello")) is None
    blocks = meta([{"type": "text", "text": REPORT}])
    assert S.from_record(blocks).rows["System prompt"] == 2100


def test_core_rows_are_prompt_tools_and_mcp_tools_in_order():
    snap = S.from_record(meta(REPORT))
    assert snap.core_rows() == {"System prompt": 2100, "System tools": 11900}
    assert "System tools (deferred)" not in snap.core_rows()


def test_the_time_is_read_and_rendered_in_any_zone():
    snap = S.from_record(meta(REPORT, timestamp="2030-06-09T06:00:00.000Z"), line=7)
    assert snap.at == datetime(2030, 6, 9, 6, 0, 0, tzinfo=UTC)
    assert snap.line == 7
    assert snap.hhmm(UTC) == "06:00"
    assert snap.hhmm(timezone(timedelta(hours=11))) == "17:00"


@pytest.mark.parametrize("value", [None, "", "not a time", 12])
def test_no_readable_time_is_none(value):
    snap = S.from_record(meta(REPORT, timestamp=value))
    assert snap.at is None and snap.hhmm() is None


def test_a_naive_timestamp_is_taken_as_utc():
    assert S.parse_timestamp("2030-06-09T06:00:00").tzinfo is not None
