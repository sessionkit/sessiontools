# Operator and privileged seats

The **operator** is the human other seats ask (for Fable, for a
decision). The **privileged** seats may act as administrators. Both come
from one setting, never from a name in code, so a stranger's install
works with whoever installed it.

```console
$ session operator
{"operator": "you@host.example", "mailbox": "you", "privileged": ["admin", "deploy", "you", "you@host.example"]}
```

With nothing configured:

```console
$ session operator
{"operator": null, "mailbox": null, "privileged": []}
```

- `session operator` exits 0 always, 2 for a usage error.
- The keys are exactly `operator`, `mailbox` and `privileged` (sorted).
  Keys may be added later, never renamed or removed, so a consumer
  ignores keys it does not know.

## The setting

The `operator` block of the `session` tool's settings,
`~/.sessionkit/tools/settings/session.json`, read through the settings
cascade (template and host layers, `_locked` honoured):

```json
{"operator": {"address": "you@host.example", "mailbox": "you",
              "privileged": ["admin", "deploy"]}}
```

## From Python

```python
from sessiontools import read_operator, Operator

op = read_operator()          # environ defaults to os.environ
op.address                    # str | None: the operator's ask address
op.mailbox                    # str | None: operator.mailbox as configured
op.privileged                 # frozenset[str]: the operator always included
op.is_privileged("Admin")     # exact match after strip, case-insensitive
```

## The rules

1. **`operator`, the address:** `AGENT_OPERATOR_ADDR` if set; else
   `operator.address`; else `<operator.mailbox>@<host>`, only when that
   maildir is local; else null. `<host>` is the name the local mail drop
   takes as this host: the FQDN when it is a name under the short host
   (a tailnet name, say), else the short host itself. A reverse-DNS
   (`*.arpa`) name or any other FQDN is never used. The short host comes
   from `SESSIONMAIL_HOST`, else `SESSION_MAIL_HOST`, else the `host` key
   of the mail tool's settings, else the hostname's first label,
   lowercased: the drop's own order. The Fable gate's
   refusal names this address (`session start`).
2. **`privileged`:** every string in `operator.privileged` (stripped,
   empties dropped), plus the operator as the settings name it:
   `operator.address` (else the derived `<mailbox>@<host>`) and
   `operator.mailbox`. The operator is always privileged.
3. **`AGENT_OPERATOR_ADDR` grants no privilege.** Any process can set
   it. It may redirect where an ask goes, but never makes its setter an
   administrator. With only that variable set, `operator` is its value
   and `privileged` is empty.
4. **Nothing configured** means no operator and nothing privileged.
5. **Fail safe.** A file that cannot be read, is not JSON, has a bad
   `_locked` or is the wrong shape reads as nothing configured. A
   `privileged` that is not a list (a string, a map, a number) is
   ignored, and members that are not strings are dropped. A bad file can
   only remove privilege, never grant it.
6. **No guesses.** No person is a default anywhere.
