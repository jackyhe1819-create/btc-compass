#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py 并发收口测试 (2026-07 concurrency)
==========================================
锁定三件收口的可观测不变量:

1. `_do_refresh_dashboard` 原子发布 —— 完整结果 (含 decision/cycle_phase/notify)
   先在局部 dict 算齐, 最后一次性替换全局 `_dashboard_cache`。刷新期间读者只会看到
   "旧的完整缓存"或"新的完整缓存", 绝无"新时间戳但缺字段"的半成品。
   (旧实现先挂空壳 dict 再原地补字段, 存在该中间态。)

2. 磁盘缓存同样只落完整快照 (真实落盘往返 tmp_path, 不 mock 存储层)。

3. `get_cached_btc_data` 由 `_btc_data_lock` 单飞: 并发命中过期缓存时只 fetch 一次,
   并发者复用同一份 df。

设计取舍: 领域计算函数 (run_dashboard / compute_decision / cycle_phase / notify /
score-snapshot) 被替身以保持确定性且不打真实网络 —— 它们是被编排的对象, 非本测试要
验证的存储层; 存储层 (`_save_cache_to_disk` + 磁盘) 保持真实往返。
"""
import json
import threading
import time
from datetime import datetime, timedelta

import pandas as pd

import app as appmod
import btc_dashboard.cycle_phase as cycle_phase_mod
import btc_dashboard.notify as notify_mod


# 刷新完成后, 完整缓存必须携带的全部键 (基础字段 + 三个后置字段)
_EXPECTED_KEYS = {
    "timestamp", "btc_price", "total_score", "recommendation",
    "tactical_score", "tactical_recommendation", "cycle_buckets",
    "tactical_buckets", "data_source", "data_synthetic", "cycle_coverage",
    "tactical_coverage", "trigger_levels", "indicators", "sparklines",
    "cycle_factor_coverage", "tactical_factor_coverage",
    "price_stale", "price_history_last_date", "price_history_lag_days",
    "decision", "cycle_phase", "notify",
}


class _FakeIndicator:
    name = "MVRV-Z"
    value = 1.0
    score = 0.5
    color = "green"
    status = "ok"
    priority = "P0"
    url = ""
    description = ""
    method = ""


class _FakeResult:
    """DashboardResult 的最小替身 (仅供 _do_refresh_dashboard 读取的属性)。"""
    def __init__(self, synthetic=False, stale=False):
        self.timestamp = datetime(2026, 7, 18, 12, 0, 0)
        self.btc_price = 118000.0
        self.indicators = {"MVRV-Z": _FakeIndicator()}
        self.total_score = 0.3
        self.recommendation = "增持"
        self.tactical_score = 0.1
        self.tactical_recommendation = "观望"
        self.cycle_buckets = {
            "估值": {"weight": 1.0, "score": 0.2,
                     "members": [{"name": "MVRV-Z", "score": 0.5}]},
        }
        self.tactical_buckets = {
            "动量": {"weight": 1.0, "score": 0.1,
                     "members": [{"name": "RSI", "score": 0.1}]},
        }
        self.data_source = "test-source"
        self.data_synthetic = synthetic
        self.cycle_coverage = 1.0
        self.tactical_coverage = 1.0
        self.trigger_levels = None
        self.price_stale = stale
        self.price_history_last_date = "2026-07-18"
        self.price_history_lag_days = 12 if stale else 0
        self.price_df = _make_df()  # 评分 df 复用通道 (app 回灌共享缓存)


def _make_df():
    idx = pd.date_range("2026-06-01", periods=30, freq="D")
    return pd.DataFrame({"price": [float(100 + i) for i in range(30)]}, index=idx)


def _patch_refresh_deps(monkeypatch, tmp_path, *, on_produce=None, phase_delay=0.0,
                        synthetic=False, stale=False, snapshot_calls=None):
    """把 _do_refresh_dashboard 的所有领域依赖替换为确定性替身, 存储层保持真实。

    on_produce(): 每个字段生产者被调用时的回调 (用于观测发布时机)。
    snapshot_calls: 传入 list 时, 每次 record_score_snapshot 被调用追加一次 (观测跳过)。
    """
    def _hook():
        if on_produce is not None:
            on_produce()

    monkeypatch.setattr(appmod, "_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(appmod, "run_dashboard",
                        lambda: _FakeResult(synthetic=synthetic, stale=stale))
    monkeypatch.setattr(appmod, "get_cached_btc_data", _make_df)
    monkeypatch.setattr(appmod, "get_sparklines",
                        lambda df, inds, days=7: {"MVRV-Z": [1.0, 2.0, 3.0]})

    def _snapshot(dashboard, cache_dir):
        if snapshot_calls is not None:
            snapshot_calls.append(dashboard)
        _hook()
    monkeypatch.setattr(appmod, "record_score_snapshot", _snapshot)
    monkeypatch.setattr(appmod, "load_history_entries", lambda cd: [])

    def _decision(dashboard, entries):
        _hook()
        return {"target_position": 0.5, "regime": "hold"}
    monkeypatch.setattr(appmod, "compute_decision", _decision)

    def _phase(entries, price_by_date):
        if phase_delay:
            time.sleep(phase_delay)
        _hook()
        return {"phase": "markup", "confidence": 0.6}
    monkeypatch.setattr(cycle_phase_mod, "compute_cycle_phase", _phase)

    def _alert(dashboard, cache_dir):
        _hook()
        return {"channels": [], "any": False}
    monkeypatch.setattr(notify_mod, "check_and_alert", _alert)


def test_refresh_publishes_atomically(monkeypatch, tmp_path):
    """全部字段生产者运行时, 全局缓存仍是旧的完整对象; 返回后才整体替换为新完整对象。"""
    OLD = {k: "OLD" for k in _EXPECTED_KEYS}
    OLD["marker"] = "OLD-COMPLETE"
    observed_during = []

    _patch_refresh_deps(
        monkeypatch, tmp_path,
        on_produce=lambda: observed_during.append(appmod._dashboard_cache),
    )
    monkeypatch.setattr(appmod, "_dashboard_cache", OLD)
    monkeypatch.setattr(appmod, "_dashboard_cache_timestamp",
                        datetime.now() - timedelta(seconds=30))

    appmod._do_refresh_dashboard()

    # 生产 decision/cycle_phase/notify/snapshot 期间, 全局始终是那份旧的完整缓存 ——
    # 证明字段累积在局部 dict 上, 未原地污染已发布的全局。
    assert observed_during, "字段生产者应当被调用"
    assert all(o is OLD for o in observed_during), (
        "刷新中途全局 _dashboard_cache 被改写 —— 读者可见半成品!"
    )

    # 返回后: 全局已是新的、完整的 dict, 且不是旧对象。
    new_cache = appmod._dashboard_cache
    assert new_cache is not OLD
    assert _EXPECTED_KEYS.issubset(new_cache.keys()), (
        f"发布的缓存缺字段: {_EXPECTED_KEYS - set(new_cache.keys())}"
    )
    assert new_cache["decision"] == {"target_position": 0.5, "regime": "hold"}
    assert new_cache["cycle_phase"] == {"phase": "markup", "confidence": 0.6}
    assert new_cache["notify"] == {"channels": [], "any": False}
    assert appmod._dashboard_cache_timestamp is not None

    # 磁盘缓存同样是完整快照 (真实落盘往返, 非 mock 存储层)。
    with open(tmp_path / "dashboard.json", encoding="utf-8") as f:
        disk = json.load(f)
    assert _EXPECTED_KEYS.issubset(disk["data"].keys()), (
        f"磁盘缓存缺字段: {_EXPECTED_KEYS - set(disk['data'].keys())}"
    )
    assert disk["data"]["decision"] == {"target_position": 0.5, "regime": "hold"}


def test_concurrent_reader_never_sees_partial(monkeypatch, tmp_path):
    """并发读者线程在整个刷新过程中反复快照全局缓存, 永不撞见缺字段的半成品。"""
    OLD = {k: "OLD" for k in _EXPECTED_KEYS}

    # 在 cycle_phase 处注入延迟, 拉宽"若非原子则半成品可见"的时间窗。
    _patch_refresh_deps(monkeypatch, tmp_path, phase_delay=0.05)
    monkeypatch.setattr(appmod, "_dashboard_cache", OLD)
    monkeypatch.setattr(appmod, "_dashboard_cache_timestamp", datetime.now())

    partials = []
    stop = threading.Event()

    def _reader():
        # 读者语义同 api_dashboard 的 {**_dashboard_cache}: 先抓引用, 再看完整性。
        while not stop.is_set():
            snap = appmod._dashboard_cache
            if snap is not None and not _EXPECTED_KEYS.issubset(snap.keys()):
                partials.append(sorted(snap.keys()))

    reader = threading.Thread(target=_reader)
    reader.start()
    try:
        appmod._do_refresh_dashboard()
        time.sleep(0.02)  # 给读者继续观测已发布的新缓存
    finally:
        stop.set()
        reader.join(timeout=2)

    assert not partials, f"读者观测到半成品缓存状态: {partials[:3]}"
    assert _EXPECTED_KEYS.issubset(appmod._dashboard_cache.keys())


def test_synthetic_refresh_still_atomic_and_complete(monkeypatch, tmp_path):
    """合成数据路径 (跳过快照/相位) 也必须发布携带全部键的完整缓存。"""
    OLD = {k: "OLD" for k in _EXPECTED_KEYS}
    observed_during = []
    _patch_refresh_deps(
        monkeypatch, tmp_path, synthetic=True,
        on_produce=lambda: observed_during.append(appmod._dashboard_cache),
    )
    monkeypatch.setattr(appmod, "_dashboard_cache", OLD)
    monkeypatch.setattr(appmod, "_dashboard_cache_timestamp", datetime.now())

    appmod._do_refresh_dashboard()

    new_cache = appmod._dashboard_cache
    assert new_cache is not OLD
    assert _EXPECTED_KEYS.issubset(new_cache.keys())
    # 合成路径: 相位熔断为 None, 但键必须在场 (完整性 != 值非空)。
    assert new_cache["cycle_phase"] is None
    assert new_cache["data_synthetic"] is True
    # 中途未污染全局。
    assert all(o is OLD for o in observed_during)


def test_stale_price_skips_snapshot_but_publishes(monkeypatch, tmp_path):
    """价格全源陈旧 (price_stale) → 跳过评分快照 (不污染滞回重放历史), 但缓存照常
    完整发布 (陈旧数据仍供展示, frozen 已置灰可执行数字)。对照 data_synthetic 同款跳过。"""
    snapshot_calls = []
    _patch_refresh_deps(monkeypatch, tmp_path, stale=True, snapshot_calls=snapshot_calls)
    monkeypatch.setattr(appmod, "_dashboard_cache", None)
    monkeypatch.setattr(appmod, "_dashboard_cache_timestamp", None)

    appmod._do_refresh_dashboard()

    # 快照被跳过 —— 陈旧分数不进 score_history。
    assert snapshot_calls == [], "price_stale 时评分快照仍被写入 — 会污染滞回重放历史"
    # 但缓存仍完整发布 (展示层不断供)。
    new_cache = appmod._dashboard_cache
    assert new_cache is not None and _EXPECTED_KEYS.issubset(new_cache.keys())
    assert new_cache["price_stale"] is True


def test_fresh_price_still_records_snapshot(monkeypatch, tmp_path):
    """对照组: 价格新鲜 (非 stale/非 synthetic) 时快照照常写入 — 证明跳过是 stale 专属。"""
    snapshot_calls = []
    _patch_refresh_deps(monkeypatch, tmp_path, snapshot_calls=snapshot_calls)
    monkeypatch.setattr(appmod, "_dashboard_cache", None)
    monkeypatch.setattr(appmod, "_dashboard_cache_timestamp", None)

    appmod._do_refresh_dashboard()

    assert len(snapshot_calls) == 1, "新鲜价格快照应正常写入"


def test_get_cached_btc_data_dedups_concurrent_fetch(monkeypatch):
    """并发命中空/过期缓存时, _btc_data_lock 保证只 fetch 一次, 并发者复用同一份 df。"""
    fetch_calls = []
    start = threading.Barrier(6)  # 5 worker + 主线程, 逼近同时触发

    def _slow_fetch():
        fetch_calls.append(1)
        time.sleep(0.05)  # 拉长 fetch, 让无锁实现有机会重复穿透
        idx = pd.date_range("2026-06-01", periods=3, freq="D")
        return pd.DataFrame({"price": [1.0, 2.0, 3.0]}, index=idx)

    monkeypatch.setattr(appmod, "fetch_btc_data", _slow_fetch)
    monkeypatch.setattr(appmod, "_btc_data_cache", None)
    monkeypatch.setattr(appmod, "_btc_data_timestamp", None)

    results = []

    def _worker():
        start.wait()
        results.append(appmod.get_cached_btc_data())

    workers = [threading.Thread(target=_worker) for _ in range(5)]
    for w in workers:
        w.start()
    start.wait()  # 与 worker 同步放行
    for w in workers:
        w.join(timeout=3)

    assert len(fetch_calls) == 1, f"加锁后应只 fetch 一次, 实际 {len(fetch_calls)} 次"
    assert len(results) == 5
    # 所有并发者拿到的是同一个缓存对象 (单飞复用, 非各自的 df)。
    assert all(r is results[0] for r in results)
    assert appmod._btc_data_cache is results[0]
