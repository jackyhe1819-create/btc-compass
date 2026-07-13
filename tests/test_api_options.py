#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
/api/options 路由测试：照 /api/derivatives 的 SWR 缓存范式。
预置模块全局缓存，避免依赖后台刷新线程造成的不稳定。
"""
from datetime import datetime

import app as appmod
from app import app

app.testing = True


def test_api_options_served_from_cache(monkeypatch):
    fixture = {"dvol_now": 36.1, "dvol_pct": 4.0, "put_call_oi": 0.55}
    monkeypatch.setattr(appmod, "_options_cache", fixture)
    monkeypatch.setattr(appmod, "_options_cache_timestamp", datetime.now())

    client = app.test_client()
    resp = client.get("/api/options")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["dvol_now"] == 36.1


def test_api_options_cold_start_returns_computing(monkeypatch):
    monkeypatch.setattr(appmod, "_options_cache", None)
    monkeypatch.setattr(appmod, "_options_cache_timestamp", None)
    monkeypatch.setattr(appmod, "trigger_options_refresh", lambda: None)   # 避免真网络后台线程

    client = app.test_client()
    resp = client.get("/api/options")

    assert resp.status_code == 202
    body = resp.get_json()
    assert body["computing"] is True


def test_do_refresh_options_keeps_good_cache_on_partial(monkeypatch):
    # partial 空壳不得覆盖既有完整缓存(复审唯一 high): 保旧数据 + 回拨时间戳 2 分钟重试 + 不落盘
    good = {"dvol_now": 36.1, "partial": False}
    monkeypatch.setattr(appmod, "_options_cache", good)
    monkeypatch.setattr(appmod, "_options_cache_timestamp", datetime.now())
    monkeypatch.setattr(appmod, "fetch_options_panel",
                        lambda: {"dvol_now": None, "partial": True})
    saved = []
    monkeypatch.setattr(appmod, "_save_cache_to_disk", lambda *a: saved.append(a))

    appmod._do_refresh_options()

    assert appmod._options_cache is good        # 旧完整缓存未被覆盖
    assert saved == []                          # partial 未落盘
    age = (datetime.now() - appmod._options_cache_timestamp).total_seconds()
    assert age >= appmod._OPTIONS_TTL - 130     # 时间戳已回拨 → ~2 分钟后重试


def test_do_refresh_options_caches_partial_when_no_old(monkeypatch):
    # 无旧数据时 partial 可以入缓存(有总比没有强), 但时间戳回拨、约 2 分钟后自动重试
    monkeypatch.setattr(appmod, "_options_cache", None)
    monkeypatch.setattr(appmod, "_options_cache_timestamp", None)
    part = {"dvol_now": None, "partial": True}
    monkeypatch.setattr(appmod, "fetch_options_panel", lambda: part)
    saved = []
    monkeypatch.setattr(appmod, "_save_cache_to_disk", lambda *a: saved.append(a))

    appmod._do_refresh_options()

    assert appmod._options_cache is part
    assert len(saved) == 1
    age = (datetime.now() - appmod._options_cache_timestamp).total_seconds()
    assert age >= appmod._OPTIONS_TTL - 130
