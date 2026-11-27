import gzip
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from fixprinter import START, LatencyStats, Timing, format_duration, parse_message, same_capture

CLI = Path(__file__).with_name("fixprinter.py")


def wire(version, body):
    framed = b"8=FIX." + version + b"\x019=" + str(len(body)).encode() + b"\x01" + body
    return framed + b"10=%03d\x01" % (sum(framed) % 256)


class PrinterTests(unittest.TestCase):
    def run_cli(self, *args, data=None):
        return subprocess.run(
            [sys.executable, str(CLI), *map(str, args)], input=data,
            capture_output=True, check=False,
        )

    def test_default_ids_are_exact_and_ignore_non_id_tags(self):
        data = (
            b"14:30:00 ==> 8=FIX.4.2|9=1|35=D|11=ABC|40=1|40=2|10=000|\n"
            b"14:30:01 <== 8=FIX.4.4|9=1|35=8|37=ABC|150=F|10=000|\n"
            b"14:30:02 ==> 8=FIX.4.4|9=1|35=G|41=ABC|10=000|\n"
            b"14:30:03 ==> 8=FIX.4.4|9=1|35=D|11=ABC2|58=ABC|10=000|\n"
        )
        result = self.run_cli("ABC", "-", data=data)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count(b"=== stdin:"), 3)
        self.assertEqual(result.stdout.count(b"OrdType"), 2)
        self.assertIn(b"14:30:00 ==>", result.stdout)
        self.assertIn(b"Trade [F]", result.stdout)
        self.assertNotIn(b"14:30:03", result.stdout)

    def test_gzip_custom_tag_and_dictionary(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, "orders.log.gz")
            log.write_bytes(gzip.compress(b"8=FIX.4.4|9=1|35=D|54=1|9001=S|10=000|\n"))
            xml = Path(tmp, "venue.xml")
            xml.write_text(
                '<fix major="4" minor="4"><fields>'
                '<field number="9001" name="VenueID"><value enum="S" description="RIC_CODE"/>'
                '</field></fields></fix>', encoding="utf-8",
            )
            result = self.run_cli("S", log, "--tag", 9001, "--dictionary", xml)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b"VenueID", result.stdout)
            self.assertIn(b"RIC Code [S]", result.stdout)
            self.assertIn(b"Buy [1]", result.stdout)
            self.assertEqual(self.run_cli("S", log).returncode, 1)

    def test_corrupt_gzip_reports_input_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, "corrupt.log.gz")
            header = gzip.compress(b"payload")[:10]
            log.write_bytes(header + b"\x07" + bytes(8))
            result = self.run_cli("ID", log)
            self.assertEqual(result.returncode, 2)
            self.assertIn(b"fixprinter:", result.stderr)
            self.assertNotIn(b"Traceback", result.stderr)

    def test_bundled_dictionary_resolves_version_specific_fields(self):
        data = (
            b"8=FIX.4.2|9=1|35=8|11=A|32=40|777777=raw|10=000|\n"
            b"8=FIX.4.4|9=1|35=8|11=A|32=40|851=1|10=000|\n"
        )
        result = self.run_cli("A", "-", data=data)
        self.assertEqual(result.returncode, 0, result.stderr)
        for label in (b"LastShares", b"LastQty", b"LastLiquidityInd", b"Unknown"):
            self.assertIn(label, result.stdout)
        self.assertIn(b"Added Liquidity [1]", result.stdout)

    def test_sample_sessions_contain_required_dictionary_fields(self):
        for version, sample in (("42", "orders.fix"), ("44", "session-44.fix")):
            with self.subTest(version=version):
                root = ET.parse(CLI.parent / "dictionaries" / f"FIX{version}.xml")
                tags = {field.attrib["name"]: int(field.attrib["number"])
                        for field in root.findall("fields/field")}
                components = {node.attrib["name"]: node
                              for node in root.findall("components/component")}
                messages = {node.attrib["msgtype"].encode(): node
                            for node in root.findall("messages/message")}

                def required(section):
                    for entry in section:
                        if entry.attrib["required"] != "Y":
                            continue
                        name = entry.attrib["name"]
                        if entry.tag == "component":
                            yield from required(components[name])
                        else:
                            yield tags[name]

                kinds = []
                for row in (CLI.parent / "samples" / sample).read_bytes().splitlines():
                    wire_row = row.replace(b"|", b"\x01")
                    start = START.search(wire_row)
                    self.assertIsNotNone(start)
                    message = parse_message(wire_row, start)
                    self.assertFalse(message.issues, message.issues)
                    fields = dict(message.fields)
                    kind = fields[35]
                    kinds.append(kind)
                    for section in (root.find("header"), messages[kind], root.find("trailer")):
                        self.assertFalse(set(required(section)) - fields.keys())
                expected = b"AAD888001055" if version == "42" else b"AAD88F8001055"
                self.assertEqual(b"".join(kinds), expected)

    def test_fix44_sample_cancel_and_repeating_parties(self):
        sample = CLI.parent / "samples" / "session-44.fix"
        cancel = self.run_cli("CANCEL-44", sample, "--tag", 11)
        self.assertEqual(cancel.returncode, 0, cancel.stderr)
        self.assertEqual(cancel.stdout.count(b"=== "), 2)
        self.assertEqual(cancel.stdout.count(b"OrigClOrdID"), 2)
        self.assertIn(b"Canceled [4]", cancel.stdout)
        original = self.run_cli("Outbound_ClOrderId", sample)
        self.assertEqual(original.returncode, 0, original.stderr)
        self.assertEqual(original.stdout.count(b"=== "), 5)
        self.assertEqual(original.stdout.count(b"PartyRole"), 2)

    def test_latency_units_and_boundaries(self):
        for micros, expected in (
            (0, "0\u03bcs"), (140, "140\u03bcs"), (999, "999\u03bcs"),
            (1_000, "1ms"), (1_001, "1.001ms"), (999_000, "999ms"),
            (999_999, "999.999ms"), (1_000_000, "1s"),
            (1_000_001, "1.000001s"), (65_000_000, "65s"),
        ):
            with self.subTest(micros=micros):
                self.assertEqual(format_duration(micros, color=False), expected)
                colored = f"\x1b[31m{expected}\x1b[0m" if micros >= 1_000_000 else expected
                self.assertEqual(format_duration(micros, color=True), colored)

    def test_latency_cli_uses_utf8_and_highlights_seconds_when_enabled(self):
        request = b"2026-01-12 14:30:00.000 ==> 8=FIX.4.4|9=1|35=D|49=A|56=B|11=ID|10=000|\n"
        report = b" <== 8=FIX.4.4|9=1|35=8|49=B|56=A|11=ID|150=0|10=000|\n"
        fast = self.run_cli("ID", "-", data=request + b"2026-01-12 14:30:00.000140" + report)
        self.assertEqual(fast.returncode, 0, fast.stderr)
        self.assertIn("Order ack: 140\u03bcs".encode(), fast.stdout)
        slow_data = request + b"2026-01-12 14:30:01.250" + report
        for color, expected in (("always", b"\x1b[31m1.25s\x1b[0m"), ("never", b"1.25s")):
            slow = self.run_cli("ID", "-", "--color", color, data=slow_data)
            self.assertEqual(slow.returncode, 0, slow.stderr)
            self.assertIn(b"Order ack: " + expected, slow.stdout)

    def test_latency_uses_sample_requests_outside_the_tag_filter(self):
        sample = CLI.parent / "samples" / "session-44.fix"
        result = self.run_cli("BROKER-44", sample, "--tag", 37)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"Order ack: 15ms", result.stdout)
        self.assertIn(b"Cancel ack: 12ms", result.stdout)
        self.assertEqual(result.stdout.count(b"ms"), 2)
        fix42 = self.run_cli("CLIENT-42", CLI.parent / "samples" / "orders.fix")
        self.assertIn(b"Order ack: 15ms", fix42.stdout)

    def test_route_joins_inbound_tag_11_to_outbound_tag_37(self):
        samples = CLI.parent / "samples"
        ingress, exchange = samples / "client-ingress.fix", samples / "session-44.fix"
        result = self.run_cli("Inbound_ClOrderId", ingress, "--routing-latency", exchange, "--outbound-link-tag", 37)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"ClOrdID=Inbound_ClOrderId", result.stdout)
        self.assertIn(b"ClOrdID=Outbound_ClOrderId | tag 37=Inbound_ClOrderId", result.stdout)
        self.assertIn(b"Internal routing: 4ms", result.stdout)
        self.assertIn(b"Exchange order ack round trip: 15ms", result.stdout)
        single = self.run_cli("Outbound_ClOrderId", exchange, "--exchange-facing")
        self.assertEqual(single.returncode, 0, single.stderr)
        self.assertIn(b"Exchange order ack round trip: 15ms", single.stdout)

    def test_route_defaults_to_tag_11_and_accepts_explicit_outbound_id(self):
        samples = CLI.parent / "samples"
        ingress = samples / "client-ingress.fix"
        exchange = samples / "session-44.fix"
        default = self.run_cli("Inbound_ClOrderId", ingress, "--routing-latency", exchange)
        self.assertEqual(default.returncode, 1, default.stderr)
        self.assertIn(b"new order not found by tag 11", default.stdout)
        explicit = self.run_cli(
            "Inbound_ClOrderId", ingress, "--routing-latency", exchange,
            "--outbound-clordid", "Outbound_ClOrderId",
        )
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertIn(b"Internal routing: 4ms", explicit.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            shared_id = Path(tmp, "shared.fix")
            shared_id.write_bytes(ingress.read_bytes().replace(
                b"11=Inbound_ClOrderId|", b"11=Outbound_ClOrderId|"
            ))
            same = self.run_cli("Outbound_ClOrderId", shared_id, "--routing-latency", exchange)
            self.assertEqual(same.returncode, 0, same.stderr)
            self.assertIn(b"Internal routing: 4ms", same.stdout)
            self.assertIn(b"Exchange order ack round trip: 15ms", same.stdout)

    def test_route_refuses_ambiguous_and_unlinked_orders(self):
        samples = CLI.parent / "samples"
        ingress, exchange = samples / "client-ingress.fix", samples / "session-44.fix"
        with tempfile.TemporaryDirectory() as tmp:
            duplicate = Path(tmp, "duplicate.fix")
            duplicate.write_bytes(ingress.read_bytes() * 2)
            result = self.run_cli("Inbound_ClOrderId", duplicate, "--routing-latency", exchange, "--outbound-link-tag", 37)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(b"multiple new orders match tag 11", result.stdout)
            self.assertIn(b"Internal routing: latency unavailable", result.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            unlinked = Path(tmp, "unlinked.fix")
            unlinked.write_bytes(exchange.read_bytes().replace(b"37=Inbound_ClOrderId|", b""))
            result = self.run_cli("Inbound_ClOrderId", ingress, "--routing-latency", unlinked, "--outbound-link-tag", 37)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(b"new order not found by tag 37", result.stdout)

    def test_route_ack_is_unavailable_when_outbound_clordid_is_reused(self):
        samples = CLI.parent / "samples"
        ingress = samples / "client-ingress.fix"
        outbound = (samples / "session-44.fix").read_bytes().splitlines(keepends=True)
        competing = outbound[2].replace(b"37=Inbound_ClOrderId|", b"37=OTHER|")
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp, "outbound.fix")
            capture.write_bytes(b"".join(outbound[:2] + [competing] + outbound[2:]))
            result = self.run_cli(
                "Inbound_ClOrderId", ingress, "--routing-latency", capture,
                "--outbound-link-tag", 37,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"Internal routing: 4ms", result.stdout)
        self.assertIn(b"multiple requests share this ClOrdID", result.stdout)
        self.assertNotIn(b"Exchange order ack round trip: 15ms", result.stdout)

    def test_latency_pairs_rejects_and_ignores_retransmits(self):
        data = (
            b"01/12/2026 14:30:00.000 ==> 8=FIX.4.4|9=1|35=D|49=A|56=B|11=ID|10=000|\n"
            b"01/12/2026 14:30:00.002 ==> 8=FIX.4.4|9=1|35=D|49=A|56=B|43=Y|11=ID|10=000|\n"
            b"01/12/2026 14:30:00.005 <== 8=FIX.4.4|9=1|35=8|49=B|56=A|11=ID|150=8|10=000|\n"
            b"2026-01-12T14:30:00.010Z ==> 8=FIX.4.4|9=1|35=F|49=A|56=B|11=C|41=ID|10=000|\n"
            b"2026-01-12T09:30:00.022500-05:00 <== 8=FIX.4.4|9=1|35=9|49=B|56=A|"
            b"11=C|41=ID|434=1|10=000|\n"
        )
        result = self.run_cli("ID", "-", data=data)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"Order reject: 5ms", result.stdout)
        self.assertIn(b"Cancel reject: 12.5ms", result.stdout)

    def test_latency_reports_missing_ambiguous_and_backwards_pairs(self):
        for prefixes, reason in (
            (("14:30:00.000",), b"unsupported capture timestamp"),
            (("2026-01-12 14:30:00.020",), b"response timestamp precedes request"),
            ((), b"request not found in this session"),
            (("2026-01-12 14:30:00.000",) * 2, b"multiple requests share this ClOrdID"),
        ):
            with self.subTest(reason=reason):
                requests = b"".join(
                    prefix.encode() + b" ==> 8=FIX.4.4|9=1|35=D|49=A|56=B|11=ID|10=000|\n"
                    for prefix in prefixes
                )
                response = (
                    b"2026-01-12 14:30:00.010 <== "
                    b"8=FIX.4.4|9=1|35=8|49=B|56=A|11=ID|150=0|10=000|\n"
                )
                result = self.run_cli("ID", "-", data=requests + response)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(b"latency unavailable", result.stdout)
                self.assertIn(reason, result.stdout)

    def test_session_and_time_filters_include_both_directions(self):
        sample = CLI.parent / "samples" / "orders.fix"
        result = self.run_cli(
            "CLIENT-42", sample, "--session", "NORTHBRK42", "ALPHAOMS42",
            "--from", "2026-01-12T14:30:01", "--until", "2026-01-12T14:30:03",
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        messages = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([message["line"] for message in messages], [3, 4, 5])
        self.assertTrue(all(message["schema_version"] == 1 for message in messages))
        timing = messages[1]["timing"]
        self.assertEqual(timing["microseconds"], 15_000)
        self.assertEqual((timing["start"]["line"], timing["end"]["line"]), (3, 4))
        mismatch = self.run_cli("CLIENT-42", sample, "--session", "OTHER", "ALPHAOMS42")
        self.assertEqual(mismatch.returncode, 1, mismatch.stderr)
        invalid = self.run_cli(
            "CLIENT-42", sample, "--from", "2026-01-12T14:30:03Z",
            "--until", "2026-01-12T14:30:04",
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertIn(b"same timezone convention", invalid.stderr)

    def test_routing_json_uses_separate_session_filters(self):
        samples = CLI.parent / "samples"
        args = (
            "Inbound_ClOrderId", samples / "client-ingress.fix",
            "--routing-latency", samples / "session-44.fix", "--outbound-link-tag", 37,
            "--session", "ALPHACLIENT", "FIRMINGRESS", "--json",
        )
        result = self.run_cli(*args, "--outbound-session", "ALPHAOMS44", "NORTHBRK44")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["type"], "routing")
        self.assertEqual(report["outbound"]["match_tag"], 37)
        self.assertEqual([item["microseconds"] for item in report["timings"]], [4_000, 15_000])
        self.assertEqual(report["timings"][0]["start"]["line"], 1)
        self.assertEqual(report["timings"][0]["end"]["line"], 3)
        missing = self.run_cli(*args, "--outbound-session", "MISSING", "NORTHBRK44")
        self.assertEqual(missing.returncode, 1, missing.stderr)
        self.assertIn("not found", json.loads(missing.stdout)["outbound"]["reason"])

    def test_lifecycle_reports_fill_times_and_follows_cancel_id(self):
        for identifier, filename, lines, full in (
            ("CLIENT-42", "orders.fix", [3, 4, 5, 6], 2_000_000),
            ("Outbound_ClOrderId", "session-44.fix", [3, 4, 5, 6, 7], None),
        ):
            with self.subTest(filename=filename):
                result = self.run_cli(identifier, CLI.parent / "samples" / filename, "--lifecycle", "--json")
                self.assertEqual(result.returncode, 0, result.stderr)
                *messages, summary = [json.loads(line) for line in result.stdout.splitlines()]
                self.assertEqual([message["line"] for message in messages], lines)
                self.assertEqual(summary["type"], "lifecycle_summary")
                first, final = summary["timings"]
                self.assertEqual(first["microseconds"], 1_000_000)
                self.assertEqual((first["start"]["line"], first["end"]["line"]), (3, 5))
                self.assertEqual(final["microseconds"], full)
                if full is None:
                    self.assertEqual(final["reason"], "no full fill seen")
                    self.assertEqual(messages[-1]["timing"]["microseconds"], 12_000)

    def test_lifecycle_follows_replace_and_excludes_another_session(self):
        def row(millis, body):
            return f"2026-01-12 14:30:00.{millis:03d} 8=FIX.4.4|9=1|{body}|10=000|\n".encode()

        data = b"".join((
            row(0, "35=D|49=OMS|56=VENUE|11=ORIGINAL"),
            row(1, "35=8|49=VENUE|56=OMS|11=ORIGINAL|37=EXCHANGE|150=0"),
            row(2, "35=G|49=OMS|56=VENUE|11=REPLACED|41=ORIGINAL"),
            row(3, "35=8|49=VENUE|56=OTHER|11=REPLACED|37=EXCHANGE|150=F|32=1|39=2"),
            row(4, "35=8|49=VENUE|56=OMS|11=REPLACED|41=ORIGINAL|37=EXCHANGE|150=5"),
            row(5, "35=8|49=VENUE|56=OMS|37=EXCHANGE|150=F|32=1|39=2"),
        ))
        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp, "capture.log.gz")
            sample.write_bytes(gzip.compress(data))
            result = self.run_cli("ORIGINAL", sample, "--lifecycle", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        *messages, summary = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([message["line"] for message in messages], [1, 2, 3, 5, 6])
        self.assertEqual(messages[-2]["timing"]["label"], "Replace ack")
        self.assertEqual(summary["timings"][1]["microseconds"], 5_000)

    def test_json_preserves_repeated_tags_and_binary_values_without_color(self):
        data = b"8=FIX.4.4|9=1|35=D|11=ID|40=1|40=2|95=2|96=\xff||10=000|\n"
        result = self.run_cli("ID", "-", "--json", "--color", "always", data=data)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(b"\x1b", result.stdout)
        fields = json.loads(result.stdout)["fields"]
        self.assertEqual([field["value"] for field in fields if field["tag"] == 40], ["1", "2"])
        self.assertEqual(next(field for field in fields if field["tag"] == 96)["raw_base64"], "/3w=")

    def test_undated_session_boundary_stops_correlation_with_a_time_filter(self):
        data = (
            b"2026-01-12 14:30:01 8=FIX.4.4|9=1|35=D|49=A|56=B|11=ID|10=000|\n"
            b"8=FIX.4.4|9=1|35=A|49=A|56=B|10=000|\n"
            b"2026-01-12 14:30:02 8=FIX.4.4|9=1|35=8|49=B|56=A|11=ID|150=0|10=000|\n"
            b"2026-01-12 14:30:03 8=FIX.4.4|9=1|35=8|49=B|56=A|11=ID|150=F|32=1|39=2|10=000|\n"
        )
        options = ("--from", "2026-01-12T14:30:00", "--json")
        search = self.run_cli("ID", "-", *options, data=data)
        self.assertEqual(search.returncode, 0, search.stderr)
        messages = [json.loads(line) for line in search.stdout.splitlines()]
        self.assertEqual([message["line"] for message in messages], [1, 3, 4])
        self.assertEqual(messages[1]["timing"]["reason"], "request not found in this session")
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp, "capture.fix")
            capture.write_bytes(data)
            lifecycle = self.run_cli("ID", capture, "--lifecycle", *options)
        self.assertEqual(lifecycle.returncode, 0, lifecycle.stderr)
        message, summary = [json.loads(line) for line in lifecycle.stdout.splitlines()]
        self.assertEqual(message["line"], 1)
        self.assertEqual(summary["timings"][0]["reason"], "no fill seen")

    def test_routing_keeps_exchange_timing_when_inbound_order_is_missing(self):
        samples = CLI.parent / "samples"
        result = self.run_cli(
            "missing", samples / "client-ingress.fix", "--routing-latency", samples / "session-44.fix",
            "--outbound-clordid", "Outbound_ClOrderId", "--json",
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        routing, exchange = json.loads(result.stdout)["timings"]
        self.assertIsNone(routing["microseconds"])
        self.assertEqual(exchange["microseconds"], 15_000)

    def test_rotation_keeps_search_lifecycle_and_outbound_timing(self):
        samples = CLI.parent / "samples"
        rows = (samples / "session-44.fix").read_bytes().splitlines(keepends=True)
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp, "exchange.log.1.gz")
            first.write_bytes(gzip.compress(b"".join(rows[:3])))
            last = Path(tmp, "exchange.log")
            last.write_bytes(b"".join(rows[3:]))
            for mode in ((), ("--lifecycle",)):
                result = self.run_cli(
                    "Outbound_ClOrderId", first, "--next-log", last, "--json", *mode,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                messages = [json.loads(line) for line in result.stdout.splitlines()]
                ack = messages[1]["timing"]
                self.assertEqual(ack["microseconds"], 15_000)
                self.assertEqual(ack["start"]["source"], str(first))
                self.assertEqual(ack["end"]["source"], str(last))
                self.assertEqual(ack["end"]["line"], 1)
            route = self.run_cli(
                "Inbound_ClOrderId", samples / "client-ingress.fix",
                "--routing-latency", first, "--next-outbound-log", last,
                "--outbound-link-tag", 37, "--json",
            )
        self.assertEqual(route.returncode, 0, route.stderr)
        timing = json.loads(route.stdout)["timings"][1]
        self.assertEqual(timing["microseconds"], 15_000)
        self.assertEqual(timing["end"]["source"], str(last))

    def test_session_diagnostics_tracks_each_direction_and_gap_fill(self):
        rows = (
            b"35=0|49=A|56=B|34=4|",
            b"35=0|49=B|56=A|34=20|",
            b"35=0|49=A|56=B|34=6|",
            b"35=4|49=A|56=B|34=7|36=9|123=Y|43=Y|",
            b"35=0|49=A|56=B|34=9|",
            b"35=A|49=A|56=B|34=1|141=Y|",
            b"35=0|49=A|56=B|34=2|",
        )
        data = b"".join(b"8=FIX.4.4|9=1|" + row + b"10=000|\n" for row in rows)
        result = self.run_cli("-", "--session-diagnostics", "--json", data=data)
        self.assertEqual(result.returncode, 0, result.stderr)
        *events, summary = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([event["event"] for event in events],
                         ["sequence_gap", "replay", "gap_fill", "logon_reset"])
        self.assertEqual((events[0]["expected"], events[0]["observed"]), (5, 6))
        self.assertEqual(summary["messages"], 7)
        expected = {(row["sender"], row["target"]): row["next_sequence"] for row in summary["expected_next"]}
        self.assertEqual(expected, {("A", "B"): 3, ("B", "A"): 21})

    def test_latency_summary_reports_percentiles_unavailable_and_evidence(self):
        def row(millis, body):
            return f"2026-01-12 14:30:00.{millis:03d} 8=FIX.4.4|9=1|{body}|10=000|\n".encode()

        rows = []
        for index, elapsed in enumerate((1, 2, 3, 50), start=1):
            start = index * 100
            rows.append(row(start, f"35=D|49=A|56=B|11=ID{index}"))
            rows.append(row(start + elapsed, f"35=8|49=B|56=A|11=ID{index}|150=0"))
        rows.append(row(500, "35=8|49=B|56=A|11=MISSING|150=0"))
        rows.append(row(501, "35=8|49=B|56=A|11=ID1|150=F|32=1|39=2"))
        result = self.run_cli("-", "--latency-summary", "--json", data=b"".join(rows))
        self.assertEqual(result.returncode, 0, result.stderr)
        *metrics, summary = [json.loads(line) for line in result.stdout.splitlines()]
        indexed = {metric["label"]: metric for metric in metrics}
        ack = indexed["Order ack"]
        self.assertEqual(ack["session"], ["A", "B"])
        self.assertEqual(ack["measured"], 4)
        self.assertEqual(ack["percentiles_microseconds"], {"p50": 2000, "p95": 50000, "p99": 50000})
        self.assertEqual(ack["unavailable"], {"request not found in this session": 1})
        self.assertEqual(ack["slowest"][0]["end"]["line"], 8)
        self.assertEqual(indexed["Time to full fill"]["percentiles_microseconds"]["p50"], 401_000)
        self.assertEqual(summary["messages"], 10)

    def test_nanosecond_capture_duplicate_response_and_unanswered_request(self):
        data = (
            b"2026-01-12 14:30:00.000000900 8=FIX.4.4|9=1|35=D|49=A|56=B|11=ID|10=000|\n"
            b"2026-01-12 14:30:00.000140100 8=FIX.4.4|9=1|35=8|49=B|56=A|11=ID|150=0|10=000|\n"
            b"2026-01-12 14:30:00.000141999 8=FIX.4.4|9=1|35=8|49=B|56=A|11=ID|150=0|10=000|\n"
            b"2026-01-12 14:30:00.000142999 8=FIX.4.4|9=1|35=D|49=A|56=B|11=OTHER|10=000|\n"
        )
        result = self.run_cli("-", "--latency-summary", "--json", data=data)
        self.assertEqual(result.returncode, 0, result.stderr)
        *metrics, _ = [json.loads(line) for line in result.stdout.splitlines()]
        by_label = {metric["label"]: metric for metric in metrics}
        self.assertEqual(by_label["Order ack"]["percentiles_microseconds"]["p50"], 139)
        self.assertEqual(by_label["Order ack"]["unavailable"], {"duplicate response": 1})
        self.assertEqual(by_label["Order ack"]["slowest"][0]["start"]["nanosecond_remainder"], 900)
        filtered = self.run_cli("ID", "-", "--until", "2026-01-12T14:30:00.000140101", "--json", data=data)
        self.assertEqual(filtered.returncode, 0, filtered.stderr)
        self.assertEqual(len(filtered.stdout.splitlines()), 2)
        self.assertEqual(by_label["Order unanswered"]["unavailable"],
                         {"capture ended before a matching reply": 1})
        pending = self.run_cli("-", "--latency-summary", "--json", data=data.splitlines(keepends=True)[-1])
        self.assertEqual(pending.returncode, 0, pending.stderr)
        self.assertEqual(json.loads(pending.stdout.splitlines()[0])["label"], "Order unanswered")

    def test_legacy_fills_are_explicit_and_fill_like_reports_explain_exclusion(self):
        def row(index, body):
            return f"2026-01-12 14:30:0{index} 8=FIX.4.4|9=1|{body}|10=000|\n".encode()

        data = b"".join((
            row(0, "35=D|49=A|56=B|11=ID"),
            row(1, "35=8|49=B|56=A|11=ID|150=Z|39=1|32=10"),
            row(2, "35=8|49=B|56=A|11=ID|150=1|39=1|32=10"),
            row(3, "35=8|49=B|56=A|11=ID|150=2|39=2|32=10"),
        ))
        strict = self.run_cli("-", "--latency-summary", "--json", data=data)
        self.assertEqual(strict.returncode, 0, strict.stderr)
        metrics = {item.get("label"): item for item in map(json.loads, strict.stdout.splitlines())}
        self.assertEqual(sum(metrics["Fill timing"]["unavailable"].values()), 3)
        compat = self.run_cli("-", "--latency-summary", "--legacy-fills", "--json", data=data)
        self.assertEqual(compat.returncode, 0, compat.stderr)
        metrics = {item.get("label"): item for item in map(json.loads, compat.stdout.splitlines())}
        self.assertEqual(metrics["Time to first fill"]["percentiles_microseconds"]["p50"], 2_000_000)
        self.assertEqual(metrics["Time to full fill"]["percentiles_microseconds"]["p50"], 3_000_000)

    def test_filter_diagnostics_and_incompatible_options(self):
        data = b"2026-01-12 14:30:00 8=FIX.4.4|9=1|35=D|49=A|56=B|11=ID|10=000|\n"
        for options, expected in (
            (("--session", "X", "Y"), b"no messages for session X / Y"),
            (("--from", "2026-01-12T14:29:59Z"), b"timezone convention differs"),
        ):
            result = self.run_cli("ID", "-", *options, data=data)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(result.stdout, b"")
            self.assertIn(expected, result.stderr)
        incompatible = self.run_cli("-", "--session-diagnostics", "--exchange-facing")
        self.assertEqual(incompatible.returncode, 2)
        self.assertIn(b"does not apply", incompatible.stderr)

    def test_rotated_inputs_reject_duplicate_and_reversed_captures(self):
        sample = CLI.parent / "samples" / "orders.fix"
        rows = sample.read_bytes().splitlines(keepends=True)
        with tempfile.TemporaryDirectory() as tmp:
            older = Path(tmp, "older.log")
            newer = Path(tmp, "newer.log.gz")
            older.write_bytes(b"".join(rows[:3]))
            newer.write_bytes(gzip.compress(b"".join(rows[3:])))
            for first, second, reason in (
                (older, older, b"duplicate rotated capture"),
                (newer, older, b"out of order or overlap"),
            ):
                result = self.run_cli("CLIENT-42", first, "--next-log", second)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(reason, result.stderr)
        with patch("fixprinter.os.path.samefile", side_effect=OSError):
            self.assertTrue(same_capture(str(sample), str(sample)))
            self.assertFalse(same_capture(str(sample), str(sample.with_name("other.fix"))))

    def test_oversized_record_keeps_partial_lifecycle_and_route_output(self):
        samples = CLI.parent / "samples"
        with tempfile.TemporaryDirectory() as tmp:
            partial = Path(tmp, "partial.log")
            partial.write_bytes((samples / "session-44.fix").read_bytes() + b"x" * (8 * 1024 * 1024) + b"\n")
            lifecycle = self.run_cli("Outbound_ClOrderId", partial, "--lifecycle", "--json")
            self.assertEqual(lifecycle.returncode, 2, lifecycle.stderr)
            *messages, summary = [json.loads(line) for line in lifecycle.stdout.splitlines()]
            self.assertEqual(len(messages), 5)
            self.assertEqual(summary["timings"][0]["reason"], "capture has a skipped record")
            route = self.run_cli(
                "Inbound_ClOrderId", samples / "client-ingress.fix", "--routing-latency", partial,
                "--outbound-link-tag", 37, "--json",
            )
            self.assertEqual(route.returncode, 2, route.stderr)
            self.assertIsNotNone(json.loads(route.stdout)["outbound"]["line"])
            self.assertIsNone(json.loads(route.stdout)["timings"][0]["microseconds"])

    def test_closed_stdout_pipe_exits_quietly(self):
        sample = CLI.parent / "samples" / "orders.fix"
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp, "many.log")
            capture.write_bytes(sample.read_bytes() * 200)
            with subprocess.Popen(
                [sys.executable, str(CLI), "CLIENT-42", str(capture)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ) as process:
                self.assertTrue(process.stdout.read(1))
                process.stdout.close()
                self.assertEqual(process.stderr.read(), b"")
                self.assertEqual(process.wait(timeout=10), 0)

    def test_dictionary_acronyms_color_environments_and_omitted_checksum_separator(self):
        data = wire(b"4.4", b"35=D\x0111=ID\x0122=5\x01")[:-1] + b"\n"
        result = self.run_cli("ID", "-", "--json", data=data)
        self.assertEqual(result.returncode, 0, result.stderr)
        message = json.loads(result.stdout)
        self.assertEqual(message["warnings"], [])
        self.assertIn("RIC Code", [item["description"] for item in message["fields"]])
        for name in ("CLICOLOR_FORCE", "FORCE_COLOR"):
            with patch.dict(os.environ, {name: "1"}, clear=True):
                forced = self.run_cli("ID", "-", data=data)
                plain = self.run_cli("ID", "-", "--color", "never", data=data)
            self.assertIn(b"\x1b[36m", forced.stdout)
            self.assertNotIn(b"\x1b", plain.stdout)

    def test_percentiles_bound_storage_and_preserve_slowest_evidence(self):
        with patch("fixprinter.MAX_EXACT_LATENCY_VALUES", 8):
            stats = LatencyStats()
            for value in range(1, 10_001):
                stats.add(Timing("test", microseconds=value))
        self.assertTrue(stats.approximate)
        self.assertLess(len(stats.values), 1_000)
        self.assertEqual(sum(stats.values.values()), 10_000)
        self.assertEqual(stats.slowest[0].microseconds, 10_000)
        self.assertGreaterEqual(stats.percentiles(10_000)["p99"], 9_700)

    def test_wire_data_with_embedded_delimiter_and_fake_field(self):
        payload = b"hi\x0111=FAKE"
        data = wire(b"4.2", b"35=8\x01150=1\x01354=10\x01355=" + payload + b"\x0111=REAL\x01")
        result = self.run_cli("REAL", "-", data=data)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"Partial Fill [1]", result.stdout)
        self.assertNotIn(b"Warning", result.stdout)
        self.assertEqual(self.run_cli("FAKE", "-", data=data).returncode, 1)

    def test_wire_length_and_checksum_warnings(self):
        data = b"8=FIX.4.4\x019=1\x0135=D\x0111=A\x0110=999\x01"
        result = self.run_cli("A", "-", data=data)
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"BodyLength mismatch", result.stdout)
        self.assertIn(b"CheckSum mismatch", result.stdout)

    def test_multiple_messages_and_recovery_after_missing_trailer(self):
        data = (
            b"8=FIX.4.2|9=1|35=D|11=A|"
            b"8=FIX.4.4|9=1|35=8|11=A|10=000|"
            b"8=FIX.4.4|9=1|35=8|11=A|10=000|\n"
        )
        result = self.run_cli("A", "-", data=data)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.count(b"=== stdin:"), 3)
        self.assertEqual(result.stdout.count(b"Missing CheckSum"), 1)

    def test_terminal_controls_are_escaped(self):
        data = b"8=FIX.4.4|9=1|35=D|11=A|58=\x1b[2J|10=000|\n"
        result = self.run_cli("A", "-", data=data)
        self.assertNotIn(b"\x1b", result.stdout)
        self.assertIn(rb"\x1b[2J", result.stdout)
        colored = self.run_cli("A", "-", "--color", "always", data=data).stdout
        self.assertIn(b"\x1b[36mMsgType", colored)
        self.assertIn(b"\x1b[36mNew Order Single [D]", colored)
        self.assertIn(b"\x1b[32mBeginString", colored)
        self.assertIn(b"\x1b[1;33mA", colored)

    def test_oversized_record_is_skipped_and_later_match_is_printed(self):
        data = b"x" * (8 * 1024 * 1024 + 1) + b"\n8=FIX.4.4|9=1|35=D|11=A|10=000|\n"
        result = self.run_cli("A", "-", data=data)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"skipped record", result.stderr)
        self.assertIn(b"=== stdin:2", result.stdout)

    def test_missing_file_reports_input_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_cli("A", Path(tmp, "missing.log"))
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"fixprinter:", result.stderr)


if __name__ == "__main__":
    unittest.main()
