"""Read and print FIX 4.2 and 4.4 messages from exported logs."""

import argparse
import base64
import gzip
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
import zlib
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
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


@dataclass(frozen=True)
class CaptureFilter:
    session: tuple[bytes, bytes] | None = None
    start: datetime | None = None
    stop: datetime | None = None

    def accepts(self, message: Message, prefix: bytes) -> bool:
        if self.session:
            sender = target = None
            for tag, value in message.fields:
                if tag == 49:
                    sender = value
                elif tag == 56:
                    target = value
            if (sender, target) not in (self.session, self.session[::-1]):
                return False
        if self.start is None and self.stop is None:
            return True
        timestamp = capture_time(prefix)
        bound = self.start if self.start is not None else self.stop
        if timestamp is None or (timestamp.tzinfo is None) != (bound.tzinfo is None):
            return False
        return (self.start is None or timestamp >= self.start) and (
            self.stop is None or timestamp < self.stop
        )


@dataclass(frozen=True)
class CapturePoint:
    source: str
    line: int
    timestamp: datetime | None

    @classmethod
    def from_prefix(cls, source: str, line: int, prefix: bytes):
        return cls(source, line, capture_time(prefix))


@dataclass(frozen=True)
class Timing:
    label: str
    start: CapturePoint | None = None
    end: CapturePoint | None = None
    microseconds: int | None = None
    reason: str | None = None

    def render(self, color: bool) -> str:
        if self.microseconds is None:
            return f"{self.label}: latency unavailable ({self.reason})"
        return f"{self.label}: {format_duration(self.microseconds, color)}"


@dataclass
class PendingRequest:
    kind: bytes
    original_id: bytes | None
    point: CapturePoint


SESSION_BOUNDARIES = {b"A", b"5"}
TIMED_TYPES = SESSION_BOUNDARIES | {b"D", b"F", b"G", b"8", b"9"}


