# FIX Printer

Find an order in a FIX 4.2 or 4.4 log and print each matching message as a readable tag table. The printer runs locally on Python 3.12+ using the standard library.

| Task | Command pattern |
| --- | --- |
| Print matching messages and same-session response times | `fixprinter.py ID LOG` |
| Follow an order from submission through fills and cancels | `fixprinter.py ORIGINAL_CLORDID LOG --lifecycle` |
| Label response times in an exchange-facing capture | `fixprinter.py ID LOG --exchange-facing` |
| Check sequence gaps, resets, and replay flags | `fixprinter.py LOG --session-diagnostics` |
| Summarize response and fill latency by session | `fixprinter.py LOG --latency-summary` |
| Measure internal routing when the gateway logs are separate | `fixprinter.py INBOUND_ID INBOUND_LOG --routing-latency OUTBOUND_LOG` |

Run these examples from the `fix-printer` directory:

```sh
python3.12 fixprinter.py CLIENT-42 samples/orders.fix
python3.12 fixprinter.py BROKER-44 samples/session-44.fix --tag 37
python3.12 fixprinter.py CANCEL-44 samples/session-44.fix --tag 11
python3.12 fixprinter.py ALPHAOMS44 samples/session-44.fix --tag 49 --tag 56
```

On Windows, use `python` from your Python 3.12+ virtual environment.

| Example | Expected output |
| --- | --- |
| Client order ID `CLIENT-42` | Four messages covering submission, acknowledgement, partial fill, and final fill. |
| Broker order ID `BROKER-44` | Four messages covering acknowledgement, partial fill, cancel request, and cancel confirmation. |
| Cancel request ID `CANCEL-44` | Two messages. Tag `11` identifies the cancel request and tag `41` points back to `Outbound_ClOrderId`. |
| OMS CompID `ALPHAOMS44` | All 13 messages in the FIX 4.4 session, matching the OMS in either direction. |

## Sample sessions

Both synthetic captures show a two-way Logon and Logout, routine heartbeats, and a Test Request with a matching Heartbeat response. Tags `49` and `56` identify the two configured endpoints and swap on replies. Tag `34` increments independently for each sender. Log timestamps and SendingTime are UTC.

- [FIX 4.2](samples/orders.fix) follows a 100-share limit order filled in two executions. The final report shows `CumQty=100`, `LeavesQty=0`, and `AvgPx=25.46`.
- [FIX 4.4](samples/session-44.fix) shows a two-entry Parties group on the order, a 40-share fill, and cancellation of the remaining 60 shares. The final report shows `OrdStatus=Canceled`, `CumQty=40`, and `LeavesQty=0`.

The fixtures include the mandatory fields defined by the bundled dictionaries for the messages shown. Logon includes `98` (EncryptMethod) and `108` (HeartBtInt), and the Test Request and reply share `112` (TestReqID). Venue-specific session profiles may require additional fields. The displayed pipes represent wire SOH bytes when calculating BodyLength and CheckSum. Keep the session and date in view when investigating live logs because identifiers can be reused.

## Matching messages

By default, the printer matches the exact value in `11` (ClOrdID), `37` (OrderID), or `41` (OrigClOrdID). Repeat `--tag` to search several explicit tags. Each match includes the file and line number and any prefix present before the message, such as a log timestamp or direction arrow. Repeated fields stay in the order they appeared. In color mode, MsgType (tag `35`) is cyan and the matching value is yellow. Use `--color never` for plain-text output or `--color always` when piping to an ANSI-aware pager. In auto mode, `CLICOLOR_FORCE` and `FORCE_COLOR` enable color unless `NO_COLOR` is set. JSON Lines always stays plain.

## Order, cancel, and replace response latency

The printer automatically shows elapsed capture time on order, cancel, and replace acknowledgements or rejects. Latencies below one millisecond use microseconds (for example, `140μs`). From one millisecond up to one second, they use milliseconds (for example, `12.5ms`). From one second onward, they use seconds (for example, `1.25s` or `65s`). Seconds appear in red when color is enabled. Fractional values preserve the precision of the measured interval.

```sh
python3.12 fixprinter.py BROKER-44 samples/session-44.fix --tag 37
```

The sample prints **Order ack: 15ms** and **Cancel ack: 12ms**. The printer reads requests even when the search filter hides them. It pairs by ClOrdID (`11`), FIX version, and reversed sender/target CompIDs. A cancel uses its own ClOrdID and checks OrigClOrdID (`41`) when both records carry it. Logon and Logout clear pending requests for that CompID pair. Messages marked as possible duplicates are skipped for timing.

