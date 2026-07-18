#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
version_stamp 决策配置身份戳回归测试:
  1. 指纹稳定性 — 同配置多次调用同哈希; 格式为 12 位十六进制。
  2. 指纹敏感性 — 改档位阈值 / 桶权重 / 滞回参数 → 哈希变; 改档名/note 等展示
     文本 → 哈希不变 (只覆盖数值旋钮)。
  3. engine_sha — 读 RENDER_GIT_COMMIT 短 sha, 缺省兜底 'unknown'。
  4. 快照落盘往返 — record_score_snapshot 写入的条目含 config_hash + engine_sha
     两戳 (穿真实存储往返, 不 mock 存储层)。
"""
import re

import pytest

from btc_dashboard import version_stamp, scoring, decision
from btc_dashboard.score_history import record_score_snapshot, load_history_entries

_HEX12 = re.compile(r"^[0-9a-f]{12}$")


# ---------- 1. 指纹稳定性 ----------

def test_fingerprint_stable_and_shaped():
    h1 = version_stamp.config_fingerprint()
    h2 = version_stamp.config_fingerprint()
    assert h1 == h2, "同配置两次调用哈希应一致 (确定性)"
    assert _HEX12.match(h1), f"指纹应为 12 位十六进制, 得到 {h1!r}"


def test_fingerprint_ignores_member_order():
    """成员顺序不影响决策 (桶内取均值), 故重排不应改变指纹。"""
    baseline = version_stamp.config_fingerprint()
    orig = scoring.CYCLE_BUCKETS["趋势伸展"]["members"]
    reordered = dict(scoring.CYCLE_BUCKETS)
    reordered["趋势伸展"] = {**scoring.CYCLE_BUCKETS["趋势伸展"],
                             "members": list(reversed(orig))}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scoring, "CYCLE_BUCKETS", reordered)
        assert version_stamp.config_fingerprint() == baseline


# ---------- 2. 指纹敏感性 ----------

def test_fingerprint_changes_on_band_threshold():
    baseline = version_stamp.config_fingerprint()
    bumped = [(lo + 0.01, *rest) for (lo, *rest) in decision.CYCLE_BANDS]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(decision, "CYCLE_BANDS", bumped)
        assert version_stamp.config_fingerprint() != baseline


def test_fingerprint_changes_on_bucket_weight():
    baseline = version_stamp.config_fingerprint()
    tweaked = dict(scoring.CYCLE_BUCKETS)
    tweaked["资金流"] = {**scoring.CYCLE_BUCKETS["资金流"], "weight": 0.99}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scoring, "CYCLE_BUCKETS", tweaked)
        assert version_stamp.config_fingerprint() != baseline


def test_fingerprint_changes_on_member_weight():
    baseline = version_stamp.config_fingerprint()
    tweaked = dict(scoring.MEMBER_WEIGHTS)
    tweaked["链上筹码"] = {**scoring.MEMBER_WEIGHTS["链上筹码"], "MVRV-Z": 0.99}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scoring, "MEMBER_WEIGHTS", tweaked)
        assert version_stamp.config_fingerprint() != baseline


def test_fingerprint_changes_on_hysteresis():
    baseline = version_stamp.config_fingerprint()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(decision, "HYST_DELTA", decision.HYST_DELTA + 0.01)
        assert version_stamp.config_fingerprint() != baseline
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(decision, "HYST_CONFIRM", decision.HYST_CONFIRM + 1)
        assert version_stamp.config_fingerprint() != baseline


def test_fingerprint_ignores_cosmetic_label():
    """改档名/note 等展示文本不该误报'配置已变' —— 只覆盖数值旋钮。"""
    baseline = version_stamp.config_fingerprint()
    relabeled = [(lo, name + "★", *rest) for (lo, name, *rest) in decision.CYCLE_BANDS]
    tweaked_bucket = dict(scoring.CYCLE_BUCKETS)
    tweaked_bucket["资金流"] = {**scoring.CYCLE_BUCKETS["资金流"], "note": "改了注释"}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(decision, "CYCLE_BANDS", relabeled)
        mp.setattr(scoring, "CYCLE_BUCKETS", tweaked_bucket)
        assert version_stamp.config_fingerprint() == baseline


# ---------- 3. engine_sha ----------

def test_engine_sha_reads_render_env(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abcdef0123456789deadbeef")
    assert version_stamp.engine_sha() == "abcdef012345"  # 短 12 位


def test_engine_sha_fallback_unknown(monkeypatch):
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    assert version_stamp.engine_sha() == "unknown"


def test_engine_sha_blank_env_falls_back(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "   ")
    assert version_stamp.engine_sha() == "unknown"


# ---------- 4. 快照落盘往返 ----------

def _minimal_dashboard():
    return {
        "timestamp": "2026-07-18 00:00:00",
        "btc_price": 100000.0,
        "total_score": 0.3,
        "tactical_score": 0.1,
        "recommendation": "标准配置",
        "cycle_coverage": 0.92,
        "tactical_coverage": 0.85,
        "indicators": {
            "MVRV-Z": {"score": 0.2, "status": "中性", "value": 1.5},
        },
    }


def test_snapshot_roundtrip_carries_both_stamps(tmp_path, monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "0123456789abcdef")
    cache_dir = str(tmp_path)

    record_score_snapshot(_minimal_dashboard(), cache_dir)

    entries = load_history_entries(cache_dir)
    assert len(entries) == 1
    entry = entries[-1]
    assert entry["config_hash"] == version_stamp.config_fingerprint()
    assert entry["engine_sha"] == "0123456789ab"
    # 两戳紧挨 coverage 戳、与既有字段共存, 不破坏原 schema
    for key in ("total_score", "tactical_score", "cycle_coverage",
                "tactical_coverage", "scores", "statuses"):
        assert key in entry
