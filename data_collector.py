"""
yfinance 기반 OHLCV 데이터 수집 모듈

주요 기능:
- 단일/복수 심볼 다운로드
- High, Low, Close 컬럼 정규화
- 국내 주식(.KS/.KQ), 해외 주식, 암호화폐, ETF 통합 처리
- 시각이 확인된 Yahoo metadata로 시장 날짜의 부분 봉 보완
"""
from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

import requests

from config import LOOKBACK_DAYS
from market_dates import daily_bar_date, market_date, utc_now

logger = logging.getLogger(__name__)


_CRYPTO_SEARCH_LIMIT = 15


def _crypto_search_parts(symbol: str) -> tuple[str, str, str] | None:
    if not isinstance(symbol, str):
        return None
    normalized = symbol.strip().upper()
    for suffix in ("-USDT", "-USD"):
        if normalized.endswith(suffix):
            base = normalized[:-len(suffix)]
            return (normalized, base, suffix) if base else None
    return None


def _valid_crypto_search_field(value: object) -> bool:
    """Provider identity fields must not need whitespace/control cleanup."""
    return (isinstance(value, str) and bool(value)
            and not any(char.isspace() or ord(char) < 32 or ord(char) == 127
                        for char in value))


def _select_yahoo_crypto_ticker(symbol: str, data: object) -> str | None:
    """Choose an exact symbol or one unambiguous same-currency numeric variant.

    Search ordering is not identity evidence. Review the entire response and
    reject malformed or potentially truncated results before inferring an alias.
    Repeated rows for the same normalized ticker do not create ambiguity.
    """
    parts = _crypto_search_parts(symbol)
    if parts is None:
        return None
    requested, base, suffix = parts
    if not isinstance(data, dict) or not isinstance(data.get("quotes"), list):
        logger.warning("크립토 심볼 보정 보류: 검색 응답 형식이 올바르지 않습니다.")
        return None

    exact = False
    variants: set[str] = set()
    quotes = data["quotes"]
    for quote in quotes:
        if (not isinstance(quote, dict)
                or not _valid_crypto_search_field(quote.get("symbol"))
                or not _valid_crypto_search_field(quote.get("quoteType"))):
            logger.warning("크립토 심볼 보정 보류: 검색 응답 형식이 올바르지 않습니다.")
            return None
        qsym = quote["symbol"].upper()
        qtype = quote["quoteType"].upper()
        if qtype != "CRYPTOCURRENCY" or not qsym.endswith(suffix):
            continue
        if qsym == requested:
            exact = True
            continue
        core = qsym[:-len(suffix)]
        if core.startswith(base):
            tail = core[len(base):]
            if tail and tail.isascii() and tail.isdigit():
                variants.add(qsym)

    # An exact hit also prevents a temporarily empty original ticker from being
    # silently replaced with another coin merely because its result came first.
    if exact:
        return requested
    if len(variants) > 1:
        logger.warning("크립토 심볼 보정 보류: 숫자 변형 후보가 여러 개입니다.")
        return None
    if len(quotes) >= _CRYPTO_SEARCH_LIMIT:
        logger.warning("크립토 심볼 보정 보류: 검색 결과가 조회 한도에 도달했습니다.")
        return None
    return next(iter(variants)) if variants else None


