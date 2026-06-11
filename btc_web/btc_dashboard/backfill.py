#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.backfill
======================
评分历史回填：用免费数据源的历史序列，按 Compass 当前评分公式重建
过去 N 天的「周期分 + 战术分」，写入 score_history.json。

设计要点:
- 幂等: 已回填(marker 文件存在)则直接跳过; 真实快照(非 backfilled)永远优先
- 数据源磁盘缓存 7 天: 重跑/重启不重复打 API (bitcoin-data.com 匿名限 10 req/h)
- 部分数据源失败 → 当天该指标缺席, 桶内权重自动归一 (与实时评分同一套降级逻辑)
- 近似声明: MACD/RSI/布林带用日线单周期重建(实时版为多周期聚合);
  期货基差/多空比/交易所余额无历史数据, 回填期间缺席
- 各指标分档阈值与 indicators_v2.py / indicators_short.py 保持一致
"""

import os
import json
import time
import tempfile
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

from .core import IndicatorResult, GENESIS_DATE, HALVING_DATES, AHR999_A, AHR999_B, fetch_btc_data
from .scoring import (
    CYCLE_BUCKETS, TACTICAL_BUCKETS, _compute_bucket_scores,
    cycle_recommendation, PERCENTILE_WINDOW,
)
from .score_history import _load_history, _save_history

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "Accept": "application/json"}
_SRC_TTL = 7 * 24 * 3600   # 数据源磁盘缓存 7 天
_MARKER = "score_history_backfilled.marker"


# ============================================================
# 数据源抓取（带磁盘缓存）
# ============================================================

def _src_cache_dir(cache_dir: str) -> str:
    p = os.path.join(cache_dir, "backfill_src")
    os.makedirs(p, exist_ok=True)
    return p


def _cached_fetch(cache_dir: str, name: str, fetch_fn):
    """磁盘缓存的抓取: 7 天内直接读缓存, 失败返回 None 且不写缓存"""
    path = os.path.join(_src_cache_dir(cache_dir), f"{name}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            if time.time() - obj["ts"] < _SRC_TTL:
                return obj["data"]
        except Exception:
            pass
    try:
        data = fetch_fn()
    except Exception as e:
        print(f"⚠️ backfill 源 [{name}] 失败: {e}")
        data = None
    if data:
        try:
            fd, tmp = tempfile.mkstemp(dir=_src_cache_dir(cache_dir), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "data": data}, f)
            os.replace(tmp, path)
        except Exception:
            pass
        return data
    return None


def _fetch_bd_series(metric: str, field: str, days: int):
    """bitcoin-data.com 范围端点 → {date: value}"""
    start = (datetime.now() - timedelta(days=days + 10)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    r = requests.get(f"https://bitcoin-data.com/v1/{metric}",
                     params={"startday": start, "endday": end},
                     timeout=20, headers=_HEADERS)
    if r.status_code != 200:
        print(f"⚠️ bitcoin-data {metric} HTTP {r.status_code}")
        return None
    out = {}
    for row in r.json():
        try:
            out[row["d"]] = float(row[field])
        except (KeyError, TypeError, ValueError):
            continue
    return out or None


def _fetch_fng():
    """alternative.me 恐惧贪婪全量历史 → {date: int}"""
    r = requests.get("https://api.alternative.me/fng/?limit=0", timeout=20, headers=_HEADERS)
    if r.status_code != 200:
        return None
    out = {}
    for row in (r.json().get("data") or []):
        d = datetime.fromtimestamp(int(row["timestamp"])).strftime("%Y-%m-%d")
        out[d] = int(row["value"])
    return out or None


def _fetch_stablecoins():
    """DefiLlama 稳定币总市值日度 → {date: usd}"""
    r = requests.get("https://stablecoins.llama.fi/stablecoincharts/all",
                     timeout=25, headers=_HEADERS)
    if r.status_code != 200:
        return None
    out = {}
    for p in r.json():
        if not p.get("totalCirculating"):
            continue
        d = datetime.fromtimestamp(int(p["date"])).strftime("%Y-%m-%d")
        out[d] = float(p["totalCirculating"].get("peggedUSD", 0))
    return out or None


def _fetch_etf_daily():
    """SoSoValue ETF 日度净流入 → {date: 百万美元}"""
    from .etf_flow import fetch_etf_flow_history
    data = fetch_etf_flow_history(limit=400)
    if not data or not data.get("series"):
        return None
    return {row["date"]: float(row["total"]) for row in data["series"]}


def _fetch_hashrate_1y():
    """mempool.space 一年日度算力 → [(date, hashrate)]"""
    r = requests.get("https://mempool.space/api/v1/mining/hashrate/1y",
                     timeout=20, headers=_HEADERS)
    if r.status_code != 200:
        return None
    out = []
    for x in (r.json() or {}).get("hashrates", []):
        d = datetime.fromtimestamp(int(x["timestamp"])).strftime("%Y-%m-%d")
        out.append([d, float(x["avgHashrate"])])
    return out or None


def _fetch_funding_history():
    """OKX 资金费率历史 (3 页 ≈ 100 天) → {date: [rates]}"""
    out = {}
    after = None
    for _ in range(3):
        params = {"instId": "BTC-USDT-SWAP", "limit": "100"}
        if after:
            params["after"] = after
        r = requests.get("https://www.okx.com/api/v5/public/funding-rate-history",
                         params=params, timeout=15, headers=_HEADERS)
        if r.status_code != 200:
            break
        rows = (r.json() or {}).get("data", [])
        if not rows:
            break
        for row in rows:
            ts = int(row["fundingTime"])
            d = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
            out.setdefault(d, []).append(float(row["fundingRate"]))
        after = rows[-1]["fundingTime"]
        time.sleep(0.3)
    return out or None


# ============================================================
# 分档函数（阈值与 indicators_v2.py / indicators_short.py 一致）
# ============================================================

def _band_mvrv_z(z):
    return 1 if z < 0 else 0.5 if z < 1 else 0 if z < 3 else -0.5 if z < 5 else -1

def _band_sth_ratio(r):
    return 1 if r < 0.80 else 0.5 if r < 0.95 else 0 if r < 1.15 else -0.5 if r < 1.35 else -1

def _band_nupl(v):
    return 1 if v < 0 else 0.5 if v < 0.25 else 0 if v < 0.5 else -0.5 if v < 0.75 else -1

def _band_sopr(v):
    return 1 if v < 0.97 else 0.5 if v < 0.995 else 0 if v < 1.02 else -0.5 if v < 1.05 else -1

def _band_puell(v):
    return 1 if v < 0.5 else 0.5 if v < 0.8 else 0 if v < 2.0 else -0.5 if v < 4.0 else -1

def _band_fng(v):
    return 1 if v <= 20 else 0.5 if v <= 30 else 0 if v < 70 else -0.5 if v < 80 else -1

def _band_funding7d(pct):
    return -1 if pct > 0.05 else -0.5 if pct > 0.02 else 0 if pct > -0.02 else 0.5 if pct > -0.05 else 1

def _band_etf_5d(m):
    return 1 if m > 1000 else 0.5 if m > 200 else 0 if m > -200 else -0.5 if m > -1000 else -1

def _band_stable_growth(pct):
    return 1 if pct > 2.5 else 0.5 if pct > 1.0 else 0 if pct > -1.0 else -0.5 if pct > -2.5 else -1

def _band_halving(months):
    return 1 if months <= 12 else 0 if months <= 24 else -1

def _band_rsi(v):
    return -1 if v >= 80 else -0.5 if v >= 70 else 1 if v <= 20 else 0.5 if v <= 30 else 0

def _band_bb(pct_b):
    if pct_b >= 1: return -0.5
    if pct_b <= 0: return 0.5
    if pct_b > 0.8: return -0.3
    if pct_b < 0.2: return 0.3
    return 0


def _stub(name: str, score) -> IndicatorResult:
    """构造参与桶计算的指标桩 (value=1.0 表示有效)"""
    return IndicatorResult(name=name, value=1.0, score=float(score),
                           color="", status="", priority="P0")


# ============================================================
# 历史重建
# ============================================================

def reconstruct(days: int = 90, cache_dir: str = None) -> list:
    """重建过去 days 天 (不含今天) 的双评分序列, 返回 entry 列表"""
    df = fetch_btc_data()
    price = df['price']

    # ── 拉取所有历史序列（带磁盘缓存）──
    src = {
        "mvrv":   _cached_fetch(cache_dir, "mvrv-zscore", lambda: _fetch_bd_series("mvrv-zscore", "mvrvZscore", days)),
        "sth":    _cached_fetch(cache_dir, "sth-realized-price", lambda: _fetch_bd_series("sth-realized-price", "sthRealizedPrice", days)),
        "nupl":   _cached_fetch(cache_dir, "nupl", lambda: _fetch_bd_series("nupl", "nupl", days)),
        "sopr":   _cached_fetch(cache_dir, "sopr", lambda: _fetch_bd_series("sopr", "sopr", days)),
        "puell":  _cached_fetch(cache_dir, "puell-multiple", lambda: _fetch_bd_series("puell-multiple", "puellMultiple", days)),
        "fng":    _cached_fetch(cache_dir, "fng", _fetch_fng),
        "stable": _cached_fetch(cache_dir, "stablecoins", _fetch_stablecoins),
        "etf":    _cached_fetch(cache_dir, "etf-daily", _fetch_etf_daily),
        "hash":   _cached_fetch(cache_dir, "hashrate-1y", _fetch_hashrate_1y),
        "fund":   _cached_fetch(cache_dir, "funding-history", _fetch_funding_history),
    }
    ok = [k for k, v in src.items() if v]
    print(f"📦 backfill 数据源: {len(ok)}/10 可用 ({', '.join(ok)})")
    # 链上核心序列至少要有 3 个, 否则视为本轮失败 (调用方稍后重试)
    core_ok = sum(1 for k in ("mvrv", "sth", "nupl", "sopr", "puell") if src[k])
    if core_ok < 3:
        raise RuntimeError(f"链上核心序列仅 {core_ok}/5 可用, 放弃本轮回填")

    # ── 预计算 df 派生序列 ──
    ma111 = price.rolling(111).mean()
    ma200 = price.rolling(200).mean()
    ma350 = price.rolling(350).mean()
    ma1400 = price.rolling(1400).mean()
    ema140 = price.ewm(span=140, adjust=False).mean()
    mayer_s = price / ma200
    w200_s = (price - ma1400) / ma1400
    days_geo = (df.index - pd.Timestamp(GENESIS_DATE)).days.values.astype(float)
    with np.errstate(invalid='ignore', divide='ignore'):
        fair = 10 ** (AHR999_B * np.log10(np.where(days_geo > 0, days_geo, np.nan)) + AHR999_A)
    plaw_s = pd.Series(price.values / fair, index=df.index)

    ema12 = price.ewm(span=12, adjust=False).mean()
    ema26 = price.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    macd_sig = macd_line.ewm(span=9, adjust=False).mean()
    delta = price.diff()
    rsi_s = 100 - 100 / (1 + delta.clip(lower=0).rolling(14).mean() /
                         (-delta.clip(upper=0)).rolling(14).mean())
    bb_mid = price.rolling(20).mean()
    bb_std = price.rolling(20).std()
    pct_b = (price - (bb_mid - 2 * bb_std)) / (4 * bb_std)

    # hash ribbons 序列
    hash_sma30 = hash_sma60 = None
    if src["hash"]:
        hs = pd.Series({pd.Timestamp(d): v for d, v in src["hash"]}).sort_index()
        hash_sma30, hash_sma60 = hs.rolling(30).mean(), hs.rolling(60).mean()

    stable_s = pd.Series({pd.Timestamp(d): v for d, v in (src["stable"] or {}).items()}).sort_index()
    etf_s = pd.Series({pd.Timestamp(d): v for d, v in (src["etf"] or {}).items()}).sort_index()
    etf_5d = etf_s.rolling(5).sum() if len(etf_s) else None

    def _at(series_dict, d):
        return series_dict.get(d) if series_dict else None

    def _percentile_at(s: pd.Series, ts) -> float:
        sub = s.loc[:ts].dropna()
        if len(sub) < PERCENTILE_WINDOW // 4:
            return float('nan')
        tail = sub.tail(PERCENTILE_WINDOW)
        return float((0.5 - (tail < tail.iloc[-1]).mean()) * 2)

    entries = []
    today = datetime.now().strftime("%Y-%m-%d")
    for ts in df.index[-(days + 1):]:
        d = ts.strftime("%Y-%m-%d")
        if d >= today:
            continue  # 今天交给实时快照
        inds = {}

        # 趋势伸展 (分位数) + Pi Cycle
        for name, s in (("Mayer Multiple", mayer_s), ("200-Week Heatmap", w200_s), ("幂律走廊", plaw_s)):
            sc = _percentile_at(s, ts)
            if not np.isnan(sc):
                inds[name] = _stub(name, sc)
        if not np.isnan(ma111.loc[ts]) and not np.isnan(ma350.loc[ts]):
            ma350x2 = ma350.loc[ts] * 2
            gap = (ma350x2 - ma111.loc[ts]) / ma350x2 * 100
            inds["Pi Cycle Top"] = _stub("Pi Cycle Top", -1 if gap <= 0 else 0 if gap <= 20 else 1)

        # 链上筹码
        z = _at(src["mvrv"], d)
        if z is not None:
            inds["MVRV-Z"] = _stub("MVRV-Z", _band_mvrv_z(z))
        sth = _at(src["sth"], d)
        if sth and sth > 0:
            inds["STH成本线"] = _stub("STH成本线", _band_sth_ratio(price.loc[ts] / sth))
        nu = _at(src["nupl"], d)
        if nu is not None:
            inds["NUPL"] = _stub("NUPL", _band_nupl(nu))

        # 资金流
        if etf_5d is not None and ts in etf_5d.index and not np.isnan(etf_5d.loc[ts]):
            inds["ETF净流入"] = _stub("ETF净流入", _band_etf_5d(etf_5d.loc[ts]))
        if len(stable_s) and ts in stable_s.index:
            past = stable_s.loc[:ts]
            if len(past) > 30 and past.iloc[-31] > 0:
                growth = (past.iloc[-1] / past.iloc[-31] - 1) * 100
                inds["稳定币增速"] = _stub("稳定币增速", _band_stable_growth(growth))

        # 趋势确认
        if not np.isnan(ema140.loc[ts]) and len(ma200.loc[:ts].dropna()) > 31:
            ma200_past = ma200.loc[:ts].dropna()
            slope = (ma200_past.iloc[-1] / ma200_past.iloc[-31] - 1) * 100
            above = price.loc[ts] > ema140.loc[ts]
            if above and slope > 0.5:
                tf = 1
            elif above and slope >= -0.5:
                tf = 0.5
            elif not above and slope < -0.5:
                tf = -1
            elif not above:
                tf = -0.5
            else:
                tf = 0
            inds["趋势过滤器"] = _stub("趋势过滤器", tf)

        # 矿工经济
        pl = _at(src["puell"], d)
        if pl is not None:
            inds["Puell Multiple"] = _stub("Puell Multiple", _band_puell(pl))
        if hash_sma30 is not None and ts in hash_sma30.index and not np.isnan(hash_sma60.loc[ts]):
            above_h = hash_sma30.loc[ts] > hash_sma60.loc[ts]
            sub = (hash_sma30.loc[:ts] > hash_sma60.loc[:ts])
            flip_days = None
            arr = sub.values
            for i in range(len(arr) - 2, -1, -1):
                if bool(arr[i]) != bool(arr[-1]):
                    flip_days = len(arr) - 1 - i
                    break
            if above_h and flip_days is not None and flip_days <= 45:
                hr_sc = 1
            elif above_h:
                hr_sc = 0.25
            else:
                hr_sc = -0.25
            inds["Hash Ribbons"] = _stub("Hash Ribbons", hr_sc)

        # 时间周期
        last_h = max((h for h in HALVING_DATES if h <= ts.to_pydatetime()), default=HALVING_DATES[0])
        months = (ts.to_pydatetime() - last_h).days / 30.44
        inds["减半周期"] = _stub("减半周期", _band_halving(months))

        # ── 战术分组件 ──
        if src["fund"]:
            # 7 日均: 取 d 往前 7 天的所有费率
            rates7 = []
            for k in range(7):
                dd = (ts - timedelta(days=k)).strftime("%Y-%m-%d")
                rates7.extend((src["fund"] or {}).get(dd, []))
            if len(rates7) >= 9:
                inds["资金费率(7d)"] = _stub("资金费率(7d)", _band_funding7d(sum(rates7) / len(rates7) * 100))
        fg = _at(src["fng"], d)
        if fg is not None:
            inds["恐惧贪婪指数"] = _stub("恐惧贪婪指数", _band_fng(fg))
        so = _at(src["sopr"], d)
        if so is not None:
            inds["SOPR"] = _stub("SOPR", _band_sopr(so))
        # 日线单周期近似 (实时版为多周期聚合)
        if not np.isnan(macd_line.loc[ts]) and not np.isnan(macd_sig.loc[ts]):
            inds["MACD"] = _stub("MACD", 0.5 if macd_line.loc[ts] > macd_sig.loc[ts] else -0.5)
        if not np.isnan(rsi_s.loc[ts]):
            inds["RSI(14)"] = _stub("RSI(14)", _band_rsi(rsi_s.loc[ts]))
        if not np.isnan(pct_b.loc[ts]):
            inds["布林带"] = _stub("布林带", _band_bb(pct_b.loc[ts]))

        cycle, _ = _compute_bucket_scores(CYCLE_BUCKETS, inds)
        tactical, _ = _compute_bucket_scores(TACTICAL_BUCKETS, inds)

        entries.append({
            "date": d,
            "ts": f"{d} 00:00:00",
            "btc_price": round(float(price.loc[ts]), 2),
            "total_score": round(float(cycle), 4),
            "recommendation": cycle_recommendation(cycle),
            "tactical_score": round(float(tactical), 4),
            "scores": {n: i.score for n, i in inds.items()},
            "statuses": {},
            "backfilled": True,
        })

    return entries


def ensure_backfilled(cache_dir: str, days: int = 90) -> bool:
    """
    幂等回填入口 (app 启动线程调用)。
    - marker 文件存在 → 跳过
    - 成功 → 合并写入 score_history.json + 写 marker
    - 失败 → 抛异常, 由调用方稍后重试
    """
    marker = os.path.join(cache_dir, _MARKER)
    if os.path.exists(marker):
        return False

    new_entries = reconstruct(days, cache_dir)
    if not new_entries:
        raise RuntimeError("回填结果为空")

    existing = _load_history(cache_dir)
    real_dates = {e["date"] for e in existing if not e.get("backfilled")}
    merged = {e["date"]: e for e in new_entries if e["date"] not in real_dates}
    for e in existing:
        if e["date"] not in merged or not e.get("backfilled"):
            merged[e["date"]] = e
    final = sorted(merged.values(), key=lambda x: x["date"])
    _save_history(cache_dir, final)

    with open(marker, "w") as f:
        f.write(datetime.now().isoformat())
    print(f"✅ 评分历史回填完成: 新增 {len(new_entries)} 天, 合计 {len(final)} 天")
    return True


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache")
    os.makedirs(cdir, exist_ok=True)
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    ensure_backfilled(cdir, n)
