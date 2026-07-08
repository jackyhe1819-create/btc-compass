#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.evaluate
=================
评分效果评估:
1. 分档前瞻收益表 (周期分 30/90/180/365d, 战术分 7/14/30d)
2. 评分 vs 前瞻收益 秩相关 IC (Spearman, pandas rank 实现, 不依赖 scipy)
3. 周期分档位 → 仓位映射策略 净值 vs HODL / 恒定50%仓
"""

import numpy as np
import pandas as pd

# 周期分档位 (scoring.cycle_recommendation 阈值, 2026-07 按评分分布分位数重标定) → 档位中值仓位
CYCLE_BANDS = [
    (0.45, float("inf"), "重仓区 80-100%", 0.90),
    (0.30, 0.45, "偏多配置 60-80%", 0.70),
    (0.15, 0.30, "标准配置 40-60%", 0.50),
    (0.00, 0.15, "中性观望 30-50%", 0.40),
    (-0.12, 0.00, "减配 15-30%", 0.225),
    (-0.30, -0.12, "低配 5-15%", 0.10),
    (float("-inf"), -0.30, "防守区 0-5%", 0.025),
]

# 战术分档位 (scoring.tactical_recommendation 阈值, 2026-07 重标定)
TACTICAL_BANDS = [
    (0.25, float("inf"), "入场窗口 (≥0.25)"),
    (0.10, 0.25, "逢低分批 (0.1~0.25)"),
    (-0.10, 0.10, "等待信号 (-0.1~0.1)"),
    (-0.35, -0.10, "谨慎 (-0.35~-0.1)"),
    (float("-inf"), -0.35, "杠杆拥挤 (<-0.35)"),
]


def band_label(score, bands):
    for lo, hi, label, *_ in bands:
        if lo <= score < hi or (hi == float("inf") and score >= lo):
            return label
    return "—"


def band_position(score):
    for lo, hi, _, pos in CYCLE_BANDS:
        if lo <= score < hi or (hi == float("inf") and score >= lo):
            return pos
    return 0.4


def forward_return_table(score: pd.Series, price: pd.Series,
                         horizons, bands) -> pd.DataFrame:
    """分档 × 前瞻窗口 的均值/中位数/胜率/样本数。"""
    df = pd.DataFrame({"score": score}).dropna()
    df["band"] = df["score"].map(lambda s: band_label(s, bands))
    px = price.reindex(score.index)
    rows = []
    order = [b[2] for b in bands]
    for h in horizons:
        fwd = px.shift(-h) / px - 1
        df[f"fwd{h}"] = fwd
        g = df.dropna(subset=[f"fwd{h}"]).groupby("band")[f"fwd{h}"]
        stats = g.agg(["count", "mean", "median", lambda x: (x > 0).mean()])
        stats.columns = ["样本数", "均值", "中位数", "胜率"]
        for band in order:
            if band in stats.index:
                s = stats.loc[band]
                rows.append({"档位": band, "窗口": f"{h}d",
                             "样本数": int(s["样本数"]),
                             "均值%": s["均值"] * 100,
                             "中位数%": s["中位数"] * 100,
                             "胜率%": s["胜率"] * 100})
    return pd.DataFrame(rows)


def spearman_ic(score: pd.Series, price: pd.Series, horizons) -> pd.DataFrame:
    """评分与前瞻收益的 Spearman 秩相关 (评分高→看多, 期望 IC>0)。"""
    px = price.reindex(score.index)
    rows = []
    for h in horizons:
        fwd = px.shift(-h) / px - 1
        pair = pd.DataFrame({"s": score, "f": fwd}).dropna()
        if len(pair) < 50:
            rows.append({"窗口": f"{h}d", "IC": np.nan, "样本数": len(pair)})
            continue
        ic = pair["s"].rank().corr(pair["f"].rank())
        rows.append({"窗口": f"{h}d", "IC": ic, "样本数": len(pair)})
    return pd.DataFrame(rows)


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    return float((equity / peak - 1).min())


def strategy_metrics(equity: pd.Series, daily_ret: pd.Series) -> dict:
    n_years = (equity.index[-1] - equity.index[0]).days / 365.25
    total = float(equity.iloc[-1] / equity.iloc[0] - 1)
    cagr = (1 + total) ** (1 / n_years) - 1 if n_years > 0 else np.nan
    vol = float(daily_ret.std() * np.sqrt(365))
    sharpe = float(daily_ret.mean() * 365 / vol) if vol > 0 else np.nan
    return {"总收益%": total * 100, "CAGR%": cagr * 100, "年化波动%": vol * 100,
            "Sharpe": sharpe, "最大回撤%": max_drawdown(equity) * 100}


def run_cycle_strategy(score: pd.Series, price: pd.Series) -> dict:
    """
    档位仓位策略: 当日收盘评分 → 次日起持仓 (shift 1 防前视)。
    基准: HODL(100%) 与 恒定50%仓。
    """
    common = pd.DataFrame({"score": score, "price": price}).dropna()
    px = common["price"]
    ret = px.pct_change().fillna(0)
    pos = common["score"].map(band_position).shift(1).fillna(0.4)

    strat_ret = pos * ret
    bench50_ret = 0.5 * ret

    eq = pd.DataFrame({
        "策略": (1 + strat_ret).cumprod(),
        "HODL": (1 + ret).cumprod(),
        "恒定50%": (1 + bench50_ret).cumprod(),
    }, index=common.index)

    turnover = float(pos.diff().abs().sum())
    metrics = {
        "策略": strategy_metrics(eq["策略"], strat_ret),
        "HODL": strategy_metrics(eq["HODL"], ret),
        "恒定50%": strategy_metrics(eq["恒定50%"], bench50_ret),
    }
    avg_pos = float(pos.mean())
    return {"equity": eq, "metrics": metrics, "turnover": turnover,
            "avg_pos": avg_pos, "position": pos}


def hysteresis_band_indices(score: pd.Series, delta: float = 0.05,
                            confirm: int = 5) -> pd.Series:
    """
    决策层滞回换档: 分数须越过当前档位边界 ±delta 且新档位连续 confirm 天
    保持, 才切换生效档。消除边界附近的日频往返换档 (基线 12 年换档 787 次)。
    参数取自 δ∈[0.03,0.06]×N∈[3,7] 网格的平台中部 (非单点最优, 防过拟合)。
    返回逐日生效档位索引 (CYCLE_BANDS 下标)。
    """
    def _idx(s):
        for i, (lo, hi, _, _) in enumerate(CYCLE_BANDS):
            if lo <= s < hi or (hi == float("inf") and s >= lo):
                return i
        return 3

    vals = score.values
    out = np.empty(len(vals), dtype=int)
    cur = _idx(vals[0])
    pending, pend_days = cur, 0
    for i, s in enumerate(vals):
        lo, hi, _, _ = CYCLE_BANDS[cur]
        lo_x = lo - delta if lo != float("-inf") else lo
        hi_x = hi + delta if hi != float("inf") else hi
        cand = _idx(s) if (s < lo_x or s >= hi_x) else cur
        if cand != cur:
            if cand == pending:
                pend_days += 1
            else:
                pending, pend_days = cand, 1
            if pend_days >= confirm:
                cur, pend_days = cand, 0
        else:
            pending, pend_days = cur, 0
        out[i] = cur
    return pd.Series(out, index=score.index)


def run_cycle_strategy_hysteresis(score: pd.Series, price: pd.Series,
                                  delta: float = 0.05, confirm: int = 5,
                                  cost: float = 0.001) -> dict:
    """
    滞回档位仓位策略 (决策层现网同款规则), 计单边 cost 交易成本。
    与 run_cycle_strategy 同结构, 另含换档次数。
    """
    common = pd.DataFrame({"score": score, "price": price}).dropna()
    px = common["price"]
    ret = px.pct_change().fillna(0)
    bands = hysteresis_band_indices(common["score"], delta, confirm)
    pos = bands.map(lambda i: CYCLE_BANDS[i][3]).shift(1).fillna(0.4)

    dpos = pos.diff().abs().fillna(0)
    strat_ret = pos * ret - dpos * cost
    eq = pd.DataFrame({"策略": (1 + strat_ret).cumprod()}, index=common.index)

    n_switches = int((bands.diff() != 0).sum()) - 1
    return {
        "equity": eq, "metrics": {"策略": strategy_metrics(eq["策略"], strat_ret)},
        "turnover": float(dpos.sum()), "avg_pos": float(pos.mean()),
        "position": pos, "bands": bands, "n_switches": n_switches,
    }


def md_table(df: pd.DataFrame, floatfmt="{:+.1f}") -> str:
    """DataFrame → markdown 表格 (数值列格式化)。"""
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |",
             "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, row in df.iterrows():
        cells = []
        for c in cols:
            v = row[c]
            if isinstance(v, (int, np.integer)):
                cells.append(f"{v}")
            elif isinstance(v, (float, np.floating)):
                if np.isnan(v):
                    cells.append("—")
                elif c in ("IC",):
                    cells.append(f"{v:+.3f}")
                else:
                    cells.append(floatfmt.format(v))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)
