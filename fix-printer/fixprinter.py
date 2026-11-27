"""Run FIX Printer directly from a source checkout."""

from fix_printer.cli import run_cli

if __name__ == "__main__":
    raise SystemExit(run_cli())
