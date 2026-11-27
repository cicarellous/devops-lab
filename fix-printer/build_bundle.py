"""Build the offline download bundle from the wheel in dist/."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from fix_printer._version import __version__


def main() -> None:
    root = Path(__file__).resolve().parent
    dist = root / "dist"
    wheel = dist / f"fix_printer-{__version__}-py3-none-any.whl"
    if not wheel.is_file():
        raise SystemExit(f"Missing {wheel.name}. Run python -m build --wheel from fix-printer/ first.")

    files = (
        "README.md",
        "LICENSE",
        "NOTICE",
        "docs/reference.md",
        "docs/runbook.md",
        "fixprinter.py",
        "test_fixprinter.py",
        "fix_printer/__init__.py",
        "fix_printer/__main__.py",
        "fix_printer/_version.py",
        "fix_printer/cli.py",
        "fix_printer/dictionaries/FIX42.xml",
        "fix_printer/dictionaries/FIX44.xml",
        "fix_printer/dictionaries/LICENSE",
        "samples/client-ingress.fix",
        "samples/orders.fix",
        "samples/session-44.fix",
    )
    bundle = dist / f"fix-printer-{__version__}-bundle.zip"
    with ZipFile(bundle, "w", ZIP_DEFLATED) as archive:
        archive.write(wheel, wheel.name)
        for path in files:
            archive.write(root / path, path)
    print(bundle)


if __name__ == "__main__":
    main()
