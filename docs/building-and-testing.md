# Building and testing sessiontools

A checkout you can import from and test, then `make check` before you
push. In the source repository, read `openspec/specs/` before you
build: it is what this package is meant to be.

```console
make install     # uv sync: a checkout you can import from and test
make             # list the targets
make check       # lint, then test
make test        # the pytest suite
```

- `make` alone prints the targets, one per line.
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
- `make push` pushes the branch and the tags.
- `make publish` uploads this version to PyPI. The token comes from
  `$PYPI_TOKEN_PASS_PATH` (a `pass` entry) or `$UV_PUBLISH_TOKEN`, never
  a command line.
- `make scan` checks HEAD's tree against the private word list before a
  publish (`$SESSIONKIT_SCAN`).

## History

- 0.0.1, 2030-07-18: born as a library.
- 2030-07-29: sessiontools took over the `session` command when the
  session repo was retired. `list` and `get`
  were ported onto the library, `start` ported as `sessiontools.start`,
  and `send` disabled (2030-07-30).
- On PyPI as `sessiontools`: 0.0.1, 0.0.2, 0.0.3 and 0.1.0.
- The [CHANGELOG][changelog] has every release.

[changelog]: ../CHANGELOG.md
