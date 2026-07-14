#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.blackswan_study
========================
黑天鹅事件对 BTC 的冲击画像 (2026-07, 事件研究 C 层)。

与前面所有研究的根本区别: 黑天鹅是**一次性、特异**事件, n=1, 不存在
"统计显著性"或"可预测规律"。这里做的是**风险画像 + 复盘**:
- 逐次: 急跌幅度 / 见底天数 / 收复前高天数 / 90-365 天后瞻 / 减半周期相位
- 聚合(诚实): 急跌幅度的分布, 收复时间与周期相位的关系
- 核心洞见(需数据检验):
  (1) 加密黑天鹅**扎堆熊市** — 2022 Luna+Celsius+FTX 三连, 反身性:
      下跌行情暴露杠杆与欺诈, 是熊市"揭穿"脆弱, 而非黑天鹅"造成"熊市
  (2) 黑天鹅方向未必向下: 硅谷银行暴雷时 BTC 反涨(避险逃离银行体系)
  (3) 急跌幅度趋同, 但收复时间由周期相位决定, 非事件本身

⚠️ 日线收盘口径: 会低估盘中急跌 (如 COVID 3-12 盘中 -50%, 日线约 -40%)。
事件日期为历史事实, 供复盘参考, 非交易信号。

用法: cd backtest && python3 blackswan_study.py
"""

import os
import sys
import json

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seasonality_study import load_price, cycle_year, HALVINGS  # HALVINGS 与现网 core.py 同源

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
DATA_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "btc_web", "btc_dashboard", "data")

# (锚点=急跌触发日, 名称, 类型, 一句话)
BLACK_SWANS = [
    ("2014-02-24", "Mt.Gox 崩塌", "交易所", "最大交易所停提→破产, 早期信任危机"),
    ("2016-08-02", "Bitfinex 被盗", "交易所", "12万 BTC 失窃, 当时第二大交易所"),
    ("2017-09-04", "中国 ICO/交易所禁令", "监管", "94 禁令+交易所关停, 政策冲击"),
    ("2020-03-12", "新冠黑色星期四", "宏观", "全球流动性挤兑, 股债商同崩"),
    ("2021-05-19", "中国挖矿/交易禁令", "监管", "算力大迁徙+杠杆连环爆"),
    ("2021-09-24", "中国全面禁令+恒大", "监管/宏观", "'所有加密交易非法'+恒大暴雷"),
    ("2022-05-09", "Luna/UST 崩盘", "稳定币", "算法稳定币脱锚归零, 400亿蒸发"),
    ("2022-06-12", "Celsius/3AC 爆雷", "借贷/基金", "CeFi 借贷挤兑+对冲基金强平"),
    ("2022-11-08", "FTX 暴雷", "交易所", "第二大交易所挪用客户资金崩塌"),
    ("2023-03-10", "硅谷银行/USDC 脱锚", "银行", "美国银行危机, USDC 短暂脱锚"),
    ("2024-08-05", "日元套息平仓", "宏观", "日央行加息触发全球风险资产去杠杆"),
]

ACUTE = 30   # 急跌观察窗 (日历日)


def profile(price: pd.Series, anchor_str, name, kind, desc, last_price):
    anchor = pd.Timestamp(anchor_str)
    pre = price.loc[:anchor - pd.Timedelta(days=1)]
    if not len(pre):
        return None
    ref = float(pre.iloc[-1])                       # 事件前一交易日收盘
    # 事件前 30 日高点 (急跌是从局部高点算才有意义)
    ref_high = float(price.loc[anchor - pd.Timedelta(days=30):anchor - pd.Timedelta(days=1)].max())

    acute = price.loc[anchor:anchor + pd.Timedelta(days=ACUTE)]
    if len(acute) < 2:
        return None
    trough = float(acute.min())
    trough_date = acute.idxmin()
    acute_dd = (trough / ref - 1) * 100             # 相对事件前一日
    dd_from_high = (trough / ref_high - 1) * 100     # 相对前30日高点
    days_to_trough = (trough_date - anchor).days

    # 收复: 事件前一日收盘价何时被重新站上
    after = price.loc[trough_date:]
    recl = after[after >= ref]
    recovery_days = int((recl.index[0] - anchor).days) if len(recl) else None

    def fwd(days):
        tgt = price.loc[anchor:anchor + pd.Timedelta(days=days)]
        if len(tgt) < 2 or (price.index[-1] - anchor).days < days:
            return None
        return round((float(tgt.iloc[-1]) / ref - 1) * 100, 1)

    m = [h for h in HALVINGS if h <= anchor]
    cyc_m = ((anchor - max(m)).days / 30.44) if m else None

    return {
        "date": anchor_str, "name": name, "kind": kind, "desc": desc,
        "ref_price": round(ref, 0),
        "acute_dd_pct": round(acute_dd, 1),
        "dd_from_30d_high_pct": round(dd_from_high, 1),
        "days_to_trough": days_to_trough,
        "recovery_days": recovery_days,
        "fwd90": fwd(90), "fwd365": fwd(365),
        "since": round((last_price / ref - 1) * 100, 1) if fwd(365) is None else None,
        "cycle_month": round(cyc_m, 1) if cyc_m is not None else None,
        "cycle_year": cycle_year(anchor),
    }


def main():
    price = load_price()
    last_price = float(price.iloc[-1])
    rows = [r for e in BLACK_SWANS
            if (r := profile(price, *e, last_price)) is not None]

    dds = np.array([r["acute_dd_pct"] for r in rows])
    highs = np.array([r["dd_from_30d_high_pct"] for r in rows])
    recs = [r["recovery_days"] for r in rows if r["recovery_days"] is not None]

    # 熊市扎堆检验: 各事件的减半后月数
    bear_window = [r for r in rows if 18 <= (r["cycle_month"] or 0) <= 30]

    asset = {
        "generated": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "honest_note": ("黑天鹅为一次性特异事件, n=1, 无统计规律或可预测性 — 仅风险画像/复盘。"
                        "样本为事后追认的知名暴跌 (选样偏差), BTC 存活至今才谈得上收复 "
                        "(幸存者偏差), 汇总中位数不构成未来下界。"
                        "日线收盘低估盘中急跌。事件日期为历史事实, 非交易信号。"),
        "events": rows,
        "summary": {
            "n": len(rows),
            "acute_dd_median": round(float(np.median(dds)), 1),
            "acute_dd_worst": round(float(dds.min()), 1),
            "acute_dd_best": round(float(dds.max()), 1),   # 硅谷银行=避险逆势
            "dd_from_high_median": round(float(np.median(highs)), 1),
            "recovery_days_median": int(np.median(recs)) if recs else None,
            "bear_cluster": {
                "n_in_18_30m": len(bear_window),
                "events": [r["name"] for r in bear_window],
                "note": "加密黑天鹅集中在减半后18-30月熊市段 — 反身性: 下跌暴露杠杆/欺诈",
            },
            # 逆势反例取"外部体系"型且 90 天净正的事件 (银行/宏观危机 BTC 反成避风港)。
            # 用相对前30日高点的回撤做标题 (锚点当日收盘会低估: SVB 那周 BTC 已随
            # Silvergate 先跌, acute_dd 仅 -0.5% 但周内真实回撤 dd_from_30d_high ≈ -18%)
            "counter_example": next(
                ({"name": r["name"], "acute_dd_pct": r["acute_dd_pct"],
                  "dd_from_30d_high_pct": r["dd_from_30d_high_pct"],
                  "fwd90": r["fwd90"], "fwd365": r["fwd365"],
                  "note": "外部银行危机: BTC 先随危机跌约 10%(周内高点算 -18%), 后强弹, "
                          "90天净+30%/一年+237% — 震中在传统金融时 BTC 可成避风港"}
                 for r in rows if r["kind"] == "银行" and (r["fwd90"] or 0) > 0), None),
        },
    }
    os.makedirs(DATA_OUT, exist_ok=True)
    with open(os.path.join(DATA_OUT, "blackswan_events.json"), "w", encoding="utf-8") as f:
        json.dump(asset, f, ensure_ascii=False, indent=1)

    print("=" * 72)
    print(f"黑天鹅冲击画像 | BTC {price.index[0].date()}→{price.index[-1].date()} | n={len(rows)}")
    print("=" * 72)
    print(f"{'事件':<22}{'减半后':>6}{'急跌%':>8}{'见底天':>6}{'收复天':>7}{'+90d':>8}{'+365d/至今':>11}")
    for r in rows:
        rec = r["recovery_days"] if r["recovery_days"] is not None else "未收复"
        f90 = f"{r['fwd90']:+.0f}" if r["fwd90"] is not None else "—"
        f365 = (f"{r['fwd365']:+.0f}" if r["fwd365"] is not None
                else (f"{r['since']:+.0f}*" if r["since"] is not None else "—"))
        print(f"{r['name']:<22}{r['cycle_month']:>5.0f}m{r['acute_dd_pct']:>7.0f}%"
              f"{r['days_to_trough']:>5}d{str(rec):>6}{f90:>8}{f365:>11}")
    s = asset["summary"]
    print("-" * 72)
    print(f"急跌幅度: 中位 {s['acute_dd_median']}% | 最深 {s['acute_dd_worst']}% "
          f"| 最浅 {s['acute_dd_best']}% (>0=逆势避险)")
    print(f"相对前30日高点回撤中位: {s['dd_from_high_median']}% | 收复前高中位: {s['recovery_days_median']}天")
    print(f"熊市扎堆: {s['bear_cluster']['n_in_18_30m']}/{len(rows)} 落在减半后18-30月熊市段 "
          f"→ {s['bear_cluster']['events']}")
    if s["counter_example"]:
        c = s["counter_example"]
        print(f"逆势反例: {c['name']} 周内高点回撤 {c['dd_from_30d_high_pct']}% (锚点日收盘仅 {c['acute_dd_pct']}%), "
              f"后90天 {c['fwd90']:+.0f}% / 一年 {c['fwd365']:+.0f}% — 外部危机 BTC 成避风港")
    print(f"\n✅ 落盘 {os.path.join(DATA_OUT, 'blackswan_events.json')}")


if __name__ == "__main__":
    main()
