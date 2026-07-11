#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置一致性对账 — 把 CLAUDE.md 里"改档位阈值后须同步各处"的人工纪律变成机器检查。

周期分档位阈值存在于 5 处, 战术分 3 处, 滞回参数 3 处:
  1. btc_web/btc_dashboard/decision.py      CYCLE_BANDS / TACTICAL_BANDS / HYST_*
  2. btc_web/btc_dashboard/scoring.py       cycle_recommendation / tactical_recommendation
  3. btc_web/btc_dashboard/score_history.py _BANDS
  4. backtest/evaluate.py                   CYCLE_BANDS / TACTICAL_BANDS
  5. btc_web/btc_dashboard/triggers.py      _BAND_EDGES (触发价位表越界标签, 内嵌档名+仓位区间)
  6. btc_web/btc_dashboard/data/band_stats.json (由 backtest/run_backtest.py 生成)
任何一处改动未同步, 本文件必须红。
"""
import json
import os
import re

import pytest

from btc_dashboard import decision, scoring, score_history, triggers
from backtest import evaluate

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAND_STATS_PATH = os.path.join(
    REPO_ROOT, "btc_web", "btc_dashboard", "data", "band_stats.json")
RUN_BACKTEST_PATH = os.path.join(REPO_ROOT, "backtest", "run_backtest.py")

# 阈值扫描点: 全量程细扫 + 每个档位边界的精确值与 ±epsilon
def _sweep_scores(bands):
    pts = [x / 1000.0 for x in range(-990, 991, 7)]
    for lo, *_ in bands:
        if lo != float("-inf"):
            pts += [lo, lo - 1e-9, lo + 1e-9]
    return pts


@pytest.fixture(scope="module")
def band_stats():
    with open(BAND_STATS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ────────────────────────────────────────────────────────────
# decision.py ↔ backtest/evaluate.py
# ────────────────────────────────────────────────────────────

def test_cycle_bands_match_backtest():
    assert len(decision.CYCLE_BANDS) == len(evaluate.CYCLE_BANDS)
    for i, ((d_lo, d_name, d_plo, d_phi, d_mid, d_key),
            (e_lo, e_hi, e_label, e_pos)) in enumerate(
            zip(decision.CYCLE_BANDS, evaluate.CYCLE_BANDS)):
        assert d_lo == e_lo, f"档位{i}「{d_name}」下界: decision={d_lo} evaluate={e_lo}"
        assert d_key == e_label, f"档位{i} band_stats 键: decision={d_key!r} evaluate={e_label!r}"
        # 展示中值(整数%)与回测仓位(小数)允许 ≤0.5% 舍入差 (如 22 vs 0.225)
        assert abs(d_mid - e_pos * 100) <= 0.5, \
            f"档位{i}「{d_name}」目标中值: decision={d_mid}% evaluate={e_pos*100}%"
        assert d_name in d_key and f"{d_plo}-{d_phi}%" in d_key


def test_cycle_bands_contiguous_in_backtest():
    """evaluate 档位区间必须首尾相接无缝隙 (上界=上一档下界)。"""
    assert evaluate.CYCLE_BANDS[0][1] == float("inf")
    for i in range(1, len(evaluate.CYCLE_BANDS)):
        assert evaluate.CYCLE_BANDS[i][1] == evaluate.CYCLE_BANDS[i - 1][0], \
            f"档位{i}「{evaluate.CYCLE_BANDS[i][2]}」上界与上一档下界不接"


def test_tactical_bands_match_backtest():
    assert len(decision.TACTICAL_BANDS) == len(evaluate.TACTICAL_BANDS)
    for i, ((d_lo, d_name, _pace, _adv, d_key),
            (e_lo, e_hi, e_label)) in enumerate(
            zip(decision.TACTICAL_BANDS, evaluate.TACTICAL_BANDS)):
        assert d_lo == e_lo, f"战术档{i}「{d_name}」下界: decision={d_lo} evaluate={e_lo}"
        assert d_key == e_label
    for i in range(1, len(evaluate.TACTICAL_BANDS)):
        assert evaluate.TACTICAL_BANDS[i][1] == evaluate.TACTICAL_BANDS[i - 1][0]


# ────────────────────────────────────────────────────────────
# decision.py ↔ scoring.py 推荐文案
# ────────────────────────────────────────────────────────────

def test_cycle_recommendation_matches_bands():
    for s in _sweep_scores(decision.CYCLE_BANDS):
        _, name, plo, phi, *_ = decision.CYCLE_BANDS[decision._cycle_band_idx(s)]
        rec = scoring.cycle_recommendation(s)
        assert name in rec, f"score={s}: 档位「{name}」≠ 推荐「{rec}」"
        assert f"{plo}-{phi}%" in rec, f"score={s}: 仓位区间与推荐文案不符「{rec}」"


def test_tactical_recommendation_matches_bands():
    for s in _sweep_scores(decision.TACTICAL_BANDS):
        name = decision.TACTICAL_BANDS[decision._tactical_band_idx(s)][1]
        rec = scoring.tactical_recommendation(s)
        assert name in rec, f"score={s}: 战术档「{name}」≠ 推荐「{rec}」"


# ────────────────────────────────────────────────────────────
# decision.py ↔ score_history.py
# ────────────────────────────────────────────────────────────

def test_score_history_bands_match_decision():
    assert len(score_history._BANDS) == len(decision.CYCLE_BANDS)
    for (h_lo, h_label), (d_lo, d_name, *_) in zip(
            score_history._BANDS, decision.CYCLE_BANDS):
        assert h_lo == d_lo, f"「{d_name}」下界: score_history={h_lo} decision={d_lo}"
        assert h_label == d_name


# ────────────────────────────────────────────────────────────
# decision.py ↔ triggers.py 触发价位表档位边界
# ────────────────────────────────────────────────────────────

def test_trigger_band_edges_match_decision():
    """
    triggers._BAND_EDGES (触发价位表的档位越界标签) 内嵌了阈值、档名、仓位区间,
    但此前无任何一致性测试守护 —— 改 decision.CYCLE_BANDS 却漏改 triggers 时,
    触发价位表会静默按旧边界算档位。本测试补上这道网。

    每条边界 (阈值, "进/跌入X档 (a-b%仓)", 方向) 的不变式:
      - 档名 X 必须唯一对应 CYCLE_BANDS 里一个真实档
      - 内嵌仓位区间 a-b% 必须等于该档的 (仓位下限, 仓位上限)
      - 阈值: up 边界 = 该档自身下界; down 边界 = 上一档 (更高档) 的下界
        (向上穿过某档下界即"进"该档; 向下穿过上一档下界即"跌入"下一档)
    """
    pos_re = re.compile(r"\((\d+)-(\d+)%仓\)")
    for thr, label, direction in triggers._BAND_EDGES:
        assert direction in ("up", "down"), f"未知方向「{direction}」: {label!r}"

        # 档名: 取唯一一个作为 label 子串出现的 CYCLE_BANDS 档名
        # (前 3 条带"档"后缀、防守区无, 故用子串匹配而非精确切割)
        matched = [(i, b) for i, b in enumerate(decision.CYCLE_BANDS) if b[1] in label]
        assert len(matched) == 1, \
            f"触发边界 {label!r} 未能唯一对应 CYCLE_BANDS 一档 (匹配到 {[b[1] for _, b in matched]})"
        idx, (b_lo, name, plo, phi, *_) = matched[0]

        # 内嵌仓位区间
        m = pos_re.search(label)
        assert m, f"触发边界 {label!r} 未能解析仓位区间 (格式变了?)"
        assert (int(m.group(1)), int(m.group(2))) == (plo, phi), \
            f"「{name}」仓位区间: triggers={m.group(1)}-{m.group(2)}% decision={plo}-{phi}%"

        # 阈值对应 (up=该档下界; down=上一档下界)
        if direction == "up":
            assert thr == b_lo, \
                f"「{name}」up 边界阈值 triggers={thr} ≠ 该档下界 decision={b_lo}"
        else:
            assert idx >= 1, f"「{name}」是最高档, 不应有向下跌入边界"
            above_lo = decision.CYCLE_BANDS[idx - 1][0]
            assert thr == above_lo, \
                f"「{name}」down 边界阈值 triggers={thr} ≠ 上一档下界 decision={above_lo}"


# ────────────────────────────────────────────────────────────
# decision.py ↔ band_stats.json ↔ backtest/run_backtest.py
# ────────────────────────────────────────────────────────────

def test_hysteresis_params_match_band_stats(band_stats):
    hyst = band_stats.get("hysteresis", {})
    assert hyst.get("delta") == decision.HYST_DELTA, \
        "滞回 δ 不一致 — 改了 decision.HYST_DELTA 后须重跑 backtest/run_backtest.py"
    assert hyst.get("confirm") == decision.HYST_CONFIRM, \
        "滞回确认天数不一致 — 须重跑 backtest/run_backtest.py"


def test_hysteresis_params_match_backtest_source():
    """run_backtest.py 里滞回参数是字面量, 从源码解析对账。"""
    with open(RUN_BACKTEST_PATH, "r", encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"HYST_DELTA\s*,\s*HYST_CONFIRM\s*=\s*([0-9.]+)\s*,\s*([0-9]+)", src)
    assert m, "run_backtest.py 中未找到 HYST_DELTA, HYST_CONFIRM 定义 (解析规则或代码已变)"
    assert float(m.group(1)) == decision.HYST_DELTA
    assert int(m.group(2)) == decision.HYST_CONFIRM


def test_band_stats_covers_all_bands(band_stats):
    """band_stats.json 的档位键与 decision 定义严格互为全集 (无缺失、无改名残留)。"""
    cycle_keys = {b[5] for b in decision.CYCLE_BANDS}
    tact_keys = {b[4] for b in decision.TACTICAL_BANDS}
    assert set(band_stats["cycle"].keys()) == cycle_keys, \
        "cycle 档位键不一致 — 改档位阈值/命名后须重跑 backtest/run_backtest.py"
    assert set(band_stats["tactical"].keys()) == tact_keys, \
        "tactical 档位键不一致 — 须重跑 backtest/run_backtest.py"


def test_band_stats_entries_complete(band_stats):
    for key, entry in band_stats["cycle"].items():
        for w in ("30d", "90d", "180d", "365d"):
            assert w in entry, f"cycle「{key}」缺 {w} 窗口"
            for field in ("n", "mean", "median", "win"):
                assert field in entry[w], f"cycle「{key}」{w} 缺 {field}"
            assert entry[w]["n"] > 0, f"cycle「{key}」{w} 样本数为 0"
    for key, entry in band_stats["tactical"].items():
        for w in ("7d", "14d", "30d"):
            assert w in entry, f"tactical「{key}」缺 {w} 窗口"
            assert entry[w]["n"] > 0, f"tactical「{key}」{w} 样本数为 0"


# ────────────────────────────────────────────────────────────
# scoring.py 因子桶配置自洽
# ────────────────────────────────────────────────────────────

def test_bucket_weights_sum_to_one():
    for cfg_name, cfg in (("CYCLE_BUCKETS", scoring.CYCLE_BUCKETS),
                          ("TACTICAL_BUCKETS", scoring.TACTICAL_BUCKETS)):
        total = sum(b["weight"] for b in cfg.values())
        assert abs(total - 1.0) < 1e-9, f"{cfg_name} 桶权重合计 {total} ≠ 1.0"


def test_member_weights_reference_real_members():
    all_buckets = {**scoring.CYCLE_BUCKETS, **scoring.TACTICAL_BUCKETS}
    for bucket_name, weights in scoring.MEMBER_WEIGHTS.items():
        assert bucket_name in all_buckets, f"MEMBER_WEIGHTS 引用不存在的桶「{bucket_name}」"
        members = set(all_buckets[bucket_name]["members"])
        for m, w in weights.items():
            assert m in members, f"「{bucket_name}」的成员权重引用不存在的因子「{m}」"
            assert w > 0
