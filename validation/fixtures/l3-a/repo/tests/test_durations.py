import unittest

from durations import format_duration


class FormatDurationTest(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(format_duration(0), "0s")

    def test_every_unit(self):
        self.assertEqual(format_duration(93784), "1d2h3m4s")

    def test_omits_zero_components(self):
        self.assertEqual(format_duration(3600), "1h")

    def test_negative(self):
        with self.assertRaises(ValueError):
            format_duration(-1)

    def test_not_an_int(self):
        for value in (1.5, "1", True):
            with self.assertRaises(TypeError):
                format_duration(value)


if __name__ == "__main__":
    unittest.main()
