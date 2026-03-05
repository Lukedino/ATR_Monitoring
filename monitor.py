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

GitHub Actions 환경:
  환경변수 GITHUB_ACTIONS=true 시 스케줄러 없이 단일 실행 후 종료
  환경변수 GHA_JOB 으로 실행할 작업 지정:
    stop_check        — 전 종목 Chandelier Stop 갱신 (평일 30분 주기)
    crypto_stop_check — 크립토 전용 Stop 갱신 (주말 30분 주기)
    kr_daily_report   — 국내 종목 일일 리포트 (KST 18:00, 평일)
    us_daily_report   — 미국+크립토 일일 리포트 (KST 08:00, 매일)
    trigger_check     — 즉각 대응 트리거 체크

스케줄 (GHA, KST 기준):
  평일 00:00~23:30 (30분 간격) — stop_check (전 종목)
  주말 00:00~23:30 (30분 간격) — crypto_stop_check (크립토)
  평일 18:00 — kr_daily_report
  매일 08:00 — us_daily_report (미국장 포스트마켓 종료 후)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import schedule

from config import (
    ALL_SYMBOLS,
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
    check_immediate_triggers,
)
from visualizer import plot_portfolio_atr_bar, plot_atr_chart
from stop_manager import (
    load_all as load_stops,
    update_stop,
    add_position,
    remove_position,
    summary_text as stop_summary_text,
    should_send_trigger_alert,
    mark_trigger_sent,
)
import telegram_bot as tg

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


# ─────────────────────────────────────────────────────────────
# 핵심 작업 함수
# ─────────────────────────────────────────────────────────────

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
    updated_any = False

    chandelier_list = []
    for symbol, df in ohlcv_map.items():
        if df.empty:
            continue

        ch = calc_chandelier_stop(symbol, df, ATR_PERIOD)
        if ch is None:
            continue
        chandelier_list.append(ch)

        # 즉각 트리거 체크
        rec          = stop_recs.get(symbol)
        current_stop = rec.current_stop if rec else None
        trigger      = check_immediate_triggers(symbol, df, current_stop)
        if trigger.has_trigger:
            close = float(df["Close"].iloc[-1])
            if should_send_trigger_alert(symbol, trigger.triggers, close, current_stop):
                tg.send_message(tg.fmt_trigger_alert(symbol, trigger.triggers, close, current_stop))
                chart = plot_atr_chart(symbol, df, registered_stop=current_stop, as_bytes=True)
                tg.send_photo(chart, caption=f"긴급: {fmt_symbol(symbol)} 트리거 감지")
                mark_trigger_sent(symbol, trigger.triggers, close, current_stop)
                logger.warning("트리거 감지 알림: %s — %s", symbol, trigger.triggers)
            else:
                logger.info("트리거 중복 스킵: %s (당일 동일 조건 발송됨)", symbol)

        # Stop 갱신 (등록된 포지션만)
        if rec is None:
            continue

        result = update_stop(
            symbol        = symbol,
            new_stop      = ch.stop_level,
            current_close = ch.current_close,
            new_hh        = ch.highest_high,
        )
        if result.updated:
            updated_any = True
            tg.send_message(tg.fmt_stop_update(result))
            chart = plot_atr_chart(symbol, df, registered_stop=result.new_stop, as_bytes=True)
            tg.send_photo(
                chart,
                caption=f"{symbol} Stop 갱신: {result.prev_stop:,.2f} -> {result.new_stop:,.2f}",
            )
            logger.info("Stop 갱신 알림 전송: %s", symbol)

    if not updated_any:
        logger.info("Stop 갱신 없음 (전 종목 유지)")

    logger.info("Stop 갱신 체크 완료")


