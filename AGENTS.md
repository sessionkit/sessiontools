# sessiontools

The harness layer: sessions, their status, how long idle - one driver per
harness, a library first - and the `session` command.

## REFER TO THE SPECS BEFORE BUILDING ANYTHING

The specs are the record of what this package is meant to do. They live
in the source repository, beside this file; release archives leave them
out. A RATIFIED spec is the contract: the maintainer ratified it. A
PROPOSED change is not ruled yet, and nothing becomes a ratified spec
until the maintainer ratifies it.

Three rules:

1. **Our words, never a harness's.** `busy`, not busy, unknown. A driver
   translates; nothing above it sees `shell` or `waiting`.
2. **Exactly one console script: `session`.** Ported as package code, not
   copied. `tests/test_cli.py` pins it.
3. **Two runtime dependencies, PyYAML and jsonschema, each behind one
   module.** sessionmail depends on this; a stranger installing
   sessionmail pulls both. Agent front matter is YAML, so PyYAML is
   allowed; only `sessiontools.start.agentfile` imports it, for
   `session start` and the settings reader's agent layer. jsonschema
   checks `session settings` against each tool's schema, with no
   configuration library; only `sessiontools.settings.check_schema`
   imports it. `import sessiontools` stays dependency-free, and
   `tests/test_cli.py` pins all of it. Nothing else is added without the
   maintainer's agreement. Dev tools live in the dev group.

```console
make install    # uv sync - a checkout you can import from and test
make            # check, then test
```
