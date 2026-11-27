# FIX Printer runbook

Start with a single order, then use the same evidence to follow a fill,
bridge a log rotation, and compare the two gateway captures for a routed
order. Each exercise uses synthetic captures supplied with FIX Printer.

## Before you start

Run the examples from the extracted bundle or the `fix-printer/` source
directory. You need Python 3.12 or later. If you already installed the
wheel, check the CLI:

```sh
fix-printer --version
```

If you are running from source, replace `fix-printer` in the examples
with `python3.12 fixprinter.py` on Linux or `python fixprinter.py` in
Windows PowerShell. The rotation exercise also uses `python` to prepare
practice files. Use the Python interpreter in your active environment.

| When you have | Start with |
| --- | --- |
| One order ID and one capture | Exact lookup |
| An original order ID and questions about fills or cancels | Lifecycle |
| A partial export or a rotated capture | Session diagnostics and `--next-log` |
| Client-facing and exchange-facing gateway captures | Routing latency |
| Many orders in a time window | Latency summary and JSON Lines |

## 1. Find an order and read the acknowledgement

```sh
fix-printer CLIENT-42 samples/orders.fix
```

Expect four messages. The order is sent at line 3, and the acknowledgement
at line 4 shows **Order ack: 15ms**. Find tags 35 (MsgType) and 11
(ClOrdID) first, then use the file and line to check the evidence. This
interval uses the capture timestamps at the same observation point.

If the ID is an exchange OrderID, search tag 37 explicitly:

```sh
fix-printer BROKER-44 samples/session-44.fix --tag 37
```

The result includes four messages. The acknowledgement and cancel reply
show **15ms** and **12ms**. The printer can use a request to calculate
latency even when the search filter hides that request from the display.

## 2. Follow a partially filled order through cancellation

Start with the original order's ClOrdID. Include both directions of the
FIX session and the full time window for the order.

```sh
fix-printer Outbound_ClOrderId samples/session-44.fix --lifecycle --session ALPHAOMS44 NORTHBRK44 --from 2026-01-12T14:30:01 --until 2026-01-12T14:30:04
```

Expect five messages. The order fills **40 of 100 shares** at line 5,
**1s** after submission. The cancel request at line 6 has its own tag 11,
`CANCEL-44`, and tag 41 points back to the original order. The reply at
line 7 confirms cancellation and shows **Cancel ack: 12ms**. No full fill
was seen because the order ended with 60 shares canceled.

To see a full fill, run:

```sh
fix-printer CLIENT-42 samples/orders.fix --lifecycle
```

Expect **1s to first fill** and **2s to full fill**. The final report
shows Filled in tag 39. If the new order is missing from a live export,
expand the time range or add the preceding rotated capture.

## 3. Check a session and follow an order across rotation

Start with session diagnostics:

```sh
fix-printer samples/session-44.fix --session-diagnostics
```

The capture shows **13 messages** and **no events**. The next expected
sequences are **7** from ALPHAOMS44 to NORTHBRK44 and **8** in reverse.
A gap in a real capture tells you to check the export and adjacent
files before concluding that the counterparty never sent a reply.

Practice a rotation by splitting the FIX 4.2 sample just after the
new order. The command creates a new directory named `fix-practice`
and refuses to overwrite an existing directory of that name.

```sh
python -c "from pathlib import Path; import gzip; p=Path('fix-practice'); p.mkdir(); rows=Path('samples/orders.fix').read_bytes().splitlines(keepends=True); (p/'orders.log.1.gz').write_bytes(gzip.compress(b''.join(rows[:3]))); (p/'orders.log').write_bytes(b''.join(rows[3:]))"
fix-printer CLIENT-42 fix-practice/orders.log.1.gz --next-log fix-practice/orders.log --lifecycle
```

The order is at `fix-practice/orders.log.1.gz:3`. Its acknowledgement
is at `fix-practice/orders.log:1` and still shows **15ms**. The first
and full fills still show **1s** and **2s**. Line numbers are relative
to each file. Pass rotations oldest to newest and avoid overlapping
exports.

## 4. Separate internal routing from exchange response time

The inbound capture uses `Inbound_ClOrderId`. The outbound gateway
rewrites it to `Outbound_ClOrderId`. Supply the known outbound ID:

```sh
fix-printer Inbound_ClOrderId samples/client-ingress.fix --routing-latency samples/session-44.fix --outbound-clordid Outbound_ClOrderId
```

The printer links inbound line 1 to outbound line 3 and reports
**Internal routing: 4ms**. It then links the outbound order to the
exchange acknowledgement and reports **15ms** for that round trip.

If your gateway places the inbound ID in a known outbound tag instead,
use a link tag. This sample uses tag 37 on its outbound new order:

```sh
fix-printer Inbound_ClOrderId samples/client-ingress.fix --routing-latency samples/session-44.fix --outbound-link-tag 37
```

The measurements are identical. Use the link tag only when your gateway
profile defines it. The 4ms is a cross-gateway interval and needs aligned
capture clocks. The 15ms is the exchange-facing send-to-reply interval,
which includes transit and exchange processing.

## 5. Export a latency summary for another analyst

```sh
fix-printer samples/orders.fix --latency-summary --session ALPHAOMS42 NORTHBRK42 --json > fix-latency.jsonl
```

The file has three `latency_metric` records and one `latency_summary`.
The metrics show an order acknowledgement at **15ms**, a first fill at
**1s**, and a full fill at **2s**. Each metric has one measurement, so
p95 and p99 are marked sparse. For automation, check both the exit code
and whether the required measurements are available. Share the source
lines for outliers so the next analyst can inspect the original events.

## Investigate without production host access

Use an approved central export or archived capture. Preserve message
order, timestamps and timezone, sender and target tags, and any
gateway-specific ID used to join the two legs. Ask for the files before
and after a rotation if a request or reply falls at the boundary. Scope
to the relevant session and window before sharing sanitized evidence.

## Write a short incident handoff

Include the order IDs, CompID pair and direction, time window, timing
endpoints, and any gaps or unavailable measurements. For the routing
exercise, a handoff could read:

> Inbound order `Inbound_ClOrderId` at `client-ingress.fix:1` was sent
> to the exchange as `Outbound_ClOrderId` at `session-44.fix:3` after
> 4ms. The exchange acknowledgement at `session-44.fix:4` returned 15ms
> after the outbound send.

See the [reference](reference.md) for option combinations, timestamp
rules, and JSON fields.
