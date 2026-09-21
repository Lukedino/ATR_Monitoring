"""Crypto alias selection is deterministic and conservative; all providers fake."""
from datetime import datetime, timezone
from itertools import permutations
import logging

import pandas as pd
import pytest

import data_collector as collector


NOW = datetime(2026, 1, 13, 0, 10, tzinfo=timezone.utc)
PRIVATE = "SYNTHETIC-PRIVATE https://example.invalid/?token=SYNTHETIC-TOKEN"


def quote(symbol, kind="CRYPTOCURRENCY", **extra):
    return {"symbol": symbol, "quoteType": kind, **extra}


def select(symbol, *quotes):
    return collector._select_yahoo_crypto_ticker(symbol, {"quotes": list(quotes)})


def test_exact_symbol_always_wins_regardless_of_candidate_order():
    candidates = [quote("MOCK123-USD"), quote("mock-usd", "cryptocurrency"), quote("MOCK456-USD")]
    for ordered in permutations(candidates):
        assert select("mock-usd", *ordered) == "MOCK-USD"


@pytest.mark.parametrize("symbol,candidate", [
    ("MOCK-USD", "MOCK123-USD"), ("MOCK-USDT", "MOCK123-USDT"),
    ("1MOCK-USD", "1MOCK123-USD"), ("MOCK2-USD", "MOCK2123-USD"),
])
def test_only_unique_numeric_variant_is_accepted(symbol, candidate):
    assert select(symbol, quote(candidate)) == candidate


def test_duplicate_rows_for_same_normalized_variant_are_not_ambiguous():
    assert select("MOCK-USD", quote("MOCK123-USD"), quote("mock123-usd")) == "MOCK123-USD"
    assert select("MOCK-USD", quote("MOCK-USD"), quote("mock-usd"), quote("MOCK123-USD")) == "MOCK-USD"


@pytest.mark.parametrize("candidates", [
    ["MOCK123-USD", "MOCK456-USD"],
    ["MOCK1-USD", "MOCK01-USD"],
])
def test_distinct_variants_are_rejected_with_no_candidate_metadata_in_warning(candidates, caplog):
    for ordered in permutations(candidates):
        caplog.clear()
        assert select("MOCK-USD", *(quote(symbol, longname=PRIVATE) for symbol in ordered)) is None
        assert any(record.levelno == logging.WARNING for record in caplog.records)
        assert "후보가 여러 개" in caplog.text
        assert PRIVATE not in caplog.text
        assert all(symbol not in caplog.text for symbol in candidates)


@pytest.mark.parametrize("candidate,kind", [
    ("MOCK123-USDT", "CRYPTOCURRENCY"), ("MOCK123-EUR", "CRYPTOCURRENCY"),
    ("MOCK123-USD", "EQUITY"), ("MOCK-USD", "ETF"),
    ("MOCKABC-USD", "CRYPTOCURRENCY"), ("MOCK123X-USD", "CRYPTOCURRENCY"),
    ("XMOCK123-USD", "CRYPTOCURRENCY"), ("MOCK-123-USD", "CRYPTOCURRENCY"),
    ("MOCK١٢٣-USD", "CRYPTOCURRENCY"), ("MOCK１２３-USD", "CRYPTOCURRENCY"),
    ("MOCK²-USD", "CRYPTOCURRENCY"), ("MOCK123-USD.EXTRA", "CRYPTOCURRENCY"),
])
def test_wrong_currency_type_prefix_or_non_ascii_digits_never_become_an_alias(candidate, kind):
    assert select("MOCK-USD", quote(candidate, kind)) is None


def test_unrelated_well_formed_results_do_not_hide_one_valid_variant():
    assert select("MOCK-USD", quote("UNRELATED-USD"), quote("MOCK123-USD", "EQUITY"),
                  quote("MOCK123-USDT"), quote("MOCK456-USD")) == "MOCK456-USD"