Use a capture with one clock and a single logical session per CompID pair at any given time. Each timestamp must begin the log prefix in `YYYY-MM-DD HH:MM:SS[.ffffff]` or `MM/DD/YYYY HH:MM:SS[.ffffff]` format. The fractional part can have up to nine digits. Filters compare the full timestamp, and elapsed values are reported in whole microseconds. A `T` separator, `Z`, and offsets such as `-05:00` are supported. Offset-free timestamps are compared as written. Eligible responses with a missing or ambiguous request, incompatible timestamps, or negative elapsed time show a reason. A repeated reply to a recently completed request is reported as a duplicate response. On very large captures, the oldest entries are evicted once 100,000 requests are pending.

This measurement uses the capture timestamps on both records. It measures elapsed time at the capture point. Pending statuses and fills are outside the acknowledgement metric.

## Filter by session and capture time

`--session COMP_ID PEER_ID` selects one SenderCompID/TargetCompID pair in both directions. In two-log routing mode, it filters the inbound log. Use `--outbound-session COMP_ID PEER_ID` to filter the outbound log separately.

```sh
python3.12 fixprinter.py CLIENT-42 samples/orders.fix --session ALPHAOMS42 NORTHBRK42 --from 2026-01-12T14:30:01 --until 2026-01-12T14:30:03
```

This prints the new order, acknowledgement, and partial fill. `--from` includes its timestamp and `--until` excludes its timestamp. Either bound can be omitted. Time filters apply to both inputs in routing mode and only include records with parseable capture timestamps and compatible timezone conventions. If the session matches no records or the time window excludes every candidate, the printer explains why on stderr. Include the original request in the window to calculate its response time. Logon and Logout still end correlation for their session, even if their timestamps are missing or outside the window.

## Follow an order lifecycle

Start with the original ClOrdID on a New Order Single (`35=D`). The printer follows the matching session forward using ClOrdID (`11`), OrigClOrdID (`41`), and session OrderID (`37`) learned from linked execution reports. The timeline includes acknowledgements, fills, cancel and replace requests, their responses, and rejects. Use session and time filters if the original ID appears more than once.

```sh
python3.12 fixprinter.py CLIENT-42 samples/orders.fix --lifecycle
python3.12 fixprinter.py Outbound_ClOrderId samples/session-44.fix --lifecycle
```

The FIX 4.2 example shows **1s to first fill** and **2s to full fill**. The FIX 4.4 example shows **1s to first fill** and a cancellation of the remainder. It reports that no full fill was seen. Both timings start at the original new order and use capture timestamps. A fill must be a trade execution report with a positive LastShares (FIX 4.2) or LastQty (FIX 4.4). A full fill also requires Filled in OrdStatus (tag `39`). FIX 4.4 uses `ExecType=F` for a trade. If a venue sends legacy `1` or `2` on FIX 4.4, use `--legacy-fills` with lifecycle or latency summary. Without that opt-in, legacy fills are reported as unavailable. Unrecognized execution types with a positive last quantity and a partial or filled status are also flagged. The printer skips replay-flagged messages and stops at the selected session's next Logon or Logout. Supply a plain or gzip file in capture order so the printer can first locate the unique new order, then follow it.

## Read rotated captures

Pass the oldest file first and repeat `--next-log` for each later file. The printer reads files in the supplied order and retains correlation state between them. Every match and timing endpoint keeps its original filename and line number.

```sh
python3.12 fixprinter.py ORDER-123 gateway.log.2.gz --next-log gateway.log.1.gz --next-log gateway.log --lifecycle
```

For two-log routing, `--next-log` extends the inbound capture and `--next-outbound-log` extends the outbound capture. Use nonoverlapping files from each stream in oldest-to-newest order. Duplicate files and reversed or overlapping timestamp ranges at rotation boundaries are rejected when those boundaries have comparable timestamps. The same session and time filters apply across a stream's files.

## Inspect session sequence numbers

```sh
python3.12 fixprinter.py samples/session-44.fix --session-diagnostics
```

