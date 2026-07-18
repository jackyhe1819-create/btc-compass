#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scoring.py 纯函数冒烟测试: 分位数归一化 + 因子分桶聚合。
不触网 — 全部用合成数据。
"""
from types import SimpleNamespace

import numpy as np
import pandas as pd

from btc_dashboard import scoring


def _ind(score, value=1.0):
    """构造最小指标对象 (_compute_bucket_scores 只读 .value/.score)。"""
    return SimpleNamespace(value=value, score=score)


def _ind_card(score, value=1.0, color="🟡", status="ok"):
    """构造完整卡片形态指标 (含 .color/.status), 供降级路径改写卡片状态的测试。"""
    return SimpleNamespace(value=value, score=score, color=color, status=status)


# ────────────────────────────────────────────────────────────
# _percentile_score
# ────────────────────────────────────────────────────────────

def test_percentile_score_at_historical_high_is_bearish():
    s = pd.Series(np.linspace(1, 100, scoring.PERCENTILE_WINDOW))
    score, n_used = scoring._percentile_score(s)
    assert n_used == scoring.PERCENTILE_WINDOW
    assert score < -0.99  # 当前值为 4 年最高 → 接近 -1


def test_percentile_score_at_historical_low_is_bullish():
    s = pd.Series(np.linspace(100, 1, scoring.PERCENTILE_WINDOW))
    score, _ = scoring._percentile_score(s)
    assert score > 0.99


def test_percentile_score_at_median_is_neutral():
    vals = list(np.linspace(1, 100, scoring.PERCENTILE_WINDOW - 1)) + [50.5]
    score, _ = scoring._percentile_score(pd.Series(vals))
    assert abs(score) < 0.02


def test_percentile_score_insufficient_history_returns_nan():
    s = pd.Series(np.linspace(1, 100, scoring.PERCENTILE_WINDOW // 4 - 1))
    score, _ = scoring._percentile_score(s)
    assert np.isnan(score)


def test_percentile_score_reports_short_window():
    """短历史 (≥1年但<4年) 仍打分, 但必须如实返回实际窗口天数。"""
    n = 500
    score, n_used = scoring._percentile_score(pd.Series(np.linspace(1, 100, n)))
    assert n_used == n
    assert not np.isnan(score)
    note = scoring._percentile_note(50.0, n_used)
    assert "非4年" in note  # 2026-07 对抗性审查修复: 短窗口须明示


# ────────────────────────────────────────────────────────────
# _compute_bucket_scores
# ────────────────────────────────────────────────────────────

def _full_indicators(cfg, score=0.5):
    return {m: _ind(score) for b in cfg.values() for m in b["members"]}


def test_bucket_scores_full_coverage():
    inds = _full_indicators(scoring.CYCLE_BUCKETS, score=0.5)
    total, detail, cov = scoring._compute_bucket_scores(scoring.CYCLE_BUCKETS, inds)
    assert abs(total - 0.5) < 1e-9  # 所有因子同分 → 总分等于该分
    assert cov == 1.0
    assert set(detail.keys()) == set(scoring.CYCLE_BUCKETS.keys())


def test_bucket_scores_nan_member_excluded_not_neutral():
    """失败因子 (value=NaN) 应被剔除重归一, 而非按 0 分拖低桶分。"""
    cfg = {"桶A": {"weight": 1.0, "members": ["x", "y"], "note": ""}}
    inds = {"x": _ind(0.8), "y": _ind(0.0, value=float("nan"))}
    total, detail, cov = scoring._compute_bucket_scores(cfg, inds)
    assert abs(total - 0.8) < 1e-9
    assert cov == 1.0
    assert detail["桶A"]["members"][1]["score"] is None


def test_bucket_scores_dead_bucket_lowers_coverage():
    cfg = {
        "活桶": {"weight": 0.6, "members": ["x"], "note": ""},
        "死桶": {"weight": 0.4, "members": ["y"], "note": ""},
    }
    inds = {"x": _ind(0.5), "y": None}
    total, detail, cov = scoring._compute_bucket_scores(cfg, inds)
    assert abs(cov - 0.6) < 1e-9
    assert abs(total - 0.5) < 1e-9  # 缺桶后重归一
    assert detail["死桶"]["score"] is None


def test_bucket_scores_member_weights_applied():
    cfg = {"链上筹码": {"weight": 1.0,
                    "members": ["MVRV-Z", "STH成本线", "NUPL"],
                    "note": ""}}
    inds = {"MVRV-Z": _ind(1.0), "STH成本线": _ind(0.0), "NUPL": _ind(0.0)}
    total, _, _ = scoring._compute_bucket_scores(cfg, inds)
    # MEMBER_WEIGHTS 里 MVRV-Z 0.35, 三成员合 0.80 → 0.35/0.80
    assert abs(total - 0.35 / 0.80) < 1e-9


def test_bucket_scores_all_dead_is_zero_coverage():
    inds = {}
    total, _, cov = scoring._compute_bucket_scores(scoring.CYCLE_BUCKETS, inds)
    assert total == 0.0 and cov == 0.0


# ────────────────────────────────────────────────────────────
# compute_dual_scores 端到端 (df=None 跳过分位数覆盖, 不触网)
# ────────────────────────────────────────────────────────────

def test_compute_dual_scores_structure_and_range():
    inds = {**_full_indicators(scoring.CYCLE_BUCKETS, 0.3),
            **_full_indicators(scoring.TACTICAL_BUCKETS, -0.2)}
    out = scoring.compute_dual_scores(inds, None)
    for key in ("cycle_score", "cycle_recommendation", "cycle_buckets", "cycle_coverage",
                "tactical_score", "tactical_recommendation", "tactical_buckets",
                "tactical_coverage"):
        assert key in out
    assert -1 <= out["cycle_score"] <= 1
    assert -1 <= out["tactical_score"] <= 1
    assert out["cycle_coverage"] == 1.0 and out["tactical_coverage"] == 1.0
    assert "⚠️" not in out["cycle_recommendation"]


def test_compute_dual_scores_low_coverage_warns():
    inds = {"Mayer Multiple": _ind(0.3)}  # 仅 1 因子 → 覆盖率远低于 0.5
    out = scoring.compute_dual_scores(inds, None)
    assert out["cycle_coverage"] < 0.5
    assert "可信度低" in out["cycle_recommendation"]
    assert "可信度低" in out["tactical_recommendation"]


def _full_cycle_cards(score=0.3):
    """按 CYCLE_BUCKETS 造齐全套完整卡片形态指标。"""
    return {m: _ind_card(score) for b in scoring.CYCLE_BUCKETS.values()
            for m in b["members"]}


def test_compute_dual_scores_short_history_degrades_trend_bucket():
    """价格史 <400 天 (CoinGecko 365 天备源真实降级) 时, 趋势伸展桶的分位数
    归一化整体失效, 不得静默保留已被裁决淘汰的绝对阈值离散分。应按 NaN 剔除
    (与 backfill 同语义): 整桶退出评分、覆盖率如实下降。df=None (纯单元测试
    路径) 行为不变。"""
    trend_members = scoring.CYCLE_BUCKETS["趋势伸展"]["members"]

    # 对照组: df=None → 趋势桶保留, 覆盖率满
    out_none = scoring.compute_dual_scores(_full_cycle_cards(), None)
    assert out_none["cycle_coverage"] == 1.0
    assert out_none["cycle_buckets"]["趋势伸展"]["score"] is not None

    # 降级组: 300 天 (<400) 合成 df → 趋势桶成员被剔除, 覆盖率下降
    short_df = pd.DataFrame(
        {"price": np.linspace(100.0, 200.0, 300)},
        index=pd.date_range("2025-01-01", periods=300),
    )
    degraded = _full_cycle_cards()
    out_short = scoring.compute_dual_scores(degraded, short_df)

    # 覆盖率如实下降 (趋势伸展桶 0.25 权重退出, 其余 5 桶合 0.75)
    assert out_short["cycle_coverage"] < out_none["cycle_coverage"]
    assert abs(out_short["cycle_coverage"] - 0.75) < 1e-9

    # 整桶退出评分, 每个成员被剔除 (score=None)
    assert out_short["cycle_buckets"]["趋势伸展"]["score"] is None
    for md in out_short["cycle_buckets"]["趋势伸展"]["members"]:
        assert md["score"] is None

    # 底层指标被置为不可用: value=NaN → _compute_bucket_scores 剔除; 卡片标注降级原因
    for m in trend_members:
        assert np.isnan(degraded[m].value)
        assert "价格史不足" in degraded[m].status


def test_compute_dual_scores_ample_history_keeps_trend_bucket():
    """价格史 ≥400 天时不触发降级剔除, 趋势伸展桶正常参与评分。"""
    ample_df = pd.DataFrame(
        {"price": np.linspace(100.0, 200.0, 500)},
        index=pd.date_range("2024-01-01", periods=500),
    )
    inds = _full_cycle_cards()
    out = scoring.compute_dual_scores(inds, ample_df)
    assert out["cycle_coverage"] == 1.0
    assert out["cycle_buckets"]["趋势伸展"]["score"] is not None
    # 未被降级剔除 (value 仍有效)
    for m in scoring.CYCLE_BUCKETS["趋势伸展"]["members"]:
        assert not np.isnan(inds[m].value)
