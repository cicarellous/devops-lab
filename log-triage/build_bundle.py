"""Build an offline download bundle from the wheel in dist/."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from log_triage._version import __version__


def main() -> None:
    root = Path(__file__).resolve().parent
    dist = root / "dist"
    wheel = dist / f"log_triage-{__version__}-py3-none-any.whl"
    if not wheel.is_file():
        raise SystemExit(f"Build the wheel first: {wheel.name}")

    files = (
        "README.md",
        "LICENSE",
        "docs/reference.md",
        "docs/runbook.md",
        "log_triage/app.log",
        "app.jsonl",
        "app-jsonl-profile.json",
        "incident.jsonl",
        "incident-profile.json",
        "stacktrace-example.txt",
        "stacktrace-profile.json",
        "worker-example.txt",
        "worker-profile.json",
    )
    bundle = dist / f"log-triage-{__version__}-bundle.zip"
    with ZipFile(bundle, "w", ZIP_DEFLATED) as archive:
        archive.write(wheel, wheel.name)
        for path in files:
            archive.write(root / path, path)
    print(bundle)


if __name__ == "__main__":
    main()
