"""
ATR 시각화 모듈

생성 차트:
1. 종가 + Chandelier Stop + ATR 3단 서브플롯 (단일 종목)
2. 포트폴리오 ATR% 비교 바차트 (포트폴리오 요약)
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")   # GUI 없는 환경에서도 렌더링 가능
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
import pandas as pd

from config import ATR_PERIOD, CHART_LOOKBACK, CHART_OUTPUT_DIR
from atr_calculator import calc_atr, calc_atr_pct, calc_chandelier_stop

logger = logging.getLogger(__name__)

# 한글 폰트 설정 (Windows)
try:
    plt.rcParams["font.family"] = "Malgun Gothic"
except Exception:
    pass
plt.rcParams["axes.unicode_minus"] = False


def _ensure_output_dir() -> Path:
    path = Path(CHART_OUTPUT_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _rolling_chandelier_series(
    symbol: str,
    df: pd.DataFrame,
    period: int    = ATR_PERIOD,
    hh_window: int = 20,
) -> pd.Series:
    """
    각 날짜별 Chandelier Stop을 롤링으로 계산하여 시리즈로 반환합니다.
    차트에 Stop 궤적을 그리기 위해 사용합니다.
    """
    from config import get_atr_multiple, get_market_type
    from atr_calculator import calc_atr

    atr_series = calc_atr(df, period)
    atr_pct    = (atr_series / df["Close"] * 100)
    stops      = pd.Series(index=df.index, dtype=float)

    for i in range(hh_window + period, len(df)):
        hh   = df["High"].iloc[i - hh_window: i + 1].max()
        atr  = atr_series.iloc[i]
        pct  = atr_pct.iloc[i]
        if pd.isna(atr):
            continue
        mult        = get_atr_multiple(symbol, float(pct))
        stops.iloc[i] = hh - atr * mult

    stops.name = "ChandelierStop"
    return stops


# ─────────────────────────────────────────────────────────────
# 단일 종목 ATR 차트
# ─────────────────────────────────────────────────────────────

def plot_atr_chart(
    symbol: str,
    df: pd.DataFrame,
    period: int             = ATR_PERIOD,
    lookback: int           = CHART_LOOKBACK,
    as_bytes: bool          = True,
    registered_stop: Optional[float] = None,   # stop_manager 등록 Stop (수평선)
) -> bytes | str:
    """
    종가 + Chandelier Stop + ATR 3단 서브플롯 차트를 생성합니다.

    Parameters
    ----------
    symbol          : 차트 제목에 사용할 심볼
    df              : High, Low, Close 컬럼 포함 DataFrame
    period          : ATR 기간
    lookback        : 차트에 표시할 최근 N일
    as_bytes        : True → PNG bytes / False → 파일 경로
    registered_stop : stop_manager에 등록된 현재 Stop (수평 기준선)

    Returns
    -------
    PNG 바이트 또는 저장된 파일 경로
    """
    atr_series  = calc_atr(df, period)
    atr_pct_ser = calc_atr_pct(df, period)
    chandelier  = calc_chandelier_stop(symbol, df, period)

    # 최근 lookback 기간만 표시
    df_plot  = df.iloc[-lookback:]
    atr_plot = atr_series.iloc[-lookback:]
    pct_plot = atr_pct_ser.iloc[-lookback:]

    # Chandelier Stop 시계열 (lookback 구간)
    stop_series = _rolling_chandelier_series(symbol, df, period, hh_window=20)
    stop_plot   = stop_series.iloc[-lookback:] if not stop_series.empty else None

    fig, axes = plt.subplots(
        3, 1,
        figsize=(12, 10),
        sharex=True,
        gridspec_kw={"height_ratios": [3.5, 1.5, 1.5]},
    )

    # 제목에 Stop 정보 포함
    stop_info = f"  Stop={chandelier.stop_level:,.2f}({chandelier.dist_to_stop_pct:.1f}%)" if chandelier else ""
    mult_info = f"  x{chandelier.multiple}" if chandelier else ""
    fig.suptitle(
        f"{symbol} [{chandelier.market if chandelier else '?'}]{mult_info}  |  ATR({period}){stop_info}",
        fontsize=13, fontweight="bold",
    )

    # ── 상단: 종가 + Chandelier Stop ────────────────────────
    ax_price = axes[0]
    ax_price.plot(df_plot.index, df_plot["Close"], color="#1f77b4", linewidth=1.5, label="종가", zorder=3)

    # Chandelier Stop 시계열 (롤링 계산)
    if stop_plot is not None and not stop_plot.dropna().empty:
        ax_price.plot(
            stop_plot.index, stop_plot,
            color="#d62728", linewidth=1.5, linestyle="--", label="Chandelier Stop", zorder=4,
        )
        # Stop과 종가 사이 음영 (안전 구간)
        ax_price.fill_between(
            stop_plot.index, stop_plot, df_plot["Close"],
            where=(df_plot["Close"] > stop_plot),
            alpha=0.08, color="#2ca02c", label="안전 구간",
        )

    # 등록된 현재 Stop 수평선
    if registered_stop is not None:
        ax_price.axhline(
            registered_stop, color="#e377c2", linewidth=2, linestyle="-.",
            label=f"등록 Stop {registered_stop:,.2f}", zorder=5,
        )

    # Highest High 마커
    if chandelier:
        ax_price.axhline(
            chandelier.highest_high, color="#bcbd22", linewidth=1,
            linestyle=":", alpha=0.8, label=f"Highest High {chandelier.highest_high:,.2f}",
        )

    ax_price.set_ylabel("가격")
    ax_price.legend(loc="upper left", fontsize=8)
    ax_price.grid(True, alpha=0.3)

    # ── 중단: ATR ────────────────────────────────────────────
    ax_atr = axes[1]
    ax_atr.plot(atr_plot.index, atr_plot, color="#ff7f0e", linewidth=1.5, label=f"ATR({period})")
    atr_avg = atr_plot.dropna().mean()
    ax_atr.axhline(
        atr_avg, color="red", linestyle="--", linewidth=1, alpha=0.7,
        label=f"평균 ATR ({atr_avg:.2f})",
    )
    ax_atr.set_ylabel("ATR")
    ax_atr.legend(loc="upper left", fontsize=8)
    ax_atr.grid(True, alpha=0.3)

    # ── 하단: ATR% ───────────────────────────────────────────
    ax_pct = axes[2]
    ax_pct.fill_between(pct_plot.index, pct_plot, alpha=0.5, color="#2ca02c", label="ATR%")
    ax_pct.set_ylabel("ATR%")
    ax_pct.set_xlabel("날짜")
    ax_pct.legend(loc="upper left", fontsize=8)
    ax_pct.grid(True, alpha=0.3)

    # X축 날짜 포맷
    ax_pct.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax_pct.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    fig.autofmt_xdate(rotation=30)

    plt.tight_layout()

    if as_bytes:
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=120, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    else:
        out_dir = _ensure_output_dir()
        fpath   = out_dir / f"{symbol.replace('.', '_')}_atr.png"
        plt.savefig(fpath, format="png", dpi=120, bbox_inches="tight")
        plt.close(fig)
        return str(fpath)


# ─────────────────────────────────────────────────────────────
# 포트폴리오 ATR% 비교 바차트
# ─────────────────────────────────────────────────────────────

def plot_portfolio_atr_bar(
    summary_df: pd.DataFrame,
    as_bytes: bool = True,
) -> bytes | str:
    """
    포트폴리오 전체 종목의 ATR%를 수평 바차트로 비교합니다.

    Parameters
    ----------
    summary_df : atr_calculator.summarize_portfolio_atr() 반환 DataFrame
    as_bytes   : True → PNG bytes / False → 파일 경로
    """
    if summary_df.empty:
        logger.warning("포트폴리오 요약 데이터 없음 — 차트 생성 건너뜀")
        return b"" if as_bytes else ""

    df_sorted = summary_df.sort_values("ATR%", ascending=True)
    colors    = ["#d62728" if s else "#1f77b4" for s in df_sorted["Spike"]]

    fig, ax = plt.subplots(figsize=(10, max(4, len(df_sorted) * 0.45)))
    bars = ax.barh(df_sorted["Symbol"], df_sorted["ATR%"], color=colors)

    # 값 레이블
    for bar, val in zip(bars, df_sorted["ATR%"]):
        ax.text(
            bar.get_width() + 0.02,
            bar.get_y() + bar.get_height() / 2,
            f"{val:.2f}%",
            va="center",
            fontsize=9,
        )

    ax.set_xlabel("ATR% (변동성)")
    ax.set_title("포트폴리오 ATR% 비교  (빨강=스파이크)", fontweight="bold")
    ax.grid(True, axis="x", alpha=0.3)
    plt.tight_layout()

    if as_bytes:
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=120, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    else:
        out_dir = _ensure_output_dir()
        fpath   = out_dir / "portfolio_atr_bar.png"
        plt.savefig(fpath, format="png", dpi=120, bbox_inches="tight")
        plt.close(fig)
        return str(fpath)
