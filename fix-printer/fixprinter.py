"""Read and print FIX 4.2 and 4.4 messages from exported logs."""

import argparse
import gzip
import os
import re
import sys
import xml.etree.ElementTree as ET
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

START = re.compile(rb"(?<![A-Za-z0-9_=])8=(FIX\.4\.[24])([\x01|])")
FIELD = re.compile(rb"([1-9][0-9]{0,8})=")
MAX_LINE = 8 * 1024 * 1024
MAX_PENDING = 100_000
CAPTURE_TIME = re.compile(
    rb"^(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})[ T]+"
    rb"(\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)"
    rb"(Z|[+-]\d{2}:\d{2})?(?=\s|$)"
)
ID_TAGS = {11, 37, 41}
DATA_TAGS = {
    90: 91, 93: 89, 95: 96, 212: 213, 348: 349, 350: 351,
    352: 353, 354: 355, 356: 357, 358: 359, 360: 361,
    362: 363, 364: 365, 445: 446, 618: 619, 621: 622,
}
DICTIONARIES = Path(__file__).resolve().with_name("dictionaries")
BUILTIN_FILES = {b"FIX.4.2": "FIX42.xml", b"FIX.4.4": "FIX44.xml"}



@dataclass
class Message:
    version: bytes
    fields: list[tuple[int, bytes]]
    issues: list[str]
    end: int


def parse_message(line: bytes, start: re.Match[bytes]) -> Message:
    version, separator = start.groups()
    position = start.start()
    fields: list[tuple[int, bytes]] = []
    issues: list[str] = []
    body_start = None
    expected_data = None
    while position < len(line):
        if fields and line.startswith(b"8=FIX.", position) and START.match(line, position):
            break
        match = FIELD.match(line, position)
        if match is None:
            issues.append("Invalid field syntax")
            break
        tag = int(match[1])
        if expected_data is None:
            end = line.find(separator, match.end())
        else:
            data_tag, length = expected_data
            if tag != data_tag:
                issues.append(f"Expected encoded data tag {data_tag}")
                break
            end = match.end() + length
            if line[end:end + 1] != separator:
                issues.append(f"Invalid encoded length for tag {tag}")
                break
            expected_data = None
        if end < 0:
            if tag != 10:
                issues.append(f"Incomplete field {tag}")
                break
            end = len(line)
            issues.append("Missing trailing delimiter")
        value = line[match.end():end]
        fields.append((tag, value))
        if len(fields) == 2 and tag == 9 and value.isdigit() and len(value) <= 9:
            body_start = end + 1
        if tag in DATA_TAGS:
            if not value.isdigit() or len(value) > 9:
                issues.append(f"Invalid encoded length in tag {tag}")
                break
            expected_data = (DATA_TAGS[tag], int(value))
        if tag == 10:
            if not re.fullmatch(rb"[0-9]{3}", value) or int(value) > 255:
                issues.append("Invalid CheckSum field")
            if separator == b"\x01":
                if body_start is not None and int(fields[1][1]) != position - body_start:
                    issues.append("BodyLength mismatch")
                checksum = sum(memoryview(line)[start.start():position]) % 256
                if value != f"{checksum:03d}".encode():
                    issues.append("CheckSum mismatch")
            position = min(end + 1, len(line))
            break
        position = end + 1
    if body_start is None:
        issues.append("Missing or invalid BodyLength")
    if len(fields) < 3 or fields[2][0] != 35:
        issues.append("Missing or misplaced MsgType")
    if not fields or fields[-1][0] != 10:
        issues.append("Missing CheckSum")
    return Message(version, fields, issues, position)


def safe_text(value: bytes) -> str:
    decoded = value.decode("utf-8", errors="backslashreplace")
    return "".join(char if char.isprintable() else ascii(char)[1:-1] for char in decoded)


def load_dictionary(path: str | Path) -> tuple[bytes, dict[int, str], dict[int, dict[str, str]]]:
    root = ET.parse(path).getroot()
    version = f"FIX.{root.get('major')}.{root.get('minor')}".encode()
    fields = root.find("fields")
    if root.tag != "fix" or version not in {b"FIX.4.2", b"FIX.4.4"} or fields is None:
        raise ValueError("expected a QuickFIX XML dictionary for FIX 4.2 or 4.4")
    names = {}
    enums = {}
    for field in fields.findall("field"):
        tag = int(field.attrib["number"])
        names[tag] = field.attrib["name"]
        enums[tag] = {value.attrib["enum"]: value.attrib["description"]
                      for value in field.findall("value")}
    return version, names, enums


def printable_label(value: str) -> str:
    return safe_text(value.encode("utf-8"))


