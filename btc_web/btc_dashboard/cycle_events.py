#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.cycle_events
==========================
周期相位与历史大事件规律 (事件研究 C 层, 2026-07)。

静态部分 (逐次事件表 + 相位地图 + 混杂标注) 来自 backtest/calendar_study.py
生成的 data/calendar_events.json — n=3~4 的事件全部逐次列出并带混杂说明,
不包装成"规律信号"。动态部分 (当前减半后月数 / 倒计时) 每次请求时现算,
不随资产文件过期。减半日期唯一事实源在 core.py。
"""

import os
import json
from datetime import datetime

from .core import HALVING_DATES, NEXT_HALVING_ESTIMATE

_ASSET = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "data", "calendar_events.json")


def get_cycle_events():
    """返回前端卡数据; 资产文件缺失时返回 None (卡片隐藏)。"""
    try:
        with open(_ASSET, "r", encoding="utf-8") as f:
            asset = json.load(f)
    except Exception as e:
        print(f"⚠️ calendar_events.json 加载失败: {e}")
        return None

    now = datetime.now()
    prior = [h for h in HALVING_DATES if h <= now]
    months = (now - max(prior)).days / 30.44 if prior else None
    asset["current"] = {
        "as_of": now.strftime("%Y-%m-%d"),
        "cycle_no": len(prior),
        "months_since_halving": round(months, 1) if months is not None else None,
        "days_to_next_halving_est": (NEXT_HALVING_ESTIMATE - now).days,
    }
    return asset
