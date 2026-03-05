"""
포트폴리오 ATR 모니터링 시스템 설정
모든 종목 코드, 임계값, 스케줄링 파라미터를 여기서 관리합니다.
"""
import json
import os
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────
# 텔레그램 설정
# ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str   = os.getenv("TELEGRAM_CHAT_ID", "")

# ─────────────────────────────────────────────────────────────
# 포트폴리오 구성
#
# 우선순위:
#   1) Google Drive (Service Account)
#      환경변수: GOOGLE_SERVICE_ACCOUNT_JSON + GDRIVE_PORTFOLIO_FILE_ID
#      파일 형식: Excel 복사 CSV (계좌, 구분, 종목, Ticker)
#      구분값: 한국 / 코스닥 / 미국 / 크립토 / ETF
#   2) GitHub Secret 'STOCK_LIST' 환경변수 (JSON 문자열, 레거시)
#   3) 로컬 개발용 fallback
# ─────────────────────────────────────────────────────────────

# 로컬 개발용 fallback (테스트 종목만 소수 유지)
_PORTFOLIO_FALLBACK: dict[str, list[str]] = {
    "국내주식": ["005930.KS"],   # 삼성전자
    "해외주식": ["AAPL", "NVDA"],
    "암호화폐": ["BTC-USD"],
    "ETF":      ["SPY"],
}

# Drive에서 읽어온 종목명 (KR_STOCK_NAMES 정의 후 병합)
_kr_names_from_drive: dict[str, str] = {}

# Drive에서 읽어온 계좌 매핑 (symbol → 계좌명 리스트)
_symbol_accounts: dict[str, list[str]] = {}

# Drive에서 읽어온 진입가격 (symbol → 평균 진입가, 복수 계좌면 평균)
_symbol_entry_prices: dict[str, float] = {}

# 구분 컬럼 → yfinance 심볼 suffix 매핑
_CATEGORY_SUFFIX: dict[str, str] = {
    "한국":   ".KS",
    "코스닥": ".KQ",
    "미국":   "",
    "크립토": "-USD",
    "ETF":    "",
}


def _parse_portfolio_df(df) -> dict[str, list[str]]:
    """
    Excel 복사 형식 DataFrame → PORTFOLIO dict.
    컬럼: 계좌, 구분, 종목(종목명), Ticker(종목코드), 거래일, 진입가격
    - Ticker 컬럼 우선 사용 (종목코드: 005930 / AAPL / BTC)
    - Ticker 없으면 종목 컬럼 사용 (US/크립토는 종목명 = ticker 코드)
    - 구분 컬럼으로 yfinance suffix 자동 결정
    - 계좌 컬럼 → _symbol_accounts 에 수집
    - 종목명 → _kr_names_from_drive 에 수집 (Ticker 컬럼 사용 시)
    """
    global _kr_names_from_drive, _symbol_accounts, _symbol_entry_prices
    df.columns = [str(c).strip() for c in df.columns]
    _ep_acc: dict[str, list[float]] = {}
    symbols: list[str] = []
    for _, row in df.iterrows():
        category = str(row.get("구분", "")).strip()
        account  = str(row.get("계좌", "")).strip()
        name     = str(row.get("종목", "")).strip()   # 종목명 (표시용)

        # Ticker 컬럼 우선 — 없으면 종목 컬럼을 코드로 사용
        ticker_raw = str(row.get("Ticker", "")).strip()
        ticker = ticker_raw if (ticker_raw and ticker_raw != "nan") else name

        if not ticker or ticker == "nan":
            continue

        suffix = _CATEGORY_SUFFIX.get(category, "")
        symbol = ticker + suffix

        if symbol not in symbols:          # 중복 제거 (순서 유지)
            symbols.append(symbol)

        # 종목명 수집 (Ticker 컬럼이 있을 때만 — 이름 ≠ 코드)
        if ticker_raw and ticker_raw != "nan" and name and name != "nan":
            _kr_names_from_drive[symbol] = name

        # 계좌 매핑 수집 (같은 종목이 여러 계좌에 있으면 모두 기록)
        if account and account != "nan":
            acct_list = _symbol_accounts.setdefault(symbol, [])
            if account not in acct_list:
                acct_list.append(account)

        # 진입가격 수집 (같은 종목 복수 계좌면 평균)
        ep_str = str(row.get("진입가격", "")).strip()
        if ep_str and ep_str != "nan":
            try:
                ep = float(ep_str.replace(",", ""))
                if ep > 0:
                    _ep_acc.setdefault(symbol, []).append(ep)
            except ValueError:
                pass

    _symbol_entry_prices = {sym: sum(v) / len(v) for sym, v in _ep_acc.items()}
    return {"포트폴리오": symbols}


