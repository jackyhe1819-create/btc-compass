#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SWR 面板缓存 — app.py 六份复制粘贴样板的收敛(批次三 Y5)。

一个面板 = 一个 PanelCache 实例: 内存缓存 + 时间戳 + 后台单飞刷新 +
partial 守卫(批次一 R1 语义) + 磁盘持久化(save/load 依赖注入, 路径归 app 层)。
复制样板的政策漂移(builders 有空数据保护而 options/probdist 没有)正是
批次一唯一 high 缺陷的根因 — 收敛后新面板自动继承正确政策。
"""
import threading
import traceback
from datetime import datetime, timedelta


class PanelCache:
    def __init__(self, name, fetch_fn, ttl, label=None,
                 save_fn=None, load_fn=None, retry_backoff=120):
        self.name = name
        self.fetch_fn = fetch_fn
        self.ttl = ttl
        self.label = label or name
        self.retry_backoff = retry_backoff
        self._save = save_fn or (lambda name, data, ts: None)
        self._lock = threading.Lock()
        self._refreshing = False
        self.cache = None
        self.timestamp = None
        if load_fn is not None:
            data, ts = load_fn(name)
            if data is not None:
                self.cache, self.timestamp = data, ts
                print(f"📦 从磁盘恢复{self.label}缓存（{ts.strftime('%Y-%m-%d %H:%M:%S')}）")

    def age_s(self):
        if self.timestamp is None:
            return None
        return int((datetime.now() - self.timestamp).total_seconds())

    def trigger_refresh(self):
        """单飞触发后台刷新; Thread.start 失败必须复位 _refreshing,
        否则该面板到进程重启前永不再刷新(Y8)。"""
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        try:
            threading.Thread(target=self._do_refresh, daemon=True).start()
        except Exception as e:
            with self._lock:
                self._refreshing = False
            print(f"⚠️ {self.label}刷新线程启动失败: {e}")

    def _backdated(self):
        return datetime.now() - timedelta(seconds=self.ttl - self.retry_backoff)

    def _do_refresh(self):
        try:
            data = self.fetch_fn()
            if isinstance(data, dict) and data.get("partial"):
                if self.cache and not self.cache.get("partial"):
                    # 保留旧完整缓存, 回拨时间戳 → retry_backoff 秒后自动重试
                    self.timestamp = self._backdated()
                    print(f"⚠️ {self.label}刷新为 partial, 保留旧完整缓存, {self.retry_backoff}s 后重试")
                    return
                self.cache = data
                self.timestamp = self._backdated()
                self._save(self.name, self.cache, self.timestamp)
                print(f"⚠️ {self.label}为 partial(无旧缓存), {self.retry_backoff}s 后重试")
                return
            self.cache = data
            self.timestamp = datetime.now()
            self._save(self.name, self.cache, self.timestamp)
            print(f"✅ {self.label}缓存刷新完成 {self.timestamp.strftime('%H:%M:%S')}")
        except Exception as e:
            traceback.print_exc()
            print(f"⚠️ {self.label}缓存刷新失败: {e}")
        finally:
            self._refreshing = False
