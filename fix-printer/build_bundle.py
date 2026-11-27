"""Build the offline download bundle from the wheel in dist/."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from fixprinter import __version__


def main() -> None:
    root = Path(__file__).resolve().parent
    dist = root / "dist"
    wheel = dist / f"fix_printer-{__version__}-py3-none-any.whl"
    if not wheel.is_file():
        raise SystemExit(f"Build the wheel first: {wheel.name}")

    files = (
        "README.md",
        "LICENSE",
        "NOTICE",
        "docs/reference.md",
        "docs/runbook.md",
        "fixprinter.py",
        "dictionaries/FIX42.xml",
        "dictionaries/FIX44.xml",
        "dictionaries/LICENSE",
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
