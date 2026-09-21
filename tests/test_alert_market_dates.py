"""Synthetic alert-state dates across market midnight and DST boundaries."""
from datetime import datetime, timezone
import json

import pytest

import stop_manager as sm


TRIGGERS = ["STOP NEAR 1.0%"]


def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


@pytest.fixture
def local_state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"positions": {}, "alert_log": {}}', encoding="utf-8")
    monkeypatch.setattr(sm, "DATA_FILE", path)
    return path


@pytest.mark.parametrize("symbol,when,expected", [
    ("SYM-US", "2026-01-13T00:10", "2026-01-12"),
    ("SYM-US", "2026-07-14T00:10", "2026-07-13"),
    ("SYM-KR.KS", "2026-01-12T15:10", "2026-01-13"),
    ("SYM-KR.KQ", "2026-07-13T15:10", "2026-07-14"),
    ("SYM-USD", "2026-01-13T00:10", "2026-01-13"),
    ("SYM-USDT", "2026-07-14T00:10", "2026-07-14"),
    ("SYM-US", "2026-03-08T04:59", "2026-03-07"),
    ("SYM-US", "2026-03-08T05:00", "2026-03-08"),
    ("SYM-US", "2026-03-09T03:59", "2026-03-08"),
    ("SYM-US", "2026-03-09T04:00", "2026-03-09"),
    ("SYM-US", "2026-11-01T03:59", "2026-10-31"),
    ("SYM-US", "2026-11-01T04:00", "2026-11-01"),
    ("SYM-US", "2026-11-02T04:59", "2026-11-01"),
    ("SYM-US", "2026-11-02T05:00", "2026-11-02"),
])
def test_successful_mark_uses_symbol_market_date_and_aware_utc_timestamp(local_state, symbol, when, expected):
    now = utc(when)
    sm.mark_trigger_sent(symbol, TRIGGERS, 100.0, 99.0, now_utc=now)
    entry = json.loads(local_state.read_bytes())["alert_log"][symbol]
    assert entry["date"] == expected
    assert entry["date_basis"] == "market-v1"
    assert entry["sent_at"] == now.isoformat(timespec="minutes")
    assert datetime.fromisoformat(entry["sent_at"]).tzinfo == timezone.utc
    assert sm.should_send_trigger_alert(symbol, TRIGGERS, 100.0, 99.0, now_utc=now) is False


@pytest.mark.parametrize("start,check", [
    ("2026-01-12T23:50", "2026-01-13T00:10"),
    ("2026-07-13T23:50", "2026-07-14T00:10"),
    ("2026-11-01T05:30", "2026-11-01T06:30"),
])
def test_us_same_market_day_does_not_repeat_at_utc_midnight_or_repeated_dst_hour(local_state, start, check):
    sm.mark_trigger_sent("SYM-US", TRIGGERS, 100.0, 99.0, now_utc=utc(start))
    before = local_state.read_bytes()
    assert sm.should_send_trigger_alert("SYM-US", TRIGGERS, 100.0, 99.0, now_utc=utc(check)) is False
    assert local_state.read_bytes() == before


@pytest.mark.parametrize("symbol,start,check", [
    ("SYM-US", "2026-01-13T04:59", "2026-01-13T05:00"),
    ("SYM-US", "2026-07-14T03:59", "2026-07-14T04:00"),
    ("SYM-KR.KS", "2026-01-12T14:59", "2026-01-12T15:00"),
    ("SYM-KR.KQ", "2026-07-13T14:59", "2026-07-13T15:00"),
    ("SYM-USD", "2026-01-12T23:59", "2026-01-13T00:00"),
    ("SYM-USDT", "2026-07-13T23:59", "2026-07-14T00:00"),
])
def test_each_market_starts_a_new_alert_day_at_its_own_midnight(local_state, symbol, start, check):
    sm.mark_trigger_sent(symbol, TRIGGERS, 100.0, 99.0, now_utc=utc(start))
    assert sm.should_send_trigger_alert(symbol, TRIGGERS, 100.0, 99.0, now_utc=utc(check)) is True


