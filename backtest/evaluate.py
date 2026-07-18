#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.evaluate
=================
评分效果评估:
1. 分档前瞻收益表 (周期分 30/90/180/365d, 战术分 7/14/30d)
2. 评分 vs 前瞻收益 秩相关 IC (Spearman, pandas rank 实现, 不依赖 scipy)
3. 周期分档位 → 仓位映射策略 净值 vs HODL / 恒定50%仓
4. 简单基准横比 (200DMA / 减半 / 纯估值 / 估值+趋势 / 恒定均仓) — 证伪对照
5. 移动块自助 (block bootstrap) 给 IC 与策略 Sharpe 加 95% 置信区间
"""

import os
import sys

import numpy as np
import pandas as pd

# 引入现网估值常量与减半日期, 与生产同源
# (test_consistency.test_halving_dates_single_source 守卫: 减半日期字面量只许在 core.py)
_BTC_WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "btc_web")
if _BTC_WEB not in sys.path:
    sys.path.insert(0, _BTC_WEB)
from btc_dashboard.core import (GENESIS_DATE, AHR999_A, AHR999_B,  # noqa: E402
                                HALVING_DATES)

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


def band_score_bounds() -> dict:
    """周期/战术档位的评分下界向量 (排除 -inf 哨兵) —— band_stats.json 自描述元数据。

    run_backtest.py 生成 band_stats.json 时把本函数返回值展开进顶层
    (``**ev.band_score_bounds()``), 使落盘的档位边界随评分阈值一起刷新;
    tests/test_consistency.py 再把落盘边界与 decision.CYCLE_BANDS/TACTICAL_BANDS
    逐一对账。此前 band_stats 只被档位仓位标签键集合钉住, 微调评分下界而保标签、
    漏重跑回测时全绿而分档前瞻收益静默过期 —— 本字段复刻 hysteresis 那条数据侧守卫补缺。
    """
    return {
        "cycle_bounds": [lo for lo, *_ in CYCLE_BANDS if lo != float("-inf")],
        "tactical_bounds": [lo for lo, *_ in TACTICAL_BANDS if lo != float("-inf")],
    }


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
            "avg_pos": avg_pos, "position": pos, "daily_ret": strat_ret}


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


# ------------------------------------------------------------
# 简单基准策略 (证伪对照)
# ------------------------------------------------------------
# 全部仅由 price 派生、无 look-ahead, 与周期分档位策略并列, 回答两个最扎心的问题:
#   "14 因子体系比一根 200DMA / 单个估值因子好多少?" 与 "择时是否比恒定均仓多赚?"
# Sharpe/最大回撤对恒定杠杆不变, 故按 Sharpe+maxDD+CAGR 对齐即诚实横比 (不缩放净值);
# 各基准平均仓位与本系统是否可比在横比表单列标注 (见 run_benchmarks)。
# 这些基准近乎无参 (200日/18月/滚动分位皆约定俗成或无拟合阈值), 是比档位阈值更诚实的参照。

def _rolling_pct(series: pd.Series, window: int = 1460, minp: int = 365) -> pd.Series:
    """滚动分位: 窗口内严格小于当前值的占比 (0..1)。纯排名, 无拟合阈值。"""
    s = series.dropna()
    if s.empty:
        return pd.Series(np.nan, index=series.index)
    r = s.rolling(window, min_periods=minp).rank(method="min")
    n = s.rolling(window, min_periods=minp).count()
    pct = (r - 1) / n
    return pct.reindex(series.index)


def benchmark_positions(price: pd.Series, const_pos: float = 0.41) -> dict:
    """返回 {基准名: 目标仓位序列(0..1)}, 全部仅由 price 派生 (含足够前史预热)。

    - 200日均线开关: 收盘 > 200DMA → 满仓, 否则空仓 (经典趋势跟随)
    - 减半规则: 减半后 0~18 个月满仓, 其余空仓 (民间四年周期先验)
    - 纯估值(幂律分位): 逆向, 仓位 = 1 − 幂律走廊滚动分位 (越低估越重仓; 单因子对照)
    - 估值+趋势: 上两者均值 (2 因子 vs 14 因子对照)
    - 恒定均仓: 等均仓 null, 隔离"择时是否比恒定持有均仓多赚"
    仓位次日生效 (防前视) 由调用方 (run_benchmarks) 统一 shift, 本函数只出当日目标仓位。
    """
    ma200 = price.rolling(200).mean()

    # 200 日均线开关
    ma_switch = (price > ma200).astype(float)
    ma_switch[ma200.isna()] = np.nan

    # 减半规则 (减半日期单一事实源 = core.HALVING_DATES)
    halvings = pd.Series([pd.Timestamp(d) for d in HALVING_DATES])
    last_h = pd.Series(
        [halvings[halvings <= d].max() if (halvings <= d).any() else pd.NaT
         for d in price.index], index=price.index)
    months_since = (pd.Series(price.index, index=price.index) - last_h).dt.days / 30.44
    halving_rule = (months_since < 18).astype(float)
    halving_rule[last_h.isna()] = np.nan

    # 纯估值单因子: 幂律走廊 (价格/幂律公允价), 分位逆向。刻意独立于评分管道重算,
    # 使基准不依赖被评估系统本身 (与 factors.py:110-114 同公式, 但不共享代码是有意的对照独立性)。
    days = (price.index - pd.Timestamp(GENESIS_DATE)).days.values.astype(float)
    with np.errstate(invalid="ignore", divide="ignore"):
        fair = 10 ** (AHR999_B * np.log10(np.where(days > 0, days, np.nan)) + AHR999_A)
    powerlaw = pd.Series(price.values / fair, index=price.index)
    val_pos = 1 - _rolling_pct(powerlaw)

    # 估值 + 趋势 两因子
    val_trend = (val_pos + ma_switch) / 2

    return {
        "200日均线开关": ma_switch,
        "减半规则(≤18月)": halving_rule,
        "纯估值(幂律分位)": val_pos,
        "估值+趋势(2因子)": val_trend,
        f"恒定{const_pos:.0%}均仓": pd.Series(const_pos, index=price.index),
    }


def _comparability(avg_pos: float, ref_pos: float, tol: float = 0.05) -> str:
    """基准与本系统平均仓位是否可比 (诚实标注)。

    Sharpe/最大回撤对恒定杠杆不变, 故风险调整排序始终可比; 但绝对收益与回撤幅度会随
    平均仓位缩放, 均仓差异大时须提醒读者别直接比 CAGR 绝对值。
    """
    d = avg_pos - ref_pos
    return "≈可比" if abs(d) <= tol else f"仓位{d:+.0%}"


def run_benchmarks(price: pd.Series, ref_index: pd.Index,
                   ref_avg_pos: float, const_pos: float = 0.41) -> pd.DataFrame:
    """简单基准横比表: 各基准仓位次日生效, 在 ref_index (本系统策略同窗) 上算
    平均仓位 / CAGR / Sharpe / 最大回撤, 并标注与本系统平均仓位是否可比。"""
    ret = price.pct_change().fillna(0)
    rows = []
    for name, pos in benchmark_positions(price, const_pos).items():
        sig = pos.shift(1).clip(lower=0, upper=1)
        df = pd.DataFrame({"pos": sig, "ret": ret}).reindex(ref_index).dropna()
        sr = df["pos"] * df["ret"]
        eq = (1 + sr).cumprod()
        m = strategy_metrics(eq, sr)
        avg = float(df["pos"].mean())
        rows.append({"策略": name, "平均仓位%": avg * 100, "CAGR%": m["CAGR%"],
                     "Sharpe": m["Sharpe"], "最大回撤%": m["最大回撤%"],
                     "vs系统均仓": _comparability(avg, ref_avg_pos)})
    return pd.DataFrame(rows)


# ------------------------------------------------------------
# 移动块自助置信区间 (block bootstrap)
# ------------------------------------------------------------
# 长窗前瞻样本高度重叠 (365d 相邻样本几乎全共享), 名义天数远大于有效独立观测;
# 移动块自助 (Künsch 1989) 重采样连续块以保留短程自相关, 把"有效 N 很小"从散文
# 变成一个诚实 (通常很宽) 的 95% CI。纯重采样, 无新数据源。

def block_bootstrap_ci(data, stat_fn, block: int, n_boot: int = 1000,
                       ci: float = 0.95, seed: int = 42):
    """对序列 (可为二维: 逐行为一次观测) 做移动块自助, 返回统计量 (lo, hi) 分位 CI。

    block: 块长 (IC 用前瞻窗口 h 吸收重叠自相关; Sharpe 用 ~n**(1/3))。
    stat_fn: 接收重采样后的数组 (与 data 同列形状), 返回标量; 返回非有限值将被丢弃。
    seed 固定 → 报告可复现。
    """
    arr = np.asarray(data, dtype=float)
    n = arr.shape[0]
    if n < 30:
        return (float("nan"), float("nan"))
    block = int(max(1, min(block, n)))
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    max_start = n - block
    offs = np.arange(block)
    stats = []
    for _ in range(n_boot):
        starts = rng.integers(0, max_start + 1, size=n_blocks)
        idx = (starts[:, None] + offs[None, :]).reshape(-1)[:n]
        s = stat_fn(arr[idx])
        if s is not None and np.isfinite(s):
            stats.append(s)
    if not stats:
        return (float("nan"), float("nan"))
    lo, hi = np.percentile(stats, [(1 - ci) / 2 * 100, (1 + ci) / 2 * 100])
    return (float(lo), float(hi))


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    """Spearman 秩相关 (pandas rank 处理并列, 不依赖 scipy)。"""
    ra = pd.Series(a).rank().values
    rb = pd.Series(b).rank().values
    ra = ra - ra.mean()
    rb = rb - rb.mean()
    denom = float(np.sqrt((ra ** 2).sum() * (rb ** 2).sum()))
    return float((ra * rb).sum() / denom) if denom > 0 else float("nan")


def ic_bootstrap_ci(score: pd.Series, price: pd.Series, h: int,
                    n_boot: int = 1000, seed: int = 42):
    """周期/战术分 IC 的移动块自助 95% CI (块长 = 前瞻窗口 h, 吸收重叠自相关)。"""
    px = price.reindex(score.index)
    fwd = px.shift(-h) / px - 1
    pair = pd.DataFrame({"s": score, "f": fwd}).dropna()
    if len(pair) < 50:
        return (float("nan"), float("nan"))
    return block_bootstrap_ci(
        pair.values, lambda rows: _spearman(rows[:, 0], rows[:, 1]),
        block=h, n_boot=n_boot, seed=seed)


def sharpe_bootstrap_ci(daily_ret: pd.Series, block: int = None,
                        n_boot: int = 1000, seed: int = 42):
    """策略 Sharpe 的移动块自助 95% CI (块长缺省 ~n**(1/3), 保留持仓持续性)。"""
    r = pd.Series(daily_ret).dropna().values
    if len(r) < 60:
        return (float("nan"), float("nan"))
    if block is None:
        block = max(5, int(round(len(r) ** (1 / 3))))

    def _sharpe(x):
        vol = x.std()
        return float(x.mean() * np.sqrt(365) / vol) if vol > 0 else float("nan")

    return block_bootstrap_ci(r, _sharpe, block=block, n_boot=n_boot, seed=seed)


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
