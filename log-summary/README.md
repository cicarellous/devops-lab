# Log Summary

A Python CLI for incident triage. Summarize severity counts, search with context,
rank recurring values, reconstruct a request timeline, or compare event counts
and observed spans across IDs. It reads files or standard input.

| Task | Command |
| --- | --- |
| Discover a log's format and fields | `python log_summary.py inspect -h` |
| Count entries by severity | `python log_summary.py [FILE]` |
| Find matching lines and nearby context | `python log_summary.py search -h` |
| Rank values by frequency | `python log_summary.py group -h` |
| Reconstruct one ID's events | `python log_summary.py timeline -h` |
| Rank IDs by event count or observed span | `python log_summary.py correlate -h` |
| Compare event rates before and during an incident | `python log_summary.py compare -h` |
| Find when event activity changed | `python log_summary.py bucket -h` |

## Quick start

Use Python 3.12 or later. Log Summary uses the Python standard library.

```powershell
# From the devops-lab repository root
cd log-summary
python log_summary.py
```

Run the remaining examples from `log-summary/`. The included `app.log` and
`app.jsonl` represent the same 20 synthetic events, with interleaved requests,
retries, and recurring error codes. Pass a UTF-8 file path or use `-` to read
from standard input.

## Inspect an unfamiliar log

```powershell
python log_summary.py inspect incident.jsonl
```

Inspect samples the first 100 lines by default. It shows top-level field
types, presence, and timestamp candidates. On `incident.jsonl`, look for
`ts`, `code`, and `request`. Run the suggested `bucket` command, then add
`--signature code --value TIMEOUT` to isolate the jump at 14:01 UTC.
For text logs, inspect reports recognized timestamp prefixes.

Empty results still exit with status 0. Guidance goes to stderr and
distinguishes no matches, an empty window, and unusable input fields.

## Summarize severity

Run the log summary with the included `app.log` as the default. The summary
recognizes severity at the start of a line or after an ISO timestamp.

```powershell
python log_summary.py app.log --level ERROR --contains "timed out" --show-lines
python log_summary.py app.jsonl --format jsonl --level-field severity
```

JSON Lines uses the top-level `severity` field by default. Its values are
matched case-insensitively against INFO, WARNING, and ERROR. Coverage goes
to stderr for JSON Lines and for text with unclassified lines. It counts
lines after text filters, across all levels.

## Search logs

Search any text log with original line numbers and surrounding context:

```powershell
python log_summary.py search app.log --regex "timed out|lookup failed" --context 2
Get-Content app.log | python log_summary.py search - --contains "timed out"
```

Use `--contains` for case-sensitive literal text or `--regex` for a Python regular
expression. When both are supplied, lines must match both. Overlapping context
is printed once.

## Group log entries

Use a named regex capture for plain text or a top-level field for JSON Lines.

```powershell
python log_summary.py group app.log --regex 'code=(?P<code>E\d{4})' --by code
python log_summary.py group app.jsonl --format jsonl --by code
Get-Content app.jsonl | python log_summary.py group - --format jsonl --by service
```

Both error-code reports rank `E1042` (4), `E2007` (3), and `E3001` (1).
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

## Compare event groups

Use the same field mappings as `timeline` to compare every ID in a log:

```powershell
python log_summary.py correlate app.jsonl --format jsonl --key request_id --time timestamp --sort span
python log_summary.py correlate app.log --regex '^(?P<ts>\S+).*request_id=(?P<id>\S+)' --key id --time ts --sort count
```

The report shows each ID's event count, first and last UTC timestamps, and
observed span. The span is the difference between the first and last valid
events present in the input. Use `--sort span` (the default) or `--sort count`.
Records without a usable ID or timestamp count as skipped. Use `-` for stdin
and `python log_summary.py correlate -h` for all options. Use `timeline` with
an ID from the report to inspect its individual events.

## Focus on an incident window

Use `--since` and `--until` with `timeline` or `correlate`. Both bounds are
optional and inclusive. Use ISO 8601 timestamps with an offset, or omit the
offset to use UTC. Events outside the window are excluded from the report.
Correlation spans are calculated from the events within the window.

```powershell
python log_summary.py correlate app.jsonl --format jsonl --key request_id --time timestamp --since "2026-11-26T14:00:00.548Z" --until "2026-11-26T14:00:00.959Z"
```

## Find when event activity changed

```powershell
python log_summary.py bucket incident.jsonl --format jsonl --time ts --interval 30s --signature code --value TIMEOUT --since 2026-11-26T14:00Z --until 2026-11-26T14:03Z
```

Timeouts jump at 14:01 UTC. Use that boundary in the `compare` example below.
Replace `TIMEOUT` with `QUEUE_WARNING` to see a steady rate. Text input requires
`--regex` with named captures. Add `--json` for a versioned report with bucket
boundaries, counts, rates, and coverage.

Buckets align to UTC epoch intervals. `--since` is inclusive and `--until`
is exclusive. Omitted bounds use observed timestamps before signature
filtering. Empty buckets inside the reported range show zero observed
events. Partial edge buckets show counts with no rate (`null` in JSON).
Reports are limited to 10,000 buckets.

## Compare an incident with a baseline

```powershell
python log_summary.py compare incident.jsonl --format jsonl --time ts --signature code --service service --id request --baseline 2026-11-26T14:00Z 2026-11-26T14:01Z --incident 2026-11-26T14:01Z 2026-11-26T14:03Z
```

The synthetic example shows 80 timeout events across 12 observed IDs and a
warning whose count doubles while its rate stays flat. Rows are ranked by the
change in events per minute. Each row gives an example line, and the footer
shows coverage. `NEW` means the signature was unseen in the supplied baseline.

Windows include their start and exclude their end. Use `--regex` with named
captures for text logs. `--service` and `--id` are optional. Observed ID counts
reflect only records with usable IDs. Rates use the full requested windows.

## Tests

From `log-summary/`, run the unit tests:

```powershell
python -m unittest -v
```

GitHub Actions runs the unit tests on pushes to `main` and on pull requests targeting `main`. You can see each run in the repository's **Actions** tab.