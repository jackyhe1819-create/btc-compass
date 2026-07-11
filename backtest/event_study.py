#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.event_study
====================
信号事件研究：把"评分状态转换"定义成离散买/卖信号事件, 统计每个信号的
独立事件数、前瞻收益分布、胜率、窗口内最大回撤, 与无条件基线对照。
目的: 决定哪些信号有资格上决策面板 (信号卡必须自带这些统计)。

方法论要点 (防前视 / 防重叠):
- 信号在 t 日收盘可知 → 以 t+1 日收盘为入场价 (滞后一天, 保守)
- 事件独立性: 触发后 gap 天内的再次触发并入同一事件段 (不重复计数)
- 去抖: 穿越类信号要求穿越前在另一侧至少 debounce 天
- 前瞻窗口: 90/180/365 日历日; 窗口越出数据末端的事件按窗口剔除
- 基线: 同一评估期内全部交易日的无条件同窗口统计 (信号必须显著优于基线才算数)

用法: cd backtest && python3 event_study.py
输出: output/event_study.md + stdout 摘要
"""

import os
import sys
import json

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evaluate as ev

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")

WINDOWS = [90, 180, 365]
EVAL_START = "2014-01-01"   # 与周期分回测主窗口一致


# ------------------------------------------------------------
# 事件抽取工具
# ------------------------------------------------------------

def episodes(trigger: pd.Series, gap: int) -> list:
    """布尔触发序列 → 独立事件日列表 (触发后 gap 天内的触发并入同段)。"""
    days = list(trigger.index[trigger.fillna(False)])
    out = []
    for d in days:
        if out and (d - out[-1]).days <= gap:
            continue
        out.append(d)
    return out


def cross_up(s: pd.Series, level: float, debounce: int = 10) -> pd.Series:
    """上穿 level: 当日 >= level 且之前 debounce 天全部 < level。"""
    below = s < level
    prev_all_below = below.shift(1).rolling(debounce).min() == 1
    return (s >= level) & prev_all_below


def cross_down(s: pd.Series, level: float, debounce: int = 10) -> pd.Series:
    above = s > level
    prev_all_above = above.shift(1).rolling(debounce).min() == 1
    return (s <= level) & prev_all_above


def fwd_stats(price: pd.Series, days: list, window: int) -> dict:
    """事件日列表 → 前瞻 window 天收益统计 (t+1 收盘入场) + 窗口内最大回撤。"""
    entry = price.shift(-1)          # t+1 收盘
    exitp = price.shift(-(1 + window))
    rets, mdds = [], []
    for d in days:
        if d not in price.index:
            continue
        e, x = entry.get(d), exitp.get(d)
        if e is None or x is None or np.isnan(e) or np.isnan(x):
            continue  # 窗口越界剔除
        rets.append(x / e - 1)
        loc = price.index.get_loc(d)
        seg = price.iloc[loc + 1: loc + 1 + window + 1]
        if len(seg) > 1:
            mdds.append(float((seg / seg.cummax() - 1).min()))
    if not rets:
        return {"n": 0}
    r = np.array(rets)
    return {"n": len(r), "win": float((r > 0).mean() * 100),
            "median": float(np.median(r) * 100), "mean": float(r.mean() * 100),
            "worst": float(r.min() * 100),
            "mdd_med": float(np.median(mdds) * 100) if mdds else float("nan")}


def baseline_stats(price: pd.Series, start: str, window: int) -> dict:
    """无条件基线: 评估期内每个交易日都'入场'的同窗口统计。"""
    p = price.loc[start:]
    fwd = (p.shift(-(1 + window)) / p.shift(-1) - 1).dropna()
    r = fwd.values
    return {"n": len(r), "win": float((r > 0).mean() * 100),
            "median": float(np.median(r) * 100), "mean": float(r.mean() * 100)}


# ------------------------------------------------------------
# 数据装载
# ------------------------------------------------------------

def load():
    s = pd.read_csv(os.path.join(OUT, "scores.csv"), index_col=0, parse_dates=True)
    f = pd.read_csv(os.path.join(OUT, "cycle_factors.csv"), index_col=0, parse_dates=True)
    cm = pd.read_csv(os.path.join(CACHE, "coinmetrics_btc.csv"), parse_dates=["time"]
                     ).set_index("time")
    cm.index = cm.index.tz_localize(None)
    price = s["price"].dropna()
    cycle = s["cycle_score"]
    mvrv = pd.to_numeric(cm["CapMVRVCur"], errors="coerce").reindex(price.index)
    hashrate = pd.to_numeric(cm["HashRate"], errors="coerce").reindex(price.index)
    trend = f["趋势过滤器"].reindex(price.index)
    return price, cycle.reindex(price.index), mvrv, hashrate, trend


# ------------------------------------------------------------
# 信号定义
# ------------------------------------------------------------

def build_signals(price, cycle, mvrv, hashrate, trend):
    sigs = []  # (名称, 方向, 事件日列表, 定义说明)

    c = cycle.loc[EVAL_START:]

    sigs.append(("周期分上穿0.30 (进偏多档)", "买",
                 episodes(cross_up(c, 0.30), gap=60),
                 "评分从偏多档下方站上 0.30, 此前≥10天低于该位; 60天内重复触发并段"))
    sigs.append(("周期分上穿0.15 (进标准档)", "买",
                 episodes(cross_up(c, 0.15), gap=60),
                 "同上, 阈值 0.15"))
    sigs.append(("周期分跌破0 (转负)", "卖/避险",
                 episodes(cross_down(c, 0.0), gap=60),
                 "评分由正转负, 此前≥10天为正; 60天并段"))
    sigs.append(("周期分跌破-0.30 (进防守区)", "卖/避险",
                 episodes(cross_down(c, -0.30), gap=60),
                 "跌入防守区"))

    # 滞回换档事件 (决策层同款规则重放)
    bands = ev.hysteresis_band_indices(c.dropna(), 0.05, 5)
    delta = bands.diff()
    up_days = list(bands.index[delta < 0])      # idx 变小 = 升档
    dn_days = list(bands.index[delta > 0])
    sigs.append(("滞回升档 (决策层上调仓位)", "买",
                 episodes(pd.Series(True, index=pd.DatetimeIndex(up_days)
                                    ).reindex(bands.index, fill_value=False), gap=30),
                 "现网决策面板的'上调目标仓位'动作日 (δ=0.05, N=5)"))
    sigs.append(("滞回降档 (决策层下调仓位)", "卖/避险",
                 episodes(pd.Series(True, index=pd.DatetimeIndex(dn_days)
                                    ).reindex(bands.index, fill_value=False), gap=30),
                 "现网决策面板的'下调目标仓位'动作日"))

    # 链上投降带: MVRV 下穿 1 (全历史可测; STH/Z 共振腿仅近4年有数据, 不单列)
    m = mvrv.loc["2011-01-01":]
    sigs.append(("MVRV跌破1 (链上投降带)", "买(历史级)",
                 episodes(cross_down(m, 1.0, debounce=30), gap=180),
                 "全网平均持仓转浮亏; 30天去抖, 180天并段 — 对应现网 $54k 三线共振位主线"))

    # Hash Ribbons 买点: 投降(sma30<sma60 ≥14天) 后恢复上穿
    sma30 = hashrate.rolling(30).mean()
    sma60 = hashrate.rolling(60).mean()
    below = sma30 < sma60
    capit = below.shift(1).rolling(14).min() == 1     # 此前至少14天投降
    hr_buy = (sma30 >= sma60) & capit
    sigs.append(("Hash Ribbons 恢复上穿 (投降结束)", "买",
                 episodes(hr_buy.loc[EVAL_START:], gap=45),
                 "算力30日均线在≥14天投降后重新上穿60日均线"))

    # 组合: 趋势过滤器翻正 且 周期分 ≥0.15 ("便宜且企稳")
    t = trend.loc[EVAL_START:]
    turn_pos = (t >= 0.5) & (t.shift(1).rolling(10).max() <= 0)   # 此前10天均 ≤0
    combo_buy = turn_pos & (c >= 0.15)
    sigs.append(("趋势翻正 且 周期分≥0.15", "买",
                 episodes(combo_buy, gap=60),
                 "趋势过滤器由非正转 ≥0.5 且当日周期分处于标准档以上"))

    turn_neg = (t <= -0.5) & (t.shift(1).rolling(10).min() >= 0)
    combo_sell = turn_neg & (c < 0.15)
    sigs.append(("趋势翻负 且 周期分<0.15", "卖/避险",
                 episodes(combo_sell, gap=60),
                 "趋势过滤器由非负转 ≤-0.5 且评分已失去标准档支撑"))

    return sigs


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------

def main():
    price, cycle, mvrv, hashrate, trend = load()
    sigs = build_signals(price, cycle, mvrv, hashrate, trend)

    base = {w: baseline_stats(price, EVAL_START, w) for w in WINDOWS}

    lines = ["# 信号事件研究 (2026-07)", "",
             f"数据: {price.index[0].date()} → {price.index[-1].date()} | "
             f"评估期 {EVAL_START} 起 | 入场=信号日+1收盘 | 窗口越界事件剔除", "",
             "## 无条件基线 (同期每日入场)", "",
             "| 窗口 | n | 胜率% | 中位% | 均值% |", "|---|---|---|---|---|"]
    for w in WINDOWS:
        b = base[w]
        lines.append(f"| {w}d | {b['n']} | {b['win']:.0f} | {b['median']:+.1f} | {b['mean']:+.1f} |")
    lines += ["", "## 信号统计", ""]

    summary = []
    for name, side, days, desc in sigs:
        lines += [f"### {name}  ‹{side}›", "", f"- 定义: {desc}",
                  f"- 独立事件数: {len(days)}"
                  + (f" | 触发日: {', '.join(d.strftime('%Y-%m-%d') for d in days)}"
                     if len(days) <= 14 else ""), "",
                  "| 窗口 | n | 胜率% | 中位% | 均值% | 最差% | 窗内中位回撤% | 基线胜率% | 基线中位% |",
                  "|---|---|---|---|---|---|---|---|---|"]
        row_stats = {}
        for w in WINDOWS:
            st = fwd_stats(price, days, w)
            row_stats[w] = st
            if st["n"] == 0:
                lines.append(f"| {w}d | 0 | — | — | — | — | — | {base[w]['win']:.0f} | {base[w]['median']:+.1f} |")
                continue
            lines.append(
                f"| {w}d | {st['n']} | {st['win']:.0f} | {st['median']:+.1f} | {st['mean']:+.1f} "
                f"| {st['worst']:+.1f} | {st['mdd_med']:+.1f} | {base[w]['win']:.0f} | {base[w]['median']:+.1f} |")
        lines.append("")
        summary.append({"name": name, "side": side, "episodes": len(days),
                        "dates": [d.strftime("%Y-%m-%d") for d in days],
                        "stats": {str(w): row_stats[w] for w in WINDOWS}})

    # 上线门槛判定
    lines += ["## 上线门槛判定 (买: n≥8 且 90d或180d 胜率≥65 且超基线≥10pp; "
              "卖: n≥8 且 180d 胜率≤40 或中位≤-10)", ""]
    for s in summary:
        st90, st180 = s["stats"]["90"], s["stats"]["180"]
        ok = False
        why = []
        if s["side"].startswith("买"):
            if s["episodes"] >= 8:
                for w, st in (("90", st90), ("180", st180)):
                    if st.get("n", 0) >= 8 and st.get("win", 0) >= 65 \
                       and st["win"] - base[int(w)]["win"] >= 10:
                        ok = True
                        why.append(f"{w}d 胜率 {st['win']:.0f} vs 基线 {base[int(w)]['win']:.0f}")
            else:
                why.append(f"事件数 {s['episodes']} <8")
        else:
            if s["episodes"] >= 8 and st180.get("n", 0) >= 8 \
               and (st180.get("win", 100) <= 40 or st180.get("median", 0) <= -10):
                ok = True
                why.append(f"180d 胜率 {st180['win']:.0f} / 中位 {st180['median']:+.1f}")
            elif s["episodes"] < 8:
                why.append(f"事件数 {s['episodes']} <8")
        verdict = "✅ 过线" if ok else "❌ 不过线"
        s["pass"] = ok
        lines.append(f"- {verdict} — {s['name']} ({'; '.join(why) if why else '统计不足'})")

    md = "\n".join(lines)
    with open(os.path.join(OUT, "event_study.md"), "w") as fp:
        fp.write(md)
    with open(os.path.join(OUT, "event_study.json"), "w") as fp:
        json.dump(summary, fp, ensure_ascii=False, indent=1)
    print(md)


if __name__ == "__main__":
    main()
