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
LOG_FORMATS = ("text", "jsonl", "logfmt")
TIMESTAMP_PREFIX = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?"
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
    input_format: str = "jsonl",
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
            value = event_fields(line, None, input_format).get(level_field)
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


def stdin_lines() -> Iterable[str]:
    first = sys.stdin.readline()
    if first:
        yield first.removeprefix("\ufeff")
        yield from sys.stdin


def open_log(log_path: str) -> ContextManager[Iterable[str]]:
    if log_path == "-":
        return nullcontext(stdin_lines())
    return Path(log_path).open(encoding="utf-8-sig")


def report_input_error(path: str, error: OSError | UnicodeError) -> int:
    message = f"Log file not found: {path}" if isinstance(error, FileNotFoundError) else f"Cannot read log: {error}"
    print(message, file=sys.stderr)
    return 1


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
    number = 0
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
    except (OSError, UnicodeError) as error:
        return report_input_error(args.log_file, error)
    if not number:
        print("The input is empty.", file=sys.stderr)
    elif not context_end:
        print("No matching lines. Check --contains or --regex.", file=sys.stderr)
    return 0


def positive_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if count < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return count


def group_log(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="log_summary.py group",
        description="Rank values extracted from text or JSON Lines logs by frequency.",
        epilog="Use a field from JSON Lines or logfmt, or a named regex capture for text.",
    )
    parser.add_argument("log_file", help="log file path or - for stdin")
    parser.add_argument(
        "--format", choices=LOG_FORMATS,
        help="input format (default: text)",
    )
    parser.add_argument("--profile", metavar="FILE", help="JSON log mapping")
    parser.add_argument("--by", metavar="FIELD",
                        help="named regex group or top-level JSON field")
    parser.add_argument("--regex", metavar="PATTERN",
                        help="Python regex with a named capture for text input")
    parser.add_argument("--top", type=positive_count, metavar="N", help="show only the first N ranked rows")
    args = parser.parse_args(argv)

    apply_log_mapping(parser, args, required=("by",))
    pattern = input_pattern(parser, args)
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
                    value = event_fields(line, None, args.format).get(args.by)
                if value is None or value == "" or not isinstance(value, (str, int, float, bool)):
                    continue
                if isinstance(value, str):
                    strings[value] += 1
                else:
                    encoded_values[json.dumps(value)] += 1
                grouped += 1
    except (OSError, UnicodeError) as error:
        return report_input_error(args.log_file, error)

    # Encode each distinct string once, preserving the report's ordering and escaping.
    encoded_strings = ((json.dumps(value), count) for value, count in strings.items())
    values = chain(encoded_values.items(), encoded_strings)
    print("COUNT  VALUE")
    ranked = sorted(values, key=lambda item: (-item[1], item[0]))
    for value, count in ranked[:args.top]:
        print(f"{count:5}  {value}")
    if args.top is not None:
        print(f"Showing {min(len(ranked), args.top)} of {len(ranked)} values")
    print(f"Scanned: {scanned}  Grouped: {grouped}  Skipped: {scanned - grouped}")
    if scanned and not grouped:
        print(f"No usable values for {args.by!r}. Run inspect to check the field or capture.", file=sys.stderr)
    elif not scanned:
        print("The input is empty.", file=sys.stderr)
    return 0


def add_mapping_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", metavar="FILE", help="JSON log mapping; explicit flags override it")
    parser.add_argument("--format", choices=LOG_FORMATS, help="input format (default: text)")
    parser.add_argument("--time", help="timestamp field or named regex capture")
    parser.add_argument("--regex", help="named field captures for text input")


