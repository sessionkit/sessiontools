"""The token counter's child: run by uv, NEVER imported by sessiontools.

    uv run --no-project --offline --quiet --with tiktoken python -I _tiktoken_child.py

stdin is one JSON array of strings; stdout is one JSON array of integers,
the same length and order: `len(enc.encode(text, disallowed_special=()))`
under tiktoken's `cl100k_base`, the backend of the skillbox `tokens`
command. This is the ONLY file in the package that imports
tiktoken (a test pins it), so `import sessiontools` stays stdlib-only and no
dependency is declared.

It never downloads. The vocabulary must already be in `$TIKTOKEN_CACHE_DIR`
(the parent sets it, and checks first), and tiktoken's one fetch function
is replaced by a refusal before the encoder loads, so a missing or corrupt
cached file is exit 3, never a network act. Run with `python -I`, so it
imports nothing from the caller's cwd or `PYTHON*` environment, and it
imports nothing from sessiontools: the vocabulary's file name is repeated
here, and a test pins it equal to `count.VOCAB_FILE`.
"""

import json
import os
import sys

#: tiktoken's cache key for cl100k_base: sha1 of its download URL.
VOCAB_FILE = "9b5ad71b2ce5302211f9c61530b329a4922fc6a4"
ENCODING = "cl100k_base"


def _refuse(blobpath):
    raise OSError(f"refused to fetch {blobpath}: the counter never downloads")


def main() -> int:
    cache = os.environ.get("TIKTOKEN_CACHE_DIR", "")
    if not cache or not os.path.isfile(os.path.join(cache, VOCAB_FILE)):
        print(f"no {ENCODING} vocabulary in TIKTOKEN_CACHE_DIR={cache!r}", file=sys.stderr)
        return 3
    import tiktoken
    import tiktoken.load

    tiktoken.load.read_file = _refuse
    try:
        enc = tiktoken.get_encoding(ENCODING)
    except (OSError, ValueError) as e:  # a corrupt cache file: tiktoken would re-fetch it
        print(f"could not load {ENCODING} from the cache: {e}", file=sys.stderr)
        return 3
    try:
        texts = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except ValueError as e:
        print(f"stdin is not a JSON array of strings: {e}", file=sys.stderr)
        return 4
    if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
        print("stdin is not a JSON array of strings", file=sys.stderr)
        return 4
    # A lone surrogate (half of a split emoji escape in a transcript) cannot
    # cross into tiktoken's Rust side; "replace" leaves valid text unchanged.
    counts = [len(enc.encode(t.encode("utf-8", "replace").decode("utf-8"),
                             disallowed_special=())) for t in texts]
    sys.stdout.write(json.dumps(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
