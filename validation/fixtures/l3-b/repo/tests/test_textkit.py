import unittest

from textkit import normalize_space


class NormalizeSpaceTest(unittest.TestCase):
    def test_collapses_runs(self):
        self.assertEqual(normalize_space("a \t\n b"), "a b")

    def test_strips_ends(self):
        self.assertEqual(normalize_space("  a  "), "a")

    def test_not_a_string(self):
        with self.assertRaises(TypeError):
            normalize_space(None)


if __name__ == "__main__":
    unittest.main()
