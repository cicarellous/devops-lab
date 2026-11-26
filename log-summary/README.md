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
retries, and recurring error codes. Pass a UTF-8 file path (plain or `.gz`, with or without a BOM) or use `-` to read
from standard input. Redirected stdin must be UTF-8. In Windows PowerShell,
set `$OutputEncoding = [System.Text.UTF8Encoding]::new($false)` if an upstream
PowerShell command needs to send non-ASCII text.

## Inspect an unfamiliar log

```powershell
python log_summary.py inspect incident.jsonl
```

Inspect samples the first 100 lines by default. Add `--sample spread` to
scan the full input once, including gzip or stdin, while retaining at most
`--lines` sampled records. Spread keeps the first and last records when
`--lines` is at least 2 and samples the interior reproducibly. With
`--lines 1` it keeps the first record. It shows top-level field
types, presence, and timestamp candidates. On `incident.jsonl`, look for
`ts`, `code`, and `request`. Run the suggested `bucket` command, then add
`--signature code --value TIMEOUT` to isolate the jump at 14:01 UTC.
For key-value logs, inspect suggests `logfmt`. Save a suggested mapping with
`python log_summary.py inspect app.log --profile-out app-profile.json`, then
review the new profile before using it. Inspect uses the inferred signature as
the default grouping field (`by`). Its bucket suggestion preserves
`--multiline` and chooses an interval for the sampled time span. Longer logs
may need a wider interval. Existing files are kept intact.

Empty results exit with status 0 by default. Add `--fail-on empty` to return
3 instead. Guidance goes to stderr and distinguishes no matches, an empty
window, and unusable input fields. For `inspect`, empty means there is no
usable mapping to suggest. An empty result leaves `--profile-out` untouched. A partial profile can still
be saved, with a warning if timed commands need an explicit `--time`.

## Summarize severity

Run the log summary with the included `app.log` as the default. The summary
recognizes severity at the start of a line or after an ISO timestamp.
Use `python log_summary.py -- search` to summarize a file named `search`.
Run `python log_summary.py --version` to display the CLI version.

```powershell
python log_summary.py app.log --level ERROR --contains "timed out" --show-lines
python log_summary.py app.jsonl --format jsonl --level-field severity
```

JSON Lines uses the `severity` field by default. Its values are
matched case-insensitively against INFO, WARNING, and ERROR. Coverage goes
to stderr for JSON Lines and for text with unclassified lines. It counts
lines after text filters, across all levels. Use `--levels INFO WARNING ERROR CRITICAL`
to choose the severity vocabulary. `WARN` counts as `WARNING` unless you
include `WARN` as its own level.

Key-value logs can use `--format logfmt`. The parser reads consecutive
`key=value` fields, including double-quoted values, after an optional ISO
timestamp and severity. Those prefixes supply `timestamp` and `level`.
Trailing free text remains visible in the original record.

## Search logs

Search any text log with original line numbers and surrounding context:

```powershell
python log_summary.py search app.log --regex "timed out|lookup failed" --context 2
python log_summary.py search "app.log*" --contains "timed out" --context 2
Get-Content app.log | python log_summary.py search - --contains "timed out"
```

Pass multiple paths or quoted globs to search current and rotated logs,
including `.gz` files. Context stays within each file. When several inputs
are searched, text prefixes each result with its source and JSON adds
`sources` to the report and `source` to each result. Use `-` once to include
stdin.


Use `--contains` for case-sensitive literal text or `--regex` for a Python regular
expression. When both are supplied, lines must match both. Overlapping context
is printed once.

## Read multiline text records

Use `--multiline` with any command when reading text logs. For `inspect`,
`--lines` counts sampled records in this mode.
Lines that start with a space or tab attach to the preceding record.
Unindented stack frames start new records.
Summary and search can match text anywhere in a record. Severity and field
mappings read its header. JSON Lines stays line-based in structured commands and when `inspect` recognizes
a JSON object. A multiline record exceeding 10,000 lines or 1 MiB of UTF-8
text stops the command with an input error and the record's starting line.

