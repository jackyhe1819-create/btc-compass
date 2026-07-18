#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gist_store 评分历史 GitHub Gist 持久化的核心承诺回归测试:
  1. 恢复往返 — push 的载荷经 GET 还原, ensure_backfilled 冷启动恢复深基线,
     只重建近端缺口 (老日期保 Gist 稳定字节, 近端由 reconstruct 刷新)
  2. marker 口径版本门 — 载荷 marker != 当前 → 拒绝恢复 + 打日志; ensure_backfilled
     不被旧口径 Gist 带偏, 照常全窗重建 (绝不让旧数据冲掉 v7 式口径修正)
  3. 无 env 静默禁用 — 任一环境变量缺失 → is_enabled False / fetch None / push False,
     ensure_backfilled 行为与接线前完全一致 (不尝试恢复、不收窄重建窗口)
  4. 推送失败降级 — 网络异常 / 非 200 一律返回 False 不抛, 不影响主流程
  5. token 脱敏 — 异常串内嵌 token 不得原样进日志 (CWE-532, notify 泄漏教训)
  6. 每日去抖 — record_score_snapshot 仅在跨日首次落盘时触发推送, 同日多刷不重复推

网络层 (gist_store.requests) 用内存版 GitHub Gist API 替身, 其余 (score_history.json /
marker / 文件锁 / 载荷序列化) 全走 tmp_path 真实落盘再读回。
"""
import json
from datetime import datetime, timedelta

import pytest

from btc_dashboard import gist_store, backfill, score_history

RESTORE_SCORE = 0.33   # Gist 恢复条目的哨兵分, 与回填/真实分互异, 可无歧义断言归属
BACKFILL_SCORE = -0.5  # reconstruct 替身产出的近端回填分


# ============================================================
# 测试替身: 内存版 GitHub Gist API + 合成条目 + 合成 reconstruct
# ============================================================

class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _FakeGitHub:
    """内存版 GitHub Gist API 替身: store[gid] = {filename: content_str}。"""
    def __init__(self):
        self.store = {}
        self.calls = {"get": 0, "patch": 0}

    def _gid(self, url):
        return url.rstrip("/").split("/")[-1]

    def get(self, url, headers=None, timeout=None):
        self.calls["get"] += 1
        files = self.store.get(self._gid(url))
        if files is None:
            return _Resp(200, {"files": {}})
        return _Resp(200, {"files": {name: {"content": c, "truncated": False}
                                     for name, c in files.items()}})

    def patch(self, url, headers=None, json=None, timeout=None):  # noqa: A002 (requests kwarg)
        self.calls["patch"] += 1
        cur = self.store.setdefault(self._gid(url), {})
        for name, spec in (json.get("files") or {}).items():
            cur[name] = spec["content"]
        return _Resp(200, {"id": self._gid(url)})


def _entry(date: str, score: float, backfilled: bool = True) -> dict:
    e = {
        "date": date, "ts": f"{date} 00:00:00", "btc_price": 50000.0,
        "total_score": score, "recommendation": "test", "tactical_score": 0.0,
        "cycle_coverage": 1.0, "tactical_coverage": 1.0, "scores": {}, "statuses": {},
    }
    if backfilled:
        e["backfilled"] = True
    return e


def _make_fake_reconstruct(record: dict):
    """替身 reconstruct: 不触网, 为过去 days 天 (不含今天) 逐日生成回填条目。"""
    def _fake(days, cache_dir=None):
        record["calls"] += 1
        record["days"].append(days)
        today = datetime.now()
        return [_entry((today - timedelta(days=k)).strftime("%Y-%m-%d"), BACKFILL_SCORE)
                for k in range(days, 0, -1)]
    return _fake


def _enable(monkeypatch, fake, token="ghp_secretTOKENabcdef0123456789"):
    monkeypatch.setattr(gist_store.requests, "get", fake.get)
    monkeypatch.setattr(gist_store.requests, "patch", fake.patch)
    monkeypatch.setenv("GIST_ID", "gid123")
    monkeypatch.setenv("GIST_TOKEN", token)


# ============================================================
# 1. 恢复往返 + 收窄近端重建
# ============================================================

def test_restore_round_trip_and_gap_only_rebuild(tmp_path, monkeypatch):
    fake = _FakeGitHub()
    _enable(monkeypatch, fake)
    rec = {"calls": 0, "days": []}
    monkeypatch.setattr(backfill, "reconstruct", _make_fake_reconstruct(rec))
    cache = str(tmp_path)
    today = datetime.now()

    # 深基线: 320 天连续回填, 最后一条在 2 天前 (≥ _GIST_TRUST_DEPTH)
    restored = [_entry((today - timedelta(days=k)).strftime("%Y-%m-%d"), RESTORE_SCORE)
                for k in range(321, 1, -1)]  # today-321 .. today-2
    assert len(restored) == 320
    assert gist_store.push_history_to_gist(gist_store.build_payload(restored)) is True

    # 冷启动: 本地无历史 → 从 Gist 恢复深基线 + 只重建近端缺口
    assert backfill.ensure_backfilled(cache, days=365) is True

    # 收窄生效: reconstruct 只被要求近端缺口 (2 天 gap + 7 余量), 远小于 365
    assert rec["days"][-1] < 365
    assert rec["days"][-1] == 9

    hist = {e["date"]: e for e in backfill._load_history(cache)}
    # 深基线被恢复且铺满 (320 老日期 + 近端新日期), 未整段重掷
    assert len(hist) >= 320
    # 远端老日期保 Gist 恢复的稳定字节 (RESTORE_SCORE)
    d_old = (today - timedelta(days=100)).strftime("%Y-%m-%d")
    assert hist[d_old]["total_score"] == RESTORE_SCORE
    # 近端缺口 (Gist 未覆盖的昨天) 由 reconstruct 刷新
    d_gap = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    assert hist[d_gap]["total_score"] == BACKFILL_SCORE
    # marker 建立 → 后续冷启动内幂等短路
    assert backfill.ensure_backfilled(cache, days=365) is False


def test_restore_preserves_real_snapshot(tmp_path, monkeypatch):
    """恢复合并优先级: 本地真实快照永远压过 Gist 恢复的回填条目。"""
    fake = _FakeGitHub()
    _enable(monkeypatch, fake)
    rec = {"calls": 0, "days": []}
    monkeypatch.setattr(backfill, "reconstruct", _make_fake_reconstruct(rec))
    cache = str(tmp_path)
    today = datetime.now()
    d_real = (today - timedelta(days=50)).strftime("%Y-%m-%d")

    restored = [_entry((today - timedelta(days=k)).strftime("%Y-%m-%d"), RESTORE_SCORE)
                for k in range(321, 1, -1)]
    gist_store.push_history_to_gist(gist_store.build_payload(restored))

    # 本地已有该日真实快照 (非 backfilled, 分值 0.77) —— 恢复不得覆盖它
    backfill._save_history(cache, [_entry(d_real, 0.77, backfilled=False)])
    # 本地已有 1 条 (< 300) → 仍触发恢复
    assert backfill.ensure_backfilled(cache, days=365) is True

    hist = {e["date"]: e for e in backfill._load_history(cache)}
    assert hist[d_real]["total_score"] == 0.77
    assert "backfilled" not in hist[d_real]


# ============================================================
# 2. marker 口径版本门
# ============================================================

def test_marker_mismatch_rejects_restore(monkeypatch, capsys):
    fake = _FakeGitHub()
    _enable(monkeypatch, fake)
    fake.store["gid123"] = {gist_store.GIST_FILENAME: json.dumps({
        "marker": "score_history_backfilled.v1.marker",   # 旧口径
        "config_hash": "deadbeef1234",
        "entries": [_entry("2026-01-01", RESTORE_SCORE)],
    })}
    assert gist_store.restore_entries(backfill._MARKER) is None
    out = capsys.readouterr().out
    assert "marker 不匹配" in out


def test_ensure_backfilled_ignores_stale_gist(tmp_path, monkeypatch):
    """Gist 里是旧口径历史时, ensure_backfilled 拒绝恢复, 照常全窗重建 (不收窄)。"""
    fake = _FakeGitHub()
    _enable(monkeypatch, fake)
    rec = {"calls": 0, "days": []}
    monkeypatch.setattr(backfill, "reconstruct", _make_fake_reconstruct(rec))
    cache = str(tmp_path)
    today = datetime.now()
    stale = [_entry((today - timedelta(days=k)).strftime("%Y-%m-%d"), RESTORE_SCORE)
             for k in range(321, 1, -1)]
    fake.store["gid123"] = {gist_store.GIST_FILENAME: json.dumps({
        "marker": "score_history_backfilled.v1.marker", "config_hash": "x",
        "entries": stale,
    })}

    assert backfill.ensure_backfilled(cache, days=30) is True
    # 未收窄: 全窗 30 天重建 (恢复被拒), 且 Gist 的 RESTORE_SCORE 不入本地历史
    assert rec["days"][-1] == 30
    hist = {e["date"]: e for e in backfill._load_history(cache)}
    assert all(e["total_score"] == BACKFILL_SCORE for e in hist.values())


# ============================================================
# 3. 无 env 静默禁用 (零行为变化)
# ============================================================

def test_disabled_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("GIST_ID", raising=False)
    monkeypatch.delenv("GIST_TOKEN", raising=False)
    assert gist_store.is_enabled() is False
    assert gist_store.fetch_history_from_gist() is None
    assert gist_store.restore_entries("anything") is None
    assert gist_store.push_history_to_gist({"marker": "m", "entries": []}) is False

    rec = {"calls": 0, "days": []}
    monkeypatch.setattr(backfill, "reconstruct", _make_fake_reconstruct(rec))
    assert backfill.ensure_backfilled(str(tmp_path), days=30) is True
    # 未尝试恢复、未收窄: reconstruct 照常收到全窗 30 天
    assert rec["days"] == [30]


def test_disabled_when_only_one_var_present(monkeypatch):
    monkeypatch.setenv("GIST_ID", "gid123")
    monkeypatch.delenv("GIST_TOKEN", raising=False)
    assert gist_store.is_enabled() is False
    monkeypatch.delenv("GIST_ID", raising=False)
    monkeypatch.setenv("GIST_TOKEN", "tok")
    assert gist_store.is_enabled() is False


# ============================================================
# 4. 推送/拉取失败降级 (绝不 crash 主流程)
# ============================================================

def test_push_failure_degrades(monkeypatch, capsys):
    fake = _FakeGitHub()
    _enable(monkeypatch, fake)

    def _boom(*a, **k):
        raise ConnectionError("network down")
    monkeypatch.setattr(gist_store.requests, "patch", _boom)
    assert gist_store.push_history_to_gist({"marker": "m", "entries": []}) is False
    assert "推送异常" in capsys.readouterr().out

    # 非 200 也降级为 False (不抛)
    monkeypatch.setattr(gist_store.requests, "patch", lambda *a, **k: _Resp(422, {}))
    assert gist_store.push_history_to_gist({"marker": "m", "entries": []}) is False
    assert "被拒" in capsys.readouterr().out


def test_fetch_failure_degrades(monkeypatch, capsys):
    fake = _FakeGitHub()
    _enable(monkeypatch, fake)
    monkeypatch.setattr(gist_store.requests, "get",
                        lambda *a, **k: _Resp(503, None))
    assert gist_store.fetch_history_from_gist() is None
    assert "拉取失败" in capsys.readouterr().out

    def _boom(*a, **k):
        raise TimeoutError("read timed out")
    monkeypatch.setattr(gist_store.requests, "get", _boom)
    assert gist_store.fetch_history_from_gist() is None
    assert "拉取异常" in capsys.readouterr().out


# ============================================================
# 5. token 脱敏 (日志不得泄漏完整凭据)
# ============================================================

def test_token_not_leaked_in_logs(monkeypatch, capsys):
    token = "ghp_SUPERSECRETtoken1234567890"
    fake = _FakeGitHub()
    _enable(monkeypatch, fake, token=token)

    def _boom(*a, **k):
        raise ConnectionError(f"auth failed with token {token} @ api.github.com")
    monkeypatch.setattr(gist_store.requests, "get", _boom)
    assert gist_store.fetch_history_from_gist() is None
    out = capsys.readouterr().out
    assert token not in out             # 完整 token 不得泄漏
    assert "ghp_" in out                # 掩码后保留前缀便于对账 (前4后4)

    monkeypatch.setattr(gist_store.requests, "patch", _boom)
    assert gist_store.push_history_to_gist({"marker": "m", "entries": []}) is False
    assert token not in capsys.readouterr().out


def test_redact_masks_token_but_keeps_diagnostics():
    tok = "ghp_SUPERSECRETtoken1234567890"
    masked = gist_store._redact(f"boom token={tok} end", tok)
    assert tok not in masked and "ghp_" in masked
    # 无凭据的普通串原样通过 (空 token 不误伤诊断信息)
    plain = "Read timed out. (read timeout=10)"
    assert gist_store._redact(plain, "") == plain


# ============================================================
# 6. 每日去抖: record_score_snapshot 仅跨日首次落盘时推送
# ============================================================

def test_record_snapshot_pushes_once_per_day(tmp_path, monkeypatch):
    pushes = []
    monkeypatch.setattr(score_history, "_push_history_to_gist_async",
                        lambda entries: pushes.append(list(entries)))
    dash = {"total_score": 0.12, "timestamp": "2026-07-18 00:00:00",
            "btc_price": 50000, "recommendation": "x", "tactical_score": 0.0,
            "cycle_coverage": 1.0, "tactical_coverage": 1.0, "indicators": {}}
    cache = str(tmp_path)

    score_history.record_score_snapshot(dash, cache)   # 跨日 (空→今天) → 推
    score_history.record_score_snapshot(dash, cache)   # 同日再刷 → 覆盖不推
    assert len(pushes) == 1
    # 落盘真实性: 当日只 1 条 (同日覆盖)
    assert len(backfill._load_history(cache)) == 1


def test_push_history_snapshot_noop_when_disabled(monkeypatch):
    """禁用时同步推送核心为空操作, 不触碰网络。"""
    monkeypatch.delenv("GIST_ID", raising=False)
    monkeypatch.delenv("GIST_TOKEN", raising=False)

    def _explode(*a, **k):
        raise AssertionError("禁用时不应触网")
    monkeypatch.setattr(gist_store.requests, "patch", _explode)
    score_history._push_history_snapshot([_entry("2026-07-18", 0.1)])  # 不抛即通过
