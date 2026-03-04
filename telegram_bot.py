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
import time
from datetime import datetime

import requests

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    fmt_price,
    fmt_symbol,
    SYMBOL_ACCOUNTS,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.telegram.org/bot{token}/{method}"

TIMEOUT_SEC = 20


def _account_line(symbol: str) -> str:
    """알람용 계좌 한 줄 반환. 계좌 미등록 종목은 빈 문자열."""
    accounts = SYMBOL_ACCOUNTS.get(symbol, [])
    return f"대상: {', '.join(accounts)}\n" if accounts else ""


def _account_tag(symbol: str) -> str:
    """인라인 계좌 태그 반환. 예: ' [ISA계좌, 연금저축]'"""
    accounts = SYMBOL_ACCOUNTS.get(symbol, [])
    return f" [{', '.join(accounts)}]" if accounts else ""


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
    except requests.exceptions.HTTPError as exc:
        # 400 = Markdown 파싱 오류 → plain text로 재시도
        if exc.response is not None and exc.response.status_code == 400 and parse_mode:
            logger.warning(
                "Markdown 파싱 실패 (400) — plain text 재시도\n원문 앞 200자: %.200s", text
            )
            plain = text.replace("*", "").replace("_", "").replace("`", "")
            try:
                resp2 = requests.post(
                    _url("sendMessage"),
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": plain},
                    timeout=TIMEOUT_SEC,
                )
                resp2.raise_for_status()
                logger.info("텔레그램 메시지 전송 성공 (plain text 폴백)")
                return True
            except requests.RequestException as exc2:
                logger.error("텔레그램 메시지 전송 실패 (폴백): %s", exc2)
                return False
        logger.error("텔레그램 메시지 전송 실패: %s", exc)
        return False
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

    payload = {"chat_id": TELEGRAM_CHAT_ID}
    if caption:
        payload["caption"] = caption[:1024]   # 텔레그램 캡션 최대 1024자

    for attempt in range(3):
        try:
            resp = requests.post(
                _url("sendPhoto"),
                files={"photo": ("chart.png", image_bytes, "image/png")},
                data=payload,
                timeout=TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            logger.error("텔레그램 이미지 전송 실패 (네트워크): %s", exc)
            return False

        if resp.status_code == 429:
            # retry_after 파싱 후 최소 30초 대기 (Telegram 권장)
            retry_after = max(
                resp.json().get("parameters", {}).get("retry_after", 30),
                30,
            )
            logger.warning("Rate limit (시도 %d/3) — %d초 대기", attempt + 1, retry_after)
            time.sleep(retry_after)
            continue   # 재시도

        try:
            resp.raise_for_status()
            logger.info("텔레그램 이미지 전송 성공")
            return True
        except requests.exceptions.HTTPError as exc:
            logger.error("텔레그램 이미지 전송 실패: %s", exc)
            return False

    logger.error("텔레그램 이미지 전송 실패: Rate Limit — 재시도 3회 소진")
    return False


def send_long_message(text: str, parse_mode: str = "Markdown") -> None:
    """
    4096자 초과 메시지를 줄 단위로 분할하여 순서대로 전송합니다.

    Telegram sendMessage 최대 길이(4096자)를 초과하는 일일 리포트 등에 사용.
    """
    MAX = 2500   # 이모지 UTF-16 2-code-unit 계산 여유 (4096 - 이모지 오버헤드)
    if len(text) <= MAX:
        send_message(text, parse_mode)
        return

    lines     = text.split("\n")
    chunk: list[str] = []
    size      = 0

    for line in lines:
        line_len = len(line) + 1          # +1 for \n
        if size + line_len > MAX and chunk:
            send_message("\n".join(chunk), parse_mode)
            chunk = []
            size  = 0
        chunk.append(line)
        size += line_len

    if chunk:
        send_message("\n".join(chunk), parse_mode)


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
        f"{_account_line(symbol)}"
        f"ATR {fmt_price(symbol, atr)} | 평균 {fmt_price(symbol, atr_avg)} | 배율 {ratio:.2f}x | ATR% {atr_pct:.2f}%\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📅 {now}"
    )


def fmt_daily_report(summary_df, title: str = "📈 포트폴리오 ATR 일일 리포트") -> str:
    """일별 포트폴리오 ATR 현황 리포트 메시지 생성."""
    now         = datetime.now().strftime("%Y-%m-%d %H:%M")
    spike_count = int(summary_df["Spike"].sum()) if "Spike" in summary_df.columns else 0

    lines = [
        f"*{title}*",
        f"━━━━━━━━━━━━━━━━",
        f"📅 {now}",
        f"종목 {len(summary_df)}개 | 스파이크 {spike_count}개",
        f"━━━━━━━━━━━━━━━━",
    ]

    for _, row in summary_df.iterrows():
        sym         = row["Symbol"]
        spike_mark  = "🔴" if row.get("Spike", False) else "🟢"
        sym_display = fmt_symbol(sym)
        price_str   = fmt_price(sym, row["Close"])
        atr_str     = fmt_price(sym, row["ATR"])
        lines.append("")
        lines.append(
            f"{spike_mark} *{sym_display}*{_account_tag(sym)}\n"
            f"   종가 {price_str} | ATR {atr_str} | ATR% {row['ATR%']:.2f}%"
        )

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
        f"{_account_line(sym)}"
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
        f"{_account_line(symbol)}"
        f"{stop_line}"
        f"━━━━━━━━━━━━━━━━\n"
        f"{tlist}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⚠️ ATR Stop 즉각 재점검 필요!\n"
        f"📅 {now}"
    )


def fmt_chandelier_report(chandelier_results: list) -> str:
    """
    전체 포트폴리오 Chandelier Exit 현황 리포트.

    Parameters
    ----------
    chandelier_results : list[ChandelierResult]
    """
    now        = datetime.now().strftime("%Y-%m-%d %H:%M")
    near_count = sum(1 for r in chandelier_results if r.is_near_stop)

    lines = [
        f"📊 *ATR Trailing Stop 현황*",
        f"━━━━━━━━━━━━━━━━",
        f"📅 {now}",
        f"모니터링 {len(chandelier_results)}종목 | Stop근접 {near_count}개",
        f"━━━━━━━━━━━━━━━━",
    ]

    for r in sorted(chandelier_results, key=lambda x: x.dist_to_stop_pct):
        sym_display = fmt_symbol(r.symbol)
        near_flag   = "🔴" if r.is_near_stop else "🟢"
        line = (
            f"{near_flag} *{sym_display}*{_account_tag(r.symbol)} ({r.market}·x{r.multiple})\n"
            f"   현재가 {fmt_price(r.symbol, r.current_close)} | Stop {fmt_price(r.symbol, r.stop_level)} | Gap {r.dist_to_stop_pct:.1f}% | ATR% {r.atr_pct:.2f}%"
        )
        lines.append("")
        lines.append(line)

    return "\n".join(lines)