```powershell
python log_summary.py search stacktrace-example.txt --multiline --contains TimeoutError --context 1
python log_summary.py stacktrace-example.txt --multiline --contains TimeoutError --show-lines
python log_summary.py timeline stacktrace-example.txt --format logfmt --time timestamp --key request_id --value r1 --multiline
python log_summary.py bucket stacktrace-example.txt --format logfmt --time timestamp --interval 1m --multiline
```

Search context counts records in multiline mode. Search and timeline JSON
include the inclusive `line` and `end_line` of each displayed record. Coverage
in search, group, timed commands, and inspect reports physical lines
separately from logical records.

## Group log entries

Use a named regex capture for text or a field from JSON Lines or logfmt. Use
dotted paths such as `event.code` for nested JSON fields.

```powershell
python log_summary.py group app.log --regex 'code=(?P<code>E\d{4})' --by code
python log_summary.py group app.jsonl --format jsonl --by code
Get-Content app.jsonl | python log_summary.py group - --format jsonl --by service
```

Both error-code reports rank `E1042` (4), `E2007` (3), and `E3001` (1).
Results are ordered by count, with ties sorted by displayed value.
Each line contributes at most one value. Text uses the first regex match.
JSON Lines uses a scalar field. Missing, empty, unmatched, and
malformed records count as skipped. The footer shows how much of the input
contributed to the report.

## Reconstruct an event timeline

Select an ID and timestamp from JSON Lines fields or named regex captures in text.

```powershell
python log_summary.py timeline app.jsonl --format jsonl --key request_id --value r-100 --time timestamp
python log_summary.py timeline app.log --format logfmt --key request_id --value r-100 --time timestamp
```

Both commands reconstruct the same six events for `r-100`. The tool sorts by
ISO 8601 timestamp and prints each record with its UTC time and source line.
Ties retain source and line order. Timestamps without an offset are treated as
UTC. The footer reports records that cannot yield an ID or matching events with
an invalid or missing timestamp. Use `-` in place of the file path for stdin
and `python log_summary.py timeline -h` for all options. Coverage separates
matched events, events for the same ID outside the window, other IDs, and
skipped records (missing ID or unusable timestamp for the requested ID).

### Follow an ID across sources

```powershell
python log_summary.py timeline --source incident.jsonl incident-profile.json --source worker-example.txt worker-profile.json --value incident-00 --json
```

Each `--source` pairs a file with its own profile. This example follows the
same ID across JSON Lines and logfmt. The report includes per-source coverage.

## Compare event groups

Use the same field mappings as `timeline` to compare every ID in a log:

```powershell
python log_summary.py correlate app.jsonl --format jsonl --key request_id --time timestamp --sort span
python log_summary.py correlate app.log --format logfmt --key request_id --time timestamp --sort count
```

The report shows each ID's event count, first and last UTC timestamps, and
observed span. The span is the difference between the first and last valid
events present in the input. Use `--sort span` (the default) or `--sort count`.
Coverage reconciles scanned records into used, skipped (missing a usable ID
or timestamp), and outside the selected window. Use `-` for stdin
and `python log_summary.py correlate -h` for all options. Use `timeline` with
an ID from the report to inspect its individual events.

## Focus on an incident window

Use `--since` (inclusive) and `--before` (exclusive) with `timeline`,
`correlate`, and `bucket`. Adjacent windows share a boundary without
counting that event twice. `compare` uses the same rule for both windows.
Use ISO 8601 timestamps. An omitted offset means UTC.

For existing scripts, `--until` remains inclusive in `timeline` and
`correlate`, and exclusive in `bucket`. Use one upper-bound flag at a time.

```powershell
python log_summary.py correlate app.jsonl --format jsonl --key request_id --time timestamp --since "2026-11-26T14:00:00.548Z" --before "2026-11-26T14:00:01Z"
```

## Read epoch timestamps

Timed commands default to ISO 8601. Use `--time-format epoch_s` for Unix seconds
or `--time-format epoch_ms` for milliseconds. Profiles can set `time_format`
per source. Window bounds still use ISO 8601.

