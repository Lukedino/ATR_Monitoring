"""Synthetic venue routing without changing ETF risk settings or schedules."""
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

import atr_calculator
import config
import market_dates
import market_hours
import monitor


KR_ETFS = ("999991.KS", "999992.KQ")
US_ETF = "SYNTH-ETF"
COINS = ("SYNTH-USD", "SYNTH-USDT")


@pytest.fixture
def etf_assets(monkeypatch):
    monkeypatch.setattr(config, "_ETF_SYMBOLS", frozenset((*KR_ETFS, US_ETF)))


@pytest.mark.parametrize("symbol", KR_ETFS)
@pytest.mark.parametrize("when,active", [
    ("2026-09-14T09:30:00+09:00", True),   # US Sunday night, KR Monday open.
    ("2026-09-14T19:59:00+09:00", True),
    ("2026-09-14T20:00:00+09:00", False),
    ("2026-09-14T21:30:00+09:00", False),  # US premarket must not activate KR.
    ("2026-09-12T09:30:00+09:00", False),
    ("2026-01-19T09:30:00+09:00", True),
])
def test_kr_etf_uses_kr_hours_even_though_its_risk_type_is_etf(etf_assets, symbol, when, active):
    assert config.get_market_type(symbol) == "ETF"
    assert monitor._is_market_active_for_triggers(symbol, now_utc=datetime.fromisoformat(when)) is active


@pytest.mark.parametrize("when,active", [
    ("2026-07-17T04:00:00-04:00", True),
    ("2026-01-16T04:00:00-05:00", True),
    ("2026-01-17T00:30:00+00:00", True),  # Friday 19:30 ET, Saturday UTC.
    ("2026-07-17T20:00:00-04:00", False),
    ("2026-07-19T12:00:00-04:00", False),
])
def test_us_etf_keeps_dst_and_weekend_rules(etf_assets, when, active):
    assert config.get_market_type(US_ETF) == "ETF"
    assert monitor._is_market_active_for_triggers(US_ETF, now_utc=datetime.fromisoformat(when)) is active


@pytest.mark.parametrize("symbol", COINS)
@pytest.mark.parametrize("when", ["2026-09-12T02:00:00+00:00", "2026-09-14T22:00:00+00:00"])
def test_crypto_remains_active_at_all_hours(symbol, when):
    assert monitor._is_market_active_for_triggers(symbol, now_utc=datetime.fromisoformat(when)) is True


def test_gate_does_not_consult_atr_risk_classification(monkeypatch):
    def unexpected(*args):
        pytest.fail("ATR asset classification must not select a trading venue")
    monkeypatch.setattr(config, "get_market_type", unexpected)
    assert monitor._is_market_active_for_triggers(
        KR_ETFS[0], now_utc=datetime.fromisoformat("2026-09-14T09:30:00+09:00")) is True


def test_existing_gate_call_uses_an_aware_utc_clock(monkeypatch):
    now = datetime(2026, 9, 14, 0, 30, tzinfo=timezone.utc)
    clock_calls, gate_calls = [], []
    monkeypatch.setattr(market_dates, "utc_now", lambda value=None: clock_calls.append(value) or now)
    monkeypatch.setattr(market_hours, "is_market_active",
                        lambda venue, instant: gate_calls.append((venue, instant)) or True)
    assert monitor._is_market_active_for_triggers(KR_ETFS[0]) is True
    assert clock_calls == [None]
    assert gate_calls == [("KR", now)]


def test_gate_rejects_a_naive_reference_clock():
    with pytest.raises(ValueError, match="timezone-aware"):
        monitor._is_market_active_for_triggers(KR_ETFS[0], now_utc=datetime(2026, 9, 14, 9, 30))


