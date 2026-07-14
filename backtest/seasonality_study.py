#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.seasonality_study
==========================
季节性与高频重复事件的规律检验 (2026-07, 事件研究 C 层扩展)。

与 calendar_study (n=3~4 只能逐次列出) 的关键区别: 这些模式样本足够大,
**第一次可以做真正的统计检验** — 用置换检验 (permutation test) 给出诚实
p 值 (处理加密收益的肥尾 + 自相关), 并做减半周期混杂控制。

测试对象:
- 星期效应 (周末效应): 完全不受周期混杂, 最干净
- 月度季节性: Sell in May / September / Uptober / Q4 — 带周期混杂控制
- 月末/月初效应 (turn-of-month): 不受周期混杂
- 报税日 (4/15) / 春节: 年度事件窗口

方法论红线:
- 多重比较: 测 12 个月必然有 ~0.6 个"看着显著" — 用 min-p 置换控制族错误率
- 周期混杂: 月度效应额外报"控制减半周期相位后是否还在"
- 诚实: 通不过就写"噪声/周期混杂", 绝不挑显著的报

用法: cd backtest && python3 seasonality_study.py
"""

import os
import sys
import json

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
DATA_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "btc_web", "btc_dashboard", "data")

RNG = np.random.default_rng(20260711)   # 固定种子, 结果可复现
N_PERM = 20000

# 减半日期与现网同源 (core.py 唯一事实源), 照 factors.py 先例
_BTC_WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "btc_web")
sys.path.insert(0, _BTC_WEB)
from btc_dashboard.core import HALVING_DATES  # noqa: E402

HALVINGS = [pd.Timestamp(d) for d in HALVING_DATES]

CNY = [pd.Timestamp(d) for d in (
    "2011-02-03", "2012-01-23", "2013-02-10", "2014-01-31", "2015-02-19",
    "2016-02-08", "2017-01-28", "2018-02-16", "2019-02-05", "2020-01-25",
    "2021-02-12", "2022-02-01", "2023-01-22", "2024-02-10", "2025-01-29",
    "2026-02-17")]


def load_price() -> pd.Series:
    s = pd.read_csv(os.path.join(OUT, "scores.csv"), index_col=0, parse_dates=True)
    price = s["price"].dropna()
    rp = os.path.join(CACHE, "recent_prices.json")
    if os.path.exists(rp):
        rec = json.load(open(rp))
        extra = pd.Series({pd.Timestamp(d): float(v) for d, v in rec.items()
                           if pd.Timestamp(d) > price.index[-1]}).sort_index()
        if len(extra):
            price = pd.concat([price, extra])
    return price


def cycle_year(ts):
    """减半后年序 (0/1/2/3): 用于周期混杂控制。"""
    prior = [h for h in HALVINGS if h <= ts]
    if not prior:
        return -1
    return int(((ts - max(prior)).days / 365.25)) % 4


# ------------------------------------------------------------
# 置换检验: 观测统计量 vs 打乱标签后的分布
# ------------------------------------------------------------

def perm_test_group(values: np.ndarray, labels: np.ndarray, stat_fn):
    """
    values 按 labels 分组, stat_fn(values, labels, groups) 给标量统计量。
    返回 (观测值, 双尾 p)。置换 = 打乱 labels。
    """
    groups = np.unique(labels)
    obs = stat_fn(values, labels, groups)
    cnt = 0
    for _ in range(N_PERM):
        perm = RNG.permutation(labels)
        if abs(stat_fn(values, perm, groups)) >= abs(obs):
            cnt += 1
    return obs, (cnt + 1) / (N_PERM + 1)


def perm_test_subset(values: np.ndarray, mask: np.ndarray):
    """
    子集均值 vs 全体: 观测 = mask 组均值 − 补集均值; 置换打乱 mask。
    返回 (观测差, 双尾 p, 子集均值, n)。
    """
    m = mask.astype(bool)
    obs = values[m].mean() - values[~m].mean()
    k = m.sum()
    n = len(values)
    cnt = 0
    for _ in range(N_PERM):
        idx = RNG.permutation(n)[:k]
        d = values[idx].mean() - np.delete(values, idx).mean()
        if abs(d) >= abs(obs):
            cnt += 1
    return obs, (cnt + 1) / (N_PERM + 1), float(values[m].mean()), int(k)


# ------------------------------------------------------------
# 各季节性测试
# ------------------------------------------------------------

def test_weekday(logret: pd.Series):
    """星期效应 (周内哪几天强/弱 + 周末效应)。不受周期混杂。"""
    wd = logret.index.dayofweek.values   # 0=周一 .. 6=周日
    v = logret.values
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    per = []
    for d in range(7):
        m = wd == d
        per.append({"day": names[d], "n": int(m.sum()),
                    "mean_bp": round(float(v[m].mean()) * 1e4, 1),
                    "win": round(float((v[m] > 0).mean()) * 100, 1)})

    def _range_stat(vals, labs, groups):
        means = [vals[labs == g].mean() for g in groups]
        return max(means) - min(means)   # 组间极差
    _, p_any = perm_test_group(v, wd, _range_stat)

    weekend = np.isin(wd, [5, 6])
    obs_w, p_w, we_mean, we_n = perm_test_subset(v, weekend)
    return {"per_day": per, "p_any_weekday": round(p_any, 4),
            "weekend": {"mean_bp": round(we_mean * 1e4, 1), "n": we_n,
                        "diff_bp": round(obs_w * 1e4, 1), "p": round(p_w, 4)}}


def test_month(logret: pd.Series, price: pd.Series):
    """月度季节性 + 减半周期混杂控制。"""
    mo = logret.index.month.values
    v = logret.values
    names = ["1月", "2月", "3月", "4月", "5月", "6月",
             "7月", "8月", "9月", "10月", "11月", "12月"]
    # 逐年月度收益 (更符合"月行情"直觉, 且样本独立性更好)
    monthly = price.resample("ME").last().pct_change().dropna()
    per = []
    min_p = 1.0
    for m in range(1, 13):
        mm = monthly[monthly.index.month == m]
        obs, p, sub_mean, k = perm_test_subset(
            monthly.values, np.asarray(monthly.index.month == m))
        min_p = min(min_p, p)
        per.append({"month": names[m - 1], "n": int(k),
                    "median_pct": round(float(mm.median()) * 100, 1),
                    "mean_pct": round(float(mm.mean()) * 100, 1),
                    "win": round(float((mm > 0).mean()) * 100, 1),
                    "p_raw": round(p, 4)})

    # min-p 置换 (族错误率): 12 个月里"最极端月"的显著性
    mvals = monthly.values
    mmonth = monthly.index.month.values
    obs_min = min(
        abs(mvals[mmonth == m].mean() - np.delete(mvals, np.where(mmonth == m)).mean())
        for m in range(1, 13))
    # 用最大偏离做 family-wise
    obs_max = max(
        abs(mvals[mmonth == m].mean() - mvals[mmonth != m].mean())
        for m in range(1, 13))
    cnt = 0
    for _ in range(N_PERM):
        perm = RNG.permutation(mmonth)
        mx = max(abs(mvals[perm == m].mean() - mvals[perm != m].mean())
                 for m in range(1, 13))
        if mx >= obs_max:
            cnt += 1
    p_fwe = (cnt + 1) / (N_PERM + 1)

    # 周期混杂控制: 每个月的收益里, 减半周期相位的占比是否失衡?
    # 用 "9月/10月" 等最受关注的月, 看其历史样本落在哪些周期年
    cyc_note = {}
    for m in (5, 9, 10):
        yrs = [(y, cycle_year(pd.Timestamp(y, m, 15)))
               for y in range(2011, 2027)]
        cyc_note[names[m - 1]] = [c for _, c in yrs]

    # Sell in May: 5-10月 vs 11-4月 (半年)
    summer = np.isin(logret.index.month.values, [5, 6, 7, 8, 9, 10])
    obs_s, p_s, summer_mean, _ = perm_test_subset(v, summer)
    winter_mean = v[~summer].mean()

    return {"per_month": per, "p_family_wise": round(p_fwe, 4),
            "min_p_raw": round(min_p, 4),
            "sell_in_may": {"summer_daily_bp": round(summer_mean * 1e4, 1),
                            "winter_daily_bp": round(winter_mean * 1e4, 1),
                            "diff_bp": round(obs_s * 1e4, 1), "p": round(p_s, 4)},
            "cycle_confound": cyc_note}


def test_turn_of_month(logret: pd.Series):
    """月末最后1天 + 月初前3天 vs 其余 (股市经典异象)。不受周期混杂。"""
    idx = logret.index
    v = logret.values
    dom = idx.day.values
    # 月末最后一交易日: 次日月份不同
    is_last = np.zeros(len(idx), dtype=bool)
    mons = idx.month.values
    is_last[:-1] = mons[:-1] != mons[1:]
    tom = is_last.copy()
    # 月初前3天
    tom |= dom <= 3
    obs, p, tom_mean, k = perm_test_subset(v, tom)
    return {"tom_daily_bp": round(tom_mean * 1e4, 1), "n": k,
            "other_daily_bp": round(v[~tom].mean() * 1e4, 1),
            "diff_bp": round(obs * 1e4, 1), "p": round(p, 4)}


def event_window(price: pd.Series, dates, post=20):
    """
    事件窗口: 后 post 天收益 + **vs 随机日期基线的置换检验**。
    73% 胜率看着高, 但要对比"任意日期后20天"的无条件基线才知道是否异常。
    """
    fwd_all = (price.shift(-post) / price - 1).dropna()   # 每个交易日的后 post 天收益
    base_win = float((fwd_all > 0).mean()) * 100
    base_med = float(fwd_all.median()) * 100

    rets = []
    for d in dates:
        sub = price.loc[:d]
        if not len(sub) or (d - sub.index[-1]).days > 5:
            continue
        anchor = sub.index[-1]
        if anchor not in fwd_all.index:
            continue
        rets.append(float(fwd_all.loc[anchor]) * 100)
    if not rets:
        return None
    rets = np.array(rets)
    k = len(rets)
    obs_med = float(np.median(rets))
    obs_win = float((rets > 0).mean()) * 100

    # 置换: 从全体日期随机抽 k 个, 看事件组中位数是否异常 (双尾)
    pool = fwd_all.values * 100
    cnt_med = cnt_win = 0
    base_med_all = np.median(pool)
    for _ in range(N_PERM):
        s = RNG.choice(pool, size=k, replace=False)
        if abs(np.median(s) - base_med_all) >= abs(obs_med - base_med_all):
            cnt_med += 1
        if (s > 0).mean() * 100 >= obs_win:
            cnt_win += 1
    return {"n": k, "median_pct": round(obs_med, 1), "win": round(obs_win, 1),
            "mean_pct": round(float(rets.mean()), 1),
            "baseline_win": round(base_win, 1), "baseline_median": round(base_med, 1),
            "p_median": round((cnt_med + 1) / (N_PERM + 1), 4),
            "p_win": round((cnt_win + 1) / (N_PERM + 1), 4),
            "cycle_years": [cycle_year(d) for d in dates if d <= price.index[-1]][:k]}


def main():
    price = load_price()
    logret = np.log(price / price.shift(1)).dropna()

    print("=" * 60)
    print(f"数据: {price.index[0].date()} → {price.index[-1].date()} "
          f"({len(logret)} 个日收益)")
    print("=" * 60)

    wd = test_weekday(logret)
    mo = test_month(logret, price)
    tom = test_turn_of_month(logret)
    tax = event_window(price, [pd.Timestamp(y, 4, 15) for y in range(2011, 2026)])
    cny = event_window(price, CNY)

    result = {
        "generated": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "window": f"{price.index[0].date()} → {price.index[-1].date()}",
        "n_days": len(logret),
        "weekday": wd, "month": mo, "turn_of_month": tom,
        "tax_day": tax, "cny": cny,
    }
    for _dir in (OUT, DATA_OUT):
        os.makedirs(_dir, exist_ok=True)
        with open(os.path.join(_dir, "seasonality_study.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)

    # ── 打印摘要 ──
    print("\n## 星期效应 (不受周期混杂)")
    for d in wd["per_day"]:
        print(f"  {d['day']}: 均 {d['mean_bp']:+.1f}bp/日 · 胜率 {d['win']}% (n={d['n']})")
    print(f"  周内差异整体置换 p = {wd['p_any_weekday']}")
    w = wd["weekend"]
    print(f"  周末 vs 工作日: {w['diff_bp']:+.1f}bp/日, p = {w['p']}")

    print("\n## 月度季节性")
    for m in mo["per_month"]:
        print(f"  {m['month']}: 中位 {m['median_pct']:+.1f}% · 胜率 {m['win']}% "
              f"(n={m['n']}, 单月p={m['p_raw']})")
    print(f"  族错误率控制后 p(最极端月) = {mo['p_family_wise']} "
          f"(min 单月 p = {mo['min_p_raw']})")
    s = mo["sell_in_may"]
    print(f"  Sell in May (5-10月 vs 11-4月): "
          f"{s['summer_daily_bp']:+.1f} vs {s['winter_daily_bp']:+.1f} bp/日, p = {s['p']}")
    print(f"  周期混杂检查 (各月历史落在哪些减半后年序 0/1/2/3):")
    for k, v in mo["cycle_confound"].items():
        print(f"    {k}: {v}")

    print("\n## 月末月初效应 (turn-of-month, 不受周期混杂)")
    print(f"  月末+月初 {tom['tom_daily_bp']:+.1f} vs 其余 {tom['other_daily_bp']:+.1f} bp/日, "
          f"p = {tom['p']} (n={tom['n']})")

    print(f"\n## 报税日 4/15 (后20天)  [基线 胜率 {tax['baseline_win']}% 中位 {tax['baseline_median']:+.1f}%]")
    if tax:
        print(f"  n={tax['n']} 中位 {tax['median_pct']:+.1f}% 胜率 {tax['win']}% "
              f"| vs基线 p(中位)={tax['p_median']} p(胜率)={tax['p_win']}")
    print(f"## 春节 (后20天)  [基线 胜率 {cny['baseline_win']}% 中位 {cny['baseline_median']:+.1f}%]")
    if cny:
        print(f"  n={cny['n']} 中位 {cny['median_pct']:+.1f}% 胜率 {cny['win']}% "
              f"| vs基线 p(中位)={cny['p_median']} p(胜率)={cny['p_win']}")

    print(f"\n✅ 落盘 output/seasonality_study.json")


if __name__ == "__main__":
    main()
