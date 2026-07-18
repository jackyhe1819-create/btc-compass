#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.version_stamp
===========================
决策配置身份戳 — 把"把分数变成仓位的那套配置"哈希成一个稳定指纹, 连同引擎
git sha 一起打进 score_history 快照, 使历史曲线的纵向可比性可核查。

动机: score_history 已保住"分数", 却没保住"把分数变成仓位的那套配置(档位/权重/
滞回)"。本用户评分配置改动频繁(因子移入移出、阈值重标定), 三个月后回看历史曲线
上一段 regime-shift, 无法区分是行情还是某天改了配置。config_hash 把项目已就此
问题起头的卫生(coverage 戳、backfill marker 版本)延伸到决策期配置, 且比它们已
接受的 coverage 计算更便宜。

- config_fingerprint(): 对决策相关配置 (CYCLE_BUCKETS + TACTICAL_BUCKETS +
  MEMBER_WEIGHTS + CYCLE_BANDS/TACTICAL_BANDS 数值阈值 + HYST_DELTA/HYST_CONFIRM)
  做 json.dumps(sort_keys=True) → sha256[:12]。这恰好=test_consistency 已守护的
  "四处同步"配置面, 哈希天然覆盖载荷旋钮。只取数值旋钮, 剔除档名/note/band_stats
  键等展示性文本 —— 改注释不该误报"配置已变"。
- engine_sha(): 优先读 Render 注入的 RENDER_GIT_COMMIT 环境变量 (部署实例可用、
  无需 shell git), 兜底 'unknown'。
"""

import os
import json
import hashlib


def _norm(x):
    """把非有限浮点 (档位最低界 -inf) 规约为确定字符串, 保证 JSON 严格有效且稳定。"""
    if isinstance(x, float) and x == float("-inf"):
        return "-inf"
    return x


def _fingerprint_payload(cycle_buckets, tactical_buckets, member_weights,
                         cycle_bands, tactical_bands, hyst_delta, hyst_confirm):
    """把决策相关配置规约成只含'旋钮'的规范结构 (剔除档名/note/band_stats 键等文本)。

    members 排序: 桶内取加权/等权均值, 成员顺序不影响决策, 故排序后再哈希, 使
    纯重排不误报为配置变更。
    """
    return {
        "cycle_buckets": {
            name: {"weight": b["weight"], "members": sorted(b["members"])}
            for name, b in cycle_buckets.items()
        },
        "tactical_buckets": {
            name: {"weight": b["weight"], "members": sorted(b["members"])}
            for name, b in tactical_buckets.items()
        },
        "member_weights": {
            bucket: dict(weights) for bucket, weights in member_weights.items()
        },
        # 只留数值旋钮: 下界阈值 + 仓位区间下限/上限/中值; 剔除档名与 band_stats 键
        "cycle_bands": [
            [_norm(lo), lo_pct, hi_pct, mid_pct]
            for (lo, _name, lo_pct, hi_pct, mid_pct, _key) in cycle_bands
        ],
        # 战术档位只有下界是数值旋钮; 执行节奏/说明文本属展示层, 不入指纹
        "tactical_bands": [_norm(band[0]) for band in tactical_bands],
        "hyst_delta": hyst_delta,
        "hyst_confirm": hyst_confirm,
    }


def _hash_payload(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def config_fingerprint() -> str:
    """决策配置的稳定 12 位十六进制指纹 (每次读实时配置, 配置一变即变)。"""
    # 惰性导入: 避免任何导入期环路, 并使 version_stamp 本身导入零成本
    from . import scoring, decision
    payload = _fingerprint_payload(
        scoring.CYCLE_BUCKETS,
        scoring.TACTICAL_BUCKETS,
        scoring.MEMBER_WEIGHTS,
        decision.CYCLE_BANDS,
        decision.TACTICAL_BANDS,
        decision.HYST_DELTA,
        decision.HYST_CONFIRM,
    )
    return _hash_payload(payload)


def engine_sha() -> str:
    """当前部署引擎的短 git sha。优先 Render 注入的 RENDER_GIT_COMMIT, 兜底 'unknown'。"""
    sha = (os.environ.get("RENDER_GIT_COMMIT") or "").strip()
    return sha[:12] if sha else "unknown"


def version_stamp() -> dict:
    """决策身份戳: {config_hash, engine_sha} —— 快照与 /api/version 共用。"""
    return {"config_hash": config_fingerprint(), "engine_sha": engine_sha()}
