import argparse
import gzip
import json
import re
import shutil
import sys
from collections import Counter, deque
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from glob import glob
from io import TextIOWrapper
from itertools import chain, islice
from math import isfinite
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import Iterable, Iterator, TextIO, TypedDict


LOG_LEVELS = ("INFO", "WARNING", "ERROR")
LEVEL_ALIASES = {"WARN": "WARNING"}
LOG_FORMATS = ("text", "jsonl", "logfmt")
TIME_FORMATS = ("iso", "epoch_s", "epoch_ms")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MICROSECOND = timedelta(microseconds=1)
INSPECT_EPOCH_START = datetime(2000, 1, 1, tzinfo=timezone.utc)
INSPECT_EPOCH_END = datetime(2100, 1, 1, tzinfo=timezone.utc)
EPOCH_FIELD_NAME = re.compile(
    r"(?:^|_)(?:ts|time|timestamp)(?:_(?:ms|s|millis|seconds))?$"
    r"|^(?:created|updated|logged|observed|received)_at$",
    re.IGNORECASE,
)
TIMESTAMP_PREFIX = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?[ \t]+"
)


def log_records(
    lines: Iterable[str], *, multiline: bool = False
) -> Iterator[tuple[int, int, str, str]]:
    """Yield physical start/end lines, the header, and the complete record."""
    if not multiline:
        for number, line in enumerate(lines, start=1):
            yield number, number, line, line
        return

    pending: list[str] = []
    start = last = 0
    for number, line in enumerate(lines, start=1):
        if pending and not line.startswith((" ", "\t")):
            yield start, last, pending[0], "".join(pending)
            pending = []
        if not pending:
            start = number
        pending.append(line)
        last = number
    if pending:
        yield start, last, pending[0], "".join(pending)


def line_range(start: int, end: int) -> str:
    return str(start) if start == end else f"{start}-{end}"


def add_multiline_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--multiline", action="store_true",
                        help="attach indented continuation lines to the preceding record")


@dataclass
class SeverityCoverage:
    classified: int = 0
    unclassified: int = 0


def normalize_level(value: str, levels: Iterable[str]) -> str:
    level = value.strip().upper()
    return level if level in levels else LEVEL_ALIASES.get(level, level)


def count_levels(
    lines: Iterable[str],
    *,
    contains: str = "",
    pattern: re.Pattern[str] | None = None,
    output: TextIO | None = None,
    selected_level: str | None = None,
    level_field: str | None = None,
    level_pattern: re.Pattern[str] | None = None,
    input_format: str = "jsonl",
    levels: tuple[str, ...] = LOG_LEVELS,
    coverage: SeverityCoverage | None = None,
    multiline: bool = False,
) -> dict[str, int]:
    """Count matching lines and optionally write them as they are read."""
    counts = dict.fromkeys(levels, 0)
    prefixes = LOG_LEVELS if levels == LOG_LEVELS else sorted(levels, key=len, reverse=True)
    aliases = {alias: level for alias, level in LEVEL_ALIASES.items()
               if alias not in counts and level in counts}
    records = log_records(lines, multiline=multiline)
    for line_number, end_line, header, line in records:
        if contains not in line:
            continue
        if pattern is not None and pattern.search(line.rstrip("\r\n")) is None:
            continue
        if level_field is None:
            timestamp = TIMESTAMP_PREFIX.match(header)
            level_start = timestamp.end() if timestamp else 0
            level = None
            for name in prefixes:
                if header.startswith(name, level_start):
                    level = name
                    break
            if level is None and aliases:
                for alias, canonical in aliases.items():
                    end = level_start + len(alias)
                    if header.startswith(alias, level_start) and (len(header) == end or header[end].isspace()):
                        level = canonical
                        break
        else:
            fields = event_fields(header.rstrip("\r\n"), level_pattern, input_format)
            value = log_field(fields, level_field)
            level = normalize_level(value, counts) if isinstance(value, str) else None

        if level is None or level not in counts:
            if coverage is not None:
                coverage.unclassified += 1
            continue

        counts[level] += 1
        if coverage is not None:
            coverage.classified += 1
        if output is not None and selected_level in (None, level):
            text = line.rstrip("\r\n")
            print(f"{line_range(line_number, end_line)}: {text}", file=output)
    return counts


def count_errors(lines: Iterable[str]) -> int:
    """Count lines whose severity prefix is ERROR."""
    return count_levels(lines)["ERROR"]


def stdin_lines() -> Iterable[str]:
    if isinstance(sys.stdin, TextIOWrapper) and not sys.stdin.isatty():
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    first = sys.stdin.readline()
    if first:
        yield first.removeprefix("\ufeff")
        yield from sys.stdin


@contextmanager
def open_log(log_path: str) -> Iterator[Iterable[str]]:
    if log_path == "-":
        yield stdin_lines()
        return
    if Path(log_path).suffix.lower() == ".gz":
        try:
            with gzip.open(log_path, "rt", encoding="utf-8-sig") as log_file:
                yield log_file
        except EOFError as error:
            raise OSError(f"{log_path}: truncated gzip stream") from error
        return
    with Path(log_path).open(encoding="utf-8-sig") as log_file:
        yield log_file


def report_input_error(path: str, error: OSError | UnicodeError) -> int:
    message = f"Log file not found: {path}" if isinstance(error, FileNotFoundError) else f"Cannot read log: {error}"
    print(message, file=sys.stderr)
    return 1


def compile_pattern(parser: argparse.ArgumentParser, value: str | None) -> re.Pattern[str] | None:
    try:
        return re.compile(value) if value is not None else None
    except re.error as error:
        parser.error(f"invalid --regex pattern: {error}")


def add_report_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit versioned JSON")
    parser.add_argument("--fail-on", choices=("empty",), help="exit 3 when the result is empty")