```powershell
'{"timestamp_ms":1795701660000}' | python log_summary.py bucket - --format jsonl --time timestamp_ms --time-format epoch_ms --interval 1m --since 2026-11-26T14:01Z --before 2026-11-26T14:02Z
```

Use integer or decimal-string timestamps when exact precision matters. Values
finer than a microsecond are skipped. `inspect` suggests a unit for plausible
modern epoch values in timestamp-named fields. Review it before using it.
Timed commands warn on stderr if a source sample looks like the other epoch
unit. The selected unit still governs the results.

## Find when event activity changed

```powershell
python log_summary.py bucket incident.jsonl --format jsonl --time ts --interval 30s --signature code --value TIMEOUT --since 2026-11-26T14:00Z --before 2026-11-26T14:03Z
```

Timeouts jump at 14:01 UTC. Use that boundary in the `compare` example below.
Replace `TIMEOUT` with `QUEUE_WARNING` to see a steady rate. Text input requires
`--regex` with named captures. Add `--json` for a versioned report with bucket
boundaries, counts, rates, and coverage.

Add `--spike-factor 3` to flag a full bucket whose count exceeds three
times the median of the previous five full buckets. Empty full buckets
count as zero in the baseline. With a zero median, opt in with
`--spike-min-count 20` to flag counts of 20 or more. Partial buckets and
buckets with insufficient history are excluded. Supply an interval-aligned
`--before` at the exclusive end of a known complete window to evaluate its
last bucket.

With `--fail-on spike`, exit 4 means a breach, 0 means at least one bucket
was evaluated and none breached, and 3 means no bucket could be evaluated.
When detection is enabled, JSON reports include `spike_factor`,
`spike_min_count`, and `evaluated_buckets`. Each bucket includes
`baseline_count`, `evaluated`, and `spike`.

Add `--spike-examples 3` to show the first three counted events from each
flagged bucket in input order. Each example includes the source, original
line range, and a header preview of up to 240 characters. Use `--id FIELD`
or an `id` mapping in the source profile to include a correlation ID.
JSON includes an `examples` array on each flagged bucket when requested.



Buckets align to UTC epoch intervals. Omitted bounds use observed timestamps before signature
filtering. Empty buckets inside the reported range show zero observed
events. Partial edge buckets show counts with no rate (`null` in JSON).
Reports are limited to 10,000 buckets. Signature matching is exact unless
you pass `--ignore-case`. Use `--level ERROR` to bucket by severity directly.
JSON includes the severity selection, case rule, and `matched: false` when
no events counted, even if the report contains zero-count buckets.

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
In per-source coverage, `skipped` includes `invalid_timestamp` and in-window
records without a usable signature. `outside_windows` is separate.

## Analyze multiple sources

Use the same `--source FILE PROFILE` pairs with `compare`, `correlate`, and
`timeline`. Each profile maps the fields in its own file. Quote a glob such
as `'logs/*.jsonl.gz'` to apply one profile to all matching files.

```powershell
python log_summary.py compare --source incident.jsonl incident-profile.json --source worker-example.txt worker-profile.json --baseline 2026-11-26T14:00Z 2026-11-26T14:01Z --incident 2026-11-26T14:01Z 2026-11-26T14:03Z
python log_summary.py correlate --source incident.jsonl incident-profile.json --source worker-example.txt worker-profile.json --top 3
```

Compare shows coverage per source and window, and links examples to the source
line. Correlate lists the sources that observed each ID. IDs are matched by
value across files. Each input record counts once per occurrence, so supplying
overlapping copies can inflate event counts.

For combined totals, severity summary uses each profile's `level_field`,
`group` uses `by`, and `bucket` aligns every source on one UTC time axis.
When you filter a bucket with `--value`, each profile supplies its own
`signature` field. Use `--level` for a severity filter instead.

