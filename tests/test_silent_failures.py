"""손절 감시가 '실패했는데 성공으로 기록되는' 경로를 막는다 (2026-09-19 심층 검토 ATR-01~05)."""
from datetime import datetime, timezone

import pandas as pd
import pytest

import atr_calculator

NOW_UTC = datetime(2026, 1, 12, 12, 0, tzinfo=timezone.utc)


def _bars(last_close, rows=30, base=100.0):
    idx = pd.date_range(end="2026-01-12", periods=rows, freq="D")
    df = pd.DataFrame({"Open": base, "High": base * 1.01, "Low": base * 0.99, "Close": base, "Volume": 1000.0}, index=idx)
    df.iloc[-1, df.columns.get_loc("Close")] = last_close
    df.iloc[-1, df.columns.get_loc("Low")] = min(last_close, base * 0.99)
    df.iloc[-1, df.columns.get_loc("High")] = max(last_close, base * 1.01)
    return df


# ── ATR-01 ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("symbol, last_close", [("999991.KQ", 60.0), ("999992.KS", 140.0)])
def test_kr_price_limit_guard_returns_quietly_instead_of_raising(symbol, last_close, caplog):
    """액면분할·권리락 당일(.KQ 는 auto_adjust=False)에 정확히 이 분기를 탄다. 예전엔 NameError."""
    result = atr_calculator.check_immediate_triggers(symbol, _bars(last_close), None, now_utc=NOW_UTC)
    assert result.triggers == []
    assert any("가격제한폭" in r.getMessage() for r in caplog.records)


# ── 공통: monitor 의 외부 의존을 전부 가짜로 ─────────────────────────────────
import json
import types

import monitor
import stop_manager as sm


class FakeTelegram:
    def __init__(self, ok=True):
        self.ok, self.messages, self.photos = ok, [], []

    def send_message(self, text, *a, **k):
        self.messages.append(text)
        return self.ok

    def send_photo(self, *a, **k):
        self.photos.append(a)
        return self.ok

    def send_long_message(self, text, *a, **k):
        self.messages.append(text)
        return self.ok

    def __getattr__(self, name):                     # fmt_* 는 식별 가능한 문자열만 돌려준다
        if name.startswith("fmt_"):
            return lambda *a, **k: f"{name}:{a[0] if a else ''}"
        raise AttributeError(name)


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = tmp_path / "stop_levels.json"
    state.write_text(json.dumps({"positions": {}, "alert_log": {}}), encoding="utf-8")
    monkeypatch.setattr(sm, "DATA_FILE", state)
    tg = FakeTelegram()
    monkeypatch.setattr(monitor, "tg", tg)
    monkeypatch.setattr(monitor, "plot_atr_chart", lambda *a, **k: b"png")
    monkeypatch.setattr(monitor, "_is_market_active_for_triggers", lambda s: True)
    monkeypatch.setattr(monitor, "calc_chandelier_stop",
                        lambda symbol, df, period: types.SimpleNamespace(symbol=symbol, stop_level=90.0,
                                                                          current_close=100.0, highest_high=110.0))
    monkeypatch.setattr(monitor, "_problems", [])
    return types.SimpleNamespace(tg=tg, state=state, monkeypatch=monkeypatch)


def _market(env, symbols, triggers):
    """symbols 전부 시세가 오고, triggers[symbol] 이 예외면 던지고 아니면 그 트리거 목록을 돌려준다."""
    env.monkeypatch.setattr(monitor, "fetch_portfolio", lambda syms: {s: _bars(100.0) for s in symbols})

    def check(symbol, df, stop):
        outcome = triggers.get(symbol, [])
        if isinstance(outcome, Exception):
            raise outcome
        return types.SimpleNamespace(has_trigger=bool(outcome), triggers=outcome)
    env.monkeypatch.setattr(monitor, "check_immediate_triggers", check)


# ── ATR-02: 한 종목의 예외가 나머지 종목을 멈추지 않는다 ─────────────────────
def test_one_broken_symbol_does_not_stop_the_others(env):
    _market(env, ["AAA", "BBB"], {"AAA": NameError("boom"), "BBB": ["SURGE DOWN -6%"]})
    monitor.job_stop_check(["AAA", "BBB"])
    assert any("BBB" in m for m in env.tg.messages)           # 뒤 종목의 급락 알림은 나간다
    assert any("AAA" in p or "1/2" in p for p in monitor._problems)


