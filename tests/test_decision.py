#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
decision.py 冒烟测试: 滞回换档重放 + compute_decision 端到端。
不触网 — band_stats.json 为仓库内静态资产, 可直接依赖。
"""
from btc_dashboard import decision

# 档位速查 (与 decision.CYCLE_BANDS 对齐):
# 0 重仓区≥0.45 | 1 偏多≥0.30 | 2 标准≥0.15 | 3 中性≥0.00 | 4 减配≥-0.12 | 5 低配≥-0.30 | 6 防守
STD = 2   # 标准配置 [0.15, 0.30)
NEU = 3   # 中性观望 [0.00, 0.15)
BULL = 1  # 偏多配置 [0.30, 0.45)

N = decision.HYST_CONFIRM
D = decision.HYST_DELTA


def test_cycle_band_idx_boundaries():
    cases = [(0.50, 0), (0.45, 0), (0.4499, 1), (0.30, 1), (0.15, 2),
             (0.0, 3), (-0.001, 4), (-0.12, 4), (-0.121, 5), (-0.30, 5), (-0.31, 6)]
    for score, idx in cases:
        assert decision._cycle_band_idx(score) == idx, f"score={score}"


def test_band_bounds():
    assert decision._band_bounds(0) == (0.45, float("inf"))
    assert decision._band_bounds(2) == (0.15, 0.30)
    assert decision._band_bounds(6) == (float("-inf"), -0.30)


# ────────────────────────────────────────────────────────────
# replay_hysteresis — 防抖是决策层的核心承诺
# ────────────────────────────────────────────────────────────

def test_hysteresis_boundary_oscillation_does_not_flap():
    """分数在档位边界 ±δ 内来回抖动, 生效档位必须纹丝不动。"""
    lo = 0.15  # 标准配置下界
    scores = [0.20] + [lo - D + 0.01, lo + 0.01] * 20  # 0.11/0.16 交替, 未破 0.10
    seq, pending, _ = decision.replay_hysteresis(scores)
    assert all(b == STD for b in seq), "边界抖动导致档位翻转 — 滞回失效"
    assert pending is None


def test_hysteresis_confirmed_downshift():
    """决定性跌破 (越过 δ) 且持续 N 天 → 第 N 天生效换档。"""
    scores = [0.20] * 5 + [0.15 - D - 0.01] * (N + 2)  # 0.09 < 0.10
    seq, _, _ = decision.replay_hysteresis(scores)
    assert seq[4 + N] == NEU, f"第 {N} 个确认快照应换档"
    assert seq[4 + N - 1] == STD, "确认满足前不得换档"
    assert seq[-1] == NEU


def test_hysteresis_short_spike_ignored():
    """持续不足 N 天的破位是噪声, 不换档且回归后候选清零。"""
    scores = [0.20] * 5 + [0.09] * (N - 1) + [0.20] * 5
    seq, pending, days = decision.replay_hysteresis(scores)
    assert all(b == STD for b in seq)
    assert pending is None and days == 0


def test_hysteresis_reports_pending():
    scores = [0.20] * 5 + [0.09] * (N - 2)
    seq, pending, days = decision.replay_hysteresis(scores)
    assert seq[-1] == STD  # 尚未换档
    assert pending == NEU and days == N - 2


def test_hysteresis_empty_history():
    assert decision.replay_hysteresis([]) == ([], None, 0)


# ────────────────────────────────────────────────────────────
# compute_decision 端到端
# ────────────────────────────────────────────────────────────

def _dashboard(cycle=0.20, tactical=0.0, cov_c=0.95, cov_t=0.95, synthetic=False):
    return {"total_score": cycle, "tactical_score": tactical,
            "cycle_coverage": cov_c, "tactical_coverage": cov_t,
            "data_synthetic": synthetic}


def _history(scores):
    return [{"date": f"d{i}", "total_score": s} for i, s in enumerate(scores)]


def test_compute_decision_steady_state():
    out = decision.compute_decision(_dashboard(0.20), _history([0.20] * 60))
    c = out["cycle"]
    assert c["band"] == "标准配置"
    assert (c["target_lo"], c["target_hi"]) == (40, 60)
    assert c["action_type"] == "hold"
    assert c["pending"] is None
    assert c["raw_differs"] is False
    assert c["stats"] and set(c["stats"]) == {"90d", "180d", "365d"}
    assert out["tactical"]["band"] == "等待信号"
    assert out["tactical"]["stats"] and set(out["tactical"]["stats"]) == {"14d", "30d"}
    assert out["warnings"] == []
    assert out["hysteresis"]["history_days"] == 60


def test_compute_decision_upshift_moment():
    """历史末端刚满足确认 → 当日动作应为「上调」。"""
    hist = [0.20] * 30 + [0.30 + D + 0.01] * N  # 0.36 ≥ 0.30+δ
    out = decision.compute_decision(_dashboard(0.36), _history(hist))
    c = out["cycle"]
    assert c["band"] == "偏多配置"
    assert c["action_type"] == "increase"
    assert "上调" in c["action"]


def test_compute_decision_hysteresis_lag_flagged():
    """分数已破位但确认未满 → 生效档不变, raw_differs 提示滞回生效中。"""
    hist = [0.20] * 30 + [0.09] * (decision.HYST_CONFIRM - 2)
    out = decision.compute_decision(_dashboard(0.09), _history(hist))
    c = out["cycle"]
    assert c["band"] == "标准配置"      # 滞回维持
    assert c["raw_band"] == "中性观望"  # 原始档位已变
    assert c["raw_differs"] is True
    assert c["pending"] and c["pending"]["direction"] == "下调"


def test_compute_decision_warnings():
    out = decision.compute_decision(
        _dashboard(0.2, cov_c=0.4, cov_t=0.3, synthetic=True), _history([0.2] * 3))
    w = " | ".join(out["warnings"])
    assert "演示数据" in w
    assert "评分历史仅 3 天" in w
    assert "周期分因子覆盖率" in w and "战术分因子覆盖率" in w


def test_compute_decision_empty_history_degrades():
    out = decision.compute_decision(_dashboard(0.2), [])
    assert out["cycle"]["band"] == "标准配置"  # 单点退化仍给出档位
    assert any("评分历史" in w for w in out["warnings"])


# ────────────────────────────────────────────────────────────
# 窗口不变量 — 重放窗口滑动不得造成无确认的静默换档
# (回归 hysteresis-window: [-365:] 子窗左边界滑动使 seed 漂移)
# ────────────────────────────────────────────────────────────

# 复现序列: 0.13 落在标准档 [0.15,0.30) 的滞回缓冲 (下界-δ=0.10) 内 — 既不确认
# 破位也不触发候选; 生效档位纯由窗口起点 (0.16→标准 / 0.13→中性) 决定。恰 365 天,
# 等于旧 REPLAY_DAYS 窗口, 尾部再增一天即令 [-365:] 左边界滑过开头的 0.16。
_DRIFT_BASE = [0.16] * 5 + [0.13] * 360


def test_hysteresis_window_offset_no_silent_drift():
    """尾部增删 ±5/±10 天 (模拟历史随每日快照增长), 生效档位不得静默变化。

    修复前 compute_decision 取 [-365:] 子窗: 尾部增 5 天使左边界滑过开头 5 个 0.16,
    seed 从 0.16(标准) 跳到 0.13(中性), 无 δ/N 确认却静默降档、界面仍显示「维持」。
    """
    def held_band(scores):
        out = decision.compute_decision(_dashboard(scores[-1]), _history(scores))
        return out["cycle"]["band"]

    base_band = held_band(_DRIFT_BASE)
    assert base_band == "标准配置"
    for off in (5, 10, -5, -10):
        scores = _DRIFT_BASE + [0.13] * off if off > 0 else _DRIFT_BASE[:off]
        assert held_band(scores) == base_band, \
            f"窗口偏移 {off} 天导致无确认的静默换档: {held_band(scores)} != {base_band}"


def test_full_history_replay_not_365_subwindow():
    """>365 天历史: 滞回重放须用全量历史 (与 extract_events 同源), 而非 [-365:] 子窗。

    [0.16]*5 + [0.13]*400: 全量重放 seed=0.16→标准; 旧 [-365:] 子窗丢掉全部 0.16 →
    seed=0.13→中性。断言取全量口径 (标准), 证实两条 seq 分裂已消除。
    """
    scores = [0.16] * 5 + [0.13] * 400
    out = decision.compute_decision(_dashboard(0.13), _history(scores))
    assert out["cycle"]["band"] == "标准配置"
    # extract_events 同源: 档位全程不变 → 不产生任何「滞回换档」事件
    switches = [e for e in decision.extract_events(_history(scores))
                if "滞回" in e["label"]]
    assert switches == []


def test_silent_drift_flagged_in_warnings():
    """生效档位由窗口起点原始档决定、当前分数原始档已不同时, warnings 须如实标注。"""
    out = decision.compute_decision(_dashboard(0.13), _history(_DRIFT_BASE))
    c = out["cycle"]
    assert c["band"] == "标准配置" and c["raw_band"] == "中性观望"
    assert c["raw_differs"] is True
    assert any("窗口起点" in w for w in out["warnings"]), \
        "生效档位由窗口起点决定却未标注静默漂移风险"


def test_steady_state_no_silent_drift_warning():
    """稳态 (生效档==原始档): 新增的静默漂移警告不得误报。"""
    out = decision.compute_decision(_dashboard(0.20), _history([0.20] * 60))
    assert not any("窗口起点" in w for w in out["warnings"])
