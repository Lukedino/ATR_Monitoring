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
#   1) GitHub Secret 'STOCK_LIST' 환경변수 (JSON 문자열)
#      예) {"국내주식":["005930.KS"],"해외주식":["AAPL","NVDA"]}
#   2) 로컬 개발용 fallback (아래 하드코딩 값)
#
# yfinance 심볼 기준
#   국내주식: 종목코드.KS (KOSPI) 또는 .KQ (KOSDAQ)
#   해외주식: 그대로 (AAPL, MSFT …)
#   암호화폐: BTC-USD, ETH-USD …
#   ETF: SPY, QQQ, 069500.KS …
# ─────────────────────────────────────────────────────────────

# 로컬 개발용 fallback (테스트 종목만 소수 유지)
_PORTFOLIO_FALLBACK: dict[str, list[str]] = {
    "국내주식": [
        "005930.KS",   # 삼성전자
    ],
    "해외주식": [
        "AAPL",        # Apple
        "NVDA",        # NVIDIA
    ],
    "암호화폐": [
        "BTC-USD",
    ],
    "ETF": [
        "SPY",         # S&P 500
    ],
}

_stock_list_env = os.getenv("STOCK_LIST", "")
if _stock_list_env:
    try:
        PORTFOLIO: dict[str, list[str]] = json.loads(_stock_list_env)
    except json.JSONDecodeError:
        import logging as _logging
        _logging.getLogger("config").warning(
            "STOCK_LIST 환경변수 JSON 파싱 실패 — fallback 사용"
        )
        PORTFOLIO = _PORTFOLIO_FALLBACK
else:
    PORTFOLIO = _PORTFOLIO_FALLBACK

# 모든 심볼을 플랫 리스트로
ALL_SYMBOLS: list[str] = [s for symbols in PORTFOLIO.values() for s in symbols]

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
# 스케줄링 (한국 시간 기준 HH:MM)
# ─────────────────────────────────────────────────────────────
SCHEDULE_TIMES: list[str] = [
    "09:05",   # 국내장 시작 직후
    "16:00",   # 국내장 마감 후
    "23:00",   # 미국장 개장 전 (KST 23:30 = EST 09:30)
]

# ─────────────────────────────────────────────────────────────
# 차트 설정
# ─────────────────────────────────────────────────────────────
CHART_OUTPUT_DIR: str = "charts"   # 차트 임시 저장 폴더
CHART_LOOKBACK: int   = 60         # 차트에 표시할 최근 N일