def apply_log_mapping(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    *,
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
) -> None:
    profile = {}
    if args.profile is not None:
        try:
            profile = json.loads(Path(args.profile).read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError) as error:
            parser.error(f"cannot load profile {args.profile!r}: {error}")
        allowed = {"format", "time", "regex", "signature", "service", "id", "by", "level_field"}
        if not isinstance(profile, dict):
            parser.error("profile must be a JSON object")
        for name, value in profile.items():
            if name not in allowed:
                parser.error(f"unknown profile field {name!r}")
            if not isinstance(value, str) or not value.strip():
                parser.error(f"profile field {name!r} must be a nonempty string")
        if "format" in profile and profile["format"] not in LOG_FORMATS:
            parser.error("profile format must be text, jsonl, or logfmt")
        if profile.get("format", "text") != "text" and "regex" in profile:
            parser.error("profile regex applies only to text input")

    if args.format is None:
        args.format = profile.get("format", "text")
    if args.regex is None and args.format == "text":
        args.regex = profile.get("regex")

    for name in required + optional:
        if getattr(args, name, None) is None:
            setattr(args, name, profile.get("id" if name == "key" else name))
    missing = [f"--{name}" for name in required if getattr(args, name, None) is None]
    if missing:
        parser.error(f"missing {', '.join(missing)}; supply flags or a --profile")


def add_window_arguments(
    parser: argparse.ArgumentParser, *, exclusive_until: bool = False
) -> None:
    parser.add_argument("--since", metavar="TIME", help="inclusive start (ISO 8601)")
    end = parser.add_mutually_exclusive_group()
    end.add_argument("--before", metavar="TIME", help="exclusive end (ISO 8601)")
    until_help = "alias for --before" if exclusive_until else "inclusive end (legacy behavior)"
    end.add_argument("--until", metavar="TIME", help=until_help)


def add_event_arguments(parser: argparse.ArgumentParser, *, optional_file: bool = False) -> None:
    parser.add_argument("log_file", nargs="?" if optional_file else None,
                        help="log file path or - for stdin")
    add_mapping_arguments(parser)
    parser.add_argument("--key", help="ID field or named regex capture (profile: id)")
    add_window_arguments(parser)
    parser.epilog = (
        "Timestamps use ISO 8601. Timestamps without an offset are treated as UTC. "
        "Use --format jsonl or logfmt for fields, or --regex for text."
    )


def input_pattern(parser: argparse.ArgumentParser, args: argparse.Namespace) -> re.Pattern[str] | None:
    if args.format == "text" and args.regex is None:
        parser.error("text input requires --regex")
    if args.format != "text" and args.regex is not None:
        parser.error("--regex applies only to text input")
    pattern = compile_pattern(parser, args.regex)
    return pattern


def event_pattern(parser: argparse.ArgumentParser, args: argparse.Namespace) -> re.Pattern[str] | None:
    pattern = input_pattern(parser, args)
    if pattern is not None and not {args.key, args.time} <= pattern.groupindex.keys():
        parser.error("--regex must define named captures for --key and --time")
    return pattern


LOGFMT_PAIR = re.compile(
    r'[ \t]*([^=\s"]+)=(?:"((?:[^"\\]|\\.)*)"|([^\s"]*))(?=\s|$)'
)
LOGFMT_LEVEL = re.compile(r"(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL)\s+", re.IGNORECASE)


def logfmt_fields(text: str) -> dict[str, object]:
    fields: dict[str, object] = {}
    position = 0
    timestamp = TIMESTAMP_PREFIX.match(text)
    if timestamp:
        fields["timestamp"] = timestamp[0].rstrip()
        position = timestamp.end()
    level = LOGFMT_LEVEL.match(text, position)
    if level:
        fields["level"] = level[1]
        position = level.end()

    while pair := LOGFMT_PAIR.match(text, position):
        name, quoted, bare = pair.groups()
        try:
            fields[name] = json.loads(f'"{quoted}"') if quoted is not None else bare
        except json.JSONDecodeError:
            break
        position = pair.end()
    return fields


def event_fields(
    text: str, pattern: re.Pattern[str] | None, input_format: str = "jsonl"
) -> dict[str, object]:
    if input_format == "logfmt":
        return logfmt_fields(text)
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
    parser: argparse.ArgumentParser, args: argparse.Namespace, *, exclusive_until: bool = False
) -> tuple[datetime, datetime, bool]:
    since = event_time(args.since) if args.since is not None else datetime.min.replace(tzinfo=timezone.utc)
    end_flag = "--before" if args.before is not None else "--until"
    end_value = args.before if args.before is not None else args.until
    until = event_time(end_value) if end_value is not None else datetime.max.replace(tzinfo=timezone.utc)
    exclusive_end = args.before is not None or (exclusive_until and args.until is not None)
    if since is None:
        parser.error("invalid --since timestamp: expected ISO 8601")
    if until is None:
        parser.error(f"invalid {end_flag} timestamp: expected ISO 8601")
    if exclusive_end and since >= until:
        parser.error(f"--since must be before {end_flag}")
    if since > until:
        parser.error(f"--since must be at or before {end_flag}")
    return since, until, exclusive_end


