import argparse
import json
import re
import sys
from collections import Counter, deque
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from itertools import chain, islice
from pathlib import Path
from typing import ContextManager, Iterable, TextIO, TypedDict


LOG_LEVELS = ("INFO", "WARNING", "ERROR")
TIMESTAMP_PREFIX = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?[ \t]+"
)


@dataclass
class SeverityCoverage:
    classified: int = 0
    unclassified: int = 0


def count_levels(
    lines: Iterable[str],
    *,
    contains: str = "",
    pattern: re.Pattern[str] | None = None,
    output: TextIO | None = None,
    selected_level: str | None = None,
    level_field: str | None = None,
    coverage: SeverityCoverage | None = None,
) -> dict[str, int]:
    """Count matching lines and optionally write them as they are read."""
    counts = dict.fromkeys(LOG_LEVELS, 0)
    for line_number, line in enumerate(lines, start=1):
        if contains not in line:
            continue
        if pattern is not None and pattern.search(line.rstrip("\r\n")) is None:
            continue
        if level_field is None:
            timestamp = TIMESTAMP_PREFIX.match(line)
            level_start = timestamp.end() if timestamp else 0
            level = None
            for name in LOG_LEVELS:
                if line.startswith(name, level_start):
                    level = name
                    break
        else:
            value = event_fields(line, None).get(level_field)
            level = value.strip().upper() if isinstance(value, str) else None

        if level is None or level not in counts:
            if coverage is not None:
                coverage.unclassified += 1
            continue

        counts[level] += 1
        if coverage is not None:
            coverage.classified += 1
        if output is not None and selected_level in (None, level):
            text = line.rstrip("\r\n")
            print(f"{line_number}: {text}", file=output)
    return counts


def count_errors(lines: Iterable[str]) -> int:
    """Count lines whose severity prefix is ERROR."""
    return count_levels(lines)["ERROR"]


def open_log(log_path: str) -> ContextManager[TextIO]:
    if log_path == "-":
        return nullcontext(sys.stdin)
    return Path(log_path).open(encoding="utf-8")


def compile_pattern(parser: argparse.ArgumentParser, value: str | None) -> re.Pattern[str] | None:
    try:
        return re.compile(value) if value is not None else None
    except re.error as error:
        parser.error(f"invalid --regex pattern: {error}")


