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
                "app.log",
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
            [
                "8: 2026-11-26T14:00:00.959Z ERROR service=database request_id=r-100 status=503 code=E1042 Connection acquisition timed out",
                "13: 2026-11-26T14:00:01.644Z ERROR service=payments request_id=r-101 status=504 code=E2007 Authorization retry timed out",
                "ERROR: 2",
            ],
        )


class StdinTests(unittest.TestCase):
    def test_counts_errors_from_stdin(self):
        repo = Path(__file__).resolve().parent
        result = subprocess.run(
            [sys.executable, "log_summary.py", "-", "--level", "ERROR"],
            cwd=repo,
            input=(repo / "app.log").read_text(encoding="utf-8"),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout, "ERROR: 5\n")


class RegexTests(unittest.TestCase):
    def test_matches_alternatives_and_shows_line_numbers(self):
        result = subprocess.run(
            [
                sys.executable,
                "log_summary.py",
                "app.log",
                "--level", "ERROR",
                "--regex", "acquisition timed out|lookup failed",
                "--show-lines",
            ],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "8: 2026-11-26T14:00:00.959Z ERROR service=database request_id=r-100 status=503 code=E1042 Connection acquisition timed out",
                "9: 2026-11-26T14:00:01.096Z ERROR service=orders request_id=r-100 status=503 code=E1042 Order lookup failed",
                "ERROR: 2",
            ],
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


class TimelineTests(unittest.TestCase):
    def test_reconstructs_same_request_from_both_formats_and_reversed_input(self):
        repo = Path(__file__).resolve().parent
        expected_times = ["00.000", "00.274", "00.548", "00.959", "01.096", "01.781"]
        formats = [
            ("app.log", ["--regex", r"^(?P<timestamp>\S+).*request_id=(?P<id>\S+)", "--key", "id"]),
            ("app.jsonl", ["--format", "jsonl", "--key", "request_id"]),
        ]
        for filename, options in formats:
            with self.subTest(filename=filename):
                command = [sys.executable, "log_summary.py", "timeline"]
                options += ["--value", "r-100", "--time", "timestamp"]
                events = (repo / filename).read_text(encoding="utf-8").splitlines()
                reversed_input = "\n".join(reversed(events)) + "\n"
                file_result = subprocess.run(
                    [*command, filename, *options], cwd=repo, capture_output=True,
                    text=True, check=True,
                )
                stdin_result = subprocess.run(
                    [*command, "-", *options], cwd=repo, input=reversed_input,
                    capture_output=True, text=True, check=True,
                )
                self.assertEqual(stdin_result.stdout, file_result.stdout)
                rows = stdin_result.stdout.splitlines()
                self.assertEqual(rows[-1], "Scanned: 20  Matched: 6  Skipped: 0")
                self.assertEqual(
                    [row.split("  ", 1)[0] for row in rows[:-1]],
                    [f"2026-11-26 14:00:{time}000+00:00" for time in expected_times],
                )

    def test_counts_invalid_records_and_sorts_by_absolute_time(self):
        result = subprocess.run(
            [sys.executable, "log_summary.py", "timeline", "-", "--format", "jsonl",
             "--key", "id", "--value", "a", "--time", "time"],
            cwd=Path(__file__).resolve().parent,
            input='{"id":"a","time":"2026-11-26T10:00:00-05:00"}\n'
                  '{"id":"a","time":"2026-11-26T14:00:00Z"}\n'
                  '{"id":"a","time":"invalid"}\ninvalid\n{"id":"b"}\n',
            capture_output=True, text=True, check=True,
        )
        rows = result.stdout.splitlines()
        self.assertTrue(rows[0].startswith("2026-11-26 14:00:00.000000+00:00"))
        self.assertTrue(rows[1].startswith("2026-11-26 15:00:00.000000+00:00"))
        self.assertEqual(rows[2], "Scanned: 5  Matched: 2  Skipped: 2")


class CorrelateTests(unittest.TestCase):
    def test_reports_are_identical_for_text_and_reversed_jsonl(self):
        repo = Path(__file__).resolve().parent
        command = [sys.executable, "log_summary.py", "correlate"]
        text = subprocess.run(
            [*command, "app.log", "--key", "id", "--time", "ts",
             "--regex", r"^(?P<ts>\S+).*request_id=(?P<id>\S+)"],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        json_lines = (repo / "app.jsonl").read_text(encoding="utf-8").splitlines()
        structured = subprocess.run(
            [*command, "-", "--format", "jsonl", "--key", "request_id",
             "--time", "timestamp", "--sort", "span"],
            cwd=repo, input="\n".join(reversed(json_lines)),
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(text.stdout, structured.stdout)
        rows = text.stdout.splitlines()
        self.assertEqual(rows[1].split(), [
            "6", "0:00:01.781000", "2026-11-26T14:00:00.000000+00:00",
            "2026-11-26T14:00:01.781000+00:00", '"r-100"',
        ])
        self.assertEqual(rows[-1], "Scanned: 20  IDs: 5  Skipped: 0")

    def test_count_sort_and_skipped_records(self):
        repo = Path(__file__).resolve().parent
        log = (
            '{"id":"wide","ts":"2026-11-26T14:10:00Z"}\n'
            '{"id":"busy","ts":"2026-11-26T14:00:00Z"}\n'
            '{"id":"wide","ts":"2026-11-26T14:00:00Z"}\n'
            '{"id":"busy","ts":"2026-11-26T14:00:01Z"}\n'
            '{"id":"busy","ts":"2026-11-26T14:00:02Z"}\n'
            '{"id":"busy","ts":"invalid"}\n'
            '{"ts":"2026-11-26T14:00:00Z"}\n'
        )
        for ranking, first_id in [("count", "busy"), ("span", "wide")]:
            with self.subTest(ranking=ranking):
                result = subprocess.run(
                    [sys.executable, "log_summary.py", "correlate", "-",
                     "--format", "jsonl", "--key", "id", "--time", "ts",
                     "--sort", ranking],
                    cwd=repo, input=log, capture_output=True, text=True, check=True,
                )
                rows = result.stdout.splitlines()
                self.assertTrue(rows[1].endswith(f'"{first_id}"'))
                self.assertEqual(rows[-1], "Scanned: 7  IDs: 2  Skipped: 2")


if __name__ == "__main__":
    unittest.main()
