#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.roadmap
=====================
BTC 里程碑路线图 (事件研究 C 层, 2026-07)。

静态部分 (分时代里程碑) 来自策展资产 data/btc_roadmap.json — 日期经对抗
核实 (WebSearch 逐条复核), 确定性分级 历史/预定/提案 严格标注。
动态部分 (当前减半后月数 / 距下次减半倒计时 / 你在这里定位) 每次请求现算。
"""

import os
import json
from datetime import datetime

from .core import HALVING_DATES, NEXT_HALVING_ESTIMATE

_ASSET = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "data", "btc_roadmap.json")


def get_roadmap():
    """返回路线图 + 动态当前位置; 资产缺失返回 None。"""
    try:
        with open(_ASSET, "r", encoding="utf-8") as f:
            asset = json.load(f)
    except Exception as e:
        print(f"⚠️ btc_roadmap.json 加载失败: {e}")
        return None

    now = datetime.now()
    prior = [h for h in HALVING_DATES if h <= now]
    months = (now - max(prior)).days / 30.44 if prior else None
    asset["current"] = {
        "as_of": now.strftime("%Y-%m-%d"),
        "halving_no": len(prior),
        "months_since_halving": round(months, 1) if months is not None else None,
        "days_to_next_halving_est": (NEXT_HALVING_ESTIMATE - now).days,
        "note": f"第 {len(prior)} 次减半后 {round(months, 1)} 月, 距第 {len(prior)+1} 次减半约 "
                f"{(NEXT_HALVING_ESTIMATE - now).days} 天" if months is not None else "",
    }
    return asset
