import argparse
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Iterable, TextIO


LOG_LEVELS = ("INFO", "WARNING", "ERROR")


def count_levels(
    lines: Iterable[str],
    *,
    contains: str = "",
    output: TextIO | None = None,
    selected_level: str | None = None,
) -> dict[str, int]:
    """Count matching lines and optionally write them as they are read."""
    counts = dict.fromkeys(LOG_LEVELS, 0)
    for line_number, line in enumerate(lines, start=1):
        if contains not in line:
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Count lines starting with INFO, WARNING, or ERROR.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
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
            "                                        Read from a PowerShell pipeline"
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
    args = parser.parse_args()
    log_path = args.log_file

    try:
        input_stream = (
            nullcontext(sys.stdin) if log_path == "-"
            else Path(log_path).open(encoding="utf-8")
        )
        with input_stream as log_file:
            counts = count_levels(
                log_file,
                contains=args.contains,
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
