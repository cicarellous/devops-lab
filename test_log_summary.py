import unittest

from log_summary import count_errors, count_levels


class CountErrorsTests(unittest.TestCase):
    def test_counts_errors_in_mixed_log(self):
        lines = iter(("INFO Started\n", "ERROR Timeout\n", "WARNING Retrying\n", "ERROR Failed"))
        self.assertEqual(count_errors(lines), 2)

    def test_only_counts_lines_starting_with_uppercase_error(self):
        lines = ["INFO Previous ERROR cleared\n", "error Timeout\n", " ERROR Indented\n"]
        self.assertEqual(count_errors(lines), 0)

    def test_empty_log(self):
        self.assertEqual(count_errors([]), 0)


class CountLevelsTests(unittest.TestCase):
    def test_counts_all_supported_levels(self):
        lines = iter(("INFO Started", "WARNING Retrying", "ERROR Failed", "INFO Stopped"))
        self.assertEqual(count_levels(lines), {"INFO": 2, "WARNING": 1, "ERROR": 1})

    def test_includes_zero_counts_and_ignores_unknown_levels(self):
        lines = ["DEBUG Started", "warning Retrying", " ERROR Indented"]
        self.assertEqual(count_levels(lines), {"INFO": 0, "WARNING": 0, "ERROR": 0})


if __name__ == "__main__":
    unittest.main()