def report_status(args: argparse.Namespace, *, empty: bool) -> int:
    return 3 if empty and args.fail_on == "empty" else 0


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
                        help="show N lines (records with --multiline) before and after each match")
    add_multiline_option(parser)
    add_report_arguments(parser)
    args = parser.parse_args(argv)
    if args.contains is None and args.regex is None:
        parser.error("search requires --contains or --regex")
    if args.context < 0:
        parser.error("--context must be nonnegative")
    pattern = compile_pattern(parser, args.regex)
    contains = args.contains or ""

    previous: deque[tuple[int, int, str]] = deque(maxlen=args.context)
    context_end = 0
    number = physical_lines = matched = displayed = 0
    with (
        SpooledTemporaryFile(max_size=1_048_576, mode="w+t", encoding="utf-8")
        if args.json else nullcontext()
    ) as report:
        def write_record(line_number: int, end_line: int, text: str) -> None:
            nonlocal displayed
            if report is None:
                if line_number == end_line:
                    print(f"{line_number}: {text}")
                else:
                    print(f"{line_number}-{end_line}: {text}")
            else:
                if displayed:
                    report.write(",")
                row = {"line": line_number, "text": text}
                if args.multiline:
                    row["end_line"] = end_line
                report.write(json.dumps(row))
            displayed += 1

        try:
            with open_log(args.log_file) as log_file:
                records = log_records(log_file, multiline=args.multiline)
                for number, (line_number, end_line, _, line) in enumerate(records, start=1):
                    physical_lines = end_line
                    text = line.rstrip("\r\n")
                    if contains in text and (pattern is None or pattern.search(text)):
                        matched += 1
                        for context_record in previous:
                            write_record(*context_record)
                        previous.clear()
                        context_end = number + args.context
                    if number <= context_end:
                        write_record(line_number, end_line, text)
                    elif args.context:
                        previous.append((line_number, end_line, text))
        except (OSError, UnicodeError) as error:
            return report_input_error(args.log_file, error)
        if report is not None:
            coverage = {"scanned": number, "matched": matched, "displayed": displayed}
            if args.multiline:
                coverage["physical_lines"] = physical_lines
            header = {
                "schema_version": 1, "command": "search", "source": source_name(args.log_file),
                "coverage": coverage,
            }
            print("{", end="")
            for key, value in header.items():
                print(f"{json.dumps(key)}: {json.dumps(value)}, ", end="")
            print('"lines": [', end="")
            report.seek(0)
            shutil.copyfileobj(report, sys.stdout)
            print("]}")
    if not number:
        print("The input is empty.", file=sys.stderr)
    elif not context_end:
        unit = "records" if args.multiline else "lines"
        print(f"No matching {unit}. Check --contains or --regex.", file=sys.stderr)
    return report_status(args, empty=not matched)


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
    add_multiline_option(parser)
    add_report_arguments(parser)
    args = parser.parse_args(argv)

    apply_log_mapping(parser, args, required=("by",))
    pattern = input_pattern(parser, args)
    if pattern is not None and args.by not in pattern.groupindex:
        parser.error(f"--regex must define a named capture group {args.by!r}")

    strings: Counter[str] = Counter()
    encoded_values: Counter[str] = Counter()
    scanned = physical_lines = 0
    multiline = args.multiline and args.format != "jsonl"
    grouped = 0
    try:
        with open_log(args.log_file) as log_file:
            for _, physical_lines, line, _ in log_records(log_file, multiline=multiline):
                scanned += 1
                if pattern is not None:
                    match = pattern.search(line.rstrip("\r\n"))
                    value = match.group(args.by) if match else None
                else:
                    value = log_field(event_fields(line, None, args.format), args.by)
                if value is None or value == "" or not isinstance(value, (str, int, float, bool)):
                    continue
                if isinstance(value, float) and not isfinite(value):
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
    ranked = sorted(values, key=lambda item: (-item[1], item[0]))
    displayed = ranked[:args.top]
    coverage = {"scanned": scanned, "grouped": grouped, "skipped": scanned - grouped}
    if args.multiline:
        coverage["physical_lines"] = physical_lines
    if args.json:
        print(json.dumps({
            "schema_version": 1,
            "command": "group",
            "source": source_name(args.log_file),
            "field": args.by,
            "total_rows": len(ranked),
            "coverage": coverage,
            "results": [{"value": json.loads(value), "count": count} for value, count in displayed],
        }, allow_nan=False))
    else:
        print("COUNT  VALUE")
        for value, count in displayed:
            print(f"{count:5}  {value}")
        if args.top is not None:
            print(f"Showing {len(displayed)} of {len(ranked)} values")
        suffix = f"  Physical lines: {physical_lines}" if args.multiline else ""
        print(f"Scanned: {scanned}  Grouped: {grouped}  Skipped: {scanned - grouped}{suffix}")
    if scanned and not grouped:
        print(f"No usable values for {args.by!r}. Run inspect to check the field or capture.", file=sys.stderr)
    elif not scanned:
        print("The input is empty.", file=sys.stderr)
    return report_status(args, empty=not grouped)


def add_mapping_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", metavar="FILE", help="JSON log mapping; explicit flags override it")
    parser.add_argument("--format", choices=LOG_FORMATS, help="input format (default: text)")
    parser.add_argument("--time", help="timestamp field or named regex capture")
    parser.add_argument("--time-format", choices=TIME_FORMATS, help="record timestamp format (default: iso)")
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
        allowed = {"format", "time", "regex", "signature", "service", "id", "key", "by", "level_field", "time_format"}
        if not isinstance(profile, dict):
            parser.error("profile must be a JSON object")
        for name, value in profile.items():
            if name not in allowed:
                parser.error(f"unknown profile field {name!r}")
            if not isinstance(value, str) or not value.strip():
                parser.error(f"profile field {name!r} must be a nonempty string")
        if "format" in profile and profile["format"] not in LOG_FORMATS:
            parser.error("profile format must be text, jsonl, or logfmt")
        if "time_format" in profile and profile["time_format"] not in TIME_FORMATS:
            parser.error("profile time_format must be iso, epoch_s, or epoch_ms")
        if profile.get("format", "text") != "text" and "regex" in profile:
            parser.error("profile regex applies only to text input")
        if "key" in profile:
            if "id" in profile and profile["id"] != profile["key"]:
                parser.error("profile fields id and key must agree")
            profile["id"] = profile["key"]

    if args.format is None:
        args.format = profile.get("format", "text")
    if args.regex is None and args.format == "text":
        args.regex = profile.get("regex")

    if hasattr(args, "time_format") and args.time_format is None:
        args.time_format = profile.get("time_format", "iso")

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