def print_message(
    message: Message, heading: str, tags: set[int], identifier: bytes,
    names: dict[int, str], enums: dict[int, dict[str, str]], color: bool,
) -> None:
    print(heading)
    for issue in message.issues:
        print(f"  Warning: {issue}")
    for tag, raw in message.fields:
        name = printable_label(names.get(tag, "Unknown"))
        value = safe_text(raw)
        description = enums.get(tag, {}).get(raw.decode("utf-8", errors="replace"))
        if description:
            value = f"{printable_label(description)} [{value}]"
        if color:
            name_color = 36 if tag == 35 else 32
            name = f"\x1b[{name_color}m{name:24}\x1b[0m"
            if tag in tags and raw == identifier:
                value = f"\x1b[1;33m{value}\x1b[0m"
            elif tag == 35:
                value = f"\x1b[36m{value}\x1b[0m"
        else:
            name = f"{name:24}"
        print(f"  <{tag:>5}>  {name} = {value}")
    print()


def capture_time(prefix: bytes) -> datetime | None:
    match = CAPTURE_TIME.match(prefix)
    if match is None:
        return None
    date, time, zone = match.groups(default=b"")
    if b"/" in date:
        month, day, year = date.split(b"/")
        date = b"-".join((year, month, day))
    try:
        return datetime.fromisoformat((date + b"T" + time + zone).decode("ascii"))
    except ValueError:
        return None


@dataclass
class PendingRequest:
    kind: bytes
    original_id: bytes | None
    timestamp: datetime | None


TIMED_TYPES = {b"A", b"5", b"D", b"F", b"8", b"9"}


class LatencyTracker:
    def __init__(self, exchange_facing: bool = False, color: bool = False) -> None:
        self.exchange_facing = exchange_facing
        self.color = color
        self.pending: OrderedDict[
            tuple[bytes, bytes, bytes, bytes], PendingRequest | None
        ] = OrderedDict()

    def observe(self, message: Message, prefix: bytes) -> str | None:
        if message.issues or message.fields[2][1] not in TIMED_TYPES:
            return None
        fields = dict(message.fields)
        kind = fields[35]
        sender, target = fields.get(49), fields.get(56)
        if (not sender or not target or
                fields.get(43) == b"Y" or fields.get(97) == b"Y"):
            return None
        if kind in (b"A", b"5"):
            for key in list(self.pending):
                version, origin, destination, _ = key
                if version == message.version and (origin, destination) in (
                    (sender, target), (target, sender)
                ):
                    del self.pending[key]
            return None

        identifier = fields.get(11)
        if kind in (b"D", b"F"):
            if identifier:
                key = (message.version, sender, target, identifier)
                self.pending[key] = None if key in self.pending else PendingRequest(
                    kind, fields.get(41), capture_time(prefix)
                )
                self.pending.move_to_end(key)
                if len(self.pending) > MAX_PENDING:
                    self.pending.popitem(last=False)
            return None

        execution = fields.get(150)
        if kind == b"8" and execution in (b"0", b"8"):
            label, expected_kind = ("Order ack" if execution == b"0" else "Order reject"), b"D"
        elif kind == b"8" and execution == b"4":
            label, expected_kind = "Cancel ack", b"F"
        elif kind == b"9" and fields.get(434) in (None, b"1"):
            label, expected_kind = "Cancel reject", b"F"
        else:
            return None

        if self.exchange_facing:
            label = f"Exchange {label.lower()} round trip"

        if not identifier:
            return f"{label}: latency unavailable (missing ClOrdID)"
        key = (message.version, target, sender, identifier)
        if key not in self.pending:
            return f"{label}: latency unavailable (request not found in this session)"
        request = self.pending[key]
        if request is None:
            del self.pending[key]
            return f"{label}: latency unavailable (multiple requests share this ClOrdID)"
        if request.kind != expected_kind or (
            expected_kind == b"F" and request.original_id and fields.get(41)
            and request.original_id != fields[41]
        ):
            return f"{label}: latency unavailable (request and response do not match)"
        del self.pending[key]

        return elapsed_time(label, request.timestamp, capture_time(prefix), self.color)


def format_duration(micros: int, color: bool) -> str:
    if micros < 1_000:
        return f"{micros}\u03bcs"
    if micros < 1_000_000:
        whole, fraction = divmod(micros, 1_000)
        number = f"{whole}.{fraction:03d}".rstrip("0").rstrip(".")
        return f"{number}ms"

    whole, fraction = divmod(micros, 1_000_000)
    number = f"{whole}.{fraction:06d}".rstrip("0").rstrip(".")
    value = f"{number}s"
    return f"\x1b[31m{value}\x1b[0m" if color else value


