"""Service files: tools' word to `sessionkit service` about each of its jobs.

One JSON file per service, `~/.sessionkit/tools/services/<name>.json`
(settings.services_dir), in the schema sessionkit owns (its services
design 1.2, version SCHEMA). The owner's install writes it with
`enabled: true`, its uninstall with `enabled: false`, and the file is
kept. This module is the ONE writer: each verb builds its record beside
its own LABEL (`service_record(enabled)`) and makes one call, `declare`.

THE WRITE IS ATOMIC. `replace_bytes` writes a temp file in the target's
own directory (fchmod 0644, write, flush, fsync), then renames it over the
target, so a reader sees the old file or the new one and never part of
either. Any failure before the rename removes the temp file and leaves the
old file byte-identical. The directory is not fsynced: the file is
rewritten at every `on` and `off`, and a rename lost to a power cut leaves
the previous file, which is still a valid declaration. A symlink at the
target is replaced, never followed. Two writers at once: the last rename
wins, and both files were whole.

THE FILE IS REWRITTEN WHOLE. Nothing is merged from the old file, so the
facts follow the release (the services design 1.6); a hand-edited file,
or one that is not JSON, is replaced.

A FAILED WRITE does not stop the launchd act it goes with: `declare`
returns the COULD_NOT_WRITE line and ok=False, the verb finishes and exits
1 (4 for default-model). The job is loaded (or booted out) as asked; a re-run is the fix.

Stdlib only, and nothing heavier than `settings`: `import sessiontools`
stays dependency-free. The name is `service_file`, not `services`, so it
is never mistaken for the kit's own `sessionkit.services`.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from sessiontools import settings

#: The value of the `service` key: "this file is a service", schema version 1.
SCHEMA = 1
#: Every tools label starts with this; a service's `name` is the rest of it
#: (the services design 1.2: the label after `dev.sessionkit.<package>.`,
#: dots allowed).
LABEL_PREFIX = f"dev.sessionkit.{settings.PACKAGES['session']}."
#: The keys every file carries, besides exactly one of `label` and `run`.
REQUIRED = ("service", "name", "enabled", "install")
#: The optional keys, in the order a file is written: `uninstall` beside
#: `install`, then the services design 1.2's table order. An unknown key is
#: refused here: the schema ignores one, but a builder in this package that
#: passes one has a typo.
OPTIONAL = ("uninstall", "description", "kind", "port", "protocol", "bind", "health",
            "hazard", "agent", "logs", "order", "note")
#: The file mode: the file holds no secret, and the plists beside it are 0644.
MODE = 0o644
#: The line a failed write prints. The help and docs quote it, so a user who
#: sees it knows the launchd act still ran.
COULD_NOT_WRITE = "service could not write {path}: {reason}; the previous file is unchanged"


def path(name: str, env: Mapping[str, str] | None = None) -> Path:
    """`~/.sessionkit/tools/services/<name>.json`."""
    return settings.services_dir(env) / f"{name}.json"


def shown(name: str) -> str:
    """The path as the help text spells it, `~/.sessionkit/tools/services/<name>.json`,
    built from the constants, never typed."""
    return (f"~/{settings.KIT_DIR}/{settings.PACKAGES['session']}/"
            f"{settings.SERVICES_DIR}/{name}.json")


def name_of(label: str) -> str:
    """The service's name: LABEL with LABEL_PREFIX stripped (never the last
    dotted word, so `bot.<agent>` stays whole). Raises ValueError for a label
    that is not a tools label."""
    if not label.startswith(LABEL_PREFIX) or label == LABEL_PREFIX:
        raise ValueError(f"label {label!r} does not start {LABEL_PREFIX!r}")
    return label[len(LABEL_PREFIX):]


def check(rec: Mapping) -> list[str]:
    """The problems the services design 1.2 names, as one line each; []
    when there are none: a missing required key, `label` and `run` both or
    neither, a label that does not start LABEL_PREFIX, a name that is not
    the label's tail. Also the types of the required keys, and an unknown
    key."""
    out = [f"missing required key {k!r}" for k in REQUIRED if k not in rec]
    has_label, has_run = "label" in rec, "run" in rec
    if has_label and has_run:
        out.append("both 'label' and 'run': a service has exactly one")
    elif not has_label and not has_run:
        out.append("missing required key 'label' (or 'run')")
    if "service" in rec and (rec["service"] != SCHEMA or isinstance(rec["service"], bool)):
        out.append(f"'service' is {rec['service']!r}, not {SCHEMA}")
    if "name" in rec and (not isinstance(rec["name"], str) or not rec["name"]):
        out.append("'name' is not a non-empty string")
    if "enabled" in rec and not isinstance(rec["enabled"], bool):
        out.append("'enabled' is not true or false")
    if "install" in rec and (not isinstance(rec["install"], str) or not rec["install"]):
        out.append("'install' is not a non-empty string")
    if has_run and (not isinstance(rec["run"], list) or not rec["run"]
                    or not all(isinstance(w, str) for w in rec["run"])):
        out.append("'run' is not a list of strings")
    if has_label:
        label = rec["label"]
        if not isinstance(label, str) or not label.startswith(LABEL_PREFIX) \
                or label == LABEL_PREFIX:
            out.append(f"'label' {label!r} does not start {LABEL_PREFIX!r}")
        elif "name" in rec and rec["name"] != name_of(label):
            out.append(f"'name' {rec['name']!r} is not the label's tail {name_of(label)!r}")
    known = {*REQUIRED, "label", "run", *OPTIONAL}
    out += [f"unknown key {k!r}" for k in rec if k not in known]
    return out


def record(*, enabled: bool, install: str, label: str | None = None,
           run: list[str] | None = None, name: str | None = None, **optional) -> dict:
    """The file's dict, in the written key order: service, name, label or
    run, enabled, install, then OPTIONAL's order. NAME defaults to the
    label's tail. A problem `check` finds raises ValueError: that is this
    package's own bug, never something to show a user."""
    if name is None and label is not None:
        name = name_of(label)
    rec: dict = {"service": SCHEMA}
    if name is not None:
        rec["name"] = name
    if label is not None:
        rec["label"] = label
    if run is not None:
        rec["run"] = list(run)
    rec["enabled"] = enabled
    rec["install"] = install
    unknown = [k for k in optional if k not in OPTIONAL]
    for k in OPTIONAL:
        if optional.get(k) is not None:
            rec[k] = optional[k]
    for k in unknown:
        rec[k] = optional[k]
    problems = check(rec)
    if problems:
        raise ValueError("bad service record: " + "; ".join(problems))
    return rec


