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

    client = app.test_client()
    resp = client.get("/api/options")

    assert resp.status_code == 202
    body = resp.get_json()
    assert body["computing"] is True