def _run_daily_report(symbols: list[str], title: str) -> None:
    """
    시장별 일일 ATR 리포트 공통 로직.

    Parameters
    ----------
    symbols : 리포트 대상 심볼 리스트
    title   : 텔레그램 메시지 헤더 (이모지 포함)
    """
    logger.info("%s 시작", title)
    ohlcv_map = fetch_portfolio(symbols)
    if not ohlcv_map:
        tg.send_message(f"데이터 수집 실패 — {title}")
        return

    summary = summarize_portfolio_atr(ohlcv_map, ATR_PERIOD)
    if summary.empty:
        tg.send_message(f"ATR 계산 실패 — 데이터 부족 ({title})")
        return

    # 1. ATR 요약 텍스트
    tg.send_long_message(tg.fmt_daily_report(summary, title))

    # 2. 포트폴리오 바차트
    bar_chart = plot_portfolio_atr_bar(summary, as_bytes=True)
    tg.send_photo(bar_chart, caption="ATR% 비교")

    # 3. Chandelier Stop 전체 현황
    stop_recs       = load_stops()
    chandelier_list = []
    for symbol, df in ohlcv_map.items():
        ch = calc_chandelier_stop(symbol, df, ATR_PERIOD)
        if ch:
            chandelier_list.append(ch)
    if chandelier_list:
        tg.send_long_message(tg.fmt_chandelier_report(chandelier_list))

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
        chart = plot_atr_chart(
            symbol, df,
            registered_stop=rec.current_stop if rec else None,
            as_bytes=True,
        )
        if chart:
            tg.send_photo(chart, caption=fmt_symbol(symbol))
            time.sleep(4)   # Telegram rate limit 방지 (분당 15장 ≈ 안전 한도)

    logger.info("%s 완료", title)


def job_kr_daily_report() -> None:
    """[KST 18:30, 평일] 국내 종목 ATR 일일 리포트."""
    _run_daily_report(KR_SYMBOLS, "📈 국내 포트폴리오 ATR 일일 리포트")


def job_us_daily_report() -> None:
    """[KST 09:00, 매일] 미국+크립토 ATR 일일 리포트."""
    _run_daily_report(
        US_SYMBOLS + CRYPTO_SYMBOLS,
        "🌎 미국/크립토 포트폴리오 ATR 일일 리포트",
    )


def job_trigger_check() -> None:
    """[수시] 즉각 대응 트리거 빠른 체크 (Stop 갱신 없음)."""
    logger.info("트리거 체크 시작")
    ohlcv_map = fetch_portfolio(ALL_SYMBOLS)
    stop_recs = load_stops()
    found     = False

    for symbol, df in ohlcv_map.items():
        if df.empty or len(df) < 22:
            continue
        rec          = stop_recs.get(symbol)
        current_stop = rec.current_stop if rec else None
        trigger      = check_immediate_triggers(symbol, df, current_stop)
        if trigger.has_trigger:
            close = float(df["Close"].iloc[-1])
            if should_send_trigger_alert(symbol, trigger.triggers, close, current_stop):
                found = True
                tg.send_message(tg.fmt_trigger_alert(symbol, trigger.triggers, close, current_stop))
                chart = plot_atr_chart(symbol, df, registered_stop=current_stop, as_bytes=True)
                tg.send_photo(chart, caption=f"긴급: {fmt_symbol(symbol)}")
                mark_trigger_sent(symbol, trigger.triggers, close, current_stop)
            else:
                logger.info("트리거 중복 스킵: %s (당일 동일 조건 발송됨)", symbol)

    if not found:
        logger.info("트리거 없음")


# ─────────────────────────────────────────────────────────────
# GitHub Actions 단일 실행 모드
# ─────────────────────────────────────────────────────────────

