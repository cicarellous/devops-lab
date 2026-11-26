import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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


class InputErrorTests(unittest.TestCase):
    def test_summary_and_search_report_invalid_utf8_without_traceback(self):
        with TemporaryDirectory() as directory:
            log = Path(directory) / "invalid.log"
            log.write_bytes(b"INFO invalid byte: \xff\n")
            script = Path(__file__).with_name("log_summary.py").resolve()
            for args in ([str(log)], ["search", str(log), "--contains", "INFO"]):
                with self.subTest(args=args):
                    result = subprocess.run(
                        [sys.executable, str(script), *args],
                        capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertTrue(result.stderr.startswith("Cannot read log:"))
                    self.assertNotIn("Traceback", result.stderr)


class CompareTests(unittest.TestCase):
    def run_compare(self, source, *mapping, baseline=None, incident=None, input_text=None):
        return subprocess.run(
            [
                sys.executable, "log_summary.py", "compare", source, *mapping,
                "--baseline", *(baseline or ["2026-11-26T14:00Z", "2026-11-26T14:01Z"]),
                "--incident", *(incident or ["2026-11-26T14:01Z", "2026-11-26T14:03Z"]),
            ],
            cwd=Path(__file__).resolve().parent,
            input=input_text, capture_output=True, text=True,
        )

    def test_normalizes_rates_counts_observed_ids_and_reports_coverage(self):
        result = self.run_compare(
            "incident.jsonl", "--format", "jsonl", "--time", "ts",
            "--signature", "code", "--service", "service", "--id", "request",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = result.stdout.splitlines()
        self.assertTrue(rows[0].startswith(
            'RISING "TIMEOUT" service="payments" | events 2 -> 80 | '
            'events/min 2.000 -> 40.000 | observed IDs 2 -> 12'
        ))
        self.assertRegex(rows[0], r'example incident\.jsonl:\d+ id="incident-00"$')
        self.assertTrue(rows[1].startswith('NEW "POOL_EXHAUSTED" service="orders"'))
        self.assertIn('events 2 -> 4 | events/min 2.000 -> 2.000', rows[2])
        self.assertEqual(rows[3:], [
            "Baseline: 4/5 records used",
            "Incident: 85/86 records used",
            "Unassigned (invalid or missing timestamp): 1  Outside windows: 1",
        ])

    def test_text_stdin_uses_half_open_windows(self):
        result = self.run_compare(
            "-", "--time", "ts", "--signature", "code",
            "--regex", r"^(?P<ts>\S+) (?P<code>\S+)",
            input_text=(
                "2026-11-26T14:00Z E1\n"
                "2026-11-26T09:01-05:00 E2\n"
                "2026-11-26T14:03Z E3\n"
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = result.stdout.splitlines()
        self.assertIn('NEW "E2" | events 0 -> 1 | events/min 0.000 -> 0.500 | example <stdin>:2', rows)
        self.assertIn('FALLING "E1" | events 1 -> 0 | events/min 1.000 -> 0.000 | example <stdin>:1', rows)
        self.assertTrue(rows[-1].endswith("Outside windows: 1"))

    def test_rejects_overlapping_and_invalid_windows(self):
        for bounds, message in [
            (["2026-11-26T14:00Z", "2026-11-26T14:02Z"], "baseline must end"),
            (["invalid", "2026-11-26T14:01Z"], "--baseline requires"),
        ]:
            with self.subTest(bounds=bounds):
                result = self.run_compare(
                    "-", "--format", "jsonl", "--time", "ts", "--signature", "code",
                    baseline=bounds, input_text="",
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)


class StructuredSummaryTests(unittest.TestCase):
    def test_jsonl_maps_configurable_severity_and_reports_unclassified(self):
        result = subprocess.run(
            [sys.executable, "log_summary.py", "-", "--format", "jsonl",
             "--level-field", "priority", "--level", "ERROR", "--show-lines"],
            cwd=Path(__file__).resolve().parent,
            input='{"priority":"info"}\ninvalid\n{"priority":" error "}\n{"priority":"DEBUG"}\n{}\n',
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout.splitlines(), ['3: {"priority":" error "}', "ERROR: 1"])
        self.assertEqual(result.stderr.strip(), "Classified: 2  Unclassified: 3")

    def test_text_summary_reports_unclassified_records(self):
        result = subprocess.run(
            [sys.executable, "log_summary.py", "-"], cwd=Path(__file__).resolve().parent,
            input='{"severity":"ERROR"}\n', capture_output=True, text=True, check=True,
        )
        self.assertIn("ERROR: 0", result.stdout)
        self.assertIn("No severity levels recognized", result.stderr)
        self.assertIn("Classified: 0  Unclassified: 1", result.stderr)


class BucketTests(unittest.TestCase):
    def run_bucket(self, log_file, *options, input_text=None):
        return subprocess.run(
            [sys.executable, "log_summary.py", "bucket", log_file, "--time", "ts",
             "--interval", "30s", "--json", *options],
            cwd=Path(__file__).resolve().parent, input=input_text,
            capture_output=True, text=True, check=True,
        )

    def test_reveals_timeout_cluster_and_flat_warnings(self):
        for signature, expected in [
            ("TIMEOUT", [1, 1, 30, 30, 20, 0]),
            ("QUEUE_WARNING", [1, 1, 1, 1, 1, 1]),
        ]:
            with self.subTest(signature=signature):
                result = self.run_bucket(
                    "incident.jsonl", "--format", "jsonl", "--signature", "code",
                    "--value", signature, "--since", "2026-11-26T14:00Z",
                    "--until", "2026-11-26T14:03Z",
                )
                report = json.loads(result.stdout)
                self.assertEqual(report["schema_version"], 1)
                self.assertEqual([bucket["count"] for bucket in report["buckets"]], expected)
                self.assertEqual(
                    [bucket["events_per_minute"] for bucket in report["buckets"]],
                    [2 * n for n in expected],
                )
                coverage = report["coverage"]
                self.assertEqual(coverage["invalid_timestamp"], 1)
                self.assertEqual(coverage["outside_window"], 1)
                self.assertEqual(coverage["counted"], sum(expected))
                self.assertEqual(
                    coverage["scanned"],
                    sum(value for name, value in coverage.items() if name != "scanned"),
                )

    def test_text_and_jsonl_match_with_reordered_input_and_partial_edges(self):
        text = self.run_bucket(
            "-", "--regex", r"(?P<ts>\S+)",
            input_text="2026-11-26T14:01:35Z\n2026-11-26T14:00:05Z\n",
        )
        structured = self.run_bucket(
            "-", "--format", "jsonl",
            input_text='{"ts":"2026-11-26T09:00:05-05:00"}\n{"ts":"2026-11-26T14:01:35Z"}\n',
        )
        self.assertEqual(text.stdout, structured.stdout)
        rows = json.loads(text.stdout)["buckets"]
        self.assertEqual([row["count"] for row in rows], [1, 0, 0, 1])
        self.assertEqual([row["partial"] for row in rows], [True, False, False, True])
        self.assertEqual([row["events_per_minute"] for row in rows], [None, 0, 0, None])


class InspectTests(unittest.TestCase):
    def run_cli(self, *args, input_text=None):
        return subprocess.run(
            [sys.executable, "log_summary.py", *args],
            cwd=Path(__file__).resolve().parent, input=input_text,
            capture_output=True, text=True, check=True,
        )

    def test_inspects_incident_with_usable_bucket_suggestion(self):
        result = self.run_cli("inspect", "incident.jsonl")
        self.assertIn('Timestamp candidates: "ts"', result.stdout)
        self.assertIn('Signature candidates (by name): "code"', result.stdout)
        self.assertIn('ID candidates (by name): "request"', result.stdout)
        self.assertIn('"request": 83/93 | str', result.stdout)
        self.assertIn("bucket 'incident.jsonl' --format jsonl --time='ts' --interval 30s", result.stdout)

    def test_inspect_limits_stdin_sample_and_recognizes_text(self):
        result = self.run_cli(
            "inspect", "-", "--lines", "1",
            input_text='2026-11-26T14:00:00Z INFO Hello\n{"ts":"2026-11-26T14:00Z"}\n',
        )
        self.assertIn("Sampled: 1  JSON objects: 0  Text timestamp prefixes: 1", result.stdout)

    def test_bucket_distinguishes_empty_results_and_keeps_json_parseable(self):
        cases = [
            (["--time", "wrong"], "No usable timestamps in 'wrong'"),
            (["--time", "ts", "--since", "2026-11-27T00:00Z"], "requested window"),
            (["--time", "ts", "--signature", "code", "--value", "MISSING"], "No events match"),
        ]
        for options, message in cases:
            with self.subTest(options=options):
                result = self.run_cli(
                    "bucket", "incident.jsonl", "--format", "jsonl",
                    "--interval", "30s", "--json", *options,
                )
                self.assertEqual(json.loads(result.stdout)["coverage"]["counted"], 0)
                self.assertIn(message, result.stderr)


if __name__ == "__main__":
    unittest.main()
