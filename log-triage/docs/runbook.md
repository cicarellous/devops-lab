# Log Triage runbook

Run these exercises from `log-triage/` using the included synthetic logs.
The commands use Bash and Python 3.12. On Windows, substitute `python`
in an activated environment. File-based commands also work in PowerShell.
See the [reference](reference.md) for input and output rules.

## Before you start

From the repository root, run `cd log-triage`. Then verify you are in the
right directory and that Python is available.

```bash
python3.12 --version
python3.12 log_triage.py --version
```

You should see Python 3.12.x and a line beginning with `Log Triage`. On
Windows, substitute `python` throughout after activating your Python
environment. If Python cannot find `log_triage.py`, check that your terminal
is in the directory containing that file. The README has
[setup instructions](../README.md#quick-start).

Keep the bundled files intact. Commands that create files below use scratch
directories or explicitly named outputs. Copy commands from Bash or
PowerShell blocks. Start with the example values, then change one flag at a
time and compare the result.

### Which command should I reach for?

| Question | Start here |
| --- | --- |
| What does this file contain? | `inspect`, then the severity summary |
| Where is this message or exception? | `search`, with `--context` or `--multiline` as needed |
| Which error code keeps recurring? | `group` |
| What happened to this request? | `timeline` |
| Which request ID should I investigate first? | `correlate` |
| When did the problem become visible? | `bucket` |
| Which event rates changed during the incident? | `compare` |

## 1. Start with counts and find the evidence

Run a severity summary on the bundled plain-text log.

```bash
python3.12 log_triage.py log_triage/app.log
```

Expected output:

```text
INFO: 12
WARNING: 3
ERROR: 5
```

This is a count of log entries. One request can emit several entries. To
see the error entries containing the exact text `timed out`, add a severity
filter and print the matching lines before the count.

```bash
python3.12 log_triage.py log_triage/app.log --level ERROR --contains "timed out" --show-lines
```

Expected output, shown in part:

```text
8: ... ERROR ... request_id=r-100 ... Connection acquisition timed out
13: ... ERROR ... request_id=r-101 ... Authorization retry timed out
ERROR: 2
```

The numbers at the left are line numbers in `log_triage/app.log`. A warning also
contains `timed out` but the severity filter excludes it. Use `search`
without a severity filter to see it and its neighbors.

```bash
python3.12 log_triage.py search log_triage/app.log --contains "timed out" --context 1
```

The matches are at lines 6, 8, and 13. Line 7 appears once as context for
two matches. Use `--json` and check `coverage.matched` if you need the
number of matches. `coverage.displayed` also counts the context lines.

### Turn an error code into a request timeline

The same events are available as JSON Lines in `app.jsonl`. Each line is
one JSON record. Select a field with `--by` to find the most common code.

```bash
python3.12 log_triage.py group app.jsonl --format jsonl --by code
```

Expected output:

```text
COUNT  VALUE
    4  "E1042"
    3  "E2007"
    1  "E3001"
Scanned: 20  Grouped: 8  Skipped: 12
```

The 12 skipped records have no usable `code` for this grouping. An
informational record can be valid while having no error code. Take the
request ID `r-100` from the earlier search. Here `--key` is the field name
and `--value` is the ID you want to follow.

```bash
python3.12 log_triage.py timeline app.jsonl --format jsonl --time timestamp --key request_id --value r-100
```

Expect six matching events in timestamp order. They start with the gateway
accepting the request and end with the gateway reporting failure. Look at
the service and message between those events to decide which component to
investigate next.

## 2. Get oriented in an unfamiliar log

A **profile** is a JSON file that tells Log Triage which format and field
names to use. Inspect a few records before creating one.

```bash
python3.12 log_triage.py inspect incident.jsonl --lines 3
```

Expected output, in part:

```text
Sampled: 3  JSON objects: 3  Text timestamp prefixes: 0
Suggested format: jsonl
  "code": 2/3 | str
  "request": 1/3 | str
  "ts": 3/3 | str | ISO timestamps: 3
```

The fractions describe field presence in the sample. For example, one of
the three records has a request ID. Inspect uses field names and values to
suggest mappings. Verify those suggestions against records you recognize.

Sample across the full file and save a starting profile. Choose a fresh output
path on reruns because `inspect` protects existing files.

```bash
python3.12 log_triage.py inspect incident.jsonl --sample spread --profile-out incident-local.json
python3.12 log_triage.py group incident.jsonl --profile incident-local.json --top 3
```

Review the generated JSON. Expect `time: ts`, `id: request`, and
`signature: code`. The generated `by: code` mapping lets you group without
a separate flag. Expect `TIMEOUT` to rank first with 82 records. Check the
skipped count before relying on the ranking. After saving on Linux, use
`cat incident-local.json` to read the profile. On Windows, use
`Get-Content incident-local.json`. If a profile omits a timestamp,
identify the correct field and supply `--time` for timed commands.

## 3. Find evidence in current and rotated logs

Split the sample into a compressed rotation and a current file.

```bash
scratch=$(mktemp -d)
head -n 8 log_triage/app.log | gzip -c > "$scratch/app.log.1.gz"
tail -n +9 log_triage/app.log > "$scratch/app.log"
python3.12 log_triage.py search "$scratch/app.log*" --contains "timed out" --context 1
```

Expect three matching records with context. The rotated file has matches on
lines 6 and 8. The current file has a match on line 5. Line numbers refer to
each individual file, and context stops at file boundaries. Add `--json`
to pass these locations to another tool.

You can practice multi-file input on Windows without creating a rotation.
This searches two bundled files and prints the source on every result:

```powershell
python log_triage.py search log_triage/app.log stacktrace-example.txt --contains "timed out" --context 1
```

Look for `app.log:6:` and a line prefixed with `stacktrace-example.txt:`.
Context helps explain a match, so some displayed lines lack the search text.

For stack traces, use `--multiline` to keep matching frames with their
header. The line range identifies the whole record.

```bash
python3.12 log_triage.py search stacktrace-example.txt --multiline --contains TimeoutError
```

Expected output:

```text
1-3: 2026-11-26T14:01:02Z ERROR request_id=r1 code=E1042 Database request failed
    at Client.read
    TimeoutError: connection timed out
```

Indentation in the excerpt is simplified. The match is on the last line,
and the range `1-3` gives you the header and both continuation lines.
If the frames start at the left margin, this mode treats them as separate
records.

## 4. Trace a spike back to a request

Start with a known complete observation window, including five full
baseline buckets. Filter for timeouts and show two records from each spike.

```bash
python3.12 log_triage.py bucket incident.jsonl --profile incident-profile.json --value TIMEOUT --interval 30s --since 2026-11-26T13:58:30Z --before 2026-11-26T14:03Z --spike-factor 3 --spike-min-count 10 --spike-examples 2
```

The first spike starts at **14:01 UTC**, with **30 events** in 30 seconds.
Its first example is `incident.jsonl:8` with ID `incident-00`.
The output shows the count and its equivalent per-minute rate:

```text
2026-11-26T14:01:00+00:00            30  60.000  SPIKE (baseline 0)
  incident.jsonl:8 id="incident-00"  ...
```

Here the bucket begins at 14:01 and ends just before 14:01:30. Use an aligned
`--before` for a window you know is complete. If there is no spike, check
`Counted`, `Invalid timestamp`, and `Evaluated buckets` before changing
thresholds.
The minimum count makes the onset eligible when the baseline median is zero.

Follow the ID across payment events and the worker log. The worker profile
maps its `job` field to the same ID.

```bash
python3.12 log_triage.py timeline --source incident.jsonl incident-profile.json --source worker-example.txt worker-profile.json --value incident-00
```

Expect nine events. The worker accepts the job at **14:01:01 UTC** and
logs a timeout at **14:01:04 UTC**. Use those locations to check surrounding
logs or service telemetry before drawing a conclusion about the cause.

If you do not already have an ID, rank requests by event count:

```bash
python3.12 log_triage.py correlate --source incident.jsonl incident-profile.json --source worker-example.txt worker-profile.json --sort count --top 2
```

Expect `incident-00` with 9 events across two files and `incident-05`
with 8 events in the incident file. Observed span is the time between the
first and last log entries for an ID. Use the ranking to choose a timeline
to inspect. A high count can reflect retries or extra logging.

Compare the windows to see which changes stood out after the onset.

```bash
python3.12 log_triage.py compare incident.jsonl --profile incident-profile.json --baseline 2026-11-26T14:00Z 2026-11-26T14:01Z --incident 2026-11-26T14:01Z 2026-11-26T14:03Z --top 3
```

The payments timeout rate rises from **2 to 40 events/minute**. Gateway
queue warnings rise from two to four events while staying at **2
events/minute** because the incident window is twice as long.

## 5. Use a gate in automation

Start with a JSON report whose counts you already know:

```bash
python3.12 log_triage.py search log_triage/app.log --contains "timed out" --context 1 --json
```

Look for this part of the report:

```json
{
  "scanned": 20,
  "matched": 3,
  "displayed": 8,
  "physical_lines": 20
}
```

These fields are under `coverage`. In PowerShell, read them directly:

```powershell
python log_triage.py search log_triage/app.log --contains "timed out" --context 1 --json | ConvertFrom-Json | Select-Object -ExpandProperty coverage
```

A **gate** uses an exit status to tell automation whether a condition was
met. The three Linux commands below save JSON under a temporary directory
and leave diagnostic messages on stderr. Bash's `|| status=$?` captures a
nonzero exit so the exercise can continue. In PowerShell, read
`$LASTEXITCODE` immediately after running the equivalent Python command.
A gate breach is a result you can investigate in the JSON report.

```bash
reports=$(mktemp -d)

status=0
python3.12 log_triage.py bucket incident.jsonl --profile incident-profile.json --value TIMEOUT --interval 30s --since 2026-11-26T13:58:30Z --before 2026-11-26T14:03Z --spike-factor 1000 --spike-min-count 1000 --fail-on spike --json > "$reports/pass.json" || status=$?
printf 'Pass status: %s\n' "$status"

status=0
python3.12 log_triage.py bucket incident.jsonl --profile incident-profile.json --value TIMEOUT --interval 30s --since 2026-11-26T13:58:30Z --before 2026-11-26T14:03Z --spike-factor 3 --spike-min-count 10 --fail-on spike --json > "$reports/breach.json" || status=$?
printf 'Breach status: %s\n' "$status"

status=0
python3.12 log_triage.py bucket incident.jsonl --profile incident-profile.json --value TIMEOUT --interval 30s --since 2026-11-26T14:01Z --before 2026-11-26T14:02Z --spike-factor 3 --fail-on spike --json > "$reports/unevaluable.json" || status=$?
printf 'Unevaluable status: %s\n' "$status"
```

Expected status lines:

```text
Pass status: 0
Breach status: 4
Unevaluable status: 3
```

| Status | Meaning | What to inspect |
| --- | --- | --- |
| `0` | Evaluated and passed | `evaluated_buckets` is positive, and no bucket has `spike: true`. |
| `4` | Spike detected | Find `spike: true` and compare counts with the baselines. |
| `3` | Could not evaluate | `evaluated_buckets` is zero. This window is too short to establish a baseline. |

Handle exit 3 explicitly so missing observation data is visible. For other
statuses and the comparison gate, see the [reference](reference.md#exit-codes).

## Write a short incident handoff

Capture the observation window and timezone, the files you searched, the
filter you used, and one or two source locations that support the finding.
Include skipped or invalid records when they affect confidence.

For this exercise, a handoff could read:

> In the 14:01?14:03 UTC incident window, payment timeout logs rose from
> 2 to 40 events/minute versus 14:00?14:01 UTC. The first spike starts at
> 14:01 UTC. See incident.jsonl:8 for request incident-00 and
> worker-example.txt:1 for its worker timeout. Check the worker logs and
> connection metrics around 14:01:04 UTC to investigate the cause.

If a bucket report counts fewer events than expected, rerun without the
signature filter. Remove `--value` and `--signature` if you passed both.
Then check the input paths, field names, and time bounds.
A valid timestamp on the exact upper bound falls outside a window defined
with the exclusive upper-bound flag. Use the
[troubleshooting table](reference.md#troubleshoot-a-report) for other cases.
