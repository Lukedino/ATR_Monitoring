"""Synthetic providers and frames only; diagnostics contain fixed presence codes."""
from contextvars import copy_context
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

import data_collector as collector
import price_diagnostics as diagnostics


NOW = datetime.fromisoformat("2026-01-13T00:10:00+00:00")


def bars(*, high=110.0, low=90.0, close=100.0, timezone=None):
    frame = pd.DataFrame(
        {"Open": [100.0, 100.0], "High": [110.0, high],
         "Low": [90.0, low], "Close": [100.0, close], "Volume": [10.0, 20.0]},
        index=pd.date_range(end="2026-01-12", periods=2, tz=timezone),
    )
    frame.attrs = {"synthetic_private_marker": {"value": "PRIVATE-FRAME-VALUE"}}
    return frame


class Ticker:
    def __init__(self, frame, raw=None, fast_price=100.0):
        self.frame, self.raw, self.fast_price = frame, raw, fast_price
        self.calls = []

    def history(self, **kwargs):
        self.calls.append(("history", kwargs))
        source = self.raw if kwargs["auto_adjust"] is False and self.raw is not None else self.frame
        return source.copy(deep=True)

    @property
    def fast_info(self):
        self.calls.append(("fast_info",))
        return SimpleNamespace(last_price=self.fast_price)

    def get_history_metadata(self):
        self.calls.append(("get_history_metadata",))
        return {}


@pytest.fixture(autouse=True)
def no_actual_providers(monkeypatch):
    monkeypatch.setattr(collector, "utc_now", lambda value=None: value or NOW)
    monkeypatch.setattr(collector.yf, "Ticker", lambda symbol: pytest.fail("Synthetic ticker required"))
    monkeypatch.setattr(collector, "_fetch_naver_kr_price", lambda code: None)
    monkeypatch.setattr(collector, "_resolve_yahoo_crypto_ticker", lambda symbol: pytest.fail("No search"))


@pytest.fixture
def messages(monkeypatch):
    collected = []
    monkeypatch.setattr(diagnostics.logger, "warning", lambda message, *args: collected.append(message % args))
    return collected


def collect_with(monkeypatch, ticker, *, symbol="SYM-PRIVATE", enabled=True):
    calls = []
    observer = diagnostics.observe_price_stage

    def observe(stage, frame):
        snapshot = frame.copy(deep=True)
        if enabled:
            observer(stage, frame)
        pd.testing.assert_frame_equal(frame, snapshot)
        assert frame.attrs == snapshot.attrs
        calls.append(stage)

    monkeypatch.setattr(collector, "observe_price_stage", observe)
    monkeypatch.setattr(collector.yf, "Ticker", lambda requested: ticker)
    result = collector.fetch_portfolio([symbol])
    return result, calls


def test_collection_return_attributes_and_provider_calls_are_unchanged(monkeypatch, messages):
    frame = bars(close=130.0, timezone="America/New_York")
    frame.columns = [f" {column.lower()} " for column in frame.columns]
    original = frame.copy(deep=True)
    baseline_ticker = Ticker(frame)
    baseline, baseline_stages = collect_with(monkeypatch, baseline_ticker, enabled=False)
    ticker = Ticker(frame)
    observed, stages = collect_with(monkeypatch, ticker)
    pd.testing.assert_frame_equal(observed["SYM-PRIVATE"], baseline["SYM-PRIVATE"])
    assert observed["SYM-PRIVATE"].attrs == baseline["SYM-PRIVATE"].attrs == frame.attrs
    pd.testing.assert_frame_equal(frame, original)
    assert ticker.calls == baseline_ticker.calls
    assert stages == baseline_stages == ["history_normalized", "latest_quote"]
    assert len(messages) == 1
    assert "history_normalized.close_above_high.latest.first_observed" in messages[0]
    assert "latest_quote.close_above_high.latest.already_observed" in messages[0]
    for private in ("SYM-PRIVATE", "PRIVATE-FRAME-VALUE", "2026", "130", "10", "20"):
        assert private not in messages[0]


