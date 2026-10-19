"""`session start`: launch a coding-agent session from an agent markdown file.

    session start <agent>                  launch an agent by name
    session start --dry-run <agent>        show what would run; launch nothing
    session start --idle <agent>           boot self-directed, inject nothing
    session start --converse <agent>       speak the first response
    session start --in-tmux <agent>        in a new tmux window, not in place
    session start <agent> review the auth  trailing words = the first message

The agent's own front matter IS the launch configuration: native keys reach
the vendor untouched, `claude:` renders to argv by one rule, and the
launcher's own keys live under `start:`. Front matter supplies defaults;
the command line overrides them.

THE PORT. This package is the old launcher (session-20300729 `81e5662`,
`skills/session/scripts/session-start`) ported as package code, well
structured, in the right place. Library first:

    from sessiontools.start import parse_args, prepare, execute
    plan = prepare(parse_args(["--dry-run", "alice"]))   # a LaunchPlan
    print(render_dry_run_report(plan))

THE BRIDGE IS OFF THE ROAD. Every part is ported: tmux placement
(`placement`), resume (`resume`, planned by
`plan.resolve`/`mint`/`assemble`, so `tm session resume` runs the port)
and declared secrets (`secrets`: `pass`, the 0600 file, the
shred trap). Nothing here execs the old launcher any more, and the
package does not carry it.

THE FRONT DOOR (`holders`). A hand launch - no claude
`-n`/`--name` - of an agent a live Claude Code session already holds is
refused before anything is minted, naming the holder and its age;
`--allow-second-holder` launches anyway, saying so. A session in the
launcher's own tmux pane (`$TMUX_PANE`) is being replaced, not doubled
(a `respawn-pane -k`), and holds nothing.

Exit codes: 0 ok, 1 launch failed, 2 usage or a refusal, 3 the agent is
already held (the front door).
"""

from __future__ import annotations

import sys

from sessiontools.start.agents import list_agents
from sessiontools.start.args import parse_args
from sessiontools.start.errors import (
    EXIT_HELD,
    EXIT_LAUNCH_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    PROG,
    LaunchError,
)
from sessiontools.start.holders import enforce, front_door
from sessiontools.start.placement import (
    check_placeable,
    parse_tmux_target,
    place_in_tmux,
    receipt,
    should_switch,
)
from sessiontools.start.plan import (
    LaunchPlan,
    assemble,
    mint,
    prepare,
    resolve,
)
from sessiontools.start.run import (
    default_window_name,
    execute,
    render_dry_run_report,
    report_generator_failures,
)


def main(argv: list[str]) -> int:
    """`session start ARGV`. Returns the exit code; a live launch does not
    return (the process becomes the vendor)."""
    argv = list(argv)
    try:
        args = parse_args(argv)
    except SystemExit as stop:          # argparse: usage, the trap, -h
        return stop.code if isinstance(stop.code, int) else EXIT_USAGE
    if args.list_agents:
        # Before anything else: a question about the agents dir, answered
        # from it. None is exit 0 with no output - a true answer.
        for name in list_agents():
            print(name)
        return EXIT_OK
    try:
        in_tmux = bool(args.in_tmux or args.tmux_target)
        session, window = parse_tmux_target(args.tmux_target)
        switch = False
        if in_tmux:
            # Both tmux usage refusals BEFORE anything is minted or written.
            # `--switch --no-switch` first: it is wrong wherever it was
            # typed, while "nowhere to place" also depends on the
            # environment, so the more specific complaint wins.
            switch = should_switch(args.switch, args.no_switch)
            check_placeable(session)
        launch = resolve(args)
        # THE FRONT DOOR: after resolve (it names the agent),
        # before mint - a refusal leaves nothing minted, written or placed.
        door = front_door(launch.agent.name, launch.cli_args, launch.environ)
        if not args.dry_run:
            enforce(door, args.allow_second_holder, warn=_warn)
        elif door is not None and door.unmeasured:
            _warn(f"the second-holder check could not run: {door.unmeasured}")
        plan = assemble(launch, mint(launch))
        if in_tmux:
            window = window or default_window_name(plan.agent, plan.vendor)
        if args.dry_run:
            report_generator_failures(plan)
            holder = door.report_line(args.allow_second_holder) if door else None
            print(render_dry_run_report(plan, tmux=(session, window) if in_tmux else None,
                                        switch=switch, holder=holder), end="")
            return EXIT_OK
        if in_tmux:
            report_generator_failures(plan)
            print(receipt(plan, place_in_tmux(plan, session, window, switch)))
            return EXIT_OK
        execute(plan)
    except LaunchError as error:
        print(f"{PROG}: {error}", file=sys.stderr)
        return error.code
    return EXIT_OK  # unreachable after a successful exec


def _warn(line: str) -> None:
    print(f"{PROG}: {line}", file=sys.stderr)


__all__ = [
    "EXIT_HELD",
    "EXIT_LAUNCH_FAILED",
    "EXIT_OK",
    "EXIT_USAGE",
    "LaunchError",
    "LaunchPlan",
    "assemble",
    "execute",
    "list_agents",
    "main",
    "mint",
    "parse_args",
    "prepare",
    "render_dry_run_report",
    "resolve",
]
