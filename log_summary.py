import argparse
import json
import re
import sys
from collections import Counter, deque
from contextlib import nullcontext
from pathlib import Path
from typing import Iterable, TextIO


LOG_LEVELS = ("INFO", "WARNING", "ERROR")


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
        for level in LOG_LEVELS:
            if line.startswith(level):
                counts[level] += 1
                if output is not None and selected_level in (None, level):
                    text = line.rstrip("\r\n")
                    print(f"{line_number}: {text}", file=output)
                break
    return counts


def count_errors(lines: Iterable[str]) -> int:
    """Count lines that start with ERROR."""
    return count_levels(lines)["ERROR"]


def open_log(log_path: str):
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
    last_printed = 0
    context_end = 0
    try:
        with open_log(args.log_file) as log_file:
            for number, line in enumerate(log_file, start=1):
                text = line.rstrip("\r\n")
                if contains in text and (pattern is None or pattern.search(text)):
                    for previous_number, previous_text in previous:
                        if previous_number > last_printed:
                            print(f"{previous_number}: {previous_text}")
                    context_end = number + args.context
                if number <= context_end:
                    print(f"{number}: {text}")
                    last_printed = number
                previous.append((number, text))
    except FileNotFoundError:
        print(f"Log file not found: {args.log_file}", file=sys.stderr)
        return 1
    return 0


def group_log(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="log_summary.py group",
        description="Rank values extracted from text or JSON Lines logs by frequency.",
    )
    parser.add_argument("log_file", help="log file path or - for stdin")
    parser.add_argument("--format", choices=("text", "jsonl"), default="text")
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

    counts: Counter[str] = Counter()
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
                counts[json.dumps(value, ensure_ascii=True)] += 1
                grouped += 1
    except FileNotFoundError:
        print(f"Log file not found: {args.log_file}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as error:
        print(f"Cannot read log: {error}", file=sys.stderr)
        return 1

    print("COUNT  VALUE")
    for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        print(f"{count:5}  {value}")
    print(f"Scanned: {scanned}  Grouped: {grouped}  Skipped: {scanned - grouped}")
    return 0


def main() -> int:
    if sys.argv[1:2] == ["search"]:
        return search_log(sys.argv[2:])
    if sys.argv[1:2] == ["group"]:
        return group_log(sys.argv[2:])

    parser = argparse.ArgumentParser(
        description="Count lines starting with INFO, WARNING, or ERROR.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Commands: search, group. Run either command with -h for details.\n\n"
            "Examples:\n"
            "  python log_summary.py                 Count levels in sample.log\n"
            "  python log_summary.py app.log         Count levels in app.log\n"
            "  python log_summary.py app.log --level ERROR\n"
            "                                        Show only the ERROR count\n"
            '  python log_summary.py sample.log --level ERROR --contains "timed out"\n'
            "                                        Count ERROR lines containing timed out\n"
            '  python log_summary.py sample.log --level ERROR --contains "timed out" --show-lines\n'
            "                                        Print matching lines with line numbers\n"
            "  Get-Content sample.log | python log_summary.py - --level ERROR\n"
            "                                        Read from a PowerShell pipeline\n"
            '  python log_summary.py sample.log --regex "timed out|connection failed"'
        ),
    )
    parser.add_argument(
        "log_file",
        nargs="?",
        default=str(Path(__file__).with_name("sample.log")),
        help="log file path or - for stdin (default: sample.log beside this script)",
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

    levels = (args.level,) if args.level else LOG_LEVELS
    for level in levels:
        print(f"{level}: {counts[level]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
