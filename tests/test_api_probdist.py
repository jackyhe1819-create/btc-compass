#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""/api/probdist 路由测试(PanelCache 版), 同 /api/options 范式。"""
import datetime

import app as appmod
from app import app

app.testing = True


def test_probdist_served_from_cache(monkeypatch):
    monkeypatch.setattr(appmod.PROBDIST_PANEL, "cache",
                        {"median": 64000, "p_up": 53.0, "pdf": [[60000, 1.0]]})
    monkeypatch.setattr(appmod.PROBDIST_PANEL, "timestamp", datetime.datetime.now())
    r = app.test_client().get("/api/probdist")
    assert r.status_code == 200 and r.get_json()["median"] == 64000


def test_probdist_cold_start(monkeypatch):
    monkeypatch.setattr(appmod.PROBDIST_PANEL, "cache", None)
    monkeypatch.setattr(appmod.PROBDIST_PANEL, "timestamp", None)
    monkeypatch.setattr(appmod.PROBDIST_PANEL, "trigger_refresh", lambda: None)
    r = app.test_client().get("/api/probdist")
    assert r.status_code == 202 and r.get_json().get("computing") is True
