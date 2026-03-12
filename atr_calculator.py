"""
ATR (Average True Range) 계산 모듈

Wilder's Smoothing 방식 (표준 14일 ATR):
  TR_t  = max(High - Low, |High - Close_{t-1}|, |Low - Close_{t-1}|)
  ATR_1 = SMA(TR, period)           ← 첫 번째 ATR은 단순 평균
  ATR_t = (ATR_{t-1} × (period-1) + TR_t) / period

ATR% = ATR / Close × 100  → 자산 간 변동성 비교에 사용

Chandelier Exit (ATR Trailing Stop):
  Stop Level = Highest High(N) - ATR(14) × Multiple
  Stop은 상향만 허용 (Trailing 원칙)
  Multiple은 시장별 + ATR% 구간별 2-레이어 동적 결정
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import numpy as np

from config import (
    ATR_PERIOD,
    ATR_HISTORY_PERIOD,
    ATR_ALERT_MULTIPLIER,
    get_atr_multiple,
)


# ─────────────────────────────────────────────────────────────
# True Range
# ─────────────────────────────────────────────────────────────

def calc_true_range(df: pd.DataFrame) -> pd.Series:
    """
    데이터프레임(High, Low, Close 컬럼 필수)에서 True Range 시리즈 반환.
    첫 번째 행은 NaN (이전 종가 없음).
    """
    high  = df["High"]
    low   = df["Low"]
    prev_close = df["Close"].shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # None/object 오염 방지: 모든 값을 numeric으로 강제 변환
    tr = pd.to_numeric(tr, errors="coerce")
    tr.name = "TR"
    return tr


# ─────────────────────────────────────────────────────────────
# ATR (Wilder Smoothing)
# ─────────────────────────────────────────────────────────────

def calc_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """
    Wilder 스무딩 방식으로 ATR을 계산합니다.

    Parameters
    ----------
    df     : High, Low, Close 컬럼을 포함한 DataFrame
    period : ATR 기간 (기본값 14)

    Returns
    -------
    ATR 시리즈 (이름: "ATR")
    """
    tr  = calc_true_range(df)
    atr = tr.copy().astype(float)

    # 첫 ATR: 초기 period 개 TR의 단순 평균
    first_valid = tr.first_valid_index()
    if first_valid is None:
        return pd.Series(dtype=float, name="ATR")

    loc_start = tr.index.get_loc(first_valid)
    init_end  = loc_start + period

    if init_end > len(tr):
        return pd.Series(dtype=float, name="ATR")

    # period 이전은 NaN
    atr.iloc[: init_end] = np.nan
    atr.iloc[init_end - 1] = tr.iloc[loc_start:init_end].mean()

    # Wilder 점화식
    for i in range(init_end, len(tr)):
        atr.iloc[i] = (atr.iloc[i - 1] * (period - 1) + tr.iloc[i]) / period

    atr.name = "ATR"
    return atr


# ─────────────────────────────────────────────────────────────
# ATR% (정규화 변동성)
# ─────────────────────────────────────────────────────────────

def calc_atr_pct(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """ATR을 종가로 나눈 백분율 (자산 간 비교용)."""
    atr  = calc_atr(df, period)
    pct  = (atr / df["Close"]) * 100
    pct.name = "ATR%"
    return pct


# ─────────────────────────────────────────────────────────────
# 알림 조건 평가
# ─────────────────────────────────────────────────────────────

def is_atr_spike(
    atr_series: pd.Series,
    history_period: int    = ATR_HISTORY_PERIOD,
    multiplier: float      = ATR_ALERT_MULTIPLIER,
) -> bool:
    """
    최신 ATR이 최근 history_period일 평균 ATR의 multiplier 배를 초과하면 True.

    Parameters
    ----------
    atr_series    : calc_atr() 결과 시리즈
    history_period: 평균 계산에 사용할 과거 기간 수
    multiplier    : 스파이크 판단 배수 (기본 1.5)
    """
    valid = atr_series.dropna()
    if len(valid) < history_period + 1:
        return False

    current_atr  = valid.iloc[-1]
    avg_atr      = valid.iloc[-(history_period + 1):-1].mean()
    return current_atr > avg_atr * multiplier


# ─────────────────────────────────────────────────────────────
# Chandelier Exit (ATR Trailing Stop)
# ─────────────────────────────────────────────────────────────

@dataclass
class ChandelierResult:
    symbol:          str
    highest_high:    float   # 관찰 기간 최고가
    atr:             float   # 현재 ATR(14) 절대값
    atr_pct:         float   # ATR%
    multiple:        float   # 적용된 ATR 배수 (시장별 + ATR% 구간 보정)
    stop_level:      float   # Chandelier Stop = Highest High - ATR × Multiple
    current_close:   float   # 현재 종가
    dist_to_stop_pct: float  # 현재가-Stop 거리 (%)
    market:          str     # 시장 유형 (KR/US/Crypto/ETF)
    ema_21:          float   = float("nan")  # 21일 EMA (Stop 이탈 시 대체 기준선)

    @property
    def is_breached(self) -> bool:
        """Stop이 현재가보다 높음 (Chandelier Exit 이미 이탈 상태)."""
        return self.stop_level > self.current_close

    @property
    def is_near_stop(self) -> bool:
        """현재가가 Stop의 5% 이내이거나 이미 이탈한 경우 True."""
        return self.is_breached or (0 < self.dist_to_stop_pct <= 5.0)

    def summary_line(self) -> str:
        near = " [STOP NEAR!]" if self.is_near_stop else ""
        return (
            f"{self.symbol:<14} HH={self.highest_high:,.2f}  "
            f"ATR={self.atr:.2f}({self.atr_pct:.1f}%)  "
            f"x{self.multiple}  Stop={self.stop_level:,.2f}  "
            f"Dist={self.dist_to_stop_pct:.1f}%{near}"
        )


def calc_chandelier_stop(
    symbol: str,
    df: pd.DataFrame,
    period: int = ATR_PERIOD,
    hh_window: int = 20,
) -> ChandelierResult | None:
    """
    Chandelier Exit Stop Level을 계산합니다.

    Stop Level = Highest High(hh_window) - ATR(period) × Multiple
    Multiple은 시장 유형과 ATR%에 따라 자동 결정됩니다.

    Parameters
    ----------
    symbol    : 종목 코드 (시장 분류 및 배수 결정에 사용)
    df        : High, Low, Close 컬럼 포함 DataFrame
    period    : ATR 계산 기간 (기본 14)
    hh_window : Highest High 관찰 기간 (기본 20일)

    Returns
    -------
    ChandelierResult 또는 None (데이터 부족 시)
    """
    if df.empty or len(df) < max(period, hh_window) + 1:
        return None

    atr_series  = calc_atr(df, period)
    atr_pct_ser = calc_atr_pct(df, period)

    valid_atr = atr_series.dropna()
    if valid_atr.empty:
        return None

    current_atr   = float(valid_atr.iloc[-1])
    current_pct   = float(atr_pct_ser.dropna().iloc[-1]) if not atr_pct_ser.dropna().empty else 0.0
    current_close = float(df["Close"].iloc[-1])
    highest_high  = float(df["High"].iloc[-hh_window:].max())

    # 21일 EMA (Stop 이탈 종목의 대체 기준선)
    ema_21 = float(df["Close"].ewm(span=21, adjust=False).mean().iloc[-1])

    # 시장별 + ATR% 구간 보정 배수
    multiple   = get_atr_multiple(symbol, current_pct)
    stop_level = highest_high - current_atr * multiple
    dist_pct   = (current_close - stop_level) / current_close * 100

    from config import get_market_type
    return ChandelierResult(
        symbol           = symbol,
        highest_high     = round(highest_high,  4),
        atr              = round(current_atr,   4),
        atr_pct          = round(current_pct,   2),
        multiple         = multiple,
        stop_level       = round(stop_level,    4),
        current_close    = round(current_close, 4),
        dist_to_stop_pct = round(dist_pct,      2),
        market           = get_market_type(symbol),
        ema_21           = round(ema_21,         4),
    )


def should_update_stop(new_stop: float, current_stop: float) -> bool:
    """
    Trailing Stop 갱신 여부 판단.
    Stop은 상향만 허용 (하향 불가 — Trailing 원칙).
    """
    return new_stop > current_stop


# ─────────────────────────────────────────────────────────────
# 즉각 갱신 트리거 감지
# ─────────────────────────────────────────────────────────────

@dataclass
class TriggerResult:
    symbol:   str
    triggers: list[str]

    @property
    def has_trigger(self) -> bool:
        return len(self.triggers) > 0


def check_immediate_triggers(
    symbol: str,
    df: pd.DataFrame,
    current_stop: float | None = None,
) -> TriggerResult:
    """
    즉각 대응이 필요한 6가지 트리거 조건을 감지합니다.

    트리거 조건:
      1. 당일 +5% 이상 급등   → Highest High 대폭 갱신
      2. 거래량 20일 평균 300% → 변동성 급증 전조
      3. 갭 상승 +3% 이상     → ATR 급등 선행 신호
      4. 현재가 Stop 5% 이내  → 손절 직전
      5. 당일 -5% 이상 급락   → 손실 확대 경보
      6. 갭 하락 -3% 이상     → Stop 즉시 이탈 위험

    Parameters
    ----------
    symbol       : 종목 코드
    df           : OHLCV DataFrame
    current_stop : 현재 설정된 Stop Level (None이면 트리거 4 생략)
    """
    triggers: list[str] = []

    if len(df) < 21:
        return TriggerResult(symbol=symbol, triggers=triggers)

    latest = df.iloc[-1]
    prev   = df.iloc[-2]

    # 최신 데이터 freshness 체크
    # 오늘 날짜 데이터가 아니면 어제 이벤트가 재감지되는 false trigger 방지
    # (미국 종목 프리마켓 시간, KR 장전 등에서 어제 종가로 트리거 재발생 차단)
    from datetime import date as _date
    try:
        latest_date = (latest.name.date()
                       if isinstance(latest.name, pd.Timestamp)
                       else pd.Timestamp(latest.name).date())
        if latest_date < _date.today():
            return TriggerResult(symbol=symbol, triggers=triggers)
    except Exception:
        pass  # 날짜 파싱 실패 시 체크 생략

    # 트리거 1: 당일 급등 +5%
    daily_chg = (latest["Close"] - prev["Close"]) / prev["Close"] * 100
    if daily_chg >= 5.0:
        triggers.append(f"SURGE +{daily_chg:.1f}% (Highest High 갱신)")

    # 트리거 2: 거래량 급증 300%
    if "Volume" in df.columns and prev["Volume"] > 0:
        avg_vol = df["Volume"].iloc[-21:-1].mean()
        vol_ratio = latest["Volume"] / avg_vol * 100 if avg_vol > 0 else 0
        if vol_ratio >= 300:
            triggers.append(f"VOLUME x{vol_ratio/100:.1f} (평균 대비 {vol_ratio:.0f}%)")

    # 트리거 3: 갭 상승 +3%
    gap_pct = (latest["Open"] - prev["Close"]) / prev["Close"] * 100
    if gap_pct >= 3.0:
        triggers.append(f"GAP UP +{gap_pct:.1f}%")

    # 트리거 4: 현재가 Stop 5% 이내
    if current_stop is not None and current_stop > 0:
        dist = (latest["Close"] - current_stop) / latest["Close"] * 100
        if 0 < dist <= 5.0:
            triggers.append(f"STOP NEAR {dist:.1f}% (Stop={current_stop:,.2f})")

    # 트리거 5: 당일 급락 -5%
    if daily_chg <= -5.0:
        triggers.append(f"SURGE DOWN {daily_chg:.1f}% (손실 확대 경보)")

    # 트리거 6: 갭 하락 -3%
    if gap_pct <= -3.0:
        triggers.append(f"GAP DOWN {gap_pct:.1f}% (Stop 즉시 이탈 위험)")

    return TriggerResult(symbol=symbol, triggers=triggers)


# ─────────────────────────────────────────────────────────────
# 포트폴리오 전체 ATR 요약
# ─────────────────────────────────────────────────────────────

def summarize_portfolio_atr(
    ohlcv_map: dict[str, pd.DataFrame],
    period: int = ATR_PERIOD,
) -> pd.DataFrame:
    """
    {symbol: OHLCV_df} 딕셔너리를 받아 포트폴리오 ATR 요약 DataFrame 반환.

    Returns
    -------
    columns: Symbol, Market, Close, ATR, ATR%, ATR_Avg20, Spike,
             Multiple, HighestHigh, StopLevel, DistToStop%
    """
    import logging as _log
    _logger = _log.getLogger(__name__)

    rows: list[dict] = []
    for symbol, df in ohlcv_map.items():
        if df.empty or len(df) < period + 1:
            continue

        try:
            atr_series  = calc_atr(df, period)
            atr_pct_ser = calc_atr_pct(df, period)

            latest_close   = df["Close"].iloc[-1]
            latest_atr     = atr_series.dropna().iloc[-1]  if not atr_series.dropna().empty  else float("nan")
            latest_atr_pct = atr_pct_ser.dropna().iloc[-1] if not atr_pct_ser.dropna().empty else float("nan")

            valid_atr  = atr_series.dropna()
            atr_avg20  = valid_atr.iloc[-ATR_HISTORY_PERIOD:].mean() if len(valid_atr) >= ATR_HISTORY_PERIOD else float("nan")
            spike_flag = is_atr_spike(atr_series)

            # Chandelier Exit
            chandelier = calc_chandelier_stop(symbol, df, period)
            multiple    = chandelier.multiple       if chandelier else float("nan")
            hh          = chandelier.highest_high   if chandelier else float("nan")
            stop_level  = chandelier.stop_level     if chandelier else float("nan")
            dist_pct    = chandelier.dist_to_stop_pct if chandelier else float("nan")
            market      = chandelier.market         if chandelier else "?"

            rows.append(
                {
                    "Symbol":      symbol,
                    "Market":      market,
                    "Close":       round(latest_close,  4),
                    "ATR":         round(latest_atr,     4) if not pd.isna(latest_atr)     else float("nan"),
                    "ATR%":        round(latest_atr_pct, 2) if not pd.isna(latest_atr_pct) else float("nan"),
                    "ATR_Avg20":   round(atr_avg20,      4) if not pd.isna(atr_avg20)      else float("nan"),
                    "Spike":       spike_flag,
                    "Multiple":    multiple,
                    "HighestHigh": round(hh,             4) if not pd.isna(hh) else float("nan"),
                    "StopLevel":   round(stop_level,     4) if not pd.isna(stop_level) else float("nan"),
                    "DistToStop%": round(dist_pct,       2) if not pd.isna(dist_pct) else float("nan"),
                }
            )
        except Exception as exc:
            _logger.warning("ATR 계산 건너뜀 (%s): %s", symbol, exc)

    summary = pd.DataFrame(rows)
    if not summary.empty:
        summary.sort_values("ATR%", ascending=False, inplace=True)
        summary.reset_index(drop=True, inplace=True)
    return summary
