# sessiontools - Makefile. `make` lists the targets; each line with ## is one.

.DEFAULT_GOAL := help
SHELL := /usr/bin/env bash
BUMP ?= patch
# every tracked *.sh, and every tracked file whose FIRST line is a sh/bash shebang:
# #!/bin/sh, #!/bin/bash, #!/usr/bin/env sh|bash (the old pattern had no bin/,
# so it matched none of them and shellcheck linted nothing)
SHELLSCRIPTS := $(shell { git ls-files -z | xargs -0 awk 'FNR==1 && /^\#!\/(usr\/)?bin\/(env +)?(ba)?sh([ \t]|$$)/ {print FILENAME}'; git ls-files '*.sh'; } | sort -u)
.PHONY: push publish help install test lint lock-check format check build release

help:  ## this list
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'

install:  ## a checkout you can import from and test (uv sync); nothing goes on the PATH - a library, by ruling
	@uv sync

test:  ## the pytest suite
	@uv run pytest -q

# uv.lock must agree with pyproject.toml. A version bump without a re-lock once
# left uv.lock pinning the old version, and every `uv run` rewrote
# uv.lock in each checkout it touched, a deploy checkout included.
# --offline: the check needs only pyproject.toml, uv.lock and the uv cache,
# never the index, so lint stays offline-safe. UV_PROJECT=<dir> points it at
# another tree; tests/test_lock_check.py does that.
lock-check:  ## uv.lock agrees with pyproject.toml (uv lock --check, offline); fails naming uv.lock
	@uv lock --check --offline || { echo "lock-check: uv.lock does not agree with pyproject.toml (or could not be checked offline) - run 'uv lock' and commit uv.lock" >&2; exit 1; }
	@echo "lock-check: uv.lock agrees with pyproject.toml"

lint: lock-check  ## lock-check; ruff check; shellcheck on every tracked shell script (see SHELLSCRIPTS)
	@uv run ruff check .
	@if [ -n "$(SHELLSCRIPTS)" ]; then command -v shellcheck >/dev/null || { echo "shellcheck not installed (brew install shellcheck)"; exit 1; }; shellcheck $(SHELLSCRIPTS) && echo "shellcheck: $(words $(SHELLSCRIPTS)) scripts clean"; else echo "shellcheck: no shell scripts"; fi

format:  ## ruff format
	@uv run ruff format .

check: lint test  ## lint, then test

build:  ## wheel + sdist into dist/
	@uv build

# release.py is stdlib-only, so python3 runs it: a plain `uv run` would re-lock
# the project before the script starts.
release:  ## bump (BUMP=patch|minor|major) or set (VERSION=X.Y.Z), then mirror, changelog, commit, tag
	@python3 scripts/release.py $(or $(VERSION),$(BUMP))

# Push the release tag BY NAME. `git push --tags` sent every local tag,
# the ready/ and approved/ merge-queue tags among them, to whatever origin was.
# The version is pyproject.toml's, read the way scripts/release.py reads it.
# A missing tag is refused before anything is pushed, the branch included.
push:  ## push the branch, then the ONE release tag vX.Y.Z by name (pyproject's version); refuses if that tag is not here
	@version="$$(uv version --short)" && [ -n "$$version" ] || { echo "push: could not read the version from pyproject.toml (uv version --short) - nothing pushed" >&2; exit 1; }; \
	tag="v$$version"; \
	git rev-parse -q --verify "refs/tags/$$tag" >/dev/null || { echo "push: tag $$tag is not in this repository - run 'make release' first; nothing pushed" >&2; exit 1; }; \
	git push origin HEAD && git push origin "$$tag"

# THE TOKEN NEVER TOUCHES A COMMAND LINE. argv is readable by every process
# on the machine (`ps`); a child's environment only by its own user. So the
# token reaches uv as UV_PUBLISH_TOKEN in uv's environment, from one
# `pass show` (one touch), and what `ps` shows is `uv publish dist/...`.
# The process list never includes the password. PYPI_TOKEN_PASS_PATH names the pass entry, e.g.
# pypi/token; set it in your shell profile.
publish: build  ## upload THIS version's wheel + sdist to PyPI; token via $$PYPI_TOKEN_PASS_PATH (pass) or $$UV_PUBLISH_TOKEN, never on a command line
	@dist="dist/$$(echo 'sessiontools' | tr '-' '_')-$$(uv version --short)"; \
	if [ -n "$$UV_PUBLISH_TOKEN" ]; then uv publish "$$dist"*; \
	elif [ -n "$$PYPI_TOKEN_PASS_PATH" ]; then UV_PUBLISH_TOKEN="$$(pass show "$$PYPI_TOKEN_PASS_PATH" | head -n1)" uv publish "$$dist"*; \
	else echo "publish: set PYPI_TOKEN_PASS_PATH to the pass entry holding your PyPI token (or UV_PUBLISH_TOKEN); the token is never put on a command line" >&2; exit 1; fi

# The pre-publish scrub check. The scanner and its word list are PRIVATE and never
# live in this repo: SESSIONKIT_SCAN names the release-scan tool (it finds its list,
# or takes SESSIONKIT_SCRUB_RULES). Exits non-zero on any P0 term in HEAD's tree.
.PHONY: scan scrub
scan:  ## scrub check: HEAD's tree against the private word list; fails on any P0 term
	@[ -n "$$SESSIONKIT_SCAN" ] || { echo "scan: set SESSIONKIT_SCAN to the release-scan tool" >&2; exit 2; }
	@"$$SESSIONKIT_SCAN" --here --fail-on P0

scrub: scan  ## the same check, by its other name
