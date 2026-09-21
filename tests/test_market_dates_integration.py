"""Collect, calculate and deduplicate one US evening across UTC midnight."""

from datetime import datetime
import json

import pandas as pd

import atr_calculator
import data_collector
import stop_manager


def test_delayed_history_breach_is_seen_once_across_utc_midnight(tmp_path, monkeypatch):
    history = pd.DataFrame(
        {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0, "Volume": 1000.0},
        index=pd.date_range(end="2026-01-11", periods=60, tz="America/New_York"),
    )
    state = tmp_path / "state.json"
    state.write_text('{"positions": {}, "alert_log": {}}', encoding="utf-8")
    monkeypatch.setattr(stop_manager, "DATA_FILE", state)
    sent = []

    for value in ("2026-01-12T23:59:00+00:00", "2026-01-13T00:01:00+00:00"):
        now = datetime.fromisoformat(value)

        class DelayedTicker:
            def history(self, **kwargs):
                assert kwargs["end"] == "2026-01-13"
                return history.copy()

            def get_history_metadata(self):
                return {"regularMarketTime": now.timestamp(), "regularMarketPrice": 97.0,
                        "regularMarketDayHigh": 101.0, "regularMarketDayLow": 96.0,
                        "regularMarketVolume": 1000}

        monkeypatch.setattr(data_collector.yf, "Ticker", lambda symbol: DelayedTicker())
        frame = data_collector.fetch_ohlcv("SYM-US", now_utc=now)
        assert frame.index[-1].date().isoformat() == "2026-01-12"
        assert len(frame) == len(history) + 1
        assert pd.isna(frame["Open"].iloc[-1])
        calculated = atr_calculator.calc_chandelier_stop("SYM-US", frame)
        assert calculated.current_close == 97.0
        result = atr_calculator.check_immediate_triggers("SYM-US", frame, 98.0, now_utc=now)
        assert len(result.triggers) == 1
        assert result.triggers[0].startswith("STOP BREACH")
        if stop_manager.should_send_trigger_alert("SYM-US", result.triggers, 97.0, 98.0, now_utc=now):
            sent.append(result.triggers)
            stop_manager.mark_trigger_sent("SYM-US", result.triggers, 97.0, 98.0, now_utc=now)

    assert len(sent) == 1
    entry = json.loads(state.read_bytes())["alert_log"]["SYM-US"]
    assert entry["date"] == "2026-01-12"
    assert entry["date_basis"] == "market-v1"
    assert entry["sent_at"] == "2026-01-12T23:59+00:00"
