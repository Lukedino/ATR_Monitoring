"""Synthetic report delivery and per-market completion regressions."""
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

import monitor
import telegram_bot as telegram


def _bars():
    return pd.DataFrame({"Close": [100.0]}, index=pd.to_datetime(["2026-09-21"]))


def _ch(symbol):
    return SimpleNamespace(symbol=symbol, stop_level=90.0, current_close=100.0,
                           highest_high=110.0, is_near_stop=False)


@pytest.fixture
def reports(monkeypatch):
    symbols = ["SYM-KR", "SYM-US"]
    data = {s: _bars() for s in symbols}
    sent, done = [], set()
    monkeypatch.setattr(monitor, "ALL_SYMBOLS", symbols)
    monkeypatch.setattr(monitor, "KR_SYMBOLS", symbols[:1])
    monkeypatch.setattr(monitor, "US_SYMBOLS", symbols[1:])
    monkeypatch.setattr(monitor, "CRYPTO_SYMBOLS", [])
    monkeypatch.setattr(monitor, "_BRIEF_SCOPE", {
        "KR": ("KR synthetic", symbols[:1]), "US": ("US synthetic", symbols[1:]),
    })
    monkeypatch.setattr(monitor, "_problems", [])
    monkeypatch.setattr(monitor, "fetch_portfolio", lambda syms: {s: data[s] for s in syms if s in data})
    monkeypatch.setattr(monitor, "load_stops", lambda: {})
    monkeypatch.setattr(monitor, "calc_chandelier_stop", lambda symbol, *args: _ch(symbol))
    monkeypatch.setattr(monitor, "check_immediate_triggers", lambda *a: SimpleNamespace(has_trigger=False))
    monkeypatch.setattr(monitor, "summarize_portfolio_atr", lambda bars, *a: pd.DataFrame({
        "Symbol": list(bars), "Spike": [False] * len(bars),
    }))
    monkeypatch.setattr(monitor, "plot_portfolio_atr_bar", lambda *a, **k: b"synthetic-png")
    monkeypatch.setattr(monitor, "plot_atr_chart", lambda *a, **k: b"synthetic-png")
    monkeypatch.setattr(monitor.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(telegram, "send_message", lambda text, *a, **k: sent.append(text) or True)
    monkeypatch.setattr(telegram, "send_photo", lambda *a, **k: True)
    monkeypatch.setattr(telegram, "fmt_daily_report", lambda *a, **k: "ATR summary")
    monkeypatch.setattr(telegram, "fmt_chandelier_report", lambda *a, **k: "Chandelier summary")
    monkeypatch.setattr(telegram, "fmt_daily_brief", lambda title, *a, **k: title)
    monkeypatch.setattr(monitor, "is_window_done", lambda name, day: name in done)
    monkeypatch.setattr(monitor, "mark_window_done", lambda name, day: done.add(name))

    def windows(action="weekly_report", markets=("KR",)):
        result = [SimpleNamespace(name=f"{market}-{action}", market=market, action=action,
                                  brief=action == "stop_check", local_date=lambda now: date(2026, 9, 21))
                  for market in markets]
        monkeypatch.setattr(monitor.market_hours, "due_windows", lambda now: result)
        return result

    return SimpleNamespace(data=data, sent=sent, done=done, windows=windows)


@pytest.mark.parametrize("failure", ["collection", "empty_bars", "summary", "chandelier"])
def test_weekly_generation_failure_keeps_window_open_then_recovers(reports, monkeypatch, failure):
    window = reports.windows()[0]
    with monkeypatch.context() as failed:
        if failure == "collection":
            failed.setattr(monitor, "fetch_portfolio", lambda syms: {})
        elif failure == "empty_bars":
            failed.setattr(monitor, "fetch_portfolio", lambda syms: {s: pd.DataFrame() for s in syms})
        elif failure == "summary":
            failed.setattr(monitor, "summarize_portfolio_atr", lambda *a: pd.DataFrame())
        else:
            failed.setattr(monitor, "calc_chandelier_stop", lambda *a: None)
        monitor.run_due_windows()
        assert window.name not in reports.done
        assert monitor._problems
        assert "ATR summary" not in reports.sent
    monitor.run_due_windows()
    assert window.name in reports.done
    assert reports.sent[-2:] == ["ATR summary", "Chandelier summary"]


@pytest.mark.parametrize("failure_at", [0, 1, 2, 3])
def test_failed_report_chunk_keeps_window_open_and_retry_resends_body(reports, monkeypatch, failure_at):
    window = reports.windows()[0]
    long_text = "\n".join("x" * 100 for _ in range(60))  # Three chunks, then Chandelier text.
    monkeypatch.setattr(telegram, "fmt_daily_report", lambda *a, **k: long_text)
    attempts = []

    def send(text, *a, **k):
        attempts.append(text)
        return len(attempts) - 1 != failure_at

    monkeypatch.setattr(telegram, "send_message", send)
    monitor.run_due_windows()
    assert window.name not in reports.done
    assert monitor._problems
    assert len(attempts) == (3 if failure_at < 3 else 4)
    monitor.run_due_windows()
    assert window.name in reports.done
    assert attempts[-4] == attempts[0]  # Successful earlier chunks are intentionally retried.
    count = len(attempts)
    monitor.run_due_windows()
    assert len(attempts) == count


@pytest.mark.parametrize("failure", ["bar_generation", "photo_rejected", "mini_generation"])
def test_optional_charts_do_not_block_required_text_or_completion(reports, monkeypatch, failure):
    window = reports.windows()[0]

    def broken(*a, **k):
        raise ValueError("SYNTHETIC-PRIVATE-DETAIL")

    if failure == "bar_generation":
        monkeypatch.setattr(monitor, "plot_portfolio_atr_bar", broken)
    elif failure == "photo_rejected":
        monkeypatch.setattr(telegram, "send_photo", lambda *a, **k: False)
    else:
        monkeypatch.setattr(monitor, "summarize_portfolio_atr", lambda bars, *a: pd.DataFrame({
            "Symbol": list(bars), "Spike": [True] * len(bars),
        }))
        monkeypatch.setattr(monitor, "plot_atr_chart", broken)
    monitor.run_due_windows()
    assert window.name in reports.done
    assert reports.sent == ["ATR summary", "Chandelier summary"]


@pytest.mark.parametrize("missing", ["collection", "calculation"])
def test_missing_market_stays_open_while_other_market_alerts_and_brief_succeed(reports, monkeypatch, missing):
    us_symbols = [f"SYM-US{i}" for i in range(8)]
    all_symbols = ["SYM-KR0", "SYM-KR1"] + us_symbols
    reports.data.clear()
    reports.data.update({s: _bars() for s in (us_symbols if missing == "collection" else all_symbols)})
    monkeypatch.setattr(monitor, "ALL_SYMBOLS", all_symbols)
    monkeypatch.setattr(monitor, "_BRIEF_SCOPE", {
        "KR": ("KR synthetic", all_symbols[:2]), "US": ("US synthetic", us_symbols),
    })
    monkeypatch.setattr(monitor, "calc_chandelier_stop", lambda symbol, *a:
                        None if symbol.startswith("SYM-KR") else _ch(symbol))
    monkeypatch.setattr(monitor, "check_immediate_triggers", lambda *a:
                        SimpleNamespace(has_trigger=True, triggers=["synthetic alert"]))
    monkeypatch.setattr(monitor, "_is_market_active_for_triggers", lambda symbol: True)
    monkeypatch.setattr(monitor, "should_send_trigger_alert", lambda *a: True)
    recorded = []
    monkeypatch.setattr(monitor, "mark_trigger_sent", lambda symbol, *a: recorded.append(symbol))
    monkeypatch.setattr(telegram, "fmt_trigger_alert", lambda symbol, *a: symbol)
    windows = reports.windows("stop_check", ("KR", "US"))
    monitor.run_due_windows()
    assert windows[0].name not in reports.done
    assert windows[1].name in reports.done
    assert recorded == us_symbols
    assert "KR synthetic" not in reports.sent
    assert any(windows[0].name in problem for problem in monitor._problems)
    reports.data.update({s: _bars() for s in all_symbols[:2]})
    monkeypatch.setattr(monitor, "calc_chandelier_stop", lambda symbol, *a: _ch(symbol))
    monitor.run_due_windows()
    assert windows[0].name in reports.done


def test_partial_market_keeps_existing_collection_threshold(reports, monkeypatch):
    symbols = [f"SYM-KR{i}" for i in range(5)]
    reports.data.clear()
    reports.data.update({s: _bars() for s in symbols[:4]})
    monkeypatch.setattr(monitor, "ALL_SYMBOLS", symbols)
    monkeypatch.setattr(monitor, "_BRIEF_SCOPE", {"KR": ("KR synthetic", symbols)})
    window = reports.windows("stop_check")[0]
    monitor.run_due_windows()
    assert window.name in reports.done
    assert not monitor._problems


@pytest.mark.parametrize("action", ["stop_check", "weekly_report"])
def test_intentionally_empty_market_can_complete(reports, monkeypatch, action):
    monkeypatch.setattr(monitor, "KR_SYMBOLS", [])
    monkeypatch.setattr(monitor, "_BRIEF_SCOPE", {"KR": ("KR synthetic", [])})
    window = reports.windows(action)[0]
    monitor.run_due_windows()
    assert window.name in reports.done
    assert not monitor._problems


@pytest.mark.parametrize("failure", ["connection", "timeout", 400, 500])
def test_photo_errors_do_not_log_synthetic_credentials(monkeypatch, caplog, failure):
    private = "https://api.telegram.org/botSYNTHETIC-SECRET/sendPhoto chat_id=SYNTHETIC-CHAT"
    monkeypatch.setattr(telegram, "_is_configured", lambda: True)

    def post(*a, **k):
        if failure == "connection":
            raise requests.ConnectionError(private)
        if failure == "timeout":
            raise requests.Timeout(private)
        response = requests.Response()
        response.status_code = failure
        response.url = private
        return response

    monkeypatch.setattr(telegram.requests, "post", post)
    assert telegram.send_photo(b"synthetic-png") is False
    assert "SYNTHETIC-SECRET" not in caplog.text
    assert "SYNTHETIC-CHAT" not in caplog.text
    assert "traceback" not in caplog.text.lower()
    assert caplog.records


def test_successful_photo_still_returns_true(monkeypatch):
    monkeypatch.setattr(telegram, "_is_configured", lambda: True)
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"ok":true}'
    monkeypatch.setattr(telegram.requests, "post", lambda *a, **k: response)
    assert telegram.send_photo(b"synthetic-png") is True
