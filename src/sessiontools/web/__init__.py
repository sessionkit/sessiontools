"""The kit's web front door: one address, one login, one menu.

PROPOSED (openspec/changes/kit-web-shell). The front door owns the
port, the login and the menu, and reverse-proxies each installed package's
room. This package is stdlib only (AGENTS.md rule 3), and `import
sessiontools` never imports it.

- `bind`: `check_bind`, the address the front door may listen on.
- `auth`: the pairing code, the sessions, the cookie, and the Host and
  Origin checks.
- `rooms`: the catalog, `validate_room`, and discovery by entry point or
  by a package's `web-room` command.
- `unixhttp`: HTTP over a unix socket, both ends, with the peer-uid check.
- `proxy`: one request forwarded to a room, identity headers set, the
  answer streamed back.
- `supervisor`: the rooms started, restarted with backoff, stopped by
  process group.
- `settings_page`: one tool's settings drawn from its schema, as escaped
  HTML, for the read-only Settings page.
- `server`: the front door: the gate, the login, the menu, read-only Settings,
  and the rooms through the proxy.
- `cli`: `session web pair|logout|rescan|serve`.
"""