def elapsed_time(label: str, start: datetime | None, end: datetime | None, color: bool) -> str:
    reason = None
    if start is None or end is None:
        reason = "missing or unsupported capture timestamp"
    elif (start.tzinfo is None) != (end.tzinfo is None):
        reason = "capture timestamps use different timezone conventions"
    else:
        elapsed = end - start
        if elapsed.days >= 0:
            micros = (elapsed.days * 86_400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds
            return f"{label}: {format_duration(micros, color)}"
        reason = "response timestamp precedes request" if label != "Internal routing" else (
            "outbound timestamp precedes inbound timestamp"
        )
    return f"{label}: latency unavailable ({reason})"


def records(stream, source):
    line_number = 0
    while record := stream.readline(MAX_LINE + 1):
        line_number += 1
        if len(record) > MAX_LINE:
            while record and not record.endswith(b"\n"):
                record = stream.readline(MAX_LINE + 1)
            print(f"{source}:{line_number}: skipped record over 8 MiB", file=sys.stderr)
            yield line_number, b"", None
            continue
        line = record.rstrip(b"\r\n")
        offset = 0
        while start := START.search(line, offset):
            message = parse_message(line, start)
            yield line_number, line[offset:start.start()].strip(), message
            offset = max(start.end(), message.end)


def scan(stream, source, tags, identifier, dictionaries, color, exchange_facing):
    tracker = LatencyTracker(exchange_facing, color)
    matched = False
    skipped = False
    for line_number, prefix_bytes, message in records(stream, source):
        if message is None:
            skipped = True
            tracker.pending.clear()
            continue
        timing = tracker.observe(message, prefix_bytes)
        if any(tag in tags and value == identifier for tag, value in message.fields):
            matched = True
            names, enums = dictionaries[message.version]
            # Prefix belongs to the segment preceding this message.
            prefix = safe_text(prefix_bytes)
            heading = f"=== {source}:{line_number}"
            if prefix:
                heading += f" | {prefix}"
            if timing:
                heading += f" | {timing}"
            print_message(message, heading, tags, identifier, names, enums, color)
    return 2 if skipped else 0 if matched else 1


def open_capture(path: str):
    opener = gzip.open if path.lower().endswith(".gz") else open
    return opener(path, "rb")


@dataclass
class RouteOrder:
    line: int
    clordid: bytes
    timestamp: datetime | None
    version: bytes
    sender: bytes
    target: bytes
    response: str | None = None


def locate_route_order(
    path: str, identifier: bytes, link_tag: int, exchange: bool, color: bool,
) -> RouteOrder | str:
    order = None
    tracker = LatencyTracker(exchange_facing=True, color=color)
    with open_capture(path) as stream:
        for line, prefix, message in records(stream, printable_label(path)):
            if message is None:
                raise ValueError("route timing requires captures without skipped records")
            if message.issues:
                continue
            fields = dict(message.fields)
            if fields.get(43) == b"Y" or fields.get(97) == b"Y":
                continue
            timing = tracker.observe(message, prefix) if exchange else None
            if fields.get(35) == b"D":
                link_values = [value for tag, value in message.fields if tag == link_tag]
                if identifier not in link_values:
                    continue
                if len(link_values) != 1:
                    return f"new order contains repeated tag {link_tag}"
                if order is not None:
                    return f"multiple new orders match tag {link_tag}"
                clordid_values = [value for tag, value in message.fields if tag == 11]
                if len(clordid_values) != 1 or not clordid_values[0]:
                    return "new order requires exactly one nonempty ClOrdID (tag 11)"
                sender, target = fields.get(49), fields.get(56)
                if not sender or not target:
                    return "new order is missing sender or target CompID"
                order = RouteOrder(
                    line, clordid_values[0], capture_time(prefix), message.version, sender, target
                )
            elif (
                exchange and order is not None and order.response is None
                and fields.get(35) == b"8" and fields.get(150) in (b"0", b"8")
                and fields.get(11) == order.clordid
                and message.version == order.version
                and (fields.get(49), fields.get(56)) == (order.target, order.sender)
            ):
                order.response = timing
    if order is None:
        return f"new order not found by tag {link_tag}"
    return order


def print_route(
    inbound_path: str, inbound_id: bytes, outbound_path: str, outbound_key: bytes, link_tag: int, color: bool,
) -> int:
    inbound = locate_route_order(inbound_path, inbound_id, 11, exchange=False, color=color)
    outbound = locate_route_order(outbound_path, outbound_key, link_tag, exchange=True, color=color)
    if (
        isinstance(inbound, RouteOrder) and isinstance(outbound, RouteOrder)
        and inbound.line == outbound.line and os.path.samefile(inbound_path, outbound_path)
    ):
        raise ValueError("inbound and outbound must identify different captured events")
    for leg, path, tag, key, match in (
        ("Inbound", inbound_path, 11, inbound_id, inbound),
        ("Outbound", outbound_path, link_tag, outbound_key, outbound),
    ):
        source = printable_label(path)
        if isinstance(match, RouteOrder):
            timestamp = match.timestamp.isoformat(sep=" ") if match.timestamp else "timestamp unavailable"
            link = f" | tag {tag}={safe_text(key)}" if tag != 11 else ""
            print(f"{leg} 35=D: {source}:{match.line} | ClOrdID={safe_text(match.clordid)}{link} | {timestamp}")
        else:
            print(f"{leg} 35=D unavailable: {source} | tag {tag}={safe_text(key)} ({match})")
    if isinstance(inbound, str) or isinstance(outbound, str):
        print("Internal routing: latency unavailable (requires one matching order on each leg)")
        return 1

    print(elapsed_time("Internal routing", inbound.timestamp, outbound.timestamp, color))
    print(outbound.response or "Exchange order response round trip: latency unavailable (response not found)")
    return 0


def positive_tag(text: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,8}", text):
        raise argparse.ArgumentTypeError("tag must be a positive integer of up to 9 digits")
    return int(text)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "With one log, order and cancel response timing is automatic. "
            "When the inbound receive and outbound send are in separate logs, "
            "pass the inbound log as LOG and supply the outbound log with --routing-latency."
        ),
    )
    parser.add_argument("identifier", help="exact identifier, or inbound ClOrdID with --routing-latency")
    parser.add_argument("log", help="plain or .gz log file, or - for stdin")
    parser.add_argument("--tag", action="append", type=positive_tag,
                        help="match this tag instead of 11, 37 and 41 (repeatable)")
    parser.add_argument("--dictionary", action="append", default=[], metavar="XML",
                        help="QuickFIX XML dictionary, optionally one per version")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--exchange-facing", action="store_true",
                       help="label response timings as exchange round trips")
    modes.add_argument("--routing-latency", metavar="OUTBOUND_LOG",
                       help="supply a separate outbound log to measure internal routing from inbound receive to outbound send; matches ClOrdID on both legs by default")
    outbound_match = parser.add_mutually_exclusive_group()
    outbound_match.add_argument("--outbound-clordid",
                                help="match a different outbound ClOrdID with --routing-latency")
    outbound_match.add_argument("--outbound-link-tag", type=positive_tag, metavar="TAG",
                                help="with --routing-latency, match the inbound ClOrdID in this outbound tag")
    args = parser.parse_args()
    if not args.routing_latency and (args.outbound_clordid is not None or args.outbound_link_tag):
        parser.error("--outbound-clordid and --outbound-link-tag require --routing-latency")
    if args.outbound_clordid == "":
        parser.error("--outbound-clordid requires a nonempty identifier")
    if args.routing_latency:
        if args.tag or args.dictionary or args.log == "-" or args.routing_latency == "-":
            parser.error("--routing-latency uses two log files and an inbound ClOrdID, without --tag or --dictionary")
    color = args.color == "always" or (
        args.color == "auto" and sys.stdout.isatty() and "NO_COLOR" not in os.environ
    )
    tags = set(args.tag) if args.tag else ID_TAGS
    identifier = args.identifier.encode("utf-8")
    source = "stdin" if args.log == "-" else printable_label(args.log)
    dictionaries = {}
    custom_versions = set()
    try:
        if args.routing_latency:
            outbound_key = args.outbound_clordid.encode("utf-8") if args.outbound_clordid else identifier
            return print_route(
                args.log, identifier, args.routing_latency, outbound_key, args.outbound_link_tag or 11, color
            )
        for version, filename in BUILTIN_FILES.items():
            actual_version, names, enums = load_dictionary(DICTIONARIES / filename)
            if version != actual_version:
                raise ValueError(f"unexpected FIX version in {filename}")
            dictionaries[version] = (
                names,
                {tag: {code: label.replace("_", " ").title()
                       for code, label in values.items()}
                 for tag, values in enums.items()},
            )
        for path in args.dictionary:
            version, names, enums = load_dictionary(path)
            if version in custom_versions:
                raise ValueError(f"multiple dictionaries for {safe_text(version)}")
            custom_versions.add(version)
            standard_names, standard_enums = dictionaries[version]
            standard_names.update(names)
            for tag, values in enums.items():
                standard_enums.setdefault(tag, {}).update(values)
        if args.log == "-":
            return scan(sys.stdin.buffer, source, tags, identifier, dictionaries, color, args.exchange_facing)
        with open_capture(args.log) as stream:
            return scan(stream, source, tags, identifier, dictionaries, color, args.exchange_facing)
    except (OSError, EOFError, ET.ParseError, ValueError, KeyError, zlib.error) as exc:
        print(f"fixprinter: {printable_label(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
