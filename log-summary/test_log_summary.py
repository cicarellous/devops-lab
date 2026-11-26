import gzip
import json
import shlex
import subprocess
import sys
import unittest
from os import environ
from pathlib import Path
from tempfile import TemporaryDirectory

from log_summary import count_errors, count_levels, event_time, spread_sample


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


class MultiFileSearchTests(unittest.TestCase):
    def run_search(self, directory, *options, data=None):
        script = Path(__file__).resolve().with_name("log_summary.py")
        return subprocess.run(
            [sys.executable, str(script), "search", *options],
            cwd=directory, text=True, input=data, capture_output=True,
        )

    def test_glob_preserves_source_and_context_across_plain_and_gzip_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.log").write_text("before\nERROR first\n  frame\n", encoding="utf-8")
            with gzip.open(root / "app.log.1.gz", "wt", encoding="utf-8") as archive:
                archive.write("unrelated\nbefore\nERROR second\nafter\n")
            options = ("app.log*", "--contains", "ERROR", "--context", "1", "--multiline")
            result = self.run_search(directory, *options, "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertIsNone(report["source"])
            self.assertEqual(report["sources"], ["app.log", "app.log.1.gz"])
            self.assertEqual(report["coverage"],
                             {"scanned": 6, "physical_lines": 7, "matched": 2, "displayed": 5})
            self.assertEqual([(row["source"], row["line"], row["end_line"]) for row in report["lines"]],
                             [("app.log", 1, 1), ("app.log", 2, 3), ("app.log.1.gz", 2, 2),
                              ("app.log.1.gz", 3, 3), ("app.log.1.gz", 4, 4)])
            text = self.run_search(directory, *options)
            self.assertIn("app.log:2-3: ERROR first", text.stdout)
            self.assertIn("app.log.1.gz:3: ERROR second", text.stdout)

    def test_single_input_json_always_lists_sources(self):
        with TemporaryDirectory() as directory:
            result = self.run_search(directory, "-", "--contains", "ERROR", "--json", data="ERROR stdin\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["source"], "<stdin>")
            self.assertEqual(report["sources"], ["<stdin>"])
            self.assertNotIn("source", report["lines"][0])

    def test_accepts_stdin_once_and_rejects_unmatched_globs(self):
        with TemporaryDirectory() as directory:
            (Path(directory) / "app.log").write_text("ERROR file\n", encoding="utf-8")
            result = self.run_search(directory, "-", "--contains", "ERROR", "app.log", data="ERROR stdin\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ["<stdin>:1: ERROR stdin", "app.log:1: ERROR file"])
            for inputs in (("-", "-"), ("*.missing",)):
                with self.subTest(inputs=inputs):
                    self.assertEqual(self.run_search(directory, *inputs, "--contains", "ERROR").returncode, 2)


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
                file_rows = file_result.stdout.splitlines()[:-1]
                stdin_rows = stdin_result.stdout.splitlines()[:-1]
                self.assertEqual(
                    [row.split("  ", 2)[2] for row in stdin_rows],
                    [row.split("  ", 2)[2] for row in file_rows],
                )
                self.assertIn(f"  {filename}:1  ", file_rows[0])
                self.assertIn("  <stdin>:20  ", stdin_rows[0])
                rows = stdin_result.stdout.splitlines()
                self.assertEqual(rows[-1], "Scanned: 20  Matched: 6  Skipped: 0  Outside window: 0  Other IDs: 14")
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
        self.assertEqual(rows[2], "Scanned: 5  Matched: 2  Skipped: 2  Outside window: 0  Other IDs: 1")


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
        self.assertEqual(rows[-1], "Scanned: 20  IDs: 5  Skipped: 0  Used: 20  Outside window: 0")

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
                self.assertEqual(rows[-1], "Scanned: 7  IDs: 2  Skipped: 2  Used: 5  Outside window: 0")


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
                    sum(coverage[name] for name in ("counted", "invalid_timestamp", "outside_window", "filtered")),
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


class CompareGateTests(unittest.TestCase):
    def run_compare(self, records, *options):
        data = "".join(
            json.dumps({"ts": f"2026-11-26T14:{minute}:10Z", "code": code}) + "\n"
            for minute, code, count in records
            for _ in range(count)
        )
        return subprocess.run(
            [sys.executable, "log_summary.py", "compare", "-", "--format", "jsonl",
             "--time", "ts", "--signature", "code", "--json",
             "--baseline", "2026-11-26T14:00Z", "2026-11-26T14:01Z",
             "--incident", "2026-11-26T14:01Z", "2026-11-26T14:03Z", *options],
            cwd=Path(__file__).resolve().parent, input=data, text=True, capture_output=True,
        )

    def test_gate_uses_both_normalized_thresholds_and_all_rows(self):
        records = [("00", "high", 10), ("01", "high", 30),
                   ("00", "low", 1), ("01", "low", 6),
                   ("00", "small", 1), ("01", "small", 4)]
        result = self.run_compare(records, "--fail-on", "increase", "--min-increase-rate", "2",
                                  "--min-increase-percent", "200", "--top", "1")
        self.assertEqual(result.returncode, 4, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["gate"]["breach_count"], 1)
        self.assertEqual(report["gate"]["hidden_breach_count"], 1)
        self.assertEqual(report["gate"]["highest_hidden_breach"], {
            "signature": "low", "service": None, "delta_events_per_minute": 2,
        })
        self.assertIn('--top hid 1 breach(es). Highest ranked: "low"', result.stderr)
        self.assertEqual(report["results"][0]["signature"], "high")
        self.assertFalse(report["results"][0]["breached"])

    def test_new_signature_uses_absolute_threshold_with_a_usable_baseline(self):
        options = ("--fail-on", "increase", "--min-increase-rate", "2", "--min-increase-percent", "500")
        for count, status in ((3, 0), (4, 4)):
            with self.subTest(count=count):
                result = self.run_compare([("00", "existing", 1), ("01", "new", count)], *options)
                self.assertEqual(result.returncode, status, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["results"][0]["breached"], status == 4)
        missing = self.run_compare([("01", "new", 4)], *options)
        self.assertEqual(missing.returncode, 3, missing.stderr)
        self.assertFalse(json.loads(missing.stdout)["gate"]["baseline_available"])

    def test_gate_requires_both_positive_thresholds(self):
        for options in (
            ("--fail-on", "increase", "--min-increase-rate", "2"),
            ("--min-increase-rate", "2"),
            ("--fail-on", "increase", "--min-increase-rate", "2", "--min-increase-percent", "inf"),
        ):
            with self.subTest(options=options):
                self.assertEqual(self.run_compare([], *options).returncode, 2)


class SpikeDetectionTests(unittest.TestCase):
    def run_bucket(self, counts, *options, before="2026-11-26T14:08:30Z"):
        data = "".join(
            json.dumps({"ts": f"2026-11-26T14:{minute:02d}:20Z", "request_id": f"r-{minute}-{event}"}) + "\n"
            for minute, count in enumerate(counts)
            for event in range(count)
        )
        return subprocess.run(
            [sys.executable, "log_summary.py", "bucket", "-", "--format", "jsonl",
             "--time", "ts", "--interval", "1m", "--since", "2026-11-26T14:00:10Z",
             *(("--before", before) if before else ()), *options],
            cwd=Path(__file__).resolve().parent, input=data, text=True, capture_output=True,
        )

    def test_flags_only_full_buckets_above_threshold_after_warmup(self):
        counts = [100, 2, 2, 2, 2, 2, 6, 7, 100]
        result = self.run_bucket(counts, "--spike-factor", "3", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["spike_factor"], 3)
        self.assertEqual([bucket["spike"] for bucket in report["buckets"]],
                         [False] * 7 + [True, False])
        self.assertEqual([bucket["baseline_count"] for bucket in report["buckets"]],
                         [None] * 6 + [2, 2, None])
        self.assertIn("SPIKE (baseline 2)", self.run_bucket(counts, "--spike-factor", "3").stdout)
        plain = json.loads(self.run_bucket(counts, "--json").stdout)
        self.assertNotIn("spike_factor", plain)
        self.assertNotIn("spike", plain["buckets"][7])

    def test_zero_median_does_not_flag_a_single_event(self):
        result = self.run_bucket([0, 0, 0, 0, 0, 0, 1], "--spike-factor", "3", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        bucket = json.loads(result.stdout)["buckets"][6]
        self.assertEqual((bucket["baseline_count"], bucket["count"], bucket["spike"]), (0, 1, False))

    def test_spike_gate_keeps_report_and_returns_four_only_for_a_spike(self):
        options = ("--fail-on", "spike", "--spike-factor", "3", "--json")
        for count, status in ((6, 0), (7, 4)):
            with self.subTest(count=count):
                result = self.run_bucket([0, 2, 2, 2, 2, 2, count], *options)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(json.loads(result.stdout)["buckets"][6]["spike"], status == 4)
        result = self.run_bucket([], "--fail-on", "spike")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--fail-on spike requires --spike-factor", result.stderr)

    def test_spike_gate_reports_unevaluable_windows(self):
        options = ("--fail-on", "spike", "--spike-factor", "3", "--json")
        for counts, before, extra in (
            ([], "2026-11-26T14:08:30Z", ("--spike-min-count", "20")),
            ([0, 2, 2, 2, 2, 2, 20], None, ()),
            ([0, 0, 0, 0, 0, 0, 20], "2026-11-26T14:07:00Z", ()),
        ):
            with self.subTest(counts=counts, before=before):
                result = self.run_bucket(counts, *options, *extra, before=before)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(json.loads(result.stdout)["evaluated_buckets"], 0)

    def test_aligned_window_can_flag_zero_median_onset(self):
        options = ("--fail-on", "spike", "--spike-factor", "3", "--spike-min-count", "20", "--json")
        for count, status in ((19, 0), (20, 4)):
            with self.subTest(count=count):
                result = self.run_bucket([0, 0, 0, 0, 0, 0, count], *options,
                                         before="2026-11-26T14:07:00Z")
                self.assertEqual(result.returncode, status, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["evaluated_buckets"], 1)
                self.assertTrue(report["buckets"][-1]["evaluated"])
                self.assertEqual(report["buckets"][-1]["spike"], status == 4)
        self.assertEqual(self.run_bucket([], "--spike-min-count", "20").returncode, 2)

    def test_spike_examples_include_bounded_evidence_and_ids(self):
        options = ("--spike-factor", "3", "--spike-examples", "2", "--id", "request_id")
        counts = [0, 2, 2, 2, 2, 2, 7]
        result = self.run_bucket(counts, *options, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["spike_examples"], 2)
        self.assertTrue(all("examples" not in bucket for bucket in report["buckets"][:6]))
        examples = report["buckets"][6]["examples"]
        self.assertEqual([(row["source"], row["line"], row["end_line"], row["id"]) for row in examples],
                         [("<stdin>", 11, 11, "r-6-0"), ("<stdin>", 12, 12, "r-6-1")])
        self.assertIn("r-6-0", examples[0]["preview"])
        text = self.run_bucket(counts, *options)
        self.assertIn('<stdin>:11 id="r-6-0"', text.stdout)
        for invalid in (("--spike-examples", "2"), ("--id", "request_id"),
                        ("--spike-factor", "3", "--spike-examples", "11")):
            with self.subTest(options=invalid):
                self.assertEqual(self.run_bucket(counts, *invalid).returncode, 2)

    def test_factor_rejects_invalid_values(self):
        for value in ("1", "nan", "inf", "invalid"):
            with self.subTest(value=value):
                result = self.run_bucket([], "--spike-factor", value)
                self.assertEqual(result.returncode, 2)
                self.assertIn("must be a finite number greater than 1", result.stderr)


class InspectTests(unittest.TestCase):
    def run_cli(self, *args, input_text=None):
        return subprocess.run(
            [sys.executable, "log_summary.py", *args],
            cwd=Path(__file__).resolve().parent, input=input_text,
            capture_output=True, text=True, check=True,
        )

    def test_spread_sample_is_bounded_ordered_and_retains_endpoints(self):
        for size in (0, 1, 2, 3, 5, 10, 50, 100, 1000):
            records = [(line, line, str(line), str(line)) for line in range(1, size + 1)]
            for limit in range(1, 16):
                with self.subTest(size=size, limit=limit):
                    sample = list(spread_sample(iter(records), limit))
                    self.assertEqual(len(sample), min(size, limit))
                    starts = [start for start, _, _ in sample]
                    self.assertEqual(starts, sorted(set(starts)))
                    if size:
                        self.assertEqual(starts[0], 1)
                        if limit > 1:
                            self.assertEqual(starts[-1], size)

    def test_spread_sampling_retains_late_fields_and_sampled_line_count(self):
        data = ('2026-11-26T14:00:00Z INFO Started\n  first frame\n'
                'INFO Progress\nINFO Closing\n'
                '{"ts":"2026-11-26T14:20:00Z","code":"END"}\n')
        args = ("inspect", "-", "--lines", "2", "--multiline", "--json")
        head = json.loads(self.run_cli(*args, input_text=data).stdout)
        spread = json.loads(self.run_cli(*args, "--sample", "spread", input_text=data).stdout)
        self.assertEqual(head["sample"], "head")
        self.assertEqual(spread["sample"], "spread")
        self.assertEqual(head["coverage"]["json_objects"], 0)
        self.assertEqual(spread["coverage"]["json_objects"], 1)
        self.assertEqual(spread["coverage"]["sampled"], 2)
        self.assertEqual(spread["coverage"]["physical_lines"], 3)
        self.assertEqual(spread["profile"]["time"], "ts")

    def test_version_and_summary_of_file_named_like_a_command(self):
        self.assertEqual(self.run_cli("--version").stdout.strip(), "Log Summary 0.1.0")
        script = Path(__file__).resolve().with_name("log_summary.py")
        with TemporaryDirectory() as directory:
            (Path(directory) / "search").write_text("ERROR Example\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(script), "--", "search"], cwd=directory,
                capture_output=True, text=True, check=True,
            )
        self.assertIn("ERROR: 1", result.stdout)

    def test_inspects_incident_with_usable_bucket_suggestion(self):
        result = self.run_cli("inspect", "incident.jsonl")
        self.assertIn('Timestamp candidates: "ts"', result.stdout)
        self.assertIn('Signature candidates (by name): "code"', result.stdout)
        self.assertIn('ID candidates (by name): "request"', result.stdout)
        self.assertIn('"request": 83/93 | str', result.stdout)
        self.assertIn("bucket 'incident.jsonl' --format jsonl --time='ts' --interval 30s", result.stderr)

    def test_inspect_limits_stdin_sample_and_recognizes_text(self):
        result = self.run_cli(
            "inspect", "-", "--lines", "1",
            input_text='2026-11-26T14:00:00Z INFO Hello\n{"ts":"2026-11-26T14:00Z"}\n',
        )
        self.assertIn("Sampled: 1  JSON objects: 0  Text timestamp prefixes: 1", result.stdout)
        report = json.loads(self.run_cli(
            "inspect", "-", "--lines", "1", "--json", input_text="first\nsecond\n",
        ).stdout)
        self.assertEqual(report["coverage"]["physical_lines"], 1)

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


class InspectFollowThroughTests(unittest.TestCase):
    def run_cli(self, *args, data=None):
        return subprocess.run(
            [sys.executable, "log_summary.py", *args],
            cwd=Path(__file__).resolve().parent, input=data,
            text=True, encoding="utf-8", capture_output=True, check=True,
        )

    def test_generated_profile_works_for_all_consumers(self):
        with TemporaryDirectory() as directory:
            profile = Path(directory) / "generated.json"
            self.run_cli("inspect", "app.jsonl", "--profile-out", str(profile))
            mapping = json.loads(profile.read_text(encoding="utf-8"))
            self.assertEqual((mapping["signature"], mapping["by"]), ("code", "code"))
            for command, options, field in (
                ((), (), "counts"),
                (("group",), (), "results"),
                (("bucket",), ("--interval", "30s"), "buckets"),
                (("compare",), ("--baseline", "2026-11-26T14:00:00Z", "2026-11-26T14:00:01Z",
                                "--incident", "2026-11-26T14:00:01Z", "2026-11-26T14:01:00Z"), "results"),
                (("correlate",), (), "results"),
                (("timeline",), ("--value", "r-100"), "events"),
            ):
                with self.subTest(command=command):
                    report = json.loads(self.run_cli(
                        *command, "app.jsonl", "--profile", str(profile), *options, "--json",
                    ).stdout)
                    self.assertTrue(report[field])

    def test_suggested_bucket_command_preserves_multiline(self):
        result = self.run_cli("inspect", "stacktrace-example.txt", "--multiline")
        suggestion = next(line.strip() for line in result.stderr.splitlines()
                          if " log_summary.py bucket " in line)
        report = json.loads(self.run_cli(*shlex.split(suggestion)[2:], "--json").stdout)
        coverage = report["coverage"]
        self.assertEqual((coverage["scanned"], coverage["physical_lines"], coverage["invalid_timestamp"]),
                         (3, 6, 0))

    def test_inspect_quotes_apostrophes_for_the_host_shell(self):
        with TemporaryDirectory() as directory:
            log = Path(directory) / "night's log.jsonl"
            log.write_text(json.dumps({"event's time": "2026-11-26T14:01:00Z"}), encoding="utf-8")
            result = self.run_cli("inspect", str(log))
            suggestion = next(line for line in result.stderr.splitlines() if " log_summary.py bucket " in line)
            if sys.platform == "win32":
                self.assertIn("night''s log.jsonl", suggestion)
                self.assertIn("--time='event''s time'", suggestion)
            else:
                argv = shlex.split(suggestion)
                self.assertEqual(argv[3], str(log))
                self.assertIn("--time=event's time", argv)
                self.assertEqual(argv[0], f"python{sys.version_info.major}.{sys.version_info.minor}")

    def test_suggested_bucket_interval_handles_sparse_input(self):
        data = '{"ts":"2020-01-01T00:00:00Z"}\n{"ts":"2021-01-01T00:00:00Z"}\n'
        result = self.run_cli("inspect", "-", data=data)
        suggestion = next(line.strip() for line in result.stderr.splitlines()
                          if " log_summary.py bucket " in line)
        report = json.loads(self.run_cli(*shlex.split(suggestion)[2:], "--json", data=data).stdout)
        self.assertEqual(report["coverage"]["counted"], 2)
        self.assertLessEqual(len(report["buckets"]), 10_000)

    def test_positional_source_and_stdin_emit_full_coverage(self):
        fixture = Path(__file__).resolve().with_name("app.jsonl").read_text(encoding="utf-8")
        for command, options in (((), ()), (("group",), ()), (("bucket",), ("--interval", "30s"))):
            with self.subTest(command=command):
                reports = [
                    json.loads(self.run_cli(*command, *source, *options, "--json", data=data).stdout)
                    for source, data in (
                        (("app.jsonl", "--profile", "app-jsonl-profile.json"), None),
                        (("--source", "app.jsonl", "app-jsonl-profile.json"), None),
                        (("-", "--profile", "app-jsonl-profile.json"), fixture),
                    )
                ]
                coverages = [report["coverage"] for report in reports]
                self.assertEqual(coverages[0], coverages[1])
                self.assertEqual(set(coverages[0]), set(coverages[2]))
                for coverage in coverages:
                    self.assertEqual(coverage["physical_lines"], 20)
                    self.assertEqual(len(coverage["sources"]), 1)
                    self.assertEqual(coverage["sources"][0]["physical_lines"], 20)


class ProfileTests(unittest.TestCase):
    def run_cli(self, *args, input_text=None):
        return subprocess.run(
            [sys.executable, "log_summary.py", *args],
            cwd=Path(__file__).resolve().parent, input=input_text,
            capture_output=True, text=True,
        )

    def test_one_profile_reproduces_explicit_mappings(self):
        cases = [
            ("bucket", ["--interval", "30s", "--value", "TIMEOUT", "--json"], ["--signature", "code"]),
            ("bucket", ["--interval", "30s", "--json"], []),
            ("timeline", ["--value", "incident-00"], ["--key", "request"]),
            ("correlate", [], ["--key", "request"]),
            ("compare", [
                "--baseline", "2026-11-26T14:00Z", "2026-11-26T14:01Z",
                "--incident", "2026-11-26T14:01Z", "2026-11-26T14:03Z",
            ], ["--signature", "code", "--service", "service", "--id", "request"]),
        ]
        for command, options, mapping in cases:
            with self.subTest(command=command, options=options):
                common = (command, "incident.jsonl", *options)
                explicit = self.run_cli(*common, "--format", "jsonl", "--time", "ts", *mapping)
                profiled = self.run_cli(*common, "--profile", "incident-profile.json")
                self.assertEqual(explicit.returncode, 0, explicit.stderr)
                self.assertEqual(profiled.returncode, 0, profiled.stderr)
                self.assertEqual((profiled.stdout, profiled.stderr), (explicit.stdout, explicit.stderr))

    def test_explicit_timestamp_overrides_text_profile(self):
        with TemporaryDirectory() as directory:
            profile = Path(directory) / "profile.json"
            profile.write_text(json.dumps({
                "format": "text",
                "time": "wrong",
                "id": "request",
                "regex": r"^(?P<ts>\S+) request=(?P<request>\S+)",
            }), encoding="utf-8")
            result = self.run_cli(
                "timeline", "-", "--profile", str(profile), "--time", "ts", "--value", "r1",
                input_text="2026-11-26T14:00Z request=r1\n",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Matched: 1", result.stdout)

    def test_invalid_profile_has_a_clear_usage_error(self):
        with TemporaryDirectory() as directory:
            profile = Path(directory) / "profile.json"
            profile.write_text('{"timestamp":"ts"}', encoding="utf-8")
            result = self.run_cli(
                "bucket", "incident.jsonl", "--profile", str(profile), "--interval", "30s",
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("unknown profile field 'timestamp'", result.stderr)


class InvestigationOutputTests(unittest.TestCase):
    def run_cli(self, *args, check=True):
        return subprocess.run(
            [sys.executable, "log_summary.py", *args],
            cwd=Path(__file__).resolve().parent, capture_output=True,
            text=True, check=check,
        )

    def test_multisource_timeline_orders_events_and_preserves_provenance(self):
        args = (
            "timeline", "--source", "incident.jsonl", "incident-profile.json",
            "--source", "worker-example.txt", "worker-profile.json",
            "--value", "incident-00",
        )
        text = self.run_cli(*args).stdout
        report = json.loads(self.run_cli(*args, "--json").stdout)
        self.assertEqual(report["schema_version"], 1)
        events = report["events"]
        self.assertEqual(len(events), 9)
        self.assertEqual([event["timestamp"] for event in events],
                         sorted(event["timestamp"] for event in events))
        self.assertEqual([(event["source"], event["line"]) for event in events[:3]], [
            ("incident.jsonl", 8), ("worker-example.txt", 2), ("worker-example.txt", 1),
        ])
        self.assertIn("worker-example.txt:2", text)
        self.assertEqual(report["coverage"]["sources"][1], {
            "source": "worker-example.txt", "scanned": 3, "matched": 2, "skipped": 0,
            "outside_window": 0, "other_ids": 1, "physical_lines": 3,
        })

    def test_compare_json_preserves_rank_and_coverage_when_limited(self):
        args = ("compare", "incident.jsonl", "--profile", "incident-profile.json",
                "--baseline", "2026-11-26T14:00Z", "2026-11-26T14:01Z",
                "--incident", "2026-11-26T14:01Z", "2026-11-26T14:03Z")
        complete = json.loads(self.run_cli(*args, "--json").stdout)
        limited = json.loads(self.run_cli(*args, "--json", "--top", "1").stdout)
        self.assertEqual(complete["schema_version"], 1)
        self.assertEqual(limited["total_rows"], 3)
        self.assertEqual(limited["coverage"], complete["coverage"])
        self.assertEqual(limited["results"], complete["results"][:1])
        top = limited["results"][0]
        self.assertEqual((top["signature"], top["change"]), ("TIMEOUT", "RISING"))
        self.assertEqual(top["incident"], {
            "events": 80, "events_per_minute": 40.0, "observed_ids": 12,
        })
        self.assertIn('RISING "TIMEOUT"', self.run_cli(*args, "--top", "1").stdout)

    def test_ranked_text_reports_show_the_limit_and_keep_totals(self):
        cases = [
            (("group", "app.jsonl", "--format", "jsonl", "--by", "service"),
             "Showing 1 of 4 values", "Grouped: 20"),
            (("correlate", "incident.jsonl", "--profile", "incident-profile.json"),
             "Showing 1 of 14 IDs", "IDs: 14"),
        ]
        for args, shown, totals in cases:
            with self.subTest(command=args[0]):
                result = self.run_cli(*args, "--top", "1")
                self.assertIn(shown, result.stdout)
                self.assertIn(totals, result.stdout)
                invalid = self.run_cli(*args, "--top", "0", check=False)
                self.assertEqual(invalid.returncode, 2)


class InputCompatibilityTests(unittest.TestCase):
    def run_cli(self, *args, input_text=None, check=True):
        return subprocess.run(
            [sys.executable, "log_summary.py", *args],
            cwd=Path(__file__).resolve().parent, input=input_text,
            capture_output=True, encoding="utf-8", check=check,
            env={**environ, "PYTHONIOENCODING": "utf-8"},
        )

    def test_bom_in_log_profile_and_stdin(self):
        record = '{"level":"ERROR","code":"E1"}\n'
        with TemporaryDirectory() as directory:
            log = Path(directory) / "events.jsonl"
            profile = Path(directory) / "profile.json"
            log.write_text(record, encoding="utf-8-sig")
            profile.write_text(
                '{"format":"jsonl","by":"code","level_field":"level"}',
                encoding="utf-8-sig",
            )
            result = self.run_cli(str(log), "--profile", str(profile), "--json")
            report = json.loads(result.stdout)
            self.assertEqual((report["command"], report["counts"]["ERROR"]), ("summary", 1))
            self.assertIn('    1  "E1"', self.run_cli(
                "group", str(log), "--profile", str(profile),
            ).stdout)
            result = self.run_cli("search", "-", "--contains", "INFO", input_text="\ufeffINFO Ready\n")
            self.assertEqual(result.stdout, "1: INFO Ready\n")

    def test_key_value_logs_support_quoted_values_and_saved_profile(self):
        with TemporaryDirectory() as directory:
            profile = Path(directory) / "worker.json"
            result = self.run_cli("inspect", "worker-example.txt", "--profile-out", str(profile))
            self.assertIn("Suggested format: logfmt", result.stdout)
            self.assertEqual(json.loads(profile.read_text()), {
                "format": "logfmt", "id": "job", "time": "timestamp",
                "service": "service", "signature": "code", "by": "code",
            })
            existing = self.run_cli("inspect", "worker-example.txt", "--profile-out", str(profile), check=False)
            self.assertEqual(existing.returncode, 1)
            timeline = self.run_cli(
                "timeline", "-", "--profile", str(profile), "--value", "job 1",
                input_text='2026-11-26T14:00Z ERROR job="job 1" note="has spaces" free text\n',
            )
            self.assertEqual(timeline.stdout.splitlines()[0],
                             '2026-11-26 14:00:00.000000+00:00  <stdin>:1  '
                             '2026-11-26T14:00Z ERROR job="job 1" note="has spaces" free text')
            self.assertIn("Matched: 1", timeline.stdout)

    def test_rejects_regex_in_jsonl_profile_and_reports_empty_search(self):
        with TemporaryDirectory() as directory:
            profile = Path(directory) / "invalid.json"
            profile.write_text('{"format":"jsonl","regex":"(.)"}', encoding="utf-8")
            result = self.run_cli("group", "-", "--profile", str(profile), "--by", "code", check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("profile regex applies only to text input", result.stderr)
        empty = self.run_cli("search", "-", "--contains", "x", input_text="")
        self.assertEqual(empty.stderr.strip(), "The input is empty.")
        self.assertEqual(empty.stdout, "")

    def test_bucket_text_labels_filter_and_rejects_oversized_bounds_before_open(self):
        args = ("bucket", "incident.jsonl", "--profile", "incident-profile.json",
                "--interval", "30s", "--value", "timeout", "--ignore-case")
        result = self.run_cli(*args)
        self.assertEqual(result.stdout.splitlines()[0],
                         "Interval: 30s  Filter: code='timeout' (case-insensitive)")
        self.assertIn("Counted: 82", result.stdout)
        report = json.loads(self.run_cli(*args, "--json").stdout)
        self.assertEqual((report["command"], report["source"]), ("bucket", "incident.jsonl"))
        result = self.run_cli(
            "bucket", "missing.log", "--format", "jsonl", "--time", "ts", "--interval", "1s",
            "--since", "2026-11-26T00:00Z", "--until", "2026-11-27T00:00Z", check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("exceeds 10000 buckets", result.stderr)


class WindowContractTests(unittest.TestCase):
    log = (
        '{"ts":"2026-11-26T14:00:30Z","id":"r1","code":"E1"}\n'
        '{"ts":"2026-11-26T09:01:00-05:00","id":"r1","code":"E2"}\n'
    )

    def run_cli(self, *args, data=None):
        return subprocess.run(
            [sys.executable, "log_summary.py", *args],
            cwd=Path(__file__).resolve().parent,
            input=self.log if data is None else data, text=True, capture_output=True,
        )

    def test_adjacent_windows_assign_the_boundary_only_to_the_incident(self):
        common = ("-", "--format", "jsonl", "--time", "ts")
        windows = (
            ("--since", "2026-11-26T14:00Z", "--before", "2026-11-26T14:01Z"),
            ("--since", "2026-11-26T14:01Z", "--before", "2026-11-26T14:02Z"),
        )
        for window, line, signature in zip(windows, (1, 2), ("E1", "E2")):
            with self.subTest(window=window):
                timeline = self.run_cli(
                    "timeline", *common, "--key", "id", "--value", "r1", "--json", *window,
                )
                self.assertEqual(timeline.returncode, 0, timeline.stderr)
                self.assertEqual([event["line"] for event in json.loads(timeline.stdout)["events"]], [line])

                correlate = self.run_cli("correlate", *common, "--key", "id", *window)
                self.assertEqual(correlate.returncode, 0, correlate.stderr)
                self.assertEqual(correlate.stdout.splitlines()[1].split()[0], "1")
                self.assertIn("IDs: 1", correlate.stdout)

                bucket = self.run_cli(
                    "bucket", *common, "--interval", "1m", "--signature", "code",
                    "--value", signature, "--json", *window,
                )
                self.assertEqual(bucket.returncode, 0, bucket.stderr)
                report = json.loads(bucket.stdout)
                buckets = report["buckets"]
                self.assertEqual([(row["count"], row["partial"]) for row in buckets], [(1, False)])
                self.assertEqual(report["coverage"]["outside_window"], 1)
                self.assertEqual(report["coverage"]["filtered"], 0)

        compare = self.run_cli(
            "compare", *common, "--signature", "code", "--json",
            "--baseline", "2026-11-26T14:00Z", "2026-11-26T14:01Z",
            "--incident", "2026-11-26T14:01Z", "2026-11-26T14:02Z",
        )
        self.assertEqual(compare.returncode, 0, compare.stderr)
        rows = {row["signature"]: row for row in json.loads(compare.stdout)["results"]}
        self.assertEqual((rows["E2"]["baseline"]["events"], rows["E2"]["incident"]["events"]), (0, 1))

    def test_until_preserves_legacy_semantics_and_conflicts_with_before(self):
        common = ("-", "--format", "jsonl", "--time", "ts")
        bounds = ("--since", "2026-11-26T14:00Z", "--until", "2026-11-26T14:01Z")
        timeline = self.run_cli("timeline", *common, "--key", "id", "--value", "r1", "--json", *bounds)
        self.assertEqual(timeline.returncode, 0, timeline.stderr)
        self.assertEqual(json.loads(timeline.stdout)["coverage"]["matched"], 2)
        correlate = self.run_cli("correlate", *common, "--key", "id", *bounds)
        self.assertEqual(correlate.returncode, 0, correlate.stderr)
        self.assertEqual(correlate.stdout.splitlines()[1].split()[0], "2")
        bucket = self.run_cli("bucket", *common, "--interval", "1m", "--json", *bounds)
        self.assertEqual(bucket.returncode, 0, bucket.stderr)
        self.assertEqual(json.loads(bucket.stdout)["coverage"]["counted"], 1)

        for end_args, message in [
            ((*bounds, "--before", "2026-11-26T14:02Z"), "not allowed"),
            (("--before", "bad"), "invalid --before timestamp"),
            (("--since", "2026-11-26T14:01Z", "--before", "2026-11-26T14:01Z"),
             "--since must be before --before"),
        ]:
            result = self.run_cli("timeline", *common, "--key", "id", "--value", "r1", *end_args)
            self.assertEqual(result.returncode, 2)
            self.assertIn(message, result.stderr)

    def test_empty_result_diagnostics_stay_on_stderr(self):
        common = ("-", "--format", "jsonl", "--time", "ts")
        timeline = self.run_cli(
            "timeline", *common, "--key", "id", "--value", "missing", "--json",
        )
        self.assertEqual(timeline.returncode, 0, timeline.stderr)
        self.assertEqual(json.loads(timeline.stdout)["events"], [])
        self.assertIn("No events match ID", timeline.stderr)

        timeline = self.run_cli(
            "timeline", *common, "--key", "id", "--value", "r1", "--json",
            "--before", "2026-11-26T14:00Z",
        )
        self.assertEqual(timeline.returncode, 0, timeline.stderr)
        self.assertEqual(json.loads(timeline.stdout)["events"], [])
        self.assertIn("requested window", timeline.stderr)

        compare = self.run_cli(
            "compare", "-", "--format", "jsonl", "--time", "wrong", "--signature", "code",
            "--baseline", "2026-11-26T14:00Z", "2026-11-26T14:01Z",
            "--incident", "2026-11-26T14:01Z", "2026-11-26T14:02Z", "--json",
        )
        self.assertEqual(compare.returncode, 0, compare.stderr)
        self.assertEqual(json.loads(compare.stdout)["results"], [])
        self.assertIn("No usable timestamps in 'wrong'", compare.stderr)

        for args, message in [
            (("search", "-", "--contains", "missing"), "No matching lines"),
            (("group", "-", "--format", "jsonl", "--by", "wrong"), "No usable values for 'wrong'"),
            (("correlate", *common, "--key", "wrong"), "No usable events"),
        ]:
            with self.subTest(command=args[0]):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(message, result.stderr)
                self.assertNotIn(message, result.stdout)


class MultiSourceAnalysisTests(unittest.TestCase):
    sources = (
        "--source", "incident.jsonl", "incident-profile.json",
        "--source", "worker-example.txt", "worker-profile.json",
    )

    def run_cli(self, *args, check=True):
        return subprocess.run(
            [sys.executable, "log_summary.py", *args],
            cwd=Path(__file__).resolve().parent,
            capture_output=True, text=True, check=check,
        )

    def test_compare_preserves_source_evidence_and_window_coverage(self):
        result = self.run_cli(
            "compare", *self.sources,
            "--baseline", "2026-11-26T14:00Z", "2026-11-26T14:01Z",
            "--incident", "2026-11-26T14:01Z", "2026-11-26T14:03Z", "--json",
        )
        report = json.loads(result.stdout)
        worker = next(row for row in report["results"] if row["signature"] == "WORKER_TIMEOUT")
        self.assertEqual(worker["change"], "NEW")
        self.assertEqual(worker["example"], {
            "source": "worker-example.txt", "line": 1, "id": "incident-00",
        })
        worker_coverage = report["coverage"]["sources"][1]
        self.assertEqual(worker_coverage["baseline_scanned"], 0)
        self.assertEqual(worker_coverage["incident_scanned"], 3)
        self.assertEqual(worker_coverage["incident_used"], 1)
        timeline = self.run_cli("timeline", *self.sources, "--value", worker["example"]["id"])
        self.assertIn("Matched: 9", timeline.stdout)
        self.assertIn("worker-example.txt:1", timeline.stdout)

    def test_correlate_merges_the_same_id_and_lists_sources(self):
        result = self.run_cli("correlate", *self.sources, "--sort", "count", "--top", "1")
        top = result.stdout.splitlines()[1]
        self.assertEqual(top.split()[0], "9")
        self.assertTrue(top.endswith('"incident-00"  ["incident.jsonl", "worker-example.txt"]'))
        self.assertIn("worker-example.txt: scanned 3, used 3, skipped 0", result.stdout)

    def test_source_options_reject_conflicting_mappings_and_repeated_stdin(self):
        result = self.run_cli("correlate", *self.sources, "--key", "request", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--source replaces", result.stderr)
        result = self.run_cli(
            "correlate", "--source", "-", "incident-profile.json",
            "--source", "-", "worker-profile.json", check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("stdin can only be used as one source", result.stderr)


class AutomationContractTests(unittest.TestCase):
    def run_cli(self, *args, data=None, env=None):
        return subprocess.run(
            [sys.executable, "log_summary.py", *args],
            cwd=Path(__file__).resolve().parent, input=data,
            text=True, capture_output=True, env=env,
        )

    def test_correlate_json_hands_ranked_id_to_timeline(self):
        sources = MultiSourceAnalysisTests.sources
        result = self.run_cli("correlate", *sources, "--top", "1", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report["command"], report["schema_version"], report["total_rows"]),
                         ("correlate", 1, 15))
        row, = report["results"]
        self.assertEqual((row["id"], row["events"], row["span_ms"]), ("incident-00", 9, 72_000))
        self.assertEqual(row["sources"], ["incident.jsonl", "worker-example.txt"])
        timeline = self.run_cli("timeline", *sources, "--value", row["id"], "--json")
        self.assertEqual(timeline.returncode, 0, timeline.stderr)
        self.assertEqual(len(json.loads(timeline.stdout)["events"]), row["events"])

    def test_structured_commands_skip_json_that_exceeds_decoder_limits(self):
        malformed = '{"value":' + "9" * 5000 + '}\n'
        valid = '{"severity":"ERROR","code":"E1","timestamp":"2026-11-26T14:00Z"}\n'
        cases = [
            (("-", "--format", "jsonl"), "unclassified"),
            (("group", "-", "--format", "jsonl", "--by", "code"), "skipped"),
            (("inspect", "-"), "json_objects"),
        ]
        for args, metric in cases:
            with self.subTest(args=args):
                result = self.run_cli(*args, "--json", data=malformed + valid,
                                      env={**environ, "PYTHONINTMAXSTRDIGITS": "4300"})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["coverage"][metric], 1)

    def test_inspect_and_group_use_nested_fields_with_clean_json(self):
        data = '{"event":{"ts":"2026-11-26T14:00Z","code":"E1"}}\n'
        inspect = self.run_cli("inspect", "-", "--json", data=data)
        self.assertEqual(inspect.returncode, 0, inspect.stderr)
        report = json.loads(inspect.stdout)
        self.assertEqual(report["command"], "inspect")
        self.assertEqual(report["profile"]["time"], "event.ts")
        self.assertEqual(report["profile"]["signature"], "event.code")
        shell = "PowerShell" if sys.platform == "win32" else "a POSIX shell"
        self.assertIn(f"Try in {shell}", inspect.stderr)
        group = self.run_cli("group", "-", "--format", "jsonl", "--by", "event.code",
                             "--json", data=data)
        self.assertEqual(group.returncode, 0, group.stderr)
        self.assertEqual(json.loads(group.stdout)["results"], [{"value": "E1", "count": 1}])

    def test_all_json_commands_support_opt_in_empty_status(self):
        window = ("--baseline", "2026-11-26T14:00Z", "2026-11-26T14:01Z",
                  "--incident", "2026-11-26T14:01Z", "2026-11-26T14:02Z")
        cases = [
            ("summary", ["-"]),
            ("search", ["search", "-", "--contains", "missing"]),
            ("group", ["group", "-", "--format", "jsonl", "--by", "id"]),
            ("timeline", ["timeline", "-", "--format", "jsonl", "--key", "id",
                          "--time", "ts", "--value", "missing"]),
            ("correlate", ["correlate", "-", "--format", "jsonl", "--key", "id", "--time", "ts"]),
            ("compare", ["compare", "-", "--format", "jsonl", "--time", "ts",
                         "--signature", "code", *window]),
            ("bucket", ["bucket", "-", "--format", "jsonl", "--time", "ts", "--interval", "1m"]),
            ("inspect", ["inspect", "-"]),
        ]
        for name, args in cases:
            with self.subTest(command=name):
                result = self.run_cli(*args, "--json", "--fail-on", "empty", data="")
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(json.loads(result.stdout)["command"], name)
                self.assertTrue(result.stderr)

    def test_search_json_keeps_original_line_numbers_and_context(self):
        result = self.run_cli("search", "-", "--contains", "hit", "--context", "1", "--json",
                              data="before\nhit\nafter\nignored\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["coverage"], {"scanned": 4, "matched": 1, "displayed": 3, "physical_lines": 4})
        self.assertEqual([item["line"] for item in report["lines"]], [1, 2, 3])

    def test_gzip_globs_and_configurable_severity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile.json"
            profile.write_text('{"format":"jsonl","time":"ts","key":"id","level_field":"priority"}',
                               encoding="utf-8")
            for minute in ("00", "01"):
                with gzip.open(root / f"events-{minute}.jsonl.gz", "wt", encoding="utf-8-sig") as stream:
                    stream.write(f'{{"ts":"2026-11-26T14:{minute}Z","id":"r1","priority":"critical"}}\n')
            source = self.run_cli("correlate", "--source", str(root / "*.gz"), str(profile), "--json")
            self.assertEqual(source.returncode, 0, source.stderr)
            self.assertEqual(json.loads(source.stdout)["results"][0]["events"], 2)
            log = str(root / "events-00.jsonl.gz")
            summary = self.run_cli(log, "--profile", str(profile), "--levels", "CRITICAL", "--json")
            self.assertEqual(summary.returncode, 0, summary.stderr)
            self.assertEqual(json.loads(summary.stdout)["counts"], {"CRITICAL": 1})
            bucket = self.run_cli("bucket", log, "--profile", str(profile), "--interval", "1m",
                                  "--level", "CRITICAL", "--json")
            self.assertEqual(bucket.returncode, 0, bucket.stderr)
            self.assertEqual(json.loads(bucket.stdout)["coverage"]["counted"], 1)


class ReportCorrectnessTests(unittest.TestCase):
    def run_cli(self, *args, data=None, io_encoding="utf-8"):
        return subprocess.run(
            [sys.executable, "log_summary.py", *args],
            cwd=Path(__file__).resolve().parent, input=data, capture_output=True,
            env={**environ, "PYTHONIOENCODING": io_encoding},
        )

    def test_warn_alias_respects_explicit_vocabulary(self):
        cases = [
            (("WARN ready\n", []), "WARNING"),
            (("level=warn message=x\n", ["--format", "logfmt"]), "WARNING"),
            (('{"severity":"warn"}\n', ["--format", "jsonl", "--levels", "WARN", "WARNING"]), "WARN"),
        ]
        for (data, args), level in cases:
            with self.subTest(level=level, args=args):
                result = self.run_cli("-", "--json", *args, data=data.encode())
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["counts"][level], 1)
                self.assertEqual(report["coverage"]["classified"], 1)
                self.assertEqual(report["coverage"]["unclassified"], 0)

    def test_text_profile_capture_is_independent_of_cli_regex_filter(self):
        with TemporaryDirectory() as directory:
            profile = Path(directory) / "profile.json"
            profile.write_text(json.dumps({
                "format": "text", "regex": r"priority=(?P<priority>\w+)",
                "level_field": "priority",
            }), encoding="utf-8")
            result = self.run_cli(
                "-", "--profile", str(profile), "--regex", "keep", "--json",
                data=b"priority=warn keep\npriority=error ignore\npriority=error keep\n",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["counts"], {"INFO": 0, "WARNING": 1, "ERROR": 1})
            profile.write_text('{"format":"text","regex":"(?P<other>.)"}', encoding="utf-8")
            invalid = self.run_cli("-", "--profile", str(profile), "--json", data=b"ERROR hi\n")
            self.assertEqual(invalid.returncode, 2)
            self.assertIn(b"named severity capture 'level'", invalid.stderr)

    def test_inspect_fail_on_no_mapping_leaves_profile_target_untouched(self):
        with TemporaryDirectory() as directory:
            profile = Path(directory) / "profile.json"
            for data in (b"random text\n", b'{"name":"example"}\n'):
                with self.subTest(data=data):
                    result = self.run_cli(
                        "inspect", "-", "--json", "--fail-on", "empty",
                        "--profile-out", str(profile), data=data,
                    )
                    self.assertEqual(result.returncode, 3, result.stderr)
                    self.assertIsNone(json.loads(result.stdout)["profile"])
                    self.assertFalse(profile.exists())

    def test_correlate_reports_all_coverage_including_outside_window(self):
        args = ("correlate", "app.jsonl", "--format", "jsonl", "--key", "request_id",
                "--time", "timestamp", "--since", "2026-11-26T14:00:01Z")
        result = self.run_cli(*args, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        coverage = json.loads(result.stdout)["coverage"]
        self.assertEqual(
            [coverage[name] for name in ("scanned", "used", "skipped", "outside_window")],
            [20, 12, 0, 8],
        )
        self.assertEqual(coverage["sources"], [
            {"source": "app.jsonl", "scanned": 20, "used": 12, "skipped": 0, "outside_window": 8, "physical_lines": 20},
        ])
        text = self.run_cli(*args)
        self.assertIn(b"Used: 12  Outside window: 8", text.stdout)

    def test_bucket_metadata_distinguishes_severity_alias_and_zero_matches(self):
        base = ("bucket", "-", "--format", "jsonl", "--time", "ts", "--interval", "1m",
                "--since", "2026-11-26T14:00Z", "--before", "2026-11-26T14:01Z", "--json")
        data = b'{"ts":"2026-11-26T14:00:30Z","severity":"warn"}\n'
        severity = self.run_cli(*base, "--level", "WARNING", data=data)
        self.assertEqual(severity.returncode, 0, severity.stderr)
        report = json.loads(severity.stdout)
        self.assertEqual((report["level"], report["ignore_case"], report["matched"]), ("WARNING", True, True))
        self.assertEqual(report["buckets"][0]["count"], 1)
        literal = self.run_cli(*base, "--signature", "severity", "--value", "WARNING", data=data)
        self.assertEqual(literal.returncode, 0, literal.stderr)
        report = json.loads(literal.stdout)
        self.assertEqual((report["level"], report["ignore_case"], report["matched"]), (None, False, False))
        self.assertEqual(report["buckets"][0]["count"], 0)

    def test_redirected_stdin_reads_utf8_strictly_even_under_ascii_stdio(self):
        data = ('\ufeff{"ts":"2026-11-26T14:00Z","id":"caf\u00e9"}\n').encode("utf-8")
        result = self.run_cli(
            "correlate", "-", "--format", "jsonl", "--time", "ts", "--key", "id",
            "--json", data=data, io_encoding="ascii",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["results"][0]["id"], "caf\u00e9")
        invalid = self.run_cli("search", "-", "--contains", "INFO", "--json", data=b"INFO \xff\n")
        self.assertEqual(invalid.returncode, 1)
        self.assertEqual(invalid.stdout, b"")
        self.assertIn(b"Cannot read log", invalid.stderr)


class DisplayTimezoneTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "log_summary.py", *args],
            cwd=Path(__file__).resolve().parent, text=True, capture_output=True,
        )

    def test_fixed_offset_applies_to_text_only_on_timed_commands(self):
        profile = ("app.jsonl", "--profile", "app-jsonl-profile.json")
        zone = ("--display-timezone", "UTC-03:00")
        for command, flags in (
            ("timeline", ("--key", "request_id", "--value", "r-100", "--since", "2026-11-26T14:00:00Z")),
            ("correlate", ("--key", "request_id")),
            ("bucket", ("--interval", "1m")),
            ("compare", ("--baseline", "2026-11-26T14:00:00Z", "2026-11-26T14:00:01Z",
                         "--incident", "2026-11-26T14:00:01Z", "2026-11-26T14:01:00Z")),
        ):
            with self.subTest(command=command):
                args = (command, *profile, *flags)
                result = self.run_cli(*args, *zone)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertRegex(result.stdout, r"2026-11-26[T ]11:[0-9:.]+-03:00")
                default_json = self.run_cli(*args, "--json")
                shifted_json = self.run_cli(*args, *zone, "--json")
                self.assertEqual(shifted_json.returncode, 0, shifted_json.stderr)
                self.assertEqual(shifted_json.stdout, default_json.stdout)

    def test_unavailable_named_zone_gives_actionable_error(self):
        result = self.run_cli("timeline", "--value", "r-100", "--display-timezone", "Example/Missing")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("Check the IANA name or install tzdata", result.stderr)

    def test_invalid_offset_is_a_usage_error(self):
        result = self.run_cli("timeline", "--value", "r-100", "--display-timezone", "UTC+24:00")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("use UTC or an offset", result.stderr)


class EpochTimestampTests(unittest.TestCase):
    def run_cli(self, *args, data=None):
        return subprocess.run(
            [sys.executable, "log_summary.py", *args],
            cwd=Path(__file__).resolve().parent, input=data,
            text=True, capture_output=True,
        )

    def test_timestamp_modes_agree_to_the_microsecond(self):
        expected = event_time("2026-11-26T14:01:00.123456Z")
        self.assertIsNotNone(expected)
        self.assertEqual(event_time("1795701660.123456", "epoch_s"), expected)
        self.assertEqual(event_time("1795701660123.456", "epoch_ms"), expected)
        self.assertEqual(event_time(-1, "epoch_ms"), event_time("1969-12-31T23:59:59.999Z"))
        for bad in (True, "1e99999999", "NaN", "1e-99999999", "0.0000001", "bad"):
            with self.subTest(bad=bad):
                self.assertIsNone(event_time(bad, "epoch_s"))

    def test_all_timed_commands_assign_epoch_boundary_to_later_window(self):
        start, boundary, end = ("2026-11-26T14:00Z", "2026-11-26T14:01Z", "2026-11-26T14:02Z")
        encodings = {
            "iso": ("2026-11-26T14:00:30Z", boundary),
            "epoch_s": (1795701630, 1795701660),
            "epoch_ms": (1795701630000, 1795701660000),
        }
        expected_reports = None
        for mode, timestamps in encodings.items():
            with self.subTest(mode=mode):
                data = "".join(json.dumps({"ts": ts, "id": "r1", "code": code}) + "\n"
                               for ts, code in zip(timestamps, ("BEFORE", "BOUNDARY")))
                common = ("-", "--format", "jsonl", "--time", "ts", "--time-format", mode, "--json")
                window = ("--since", boundary, "--before", end)
                commands = [
                    ("timeline", *common, "--key", "id", "--value", "r1", *window),
                    ("correlate", *common, "--key", "id", *window),
                    ("bucket", *common, "--interval", "1m", *window),
                    ("compare", *common, "--signature", "code",
                     "--baseline", start, boundary, "--incident", boundary, end),
                ]
                reports = []
                for args in commands:
                    result = self.run_cli(*args, data=data)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    reports.append(json.loads(result.stdout))
                timeline, correlate, bucket, compare = reports
                self.assertEqual([event["line"] for event in timeline["events"]], [2])
                self.assertEqual(correlate["results"][0]["events"], 1)
                self.assertEqual(bucket["buckets"][0]["count"], 1)
                changes = {row["signature"]: row for row in compare["results"]}
                self.assertEqual(changes["BOUNDARY"]["baseline"]["events"], 0)
                self.assertEqual(changes["BOUNDARY"]["incident"]["events"], 1)
                del timeline["events"][0]["text"]
                if expected_reports is None:
                    expected_reports = reports
                else:
                    self.assertEqual(reports, expected_reports)

    def test_inspect_saves_unambiguous_epoch_mapping(self):
        with TemporaryDirectory() as directory:
            profile = Path(directory) / "generated.json"
            result = self.run_cli(
                "inspect", "-", "--json", "--profile-out", str(profile),
                data='{"timestamp_ms":1795701660000,"request_id":"r1"}\n',
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            saved = json.loads(profile.read_text(encoding="utf-8"))
            self.assertEqual(saved["time_format"], "epoch_ms")
            self.assertEqual(saved, json.loads(result.stdout)["profile"])
            result = self.run_cli("bucket", "-", "--profile", str(profile), "--interval", "1m",
                                  "--json", data='{"timestamp_ms":1795701660000}\n')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["coverage"]["counted"], 1)
            mixed = self.run_cli(
                "inspect", "-", "--json",
                data='{"ts":1795701660000}\n{"ts":1795701660}\n',
            )
            self.assertEqual(mixed.returncode, 0, mixed.stderr)
            self.assertIsNone(json.loads(mixed.stdout)["profile"])

    def test_mixed_source_profiles_and_explicit_override(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.jsonl"
            second = root / "second.log"
            first.write_text('{"timestamp":"2026-11-26T14:01:00Z","id":"r1"}\n', encoding="utf-8")
            second.write_text("ts=1795701660123 job=r1 done\n", encoding="utf-8")
            iso = root / "iso.json"
            epoch = root / "epoch.json"
            iso.write_text('{"format":"jsonl","time":"timestamp","id":"id"}', encoding="utf-8")
            epoch.write_text(
                '{"format":"logfmt","time":"ts","id":"job","time_format":"epoch_ms"}',
                encoding="utf-8",
            )
            result = self.run_cli("timeline", "--source", str(second), str(epoch),
                                  "--source", str(first), str(iso), "--value", "r1", "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                [event["timestamp"] for event in json.loads(result.stdout)["events"]],
                ["2026-11-26T14:01:00.000000+00:00", "2026-11-26T14:01:00.123000+00:00"],
            )
            result = self.run_cli(
                "timeline", "-", "--profile", str(epoch), "--time-format", "epoch_s", "--value", "r1",
                "--json", data="ts=1795701660 job=r1 done\n",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["events"][0]["timestamp"],
                             "2026-11-26T14:01:00.000000+00:00")


class ReportTrustTests(unittest.TestCase):
    def run_cli(self, *args, data=None):
        return subprocess.run(
            [sys.executable, "log_summary.py", *args],
            cwd=Path(__file__).resolve().parent, input=data,
            text=True, capture_output=True,
        )

    def test_partial_profile_is_saved_with_a_clear_warning(self):
        with TemporaryDirectory() as directory:
            profile = Path(directory) / "partial.json"
            data = '{"code":"E1","severity":"warn","request_id":"r1"}\n'
            result = self.run_cli("inspect", "-", "--json", "--fail-on", "empty",
                                  "--profile-out", str(profile), data=data)
            self.assertEqual(result.returncode, 0, result.stderr)
            saved = json.loads(profile.read_text(encoding="utf-8"))
            self.assertNotIn("time", saved)
            self.assertEqual(saved, json.loads(result.stdout)["profile"])
            self.assertIn("Profile has no timestamp mapping. Timed commands need --time", result.stderr)
            summary = self.run_cli("-", "--profile", str(profile), "--json", data=data)
            self.assertEqual(summary.returncode, 0, summary.stderr)
            self.assertEqual(json.loads(summary.stdout)["counts"]["WARNING"], 1)

    def test_epoch_warnings_explain_empty_windows_without_changing_output(self):
        start, boundary, end = "2026-11-26T14:00Z", "2026-11-26T14:01Z", "2026-11-26T14:02Z"
        cases = [
            ("timeline", ("--key", "id", "--value", "r1", "--since", boundary)),
            ("correlate", ("--key", "id", "--since", boundary)),
            ("bucket", ("--interval", "1m", "--since", boundary)),
            ("compare", ("--signature", "code", "--baseline", start, boundary,
                         "--incident", boundary, end)),
        ]
        for command, options in cases:
            with self.subTest(command=command):
                result = self.run_cli(
                    command, "-", "--format", "jsonl", "--time", "ts",
                    "--time-format", "epoch_ms", "--json", *options,
                    data='{"ts":1795701660,"id":"r1","code":"E1"}\n',
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["command"], command)
                self.assertIn("field 'ts': sampled values look like epoch_s; using epoch_ms", result.stderr)

        for mode, timestamp, expected in [
            ("epoch_s", 1795701660000, "look like epoch_ms"),
            ("epoch_s", 1795701660, None),
            ("epoch_s", 86_400, None),
        ]:
            with self.subTest(mode=mode, timestamp=timestamp):
                result = self.run_cli(
                    "correlate", "-", "--format", "jsonl", "--time", "ts",
                    "--time-format", mode, "--key", "id", "--json",
                    data=json.dumps({"ts": timestamp, "id": "r1"}) + "\n",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                if expected:
                    self.assertIn(expected, result.stderr)
                    self.assertEqual(json.loads(result.stdout)["coverage"]["skipped"], 1)
                else:
                    self.assertNotIn("epoch unit mismatch", result.stderr)

    def test_timeline_coverage_reconciles_filtered_and_unrelated_events(self):
        args = ("timeline", "app.jsonl", "--format", "jsonl", "--key", "request_id",
                "--time", "timestamp", "--value", "r-100", "--since", "2026-11-26T14:00:01Z")
        result = self.run_cli(*args, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        coverage = json.loads(result.stdout)["coverage"]
        expected = {"scanned": 20, "matched": 2, "outside_window": 4, "other_ids": 14, "skipped": 0, "physical_lines": 20}
        self.assertEqual(coverage, {**expected, "sources": [{"source": "app.jsonl", **expected}]})
        text = self.run_cli(*args)
        self.assertIn("Outside window: 4  Other IDs: 14", text.stdout)


class MultilineTests(unittest.TestCase):
    def run_cli(self, *args, data=None):
        return subprocess.run(
            [sys.executable, "log_summary.py", *args],
            cwd=Path(__file__).resolve().parent, input=data,
            text=True, capture_output=True, check=True,
        )

    def test_search_matches_continuation_and_counts_context_in_records(self):
        args = ("search", "stacktrace-example.txt", "--multiline",
                "--contains", "TimeoutError", "--context", "1")
        result = self.run_cli(*args, "--json")
        report = json.loads(result.stdout)
        self.assertEqual(report["coverage"], {
            "scanned": 3, "matched": 1, "displayed": 2, "physical_lines": 6,
        })
        self.assertEqual(
            [(row["line"], row["end_line"]) for row in report["lines"]], [(1, 3), (4, 4)],
        )
        self.assertIn("\tTimeoutError: connection timed out", report["lines"][0]["text"])
        self.assertTrue(self.run_cli(*args).stdout.startswith("1-3: "))
        legacy = json.loads(self.run_cli("search", "stacktrace-example.txt",
                                       "--contains", "TimeoutError", "--json").stdout)
        self.assertEqual(legacy["lines"], [{"line": 3, "text": "\tTimeoutError: connection timed out"}])

    def test_summary_counts_exception_once_and_group_reads_headers(self):
        summary = self.run_cli(
            "stacktrace-example.txt", "--multiline", "--contains", "TimeoutError", "--show-lines",
        )
        self.assertTrue(summary.stdout.startswith("1-3: "))
        self.assertTrue(summary.stdout.endswith("ERROR: 1\n"))
        self.assertIn("Classified: 1  Unclassified: 0", summary.stderr)
        group = self.run_cli("group", "stacktrace-example.txt", "--format", "logfmt",
                            "--by", "code", "--multiline", "--json")
        report = json.loads(group.stdout)
        self.assertEqual(report["coverage"], {
            "scanned": 3, "grouped": 3, "skipped": 0, "physical_lines": 6,
            "sources": [{"source": "stacktrace-example.txt", "field": "code",
                         "scanned": 3, "grouped": 3, "skipped": 0, "physical_lines": 6}],
        })
        self.assertEqual(report["results"], [{"value": "E1042", "count": 2},
                                             {"value": "START", "count": 1}])

    def test_timeline_sorts_headers_and_keeps_stack_trace_attached(self):
        args = ("timeline", "stacktrace-example.txt", "--format", "logfmt",
                "--time", "timestamp", "--key", "request_id", "--value", "r1", "--multiline")
        result = self.run_cli(*args, "--json")
        report = json.loads(result.stdout)
        self.assertEqual([(event["line"], event["end_line"]) for event in report["events"]], [(4, 4), (1, 3)])
        self.assertIn("Client.read", report["events"][1]["text"])
        self.assertEqual(report["coverage"]["scanned"], 3)
        self.assertEqual(report["coverage"]["physical_lines"], 6)
        self.assertEqual(report["coverage"]["other_ids"], 1)
        self.assertIn("stacktrace-example.txt:1-3", self.run_cli(*args).stdout)

    def test_structured_jsonl_stays_line_based_with_multiline_flag(self):
        result = self.run_cli(
            "group", "-", "--format", "jsonl", "--by", "code", "--multiline", "--json",
            data='{"code":"E1"}\n  {"code":"E2"}\n',
        )
        report = json.loads(result.stdout)
        self.assertEqual(report["coverage"]["grouped"], 2)
        self.assertEqual(report["results"], [{"value": "E1", "count": 1},
                                             {"value": "E2", "count": 1}])


class MultilineParityTests(unittest.TestCase):
    def run_cli(self, *args, data=None, check=True):
        return subprocess.run(
            [sys.executable, "log_summary.py", *args],
            cwd=Path(__file__).resolve().parent, input=data,
            text=True, encoding="utf-8", capture_output=True, check=check,
        )

    def test_timed_commands_count_headers_and_preserve_original_evidence(self):
        fixture = "stacktrace-example.txt"
        common = (fixture, "--format", "logfmt", "--time", "timestamp", "--multiline")
        bucket = json.loads(self.run_cli("bucket", *common, "--interval", "1m", "--json").stdout)
        self.assertEqual(bucket["coverage"], {
            "scanned": 3, "counted": 3, "invalid_timestamp": 0,
            "outside_window": 0, "filtered": 0, "physical_lines": 6,
            "sources": [{"source": fixture, "signature": None, "scanned": 3, "counted": 3,
                         "invalid_timestamp": 0, "outside_window": 0, "filtered": 0, "physical_lines": 6}],
        })
        self.assertIn("Physical lines: 6", self.run_cli("bucket", *common, "--interval", "1m").stdout)
        correlate = json.loads(self.run_cli("correlate", *common, "--key", "request_id", "--json").stdout)
        self.assertEqual(correlate["coverage"], {
            "scanned": 3, "used": 3, "skipped": 0, "outside_window": 0, "physical_lines": 6,
            "sources": [{"source": fixture, "scanned": 3, "used": 3, "skipped": 0,
                         "outside_window": 0, "physical_lines": 6}],
        })
        compare = json.loads(self.run_cli(
            "compare", *common, "--signature", "code", "--id", "request_id",
            "--baseline", "2026-11-26T14:01:00Z", "2026-11-26T14:01:02Z",
            "--incident", "2026-11-26T14:01:02Z", "2026-11-26T14:01:05Z", "--json",
        ).stdout)
        self.assertEqual(compare["coverage"]["invalid_timestamp"], 0)
        self.assertEqual(compare["coverage"]["physical_lines"], 6)
        self.assertEqual(compare["coverage"]["sources"][0]["scanned"], 3)
        finding = next(row for row in compare["results"] if row["signature"] == "E1042")
        self.assertEqual(finding["incident"]["events"], 2)
        self.assertEqual(finding["example"], {"source": fixture, "line": 1, "id": "r1"})

    def test_inspect_samples_records_and_keeps_indented_json_objects_separate(self):
        result = self.run_cli("inspect", "stacktrace-example.txt", "--multiline", "--lines", "2", "--json")
        report = json.loads(result.stdout)
        self.assertEqual(report["coverage"]["sampled"], 2)
        self.assertEqual(report["coverage"]["physical_lines"], 4)
        self.assertEqual(report["profile"]["time"], "timestamp")
        json_data = '{"ts":"2026-11-26T14:01Z"}\n  {"ts":"2026-11-26T14:02Z"}\n'
        report = json.loads(self.run_cli("inspect", "-", "--multiline", "--json", data=json_data).stdout)
        self.assertEqual(report["coverage"]["json_objects"], 2)
        self.assertEqual(report["coverage"]["sampled"], 2)

    def test_oversized_multiline_records_fail_with_source_and_start_line(self):
        data = "INFO fine\nERROR fail\n" + "    frame\n" * 10_000
        args = ("search", "-", "--multiline", "--contains", "ERROR", "--json")
        result = self.run_cli(*args, data=data, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("'-': multiline record starting at line 2 exceeds 10,000 lines", result.stderr)

        with TemporaryDirectory() as directory:
            fixture = Path(directory) / "oversized.log"
            fixture.write_text(
                "2026-11-26T14:01Z ERROR id=r1\n " + "\u00e9" * (1_048_576 // 2),
                encoding="utf-8",
            )
            result = self.run_cli(
                "timeline", str(fixture), "--format", "logfmt", "--time", "timestamp",
                "--key", "id", "--value", "r1", "--multiline", "--json", check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertIn("oversized.log", result.stderr)
            self.assertIn("record starting at line 1 exceeds 1 MiB", result.stderr)


class MultiSourceTotalsTests(unittest.TestCase):
    sources = (
        "--source", "app.jsonl", "app-jsonl-profile.json",
        "--source", "stacktrace-example.txt", "stacktrace-profile.json",
    )

    def run_cli(self, *args, check=True):
        return subprocess.run(
            [sys.executable, "log_summary.py", *args],
            cwd=Path(__file__).resolve().parent, capture_output=True, text=True, check=check,
        )

    def test_summary_combines_severity_and_preserves_source_lines(self):
        result = self.run_cli(*self.sources, "--multiline", "--json")
        report = json.loads(result.stdout)
        self.assertEqual(report["counts"], {"INFO": 13, "WARNING": 3, "ERROR": 7})
        coverage = report["coverage"]
        self.assertEqual((coverage["scanned"], coverage["classified"], coverage["physical_lines"]),
                         (23, 23, 26))
        self.assertEqual([source["classified"] for source in coverage["sources"]], [20, 3])
        text = self.run_cli(*self.sources, "--multiline", "--regex", "TimeoutError", "--show-lines")
        self.assertIn("stacktrace-example.txt:1-3", text.stdout)
        self.assertIn("ERROR: 1", text.stdout)
        self.assertIn("app.jsonl: scanned 20, classified 0, unclassified 0, filtered 20", text.stdout)

    def test_summary_reports_selected_level_per_source(self):
        args = (*self.sources, "--multiline", "--level", "ERROR")
        report = json.loads(self.run_cli(*args, "--json").stdout)
        self.assertEqual(report["counts"], {"ERROR": 7})
        self.assertEqual(report["coverage"]["classified"], 23)
        self.assertEqual([source["counts"] for source in report["coverage"]["sources"]],
                         [{"ERROR": 5}, {"ERROR": 2}])
        output = self.run_cli(*args).stdout
        self.assertRegex(output, r"app\.jsonl: [^\n]+\n +ERROR: 5")
        self.assertRegex(output, r"stacktrace-example\.txt: [^\n]+\n +ERROR: 2")

    def test_group_merges_values_and_keeps_full_coverage_with_top(self):
        result = self.run_cli("group", *self.sources, "--multiline", "--top", "1", "--json")
        report = json.loads(result.stdout)
        self.assertEqual(report["results"], [{"value": "E1042", "count": 6}])
        self.assertEqual(report["total_rows"], 4)
        coverage = report["coverage"]
        self.assertEqual((coverage["scanned"], coverage["grouped"], coverage["skipped"]), (23, 11, 12))
        self.assertEqual([source["grouped"] for source in coverage["sources"]], [8, 3])

    def test_bucket_aligns_sources_to_one_window_and_keeps_contributions(self):
        result = self.run_cli(
            "bucket", *self.sources, "--interval", "1m", "--value", "E1042", "--multiline",
            "--since", "2026-11-26T14:00Z", "--before", "2026-11-26T14:02Z", "--json",
        )
        report = json.loads(result.stdout)
        self.assertEqual([bucket["count"] for bucket in report["buckets"]], [4, 2])
        self.assertEqual([bucket["events_per_minute"] for bucket in report["buckets"]], [4, 2])
        coverage = report["coverage"]
        self.assertEqual((coverage["scanned"], coverage["counted"], coverage["filtered"]), (23, 6, 17))
        self.assertEqual([source["counted"] for source in coverage["sources"]], [4, 2])

    def test_per_source_mappings_can_select_different_fields(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "first.jsonl"
            secondary = root / "second.log"
            primary.write_text('{"ts":"2026-11-26T14:00:01Z","error":"E1"}\n', encoding="utf-8")
            secondary.write_text("2026-11-26T14:00:02Z ERROR code=E1\n", encoding="utf-8")
            first = root / "first.json"
            second = root / "second.json"
            first.write_text('{"format":"jsonl","time":"ts","by":"error","signature":"error"}',
                             encoding="utf-8")
            second.write_text('{"format":"logfmt","time":"timestamp","by":"code","signature":"code"}',
                              encoding="utf-8")
            specs = ("--source", str(primary), str(first), "--source", str(secondary), str(second))
            result = self.run_cli("group", *specs, "--json")
            report = json.loads(result.stdout)
            self.assertEqual(report["results"], [{"value": "E1", "count": 2}])
            self.assertIsNone(report["field"])
            self.assertEqual([source["field"] for source in report["coverage"]["sources"]], ["error", "code"])
            result = self.run_cli("bucket", *specs, "--interval", "1m", "--value", "E1", "--json")
            report = json.loads(result.stdout)
            self.assertEqual(report["coverage"]["counted"], 2)
            self.assertIsNone(report["signature"])
            self.assertEqual([source["signature"] for source in report["coverage"]["sources"]], ["error", "code"])


if __name__ == "__main__":
    unittest.main()
