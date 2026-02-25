"""
ATR 기반 위험 조정 포지션 사이징 모듈

핵심 공식 (ATR Position Sizing):
  달러 리스크    = 계좌 크기 × 트레이드당 리스크 비율
  손절 거리(주당) = ATR × 시장별 배수 (Chandelier Exit 기준)
  포지션 수량    = 달러 리스크 / 손절 거리
  포지션 금액    = 포지션 수량 × 현재 종가

Pyramid Exit (분할 매도):
  1차 목표가 = 진입가 + ATR × 2   → 33% 매도 + Stop을 Breakeven으로 상향
  2차 목표가 = 진입가 + ATR × 4   → 33% 추가 매도
  3차 목표가 = 진입가 + ATR × 6   → 잔여 전량 청산
  (또는 ATR Trailing Stop 발동 시 청산)

시장별 배수:
  KR 2.0× / US 2.0× / Crypto 2.5× / ETF 2.0×
  + ATR% 구간별 보정 (config.get_atr_multiple 참조)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from config import ACCOUNT_SIZE, RISK_PER_TRADE, get_atr_multiple


@dataclass
class PositionResult:
    symbol:          str
    market:          str    # KR / US / Crypto / ETF
    close_price:     float  # 현재 종가 (= 진입가 기준)
    atr:             float  # 현재 ATR(14) 절대값
    atr_pct:         float  # ATR%
    multiple:        float  # 적용 배수 (시장별 + ATR% 보정)
    dollar_risk:     float  # 허용 리스크 금액
    stop_distance:   float  # 손절 거리 = ATR × multiple
    quantity:        float  # 권장 포지션 수량
    position_value:  float  # 포지션 총 금액
    stop_loss_price: float  # 손절가 (진입가 - stop_distance)
    # Pyramid Exit 목표가 (진입가 + ATR × N배)
    target_1:        float  # 1차: ATR × 2
    target_2:        float  # 2차: ATR × 4
    target_3:        float  # 3차: ATR × 6
    # 단계별 매도 수량 (33% / 33% / 잔여)
    sell_1:          int    = field(default=0)
    sell_2:          int    = field(default=0)
    sell_3:          int    = field(default=0)

    def __post_init__(self) -> None:
        total    = int(self.quantity)
        s1       = int(total * 0.33)
        s2       = int(total * 0.33)
        s3       = total - s1 - s2
        self.sell_1 = s1
        self.sell_2 = s2
        self.sell_3 = max(s3, 0)

    def to_text(self) -> str:
        return (
            f"심볼: {self.symbol} [{self.market}]\n"
            f"현재가: {self.close_price:,.4f}\n"
            f"ATR: {self.atr:.4f} ({self.atr_pct:.2f}%)  배수: {self.multiple}x\n"
            f"허용 리스크: {self.dollar_risk:,.0f}원\n"
            f"손절 거리 ({self.multiple}xATR): {self.stop_distance:,.4f}\n"
            f"권장 수량: {int(self.quantity)}주  포지션 금액: {self.position_value:,.0f}원\n"
            f"손절가: {self.stop_loss_price:,.4f}\n"
            f"---\n"
            f"1차 목표 {self.target_1:,.4f} → {self.sell_1}주 매도 (Breakeven 전환)\n"
            f"2차 목표 {self.target_2:,.4f} → {self.sell_2}주 매도 (Trailing 유지)\n"
            f"3차 목표 {self.target_3:,.4f} → {self.sell_3}주 청산"
        )


def calc_position_size(
    symbol: str,
    close_price: float,
    atr: float,
    atr_pct: float | None = None,
    account_size: float   = ACCOUNT_SIZE,
    risk_per_trade: float = RISK_PER_TRADE,
) -> PositionResult | None:
    """
    단일 자산에 대한 ATR 기반 포지션 크기를 계산합니다.

    Parameters
    ----------
    symbol        : 종목 코드 (시장 분류 자동)
    close_price   : 현재 종가 (진입가 기준)
    atr           : 현재 ATR 절대값
    atr_pct       : ATR% (None이면 atr/close_price 로 계산)
    account_size  : 계좌 금액 (원)
    risk_per_trade: 트레이드당 허용 리스크 비율 (0.01 = 1%)

    Returns
    -------
    PositionResult 또는 None (atr·close_price 가 0 이하 시)
    """
    if atr <= 0 or close_price <= 0:
        return None

    computed_pct = atr_pct if atr_pct is not None else (atr / close_price * 100)
    multiple     = get_atr_multiple(symbol, computed_pct)

    from config import get_market_type
    market = get_market_type(symbol)

    dollar_risk    = account_size * risk_per_trade
    stop_distance  = atr * multiple
    quantity       = dollar_risk / stop_distance
    position_value = quantity * close_price

    return PositionResult(
        symbol         = symbol,
        market         = market,
        close_price    = close_price,
        atr            = atr,
        atr_pct        = round(computed_pct, 2),
        multiple       = multiple,
        dollar_risk    = dollar_risk,
        stop_distance  = stop_distance,
        quantity       = quantity,
        position_value = position_value,
        stop_loss_price = close_price - stop_distance,
        target_1       = close_price + atr * 2,
        target_2       = close_price + atr * 4,
        target_3       = close_price + atr * 6,
    )


def calc_portfolio_positions(
    summary_df,
    account_size: float   = ACCOUNT_SIZE,
    risk_per_trade: float = RISK_PER_TRADE,
) -> list[PositionResult]:
    """
    포트폴리오 전체 종목의 포지션 크기를 일괄 계산합니다.
    summary_df는 atr_calculator.summarize_portfolio_atr() 반환값.
    """
    results: list[PositionResult] = []
    for _, row in summary_df.iterrows():
        result = calc_position_size(
            symbol        = row["Symbol"],
            close_price   = row["Close"],
            atr           = row["ATR"],
            atr_pct       = row.get("ATR%"),
            account_size  = account_size,
            risk_per_trade= risk_per_trade,
        )
        if result:
            results.append(result)
    return results


def format_position_table(results: list[PositionResult]) -> str:
    """포지션 결과를 텔레그램용 텍스트 테이블로 포매팅합니다."""
    if not results:
        return "포지션 데이터 없음"

    lines = [
        "*포지션 사이징 요약*",
        f"{'심볼':<14} {'시장':>5} {'배수':>5} {'ATR%':>6} {'수량':>8} {'포지션금액':>14} {'손절가':>12}",
        "-" * 68,
    ]
    for r in sorted(results, key=lambda x: x.atr_pct, reverse=True):
        lines.append(
            f"{r.symbol:<14} {r.market:>5} {r.multiple:>4.1f}x "
            f"{r.atr_pct:>5.2f}% {int(r.quantity):>8} "
            f"{r.position_value:>13,.0f}  {r.stop_loss_price:>12,.4f}"
        )
    return "\n".join(lines)
