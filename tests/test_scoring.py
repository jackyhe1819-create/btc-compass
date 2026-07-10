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
                    "members": ["MVRV-Z", "STH成本线", "NUPL", "交易所余额"],
                    "note": ""}}
    inds = {"MVRV-Z": _ind(1.0), "STH成本线": _ind(0.0),
            "NUPL": _ind(0.0), "交易所余额": _ind(0.0)}
    total, _, _ = scoring._compute_bucket_scores(cfg, inds)
    assert abs(total - 0.35) < 1e-9  # MEMBER_WEIGHTS 里 MVRV-Z 权重 0.35


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