def test_a_broken_chart_does_not_block_the_text_alert_or_its_record(env):
    _market(env, ["BBB"], {"BBB": ["SURGE DOWN -6%"]})
    env.monkeypatch.setattr(monitor, "plot_atr_chart", lambda *a, **k: (_ for _ in ()).throw(ValueError("chart")))
    monitor.job_stop_check(["BBB"])
    assert any("BBB" in m for m in env.tg.messages)
    assert "BBB" in json.loads(env.state.read_text(encoding="utf-8"))["alert_log"]


# ── ATR-03: 전송에 실패한 알림을 '보냄' 으로 적지 않는다 ─────────────────────
def test_failed_trigger_send_is_not_recorded_as_sent(env):
    _market(env, ["BBB"], {"BBB": ["SURGE DOWN -6%"]})
    env.tg.ok = False
    monitor.job_stop_check(["BBB"])
    assert json.loads(env.state.read_text(encoding="utf-8"))["alert_log"] == {}     # 다음 실행이 다시 보낸다
    assert monitor._problems


def test_failed_stop_update_notice_leaves_the_stop_unchanged_for_a_retry(env):
    sm.add_position("BBB", entry_price=80.0, initial_stop=70.0, highest_high=100.0)
    _market(env, ["BBB"], {})
    env.tg.ok = False
    monitor.job_stop_check(["BBB"])
    assert sm.load_all()["BBB"].current_stop == 70.0      # 저장부터 하면 '지정가 갱신 필요' 알림이 영영 사라진다
    env.tg.ok = True
    monitor.job_stop_check(["BBB"])
    assert sm.load_all()["BBB"].current_stop == 90.0 and any("fmt_stop_update" in m for m in env.tg.messages)


def test_failed_daily_brief_send_raises_so_the_window_stays_open(env):
    env.tg.ok = False
    result = monitor.StopCheckResult(chandelier=[], updated_symbols=[], ohlcv_map={})
    with pytest.raises(RuntimeError):
        monitor._send_daily_brief(types.SimpleNamespace(market="KR"), result)


# ── ATR-04: 시세를 못 받았으면 '이상 없음' 이 아니다 ─────────────────────────
@pytest.mark.parametrize("received", [[], ["S0", "S1", "S2", "S3", "S4"]])
def test_collection_below_the_threshold_is_a_failure_not_a_quiet_success(env, received):
    wanted = [f"S{i}" for i in range(10)]
    env.monkeypatch.setattr(monitor, "fetch_portfolio", lambda syms: {s: _bars(100.0) for s in received})
    env.monkeypatch.setattr(monitor, "check_immediate_triggers",
                            lambda *a: types.SimpleNamespace(has_trigger=False, triggers=[]))
    with pytest.raises(RuntimeError, match="시세 수집"):
        monitor.job_stop_check(wanted)
    assert any(f"{len(received)}/10" in p for p in monitor._problems)


def test_nearly_complete_collection_passes(env):
    wanted = [f"S{i}" for i in range(10)]
    env.monkeypatch.setattr(monitor, "fetch_portfolio", lambda syms: {s: _bars(100.0) for s in wanted[:9]})
    env.monkeypatch.setattr(monitor, "check_immediate_triggers",
                            lambda *a: types.SimpleNamespace(has_trigger=False, triggers=[]))
    assert monitor.job_stop_check(wanted) is not None


# ── 실패한 실행은 빨갛게 끝난다 ──────────────────────────────────────────────
def test_run_with_problems_pushes_state_then_exits_non_zero_and_tells_the_owner(env):
    pushed = []
    env.monkeypatch.setenv("GHA_JOB", "stop_check")
    env.monkeypatch.setattr(monitor.drive_state, "from_env",
                            lambda path: types.SimpleNamespace(pull=lambda: None, push=lambda: pushed.append(1)))
    env.monkeypatch.setattr(monitor, "job_stop_check", lambda *a: monitor._note_problem("종목 처리 실패 1/2"))
    with pytest.raises(SystemExit) as stopped:
        monitor.run_github_actions_mode()
    assert stopped.value.code == 1 and pushed == [1]
    assert any("종목 처리 실패 1/2" in m for m in env.tg.messages)


def test_clean_run_exits_normally(env):
    env.monkeypatch.setenv("GHA_JOB", "stop_check")
    env.monkeypatch.setattr(monitor.drive_state, "from_env", lambda path: None)
    env.monkeypatch.setattr(monitor, "job_stop_check", lambda *a: None)
    monitor.run_github_actions_mode()                     # SystemExit 없이 끝난다


