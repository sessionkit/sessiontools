# Moving tools' state: `session path migrate`

`session path migrate` moves two folders of tools' state to their new
home, once, and leaves a link at each old path. Nothing else moves them:
an upgrade never does.

- **`state`:** `~/.session/state/` to `~/.sessionkit/tools/state/`.
- **`sound`:** `~/.local/share/sessiontools/sound/` to
  `~/.sessionkit/tools/state/sound/`.
- **Dry run first.** `--dry-run` prints the plan and the receipt a real
  run would print, and changes nothing.
- **One host at a time, each with its own go.** A real run on a host is a
  deploy that the maintainer approves for that host. It is never part of
  a merge or a release.
- **The links stay** until one release after every package that reads
  these folders is deployed on every host. Removing them is a later
  change of its own.

```sh
session path migrate --dry-run
```

`sessions`, `seats`, `agent` and `backups` are not moved by this verb.
They move later, with every seat on the host stopped.

## What it does

For each folder, state first:

- **Old folder real, new one absent:** one `rename`, and the old path
  becomes a link to the new one.
- **Both real (`state` only):** a fresh sound install can make
  `~/.sessionkit/tools/state/` first. Each child of the old folder is
  renamed into the new one, and the emptied old folder is replaced by
  the link.
- **The old path is itself a link** to somewhere else: the link is moved,
  never what it points to, and the receipt says so.
- **Already moved** (the old path is a link to the new one): nothing to
  do. A second run says so and exits 0.

Only `rename` moves anything. A link inside a folder moves as a link,
with its target unchanged; nothing is copied, followed or merged.

## What it refuses

A refusal stops the whole run, exit 3, with nothing moved. Each reason is
named in the receipt:

- **A variable is set:** `SESSION_STATE_DIR` or `SESSIONTOOLS_SOUND_HOME`.
  You own that path; unset it to migrate.
- **A name is in both folders:** each one is listed. Nothing merges.
- **A cross-device move:** the new folder is on another disk. There is
  no copy fallback.
- **A relative link would point elsewhere once moved:** the old path is a
  link with a relative target, or a link inside the folder leads out of
  it by a relative path (`../agent`). Each one is named; you decide.
- **A tools agent is loaded,** on a real run: the pane signal's agent,
  the default model's watch, the sound engine, or a `pane-state watch`
  holding its lock. One it cannot check also refuses.
- **The old path is a dangling link,** or not a folder.

A name that looks like a key (`id_rsa`, `*.pem`, `secret`) is listed, not
refused: stop and report it before a real run.

## Exit codes

- `0`: moved, or nothing to do.
- `3`: refused, nothing moved.
- `1`: a move failed part way. The receipt says what moved, and a merge
  puts back the children it had moved.
- `2`: usage.

## The run on one host

Do this only with that host's go. Paste both receipts into the deploy
card's log.

- Dry run. Its `agents` block names each tools agent and says `loaded` or
  `not loaded`.

  ```sh
  session path migrate --dry-run
  ```

- Stop each agent the receipt shows `loaded`:
  - **`pane-state agent`, macOS only:**

    ```sh
    session pane-state agent off
    ```

  - **`sound engine`, macOS only:**

    ```sh
    session sound engine off
    ```

  - **`default-model watch`, macOS (launchd):**

    ```sh
    launchctl bootout gui/$(id -u)/dev.sessionkit.tools.default-model
    ```

  - **`default-model watch`, Linux (systemd):**

    ```sh
    systemctl --user stop sessiontools-default-model.path
    ```

  - **`pane-state watch`:** a `session pane-state watch` started by hand
    holds the lock. Stop it in its own terminal.
- A `session default-model watch` started by hand holds no lock, so the
  receipt cannot show it. Stop any you started.
- Dry run again. The receipt has every agent `not loaded`, no `would
  refuse` line and no key-like name.

  ```sh
  session path migrate --dry-run
  ```

- Move.

  ```sh
  session path migrate
  ```

- Check that `state` and `sound` now read `new`.

  ```sh
  session path --json
  ```

- Start again each agent you stopped:
  - **`pane-state agent`, macOS only:**

    ```sh
    session pane-state agent on
    ```

  - **`sound engine`, macOS only:**

    ```sh
    session sound engine on
    ```

  - **`default-model watch`, macOS (launchd):**

    ```sh
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.sessionkit.tools.default-model.plist
    ```

  - **`default-model watch`, Linux (systemd):**

    ```sh
    systemctl --user start sessiontools-default-model.path
    ```

## Upgrade every copy together

A host can have more than one copy of sessiontools: the `session`
command, and the one inside another package's environment. An older
copy still writes `~/.session/state/`. On a host whose state
started in the new folder, that makes the old folder real again, and the
rule then reads the old one.

- **Upgrade every copy of sessiontools on a host together,** the other
  packages' copies included.
- **To spot one that lags:** `session path --json` adds `stranded` to a
  leaf whose new folder holds state while the old one is in use.

See also: [where tools keeps its state][state-paths], [the `session`
command][session].

[state-paths]: tools-state-paths.md
[session]: session.md
