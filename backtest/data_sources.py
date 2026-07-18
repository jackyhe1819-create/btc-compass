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

# ── 价格新鲜度守卫 (2026-07 裁决级教训) ─────────────────────────
# 缓存/上游滞后 54 天曾把 LTH占比 候选因子的 post-2024-10 IC 从真实的
# +0.069 美化成 +0.605, 差点导致错误入桶 (窗口截断系统性高估近期体制表现)。
# 方法论已入库: 因子裁决前必须先刷新价格数据。守卫三级:
#   镜像缓存 → 强刷镜像 → CM 社区 API 改道 (上游镜像 2026-07 实测停更 55 天,
#   与 eth_compass 同款事故) → 仍滞后则拒跑 (BTC_ALLOW_STALE_PRICE=1 可越过)
PRICE_MAX_LAG_DAYS = 7

_CM_API_METRICS = ("PriceUSD,CapMrktCurUSD,CapMVRVCur,HashRate,IssTotUSD,"
                   "SplyExNtv,FlowInExNtv,FlowOutExNtv")


def price_lag_days(last_date, today=None) -> int:
    """价格序列末日期距今天数 (纯函数, 供守卫与测试)。"""
    now = pd.Timestamp(today) if today is not None else pd.Timestamp.now()
    return int((now.normalize() - pd.Timestamp(last_date).normalize()).days)


def freshness_verdict(lag: int, allow_stale: bool = False) -> str:
    """守卫决策 (纯函数): 'ok' | 'refresh' (须刷新/改道) | 'fail' (拒跑)。
    allow_stale 仅在改道后仍滞后时把 fail 降级为 ok (带告警照用)。"""
    if lag <= PRICE_MAX_LAG_DAYS:
        return "ok"
    return "ok" if allow_stale else "fail"


def _fetch_cm_api_text() -> str:
    """CM 社区 API 全史 (镜像停更时的改道源), 返回 JSON 行文本。"""
    url = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
    params = {"assets": "btc", "metrics": _CM_API_METRICS, "frequency": "1d",
              "start_time": "2010-01-01", "page_size": "10000"}
    rows = []
    next_url, next_params = url, params
    for _ in range(8):
        r = requests.get(next_url, params=next_params, timeout=120, headers=_HEADERS)
        r.raise_for_status()
        j = r.json() or {}
        rows.extend(j.get("data") or [])
        nxt, token = j.get("next_page_url"), j.get("next_page_token")
        if nxt:
            next_url, next_params = nxt, None
        elif token:
            next_params = {**params, "next_page_token": token}
        else:
            break
    if len(rows) < 3000:
        raise RuntimeError(f"CM 社区 API 仅返回 {len(rows)} 行")
    return json.dumps(rows)


def _cm_api_frame() -> pd.DataFrame:
    """CM API JSON → 与镜像同构的 DataFrame。"""
    rows = json.loads(_cached_text("coinmetrics_btc_api.json",
                                   _fetch_cm_api_text, max_age_hours=24))
    recs = {}
    for row in rows:
        try:
            ts = pd.Timestamp(row["time"][:10])
        except (KeyError, TypeError, ValueError):
            continue

        def _f(key):
            v = row.get(key)
            try:
                return float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                return float("nan")

        recs[ts] = {"price": _f("PriceUSD"), "mcap": _f("CapMrktCurUSD"),
                    "mvrv": _f("CapMVRVCur"), "hashrate": _f("HashRate"),
                    "iss_usd": _f("IssTotUSD"), "sply_ex": _f("SplyExNtv"),
                    "flow_in_ex": _f("FlowInExNtv"), "flow_out_ex": _f("FlowOutExNtv")}
    out = pd.DataFrame.from_dict(recs, orient="index").sort_index()
    return out[out["price"] > 0]


def fetch_coinmetrics() -> pd.DataFrame:
    """
    返回日度 DataFrame(index=date):
      price, mcap, mvrv, hashrate, iss_usd, sply_ex(交易所BTC存量),
      flow_in_ex, flow_out_ex
    末尾经价格新鲜度守卫: 镜像滞后 >7 天时强刷→CM API 改道→仍滞后拒跑。
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

    # ── 新鲜度守卫: 三级 (强刷镜像 → CM API 改道 → 拒跑) ──
    lag = price_lag_days(out.index[-1])
    if lag > PRICE_MAX_LAG_DAYS:
        print("=" * 60)
        print(f"🚨 价格数据末日期 {out.index[-1].date()} 滞后 {lag} 天 "
              f"(>{PRICE_MAX_LAG_DAYS}) — 窗口截断会系统性美化近期体制 IC")
        print("=" * 60)
        try:
            os.remove(_cache_path("coinmetrics_btc.csv"))
        except OSError:
            pass
        try:
            r2 = requests.get(
                "https://raw.githubusercontent.com/coinmetrics/data/master/csv/btc.csv",
                headers=_HEADERS, timeout=180)
            r2.raise_for_status()
            df2 = pd.read_csv(io.StringIO(r2.text), parse_dates=["time"])
            mirror_last = df2["time"].max().tz_localize(None)
        except Exception:
            mirror_last = out.index[-1]
        if price_lag_days(mirror_last) > PRICE_MAX_LAG_DAYS:
            print(f"↪️ 镜像强刷后仍滞后 (上游停更, 末={pd.Timestamp(mirror_last).date()}) "
                  f"— 改道 CM 社区 API")
            api = _cm_api_frame()
            lag_api = price_lag_days(api.index[-1])
            if freshness_verdict(lag_api,
                                 os.environ.get("BTC_ALLOW_STALE_PRICE") == "1") == "fail":
                raise RuntimeError(
                    f"价格数据经三级刷新仍滞后 {lag_api} 天 (末 {api.index[-1].date()}) "
                    f"— 拒跑以防近期 IC 被美化; 确要带旧数据跑请设 BTC_ALLOW_STALE_PRICE=1 "
                    f"且在报告声明数据截止日")
            print(f"✅ 改道成功: 数据截止 {api.index[-1].date()} (滞后 {lag_api} 天)")
            return api
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
