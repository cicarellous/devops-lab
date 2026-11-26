# devops-lab

A Python command-line tool for log analysis and operational troubleshooting. It reads logs from files or standard input, counts entries by severity, and supports text filtering and line-numbered output for incident triage.

Run the log summary script:

```powershell
python log_summary.py
```

Search any text log with original line numbers and surrounding context:

```powershell
python log_summary.py search sample.log --regex "timed out|connection failed" --context 2
Get-Content sample.log | python log_summary.py search - --contains "timed out"
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

## Tests

Run its unit tests:

```powershell
python -m unittest -v
```

GitHub Actions runs the unit tests on pushes to `main` and on pull requests targeting `main`. You can see each run in the repository's **Actions** tab.