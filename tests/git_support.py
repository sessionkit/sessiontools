"""Hermetic git for test FIXTURES: the caller's git config never reaches them (ST-161).

A fixture that shells out to `git init` / `git commit` with the caller's
environment reads the caller's config. On one developer's host a
global `core.hooksPath` ran their pre-commit inside `test_export_ignore`'s fixture
and all 39 parametrised cases ERRORed; on another host the same hostile config
ERRORed 63 tests across four modules (repro-001).

`hermetic_env()` is the environ such a fixture runs git under: the caller's
environ with every inherited `GIT_*` dropped (GIT_DIR, GIT_CONFIG_COUNT and
friends: a keyed seat injects a signing block that way), then

- GIT_CONFIG_GLOBAL=/dev/null: no ~/.gitconfig, no $XDG_CONFIG_HOME/git/config;
- GIT_CONFIG_NOSYSTEM=1: no /etc/gitconfig;
- XDG_CONFIG_HOME=/dev/null: no DEFAULT $XDG_CONFIG_HOME/git/ignore or
  .../attributes, which git reads even with no global config at all
  (measured: a `*` in ~/.config/git/ignore hid every file from `git status`
  under GIT_CONFIG_GLOBAL=/dev/null);
- a fixed author and committer, so a commit needs no config.

`HOOKS_OFF` goes on the git command line too, belt and braces: no hook runs
in a fixture's repo, whatever its template installed.

This is for the TEST's own git, never the product's: a test of the
product's env handling (test_reap_act's inherited GIT_DIR case) sets that
env on purpose and must keep it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

HOOKS_OFF = ("-c", f"core.hooksPath={os.devnull}")

HERMETIC = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "XDG_CONFIG_HOME": os.devnull,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def hermetic_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """`environ` (default os.environ) minus every GIT_*, plus HERMETIC."""
    base = os.environ if environ is None else environ
    return {k: v for k, v in base.items() if not k.startswith("GIT_")} | HERMETIC