@pytest.mark.parametrize("payload", [
    None, [], "invalid", {}, {"quotes": None}, {"quotes": {}}, {"quotes": "invalid"},
    {"quotes": [None]}, {"quotes": [[]]}, {"quotes": [{}]},
    {"quotes": [{"symbol": 123, "quoteType": "CRYPTOCURRENCY"}]},
    {"quotes": [{"symbol": "MOCK-USD", "quoteType": None}]},
    {"quotes": [{"symbol": "", "quoteType": "CRYPTOCURRENCY"}]},
    {"quotes": [{"symbol": "MOCK-USD", "quoteType": " "}]},
])
def test_malformed_search_response_is_rejected_without_string_coercion(payload, caplog):
    assert collector._select_yahoo_crypto_ticker("MOCK-USD", payload) is None
    assert "검색 응답 형식" in caplog.text


@pytest.mark.parametrize("candidate", ["MOCK-USD", "MOCK123-USD"])
def test_late_malformed_row_invalidates_an_earlier_matching_result(candidate, caplog):
    assert select("MOCK-USD", quote(candidate), {"private_payload": PRIVATE}) is None
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("candidate", ["MOCK-USD", "MOCK456-USD"])
@pytest.mark.parametrize("field", ["symbol", "quoteType"])
@pytest.mark.parametrize("marker", [" ", "\t", "\n", "\x00", "\x1b", "\x7f", "\u00a0"])
@pytest.mark.parametrize("position", ["leading", "middle", "trailing"])
def test_whitespace_or_controls_cannot_hide_exact_or_ambiguous_candidate(
        candidate, field, marker, position, caplog):
    malformed = quote(candidate, longname=PRIVATE)
    original = malformed[field]
    if position == "leading":
        malformed[field] = marker + original
    elif position == "middle":
        malformed[field] = original[:2] + marker + original[2:]
    else:
        malformed[field] = original + marker
    assert select("MOCK-USD", quote("MOCK123-USD"), malformed) is None
    assert "검색 응답 형식" in caplog.text
    assert PRIVATE not in caplog.text
    assert "MOCK" not in caplog.text


@pytest.mark.parametrize("count", [15, 16])
def test_limit_sized_response_cannot_establish_a_unique_numeric_alias(count, caplog):
    candidates = [quote("MOCK123-USD")] + [quote(f"OTHER{index}-USD") for index in range(count - 1)]
    assert select("MOCK-USD", *candidates) is None
    assert "조회 한도" in caplog.text


def test_response_below_limit_still_allows_a_unique_numeric_alias():
    candidates = [quote("MOCK123-USD")] + [quote(f"OTHER{index}-USD") for index in range(13)]
    assert select("MOCK-USD", *candidates) == "MOCK123-USD"


def test_full_response_still_accepts_an_exact_symbol_at_the_end():
    candidates = [quote(f"MOCK{index}-USD") for index in range(14)] + [quote("MOCK-USD")]
    assert select("MOCK-USD", *candidates) == "MOCK-USD"


class Response:
    def __init__(self, payload, error_stage=None):
        self.payload = payload
        self.error_stage = error_stage

    def raise_for_status(self):
        if self.error_stage == "status":
            raise RuntimeError(PRIVATE)

    def json(self):
        if self.error_stage == "json":
            raise ValueError(PRIVATE)
        return self.payload


def test_resolver_uses_one_bounded_search_and_exact_selection(monkeypatch):
    calls = []

    def search(url, **kwargs):
        calls.append((url, kwargs))
        return Response({"quotes": [quote("MOCK123-USDT"), quote("MOCK-USDT")]})

    monkeypatch.setattr(collector.requests, "get", search)
    assert collector._resolve_yahoo_crypto_ticker("mock-usdt") == "MOCK-USDT"
    assert len(calls) == 1
    _, parameters = calls[0]
    assert parameters["params"] == {"q": "MOCK", "quotesCount": 15, "newsCount": 0}
    assert parameters["timeout"] == 5


@pytest.mark.parametrize("symbol", [None, 123, "", "-USD", "-USDT", "MOCK", "MOCK-EUR"])
def test_invalid_or_non_crypto_input_never_searches(monkeypatch, symbol):
    calls = []
    monkeypatch.setattr(collector.requests, "get", lambda *a, **k: calls.append(True))
    assert collector._resolve_yahoo_crypto_ticker(symbol) is None
    assert calls == []


