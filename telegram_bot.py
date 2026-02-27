"""
텔레그램 봇 알림 모듈

Telegram Bot API를 requests로 직접 호출합니다.
(외부 SDK 의존성 없음)

지원 기능:
- 텍스트 메시지 전송 (Markdown 지원)
- PNG 이미지 전송
- 알림 메시지 포매팅
"""
from __future__ import annotations

import logging
from datetime import datetime

import requests

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    fmt_price,
    fmt_symbol,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.telegram.org/bot{token}/{method}"

TIMEOUT_SEC = 20


def _url(method: str) -> str:
    return BASE_URL.format(token=TELEGRAM_BOT_TOKEN, method=method)


def _is_configured() -> bool:
    """토큰과 채팅 ID가 설정되어 있는지 확인합니다."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("텔레그램 설정 미완료 — .env 파일에 BOT_TOKEN / CHAT_ID를 입력하세요.")
        return False
    return True


# ─────────────────────────────────────────────────────────────
# 핵심 전송 함수
# ─────────────────────────────────────────────────────────────

def send_message(text: str, parse_mode: str = "Markdown") -> bool:
    """
    텍스트 메시지를 전송합니다.

    Parameters
    ----------
    text       : 전송할 텍스트 (Markdown 또는 HTML)
    parse_mode : "Markdown" | "HTML" | "" (기본 Markdown)

    Returns
    -------
    성공 여부 (bool)
    """
    if not _is_configured():
        return False

    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": parse_mode,
    }
    try:
        resp = requests.post(_url("sendMessage"), json=payload, timeout=TIMEOUT_SEC)
        resp.raise_for_status()
        logger.info("텔레그램 메시지 전송 성공")
        return True
    except requests.RequestException as exc:
        logger.error("텔레그램 메시지 전송 실패: %s", exc)
        return False


def send_photo(image_bytes: bytes, caption: str = "") -> bool:
    """
    PNG 이미지 바이트를 전송합니다.

    Parameters
    ----------
    image_bytes : PNG 이미지 바이트 (visualizer 모듈 반환값)
    caption     : 이미지 하단 캡션 (선택)

    Returns
    -------
    성공 여부 (bool)
    """
    if not _is_configured():
        return False
    if not image_bytes:
        logger.warning("이미지 데이터 없음 — 전송 건너뜀")
        return False

    files_data = {"photo": ("chart.png", image_bytes, "image/png")}
    payload    = {"chat_id": TELEGRAM_CHAT_ID}
    if caption:
        payload["caption"] = caption[:1024]   # 텔레그램 캡션 최대 1024자

    try:
        resp = requests.post(
            _url("sendPhoto"),
            files=files_data,
            data=payload,
            timeout=TIMEOUT_SEC,
        )
        resp.raise_for_status()
        logger.info("텔레그램 이미지 전송 성공")
        return True
    except requests.RequestException as exc:
        logger.error("텔레그램 이미지 전송 실패: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────
# 메시지 포매터
# ─────────────────────────────────────────────────────────────

def fmt_spike_alert(symbol: str, atr: float, atr_avg: float, atr_pct: float) -> str:
    """ATR 스파이크 단일 종목 알림 메시지 생성."""
    now         = datetime.now().strftime("%Y-%m-%d %H:%M")
    ratio       = atr / atr_avg if atr_avg > 0 else 0
    sym_display = fmt_symbol(symbol)
    return (
        f"⚠️ *ATR 스파이크 알림*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"*{sym_display}*\n"
        f"ATR {fmt_price(symbol, atr)} | 평균 {fmt_price(symbol, atr_avg)} | 배율 {ratio:.2f}x | ATR% {atr_pct:.2f}%\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📅 {now}"
    )


def fmt_daily_report(summary_df, account_size: float) -> str:
    """일별 포트폴리오 ATR 현황 리포트 메시지 생성."""
    now         = datetime.now().strftime("%Y-%m-%d %H:%M")
    spike_count = int(summary_df["Spike"].sum()) if "Spike" in summary_df.columns else 0

    lines = [
        f"📈 *포트폴리오 ATR 일일 리포트*",
        f"━━━━━━━━━━━━━━━━",
        f"📅 {now}",
        f"💼 계좌 {account_size:,.0f}원 | 종목 {len(summary_df)}개 | 스파이크 {spike_count}개",
        f"━━━━━━━━━━━━━━━━",
    ]

    for _, row in summary_df.iterrows():
        sym         = row["Symbol"]
        spike_mark  = "🔴" if row.get("Spike", False) else "🟢"
        sym_display = fmt_symbol(sym)
        price_str   = fmt_price(sym, row["Close"])
        lines.append("")
        lines.append(
            f"{spike_mark} *{sym_display}*\n"
            f"   종가 {price_str} | ATR% {row['ATR%']:.2f}%"
        )

    return "\n".join(lines)


def fmt_position_report(position_results) -> str:
    """포지션 사이징 결과 메시지 생성."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"💰 *ATR 기반 포지션 사이징*",
        f"━━━━━━━━━━━━━━━━",
        f"📅 {now}",
        f"━━━━━━━━━━━━━━━━",
    ]
    for r in sorted(position_results, key=lambda x: x.atr_pct, reverse=True):
        sym_display = fmt_symbol(r.symbol)
        lines += [
            "",
            f"*{sym_display}* [{r.market}·x{r.multiple}]",
            f"   현재가 {fmt_price(r.symbol, r.close_price)} | ATR% {r.atr_pct:.2f}%",
            f"   수량 {int(r.quantity)}주 | 포지션 {r.position_value:,.0f}원",
            f"   🛑 손절 {fmt_price(r.symbol, r.stop_loss_price)}",
            f"   📍 1차 {fmt_price(r.symbol, r.target_1)} → {r.sell_1}주",
            f"   📍 2차 {fmt_price(r.symbol, r.target_2)} → {r.sell_2}주",
            f"   📍 3차 {fmt_price(r.symbol, r.target_3)} → {r.sell_3}주",
        ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# Chandelier Exit / Stop 관련 메시지 포매터
# ─────────────────────────────────────────────────────────────

def fmt_stop_update(update_result) -> str:
    """Stop 갱신 알림 메시지 (stop_manager.UpdateResult 사용)."""
    now         = datetime.now().strftime("%Y-%m-%d %H:%M")
    sym         = update_result.symbol
    chg         = update_result.change_pct
    sym_display = fmt_symbol(sym)
    return (
        f"🔺 *ATR Trailing Stop 갱신*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"*{sym_display}*\n"
        f"현재가 {fmt_price(sym, update_result.current_close)}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{fmt_price(sym, update_result.prev_stop)}  →  {fmt_price(sym, update_result.new_stop)}  (+{chg:.2f}% ↑)\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⚠️ 증권사 앱 지정가 갱신 필요!\n"
        f"📅 {now}"
    )


def fmt_stop_held(symbol: str, current_stop: float, new_calc: float) -> str:
    """Stop 유지 알림 (선택적 — 일반적으로 침묵)."""
    now         = datetime.now().strftime("%Y-%m-%d %H:%M")
    sym_display = fmt_symbol(symbol)
    return (
        f"🔒 *ATR Stop 유지* *{sym_display}*\n"
        f"현재 Stop {fmt_price(symbol, current_stop)} (유지)\n"
        f"신규 계산값 {fmt_price(symbol, new_calc)} (하향 — 적용 안 함)\n"
        f"📅 {now}"
    )


def fmt_breakeven_alert(symbol: str, entry_price: float) -> str:
    """1차 목표 달성 → Breakeven 전환 알림."""
    now         = datetime.now().strftime("%Y-%m-%d %H:%M")
    sym_display = fmt_symbol(symbol)
    return (
        f"🎯 *Breakeven 전환 완료*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"*{sym_display}*\n"
        f"Stop → 진입가 {fmt_price(symbol, entry_price)} 으로 상향\n"
        f"이제 리스크 없는 포지션입니다.\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📅 {now}"
    )


def fmt_trigger_alert(symbol: str, triggers: list[str], close: float, stop: float | None) -> str:
    """즉각 대응 트리거 감지 긴급 알림."""
    now         = datetime.now().strftime("%Y-%m-%d %H:%M")
    sym_display = fmt_symbol(symbol)
    tlist       = "\n".join(f"  • {t}" for t in triggers)
    if stop and stop > 0:
        dist_pct  = abs(close - stop) / stop * 100
        stop_line = f"현재가 {fmt_price(symbol, close)} | Stop {fmt_price(symbol, stop)} (거리 {dist_pct:.1f}%)\n"
    else:
        stop_line = f"현재가 {fmt_price(symbol, close)}\n"
    return (
        f"🚨 *즉각 대응 트리거 감지*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"*{sym_display}*\n"
        f"{stop_line}"
        f"━━━━━━━━━━━━━━━━\n"
        f"{tlist}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⚠️ ATR Stop 즉각 재점검 필요!\n"
        f"📅 {now}"
    )


def fmt_chandelier_report(chandelier_results: list, stop_records: dict) -> str:
    """
    전체 포트폴리오 Chandelier Exit 현황 리포트.

    Parameters
    ----------
    chandelier_results : list[ChandelierResult]
    stop_records       : dict[symbol, StopRecord]  (stop_manager.load_all() 결과)
    """
    now           = datetime.now().strftime("%Y-%m-%d %H:%M")
    near_count    = sum(1 for r in chandelier_results if r.is_near_stop)
    updated_count = sum(1 for r in chandelier_results
                        if r.symbol in stop_records
                        and r.stop_level > stop_records[r.symbol].current_stop)

    lines = [
        f"📊 *ATR Trailing Stop 현황*",
        f"━━━━━━━━━━━━━━━━",
        f"📅 {now}",
        f"모니터링 {len(chandelier_results)}종목 | Stop근접 {near_count}개 | 갱신대기 {updated_count}개",
        f"━━━━━━━━━━━━━━━━",
    ]

    for r in sorted(chandelier_results, key=lambda x: x.dist_to_stop_pct):
        rec         = stop_records.get(r.symbol)
        sym_display = fmt_symbol(r.symbol)
        reg_stop    = fmt_price(r.symbol, rec.current_stop) if rec else "미등록"
        near_flag   = "🔴" if r.is_near_stop else "🟢"
        stage_flag  = " [BE]" if rec and rec.stage >= 1 else ""
        lines.append("")
        lines.append(
            f"{near_flag} *{sym_display}* [{r.market}·x{r.multiple}]{stage_flag}\n"
            f"   Stop {fmt_price(r.symbol, r.stop_level)} | 등록 {reg_stop} | 거리 {r.dist_to_stop_pct:.1f}%"
        )

    return "\n".join(lines)
