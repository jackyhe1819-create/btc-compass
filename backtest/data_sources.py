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
# Binance 资金费率 (BTCUSDT 永续 8h, 2019-09-10 起)
# fapi 对部分网络 451 地理封锁 → 走官方数据镜像 data.binance.vision
# (S3 月度 CSV 包, 无地理限制; 与 bnb_compass/backtest 同方案, 2026-07 移植)
# ------------------------------------------------------------

# 镜像实测不托管 2019-09..2019-12 月包 (fapi 时代覆盖 2019-09-10 起, 镜像只从
# 2020-01 起), 起点按镜像实际覆盖设定, 避免每轮回测对 4 个死 URL 重复请求
_FUNDING_START = pd.Timestamp("2020-01-01")


def _fetch_funding_month(ym: str):
    """下载并解析单月 fundingRate CSV 包。返回 [(ms, rate)], 404/失败返回 None。"""
    import io as _io
    import zipfile
    url = ("https://data.binance.vision/data/futures/um/monthly/fundingRate/"
           f"BTCUSDT/BTCUSDT-fundingRate-{ym}.zip")
    r = requests.get(url, headers=_HEADERS, timeout=60)
    if r.status_code != 200:
        return None
    with zipfile.ZipFile(_io.BytesIO(r.content)) as zf:
        text = zf.read(zf.namelist()[0]).decode("utf-8")
    rows = []
    for line in text.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 3:
            continue
        try:
            rows.append((int(parts[0]), float(parts[2])))
        except ValueError:
            continue
    return rows


def fetch_funding() -> pd.Series:
    """返回 8 小时粒度资金费率序列 (index=结算时间, 值=费率小数)。逐月落盘缓存。"""
    month_dir = os.path.join(CACHE_DIR, "binance_funding_btc")
    os.makedirs(month_dir, exist_ok=True)

    # 当前月的月包要到月末后才发布 (永远 404), 只遍历到上个月;
    # 序列尾部因此最多缺 ~31 天, 该段 资金费率(7d) 为 NaN 由重归一剔除 (报告口径已声明)
    months = pd.period_range(_FUNDING_START, pd.Timestamp.now(), freq="M")[:-1]
    all_rows = []
    for p in months:
        ym = str(p)  # YYYY-MM
        path = os.path.join(month_dir, f"{ym}.json")
        rows = None
        if os.path.exists(path):
            with open(path, "r") as f:
                rows = json.load(f)
        else:
            try:
                rows = _fetch_funding_month(ym)
            except Exception as e:
                print(f"⚠️ 资金费率 {ym} 下载失败: {e}")
                rows = None
            if rows:
                tmp = path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(rows, f)
                os.replace(tmp, path)
            time.sleep(0.15)
        if rows:
            all_rows.extend(rows)

    if not all_rows:
        print("⚠️ Binance 资金费率镜像不可用 (因子将全程缺失)")
        # 必须带 DatetimeIndex: 裸空 Series 的 RangeIndex 在 factors 的
        # index.normalize() 处会抛异常, 降级路径反而崩掉整个回测
        return pd.Series(dtype=float, index=pd.DatetimeIndex([]))
    data = {pd.Timestamp(ms, unit="ms"): rate for ms, rate in all_rows}
    s = pd.Series(data).sort_index()
    print(f"   资金费率序列截止 {s.index[-1].date()} (镜像月包滞后, 尾部缺当月)")
    return s[~s.index.duplicated(keep="last")]


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
