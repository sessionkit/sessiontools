"""`session default-model show|list|enforce|watch|unit`: the verb over `default_model`.

The core (`sessiontools.default_model`) reads the setting and makes the
edit; `default_model_watch` runs it on each change and renders the units.
This module parses argv, calls them and prints what they found. Nothing
here writes a file but through `default_model.enforce`, and nothing
installs or loads a unit: `unit` prints one.

    session default-model                      same as `show`
    session default-model show [--json]        the pin, settings.json's "model", in sync or not
    session default-model list [--json]        the models a pin may name
    session default-model enforce [--dry-run]  ONE pass: put "model" back; unset: nothing
    session default-model watch [--interval S] enforce now, then on every change (foreground)
    session default-model unit launchd|systemd PRINT a unit that runs enforce on every change

Exit codes: 0 done, in sync or unset; 2 usage; 3 refused, nothing written;
4 a backup, write, read-back or log line failed; 5 settings.json unreadable.
`show` exits with the code `enforce` would, so a script can ask first.

Stdlib only, and nothing that loads PyYAML: `import sessiontools.cli` and
this verb's path never may (AGENTS.md rule 3).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from sessiontools import default_model as dm
from sessiontools import default_model_watch as dmw

PROG = "session default-model"

SUBVERBS = (
    ("show", "the pin, settings.json's \"model\", and whether they agree (the default)"),
    ("list", "the models a pin may name; Fable rows are listed, never pinned"),
    ("enforce", "one pass: if the pin is set and \"model\" differs, put it back"),
    ("watch", ("foreground: enforce now, then again each time settings.json or the pin "
               "changes")),
    ("unit", ("print a launchd agent or systemd units that run enforce on every change; "
              "installs nothing")),
)

#: Said under `list`, both forms: the list is shipped, not measured.
NOT_LIVE = ("These are the models this release of sessiontools knows. It is not a live "
            "check of what your account may use.")

EPILOG = """\
The pin is the `session` tool's setting model.default, read from the
template and host layers only, for example in
~/.sessionkit/tools/settings/session.json:

    {"model": {"default": "opus"}}

Unset, nothing happens: enforce reads no settings.json, writes no backup,
no log, nothing. Set, enforce edits only the top-level "model" member of
$CLAUDE_CONFIG_DIR/settings.json (else ~/.claude/settings.json), backs the
old file up first, and reads the new one back. Fable is never pinned.

To keep it pinned, run enforce on every change. `unit` prints what does
that; save it and load it yourself (nothing here installs or loads one).
Render it from the shell whose settings it should guard: it carries
CLAUDE_CONFIG_DIR and SESSION_STATE_DIR when they are set.

    session default-model unit launchd > LABEL.plist &&
        mv LABEL.plist ~/Library/LaunchAgents/
    mkdir -p ~/.sessionkit/tools/log
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/LABEL.plist

    mkdir -p ~/.config/systemd/user
    session default-model unit systemd | awk -v d="$HOME/.config/systemd/user" \\
        '/^# file: /{f = d "/" $3} {print > f}'
    systemctl --user daemon-reload
    systemctl --user enable --now PATHUNIT
    systemctl --user start SERVICEUNIT

Run one pinner: unload any other tool that pins "model" first, or the two
put it back over each other for ever. With neither launchd nor systemd,
`watch` does the same in the foreground.

Exit codes: 0 done, in sync or unset; 2 usage; 3 refused, nothing
written; 4 a backup, write, read-back or log line failed; 5 settings.json
unreadable. show exits with the code enforce would.""".replace(
    "LABEL", dmw.LABEL).replace("PATHUNIT", dmw.SYSTEMD_PATH_UNIT).replace(
    "SERVICEUNIT", dmw.SYSTEMD_SERVICE_UNIT)


def build_parser():
    """`session default-model`'s parser: show, list and enforce."""
    import argparse
    p = argparse.ArgumentParser(prog=PROG, epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                description="Pin the default every NEW claude starts on: when "
                                            "model.default is set, put \"model\" in Claude "
                                            "Code's settings.json back if something rewrote "
                                            "it. Unset, it does nothing.")
    sub = p.add_subparsers(dest="verb", metavar="show|list|enforce|watch|unit")
    help_ = dict(SUBVERBS)
    sp = sub.add_parser("show", help=help_["show"],
                        description="The pin and why it reads as set or unset, the "
                                    "settings.json it guards, that file's \"model\", and "
                                    "whether enforce would change it. Writes nothing.")
    sp.add_argument("--json", action="store_true", help="machine-readable: one JSON object")
    lp = sub.add_parser("list", help=help_["list"],
                        description="The models a pin may name: the aliases, then every "
                                    "model id this release knows. " + NOT_LIVE)
    lp.add_argument("--json", action="store_true", help="machine-readable: one JSON array")
    ep = sub.add_parser("enforce", help=help_["enforce"],
                        description="One pass. If model.default is set and settings.json's "
                                    "\"model\" differs, put it back: back up, edit the raw "
                                    "text, validate, replace, read back. Unset: nothing. "
                                    "Each write prints one line.")
    ep.add_argument("--dry-run", action="store_true",
                    help="print the planned edit and where the backup would go; write nothing")
    wp = sub.add_parser("watch", help=help_["watch"],
                        description="Foreground, until interrupted: enforce once, then poll "
                                    "settings.json and the session tool's host settings "
                                    "every S seconds and enforce again when either changes. "
                                    "Unset: says so and exits 0, watching nothing. For a "
                                    "host with neither launchd nor systemd, or to see it work.")
    wp.add_argument("--interval", type=_positive, default=dmw.INTERVAL, metavar="S",
                    help=f"seconds between polls (default {dmw.INTERVAL:g})")
    up = sub.add_parser("unit", help=help_["unit"],
                        description="Print, on stdout, a launchd agent (WatchPaths) or a "
                                    "systemd .path and .service pair (PathChanged=) that "
                                    "runs `session default-model enforce` each time "
                                    "settings.json or the session tool's host settings "
                                    "change. Installs nothing, loads nothing: the "
                                    "commands under `session default-model --help` do. "
                                    "Carries CLAUDE_CONFIG_DIR and SESSION_STATE_DIR when "
                                    "they are set, so render it from the shell whose "
                                    "settings it should guard.")
    up.add_argument("kind", choices=dmw.KINDS, help="launchd (macOS) or systemd (Linux)")
    return p