def test_actual_raw_replacement_is_observed_once_after_replacement(monkeypatch, messages):
    adjusted = bars()
    raw = bars(high=190.0, low=180.0, close=200.0)
    baseline_ticker = Ticker(adjusted, raw, fast_price=200.0)
    baseline, _ = collect_with(monkeypatch, baseline_ticker, symbol="999991.KS", enabled=False)
    ticker = Ticker(adjusted, raw, fast_price=200.0)
    result, stages = collect_with(monkeypatch, ticker, symbol="999991.KS")
    pd.testing.assert_frame_equal(result["999991.KS"], baseline["999991.KS"])
    assert result["999991.KS"].attrs == raw.attrs
    assert ticker.calls == baseline_ticker.calls
    assert stages == ["history_normalized", "raw_fallback", "latest_quote"]
    assert len(messages) == 1
    assert "raw_fallback.close_above_high.latest.first_observed" in messages[0]
    assert "latest_quote.close_above_high.latest.already_observed" in messages[0]
    assert "history_normalized" not in messages[0]


def test_rejected_empty_raw_candidate_does_not_claim_replacement(monkeypatch, messages):
    result, stages = collect_with(monkeypatch, Ticker(bars(), pd.DataFrame(), fast_price=200.0), symbol="999991.KS")
    assert not result["999991.KS"].empty
    assert stages == ["history_normalized", "latest_quote"]
    assert messages == []


def test_latest_quote_boundary_observes_returned_frame_without_replacing_it(monkeypatch, messages):
    returned = bars(high=95.0)
    sync_calls = []

    def sync(ticker, symbol, frame, **kwargs):
        sync_calls.append((ticker, symbol, kwargs))
        return returned

    monkeypatch.setattr(collector, "_sync_latest_quote", sync)
    result, stages = collect_with(monkeypatch, Ticker(bars()))
    assert result["SYM-PRIVATE"] is returned
    assert len(sync_calls) == 1
    assert stages == ["history_normalized", "latest_quote"]
    assert messages == [
        "price_relation_diagnostics latest_quote.close_above_high.latest.first_observed"
    ]


def test_direct_fetch_does_not_evaluate_or_emit_diagnostics(monkeypatch, messages):
    ticker = Ticker(bars(close=130.0))
    monkeypatch.setattr(collector.yf, "Ticker", lambda symbol: ticker)
    monkeypatch.setattr(diagnostics, "price_relation_diagnostics", lambda frame: pytest.fail("No active batch"))
    assert not collector.fetch_ohlcv("SYM-PRIVATE", now_utc=NOW).empty
    assert messages == []


def test_batch_output_is_deduplicated_and_independent_of_item_count_or_order(messages):
    frames = [bars(close=130.0), bars(close=80.0)]
    for order in (frames, list(reversed(frames)), frames + frames):
        with diagnostics.price_diagnostic_batch():
            for frame in order:
                with diagnostics.price_diagnostic_item():
                    diagnostics.observe_price_stage("history_normalized", frame)
                    diagnostics.observe_price_stage("latest_quote", frame)
    assert len(messages) == 3
    assert messages[0] == messages[1] == messages[2]
    codes = messages[0].removeprefix("price_relation_diagnostics ").split(";")
    assert codes == sorted(set(codes))
    assert all(code in diagnostics._CODES for code in codes)


def test_history_to_both_remains_an_already_observed_relation(messages):
    frame = bars()
    frame.iloc[0, frame.columns.get_loc("Close")] = 130.0
    both = frame.copy(deep=True)
    both.iloc[-1, both.columns.get_loc("Close")] = 130.0
    with diagnostics.price_diagnostic_batch(), diagnostics.price_diagnostic_item():
        diagnostics.observe_price_stage("history_normalized", frame)
        diagnostics.observe_price_stage("latest_quote", both)
    assert "history_normalized.close_above_high.history.first_observed" in messages[0]
    assert "latest_quote.close_above_high.both.already_observed" in messages[0]


def test_nested_batches_and_items_restore_the_outer_context(messages):
    outer, inner = bars(close=130.0), bars(close=80.0)
    with diagnostics.price_diagnostic_batch(), diagnostics.price_diagnostic_item():
        diagnostics.observe_price_stage("history_normalized", outer)
        with diagnostics.price_diagnostic_batch(), diagnostics.price_diagnostic_item():
            diagnostics.observe_price_stage("raw_fallback", inner)
        with diagnostics.price_diagnostic_item():
            diagnostics.observe_price_stage("raw_fallback", outer)
        diagnostics.observe_price_stage("latest_quote", outer)
    assert len(messages) == 2
    assert messages[0] == "price_relation_diagnostics raw_fallback.close_below_low.latest.first_observed"
    assert "close_below_low" not in messages[1]
    assert "raw_fallback.close_above_high.latest.first_observed" in messages[1]
    assert "latest_quote.close_above_high.latest.already_observed" in messages[1]


