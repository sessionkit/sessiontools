"""Helpers for tests that set a setting under a scratch HOME."""

from __future__ import annotations

import json
from pathlib import Path

from sessiontools import settings


def host(root: Path, tool: str) -> Path:
    """TOOL's settings directory under ROOT (a HOME): settings.host_dir,
    so no test spells the directory's shape itself."""
    return settings.host_dir(tool, {"HOME": str(root)})[0]


def write_session_settings(root: Path, data, name: str = "session.json") -> Path:
    """DATA as the `session` tool's host layer under ROOT (a HOME)."""
    folder = host(root, "session")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def shown(tool: str) -> str:
    """TOOL's host file as the code and docs spell it, `~/...`, derived
    from settings.host_dir."""
    fake = Path("/home-of-test")
    return f"~/{(host(fake, tool) / f'{tool}.json').relative_to(fake)}"
