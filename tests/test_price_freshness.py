#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
价格新鲜度守卫纯函数测试 (2026-07 裁决级教训的制度化)。
背景: 缓存滞后 54 天曾把候选因子 post-2024-10 IC 从 +0.069 美化成 +0.605。
不触网 — 只测 lag 计算与守卫决策逻辑。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backtest"))
from data_sources import price_lag_days, freshness_verdict, PRICE_MAX_LAG_DAYS  # noqa: E402


def test_price_lag_days():
    assert price_lag_days("2026-07-10", today="2026-07-17") == 7
    assert price_lag_days("2026-07-17", today="2026-07-17") == 0
    assert price_lag_days("2026-05-24", today="2026-07-18") == 55  # 实际事故场景
    # 时间分量不影响 (normalize)
    assert price_lag_days("2026-07-10 23:59:59", today="2026-07-17 00:00:01") == 7


def test_freshness_verdict_boundaries():
    assert PRICE_MAX_LAG_DAYS == 7  # 改此上限须同步告警文案与本测试
    assert freshness_verdict(0) == "ok"
    assert freshness_verdict(7) == "ok"          # 恰在上限 = 放行
    assert freshness_verdict(8) == "fail"        # 超限且不允许旧数据 = 拒跑
    assert freshness_verdict(55) == "fail"
    # 显式越过开关: 降级为放行 (调用方须告警并在报告声明)
    assert freshness_verdict(55, allow_stale=True) == "ok"
    assert freshness_verdict(7, allow_stale=True) == "ok"
