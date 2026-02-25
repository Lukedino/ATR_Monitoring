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
    """오늘 기준으로 시작일/종료일 문자열(YYYY-MM-DD) 반환."""
    end   = datetime.today()
    start = end - timedelta(days=lookback_days)
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

        return df[required].dropna()

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
