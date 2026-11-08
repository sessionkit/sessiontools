"""Fakes for the kit-web-shell room tests (ST-249 do-002): a short private
socket dir, a fake room package with a `sessionkit.web` entry point, a fake
`web-room` command on a tmp PATH, and a fake room program the supervisor
runs. No TCP anywhere: every socket is a unix socket in a 0700 tmp dir."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import textwrap
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

#: The descriptor every fake Talk gives, by either transport.
TALK = {"name": "talk", "title": "Talk", "package": "sessionvoice",
        "command": [sys.executable, "-m", "fake_talk", "serve", "--unix", "{socket}"]}


@contextmanager
def short_dir(prefix: str = "st-web-") -> Iterator[Path]:
    """A 0700 dir under /tmp, short enough for sun_path (the conftest does
    the same for tmux's sockets), removed after."""
    d = Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))
    try:
        os.chmod(d, 0o700)
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def install_entry_point_package(root: Path, descriptor: dict | None = None,
                                body: str | None = None) -> Path:
    """A fake room package in ROOT (put ROOT on sys.path): `fake_talk_room`
    with a `sessionkit.web` entry point `talk = fake_talk_room:room`."""
    pkg = root / "fake_talk_room"
    pkg.mkdir(parents=True)
    desc = TALK if descriptor is None else descriptor
    (pkg / "__init__.py").write_text(
        body if body is not None else f"def room():\n    return {desc!r}\n")
    info = root / "fake_talk_room-0.1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: fake-talk-room\nVersion: 0.1\n")
    (info / "entry_points.txt").write_text("[sessionkit.web]\ntalk = fake_talk_room:room\n")
    return root


def install_command(bin_dir: Path, name: str = "sessionvoice-web", descriptor: dict | None = None,
                    script: str | None = None) -> Path:
    """A fake package command NAME in BIN_DIR whose `web-room` prints
    DESCRIPTOR (default TALK)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    desc = TALK if descriptor is None else descriptor
    body = script if script is not None else textwrap.dedent(f"""\
        import json, sys
        if sys.argv[1:] == ["web-room"]:
            print(json.dumps({desc!r}))
            sys.exit(0)
        sys.exit(2)
        """)
    path = bin_dir / name
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)
    return path


#: The fake room program. argv: MODE SOCKET [MARKER]. Modes: `serve`
#: (answer /, /echo and /events), `crash` (exit 1 at once), `crash-once`
#: (exit 1 the first time MARKER is absent, then serve), `child` (start a
#: sleeping grandchild in its own group, write its pid to MARKER, serve),
#: `stubborn` (ignore SIGTERM, serve).
FAKE_ROOM = textwrap.dedent("""\
    import json, os, signal, subprocess, sys
    from sessiontools.web import unixhttp

    mode, sock = sys.argv[1], sys.argv[2]
    marker = sys.argv[3] if len(sys.argv) > 3 else None
    if mode == "crash":
        sys.exit(1)
    if mode == "crash-once" and not os.path.exists(marker):
        open(marker, "w").close()
        sys.exit(1)
    if mode == "child":
        kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        with open(marker, "w") as f:
            f.write(str(kid.pid))
    if mode == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    class Room(unixhttp.QuietHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            body = json.dumps({"room": "talk", "path": self.path, "pid": os.getpid()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = unixhttp.serve(sock, Room)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
    """)


def write_fake_room(directory: Path) -> Path:
    path = directory / "fake_room.py"
    path.write_text(FAKE_ROOM)
    return path


def wait_for(cond: Callable[[], bool], timeout: float = 10.0, every: float = 0.02) -> bool:
    """True once COND holds, False if TIMEOUT passes first."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(every)
    return cond()


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def descriptor_json(desc: dict) -> str:
    return json.dumps(desc)


# --- a browser's Origin, as the Fetch spec writes it ----------------------------------------

#: The referrer policies a Referrer-Policy header can name (W3C Referrer Policy §3).
REFERRER_POLICIES = ("no-referrer", "no-referrer-when-downgrade", "same-origin", "origin",
                     "strict-origin", "origin-when-cross-origin",
                     "strict-origin-when-cross-origin", "unsafe-url")
#: A document's policy when its response names none (Fetch "default referrer policy").
DEFAULT_REFERRER_POLICY = "strict-origin-when-cross-origin"


def referrer_policy(header: str | None) -> str:
    """The policy a document gets from its response's Referrer-Policy
    HEADER (W3C Referrer Policy §8.1): split on commas, the LAST token that
    is a policy wins; none at all is the Fetch default."""
    policy = ""
    for token in (header or "").split(","):
        token = token.strip().lower()
        if token in REFERRER_POLICIES:
            policy = token
    return policy or DEFAULT_REFERRER_POLICY


def request_origin(policy: str, page_origin: str, target_origin: str, method: str) -> str | None:
    """The Origin header a browser sends when a page at PAGE_ORIGIN, under
    referrer POLICY, submits a form (mode navigate, tainting basic) with
    METHOD to TARGET_ORIGIN. Fetch §3.1 "append a request `Origin` header":
    a GET or HEAD carries none; any other method carries the serialized
    origin, or `null` where the policy says so."""
    if method.upper() in ("GET", "HEAD"):
        return None
    downgrade = page_origin.startswith("https:") and not target_origin.startswith("https:")
    if policy == "no-referrer":
        return "null"
    if policy in ("no-referrer-when-downgrade", "strict-origin",
                  "strict-origin-when-cross-origin") and downgrade:
        return "null"
    if policy == "same-origin" and page_origin != target_origin:
        return "null"
    return page_origin
