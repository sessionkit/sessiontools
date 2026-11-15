"""ST-305's 17 parser and classifier tests, PORTED with the parser they test
(`sessiontools.hidden_failure`, from ST-305 `findings/scripts/scan.py`,
store f85819f869). Synthetic commands only.

Kept as close to verbatim as the import allows: the module is imported under
the name the tests were written against, `scan`.
"""
import unittest

from sessiontools import hidden_failure as scan


def pipes(cmd):
    st, nested = scan.parse(cmd)
    return [[scan.stage_name(s) for s in p["stages"]] for stmt in st for _c, p in stmt], nested


class Tokenize(unittest.TestCase):
    def test_or_is_not_a_pipe(self):
        p, n = pipes("make test || echo failed")
        self.assertEqual(p, [["make"], ["echo"]])
        self.assertEqual(n, 0)

    def test_simple_pipe(self):
        p, _ = pipes("pytest -q 2>&1 | tail -20")
        self.assertEqual(p, [["pytest", "tail"]])

    def test_pipe_in_quotes_is_text(self):
        p, _ = pipes("grep -E 'a|b' file.txt")
        self.assertEqual(p, [["grep"]])
        p, _ = pipes('echo "x | y"')
        self.assertEqual(p, [["echo"]])

    def test_heredoc_body_is_not_code(self):
        cmd = "cat > f.md <<'EOF'\nfoo | bar\nbaz && qux\nEOF\necho done"
        p, _ = pipes(cmd)
        self.assertEqual(p, [["cat"], ["echo"]])

    def test_heredoc_dash_and_unquoted(self):
        cmd = "cat <<-END | wc -l\n\tline | one\n\tEND"
        p, _ = pipes(cmd)
        self.assertEqual(p, [["cat", "wc"]])

    def test_nested_subst_counts_nested(self):
        p, n = pipes('x=$(git log | head -1); echo "$x"')
        self.assertEqual(n, 1)
        self.assertTrue(all(len(s) == 1 for s in p))

    def test_wrappers_and_env_stripped(self):
        p, _ = pipes("FOO=1 nice -n 19 timeout 60 make check 2>&1 | tail -5")
        self.assertEqual(p, [["make", "tail"]])

    def test_subcommands(self):
        p, _ = pipes("git commit -m x 2>&1 | tail -3")
        self.assertEqual(p, [["git commit", "tail"]])
        p, _ = pipes("message send a -s b -f c | head -2")
        self.assertEqual(p, [["message send", "head"]])
        p, _ = pipes("uv run pytest tests/ -x | tail")
        self.assertEqual(p, [["uv run pytest", "tail"]])

    def test_comment(self):
        p, _ = pipes("# a | b\nls | wc -l")
        self.assertEqual(p, [["ls", "wc"]])

    def test_pipe_ampersand(self):
        p, _ = pipes("make |& tail")
        self.assertEqual(p, [["make", "tail"]])


class Readers(unittest.TestCase):
    def kind(self, stage):
        return scan.reader_kind(stage.split())

    def test_kinds(self):
        self.assertEqual(self.kind("head -5"), "head")
        self.assertEqual(self.kind("tail -n 20"), "tail")
        self.assertEqual(self.kind("grep -q foo"), "grep-q")
        self.assertEqual(self.kind("grep -qE foo"), "grep-q")
        self.assertEqual(self.kind("grep -m1 foo"), "grep-m")
        self.assertIsNone(self.kind("grep -v foo"))
        self.assertEqual(self.kind("sed 5q"), "sed-q")
        self.assertEqual(self.kind("sed -n '1,3p;3q'"), "sed-q")
        self.assertIsNone(self.kind("sed s/a/b/"))
        self.assertEqual(self.kind("awk 'NR==2{exit}'"), "awk-exit")
        self.assertIsNone(self.kind("wc -l"))

    def test_head_limit(self):
        self.assertEqual(scan.head_limit(["head"]), 10)
        self.assertEqual(scan.head_limit(["head", "-3"]), 3)
        self.assertEqual(scan.head_limit(["head", "-n", "7"]), 7)
        self.assertEqual(scan.head_limit(["head", "-n20"]), 20)
        self.assertIsNone(scan.head_limit(["head", "-c", "100"]))


class AndOr(unittest.TestCase):
    def stmt(self, cmd):
        st, _ = scan.parse(cmd)
        return st[-1]

    def test_last_pipeline_reaches(self):
        s = self.stmt("seq 9 | head -1")
        self.assertTrue(scan.andor_status(s, 0))

    def test_and_chain_reaches(self):
        s = self.stmt("seq 9 | head -1 && echo ok")
        self.assertTrue(scan.andor_status(s, 0))

    def test_or_fallback_masks(self):
        s = self.stmt("seq 9 | head -1 || echo fallback")
        self.assertFalse(scan.andor_status(s, 0))

    def test_not_last_statement(self):
        st, _ = scan.parse("seq 9 | head -1; echo done")
        self.assertEqual(len(st), 2)

    def test_gate(self):
        self.assertTrue(scan.is_gate("make", scan.GATE_CORE))
        self.assertTrue(scan.is_gate("pytest", scan.GATE_CORE))
        self.assertTrue(scan.is_gate("uv run pytest", scan.GATE_CORE))
        self.assertTrue(scan.is_gate("git commit", scan.GATE_CORE))
        self.assertFalse(scan.is_gate("git log", scan.GATE_CORE))
        self.assertTrue(scan.is_gate("ruff", scan.GATE_EXTENDED))
        self.assertFalse(scan.is_gate("ruff", scan.GATE_CORE))
        self.assertFalse(scan.is_gate("message send", scan.GATE_NO_MSG))
        self.assertTrue(scan.is_gate("make", scan.GATE_NO_MSG))
