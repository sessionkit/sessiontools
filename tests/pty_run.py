"""Run a command AT A TERMINAL: its stdin and stdout on a fresh
pseudo-terminal, its stderr left as this process's. What the command
prints on stdout is copied to this process's stdout byte for byte (output
post-processing is off, so no newline becomes CR LF), and this process
exits with the command's code.

    python tests/pty_run.py session start scout

`session start` refuses an in-place launch whose stdin or stdout is not a
terminal, and the suite captures both through pipes: a test that means a
live launch from a terminal runs it through this (`start_support.at_a_terminal`).
"""

from __future__ import annotations

import os
import subprocess
import sys
import termios


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: pty_run.py COMMAND [ARG...]", file=sys.stderr)
        return 2
    master, slave = os.openpty()
    attrs = termios.tcgetattr(slave)
    attrs[1] &= ~termios.OPOST
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    process = subprocess.Popen(argv, stdin=slave, stdout=slave)
    os.close(slave)
    out = sys.stdout.buffer
    while True:
        try:
            chunk = os.read(master, 65536)
        except OSError:          # EIO: every holder of the slave end has closed it
            break
        if not chunk:
            break
        out.write(chunk)
    out.flush()
    os.close(master)
    return process.wait()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
