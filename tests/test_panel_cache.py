#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PanelCache 单元测试: SWR 面板缓存类(批次三 Y5 收敛)。
   partial 守卫语义 = 批次一 R1: 保旧+回拨重试; Thread.start 失败复位(Y8)。"""
from datetime import datetime

import panel_cache as pc
from panel_cache import PanelCache


def _mk(fetch, cache=None, ts=None, saves=None):
    p = PanelCache("t", fetch, ttl=600, label="测试面板",
                   save_fn=(lambda n, d, t: saves.append((n, d))) if saves is not None else None)
    p.cache, p.timestamp = cache, ts
    return p


def test_success_replaces_cache_and_saves():
    saves = []
    p = _mk(lambda: {"v": 1, "partial": False}, saves=saves)
    p._do_refresh()
    assert p.cache == {"v": 1, "partial": False}
    assert saves == [("t", {"v": 1, "partial": False})]
    assert p.age_s() is not None and p.age_s() < 5          # 时间戳新鲜


def test_partial_keeps_good_cache_backdates_no_save():
    saves = []
    good = {"v": 1, "partial": False}
    p = _mk(lambda: {"v": None, "partial": True}, cache=good, ts=datetime.now(), saves=saves)
    p._do_refresh()
    assert p.cache is good                                   # 旧完整缓存未被覆盖
    assert saves == []                                       # partial 不落盘
    assert p.age_s() >= p.ttl - 130                          # 回拨 → ~120s 后重试


def test_partial_without_old_caches_backdated_and_saves():
    saves = []
    part = {"v": None, "partial": True}
    p = _mk(lambda: part, saves=saves)
    p._do_refresh()
    assert p.cache is part
    assert len(saves) == 1
    assert p.age_s() >= p.ttl - 130


def test_fetch_exception_keeps_cache_resets_flag():
    good = {"v": 1}
    def boom():
        raise RuntimeError("down")
    p = _mk(boom, cache=good, ts=datetime.now())
    p._refreshing = True
    p._do_refresh()
    assert p.cache is good
    assert p._refreshing is False                            # finally 复位


def test_trigger_is_single_flight(monkeypatch):
    started = []
    class FakeThread:
        def __init__(self, target=None, daemon=None):
            self.target = target
        def start(self):
            started.append(1)                                # 不真跑, 保持 _refreshing=True
    monkeypatch.setattr(pc.threading, "Thread", FakeThread)
    p = _mk(lambda: {})
    p.trigger_refresh()
    p.trigger_refresh()                                      # 第二次应被单飞挡下
    assert len(started) == 1


def test_thread_start_failure_resets_flag(monkeypatch):
    class BoomThread:
        def __init__(self, target=None, daemon=None):
            pass
        def start(self):
            raise RuntimeError("cannot start thread")        # Render 512MB 内存压力场景
    monkeypatch.setattr(pc.threading, "Thread", BoomThread)
    p = _mk(lambda: {})
    p.trigger_refresh()
    assert p._refreshing is False                            # Y8: 不永久卡死, 下次请求可再触发


def test_constructor_restores_from_load_fn():
    ts = datetime(2026, 7, 13, 12, 0, 0)
    p = PanelCache("t", lambda: {}, ttl=600,
                   load_fn=lambda name: ({"v": 9}, ts))
    assert p.cache == {"v": 9} and p.timestamp == ts
