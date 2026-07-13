#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
/api/options 路由测试(PanelCache 版): 预置实例缓存, 避免依赖后台刷新线程。
partial 守卫/单飞/线程失败等行为由 tests/test_panel_cache.py 统一覆盖。
"""
from datetime import datetime

import app as appmod
from app import app

app.testing = True


def test_api_options_served_from_cache(monkeypatch):
    monkeypatch.setattr(appmod.OPTIONS_PANEL, "cache",
                        {"dvol_now": 36.1, "dvol_pct": 4.0, "put_call_oi": 0.55})
    monkeypatch.setattr(appmod.OPTIONS_PANEL, "timestamp", datetime.now())

    resp = app.test_client().get("/api/options")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["dvol_now"] == 36.1


def test_api_options_cold_start_returns_computing(monkeypatch):
    monkeypatch.setattr(appmod.OPTIONS_PANEL, "cache", None)
    monkeypatch.setattr(appmod.OPTIONS_PANEL, "timestamp", None)
    monkeypatch.setattr(appmod.OPTIONS_PANEL, "trigger_refresh", lambda: None)  # 避免真网络

    resp = app.test_client().get("/api/options")

    assert resp.status_code == 202
    assert resp.get_json()["computing"] is True
