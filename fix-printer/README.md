# FIX Printer

Find an order in a FIX 4.2 or 4.4 log and print each matching message as a readable tag table. The printer runs locally on Python 3.12+ using the standard library.

| Task | Command pattern |
| --- | --- |
| Print matching messages and same-session response times | `fixprinter.py ID LOG` |
| Label response times in an exchange-facing capture | `fixprinter.py ID LOG --exchange-facing` |
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

By default, the printer matches the exact value in `11` (ClOrdID), `37` (OrderID), or `41` (OrigClOrdID). Repeat `--tag` to search several explicit tags. Each match includes the file and line number and any prefix present before the message, such as a log timestamp or direction arrow. Repeated fields stay in the order they appeared. In color mode, MsgType (tag `35`) is cyan and the matching value is yellow. Use `--color never` for plain-text output or `--color always` when piping to an ANSI-aware pager.

## Order and cancel response latency

The printer automatically shows elapsed capture time on order and cancel acknowledgements or rejects. Latencies below one millisecond use microseconds (for example, `140μs`). From one millisecond up to one second, they use milliseconds (for example, `12.5ms`). From one second onward, they use seconds (for example, `1.25s` or `65s`). Seconds appear in red when color is enabled. Fractional values preserve the precision of the measured interval.

```sh
python3.12 fixprinter.py BROKER-44 samples/session-44.fix --tag 37
```

The sample prints **Order ack: 15ms** and **Cancel ack: 12ms**. The printer reads requests even when the search filter hides them. It pairs by ClOrdID (`11`), FIX version, and reversed sender/target CompIDs. A cancel uses its own ClOrdID and checks OrigClOrdID (`41`) when both records carry it. Logon and Logout clear pending requests for that CompID pair. Messages marked as possible duplicates are skipped for timing.

Use a capture with one clock and a single logical session per CompID pair at any given time. Each timestamp must begin the log prefix in `YYYY-MM-DD HH:MM:SS[.ffffff]` or `MM/DD/YYYY HH:MM:SS[.ffffff]` format. A `T` separator, `Z`, and offsets such as `-05:00` are supported. Offset-free timestamps are compared as written. Eligible responses with a missing or ambiguous request, incompatible timestamps, or negative elapsed time show a reason. On very large captures, the oldest entries are evicted once 100,000 requests are pending.

This measurement uses the capture timestamps on both records. It measures elapsed time at the capture point. Pending statuses and fills are outside the acknowledgement metric.

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

`--routing-latency` labels the exchange round trip automatically. The report lists the file, line, ClOrdID, and timestamp for both new orders. It requires one original matching new order per capture and reports missing or ambiguous links. An ambiguous outbound acknowledgement leaves the routing interval available when the two new orders can still be identified. Narrow the exports to the relevant session and time window if an ID is reused. Use synchronized gateway clocks and log timestamps recorded at receive and send. This mode accepts plain and gzip files.

Client-to-gateway one-way timing would require the client's send timestamp and your gateway's receive timestamp on aligned clocks. Gateway-to-exchange one-way timing would require your gateway's send timestamp and the exchange's order receipt timestamp. Tag `52` on an exchange acknowledgement supplies the exchange's reply send time, which is a later event. The printer reports the measurements in the table from the available capture events.

## Input

- Read a plain log, a `.gz` file, or stdin using `-`. For example, a logging tool that exports FIX records can feed them into `python3.12 fixprinter.py CLIENT-42 -`.
- Each message must be on one physical line, with SOH bytes or pipe separators. A single line can hold multiple messages. Lines over 8 MiB are skipped with a diagnostic. Capture formats that wrap messages or contain line breaks inside binary fields need preprocessing.
- For SOH captures, the printer checks BodyLength and CheckSum where available. Pipe exports receive structural checks. Matching partial messages are printed with warnings when the identifier is recoverable. Single-log searches match the requested tag values directly. Use the two-log workflow for a mapped inbound and outbound pair.

| Exit code | Meaning |
| --- | --- |
| `0` | The search found matching messages, or routing found one matching order on each leg. Individual timings can still be unavailable. |
| `1` | The search found no matches, or routing could not find a unique order on each leg. |
| `2` | Input, dictionary, or usage error, including corrupt gzip files and skipped oversized records. |

Run `python -m unittest -v` from this directory. The fixture tests check required fields and verify BodyLength and CheckSum after restoring SOH delimiters. The CLI uses the dictionaries for display and performs framing checks.

## FIX dictionaries

The printer uses tag `8` (BeginString) to select version-specific names and enum descriptions from the bundled dictionaries, covering all 405 field definitions in the FIX 4.2 dictionary and all 912 in FIX 4.4. Keep the `dictionaries/` folder alongside the script. Supply a trusted QuickFIX XML dictionary with `--dictionary path/to/venue.xml` to add fields or override labels and enum values for that version. You can pass one override per version. Unrecognized tags and values remain visible. Fields inside repeating groups are printed in capture order.

### Dictionary source

This product includes software developed by
quickfixengine.org (http://www.quickfixengine.org/).

The bundled [FIX 4.2](https://github.com/quickfix/quickfix/blob/386ce46e917ae494ab6e90b1be90fd421cdbe3f9/spec/FIX42.xml)
and [FIX 4.4](https://github.com/quickfix/quickfix/blob/386ce46e917ae494ab6e90b1be90fd421cdbe3f9/spec/FIX44.xml)
dictionaries are distributed under the [QuickFIX Software License](dictionaries/LICENSE).
