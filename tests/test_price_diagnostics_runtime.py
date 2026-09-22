"""Synthetic end-to-end evidence that diagnostics cannot change runtime effects."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pandas as pd
import pytest

import atr_calculator
import data_collector
import monitor
import stop_manager


NOW = datetime(2026, 1, 12, 18, tzinfo=timezone.utc)


def _prices(invalid):
    frame = pd.DataFrame(
        {"Open": 100., "High": 102., "Low": 98., "Close": 100., "Volume": 1000.},
        index=pd.date_range(end="2026-01-12", periods=60),
    )
    if invalid:
        frame.iloc[20, frame.columns.get_loc("Close")] = 110.
    frame.attrs["synthetic_marker"] = "preserve this input attribute"
    return frame


@pytest.mark.parametrize("invalid_items", [(), ("SYM-BAD",), ("SYM-GOOD", "SYM-BAD")])
def test_diagnostics_preserve_runtime_exit_state_delivery_order_and_provider_calls(
    tmp_path, monkeypatch, invalid_items,
):
    symbols = ["SYM-GOOD", "SYM-BAD"]
    frames = {symbol: _prices(symbol in invalid_items) for symbol in symbols}
    originals = {symbol: frame.copy(deep=True) for symbol, frame in frames.items()}
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"positions": {}, "alert_log": {}}), encoding="utf-8")
    monkeypatch.setattr(stop_manager, "DATA_FILE", state)
    monkeypatch.setattr(stop_manager, "_now", lambda: "2026-01-12 18:00:00")
    monkeypatch.setattr(monitor, "STATE_FILE", state)
    for symbol in symbols:
        stop_manager.add_position(symbol, 100., 80.)
    baseline = state.read_bytes()
    original_observer = data_collector.observe_price_stage

    def execute(enabled):
        state.write_bytes(baseline)
        effects, provider_calls, observed_stages = [], [], []

        class SyntheticTicker:
            def __init__(self, symbol):
                self.symbol = symbol

            def history(self, **kwargs):
                provider_calls.append(("history", self.symbol, kwargs))
                return frames[self.symbol].copy(deep=True)

            def get_history_metadata(self):
                provider_calls.append(("metadata", self.symbol))
                return {}

        class SyntheticDrive:
            def pull(self):
                effects.append(("pull",))
                return json.loads(state.read_text(encoding="utf-8"))

            def push(self):
                effects.append(("push", state.read_bytes()))

        def from_env(path):
            assert path == state
            effects.append(("from_env",))
            return SyntheticDrive()

        def send_message(message, *args, **kwargs):
            effects.append(("message", message, args, kwargs, state.read_bytes()))
            return True

        def observe(stage, frame):
            observed_stages.append(stage)
            return original_observer(stage, frame)

        with monkeypatch.context() as patch:
            patch.delenv("GITHUB_ACTIONS", raising=False)
            patch.setenv("GHA_JOB", "stop_check")
            patch.setattr(data_collector.yf, "Ticker", SyntheticTicker)
            patch.setattr(data_collector, "utc_now", lambda *args: NOW)
            patch.setattr(data_collector, "observe_price_stage", observe if enabled else lambda *a: None)
            patch.setattr(monitor, "ALL_SYMBOLS", symbols)
            patch.setattr(monitor, "fetch_portfolio", data_collector.fetch_portfolio)
            patch.setattr(monitor, "calc_chandelier_stop", atr_calculator.calc_chandelier_stop)
            patch.setattr(monitor, "load_stops", stop_manager.load_all)
            patch.setattr(monitor, "update_stop", stop_manager.update_stop)
            patch.setattr(monitor, "_problems", [])
            patch.setattr(monitor, "check_immediate_triggers", lambda *a: SimpleNamespace(has_trigger=False))
            patch.setattr(monitor, "_send_chart_quietly", lambda *a: effects.append(("chart", a[0], a[2])))
            patch.setattr(monitor.drive_state, "from_env", from_env)
            patch.setattr(monitor, "tg", SimpleNamespace(
                send_message=send_message,
                fmt_stop_update=lambda pending: "synthetic stop update: " + pending.symbol,
            ))
            try:
                monitor.run_github_actions_mode()
            except SystemExit as error:
                code = error.code
            else:
                code = 0
            result = (code, state.read_bytes(), effects, provider_calls, tuple(monitor._problems))
        return result, observed_stages

    without_diagnostics, disabled_stages = execute(False)
    with_diagnostics, enabled_stages = execute(True)
    assert with_diagnostics == without_diagnostics
    assert not disabled_stages
    assert "history_normalized" in enabled_stages
    assert "latest_quote" in enabled_stages
    assert with_diagnostics[0] == (1 if invalid_items else 0)
    assert sum(effect[0] == "push" for effect in with_diagnostics[2]) == 1
    positions = json.loads(with_diagnostics[1])["positions"]
    for symbol in symbols:
        if symbol in invalid_items:
            assert positions[symbol] == json.loads(baseline)["positions"][symbol]
        else:
            assert positions[symbol]["current_stop"] > 80.
        pd.testing.assert_frame_equal(frames[symbol], originals[symbol])
        assert frames[symbol].attrs == originals[symbol].attrs