# ── 빠른 트리거 체크 경로도 같은 규칙 ────────────────────────────────────────
def test_trigger_check_does_not_record_an_unsent_alert_and_survives_a_broken_symbol(env):
    _market(env, ["AAA", "BBB"], {"AAA": ValueError("bad row"), "BBB": ["GAP DOWN -4%"]})
    env.monkeypatch.setattr(monitor, "ALL_SYMBOLS", ["AAA", "BBB"])
    env.tg.ok = False
    monitor.job_trigger_check()
    assert json.loads(env.state.read_text(encoding="utf-8"))["alert_log"] == {}
    assert len(monitor._problems) == 2                          # 종목 실패 + 전송 실패


# ── 텔레그램: 일시 장애는 다시 시도하고, 토큰이 든 URL 은 로그에 남기지 않는다 ─
import requests

import telegram_bot


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = ({"ok": True} if status == 200 else {}) if payload is None else payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} for url: https://api.telegram.org/botSECRET-TOKEN/sendMessage", response=self)

    def json(self):
        return self._payload


@pytest.fixture
def telegram(monkeypatch):
    monkeypatch.setattr(telegram_bot, "_is_configured", lambda: True)
    monkeypatch.setattr(telegram_bot, "_url", lambda method: f"https://api.telegram.org/botSECRET-TOKEN/{method}")
    monkeypatch.setattr(telegram_bot.time, "sleep", lambda seconds: None)
    return monkeypatch


@pytest.mark.parametrize("first", [_Resp(429, {"parameters": {"retry_after": 1}}), _Resp(502),
                                   requests.exceptions.ConnectionError("https://api.telegram.org/botSECRET-TOKEN/x")])
def test_transient_failure_is_retried(telegram, first):
    outcomes = [first, _Resp(200)]

    def post(*a, **k):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    telegram.setattr(telegram_bot.requests, "post", post)
    assert telegram_bot.send_message("hello") is True and outcomes == []


def test_persistent_failure_returns_false_without_logging_the_token(telegram, caplog):
    telegram.setattr(telegram_bot.requests, "post", lambda *a, **k: _Resp(500))
    assert telegram_bot.send_message("hello") is False
    assert "SECRET-TOKEN" not in caplog.text


# ── ATR-05: GHA 에서 실보유 대신 예시 5종목(또는 0종목)을 감시하며 초록색으로 끝나지 않는다 ──
@pytest.mark.parametrize("source, configured, symbols, expect_problem", [
    ("drive", True, ["A", "B"], False),
    ("fallback", True, ["A", "B", "C", "D", "E"], True),      # Drive 로드 실패 → 하드코딩 예시
    ("stock_list", True, ["A"], True),                         # Drive 실패 → 낡았을 수 있는 레거시 목록
    ("drive", True, [], True),                                 # 빈 시트·헤더 변경
    ("stock_list", False, ["A"], False),                       # Drive 를 아예 안 쓰는 구성은 그대로 허용
])
def test_portfolio_source_is_checked_on_github_actions(env, source, configured, symbols, expect_problem):
    env.monkeypatch.setattr(monitor._config, "PORTFOLIO_SOURCE", source, raising=False)
    env.monkeypatch.setattr(monitor._config, "DRIVE_PORTFOLIO_CONFIGURED", configured, raising=False)
    env.monkeypatch.setattr(monitor, "ALL_SYMBOLS", symbols)
    assert bool(monitor._portfolio_problem()) is expect_problem


def test_bad_portfolio_stops_the_run_before_any_check(env):
    env.monkeypatch.setenv("GHA_JOB", "stop_check")
    env.monkeypatch.setenv("GITHUB_ACTIONS", "true")
    env.monkeypatch.setattr(monitor, "_portfolio_problem", lambda: "포트폴리오 로드 실패")
    env.monkeypatch.setattr(monitor, "job_stop_check", lambda *a: pytest.fail("엉뚱한 목록으로 돌면 안 된다"))
    with pytest.raises(SystemExit) as stopped:
        monitor.run_github_actions_mode()
    assert stopped.value.code == 1 and any("포트폴리오" in m for m in env.tg.messages)


def test_empty_drive_sheet_is_not_treated_as_a_loaded_portfolio():
    import config
    assert config._portfolio_source({"포트폴리오": []}, "") == "fallback"
    assert config._portfolio_source({"포트폴리오": ["A"]}, "") == "drive"
    assert config._portfolio_source(None, '{"x": ["A"]}') == "stock_list"
