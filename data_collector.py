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


def _sync_latest_price_from_fast_info(
    ticker: yf.Ticker,
    symbol: str,
    df: pd.DataFrame,
    today: "datetime.date",
) -> pd.DataFrame:
    """
    fast_info 실시간 가격으로 최신 Close를 항상 동기화합니다.

    기존 _supplement_today_from_fast_info()는 last_date < today 일 때만 실행했지만
    yfinance가 오늘 날짜 행은 있으나 Close가 전일 값 그대로인 경우(stale data)를
    처리하지 못해 previous close가 사용되는 문제가 있었습니다.

    처리 방식:
    - 0.1% 이내 차이 → 이미 최신, 변경 없음
    - last_date == today 이지만 Close가 stale → 기존 행 Close/High/Low 업데이트
    - last_date < today → 오늘 행 신규 추가

    장중에는 현재가(intraday), 장마감 후에는 종가가 적용됩니다.
    """
    try:
        fi = ticker.fast_info
        last_price = getattr(fi, "last_price", None)
        if not last_price or last_price <= 0:
            return df

        last_close = float(df["Close"].iloc[-1])
        if last_close <= 0:
            logger.warning("fast_info 동기화 건너뜀: %s → 기존 Close=%.4f (비정상값)", symbol, last_close)
            return df
        diff_ratio = abs(last_price - last_close) / last_close

        # 0.1% 이내 차이 → 이미 최신 데이터
        if diff_ratio < 0.001:
            return df

        # KR 종목: 31% 초과 차이 → 분할/권리락 의심, 동기화 건너뜀
        _is_kr = symbol.upper().endswith((".KS", ".KQ"))
        if _is_kr and diff_ratio > 0.31:
            logger.warning(
                "fast_info 동기화 건너뜀: %s → 변동 %.1f%% (KR 한도 31%% 초과, 분할/권리락 의심)",
                symbol, diff_ratio * 100,
            )
            return df

        _day_high = getattr(fi, "day_high", None)
        _day_low  = getattr(fi, "day_low",  None)
        last_date = df.index[-1].date()

        if last_date == today:
            # 오늘 행이 있지만 Close가 stale → 기존 행 업데이트
            df = df.copy()
            df.loc[df.index[-1], "Close"] = last_price
            if _day_high:
                df.loc[df.index[-1], "High"] = max(float(_day_high), float(df.loc[df.index[-1], "High"]))
            if _day_low:
                df.loc[df.index[-1], "Low"]  = min(float(_day_low),  float(df.loc[df.index[-1], "Low"]))
            logger.info(
                "fast_info 동기화: %s Close %.4f → %.4f (오늘 행 업데이트)",
                symbol, last_close, last_price,
            )
        else:
            # 오늘 행 없음 → 신규 추가
            # Open은 None이면 NaN — GAP 트리거가 SURGE와 동일값으로 중복 발생 방지
            _open = getattr(fi, "open", None)
            today_row = pd.DataFrame(
                [{
                    "Open":   float(_open)     if _open     else float("nan"),
                    "High":   float(_day_high) if _day_high else last_price,
                    "Low":    float(_day_low)  if _day_low  else last_price,
                    "Close":  last_price,
                    "Volume": int(getattr(fi, "last_volume", 0) or 0),
                }],
                index=[pd.Timestamp(today)],
            )
            today_row.index.name = "Date"
            df = pd.concat([df, today_row])
            logger.info(
                "fast_info 동기화: %s → 오늘 Close %.4f 주입 (history 지연 감지)",
                symbol, last_price,
            )

    except Exception as exc:
        logger.warning("fast_info 동기화 실패 (%s): %s", symbol, exc)

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

    KR 전용 처리 (.KS/.KQ 공통):
    Yahoo Finance 한국 주식 조정 데이터는 KOSPI/KOSDAQ 모두 신뢰도가 낮아
    auto_adjust=True 시 전체 가격 계열이 왜곡될 수 있습니다.
    .KS/.KQ 모두 auto_adjust=False로 조회하여 거래소 실제 종가를 사용합니다.
    """
    start, end = _build_date_range(lookback_days)
    _sym_upper = symbol.upper()
    _is_kq     = _sym_upper.endswith(".KQ")
    _is_kr     = _sym_upper.endswith(".KS") or _is_kq
    try:
        ticker = yf.Ticker(symbol)
        # KR 전체(.KS/.KQ): auto_adjust=False — Yahoo Finance 조정 왜곡 원천 차단
        # non-KR: auto_adjust=True (배당 제거된 연속 가격 계열 사용)
        df = ticker.history(start=start, end=end, auto_adjust=not _is_kr)

        # KR 종목 suffix 자동 교정
        # 한국 6자리 코드는 KOSPI/KOSDAQ 전체에서 유일하므로
        # 데이터 없으면 반대 suffix (.KS ↔ .KQ)로 재시도해 포트폴리오 입력 오류 보완
        if df.empty and _is_kr:
            if _sym_upper.endswith(".KS"):
                alt_symbol = symbol[:-3] + ".KQ"
            else:
                alt_symbol = symbol[:-3] + ".KS"
            alt_ticker = yf.Ticker(alt_symbol)
            # 자동 교정 후 종목도 KR → auto_adjust=False 유지
            alt_df = alt_ticker.history(start=start, end=end, auto_adjust=False)
            if not alt_df.empty:
                logger.warning(
                    "KR suffix 자동 교정: %s → %s 로 데이터 수신 (포트폴리오 구분 컬럼 확인 필요)",
                    symbol, alt_symbol,
                )
                ticker  = alt_ticker
                df      = alt_df
                _is_kq  = alt_symbol.upper().endswith(".KQ")

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

        # Close=0 행 제거: yfinance가 거래 없는 날을 0으로 채우는 경우
        # 0이 남으면 daily_chg = (0 - prev) / prev = -100% → 오발령 트리거 원인
        invalid_close = df["Close"] <= 0
        if invalid_close.any():
            logger.warning("%s: Close=0 행 %d개 제거", symbol, invalid_close.sum())
            df = df[~invalid_close]

        if df.empty:
            return pd.DataFrame()

        # fast_info로 최신 Close 항상 동기화
        # (오늘 행이 있어도 stale close인 경우 + 오늘 행이 없는 경우 모두 처리)
        today = datetime.today().date()
        df = _sync_latest_price_from_fast_info(ticker, symbol, df, today)

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
