import subprocess
import sys
import unittest
from pathlib import Path

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

    def test_contains_matches_literal_text_with_exact_case(self):
        lines = ["ERROR job[1] failed", "ERROR job1 failed", "ERROR Job[1] failed"]
        self.assertEqual(count_levels(lines, contains="job[1]")["ERROR"], 1)

    def test_contains_with_no_matches_returns_zero_counts(self):
        lines = ["ERROR timed out", "INFO Server started"]
        self.assertEqual(
            count_levels(lines, contains="missing"),
            {"INFO": 0, "WARNING": 0, "ERROR": 0},
        )


class ShowLinesTests(unittest.TestCase):
    def test_combines_filters_and_preserves_original_line_number(self):
        result = subprocess.run(
            [
                sys.executable,
                "log_summary.py",
                "sample.log",
                "--level",
                "ERROR",
                "--contains",
                "timed out",
                "--show-lines",
            ],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(
            result.stdout.splitlines(),
            ["5: ERROR Request timed out", "ERROR: 1"],
        )


class StdinTests(unittest.TestCase):
    def test_counts_errors_from_stdin(self):
        repo = Path(__file__).resolve().parent
        result = subprocess.run(
            [sys.executable, "log_summary.py", "-", "--level", "ERROR"],
            cwd=repo,
            input=(repo / "sample.log").read_text(encoding="utf-8"),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout, "ERROR: 2\n")


if __name__ == "__main__":
    unittest.main()
