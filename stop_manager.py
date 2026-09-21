"""
ATR Trailing Stop 영속성 관리 모듈

책임:
  - data/stop_levels.json 에 종목별 Stop Level 읽기/쓰기
  - New Stop > Current Stop 비교 (Trailing 원칙 — 상향만 허용)
  - 1차 목표 달성 시 Stop → Breakeven(진입가) 전환
  - 포지션 등록 / 조회 / 업데이트 / 제거

운용 흐름:
  1. 포지션 진입 시 : add_position(symbol, entry_price, initial_stop)
  2. 30분 주기 체크 : update_stop(symbol, new_chandelier_stop, current_close)
     - new > current → 갱신 + 알림 대상 표시
     - new ≤ current → 유지 (silent)
  3. 1차 목표 달성 시: trigger_breakeven(symbol) → Stop을 진입가로 상향
  4. 청산 시        : remove_position(symbol)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from market_dates import market_date, utc_now
from state_validation import (
    StateValidationError, read_state, state_locked, validate_number,
    validate_string_list, write_state,
)

logger = logging.getLogger(__name__)

DATA_FILE = Path(__file__).parent / "data" / "stop_levels.json"


# ─────────────────────────────────────────────────────────────
# 데이터 모델
# ─────────────────────────────────────────────────────────────

@dataclass
class StopRecord:
    symbol:        str
    entry_price:   float          # 진입가
    current_stop:  float          # 현재 ATR Trailing Stop
    highest_high:  float          # 마지막 계산 시 Highest High
    stage:         int   = 0      # 0=초기 / 1=Breakeven / 2=2차목표 달성
    last_updated:  str   = field(default_factory=lambda: _now())

    # ── 읽기 전용 computed 속성 ──────────────────────────────
    @property
    def is_breakeven(self) -> bool:
        return self.stage >= 1

    @property
    def stop_dist_pct(self) -> float:
        """진입가 대비 현재 Stop 거리 (%)."""
        if self.entry_price <= 0:
            return 0.0
        return round((self.entry_price - self.current_stop) / self.entry_price * 100, 2)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StopRecord":
        return cls(
            symbol       = d["symbol"],
            entry_price  = d["entry_price"],
            current_stop = d["current_stop"],
            highest_high = d["highest_high"],
            stage        = d.get("stage", 0),
            last_updated = d.get("last_updated", _now()),
        )


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ─────────────────────────────────────────────────────────────
# JSON 읽기/쓰기
# ─────────────────────────────────────────────────────────────

@state_locked
def _load_raw() -> dict:
    return read_state(DATA_FILE, missing_ok=True)


@state_locked
def _save_raw(raw: dict) -> None:
    # A malformed existing file needs explicit recovery, never silent reset.
    read_state(DATA_FILE, missing_ok=True)
    write_state(DATA_FILE, raw)


@state_locked
def load_all() -> dict[str, StopRecord]:
    """저장된 모든 포지션을 {symbol: StopRecord} 형태로 반환합니다."""
    raw = _load_raw()
    result: dict[str, StopRecord] = {}
    for sym, rec in raw.get("positions", {}).items():
        result[sym] = StopRecord.from_dict(rec)
    return result


@state_locked
def save_all(records: dict[str, StopRecord]) -> None:
    """모든 포지션을 JSON에 저장합니다."""
    raw = _load_raw()
    previous = raw.get("positions", {})
    raw["positions"] = {
        sym: {**previous.get(sym, {}), **rec.to_dict()} for sym, rec in records.items()
    }
    _save_raw(raw)
    logger.debug("stop_levels.json 저장 완료 (%d개)", len(records))


# ─────────────────────────────────────────────────────────────
# 포지션 CRUD
# ─────────────────────────────────────────────────────────────

@state_locked
def add_position(
    symbol:        str,
    entry_price:   float,
    initial_stop:  float,
    highest_high:  float | None = None,
) -> StopRecord:
    """
    새 포지션을 등록합니다. 이미 존재하면 덮어씁니다.

    Parameters
    ----------
    symbol       : 종목 코드
    entry_price  : 진입가
    initial_stop : 초기 ATR Trailing Stop
    highest_high : Chandelier 기준 최고가 (None이면 entry_price 사용)
    """
    records = load_all()
    rec = StopRecord(
        symbol       = symbol,
        entry_price  = entry_price,
        current_stop = initial_stop,
        highest_high = highest_high if highest_high is not None else entry_price,
    )
    records[symbol] = rec
    save_all(records)
    logger.info("포지션 등록: %s  진입가=%s  Stop=%s", symbol, entry_price, initial_stop)
    return rec


def get_position(symbol: str) -> Optional[StopRecord]:
    """단일 포지션 조회. 없으면 None 반환."""
    return load_all().get(symbol)


@state_locked
def remove_position(symbol: str) -> bool:
    """포지션 제거. 성공 시 True 반환."""
    records = load_all()
    if symbol not in records:
        logger.warning("제거 대상 없음: %s", symbol)
        return False
    del records[symbol]
    save_all(records)
    logger.info("포지션 제거: %s", symbol)
    return True


# ─────────────────────────────────────────────────────────────
# Stop 갱신 로직
# ─────────────────────────────────────────────────────────────

@dataclass
class UpdateResult:
    symbol:       str
    updated:      bool           # True면 Stop이 상향됨
    prev_stop:    float
    new_stop:     float
    current_close: float
    action:       str            # "UPDATED" | "HELD" | "NOT_FOUND"
    note:         str = ""

    @property
    def change_pct(self) -> float:
        if self.prev_stop <= 0:
            return 0.0
        return round((self.new_stop - self.prev_stop) / self.prev_stop * 100, 2)


@state_locked
def update_stop(
    symbol:        str,
    new_stop:      float,
    current_close: float,
    new_hh:        float | None = None,
    commit:        bool = True,
) -> UpdateResult:
    """
    Trailing Stop 갱신 여부를 판단하고 적용합니다.

    commit=False 면 판정만 하고 저장하지 않는다. 호출자는 '지정가 갱신 필요' 알림 전송이
    성공한 뒤 commit=True 로 다시 불러 저장한다 — 저장을 먼저 하면 전송 실패 시 다음 실행은
    new == current 라 그 알림이 영구히 사라진다(ATR-03).

    원칙: new_stop > current_stop 일 때만 갱신 (하향 절대 불가).

    Parameters
    ----------
    symbol        : 종목 코드
    new_stop      : 새로 계산된 Chandelier Stop
    current_close : 현재 종가 (알림 메시지용)
    new_hh        : 새 Highest High (None이면 갱신 안 함)

    Returns
    -------
    UpdateResult
    """
    validate_number(new_stop)
    validate_number(current_close)
    if new_hh is not None:
        validate_number(new_hh)
    records = load_all()
    rec     = records.get(symbol)

    if rec is None:
        return UpdateResult(
            symbol=symbol, updated=False,
            prev_stop=0, new_stop=new_stop,
            current_close=current_close, action="NOT_FOUND",
            note="포지션 미등록 — add_position() 먼저 호출 필요",
        )

    prev_stop = rec.current_stop
    if new_stop > prev_stop and not commit:
        return UpdateResult(
            symbol=symbol, updated=True,
            prev_stop=prev_stop, new_stop=round(new_stop, 4),
            current_close=current_close, action="PENDING",
        )
    if new_stop > prev_stop:
        rec.current_stop = round(new_stop, 4)
        rec.last_updated = _now()
        if new_hh is not None:
            rec.highest_high = round(new_hh, 4)
        records[symbol] = rec
        save_all(records)
        logger.info(
            "Stop 상향: %s  %s → %s (+%s%%)",
            symbol, prev_stop, rec.current_stop,
            round((rec.current_stop - prev_stop) / prev_stop * 100, 2),
        )
        return UpdateResult(
            symbol=symbol, updated=True,
            prev_stop=prev_stop, new_stop=rec.current_stop,
            current_close=current_close, action="UPDATED",
        )
    else:
        logger.debug("Stop 유지: %s  현재=%s  신규=%s (하향 거부)", symbol, prev_stop, new_stop)
        return UpdateResult(
            symbol=symbol, updated=False,
            prev_stop=prev_stop, new_stop=new_stop,
            current_close=current_close, action="HELD",
        )


@state_locked
def trigger_breakeven(symbol: str, entry_price: float | None = None) -> bool:
    """
    1차 목표 달성 시 Stop을 진입가(Breakeven)로 상향합니다.

    이미 Breakeven 상태이거나 포지션이 없으면 False 반환.
    """
    if entry_price is not None:
        validate_number(entry_price)
    records = load_all()
    rec     = records.get(symbol)

    if rec is None:
        logger.warning("Breakeven 전환 실패: %s 미등록", symbol)
        return False

    if rec.stage >= 1:
        logger.debug("이미 Breakeven 상태: %s", symbol)
        return False

    breakeven_price = entry_price if entry_price is not None else rec.entry_price
    if breakeven_price <= rec.current_stop:
        logger.debug("진입가가 Stop보다 낮음 — Breakeven 전환 불필요: %s", symbol)
        return False

    rec.current_stop = round(breakeven_price, 4)
    rec.stage        = 1
    rec.last_updated = _now()
    records[symbol]  = rec
    save_all(records)
    logger.info("Breakeven 전환: %s  Stop → 진입가 %s", symbol, breakeven_price)
    return True


@state_locked
def advance_stage(symbol: str) -> int:
    """2차 목표 달성 시 stage를 2로 진행합니다. 현재 stage 반환."""
    records = load_all()
    rec     = records.get(symbol)
    if rec is None:
        return -1
    rec.stage        = min(rec.stage + 1, 2)
    rec.last_updated = _now()
    records[symbol]  = rec
    save_all(records)
    return rec.stage


# ─────────────────────────────────────────────────────────────
# 트리거 알림 중복 방지 (alert_log)
#
# stop_levels.json 내 "alert_log" 섹션에 저장:
# {
#   "AAPL": {
#     "date":     "2026-02-27",
#     "sent_at":  "2026-02-27T07:15+00:00",  ← UTC 발송 타임스탬프 (기록용)
#     "triggers": ["SURGE +5.2%", "VOLUME 320%"],
#     "close":    185.43,
#     "stop":     160.0
#   }
# }
#
# 재발송 조건 (아래 중 하나라도 해당):
#   1) 발송 이력 없음, 또는 새 거래일 시작 (date 변경)
#   2) 트리거 종류 변경 (새 트리거 추가 / 기존 제거)
#   3) Stop 레벨이 0.5% 이상 변동
#   4) 현재가가 5% 이상 변동
#
# ※ 시간 기반 쿨다운(120분) 제거 — 동일 가격/조건이면 시장 마감 후 재발송 안 함
#   KR/US 장 마감 후에는 가격 변동 없으므로 조건 2~4가 충족되지 않아 자연 억제됨
#   시장 활성 여부 게이트는 monitor.py 의 _is_market_active_for_triggers() 가 담당
# ─────────────────────────────────────────────────────────────

_ALERT_DATE_BASIS = "market-v1"


@state_locked
def _load_alert_log() -> dict:
    """alert_log 섹션 로드."""
    raw = _load_raw()
    return raw.get("alert_log", {})


@state_locked
def _save_alert_log(log: dict) -> None:
    """alert_log 섹션 저장."""
    raw = _load_raw()
    raw["alert_log"] = log
    _save_raw(raw)


@state_locked
def should_send_trigger_alert(
    symbol:      str,
    new_triggers: list[str],
    new_close:   float,
    new_stop:    float | None,
    *,
    now_utc:     datetime | None = None,
) -> bool:
    """
    트리거 알림 발송 여부를 판단합니다.

    시간 기반 쿨다운 없이 콘텐츠 기반으로만 판단합니다.
    KR/US 장 마감 후 동일 가격/조건이면 재발송하지 않습니다.

    Returns
    -------
    True  → 발송 필요
    False → 동일 조건 발송됨 (스킵)
    """
    validate_string_list(new_triggers)
    validate_number(new_close)
    if new_stop is not None:
        validate_number(new_stop)
    now = utc_now(now_utc)
    log   = _load_alert_log()
    today = market_date(symbol, now_utc=now).isoformat()
    entry = log.get(symbol)

    # ① 발송 이력 없음
    if entry is None:
        return True

    # Legacy dates used the host calendar and may coincidentally equal today's
    # market date. Allow one successful re-notification before trusting the key.
    # Do not reinterpret the old naive timestamp or rewrite state while reading.
    if entry.get("date_basis") != _ALERT_DATE_BASIS:
        return True

    # ① 새 거래일 시작 → 이전 알람 로그 초기화 (날짜 변경 시 재발송 허용)
    if entry.get("date") != today:
        return True

    # ② 트리거 종류 변경
    # "SURGE DOWN" / "GAP DOWN" 은 "SURGE UP" / "GAP UP" 과 별개 타입으로 구분
    def _types(tlist: list[str]) -> set[str]:
        result = set()
        for t in tlist:
            parts = t.split()
            if len(parts) >= 2 and (parts[1] in ("DOWN", "UP") or parts[0] == "STOP"):
                # "STOP NEAR" → "STOP BREACH" 전환은 같은 날이어도 다시 알려야 한다.
                result.add(f"{parts[0]} {parts[1]}")   # e.g. "SURGE DOWN", "GAP UP", "STOP BREACH"
            else:
                result.add(parts[0])                   # e.g. "VOLUME", "STOP"
        return result

    if _types(new_triggers) != _types(entry.get("triggers", [])):
        return True

    # ③ Stop 레벨 0.5% 이상 변동
    prev_stop = entry.get("stop")
    if new_stop is not None and prev_stop is not None and prev_stop > 0:
        if abs(new_stop - prev_stop) / prev_stop > 0.005:
            return True

    # ④ 현재가 5% 이상 변동
    prev_close = entry.get("close", 0.0)
    if prev_close > 0 and abs(new_close - prev_close) / prev_close > 0.05:
        return True

    return False


@state_locked
def mark_trigger_sent(
    symbol:      str,
    triggers:    list[str],
    close:       float,
    stop:        float | None,
    *,
    now_utc:     datetime | None = None,
) -> None:
    """전송 성공 후 시장 날짜와 UTC 발송 시각을 기록한다.

    기존 date/sent_at은 읽을 때 재해석하지 않고 다음 성공 기록 때만 바꾼다.
    """
    now = utc_now(now_utc)
    log = _load_alert_log()
    log[symbol] = {
        "date":     market_date(symbol, now_utc=now).isoformat(),
        "date_basis": _ALERT_DATE_BASIS,
        "sent_at":  now.isoformat(timespec="minutes"),
        "triggers": triggers,
        "close":    close,
        "stop":     stop,
    }
    _save_alert_log(log)
    logger.debug("트리거 알림 기록: %s  %s", symbol, triggers)


# ─────────────────────────────────────────────────────────────
# 요약 출력
# ─────────────────────────────────────────────────────────────

def summary_text() -> str:
    """등록된 전체 포지션 현황을 텍스트 테이블로 반환합니다."""
    records = load_all()
    if not records:
        return "등록된 포지션 없음"

    lines = [
        f"{'심볼':<14} {'진입가':>10} {'현재Stop':>12} {'Stop거리':>8} {'Stage':>6} {'갱신일시'}",
        "-" * 68,
    ]
    for sym, rec in sorted(records.items()):
        stage_label = ["초기", "BEven", "2차"][rec.stage] if rec.stage <= 2 else str(rec.stage)
        lines.append(
            f"{sym:<14} {rec.entry_price:>10,.2f} {rec.current_stop:>12,.2f} "
            f"{rec.stop_dist_pct:>7.2f}% {stage_label:>6}  {rec.last_updated}"
        )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# 창(window) 실행 멱등성 (done_windows)
#
# GHA 트리거가 언제 몇 번 떨어질지 보장되지 않으므로(배달률 18%, 밀림 최대 4시간),
# "창 안에서 오늘 아직 안 했으면 한다" 를 성립시키려면 완료 기록이 필요하다.
# alert_log 와 같은 방식으로 같은 상태 파일에 얹는다 — Drive 왕복이 이미 그 파일 하나다.
#
# stop_levels.json 내 "done_windows" 섹션:
#   {"2026-09-14": ["kr_open", "crypto_2"], ...}
# 키는 창의 지역 날짜다. UTC 날짜를 쓰면 EST 금요일 애프터마켓이 토요일로 기록된다.
# ─────────────────────────────────────────────────────────────

DONE_WINDOW_RETENTION_DAYS = 7


@state_locked
def is_window_done(window_name: str, local_date) -> bool:
    """해당 창을 그 지역 날짜에 이미 실행했는지."""
    raw = _load_raw()
    return window_name in raw.get("done_windows", {}).get(local_date.isoformat(), [])


@state_locked
def mark_window_done(window_name: str, local_date) -> None:
    """창 실행 완료를 기록하고 오래된 날짜를 정리한다."""
    raw  = _load_raw()
    done = raw.get("done_windows", {})
    key  = local_date.isoformat()

    names = done.setdefault(key, [])
    if window_name not in names:
        names.append(window_name)

    # 무한 증식 방지 — 최근 며칠만 남긴다 (Drive 왕복 파일이 계속 커지지 않도록)
    if len(done) > DONE_WINDOW_RETENTION_DAYS:
        for stale in sorted(done)[: len(done) - DONE_WINDOW_RETENTION_DAYS]:
            del done[stale]

    raw["done_windows"] = done
    _save_raw(raw)