def add_source_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", action="append", nargs=2, metavar=("FILE", "PROFILE"),
                        help="file or glob and its JSON profile; repeat for multiple sources")


def source_mappings(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    *,
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
) -> list[tuple[str, argparse.Namespace]]:
    fields = ("profile", "format", "regex", "time_format", *required, *optional)
    if args.source:
        if args.log_file is not None or any(getattr(args, name) is not None for name in fields):
            parser.error("--source replaces the positional file and global mapping options")
        if sum(path == "-" for path, _ in args.source) > 1:
            parser.error("stdin can only be used as one source")
        mappings: list[tuple[str, argparse.Namespace]] = []
        for path, profile in args.source:
            expanded = [path]
            if path != "-" and not Path(path).is_file() and any(char in path for char in "*?["):
                expanded = sorted(match for match in glob(path, recursive=True) if Path(match).is_file())
                if not expanded:
                    parser.error(f"no source files match {path!r}")
            mappings.extend(
                (filename, argparse.Namespace(profile=profile, **dict.fromkeys(fields[1:])))
                for filename in expanded
            )
    elif args.log_file is not None:
        mappings = [(args.log_file, args)]
    else:
        parser.error("supply a log file or --source FILE PROFILE")

    for _, mapping in mappings:
        apply_log_mapping(parser, mapping, required=required, optional=optional)
    return mappings


def source_name(path: str) -> str:
    return "<stdin>" if path == "-" else path