def _load_portfolio_from_drive() -> "dict[str, list[str]] | None":
    """
    Google Drive에서 포트폴리오 파일을 다운로드하여 파싱합니다.
    Google Sheets / 업로드된 .xlsx / CSV 파일 모두 지원.
    """
    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    file_id = os.getenv("GDRIVE_PORTFOLIO_FILE_ID", "")
    if not sa_json or not file_id:
        return None
    try:
        import io
        import pandas as pd
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds   = service_account.Credentials.from_service_account_info(
            json.loads(sa_json),
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        service = build("drive", "v3", credentials=creds, cache_discovery=False)

        meta = service.files().get(fileId=file_id, fields="mimeType").execute()
        mime = meta["mimeType"]

        if mime == "application/vnd.google-apps.spreadsheet":
            # Google Sheets → CSV export
            raw = service.files().export(fileId=file_id, mimeType="text/csv").execute()
            df  = pd.read_csv(io.StringIO(raw.decode("utf-8")), sep=None, engine="python")
        elif mime in (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
        ):
            # 업로드된 Excel (.xlsx / .xls) → 이진 파일로 읽기
            raw = service.files().get_media(fileId=file_id).execute()
            df  = pd.read_excel(io.BytesIO(raw))
        else:
            # CSV 또는 텍스트 파일
            raw = service.files().get_media(fileId=file_id).execute()
            df  = pd.read_csv(io.StringIO(raw.decode("utf-8")), sep=None, engine="python")

        import logging as _log
        _log.getLogger("config").info("Drive 포트폴리오 로드 성공: %d종목 (mime=%s)", len(df), mime)
        return _parse_portfolio_df(df)

    except Exception as exc:
        import logging as _log
        _log.getLogger("config").warning("Drive 포트폴리오 로드 실패: %s — fallback 사용", exc)
        return None


_stock_list_env  = os.getenv("STOCK_LIST", "")
_drive_portfolio = _load_portfolio_from_drive()

if _drive_portfolio:
    PORTFOLIO: dict[str, list[str]] = _drive_portfolio
elif _stock_list_env:
    try:
        PORTFOLIO = json.loads(_stock_list_env)
    except json.JSONDecodeError:
        import logging as _logging
        _logging.getLogger("config").warning(
            "STOCK_LIST 환경변수 JSON 파싱 실패 — fallback 사용"
        )
        PORTFOLIO = _PORTFOLIO_FALLBACK
else:
    PORTFOLIO = _PORTFOLIO_FALLBACK

# 모든 심볼을 플랫 리스트로
ALL_SYMBOLS: list[str] = list(dict.fromkeys(s for symbols in PORTFOLIO.values() for s in symbols))

# 심볼 → 계좌명 리스트 (알람 메시지에서 "대상: 계좌A, 계좌B" 표시용)
SYMBOL_ACCOUNTS: dict[str, list[str]] = _symbol_accounts

# 심볼 → 평균 진입가격 (진입가 대비 ATR 분석용, 미등록 종목은 키 없음)
SYMBOL_ENTRY_PRICES: dict[str, float] = _symbol_entry_prices

# 시장별 심볼 분류 (job_stop_check / daily_report 분리용)
KR_SYMBOLS: list[str] = [
    s for s in ALL_SYMBOLS
    if s.upper().endswith('.KS') or s.upper().endswith('.KQ')
]
CRYPTO_SYMBOLS: list[str] = [
    s for s in ALL_SYMBOLS
    if s.upper().endswith('-USD') or s.upper().endswith('-USDT')
]
US_SYMBOLS: list[str] = [
    s for s in ALL_SYMBOLS
    if s not in KR_SYMBOLS and s not in CRYPTO_SYMBOLS
]

# ─────────────────────────────────────────────────────────────
# ATR 파라미터
# ─────────────────────────────────────────────────────────────
ATR_PERIOD: int          = 14      # Wilder 표준 14일
LOOKBACK_DAYS: int       = 100     # 데이터 조회 기간 (ATR 안정화용 여유분 포함)
ATR_HISTORY_PERIOD: int  = 20      # 알림 임계값 계산에 사용할 과거 ATR 평균 기간

# ATR 알림 조건
# 현재 ATR이 최근 N일 ATR 평균의 ALERT_MULTIPLIER 배를 초과하면 알림
ATR_ALERT_MULTIPLIER: float = 1.5

# ─────────────────────────────────────────────────────────────
# 시장별 ATR 배수 (Chandelier Exit 기준)
#
# 데이터 분석 근거 (약 2년, 21개 종목 검증):
#   시장별 ATR% 중앙값: KR 3.41% / US 2.32% / Crypto 4.94%
#   실효 Stop 거리 (ATR% × Multiple):
#     KR   2.0× → 6.82%  (1.5× 사용 시 FSR 39.5%로 과도)
#     US   2.0× → 4.64%  (검증 기준)
#     Crypto 2.5× → 12.35% (암호화폐 변동성 반영)
#   ETF는 US 주식과 유사한 변동성 → 2.0× 적용
# ─────────────────────────────────────────────────────────────
ATR_MULTIPLE_BY_MARKET: dict[str, float] = {
    "KR":     2.0,   # 한국 주식 — 분석 결과 1.5×는 FSR 39.5%로 과도, 2.0×로 상향
    "US":     2.0,   # 미국 주식 — 검증값 유지
    "Crypto": 2.5,   # 암호화폐 — 높은 변동성 반영, 유지
    "ETF":    2.0,   # ETF     — US 주식 수준
}

# ATR% 구간별 배수 보정 (시장 baseline 위에 추가 적용)
# 고변동성 종목에는 배수를 줄여 Stop 거리 과도 확장 방지
# 최종 배수 = max(baseline - adjustment, 1.0)
ATR_PCT_ADJUSTMENTS: list[tuple[float, float]] = [
    # (ATR% 하한,  배수 조정값)
    (10.0, -0.5),   # ATR% ≥ 10% → baseline에서 0.5 차감
    (7.0,  -0.25),  # ATR% ≥  7% → baseline에서 0.25 차감
    (0.0,   0.0),   # ATR% <  7% → 조정 없음
]

# ─────────────────────────────────────────────────────────────
# 한국 주식 종목명 매핑
# 이 목록에 없는 종목은 코드 그대로 표시됩니다.
# ─────────────────────────────────────────────────────────────
KR_STOCK_NAMES: dict[str, str] = {
    "005930.KS": "삼성전자",
    "000660.KS": "SK하이닉스",
    "035420.KS": "NAVER",
    "035720.KS": "카카오",
    "005380.KS": "현대차",
    "000270.KS": "기아",
    "051910.KS": "LG화학",
    "006400.KS": "삼성SDI",
    "207940.KS": "삼성바이오",
    "068270.KS": "셀트리온",
    "055550.KS": "신한지주",
    "105560.KS": "KB금융",
    "003550.KS": "LG",
    "066570.KS": "LG전자",
    "028260.KS": "삼성물산",
    "012330.KS": "현대모비스",
    "096770.KS": "SK이노베이션",
    "017670.KS": "SK텔레콤",
    "030200.KS": "KT",
    "034730.KS": "SK",
    "373220.KS": "LG에너지솔루션",
    "000100.KS": "유한양행",
    "035900.KQ": "JYP Ent.",
    "041510.KQ": "에스엠",
    "352820.KS": "하이브",
    # 국내 ETF
    "069500.KS": "KODEX 200",
    "102110.KS": "TIGER 200",
    "114800.KS": "KODEX 인버스",
    "252670.KS": "KODEX 200선물인버스2X",
}
# Drive에서 읽어온 종목명 병합 (Excel 종목 컬럼 → 한글명 직접 등록)
KR_STOCK_NAMES.update(_kr_names_from_drive)


def fmt_price(symbol: str, price: float) -> str:
    """
    시장별 가격 표기 형식 반환.

    .KS/.KQ 심볼 → 57,500원  (정수, 소수점 없음)
    그 외         → $185.43   (달러, 소수점 2자리)
    """
    sym_upper = symbol.upper()
    if sym_upper.endswith(".KS") or sym_upper.endswith(".KQ"):
        return f"{int(round(price)):,}원"
    return f"${price:,.2f}"


def fmt_symbol(symbol: str) -> str:
    """
    종목 표시명 반환.

    KR 등록 종목: 삼성전자 | 005930.KS
    KR 미등록   : 200710 (KS)   ← .KS suffix 분리
    그 외        : 그대로  (AAPL, BTC-USD, SPY 등)
    """
    name = KR_STOCK_NAMES.get(symbol)
    if name:
        return f"{name} | {symbol}"
    sym_upper = symbol.upper()
    if sym_upper.endswith(".KS"):
        return f"{symbol[:-3]} (KS)"
    if sym_upper.endswith(".KQ"):
        return f"{symbol[:-3]} (KQ)"
    return symbol


# ─────────────────────────────────────────────────────────────
# 시장 분류 헬퍼
# ─────────────────────────────────────────────────────────────

# ETF 심볼 목록 (yfinance 기준)
_ETF_SYMBOLS: frozenset[str] = frozenset({
    "SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT", "VTI", "VOO",
    "ARKK", "XLF", "XLE", "XLK", "SOXX",
    "069500.KS", "102110.KS", "114800.KS", "252670.KS",  # 국내 ETF
})


def get_market_type(symbol: str) -> str:
    """
    심볼 문자열로 시장 유형을 분류합니다.

    Returns
    -------
    "KR" | "US" | "Crypto" | "ETF"
    """
    sym_upper = symbol.upper()
    if sym_upper in _ETF_SYMBOLS:
        return "ETF"
    if sym_upper.endswith(".KS") or sym_upper.endswith(".KQ"):
        return "KR"
    if sym_upper.endswith("-USD") or sym_upper.endswith("-USDT"):
        return "Crypto"
    return "US"


def get_atr_multiple(symbol: str, atr_pct: float | None = None) -> float:
    """
    심볼과 현재 ATR%를 기반으로 최종 ATR 배수를 반환합니다.

    Parameters
    ----------
    symbol  : 종목 코드
    atr_pct : 현재 ATR% (None이면 시장 baseline만 적용)

    Returns
    -------
    최종 ATR 배수 (≥ 1.0)
    """
    market   = get_market_type(symbol)
    baseline = ATR_MULTIPLE_BY_MARKET.get(market, 2.0)

    if atr_pct is None:
        return baseline

    # ATR% 구간별 보정
    adjustment = 0.0
    for threshold, adj in ATR_PCT_ADJUSTMENTS:
        if atr_pct >= threshold:
            adjustment = adj
            break

    return max(baseline + adjustment, 1.0)

# ─────────────────────────────────────────────────────────────
# 위험 관리 / 포지션 사이징
# ─────────────────────────────────────────────────────────────
ACCOUNT_SIZE: float      = float(os.getenv("ACCOUNT_SIZE", 10_000_000))  # 원
RISK_PER_TRADE: float    = float(os.getenv("RISK_PER_TRADE", 0.01))       # 1%
STOP_LOSS_ATR_MULT: float = 2.0    # 레거시 호환 유지 (get_atr_multiple() 사용 권장)

# ─────────────────────────────────────────────────────────────
# 스케줄링 (KST 기준 — 로컬 스케줄러용)
# GHA 스케줄은 atr_monitor.yml 참조
# ─────────────────────────────────────────────────────────────
KR_REPORT_TIME: str = "18:00"   # 국내장 마감 후 일일 리포트 (KST)
US_REPORT_TIME: str = "08:00"   # 미국장+크립토 마감 후 일일 리포트 (KST)

# ─────────────────────────────────────────────────────────────
# 차트 설정
# ─────────────────────────────────────────────────────────────
CHART_OUTPUT_DIR: str = "charts"   # 차트 임시 저장 폴더
CHART_LOOKBACK: int   = 60         # 차트에 표시할 최근 N일