The sample reports **13 messages and no sequence events**. Use this report before investigating an incomplete order. It tracks sequence numbers independently by FIX version and sender/target direction, starting with the first visible message. It reports forward gaps, messages behind the expected sequence, replay flags, gap fills, and resets, with source lines and the final expected sequence for each direction. A gap is a discontinuity in the capture. Missing exports, capture filters, or a replay can affect what you see. Malformed messages and oversized records are counted separately.

## Summarize latency for a session

```sh
python3.12 fixprinter.py samples/orders.fix --latency-summary --session ALPHAOMS42 NORTHBRK42
```

The sample reports an order acknowledgement at **15ms**, a first fill at **1s**, and a full fill at **2s**. For each FIX version, request direction, and timing type, the report shows measured counts, unavailable observations by reason, nearest-rank p50/p95/p99, and the three slowest intervals with start and end file and line references. Add `--exchange-facing` for exchange round-trip labels.

Percentiles use measured observations. Requests that are still unanswered at capture end, lost at a session boundary, or evicted from tracking are counted separately. Visible replies and fills with missing evidence are counted as unavailable. Orders with no visible fill are outside fill distributions.

The report marks p95 as sparse below 20 measured values and p99 below 100. After 2,048 distinct durations for a metric, it switches from exact ranks to bounded buckets and labels percentile estimates as approximate. The three slowest intervals retain exact microsecond values. Fill tracking retains up to 100,000 recent identifier links.

## JSON Lines

Add `--json` to any workflow. Stdout contains one JSON object per line with `schema_version: 1`. Diagnostics go to stderr. Output contains no terminal color codes.

```sh
python3.12 fixprinter.py CLIENT-42 samples/orders.fix --lifecycle --json
```

- `message` includes the capture file, line, timestamp, prefix, FIX version, framing warnings, and fields in original order. Each field includes its numeric tag, name, raw value, optional enum description, and match status. Fields containing invalid UTF-8 also include `raw_base64` for exact recovery.
- `lifecycle_summary` follows the messages and contains first-fill and full-fill timings, or a reason the original order could not be resolved.
- `routing` contains both legs and their measurements in one record. It includes a reason for any leg that could not be resolved. The exchange response time can still be available when the inbound order is missing.

- `session_event` and `session_summary` report sequence evidence and counts.
- `latency_metric` reports one metric and session direction, with percentiles and slowest intervals. `latency_summary` reports capture counts.

Each timing has a label, start and end capture points when known, an integer `microseconds` value when measurable, and a reason when unavailable. Capture points include `nanosecond_remainder`, the 0?999 nanoseconds beyond the timestamp's microsecond precision.

## Exchange and cross-gateway timing

### Label an exchange-facing capture

Use `--exchange-facing` when the log is from your gateway's FIX session with an exchange. The flag changes the timing labels only. Matching, timestamps, and elapsed values stay the same. Use the ordinary command on a client-facing capture.

```sh
python3.12 fixprinter.py Outbound_ClOrderId samples/session-44.fix --exchange-facing
```

The acknowledgement header shows `Exchange order ack round trip: 15ms`, where the ordinary command shows `Order ack: 15ms`. The clock starts at your gateway's logged send of `35=D` and stops at its logged receipt of the exchange acknowledgement. The interval includes transit in both directions and exchange processing.

### Measure internal routing across two captures

Use one log to measure request-to-acknowledgement timing automatically. In a client-facing log, that is our gateway receiving the order and sending its reply. In an exchange-facing log, it is our gateway sending the order and receiving the exchange reply.

Use `--routing-latency OUTBOUND_LOG` when the gateway events are split between `INBOUND.LOG` and `OUTBOUND.LOG`. The positional log is `INBOUND.LOG`. The option supplies the second file, so the printer can measure from our inbound gateway receiving `35=D` to our outbound gateway sending the corresponding `35=D`.

Supply the inbound ClOrdID and client-facing receive capture first, then the exchange-facing capture after `--routing-latency`. By default, the printer finds `35=D` with the same ClOrdID (tag `11`) in both captures.

```sh
python3.12 fixprinter.py ORDER-123 inbound.log --routing-latency outbound.log.gz
```

If your gateway rewrites ClOrdID and you know the outbound value, add `--outbound-clordid`:

```sh
python3.12 fixprinter.py Inbound_ClOrderId samples/client-ingress.fix --routing-latency samples/session-44.fix --outbound-clordid Outbound_ClOrderId
```

If the outbound `35=D` carries the inbound ClOrdID in another tag, add `--outbound-link-tag` with that tag number. The bundled gateway example uses tag `37`:

