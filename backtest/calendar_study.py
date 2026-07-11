#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.calendar_study
=======================
历史大事件的规律量化 (2026-07, 事件研究 C 层)。

对象: 减半周期相位 / 世界杯 / 美联储换主席 / 美国大选。
方法论红线 (延续 event_study 的诚实结论):
- 这些事件 n=3~4, **不可能有统计显著性** — 全部逐次列出, 绝不只报均值
- 核心交付是**混杂分析**: 世界杯(2014/18/22/26)与减半(2012/16/20/24)同为
  4 年周期且恰好反相位, 换主席三次也都落在减半后周期后半段 —
  "事件规律"大多是同一个减半周期穿了不同的衣服, 量化并呈现这个事实本身
- 输出: output/calendar_study.md + btc_web/btc_dashboard/data/calendar_events.json
  (前端"周期相位与事件规律"卡的数据资产, 每行自带 n 与混杂标注)

用法: cd backtest && python3 calendar_study.py
"""

import os
import sys
import json
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
DATA_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "btc_web", "btc_dashboard", "data")

HALVINGS = [pd.Timestamp(d) for d in
            ("2012-11-28", "2016-07-09", "2020-05-11", "2024-04-19")]
NEXT_HALVING_EST = pd.Timestamp("2028-04-15")   # 按出块速率估算, 前端动态展示倒计时

WORLD_CUPS = [  # (开幕日, 届)
    (pd.Timestamp("2014-06-12"), "2014 巴西"),
    (pd.Timestamp("2018-06-14"), "2018 俄罗斯"),
    (pd.Timestamp("2022-11-20"), "2022 卡塔尔"),
    (pd.Timestamp("2026-06-11"), "2026 北美"),
]

FED_CHAIRS = [  # (宣誓就任日, 换届)
    (pd.Timestamp("2014-02-03"), "伯南克→耶伦"),
    (pd.Timestamp("2018-02-05"), "耶伦→鲍威尔"),
    (pd.Timestamp("2026-05-22"), "鲍威尔→沃什"),
]

ELECTIONS = [
    (pd.Timestamp("2012-11-06"), "2012 奥巴马连任"),
    (pd.Timestamp("2016-11-08"), "2016 特朗普"),
    (pd.Timestamp("2020-11-03"), "2020 拜登"),
    (pd.Timestamp("2024-11-05"), "2024 特朗普"),
]


def load_price() -> pd.Series:
    """CM 长历史 + 现网评分历史近端补齐 (CM CSV 滞后 ~6 周)。"""
    s = pd.read_csv(os.path.join(OUT, "scores.csv"), index_col=0, parse_dates=True)
    price = s["price"].dropna()
    recent_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "cache", "recent_prices.json")
    if os.path.exists(recent_path):
        with open(recent_path) as f:
            rec = json.load(f)
        extra = pd.Series({pd.Timestamp(d): float(v) for d, v in rec.items()
                           if pd.Timestamp(d) > price.index[-1]}).sort_index()
        if len(extra):
            price = pd.concat([price, extra])
    return price


def months_since_halving(ts: pd.Timestamp):
    prior = [h for h in HALVINGS if h <= ts]
    if not prior:
        return None, None
    h = max(prior)
    return (ts - h).days / 30.44, HALVINGS.index(h) + 1  # (月数, 第几周期)


def _near_price(price: pd.Series, ts: pd.Timestamp):
    """事件日 (或最近交易记录日) 收盘价。超出数据末端 5 天返回 None。"""
    sub = price.loc[:ts]
    if not len(sub) or (ts - sub.index[-1]).days > 5:
        return None
    return float(sub.iloc[-1])


def event_rows(price: pd.Series, events, last_price: float):
    """逐次事件: 事件日价 / 距前高回撤 / 前瞻 90/180/365 (或至今) / 周期相位。"""
    rows = []
    for ts, label in events:
        p0 = _near_price(price, ts)
        if p0 is None:
            continue
        ath = float(price.loc[:ts].max())
        dd_at = (p0 / ath - 1) * 100
        m, cyc = months_since_halving(ts)
        row = {"date": ts.strftime("%Y-%m-%d"), "label": label,
               "price": round(p0, 0), "drawdown_at_event": round(dd_at, 1),
               "cycle_month": round(m, 1) if m is not None else None,
               "cycle_no": cyc}
        for w in (90, 180, 365):
            tgt = ts + pd.Timedelta(days=w)
            pw = _near_price(price, tgt)
            if pw is not None and (price.index[-1] - ts).days >= w:
                row[f"fwd{w}"] = round((pw / p0 - 1) * 100, 1)
            else:
                row[f"fwd{w}"] = None  # 进行中
        if row["fwd365"] is None and last_price:
            row["since_event"] = round((last_price / p0 - 1) * 100, 1)
        rows.append(row)
    return rows


def cycle_phase_map(price: pd.Series) -> list:
    """
    减半周期相位地图: 每个周期在关键相位窗口的表现, 逐周期列出。
    相位窗口: 0-12月(减半后首年) / 12-18(历史顶部区) / 18-30(熊市段) /
              30-42(筑底复苏) / 42-48(减半前)
    """
    phases = [(0, 12, "减半后 0-12 月 (历史主升段)"),
              (12, 18, "12-18 月 (历史周期顶窗口)"),
              (18, 30, "18-30 月 (历史熊市段)"),
              (30, 42, "30-42 月 (筑底复苏段)"),
              (42, 48, "42-48 月 (减半前蓄势)")]
    out = []
    for lo, hi, label in phases:
        per_cycle = []
        for i, h in enumerate(HALVINGS):
            a = h + pd.Timedelta(days=int(lo * 30.44))
            b = h + pd.Timedelta(days=int(hi * 30.44))
            pa, pb = _near_price(price, a), _near_price(price, b)
            if pa is None:
                continue
            if pb is None:  # 进行中的周期
                pb = float(price.iloc[-1])
                per_cycle.append({"cycle": i + 1, "ret": round((pb / pa - 1) * 100, 1),
                                  "partial": True})
                continue
            seg = price.loc[a:b]
            per_cycle.append({"cycle": i + 1, "ret": round((pb / pa - 1) * 100, 1),
                              "maxdd": round(float((seg / seg.cummax() - 1).min()) * 100, 1),
                              "partial": False})
        out.append({"phase": label, "cycles": per_cycle})
    return out


def main():
    price = load_price()
    last_price = float(price.iloc[-1])
    now = pd.Timestamp.now().normalize()
    cur_m, cur_cyc = months_since_halving(now)

    wc = event_rows(price, WORLD_CUPS, last_price)
    fed = event_rows(price, FED_CHAIRS, last_price)
    elec = event_rows(price, ELECTIONS, last_price)
    phases = cycle_phase_map(price)

    # ── 混杂分析: 各事件的周期相位落点 ──
    confound = {
        "世界杯": {"cycle_months": [r["cycle_month"] for r in wc],
                   "note": ("世界杯年(2014/18/22/26)与减半年(2012/16/20/24)同为4年周期且"
                            "恰好反相位(错开2年) — 四届开幕日均落在减半后 18-30 月的熊市段"
                            "(2022 卡塔尔因冬季办赛偏后至 30.3 月, 恰在熊底转复苏的边界)。"
                            "'世界杯=深熊'与'减半后第2年=熊底'是同一件事, 足球不搬动市场。")},
        "美联储换主席": {"cycle_months": [r["cycle_month"] for r in fed],
                         "note": ("三次换届(2014/2018/2026)都发生在减半后 14-25 月的周期"
                                  "顶后/熊市段 — 主席任期4年与减半周期同频。n=3 无从区分"
                                  "'换届效应'与周期本身; 两次历史换届后 12 个月均为深跌,"
                                  "但那也是周期熊市该跌的时候。")},
        "美国大选": {"cycle_months": [r["cycle_month"] for r in elec],
                     "note": ("大选年(2012/16/20/24)与减半年完全重合 (相距 0.5-7 月) —"
                              "'大选年牛市'≡'减半年牛市', 同一周期的两个名字。")},
    }

    current = {
        "as_of": now.strftime("%Y-%m-%d"),
        "cycle_no": cur_cyc,
        "months_since_halving": round(cur_m, 1),
        "days_to_next_halving_est": int((NEXT_HALVING_EST - now).days),
        "active_windows": [],
    }
    # 当前活跃事件窗口
    if pd.Timestamp("2026-06-11") <= now <= pd.Timestamp("2026-07-19"):
        current["active_windows"].append("2026 世界杯进行中 (6-11 → 7-19)")
    _chair_days = (now - pd.Timestamp("2026-05-22")).days
    if 0 <= _chair_days <= 365:
        current["active_windows"].append(
            f"联储换届后第 {_chair_days} 天 (沃什 2026-05-22 就任)")

    asset = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "honest_note": ("事件样本 n=3~4, 无统计显著性可言; 逐次列出+混杂标注, "
                        "周期叙事参考, 非交易信号。数据: CoinMetrics/现网价格序列。"),
        "current": current,
        "cycle_phases": phases,
        "events": {
            "世界杯": {"rows": wc, **confound["世界杯"]},
            "美联储换主席": {"rows": fed, **confound["美联储换主席"]},
            "美国大选": {"rows": elec, **confound["美国大选"]},
        },
    }
    os.makedirs(DATA_OUT, exist_ok=True)
    with open(os.path.join(DATA_OUT, "calendar_events.json"), "w", encoding="utf-8") as f:
        json.dump(asset, f, ensure_ascii=False, indent=1)

    # ── Markdown 报告 ──
    def rows_md(rows):
        lines = ["| 事件 | 日期 | 减半后月数 | 事件日距前高 | +90d | +180d | +365d | 至今 |",
                 "|---|---|---|---|---|---|---|---|"]
        for r in rows:
            f = lambda k: (f"{r[k]:+.1f}%" if r.get(k) is not None else "进行中")
            lines.append(
                f"| {r['label']} | {r['date']} | {r['cycle_month']} | {r['drawdown_at_event']:+.1f}% "
                f"| {f('fwd90')} | {f('fwd180')} | {f('fwd365')} | {r.get('since_event', '—')} |")
        return "\n".join(lines)

    md = [f"# 历史大事件规律量化 (生成 {asset['generated']})", "",
          f"**当前位置**: 第 {cur_cyc} 周期, 减半后 {cur_m:.1f} 月; "
          f"距下次减半(估) {current['days_to_next_halving_est']} 天; "
          f"活跃窗口: {'; '.join(current['active_windows']) or '无'}", "",
          f"⚠️ {asset['honest_note']}", "",
          "## 减半周期相位地图 (逐周期, 不平均)", ""]
    for ph in phases:
        md.append(f"### {ph['phase']}")
        md.append("| 周期 | 区间收益 | 区间内最大回撤 |")
        md.append("|---|---|---|")
        for c in ph["cycles"]:
            dd = "进行中" if c.get("partial") else f"{c['maxdd']:+.1f}%"
            md.append(f"| 第{c['cycle']}周期 | {c['ret']:+.1f}% | {dd} |")
        md.append("")
    for name in ("世界杯", "美联储换主席", "美国大选"):
        ev = asset["events"][name]
        md += [f"## {name} (n={len(ev['rows'])})", "", rows_md(ev["rows"]), "",
               f"**混杂标注**: {ev['note']}", ""]
    with open(os.path.join(OUT, "calendar_study.md"), "w") as f:
        f.write("\n".join(md))
    print("\n".join(md))


if __name__ == "__main__":
    main()
