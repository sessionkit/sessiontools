"""The launcher's exit codes and its exceptions, shared by every module.

Its own module so the rest can import it without a cycle: agents, the
agent file, the gates and the plan all raise `LaunchError`, and `main`
turns it into a `session start:` line and an exit code.

Exit codes: 0 ok, 1 launch failed, 2 usage or a refusal, 3 the agent is
already held by a live session (`holders`): its own code, so a
caller can tell "someone holds this agent" from a failure or a usage error.
130 is Ctrl-C at the home-folder question (`homefolder`), before anything
is minted: the shell's own code for an interrupt.
(The old header's "a real launch exits 3" was stale; nothing ever returned
3 until the front door took it.)
"""

from __future__ import annotations

PROG = "session start"

EXIT_OK = 0
EXIT_LAUNCH_FAILED = 1
EXIT_USAGE = 2
EXIT_HELD = 3
EXIT_INTERRUPTED = 130


class LaunchError(Exception):
    """A launch-time failure with a message for stderr and an exit code."""

    def __init__(self, message: str, code: int = EXIT_LAUNCH_FAILED) -> None:
        super().__init__(message)
        self.code = code


class ContextRefused(LaunchError):
    """A generator the agent file marked `required: true` failed (a non-zero
    exit it did not declare informational, a timeout, or a built-in that
    raised), so the seat is refused: no vendor starts. Exit 1, launch
    failed - the environment failed at launch time, not the author.

    result   the failing required `context.ContextResult`, its stdout (partial
             on a timeout) and full stderr kept
    door     "system-prompt" or "initial": the list it was declared in
    position (index, count): 1-based place of the entry within that list;
             every later entry, in either list, was not run
    earlier  the non-required failed results that ran before it, in run order
    """

    def __init__(self, result: object, door: str, position: tuple[int, int],
                 earlier: tuple = ()) -> None:
        super().__init__(f"required context generator `{result.source}` "
                         f"{result.error}", EXIT_LAUNCH_FAILED)
        self.result = result
        self.door = door
        self.position = position
        self.earlier = tuple(earlier)


class AuthoringError(ValueError):
    """An agent file (or a passthrough) says something the launcher cannot
    honour: an unknown key, a switch that is not a boolean, a door opened
    twice. A ValueError, so `load` and `assemble` turn every one into the
    launcher's own line and exit 2, naming the file."""


def unknown_keys(where: str, unknown: list, known: tuple) -> AuthoringError:
    """The one spelling of "you wrote a key I do not know" - loud, because a
    silently ignored key is a session configured wrong that looks fine."""
    names = ", ".join(repr(key) for key in sorted(unknown))
    return AuthoringError(f"unknown key(s) under `{where}`: {names} "
                          f"(known: {', '.join(known)})")
