#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.data_sources
=====================
回测数据层：拉取并落盘缓存各因子的最长免费历史序列。

数据源与覆盖范围（2026-06 实测）:
- CoinMetrics 社区 CSV: 价格/市值/MVRV/算力/发行USD/交易所存量, 2010-07 起
- bitcoin-data.com:     SOPR / STH已实现价格, 仅最近 1460 天 (匿名 10次/小时)
- alternative.me:       恐惧贪婪指数, 2018-02-01 起
- Binance fapi:         BTCUSDT 永续资金费率(8h), 2019-09-10 起
- DefiLlama:            稳定币总市值, 2017-11-29 起
- SoSoValue:            美国现货 BTC ETF 日度净流入, 2024-01-11 起
"""

import io
import json
import os
import time
from datetime import datetime, timezone

import pandas as pd
import requests

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "Accept": "application/json"}


def _cache_path(name: str) -> str:
    return os.path.join(CACHE_DIR, name)


def _cached_text(name: str, fetch_fn, max_age_hours: float = 24.0) -> str:
    """文本缓存: 在有效期内直接读盘, 否则调 fetch_fn 并写盘。失败时回退旧缓存。"""
    path = _cache_path(name)
    if os.path.exists(path):
        age_h = (time.time() - os.path.getmtime(path)) / 3600
        if age_h < max_age_hours:
            with open(path, "r") as f:
                return f.read()
    try:
        text = fetch_fn()
        if text:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.write(text)
            os.replace(tmp, path)
            return text
    except Exception as e:
        print(f"⚠️ [{name}] 拉取失败: {e}")
    if os.path.exists(path):
        print(f"↩️ [{name}] 回退旧缓存")
        with open(path, "r") as f:
            return f.read()
    raise RuntimeError(f"{name}: 无可用数据 (拉取失败且无缓存)")


# ------------------------------------------------------------
# CoinMetrics 社区 CSV
# ------------------------------------------------------------

def fetch_coinmetrics() -> pd.DataFrame:
    """
    返回日度 DataFrame(index=date):
      price, mcap, mvrv, hashrate, iss_usd, sply_ex(交易所BTC存量),
      flow_in_ex, flow_out_ex
    """
    def _fetch():
        r = requests.get(
            "https://raw.githubusercontent.com/coinmetrics/data/master/csv/btc.csv",
            headers=_HEADERS, timeout=180)
        r.raise_for_status()
        return r.text

    text = _cached_text("coinmetrics_btc.csv", _fetch, max_age_hours=24)
    df = pd.read_csv(io.StringIO(text), parse_dates=["time"])
    df = df.set_index("time").sort_index()
    df.index = df.index.tz_localize(None)

    out = pd.DataFrame(index=df.index)
    out["price"] = pd.to_numeric(df["PriceUSD"], errors="coerce")
    out["mcap"] = pd.to_numeric(df["CapMrktCurUSD"], errors="coerce")
    out["mvrv"] = pd.to_numeric(df["CapMVRVCur"], errors="coerce")
    out["hashrate"] = pd.to_numeric(df["HashRate"], errors="coerce")
    out["iss_usd"] = pd.to_numeric(df["IssTotUSD"], errors="coerce")
    out["sply_ex"] = pd.to_numeric(df["SplyExNtv"], errors="coerce")
    out["flow_in_ex"] = pd.to_numeric(df["FlowInExNtv"], errors="coerce")
    out["flow_out_ex"] = pd.to_numeric(df["FlowOutExNtv"], errors="coerce")
    out = out[out["price"] > 0]
    return out


# ------------------------------------------------------------
# bitcoin-data.com (近 1460 天)
# ------------------------------------------------------------

def fetch_bd_series(endpoint: str, field: str) -> pd.Series:
    """bitcoin-data.com /v1/<endpoint> 全量(1460天)序列。缓存 7 天减少限流。"""
    def _fetch():
        r = requests.get(f"https://bitcoin-data.com/v1/{endpoint}",
                         headers=_HEADERS, timeout=60)
        r.raise_for_status()
        return r.text

    text = _cached_text(f"bd_{endpoint}.json", _fetch, max_age_hours=24 * 7)
    rows = json.loads(text)
    data = {}
    for it in rows:
        d = it.get("d")
        v = it.get(field)
        if d is None or v is None:
            continue
        try:
            data[pd.Timestamp(d)] = float(v)
        except (ValueError, TypeError):
            continue
    s = pd.Series(data).sort_index()
    s.name = endpoint
    return s


# ------------------------------------------------------------
# 恐惧贪婪指数 (alternative.me)
# ------------------------------------------------------------

def fetch_fng() -> pd.Series:
    def _fetch():
        r = requests.get("https://api.alternative.me/fng/?limit=0&format=json",
                         headers=_HEADERS, timeout=60)
        r.raise_for_status()
        return r.text

    text = _cached_text("fng.json", _fetch, max_age_hours=24)
    rows = json.loads(text)["data"]
    data = {}
    for it in rows:
        ts = pd.Timestamp(datetime.fromtimestamp(int(it["timestamp"]), tz=timezone.utc).date())
        data[ts] = float(it["value"])
    return pd.Series(data).sort_index()


# ------------------------------------------------------------
# Binance 资金费率 (8h, 2019-09-10 起)
# ------------------------------------------------------------

def fetch_funding() -> pd.Series:
    """返回 8 小时粒度资金费率序列 (index=结算时间, 值=费率小数)。增量缓存。"""
    path = _cache_path("binance_funding.json")
    rows = []
    if os.path.exists(path):
        with open(path, "r") as f:
            rows = json.load(f)

    start = int(rows[-1]["fundingTime"]) + 1 if rows else 1567296000000  # 2019-09-01
    fetched = 0
    while True:
        try:
            r = requests.get("https://fapi.binance.com/fapi/v1/fundingRate",
                             params={"symbol": "BTCUSDT", "startTime": start, "limit": 1000},
                             headers=_HEADERS, timeout=30)
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            print(f"⚠️ Binance 资金费率分页失败 (已有 {len(rows)} 条): {e}")
            break
        if not batch:
            break
        rows.extend(batch)
        fetched += len(batch)
        start = int(batch[-1]["fundingTime"]) + 1
        if len(batch) < 1000:
            break
        time.sleep(0.25)

    if fetched:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rows, f)
        os.replace(tmp, path)

    data = {pd.Timestamp(int(it["fundingTime"]), unit="ms"): float(it["fundingRate"])
            for it in rows}
    return pd.Series(data).sort_index()


# ------------------------------------------------------------
# DefiLlama 稳定币总市值 (2017-11 起)
# ------------------------------------------------------------

def fetch_stablecoins() -> pd.Series:
    def _fetch():
        r = requests.get("https://stablecoins.llama.fi/stablecoincharts/all",
                         headers=_HEADERS, timeout=60)
        r.raise_for_status()
        return r.text

    text = _cached_text("stablecoins.json", _fetch, max_age_hours=24)
    rows = json.loads(text)
    data = {}
    for p in rows:
        tc = p.get("totalCirculating") or {}
        v = tc.get("peggedUSD")
        if v is None:
            continue
        ts = pd.Timestamp(datetime.fromtimestamp(int(p["date"]), tz=timezone.utc).date())
        data[ts] = float(v)
    return pd.Series(data).sort_index()


# ------------------------------------------------------------
# SoSoValue ETF 日度净流入 (2024-01-11 起)
# ------------------------------------------------------------

def fetch_etf() -> pd.Series:
    """美元计净流入 (USD), index=交易日。"""
    def _fetch():
        r = requests.post(
            "https://api.sosovalue.xyz/openapi/v2/etf/historicalInflowChart",
            json={"type": "us-btc-spot"},
            headers={"Content-Type": "application/json",
                     "x-soso-api-key": os.environ.get("SOSOVALUE_API_KEY", "public"),
                     "User-Agent": _HEADERS["User-Agent"]},
            timeout=30)
        r.raise_for_status()
        payload = r.json()
        if payload.get("code") != 0 or not payload.get("data"):
            raise RuntimeError(f"SoSoValue 返回异常: {str(payload)[:100]}")
        return r.text

    text = _cached_text("sosovalue_etf.json", _fetch, max_age_hours=24)
    payload = json.loads(text)
    data = {}
    for it in payload["data"]:
        d = it.get("date")
        v = it.get("totalNetInflow")
        if d is None or v is None:
            continue
        data[pd.Timestamp(d)] = float(v)
    return pd.Series(data).sort_index()


if __name__ == "__main__":
    cm = fetch_coinmetrics()
    print(f"CoinMetrics: {len(cm)} 天, {cm.index[0].date()} → {cm.index[-1].date()}")
    print(cm.tail(2))
