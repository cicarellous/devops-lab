import argparse
import sys
from pathlib import Path
from typing import Iterable


def count_errors(lines: Iterable[str]) -> int:
    """Count lines that start with ERROR."""
    error_count = 0
    for line in lines:
        if line.startswith("ERROR"):
            error_count += 1
    return error_count


def main() -> int:
    parser = argparse.ArgumentParser(description="Count lines starting with ERROR.")
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
            error_count = count_errors(log_file)
    except FileNotFoundError:
        print(f"Log file not found: {log_path}", file=sys.stderr)
        return 1

    print(f"ERROR entries: {error_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
