#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill.ensure_backfilled 的三块核心承诺回归测试:
  1. 幂等性 — v6 marker 存在即跳过, 不重复调 reconstruct、不产生重复条目
  2. marker 版本迁移 — 旧版 marker 触发清除-重建, 窗口扩到被清除最早日期, 旧 marker 删除
  3. 合并优先级 — 同日真实快照永远压过回填条目; 新回填压过旧回填; 窗口外旧回填保留

不触网 — 只 mock 网络取数层 (reconstruct 返回合成 entries), 存储层 (score_history.json /
marker / 文件锁) 全部走 tmp_path 真实落盘再读回。
"""
import json
import os
from datetime import datetime, timedelta

import pytest

from btc_dashboard import backfill

# reconstruct 合成条目的评分标记; 三个常量互不相同, 使合并归属可无歧义断言
BACKFILL_SCORE = -0.5   # 本轮回填条目
REAL_SCORE = 0.42       # 真实快照 (非 backfilled)
OLD_SCORE = 0.99        # 旧口径回填条目 (应被清除或压过)


def _entry(date: str, score: float, backfilled: bool, tactical: float = 0.0) -> dict:
    """构造一条与 reconstruct / record_score_snapshot 同 schema 的历史条目"""
    e = {
        "date": date,
        "ts": f"{date} 00:00:00",
        "btc_price": 50000.0,
        "total_score": score,
        "recommendation": "test",
        "tactical_score": tactical,
        "cycle_coverage": 1.0,
        "tactical_coverage": 1.0,
        "scores": {},
        "statuses": {},
    }
    if backfilled:
        e["backfilled"] = True
    return e


def _make_fake_reconstruct(record: dict):
    """
    替身 reconstruct: 不触网, 为过去 days 天 (不含今天) 逐日生成 backfilled 条目。
    record 记录调用次数与每次 days 入参, 供断言窗口扩展/幂等跳过。
    """
    def _fake(days, cache_dir=None):
        record["calls"] += 1
        record["days"].append(days)
        today = datetime.now()
        out = []
        for k in range(days, 0, -1):  # today-days .. today-1, 与真实 reconstruct 一样排除今天
            d = (today - timedelta(days=k)).strftime("%Y-%m-%d")
            out.append(_entry(d, BACKFILL_SCORE, backfilled=True))
        return out
    return _fake


def _hist_by_date(cache: str) -> dict:
    """真实读回磁盘上的 score_history.json, 按日期建索引"""
    return {e["date"]: e for e in backfill._load_history(cache)}


# ============================================================
# 1. 幂等性
# ============================================================

def test_ensure_backfilled_idempotent(tmp_path, monkeypatch):
    rec = {"calls": 0, "days": []}
    monkeypatch.setattr(backfill, "reconstruct", _make_fake_reconstruct(rec))
    cache = str(tmp_path)

    # 首跑: 写入条目 + v6 marker, 返回 True
    assert backfill.ensure_backfilled(cache, days=30) is True
    assert rec["calls"] == 1
    assert os.path.exists(os.path.join(cache, backfill._MARKER))
    hist1 = backfill._load_history(cache)
    assert len(hist1) == 30
    dates = [e["date"] for e in hist1]
    assert len(dates) == len(set(dates)), "回填条目不应有重复日期"

    # 二跑: marker 已存在 → 跳过, 不再调 reconstruct, 历史字节级不变 (无重复/无破坏)
    assert backfill.ensure_backfilled(cache, days=30) is False
    assert rec["calls"] == 1, "marker 命中时不应再触发 reconstruct"
    hist2 = backfill._load_history(cache)
    assert hist2 == hist1


# ============================================================
# 2. marker 版本迁移 (旧 marker → 清除-重建)
# ============================================================

def test_ensure_backfilled_marker_migration(tmp_path, monkeypatch):
    rec = {"calls": 0, "days": []}
    monkeypatch.setattr(backfill, "reconstruct", _make_fake_reconstruct(rec))
    cache = str(tmp_path)
    today = datetime.now()
    d_old = (today - timedelta(days=8)).strftime("%Y-%m-%d")   # 旧口径回填, 早于传入 days=3
    d_real = (today - timedelta(days=2)).strftime("%Y-%m-%d")  # 真实快照

    # 预置 v5 旧 marker + 混合历史 (旧口径回填条目 + 真实快照)
    v5 = os.path.join(cache, "score_history_backfilled.v5.marker")
    with open(v5, "w") as f:
        f.write("stale")
    backfill._save_history(cache, [
        _entry(d_old, OLD_SCORE, backfilled=True),
        _entry(d_real, REAL_SCORE, backfilled=False),
    ])

    # 传入 days=3, 但被清除条目最早在 8 天前 → 窗口须扩展到覆盖 d_old
    assert backfill.ensure_backfilled(cache, days=3) is True
    assert rec["calls"] == 1
    assert rec["days"][-1] >= 8, "重建窗口须扩到被清除条目最早日期"

    hist = _hist_by_date(cache)
    # 旧口径回填条目被按新口径重建 (score 变为回填标记值, 非 OLD_SCORE)
    assert d_old in hist
    assert hist[d_old]["total_score"] == BACKFILL_SCORE
    assert hist[d_old].get("backfilled") is True
    # 真实快照原样保留 (无 backfilled 标记, score 不动)
    assert hist[d_real]["total_score"] == REAL_SCORE
    assert "backfilled" not in hist[d_real]
    # v5 旧 marker 被删, v6 新 marker 建立
    assert not os.path.exists(v5)
    assert os.path.exists(os.path.join(cache, backfill._MARKER))


# ============================================================
# 3. 合并优先级 (已有条目 vs 回填条目谁赢)
# ============================================================

def test_ensure_backfilled_merge_priority(tmp_path, monkeypatch):
    rec = {"calls": 0, "days": []}
    monkeypatch.setattr(backfill, "reconstruct", _make_fake_reconstruct(rec))
    cache = str(tmp_path)
    today = datetime.now()
    d_real = (today - timedelta(days=4)).strftime("%Y-%m-%d")     # 真实快照, 落在回填窗口内
    d_bf_in = (today - timedelta(days=6)).strftime("%Y-%m-%d")    # 旧回填, 落在回填窗口内
    d_bf_out = (today - timedelta(days=200)).strftime("%Y-%m-%d")  # 旧回填, 在回填窗口外

    # 预置真实快照 + 两条旧回填 (窗口内/外各一); 无旧 marker → 走常规 (非 stale) 合并
    backfill._save_history(cache, [
        _entry(d_bf_out, OLD_SCORE, backfilled=True),
        _entry(d_bf_in, OLD_SCORE, backfilled=True),
        _entry(d_real, REAL_SCORE, backfilled=False),
    ])

    assert backfill.ensure_backfilled(cache, days=10) is True
    hist = _hist_by_date(cache)

    # 同日冲突: 真实快照胜出 (score 保持 REAL_SCORE, 不被回填值覆盖), 且不带 backfilled 标记
    assert hist[d_real]["total_score"] == REAL_SCORE
    assert "backfilled" not in hist[d_real]

    # 窗口内旧回填条目: 被本轮新回填压过 (刷新为 BACKFILL_SCORE)
    assert hist[d_bf_in]["total_score"] == BACKFILL_SCORE
    assert hist[d_bf_in].get("backfilled") is True

    # 窗口外旧回填条目: 原样保留 (历史深度不因回填单向缩水)
    assert d_bf_out in hist
    assert hist[d_bf_out]["total_score"] == OLD_SCORE

    # 全程无重复日期
    dates = [e["date"] for e in backfill._load_history(cache)]
    assert len(dates) == len(set(dates))


# ============================================================
# 4. 写盘失败不静默 (p1-6): marker 不误建 + 失败信号可见 + 异常上抛给重试循环
# ============================================================

def test_ensure_backfilled_write_failure_no_marker(tmp_path, monkeypatch, capsys):
    """
    磁盘满 (OSError 28) 等写盘失败时:
      - 绝不创建 marker (否则幂等短路→8 次重试被绕过→评分历史静默永久丢失)
      - 失败信号可见 (不再静默吞异常)
      - 异常向上抛出, 供 app.py 的 8 次重试循环捕获重试
    磁盘恢复后重试应成功建 marker (重试语义不变 — marker 未误建故可重建)。
    """
    rec = {"calls": 0, "days": []}
    monkeypatch.setattr(backfill, "reconstruct", _make_fake_reconstruct(rec))
    cache = str(tmp_path)

    # 在真实 _save_history 的序列化点注入 OSError(28) —— 不 mock 存储层本身,
    # 走真实 tmpfile / atomic-replace / 文件锁全链路, 只让"落盘那一下"失败
    orig_dump = json.dump
    state = {"fail": True}

    def _maybe_boom(*a, **k):
        if state["fail"]:
            raise OSError(28, "No space left on device")
        return orig_dump(*a, **k)

    monkeypatch.setattr(json, "dump", _maybe_boom)

    # 写盘失败必须向调用方抛出 (不再静默返回成功)
    with pytest.raises(OSError):
        backfill.ensure_backfilled(cache, days=30)

    # marker 绝不能被创建
    assert not os.path.exists(os.path.join(cache, backfill._MARKER))
    # 原子写: tmp 已清理, 正式历史文件未被写坏 / 未创建
    assert not os.path.exists(os.path.join(cache, "score_history.json"))
    assert not any(f.startswith(".score_history_") for f in os.listdir(cache))
    # 失败信号可见
    assert "失败" in capsys.readouterr().out

    # 磁盘恢复 → 重试成功: marker 建立、30 天历史真实落盘
    state["fail"] = False
    assert backfill.ensure_backfilled(cache, days=30) is True
    assert os.path.exists(os.path.join(cache, backfill._MARKER))
    assert len(backfill._load_history(cache)) == 30
