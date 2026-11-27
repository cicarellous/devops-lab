"""Run FIX Printer as a Python module."""

from .cli import run_cli


if __name__ == "__main__":
    raise SystemExit(run_cli())