```powershell
python log_summary.py --source app.jsonl app-jsonl-profile.json --source stacktrace-example.txt stacktrace-profile.json --multiline
python log_summary.py group --source app.jsonl app-jsonl-profile.json --source stacktrace-example.txt stacktrace-profile.json --multiline
python log_summary.py bucket --source app.jsonl app-jsonl-profile.json --source stacktrace-example.txt stacktrace-profile.json --interval 1m --value E1042 --multiline --since 2026-11-26T14:00Z --before 2026-11-26T14:02Z
```

Summary shows the severity counts for each source. `--level` limits the
displayed counts, and coverage still includes all classified severities.
With `--source`, summary `--show-lines` includes the source and line range.

## Display timestamps in another timezone

Use `--display-timezone` with `timeline`, `correlate`, `compare`, or
`bucket` to show local clock times in text reports.

```powershell
python log_summary.py timeline app.jsonl --profile app-jsonl-profile.json --key request_id --value r-100 --display-timezone UTC-03:00
```

Use a fixed offset such as `UTC-03:00` or an IANA name such as
`America/Sao_Paulo`. Named zones follow their historical offset and
daylight-saving rules. If Python cannot find named-zone data, install
`tzdata` in your environment or use a fixed offset. JSON stays in UTC,
filters without an offset are interpreted as UTC, and buckets stay aligned
to UTC.

## Limit or export reports

Use `--top N` with `group`, `correlate`, or `compare` to show the first N
ranked rows. Coverage still describes the entire input. Every command accepts
`--json` for one versioned report on stdout. Each report identifies its command.
Use `search --json` for matching lines and context, or severity summary
`--json` for counts. JSON reports always include `coverage.physical_lines`. For `inspect`, this
counts only sampled lines and the report includes the `sample` mode.
Summary, group, and bucket also include
`coverage.sources` for a single file or stdin.

## Reuse field mappings

Save your log mapping in a JSON file and pass it to `bucket`, `compare`,
`timeline`, `correlate`, `group`, or the severity summary through `--profile`. See
[`incident-profile.json`](incident-profile.json) for an example.

```powershell
python log_summary.py bucket incident.jsonl --profile incident-profile.json --interval 30s --value TIMEOUT
python log_summary.py timeline incident.jsonl --profile incident-profile.json --value incident-00
```

Profile keys are `format`, `time`, `time_format`, `regex`, `signature`,
`service`, `id` (alias `key`), `by`, and `level_field`.
Use `regex` for named captures in text logs. For severity summary, the text
profile regex captures `level` (or the configured `level_field`), while the
CLI's `--regex` still filters whole lines. The `id` field supplies `--key`
for timeline and correlation. Explicit flags override the profile. Bucket uses its signature mapping when you pass
`--value`. Time
windows, requested values, and output options stay on the command line.

## Gate a comparison

Add `--fail-on increase --min-increase-rate 5 --min-increase-percent 50`
to `compare` to return 4 if any signature increases by at least 5 events
per minute and at least 50% over its baseline rate. A signature unseen
in a usable baseline only needs to meet the absolute threshold.
If the baseline has no usable events, the gate returns 3. The gate
checks every signature, including rows hidden by `--top`. JSON reports
include the thresholds, breach count, and a `breached` flag on displayed rows.
If `--top` hides any breaches, stderr and JSON identify the highest-ranked
hidden breach and how many were hidden.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Completed, including empty results by default |
| `1` | Input or output error |
| `2` | Invalid arguments or profile |
| `3` | Empty result with `--fail-on empty`, or a gate could not evaluate |
| `4` | Spike or increase gate breached |

## Tests

From `log-summary/`, run the unit tests:

```powershell
python -m unittest -v
```

In GitHub Actions, **Log Summary CI** shows Python 3.12 tests on Ubuntu and Windows,
Python 3.13 tests on Ubuntu, Python 3.14 tests on Ubuntu and Windows, and a Ruff and mypy job. You can see each run in the repository's **Actions** tab.

The Ubuntu Python 3.14 job also publishes line and branch coverage for
`log_summary.py` in the run summary, including CLI subprocesses launched by
the tests. Coverage reporting has no minimum percentage.
