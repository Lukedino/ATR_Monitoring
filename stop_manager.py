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

import json
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional

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

def _load_raw() -> dict:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        return {"positions": {}}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_raw(raw: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)


def load_all() -> dict[str, StopRecord]:
    """저장된 모든 포지션을 {symbol: StopRecord} 형태로 반환합니다."""
    raw = _load_raw()
    result: dict[str, StopRecord] = {}
    for sym, rec in raw.get("positions", {}).items():
        try:
            result[sym] = StopRecord.from_dict(rec)
        except (KeyError, TypeError) as e:
            logger.warning("레코드 파싱 실패 (%s): %s", sym, e)
    return result


def save_all(records: dict[str, StopRecord]) -> None:
    """모든 포지션을 JSON에 저장합니다."""
    raw = _load_raw()
    raw["positions"] = {sym: rec.to_dict() for sym, rec in records.items()}
    _save_raw(raw)
    logger.debug("stop_levels.json 저장 완료 (%d개)", len(records))


# ─────────────────────────────────────────────────────────────
# 포지션 CRUD
# ─────────────────────────────────────────────────────────────

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


def update_stop(
    symbol:        str,
    new_stop:      float,
    current_close: float,
    new_hh:        float | None = None,
) -> UpdateResult:
    """
    Trailing Stop 갱신 여부를 판단하고 적용합니다.

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


def trigger_breakeven(symbol: str, entry_price: float | None = None) -> bool:
    """
    1차 목표 달성 시 Stop을 진입가(Breakeven)로 상향합니다.

    이미 Breakeven 상태이거나 포지션이 없으면 False 반환.
    """
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
#     "sent_at":  "2026-02-27T07:15",   ← 마지막 발송 타임스탬프
#     "triggers": ["SURGE +5.2%", "VOLUME 320%"],
#     "close":    185.43,
#     "stop":     160.0
#   }
# }
#
# 재발송 조건 (아래 중 하나라도 해당):
#   1) 마지막 발송 후 120분 이상 경과 (또는 이력 없음)
#   2) 트리거 종류 변경 (새 트리거 추가 / 기존 제거)
#   3) Stop 레벨이 0.5% 이상 변동
#   4) 현재가가 5% 이상 변동
# ─────────────────────────────────────────────────────────────

_TRIGGER_COOLDOWN_MINUTES: int = 120   # 동일 조건 트리거 재발송 최소 간격 (분)

def _load_alert_log() -> dict:
    """alert_log 섹션 로드."""
    raw = _load_raw()
    return raw.get("alert_log", {})


def _save_alert_log(log: dict) -> None:
    """alert_log 섹션 저장."""
    raw = _load_raw()
    raw["alert_log"] = log
    _save_raw(raw)


def should_send_trigger_alert(
    symbol:      str,
    new_triggers: list[str],
    new_close:   float,
    new_stop:    float | None,
) -> bool:
    """
    트리거 알림 발송 여부를 판단합니다.

    Returns
    -------
    True  → 발송 필요
    False → 오늘 이미 동일 조건으로 발송됨 (스킵)
    """
    log   = _load_alert_log()
    today = datetime.now().strftime("%Y-%m-%d")
    entry = log.get(symbol)

    # ① 발송 이력 없음
    if entry is None:
        return True

    # ① 마지막 발송 후 쿨다운(120분) 경과 여부
    sent_at = entry.get("sent_at")
    if sent_at:
        try:
            elapsed_min = (datetime.now() - datetime.fromisoformat(sent_at)).total_seconds() / 60
            if elapsed_min >= _TRIGGER_COOLDOWN_MINUTES:
                return True   # 쿨다운 초과 → 재발송 허용
        except (ValueError, TypeError):
            # 파싱 실패 시 날짜 기준으로 fallback
            if entry.get("date") != today:
                return True
    else:
        # 구버전 alert_log (sent_at 없음) — 날짜 기준 유지
        if entry.get("date") != today:
            return True

    # ② 트리거 종류 변경 (쿨다운 내에도 즉각 재발송)
    # "SURGE DOWN" / "GAP DOWN" 은 "SURGE UP" / "GAP UP" 과 별개 타입으로 구분
    def _types(tlist: list[str]) -> set[str]:
        result = set()
        for t in tlist:
            parts = t.split()
            if len(parts) >= 2 and parts[1] in ("DOWN", "UP"):
                result.add(f"{parts[0]} {parts[1]}")   # e.g. "SURGE DOWN", "GAP UP"
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

    # ④ 현재가 5% 이상 변동 (2%는 장중 잦은 재발송 유발 → 5%로 상향)
    prev_close = entry.get("close", 0.0)
    if prev_close > 0 and abs(new_close - prev_close) / prev_close > 0.05:
        return True

    return False


def mark_trigger_sent(
    symbol:      str,
    triggers:    list[str],
    close:       float,
    stop:        float | None,
) -> None:
    """트리거 알림 발송 기록을 저장합니다."""
    log = _load_alert_log()
    now = datetime.now()
    log[symbol] = {
        "date":     now.strftime("%Y-%m-%d"),
        "sent_at":  now.isoformat(timespec="minutes"),   # 쿨다운 계산용 타임스탬프
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
