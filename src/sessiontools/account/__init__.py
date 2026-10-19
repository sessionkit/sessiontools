"""`session account`: which of the user's Claude accounts the host is on, and
the switch between them.

    session account list | status | use NAME | add NAME | remove NAME

`main(argv)` is the group (`cli`). The pieces are each their own module:
the registry of names (`registry`), the Keychain seam (`keychain`), `use`
(`use`), `status` and `list` (`status`), `add`/`remove` and the argparse
group (`cli`), and the token-leak scan (`leakscan`). Nothing here prints,
logs or stores a token.
"""

from sessiontools.account.cli import main

__all__ = ["main"]