class LatencyTracker:
    def __init__(self, exchange_facing: bool = False) -> None:
        self.exchange_facing = exchange_facing
        self.pending: OrderedDict[
            tuple[bytes, bytes, bytes, bytes], PendingRequest | None
        ] = OrderedDict()

    def observe(
        self, message: Message, prefix: bytes, source: str, line: int,
        fields: dict[int, bytes] | None = None,
    ) -> Timing | None:
        if message.issues or message.fields[2][1] not in TIMED_TYPES:
            return None
        if fields is None:
            fields = dict(message.fields)
        kind = fields[35]
        sender, target = fields.get(49), fields.get(56)
        if (not sender or not target or
                fields.get(43) == b"Y" or fields.get(97) == b"Y"):
            return None
        if kind in SESSION_BOUNDARIES:
            for key in list(self.pending):
                version, origin, destination, _ = key
                if version == message.version and (origin, destination) in (
                    (sender, target), (target, sender)
                ):
                    del self.pending[key]
            return None

        identifier = fields.get(11)
        if kind in (b"D", b"F", b"G"):
            if identifier:
                key = (message.version, sender, target, identifier)
                self.pending[key] = None if key in self.pending else PendingRequest(
                    kind, fields.get(41), CapturePoint.from_prefix(source, line, prefix)
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
        elif kind == b"8" and execution == b"5":
            label, expected_kind = "Replace ack", b"G"
        elif kind == b"9" and fields.get(434) in (None, b"1", b"2"):
            if fields.get(434) == b"2":
                label, expected_kind = "Replace reject", b"G"
            else:
                label, expected_kind = "Cancel reject", b"F"
        else:
            return None

        if self.exchange_facing:
            label = f"Exchange {label.lower()} round trip"

        point = CapturePoint.from_prefix(source, line, prefix)
        if not identifier:
            return Timing(label, end=point, reason="missing ClOrdID")
        key = (message.version, target, sender, identifier)
        if key not in self.pending:
            return Timing(label, end=point, reason="request not found in this session")
        request = self.pending[key]
        if request is None:
            del self.pending[key]
            return Timing(label, end=point, reason="multiple requests share this ClOrdID")
        if request.kind != expected_kind or (
            expected_kind in (b"F", b"G") and request.original_id and fields.get(41)
            and request.original_id != fields[41]
        ):
            return Timing(label, request.point, point, reason="request and response do not match")
        del self.pending[key]

        return elapsed_time(label, request.point, point)


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


def elapsed_time(label: str, start_point: CapturePoint, end_point: CapturePoint) -> Timing:
    start, end = start_point.timestamp, end_point.timestamp
    if start is None or end is None:
        reason = "missing or unsupported capture timestamp"
    elif (start.tzinfo is None) != (end.tzinfo is None):
        reason = "capture timestamps use different timezone conventions"
    else:
        elapsed = end - start
        if elapsed.days >= 0:
            micros = (elapsed.days * 86_400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds
            return Timing(label, start_point, end_point, microseconds=micros)
        reason = "response timestamp precedes request" if label != "Internal routing" else (
            "outbound timestamp precedes inbound timestamp"
        )
    return Timing(label, start_point, end_point, reason=reason)


def records(stream, source, *, containing: bytes | None = None, from_line: int = 1):
    line_number = 0
    while record := stream.readline(MAX_LINE + 1):
        line_number += 1
        if len(record) > MAX_LINE:
            while record and not record.endswith(b"\n"):
                record = stream.readline(MAX_LINE + 1)
            print(f"{source}:{line_number}: skipped record over 8 MiB", file=sys.stderr)
            yield line_number, b"", None
            continue
        if line_number < from_line or (containing is not None and containing not in record):
            continue
        line = record.rstrip(b"\r\n")
        offset = 0
        while start := START.search(line, offset):
            message = parse_message(line, start)
            yield line_number, line[offset:start.start()].strip(), message
            offset = max(start.end(), message.end)


def emit_json(kind: str, **values) -> None:
    print(json.dumps({"schema_version": 1, "type": kind, **values}, default=datetime.isoformat))


def emit_message(
    message, source, line, prefix, timing, tags, identifier, dictionaries, color, json_output,
):
    names, enums = dictionaries[message.version]
    if json_output:
        fields = []
        for tag, raw in message.fields:
            try:
                value = raw.decode("utf-8")
                raw_base64 = None
            except UnicodeDecodeError:
                value = safe_text(raw)
                raw_base64 = base64.b64encode(raw).decode("ascii")
            field = {
                "tag": tag,
                "name": names.get(tag, "Unknown"),
                "value": value,
                "description": enums.get(tag, {}).get(value),
                "matched": tag in tags and raw == identifier,
            }
            if raw_base64 is not None:
                field["raw_base64"] = raw_base64
            fields.append(field)
        emit_json(
            "message", **asdict(CapturePoint.from_prefix(source, line, prefix)),
            prefix=prefix.decode("utf-8", errors="backslashreplace"), version=message.version.decode(),
            fields=fields, warnings=message.issues, timing=asdict(timing) if timing else None,
        )
        return
    heading = f"=== {source}:{line}"
    if prefix:
        heading += f" | {safe_text(prefix)}"
    if timing:
        heading += f" | {timing.render(color)}"
    print_message(message, heading, tags, identifier, names, enums, color)


def scan(stream, source, tags, identifier, dictionaries, color, args, capture_filter):
    tracker = LatencyTracker(args.exchange_facing)
    matched = False
    skipped = False
    for line, prefix, message in records(stream, source):
        if message is None:
            skipped = True
            tracker.pending.clear()
            continue
        if not capture_filter.accepts(message, prefix):
            if not message.issues and message.fields[2][1] in SESSION_BOUNDARIES:
                tracker.observe(message, prefix, source, line)
            continue
        timing = tracker.observe(message, prefix, source, line)
        if any(tag in tags and value == identifier for tag, value in message.fields):
            matched = True
            emit_message(message, source, line, prefix, timing, tags, identifier, dictionaries, color, args.json)
    return 2 if skipped else 0 if matched else 1


def open_capture(path: str):
    opener = gzip.open if path.lower().endswith(".gz") else open
    return opener(path, "rb")


@dataclass
class RouteOrder:
    point: CapturePoint
    clordid: bytes
    version: bytes
    sender: bytes
    target: bytes
    response: Timing | None = None


def locate_route_order(
    path: str, identifier: bytes, link_tag: int, exchange: bool, capture_filter: CaptureFilter,
) -> RouteOrder | str:
    order = None
    tracker = LatencyTracker(exchange_facing=True)
    source = printable_label(path)
    with open_capture(path) as stream:
        # The inbound lookup only needs exact matches. Exchange timing also
        # needs session boundaries and requests that may share an outbound ID.
        containing = None if exchange else identifier
        for line, prefix, message in records(stream, source, containing=containing):
            if message is None:
                raise ValueError("order correlation requires captures without skipped records")
            if message.issues:
                continue
            kind = message.fields[2][1]
            if kind != b"D" and (not exchange or kind not in TIMED_TYPES):
                continue
            if not capture_filter.accepts(message, prefix):
                if exchange and kind in SESSION_BOUNDARIES:
                    tracker.observe(message, prefix, source, line)
                continue
            fields = dict(message.fields)
            if fields.get(43) == b"Y" or fields.get(97) == b"Y":
                continue
            timing = tracker.observe(message, prefix, source, line, fields) if exchange else None
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
                    CapturePoint.from_prefix(source, line, prefix), clordid_values[0],
                    message.version, sender, target
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
    args, inbound_id: bytes, outbound_key: bytes, color: bool,
    inbound_filter: CaptureFilter, outbound_filter: CaptureFilter,
) -> int:
    link_tag = args.outbound_link_tag or 11
    inbound = locate_route_order(args.log, inbound_id, 11, False, inbound_filter)
    outbound = locate_route_order(args.routing_latency, outbound_key, link_tag, True, outbound_filter)
    found = isinstance(inbound, RouteOrder) and isinstance(outbound, RouteOrder)
    if found:
        if inbound.point.line == outbound.point.line and os.path.samefile(args.log, args.routing_latency):
            raise ValueError("inbound and outbound must identify different captured events")
        routing = elapsed_time("Internal routing", inbound.point, outbound.point)
    else:
        routing = Timing("Internal routing", reason="requires one matching order on each leg")
    exchange = None
    if isinstance(outbound, RouteOrder):
        exchange = outbound.response or Timing(
            "Exchange order response round trip", outbound.point, reason="response not found"
        )

    legs = {}
    for leg, path, tag, key, match in (
        ("inbound", args.log, 11, inbound_id, inbound),
        ("outbound", args.routing_latency, link_tag, outbound_key, outbound),
    ):
        source = printable_label(path)
        if isinstance(match, RouteOrder):
            legs[leg] = {
                **asdict(match.point), "clordid": safe_text(match.clordid),
                "match_tag": tag, "match_value": safe_text(key), "reason": None,
            }
            if not args.json:
                timestamp = match.point.timestamp.isoformat(sep=" ") if match.point.timestamp else "timestamp unavailable"
                link = f" | tag {tag}={safe_text(key)}" if tag != 11 else ""
                print(f"{leg.title()} 35=D: {source}:{match.point.line} | "
                      f"ClOrdID={safe_text(match.clordid)}{link} | {timestamp}")
        else:
            legs[leg] = {"source": source, "match_tag": tag, "match_value": safe_text(key), "reason": match}
            if not args.json:
                print(f"{leg.title()} 35=D unavailable: {source} | tag {tag}={safe_text(key)} ({match})")

    timings = [routing, exchange] if exchange else [routing]
    if args.json:
        emit_json("routing", **legs, timings=[asdict(timing) for timing in timings])
    else:
        for timing in timings:
            print(timing.render(color))
    return 0 if found else 1


def is_fill(version: bytes, fields: dict[int, bytes]) -> bool:
    execution = fields.get(150)
    if execution not in ({b"1", b"2"} if version == b"FIX.4.2" else {b"F"}):
        return False
    if version == b"FIX.4.2" and fields.get(20, b"0") != b"0":
        return False
    try:
        quantity = Decimal(fields.get(32, b"0").decode("ascii"))
        return quantity.is_finite() and quantity > 0
    except (InvalidOperation, UnicodeDecodeError):
        return False


def print_lifecycle(args, identifier, dictionaries, color, capture_filter):
    root = locate_route_order(args.log, identifier, 11, False, capture_filter)
    if isinstance(root, str):
        if args.json:
            emit_json("lifecycle_summary", clordid=safe_text(identifier), reason=root, timings=[])
        else:
            print(f"Lifecycle unavailable: {root}")
        return 1

    source = root.point.source
    ids = {identifier}
    order_ids = set()
    tracker = LatencyTracker(args.exchange_facing)
    started = False
    first_fill = full_fill = None
    with open_capture(args.log) as stream:
        for line, prefix, message in records(stream, source, from_line=root.point.line):
            if message is None:
                raise ValueError("order correlation requires captures without skipped records")
            if message.issues or message.version != root.version:
                continue
            kind = message.fields[2][1]
            if kind not in TIMED_TYPES:
                continue
            if kind not in SESSION_BOUNDARIES and not capture_filter.accepts(message, prefix):
                continue
            fields = dict(message.fields)
            if fields.get(43) == b"Y" or fields.get(97) == b"Y":
                continue
            endpoints = fields.get(49), fields.get(56)
            forward = endpoints == (root.sender, root.target)
            if not forward and endpoints != (root.target, root.sender):
                continue
            clordid = fields.get(11)
            orig, orderid = fields.get(41), fields.get(37)
            if not started:
                if kind != b"D" or clordid != identifier or not forward:
                    continue
                started = True
            elif kind in SESSION_BOUNDARIES:
                break
            elif forward and kind in (b"F", b"G"):
                if orig not in ids and orderid not in order_ids:
                    continue
                if clordid:
                    ids.add(clordid)
            elif not forward and kind in (b"8", b"9"):
                if clordid not in ids and orig not in ids and orderid not in order_ids:
                    continue
                if kind == b"8" and clordid in ids and fields.get(150) != b"8":
                    if orderid and orderid.upper() not in (b"0", b"NONE", b"UNKNOWN", b"N/A"):
                        order_ids.add(orderid)
            else:
                continue

            timing = tracker.observe(message, prefix, source, line, fields)
            emit_message(
                message, source, line, prefix, timing, ID_TAGS, identifier, dictionaries, color, args.json
            )
            if not forward and kind == b"8" and is_fill(message.version, fields):
                point = CapturePoint.from_prefix(source, line, prefix)
                if first_fill is None:
                    first_fill = elapsed_time("Time to first fill", root.point, point)
                if full_fill is None and fields.get(39) == b"2":
                    full_fill = elapsed_time("Time to full fill", root.point, point)

    timings = [
        first_fill or Timing("Time to first fill", root.point, reason="no fill seen"),
        full_fill or Timing("Time to full fill", root.point, reason="no full fill seen"),
    ]
    if args.json:
        emit_json("lifecycle_summary", clordid=safe_text(identifier),
                  timings=[asdict(timing) for timing in timings])
    else:
        for timing in timings:
            print(timing.render(color))
    return 0


def time_bound(text: str) -> datetime:
    try:
        value = text.encode("ascii")
    except UnicodeEncodeError:
        value = b""
    match = CAPTURE_TIME.fullmatch(value)
    if match is not None:
        timestamp = capture_time(value)
        if timestamp is not None:
            return timestamp
    raise argparse.ArgumentTypeError("enter a full capture timestamp, e.g. 2026-01-12T14:30:00Z")


def positive_tag(text: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,8}", text):
        raise argparse.ArgumentTypeError("tag must be a positive integer of up to 9 digits")
    return int(text)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "With one log, order, cancel, and replace response timing is automatic. "
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
    parser.add_argument("--session", nargs=2, metavar=("COMP_ID", "PEER_ID"),
                        help="limit the main log to this CompID pair in either direction")
    parser.add_argument("--outbound-session", nargs=2, metavar=("COMP_ID", "PEER_ID"),
                        help="limit the second log to this pair with --routing-latency")
    parser.add_argument("--from", dest="start", type=time_bound, metavar="TIME",
                        help="include capture timestamps at or after TIME (both logs in routing mode)")
    parser.add_argument("--until", type=time_bound, metavar="TIME",
                        help="include capture timestamps before TIME (both logs in routing mode)")
    parser.add_argument("--lifecycle", action="store_true",
                        help="follow an order from its original ClOrdID in one file and report fill times")
    parser.add_argument("--json", action="store_true", help="write versioned JSON Lines to stdout")
    args = parser.parse_args()
    if args.outbound_session and not args.routing_latency:
        parser.error("--outbound-session requires --routing-latency")
    if args.lifecycle and (args.routing_latency or args.log == "-" or args.tag):
        parser.error("--lifecycle requires a log file and original ClOrdID, without --routing-latency or --tag")
    if args.start and args.until and (
        (args.start.tzinfo is None) != (args.until.tzinfo is None) or args.start >= args.until
    ):
        parser.error("--from must precede --until and use the same timezone convention")
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
    inbound_session = tuple(comp.encode("utf-8") for comp in args.session) if args.session else None
    outbound_session = tuple(comp.encode("utf-8") for comp in args.outbound_session) if args.outbound_session else None
    capture_filter = CaptureFilter(inbound_session, args.start, args.until)
    tags = set(args.tag) if args.tag else ID_TAGS
    identifier = args.identifier.encode("utf-8")
    source = "stdin" if args.log == "-" else printable_label(args.log)
    dictionaries = {}
    custom_versions = set()
    try:
        if args.routing_latency:
            outbound_key = args.outbound_clordid.encode("utf-8") if args.outbound_clordid else identifier
            return print_route(
                args, identifier, outbound_key, color, capture_filter,
                CaptureFilter(outbound_session, args.start, args.until),
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
        if args.lifecycle:
            return print_lifecycle(args, identifier, dictionaries, color, capture_filter)
        if args.log == "-":
            return scan(sys.stdin.buffer, source, tags, identifier, dictionaries, color, args, capture_filter)
        with open_capture(args.log) as stream:
            return scan(stream, source, tags, identifier, dictionaries, color, args, capture_filter)
    except (OSError, EOFError, ET.ParseError, ValueError, KeyError, zlib.error) as exc:
        print(f"fixprinter: {printable_label(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
