#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.options_study
=======================
DVOL(BTC 期权隐含波动率指数) 4年滚动分位 → 前瞻 BTC 收益的 IC 研究。
判定 DVOL 分位是否够格从"仅展示"升级进战术分评分因子 (决策门, 见
.superpowers/sdd/task-7-brief.md)。

方法:
- 分位序列复刻现网 calc_dvol_percentile / scoring._percentile_score 的口径:
  滚动 1460 天(4年)窗口, 严格 `<` (不含等号), min_periods=365。
- IC 用项目统一的 evaluate.spearman_ic (Spearman 秩相关, 评分高→看多,
  期望 IC>0 的约定); 这里评分是 DVOL 分位, 若高 IV 对应后市走弱, IC 应为负。

⚠️ 诚实声明:
- Deribit DVOL 历史仅自 ~2021-03 起可得, 叠加 1460d/365最小期的暖机, 与价格
  的有效重叠样本实际只有约 3-4 年 (远短于周期分其它因子的 4 年+ 窗口) —
  结论是**试验性**的, 样本量偏小。
- 高 IV 事件在时间上高度聚簇(同一波动 regime 连续多日高分位), 不是独立同分
  布(IID)样本 — 有效自由度远低于表面样本数, 统计显著性需谨慎看待。

用法: cd backtest && python3 options_study.py
"""

import os
import sys
import json
import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data_sources as ds
import evaluate as ev

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
DVOL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                          "btc_web", "btc_dashboard", "data", "dvol_history.json")

HORIZONS = [30, 60, 90]
IC_THRESHOLD = 0.08


# ------------------------------------------------------------
# 数据装载
# ------------------------------------------------------------

def _dvol_series(path: str = DVOL_PATH) -> pd.Series:
    """读取 dvol_history.json → 日频 tz-naive Series(index=date, value=DVOL close)。"""
    with open(path) as f:
        series = json.load(f)["series"]
    idx = pd.to_datetime([datetime.datetime.utcfromtimestamp(ts / 1000).date()
                          for ts, _ in series])
    s = pd.Series([v for _, v in series], index=idx).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    return s


def rolling_pct(x: pd.Series, window: int = 1460) -> pd.Series:
    """滚动 window 天分位数(严格 `<`, 与 scoring._percentile_score /
    calc_dvol_percentile 口径一致), min_periods=365(≈1年暖机)。"""
    return x.rolling(window, min_periods=365).apply(
        lambda w: (w < w.iloc[-1]).sum() / len(w) * 100, raw=False)


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------

def run() -> dict:
    dvol = _dvol_series()
    dvol_pct = rolling_pct(dvol).dropna()

    price = ds.fetch_coinmetrics()["price"].dropna()

    ic_df = ev.spearman_ic(dvol_pct, price, HORIZONS)

    out = {}
    ics = []
    samples = []
    for h, row in zip(HORIZONS, ic_df.itertuples(index=False)):
        ic = None if pd.isna(row.IC) else round(float(row.IC), 3)
        out[f"ic_fwd{h}"] = ic
        out[f"n_fwd{h}"] = int(row.样本数)
        samples.append(int(row.样本数))
        if ic is not None:
            ics.append(ic)

    if ics:
        out["direction"] = "high_iv_bearish" if sum(ics) < 0 else "high_iv_bullish"
    else:
        out["direction"] = None

    strong = any(abs(i) >= IC_THRESHOLD for i in ics)
    consistent = len({i > 0 for i in ics}) == 1 if ics else False
    out["verdict"] = "score" if (strong and consistent) else "display_only"

    out["n_samples"] = min(samples) if samples else 0
    out["dvol_history_span"] = f"{dvol.index[0].date()} → {dvol.index[-1].date()}"
    out["dvol_pct_span"] = (f"{dvol_pct.index[0].date()} → {dvol_pct.index[-1].date()}"
                            if len(dvol_pct) else None)

    honest_note = (
        "DVOL 历史仅自 ~2021-03 起可得(Deribit), 叠加 1460d/365最小期暖机, "
        "有效重叠样本实际只有约 3-4 年 — 结论为试验性, 样本量偏小于其它周期分因子。"
        "高 IV 事件在时间上高度聚簇(同一波动 regime 连续多日高分位), 非独立同分布"
        "(IID)样本, 有效自由度远低于表面样本数, 统计显著性需谨慎看待。"
    )
    out["honest_note"] = honest_note

    # ------------------------------------------------------------
    # 打印可读研究表
    # ------------------------------------------------------------
    print("=" * 72)
    print("DVOL 分位 IC 研究 (期权隐含波动率 → 前瞻 BTC 收益)")
    print("=" * 72)
    print(f"DVOL 原始序列: {out['dvol_history_span']} (n={len(dvol)})")
    print(f"DVOL 分位序列(暖机后可用): {out['dvol_pct_span']} (n={len(dvol_pct)})")
    print(f"BTC 价格序列: {price.index[0].date()} → {price.index[-1].date()} (n={len(price)})")
    print("-" * 72)
    print(f"{'窗口':<8}{'IC':>10}{'样本数':>10}")
    for h in HORIZONS:
        ic = out[f"ic_fwd{h}"]
        ic_str = f"{ic:+.3f}" if ic is not None else "—"
        print(f"{str(h) + 'd':<8}{ic_str:>10}{out[f'n_fwd{h}']:>10}")
    print("-" * 72)
    print(f"方向 direction: {out['direction']}")
    print(f"判定 verdict:   {out['verdict']}  "
          f"(门槛: 任一窗口 |IC|>={IC_THRESHOLD} 且各窗符号一致)")
    print(f"最小样本数 n_samples: {out['n_samples']}")
    print("-" * 72)
    print("⚠️  诚实声明:")
    print(f"    {honest_note}")
    print("=" * 72)

    os.makedirs(OUT, exist_ok=True)
    try:
        with open(os.path.join(OUT, "options_study.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"⚠️ 落盘 output/options_study.json 失败(不影响 run() 结果): {e}")

    return out


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
