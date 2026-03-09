"""
yfinance 기반 OHLCV 데이터 수집 모듈

주요 기능:
- 단일/복수 심볼 다운로드
- High, Low, Close 컬럼 정규화
- 국내 주식(.KS/.KQ), 해외 주식, 암호화폐, ETF 통합 처리
- fast_info 실시간 보완: history() 지연 시 오늘 데이터 주입
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


def _strip_timezone(df: pd.DataFrame) -> pd.DataFrame:
    """
    yfinance가 반환하는 timezone-aware DatetimeIndex를 로컬 날짜 기준으로 변환합니다.

    .tz_convert(None)은 UTC로 변환하므로 KR/US 시장 날짜가 하루 밀릴 수 있습니다.
    strftime('%Y-%m-%d')로 로컬 날짜 문자열을 추출 후 재파싱하여 날짜 이동 없이 tz 제거.

    예: 2026-03-09 00:00:00+09:00 → 2026-03-09 00:00:00 (KST 날짜 유지)
    """
    if df.index.tz is not None:
        df = df.copy()
        df.index = pd.to_datetime(df.index.strftime("%Y-%m-%d"))
        df.index.name = "Date"
    return df


def _is_stale(last_date: "datetime.date", today: "datetime.date") -> bool:
    """
    마지막 데이터 날짜가 오래된 것인지 판별합니다.

    주말/공휴일을 고려하여 3 거래일(달력 기준 5일) 이상 차이날 때만 True.
    월요일 실행 시 금요일 데이터(3일 차이)는 정상으로 간주합니다.
    """
    days_old = (today - last_date).days
    return days_old > 4   # 공휴일 연휴까지 감안하여 4일 초과부터 경고


def _supplement_today_from_fast_info(
    ticker: yf.Ticker,
    symbol: str,
    df: pd.DataFrame,
    today: "datetime.date",
) -> pd.DataFrame:
    """
    yfinance history()에 오늘 데이터가 없을 때 fast_info 실시간 가격으로 보완합니다.

    보완 조건:
    - fast_info.last_price 가 존재하고 0보다 클 것
    - df의 마지막 Close와 0.1% 이상 차이날 것 (같으면 비거래일 = 보완 불필요)

    장중 호출 시에는 현재가(intraday)가, 장마감 후에는 종가가 주입됩니다.
    """
    try:
        fi = ticker.fast_info
        last_price = getattr(fi, "last_price", None)
        if not last_price or last_price <= 0:
            return df

        last_close = float(df.iloc[-1]["Close"])
        # 가격 차이 0.1% 이내 → 이미 최신 데이터 포함 (비거래일 등)
        if abs(last_price - last_close) / last_close < 0.001:
            return df

        today_row = pd.DataFrame(
            [{
                "Open":   getattr(fi, "open",        last_price),
                "High":   getattr(fi, "day_high",    last_price),
                "Low":    getattr(fi, "day_low",     last_price),
                "Close":  last_price,
                "Volume": int(getattr(fi, "last_volume", 0) or 0),
            }],
            index=[pd.Timestamp(today)],
        )
        today_row.index.name = "Date"
        df = pd.concat([df, today_row])
        logger.info(
            "fast_info 보완: %s → 오늘 Close %.4f 주입 (history 지연 감지)",
            symbol, last_price,
        )

    except Exception as exc:
        logger.warning("fast_info 보완 실패 (%s): %s", symbol, exc)

    return df


def fetch_ohlcv(symbol: str, lookback_days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    """
    단일 심볼의 OHLCV 데이터를 가져옵니다.

    Parameters
    ----------
    symbol       : yfinance 심볼 (예: "005930.KS", "AAPL", "BTC-USD")
    lookback_days: 조회 기간 (기본값은 config.LOOKBACK_DAYS)

    Returns
    -------
    columns: Date(index, timezone-naive), Open, High, Low, Close, Volume
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

        # 타임존 제거: 시장 로컬 날짜 기준으로 naive index 변환
        # (tz_convert(None)은 UTC 변환으로 날짜가 밀리므로 replace 방식 사용)
        df = _strip_timezone(df)

        # 필요한 컬럼만 유지
        required = ["Open", "High", "Low", "Close", "Volume"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            logger.warning("%s: 누락 컬럼 %s", symbol, missing)
            return pd.DataFrame()

        # Close 기준으로만 dropna — Volume NaN으로 인해 오늘 데이터가 삭제되는 것 방지
        df = df[required].dropna(subset=["Close"])

        if df.empty:
            return pd.DataFrame()

        # 최신성 확인 및 fast_info 보완
        today = datetime.today().date()
        last_date = df.index[-1].date()

        if last_date < today:
            # 거래일 지연: fast_info 실시간 가격으로 오늘 행 보완 시도
            df = _supplement_today_from_fast_info(ticker, symbol, df, today)
            # 보완 후에도 stale하면 경고
            last_date_after = df.index[-1].date()
            if _is_stale(last_date_after, today):
                logger.warning(
                    "오래된 데이터 감지: %s → 마지막 날짜 %s (%d일 전) — 거래 정지/Yahoo 지연 가능성",
                    symbol, last_date_after, (today - last_date_after).days,
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
    """
    심볼의 최신 종가(또는 현재가)를 반환합니다.

    fast_info를 우선 사용하여 실시간 가격을 반환하고,
    실패 시 history fallback.
    """
    try:
        fi = yf.Ticker(symbol).fast_info
        price = getattr(fi, "last_price", None)
        if price and price > 0:
            return float(price)
    except Exception:
        pass
    # fallback: history
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
