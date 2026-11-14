"""
텔레그램 봇 알림 모듈

Telegram Bot API를 requests로 직접 호출합니다.
(외부 SDK 의존성 없음)

지원 기능:
- 텍스트 메시지 전송 (HTML 서식, 파싱 실패 시 평문 재전송)
- PNG 이미지 전송 (HTML 캡션)
- 알림 메시지 포매팅

메시지 양식은 작업공간 공통 양식(2026-11-14)을 따른다:
첫 줄 `{이모지} <b>[ATR] 제목</b>`, 둘째 줄 한국 시각, 섹션 사이 빈 줄 하나, 구분선 없음.
가변 문자열은 전부 esc() 로 이스케이프한다.
"""
from __future__ import annotations

import html
import logging
import math
import os
import re
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

from config import (
    ATR_PERIOD,
    KR_STOCK_NAMES,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    fmt_price,
    fmt_symbol,
    SYMBOL_ACCOUNTS,
    SYMBOL_ENTRY_PRICES,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.telegram.org/bot{token}/{method}"

TIMEOUT_SEC = 20

MAX_MESSAGE_LEN = 4096   # sendMessage 상한 — 이스케이프 뒤 UTF-16 길이로 잰다
MAX_CAPTION_LEN = 1024   # sendPhoto 캡션 상한

SOURCE = "[ATR]"
KST = ZoneInfo("Asia/Seoul")
_WEEKDAYS = "월화수목금토일"


# ─────────────────────────────────────────────────────────────
# 공통 서식 헬퍼 — 이스케이프·한국 시각·이름
# ─────────────────────────────────────────────────────────────

def esc(value) -> str:
    """HTML 본문용 이스케이프 (& < >)."""
    return html.escape(str(value), quote=False)


def esc_attr(value) -> str:
    """HTML 속성값용 이스케이프 (& < > " ')."""
    return html.escape(str(value), quote=True)


def html_to_plain(text: str) -> str:
    """태그를 지우고 엔티티를 되돌린다 — 400 파싱 실패 시 평문 재전송용."""
    return html.unescape(re.sub(r"<[^>]*>", "", text))


def _now() -> datetime:
    """현재 한국 시각. GHA 서버는 UTC 라 시간대 없는 현재 시각을 쓰면 안 된다."""
    return datetime.now(KST)


def fmt_kst(dt: datetime | None = None) -> str:
    """`11/13(금) 16:28` — 한국 시각. naive 값은 UTC 로 보고 변환한다."""
    if dt is None:
        dt = _now()
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    dt = dt.astimezone(KST)
    return f"{dt:%m/%d}({_WEEKDAYS[dt.weekday()]}) {dt:%H:%M}"


def fmt_day(value) -> str:
    """시장 날짜(`2026-09-14` 문자열·date)를 `09/14(월)` 로. 올해가 아니면 연도를 붙인다.
    읽을 수 없는 값은 그대로(이스케이프해서) 돌려준다."""
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        try:
            value = date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return esc(value)
    day = f"{value:%m/%d}({_WEEKDAYS[value.weekday()]})"
    if value.year != _now().year:
        day = f"{value.year}/{day}"
    return day


def _name(symbol: str) -> str:
    """제목·목록용 종목 이름 — 등록된 국내 종목은 이름만, 나머지는 기존 표기."""
    return KR_STOCK_NAMES.get(symbol) or fmt_symbol(symbol)


def _code(symbol: str) -> str:
    """이름만 보여 준 국내 종목의 티커(둘째 줄 보조 정보). 그 외는 빈 문자열."""
    return symbol if symbol in KR_STOCK_NAMES else ""


def _accounts(symbol: str) -> str:
    return ", ".join(SYMBOL_ACCOUNTS.get(symbol, []))


def _header(emoji: str, title: str) -> str:
    return f"{emoji} <b>{SOURCE} {esc(title)}</b>"


def _subline(*extras: str) -> str:
    """둘째 줄: 한국 시각 + 보조 정보(빈 값은 뺀다)."""
    return " · ".join([fmt_kst(), *[esc(e) for e in extras if e]])


def _symbol_subline(symbol: str) -> str:
    return _subline(_code(symbol), _accounts(symbol))


def _entry_info_line(symbol: str, current_close: float, atr: float) -> str:
    """
    진입가 대비 수익률·ATR R배수 한 줄 반환. 진입가 미등록 시 빈 문자열.

    R배수 = (현재가 - 진입가) / ATR  → 몇 ATR만큼 이익/손실인지
    R ≥ 2 → 적극적 손절선 관리 / R ≥ 1 → 본전 손절 고려 / R < 0 → 손실 포지션

    예: 진입 $185.00 · +5.6% (+2.3ATR) → 본전 손절 고려
    """
    entry = SYMBOL_ENTRY_PRICES.get(symbol)
    if not entry or entry <= 0:
        return ""
    ret_pct = (current_close - entry) / entry * 100
    r_val   = (current_close - entry) / atr if atr > 0 else 0.0
    sign    = "+" if ret_pct >= 0 else ""
    if r_val >= 2.0:
        hint = " → 적극적 손절선 관리"
    elif r_val >= 1.0:
        hint = " → 본전 손절 고려"
    elif r_val < 0:
        hint = " ⚠️ 손실 포지션"
    else:
        hint = ""
    return f"진입 {fmt_price(symbol, entry)} · {sign}{ret_pct:.1f}% ({sign}{r_val:.1f}ATR){hint}"


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

def _describe(exc: Exception) -> str:
    """예외를 로그에 남길 때 URL 을 빼고 남긴다 — requests 예외 문자열에는 봇 토큰이 든 URL 이 들어 있다."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return f"{type(exc).__name__}" + (f" HTTP {status}" if status else "")


SEND_ATTEMPTS = 3


def _confirmed(response) -> bool:
    """HTTP와 Telegram의 명시적 성공 응답을 함께 확인한다."""
    if response.status_code != 200:
        return False
    try:
        result = response.json()
    except (ValueError, TypeError):
        return False
    return isinstance(result, dict) and result.get("ok") is True


def _plain(text: str, parse_mode: str) -> str:
    if parse_mode == "HTML":
        return html_to_plain(text)
    return text.replace("*", "").replace("_", "").replace("`", "")


def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """
    텍스트 메시지를 전송합니다. 성공 여부(bool)를 돌려주며 **호출자는 반드시 확인해야 한다** —
    실패한 알림을 '보냄' 으로 기록하면 다음 실행이 중복이라며 건너뛴다.

    429(레이트리밋)·5xx·네트워크 오류는 최대 3회까지 다시 시도한다. 장 전체가 움직여 알림이
    몰리는 날이 곧 레이트리밋이 나기 쉬운 날이고, 알림이 가장 필요한 날이다.
    400 은 서식 파싱 오류로 보고 태그를 뺀 평문으로 한 번 더 보낸다. 링크 미리보기는 끈다.
    """
    if not _is_configured():
        return False

    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text,
               "link_preview_options": {"is_disabled": True}}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    for attempt in range(1, SEND_ATTEMPTS + 1):
        wait = 2 ** attempt
        try:
            resp = requests.post(_url("sendMessage"), json=payload, timeout=TIMEOUT_SEC)
            status = resp.status_code
            if status == 400 and "parse_mode" in payload:
                # 공개 저장소의 Actions 로그는 누구나 열람 가능 → 메시지 원문(계좌명·진입가 포함)은
                # 남기지 않고 길이만 기록한다 (2026-08-27 보안점검)
                logger.warning("서식 파싱 실패 (400) — plain text 재시도 (원문 %d자)", len(text))
                payload = {"chat_id": TELEGRAM_CHAT_ID, "text": _plain(text, parse_mode),
                           "link_preview_options": {"is_disabled": True}}
                continue
            if status == 429 or status >= 500:
                try:
                    candidate_wait = int(resp.json().get("parameters", {}).get("retry_after", wait))
                    if candidate_wait < 0:
                        raise ValueError()
                    wait = candidate_wait
                except (ValueError, AttributeError, TypeError, OverflowError):
                    pass
                logger.warning("텔레그램 일시 오류 HTTP %s — %d초 뒤 재시도 (%d/%d)",
                               status, wait, attempt, SEND_ATTEMPTS)
                time.sleep(min(wait, 30))
                continue
            resp.raise_for_status()
            if not _confirmed(resp):
                logger.error("텔레그램 메시지 수신 미확인: telegram_response_unconfirmed")
                return False
            logger.info("텔레그램 메시지 전송 성공")
            return True
        except requests.RequestException as exc:
            logger.warning("텔레그램 전송 오류 %s — 재시도 (%d/%d)", _describe(exc), attempt, SEND_ATTEMPTS)
            time.sleep(min(wait, 30))
    logger.error("텔레그램 메시지 전송 실패 — %d회 시도", SEND_ATTEMPTS)
    return False


def _tg_len(text: str) -> int:
    """텔레그램은 길이를 UTF-16 코드 단위로 센다(이모지 = 2)."""
    return len(text.encode("utf-16-le")) // 2


def _caption_payload(caption: str) -> dict:
    """캡션은 HTML. 이스케이프 뒤 1,024자를 넘으면 태그가 잘리지 않게 평문으로 줄여 보낸다."""
    if _tg_len(caption) <= MAX_CAPTION_LEN:
        return {"caption": caption, "parse_mode": "HTML"}
    plain = html_to_plain(caption)
    while _tg_len(plain) > MAX_CAPTION_LEN:
        plain = plain[:-1]
    return {"caption": plain}


def send_photo(image_bytes: bytes, caption: str = "") -> bool:
    """
    PNG 이미지 바이트를 전송합니다.

    Parameters
    ----------
    image_bytes : PNG 이미지 바이트 (visualizer 모듈 반환값)
    caption     : 이미지 하단 캡션 (선택, HTML)

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
        payload.update(_caption_payload(caption))

    for attempt in range(3):
        try:
            resp = requests.post(
                _url("sendPhoto"),
                files={"photo": ("chart.png", image_bytes, "image/png")},
                data=payload,
                timeout=TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            logger.error("텔레그램 이미지 전송 실패 (네트워크): %s", _describe(exc))
            return False

        if resp.status_code == 400 and "parse_mode" in payload:
            logger.warning("캡션 서식 파싱 실패 (400) — 평문 캡션으로 재시도")
            payload = {"chat_id": TELEGRAM_CHAT_ID, "caption": html_to_plain(payload["caption"])}
            continue

        if resp.status_code == 429:
            # retry_after 파싱 후 최소 30초 대기 (Telegram 권장)
            try:
                retry_after = max(resp.json().get("parameters", {}).get("retry_after", 30), 30)
                if not math.isfinite(retry_after):
                    raise ValueError()
            except (ValueError, AttributeError, TypeError, OverflowError):
                logger.error("텔레그램 이미지 수신 미확인: telegram_response_unconfirmed")
                return False
            logger.warning("Rate limit (시도 %d/3) — %d초 대기", attempt + 1, retry_after)
            time.sleep(retry_after)
            continue   # 재시도

        try:
            resp.raise_for_status()
            if not _confirmed(resp):
                logger.error("텔레그램 이미지 수신 미확인: telegram_response_unconfirmed")
                return False
            logger.info("텔레그램 이미지 전송 성공")
            return True
        except requests.exceptions.HTTPError as exc:
            logger.error("텔레그램 이미지 전송 실패: %s", _describe(exc))
            return False

    logger.error("텔레그램 이미지 전송 실패: 재시도 3회 소진")
    return False


def _hard_wrap(line: str, limit: int) -> list[str]:
    """한 줄이 상한을 넘을 때만 글자 단위로 자른다(포매터가 만드는 줄에서는 일어나지 않는다)."""
    if _tg_len(line) <= limit:
        return [line]
    parts, cur = [], ""
    for char in line:
        if _tg_len(cur + char) > limit:
            parts.append(cur)
            cur = ""
        cur += char
    if cur:
        parts.append(cur)
    return parts


def split_message(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """상한(이스케이프 뒤 UTF-16 길이)을 넘으면 빈 줄(섹션) 경계에서, 안 되면 줄 경계에서 나눈다.

    포매터는 태그를 한 줄 안에서만 열고 닫으므로 줄 경계로 나누면 태그가 조각 사이에 걸치지 않는다.
    """
    if _tg_len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur: list[str] = []

    def emit(lines: list[str]) -> None:
        piece = "\n".join(lines).strip("\n")
        if piece:
            chunks.append(piece)

    for line in text.split("\n"):
        for seg in _hard_wrap(line, limit):
            if cur and _tg_len("\n".join(cur + [seg])) > limit:
                blanks = [i for i, value in enumerate(cur) if value == ""]
                cut = blanks[-1] if blanks and blanks[-1] > 0 else None
                if cut is not None:
                    emit(cur[:cut])
                    cur = cur[cut + 1:]
                else:
                    emit(cur)
                    cur = []
                if cur and _tg_len("\n".join(cur + [seg])) > limit:
                    emit(cur)
                    cur = []
            cur.append(seg)
    emit(cur)
    return chunks


def send_long_message(text: str, parse_mode: str = "HTML") -> bool:
    """
    4096자 초과 메시지를 섹션·줄 경계로 분할하여 순서대로 전송합니다.

    한 조각이라도 실패하면 False — 나머지 조각은 그대로 보낸다.
    """
    ok = True
    for piece in split_message(text):
        ok = send_message(piece, parse_mode) and ok
    return ok


# ─────────────────────────────────────────────────────────────
# 메시지 포매터
# ─────────────────────────────────────────────────────────────

def fmt_spike_alert(symbol: str, atr: float, atr_avg: float, atr_pct: float) -> str:
    """ATR 스파이크 단일 종목 알림 메시지 생성."""
    ratio = atr / atr_avg if atr_avg > 0 else 0
    return "\n".join([
        _header("🟡", f"변동성 급증 — {_name(symbol)}"),
        _symbol_subline(symbol),
        "",
        f"ATR(평균 변동폭): {fmt_price(symbol, atr)} · 평소 {fmt_price(symbol, atr_avg)}",
        f"배율: {ratio:.2f}배",
        f"ATR%: {atr_pct:.2f}%",
    ])


def _strip_title_emoji(title: str) -> str:
    """옛 호출자가 제목 앞에 붙인 이모지를 뗀다 — 상태 이모지는 포매터가 정한다."""
    return re.sub(r"^[^\w\[(]+", "", title).strip() or title


def fmt_daily_report(
    summary_df,
    title: str = "ATR 주간 리포트",
    data_date: str | None = None,
) -> str:
    """포트폴리오 ATR 현황 리포트 메시지 생성(주간 리포트 1부)."""
    spike_count = int(summary_df["Spike"].sum()) if "Spike" in summary_df.columns else 0

    lines = [
        _header("📊", _strip_title_emoji(title)),
        _subline(f"{len(summary_df)}종목", f"변동성 급증 {spike_count}"),
    ]
    if data_date and str(data_date)[:10] != _now().strftime("%Y-%m-%d"):
        lines.append(f"데이터 기준: {fmt_day(data_date)}")

    for _, row in summary_df.iterrows():
        sym        = row["Symbol"]
        spike_mark = "🟡" if row.get("Spike", False) else "🟢"
        accounts   = _accounts(sym)
        lines.append("")
        lines.append(f"{spike_mark} <b>{esc(_name(sym))}</b>" + (f" · {esc(accounts)}" if accounts else ""))
        lines.append(f"  종가 {fmt_price(sym, row['Close'])} · ATR {fmt_price(sym, row['ATR'])}"
                     f" · ATR% {row['ATR%']:.2f}%")
        entry_line = _entry_info_line(sym, row["Close"], row["ATR"])
        if entry_line:
            lines.append(f"  {esc(entry_line)}")

    lines.append("")
    lines.append(f"ℹ️ ATR = {ATR_PERIOD}일 평균 변동폭 · 🟡 = 평소보다 변동성 큼")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# Chandelier Exit / Stop 관련 메시지 포매터
# ─────────────────────────────────────────────────────────────

def fmt_stop_update(update_result) -> str:
    """Stop 갱신 알림 메시지 (stop_manager.UpdateResult 사용)."""
    sym = update_result.symbol
    new = fmt_price(sym, update_result.new_stop)
    return "\n".join([
        _header("🟢", f"손절선 상향 — {_name(sym)}"),
        _symbol_subline(sym),
        "",
        f"손절선: {fmt_price(sym, update_result.prev_stop)} → {new} ({update_result.change_pct:+.2f}%)",
        f"현재가: {fmt_price(sym, update_result.current_close)}",
        "",
        f"👉 증권사 앱 지정가를 {new}으로 갱신",
    ])


def fmt_stop_held(symbol: str, current_stop: float, new_calc: float) -> str:
    """Stop 유지 알림 (선택적 — 일반적으로 침묵)."""
    return "\n".join([
        _header("🔒", f"손절선 유지 — {_name(symbol)}"),
        _symbol_subline(symbol),
        "",
        f"현재 손절선: {fmt_price(symbol, current_stop)} (유지)",
        f"새 계산값: {fmt_price(symbol, new_calc)} (더 낮아 적용 안 함)",
    ])


def fmt_breakeven_alert(symbol: str, entry_price: float) -> str:
    """1차 목표 달성 → Breakeven 전환 알림."""
    return "\n".join([
        _header("✅", f"본전 손절 전환 — {_name(symbol)}"),
        _symbol_subline(symbol),
        "",
        f"손절선: 진입가 {fmt_price(symbol, entry_price)}로 상향",
        "ℹ️ 이제 손실 위험이 없는 포지션입니다.",
    ])


_NUM = r"([+-]?\d+(?:\.\d+)?)"
_TRIGGER_RULES = [
    (re.compile(r"^STOP MISMATCH .*?현재가의 (\d+(?:\.\d+)?)배"), "손절선 단위 불일치 · 현재가의 {0}배"),
    (re.compile(r"^STOP BREACH (\d+(?:\.\d+)?)%"),              "손절선 이탈 · {0}% 아래"),
    (re.compile(r"^STOP NEAR (\d+(?:\.\d+)?)%"),                "손절선 근접 {0}%"),
    (re.compile(r"^SURGE DOWN " + _NUM + "%"),                  "급락 {0}%"),
    (re.compile(r"^SURGE " + _NUM + "%"),                       "급등 {0}%"),
    (re.compile(r"^VOLUME x(\d+(?:\.\d+)?)"),                   "거래량 {0}배 · 20일 평균 대비"),
    (re.compile(r"^GAP UP " + _NUM + "%"),                      "갭 상승 {0}%"),
    (re.compile(r"^GAP DOWN " + _NUM + "%"),                    "갭 하락 {0}% · 손절선 이탈 위험"),
]


def trigger_label(trigger: str) -> str:
    """atr_calculator 의 영문 트리거 코드(`STOP NEAR 1.2% (Stop=…)`)를 한글 한 줄로.
    판정은 원문 문자열로 그대로 하고, 표시만 바꾼다. 모르는 코드는 원문을 보여 준다."""
    for pattern, template in _TRIGGER_RULES:
        match = pattern.match(trigger)
        if match:
            return template.format(*match.groups())
    return trigger


def fmt_trigger_alert(symbol: str, triggers: list[str], close: float, stop: float | None) -> str:
    """즉각 대응 트리거 감지 긴급 알림."""
    # 트리거 유형에 따라 행동 지침 결정 (두 단어 prefix로 상승/하락 구분)
    has_stop      = any(t.startswith("STOP") for t in triggers)
    has_down      = any(t.startswith("SURGE DOWN") or t.startswith("GAP DOWN") for t in triggers)
    has_up        = any(t.startswith("SURGE +") or t.startswith("GAP UP") for t in triggers)

    has_breach    = any(t.startswith("STOP BREACH") for t in triggers)
    has_mismatch  = any(t.startswith("STOP MISMATCH") for t in triggers)

    if has_mismatch:
        emoji, action = "🟡", "등록 손절선 점검 — 현재가와 단위가 맞지 않습니다(액면분할 등). 손절선 재등록 필요"
    elif has_breach:
        emoji, action = "🔴", "손절선 이탈 — 증권사 지정가 체결 여부 확인, 미체결이면 손절 실행"
    elif has_stop and has_down:
        emoji, action = "🔴", "즉각 손절 검토 — 손절선 근접에 추가 하락, 포지션 축소 우선"
    elif has_stop:
        emoji, action = "🔴", "매도 준비 — 손절선 도달 임박, 손절 기준 재확인"
    elif has_down:
        emoji, action = "🔴", "손실 관리 — 급락·갭 하락, 손절선 이탈 여부 즉시 확인"
    elif has_up:
        emoji, action = "🟢", "손절선 상향 검토 — 가격 상승에 맞춰 손절선 재계산"
    else:  # VOLUME 등
        emoji, action = "🟡", "ATR 확인 — 거래량 급증, 추가 변동성 주의"

    lines = [
        _header(emoji, f"즉각 대응 — {_name(symbol)}"),
        _symbol_subline(symbol),
        "",
        "<b>감지</b>",
        *[f"• {esc(trigger_label(t))}" for t in triggers],
        "",
        f"현재가: {fmt_price(symbol, close)}",
    ]
    if stop and stop > 0:
        dist_pct = abs(close - stop) / stop * 100
        lines.append(f"손절선: {fmt_price(symbol, stop)} · 거리 {dist_pct:.1f}%")
    lines += ["", f"👉 {action}"]
    return "\n".join(lines)


_MARKET_LABELS = {"US": "미국", "KR": "국내", "Crypto": "크립토", "ETF": "ETF"}


def fmt_chandelier_report(chandelier_results: list) -> str:
    """
    전체 포트폴리오 Chandelier Exit 현황 리포트(주간 리포트 2부).

    Parameters
    ----------
    chandelier_results : list[ChandelierResult]
    """
    near_count = sum(1 for r in chandelier_results if r.is_near_stop)

    lines = [
        _header("📊", "손절선 현황"),
        _subline(f"{len(chandelier_results)}종목", f"손절선 근접 {near_count}"),
    ]

    for r in sorted(chandelier_results, key=lambda x: x.dist_to_stop_pct):
        near_flag = "🔴" if r.is_near_stop else "🟢"
        head = f"{near_flag} <b>{esc(_name(r.symbol))}</b>"
        accounts = _accounts(r.symbol)
        if accounts:
            head += f" · {esc(accounts)}"
        head += f" · {esc(_MARKET_LABELS.get(r.market, r.market))}"
        if r.is_breached:
            gap = f"⚠️ 이탈 {abs(r.dist_to_stop_pct):.1f}%"
        else:
            gap = f"거리 {r.dist_to_stop_pct:.1f}%"
        lines.append("")
        lines.append(head)
        lines.append(f"  현재가 {fmt_price(r.symbol, r.current_close)} · 손절선 {fmt_price(r.symbol, r.stop_level)}")
        lines.append(f"  {gap} · ATR% {r.atr_pct:.1f}% · 배수 {r.multiple}")
        # Stop 이탈 종목: 21 EMA를 대체 기준선으로 표시
        if r.is_breached and r.ema_21 > 0:
            ema_dist = (r.current_close - r.ema_21) / r.current_close * 100
            ema_flag = "🔼" if r.current_close >= r.ema_21 else "🔽"
            lines.append(f"  {ema_flag} 21일 EMA {fmt_price(r.symbol, r.ema_21)} ({ema_dist:+.1f}%)")
        entry_line = _entry_info_line(r.symbol, r.current_close, r.atr)
        if entry_line:
            lines.append(f"  {esc(entry_line)}")

    lines.append("")
    lines.append("ℹ️ 거리 = 현재가에서 손절선까지 · 이탈 = 손절선이 현재가보다 위 · 배수 = 손절선 계산의 ATR 배수")
    return "\n".join(lines)


def fmt_chart_caption(symbol: str, note: str = "") -> str:
    """차트 사진 캡션 — `📷 <b>삼성전자</b> · 즉각 대응`."""
    caption = f"📷 <b>{esc(_name(symbol))}</b>"
    code = _code(symbol)
    parts = [p for p in (code, note) if p]
    if parts:
        caption += " · " + " · ".join(esc(p) for p in parts)
    return caption


# ─────────────────────────────────────────────────────────────
# 일일 종가 요약 (종가 창 전용)
#
# 주간 리포트는 그대로 둔다 — 바차트 + Chandelier 전 종목 나열 + 종목별 미니차트 N장.
# 매일 종가에는 텍스트만 보낸다. 이미지가 없으니 rate limit 대기(장당 4초)가 없고
# 실행이 빠르다. 이탈·근접 종목은 전부 나열한다(2026-09-21 소유자 결정 — "… 외 N개" 로
# 접으면 어느 종목을 손봐야 하는지 알 수 없다). 길어지면 send_long_message 가 나눠 보낸다.
# ─────────────────────────────────────────────────────────────

def _brief_names(results: list) -> list:
    """Stop 거리가 가까운 순으로 전 종목의 이름을 나열한다. 종목마다 한 줄."""
    ordered = sorted(results, key=lambda r: r.dist_to_stop_pct)
    return [f"  • {esc(_name(r.symbol))}" for r in ordered]


def fmt_daily_brief(
    title: str,
    data_date: str,
    chandelier_results: list,
    spike_count: int = 0,
    updated_count: int = 0,
) -> str:
    """종가 요약 — 이탈/근접/여유 개수, 이탈·근접은 이름까지 전부.

    is_near_stop 은 이탈을 포함하므로 그대로 세면 이탈 종목이 두 번 잡힌다. 셋을 서로소로 나눈다.
    """
    breached = [r for r in chandelier_results if r.is_breached]
    near     = [r for r in chandelier_results if r.is_near_stop and not r.is_breached]
    safe     = [r for r in chandelier_results if not r.is_near_stop]

    lines = [
        _header("📊", title),
        f"{fmt_kst()} · {fmt_day(data_date)} 기준 · 보유 {len(chandelier_results)}종목",
        "",
    ]
    if breached:
        lines.append(f"🔴 손절 이탈 {len(breached)}")
        lines.extend(_brief_names(breached))
    if near:
        lines.append(f"🟡 손절 근접 {len(near)}")
        lines.extend(_brief_names(near))
    lines.append(f"🟢 여유 {len(safe)}")

    tail = []
    if spike_count:
        tail.append(f"변동성 급증 {spike_count}")
    if updated_count:
        tail.append(f"손절선 갱신 {updated_count}")
    if tail:
        lines.append("")
        lines.append(" · ".join(tail))

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# 운영 알림 (실행 중단·문제 요약)
# ─────────────────────────────────────────────────────────────

def run_log_url(env=None) -> str:
    """GHA 실행 로그 주소. 값이 형식에 맞지 않거나 로컬 실행이면 빈 문자열."""
    env = os.environ if env is None else env
    server = env.get("GITHUB_SERVER_URL") or "https://github.com"
    repo   = env.get("GITHUB_REPOSITORY", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    if not (re.fullmatch(r"https://[A-Za-z0-9.-]+", server)
            and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            and re.fullmatch(r"[0-9]{1,30}", run_id)):
        return ""
    return f"{server}/{repo}/actions/runs/{run_id}"


_EXCEPTION_LABELS = {
    "ConnectionError": "연결 실패", "ConnectTimeout": "연결 시간 초과", "ReadTimeout": "응답 시간 초과",
    "Timeout": "시간 초과", "TimeoutError": "시간 초과", "HTTPError": "서버 응답 오류",
    "KeyError": "데이터 항목 누락", "ValueError": "값 이상", "TypeError": "데이터 형식 이상",
    "RuntimeError": "처리 중단", "StateSyncError": "Drive 동기화 실패",
    "StateValidationError": "상태 파일 검증 실패", "StateLockError": "상태 잠금 실패",
    "PermissionError": "권한 없음", "FileNotFoundError": "파일 없음", "OSError": "파일·시스템 오류",
}

_SYNC_LABELS = [
    ("changed remotely", "다른 실행이 먼저 바꿔 올리지 않음"),
    ("preflight", "올리기 전 확인 실패 — 올리지 않음"),
    ("remote result is unconfirmed", "올렸지만 반영 확인 실패"),
    ("requires a successful pull", "내려받기 성공 전이라 올리지 않음"),
    ("recovery is required", "상태 파일 복구가 먼저 필요"),
    ("Local state file could not be read", "로컬 상태 파일 읽기 실패"),
    ("download failed", "Drive 상태 파일 내려받기 실패"),
    ("upload failed", "Drive 상태 파일 올리기 실패"),
    ("authentication", "Drive 인증 설정 실패"),
    ("initialization failed", "Drive 연결 준비 실패"),
    ("upload preparation failed", "Drive 올리기 준비 실패"),
    ("verification failed", "Drive 반영 확인 실패"),
]


def explain_state_sync_error(message: str) -> str:
    """정제된 영문 동기화 오류를 한글 + 원문 코드 한 번으로. 한글 문장이면 그대로."""
    text = str(message)
    for needle, label in _SYNC_LABELS:
        if needle in text:
            return f"{label} <code>{esc(text)}</code>"
    return esc(text)


_TRAILING_EXCEPTION = re.compile(r"^(.*?):\s*([A-Z][A-Za-z0-9_]*)$")


def _problem_line(problem: str) -> str:
    """`작업 실행 실패: ValueError` → `작업 실행 실패 — 원인: 값 이상 <code>ValueError</code>`."""
    text = str(problem)
    match = _TRAILING_EXCEPTION.match(text)
    if match and re.search(r"(Error|Exception|Timeout|Interrupt|Exit)$|^[A-Z][a-z]+[A-Z]", match.group(2)):
        head, name = match.groups()
        label = _EXCEPTION_LABELS.get(name, "예상치 못한 오류")
        return f"{esc(head)} — 원인: {label} <code>{esc(name)}</code>"
    for marker in (": Drive ", ": Local ", ": State "):
        if marker in text:
            head, tail = text.split(": ", 1)
            return f"{esc(head)} — {explain_state_sync_error(tail)}"
    return esc(text)


def _log_link_line() -> list[str]:
    url = run_log_url()
    return [f'<a href="{esc_attr(url)}">실행 로그 보기</a>'] if url else []


def fmt_ops_alert(title: str, reason_html: str = "", *, emoji: str = "🔴", action: str = "") -> str:
    """운영 알림. reason_html 은 이미 이스케이프된 HTML 이어야 한다."""
    lines = [_header(emoji, title), fmt_kst()]
    if reason_html:
        lines += ["", reason_html]
    link = _log_link_line()
    if link or action:
        lines.append("")
        lines += link
        if action:
            lines.append(f"👉 {esc(action)}")
    return "\n".join(lines)


def fmt_run_problems(problems: list[str], limit: int = 10) -> str:
    """이번 실행의 문제 목록 요약."""
    shown = problems[:limit]
    body = "\n".join(f"• {_problem_line(p)}" for p in shown)
    if len(problems) > limit:
        body += f"\n• 그 밖에 {len(problems) - limit}건"
    return fmt_ops_alert(f"실행 중 문제 {len(problems)}건", body)