@dataclass
class TimelineSource:
    path: str
    key: str
    time: str
    pattern: re.Pattern[str] | None
    input_format: str
    scanned: int = 0
    skipped: int = 0
    matching_ids: int = 0
    valid_times: int = 0
    matched: int = 0

    @property
    def name(self) -> str:
        return "<stdin>" if self.path == "-" else self.path


@dataclass
class TimelineEvent:
    timestamp: datetime
    source: str
    line: int
    text: str


def timeline_log(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="log_summary.py timeline",
        description="Show events for one ID in chronological order.",
    )
    add_event_arguments(parser, optional_file=True)
    parser.add_argument("--source", action="append", nargs=2, metavar=("FILE", "PROFILE"),
                        help="source and its JSON profile; repeat for multiple sources")
    parser.add_argument("--value", required=True, help="ID to match exactly")
    parser.add_argument("--json", action="store_true", help="emit versioned JSON")
    args = parser.parse_args(argv)
    since, until, exclusive_end = event_window(parser, args)

    if args.source:
        if args.log_file is not None or any(
            value is not None for value in (args.profile, args.format, args.key, args.time, args.regex)
        ):
            parser.error("--source replaces the positional file and global mapping options")
        if sum(path == "-" for path, _ in args.source) > 1:
            parser.error("stdin can only be used as one source")
        mappings = [
            (path, argparse.Namespace(profile=profile, format=None, key=None, time=None, regex=None))
            for path, profile in args.source
        ]
    elif args.log_file is not None:
        mappings = [(args.log_file, args)]
    else:
        parser.error("supply a log file or --source FILE PROFILE")

    sources: list[TimelineSource] = []
    for path, mapping in mappings:
        apply_log_mapping(parser, mapping, required=("key", "time"))
        pattern = event_pattern(parser, mapping)
        sources.append(TimelineSource(path, mapping.key, mapping.time, pattern, mapping.format))

    events: list[TimelineEvent] = []
    for source in sources:
        try:
            with open_log(source.path) as log_file:
                for line_number, line in enumerate(log_file, start=1):
                    source.scanned += 1
                    text = line.rstrip("\r\n")
                    fields = event_fields(text, source.pattern, source.input_format)
                    key = event_id(fields.get(source.key))
                    if key is None:
                        source.skipped += 1
                        continue
                    if key != args.value:
                        continue

                    source.matching_ids += 1
                    utc_time = event_time(fields.get(source.time))
                    if utc_time is None:
                        source.skipped += 1
                        continue
                    source.valid_times += 1
                    if since <= utc_time <= until and (not exclusive_end or utc_time != until):
                        events.append(TimelineEvent(utc_time, source.name, line_number, text))
                        source.matched += 1
        except FileNotFoundError:
            print(f"Log file not found: {source.path}", file=sys.stderr)
            return 1
        except (OSError, UnicodeError) as error:
            print(f"Cannot read log {source.path!r}: {error}", file=sys.stderr)
            return 1

    events.sort(key=lambda event: event.timestamp)
    scanned = sum(source.scanned for source in sources)
    skipped = sum(source.skipped for source in sources)
    source_coverage = [
        {"source": source.name, "scanned": source.scanned,
         "matched": source.matched, "skipped": source.skipped}
        for source in sources
    ]
    if args.json:
        print(json.dumps({
            "schema_version": 1,
            "command": "timeline",
            "value": args.value,
            "coverage": {"scanned": scanned, "matched": len(events), "skipped": skipped,
                         "sources": source_coverage},
            "events": [
                {"timestamp": event.timestamp.isoformat(timespec="microseconds"),
                 "source": event.source, "line": event.line, "text": event.text}
                for event in events
            ],
        }))
    else:
        for event in events:
            print(f"{event.timestamp.isoformat(sep=' ', timespec='microseconds')}  "
                  f"{event.source}:{event.line}  {event.text}")
        if len(sources) > 1:
            for source in sources:
                print(f"{source.name}: scanned {source.scanned}, matched {source.matched}, skipped {source.skipped}")
        print(f"Scanned: {scanned}  Matched: {len(events)}  Skipped: {skipped}")

    if not any(source.matching_ids for source in sources):
        print(f"No events match ID {args.value!r}. Check the ID field and --value.", file=sys.stderr)
    elif not any(source.valid_times for source in sources):
        fields = ", ".join(sorted({repr(source.time) for source in sources}))
        print(f"No usable timestamps in {fields} for this ID. Run inspect to find timestamp fields.", file=sys.stderr)
    elif not events:
        print("No matching events in the requested window. Check the time bounds.", file=sys.stderr)
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
    parser.add_argument("--top", type=positive_count, metavar="N", help="show only the first N ranked rows")
    args = parser.parse_args(argv)
    apply_log_mapping(parser, args, required=("key", "time"))
    pattern = event_pattern(parser, args)
    since, until, exclusive_end = event_window(parser, args)

    groups: dict[str, EventStats] = {}
    scanned = 0
    skipped = 0
    try:
        with open_log(args.log_file) as log_file:
            for line in log_file:
                scanned += 1
                fields = event_fields(line.rstrip("\r\n"), pattern, args.format)
                key = event_id(fields.get(args.key))
                timestamp = event_time(fields.get(args.time))
                if key is None or timestamp is None:
                    skipped += 1
                    continue

                if not since <= timestamp <= until or (exclusive_end and timestamp == until):
                    continue

                stats = groups.get(key)
                if stats is None:
                    groups[key] = EventStats(timestamp, timestamp)
                else:
                    stats.first = min(stats.first, timestamp)
                    stats.last = max(stats.last, timestamp)
                    stats.count += 1
    except (OSError, UnicodeError) as error:
        return report_input_error(args.log_file, error)

    # Sort the IDs first to break ties consistently even when input is reordered.
    ranked = sorted(groups)
    if args.sort == "span":
        ranked.sort(key=lambda key: groups[key].last - groups[key].first, reverse=True)
    else:
        ranked.sort(key=lambda key: groups[key].count, reverse=True)

    print("EVENTS  OBSERVED SPAN   FIRST (UTC)                       LAST (UTC)                        ID")
    for key in ranked[:args.top]:
        stats = groups[key]
        span = stats.last - stats.first
        print(
            f"{stats.count:6}  {str(span):14}  "
            f"{stats.first.isoformat(timespec='microseconds')}  "
            f"{stats.last.isoformat(timespec='microseconds')}  {json.dumps(key)}"
        )
    if args.top is not None:
        print(f"Showing {min(len(ranked), args.top)} of {len(ranked)} IDs")
    print(f"Scanned: {scanned}  IDs: {len(groups)}  Skipped: {skipped}")
    if scanned and scanned == skipped:
        print(f"No usable events. Run inspect to check the {args.key!r} and {args.time!r} mappings.", file=sys.stderr)
    elif scanned and not groups:
        print("No usable events in the requested window. Check the time bounds.", file=sys.stderr)
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
    add_mapping_arguments(parser)
    parser.add_argument("--signature", help="error code or other stable event field")
    parser.add_argument("--service", help="optional service field")
    parser.add_argument("--id", help="optional request or transaction ID field")
    parser.add_argument("--baseline", nargs=2, metavar=("START", "END"), required=True)
    parser.add_argument("--incident", nargs=2, metavar=("START", "END"), required=True)
    parser.add_argument("--top", type=positive_count, metavar="N", help="show only the first N ranked rows")
    parser.add_argument("--json", action="store_true", help="emit versioned JSON")
    args = parser.parse_args(argv)
    apply_log_mapping(parser, args, required=("time", "signature"), optional=("service", "id"))

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
                record = event_fields(line.rstrip("\r\n"), pattern, args.format)
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
    except (OSError, UnicodeError) as error:
        return report_input_error(args.log_file, error)

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
    ranked = sorted(rows, key=lambda row: (-row[0], row[1] is not None, row[1] or "", row[2]))
    results = []
    for delta, service, signature, before, after, before_rate, after_rate in ranked[:args.top]:
        if not before.count:
            label = "NEW"
        elif delta > 0:
            label = "RISING"
        elif delta < 0:
            label = "FALLING"
        else:
            label = "FLAT"
        example = after if after.count else before
        if args.json:
            results.append({
                "change": label,
                "signature": signature,
                "service": service,
                "baseline": {"events": before.count, "events_per_minute": float(before_rate),
                             "observed_ids": len(before.ids) if args.id else None},
                "incident": {"events": after.count, "events_per_minute": float(after_rate),
                             "observed_ids": len(after.ids) if args.id else None},
                "delta_events_per_minute": float(delta),
                "example": {"source": source, "line": example.example_line, "id": example.example_id},
            })
        else:
            details = f"{label} {json.dumps(signature)}"
            if args.service:
                details += f" service={json.dumps(service)}"
            details += (
                f" | events {before.count} -> {after.count}"
                f" | events/min {float(before_rate):.3f} -> {float(after_rate):.3f}"
            )
            if args.id:
                details += f" | observed IDs {len(before.ids)} -> {len(after.ids)}"
            details += f" | example {source}:{example.example_line}"
            if example.example_id is not None:
                details += f" id={json.dumps(example.example_id)}"
            print(details)

    if args.json:
        print(json.dumps({
            "schema_version": 1,
            "command": "compare",
            "source": "<stdin>" if args.log_file == "-" else args.log_file,
            "total_rows": len(ranked),
            "coverage": {
                "baseline": {"start": baseline.start.isoformat(), "end": baseline.end.isoformat(),
                             "scanned": baseline.scanned, "used": baseline.used},
                "incident": {"start": incident.start.isoformat(), "end": incident.end.isoformat(),
                             "scanned": incident.scanned, "used": incident.used},
                "invalid_timestamp": unknown_time,
                "outside_windows": outside,
            },
            "results": results,
        }))
    else:
        if args.top is not None:
            print(f"Showing {min(len(ranked), args.top)} of {len(ranked)} signatures")
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
        epilog="Text requires --regex. Choose --value with a signature field from --signature or --profile. Omitted bounds use observed timestamps. Partial buckets have no rate.",
    )
    parser.add_argument("log_file", help="log file path or - for stdin")
    add_mapping_arguments(parser)
    parser.add_argument("--interval", required=True, type=interval_seconds, help="bucket width, e.g. 30s or 5m")
    add_window_arguments(parser, exclusive_until=True)
    parser.add_argument("--signature", help="field used to select events")
    parser.add_argument("--value", help="exact value to count in the signature field")
    parser.add_argument("--ignore-case", action="store_true", help="match signature values case-insensitively")
    parser.add_argument("--json", action="store_true", help="emit versioned JSON")
    args = parser.parse_args(argv)
    apply_log_mapping(
        parser, args, required=("time",),
        optional=("signature",) if args.value is not None else (),
    )

    if args.value is not None and args.signature is None:
        parser.error("--value requires a signature field from --signature or --profile")
    if args.signature is not None and args.value is None:
        parser.error("--signature requires --value")
    pattern = input_pattern(parser, args)
    if pattern is not None:
        required_fields = {args.time, args.signature} - {None}
        if not required_fields <= pattern.groupindex.keys():
            parser.error("--regex must capture --time and any --signature field")
    since, until, _ = event_window(parser, args, exclusive_until=True)
    start_bound = (since - EPOCH) // MICROSECOND if args.since is not None else None
    end_bound = (until - EPOCH) // MICROSECOND if args.before is not None or args.until is not None else None
    width = args.interval * 1_000_000
    limit_message = f"report exceeds {MAX_BUCKETS} buckets; narrow the window or use a larger interval"
    if start_bound is not None and end_bound is not None:
        if (end_bound - 1) // width - start_bound // width + 1 > MAX_BUCKETS:
            parser.error(limit_message)
    if args.ignore_case and args.value is None:
        parser.error("--ignore-case requires --value")
    wanted = args.value.casefold() if args.ignore_case else args.value

    first = last = None
    scanned = invalid = outside = filtered = 0
    counts: Counter[int] = Counter()
    try:
        with open_log(args.log_file) as log_file:
            for line in log_file:
                scanned += 1
                fields = event_fields(line.rstrip("\r\n"), pattern, args.format)
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
                if args.signature is not None:
                    value = event_id(fields.get(args.signature))
                    if value is not None and args.ignore_case:
                        value = value.casefold()
                    if value != wanted:
                        filtered += 1
                        continue
                counts[tick // width] += 1
                if len(counts) > MAX_BUCKETS:
                    parser.error(limit_message)
    except (OSError, UnicodeError) as error:
        return report_input_error(args.log_file, error)

    start = start_bound if start_bound is not None else first
    end = end_bound if end_bound is not None else (last + 1 if last is not None else None)
    buckets: list[TimeBucket] = []
    if start is not None and end is not None:
        first_bucket, last_bucket = start // width, (end - 1) // width
        if last_bucket - first_bucket + 1 > MAX_BUCKETS:
            parser.error(limit_message)
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
        print("No usable events in the requested window. Check the time bounds.", file=sys.stderr)
    elif not counts:
        print(f"No events match {args.signature!r}={args.value!r}. Check the signature field and value.", file=sys.stderr)

    if args.json:
        print(json.dumps({
            "schema_version": 1,
            "command": "bucket",
            "source": "<stdin>" if args.log_file == "-" else args.log_file,
            "interval_seconds": args.interval,
            "signature": args.signature,
            "value": args.value,
            "coverage": coverage,
            "buckets": buckets,
        }))
    else:
        title = f"Interval: {args.interval}s"
        if args.signature is not None:
            title += f"  Filter: {args.signature}={args.value!r}"
            if args.ignore_case:
                title += " (case-insensitive)"
        print(title)
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
    parser.add_argument("--profile-out", metavar="FILE", help="save a suggested profile to a new file")
    args = parser.parse_args(argv)
    if not 1 <= args.lines <= 1000:
        parser.error("--lines must be between 1 and 1000")

    sampled = objects = timestamp_prefixes = 0
    logfmt_lines = 0
    json_fields: dict[str, FieldSample] = {}
    native_fields: dict[str, FieldSample] = {}
    try:
        with open_log(args.log_file) as log_file:
            for line in islice(log_file, args.lines):
                sampled += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    record = None
                if isinstance(record, dict):
                    objects += 1
                    fields = json_fields
                else:
                    if TIMESTAMP_PREFIX.match(line):
                        timestamp_prefixes += 1
                    record = logfmt_fields(line)
                    if not record.keys() - {"timestamp", "level"}:
                        continue
                    logfmt_lines += 1
                    fields = native_fields
                for name, value in record.items():
                    if name not in fields:
                        fields[name] = FieldSample()
                    sample = fields[name]
                    sample.present += 1
                    sample.types.add(type(value).__name__)
                    if isinstance(value, str) and event_time(value) is not None:
                        sample.timestamps += 1
    except (OSError, UnicodeError) as error:
        return report_input_error(args.log_file, error)

    print(f"Sampled: {sampled}  JSON objects: {objects}  Text timestamp prefixes: {timestamp_prefixes}")
    if not sampled:
        if args.profile_out:
            parser.error("no structured fields to save in a profile")
        print("The input is empty.", file=sys.stderr)
        return 0
    if not (objects or logfmt_lines):
        print("No JSON objects or key-value records sampled. Timed text commands need named regex captures.")
        if args.profile_out:
            parser.error("no structured fields to save in a profile")
        return 0

    input_format = "jsonl" if objects >= logfmt_lines else "logfmt"
    fields = json_fields if input_format == "jsonl" else native_fields
    print(f"Suggested format: {input_format}")
    if logfmt_lines:
        print(f"Key-value records: {logfmt_lines}")
    profile = {"format": input_format}

    print("Top-level fields (presence across sampled lines):")
    for name, sample in sorted(fields.items()):
        types = ", ".join(sorted(sample.types))
        timestamp_hint = f" | ISO timestamps: {sample.timestamps}" if sample.timestamps else ""
        print(f"  {json.dumps(name)}: {sample.present}/{sampled} | {types}{timestamp_hint}")

    for role, label, candidates in (
        ("signature", "Signature", ("code", "error_code", "signature", "event", "event_name")),
        ("id", "ID", ("request_id", "request", "trace_id", "job_id", "job", "transaction_id", "id")),
        ("service", "Service", ("service", "service_name", "component")),
        ("level_field", "Severity", ("severity", "level", "log_level", "severity_text")),
    ):
        choices = [
            name for name in sorted(fields)
            if name.lower() in candidates and fields[name].types & {"str", "int", "float"}
        ]
        if choices:
            profile[role] = max(choices, key=lambda name: fields[name].present)
            print(f"{label} candidates (by name): {', '.join(map(json.dumps, choices))}")

    timestamp_fields = sorted(
        (name for name in fields if fields[name].timestamps),
        key=lambda name: (-fields[name].timestamps, name),
    )
    if timestamp_fields:
        profile["time"] = timestamp_fields[0]
    if args.profile_out:
        try:
            with Path(args.profile_out).open("x", encoding="utf-8", newline="\n") as destination:
                json.dump(profile, destination, indent=2)
                destination.write("\n")
        except OSError as error:
            print(f"Cannot save profile {args.profile_out!r}: {error}", file=sys.stderr)
            return 1
        print(f"Saved suggested profile: {args.profile_out!r}", file=sys.stderr)
        return 0
    if not timestamp_fields:
        print("No ISO timestamp fields found in the sample. Try --lines with a larger sample.", file=sys.stderr)
        return 0

    print(f"Timestamp candidates: {', '.join(map(json.dumps, timestamp_fields))}")
    if any(re.search(r"[\x00-\x1f\x7f\u2018\u2019]", value)
           for value in (timestamp_fields[0], args.log_file)):
        print("Use bucket -h to pass the selected timestamp field and file manually.")
        return 0
    if "level_field" in profile:
        severity = profile["level_field"]
        # Use a simple field token for the displayed example.
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", severity):
            print(f"Severity summary options: --format {input_format} --level-field {severity}")
    timestamp = timestamp_fields[0].replace("'", "''")
    source = args.log_file.replace("'", "''")
    print("Try in PowerShell:")
    options = f"--format {input_format} --time='{timestamp}' --interval 30s"
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
        description="Count INFO, WARNING, and ERROR entries in text, JSON Lines, or logfmt.",
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
    parser.add_argument("--profile", metavar="FILE", help="JSON log mapping")
    parser.add_argument("--format", choices=LOG_FORMATS)
    parser.add_argument("--level-field", help="severity field (JSONL: severity; logfmt: level)")
    parser.add_argument("--json", action="store_true", help="emit versioned severity counts as JSON")
    args = parser.parse_args()
    if args.json and args.show_lines:
        parser.error("--json cannot be combined with --show-lines")
    regex_flag = args.regex
    apply_log_mapping(parser, args, required=(), optional=("level_field",))
    # Summary --regex filters whole lines; the profile regex maps fields for other commands.
    args.regex = regex_flag
    if args.level_field is not None and args.format == "text":
        parser.error("--level-field requires --format jsonl or logfmt")
    coverage = SeverityCoverage()
    level_field = None
    if args.format != "text":
        default_level = "severity" if args.format == "jsonl" else "level"
        level_field = args.level_field if args.level_field is not None else default_level
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
                input_format=args.format,
                coverage=coverage,
            )
    except (OSError, UnicodeError) as error:
        return report_input_error(log_path, error)

    levels = (args.level,) if args.level else LOG_LEVELS
    if args.json:
        print(json.dumps({
            "schema_version": 1,
            "command": "summary",
            "source": "<stdin>" if log_path == "-" else log_path,
            "counts": {level: counts[level] for level in levels},
            "coverage": {"classified": coverage.classified, "unclassified": coverage.unclassified},
        }))
    else:
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
    if args.format != "text" or coverage.unclassified:
        print(
            f"Classified: {coverage.classified}  Unclassified: {coverage.unclassified}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
