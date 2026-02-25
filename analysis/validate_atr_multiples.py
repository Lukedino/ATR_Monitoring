# -*- coding: utf-8 -*-
"""
ATR 시장별 배수 타당성 검증 분석 스크립트

분석 항목:
  1. 시장별 ATR% 분포 통계 (평균/중앙값/상위25% 등)
  2. 제안 배수(KR:1.5 / US:2.0 / Crypto:2.5)의 Stop 거리 분포
  3. False Signal Rate — Stop이 3거래일 이내 돌파되는 비율 (노이즈)
  4. Whipsaw Rate     — Stop 돌파 후 5일 이내 가격이 회복되는 비율
  5. 시장별 최적 배수 추정 (노이즈 5% 이하 유지 조건)
  6. 결론 및 최종 권장 배수
"""
from __future__ import annotations

import warnings
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────
# 분석 대상 심볼 정의
# ─────────────────────────────────────────────────────────────
SYMBOLS: dict[str, list[str]] = {
    "KR": [
        "005930.KS",   # 삼성전자 (KOSPI 대형)
        "000660.KS",   # SK하이닉스 (반도체)
        "035420.KS",   # NAVER (성장)
        "005380.KS",   # 현대차
        "051910.KS",   # LG화학
        "068270.KS",   # 셀트리온 (바이오)
        "035720.KS",   # 카카오
        "006400.KS",   # 삼성SDI
    ],
    "US": [
        "AAPL",        # 대형 테크
        "MSFT",        # 대형 테크
        "NVDA",        # 반도체/AI
        "TSLA",        # 고변동성 성장
        "AMZN",        # 빅테크
        "META",        # 소셜미디어
        "JPM",         # 금융
        "XOM",         # 에너지
    ],
    "Crypto": [
        "BTC-USD",
        "ETH-USD",
        "SOL-USD",
        "BNB-USD",
        "XRP-USD",
    ],
}

PROPOSED_MULTIPLES: dict[str, float] = {"KR": 1.5, "US": 2.0, "Crypto": 2.5}
CANDIDATE_MULTIPLES = [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.5]
ATR_PERIOD = 14
LOOKBACK_DAYS = 500   # 약 2년 데이터
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────────────────────
# ATR 계산 (Wilder Smoothing)
# ─────────────────────────────────────────────────────────────

def wilder_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)

    atr = tr.copy().astype(float)
    first_idx = tr.first_valid_index()
    if first_idx is None:
        return pd.Series(dtype=float)
    loc = tr.index.get_loc(first_idx)
    end = loc + period
    if end > len(tr):
        return pd.Series(dtype=float)

    atr.iloc[:end] = np.nan
    atr.iloc[end - 1] = tr.iloc[loc:end].mean()
    for i in range(end, len(tr)):
        atr.iloc[i] = (atr.iloc[i - 1] * (period - 1) + tr.iloc[i]) / period
    return atr


# ─────────────────────────────────────────────────────────────
# 데이터 수집
# ─────────────────────────────────────────────────────────────

def fetch_data(symbols: list[str]) -> dict[str, pd.DataFrame]:
    result = {}
    for sym in symbols:
        try:
            t  = yf.Ticker(sym)
            df = t.history(period=f"{LOOKBACK_DAYS}d", auto_adjust=True)
            if df.empty or len(df) < ATR_PERIOD + 30:
                print(f"  ⚠ 데이터 부족: {sym}")
                continue
            df.columns = [c.capitalize() for c in df.columns]
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
            result[sym] = df
            print(f"  [OK] {sym}: {len(df)}일")
        except Exception as e:
            print(f"  [FAIL] {sym}: {e}")
    return result


# ─────────────────────────────────────────────────────────────
# 시장별 ATR% 통계
# ─────────────────────────────────────────────────────────────

def calc_atr_pct_stats(
    data_map: dict[str, pd.DataFrame],
    market: str,
) -> dict:
    """시장 내 전체 종목의 ATR% 분포 통계를 반환합니다."""
    all_pcts: list[float] = []
    for sym, df in data_map.items():
        atr = wilder_atr(df)
        pct = (atr / df["Close"] * 100).dropna()
        all_pcts.extend(pct.tolist())

    if not all_pcts:
        return {}

    s = pd.Series(all_pcts)
    return {
        "market":  market,
        "count":   len(data_map),
        "n_obs":   len(all_pcts),
        "mean":    round(s.mean(), 2),
        "median":  round(s.median(), 2),
        "std":     round(s.std(), 2),
        "p25":     round(s.quantile(0.25), 2),
        "p75":     round(s.quantile(0.75), 2),
        "p90":     round(s.quantile(0.90), 2),
        "p95":     round(s.quantile(0.95), 2),
        "max":     round(s.max(), 2),
        "all_pcts": s,
    }


