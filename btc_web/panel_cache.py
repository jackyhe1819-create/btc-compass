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
        # timestamp = 当前所服务缓存数据的真实产生时刻 (对外 age_s 只认它);
        # _next_retry_at = partial 后的重试节流闸门, 与 timestamp 解耦, 互不兼职。
        self._next_retry_at = None
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
        否则该面板到进程重启前永不再刷新(Y8)。
        partial 重试节流走 _next_retry_at (与对外年龄解耦): timestamp 如实反映
        旧数据龄后, _serve_panel 见 age≥ttl 会每次请求都触发, 若不在此挡下就会
        在 backoff 窗口内反复打上游 —— 故节流下沉到这里, 而非靠回拨时间戳假装新鲜。"""
        with self._lock:
            if self._refreshing:
                return
            if self._next_retry_at is not None and datetime.now() < self._next_retry_at:
                return
            self._refreshing = True
        try:
            threading.Thread(target=self._do_refresh, daemon=True).start()
        except Exception as e:
            with self._lock:
                self._refreshing = False
            print(f"⚠️ {self.label}刷新线程启动失败: {e}")

    def _do_refresh(self):
        try:
            data = self.fetch_fn()
            if isinstance(data, dict) and data.get("partial"):
                # partial 只安排下次重试节流, 绝不回拨 timestamp —— 对外年龄如实。
                self._next_retry_at = datetime.now() + timedelta(seconds=self.retry_backoff)
                if self.cache and not self.cache.get("partial"):
                    # 保留旧完整缓存; timestamp 保持不变 → age_s() 继续如实增长,
                    # 上游长期断供时不再把陈旧数据伪装成 ttl-retry_backoff 秒新鲜。
                    print(f"⚠️ {self.label}刷新为 partial, 保留旧完整缓存, {self.retry_backoff}s 后重试")
                    return
                # 无旧完整缓存: 现取的 partial 即当前所服务数据, timestamp=now 如实(确系刚取)。
                self.cache = data
                self.timestamp = datetime.now()
                self._save(self.name, self.cache, self.timestamp)
                print(f"⚠️ {self.label}为 partial(无旧缓存), {self.retry_backoff}s 后重试")
                return
            self.cache = data
            self.timestamp = datetime.now()
            self._next_retry_at = None       # 全量成功 → 清除 partial 重试节流
            self._save(self.name, self.cache, self.timestamp)
            print(f"✅ {self.label}缓存刷新完成 {self.timestamp.strftime('%H:%M:%S')}")
        except Exception as e:
            traceback.print_exc()
            print(f"⚠️ {self.label}缓存刷新失败: {e}")
        finally:
            self._refreshing = False