@pytest.mark.parametrize("stage", ["request", "status", "json"])
def test_search_failure_does_not_expose_raw_exception_or_retry(monkeypatch, caplog, stage):
    calls = []

    def search(*args, **kwargs):
        calls.append(True)
        if stage == "request":
            raise TimeoutError(PRIVATE)
        return Response({}, stage)

    monkeypatch.setattr(collector.requests, "get", search)
    with caplog.at_level(logging.DEBUG):
        assert collector._resolve_yahoo_crypto_ticker("MOCK-USD") is None
    assert calls == [True]
    assert PRIVATE not in caplog.text
    assert "SYNTHETIC-TOKEN" not in caplog.text


def frame():
    return pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0,
                         "Close": 100.0, "Volume": 1000.0},
                        index=pd.date_range("2026-01-12", periods=2, tz="UTC")).rename_axis("Date")


@pytest.fixture
def fake_provider(monkeypatch):
    histories, created, searches = {}, [], []
    history_calls, synchronized = [], []
    payload = {"quotes": []}

    class Ticker:
        def __init__(self, symbol):
            self.symbol = symbol
            created.append(symbol)

        def history(self, **kwargs):
            history_calls.append((self.symbol, kwargs))
            result = histories.get(self.symbol, pd.DataFrame())
            if isinstance(result, Exception):
                raise result
            return result.copy()

    def search(*args, **kwargs):
        searches.append(True)
        return Response(payload)

    def sync(ticker, symbol, data, **kwargs):
        synchronized.append((ticker.symbol, symbol, kwargs))
        return data

    monkeypatch.setattr(collector.yf, "Ticker", Ticker)
    monkeypatch.setattr(collector.requests, "get", search)
    monkeypatch.setattr(collector, "_sync_latest_quote", sync)
    monkeypatch.setattr(collector, "utc_now", lambda now_utc=None: NOW if now_utc is None else now_utc)
    return histories, created, searches, history_calls, synchronized, payload


def test_nonempty_original_history_never_requests_alias_search(fake_provider):
    histories, created, searches, _, _, payload = fake_provider
    histories["MOCK-USD"] = frame()
    payload["quotes"] = [quote("MOCK123-USD")]
    result = collector.fetch_ohlcv("MOCK-USD", now_utc=NOW)
    assert not result.empty
    assert created == ["MOCK-USD"]
    assert searches == []


@pytest.mark.parametrize("candidates", [
    ["MOCK123-USD", "MOCK-USD"], ["MOCK123-USD", "MOCK456-USD"],
])
def test_empty_original_with_exact_or_ambiguous_search_never_fetches_another_coin(fake_provider, candidates):
    histories, created, searches, _, synchronized, payload = fake_provider
    histories["MOCK123-USD"] = frame()
    payload["quotes"] = [quote(symbol) for symbol in candidates]
    assert collector.fetch_ohlcv("MOCK-USD", now_utc=NOW).empty
    assert created == ["MOCK-USD"]
    assert searches == [True]
    assert synchronized == []


def test_unique_alias_uses_one_alternate_and_keeps_original_key_date_and_adjustment(fake_provider):
    histories, created, searches, calls, synchronized, payload = fake_provider
    histories["MOCK123-USD"] = frame()
    payload["quotes"] = [quote("MOCK123-USD")]
    result = collector.fetch_portfolio(["MOCK-USD"], lookback_days=10)
    assert list(result) == ["MOCK-USD"]
    assert created == ["MOCK-USD", "MOCK123-USD"]
    assert searches == [True]
    assert calls[0][1] == calls[1][1] == {
        "start": "2026-01-03", "end": "2026-01-14", "auto_adjust": True,
    }
    assert synchronized[0][:2] == ("MOCK123-USD", "MOCK-USD")
    assert result["MOCK-USD"].index[-1] == pd.Timestamp("2026-01-13")


@pytest.mark.parametrize("alternate", [None, RuntimeError(PRIVATE)])
def test_failed_alternate_does_not_try_another_ticker_or_change_original_key(fake_provider, caplog, alternate):
    histories, created, searches, _, _, payload = fake_provider
    histories["MOCK123-USD"] = pd.DataFrame() if alternate is None else alternate
    payload["quotes"] = [quote("MOCK123-USD")]
    assert collector.fetch_ohlcv("MOCK-USD", now_utc=NOW).empty
    assert created == ["MOCK-USD", "MOCK123-USD"]
    assert searches == [True]
    assert PRIVATE not in caplog.text