def _positive(text: str) -> float:
    import argparse
    import math
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of seconds: {text!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"must be a number of seconds above 0, not {text!r}")
    return value


def main(argv: list[str]) -> int:
    a = build_parser().parse_args(argv)
    if a.verb in (None, "show"):
        return show(json_out=getattr(a, "json", False))
    if a.verb == "list":
        return list_models(json_out=a.json)
    if a.verb == "watch":
        return watch(interval=a.interval)
    if a.verb == "unit":
        return unit(a.kind)
    return enforce(dry_run=a.dry_run)


# --- show ---------------------------------------------------------------------------------------

def current(path: Path) -> dict:
    """settings.json's top-level "model", read only: `present` is True,
    False (no key, or no file), or None when it could not be told."""
    from sessiontools.statusline import settings_target
    try:
        data = settings_target(path).read_bytes()
    except FileNotFoundError:
        return {"present": False, "model": None, "note": "no settings.json"}
    except OSError as e:
        return {"present": None, "model": None, "note": f"unreadable ({e.__class__.__name__})"}
    try:
        parsed = json.loads(data.decode("utf-8"))
    except ValueError:
        return {"present": None, "model": None, "note": "does not parse as JSON"}
    if not isinstance(parsed, dict):
        return {"present": None, "model": None, "note": "not a JSON object"}
    if dm.KEY not in parsed:
        return {"present": False, "model": None, "note": "no \"model\" key"}
    return {"present": True, "model": parsed[dm.KEY], "note": ""}


#: What each `Outcome.state` of a dry run says about the pin and the file.
SAID = {"unset": "unset: nothing is pinned, nothing is done",
        "in-sync": "in sync",
        "planned": "out of sync: enforce would put \"model\" back",
        "refused": "refused: enforce would write nothing",
        "unreadable": "settings.json unreadable: enforce would write nothing"}


def show(json_out: bool = False) -> int:
    found = dm.setting()
    path = dm.settings_path()
    now = current(path)
    if found.value is None:
        out = dm.Outcome(dm.OK, "unset", f"default-model: {found.why}: nothing to do")
    else:
        out = dm.enforce(dry_run=True)
    if json_out:
        print(json.dumps({"pin": found.value, "setting": found.why, "file": str(path),
                          "present": now["present"], "model": now["model"],
                          "note": now["note"], "state": out.state, "code": out.code,
                          "message": out.message}))
        return out.code
    if now["present"]:
        model = json.dumps(now["model"])
    else:
        model = now["note"]
    rows = [("pin", json.dumps(found.value) if found.value is not None else "unset"),
            ("setting", found.why),
            ("file", str(path)),
            ("model", model),
            ("state", SAID.get(out.state, out.state))]
    print("\n".join(f"{k + ':':9}{v}" for k, v in rows))
    if out.state in ("planned", "refused", "unreadable"):
        print(out.message)
    return out.code


# --- list ---------------------------------------------------------------------------------------

def list_models(json_out: bool = False) -> int:
    rows = dm.models()
    if json_out:
        print(json.dumps([{"name": m.name, "kind": m.kind, "pinnable": m.pinnable,
                           "why": m.why} for m in rows]))
        return dm.OK
    width = max(len("NAME"), *(len(m.name) for m in rows))
    lines = [f"{'NAME':{width}}  KIND   PINNABLE"]
    lines += [f"{m.name:{width}}  {m.kind:5}  {'yes' if m.pinnable else 'no: ' + m.why}"
              for m in rows]
    print("\n".join(lines))
    print()
    print(NOT_LIVE)
    return dm.OK


# --- enforce ------------------------------------------------------------------------------------

def enforce(dry_run: bool = False) -> int:
    """One pass. The outcome's one line goes to stdout when the pass is
    fine (a write is the log line a unit's log file keeps), else stderr."""
    out = dm.enforce(dry_run=dry_run)
    print(out.message, file=sys.stdout if out.code == dm.OK else sys.stderr)
    return out.code


# --- watch --------------------------------------------------------------------------------------

def watch(interval: float = dmw.INTERVAL) -> int:
    """Foreground until interrupted. Each write's line on stdout, as
    enforce's; refusals and failures on stderr."""
    def say(line: str) -> None:
        print(line, flush=True)

    def warn(line: str) -> None:
        print(line, file=sys.stderr, flush=True)

    try:
        return dmw.watch(interval=interval, say=say, warn=warn).code
    except KeyboardInterrupt:
        return dm.OK


# --- unit ---------------------------------------------------------------------------------------

def unit(kind: str) -> int:
    """The unit on stdout; a refusal, or the note that the pin is unset, on
    stderr. Nothing is written or loaded."""
    try:
        text, note = dmw.unit(kind)
    except dmw.Refused as e:
        print(f"default-model unit: {e}", file=sys.stderr)
        return dm.REFUSED
    sys.stdout.write(text)
    if note:
        print(note, file=sys.stderr)
    return dm.OK
