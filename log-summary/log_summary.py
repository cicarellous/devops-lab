import argparse
import json
import re
import sys
from collections import Counter, deque
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import chain
from pathlib import Path
from typing import ContextManager, Iterable, TextIO


LOG_LEVELS = ("INFO", "WARNING", "ERROR")
TIMESTAMP_PREFIX = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?[ \t]+"
)


def count_levels(
    lines: Iterable[str],
    *,
    contains: str = "",
    pattern: re.Pattern[str] | None = None,
    output: TextIO | None = None,
    selected_level: str | None = None,
) -> dict[str, int]:
    """Count matching lines and optionally write them as they are read."""
    counts = dict.fromkeys(LOG_LEVELS, 0)
    for line_number, line in enumerate(lines, start=1):
        if contains not in line:
            continue
        if pattern is not None and pattern.search(line.rstrip("\r\n")) is None:
            continue
        timestamp = TIMESTAMP_PREFIX.match(line)
        level_start = timestamp.end() if timestamp else 0
        for level in LOG_LEVELS:
            if line.startswith(level, level_start):
                counts[level] += 1
                if output is not None and selected_level in (None, level):
                    text = line.rstrip("\r\n")
                    print(f"{line_number}: {text}", file=output)
                break
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


def event_pattern(parser: argparse.ArgumentParser, args: argparse.Namespace) -> re.Pattern[str] | None:
    if args.format == "text" and args.regex is None:
        parser.error("text input requires --regex")
    if args.format == "jsonl" and args.regex is not None:
        parser.error("--regex applies only to text input")
    pattern = compile_pattern(parser, args.regex)
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

                utc_time = event_time(fields.get(args.time))
                if utc_time is None:
                    skipped += 1
                    continue
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

    parser = argparse.ArgumentParser(
        description="Count INFO, WARNING, and ERROR entries, with optional ISO timestamps.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Commands (add -h after a command for its options):\n"
            "  search      Find matching lines and surrounding context\n"
            "  group       Rank extracted values by frequency\n"
            "  timeline    Reconstruct events for one ID\n"
            "  correlate   Rank IDs by event count or observed span\n\n"
            "Examples:\n"
            "  python log_summary.py                 Count levels in app.log\n"
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
    args = parser.parse_args()
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
