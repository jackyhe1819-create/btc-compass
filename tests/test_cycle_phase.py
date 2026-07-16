#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cycle_phase 冒烟测试: 相位规则边界 + 温度计合成 + 平滑 + 端到端 since 推导。
不触网; phase_stats.json 为仓库静态资产可直接依赖。
"""
from btc_dashboard import cycle_phase as cp


# ── 规则边界 ──

def test_classify_boundaries():
    C = cp.classify_phase
    # 泡沫: 贴近 ATH + 过热
    assert C(-0.51, 1.0, -0.05, 10) == "bubble"
    assert C(-0.49, 1.0, -0.05, 10) != "bubble"      # 温度未过线
    assert C(-0.60, 1.0, -0.13, 10) != "bubble"      # 回撤超 12%
    # 熊末: 深回撤 + 过冷 + 距顶超300天 (age 门槛防温度计早熟)
    assert C(0.40, -1.0, -0.55, 301) == "bear_late"
    assert C(0.40, -1.0, -0.55, 200) != "bear_late"  # age 不足 → 落到熊中
    assert C(0.30, -1.0, -0.55, 400) != "bear_late"  # 温度不够冷
    # 牛初: 深回撤 + 偏冷 + 趋势转正
    assert C(0.20, 1.0, -0.40, 400) == "early_bull"
    assert C(0.20, 0.0, -0.40, 400) != "early_bull"
    # 繁荣: 趋势正 + 距高点近 + 未过热
    assert C(-0.20, 1.0, -0.10, 5) == "boom"
    # 回调/熊初: 破位 1-10 个月, 回撤 10-40%, 趋势不正
    assert C(0.0, -0.5, -0.25, 90) == "pullback_or_bear"
    assert C(0.0, -0.5, -0.25, 400) != "pullback_or_bear"  # 破位太久 → 熊中
    # 熊中: 趋势负 + 回撤 ≥20%
    assert C(0.0, -1.0, -0.45, 400) == "bear_mid"
    # 过渡与数据不足
    assert C(0.0, 0.0, -0.05, 5) == "transition"
    assert C(None, 1.0, -0.05, 5) == "unknown"


def test_smooth_bubble_bypass():
    raw = ["boom"] * 13 + ["bubble"] + ["boom"] * 3
    sm = cp.smooth_phases(raw, k=14)
    assert sm[13] == "bubble"          # 急信号免平滑, 单日闪现也亮
    assert sm[14] == "boom"            # 其余走众数
    # 众数平滑吞掉 3 天以内的普通抖动
    raw2 = ["boom"] * 10 + ["transition"] * 3 + ["boom"] * 5
    assert cp.smooth_phases(raw2, k=14)[12] == "boom"


def test_smooth_tie_deterministic_sticky():
    """回归 (2026-07 对抗审查 major): 众数平局曾按字符串哈希序裁决,
    跨进程不可复现。现规则: 平局粘滞前一日, 无前值按固定优先序。"""
    raw = ["boom"] * 7 + ["bear_mid"] * 7
    sm = cp.smooth_phases(raw, k=14)
    assert sm[13] == "boom"            # 7:7 平局 → 粘滞前一日 (boom)
    # 多次调用完全一致 (确定性)
    assert all(cp.smooth_phases(raw, k=14) == sm for _ in range(5))
    # 无前值时的平局: 固定优先序 (bubble 最先)
    assert cp.smooth_phases(["boom", "bear_mid"], k=2)[1] in ("boom",)  # 前值粘滞


def test_price_history_depth_guard():
    """价格史降级 (备源 365 天) 时不得用失真 ATH 硬判 → 返回 None。"""
    from datetime import datetime, timedelta
    end = datetime(2026, 7, 15)
    prices = {(end - timedelta(days=i)).strftime("%Y-%m-%d"): 100000.0
              for i in range(400)}   # 仅 400 天 < MIN_PRICE_HISTORY_DAYS
    entries = [{"date": (end - timedelta(days=1)).strftime("%Y-%m-%d"),
                "scores": {"减半周期": 0, "趋势过滤器": -1}}]
    assert cp.compute_cycle_phase(entries, prices) is None


def test_thermometer_composition_and_renorm():
    # 全因子: 趋势伸展 4 项等权 -1, 筹码 3 项 (MEMBER_WEIGHTS .35/.25/.20 重归一) +1, 减半 -1
    scores = {"200-Week Heatmap": -1, "幂律走廊": -1, "Pi Cycle Top": -1, "Ahr999": -1,
              "MVRV-Z": 1, "STH成本线": 1, "NUPL": 1, "减半周期": -1}
    t = cp.thermometer_from_scores(scores)
    # 0.45*(-1) + 0.45*(+1) + 0.10*(-1) = -0.10
    assert abs(t - (-0.10)) < 1e-9
    # 缺整桶: 覆盖率重归一仍可算; 全缺 → None
    t2 = cp.thermometer_from_scores({"减半周期": -1})
    assert t2 is None or isinstance(t2, float)  # 单桶覆盖率 0.10 < 0.5 → None
    assert cp.thermometer_from_scores({"减半周期": -1}) is None
    assert cp.thermometer_from_scores({}) is None
    # 交易所余额刻意不在温度计成员里: 传入也不改变结果
    t3 = cp.thermometer_from_scores({**scores, "交易所余额": 1})
    assert abs(t3 - t) < 1e-9


def _entries_prices(n=40, phase_scores=None, base_price=100000.0, drift=0.0):
    """构造 n 天合成历史: 价格常数(或漂移) + 固定因子分。"""
    from datetime import datetime, timedelta
    end = datetime(2026, 7, 15)
    entries, prices = [], {}
    # 前置 2200 天价格史 (满足深度守卫; ATH 锚定在 400 天前的 200000)
    for i in range(2200, n, -1):
        d = (end - timedelta(days=i)).strftime("%Y-%m-%d")
        prices[d] = 200000.0 if i == 400 else 120000.0
    for i in range(n, 0, -1):
        d = (end - timedelta(days=i - 1)).strftime("%Y-%m-%d")
        p = base_price * (1 + drift * (n - i))
        prices[d] = p
        entries.append({"date": d, "scores": dict(phase_scores or {})})
    return entries, prices


def test_end_to_end_bear_mid_and_since():
    # 价格 10 万, ATH 20 万 (dd=-50%... 用 -45% 避开熊末线), 趋势 -1, 温度中性
    scores = {"200-Week Heatmap": 0, "幂律走廊": 0, "Pi Cycle Top": 0, "Ahr999": 0,
              "MVRV-Z": 0, "STH成本线": 0, "NUPL": 0, "减半周期": 0, "趋势过滤器": -1}
    entries, prices = _entries_prices(40, scores, base_price=110000.0)
    out = cp.compute_cycle_phase(entries, prices)
    assert out is not None and out["phase"] == "bear_mid"
    assert out["since"] == entries[0]["date"]      # 40 天同相位 → since=段首
    assert out["criteria"]["drawdown_pct"] is not None
    assert "n=3~4" in out["note"]


def test_end_to_end_unknown_on_missing_factors():
    entries, prices = _entries_prices(20, {})      # 无因子分 → 温度计 None
    out = cp.compute_cycle_phase(entries, prices)
    assert out is not None and out["phase"] == "unknown"


def test_phase_stats_asset_shape():
    stats = cp.load_phase_stats()
    assert stats and "phases" in stats
    assert "样本内" in stats.get("note", "")
    pb = stats["phases"].get("pullback_or_bear", {})
    res = pb.get("resolution")
    assert res is not None and set(res) == {"confirmed_pullback", "confirmed_bear", "pending"}
    # 泡沫领先天数统计存在 (过热≠立刻见顶的诚实展示依赖它)
    assert "top_lead_days" in stats["phases"].get("bubble", {})
