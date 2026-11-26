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


class RegexTests(unittest.TestCase):
    def test_matches_alternatives_and_shows_line_numbers(self):
        result = subprocess.run(
            [
                sys.executable,
                "log_summary.py",
                "sample.log",
                "--level", "ERROR",
                "--regex", "timed out|connection failed",
                "--show-lines",
            ],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(
            result.stdout.splitlines(),
            ["2: ERROR Database connection failed", "5: ERROR Request timed out", "ERROR: 2"],
        )

    def test_rejects_invalid_regex_before_opening_file(self):
        result = subprocess.run(
            [sys.executable, "log_summary.py", "missing.log", "--regex", "["],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("invalid --regex pattern", result.stderr)


class SearchTests(unittest.TestCase):
    def test_searches_arbitrary_text_and_merges_overlapping_context(self):
        result = subprocess.run(
            [sys.executable, "log_summary.py", "search", "-", "--regex",
             "timeout|refused", "--context", "1"],
            cwd=Path(__file__).resolve().parent,
            input="before\n2026-11-26 timeout\n  stack frame\nconnection refused\nafter\nskipped\n",
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.splitlines(), [
            "1: before",
            "2: 2026-11-26 timeout",
            "3:   stack frame",
            "4: connection refused",
            "5: after",
        ])


class GroupTests(unittest.TestCase):
    def test_groups_equivalent_text_and_jsonl_examples(self):
        repo = Path(__file__).resolve().parent
        cases = [
            ["app.log", "--regex", r"code=(?P<code>E\d{4})"],
            ["app.jsonl", "--format", "jsonl"],
        ]
        for options in cases:
            with self.subTest(options=options):
                result = subprocess.run(
                    [sys.executable, "log_summary.py", "group", *options, "--by", "code"],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(
                    result.stdout.splitlines(),
                    [
                        "COUNT  VALUE",
                        '    4  "E1042"',
                        '    3  "E2007"',
                        '    1  "E3001"',
                        "Scanned: 20  Grouped: 8  Skipped: 12",
                    ],
                )

    def test_jsonl_stdin_skips_invalid_and_missing_values(self):
        result = subprocess.run(
            [sys.executable, "log_summary.py", "group", "-", "--format", "jsonl",
             "--by", "service"],
            cwd=Path(__file__).resolve().parent,
            input='{"service":"gateway"}\ninvalid\n{}\n{"service":null}\n{"service":[]}\n',
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(
            result.stdout.splitlines(),
            ["COUNT  VALUE", '    1  "gateway"', "Scanned: 5  Grouped: 1  Skipped: 4"],
        )

    def test_text_group_requires_the_named_capture(self):
        result = subprocess.run(
            [sys.executable, "log_summary.py", "group", "app.log",
             "--regex", "(?P<other>ERROR)", "--by", "code"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("named capture group 'code'", result.stderr)


if __name__ == "__main__":
    unittest.main()
