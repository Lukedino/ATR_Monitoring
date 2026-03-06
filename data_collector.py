"""
yfinance 기반 OHLCV 데이터 수집 모듈

주요 기능:
- 단일/복수 심볼 다운로드
- High, Low, Close 컬럼 정규화
- 국내 주식(.KS/.KQ), 해외 주식, 암호화폐, ETF 통합 처리
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

from config import LOOKBACK_DAYS

logger = logging.getLogger(__name__)


def _build_date_range(lookback_days: int) -> tuple[str, str]:
    """오늘 기준으로 시작일/종료일 문자열(YYYY-MM-DD) 반환.

    yfinance history(end=...) 는 end 날짜를 exclusive 처리하므로
    오늘 데이터를 포함하려면 end = today + 1일 이 필요합니다.
    """
    today = datetime.today()
    end   = today + timedelta(days=1)   # yfinance exclusive end → 오늘 포함
    start = today - timedelta(days=lookback_days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def fetch_ohlcv(symbol: str, lookback_days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    """
    단일 심볼의 OHLCV 데이터를 가져옵니다.

    Parameters
    ----------
    symbol       : yfinance 심볼 (예: "005930.KS", "AAPL", "BTC-USD")
    lookback_days: 조회 기간 (기본값은 config.LOOKBACK_DAYS)

    Returns
    -------
    columns: Date(index), Open, High, Low, Close, Volume
    실패 시 빈 DataFrame 반환
    """
    start, end = _build_date_range(lookback_days)
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(start=start, end=end, auto_adjust=True)

        if df.empty:
            logger.warning("데이터 없음: %s", symbol)
            return pd.DataFrame()

        # 컬럼명 정규화 (yfinance 버전에 따라 대소문자 다를 수 있음)
        df.columns = [c.strip().capitalize() for c in df.columns]
        df.index.name = "Date"

        # 필요한 컬럼만 유지
        required = ["Open", "High", "Low", "Close", "Volume"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            logger.warning("%s: 누락 컬럼 %s", symbol, missing)
            return pd.DataFrame()

        # Close 기준으로만 dropna — Volume NaN으로 인해 오늘 데이터가 삭제되는 것 방지
        df = df[required].dropna(subset=["Close"])

        # 데이터 최신성 경고 (1 거래일 이상 오래된 경우)
        if not df.empty:
            last_idx = df.index[-1]
            last_date = last_idx.date() if hasattr(last_idx, "date") else last_idx
            today_date = datetime.today().date()
            days_old = (today_date - last_date).days
            if days_old > 1:
                logger.warning(
                    "오래된 데이터 감지: %s → 마지막 날짜 %s (%d일 전) — Yahoo Finance 지연 가능성",
                    symbol, last_date, days_old,
                )

        return df

    except Exception as exc:
        logger.error("데이터 조회 실패 (%s): %s", symbol, exc)
        return pd.DataFrame()


def fetch_portfolio(
    symbols: list[str],
    lookback_days: int = LOOKBACK_DAYS,
) -> dict[str, pd.DataFrame]:
    """
    복수 심볼을 일괄 조회합니다.

    Returns
    -------
    {symbol: DataFrame}  — 실패한 심볼은 딕셔너리에서 제외
    """
    result: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        df = fetch_ohlcv(symbol, lookback_days)
        if not df.empty:
            result[symbol] = df
        else:
            logger.warning("건너뜀: %s", symbol)
    logger.info("데이터 수집 완료: %d / %d 종목", len(result), len(symbols))
    return result


def get_latest_price(symbol: str) -> float | None:
    """심볼의 최신 종가를 반환합니다."""
    df = fetch_ohlcv(symbol, lookback_days=5)
    if df.empty:
        return None
    return float(df["Close"].iloc[-1])


def enrich_kr_stock_names(symbols: list[str]) -> None:
    """
    KR_STOCK_NAMES에 없는 KR 종목을 yfinance로 조회하여 한글명을 추가합니다.
    앱 시작 시 한 번 호출하세요.
    """
    from config import KR_STOCK_NAMES
    for symbol in symbols:
        sym_upper = symbol.upper()
        if not (sym_upper.endswith(".KS") or sym_upper.endswith(".KQ")):
            continue
        if symbol in KR_STOCK_NAMES:
            continue
        try:
            info = yf.Ticker(symbol).info
            name = info.get("longName") or info.get("shortName")
            if name:
                KR_STOCK_NAMES[symbol] = name
                logger.info("종목명 조회: %s → %s", symbol, name)
        except Exception as exc:
            logger.debug("종목명 조회 실패 (%s): %s", symbol, exc)


def fetch_usd_krw() -> float:
    """
    USD/KRW 현재 환율을 조회합니다 (yfinance USDKRW=X).

    Returns
    -------
    환율 (float) — 조회 실패 시 기본값 1,350 반환
    """
    try:
        df = fetch_ohlcv("USDKRW=X", lookback_days=5)
        if not df.empty:
            rate = float(df["Close"].iloc[-1])
            logger.info("USD/KRW 환율: %.0f", rate)
            return rate
    except Exception as exc:
        logger.error("환율 조회 실패: %s", exc)
    logger.warning("환율 조회 실패 — 기본값 1,350 사용")
    return 1350.0
