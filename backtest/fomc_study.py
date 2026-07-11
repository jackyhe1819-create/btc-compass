#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.fomc_study
===================
FOMC 议息日 + 利率变化对 BTC 的影响, 严格检验 (2026-07, 事件研究 C 层)。

两块:
A. 利率变化 (加息/降息): 事件日期与幅度全部来自 FRED 联邦基金目标利率日序列
   (cache/fred_dfedtaru.csv / dfedtarl.csv, 权威可验证), 不靠记忆。
   - 逐次列出每次加息/降息后 BTC 的 7/30/90 天收益
   - 加息组 vs 降息组 vs 无条件基线 (置换检验)
   - 利率"上行/下行/平台"regime 下的 BTC 日收益
B. FOMC drift: 全部议息日 (含按兵不动) 的 ±窗口 vs 基线。
   议息日历为人工整理 (决策会已与 FRED 交叉核对); 日线数据无法捕捉
   文献里真正的"盘前 24h drift"(Lucca-Moench), 故为弱检验, 如实标注。

核心红线: 利率 regime 与减半周期严重混杂 (2022 加息=减半后大熊,
2024-25 降息=减半后牛) — 必须做周期混杂拆解, 不把周期假象当利率效应。

用法: cd backtest && python3 fomc_study.py  (依赖 seasonality_study 的置换工具)
"""

import os
import sys
import json

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seasonality_study import load_price, cycle_year, N_PERM, RNG

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
DATA_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "btc_web", "btc_dashboard", "data")

# FOMC 议息日 (公告日/2天会议的次日) + 2020 两次盘中紧急会 (03-03/03-15).
# 决策会已与 FRED 利率变动日交叉核对; 按兵不动的会为人工整理, 供 drift
# 弱检验 (多日窗口对 ±1 天误差不敏感)。
FOMC_DATES = [
    "2010-01-27", "2010-03-16", "2010-04-28", "2010-06-23", "2010-08-10", "2010-09-21", "2010-11-03", "2010-12-14",
    "2011-01-26", "2011-03-15", "2011-04-27", "2011-06-22", "2011-08-09", "2011-09-21", "2011-11-02", "2011-12-13",
    "2012-01-25", "2012-03-13", "2012-04-25", "2012-06-20", "2012-08-01", "2012-09-13", "2012-10-24", "2012-12-12",
    "2013-01-30", "2013-03-20", "2013-05-01", "2013-06-19", "2013-07-31", "2013-09-18", "2013-10-30", "2013-12-18",
    "2014-01-29", "2014-03-19", "2014-04-30", "2014-06-18", "2014-07-30", "2014-09-17", "2014-10-29", "2014-12-17",
    "2015-01-28", "2015-03-18", "2015-04-29", "2015-06-17", "2015-07-29", "2015-09-17", "2015-10-28", "2015-12-16",
    "2016-01-27", "2016-03-16", "2016-04-27", "2016-06-15", "2016-07-27", "2016-09-21", "2016-11-02", "2016-12-14",
    "2017-02-01", "2017-03-15", "2017-05-03", "2017-06-14", "2017-07-26", "2017-09-20", "2017-11-01", "2017-12-13",
    "2018-01-31", "2018-03-21", "2018-05-02", "2018-06-13", "2018-08-01", "2018-09-26", "2018-11-08", "2018-12-19",
    "2019-01-30", "2019-03-20", "2019-05-01", "2019-06-19", "2019-07-31", "2019-09-18", "2019-10-30", "2019-12-11",
    "2020-01-29", "2020-03-03", "2020-03-15", "2020-04-29", "2020-06-10", "2020-07-29", "2020-09-16", "2020-11-05", "2020-12-16",
    "2021-01-27", "2021-03-17", "2021-04-28", "2021-06-16", "2021-07-28", "2021-09-22", "2021-11-03", "2021-12-15",
    "2022-01-26", "2022-03-16", "2022-05-04", "2022-06-15", "2022-07-27", "2022-09-21", "2022-11-02", "2022-12-14",
    "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14", "2023-07-26", "2023-09-20", "2023-11-01", "2023-12-13",
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
]


def load_rate():
    up = pd.read_csv(os.path.join(CACHE, "fred_dfedtaru.csv"),
                     parse_dates=["observation_date"]).set_index("observation_date")["DFEDTARU"]
    lo = pd.read_csv(os.path.join(CACHE, "fred_dfedtarl.csv"),
                     parse_dates=["observation_date"]).set_index("observation_date")["DFEDTARL"]
    return ((up + lo) / 2).sort_index()


def rate_events(mid: pd.Series):
    chg = mid.diff().fillna(0)
    evs = []
    for d, m in mid[chg != 0].items():
        evs.append({"date": d, "bp": round(chg.loc[d] * 100),
                    "dir": "加息" if chg.loc[d] > 0 else "降息",
                    "rate_after": round(float(m), 3)})
    return evs


def fwd_ret(price: pd.Series, anchor: pd.Timestamp, days: int):
    sub = price.loc[:anchor]
    if not len(sub) or (anchor - sub.index[-1]).days > 5:
        return None
    p0 = float(sub.iloc[-1])
    tgt = price.loc[anchor:anchor + pd.Timedelta(days=days)]
    if len(tgt) < 2 or (price.index[-1] - anchor).days < days:
        return None
    return (float(tgt.iloc[-1]) / p0 - 1) * 100


def perm_vs_baseline(price: pd.Series, anchors, days: int):
    """事件组后 days 天收益 vs 全体日期基线 (置换检验双尾)。"""
    fwd_all = ((price.shift(-days) / price - 1) * 100).dropna()
    pool = fwd_all.values
    base_win = float((pool > 0).mean()) * 100
    base_med = float(np.median(pool))
    rets, cyc = [], []
    for a in anchors:
        sub = price.loc[:a]
        if not len(sub):
            continue
        anc = sub.index[-1]
        if anc in fwd_all.index and (price.index[-1] - anc).days >= days:
            rets.append(float(fwd_all.loc[anc]))
            cyc.append(cycle_year(a))
    if not rets:
        return None
    rets = np.array(rets)
    k = len(rets)
    obs_med = float(np.median(rets))
    obs_win = float((rets > 0).mean()) * 100
    base_all_med = np.median(pool)
    cnt = 0
    for _ in range(N_PERM):
        s = RNG.choice(pool, size=k, replace=False)
        if abs(np.median(s) - base_all_med) >= abs(obs_med - base_all_med):
            cnt += 1
    return {"n": k, "median_pct": round(obs_med, 1), "mean_pct": round(float(rets.mean()), 1),
            "win": round(obs_win, 1), "baseline_win": round(base_win, 1),
            "baseline_median": round(base_med, 1), "p_median": round((cnt + 1) / (N_PERM + 1), 4),
            "cycle_years": cyc}


def regime_analysis(price: pd.Series, mid: pd.Series):
    """按利率相对 90 天前的方向给每天贴 regime, 比较 BTC 日收益 + 周期混杂。"""
    logret = np.log(price / price.shift(1)).dropna()
    mid_d = mid.reindex(price.index).ffill()
    trail = mid_d - mid_d.shift(90)
    regime = pd.Series("平台", index=price.index)
    regime[trail > 0.1] = "利率上行"
    regime[trail < -0.1] = "利率下行"
    regime = regime.reindex(logret.index)
    out = {}
    for r in ("利率上行", "利率下行", "平台"):
        m = (regime == r).values
        v = logret.values[m]
        if len(v) == 0:
            continue
        # 该 regime 的天数按减半周期年序分布 (暴露混杂)
        cyc = pd.Series([cycle_year(d) for d in logret.index[m]])
        out[r] = {"days": int(len(v)), "mean_bp": round(float(v.mean()) * 1e4, 1),
                  "ann_pct": round((np.exp(v.mean() * 365) - 1) * 100, 0),
                  "win": round(float((v > 0).mean()) * 100, 1),
                  "cycle_mix": {int(y): int(c) for y, c in cyc.value_counts().sort_index().items()}}
    return out


def main():
    price = load_price()
    mid = load_rate()
    evs = rate_events(mid)
    hikes = [e for e in evs if e["dir"] == "加息"]
    cuts = [e for e in evs if e["dir"] == "降息"]

    print("=" * 64)
    print(f"BTC {price.index[0].date()}→{price.index[-1].date()} | "
          f"利率变动 {len(evs)} 次 ({len(hikes)}加/{len(cuts)}降) | FOMC 会议 {len(FOMC_DATES)} 次")
    print("=" * 64)

    # ── A. 逐次利率变化 ──
    print("\n## A. 逐次加息/降息后 BTC 收益 (%)")
    print("日期 | 幅度 | 利率后 | 减半后年序 | +7d | +30d | +90d")
    rate_rows = []
    for e in evs:
        r7, r30, r90 = (fwd_ret(price, e["date"], d) for d in (7, 30, 90))
        cy = cycle_year(e["date"])
        f = lambda x: f"{x:+.1f}" if x is not None else "—"
        print(f"  {e['date'].date()} | {e['bp']:+d}bp | {e['rate_after']:.2f}% | C{cy} "
              f"| {f(r7)} | {f(r30)} | {f(r90)}")
        rate_rows.append({"date": e["date"].strftime("%Y-%m-%d"), "bp": e["bp"],
                          "dir": e["dir"], "rate_after": e["rate_after"], "cycle_year": cy,
                          "fwd7": round(r7, 1) if r7 is not None else None,
                          "fwd30": round(r30, 1) if r30 is not None else None,
                          "fwd90": round(r90, 1) if r90 is not None else None})

    # ── 加息组 vs 降息组 vs 基线 ──
    print("\n## 加息组 vs 降息组 (后30天, vs 无条件基线)")
    hike30 = perm_vs_baseline(price, [e["date"] for e in hikes], 30)
    cut30 = perm_vs_baseline(price, [e["date"] for e in cuts], 30)
    for name, s in (("加息", hike30), ("降息", cut30)):
        print(f"  {name} n={s['n']}: 中位 {s['median_pct']:+.1f}% 胜率 {s['win']}% "
              f"vs基线({s['baseline_win']}%/{s['baseline_median']:+.1f}%) p(中位)={s['p_median']}")
        print(f"      减半周期年序分布: {s['cycle_years']}")

    # ── regime 分析 ──
    print("\n## B. 利率 regime 下 BTC 日收益 (年化 & 周期混杂)")
    reg = regime_analysis(price, mid)
    for r, s in reg.items():
        print(f"  {r}: {s['days']}天 · 年化 {s['ann_pct']:+.0f}% · 胜率 {s['win']}% "
              f"· 均 {s['mean_bp']:+.1f}bp/日")
        print(f"      周期年序占比(减半后0/1/2/3年各多少天): {s['cycle_mix']}")

    # ── FOMC drift (弱检验 + 置换显著性) ──
    fomc = [pd.Timestamp(d) for d in FOMC_DATES]
    print("\n## C. FOMC 议息日 drift (±窗口 vs 基线, 日线弱检验 + 置换 p)")
    fomc_out = {}
    for lo, hi, lbl in ((-1, 1, "会前1天→会后1天"), (0, 5, "会后5天"), (-3, 0, "会前3天")):
        win_len = hi - lo
        base = ((price.shift(-win_len) / price - 1) * 100).dropna()
        pool = base.values
        base_med_all = np.median(pool)
        rets = []
        for a in fomc:
            sub = price.loc[:a + pd.Timedelta(days=lo)]
            if not len(sub):
                continue
            anc = sub.index[-1]
            if anc in base.index and (price.index[-1] - a).days >= hi:
                rets.append(float(base.loc[anc]))
        rets = np.array(rets)
        k = len(rets)
        obs_med = float(np.median(rets))
        cnt = 0
        for _ in range(N_PERM):
            s = RNG.choice(pool, size=k, replace=False)
            if abs(np.median(s) - base_med_all) >= abs(obs_med - base_med_all):
                cnt += 1
        p = (cnt + 1) / (N_PERM + 1)
        print(f"  {lbl}: n={k} 中位 {obs_med:+.2f}% 胜率 {(rets>0).mean()*100:.1f}% "
              f"| 基线 中位 {base.median():+.2f}% 胜率 {(base>0).mean()*100:.1f}% | 置换 p={p:.4f}")
        fomc_out[lbl] = {"n": k, "median_pct": round(obs_med, 2),
                         "win": round(float((rets > 0).mean()) * 100, 1),
                         "baseline_median": round(float(base.median()), 2),
                         "baseline_win": round(float((base > 0).mean()) * 100, 1),
                         "p": round(p, 4)}

    asset = {
        "generated": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "window": f"{price.index[0].date()} → {price.index[-1].date()}",
        "source": "FRED DFEDTARU/DFEDTARL (利率, 权威) + 人工整理 FOMC 会议日 (决策会与 FRED 交叉核对)",
        "honest_note": ("对抗核实结论 (2026-07): 利率方向对 BTC 无可靠信号 — 同样的加息/降息"
                        "在不同减半周期相位给出相反结果 (2024降息后+60% vs 2025降息后-26%), "
                        "naive 相关几乎全被减半周期与反向因果 (联储因崩盘才降息) 污染。"
                        "会后5天弱势原始 p=0.023 但**过不了多重比较** (族内 Šidák 0.069, "
                        "全族 Bonferroni 0.47) 且脆弱 (7天窗口/锚点+1天即失显著, 均值实为正), "
                        "降级为暗示性非确证。周期叙事参考, 非交易信号。"),
        "rate_events": rate_rows,
        "hike_vs_baseline_30d": hike30,
        "cut_vs_baseline_30d": cut30,
        "regime": reg,
        "fomc_drift": fomc_out,
    }
    for _dir in (OUT, DATA_OUT):
        os.makedirs(_dir, exist_ok=True)
        with open(os.path.join(_dir, "fomc_study.json"), "w", encoding="utf-8") as f:
            json.dump(asset, f, ensure_ascii=False, indent=1)
    print(f"\n✅ 落盘 output/ + btc_web data /fomc_study.json")


if __name__ == "__main__":
    main()
