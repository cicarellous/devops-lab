from pathlib import Path

log_path = Path(__file__).with_name("sample.log")
error_count = 0

with log_path.open(encoding="utf-8") as log_file:
    for line in log_file:
        if line.startswith("ERROR"):
            error_count += 1

print(f"ERROR entries: {error_count}")