def run_github_actions_mode() -> None:
    """
    GitHub Actions 환경: 환경변수 GHA_JOB 으로 작업 선택 후 종료.
      GHA_JOB=stop_check        (기본값) — 전 종목 장중 체크
      GHA_JOB=crypto_stop_check          — 크립토 전용 체크 (주말)
      GHA_JOB=kr_daily_report            — 국내 일일 리포트 (KST 17:00)
      GHA_JOB=us_daily_report            — 미국+크립토 리포트 (KST 09:00)
      GHA_JOB=trigger_check              — 트리거 체크
    """
    job_name = os.getenv("GHA_JOB", "stop_check")
    logger.info("GitHub Actions 모드 — 작업: %s", job_name)

    dispatch = {
        "stop_check":        lambda: job_stop_check(),
        "crypto_stop_check": lambda: job_stop_check(CRYPTO_SYMBOLS),
        "kr_daily_report":   job_kr_daily_report,
        "us_daily_report":   job_us_daily_report,
        "trigger_check":     job_trigger_check,
    }
    fn = dispatch.get(job_name)
    if fn is None:
        logger.error("알 수 없는 GHA_JOB: %s", job_name)
        sys.exit(1)

    fn()
    logger.info("GitHub Actions 완료")


# ─────────────────────────────────────────────────────────────
# 로컬 스케줄러 데몬
# ─────────────────────────────────────────────────────────────

def run_scheduler() -> None:
    # 국내 일일 리포트 (KST 17:00, 평일)
    schedule.every().day.at(KR_REPORT_TIME).do(job_kr_daily_report)

    # 미국+크립토 일일 리포트 (KST 09:00, 매일)
    schedule.every().day.at(US_REPORT_TIME).do(job_us_daily_report)

    # 전 종목 Stop 체크 (30분 주기, 24h)
    schedule.every(30).minutes.do(job_stop_check)

    # 즉각 트리거 체크 (10분 주기, 24h)
    schedule.every(10).minutes.do(job_trigger_check)

    logger.info("스케줄러 시작 — Ctrl+C로 종료")
    tg.send_message(
        f"ATR 모니터링 시작\n"
        f"모니터링: {len(ALL_SYMBOLS)}종목 "
        f"(KR {len(KR_SYMBOLS)} / US {len(US_SYMBOLS)} / Crypto {len(CRYPTO_SYMBOLS)})\n"
        f"국내 리포트: KST {KR_REPORT_TIME} / 미국 리포트: KST {US_REPORT_TIME}\n"
        f"Stop 체크: 30분 주기 / 트리거: 10분 주기"
    )
    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("스케줄러 종료")
        tg.send_message("ATR 모니터링 종료")


# ─────────────────────────────────────────────────────────────
# CLI 진입점
# ─────────────────────────────────────────────────────────────

def main() -> None:
    if IS_GITHUB_ACTIONS:
        run_github_actions_mode()
        return

    parser = argparse.ArgumentParser(description="포트폴리오 ATR Trailing Stop 모니터")
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

    if args.once:
        job_kr_daily_report()
        job_us_daily_report()
    elif args.stop_check:
        job_stop_check()
    elif args.trigger_check:
        job_trigger_check()
    elif args.kr_report:
        job_kr_daily_report()
    elif args.us_report:
        job_us_daily_report()
    elif args.chart:
        sym = args.chart.upper()
        df  = fetch_ohlcv(sym)
        if df.empty:
            tg.send_message(f"{sym} 데이터 조회 실패")
            return
        rec   = load_stops().get(sym)
        chart = plot_atr_chart(sym, df, registered_stop=rec.current_stop if rec else None, as_bytes=True)
        tg.send_photo(chart, caption=f"{sym} ATR({ATR_PERIOD}일) 차트")
    elif args.add_pos:
        sym, entry_s, stop_s = args.add_pos
        rec = add_position(sym.upper(), float(entry_s), float(stop_s))
        print(f"등록 완료: {rec.symbol}  진입가={rec.entry_price}  Stop={rec.current_stop}")
    elif args.remove_pos:
        ok = remove_position(args.remove_pos.upper())
        print("제거 완료" if ok else "포지션 없음")
    elif args.list_pos:
        print(stop_summary_text())
    else:
        run_scheduler()


if __name__ == "__main__":
    main()