def search_log(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="log_summary.py search",
        description="Search any text log and print matching lines with surrounding context.",
        epilog="Supply --contains, --regex, or both. With both, a line must match both filters.",
    )
    parser.add_argument("log_file", help="log file path or - for stdin")
    parser.add_argument("--contains", metavar="TEXT", help="match case-sensitive literal text")
    parser.add_argument("--regex", metavar="PATTERN", help="match a Python regular expression")
    parser.add_argument("--context", type=int, default=0, metavar="N",
                        help="show N lines before and after each match (default: 0)")
    args = parser.parse_args(argv)
    if args.contains is None and args.regex is None:
        parser.error("search requires --contains or --regex")
    if args.context < 0:
        parser.error("--context must be nonnegative")
    pattern = compile_pattern(parser, args.regex)
    contains = args.contains or ""

    previous = deque(maxlen=args.context)
    context_end = 0
    try:
        with open_log(args.log_file) as log_file:
            for number, line in enumerate(log_file, start=1):
                text = line.rstrip("\r\n")
                if contains in text and (pattern is None or pattern.search(text)):
                    for previous_number, previous_text in previous:
                        print(f"{previous_number}: {previous_text}")
                    previous.clear()
                    context_end = number + args.context
                if number <= context_end:
                    print(f"{number}: {text}")
                elif args.context:
                    previous.append((number, text))
    except FileNotFoundError:
        print(f"Log file not found: {args.log_file}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as error:
        print(f"Cannot read log: {error}", file=sys.stderr)
        return 1
    if not context_end:
        print("No matching lines. Check --contains or --regex.", file=sys.stderr)
    return 0


def group_log(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="log_summary.py group",
        description="Rank values extracted from text or JSON Lines logs by frequency.",
        epilog="Text input requires --regex. For JSON Lines, use --format jsonl and a field name.",
    )
    parser.add_argument("log_file", help="log file path or - for stdin")
    parser.add_argument(
        "--format", choices=("text", "jsonl"), default="text",
        help="input format (default: text)",
    )
    parser.add_argument("--by", required=True, metavar="FIELD",
                        help="named regex group or top-level JSON field")
    parser.add_argument("--regex", metavar="PATTERN",
                        help="Python regex with a named capture for text input")
    args = parser.parse_args(argv)

    if args.format == "text" and args.regex is None:
        parser.error("text input requires --regex")
    if args.format == "jsonl" and args.regex is not None:
        parser.error("--regex applies only to text input")
    pattern = compile_pattern(parser, args.regex)
    if pattern is not None and args.by not in pattern.groupindex:
        parser.error(f"--regex must define a named capture group {args.by!r}")

    strings: Counter[str] = Counter()
    encoded_values: Counter[str] = Counter()
    scanned = 0
    grouped = 0
    try:
        with open_log(args.log_file) as log_file:
            for line in log_file:
                scanned += 1
                if pattern is not None:
                    match = pattern.search(line.rstrip("\r\n"))
                    value = match.group(args.by) if match else None
                else:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    value = record.get(args.by) if isinstance(record, dict) else None
                if value is None or value == "" or not isinstance(value, (str, int, float, bool)):
                    continue
                if isinstance(value, str):
                    strings[value] += 1
                else:
                    encoded_values[json.dumps(value)] += 1
                grouped += 1
    except FileNotFoundError:
        print(f"Log file not found: {args.log_file}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as error:
        print(f"Cannot read log: {error}", file=sys.stderr)
        return 1

    # Encode each distinct string once, preserving the report's ordering and escaping.
    encoded_strings = ((json.dumps(value), count) for value, count in strings.items())
    values = chain(encoded_values.items(), encoded_strings)
    print("COUNT  VALUE")
    for value, count in sorted(values, key=lambda item: (-item[1], item[0])):
        print(f"{count:5}  {value}")
    print(f"Scanned: {scanned}  Grouped: {grouped}  Skipped: {scanned - grouped}")
    if scanned and not grouped:
        print(f"No usable values for {args.by!r}. Run inspect to check the field or capture.", file=sys.stderr)
    elif not scanned:
        print("The input is empty.", file=sys.stderr)
    return 0


def add_event_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("log_file", help="log file path or - for stdin")
    parser.add_argument(
        "--format", choices=("text", "jsonl"), default="text",
        help="input format (default: text)",
    )
    parser.add_argument("--key", required=True, help="ID field or named regex capture")
    parser.add_argument("--time", required=True, help="timestamp field or named regex capture")
    parser.add_argument("--regex", help="text pattern capturing the ID and timestamp")
    parser.add_argument("--since", metavar="TIME", help="include events at or after this ISO timestamp")
    parser.add_argument("--until", metavar="TIME", help="include events at or before this ISO timestamp")
    parser.epilog = (
        "Timestamps use ISO 8601. Timestamps without an offset are treated as UTC. "
        "Use --format jsonl for top-level JSON fields, or --regex for text."
    )


def input_pattern(parser: argparse.ArgumentParser, args: argparse.Namespace) -> re.Pattern[str] | None:
    if args.format == "text" and args.regex is None:
        parser.error("text input requires --regex")
    if args.format == "jsonl" and args.regex is not None:
        parser.error("--regex applies only to text input")
    pattern = compile_pattern(parser, args.regex)
    return pattern


def event_pattern(parser: argparse.ArgumentParser, args: argparse.Namespace) -> re.Pattern[str] | None:
    pattern = input_pattern(parser, args)
    if pattern is not None and not {args.key, args.time} <= pattern.groupindex.keys():
        parser.error("--regex must define named captures for --key and --time")
    return pattern


def event_fields(text: str, pattern: re.Pattern[str] | None) -> dict[str, object]:
    if pattern is not None:
        match = pattern.search(text)
        return match.groupdict() if match else {}
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return record if isinstance(record, dict) else {}


def event_id(value: object) -> str | None:
    if isinstance(value, (str, int, float)) and not isinstance(value, bool) and value != "":
        return str(value)
    return None


def event_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def event_window(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> tuple[datetime, datetime]:
    since = event_time(args.since) if args.since is not None else datetime.min.replace(tzinfo=timezone.utc)
    until = event_time(args.until) if args.until is not None else datetime.max.replace(tzinfo=timezone.utc)
    if since is None:
        parser.error("invalid --since timestamp: expected ISO 8601")
    if until is None:
        parser.error("invalid --until timestamp: expected ISO 8601")
    if since > until:
        parser.error("--since must be at or before --until")
    return since, until


def timeline_log(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="log_summary.py timeline",
        description="Show events for one ID in chronological order.",
    )
    add_event_arguments(parser)
    parser.add_argument("--value", required=True, help="ID to match exactly")
    args = parser.parse_args(argv)
    pattern = event_pattern(parser, args)
    since, until = event_window(parser, args)

    events: list[tuple[datetime, str]] = []
    scanned = 0
    skipped = 0
    matching_ids = valid_times = 0
    try:
        with open_log(args.log_file) as log_file:
            for line in log_file:
                scanned += 1
                text = line.rstrip("\r\n")
                fields = event_fields(text, pattern)
                key = event_id(fields.get(args.key))
                if key is None:
                    skipped += 1
                    continue
                if key != args.value:
                    continue

                matching_ids += 1
                utc_time = event_time(fields.get(args.time))
                if utc_time is None:
                    skipped += 1
                    continue
                valid_times += 1
                if since <= utc_time <= until:
                    events.append((utc_time, text))
    except FileNotFoundError:
        print(f"Log file not found: {args.log_file}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as error:
        print(f"Cannot read log: {error}", file=sys.stderr)
        return 1

    for timestamp, text in sorted(events, key=lambda event: event[0]):
        print(f"{timestamp.isoformat(sep=' ', timespec='microseconds')}  {text}")
    print(f"Scanned: {scanned}  Matched: {len(events)}  Skipped: {skipped}")
    if not matching_ids:
        print(f"No events match {args.key!r}={args.value!r}. Check --key and --value.", file=sys.stderr)
    elif not valid_times:
        print(f"No usable timestamps in {args.time!r} for this ID. Run inspect to find timestamp fields.", file=sys.stderr)
    elif not events:
        print("No matching events in the requested window. Check --since or --until.", file=sys.stderr)
    return 0


@dataclass
class EventStats:
    first: datetime
    last: datetime
    count: int = 1


def correlate_log(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="log_summary.py correlate",
        description="Summarize event counts and observed time spans by ID.",
    )
    add_event_arguments(parser)
    parser.add_argument("--sort", choices=("span", "count"), default="span",
                        help="rank IDs by observed span or event count (default: span)")
    args = parser.parse_args(argv)
    pattern = event_pattern(parser, args)
    since, until = event_window(parser, args)

    groups: dict[str, EventStats] = {}
    scanned = 0
    skipped = 0
    try:
        with open_log(args.log_file) as log_file:
            for line in log_file:
                scanned += 1
                fields = event_fields(line.rstrip("\r\n"), pattern)
                key = event_id(fields.get(args.key))
                timestamp = event_time(fields.get(args.time))
                if key is None or timestamp is None:
                    skipped += 1
                    continue

                if not since <= timestamp <= until:
                    continue

                stats = groups.get(key)
                if stats is None:
                    groups[key] = EventStats(timestamp, timestamp)
                else:
                    stats.first = min(stats.first, timestamp)
                    stats.last = max(stats.last, timestamp)
                    stats.count += 1
    except FileNotFoundError:
        print(f"Log file not found: {args.log_file}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as error:
        print(f"Cannot read log: {error}", file=sys.stderr)
        return 1

    # Sort the IDs first to break ties consistently even when input is reordered.
    ranked = sorted(groups)
    if args.sort == "span":
        ranked.sort(key=lambda key: groups[key].last - groups[key].first, reverse=True)
    else:
        ranked.sort(key=lambda key: groups[key].count, reverse=True)

    print("EVENTS  OBSERVED SPAN   FIRST (UTC)                       LAST (UTC)                        ID")
    for key in ranked:
        stats = groups[key]
        span = stats.last - stats.first
        print(
            f"{stats.count:6}  {str(span):14}  "
            f"{stats.first.isoformat(timespec='microseconds')}  "
            f"{stats.last.isoformat(timespec='microseconds')}  {json.dumps(key)}"
        )
    print(f"Scanned: {scanned}  IDs: {len(groups)}  Skipped: {skipped}")
    if scanned and scanned == skipped:
        print(f"No usable events. Run inspect to check the {args.key!r} and {args.time!r} mappings.", file=sys.stderr)
    elif scanned and not groups:
        print("No usable events in the requested window. Check --since or --until.", file=sys.stderr)
    elif not scanned:
        print("The input is empty.", file=sys.stderr)
    return 0


@dataclass
class SignatureStats:
    count: int = 0
    ids: set[str] = field(default_factory=set)
    example_line: int = 0
    example_id: str | None = None

    def record(self, line_number: int, event_key: str | None) -> None:
        self.count += 1
        if event_key is not None:
            self.ids.add(event_key)
        if not self.example_line or (self.example_id is None and event_key is not None):
            self.example_line = line_number
            self.example_id = event_key


@dataclass
class ComparisonWindow:
    start: datetime
    end: datetime
    scanned: int = 0
    used: int = 0
    groups: dict[tuple[str | None, str], SignatureStats] = field(default_factory=dict)

    def rate(self, count: int) -> Fraction:
        duration_us = (self.end - self.start) // timedelta(microseconds=1)
        return Fraction(count * 60_000_000, duration_us)


def comparison_window(
    parser: argparse.ArgumentParser, name: str, bounds: list[str]
) -> ComparisonWindow:
    start, end = map(event_time, bounds)
    if start is None or end is None or start >= end:
        parser.error(f"--{name} requires two ISO 8601 timestamps in ascending order")
    return ComparisonWindow(start, end)


def compare_log(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="log_summary.py compare",
        description="Rank signature changes between baseline and incident windows.",
        epilog="Windows include START and exclude END. The baseline must end by the incident start. Times without offsets use UTC.",
    )
    parser.add_argument("log_file", help="log file path or - for stdin")
    parser.add_argument("--format", choices=("text", "jsonl"), default="text")
    parser.add_argument("--regex", help="named field captures for text input")
    parser.add_argument("--time", required=True, help="timestamp field")
    parser.add_argument("--signature", required=True, help="error code or other stable event field")
    parser.add_argument("--service", help="optional service field")
    parser.add_argument("--id", help="optional request or transaction ID field")
    parser.add_argument("--baseline", nargs=2, metavar=("START", "END"), required=True)
    parser.add_argument("--incident", nargs=2, metavar=("START", "END"), required=True)
    args = parser.parse_args(argv)

    pattern = input_pattern(parser, args)
    if pattern is not None:
        fields = {args.time, args.signature, args.service, args.id} - {None}
        missing = fields - pattern.groupindex.keys()
        if missing:
            parser.error(f"--regex is missing named captures: {', '.join(sorted(missing))}")
    baseline = comparison_window(parser, "baseline", args.baseline)
    incident = comparison_window(parser, "incident", args.incident)
    if baseline.end > incident.start:
        parser.error("the baseline must end at or before the incident starts")

    unknown_time = outside = 0
    try:
        with open_log(args.log_file) as log_file:
            for line_number, line in enumerate(log_file, start=1):
                record = event_fields(line.rstrip("\r\n"), pattern)
                timestamp = event_time(record.get(args.time))
                if timestamp is None:
                    unknown_time += 1
                    continue
                if baseline.start <= timestamp < baseline.end:
                    window = baseline
                elif incident.start <= timestamp < incident.end:
                    window = incident
                else:
                    outside += 1
                    continue

                window.scanned += 1
                signature = event_id(record.get(args.signature))
                if signature is None:
                    continue
                service = event_id(record.get(args.service)) if args.service else None
                event_key = event_id(record.get(args.id)) if args.id else None
                window.used += 1
                group = (service, signature)
                if group not in window.groups:
                    window.groups[group] = SignatureStats()
                window.groups[group].record(line_number, event_key)
    except FileNotFoundError:
        print(f"Log file not found: {args.log_file}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as error:
        print(f"Cannot read log: {error}", file=sys.stderr)
        return 1

    empty = SignatureStats()
    rows = []
    for service, signature in baseline.groups.keys() | incident.groups.keys():
        group = (service, signature)
        before = baseline.groups.get(group, empty)
        after = incident.groups.get(group, empty)
        before_rate = baseline.rate(before.count)
        after_rate = incident.rate(after.count)
        rows.append((after_rate - before_rate, service, signature, before, after, before_rate, after_rate))

    source = "<stdin>" if args.log_file == "-" else args.log_file
    for delta, service, signature, before, after, before_rate, after_rate in sorted(
        rows, key=lambda row: (-row[0], row[1] is not None, row[1] or "", row[2])
    ):
        if not before.count:
            label = "NEW"
        elif delta > 0:
            label = "RISING"
        elif delta < 0:
            label = "FALLING"
        else:
            label = "FLAT"
        details = f"{label} {json.dumps(signature)}"
        if args.service:
            details += f" service={json.dumps(service)}"
        details += (
            f" | events {before.count} -> {after.count}"
            f" | events/min {float(before_rate):.3f} -> {float(after_rate):.3f}"
        )
        if args.id:
            details += f" | observed IDs {len(before.ids)} -> {len(after.ids)}"
        example = after if after.count else before
        details += f" | example {source}:{example.example_line}"
        if example.example_id is not None:
            details += f" id={json.dumps(example.example_id)}"
        print(details)

    for name, window in (("Baseline", baseline), ("Incident", incident)):
        print(f"{name}: {window.used}/{window.scanned} records used")
    print(f"Unassigned (invalid or missing timestamp): {unknown_time}  Outside windows: {outside}")
    if not (unknown_time or baseline.scanned or incident.scanned or outside):
        print("The input is empty.", file=sys.stderr)
    elif unknown_time and not (baseline.scanned or incident.scanned or outside):
        print(f"No usable timestamps in {args.time!r}. Run inspect to find timestamp fields.", file=sys.stderr)
    else:
        for name, window in (("Baseline", baseline), ("Incident", incident)):
            if not window.scanned:
                print(f"{name}: no events in the requested window. Check the time bounds.", file=sys.stderr)
            elif not window.used:
                print(f"{name}: no usable signatures in {args.signature!r}. Check --signature.", file=sys.stderr)
    return 0


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MICROSECOND = timedelta(microseconds=1)
MAX_BUCKETS = 10_000


class TimeBucket(TypedDict):
    start: str
    end: str
    count: int
    events_per_minute: float | None
    partial: bool


def interval_seconds(value: str) -> int:
    match = re.fullmatch(r"([0-9]{1,9})([smhd])", value)
    if match is None or int(match[1]) == 0:
        raise argparse.ArgumentTypeError("use a positive interval such as 30s, 5m, 1h, or 1d")
    return int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]


def bucket_log(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="log_summary.py bucket",
        description="Count observed events in aligned UTC time buckets.",
        epilog="Text requires --regex. Use --signature and --value together. Omitted bounds use observed timestamps. Partial buckets have no rate.",
    )
    parser.add_argument("log_file", help="log file path or - for stdin")
    parser.add_argument("--format", choices=("text", "jsonl"), default="text")
    parser.add_argument("--regex", help="named captures for text input")
    parser.add_argument("--time", required=True, help="timestamp field")
    parser.add_argument("--interval", required=True, type=interval_seconds, help="bucket width, e.g. 30s or 5m")
    parser.add_argument("--since", help="inclusive start of the report (ISO 8601)")
    parser.add_argument("--until", help="exclusive end of the report (ISO 8601)")
    parser.add_argument("--signature", help="field used to select events")
    parser.add_argument("--value", help="exact value to count in the signature field")
    parser.add_argument("--json", action="store_true", help="emit versioned JSON")
    args = parser.parse_args(argv)

    if (args.signature is None) != (args.value is None):
        parser.error("--signature and --value must be used together")
    pattern = input_pattern(parser, args)
    if pattern is not None:
        required_fields = {args.time, args.signature} - {None}
        if not required_fields <= pattern.groupindex.keys():
            parser.error("--regex must capture --time and any --signature field")
    since, until = event_window(parser, args)
    if since >= until:
        parser.error("--since must be before --until")
    start_bound = (since - EPOCH) // MICROSECOND if args.since is not None else None
    end_bound = (until - EPOCH) // MICROSECOND if args.until is not None else None
    width = args.interval * 1_000_000

    first = last = None
    scanned = invalid = outside = filtered = 0
    counts: Counter[int] = Counter()
    try:
        with open_log(args.log_file) as log_file:
            for line in log_file:
                scanned += 1
                fields = event_fields(line.rstrip("\r\n"), pattern)
                timestamp = event_time(fields.get(args.time))
                if timestamp is None:
                    invalid += 1
                    continue
                if timestamp < since or (end_bound is not None and timestamp >= until):
                    outside += 1
                    continue

                tick = (timestamp - EPOCH) // MICROSECOND
                first = tick if first is None else min(first, tick)
                last = tick if last is None else max(last, tick)
                if args.signature is not None and event_id(fields.get(args.signature)) != args.value:
                    filtered += 1
                    continue
                counts[tick // width] += 1
    except FileNotFoundError:
        print(f"Log file not found: {args.log_file}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as error:
        print(f"Cannot read log: {error}", file=sys.stderr)
        return 1

    start = start_bound if start_bound is not None else first
    end = end_bound if end_bound is not None else (last + 1 if last is not None else None)
    buckets: list[TimeBucket] = []
    if start is not None and end is not None:
        first_bucket, last_bucket = start // width, (end - 1) // width
        if last_bucket - first_bucket + 1 > MAX_BUCKETS:
            parser.error(f"report exceeds {MAX_BUCKETS} buckets; narrow the window or use a larger interval")
        try:
            for index in range(first_bucket, last_bucket + 1):
                bucket_start = index * width
                bucket_end = bucket_start + width
                partial = bucket_start < start or bucket_end > end
                count = counts[index]
                buckets.append({
                    "start": (EPOCH + timedelta(microseconds=bucket_start)).isoformat(),
                    "end": (EPOCH + timedelta(microseconds=bucket_end)).isoformat(),
                    "count": count,
                    "events_per_minute": None if partial else count * 60 / args.interval,
                    "partial": partial,
                })
        except OverflowError:
            parser.error("aligned bucket boundaries fall outside the supported timestamp range")

    coverage = {
        "scanned": scanned,
        "counted": counts.total(),
        "invalid_timestamp": invalid,
        "outside_window": outside,
        "filtered": filtered,
    }
    if not scanned:
        print("The input is empty.", file=sys.stderr)
    elif invalid == scanned:
        print(f"No usable timestamps in {args.time!r}. Run inspect to find timestamp fields.", file=sys.stderr)
    elif invalid + outside == scanned:
        print("No usable events in the requested window. Check --since or --until.", file=sys.stderr)
    elif not counts:
        print(f"No events match {args.signature!r}={args.value!r}. Check the signature field and value.", file=sys.stderr)

    if args.json:
        print(json.dumps({
            "schema_version": 1,
            "interval_seconds": args.interval,
            "signature": args.signature,
            "value": args.value,
            "coverage": coverage,
            "buckets": buckets,
        }))
    else:
        print("START (UTC)                       COUNT  EVENTS/MIN")
        for bucket in buckets:
            rate = bucket["events_per_minute"]
            rate_text = "partial" if rate is None else f"{rate:.3f}"
            print(f'{bucket["start"]:32}  {bucket["count"]:5}  {rate_text}')
        print(
            f"Scanned: {scanned}  Counted: {coverage['counted']}  "
            f"Invalid timestamp: {invalid}  Outside window: {outside}  Filtered: {filtered}"
        )
    return 0


@dataclass
class FieldSample:
    present: int = 0
    timestamps: int = 0
    types: set[str] = field(default_factory=set)


def inspect_log(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="log_summary.py inspect",
        description="Sample a log and suggest field mappings.",
    )
    parser.add_argument("log_file", help="log file path or - for stdin")
    parser.add_argument("--lines", type=int, default=100, help="sample up to this many lines (1-1000)")
    args = parser.parse_args(argv)
    if not 1 <= args.lines <= 1000:
        parser.error("--lines must be between 1 and 1000")

    sampled = objects = timestamp_prefixes = 0
    fields: dict[str, FieldSample] = {}
    try:
        with open_log(args.log_file) as log_file:
            for line in islice(log_file, args.lines):
                sampled += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    record = None
                if not isinstance(record, dict):
                    if TIMESTAMP_PREFIX.match(line):
                        timestamp_prefixes += 1
                    continue
                objects += 1
                for name, value in record.items():
                    if name not in fields:
                        fields[name] = FieldSample()
                    sample = fields[name]
                    sample.present += 1
                    sample.types.add(type(value).__name__)
                    if isinstance(value, str) and event_time(value) is not None:
                        sample.timestamps += 1
    except FileNotFoundError:
        print(f"Log file not found: {args.log_file}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as error:
        print(f"Cannot read log: {error}", file=sys.stderr)
        return 1

    print(f"Sampled: {sampled}  JSON objects: {objects}  Text timestamp prefixes: {timestamp_prefixes}")
    if not sampled:
        print("The input is empty.", file=sys.stderr)
        return 0
    if not objects:
        print("No JSON objects sampled. For timed text commands, pass --regex with named captures.")
        return 0

    print("Top-level fields (presence across sampled lines):")
    for name, sample in sorted(fields.items()):
        types = ", ".join(sorted(sample.types))
        timestamp_hint = f" | ISO timestamps: {sample.timestamps}" if sample.timestamps else ""
        print(f"  {json.dumps(name)}: {sample.present}/{sampled} | {types}{timestamp_hint}")

    for label, candidates in (
        ("Signature", ("code", "error_code", "signature", "event", "event_name")),
        ("ID", ("request_id", "request", "trace_id", "job_id", "transaction_id", "id")),
        ("Service", ("service", "service_name", "component")),
        ("Severity", ("severity", "level", "log_level", "severity_text")),
    ):
        choices = [
            name for name in sorted(fields)
            if name.lower() in candidates and fields[name].types & {"str", "int", "float"}
        ]
        if choices:
            print(f"{label} candidates (by name): {', '.join(map(json.dumps, choices))}")

    timestamp_fields = sorted(
        (name for name in fields if fields[name].timestamps),
        key=lambda name: (-fields[name].timestamps, name),
    )
    if not timestamp_fields:
        print("No ISO timestamp fields found in the sample. Try --lines with a larger sample.", file=sys.stderr)
        return 0

    print(f"Timestamp candidates: {', '.join(map(json.dumps, timestamp_fields))}")
    if any(re.search(r"[\x00-\x1f\x7f\u2018\u2019]", value)
           for value in (timestamp_fields[0], args.log_file)):
        print("Use bucket -h to pass the selected timestamp field and file manually.")
        return 0
    timestamp = timestamp_fields[0].replace("'", "''")
    source = args.log_file.replace("'", "''")
    print("Try in PowerShell:")
    options = f"--format jsonl --time='{timestamp}' --interval 30s"
    if args.log_file.startswith("-") and args.log_file != "-":
        print(f"  python log_summary.py bucket {options} -- '{source}'")
    else:
        print(f"  python log_summary.py bucket '{source}' {options}")
    if args.log_file == "-":
        print("Pipe the input into the suggested command again.")
    return 0


def main() -> int:
    if sys.argv[1:2] == ["search"]:
        return search_log(sys.argv[2:])
    if sys.argv[1:2] == ["group"]:
        return group_log(sys.argv[2:])
    if sys.argv[1:2] == ["timeline"]:
        return timeline_log(sys.argv[2:])
    if sys.argv[1:2] == ["correlate"]:
        return correlate_log(sys.argv[2:])
    if sys.argv[1:2] == ["compare"]:
        return compare_log(sys.argv[2:])
    if sys.argv[1:2] == ["bucket"]:
        return bucket_log(sys.argv[2:])
    if sys.argv[1:2] == ["inspect"]:
        return inspect_log(sys.argv[2:])

    parser = argparse.ArgumentParser(
        description="Count INFO, WARNING, and ERROR entries in text or JSON Lines.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Commands (add -h after a command for its options):\n"
            "  search      Find matching lines and surrounding context\n"
            "  group       Rank extracted values by frequency\n"
            "  timeline    Reconstruct events for one ID\n"
            "  correlate   Rank IDs by event count or observed span\n"
            "  compare     Compare incident and baseline event rates\n"
            "  bucket      Show observed event rates over time\n"
            "  inspect     Sample input and suggest field mappings\n\n"
            "Examples:\n"
            "  python log_summary.py                 Count levels in app.log\n"
            "  python log_summary.py app.jsonl --format jsonl\n"
            "  python log_summary.py app.log --level ERROR\n"
            "                                        Show only the ERROR count\n"
            '  python log_summary.py app.log --level ERROR --contains "timed out"\n'
            "                                        Count ERROR lines containing timed out\n"
            '  python log_summary.py app.log --level ERROR --contains "timed out" --show-lines\n'
            "                                        Print matching lines with line numbers\n"
            "  Get-Content app.log | python log_summary.py - --level ERROR\n"
            "                                        Read from a PowerShell pipeline\n"
            '  python log_summary.py app.log --regex "timed out|lookup failed"'
        ),
    )
    parser.add_argument(
        "log_file",
        nargs="?",
        default=str(Path(__file__).with_name("app.log")),
        help="log file path or - for stdin (default: app.log beside this script)",
    )
    parser.add_argument(
        "--level",
        choices=LOG_LEVELS,
        help="show the count for one log level (default: show all levels)",
    )
    parser.add_argument(
        "--contains",
        default="",
        metavar="TEXT",
        help="count only lines containing this case-sensitive literal text",
    )
    parser.add_argument(
        "--show-lines",
        action="store_true",
        help="print matching lines with original line numbers before the counts",
    )
    parser.add_argument(
        "--regex",
        metavar="PATTERN",
        help="count only lines matching this Python regular expression",
    )
    parser.add_argument("--format", choices=("text", "jsonl"), default="text")
    parser.add_argument("--level-field", help="top-level JSONL severity field (default: severity)")
    args = parser.parse_args()
    if args.level_field is not None and args.format != "jsonl":
        parser.error("--level-field requires --format jsonl")
    coverage = SeverityCoverage()
    level_field = None
    if args.format == "jsonl":
        level_field = args.level_field if args.level_field is not None else "severity"
    pattern = compile_pattern(parser, args.regex)
    log_path = args.log_file

    try:
        with open_log(log_path) as log_file:
            counts = count_levels(
                log_file,
                contains=args.contains,
                pattern=pattern,
                output=sys.stdout if args.show_lines else None,
                selected_level=args.level,
                level_field=level_field,
                coverage=coverage,
            )
    except FileNotFoundError:
        print(f"Log file not found: {log_path}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as error:
        print(f"Cannot read log: {error}", file=sys.stderr)
        return 1

    levels = (args.level,) if args.level else LOG_LEVELS
    for level in levels:
        print(f"{level}: {counts[level]}")
    if not coverage.classified and coverage.unclassified:
        print("No severity levels recognized. Run inspect to check the input format and fields.", file=sys.stderr)
        if args.format == "text" and Path(log_path).suffix.lower() in {".jsonl", ".ndjson"}:
            print("For JSON Lines, try --format jsonl and, if needed, --level-field.", file=sys.stderr)
    elif not coverage.classified:
        print("No lines to classify. Check the input and text filters.", file=sys.stderr)
    elif args.level and not counts[args.level]:
        print(f"No {args.level} entries matched.", file=sys.stderr)
    if args.format == "jsonl" or coverage.unclassified:
        print(
            f"Classified: {coverage.classified}  Unclassified: {coverage.unclassified}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
