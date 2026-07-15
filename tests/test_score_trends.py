#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
score_history.compute_trends 冒烟测试: 月/季/年尺度评分变化。
锁死: 深度不足 → None (不伪造); 基准取 ≤目标日 最近条目; 容忍窗口外缺口 → None。
"""
from datetime import datetime, timedelta

from btc_dashboard.score_history import compute_trends, _TREND_TOLERANCE_DAYS


def _entries(n, end="2026-07-15", cycle_fn=None, tactical_fn=None):
    """构造 n 天连续日频条目, 末条为 end。分数默认线性爬升便于精确断言。"""
    end_d = datetime.strptime(end, "%Y-%m-%d")
    out = []
    for i in range(n):
        d = end_d - timedelta(days=n - 1 - i)
        out.append({
            "date": d.strftime("%Y-%m-%d"),
            "total_score": (cycle_fn or (lambda k: round(0.001 * k, 4)))(i),
            "tactical_score": (tactical_fn or (lambda k: round(-0.002 * k, 4)))(i),
        })
    return out


def test_full_depth_all_horizons_exact():
    es = _entries(400)  # 400天: 三档全可算
    t = compute_trends(es)
    assert t["depth_days"] == 400
    # cycle: score(i)=0.001*i, 末条 i=399; 30天前条目 i=369 → Δ=0.030
    assert t["cycle"]["d30"]["delta"] == 0.030
    assert t["cycle"]["d90"]["delta"] == 0.090
    assert t["cycle"]["d365"]["delta"] == 0.365
    assert t["cycle"]["d365"]["base_date"] == "2025-07-15"
    # tactical: -0.002/天
    assert t["tactical"]["d7"]["delta"] == -0.014
    assert t["tactical"]["d30"]["delta"] == -0.06


def test_thin_history_yields_none_not_fabrication():
    es = _entries(50)  # 只有50天: d30 可算, d90/d365 必须为 None
    t = compute_trends(es)
    assert t["cycle"]["d30"] is not None
    assert t["cycle"]["d90"] is None
    assert t["cycle"]["d365"] is None
    assert t["tactical"]["d7"] is not None


def test_gap_beyond_tolerance_is_none():
    # 91天历史但中段挖空: 目标日±容忍窗口内无条目 → None (不拿更老数据冒充)
    es = _entries(91)
    cut_lo = 91 - 1 - 30 - _TREND_TOLERANCE_DAYS - 5
    cut_hi = 91 - 1 - 30 + 1
    es_gapped = es[:cut_lo] + es[cut_hi:]
    t = compute_trends(es_gapped)
    assert t["cycle"]["d30"] is None      # 缺口吞掉了 d30 基准窗口
    assert t["cycle"]["d90"] is not None  # 90天前的条目仍在


def test_none_scores_skipped_as_baseline():
    es = _entries(60)
    # 30天前附近的条目分数为 None → 回退到容忍窗口内更早的有效条目
    es[60 - 1 - 30]["total_score"] = None
    t = compute_trends(es)
    assert t["cycle"]["d30"] is not None
    assert t["cycle"]["d30"]["base_date"] < "2026-06-15"


def test_empty_and_missing_fields():
    t = compute_trends([])
    assert t == {"cycle": {"d30": None, "d90": None, "d365": None},
                 "tactical": {"d7": None, "d30": None}, "depth_days": 0}
    # 末条无日期 → 全 None 不炸
    t2 = compute_trends([{"total_score": 0.1}])
    assert t2["cycle"]["d30"] is None
