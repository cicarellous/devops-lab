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

## Documentation

- [Start with the runbook](docs/runbook.md). It walks through sample commands, expected results, and what to investigate next.
- [Reference](docs/reference.md) for formats, field mappings, time windows, JSON, exit codes, and troubleshooting.
- `python3.12 log_triage.py -h` lists commands. Add `-h` after any command for its flags.