def render(rec: Mapping) -> str:
    """The file's text: two-space JSON in the record's own key order, one
    trailing newline. The dry run prints exactly these bytes."""
    return json.dumps(rec, indent=2) + "\n"


def replace_bytes(target: Path, data: bytes, mode: int = MODE) -> None:
    """Put DATA at TARGET atomically: a temp file in TARGET's directory,
    fchmod MODE, write, flush, fsync, then one rename. On any failure the
    temp file is removed, TARGET is left as it was, and the error re-raised.
    The parent must exist."""
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def write(rec: Mapping, env: Mapping[str, str] | None = None) -> Path:
    """Write REC to its file, making the directory first. Raises OSError."""
    target = path(rec["name"], env)
    target.parent.mkdir(parents=True, exist_ok=True)
    replace_bytes(target, render(rec).encode())
    return target


def _written_line(rec: Mapping, target: Path) -> str:
    what = f"enabled: {json.dumps(rec['enabled'])}"
    if "run" in rec:
        what += f"; run: {' '.join(rec['run'])}"
    return f"service {target} ({what})"


def declare(rec: Mapping, dry_run: bool,
            env: Mapping[str, str] | None = None) -> tuple[list[str], bool]:
    """The one call each verb makes: (the lines to print, whether the write
    held). DRY_RUN prints the file under `service would write PATH:` and
    creates nothing, not even the folder. A write that fails gives the
    COULD_NOT_WRITE line and False; it never raises OSError."""
    target = path(rec["name"], env)
    if dry_run:
        return [f"service would write {target}:", render(rec).rstrip("\n")], True
    try:
        write(rec, env)
    except OSError as e:
        reason = e.strerror or str(e) or type(e).__name__
        return [COULD_NOT_WRITE.format(path=target, reason=reason)], False
    return [_written_line(rec, target)], True


def read(name: str, env: Mapping[str, str] | None = None) -> dict | None:
    """NAME's file as a dict, for a status; None when it is absent, unreadable,
    not JSON, or not a JSON object. Never raises."""
    try:
        data = json.loads(path(name, env).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