def test_copied_context_observation_does_not_mutate_the_parent(messages):
    with diagnostics.price_diagnostic_batch(), diagnostics.price_diagnostic_item():
        diagnostics.observe_price_stage("history_normalized", bars(close=130.0))
        copy_context().run(diagnostics.observe_price_stage, "latest_quote", bars(close=80.0))
    assert messages == [
        "price_relation_diagnostics history_normalized.close_above_high.latest.first_observed"
    ]


def test_batch_exception_propagates_unchanged_and_context_is_restored(messages):
    error = ValueError("synthetic caller failure")
    with pytest.raises(ValueError) as raised:
        with diagnostics.price_diagnostic_batch(), diagnostics.price_diagnostic_item():
            diagnostics.observe_price_stage("history_normalized", bars(close=130.0))
            raise error
    assert raised.value is error
    diagnostics.observe_price_stage("latest_quote", bars(close=80.0))
    with diagnostics.price_diagnostic_batch():
        pass
    assert len(messages) == 1
    assert "close_below_low" not in messages[0]


def test_diagnostic_failure_preserves_collection_and_provider_calls(monkeypatch, messages):
    frame = bars(close=130.0)
    baseline_ticker = Ticker(frame)
    baseline, _ = collect_with(monkeypatch, baseline_ticker, enabled=False)

    def broken(frame):
        raise ValueError("PRIVATE-ERROR-SYMBOL-PRICE-DATE")

    monkeypatch.setattr(diagnostics, "price_relation_diagnostics", broken)
    ticker = Ticker(frame)
    result, _ = collect_with(monkeypatch, ticker)
    pd.testing.assert_frame_equal(result["SYM-PRIVATE"], baseline["SYM-PRIVATE"])
    assert result["SYM-PRIVATE"].attrs == baseline["SYM-PRIVATE"].attrs
    assert ticker.calls == baseline_ticker.calls
    assert messages == ["price_relation_diagnostics diagnostics_unavailable"]


def test_logger_failure_cannot_change_collection_result(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("PRIVATE-LOGGER-ERROR")

    monkeypatch.setattr(diagnostics.logger, "warning", broken)
    result, _ = collect_with(monkeypatch, Ticker(bars(close=130.0)))
    assert result["SYM-PRIVATE"]["Close"].iloc[-1] == 130.0
    error = ValueError("caller remains unchanged")
    with pytest.raises(ValueError) as raised:
        with diagnostics.price_diagnostic_batch(), diagnostics.price_diagnostic_item():
            diagnostics.observe_price_stage("latest_quote", bars(close=130.0))
            raise error
    assert raised.value is error


@pytest.mark.parametrize("response", [
    frozenset({("PRIVATE-RELATION", "latest")}),
    frozenset({("close_above_high", "PRIVATE-SCOPE")}),
    frozenset({"PRIVATE-MALFORMED"}),
    [("close_above_high", "latest")],
])
def test_unknown_diagnostic_content_is_never_logged(monkeypatch, messages, response):
    monkeypatch.setattr(diagnostics, "price_relation_diagnostics", lambda frame: response)
    with diagnostics.price_diagnostic_batch(), diagnostics.price_diagnostic_item():
        diagnostics.observe_price_stage("history_normalized", bars())
    assert messages == ["price_relation_diagnostics diagnostics_unavailable"]


def test_unknown_stage_is_never_formatted_or_passed_to_helper(monkeypatch, messages):
    class PrivateStage:
        def __str__(self):
            pytest.fail("Never format untrusted stages")

    monkeypatch.setattr(diagnostics, "price_relation_diagnostics", lambda frame: pytest.fail("Unknown stage"))
    with diagnostics.price_diagnostic_batch(), diagnostics.price_diagnostic_item():
        diagnostics.observe_price_stage(PrivateStage(), bars())
        diagnostics.observe_price_stage("PRIVATE-STAGE", bars())
    assert messages == ["price_relation_diagnostics diagnostics_unavailable"]
