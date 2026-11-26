# Log Triage

A command-line tool for investigating incidents in local logs. Search current
and rotated files, identify spikes, compare incident and baseline windows,
and follow a request across services. Use text reports during an investigation
and versioned JSON and exit codes in automation.

## Quick start

Linux is the primary platform. On **RHEL 8.10**, install Python 3.12 from
AppStream. Log Triage uses the Python standard library.

```bash
sudo dnf install python3.12
cd log-triage
python3.12 log_triage.py app.jsonl --format jsonl
```

Expected counts:

```text
INFO: 12
WARNING: 3
ERROR: 5
```

Run commands from `log-triage/` in the repository. On Windows, use
`python log_triage.py` in an activated environment with Python 3.12 or later.
[Red Hat Python setup guide](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/8/html/configuring_basic_system_settings/installing-and-using-dynamic-programming-languages_configuring-basic-system-settings)

## Install the command from this checkout

From `log-triage/`, create and activate a virtual environment if you
do not already have one.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Then install the project and try the bundled sample from any directory:

```text
python -m pip install .
log-triage --version
log-triage
```

The install command runs from `log-triage/`. Once installed,
`log-triage` is available whenever the environment is active.
The guides use the source checkout and its exercise files. You can
replace `python3.12 log_triage.py` with `log-triage` after installing.

## Documentation

Use a centralized log export or a UTF-8 stream from your approved logging CLI. See [Investigate without production host access](docs/runbook.md#investigate-without-production-host-access).

- [Start with the runbook](docs/runbook.md). It walks through sample commands, expected results, and what to investigate next.
- [Reference](docs/reference.md) for formats, field mappings, time windows, JSON, exit codes, and troubleshooting.
- `python3.12 log_triage.py -h` lists commands. Add `-h` after any command for its flags.

## License

Log Triage is licensed under the [MIT License](LICENSE).