def _resolve_yahoo_crypto_ticker(symbol: str) -> str | None:
    """
    야후 검색 API로 크립토 심볼의 실제 yfinance 티커를 찾습니다.

    배경: 신규 코인은 야후가 충돌 회피용 숫자 ID를 부여하여 베이스 심볼만으로
          조회 불가한 경우가 있음 (예: HYPE-USD 직접 조회 실패 → 실제는 HYPE32196-USD).

    동작:
    - "BASE-USD" / "BASE-USDT" 형식 입력 → 베이스 부분 추출 후 검색
    - 같은 통화의 CRYPTOCURRENCY 중 정확한 심볼을 최우선으로 반환
    - 정확한 심볼이 없으면 ASCII 숫자 변형이 유일하고 응답이 한도 미만일 때만 반환
    - 여러 후보나 잘못된 응답은 자동 보정하지 않음

    Returns
    -------
    실제 yfinance 티커 (예: "HYPE32196-USD") 또는 None
    """
    parts = _crypto_search_parts(symbol)
    if parts is None:
        return None
    _, base, _ = parts

    try:
        url = "https://query1.finance.yahoo.com/v1/finance/search"
        params = {"q": base, "quotesCount": _CRYPTO_SEARCH_LIMIT, "newsCount": 0}
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = requests.get(url, params=params, headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()

        return _select_yahoo_crypto_ticker(symbol, data)
    except Exception as exc:
        logger.debug("야후 크립토 심볼 검색 실패: %s", type(exc).__name__)
        return None


def _fetch_naver_kr_price(code6: str) -> float | None:
    """
    네이버 금융 모바일 API로 국내 종목 현재가를 조회합니다.

    새 부분 봉을 만들기 전 Yahoo 가격과 독립적으로 교차 검증합니다.
    이 응답은 가격만 반환하므로 시각이 있는 Yahoo 가격 대신 주입하지 않습니다.

    Parameters
    ----------
    code6 : 6자리 종목코드 (예: "019540")

    Returns
    -------
    현재가 (float) 또는 None (조회 실패 / 장 마감 후 무거래 상태)
    """
    try:
        url = f"https://m.stock.naver.com/api/stock/{code6}/basic"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer":    "https://m.stock.naver.com/",
            "Accept":     "application/json",
        }
        resp = requests.get(url, headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        # 장중/장마감 공통: closePrice → stockPrice 순으로 시도
        for field in ("closePrice", "stockPrice"):
            raw = data.get(field, "")
            if raw:
                price = float(str(raw).replace(",", ""))
                if math.isfinite(price) and price > 0:
                    return price
        return None
    except Exception as exc:
        logger.debug("네이버 가격 조회 실패 (%s): %s", code6, type(exc).__name__)
        return None


def _build_date_range(lookback_days: int, today: date) -> tuple[str, str]:
    """호출자가 정한 시장 날짜로 시작일/종료일 문자열(YYYY-MM-DD) 반환.

    yfinance history(end=...) 는 end 날짜를 exclusive 처리하므로
    오늘 데이터를 포함하려면 end = today + 1일 이 필요합니다.
    """
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


def _positive_quote_number(value) -> float | None:
    """Only finite positive JSON numbers qualify as quote prices/timestamps."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _sync_latest_quote(
    ticker: yf.Ticker,
    symbol: str,
    df: pd.DataFrame,
    *,
    now_utc: datetime | None = None,
    receipt_clock: Callable[[], datetime] | None = None,
) -> pd.DataFrame:
    """같은 Yahoo metadata 응답의 시각·HLC로만 시장 당일 봉을 보완합니다.

    fast_info에는 이 가격과 짝지을 시각이 없으므로 사용하지 않습니다.
    regularMarketTime/Price/DayHigh/DayLow 중 하나라도 없거나 오래된 응답이면
    원본을 유지합니다. 제공자가 metadata를 생략하면 부분 봉 보완도 생략됩니다.
    KR 신규 봉은 Naver와 5% 초과 괴리 시 보류하며, 시간 없는 Naver 값을
    Yahoo timestamp에 결합하지 않습니다. 신규 봉 Open은 확인할 수 없어 NaN입니다.
    시장 날짜는 조회 시작 시각에 고정하되, 미래 시세는 응답 수신 시각과 비교합니다.
    명시한 now_utc는 receipt_clock이 없으면 재현 가능한 고정 시각으로 취급합니다.
    """
    now = utc_now(now_utc)
    today = market_date(symbol, now)
    if df.empty:
        return df
    last_date = daily_bar_date(df.index[-1])
    if last_date is None or last_date > today:
        return df
    try:
        metadata = ticker.get_history_metadata()
        received = utc_now(receipt_clock()) if receipt_clock is not None else (
            utc_now() if now_utc is None else now
        )
        if received < now:
            return df
        if not isinstance(metadata, dict):
            return df
        timestamp = _positive_quote_number(metadata.get("regularMarketTime"))
        last_price = _positive_quote_number(metadata.get("regularMarketPrice"))
        day_high = _positive_quote_number(metadata.get("regularMarketDayHigh"))
        day_low = _positive_quote_number(metadata.get("regularMarketDayLow"))
        if any(value is None for value in (timestamp, last_price, day_high, day_low)):
            return df
        quote_time = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        if quote_time > received or market_date(symbol, quote_time) != today:
            return df
        if not day_low <= last_price <= day_high:
            return df

        last_close = float(df["Close"].iloc[-1])
        if not math.isfinite(last_close) or last_close <= 0:
            logger.warning("시세 보완 건너뜀: %s → 기존 Close 비정상", symbol)
            return df
        diff_ratio = abs(last_price - last_close) / last_close

        # 이미 당일 행이 있으면 기존 작은 차이 무시 정책 유지.
        # 전일과 가격이 같아도 확인된 당일 시세는 새 봉을 만들 수 있다.
        if last_date == today and diff_ratio < 0.001:
            return df

        # KR 종목: 31% 초과 차이 → 분할/권리락 의심, 동기화 건너뜀
        _is_kr = symbol.upper().endswith((".KS", ".KQ"))
        if _is_kr and diff_ratio > 0.31:
            logger.warning(
                "시세 보완 건너뜀: %s → 변동 %.1f%% (KR 한도 31%% 초과, 분할/권리락 의심)",
                symbol, diff_ratio * 100,
            )
            return df

        if last_date == today:
            # 오늘 행이 있지만 Close가 stale → 기존 행 업데이트
            result = df.copy()
            result.loc[result.index[-1], "Close"] = last_price
            result.loc[result.index[-1], "High"] = max(day_high, float(df["High"].iloc[-1]))
            result.loc[result.index[-1], "Low"] = min(day_low, float(df["Low"].iloc[-1]))
            logger.info(
                "시각 확인된 시세 보완: %s Close %.4f → %.4f (시장 당일 행 업데이트)",
                symbol, last_close, last_price,
            )
        else:
            # last_date < today만 남는다. 기존 행 뒤에 과거/중복 날짜를 붙이지 않는다.
            if _is_kr:
                _code6 = symbol.split(".")[0]
                _naver = _positive_quote_number(_fetch_naver_kr_price(_code6))
                if _naver is not None:
                    _nv_diff = abs(_naver - last_price) / last_price
                    if _nv_diff > 0.05:
                        logger.warning(
                            "시세 보완 보류: %s → Yahoo/Naver %.1f%% 괴리 (시간 없는 가격 대체 금지)",
                            symbol, _nv_diff * 100,
                        )
                        return df

            volume = _positive_quote_number(metadata.get("regularMarketVolume"))
            today_row = pd.DataFrame(
                [{
                    "Open":   float("nan"),
                    "High":   day_high,
                    "Low":    day_low,
                    "Close":  last_price,
                    "Volume": int(volume) if volume is not None and volume.is_integer() else 0,
                }],
                index=[pd.Timestamp(today)],
            )
            today_row.index.name = "Date"
            result = pd.concat([df, today_row])
            result.attrs = dict(df.attrs)
            logger.info(
                "시각 확인된 시세 보완: %s → 시장 당일 Close %.4f 부분 봉 추가",
                symbol, last_price,
            )
        return result
    except Exception as exc:
        logger.warning("시각 확인된 시세 보완 실패 (%s): %s", symbol, type(exc).__name__)
    return df


def fetch_ohlcv(
    symbol: str,
    lookback_days: int = LOOKBACK_DAYS,
    *,
    now_utc: datetime | None = None,
) -> pd.DataFrame:
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

    시장별 auto_adjust 처리:
    - .KQ (KOSDAQ): auto_adjust=False — Yahoo의 KOSDAQ 보정 데이터가 비신뢰,
                    거래소 원시 종가 사용으로 가격 왜곡 차단
    - .KS (KOSPI) : auto_adjust=True  — Yahoo의 KOSPI 보정은 비교적 신뢰도 높음.
                    배당·권리락 등 기업 이벤트를 소급 반영하여 연속 가격 계열 유지.
                    (False로 바꾸면 권리락일 비교 시 phantom 급락 트리거 오발령)
    - non-KR      : auto_adjust=True  — 배당 제거된 연속 가격 계열 사용
    """
    now = utc_now(now_utc)
    today = market_date(symbol, now)
    start, end = _build_date_range(lookback_days, today)
    _sym_upper = symbol.upper()
    _is_kq     = _sym_upper.endswith(".KQ")
    _is_kr     = _sym_upper.endswith(".KS") or _is_kq
    try:
        ticker = yf.Ticker(symbol)
        # .KQ만 auto_adjust=False (KOSDAQ 보정 왜곡 차단)
        # .KS 및 non-KR은 auto_adjust=True (권리락·배당 소급 반영)
        df = ticker.history(start=start, end=end, auto_adjust=not _is_kq)

        # KR 종목 suffix 자동 교정
        # 한국 6자리 코드는 KOSPI/KOSDAQ 전체에서 유일하므로
        # 데이터 없으면 반대 suffix (.KS ↔ .KQ)로 재시도해 포트폴리오 입력 오류 보완
        if df.empty and _is_kr:
            if _sym_upper.endswith(".KS"):
                alt_symbol = symbol[:-3] + ".KQ"
                alt_is_kq  = True
            else:
                alt_symbol = symbol[:-3] + ".KS"
                alt_is_kq  = False
            alt_ticker = yf.Ticker(alt_symbol)
            # 교정 후 종목도 각 시장에 맞는 auto_adjust 적용 (.KQ는 False, .KS는 True)
            alt_df = alt_ticker.history(start=start, end=end, auto_adjust=not alt_is_kq)
            if not alt_df.empty:
                logger.warning(
                    "KR suffix 자동 교정: %s → %s 로 데이터 수신 (포트폴리오 구분 컬럼 확인 필요)",
                    symbol, alt_symbol,
                )
                ticker  = alt_ticker
                df      = alt_df
                _is_kq  = alt_is_kq

        # 크립토 심볼 자동 교정
        # 신규 코인은 야후가 충돌 회피용 숫자 ID를 부여 (예: HYPE-USD → HYPE32196-USD)
        # 데이터 없으면 야후 검색 API로 실제 티커를 찾아 재조회
        _is_crypto = _sym_upper.endswith("-USD") or _sym_upper.endswith("-USDT")
        if df.empty and _is_crypto:
            resolved = _resolve_yahoo_crypto_ticker(symbol)
            if resolved and resolved.upper() != _sym_upper:
                alt_ticker = yf.Ticker(resolved)
                alt_df = alt_ticker.history(start=start, end=end, auto_adjust=True)
                if not alt_df.empty:
                    logger.warning(
                        "크립토 심볼 자동 교정: %s → %s (야후 숫자 ID 부여 코인)",
                        symbol, resolved,
                    )
                    ticker = alt_ticker
                    df     = alt_df

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

        # Open/High/Low=0 → NaN 정리
        # 원인: KR 장 개장 전(UTC 23시대) yfinance가 KST 다음날 부분 행을 반환하며
        #       Open/High/Low=0, Close=전일 종가 형태로 반환 → GAP DOWN -100% 오발령
        # Close는 이미 위에서 필터했으므로 나머지 OHLC만 처리
        df = df.copy()
        for _col in ("Open", "High", "Low"):
            _zero = df[_col] <= 0
            if _zero.any():
                logger.debug("%s: %s=0 행 %d개 → NaN 처리 (장 개시 전 부분 데이터)", symbol, _col, _zero.sum())
                df.loc[_zero, _col] = float("nan")

        # ──────────────────────────────────────────────────────────
        # .KS 전용: auto_adjust=True 왜곡 감지 → auto_adjust=False 폴백
        #
        # 일부 KOSPI 종목은 Yahoo의 조정 팩터가 비신뢰:
        #   auto_adjust=True 역사 종가가 왜곡되어 fast_info 현재가와 31%+ 괴리 발생
        # → 해당 종목은 auto_adjust=False 재조회로 원시 종가 사용
        #
        # 일반 배당·권리락이 정상적으로 반영된 종목:
        #   auto_adjust=True 역사가 ≈ fast_info → 괴리 없음 → 재조회 불필요
        # ──────────────────────────────────────────────────────────
        _is_ks = _is_kr and not _is_kq   # .KS (KOSPI) 전용
        if _is_ks and not df.empty:
            try:
                _fp = getattr(ticker.fast_info, "last_price", None)
                _lc = float(df["Close"].iloc[-1])
                if _fp and _fp > 0 and _lc > 0 and abs(_fp - _lc) / _lc > 0.31:
                    _df_raw = ticker.history(start=start, end=end, auto_adjust=False)
                    if not _df_raw.empty:
                        _df_raw.columns = [c.strip().capitalize() for c in _df_raw.columns]
                        _df_raw = _strip_timezone(_df_raw)
                        _df_raw = _df_raw[[c for c in required if c in _df_raw.columns]]
                        _df_raw = _df_raw.dropna(subset=["Close"])
                        _df_raw = _df_raw[_df_raw["Close"] > 0]
                        if not _df_raw.empty:
                            logger.warning(
                                "%s: auto_adjust=True 왜곡 감지 (±%.1f%%) → "
                                "auto_adjust=False 원시 데이터로 대체",
                                symbol, abs(_fp - _lc) / _lc * 100,
                            )
                            df = _df_raw
            except Exception as _exc:
                logger.debug("%s: auto_adjust 품질 검증 실패: %s", symbol, type(_exc).__name__)

        # 조회 범위의 시장 날짜는 고정하고 응답 수신 시각으로 미래 quote를 걸러낸다.
        df = _sync_latest_quote(ticker, symbol, df, now_utc=now,
                                receipt_clock=utc_now if now_utc is None else None)

        # 보완 후에도 stale하면 경고
        last_date_after = daily_bar_date(df.index[-1])
        if last_date_after is not None and _is_stale(last_date_after, today):
            logger.warning(
                "오래된 데이터 감지: %s → 마지막 날짜 %s (%d일 전) — 거래 정지/Yahoo 지연 가능성",
                symbol, last_date_after, (today - last_date_after).days,
            )

        return df

    except Exception as exc:
        logger.error("데이터 조회 실패 (%s): %s", symbol, type(exc).__name__)
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
            logger.debug("종목명 조회 실패 (%s): %s", symbol, type(exc).__name__)


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
        logger.error("환율 조회 실패: %s", type(exc).__name__)
    logger.warning("환율 조회 실패 — 기본값 1,350 사용")
    return 1350.0
