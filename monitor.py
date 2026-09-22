"""
포트폴리오 ATR Trailing Stop 모니터링 메인 루프

실행 모드 (CLI):
  python monitor.py                  ← 스케줄 데몬 (로컬 실행용)
  python monitor.py --once           ← 즉시 1회 전체 리포트 (KR+US+Crypto)
  python monitor.py --stop-check     ← 즉시 1회 Stop 갱신 체크 (전 종목)
  python monitor.py --trigger-check  ← 즉시 1회 즉각 트리거 체크
  python monitor.py --kr-report      ← 즉시 국내 일일 리포트
  python monitor.py --us-report      ← 즉시 미국+크립토 일일 리포트
  python monitor.py --chart AAPL     ← 특정 종목 차트 전송
  python monitor.py --add-pos 005930.KS 72000 67000   ← 포지션 등록
  python monitor.py --remove-pos 005930.KS             ← 포지션 제거
  python monitor.py --list-pos                         ← 포지션 현황 출력

GitHub Actions는 외부 dispatch로 한 번 실행하며 GHA_JOB이 작업을 고른다.
auto는 전 종목 Stop 점검 후 market_hours의 현행 창에 해당하는 추가 작업을 실행한다.
로컬 데몬은 config의 KR/US 시각에 매일 리포트, 30분 Stop, 10분 트리거를 실행한다.
관리 명령의 --state-scope local|drive, --replace, --recover-state는 position_cli가 처리한다.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

# Administration must validate arguments before config can load a portfolio.
if __name__ == '__main__':
    from position_cli import maybe_run
    administration_result = maybe_run(sys.argv[1:])
    if administration_result is not None:
        raise SystemExit(administration_result)

import log_masking
log_masking.install_exception_hooks_for_github_actions()

import schedule

import config as _config
from config import (
    ALL_SYMBOLS,
    KR_STOCK_NAMES,
    KR_SYMBOLS,
    US_SYMBOLS,
    CRYPTO_SYMBOLS,
    KR_REPORT_TIME,
    US_REPORT_TIME,
    ATR_PERIOD,
    fmt_symbol,
)
from data_collector import fetch_portfolio, fetch_ohlcv
from atr_calculator import (
    summarize_portfolio_atr,
    calc_chandelier_stop,
    atr_input_issue,
    check_immediate_triggers,
)
from visualizer import plot_portfolio_atr_bar, plot_atr_chart
import drive_state
from state_validation import StateValidationError, state_locked
from state_lock import StateLockError, state_transaction
from stop_manager import (
    DATA_FILE as STATE_FILE,
    load_all as load_stops,
    update_stop,
    add_position,
    remove_position,
    summary_text as stop_summary_text,
    should_send_trigger_alert,
    mark_trigger_sent,
    is_window_done,
    mark_window_done,
)
import telegram_bot as tg
import market_hours
from dataclasses import dataclass

# ─────────────────────────────────────────────────────────────
# 로깅 설정
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("monitor")

IS_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS", "").lower() == "true"

# public repo 라 Actions 로그가 공개된다 → GHA 에서만 실보유 티커·종목명을 가명으로 치환한다.
# basicConfig 가 핸들러를 만든 뒤에 불러야 필터가 붙는다. config import 단계의 로그는
# 종목 수만 찍으므로 이 시점보다 앞서 티커가 새는 경로는 없다. (2026-09-11 점검)
log_masking.install_for_github_actions(ALL_SYMBOLS, KR_STOCK_NAMES)


# ─────────────────────────────────────────────────────────────
# 시장 활성 시간 게이트
# ─────────────────────────────────────────────────────────────

def _is_market_active_for_triggers(symbol: str, *, now_utc=None) -> bool:
    """해당 종목의 시장이 지금 트리거 알람을 발송할 수 있는 활성 시간대인지 반환합니다.

    판정은 market_hours 로 위임한다. 예전에는 여기서 UTC 시를 직접 계산했는데,
    그 방식이 KR 애프터마켓 연장(2026-09-14)과 US 서머타임을 둘 다 놓쳤다.
    ETF의 ATR 자산 분류와 별개로 심볼의 거래시장 시간대를 적용한다.
    """
    from config import get_trading_market
    from market_dates import utc_now
    from market_hours import is_market_active

    return is_market_active(get_trading_market(symbol), utc_now(now_utc))


# ─────────────────────────────────────────────────────────────
# 핵심 작업 함수
# ─────────────────────────────────────────────────────────────

@dataclass
class StopCheckResult:
    """종가 창의 일일 요약에 필요한 값. 요약이 붙지 않는 창에서는 쓰이지 않는다.

    전 종목 결과다 — 요약은 창의 시장 종목만 골라 쓴다(_send_daily_brief).
    """
    chandelier:      list
    updated_symbols: list
    ohlcv_map:       dict


# ── 실행 중 생긴 문제 모음 ──────────────────────────────────────────────────
# 손절 감시에서 가장 위험한 것은 '실패했는데 초록색으로 끝나는 실행' 이다(2026-09-19 검토 ATR-02~05).
# 종목 하나의 예외, 시세 수집 전멸, 텔레그램 전송 실패를 여기 모았다가 실행 끝에
# 소유자에게 한 번 알리고 종료코드 1 로 끝낸다 — Actions 목록에 빨갛게 남고 운영 감시도 잡는다.
_problems: list[str] = []

# 시세를 받은 종목 비율이 이보다 낮으면 그 실행의 '이상 없음' 은 믿을 수 없다.
MIN_COLLECTION_RATIO = 0.8


def _portfolio_problem() -> str | None:
    """감시 대상 목록을 믿을 수 있는지. 상태 파일은 로드 실패 시 중단하는데(fail-closed)
    포트폴리오는 조용히 예시 종목으로 넘어가던 비대칭을 없앤다(ATR-05)."""
    if getattr(_config, "PORTFOLIO_ERROR", ""):
        return "포트폴리오 설정 오류: " + _config.PORTFOLIO_ERROR
    if not ALL_SYMBOLS:
        return "감시 대상 0종목 — 포트폴리오 시트가 비었거나 헤더(Ticker·종목·구분)가 바뀌었습니다"
    if _config.DRIVE_PORTFOLIO_CONFIGURED and _config.PORTFOLIO_SOURCE != "drive":
        return ("Drive 포트폴리오를 읽지 못해 다른 목록(" + _config.PORTFOLIO_SOURCE
                + f", {len(ALL_SYMBOLS)}종목)으로 넘어갔습니다 — 실보유를 감시하지 못합니다")
    return None


def _note_problem(message: str) -> None:
    logger.error("실행 문제: %s", message)
    _problems.append(message)


def _send_chart_quietly(symbol, df, stop, caption: str) -> None:
    """차트는 부속물이다. 차트 버그가 텍스트 알림과 그 기록을 막으면 안 된다."""
    try:
        tg.send_photo(plot_atr_chart(symbol, df, registered_stop=stop, as_bytes=True), caption=caption)
    except Exception as exc:
        logger.warning("차트 전송 실패 — 텍스트 알림은 이미 나갔다: %s (%s)", symbol, type(exc).__name__)


def _state_path(*args, **kwargs):
    return STATE_FILE


@state_locked(_state_path)
def job_stop_check(symbols: list[str] | None = None) -> None:
    """
    [30분 주기] Chandelier Stop 갱신 체크.

    symbols=None 이면 ALL_SYMBOLS 전 종목 처리.
    주말 크립토 전용 체크 시 CRYPTO_SYMBOLS 전달.

    각 종목:
      1. Chandelier Stop 재계산
      2. 등록 Stop과 비교 → New > Current 이면 갱신 알림
      3. 즉각 트리거 감지 → 긴급 알림
    """
    syms = symbols if symbols is not None else ALL_SYMBOLS
    logger.info("Stop 갱신 체크 시작 (%d종목)", len(syms))
    ohlcv_map   = fetch_portfolio(syms)
    stop_recs   = load_stops()
    updated_symbols: list[str] = []

    chandelier_list = []
    failed: list[str] = []
    unsent = 0
    for symbol, df in ohlcv_map.items():
        if df.empty:
            continue
        # 종목 하나의 예외(데이터 이상, 계산·포맷 버그)가 뒤 종목 전부를 멈추면 안 된다(ATR-02).
        try:
            ch = calc_chandelier_stop(symbol, df, ATR_PERIOD)
            if ch is None:
                failed.append(symbol)
                logger.error("ATR 계산 불가 — 상태 유지 (%s)", atr_input_issue(df, ATR_PERIOD) or "calculation_unavailable")
                continue
            chandelier_list.append(ch)

            # 즉각 트리거 체크
            rec          = stop_recs.get(symbol)
            current_stop = rec.current_stop if rec else None
            trigger      = check_immediate_triggers(symbol, df, current_stop)
            if trigger.has_trigger:
                if not _is_market_active_for_triggers(symbol):
                    logger.debug("트리거 스킵 (장 마감): %s — %s", symbol, trigger.triggers)
                else:
                    close = float(df["Close"].iloc[-1])
                    if should_send_trigger_alert(symbol, trigger.triggers, close, current_stop):
                        # 전송에 성공했을 때만 '보냄' 으로 적는다. 실패를 적어 두면 텔레그램이
                        # 회복된 다음 실행이 '중복' 이라며 건너뛴다(ATR-03).
                        if tg.send_message(tg.fmt_trigger_alert(symbol, trigger.triggers, close, current_stop)):
                            mark_trigger_sent(symbol, trigger.triggers, close, current_stop)
                            _send_chart_quietly(symbol, df, current_stop,
                                                f"긴급: {fmt_symbol(symbol)} 트리거 감지")
                            logger.warning("트리거 감지 알림: %s — %s", symbol, trigger.triggers)
                        else:
                            unsent += 1
                    else:
                        logger.info("트리거 중복 스킵: %s (동일 조건 발송됨)", symbol)

            # Stop 갱신 (등록된 포지션만)
            if rec is None:
                continue

            stop_args = dict(symbol=symbol, new_stop=ch.stop_level,
                             current_close=ch.current_close, new_hh=ch.highest_high)
            pending = update_stop(**stop_args, commit=False)
            if pending.updated:
                # 알림이 나간 뒤에 저장한다. 순서가 반대면 전송 실패 시 '지정가 갱신 필요' 가 영영 사라진다.
                if tg.send_message(tg.fmt_stop_update(pending)):
                    result = update_stop(**stop_args)
                    updated_symbols.append(symbol)
                    _send_chart_quietly(symbol, df, result.new_stop,
                                        f"{symbol} Stop 갱신: {result.prev_stop:,.2f} -> {result.new_stop:,.2f}")
                    logger.info("Stop 갱신 알림 전송: %s", symbol)
                else:
                    unsent += 1
        except Exception as exc:
            failed.append(symbol)
            logger.error("종목 처리 실패 — 다음 종목으로 계속: %s (%s)", symbol, type(exc).__name__)

    if failed:
        _note_problem(f"종목 처리 실패 {len(failed)}/{len(ohlcv_map)}")
    if unsent:
        _note_problem(f"텔레그램 전송 실패 {unsent}건 — 기록하지 않았으므로 다음 실행이 다시 보낸다")

    if not updated_symbols:
        logger.info("Stop 갱신 없음 (전 종목 유지)")

    # 시세를 못 받은 종목이 많으면 '트리거 없음' 은 '못 봤다' 는 뜻이다(ATR-04). 받은 종목은 위에서
    # 이미 처리했다. 여기서 실패로 올리면 종가 요약 창이 완료로 적히지 않아 다음 실행이 다시 시도한다.
    collected = sum(1 for df in ohlcv_map.values() if not df.empty)
    if syms and collected < len(syms) * MIN_COLLECTION_RATIO:
        _note_problem(f"시세 수집 {collected}/{len(syms)}")
        raise RuntimeError(f"시세 수집 부족 {collected}/{len(syms)}")

    logger.info("Stop 갱신 체크 완료")
    return StopCheckResult(
        chandelier      = chandelier_list,
        updated_symbols = updated_symbols,
        ohlcv_map       = ohlcv_map,
    )


def _get_data_date(ohlcv_map: dict) -> str:
    """
    ohlcv_map 내 종목들의 공통 데이터 기준일을 반환합니다.

    가장 많은 종목이 공유하는 마지막 거래일을 선택합니다.
    """
    from collections import Counter
    dates = []
    for df in ohlcv_map.values():
        if df.empty:
            continue
        last_idx = df.index[-1]
        d = last_idx.date() if hasattr(last_idx, "date") else last_idx
        dates.append(d)
    if not dates:
        return "?"
    most_common = Counter(dates).most_common(1)[0][0]
    return most_common.strftime("%Y-%m-%d")


@state_locked(_state_path)
def _run_daily_report(symbols: list[str], title: str) -> None:
    """
    시장별 일일 ATR 리포트 공통 로직.

    Parameters
    ----------
    symbols : 리포트 대상 심볼 리스트
    title   : 텔레그램 메시지 헤더 (이모지 포함)

    필수 본문 생성·전송 실패는 호출자에게 전달해 창을 미완료로 남긴다.
    재시도는 본문 전체를 다시 보내므로 이전에 성공한 조각은 중복될 수 있다.
    차트는 부속물이며 필수 본문 전송 후 별도로 시도한다.
    """
    logger.info("%s 시작", title)
    if not symbols:
        logger.info("%s — 해당 시장의 설정된 종목 없음", title)
        return

    ohlcv_map = {s: df for s, df in fetch_portfolio(symbols).items() if not df.empty}
    if not ohlcv_map:
        raise RuntimeError(f"리포트 시세 수집 실패 — {title}")

    summary = summarize_portfolio_atr(ohlcv_map, ATR_PERIOD)
    if summary.empty:
        raise RuntimeError(f"리포트 ATR 계산 실패 — {title}")

    data_date = _get_data_date(ohlcv_map)

    # 필수 보고 내용을 모두 계산한 뒤 전송한다. 계산 0건을 정상 리포트로 보내지 않는다.
    stop_recs       = load_stops()
    chandelier_list = []
    for symbol, df in ohlcv_map.items():
        ch = calc_chandelier_stop(symbol, df, ATR_PERIOD)
        if ch:
            chandelier_list.append(ch)
    if not chandelier_list:
        raise RuntimeError(f"리포트 Chandelier 계산 실패 — {title}")

    # 필수 본문의 어느 조각이든 실패하면 창을 완료로 기록하지 않는다.
    if not tg.send_long_message(tg.fmt_daily_report(summary, title, data_date=data_date)):
        raise RuntimeError("리포트 ATR 요약 전송 실패")
    if not tg.send_long_message(tg.fmt_chandelier_report(chandelier_list)):
        raise RuntimeError("리포트 Chandelier 현황 전송 실패")

    # 차트 실패가 이미 전달한 필수 본문을 실패로 바꾸거나 뒤 차트를 막지 않게 한다.
    try:
        bar_chart = plot_portfolio_atr_bar(summary, as_bytes=True)
        if not tg.send_photo(bar_chart, caption="ATR% 비교"):
            logger.warning("리포트 바차트 전송 실패 — 필수 본문은 이미 나갔다")
    except Exception as exc:
        logger.warning("리포트 바차트 실패 — 필수 본문은 이미 나갔다 (%s)", type(exc).__name__)

    # 4. 종목별 미니 차트 — Stop 근접 / ATR 스파이크 종목만 전송 (rate limit 방지)
    near_stop_syms = {ch.symbol for ch in chandelier_list if ch.is_near_stop}
    if "Spike" in summary.columns:
        spike_syms = set(summary.loc[summary["Spike"].astype(bool), "Symbol"])
    else:
        spike_syms = set()
    chart_targets = near_stop_syms | spike_syms

    logger.info(
        "종목별 미니 차트 전송 시작 (Stop근접 %d + 스파이크 %d = 총 %d종목)",
        len(near_stop_syms), len(spike_syms), len(chart_targets),
    )
    for symbol, df in ohlcv_map.items():
        if symbol not in chart_targets or df.empty:
            continue
        rec   = stop_recs.get(symbol)
        _send_chart_quietly(symbol, df, rec.current_stop if rec else None, fmt_symbol(symbol))
        time.sleep(4)   # Telegram rate limit 방지 (분당 15장 ≈ 안전 한도)

    logger.info("%s 완료", title)


def job_kr_daily_report() -> None:
    """[KST 금요일 18:00] 국내 종목 ATR 주간 리포트."""
    _run_daily_report(KR_SYMBOLS, "📈 국내 포트폴리오 ATR 주간 리포트")


def job_us_daily_report() -> None:
    """[KST 토요일 08:00] 미국+크립토 ATR 주간 리포트."""
    _run_daily_report(
        US_SYMBOLS + CRYPTO_SYMBOLS,
        "🌎 미국/크립토 포트폴리오 ATR 주간 리포트",
    )


@state_locked(_state_path)
def job_trigger_check() -> None:
    """[수시] 즉각 대응 트리거 빠른 체크 (Stop 갱신 없음)."""
    logger.info("트리거 체크 시작")
    ohlcv_map = fetch_portfolio(ALL_SYMBOLS)
    stop_recs = load_stops()
    found     = False

    failed: list[str] = []
    unsent = 0
    for symbol, df in ohlcv_map.items():
        if df.empty or len(df) < 22:
            continue
        try:
            rec          = stop_recs.get(symbol)
            current_stop = rec.current_stop if rec else None
            trigger      = check_immediate_triggers(symbol, df, current_stop)
            if not trigger.has_trigger:
                continue
            if not _is_market_active_for_triggers(symbol):
                logger.debug("트리거 스킵 (장 마감): %s — %s", symbol, trigger.triggers)
                continue
            close = float(df["Close"].iloc[-1])
            if should_send_trigger_alert(symbol, trigger.triggers, close, current_stop):
                found = True
                if tg.send_message(tg.fmt_trigger_alert(symbol, trigger.triggers, close, current_stop)):
                    mark_trigger_sent(symbol, trigger.triggers, close, current_stop)
                    _send_chart_quietly(symbol, df, current_stop, f"긴급: {fmt_symbol(symbol)}")
                else:
                    unsent += 1
            else:
                logger.info("트리거 중복 스킵: %s (동일 조건 발송됨)", symbol)
        except Exception as exc:
            failed.append(symbol)
            logger.error("종목 처리 실패 — 다음 종목으로 계속: %s (%s)", symbol, type(exc).__name__)

    if failed:
        _note_problem(f"종목 처리 실패 {len(failed)}/{len(ohlcv_map)}")
    if unsent:
        _note_problem(f"텔레그램 전송 실패 {unsent}건 — 기록하지 않았으므로 다음 실행이 다시 보낸다")

    if not found:
        logger.info("트리거 없음")


# ─────────────────────────────────────────────────────────────
# GitHub Actions 단일 실행 모드
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# 창(window) 실행기
#
# GHA schedule 배달률이 18% 라(2026-09-12 실측) "정각 실행" 을 전제할 수 없다.
# 창 안에서 오늘 아직 안 했으면 한다 — 트리거가 몇 번 떨어지든 창당 1회.
# 2026-09-17 부터는 PA 디스패처가 창마다 1회 job=auto 로 부른다 — 이 함수는 그대로 맞물린다.
# ─────────────────────────────────────────────────────────────

# 종가 요약 창(market_hours 의 brief=True)별 제목과 셀 종목. 보유가 어느 요약에서도 빠지지 않게
# 한 번씩만 들어간다. 크립토는 전용 종가 창이 없어 US 요약에 붙인다 — 주간 리포트와 같은 묶음.
_BRIEF_SCOPE = {
    "KR": ("KR 종가 요약",        KR_SYMBOLS),
    "US": ("US/크립토 종가 요약", US_SYMBOLS + CRYPTO_SYMBOLS),   # US_SYMBOLS = KR·크립토가 아닌 전부(ETF 포함)
}


def _send_daily_brief(window, result: StopCheckResult) -> None:
    """종가 요약 — 텍스트만. 주간 리포트와 달리 차트를 붙이지 않는다.

    result 는 전 종목이다(안전망 stop_check). 창의 시장 종목만 골라 센다 — 09-15 에 stop_check 을
    전 종목으로 넓히면서 이 필터가 빠져 "KR 종가 요약" 에 미국 종목이 섞였다(2026-09-21 발견).
    기준일도 그 시장 종목끼리 다시 구한다. 전 종목 최빈값은 미국 거래일일 수 있다.
    """
    title, symbols = _BRIEF_SCOPE[window.market]
    scope      = set(symbols)
    chandelier = [ch for ch in result.chandelier if ch.symbol in scope]
    ohlcv_map  = {s: df for s, df in result.ohlcv_map.items() if s in scope}

    # 전체 수집률이 충분해도 한 시장이 전멸할 수 있다. 설정 자체가 빈 시장은 기존대로
    # 0종목 요약을 허용하고, 요청이 있는 시장의 수집/계산 0건만 완료를 막는다.
    collected = sum(1 for df in ohlcv_map.values() if not df.empty)
    if scope and (not collected or not chandelier):
        raise RuntimeError(f"종가 요약 데이터 부족 — 요청 {len(scope)}, 수집 {collected}, 계산 {len(chandelier)}")

    spike_count = 0
    try:
        summary = summarize_portfolio_atr(ohlcv_map, ATR_PERIOD)
        if not summary.empty and "Spike" in summary.columns:
            spike_count = int(summary["Spike"].astype(bool).sum())
    except Exception as exc:
        # 요약의 곁가지일 뿐이라 실패해도 본문은 보낸다
        logger.warning("스파이크 집계 실패 — 요약에서 생략 (%s)", type(exc).__name__)

    sent = tg.send_long_message(tg.fmt_daily_brief(
        title,
        _get_data_date(ohlcv_map),
        chandelier,
        spike_count   = spike_count,
        updated_count = sum(1 for s in result.updated_symbols if s in scope),
    ))
    if not sent:
        # 호출자가 창을 완료로 적지 않게 실패로 올린다(ATR-03) — 다음 실행이 다시 보낸다.
        raise RuntimeError("종가 요약 전송 실패")


def _run_window_extra(window, result) -> None:
    """창이 덧붙이는 것 — 종가 요약, 주간 리포트. 전 종목 stop_check 은 이미 돌았다."""
    if window.action == "weekly_report":
        (job_kr_daily_report if window.market == "KR" else job_us_daily_report)()
        return
    if window.brief:
        if result is None:
            # stop_check 이 실패해 요약할 데이터가 없다. 조용히 넘어가면 호출자가 창을 완료로 적어
            # 그날 요약이 영영 안 가고 창 안 수동 재시도도 "이미 완료" 로 거부된다 — 실패로 올린다.
            raise RuntimeError("전 종목 stop_check 실패로 요약 데이터 없음")
        _send_daily_brief(window, result)


@state_locked(_state_path)
def run_due_windows(now_utc=None) -> None:
    """전 종목 stop_check 을 항상 돌리고, 해당 창이 있으면 그 위에 얹는 것만 추가한다.

    2026-09-15 실측으로 고친 구조다. 창 안에서만 stop_check 을 돌렸더니 배달된 런 10건 중
    창(25~30분)에 들어간 건 1건뿐이었고, 월요일 하루 KR 창이 하나도 돌지 않았다. 배달률이
    7% 라 좁은 창을 요구하면 대부분의 날에 아무것도 안 돈다 — 트리거 알림을 창에 가두면
    안 된다. 배달된 런은 무조건 전 종목을 본다(교체 전과 같은 안전망).
    """
    from datetime import datetime, timezone

    now = now_utc if now_utc is not None else datetime.now(timezone.utc)

    # 안전망 — 창과 무관하게, 창이 몇 개 겹치든 한 번.
    result = None
    try:
        result = job_stop_check()
    except Exception as exc:
        # 손절 체크가 실패해도 요약·리포트 시도는 막지 않는다. 다만 조용히 넘기지는 않는다.
        _note_problem(f"전 종목 stop_check 실패: {type(exc).__name__}")

    for window in market_hours.due_windows(now):
        if window.action == "stop_check" and not window.brief:
            continue   # 안전망이 이미 덮는다

        local_date = window.local_date(now)
        if is_window_done(window.name, local_date):
            logger.info("창 건너뜀 (오늘 이미 완료): %s", window.name)
            continue

        logger.info("창 추가 작업: %s (%s / %s)", window.name, window.market, window.action)
        try:
            _run_window_extra(window, result)
        except Exception as exc:
            # 실패를 완료로 적으면 그날 그 창은 영영 안 간다 → 표시하지 않고 재시도에 맡긴다
            _note_problem(f"창 추가 작업 실패({window.name}) — 완료 표시 안 함: {type(exc).__name__}")
            continue

        mark_window_done(window.name, local_date)
        logger.info("창 완료: %s", window.name)


def run_github_actions_mode() -> None:
    """
    GitHub Actions 환경: 환경변수 GHA_JOB 으로 작업 선택 후 종료.
      GHA_JOB=stop_check        (기본값) — 전 종목 장중 체크
      GHA_JOB=crypto_stop_check          — 크립토 전용 체크 (주말)
      GHA_JOB=kr_daily_report            — 국내 일일 리포트 (KST 17:00)
      GHA_JOB=us_daily_report            — 미국+크립토 리포트 (KST 09:00)
      GHA_JOB=trigger_check              — 트리거 체크
    """
    _problems.clear()
    job_name = os.getenv("GHA_JOB", "stop_check")
    logger.info("GitHub Actions 모드 — 작업: %s", job_name)

    dispatch = {
        "stop_check":        lambda: job_stop_check(),
        "crypto_stop_check": lambda: job_stop_check(CRYPTO_SYMBOLS),
        "kr_daily_report":   job_kr_daily_report,
        "us_daily_report":   job_us_daily_report,
        "trigger_check":     job_trigger_check,
        "auto":              run_due_windows,
    }
    fn = dispatch.get(job_name)
    if fn is None:
        logger.error("알 수 없는 GHA_JOB: %s", job_name)
        sys.exit(1)

    if os.getenv("GITHUB_ACTIONS", "").lower() == "true":
        problem = _portfolio_problem()
        if problem:
            logger.error("포트폴리오 점검 실패: %s", problem)
            tg.send_message(f"⚠️ ATR 모니터 중단 — {problem}", parse_mode="")
            sys.exit(1)

    # A single local transaction covers download, decisions, notifications and
    # upload. Locking each file operation separately would still lose updates.
    try:
        with state_transaction(STATE_FILE):
            # pull 실패 시 빈 상태로 실행하지 않으며 push도 시도하지 않는다.
            try:
                state = drive_state.from_env(STATE_FILE)
                if state is not None:
                    loaded_state = state.pull()
                    log_masking.register_state_symbols_for_github_actions(loaded_state, KR_STOCK_NAMES)
            except drive_state.StateSyncError as e:
                logger.error("상태 파일 로드 실패: %s", e)
                tg.send_message(f"⚠️ ATR 모니터 중단 — 상태 파일 로드 실패: {e}")
                sys.exit(1)

            try:
                fn()
            except StateValidationError as error:
                _note_problem("상태 검증 실패: " + str(error))
            except Exception as error:
                # Third-party exceptions can contain private state or credential URLs.
                _note_problem("작업 실행 실패: " + type(error).__name__)
            finally:
                # 작업이 실패해도 그때까지 성공한 알림 이력은 같은 잠금 안에서 저장한다.
                if state is not None:
                    try:
                        state.push()
                    except drive_state.StateSyncError as e:
                        _note_problem("상태 파일 저장 실패 — 원격 반영 미확정: " + str(e))
    except StateLockError:
        # Acquisition failure must never enter pull/job/push or expose a path.
        logger.error("상태 잠금 실패 — 이번 실행을 중단합니다")
        tg.send_message("⚠️ ATR 모니터 중단 — 상태 잠금을 확보하지 못했습니다", parse_mode="")
        sys.exit(1)
    if _problems:
        # 상태는 위에서 이미 저장했다. 실패를 초록색으로 끝내지 않는다.
        lines = "\n".join(f"• {problem}" for problem in _problems[:10])
        tg.send_message(f"⚠️ ATR 모니터 — 이번 실행에 문제가 있었습니다\n{lines}", parse_mode="")
        logger.error("문제가 있었던 실행 — 종료코드 1 (%d건)", len(_problems))
        sys.exit(1)
    logger.info("GitHub Actions 완료")


# ─────────────────────────────────────────────────────────────
# 로컬 스케줄러 데몬
# ─────────────────────────────────────────────────────────────

def _execute_local_jobs(functions) -> int:
    """One invocation owns its problems; preserve successful earlier state writes."""
    _problems.clear()
    for function in functions:
        try:
            before = len(_problems)
            if function() is False and len(_problems) == before:
                _note_problem('필수 작업이 실패 상태를 반환했습니다')
        except Exception as error:
            _note_problem('로컬 작업 실패: ' + type(error).__name__)
    if _problems:
        logger.error('로컬 실행 실패 — 문제 %d건', len(_problems))
        return 1
    logger.info('로컬 실행 완료')
    return 0


def _scheduled_job(function):
    # Returning a failure code allows schedule to keep the original next run.
    return _execute_local_jobs([function])


def _job_chart(symbol):
    symbol = symbol.strip().upper()
    if not symbol or any(char.isspace() or ord(char) < 32 for char in symbol):
        raise ValueError('chart_symbol_invalid')
    frame = fetch_ohlcv(symbol)
    if frame is None or frame.empty or atr_input_issue(frame, ATR_PERIOD):
        _note_problem('차트 시세 입력을 확인할 수 없습니다')
        tg.send_message(f'{symbol} 데이터 조회 실패')
        return False
    record = load_stops().get(symbol)
    chart = plot_atr_chart(symbol, frame, registered_stop=record.current_stop if record else None, as_bytes=True)
    if not chart or not tg.send_photo(chart, caption=f'{symbol} ATR({ATR_PERIOD}일) 차트'):
        _note_problem('차트 전송을 확인할 수 없습니다')
        return False
    return True


def run_scheduler() -> None:
    # Preserve the existing local daily schedule; GHA uses dispatch windows.
    schedule.every().day.at(KR_REPORT_TIME).do(_scheduled_job, job_kr_daily_report)

    # 미국+크립토 일일 리포트 (KST 09:00, 매일)
    schedule.every().day.at(US_REPORT_TIME).do(_scheduled_job, job_us_daily_report)

    # 전 종목 Stop 체크 (30분 주기, 24h)
    schedule.every(30).minutes.do(_scheduled_job, job_stop_check)

    # 즉각 트리거 체크 (10분 주기, 24h)
    schedule.every(10).minutes.do(_scheduled_job, job_trigger_check)

    logger.info("스케줄러 시작 — Ctrl+C로 종료")
    if not tg.send_message(
        f"ATR 모니터링 시작\n"
        f"모니터링: {len(ALL_SYMBOLS)}종목 "
        f"(KR {len(KR_SYMBOLS)} / US {len(US_SYMBOLS)} / Crypto {len(CRYPTO_SYMBOLS)})\n"
        f"로컬 일일 리포트: KR 매일 {KR_REPORT_TIME} / US·Crypto 매일 {US_REPORT_TIME}\n"
        f"Stop 체크: 30분 주기 / 트리거: 10분 주기"
    ):
        _note_problem('스케줄러 시작 알림 전송 미확인')
    try:
        while True:
            try:
                schedule.run_pending()
            except Exception as error:
                _note_problem('예약 실행 오류: ' + type(error).__name__)
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("스케줄러 종료")
        if not tg.send_message("ATR 모니터링 종료"):
            _note_problem('스케줄러 종료 알림 전송 미확인')


# ─────────────────────────────────────────────────────────────
# CLI 진입점
# ─────────────────────────────────────────────────────────────

def main() -> int:
    from position_cli import maybe_run
    administration_result = maybe_run(sys.argv[1:])
    if administration_result is not None:
        if administration_result:
            raise SystemExit(administration_result)
        return 0

    if IS_GITHUB_ACTIONS:
        run_github_actions_mode()
        return 0

    parser = argparse.ArgumentParser(description="포트폴리오 ATR Trailing Stop 모니터",
        epilog='관리·복구 옵션: python position_cli.py --help (--state-scope, --replace, --recover-state)')
    group  = parser.add_mutually_exclusive_group()
    group.add_argument("--once",          action="store_true", help="즉시 1회 전체 리포트 (KR+US+Crypto)")
    group.add_argument("--stop-check",    action="store_true", help="즉시 1회 Stop 갱신 체크")
    group.add_argument("--trigger-check", action="store_true", help="즉시 1회 트리거 체크")
    group.add_argument("--kr-report",     action="store_true", help="즉시 국내 일일 리포트")
    group.add_argument("--us-report",     action="store_true", help="즉시 미국+크립토 일일 리포트")
    group.add_argument("--chart",         metavar="SYMBOL")
    group.add_argument("--add-pos",       nargs=3, metavar=("SYMBOL", "ENTRY", "STOP"))
    group.add_argument("--remove-pos",    metavar="SYMBOL")
    group.add_argument("--list-pos",      action="store_true")
    args = parser.parse_args()

    # Explicitly broken source settings must not start a local collection job.
    # Position administration remains available without loading a portfolio.
    if getattr(_config, "PORTFOLIO_ERROR", "") and not (args.add_pos or args.remove_pos or args.list_pos):
        logger.error("포트폴리오 점검 실패: %s", _portfolio_problem())
        raise SystemExit(1)

    if args.once:
        code = _execute_local_jobs([job_kr_daily_report, job_us_daily_report])
    elif args.stop_check:
        code = _execute_local_jobs([job_stop_check])
    elif args.trigger_check:
        code = _execute_local_jobs([job_trigger_check])
    elif args.kr_report:
        code = _execute_local_jobs([job_kr_daily_report])
    elif args.us_report:
        code = _execute_local_jobs([job_us_daily_report])
    elif args.chart:
        code = _execute_local_jobs([lambda: _job_chart(args.chart)])
    elif args.add_pos or args.remove_pos or args.list_pos:
        parser.error('관리 명령을 검증하지 못했습니다')
    else:
        run_scheduler()
        return 0
    if code:
        raise SystemExit(code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
