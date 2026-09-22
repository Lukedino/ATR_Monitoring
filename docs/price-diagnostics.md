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
