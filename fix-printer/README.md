# FIX Printer

A command-line tool that decodes and prints FIX 4.2 and 4.4 messages
from logs in a readable tag table. Find messages by order ID or another
FIX tag and see field names and decoded values alongside the raw values.
You can also follow orders through fills and cancels, measure response
and routing latency, and export versioned JSON Lines for automation.

## Download and install

Download the **FIX Printer bundle** from [GitHub Releases](https://github.com/cicarellous/devops-lab/releases/tag/fix-printer-v0.1.3).
Under **Assets**, choose `fix-printer-<version>-bundle.zip`. Extract it
and open a terminal in the extracted folder. The bundle includes the
installable wheel, the analyst guides, sample logs, and the MIT license
and QuickFIX notices.

All you need is **Python 3.12 or later** with pip. The wheel installs
offline on Linux, including RHEL 8.10, and on Windows. On RHEL 8.10, install
`python3.12` and `python3.12-pip` from AppStream if needed.

On Linux

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install the wheel from the extracted folder. If you downloaded a different
version, use the wheel filename in your bundle.

```sh
python -m pip install --no-index --no-deps fix_printer-0.1.3-py3-none-any.whl
fix-printer --version
```

## Sample workflows for incident triage

Run these examples from the extracted bundle or the `fix-printer/` folder
in the source checkout. Install the command first. Start with one order,
then follow it across two gateways. The sample captures are synthetic.

### 1. Find an order and check its acknowledgement

```sh
fix-printer CLIENT-42 samples/orders.fix
```

Line 3 shows the new order and line 4 shows its acknowledgement, **15ms**
later. Both messages come from the same capture. The file and line numbers
let you check the timestamps and tags in the original log.

### 2. Follow a partial fill through cancellation

```sh
fix-printer Outbound_ClOrderId samples/session-44.fix --lifecycle
```

The exchange fills **40 of 100 shares** after **1s**. The cancel takes
**12ms** to acknowledge, and the order never reaches a full fill. The
timeline links the cancel request back to the original order.

### 3. Check the session for sequence gaps

```sh
fix-printer samples/session-44.fix --session-diagnostics
```

The report shows **13 messages** and **no sequence events**. In a live
capture, a gap before an expected acknowledgement or fill gives you a
reason to check adjacent rotations or re-export the capture.

### 4. Measure routing across two gateways

```sh
fix-printer Inbound_ClOrderId samples/client-ingress.fix --routing-latency samples/session-44.fix --outbound-clordid Outbound_ClOrderId
```

The printer measures **4ms** from inbound receipt to outbound send, then
**15ms** from outbound send to the exchange acknowledgement. The two IDs
identify the same order before and after the gateway rewrites its ClOrdID.
The report identifies the source file and line for both new orders.

## Install directly from GitHub

If you have Git and an active Python 3.12+ virtual environment, you can
install from the repository in one command. This needs internet access
and installs the current code from the default branch.

```sh
python -m pip install "git+https://github.com/cicarellous/devops-lab.git#subdirectory=fix-printer"
fix-printer --version
```

## Use a source checkout

If you already cloned the repository, run `python -m pip install .` from
`fix-printer/` with your environment active. This needs internet access
to fetch build dependencies in a fresh environment. You can also run
the script directly with `python3.12 fixprinter.py` on Linux or
`python fixprinter.py` on Windows. The download bundle and checkout both
include the sample files used in the runbook.

To build the ZIP from this checkout, run these commands in `fix-printer/`
with your Python environment active and internet access for the build tools.

```sh
python -m pip install build
python -m build
python build_bundle.py
```

The ZIP, wheel, and pip-installable source distribution appear in `dist/`.
After a version bump, build the wheel again before running `build_bundle.py`
and use the files for the new version. The script ignores older wheels in
`dist/`. The source distribution is also available on the release page.

## Documentation

Use an approved central export or archived capture. See [Investigate without production host access](docs/runbook.md#investigate-without-production-host-access).

- [Start with the runbook](docs/runbook.md). It walks through sample commands, expected results, and what to investigate next.
- [Reference](docs/reference.md) for capture formats, session filters, ID mappings, timing rules, JSON, exit codes, and troubleshooting.
- `fix-printer -h` lists all options. `python -m fix_printer -h` also works.

## License

FIX Printer is licensed under the [MIT License](LICENSE). The bundled
FIX dictionaries retain the [QuickFIX Software License](fix_printer/dictionaries/LICENSE).
See [NOTICE](NOTICE) for attribution.

This product includes software developed by quickfixengine.org
(http://www.quickfixengine.org/).
