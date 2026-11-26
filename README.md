# devops-lab

A Python command-line tool for log analysis and operational troubleshooting. It reads logs from files or standard input, counts entries by severity, and supports text filtering and line-numbered output for incident triage.

Run the log summary with the included `app.log` as the default. The summary
recognizes severity at the start of a line or after an ISO timestamp.

```powershell
python log_summary.py
```

Search any text log with original line numbers and surrounding context:

```powershell
python log_summary.py search app.log --regex "timed out|lookup failed" --context 2
Get-Content app.log | python log_summary.py search - --contains "timed out"
```

Use `python log_summary.py search -h` for search options. When both filters
are supplied, lines must match both. Overlapping context is printed once.

## Group log entries

The synthetic `app.log` and `app.jsonl` contain the same 20 events. They
include interleaved requests across services, retries, and recurring error codes.

```powershell
python log_summary.py group app.log --regex 'code=(?P<code>E\d{4})' --by code
python log_summary.py group app.jsonl --format jsonl --by code
Get-Content app.jsonl | python log_summary.py group - --format jsonl --by service
```

Both code reports rank `E1042` (4), `E2007` (3), and `E3001` (1).
Results are ordered by count, with ties sorted by displayed value.
Each line contributes at most one value. Text uses the first regex match.
JSON Lines uses a top-level scalar field. Missing, empty, unmatched, and
malformed records count as skipped. The footer shows how much of the input
contributed to the report.

## Reconstruct an event timeline

Select an ID and timestamp from JSON Lines fields or named regex captures in text.

```powershell
python log_summary.py timeline app.jsonl --format jsonl --key request_id --value r-100 --time timestamp
python log_summary.py timeline app.log --regex '^(?P<ts>\S+).*request_id=(?P<id>\S+)' --key id --value r-100 --time ts
```

Both commands reconstruct the same six events for `r-100`. The tool sorts by
ISO 8601 timestamp, prints times in UTC beside each original record, and keeps
input order for equal timestamps. Timestamps without an offset are treated as
UTC. The footer reports records that cannot yield an ID or matching events with
an invalid or missing timestamp. Use `-` in place of the file path for stdin
and `python log_summary.py timeline -h` for all options.

## Tests

Run its unit tests:

```powershell
python -m unittest -v
```

GitHub Actions runs the unit tests on pushes to `main` and on pull requests targeting `main`. You can see each run in the repository's **Actions** tab.