def test_legacy_naive_timestamp_is_not_reinterpreted_or_bulk_rewritten(local_state):
    legacy = {"positions": {}, "alert_log": {
        "SYM-US": {"date": "2026-01-13", "sent_at": "2026-01-13T00:10",
                   "triggers": TRIGGERS, "close": 100.0, "stop": 99.0},
        "SYM-OTHER": {"date": "2026-01-12", "sent_at": "2026-01-12T18:00"},
    }}
    local_state.write_text(json.dumps(legacy), encoding="utf-8")
    before = local_state.read_bytes()
    now = utc("2026-01-13T00:10")
    assert sm.should_send_trigger_alert("SYM-US", TRIGGERS, 100.0, 99.0, now_utc=now) is True
    assert local_state.read_bytes() == before
    sm.mark_trigger_sent("SYM-US", TRIGGERS, 100.0, 99.0, now_utc=now)
    after = json.loads(local_state.read_bytes())
    assert after["alert_log"]["SYM-US"]["date"] == "2026-01-12"
    assert after["alert_log"]["SYM-US"]["date_basis"] == "market-v1"
    assert after["alert_log"]["SYM-US"]["sent_at"] == "2026-01-13T00:10+00:00"
    assert after["alert_log"]["SYM-OTHER"] == legacy["alert_log"]["SYM-OTHER"]


@pytest.mark.parametrize("basis", [None, "utc-v0", "unknown"])
def test_legacy_date_equal_to_current_market_day_cannot_suppress_first_market_alert(local_state, basis):
    # A host-UTC Jan 13 record may have been sent during New York Jan 12 evening.
    # Its unchanged date must not suppress New York Jan 13's first valid alert.
    entry = {"date": "2026-01-13", "sent_at": "2026-01-13T00:10",
             "triggers": TRIGGERS, "close": 100.0, "stop": 99.0}
    if basis is not None:
        entry["date_basis"] = basis
    legacy = {"positions": {}, "alert_log": {"SYM-US": entry}}
    local_state.write_text(json.dumps(legacy), encoding="utf-8")
    before = local_state.read_bytes()
    now = utc("2026-01-13T15:00")
    # If delivery has not succeeded, subsequent reads must still request it.
    for _ in range(2):
        assert sm.should_send_trigger_alert("SYM-US", TRIGGERS, 100.0, 99.0, now_utc=now) is True
        assert local_state.read_bytes() == before
    sm.mark_trigger_sent("SYM-US", TRIGGERS, 100.0, 99.0, now_utc=now)
    updated = json.loads(local_state.read_bytes())["alert_log"]["SYM-US"]
    assert updated["date"] == "2026-01-13"
    assert updated["date_basis"] == "market-v1"
    assert updated["sent_at"] == "2026-01-13T15:00+00:00"
    assert sm.should_send_trigger_alert("SYM-US", TRIGGERS, 100.0, 99.0, now_utc=now) is False


@pytest.mark.parametrize("operation", ["check", "mark"])
def test_a_naive_injected_clock_is_rejected_without_modifying_state(local_state, operation):
    before = local_state.read_bytes()
    function = sm.should_send_trigger_alert if operation == "check" else sm.mark_trigger_sent
    with pytest.raises(ValueError):
        function("SYM-US", TRIGGERS, 100.0, 99.0, now_utc=datetime(2026, 1, 13, 0, 10))
    assert local_state.read_bytes() == before


@pytest.mark.parametrize("operation", ["check", "mark"])
def test_each_function_captures_one_clock_and_passes_it_to_market_date(local_state, monkeypatch, operation):
    now = utc("2026-01-13T04:59")
    clocks, dates = [], []
    original_market_date = sm.market_date

    def clock(value=None):
        clocks.append(value)
        assert len(clocks) == 1
        return now

    def market_day(symbol, now_utc=None):
        dates.append(now_utc)
        assert now_utc is now
        return original_market_date(symbol, now_utc=now_utc)

    monkeypatch.setattr(sm, "utc_now", clock)
    monkeypatch.setattr(sm, "market_date", market_day)
    function = sm.should_send_trigger_alert if operation == "check" else sm.mark_trigger_sent
    function("SYM-US", TRIGGERS, 100.0, 99.0)
    assert clocks == [None]
    assert dates == [now]


@pytest.mark.parametrize("triggers,close,stop,expected", [
    (["STOP NEAR 2.0%"], 100.0, 100.0, False),
    (["STOP BREACH 1.0%"], 100.0, 100.0, True),
    (TRIGGERS, 105.0, 100.0, False),
    (TRIGGERS, 105.01, 100.0, True),
    (TRIGGERS, 100.0, 100.5, False),
    (TRIGGERS, 100.0, 100.51, True),
])
def test_content_and_price_threshold_policies_are_unchanged(local_state, triggers, close, stop, expected):
    now = utc("2026-01-13T00:10")
    sm.mark_trigger_sent("SYM-US", TRIGGERS, 100.0, 100.0, now_utc=now)
    assert sm.should_send_trigger_alert("SYM-US", triggers, close, stop, now_utc=now) is expected