@pytest.mark.parametrize("symbol", (*KR_ETFS, US_ETF))
def test_etf_atr_classification_and_chandelier_formula_stay_independent(etf_assets, monkeypatch, symbol):
    # Different baselines catch a tempting ETF/suffix reordering regression.
    monkeypatch.setattr(config, "ATR_MULTIPLE_BY_MARKET", {"KR": 2.25, "US": 3.25, "Crypto": 4.25, "ETF": 7.25})
    monkeypatch.setattr(config, "ATR_PCT_ADJUSTMENTS", [])
    frame = pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0},
                         index=pd.date_range("2026-07-01", periods=60))
    result = atr_calculator.calc_chandelier_stop(symbol, frame)
    assert config.get_market_type(symbol) == result.market == "ETF"
    assert config.get_atr_multiple(symbol) == result.multiple == 7.25
    assert result.atr == 2.0
    assert result.stop_level == 86.5


@pytest.fixture
def configured_monitor(monkeypatch):
    """Load code with a wholly synthetic portfolio and no inherited environment."""
    symbols = [*KR_ETFS, US_ETF, "999993.KS", "SYNTH-STOCK", *COINS]
    monkeypatch.setattr(os, "environ", {"STOCK_LIST": json.dumps({"synthetic": symbols})})
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("synthetic_routing_config", root / "config.py")
    settings = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(settings)
    monkeypatch.setattr(settings, "_ETF_SYMBOLS", frozenset((*KR_ETFS, US_ETF)))
    monkeypatch.setitem(sys.modules, "config", settings)
    spec = importlib.util.spec_from_file_location("synthetic_routing_monitor", root / "monitor.py")
    loaded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, loaded)
    spec.loader.exec_module(loaded)
    return loaded, symbols


def test_each_etf_enters_only_its_venue_weekly_report(configured_monitor, monkeypatch):
    loaded, symbols = configured_monitor
    calls = []
    monkeypatch.setattr(loaded, "_run_daily_report", lambda selected, title: calls.append(list(selected)))
    loaded.job_kr_daily_report()
    loaded.job_us_daily_report()
    kr, us_crypto = calls
    assert kr == [*KR_ETFS, "999993.KS"]
    assert us_crypto == [US_ETF, "SYNTH-STOCK", *COINS]
    assert sorted(kr + us_crypto) == sorted(symbols)
    assert not set(kr) & set(us_crypto)


@pytest.mark.parametrize("venue,expected", [
    ("KR", [*KR_ETFS, "999993.KS"]),
    ("US", [US_ETF, "SYNTH-STOCK", *COINS]),
])
def test_brief_filters_etfs_by_membership_without_reclassifying_risk(configured_monitor, monkeypatch, venue, expected):
    loaded, symbols = configured_monitor
    frame = pd.DataFrame({"Close": [100.0]}, index=pd.to_datetime(["2026-09-14"]))
    result = loaded.StopCheckResult(
        chandelier=[SimpleNamespace(symbol=s, market="ETF") for s in symbols],
        updated_symbols=list(symbols), ohlcv_map={s: frame for s in symbols})
    formatted, sent, summarized = [], [], []
    def fmt(title, data_date, chandelier, **kwargs):
        formatted.append(([ch.symbol for ch in chandelier], kwargs["updated_count"]))
        return "synthetic brief"
    monkeypatch.setattr(loaded, "tg", SimpleNamespace(
        fmt_daily_brief=fmt, send_long_message=lambda text: sent.append(text) or True))
    monkeypatch.setattr(loaded, "summarize_portfolio_atr",
                        lambda frames, period: summarized.append(list(frames)) or pd.DataFrame())
    loaded._send_daily_brief(SimpleNamespace(market=venue), result)
    # The all-symbol result remains in portfolio order, unlike US+Crypto scope.
    assert set(formatted[0][0]) == set(expected)
    assert len(formatted[0][0]) == len(expected)
    assert formatted[0][1] == len(expected)
    assert set(summarized[0]) == set(expected)
    assert sent == ["synthetic brief"]
    scoped = [s for _, selected in loaded._BRIEF_SCOPE.values() for s in selected]
    assert sorted(scoped) == sorted(symbols)
