#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生产价格新鲜度守卫测试 (p0-2-price-freshness)。

覆盖 core.fetch_btc_data / runner._apply_realtime_price 的三个新行为:
  1) 跳源     — 某源滞后 >7 天但下一源新鲜时, 采用新鲜源而非首个非空源
  2) 标记     — 全部源均滞后时选滞后最小的一份, attrs 标 stale=True + lag_days
  3) 追加行   — 实时价并入历史时: 末行是旧日期则追加今日新行, 不覆盖旧收盘

不触网: monkeypatch yfinance / requests 数据源。守卫阈值与 backtest 同源,
故附一条 parity 测试锁死 PRICE_MAX_LAG_DAYS 与 lag 计算与回测一致。
"""
import numpy as np
import pandas as pd

from btc_dashboard import core, runner


# ── 数据源假体 ─────────────────────────────────────────────

def _yahoo_frame(last_date, n=40):
    """伪造 yfinance.download 返回值: 带 Close 列、以 last_date 收尾的日频 df。"""
    idx = pd.date_range(end=pd.Timestamp(last_date).normalize(), periods=n, freq="D")
    return pd.DataFrame({"Close": np.linspace(90000.0, 100000.0, n)}, index=idx)


def _unix(t):
    """把日期转 unix 秒 (按 UTC 口径, 与 pd.to_datetime(unit='s') 精确往返)。"""
    return int((pd.Timestamp(t).normalize() - pd.Timestamp("1970-01-01")).total_seconds())


def _cc_payload(last_date, n=40):
    """伪造 CryptoCompare histoday 响应体, 以 last_date 收尾。"""
    ts = pd.date_range(end=pd.Timestamp(last_date).normalize(), periods=n, freq="D")
    data = [{"time": _unix(t), "close": 90000.0 + i * 100} for i, t in enumerate(ts)]
    return {"Data": {"Data": data}}


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeRequests:
    """替换 core.requests: 只暴露 .get, 按 URL 路由。cryptocompare 之外一律 500 跳过。"""
    def __init__(self, cc_last=None):
        self._cc_last = cc_last

    def get(self, url, *args, **kwargs):
        if "cryptocompare" in url:
            if self._cc_last is None:
                return _Resp(500, {})
            return _Resp(200, _cc_payload(self._cc_last))
        # coingecko / kraken / binance: 让它们失败, 逼守卫落到 stale 候选选择
        return _Resp(500, {})


def _patch_sources(monkeypatch, yahoo_last, cc_last):
    """Yahoo 返回 yahoo_last 收尾的 df; CryptoCompare 返回 cc_last (None=失败)。"""
    monkeypatch.setattr(core.yf, "download",
                        lambda *a, **k: _yahoo_frame(yahoo_last))
    monkeypatch.setattr(core, "requests", _FakeRequests(cc_last=cc_last))


# ── 行为 1: 跳源 ───────────────────────────────────────────

def test_fetch_skips_stale_source_for_fresh_one(monkeypatch):
    """Yahoo 滞后 30 天但 CryptoCompare 新鲜 → 采用 CryptoCompare, 不用陈旧的首个非空源。"""
    today = pd.Timestamp.now().normalize()
    _patch_sources(monkeypatch, yahoo_last=today - pd.Timedelta(days=30), cc_last=today)

    df = core.fetch_btc_data()

    assert df.attrs.get("source") == "CryptoCompare"
    assert not df.attrs.get("stale", False)      # 新鲜源不标 stale
    assert core._price_lag_days(df.index[-1]) <= core.PRICE_MAX_LAG_DAYS


# ── 行为 2: 标记 ───────────────────────────────────────────

def test_fetch_marks_stale_and_picks_min_lag(monkeypatch):
    """全部源均滞后 → 选滞后最小的一份 (CryptoCompare 12 天 < Yahoo 30 天), 标 stale + lag_days。"""
    today = pd.Timestamp.now().normalize()
    _patch_sources(monkeypatch,
                   yahoo_last=today - pd.Timedelta(days=30),
                   cc_last=today - pd.Timedelta(days=12))

    df = core.fetch_btc_data()

    assert df.attrs.get("stale") is True
    assert df.attrs.get("lag_days") == 12          # 滞后最小者
    assert df.attrs.get("source") == "CryptoCompare"
    assert not df.attrs.get("synthetic", False)    # 非空数据, 不应退化到合成


def test_fetch_falls_back_to_synthetic_when_all_empty(monkeypatch):
    """全部源都取不到数据 (空/失败) 才退化到合成示例, 不与 stale 分支混淆。"""
    today = pd.Timestamp.now().normalize()

    def _empty_yahoo(*a, **k):
        return pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([]))

    monkeypatch.setattr(core.yf, "download", _empty_yahoo)
    monkeypatch.setattr(core, "requests", _FakeRequests(cc_last=None))

    df = core.fetch_btc_data()

    assert df.attrs.get("synthetic") is True
    assert not df.attrs.get("stale", False)         # 合成路径不误标 stale


# ── 行为 3: 追加行 ─────────────────────────────────────────

def test_apply_realtime_price_updates_today_row():
    """末行日期==今天 → 原地更新该行收盘, 不新增行。"""
    today = pd.Timestamp.now().normalize()
    idx = pd.date_range(end=today, periods=5, freq="D")
    df = pd.DataFrame({"price": [1.0, 2.0, 3.0, 4.0, 5.0]}, index=idx)
    n0 = len(df)

    runner._apply_realtime_price(df, 118000.0)

    assert len(df) == n0                              # 无新增
    assert df["price"].iloc[-1] == 118000.0
    assert pd.Timestamp(df.index[-1]).normalize() == today


def test_apply_realtime_price_appends_row_when_last_is_old():
    """末行是旧日期 → 追加今日新行, 旧日期收盘不被覆盖 (核心防错位)。"""
    today = pd.Timestamp.now().normalize()
    last = today - pd.Timedelta(days=5)
    idx = pd.date_range(end=last, periods=5, freq="D")
    df = pd.DataFrame({"price": [1.0, 2.0, 3.0, 4.0, 5.0]}, index=idx)
    n0 = len(df)
    old_last_price = df["price"].iloc[-1]

    runner._apply_realtime_price(df, 118000.0)

    assert len(df) == n0 + 1                          # 追加了今日行
    assert pd.Timestamp(df.index[-1]).normalize() == today
    assert df["price"].iloc[-1] == 118000.0
    # 旧末行 (5 天前) 收盘保持原值, 未被今日价覆盖
    assert df["price"].iloc[-2] == old_last_price
    assert pd.Timestamp(df.index[-2]).normalize() == last


# ── parity: 守卫与 backtest 同源同阈值 ─────────────────────

def test_freshness_guard_matches_backtest():
    """core 复刻的守卫必须与 backtest/data_sources.py 同阈值同实现 (改一处须同步另一处)。"""
    from backtest.data_sources import (
        PRICE_MAX_LAG_DAYS as BT_MAX, price_lag_days as bt_lag,
    )
    assert core.PRICE_MAX_LAG_DAYS == BT_MAX == 7
    for last, today in [("2026-07-10", "2026-07-17"),
                        ("2026-07-17", "2026-07-17"),
                        ("2026-05-24", "2026-07-18")]:
        assert core._price_lag_days(last, today=today) == bt_lag(last, today=today)