# ─────────────────────────────────────────────────────────────
# Stop 시뮬레이션 — False Signal Rate + Whipsaw Rate
# ─────────────────────────────────────────────────────────────

def simulate_trailing_stop(
    df: pd.DataFrame,
    multiple: float,
    highest_high_window: int = 20,
    noise_window: int = 3,       # 이 기간 안에 Stop 터치 → False Signal
    whipsaw_window: int = 5,     # 이 기간 안에 회복 → Whipsaw
) -> dict:
    """
    특정 ATR 배수에서 Chandelier Exit Stop 시뮬레이션.

    Returns
    -------
    {
      'false_signal_rate': % of bars where stop is hit within noise_window,
      'whipsaw_rate':      % of stop-outs where price recovers within whipsaw_window,
      'avg_stop_dist_pct': average stop distance as % of price,
      'n_total':           total simulation bars,
      'n_stop_hits':       total stop hit count,
    }
    """
    atr = wilder_atr(df)
    df2 = df.copy()
    df2["ATR"] = atr
    df2.dropna(inplace=True)

    if len(df2) < highest_high_window + whipsaw_window + 10:
        return {}

    highs   = df2["High"].values
    closes  = df2["Close"].values
    atrs    = df2["ATR"].values
    n       = len(df2)

    stop_dists = []
    false_signals = 0
    whipsaws      = 0
    stop_hits     = 0
    total         = 0

    for i in range(highest_high_window, n - whipsaw_window):
        hh   = highs[i - highest_high_window: i + 1].max()
        stop = hh - atrs[i] * multiple
        dist_pct = (closes[i] - stop) / closes[i] * 100

        if dist_pct <= 0:        # 이미 Stop 아래에 있는 경우 건너뜀
            continue

        stop_dists.append(dist_pct)
        total += 1

        # noise_window 이내 Stop 돌파 여부
        hit_in_noise = False
        for j in range(i + 1, min(i + noise_window + 1, n)):
            if closes[j] < stop:
                hit_in_noise = True
                stop_hits += 1

                # whipsaw_window 이내 가격 회복 여부
                future_max = closes[j: min(j + whipsaw_window + 1, n)].max()
                if future_max > stop:
                    whipsaws += 1
                break

        if hit_in_noise:
            false_signals += 1

    if total == 0:
        return {}

    return {
        "false_signal_rate": round(false_signals / total * 100, 2),
        "whipsaw_rate":      round(whipsaws / max(stop_hits, 1) * 100, 2),
        "avg_stop_dist_pct": round(np.mean(stop_dists), 2),
        "median_stop_dist":  round(np.median(stop_dists), 2),
        "n_total":           total,
        "n_stop_hits":       stop_hits,
    }


def simulate_market(
    data_map: dict[str, pd.DataFrame],
    multiples: list[float],
) -> pd.DataFrame:
    """시장 내 전체 종목에 대해 배수별 시뮬레이션을 실행합니다."""
    rows = []
    for mult in multiples:
        fsr_list, wsr_list, dist_list = [], [], []
        for df in data_map.values():
            r = simulate_trailing_stop(df, mult)
            if r:
                fsr_list.append(r["false_signal_rate"])
                wsr_list.append(r["whipsaw_rate"])
                dist_list.append(r["avg_stop_dist_pct"])
        if not fsr_list:
            continue
        rows.append({
            "multiple":         mult,
            "false_signal_rate": round(np.mean(fsr_list), 2),
            "whipsaw_rate":      round(np.mean(wsr_list), 2),
            "avg_stop_dist_pct": round(np.mean(dist_list), 2),
        })
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────
# 최적 배수 추정 — False Signal Rate ≤ 목표치 를 만족하는 최소 배수
# ─────────────────────────────────────────────────────────────

def find_optimal_multiple(sim_df: pd.DataFrame, target_fsr: float = 5.0) -> float:
    """False Signal Rate ≤ target_fsr % 를 만족하는 최소 배수를 반환합니다."""
    passed = sim_df[sim_df["false_signal_rate"] <= target_fsr]
    if passed.empty:
        return sim_df.loc[sim_df["false_signal_rate"].idxmin(), "multiple"]
    return float(passed["multiple"].iloc[0])


# ─────────────────────────────────────────────────────────────
# 시각화
# ─────────────────────────────────────────────────────────────

