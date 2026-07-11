#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.cycle_events
==========================
周期相位与历史大事件规律 (事件研究 C 层, 2026-07)。

静态部分 (逐次事件表 + 相位地图 + 混杂标注) 来自 backtest/calendar_study.py
生成的 data/calendar_events.json — n=3~4 的事件全部逐次列出并带混杂说明,
不包装成"规律信号"。动态部分 (当前减半后月数 / 倒计时 / 活跃事件窗口)
每次请求时现算, 不随资产文件过期。
"""

import os
import json
from datetime import datetime

_ASSET = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "data", "calendar_events.json")

HALVINGS = [datetime(2012, 11, 28), datetime(2016, 7, 9),
            datetime(2020, 5, 11), datetime(2024, 4, 19)]
NEXT_HALVING_EST = datetime(2028, 4, 15)

# 活跃事件窗口 (起, 止, 文案模板; 止为 None 表示按天数计)
_WINDOWS = [
    (datetime(2026, 6, 11), datetime(2026, 7, 19), "2026 世界杯进行中 (6-11 → 7-19)"),
    (datetime(2026, 5, 22), None, "联储换届后第 {days} 天 (沃什 2026-05-22 就任)"),
]
_CHAIR_WINDOW_DAYS = 365   # 换届后一年内视为活跃观察窗口


def get_cycle_events():
    """返回前端卡数据; 资产文件缺失时返回 None (卡片隐藏)。"""
    try:
        with open(_ASSET, "r", encoding="utf-8") as f:
            asset = json.load(f)
    except Exception as e:
        print(f"⚠️ calendar_events.json 加载失败: {e}")
        return None

    now = datetime.now()
    prior = [h for h in HALVINGS if h <= now]
    months = (now - max(prior)).days / 30.44 if prior else None
    windows = []
    for start, end, tpl in _WINDOWS:
        if end is not None:
            if start <= now <= end:
                windows.append(tpl)
        else:
            days = (now - start).days
            if 0 <= days <= _CHAIR_WINDOW_DAYS:
                windows.append(tpl.format(days=days))

    asset["current"] = {
        "as_of": now.strftime("%Y-%m-%d"),
        "cycle_no": len(prior),
        "months_since_halving": round(months, 1) if months is not None else None,
        "days_to_next_halving_est": (NEXT_HALVING_EST - now).days,
        "active_windows": windows,
    }
    return asset
