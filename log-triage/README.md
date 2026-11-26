# Log Triage

A command-line tool for investigating incidents in local logs. Search current
and rotated files, identify spikes, compare incident and baseline windows,
and follow a request across services. Use text reports during an investigation
and versioned JSON and exit codes in automation.

## Download and install

Download the **Log Triage bundle** from [GitHub Releases](https://github.com/cicarellous/devops-lab/releases).
Under **Assets**, choose `log-triage-<version>-bundle.zip`. Extract it
and open a terminal in the extracted folder. The bundle includes the
installable wheel, the analyst guides, sample logs, and the MIT license.

All you need is **Python 3.12 or later** with pip. The wheel installs
offline and works on Linux and Windows. On RHEL 8.10, install
`python3.12` from AppStream if needed.

On Linux:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install the wheel from the extracted folder and run the bundled sample.
If you downloaded a different version, use the wheel filename in your bundle.

```sh
python -m pip install --no-index --no-deps log_triage-0.1.0-py3-none-any.whl
log-triage --version
log-triage
```

Expected counts:

```text
INFO: 12
WARNING: 3
ERROR: 5
```

## Install directly from GitHub

If you have Git and an active Python 3.12+ virtual environment, you can
install from the repository in one command. This needs internet access
and installs the current code from the default branch.

```sh
python -m pip install "git+https://github.com/cicarellous/devops-lab.git#subdirectory=log-triage"
log-triage --version
```

## Use a source checkout

If you already cloned the repository, run `python -m pip install .` from
`log-triage/` with your environment active. You can also run the script
directly from that directory with `python3.12 log_triage.py` on Linux or
`python log_triage.py` on Windows. The download bundle and checkout both include the
sample files used in the runbook.

## Documentation

Use a centralized log export or a UTF-8 stream from your approved logging CLI. See [Investigate without production host access](docs/runbook.md#investigate-without-production-host-access).

- [Start with the runbook](docs/runbook.md). It walks through sample commands, expected results, and what to investigate next.
- [Reference](docs/reference.md) for formats, field mappings, time windows, JSON, exit codes, and troubleshooting.
- `log-triage -h` lists commands. Add `-h` after any command for its flags.

## License

Log Triage is licensed under the [MIT License](LICENSE).
