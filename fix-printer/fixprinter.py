"""Read and print FIX 4.2 and 4.4 messages from exported logs."""

import argparse
import base64
import builtins
import errno
import gzip
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
import zlib
from collections import Counter, OrderedDict, defaultdict
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

HEADER_AFTER_BEGIN = re.compile(rb"9=[0-9]{1,9}([\x01|])35=")
START = re.compile(rb"(?<![A-Za-z0-9_=])8=(FIX\.4\.[24])([\x01|])")
FIELD = re.compile(rb"([1-9][0-9]{0,8})=")
MAX_LINE = 8 * 1024 * 1024
MAX_DISPLAY_VALUE = 512
MAX_PENDING = 100_000
MAX_EXACT_LATENCY_VALUES = 2_048
ENUM_ACRONYMS = frozenset("ASCII ATS BIC CFI CUSIP ETF FIX FX IBAN ID IOI ISIN ISO MIC NAV OTC RFQ RIC SEDOL SWIFT UTC VWAP XML".split())
NON_FILL_EXEC_TYPES = frozenset(bytes([code]) for code in b"03456789ABCDEGHI")
CAPTURE_TIME = re.compile(
    rb"^(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})[ T]+"
    rb"(\d{2}:\d{2}:\d{2}(?:\.[0-9]+)?)"
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
        if fields and line.startswith(b"8=FIX.", position):
            next_start = START.match(line, position)
            if next_start:
                next_header = HEADER_AFTER_BEGIN.match(line, next_start.end())
                if next_header and next_header[1] == next_start[2] and expected_data is None:
                    break
                issues.append("Embedded BeginString-like field without a FIX header")
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


def enum_label(label: str) -> str:
    words = label.replace("_", " ").split()
    return " ".join(word if word in ENUM_ACRONYMS or not word.isupper() else word.capitalize()
                    for word in words)


def load_dictionary(path: str | Path) -> tuple[bytes, dict[int, str], dict[int, dict[str, str]]]:
    root = ET.parse(path).getroot()
    version = f"FIX.{root.get('major')}.{root.get('minor')}".encode()
    fields = root.find("fields")
    if root.tag != "fix" or version not in {b"FIX.4.2", b"FIX.4.4"} or fields is None:
        raise ValueError("expected a QuickFIX XML dictionary for FIX 4.2 or 4.4")
    names = {}
    enums = {}
    for entry in fields.findall("field"):
        tag = int(entry.attrib["number"])
        names[tag] = entry.attrib["name"]
        enums[tag] = {value.attrib["enum"]: enum_label(value.attrib["description"])
                      for value in entry.findall("value")}
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
        value = safe_text(raw[:MAX_DISPLAY_VALUE])
        if len(raw) > MAX_DISPLAY_VALUE:
            value += f" [truncated {len(raw) - MAX_DISPLAY_VALUE} bytes]"
        description = (enums.get(tag, {}).get(raw.decode("utf-8", errors="replace"))
                       if len(raw) <= MAX_DISPLAY_VALUE else None)
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


CaptureClock = tuple[datetime, int]


def capture_time(prefix: bytes) -> CaptureClock | None:
    match = CAPTURE_TIME.match(prefix)
    if match is None:
        return None
    date, time, zone = match.groups(default=b"")
    if b"/" in date:
        month, day, year = date.split(b"/")
        date = b"-".join((year, month, day))
    try:
        whole, separator, fraction = time.partition(b".")
        normalized = whole + separator + fraction[:6]
        timestamp = datetime.fromisoformat((date + b"T" + normalized + zone).decode("ascii"))
        remainder = int(fraction[6:9].ljust(3, b"0")) if len(fraction) > 6 else 0
        return timestamp, remainder
    except ValueError:
        return None


@dataclass
class CaptureFilter:
    session: tuple[bytes, bytes] | None = None
    start: CaptureClock | None = None
    stop: CaptureClock | None = None
    saw_session: bool = False
    accepted: int = 0
    rejected_times: Counter = field(default_factory=Counter)
    skipped: bool = False
    subids: tuple[bytes, bytes] | None = None
    locations: tuple[bytes, bytes] | None = None
    max_input_bytes: int = 0
    _last_prefix: bytes | None = field(default=None, init=False, repr=False)
    _last_clock: CaptureClock | None = field(default=None, init=False, repr=False)

    def point(self, source: str, line: int, prefix: bytes):
        if self._last_prefix is prefix:
            clock = self._last_clock
            return CapturePoint(source, line, *clock) if clock is not None else CapturePoint(source, line, None)
        return CapturePoint.from_prefix(source, line, prefix)

    def accepts_identity(self, message: Message, *, boundary=False) -> bool:
        if self.session is None:
            return True
        if self.subids is None and self.locations is None:
            sender = target = None
            for tag, value in message.fields:
                if tag == 49:
                    sender = value
                elif tag == 56:
                    target = value
            return (sender, target) in (self.session, self.session[::-1])
        fields = dict(message.fields)
        directions = (False, True) if self.session[0] == self.session[1] else (
            (fields.get(49), fields.get(56)) != self.session,
        )
        for reverse in directions:
            if (fields.get(49), fields.get(56)) != (self.session[::-1] if reverse else self.session):
                continue
            for pair, tags in ((self.subids, (50, 57)), (self.locations, (142, 143))):
                if pair is None:
                    continue
                observed = tuple(fields.get(tag) for tag in tags)
                expected = pair[::-1] if reverse else pair
                if boundary:
                    if any(actual is not None and actual != value for actual, value in zip(observed, expected)):
                        break
                elif observed != expected:
                    break
            else:
                return True
        return False

    def accepts(self, message: Message, prefix: bytes) -> bool:
        if self.session is None and self.start is None and self.stop is None:
            return True
        if not self.accepts_identity(message):
            return False
        self.saw_session = True
        if self.start is None and self.stop is None:
            self.accepted += 1
            return True
        timestamp = capture_time(prefix)
        self._last_prefix, self._last_clock = prefix, timestamp
        bound = self.start if self.start is not None else self.stop
        if timestamp is None:
            self.rejected_times["missing or unsupported capture timestamps"] += 1
        elif (timestamp[0].tzinfo is None) != (bound[0].tzinfo is None):
            self.rejected_times["timezone convention differs from the filter"] += 1
        elif (self.start is None or timestamp >= self.start) and (
            self.stop is None or timestamp < self.stop
        ):
            self.accepted += 1
            return True
        else:
            self.rejected_times["outside the requested time window"] += 1
        return False

    def explain_empty(self, stream: str) -> None:
        if self.accepted:
            return
        if self.session and not self.saw_session:
            pair = " / ".join(safe_text(comp) for comp in self.session)
            scope = []
            for label, values in (("SubIDs (50/57)", self.subids), ("LocationIDs (142/143)", self.locations)):
                if values:
                    scope.append(f"{label} {' / '.join(safe_text(value) for value in values)}")
            detail = f" with {', '.join(scope)}" if scope else ""
            print_error(f"fixprinter: {stream}: no messages for session {pair}{detail}")
        elif self.rejected_times:
            details = "; ".join(f"{count} {reason}" for reason, count in sorted(self.rejected_times.items()))
            print_error(f"fixprinter: {stream}: time filter excluded all matching records ({details})")


@dataclass(frozen=True)
class CapturePoint:
    source: str
    line: int
    timestamp: datetime | None
    nanosecond_remainder: int = 0

    @classmethod
    def from_prefix(cls, source: str, line: int, prefix: bytes):
        clock = capture_time(prefix)
        return cls(source, line, *clock) if clock is not None else cls(source, line, None)


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
    saw_conflict: bool = False


SESSION_BOUNDARIES = {b"A", b"5"}
TIMED_TYPES = SESSION_BOUNDARIES | {b"D", b"F", b"G", b"8", b"9"}


class LatencyTracker:
    def __init__(self, exchange_facing: bool = False, count_unanswered: bool = False) -> None:
        self.exchange_facing = exchange_facing
        self.pending: OrderedDict[
            tuple[bytes, bytes, bytes, bytes], PendingRequest | None
        ] = OrderedDict()
        self.completed = OrderedDict()
        self.unanswered = Counter() if count_unanswered else None

    def abandon(self, reason: str, session=None) -> None:
        for key in list(self.pending):
            if session is None or (key[0] == session[0] and key[1:3] in (
                session[1:], session[1:][::-1],
            )):
                self.record_unanswered(key, self.pending.pop(key), reason)
        for key in list(self.completed):
            if session is None or (key[0] == session[0] and key[1:3] in (
                session[1:], session[1:][::-1],
            )):
                del self.completed[key]

    def record_unanswered(self, key, request: PendingRequest | None, reason: str) -> None:
        if self.unanswered is not None and not (request and request.saw_conflict):
            kind = request.kind if request else b"?"
            self.unanswered[(*key[:3], kind, reason)] += 1

    def observe(
        self, message: Message, prefix: bytes, source: str, line: int,
        fields: dict[int, bytes] | None = None, point: CapturePoint | None = None,
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
            self.abandon("session ended or restarted", (message.version, sender, target))
            return None

        identifier = fields.get(11)
        if kind in (b"D", b"F", b"G"):
            if identifier:
                key = (message.version, sender, target, identifier)
                self.completed.pop(key, None)
                self.pending[key] = None if key in self.pending else PendingRequest(
                    kind, fields.get(41), point or CapturePoint.from_prefix(source, line, prefix)
                )
                self.pending.move_to_end(key)
                if len(self.pending) > MAX_PENDING:
                    evicted, request = self.pending.popitem(last=False)
                    self.record_unanswered(evicted, request, "request tracking limit reached")
            return None

        execution = fields.get(150)
        key = (message.version, target, sender, identifier)
        pending = self.pending.get(key)
        previous = self.completed.get(key)
        if kind == b"8" and execution == b"8":
            expected_kind = pending.kind if pending else previous[0] if previous else b"D"
            label = {b"D": "Order reject", b"F": "Cancel reject", b"G": "Replace reject"}[expected_kind]
        elif kind == b"8" and execution == b"0":
            label, expected_kind = "Order ack", b"D"
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

        point = point or CapturePoint.from_prefix(source, line, prefix)
        if not identifier:
            return Timing(label, end=point, reason="missing ClOrdID")
        if key not in self.pending:
            if previous is not None:
                reason = "duplicate response" if previous[0] == expected_kind and previous[2] == label else "conflicting reply"
                return Timing(label, previous[1], point, reason=reason)
            return Timing(label, end=point, reason="request not found in this session")
        request = self.pending[key]
        if request is None:
            del self.pending[key]
            return Timing(label, end=point, reason="multiple requests share this ClOrdID")
        if request.kind != expected_kind or (
            expected_kind in (b"F", b"G") and request.original_id and fields.get(41)
            and request.original_id != fields[41]
        ):
            request.saw_conflict = True
            return Timing(label, request.point, point, reason="request and response do not match")

        del self.pending[key]
        self.completed[key] = (expected_kind, request.point, label)
        if len(self.completed) > MAX_PENDING:
            self.completed.popitem(last=False)
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
        micros = (elapsed.days * 86_400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds
        micros += (end_point.nanosecond_remainder - start_point.nanosecond_remainder) // 1_000
        if micros >= 0:
            return Timing(label, start_point, end_point, microseconds=micros)
        reason = "response timestamp precedes request" if label != "Internal routing" else (
            "outbound timestamp precedes inbound timestamp"
        )
    return Timing(label, start_point, end_point, reason=reason)


@dataclass
class InputBudget:
    remaining: int

    def readline(self, stream, size: int) -> bytes:
        data = stream.readline(min(size, self.remaining + 1))
        self.remaining -= len(data)
        if self.remaining < 0:
            raise ValueError("decompressed input exceeds --max-input-mib")
        return data


def records(
    stream, source, *, containing=None, from_line=1, session=None, on_timestamp=None,
    warn_skipped=True, budget=None,
):
    def read_record():
        return (stream.readline(MAX_LINE + 3) if budget is None
                else budget.readline(stream, MAX_LINE + 3))

    line_number = 0
    while record := read_record():
        line_number += 1
        size = len(record)
        if record.endswith(b"\r\n"):
            size -= 2
        elif record.endswith(b"\n"):
            size -= 1
        if size > MAX_LINE:
            while record and not record.endswith(b"\n"):
                record = read_record()
            if warn_skipped:
                print_error(f"{source}:{line_number}: skipped record over 8 MiB")
            yield line_number, b"", None
            continue
        first = None
        if on_timestamp is not None:
            first = START.search(record)
            if first is not None:
                timestamp = capture_time(record[:first.start()].strip())
                if timestamp is not None:
                    on_timestamp(timestamp)
        if line_number < from_line or (containing is not None and containing not in record):
            continue
        if session and (session[0] not in record or session[1] not in record):
            continue
        line = record.rstrip(b"\r\n")
        offset = 0
        start = first if on_timestamp is not None else START.search(line)
        while start is not None:
            message = parse_message(line, start)
            yield line_number, line[offset:start.start()].strip(), message
            offset = max(start.end(), message.end)
            start = START.search(line, offset)


def same_capture(first: str, second: str) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def capture_records(
    paths, *, containing=None, start_index=0, from_line=1, session=None, warn_skipped=True,
    max_input_bytes=0, budget=None,
):
    if budget is None and max_input_bytes:
        budget = InputBudget(max_input_bytes)
    rotated = len(paths) > 1
    if rotated:
        seen = set()
        for path in paths:
            stat = os.stat(path)
            identity = (stat.st_dev, stat.st_ino) if stat.st_ino else os.path.normcase(os.path.realpath(path))
            if identity in seen:
                raise ValueError(f"duplicate rotated capture: {path}")
            seen.add(identity)
    previous = None
    previous_source = None
    for index in range(start_index, len(paths)):
        path = paths[index]
        source = "stdin" if path == "-" else printable_label(path)
        minimum_line = from_line if index == start_index else 1
        first_timestamp = True
        latest = None

        def on_timestamp(timestamp):
            nonlocal first_timestamp, latest
            if first_timestamp and previous is not None:
                if (timestamp[0].tzinfo is None) != (previous[0].tzinfo is None):
                    raise ValueError(f"rotated captures use different timezone conventions: {previous_source}, {source}")
                if timestamp < previous:
                    raise ValueError(f"rotated captures are out of order or overlap: {previous_source}, {source}")
            first_timestamp = False
            latest = timestamp

        options = {
            "containing": containing, "from_line": minimum_line, "session": session,
            "on_timestamp": on_timestamp if rotated else None, "warn_skipped": warn_skipped, "budget": budget,
        }
        if path == "-":
            for row in records(sys.stdin.buffer, source, **options):
                yield index, source, *row
        else:
            with open_capture(path) as stream:
                for row in records(stream, source, **options):
                    yield index, source, *row
        if latest is not None:
            previous, previous_source = latest, source


def is_closed_pipe(exc: OSError) -> bool:
    return isinstance(exc, BrokenPipeError) or (
        os.name == "nt" and exc.errno == errno.EINVAL
    )


def silence(stream) -> None:
    with suppress(OSError, ValueError), open(os.devnull, "wb") as sink:
        os.dup2(sink.fileno(), stream.fileno())


def print(*values, **options):
    try:
        builtins.print(*values, **options)
    except OSError as exc:
        output = options.get("file")
        if output is not None and output is sys.stderr and (is_closed_pipe(exc) or exc.errno == errno.EBADF):
            silence(output)
        elif is_closed_pipe(exc):
            raise BrokenPipeError from exc
        else:
            raise


def print_error(message: str) -> None:
    if sys.stderr is not None:
        print(message, file=sys.stderr)


class CliParser(argparse.ArgumentParser):
    def _print_message(self, message, file=None):
        stream = sys.stderr if file is None else file
        if message and stream is not None:
            print(message, end="", file=stream)


def emit_json(kind: str, **values) -> None:
    print(json.dumps({"schema_version": 1, "type": kind, **values}, default=datetime.isoformat))


def emit_message(
    message, source, line, prefix, timing, tags, identifier, dictionaries, color, json_output,
    point=None,
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
            "message", **asdict(point or CapturePoint.from_prefix(source, line, prefix)),
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


def scan(paths, tags, identifier, dictionaries, color, args, capture_filter):
    tracker = LatencyTracker(args.exchange_facing)
    matched = False
    skipped = False
    for _, source, line, prefix, message in capture_records(paths, session=capture_filter.session, max_input_bytes=capture_filter.max_input_bytes):
        if message is None:
            skipped = True
            tracker.abandon("capture has a skipped record")
            continue
        if not capture_filter.accepts(message, prefix):
            if (not message.issues and message.fields[2][1] in SESSION_BOUNDARIES
                    and capture_filter.accepts_identity(message, boundary=True)):
                tracker.observe(message, prefix, source, line)
            continue
        point = (capture_filter.point(source, line, prefix)
                 if capture_filter._last_prefix is prefix else None)
        timing = tracker.observe(message, prefix, source, line, point=point)
        if any(tag in tags and value == identifier for tag, value in message.fields):
            matched = True
            emit_message(message, source, line, prefix, timing, tags, identifier, dictionaries, color, args.json, point)
    return 2 if skipped else 0 if matched else 1


def open_capture(path: str):
    opener = gzip.open if path.lower().endswith(".gz") else open
    return opener(path, "rb")


@dataclass
class RouteOrder:
    capture_index: int
    point: CapturePoint
    clordid: bytes
    version: bytes
    sender: bytes
    target: bytes
    response: Timing | None = None


def locate_route_order(
    paths: list[str], identifier: bytes, link_tag: int, exchange: bool, capture_filter: CaptureFilter,
    *, budget: InputBudget | None = None,
) -> RouteOrder | str:
    order = None
    tracker = LatencyTracker(exchange_facing=True)
    # Exchange timing also needs session boundaries and requests that may share an outbound ID.
    containing = None if exchange else identifier
    for index, source, line, prefix, message in capture_records(
        paths, containing=containing, session=capture_filter.session,
        max_input_bytes=capture_filter.max_input_bytes, budget=budget,
    ):
        if message is None:
            capture_filter.skipped = True
            tracker.abandon("capture has a skipped record")
            continue
        if message.issues:
            continue
        kind = message.fields[2][1]
        if kind != b"D" and (not exchange or kind not in TIMED_TYPES):
            continue
        if not capture_filter.accepts(message, prefix):
            if exchange and kind in SESSION_BOUNDARIES and capture_filter.accepts_identity(message, boundary=True):
                tracker.observe(message, prefix, source, line)
            continue
        fields = dict(message.fields)
        if fields.get(43) == b"Y" or fields.get(97) == b"Y":
            continue
        point = capture_filter.point(source, line, prefix) if exchange and capture_filter._last_prefix is prefix else None
        timing = tracker.observe(message, prefix, source, line, fields, point) if exchange else None
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
                index, point or capture_filter.point(source, line, prefix), clordid_values[0],
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
    inbound_paths = [args.log, *args.next_log]
    outbound_paths = [args.routing_latency, *args.next_outbound_log]
    inbound = locate_route_order(inbound_paths, inbound_id, 11, False, inbound_filter)
    outbound = locate_route_order(outbound_paths, outbound_key, link_tag, True, outbound_filter)
    found = isinstance(inbound, RouteOrder) and isinstance(outbound, RouteOrder)
    if found:
        if inbound.point.line == outbound.point.line and same_capture(
            inbound_paths[inbound.capture_index], outbound_paths[outbound.capture_index],
        ):
            raise ValueError("inbound and outbound must identify different captured events")
        routing = elapsed_time("Internal routing", inbound.point, outbound.point)
    else:
        routing = Timing("Internal routing", reason="requires one matching order on each leg")
    exchange = None
    incomplete = inbound_filter.skipped or outbound_filter.skipped
    if isinstance(outbound, RouteOrder):
        exchange = outbound.response or Timing(
            "Exchange order response round trip", outbound.point, reason="response not found"
        )
    inbound_filter.explain_empty("inbound log")
    outbound_filter.explain_empty("outbound log")

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
        emit_json("routing", **legs, incomplete=incomplete, timings=[asdict(timing) for timing in timings])
    else:
        if incomplete:
            print("Routing evidence is partial (capture has a skipped record)")
        for timing in timings:
            print(timing.render(color))
    return 2 if incomplete else 0 if found else 1


def legacy_fills_for(args, fields: dict[int, bytes]) -> bool:
    pair = fields.get(49), fields.get(56)
    return args.legacy_fills or any(pair in (scope, scope[::-1]) for scope in args.legacy_fill_sessions)


def last_fill_quantity(fields: dict[int, bytes]) -> Decimal | None:
    try:
        quantity = Decimal(fields.get(32, b"").decode("ascii"))
    except (InvalidOperation, UnicodeDecodeError):
        return None
    return quantity if quantity.is_finite() else None


def classify_fill(version: bytes, fields: dict[int, bytes], legacy_fills: bool) -> tuple[bool, str | None]:
    execution = fields.get(150)
    legacy = execution in (b"1", b"2")
    if fields.get(20, b"0") != b"0" and (version == b"FIX.4.2" or legacy):
        return False, None
    if version == b"FIX.4.4" and legacy and not legacy_fills:
        return False, "legacy FIX 4.4 ExecType; enable --legacy-fills for this venue"
    standard_fill = legacy if version == b"FIX.4.2" else execution == b"F" or legacy
    if not standard_fill and (execution in NON_FILL_EXEC_TYPES or fields.get(39) not in (b"1", b"2")):
        return False, None
    quantity = last_fill_quantity(fields)
    positive = quantity is not None and quantity > 0
    if standard_fill:
        return (True, None) if positive else (False, "missing or invalid last fill quantity")
    if positive:
        return False, "unrecognized ExecType on a fill-like report"
    return False, None


def print_lifecycle(args, identifier, dictionaries, color, capture_filter):
    paths = [args.log, *args.next_log]
    budget = InputBudget(capture_filter.max_input_bytes) if capture_filter.max_input_bytes else None
    root = locate_route_order(paths, identifier, 11, False, capture_filter, budget=budget)
    if isinstance(root, str):
        if args.json:
            emit_json("lifecycle_summary", clordid=safe_text(identifier), reason=root, timings=[])
        else:
            print(f"Lifecycle unavailable: {root}")
        return 2 if capture_filter.skipped else 1

    ids = {identifier}
    order_ids = set()
    tracker = LatencyTracker(args.exchange_facing)
    started = False
    fills = {}
    execution_slots = {}
    retired_executions = set()
    request_refs = {}
    fill_issues = Counter()
    for ordinal, (_, source, line, prefix, message) in enumerate(capture_records(
        paths, start_index=root.capture_index, from_line=root.point.line,
        session=capture_filter.session, warn_skipped=False, budget=budget,
    )):
        if message is None:
            capture_filter.skipped = True
            if started:
                break
            continue
        if message.issues or message.version != root.version:
            continue
        kind = message.fields[2][1]
        if kind not in TIMED_TYPES and kind not in (b"3", b"j"):
            continue
        if kind in SESSION_BOUNDARIES:
            if not capture_filter.accepts_identity(message, boundary=True):
                continue
        elif not capture_filter.accepts(message, prefix):
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
        execution_id, execution_ref = fields.get(17), fields.get(19)
        bust = (kind == b"8" and not forward
                and (fields.get(150) == b"H" or message.version == b"FIX.4.2" and fields.get(20) == b"1"))
        correction = (kind == b"8" and not forward and message.version == b"FIX.4.2"
                      and fields.get(20) == b"2")
        reference = None
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
        elif not forward and kind in (b"3", b"j"):
            ref_kind = fields.get(372)
            if fields.get(45):
                reference = request_refs.get((45, fields[45]))
            if reference is None and kind == b"j" and fields.get(379) in ids:
                reference = request_refs.get((11, fields[379]))
            if reference is None or ref_kind and ref_kind != reference[0]:
                continue
        elif not forward and kind in (b"8", b"9"):
            if (clordid not in ids and orig not in ids and orderid not in order_ids
                    and not ((bust or correction)
                             and (execution_ref in execution_slots or execution_ref in retired_executions))):
                continue
            if kind == b"8" and clordid in ids and fields.get(150) != b"8":
                if orderid and orderid.upper() not in (b"0", b"NONE", b"UNKNOWN", b"N/A"):
                    order_ids.add(orderid)
        else:
            continue

        point = capture_filter.point(source, line, prefix)
        if forward and kind in (b"D", b"F", b"G"):
            if fields.get(34):
                request_refs[(45, fields[34])] = kind, point
            if clordid:
                request_refs[(11, clordid)] = kind, point
        timing = tracker.observe(message, prefix, source, line, fields, point)
        if reference is not None:
            label = "Session reject" if kind == b"3" else "Business reject"
            timing = elapsed_time(label, reference[1], point)
        fill, fill_issue = (classify_fill(message.version, fields, legacy_fills_for(args, fields))
                            if not forward and kind == b"8" and not (bust or correction) else (False, None))
        if bust or correction:
            if execution_ref in execution_slots:
                slot = execution_slots.pop(execution_ref)
                retired_executions.add(execution_ref)
                original_point, original_full = fills[slot]
                if bust:
                    del fills[slot]
                    fill_issue = "referenced execution was busted"
                else:
                    quantity = last_fill_quantity(fields)
                    if quantity is None or quantity < 0 or (quantity > 0 and fields.get(39) not in (b"1", b"2")):
                        del fills[slot]
                        fill_issue = "trade correction has missing or invalid fill evidence"
                    elif quantity == 0:
                        del fills[slot]
                        fill_issue = "trade correction removed the fill"
                    else:
                        filled = fields[39] == b"2"
                        fills[slot] = original_point, (original_full or (ordinal, point)) if filled else None
                        if execution_id:
                            execution_slots[execution_id] = slot
            elif execution_ref not in retired_executions:
                action = "trade bust" if bust else "trade correction"
                fill_issue = f"{action} could not be linked to an observed execution"
        if fill_issue:
            fill_issues[fill_issue] += 1
        if fill_issue and timing is None:
            timing = Timing("Fill timing", root.point, point, reason=fill_issue)
        emit_message(
            message, source, line, prefix, timing, ID_TAGS, identifier, dictionaries, color, args.json, point
        )
        if fill and execution_id not in retired_executions and execution_id not in execution_slots:
            slot = execution_id or point
            fills.setdefault(slot, (point, (ordinal, point) if fields.get(39) == b"2" else None))
            if execution_id:
                execution_slots[execution_id] = slot

    first = next(iter(fills.values()), None)
    _, full = min((event for _, event in fills.values() if event is not None), default=(0, None))
    first_fill = elapsed_time("Time to first fill", root.point, first[0]) if first else None
    full_fill = elapsed_time("Time to full fill", root.point, full) if full else None
    if any(reason in fill_issues for reason in (
        "trade bust could not be linked to an observed execution",
        "trade correction could not be linked to an observed execution",
        "trade correction has missing or invalid fill evidence",
    )):
        first_fill = full_fill = None
    fill_reason = f"fill candidates excluded: {', '.join(fill_issues)}" if fill_issues else None
    if capture_filter.skipped and fill_reason is None:
        fill_reason = "capture has a skipped record"
    timings = [
        first_fill or Timing("Time to first fill", root.point, reason=fill_reason or "no fill seen"),
        full_fill or Timing("Time to full fill", root.point, reason=fill_reason or "no full fill seen"),
    ]
    if args.json:
        emit_json("lifecycle_summary", clordid=safe_text(identifier), incomplete=capture_filter.skipped,
                  timings=[asdict(timing) for timing in timings])
    else:
        if capture_filter.skipped:
            print("Lifecycle evidence is partial (capture has a skipped record)")
        for timing in timings:
            print(timing.render(color))
    return 2 if capture_filter.skipped else 0


def sequence_number(raw: bytes | None) -> int | None:
    if raw and len(raw) <= 9 and raw.isdigit():
        return int(raw) or None
    return None


@dataclass
class SequenceDiagnostics:
    expected: dict[tuple[bytes, bytes, bytes], int] = field(default_factory=dict)
    counts: Counter = field(default_factory=Counter)
    awaiting_reset_reply: set = field(default_factory=set)

    def observe(self, message: Message, fields: dict[int, bytes]):
        kind = fields[35]
        sender, target = fields.get(49), fields.get(56)
        if not sender or not target:
            yield "missing_endpoints", {}
            return
        key = message.version, sender, target
        sequence = sequence_number(fields.get(34))
        replay = fields.get(43) == b"Y" or fields.get(97) == b"Y"
        if replay:
            yield "replay", {"sequence": sequence}
        if sequence is None:
            yield "invalid_sequence", {}
            return

        expected = self.expected.get(key)
        if kind == b"A" and fields.get(141) == b"Y" and not replay:
            reverse = message.version, target, sender
            if key in self.awaiting_reset_reply:
                self.awaiting_reset_reply.remove(key)
            else:
                self.expected.pop(reverse, None)
                self.awaiting_reset_reply.add(reverse)
            self.expected[key] = sequence + 1
            yield "logon_reset", {"sequence": sequence}
            return
        if kind == b"5":
            self.awaiting_reset_reply.discard(key)
            self.awaiting_reset_reply.discard((message.version, target, sender))

        if kind == b"4":
            gap_fill = fields.get(123) == b"Y"
            new_sequence = sequence_number(fields.get(36))
            if new_sequence is None or (gap_fill and new_sequence <= sequence) or (
                expected is not None and new_sequence <= expected
            ):
                yield "ignored_sequence_reset", {"sequence": sequence, "new_sequence": new_sequence}
                return
            if gap_fill and expected is not None and sequence > expected:
                yield "sequence_gap", {"expected": expected, "observed": sequence}
            self.expected[key] = new_sequence
            yield "gap_fill" if gap_fill else "sequence_reset", {
                "sequence": sequence, "new_sequence": new_sequence,
            }
            return

        if replay:
            return
        if expected is not None and sequence != expected:
            event = "sequence_gap" if sequence > expected else "sequence_behind"
            yield event, {"expected": expected, "observed": sequence}
        self.expected[key] = max(sequence + 1, expected or 1)


@dataclass
class LatencyStats:
    values: Counter = field(default_factory=Counter)
    unavailable: Counter = field(default_factory=Counter)
    slowest: list[Timing] = field(default_factory=list)
    approximate: bool = False

    @staticmethod
    def bucket(value: int) -> int:
        shift = max(0, value.bit_length() - 6)
        return (value >> shift) << shift

    def add(self, timing: Timing) -> None:
        if timing.microseconds is None:
            self.unavailable[timing.reason] += 1
            return
        value = timing.microseconds
        if not self.approximate and value not in self.values and len(self.values) >= MAX_EXACT_LATENCY_VALUES:
            buckets = Counter()
            for old_value, count in self.values.items():
                buckets[self.bucket(old_value)] += count
            self.values = buckets
            self.approximate = True
        self.values[self.bucket(value) if self.approximate else value] += 1
        if len(self.slowest) < 3 or timing.microseconds > self.slowest[-1].microseconds:
            self.slowest.append(timing)
            self.slowest.sort(key=lambda item: item.microseconds, reverse=True)
            del self.slowest[3:]

    def percentiles(self, count: int) -> dict[str, int | None]:
        percentiles = (50, 95, 99)
        if not count:
            return {f"p{percentile}": None for percentile in percentiles}
        targets = iter((percentile, (percentile * count + 99) // 100) for percentile in percentiles)
        result = {}
        target = next(targets, None)
        total = 0
        for value, frequency in sorted(self.values.items()):
            total += frequency
            while target and total >= target[1]:
                result[f"p{target[0]}"] = value
                target = next(targets, None)
            if target is None:
                break
        return result


@dataclass
class FillProgress:
    start: CapturePoint
    ambiguous: bool = False
    first_seen: bool = False
    full_seen: bool = False


class FillTracker:
    def __init__(self):
        self.orders = OrderedDict()

    def remember(self, key, progress: FillProgress) -> None:
        existing = self.orders.get(key)
        if existing is not None and existing is not progress:
            existing.ambiguous = progress.ambiguous = True
        self.orders[key] = progress
        self.orders.move_to_end(key)
        if len(self.orders) > MAX_PENDING:
            self.orders.popitem(last=False)

    def observe(self, message: Message, fields: dict[int, bytes], point: CapturePoint, legacy_fills: bool):
        sender, target = fields.get(49), fields.get(56)
        if not sender or not target:
            return
        session = message.version, sender, target
        kind, identifier = fields[35], fields.get(11)
        if kind in SESSION_BOUNDARIES:
            for key in list(self.orders):
                if key[:3] in (session, (message.version, target, sender)):
                    del self.orders[key]
            return
        if kind == b"D" and identifier:
            self.remember((*session, 11, identifier), FillProgress(point))
        elif kind == b"G" and identifier:
            owner = self.orders.get((*session, 11, fields.get(41)))
            if owner is not None:
                self.remember((*session, 11, identifier), owner)
        elif kind == b"8":
            session = message.version, target, sender
            order_id = fields.get(37)
            progress = self.orders.get((*session, 11, identifier))
            if order_id and order_id.upper() not in (b"0", b"NONE", b"UNKNOWN", b"N/A"):
                if progress is None:
                    progress = self.orders.get((*session, 37, order_id))
                elif fields.get(150) != b"8":
                    self.remember((*session, 37, order_id), progress)
            fill, reason = classify_fill(message.version, fields, legacy_fills)
            if reason:
                yield Timing("Fill timing", end=point, reason=reason)
            if not fill:
                return
            if progress is None:
                yield Timing("Fill timing", end=point, reason="original order not found")
                return
            if progress.ambiguous:
                yield Timing("Fill timing", end=point, reason="multiple orders share a session identifier")
                return
            if not progress.first_seen:
                progress.first_seen = True
                yield elapsed_time("Time to first fill", progress.start, point)
            if not progress.full_seen and fields.get(39) == b"2":
                progress.full_seen = True
                yield elapsed_time("Time to full fill", progress.start, point)


def capture_report(args, capture_filter, color: bool) -> int:
    diagnostics = SequenceDiagnostics()
    tracker = LatencyTracker(args.exchange_facing, count_unanswered=True)
    fills = FillTracker()
    stats = defaultdict(LatencyStats)
    request_directions = set()
    unmatched_replies = Counter()
    total = skipped = malformed = 0
    for _, source, line, prefix, message in capture_records(
        [args.log, *args.next_log], session=capture_filter.session, max_input_bytes=capture_filter.max_input_bytes,
    ):
        if message is None:
            skipped += 1
            diagnostics.expected.clear()
            diagnostics.awaiting_reset_reply.clear()
            tracker.abandon("capture has a skipped record")
            fills.orders.clear()
            continue
        if message.issues:
            if capture_filter.accepts(message, prefix):
                malformed += 1
            continue
        kind = message.fields[2][1]
        selected = capture_filter.accepts(message, prefix)
        if selected:
            total += 1
        if not selected and (
            kind not in SESSION_BOUNDARIES or not capture_filter.accepts_identity(message, boundary=True)
        ):
            continue
        fields = dict(message.fields)
        version = message.version.decode("ascii")
        sender = safe_text(fields.get(49, b""))
        target = safe_text(fields.get(56, b""))
        if args.session_diagnostics:
            if not selected:
                continue
            for event, detail in diagnostics.observe(message, fields):
                diagnostics.counts[event] += 1
                if args.json:
                    emit_json("session_event", event=event, version=version, sender=sender,
                              target=target, source=source, line=line, **detail)
                else:
                    values = ", ".join(f"{key}={value}" for key, value in detail.items())
                    print(f"{source}:{line} | {version} {sender} -> {target} | {event} {values}".rstrip())
        else:
            if kind not in TIMED_TYPES or fields.get(43) == b"Y" or fields.get(97) == b"Y":
                continue
            point = capture_filter.point(source, line, prefix)
            if selected and kind in (b"D", b"F", b"G") and sender and target:
                request_directions.add((version, (sender, target)))
            timing = tracker.observe(message, prefix, source, line, fields, point)
            observations = list(fills.observe(message, fields, point, legacy_fills_for(args, fields)))
            if not selected:
                continue
            if timing:
                observations.append(timing)
            for observation in observations:
                key = version, (target, sender), observation.label
                if observation.reason == "request not found in this session":
                    unmatched_replies[key] += 1
                else:
                    stats[key].add(observation)

    if args.session_diagnostics:
        expected = [
            {"version": safe_text(version), "sender": safe_text(sender), "target": safe_text(target),
             "next_sequence": sequence}
            for (version, sender, target), sequence in sorted(diagnostics.expected.items())
        ]
        if args.json:
            emit_json("session_summary", messages=total, skipped=skipped, malformed=malformed,
                      counts=diagnostics.counts, expected_next=expected)
        else:
            counts = ", ".join(f"{kind}={count}" for kind, count in sorted(diagnostics.counts.items()))
            print(f"Session diagnostics: {total} messages, {malformed} malformed, {skipped} skipped records | {counts or 'no events'}")
            for item in expected:
                print(f"  {item['version']} {item['sender']} -> {item['target']} | next expected: {item['next_sequence']}")
    else:
        orphan_count = 0
        for key, count in unmatched_replies.items():
            if key[:2] in request_directions:
                stats[key].unavailable["request not found in this session"] += count
            else:
                orphan_count += count
        unanswered_reason = ("no matching reply in the selected time window"
                             if args.start is not None or args.until is not None
                             else "capture ended before a matching reply")
        tracker.abandon(unanswered_reason)
        for (version, sender, target, kind, reason), count in tracker.unanswered.items():
            request = {b"D": "Order", b"F": "Cancel", b"G": "Replace"}.get(kind, "Ambiguous")
            label = f"Exchange {request.lower()} unanswered" if args.exchange_facing else f"{request} unanswered"
            group = stats[safe_text(version), (safe_text(sender), safe_text(target)), label]
            group.unavailable[reason] += count
        for (version, session, label), group in sorted(stats.items()):
            count = sum(group.values.values())
            percentiles = group.percentiles(count)
            method = "bucketed_nearest_rank" if group.approximate else "exact_nearest_rank"
            sparse = [name for name, minimum in (("p95", 20), ("p99", 100)) if 0 < count < minimum]
            if args.json:
                emit_json(
                    "latency_metric", version=version, session=session, label=label, measured=count,
                    unavailable=dict(group.unavailable), percentiles_microseconds=percentiles,
                    percentile_method=method, sparse_percentiles=sparse, slowest=[asdict(item) for item in group.slowest],
                )
            else:
                rendered = " ".join(
                    f"{key}={format_duration(value, color)}" for key, value in percentiles.items()
                    if value is not None
                )
                print(f"{version} {' -> '.join(session)} | {label} | "
                      f"{count} measured, {sum(group.unavailable.values())} unavailable {rendered}".rstrip())
                if group.approximate:
                    print("  Percentiles are approximate (bounded buckets).")
                if sparse:
                    print(f"  Small sample for {', '.join(sparse)}.")
                for reason, count in sorted(group.unavailable.items()):
                    print(f"  {count} unavailable: {reason}")
                for item in group.slowest:
                    print(f"  {item.render(color)} | {item.start.source}:{item.start.line} -> "
                          f"{item.end.source}:{item.end.line}")
        if args.json:
            emit_json("latency_summary", messages=total, skipped=skipped, malformed=malformed,
                      metrics=len(stats), replies_without_observed_request_direction=orphan_count)
        else:
            print(f"Latency summary: {total} messages, {malformed} malformed, {skipped} skipped records")
            if orphan_count:
                subject = "reply had" if orphan_count == 1 else "replies had"
                print(f"{orphan_count} {subject} no observed requests in the corresponding session direction.")
            if not stats:
                print("No eligible latency observations.")
    return 2 if skipped else 0 if (total if args.session_diagnostics else stats) else 1


def time_bound(text: str) -> CaptureClock:
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
    parser = CliParser(
        description=__doc__,
        epilog=(
            "With one log, order, cancel, and replace response timing is automatic. "
            "When the inbound receive and outbound send are in separate logs, "
            "pass the inbound log as LOG and supply the outbound log with --routing-latency."
        ),
    )
    parser.add_argument("identifier", metavar="ID_OR_LOG",
                        help="identifier to find, or the first capture in a report mode")
    parser.add_argument("log", nargs="?", help="plain or .gz capture, or - for stdin, for order lookup")
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
    parser.add_argument("--next-log", action="append", default=[], metavar="LOG",
                        help="continue in this capture after the main log (repeat oldest to newest)")
    parser.add_argument("--next-outbound-log", action="append", default=[], metavar="LOG",
                        help="continue the outbound capture with --routing-latency (repeat in order)")
    parser.add_argument("--session", nargs=2, metavar=("COMP_ID", "PEER_ID"),
                        help="limit the main log to this CompID pair in either direction")
    parser.add_argument("--outbound-session", nargs=2, metavar=("COMP_ID", "PEER_ID"),
                        help="limit the second log to this pair with --routing-latency")
    for option, dest, detail in (
        ("--session-subids", "session_subids", "match tags 50/57 with --session"),
        ("--session-locations", "session_locations", "match tags 142/143 with --session"),
        ("--outbound-subids", "outbound_subids", "match tags 50/57 with --outbound-session"),
        ("--outbound-locations", "outbound_locations", "match tags 142/143 with --outbound-session"),
    ):
        parser.add_argument(option, dest=dest, nargs=2, metavar=("COMP_VALUE", "PEER_VALUE"), help=detail)
    parser.add_argument("--from", dest="start", type=time_bound, metavar="TIME",
                        help="include capture timestamps at or after TIME (both logs in routing mode)")
    parser.add_argument("--until", type=time_bound, metavar="TIME",
                        help="include capture timestamps before TIME (both logs in routing mode)")
    reports = parser.add_mutually_exclusive_group()
    reports.add_argument("--session-diagnostics", action="store_true",
                         help="check sequence gaps, resets, and replays in LOG without an order ID")
    reports.add_argument("--latency-summary", action="store_true",
                         help="summarize acknowledgement and fill timings in LOG without an order ID")
    parser.add_argument("--lifecycle", action="store_true",
                        help="follow an order from its original ClOrdID in one file and report fill times")
    parser.add_argument("--legacy-fills", action="store_true",
                        help="accept venue-specific FIX 4.4 ExecType 1/2 in lifecycle and latency reports")
    parser.add_argument("--legacy-fill-session", action="append", nargs=2, default=[],
                        metavar=("COMP_ID", "PEER_ID"),
                        help="enable legacy FIX 4.4 fill codes only for this pair; repeatable")
    parser.add_argument("--max-input-mib", type=int, default=0, metavar="MIB",
                        help="limit decompressed bytes read per capture stream, including lifecycle rereads; JSON output is uncapped (0 means unlimited)")
    parser.add_argument("--json", action="store_true", help="write versioned JSON Lines to stdout")
    args = parser.parse_args()
    report = args.session_diagnostics or args.latency_summary
    if args.max_input_mib < 0:
        parser.error("--max-input-mib must be nonnegative")
    if (args.legacy_fills or args.legacy_fill_session) and not (args.lifecycle or args.latency_summary):
        parser.error("legacy fill options require --lifecycle or --latency-summary")
    if (args.session_subids or args.session_locations) and not args.session:
        parser.error("session SubID and LocationID filters require --session")
    if (args.outbound_subids or args.outbound_locations) and not args.outbound_session:
        parser.error("outbound SubID and LocationID filters require --outbound-session")
    if args.session_diagnostics and args.exchange_facing:
        parser.error("--exchange-facing does not apply to --session-diagnostics")
    if report:
        if args.log is not None or args.lifecycle or args.routing_latency or args.tag or args.dictionary:
            parser.error("report modes take one initial capture and no order ID, tag, or dictionary")
        args.log = args.identifier
    elif args.log is None:
        parser.error("order lookup requires an identifier and a log")
    if ((args.log == "-" and args.next_log)
            or "-" in args.next_log or "-" in args.next_outbound_log):
        parser.error("rotated captures require file paths")
    if args.next_outbound_log and not args.routing_latency:
        parser.error("--next-outbound-log requires --routing-latency")
    if args.outbound_session and not args.routing_latency:
        parser.error("--outbound-session requires --routing-latency")
    if args.lifecycle and (args.routing_latency or args.log == "-" or args.tag):
        parser.error("--lifecycle requires a log file and original ClOrdID, without --routing-latency or --tag")
    if args.start and args.until and (
        (args.start[0].tzinfo is None) != (args.until[0].tzinfo is None) or args.start >= args.until
    ):
        parser.error("--from must precede --until and use the same timezone convention")
    if not args.routing_latency and (args.outbound_clordid is not None or args.outbound_link_tag):
        parser.error("--outbound-clordid and --outbound-link-tag require --routing-latency")
    if args.outbound_link_tag in {8, 9, 10, 35}:
        parser.error("--outbound-link-tag must be an order identifier field, not structural tag 8, 9, 10, or 35")
    if args.outbound_clordid == "":
        parser.error("--outbound-clordid requires a nonempty identifier")
    if args.routing_latency:
        if args.tag or args.dictionary or args.log == "-" or args.routing_latency == "-":
            parser.error("--routing-latency uses two log files and an inbound ClOrdID, without --tag or --dictionary")
    forced = any(os.environ.get(name, "0") not in ("", "0") for name in ("CLICOLOR_FORCE", "FORCE_COLOR"))
    color = args.color == "always" or (
        args.color == "auto" and "NO_COLOR" not in os.environ and (sys.stdout.isatty() or forced)
    )
    def byte_pair(values):
        return tuple(value.encode("utf-8") for value in values) if values else None

    args.legacy_fill_sessions = [byte_pair(pair) for pair in args.legacy_fill_session]
    capture_filter = CaptureFilter(
        byte_pair(args.session), args.start, args.until,
        subids=byte_pair(args.session_subids), locations=byte_pair(args.session_locations),
        max_input_bytes=args.max_input_mib * 1024 * 1024,
    )
    outbound_filter = CaptureFilter(
        byte_pair(args.outbound_session), args.start, args.until,
        subids=byte_pair(args.outbound_subids), locations=byte_pair(args.outbound_locations),
        max_input_bytes=args.max_input_mib * 1024 * 1024,
    )
    tags = set(args.tag) if args.tag else ID_TAGS
    identifier = args.identifier.encode("utf-8")
    dictionaries = {}
    custom_versions = set()
    try:
        if report:
            status = capture_report(args, capture_filter, color)
            capture_filter.explain_empty("capture")
            return status
        if args.routing_latency:
            outbound_key = args.outbound_clordid.encode("utf-8") if args.outbound_clordid else identifier
            return print_route(
                args, identifier, outbound_key, color, capture_filter,
                outbound_filter,
            )
        for version, filename in BUILTIN_FILES.items():
            actual_version, names, enums = load_dictionary(DICTIONARIES / filename)
            if version != actual_version:
                raise ValueError(f"unexpected FIX version in {filename}")
            dictionaries[version] = (names, enums)
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
            status = print_lifecycle(args, identifier, dictionaries, color, capture_filter)
        else:
            status = scan([args.log, *args.next_log], tags, identifier, dictionaries, color, args, capture_filter)
        capture_filter.explain_empty("capture")
        return status
    except BrokenPipeError:
        raise
    except (OSError, EOFError, ET.ParseError, ValueError, KeyError, zlib.error) as exc:
        print_error(f"fixprinter: {printable_label(str(exc))}")
        return 2


def run_cli() -> int:
    if sys.stdout is None:
        print_error("fixprinter: stdout is unavailable")
        return 2
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (OSError, ValueError):
        silence(sys.stdout)
        print_error("fixprinter: unable to configure stdout")
        return 2
    try:
        status = main()
        sys.stdout.flush()
    except OSError as exc:
        if not is_closed_pipe(exc) and exc.errno != errno.EBADF:
            raise
        silence(sys.stdout)
        status = 0 if is_closed_pipe(exc) else 2
    if sys.stderr is not None:
        try:
            sys.stderr.flush()
        except OSError as exc:
            if not is_closed_pipe(exc) and exc.errno != errno.EBADF:
                raise
            silence(sys.stderr)
    return status


if __name__ == "__main__":
    sys.exit(run_cli())