```sh
python3.12 fixprinter.py Inbound_ClOrderId samples/client-ingress.fix --routing-latency samples/session-44.fix --outbound-link-tag 37
```

Here the printer finds the inbound `35=D` by tag `11` and the outbound `35=D` by tag `37`. It takes the outbound order's own tag `11` to match the exchange response. FIX defines tag `37` as OrderID. Carrying the inbound ClOrdID there on a New Order Single is specific to this gateway profile. On the sample exchange Execution Reports, tag `37` carries the exchange's OrderID (`BROKER-44`).

| Metric | Starts at | Ends at | Sample |
| --- | --- | --- | --- |
| Internal routing | Client-facing gateway receives `35=D` | Exchange-facing gateway sends the corresponding `35=D` | 4ms |
| Exchange order ack round trip | Exchange-facing gateway sends `35=D` | Same gateway receives the exchange acknowledgement | 15ms |

`--routing-latency` labels the exchange round trip automatically. The report lists the file, line, ClOrdID, and timestamp for both new orders. It requires one original matching new order per capture and reports missing or ambiguous links. An ambiguous outbound acknowledgement leaves the routing interval available when the two new orders can still be identified. If the inbound order is missing, the exchange round trip can still be reported from the outbound capture. The command exits with code `1` because the two-leg route is incomplete. Narrow the exports to the relevant session and time window if an ID is reused. Use synchronized gateway clocks and log timestamps recorded at receive and send. This mode accepts plain and gzip files.

Client-to-gateway one-way timing would require the client's send timestamp and your gateway's receive timestamp on aligned clocks. Gateway-to-exchange one-way timing would require your gateway's send timestamp and the exchange's order receipt timestamp. Tag `52` on an exchange acknowledgement supplies the exchange's reply send time, which is a later event. The printer reports the measurements in the table from the available capture events.

## Input

- Read a plain log, a `.gz` file, or stdin using `-`. For example, a logging tool that exports FIX records can feed them into `python3.12 fixprinter.py CLIENT-42 -`.
- Each message must be on one physical line, with SOH bytes or pipe separators. A single line can hold multiple messages. Lines over 8 MiB are skipped with a diagnostic. Lifecycle and routing emit recoverable evidence, mark affected timings unavailable, and exit with code `2`. Capture formats that wrap messages or contain line breaks inside binary fields need preprocessing.
- For SOH captures, the printer checks BodyLength and CheckSum where available. If an export omits only the separator after CheckSum, otherwise valid records are accepted. Pipe exports receive structural checks. Matching partial messages are printed with warnings when the identifier is recoverable. Single-log searches match the requested tag values directly. Use the two-log workflow for a mapped inbound and outbound pair.

| Exit code | Meaning |
| --- | --- |
| `0` | The search or report found relevant data, or routing found one matching order on each leg. Individual timings can still be unavailable. |
| `1` | The search or report found no relevant data, or routing could not find a unique order on each leg. |
| `2` | Input, dictionary, or usage error, including corrupt gzip files and skipped oversized records. |

Run `python -m unittest -v` from this directory. The fixture tests check required fields and verify BodyLength and CheckSum after restoring SOH delimiters. The CLI uses the dictionaries for display and performs framing checks.

## FIX dictionaries

The printer uses tag `8` (BeginString) to select version-specific names and enum descriptions from the bundled dictionaries, covering all 405 field definitions in the FIX 4.2 dictionary and all 912 in FIX 4.4. Keep the `dictionaries/` folder alongside the script. Supply a trusted QuickFIX XML dictionary with `--dictionary path/to/venue.xml` to add fields or override labels and enum values for that version. You can pass one override per version. Bundled and custom enum labels use the same display formatting, including common FIX acronyms. Unrecognized tags and values remain visible. Fields inside repeating groups are printed in capture order.

### Dictionary source

This product includes software developed by
quickfixengine.org (http://www.quickfixengine.org/).

The bundled [FIX 4.2](https://github.com/quickfix/quickfix/blob/386ce46e917ae494ab6e90b1be90fd421cdbe3f9/spec/FIX42.xml)
and [FIX 4.4](https://github.com/quickfix/quickfix/blob/386ce46e917ae494ab6e90b1be90fd421cdbe3f9/spec/FIX44.xml)
dictionaries are distributed under the [QuickFIX Software License](dictionaries/LICENSE).
