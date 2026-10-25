"""One warning line when a session is about to start in the home folder.

On macOS the folders the system guards behind a privacy prompt (Desktop,
Downloads, Photos, Media Library) sit directly under the home folder. A
coding agent started there reads the folder it starts in, so the user may
see one access prompt per guarded folder a second after launch. Started in
a project folder, they see none.

`home_folder_notice` says so before the launch, and never more than that:
it does not refuse, change the directory, prompt or alter the exit code.
It names the folder and the remedy, not a product. Pure: the caller hands
in the directory, the environment and (for tests) the platform, and
prints what comes back.

At a terminal, the launcher also OFFERS a way out (`offer_work_folder`):
start in the work folder (`~/work`, one constant) instead, making it
first when it is missing. `home_folder_offer` is the pure decision (move,
make or refuse); `offer_work_folder` asks the one question and, on a yes,
swaps the launch's directory as if `--directory` had named it. It never
moves, renames or deletes anything: the one write it may make is the
`mkdir` the user said yes to.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING, NamedTuple, TextIO

from sessiontools.start.environment import RELATIVE_PREFIX, warn_env_file_traps
from sessiontools.start.errors import PROG

if TYPE_CHECKING:
    from sessiontools.start.plan import Launch

HOME_FOLDER_NOTICE = (
    "starting in your home folder; macOS may ask for access to Desktop, "
    "Downloads, Photos and Media Library. Start in a project folder to avoid this."
)


def home_folder_notice(work_dir: str, environ: Mapping[str, str],
                       platform: str | None = None) -> str | None:
    """The warning line (without the `session start: ` prefix) when
    `work_dir` is the home folder on macOS, else None.

    - `work_dir` is the directory the session runs in: the plan's final
      one, so `--directory`, `start: directory:` and a resume's recorded
      cwd are honoured, not the caller's cwd.
    - The home folder is `HOME` in `environ` (the launch's environment).
      Unset or empty is silent: no guess at a home it did not name.
    - Both sides go through `os.path.realpath`, so a symlinked HOME and a
      trailing slash still match. Exact equality only: a subfolder of the
      home folder is silent.
    - `platform` defaults to `sys.platform`, read at call time; anything
      but `darwin` is silent.

    Never raises: a path that cannot be resolved is silent.
    """
    if (sys.platform if platform is None else platform) != "darwin":
        return None
    home = environ.get("HOME") if environ is not None else None
    if not home or not work_dir:
        return None
    try:
        same = os.path.realpath(work_dir) == os.path.realpath(home)
    except (OSError, ValueError):       # a NUL byte, a loop: say nothing
        return None
    return HOME_FOLDER_NOTICE if same else None


# ---------- the offer: start in the work folder instead ---------------------

#: The folder the offer names, under the home folder. One constant: a
#: rename is this line (and the kit README's quick start, kept in step).
WORK_FOLDER = "work"

#: The `work_dir_source` a launch carries after a yes.
OFFER_SOURCE = "home folder offer"

MOVE, MAKE, REFUSE = "move", "make", "refuse"

YES_ANSWERS = frozenset({"", "y", "yes"})


class Offer(NamedTuple):
    """What the launcher may offer in the home folder.

    path    -- the work folder, HOME joined to WORK_FOLDER (absolute,
               symlinks not resolved)
    display -- how the lines name it: `~/work`
    kind    -- MOVE (it is a folder, or a symlink to one), MAKE (nothing
               is there) or REFUSE (something that is not a folder is)
    """

    path: str
    display: str
    kind: str


def home_folder_offer(work_dir: str, environ: Mapping[str, str],
                      platform: str | None = None) -> Offer | None:
    """The offer for a launch in `work_dir`, or None when none applies.

    None exactly when `home_folder_notice` is None: not the home folder,
    not macOS, or `HOME` unset. Otherwise the work folder under `HOME`
    and what can be done with it, read from the disk now. Reads only;
    never raises.
    """
    if home_folder_notice(work_dir, environ, platform) is None:
        return None
    path = os.path.abspath(os.path.join(environ["HOME"], WORK_FOLDER))
    display = f"~/{WORK_FOLDER}"
    if os.path.isdir(path):                 # a symlink to a folder counts
        kind = MOVE
    elif not os.path.lexists(path):
        kind = MAKE
    else:                                   # a file, a broken symlink, ...
        kind = REFUSE
    return Offer(path, display, kind)


def offer_work_folder(launch: Launch, *, stdin: TextIO | None = None,
                      stderr: TextIO | None = None,
                      platform: str | None = None) -> tuple[Launch, bool]:
    """Ask, at a terminal in the home folder, whether to start in the work
    folder instead. Returns the launch (swapped on a yes) and whether the
    home-folder notice was already printed, so the caller does not print
    it a second time.

    It asks only when every one of these holds; otherwise it says nothing
    and returns the launch untouched:

    - `home_folder_offer` has an offer (macOS, `HOME` set, the launch's
      directory IS the home folder, not a subfolder);
    - the directory came from where the command was typed (`cwd`), so
      `--directory` (even `--directory ~`) and `start: directory:` skip
      it, and the launch is not a resume;
    - not a dry run;
    - stdin and stderr are both terminals.

    The notice prints first, so the question has its reason. A yes (Enter,
    `y`, `yes`, any case) swaps `work_dir`, making the folder first when it
    is missing; anything else, end of input included, is a no and says
    nothing more. A work folder that is not a folder is refused in one
    line, with no question. Ctrl-C is not caught: `KeyboardInterrupt`
    reaches the caller before anything is minted.
    """
    stdin = sys.stdin if stdin is None else stdin
    stderr = sys.stderr if stderr is None else stderr
    if launch.dry_run or launch.resume is not None or launch.work_dir_source != "cwd":
        return launch, False
    offer = home_folder_offer(launch.work_dir, launch.environ, platform)
    if offer is None or not (_isatty(stdin) and _isatty(stderr)):
        return launch, False
    _say(stderr, HOME_FOLDER_NOTICE)
    if offer.kind == REFUSE:
        _say(stderr, f"{offer.display} is not a folder; starting in your home folder")
        return launch, True
    question = (f"start in {offer.display} instead?" if offer.kind == MOVE
                else f"make {offer.display} and start there?")
    stderr.write(f"{PROG}: {question} [Y/n] ")
    stderr.flush()
    answer = stdin.readline()
    if not answer:                          # end of input: a no
        stderr.write("\n")
        stderr.flush()
        return launch, True
    if answer.strip().lower() not in YES_ANSWERS:
        return launch, True
    if offer.kind == MAKE:
        try:
            os.mkdir(offer.path)            # 0o777 less the umask, like mkdir(1)
        except OSError as error:
            reason = error.strerror or str(error)
            _say(stderr, f"could not make {offer.display} ({reason}); "
                         "starting in your home folder")
            return launch, True
        _say(stderr, f"made {offer.display}; starting there")
    else:
        _say(stderr, f"starting in {offer.display}")
    _warn_relative_env_file_traps(launch, offer.path)
    return launch._replace(work_dir=offer.path, work_dir_source=OFFER_SOURCE), True


def _warn_relative_env_file_traps(launch: Launch, work_dir: str) -> None:
    """`resolve` checked the env files against the home folder. Only a
    `./`-relative file names a different file in the new folder, so only
    those are checked again: an absolute one would say the same note twice."""
    environment = launch.agent_file.environment
    relative = [path for path in environment.files if path.startswith(RELATIVE_PREFIX)]
    if relative:
        warn_env_file_traps(environment._replace(files=relative), work_dir)


def _isatty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):   # closed, or no such method
        return False


def _say(stream: TextIO, line: str) -> None:
    stream.write(f"{PROG}: {line}\n")
    stream.flush()


__all__ = [
    "HOME_FOLDER_NOTICE",
    "MAKE",
    "MOVE",
    "OFFER_SOURCE",
    "REFUSE",
    "WORK_FOLDER",
    "Offer",
    "home_folder_notice",
    "home_folder_offer",
    "offer_work_folder",
]
