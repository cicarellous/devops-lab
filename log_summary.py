import argparse
import sys
from pathlib import Path
from typing import Iterable


LOG_LEVELS = ("INFO", "WARNING", "ERROR")


def count_levels(lines: Iterable[str]) -> dict[str, int]:
    """Count lines starting with each supported log level."""
    counts = dict.fromkeys(LOG_LEVELS, 0)
    for line in lines:
        for level in LOG_LEVELS:
            if line.startswith(level):
                counts[level] += 1
                break
    return counts


def count_errors(lines: Iterable[str]) -> int:
    """Count lines that start with ERROR."""
    return count_levels(lines)["ERROR"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Count lines starting with INFO, WARNING, or ERROR.")
    parser.add_argument(
        "log_file",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name("sample.log"),
        help="path to the log file (default: sample.log beside this script)",
    )
    log_path = parser.parse_args().log_file

    try:
        with log_path.open(encoding="utf-8") as log_file:
            counts = count_levels(log_file)
    except FileNotFoundError:
        print(f"Log file not found: {log_path}", file=sys.stderr)
        return 1

    for level, count in counts.items():
        print(f"{level}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
