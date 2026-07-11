#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.market_patterns
=============================
「市场规律与风险」版块数据 (事件研究 C 层, 2026-07)。

把三份研究资产 (backtest 生成) 蒸馏成前端展示就绪结构:
- 利率×周期证伪: 同样降息相反结果 + 加息降息组均不显著 + regime 反向因果
- 季节性证伪: Uptober/周末/9月/Sell-in-May/春节 → 严格检验均不显著
- 黑天鹅风险画像: 11 次逐事件急跌/收复 + 熊市扎堆 + 避风港反例

诚实红线: 全部经对抗核实; 证伪类结论明确"民间规律不显著", 黑天鹅明确
"n=1 无规律、仅风险画像"。绝不包装成交易信号。数据慢变, app 层 1h 缓存。
"""

import os
import json

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _load(name):
    try:
        with open(os.path.join(_DATA, name), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ market_patterns 资产 {name} 加载失败: {e}")
        return None


def _rates_block(f):
    """利率×周期证伪: 挑同政策相反结果的对照 + 加/降息组统计 + regime。"""
    if not f:
        return None
    ev = {r["date"]: r for r in f.get("rate_events", [])}
    # 降息天然对照实验: 2024(减半后0年牛) vs 2025(减半后1年跌)
    pairs = []
    for d in ("2024-09-19", "2024-11-08", "2025-09-18", "2025-10-30"):
        if d in ev and ev[d].get("fwd90") is not None:
            pairs.append({"date": d, "bp": ev[d]["bp"], "cycle_year": ev[d]["cycle_year"],
                          "fwd90": ev[d]["fwd90"]})
    hike = f.get("hike_vs_baseline_30d") or {}
    cut = f.get("cut_vs_baseline_30d") or {}
    reg = f.get("regime") or {}
    return {
        "title": "利率对 BTC 的影响 —— 方向无信号, 是周期错觉",
        "natural_experiment": {
            "desc": "同样是降息, 结果相反 (由减半周期相位而非政策驱动)",
            "pairs": pairs,  # 2024 降息后 +60%/+26% vs 2025 降息后 -26%/-17%
        },
        "groups": [
            {"name": "加息组", "n": hike.get("n"), "median": hike.get("median_pct"),
             "win": hike.get("win"), "baseline_win": hike.get("baseline_win"),
             "p": hike.get("p_median")},
            {"name": "降息组", "n": cut.get("n"), "median": cut.get("median_pct"),
             "win": cut.get("win"), "baseline_win": cut.get("baseline_win"),
             "p": cut.get("p_median")},
        ],
        "regime": [{"name": k, "days": v["days"], "ann_pct": v["ann_pct"], "win": v["win"]}
                   for k, v in reg.items()],
        "verdict": ("加息/降息之后 BTC 短期都偏弱且均不显著 (p=0.11/0.14); "
                    "利率下行 regime 年化 -12% 含反向因果 (联储因崩盘才降息); "
                    "利率平台 +271% 是零利率时代早期采用红利。方向无可靠信号。"),
    }


def _seasonality_block(s):
    """季节性证伪: 逐条民间规律 + 检验结论。"""
    if not s:
        return None
    mo = {m["month"]: m for m in s["month"]["per_month"]}
    items = [
        {"name": "周末效应", "claim": "周末流动性低→偏弱",
         "stat": f"周末 {s['weekday']['weekend']['diff_bp']:+.0f}bp/日",
         "p": s["weekday"]["weekend"]["p"], "verdict": "不存在"},
        {"name": "Uptober 红十月", "claim": "10月胜率 69%、中位 +13%",
         "stat": f"单月 p={mo['10月']['p_raw']}, 族校正 p={s['month']['p_family_wise']}",
         "p": s["month"]["p_family_wise"], "verdict": "多重比较陷阱, 噪声"},
        {"name": "9月魔咒", "claim": "9月胜率 44%、中位 -2.4%",
         "stat": f"单月 p={mo['9月']['p_raw']}", "p": mo["9月"]["p_raw"], "verdict": "不显著"},
        {"name": "Sell in May", "claim": "夏半年弱于冬半年",
         "stat": f"{s['month']['sell_in_may']['summer_daily_bp']:+.0f} vs "
                 f"{s['month']['sell_in_may']['winter_daily_bp']:+.0f} bp/日",
         "p": s["month"]["sell_in_may"]["p"], "verdict": "方向对但不显著"},
        {"name": "月末月初效应", "claim": "月末+月初偏强",
         "stat": f"{s['turn_of_month']['tom_daily_bp']:+.0f} vs "
                 f"{s['turn_of_month']['other_daily_bp']:+.0f} bp/日",
         "p": s["turn_of_month"]["p"], "verdict": "不显著"},
        {"name": "春节红包行情", "claim": "后20天胜率 62%",
         "stat": f"vs 基线 {s['cny']['baseline_win']}%", "p": s["cny"]["p_win"],
         "verdict": "不存在"},
    ]
    return {
        "title": "季节性民间规律 —— 严格检验均不显著",
        "items": items,
        "verdict": ("测了 6 个最有名的季节性规律, 用置换检验 + 多重比较校正, "
                    "无一通过。Uptober 是'测12个月总有1个看着显著'的经典幻觉。"),
    }


def _blackswan_block(b):
    """黑天鹅风险画像: 逐事件表 + 聚合 + 反例。"""
    if not b:
        return None
    return {
        "title": "黑天鹅冲击画像 —— 无法预测, 但有风险规律",
        "events": [{"name": e["name"], "date": e["date"], "kind": e["kind"],
                    "cycle_month": e["cycle_month"],
                    "acute_dd": e["acute_dd_pct"], "dd_from_high": e["dd_from_30d_high_pct"],
                    "days_to_trough": e["days_to_trough"], "recovery_days": e["recovery_days"],
                    "fwd90": e["fwd90"], "fwd365": e["fwd365"], "since": e.get("since")}
                   for e in b["events"]],
        "summary": b["summary"],
        "verdict": ("急跌中位 -16% (相对前30日高点 -33%)、见底快, 但收复时间 1~534 天"
                    "完全由减半周期相位决定; 加密内生黑天鹅扎堆熊市 (反身性); "
                    "外部危机 (银行/宏观) BTC 反成避风港 (硅谷银行后 90天 +30%)。"),
    }


def _forward_risk_block(fr):
    """前瞻风险登记册 (判断非统计): 灰犀牛/黑天鹅分组直接透传策展资产。"""
    if not fr:
        return None
    return {
        "title": "前瞻风险雷达 —— 未来可能的 (判断, 非回测)",
        "near_term_focus": fr.get("near_term_focus"),
        "gray_rhino": fr.get("gray_rhino", []),
        "black_swan": fr.get("black_swan", []),
        "macro_note": fr.get("macro_note"),
        "secondary": fr.get("secondary", []),
        "honest_note": fr.get("honest_note"),
    }


def get_market_patterns():
    """返回各块; 任一资产缺失则该块为 None (前端跳过)。全缺返回 None。"""
    f = _load("fomc_study.json")
    s = _load("seasonality_study.json")
    b = _load("blackswan_events.json")
    fr = _load("forward_risk_register.json")
    blocks = {
        "rates": _rates_block(f),
        "seasonality": _seasonality_block(s),
        "blackswan": _blackswan_block(b),
        "forward_risk": _forward_risk_block(fr),
    }
    if not any(blocks.values()):
        return None
    return {
        "generated": (f or s or b or {}).get("generated"),
        "honest_note": ("全部经三视角对抗核实。证伪类: 民间规律不显著; 黑天鹅: n=1 无规律"
                        "仅风险画像。唯一真实可量化的重复规律是减半周期本身 (见上方周期相位卡)。"
                        "均为周期叙事/风险参考, 非交易信号。"),
        **blocks,
    }
