# Building and testing sessiontools

A checkout you can import from and test, then `make check` before you
push. In the source repository, read `openspec/specs/` before you
build: it is what this package is meant to be.

- `make install` runs `uv sync`: a checkout you can import from and
  test.
- `make` alone prints the targets, one per line.
- `make check` is lint, then test.
- `make test` runs the pytest suite.
- `make lint` is `lock-check`, then ruff, then shellcheck on every
  tracked shell script.
- `make lock-check` fails when `uv.lock` disagrees with
  `pyproject.toml`; it runs offline.
- Run the CLI from a checkout with `uv run session ...`.

## The rules and the specs

`openspec/` and `CLAUDE.md` are in the source repository, not in release
archives.

- `openspec/specs/` is RATIFIED: the maintainer ratified it, and it is
  the contract.
- `openspec/changes/` is PROPOSED: nothing there is ratified yet.
- `AGENTS.md` carries the three rules: our words, never a harness's;
  exactly one console script, `session`; and two runtime dependencies,
  PyYAML and jsonschema, each behind one module. `CLAUDE.md` imports it:
  its first line is `@AGENTS.md`, so Claude Code reads the same file.

## Releases

- `make release` bumps the version (`BUMP=patch|minor|major`, or
  `VERSION=X.Y.Z`), mirrors it into `.claude-plugin/*.json`, stamps
  `CHANGELOG.md`, commits and tags. It never pushes.
- A release changes no dependency pin. The script runs
  `uv version --frozen`, on a dry run too, and writes the new version onto
  `uv.lock`'s own `sessiontools` line as text, so nothing is resolved and
  `uv lock --check` still passes. `make release` runs it with `python3`,
  not `uv run`, which would re-lock first.
- Every refusal comes before the first write: a tag `vX.Y.Z` that already
  exists, a `uv.lock` without exactly one root `sessiontools` block
  holding one `version` line, a `CHANGELOG.md` with no `[Unreleased]`, or a
  plugin manifest with no `version` field. A refused run leaves
  `git status` clean.
- `make push` pushes the branch, then the one release tag by name:
  `git push origin HEAD && git push origin vX.Y.Z`, with the version from
  `pyproject.toml`. It never pushes `--tags`, which would send every local
  tag, the `ready/` and `approved/` tags among them. If `vX.Y.Z` is not
  tagged here, it refuses and pushes nothing. `make release` prints the
  same line.
- `make publish` uploads this version to PyPI. The token comes from
  `$PYPI_TOKEN_PASS_PATH` (a `pass` entry) or `$UV_PUBLISH_TOKEN`, never
  a command line.
- `make scan` checks HEAD's tree against the private word list before a
  publish (`$SESSIONKIT_SCAN`).

## History

- 0.0.1, 2030-06-09: born as a library.
- 2030-06-09: sessiontools took over the `session` command when the
  session repo was retired. `list` and `get`
  were ported onto the library, `start` ported as `sessiontools.start`,
  and `send` disabled (2030-12-08).
- On PyPI as `sessiontools`: 0.0.1, 0.0.2, 0.0.3 and 0.1.0.
- The [CHANGELOG][changelog] has every release.

[changelog]: ../CHANGELOG.md
