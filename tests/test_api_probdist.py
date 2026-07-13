#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
/api/probdist 路由测试：照 /api/options 的 SWR 缓存范式。
预置模块全局缓存，避免依赖后台刷新线程造成的不稳定；
冷启动用例显式 mock trigger_probdist_refresh，避免真实网络后台线程。
"""
import datetime

import app as appmod
from app import app


def test_probdist_served_from_cache(monkeypatch):
    app.testing = True
    monkeypatch.setattr(appmod, "_probdist_cache", {"median": 64000, "p_up": 53.0, "pdf": [[60000, 1.0]]})
    monkeypatch.setattr(appmod, "_probdist_cache_timestamp", datetime.datetime.now())
    r = app.test_client().get("/api/probdist")
    assert r.status_code == 200 and r.get_json()["median"] == 64000


def test_probdist_cold_start(monkeypatch):
    app.testing = True
    monkeypatch.setattr(appmod, "_probdist_cache", None)
    monkeypatch.setattr(appmod, "_probdist_cache_timestamp", None)
    monkeypatch.setattr(appmod, "trigger_probdist_refresh", lambda: None)   # 避免真网络后台线程
    r = app.test_client().get("/api/probdist")
    assert r.status_code == 202 and r.get_json().get("computing") is True
