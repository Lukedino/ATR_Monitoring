# Value-free price relation diagnostics

The calculation contract is unchanged. High, Low and Close must satisfy the
existing validation rules throughout the supplied history. Diagnostics do not
fix prices, remove rows, change tolerances, collect another quote, resend an
alert, or turn a failed calculation into success.

`price_validation.py` shares numeric validation and the existing floating-point
tolerance with ATR calculation. Its diagnostic result contains only fixed
relation kinds (`high_below_low`, `close_above_high`, `close_below_low`) and fixed
row groups (`history`, `latest`, `both`). It contains no index, timestamp, price,
symbol, account information, quantity, or number of affected rows. Invalid
non-relation inputs remain governed by the existing validation reason; an empty
diagnostic set is not proof that an input is valid.

The collector observes these existing boundaries without changing their data:

- `history_normalized`: the selected history after column/timezone normalization.
- `raw_fallback`: the existing fallback has replaced the history with a raw frame.
- `latest_quote`: the existing latest-quote function has returned. This does not
  imply that it changed the frame or obtained a usable quote.

The collector groups observations in a context-local batch. Each item's first
observation is tracked without retaining its identity or input frame. Only
deduplicated, fixed stage/relation/row-group presence is emitted at batch end in
a deterministic order. First/subsequent observation labels describe diagnostic
boundaries, not the origin of a provider error. Aggregation deliberately loses
the linkage between individual inputs and does not report their number or order.
Normal direct single-symbol collection outside a batch does not emit this report.

Diagnostic failures are isolated from the existing collection result, retries,
monitor exit status, state writes and notifications. No original error message is
added to the report. Existing log masking remains in place; this feature does not
certify every legacy log message as value-free.

The original runtime failure's `inconsistent_prices` reason identified a rejected
H/L/C relationship, not its source. These diagnostics help distinguish the stage
where it is first observed. They do not verify real input truth, prove a specific
provider at fault, repair operational inputs, or demonstrate successful operation.
Verification uses synthetic frames, fake providers and the source-only offline
harness. Operational dispatch, live data and alert delivery are not test tools.

## What the diagnostics found — 2026-09-22

Run 35774183850 (19:30Z) emitted
`history_normalized.close_above_high.latest.first_observed;
latest_quote.close_above_high.latest.already_observed`.

The relationship is already broken in the frame leaving provider history
normalisation, before any raw fallback or latest-quote merge. `raw_fallback`
never appeared, and `latest_quote` reports only what was already present. The
location is `latest`: the final row alone, with clean history behind it.

Three runs of the same commit after the US close (20:05Z, 20:30Z, 21:05Z)
emitted no diagnostic and rejected no symbol. The failing run was intraday
(15:30 EDT). An in-progress bar can carry a Close above a High the provider has
not yet raised.

### Resulting contract

`_confirmed_frame()` in `atr_calculator.py` drops that single unconfirmed bar and
computes ATR, Highest High and the 21-day EMA from confirmed history. It applies
only when the inconsistency is confined to the last row; an inconsistency in any
earlier row is treated as provider corruption and rejected exactly as before.
Prices are never edited, rows are never synthesised, and the tolerance, ATR
period, multiple, market windows, stop policy, state writes, notifications and
exit codes are unchanged.

`current_close` still comes from the raw final row. Retreating to the previous
session's close would measure the stop distance against a stale price and could
miss a symbol that has since fallen through its stop.

Trimming can leave fewer rows than the calculation needs; that is reported as the
existing `insufficient_history`, not as a silent success. This contract addresses
the rejection boundary only. It does not verify provider truth, repair inputs, or
prove that any particular operational run succeeded.

### Charts follow the same contract — 2026-09-26

The change above let alerts through for symbols with an unconfirmed last bar, but
`visualizer.plot_atr_chart()` still computed ATR, ATR% and the stop trail from
the raw frame. `calc_atr` returns an empty series for that frame, and the rolling
stop loop raised `IndexError`. Every one of the 33 chart omissions logged from
2026-09-23 to 2026-09-26 was this `IndexError`, and each failing run also carried a
`close_*.latest` diagnostic. The text alert was unaffected because the chart is
sent separately after it.

The chart now draws its indicators (ATR, ATR%, stop trail, 21-day EMA) from
`_confirmed_frame()`, aligned to the raw index, so they end at the last confirmed
bar. The price line keeps the raw final Close, which is the price the alert is
about. Covered by `tests/test_chart_unconfirmed_bar.py`.

### Partial bars without High/Low — 2026-09-29

Runs 36361172756 (2026-09-28 00:10Z) and 36463274040 (18:10Z) each rejected 23 of
77 symbols with `non_finite_prices` and exited 1. The rejected block was the
Korean holdings, on the first session after the Chuseok break; every run between
2026-09-22 and 2026-09-27 at the same slots succeeded.

Before the KR open the provider can return a bar with Open/High/Low = 0 and Close
= the previous close (recorded in `3ca1f53`, 2026-03-20). The collector turns
those zeros into NaN so that no gap of -100% reaches the triggers. Before the
2026-09-22 hardening the calculator tolerated the NaN; the hardened validator
rejects any non-finite H/L/C, so the whole symbol failed. Feeding both observed
shapes (a next-day partial bar, and a partial bar that replaced the last session)
through `fetch_ohlcv()` reproduces the rejection on the current code and a
successful stop on `bf87e65`.

`_confirmed_frame()` now also drops the final bar when its Close is a finite
positive number and only its High and/or Low are missing. Infinite values, a
missing Close, and missing values in any earlier row are still rejected as
`non_finite_prices`. `current_close` still comes from the raw final row. Covered
by `tests/test_unconfirmed_bar.py`.

### Portfolio summary follows the same contract — 2026-09-29

`summarize_portfolio_atr()` passed `atr_input_issue()` but then computed ATR and
ATR% from the raw frame. For a symbol with an unconfirmed last bar that series is
empty, so the symbol was silently left out of the weekly report and the spike
count in the closing brief. This applied to relation errors since 2026-09-23 and
to partial bars from the change above.

The summary now computes ATR, ATR%, ATR_Avg20 and the spike flag from
`_confirmed_frame()`, the same frame the stop uses. `Close` stays the raw final
Close, matching `current_close`. Symbols with broken earlier history are still
excluded. Covered by `tests/test_unconfirmed_bar.py`; the alert chart for a
partial bar is covered by `tests/test_chart_unconfirmed_bar.py`.