def plot_atr_pct_distribution(stats_list: list[dict], save_path: Path) -> None:
    """시장별 ATR% 분포 박스플롯."""
    fig, ax = plt.subplots(figsize=(10, 5))
    data    = [s["all_pcts"].clip(upper=s["p95"] * 2) for s in stats_list]
    labels  = [f"{s['market']}\n(n={s['count']}종목)" for s in stats_list]
    colors  = ["#1f77b4", "#ff7f0e", "#2ca02c"]

    bp = ax.boxplot(data, labels=labels, patch_artist=True, notch=False)
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.6)

    ax.set_ylabel("ATR% (ATR / Close × 100)")
    ax.set_title("시장별 ATR% 분포 (2년 데이터)", fontweight="bold")
    ax.grid(True, axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(save_path, dpi=120, bbox_inches="tight")
    plt.close()
    print(f"  차트 저장: {save_path}")


def plot_false_signal_rate(
    sim_results: dict[str, pd.DataFrame],
    proposed: dict[str, float],
    save_path: Path,
) -> None:
    """시장별 배수 vs False Signal Rate 라인 차트."""
    fig, ax = plt.subplots(figsize=(10, 6))
    colors = {"KR": "#1f77b4", "US": "#ff7f0e", "Crypto": "#2ca02c"}

    for market, sim_df in sim_results.items():
        ax.plot(
            sim_df["multiple"], sim_df["false_signal_rate"],
            label=market, color=colors[market], linewidth=2, marker="o", markersize=5,
        )
        # 제안 배수 표시
        prop_mult = proposed[market]
        prop_row = sim_df[sim_df["multiple"] == prop_mult]
        if not prop_row.empty:
            fsr = prop_row["false_signal_rate"].iloc[0]
            ax.scatter(prop_mult, fsr, color=colors[market], s=150, zorder=5,
                       edgecolors="black", linewidths=1.5)
            ax.annotate(
                f"Proposed({prop_mult}x)\n{fsr:.1f}%",
                (prop_mult, fsr),
                textcoords="offset points", xytext=(8, 8),
                fontsize=8, color=colors[market], fontweight="bold",
            )

    ax.axhline(5.0, color="red", linestyle="--", linewidth=1.2, alpha=0.7,
               label="허용 임계선 (5%)")
    ax.set_xlabel("ATR 배수 (Multiple)")
    ax.set_ylabel("False Signal Rate (%)\n(3거래일 이내 Stop 돌파 비율)")
    ax.set_title("ATR 배수별 False Signal Rate — 시장 비교", fontweight="bold")
    ax.legend()
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(save_path, dpi=120, bbox_inches="tight")
    plt.close()
    print(f"  차트 저장: {save_path}")


# ─────────────────────────────────────────────────────────────
# 보고서 출력
# ─────────────────────────────────────────────────────────────

SEP = "=" * 70

def print_report(
    stats_list: list[dict],
    sim_results: dict[str, pd.DataFrame],
    proposed: dict[str, float],
) -> None:
    print(f"\n{SEP}")
    print("  포트폴리오 ATR 시장별 배수 타당성 검증 리포트")
    print(SEP)

    # ── 1. ATR% 분포 통계 ──────────────────────────────────────
    print("\n[1] 시장별 ATR% 분포 통계")
    print("-" * 70)
    hdr = f"{'시장':<8} {'종목수':>5} {'평균':>6} {'중앙값':>6} {'P25':>6} {'P75':>6} {'P90':>6} {'P95':>6}"
    print(hdr)
    print("-" * 70)
    for s in stats_list:
        print(
            f"{s['market']:<8} {s['count']:>5} "
            f"{s['mean']:>5.2f}% {s['median']:>5.2f}% "
            f"{s['p25']:>5.2f}% {s['p75']:>5.2f}% "
            f"{s['p90']:>5.2f}% {s['p95']:>5.2f}%"
        )

    # ── 2. 제안 배수 시뮬레이션 결과 ──────────────────────────
    print("\n[2] 제안 배수 시뮬레이션 결과")
    print("-" * 70)
    hdr2 = f"{'시장':<8} {'제안배수':>8} {'Stop거리':>10} {'FalseSignal':>13} {'Whipsaw':>10} {'판정':>8}"
    print(hdr2)
    print("-" * 70)
    for s in stats_list:
        market = s["market"]
        mult   = proposed[market]
        sim_df = sim_results[market]
        row    = sim_df[sim_df["multiple"] == mult]
        if row.empty:
            continue
        r = row.iloc[0]
        verdict = "[OK]  적정" if r["false_signal_rate"] <= 5.0 else "[!!] 검토필요"
        print(
            f"{market:<8} {mult:>8.1f}× "
            f"{r['avg_stop_dist_pct']:>8.2f}%  "
            f"{r['false_signal_rate']:>10.2f}%  "
            f"{r['whipsaw_rate']:>8.2f}%  "
            f"{verdict}"
        )

    # ── 3. 배수별 상세 시뮬레이션 테이블 ─────────────────────
    print("\n[3] 시장별 배수별 상세 시뮬레이션")
    for market, sim_df in sim_results.items():
        prop_mult = proposed[market]
        optimal   = find_optimal_multiple(sim_df)
        print(f"\n  [{market}]  제안:{prop_mult}x  추정 최적:{optimal}x")
        print(f"  {'배수':>6} {'Stop거리':>10} {'FalseSignal':>13} {'Whipsaw':>10}")
        print(f"  {'-'*44}")
        for _, r in sim_df.iterrows():
            marker = " <-- 제안" if r["multiple"] == prop_mult else \
                     " <-- 최적" if r["multiple"] == optimal and optimal != prop_mult else ""
            print(
                f"  {r['multiple']:>5.2f}×  "
                f"{r['avg_stop_dist_pct']:>8.2f}%  "
                f"{r['false_signal_rate']:>10.2f}%  "
                f"{r['whipsaw_rate']:>8.2f}%"
                f"{marker}"
            )

    # ── 4. 최종 권장 ───────────────────────────────────────────
    print(f"\n[4] 최종 권장 배수")
    print("-" * 70)
    for market, sim_df in sim_results.items():
        prop_mult = proposed[market]
        optimal   = find_optimal_multiple(sim_df)
        prop_row  = sim_df[sim_df["multiple"] == prop_mult]
        prop_fsr  = prop_row["false_signal_rate"].iloc[0] if not prop_row.empty else 999

        if abs(optimal - prop_mult) < 0.01:
            verdict = f"[OK]  제안값({prop_mult}x) 유지  -- FSR={prop_fsr:.1f}%"
        elif optimal < prop_mult:
            verdict = f"[DOWN] {optimal}x으로 하향 권장  -- 현재FSR={prop_fsr:.1f}%"
        else:
            verdict = f"[UP]   {optimal}x으로 상향 권장  -- 현재FSR={prop_fsr:.1f}%"

        print(f"  {market:<8}: {verdict}")

    print(f"\n{SEP}\n")


# ─────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────

def main() -> None:
    print("\n포트폴리오 ATR 배수 타당성 검증 시작...")
    print(f"대상 심볼: KR {len(SYMBOLS['KR'])}종목 / US {len(SYMBOLS['US'])}종목 / Crypto {len(SYMBOLS['Crypto'])}종목")

    # 1. 데이터 수집
    all_data: dict[str, dict[str, pd.DataFrame]] = {}
    for market, syms in SYMBOLS.items():
        print(f"\n[{market}] 데이터 다운로드 중...")
        all_data[market] = fetch_data(syms)

    # 2. ATR% 통계
    print("\n시장별 ATR% 분포 계산 중...")
    stats_list = []
    for market, data_map in all_data.items():
        if not data_map:
            continue
        stats = calc_atr_pct_stats(data_map, market)
        if stats:
            stats_list.append(stats)

    # 3. 배수별 시뮬레이션
    print("\n배수별 False Signal Rate 시뮬레이션 중...")
    sim_results: dict[str, pd.DataFrame] = {}
    for market, data_map in all_data.items():
        if not data_map:
            continue
        print(f"  [{market}] 시뮬레이션 중... ", end="", flush=True)
        sim_df = simulate_market(data_map, CANDIDATE_MULTIPLES)
        sim_results[market] = sim_df
        print("완료")

    # 4. 보고서 출력
    print_report(stats_list, sim_results, PROPOSED_MULTIPLES)

    # 5. 시각화 저장
    print("차트 저장 중...")
    if stats_list:
        plot_atr_pct_distribution(
            stats_list, OUTPUT_DIR / "atr_pct_distribution.png"
        )
    if sim_results:
        plot_false_signal_rate(
            sim_results, PROPOSED_MULTIPLES, OUTPUT_DIR / "false_signal_rate.png"
        )

    # 6. CSV 저장
    for market, sim_df in sim_results.items():
        sim_df.to_csv(OUTPUT_DIR / f"sim_{market}.csv", index=False)
    print(f"\n분석 완료. 결과 저장: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
