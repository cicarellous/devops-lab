# FIX Printer

A command-line tool for investigating orders in FIX 4.2 and 4.4 logs.
Decode messages, follow an order through fills and cancels, and measure
response and routing latency. Use text reports during an investigation
and versioned JSON Lines and exit codes in automation.

## Download and install

Start with the **FIX Printer bundle**, `fix-printer-<version>-bundle.zip`.
Extract it and open a terminal in the extracted folder. The bundle includes
the installable wheel, analyst guides, sample captures, and license notices.

The 0.1.0 bundle is available as a local build. If you do not have a copy,
[install directly from GitHub](#install-directly-from-github).

All you need is **Python 3.12 or later** with pip. The wheel installs offline
and works on Linux and Windows.

Create and activate a virtual environment on Linux.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

In Windows PowerShell, create and activate it with these commands.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install the wheel from the extracted folder. If you have a different
version, use the wheel filename in your bundle.

```sh
python -m pip install --no-index --no-deps fix_printer-0.1.0-py3-none-any.whl
fix-printer --version
```

## Sample workflows for incident triage

Run these examples from the extracted bundle or the `fix-printer/` folder
in your source checkout after installing the command. Start with one order,
then trace it across gateways. The sample captures are synthetic.

### 1. Find an order and check its acknowledgement

```sh
fix-printer CLIENT-42 samples/orders.fix
```

The printer returns **four messages**. The acknowledgement at
`samples/orders.fix:4` shows **Order ack: 15ms** from the logged submission
to the acknowledgement in the same capture.

### 2. Follow a partial fill through cancellation

```sh
fix-printer Outbound_ClOrderId samples/session-44.fix --lifecycle
```

The order fills **40 of 100 shares after 1s**. The remaining 60 shares
are canceled, and the cancel acknowledgement takes **12ms**. The report
shows no full fill. The timeline links the cancel's own ClOrdID back to
the original order.

### 3. Measure routing between two gateways

```sh
fix-printer Inbound_ClOrderId samples/client-ingress.fix --routing-latency samples/session-44.fix --outbound-clordid Outbound_ClOrderId
```

The order spent **4ms inside the routing stack**, from inbound receipt to
outbound send. The **15ms exchange acknowledgement round trip** starts at
that send and ends when the reply reaches the same gateway. This example
supplies the rewritten outbound ID. When both captures use the same
ClOrdID, omit `--outbound-clordid`.

### 4. Check the session when evidence is missing

```sh
fix-printer samples/session-44.fix --session-diagnostics
```

The sample shows **13 messages** and **no sequence events**. In a live
capture, a sequence gap can help explain why an expected reply or fill is
missing from the export. Check adjacent rotations before investigating
the counterparty.

## Install directly from GitHub

If you have Git and an active Python 3.12+ virtual environment, install
from the repository in one command. This needs internet access and
installs the current code from the default branch.

```sh
python -m pip install "git+https://github.com/cicarellous/devops-lab.git#subdirectory=fix-printer"
fix-printer --version
```

To run the sample workflows, use the captures in the bundle or a source
checkout.

## Use a source checkout

If you already cloned DevOps Lab, run `python -m pip install .` from
`fix-printer/` with your environment active. You can also run the script
directly from that folder with `python3.12 fixprinter.py` on Linux or
`python fixprinter.py` on Windows. Use that prefix in place of
`fix-printer` in the examples.

## Documentation

Use an approved central export or archived capture if you lack gateway
access. See [Investigate without production host access](docs/runbook.md#investigate-without-production-host-access).

- [Start with the runbook](docs/runbook.md). It walks through order lookup, lifecycle analysis, rotations, gateway timing, and JSON export.
- [Reference](docs/reference.md) for options, timing rules, venue overrides, JSON fields, and troubleshooting.
- `fix-printer -h` lists all options.

## License

FIX Printer is licensed under the [MIT License](LICENSE). The bundled
FIX dictionaries retain the [QuickFIX Software License](fix_printer/dictionaries/LICENSE).
See [NOTICE](NOTICE) for attribution.

This product includes software developed by quickfixengine.org
(http://www.quickfixengine.org/).
