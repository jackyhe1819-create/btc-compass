#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PanelCache 单元测试: SWR 面板缓存类(批次三 Y5 收敛)。
   partial 守卫语义 = 批次一 R1: 保旧+重试; Thread.start 失败复位(Y8)。
   时间戳解耦(p0-1-panel-cache): timestamp 只记真实数据龄, partial 重试节流
   走独立 _next_retry_at —— 不再靠回拨时间戳把陈旧数据伪装成新鲜。"""
from datetime import datetime, timedelta

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


def test_partial_keeps_good_cache_honest_age_no_save():
    saves = []
    good = {"v": 1, "partial": False}
    old_ts = datetime.now() - timedelta(hours=48)            # 旧完整缓存: 2 天前
    p = _mk(lambda: {"v": None, "partial": True}, cache=good, ts=old_ts, saves=saves)
    p._do_refresh()
    assert p.cache is good                                   # 旧完整缓存未被覆盖
    assert saves == []                                       # partial 不落盘
    # 核心回归: age_s 如实反映旧数据真实龄(~48h), 不再被回拨成 ttl-retry_backoff 冒充新鲜
    assert p.age_s() >= 48 * 3600 - 5
    # 但重试节流已就位且在 backoff 窗口内 (与对外年龄解耦)
    assert p._next_retry_at is not None
    assert p._next_retry_at > datetime.now()


def test_partial_without_old_cache_stores_fresh_partial_honest_age():
    saves = []
    part = {"v": None, "partial": True}
    p = _mk(lambda: part, saves=saves)
    p._do_refresh()
    assert p.cache is part
    assert len(saves) == 1
    assert p.age_s() < 5                                     # 现取的 partial 确系刚取 → age 如实报新鲜
    assert p._next_retry_at is not None                      # 节流仍就位, retry_backoff 后重试


def test_partial_throttles_trigger_within_backoff(monkeypatch):
    """partial 后: age 如实(旧数据龄) + trigger_refresh 在 backoff 窗口内被 _next_retry_at
    挡下, 到期后放行 —— 替代原来靠回拨时间戳做的隐式节流, 避免 age 一旦如实变大就
    每次请求都打上游。"""
    started = []
    class FakeThread:
        def __init__(self, target=None, daemon=None):
            pass
        def start(self):
            started.append(1)
    monkeypatch.setattr(pc.threading, "Thread", FakeThread)
    good = {"v": 1, "partial": False}
    old_ts = datetime.now() - timedelta(hours=48)
    p = _mk(lambda: {"v": None, "partial": True}, cache=good, ts=old_ts)
    p._do_refresh()                                          # 触发一次 partial → 设 _next_retry_at
    assert p.age_s() >= 48 * 3600 - 5                        # 对外年龄如实(旧完整数据 2 天)
    p.trigger_refresh()                                      # backoff 窗口内: 节流挡下, 不起线程
    assert started == []
    p._next_retry_at = datetime.now() - timedelta(seconds=1)  # 令节流到期
    p.trigger_refresh()                                      # 到期后: 放行, 起刷新线程
    assert started == [1]


def test_success_clears_retry_throttle():
    p = _mk(lambda: {"v": 1, "partial": False})
    p._next_retry_at = datetime.now() + timedelta(seconds=999)  # 假装此前 partial 设过节流
    p._do_refresh()
    assert p.cache == {"v": 1, "partial": False}
    assert p._next_retry_at is None                            # 全量成功 → 清除节流


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