def add_event_arguments(parser: argparse.ArgumentParser, *, optional_file: bool = False) -> None:
    parser.add_argument("log_file", nargs="?" if optional_file else None,
                        help="log file path or - for stdin")
    add_mapping_arguments(parser)
    parser.add_argument("--key", help="ID field or named regex capture (profile: id)")
    add_window_arguments(parser)
    parser.epilog = (
        "Time bounds use ISO 8601. Timestamps without an offset are treated as UTC. "
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


def log_field(record: dict[str, object], name: str) -> object:
    if name in record or "." not in name:
        return record.get(name)
    value: object = record
    for part in name.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def event_id(value: object) -> str | None:
    if isinstance(value, (str, int, float)) and not isinstance(value, bool) and value != "":
        return str(value)
    return None


def epoch_time(value: object, time_format: str) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    # Bound the conversion even for malformed or extreme numeric input.
    if isinstance(value, int) and value.bit_length() > 64:
        return None
    text = str(value).strip()
    if not text or len(text) > 64 or "_" in text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    if number.is_zero():
        return EPOCH
    digits = 6 if time_format == "epoch_s" else 3
    if not -digits <= number.adjusted() <= 17 - digits:
        return None
    numerator, denominator = number.as_integer_ratio()
    micros, remainder = divmod(numerator * 10**digits, denominator)
    if remainder:
        return None
    try:
        return EPOCH + timedelta(microseconds=micros)
    except OverflowError:
        return None


def event_time(value: object, time_format: str = "iso") -> datetime | None:
    if time_format != "iso":
        return epoch_time(value, time_format)
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


@dataclass
class RecordTimeReader:
    source: str
    field: str
    time_format: str
    checked: int = 0
    alternate_matches: int = 0
    inconsistent: bool = False

    @property
    def alternate_format(self) -> str:
        return "epoch_ms" if self.time_format == "epoch_s" else "epoch_s"

    def parse(self, value: object) -> datetime | None:
        timestamp = event_time(value, self.time_format)
        if self.time_format == "iso" or self.inconsistent or self.checked >= 100 or value is None:
            return timestamp
        self.checked += 1
        if timestamp is not None and INSPECT_EPOCH_START <= timestamp < INSPECT_EPOCH_END:
            self.inconsistent = True
            return timestamp
        alternate = epoch_time(value, self.alternate_format)
        if alternate is not None and INSPECT_EPOCH_START <= alternate < INSPECT_EPOCH_END:
            self.alternate_matches += 1
        elif timestamp is not None or alternate is not None:
            self.inconsistent = True
        return timestamp

    def warn_if_unit_suspect(self) -> None:
        if self.alternate_matches and not self.inconsistent:
            print(
                f"Possible epoch unit mismatch in {self.source!r}, field {self.field!r}: "
                f"sampled values look like {self.alternate_format}; using {self.time_format}. "
                "Check --time-format or the profile.",
                file=sys.stderr,
            )


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
    time_format: str
    scanned: int = 0
    skipped: int = 0
    matching_ids: int = 0
    valid_times: int = 0
    matched: int = 0
    outside_window: int = 0
    other_ids: int = 0
    physical_lines: int = 0

    @property
    def name(self) -> str:
        return source_name(self.path)


@dataclass
class TimelineEvent:
    timestamp: datetime
    source: str
    line: int
    end_line: int
    text: str


def timeline_log(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="log_summary.py timeline",
        description="Show events for one ID in chronological order.",
    )
    add_event_arguments(parser, optional_file=True)
    add_source_option(parser)
    parser.add_argument("--value", required=True, help="ID to match exactly")
    add_multiline_option(parser)
    add_report_arguments(parser)
    args = parser.parse_args(argv)
    since, until, exclusive_end = event_window(parser, args)

    mappings = source_mappings(parser, args, required=("key", "time"))

    sources: list[TimelineSource] = []
    for path, mapping in mappings:
        pattern = event_pattern(parser, mapping)
        sources.append(TimelineSource(path, mapping.key, mapping.time, pattern, mapping.format, mapping.time_format))

    events: list[TimelineEvent] = []
    for source in sources:
        times = RecordTimeReader(source.name, source.time, source.time_format)
        parse_time = event_time if times.time_format == "iso" else times.parse
        try:
            with open_log(source.path) as log_file:
                records = log_records(log_file, multiline=args.multiline and source.input_format != "jsonl")
                for line_number, end_line, header, line in records:
                    source.scanned += 1
                    source.physical_lines = end_line
                    fields = event_fields(header.rstrip("\r\n"), source.pattern, source.input_format)
                    key = event_id(log_field(fields, source.key))
                    if key is None:
                        source.skipped += 1
                        continue
                    if key != args.value:
                        source.other_ids += 1
                        continue

                    source.matching_ids += 1
                    utc_time = parse_time(log_field(fields, source.time))
                    if utc_time is None:
                        source.skipped += 1
                        continue
                    source.valid_times += 1
                    if since <= utc_time <= until and (not exclusive_end or utc_time != until):
                        events.append(TimelineEvent(
                            utc_time, source.name, line_number, end_line, line.rstrip("\r\n")
                        ))
                        source.matched += 1
                    else:
                        source.outside_window += 1
        except FileNotFoundError:
            print(f"Log file not found: {source.path}", file=sys.stderr)
            return 1
        except (OSError, UnicodeError) as error:
            print(f"Cannot read log {source.path!r}: {error}", file=sys.stderr)
            return 1

        times.warn_if_unit_suspect()

    events.sort(key=lambda event: event.timestamp)
    scanned = sum(source.scanned for source in sources)
    skipped = sum(source.skipped for source in sources)
    outside = sum(source.outside_window for source in sources)
    other_ids = sum(source.other_ids for source in sources)
    source_coverage = [
        {"source": source.name, "scanned": source.scanned,
         "matched": source.matched, "skipped": source.skipped,
         "outside_window": source.outside_window, "other_ids": source.other_ids,
         **({"physical_lines": source.physical_lines} if args.multiline else {})}
        for source in sources
    ]
    physical_lines = sum(source.physical_lines for source in sources)
    if args.json:
        print(json.dumps({
            "schema_version": 1,
            "command": "timeline",
            "value": args.value,
            "coverage": {"scanned": scanned, "matched": len(events), "skipped": skipped,
                         "outside_window": outside, "other_ids": other_ids,
                         "sources": source_coverage,
                         **({"physical_lines": physical_lines} if args.multiline else {})},
            "events": [
                {"timestamp": event.timestamp.isoformat(timespec="microseconds"),
                 "source": event.source, "line": event.line, "text": event.text,
                 **({"end_line": event.end_line} if args.multiline else {})}
                for event in events
            ],
        }))
    else:
        for event in events:
            print(f"{event.timestamp.isoformat(sep=' ', timespec='microseconds')}  "
                  f"{event.source}:{line_range(event.line, event.end_line)}  {event.text}")
        if len(sources) > 1:
            for source in sources:
                suffix = f", physical lines {source.physical_lines}" if args.multiline else ""
                print(f"{source.name}: scanned {source.scanned}, matched {source.matched}, "
                      f"skipped {source.skipped}, outside {source.outside_window}, other IDs {source.other_ids}{suffix}")
        suffix = f"  Physical lines: {physical_lines}" if args.multiline else ""
        print(f"Scanned: {scanned}  Matched: {len(events)}  Skipped: {skipped}  "
              f"Outside window: {outside}  Other IDs: {other_ids}{suffix}")

    if not any(source.matching_ids for source in sources):
        print(f"No events match ID {args.value!r}. Check the ID field and --value.", file=sys.stderr)
    elif not any(source.valid_times for source in sources):
        time_fields = ", ".join(sorted({repr(source.time) for source in sources}))
        print(f"No usable timestamps in {time_fields} for this ID. Run inspect to find timestamp fields.", file=sys.stderr)
    elif not events:
        print("No matching events in the requested window. Check the time bounds.", file=sys.stderr)
    return report_status(args, empty=not events)


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
    add_event_arguments(parser, optional_file=True)
    add_source_option(parser)
    parser.add_argument("--sort", choices=("span", "count"), default="span",
                        help="rank IDs by observed span or event count (default: span)")
    parser.add_argument("--top", type=positive_count, metavar="N", help="show only the first N ranked rows")
    add_report_arguments(parser)
    args = parser.parse_args(argv)
    mappings = source_mappings(parser, args, required=("key", "time"))
    patterns = [event_pattern(parser, mapping) for _, mapping in mappings]
    since, until, exclusive_end = event_window(parser, args)

    groups: dict[str, EventStats] = {}
    include_sources = args.json or len(mappings) > 1
    sources_by_id: dict[str, set[str]] = {}
    coverage: list[dict[str, str | int]] = []
    scanned = used = skipped = outside = 0
    for (path, mapping), pattern in zip(mappings, patterns):
        name = source_name(path)
        times = RecordTimeReader(name, mapping.time, mapping.time_format)
        parse_time = event_time if times.time_format == "iso" else times.parse
        source_scanned = source_skipped = source_used = source_outside = 0
        try:
            with open_log(path) as log_file:
                for line in log_file:
                    source_scanned += 1
                    fields = event_fields(line.rstrip("\r\n"), pattern, mapping.format)
                    key = event_id(log_field(fields, mapping.key))
                    timestamp = parse_time(log_field(fields, mapping.time))
                    if key is None or timestamp is None:
                        source_skipped += 1
                        continue

                    if not since <= timestamp <= until or (exclusive_end and timestamp == until):
                        source_outside += 1
                        continue

                    source_used += 1
                    if include_sources:
                        sources_by_id.setdefault(key, set()).add(name)
                    stats = groups.get(key)
                    if stats is None:
                        groups[key] = EventStats(timestamp, timestamp)
                    else:
                        stats.first = min(stats.first, timestamp)
                        stats.last = max(stats.last, timestamp)
                        stats.count += 1
        except (OSError, UnicodeError) as error:
            return report_input_error(path, error)
        times.warn_if_unit_suspect()
        coverage.append({
            "source": name, "scanned": source_scanned, "used": source_used,
            "skipped": source_skipped, "outside_window": source_outside,
        })
        scanned += source_scanned
        used += source_used
        skipped += source_skipped
        outside += source_outside

    # Sort the IDs first to break ties consistently even when input is reordered.
    ranked = sorted(groups)
    if args.sort == "span":
        ranked.sort(key=lambda key: groups[key].last - groups[key].first, reverse=True)
    else:
        ranked.sort(key=lambda key: groups[key].count, reverse=True)

    if args.json:
        print(json.dumps({
            "schema_version": 1,
            "command": "correlate",
            "sort": args.sort,
            "total_rows": len(ranked),
            "coverage": {
                "scanned": scanned,
                "used": used,
                "skipped": skipped,
                "outside_window": outside,
                "sources": coverage,
            },
            "results": [
                {
                    "id": key,
                    "events": groups[key].count,
                    "first": groups[key].first.isoformat(timespec="microseconds"),
                    "last": groups[key].last.isoformat(timespec="microseconds"),
                    "span_ms": (groups[key].last - groups[key].first) // MICROSECOND / 1000,
                    "sources": sorted(sources_by_id[key]),
                }
                for key in ranked[:args.top]
            ],
        }))
    else:
        heading = "EVENTS  OBSERVED SPAN   FIRST (UTC)                       LAST (UTC)                        ID"
        print(heading + ("  SOURCES" if len(mappings) > 1 else ""))
        for key in ranked[:args.top]:
            stats = groups[key]
            span = stats.last - stats.first
            source_list = json.dumps(sorted(sources_by_id[key])) if len(mappings) > 1 else ""
            print(
                f"{stats.count:6}  {str(span):14}  "
                f"{stats.first.isoformat(timespec='microseconds')}  "
                f"{stats.last.isoformat(timespec='microseconds')}  {json.dumps(key)}"
                + (f"  {source_list}" if source_list else "")
            )
        if args.top is not None:
            print(f"Showing {min(len(ranked), args.top)} of {len(ranked)} IDs")
        if len(coverage) > 1:
            for source in coverage:
                print(f"{source['source']}: scanned {source['scanned']}, used {source['used']}, "
                      f"skipped {source['skipped']}, outside window {source['outside_window']}")
        print(f"Scanned: {scanned}  IDs: {len(groups)}  Skipped: {skipped}  "
              f"Used: {used}  Outside window: {outside}")
    if scanned and scanned == skipped:
        if len(mappings) == 1:
            mapping = mappings[0][1]
            print(f"No usable events. Run inspect to check the {mapping.key!r} and {mapping.time!r} mappings.", file=sys.stderr)
        else:
            print("No usable events. Run inspect to check each source's ID and timestamp mappings.", file=sys.stderr)
    elif scanned and not groups:
        print("No usable events in the requested window. Check the time bounds.", file=sys.stderr)
    elif not scanned:
        print("The input is empty.", file=sys.stderr)
    return report_status(args, empty=not groups)


@dataclass
class SignatureStats:
    count: int = 0
    ids: set[str] = field(default_factory=set)
    example_source: str | None = None
    example_line: int = 0
    example_id: str | None = None

    def record(self, source: str, line_number: int, event_key: str | None) -> None:
        self.count += 1
        if event_key is not None:
            self.ids.add(event_key)
        if not self.example_line or (self.example_id is None and event_key is not None):
            self.example_source = source
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
    parser.add_argument("log_file", nargs="?", help="log file path or - for stdin")
    add_source_option(parser)
    add_mapping_arguments(parser)
    parser.add_argument("--signature", help="error code or other stable event field")
    parser.add_argument("--service", help="optional service field")
    parser.add_argument("--id", help="optional request or transaction ID field")
    parser.add_argument("--baseline", nargs=2, metavar=("START", "END"), required=True)
    parser.add_argument("--incident", nargs=2, metavar=("START", "END"), required=True)
    parser.add_argument("--top", type=positive_count, metavar="N", help="show only the first N ranked rows")
    add_report_arguments(parser)
    args = parser.parse_args(argv)
    mappings = source_mappings(
        parser, args, required=("time", "signature"), optional=("service", "id")
    )
    patterns = []
    for _, mapping in mappings:
        pattern = input_pattern(parser, mapping)
        if pattern is not None:
            fields = {mapping.time, mapping.signature, mapping.service, mapping.id} - {None}
            missing = fields - pattern.groupindex.keys()
            if missing:
                parser.error(f"--regex is missing named captures: {', '.join(sorted(missing))}")
        patterns.append(pattern)
    has_service = any(mapping.service is not None for _, mapping in mappings)
    has_id = any(mapping.id is not None for _, mapping in mappings)
    baseline = comparison_window(parser, "baseline", args.baseline)
    incident = comparison_window(parser, "incident", args.incident)
    if baseline.end > incident.start:
        parser.error("the baseline must end at or before the incident starts")

    coverage: list[dict[str, str | int]] = []
    unknown_time = outside = 0
    for (path, mapping), pattern in zip(mappings, patterns):
        name = source_name(path)
        times = RecordTimeReader(name, mapping.time, mapping.time_format)
        parse_time = event_time if times.time_format == "iso" else times.parse
        source_counts = dict.fromkeys(
            ("scanned", "used", "baseline_scanned", "baseline_used", "incident_scanned",
             "incident_used", "invalid_timestamp", "outside_windows"), 0
        )
        try:
            with open_log(path) as log_file:
                for line_number, line in enumerate(log_file, start=1):
                    source_counts["scanned"] += 1
                    record = event_fields(line.rstrip("\r\n"), pattern, mapping.format)
                    timestamp = parse_time(log_field(record, mapping.time))
                    if timestamp is None:
                        source_counts['invalid_timestamp'] += 1
                        continue
                    if baseline.start <= timestamp < baseline.end:
                        window = baseline
                        window_name = "baseline"
                    elif incident.start <= timestamp < incident.end:
                        window = incident
                        window_name = "incident"
                    else:
                        source_counts['outside_windows'] += 1
                        continue

                    window.scanned += 1
                    source_counts[f"{window_name}_scanned"] += 1
                    signature = event_id(log_field(record, mapping.signature))
                    if signature is None:
                        continue
                    service = event_id(log_field(record, mapping.service)) if mapping.service else None
                    event_key = event_id(log_field(record, mapping.id)) if mapping.id else None
                    window.used += 1
                    source_counts["used"] += 1
                    source_counts[f"{window_name}_used"] += 1
                    group = (service, signature)
                    if group not in window.groups:
                        window.groups[group] = SignatureStats()
                    window.groups[group].record(name, line_number, event_key)
        except (OSError, UnicodeError) as error:
            return report_input_error(path, error)
        times.warn_if_unit_suspect()
        unknown_time += source_counts["invalid_timestamp"]
        outside += source_counts["outside_windows"]
        coverage.append({
            "source": name,
            **source_counts,
            "skipped": source_counts["scanned"] - source_counts["used"] - source_counts["outside_windows"],
        })

    empty = SignatureStats()
    rows = []
    for service, signature in baseline.groups.keys() | incident.groups.keys():
        group = (service, signature)
        before = baseline.groups.get(group, empty)
        after = incident.groups.get(group, empty)
        before_rate = baseline.rate(before.count)
        after_rate = incident.rate(after.count)
        rows.append((after_rate - before_rate, service, signature, before, after, before_rate, after_rate))

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
                             "observed_ids": len(before.ids) if has_id else None},
                "incident": {"events": after.count, "events_per_minute": float(after_rate),
                             "observed_ids": len(after.ids) if has_id else None},
                "delta_events_per_minute": float(delta),
                "example": {"source": example.example_source, "line": example.example_line, "id": example.example_id},
            })
        else:
            details = f"{label} {json.dumps(signature)}"
            if has_service:
                details += f" service={json.dumps(service)}"
            details += (
                f" | events {before.count} -> {after.count}"
                f" | events/min {float(before_rate):.3f} -> {float(after_rate):.3f}"
            )
            if has_id:
                details += f" | observed IDs {len(before.ids)} -> {len(after.ids)}"
            details += f" | example {example.example_source}:{example.example_line}"
            if example.example_id is not None:
                details += f" id={json.dumps(example.example_id)}"
            print(details)

    if args.json:
        print(json.dumps({
            "schema_version": 1,
            "command": "compare",
            "source": coverage[0]["source"] if len(coverage) == 1 else None,
            "total_rows": len(ranked),
            "coverage": {
                "baseline": {"start": baseline.start.isoformat(), "end": baseline.end.isoformat(),
                             "scanned": baseline.scanned, "used": baseline.used},
                "incident": {"start": incident.start.isoformat(), "end": incident.end.isoformat(),
                             "scanned": incident.scanned, "used": incident.used},
                "invalid_timestamp": unknown_time,
                "outside_windows": outside,
                "sources": coverage,
            },
            "results": results,
        }))
    else:
        if args.top is not None:
            print(f"Showing {min(len(ranked), args.top)} of {len(ranked)} signatures")
        for name, window in (("Baseline", baseline), ("Incident", incident)):
            print(f"{name}: {window.used}/{window.scanned} records used")
        if len(coverage) > 1:
            for item in coverage:
                print(f"{item['source']}: scanned {item['scanned']}, used {item['used']}, "
                      f"skipped {item['skipped']}, outside {item['outside_windows']}")
                print(f"  Baseline: {item['baseline_used']}/{item['baseline_scanned']} used  "
                      f"Incident: {item['incident_used']}/{item['incident_scanned']} used")
        print(f"Unassigned (invalid or missing timestamp): {unknown_time}  Outside windows: {outside}")
    if not (unknown_time or baseline.scanned or incident.scanned or outside):
        print("The input is empty.", file=sys.stderr)
    elif unknown_time and not (baseline.scanned or incident.scanned or outside):
        time_fields = ", ".join(sorted({repr(mapping.time) for _, mapping in mappings}))
        print(f"No usable timestamps in {time_fields}. Run inspect to find timestamp fields.", file=sys.stderr)
    else:
        for name, window in (("Baseline", baseline), ("Incident", incident)):
            if not window.scanned:
                print(f"{name}: no events in the requested window. Check the time bounds.", file=sys.stderr)
            elif not window.used:
                signatures = ", ".join(sorted({repr(mapping.signature) for _, mapping in mappings}))
                print(f"{name}: no usable signatures in {signatures}. Check --signature or the per-source profiles.", file=sys.stderr)
    return report_status(args, empty=not ranked)


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
    parser.add_argument("--level", help="filter severity, case-insensitively (WARN aliases WARNING)")
    parser.add_argument("--level-field", help="severity field (JSONL: severity; text or logfmt: level)")
    parser.add_argument("--value", help="exact value to count in the signature field")
    parser.add_argument("--ignore-case", action="store_true", help="match signature values case-insensitively")
    add_report_arguments(parser)
    args = parser.parse_args(argv)
    if args.level is not None and (args.signature is not None or args.value is not None):
        parser.error("--level cannot be combined with --signature or --value")
    if args.level_field is not None and args.level is None:
        parser.error("--level-field requires --level")
    apply_log_mapping(
        parser, args, required=("time",),
        optional=("level_field",) if args.level is not None else (("signature",) if args.value is not None else ()),
    )

    if args.level is not None:
        default_level = "severity" if args.format == "jsonl" else "level"
        args.signature = args.level_field if args.level_field is not None else default_level
        args.value = normalize_level(args.level, LOG_LEVELS)
        args.ignore_case = True
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
    inferred_bounds = start_bound is None or end_bound is None
    if start_bound is not None and end_bound is not None:
        if (end_bound - 1) // width - start_bound // width + 1 > MAX_BUCKETS:
            parser.error(limit_message)
    if args.ignore_case and args.value is None:
        parser.error("--ignore-case requires --value")
    wanted = args.value.casefold() if args.ignore_case else args.value

    first = last = None
    scanned = invalid = outside = filtered = 0
    counts: Counter[int] = Counter()
    times = RecordTimeReader(source_name(args.log_file), args.time, args.time_format)
    parse_time = event_time if times.time_format == "iso" else times.parse
    try:
        with open_log(args.log_file) as log_file:
            for line in log_file:
                scanned += 1
                fields = event_fields(line.rstrip("\r\n"), pattern, args.format)
                timestamp = parse_time(log_field(fields, args.time))
                if timestamp is None:
                    invalid += 1
                    continue
                if timestamp < since or (end_bound is not None and timestamp >= until):
                    outside += 1
                    continue

                tick = (timestamp - EPOCH) // MICROSECOND
                extends_range = first is None or last is None or tick < first or tick > last
                first = tick if first is None else min(first, tick)
                last = tick if last is None else max(last, tick)
                if inferred_bounds and extends_range:
                    start_tick = start_bound if start_bound is not None else first
                    end_tick = end_bound - 1 if end_bound is not None else last
                    if end_tick // width - start_tick // width + 1 > MAX_BUCKETS:
                        parser.error(limit_message)
                if args.signature is not None:
                    value = event_id(log_field(fields, args.signature))
                    if value is not None and args.level is not None:
                        value = normalize_level(value, (args.value,))
                    if value is not None and args.ignore_case:
                        value = value.casefold()
                    if value != wanted:
                        filtered += 1
                        continue
                counts[tick // width] += 1
    except (OSError, UnicodeError) as error:
        return report_input_error(args.log_file, error)
    times.warn_if_unit_suspect()

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
            "level": args.level,
            "ignore_case": args.ignore_case,
            "matched": bool(counts),
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
    return report_status(args, empty=not counts)


@dataclass
class FieldSample:
    present: int = 0
    timestamps: int = 0
    epochs: Counter[str] = field(default_factory=Counter)
    types: set[str] = field(default_factory=set)


def inspect_json_fields(record: dict[str, object]) -> dict[str, object]:
    fields = record.copy()
    pending = [(key, value) for key, value in record.items() if isinstance(value, dict)]
    while pending:
        parent, obj = pending.pop()
        for key, value in obj.items():
            name = f"{parent}.{key}"
            fields.setdefault(name, value)
            if isinstance(value, dict):
                pending.append((name, value))
    return fields


def inspect_log(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="log_summary.py inspect",
        description="Sample a log and suggest field mappings.",
    )
    parser.add_argument("log_file", help="log file path or - for stdin")
    parser.add_argument("--lines", type=int, default=100, help="sample up to this many lines (1-1000)")
    parser.add_argument("--profile-out", metavar="FILE", help="write a new profile when a usable mapping is found")
    add_report_arguments(parser)
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
                    record = inspect_json_fields(record)
                    sample_fields = json_fields
                else:
                    if TIMESTAMP_PREFIX.match(line):
                        timestamp_prefixes += 1
                    record = logfmt_fields(line)
                    if not record.keys() - {"timestamp", "level"}:
                        continue
                    logfmt_lines += 1
                    sample_fields = native_fields
                for name, value in record.items():
                    if name not in sample_fields:
                        sample_fields[name] = FieldSample()
                    sample = sample_fields[name]
                    sample.present += 1
                    sample.types.add(type(value).__name__)
                    if isinstance(value, str) and event_time(value) is not None:
                        sample.timestamps += 1
                    elif EPOCH_FIELD_NAME.search(name.rsplit(".", 1)[-1]):
                        for mode in ("epoch_s", "epoch_ms"):
                            candidate_time = epoch_time(value, mode)
                            if candidate_time is not None and INSPECT_EPOCH_START <= candidate_time < INSPECT_EPOCH_END:
                                sample.epochs[mode] += 1
    except (OSError, UnicodeError) as error:
        return report_input_error(args.log_file, error)

    input_format = None
    fields: dict[str, FieldSample] = {}
    candidates: dict[str, list[str]] = {}
    profile: dict[str, str] = {}
    if objects or logfmt_lines:
        input_format = "jsonl" if objects >= logfmt_lines else "logfmt"
        fields = json_fields if input_format == "jsonl" else native_fields
        profile["format"] = input_format
        for role, names in (
            ("signature", ("code", "error_code", "signature", "event", "event_name")),
            ("id", ("request_id", "request", "trace_id", "job_id", "job", "transaction_id", "id")),
            ("service", ("service", "service_name", "component")),
            ("level_field", ("severity", "level", "log_level", "severity_text")),
        ):
            choices = [
                name for name in sorted(fields)
                if name.rsplit(".", 1)[-1].lower() in names
                and fields[name].types & {"str", "int", "float"}
            ]
            if choices:
                candidates[role] = choices
                profile[role] = max(choices, key=lambda name: fields[name].present)

    time_modes: dict[str, tuple[str, int]] = {}
    for name, sample in fields.items():
        modes = dict(sample.epochs)
        if sample.timestamps:
            modes["iso"] = sample.timestamps
        if len(modes) == 1:
            time_modes[name] = next(iter(modes.items()))
    timestamp_fields = sorted(
        time_modes, key=lambda name: (-time_modes[name][1], name)
    )
    if timestamp_fields:
        candidates["time"] = timestamp_fields
        selected = timestamp_fields[0]
        profile["time"] = selected
        mode = time_modes[selected][0]
        if mode != "iso":
            profile["time_format"] = mode

    if not candidates:
        profile.clear()
    if args.profile_out and not profile and args.fail_on != "empty":
        parser.error("no usable field mapping to save in a profile")
    if args.profile_out and profile:
        try:
            with Path(args.profile_out).open("x", encoding="utf-8", newline="\n") as destination:
                json.dump(profile, destination, indent=2)
                destination.write("\n")
        except OSError as error:
            print(f"Cannot save profile {args.profile_out!r}: {error}", file=sys.stderr)
            return 1
        print(f"Saved suggested profile: {args.profile_out!r}", file=sys.stderr)
        if "time" not in profile:
            print("Profile has no timestamp mapping. Timed commands need --time and, for epoch values, --time-format.",
                  file=sys.stderr)

    if args.json:
        print(json.dumps({
            "schema_version": 1,
            "command": "inspect",
            "source": source_name(args.log_file),
            "coverage": {
                "sampled": sampled, "json_objects": objects, "logfmt_lines": logfmt_lines,
                "text_timestamp_prefixes": timestamp_prefixes,
            },
            "format": input_format,
            "fields": {
                name: {"present": sample.present, "types": sorted(sample.types),
                       "timestamps": sample.timestamps, "epoch_candidates": dict(sample.epochs)}
                for name, sample in sorted(fields.items())
            },
            "candidates": candidates,
            "profile": profile or None,
        }))
    else:
        print(f"Sampled: {sampled}  JSON objects: {objects}  Text timestamp prefixes: {timestamp_prefixes}")
        if input_format is not None:
            print(f"Suggested format: {input_format}")
            if logfmt_lines:
                print(f"Key-value records: {logfmt_lines}")
            print("Fields (presence across sampled lines):")
            for name, sample in sorted(fields.items()):
                types = ", ".join(sorted(sample.types))
                timestamp_hint = f" | ISO timestamps: {sample.timestamps}" if sample.timestamps else ""
                epoch_hint = "".join(f" | {mode} candidates: {count}" for mode, count in sorted(sample.epochs.items()))
                print(f"  {json.dumps(name)}: {sample.present}/{sampled} | {types}{timestamp_hint}{epoch_hint}")
            labels = {"signature": "Signature", "id": "ID", "service": "Service",
                      "level_field": "Severity", "time": "Timestamp"}
            for role, choices in candidates.items():
                qualifier = "" if role == "time" else " (by name)"
                print(f"{labels[role]} candidates{qualifier}: {', '.join(map(json.dumps, choices))}")

    if not sampled:
        print("The input is empty.", file=sys.stderr)
        return report_status(args, empty=True)
    if input_format is None:
        print("No JSON objects or key-value records sampled. Timed text commands need named regex captures.", file=sys.stderr)
        return report_status(args, empty=True)
    if not profile:
        print("No usable field mapping suggested. Try a larger sample or map fields manually.", file=sys.stderr)
        return report_status(args, empty=True)
    if args.profile_out:
        return 0
    print("Save and review the suggested mapping with --profile-out FILE, then reuse it with --profile FILE.",
          file=sys.stderr)
    if not timestamp_fields:
        print("No unambiguous timestamp fields found. Try --lines or choose a field and time format explicitly.", file=sys.stderr)
        return 0

    if any(re.search(r"[\x00-\x1f\x7f\u2018\u2019]", value)
           for value in (timestamp_fields[0], args.log_file)):
        print("Use bucket -h to pass the selected timestamp field and file manually.", file=sys.stderr)
        return 0
    if "level_field" in profile:
        severity = profile["level_field"]
        # Use a simple field token for the displayed example.
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", severity):
            print(f"Severity summary options: --format {input_format} --level-field {severity}", file=sys.stderr)
    timestamp = timestamp_fields[0].replace("'", "''")
    source = args.log_file.replace("'", "''")
    print("Try in PowerShell:", file=sys.stderr)
    options = f"--format {input_format} --time='{timestamp}' --interval 30s"
    if "time_format" in profile:
        print("Confirm the suggested epoch unit before using the mapping.", file=sys.stderr)
        options += f" --time-format {profile['time_format']}"
    if args.log_file.startswith("-") and args.log_file != "-":
        print(f"  python log_summary.py bucket {options} -- '{source}'", file=sys.stderr)
    else:
        print(f"  python log_summary.py bucket '{source}' {options}", file=sys.stderr)
    if args.log_file == "-":
        print("Pipe the input into the suggested command again.", file=sys.stderr)
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
            "Exit codes: 0 success, 1 I/O error, 2 usage error, 3 empty with --fail-on empty.\n\n"
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
        metavar="LEVEL",
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
    parser.add_argument("--level-field", help="severity field (JSONL: severity; logfmt or text profile: level)")
    parser.add_argument("--levels", nargs="+", metavar="LEVEL", help="severity vocabulary (default: INFO WARNING ERROR; WARN aliases WARNING)")
    add_multiline_option(parser)
    add_report_arguments(parser)
    args = parser.parse_args()
    if args.json and args.show_lines:
        parser.error("--json cannot be combined with --show-lines")
    regex_flag = args.regex
    args.regex = None
    apply_log_mapping(parser, args, required=(), optional=("level_field",))
    # Keep the CLI search pattern separate from the profile's field-mapping pattern.
    level_pattern = compile_pattern(parser, args.regex) if args.format == "text" else None
    if args.level_field is not None and args.format == "text" and level_pattern is None:
        parser.error("--level-field requires structured input or a text profile regex")
    vocabulary = tuple(dict.fromkeys(level.upper() for level in args.levels)) if args.levels else LOG_LEVELS
    if any(re.fullmatch(r"[A-Z][A-Z0-9_-]*", level) is None for level in vocabulary):
        parser.error("--levels expects names like INFO or CRITICAL")
    if args.level is not None:
        args.level = normalize_level(args.level, vocabulary)
        if args.level not in vocabulary:
            parser.error(f"argument --level: invalid choice: {args.level!r} (choose from {', '.join(vocabulary)})")
    coverage = SeverityCoverage()
    level_field = None
    if args.format != "text" or level_pattern is not None:
        default_level = "severity" if args.format == "jsonl" else "level"
        level_field = args.level_field if args.level_field is not None else default_level
    if level_pattern is not None and level_field not in level_pattern.groupindex:
        parser.error(f"profile regex must define a named severity capture {level_field!r}")
    pattern = compile_pattern(parser, regex_flag)
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
                level_pattern=level_pattern,
                input_format=args.format,
                levels=vocabulary,
                coverage=coverage,
                multiline=args.multiline and args.format != "jsonl",
            )
    except (OSError, UnicodeError) as error:
        return report_input_error(log_path, error)

    levels = (args.level,) if args.level else vocabulary
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
    if args.format != "text" or level_pattern is not None or args.multiline or coverage.unclassified:
        print(
            f"Classified: {coverage.classified}  Unclassified: {coverage.unclassified}",
            file=sys.stderr,
        )
    return report_status(args, empty=not any(counts[level] for level in levels))


if __name__ == "__main__":
    sys.exit(main())
