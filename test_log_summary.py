import unittest

from log_summary import count_errors


class CountErrorsTests(unittest.TestCase):
    def test_counts_errors_in_mixed_log(self):
        lines = iter(("INFO Started\n", "ERROR Timeout\n", "WARNING Retrying\n", "ERROR Failed"))
        self.assertEqual(count_errors(lines), 2)

    def test_only_counts_lines_starting_with_uppercase_error(self):
        lines = ["INFO Previous ERROR cleared\n", "error Timeout\n", " ERROR Indented\n"]
        self.assertEqual(count_errors(lines), 0)

    def test_empty_log(self):
        self.assertEqual(count_errors([]), 0)


if __name__ == "__main__":
    unittest.main()
