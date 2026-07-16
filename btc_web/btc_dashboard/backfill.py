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
  期货基差/多空比无历史数据, 回填期间缺席
- 各指标分档阈值与 indicators_v2.py / indicators_short.py 保持一致

2026-06 云端适配 (Render 共享 IP 上 bitcoin-data 限额经常打不通, 回填成功率低):
- 主链路改为 CoinMetrics 社区 API (免key/云IP友好/T-1): MVRV-Z·NUPL·Puell 由
  CM 派生 (与 backtest 校准: 相关 0.9996/0.9999/0.94), 价格序列也以 CM 兜底
- bitcoin-data 降级为可选增强 (STH/SOPR 仅它有; 拿不到当天缺席重归一)
- 回填因子集同步现网 2026-06 配置: 新增 Ahr999 / 交易所余额(30d) / 交易所净流(7d)
  (交易所余额于 2026-07 退出周期评分 — ETF 时代结构性失真, 见 scoring.CYCLE_BUCKETS 注)
"""

import os
import json
import time
import tempfile
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

from .core import (IndicatorResult, GENESIS_DATE, HALVING_DATES, AHR999_A, AHR999_B,
                   halving_band, fetch_btc_data)
from .scoring import (
    CYCLE_BUCKETS, TACTICAL_BUCKETS, _compute_bucket_scores,
    cycle_recommendation, PERCENTILE_WINDOW,
)
from .score_history import _load_history, _save_history, history_write_lock
from .options import fetch_dvol_history

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "Accept": "application/json"}
_SRC_TTL = 7 * 24 * 3600   # 数据源磁盘缓存 7 天
# marker 带版本号: 回填口径修复后 bump 版本, 旧版本回填的条目被判定为污染数据,
# 下次启动自动清除并按新口径重建 (真实快照永远保留)。
# v2 (2026-07-10 对抗性审查修复): Pi Cycle 旧编码 / ETF 日期键 / MVRV-Z·NUPL·Puell 分位混合口径
# v3 (2026-07-15): 回填窗口 90→365 天 — 喂满 decision.REPLAY_DAYS=365 的滞回重放窗口,
#   并支撑月/年尺度评分变化展示; 算力拉长至 2y、资金费率翻页至 ~1y 配套
# v4 (2026-07-16): 趋势伸展桶移除 Mayer Multiple (反信号, 见 scoring.CYCLE_BUCKETS 注) —
#   桶均值逐日变化, 旧回填历史与新口径不可比, 须整段重建
# v5 (2026-07-16): 减半时钟 12-24月段 0→-1 (顶部与崩塌段, 见 core.halving_band 注) —
#   回填窗含该段约 9 个月, 历史随新口径重建
# v6 (2026-07-16): 链上筹码桶移除 交易所余额 (ETF 时代常亮看多灯 + 与ETF净流入双重
#   计数, 见 scoring.CYCLE_BUCKETS 注) — 桶均值逐日变化, 历史整段重建
_MARKER = "score_history_backfilled.v6.marker"
_OLD_MARKERS = ("score_history_backfilled.marker", "score_history_backfilled.v2.marker",
                "score_history_backfilled.v3.marker", "score_history_backfilled.v4.marker",
                "score_history_backfilled.v5.marker")


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


def _fetch_cm_full():
    """
    CoinMetrics 社区 API 全历史日度序列 (免key, 云IP友好, T-1)。
    返回 {"dates": [...], "mcap": [...], "mvrv": [...], "iss": [...],
          "sply_ex": [...], "net": [...], "price": [...]} 或 None。
    单页 page_size=10000 覆盖全历史 (~5800 行), 有 next_page_token 则跟进。
    """
    metrics = "PriceUSD,CapMrktCurUSD,CapMVRVCur,IssTotUSD,SplyExNtv,FlowInExNtv,FlowOutExNtv"
    url = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
    params = {"assets": "btc", "metrics": metrics, "frequency": "1d",
              "start_time": "2010-07-18", "page_size": 10000}
    out = {"dates": [], "mcap": [], "mvrv": [], "iss": [],
           "sply_ex": [], "net": [], "price": []}
    for _ in range(3):  # 最多 3 页, 防御性上限
        r = requests.get(url, params=params, timeout=60, headers=_HEADERS)
        if r.status_code != 200:
            print(f"⚠️ CoinMetrics API HTTP {r.status_code}")
            return None
        payload = r.json() or {}
        for row in payload.get("data", []):
            def _f(key):
                v = row.get(key)
                try:
                    return float(v) if v is not None else None
                except (TypeError, ValueError):
                    return None
            out["dates"].append(row["time"][:10])
            out["price"].append(_f("PriceUSD"))
            out["mcap"].append(_f("CapMrktCurUSD"))
            out["mvrv"].append(_f("CapMVRVCur"))
            out["iss"].append(_f("IssTotUSD"))
            out["sply_ex"].append(_f("SplyExNtv"))
            fin, fout = _f("FlowInExNtv"), _f("FlowOutExNtv")
            out["net"].append((fin - fout) if (fin is not None and fout is not None) else None)
        token = payload.get("next_page_token")
        if not token:
            break
        params["next_page_token"] = token
    return out if len(out["dates"]) >= 500 else None


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
    """
    SoSoValue ETF 日度净流入 → {date: 百万美元}。
    键必须用 iso 全日期: series 的 date 字段是前端图表用的 "MM-DD" 短标签,
    直接当日期键会被 pd.Timestamp 解析成公元 1 年且跨年撞键 (2026-07 审查修复)。
    """
    from .etf_flow import fetch_etf_flow_history
    data = fetch_etf_flow_history(limit=400)
    if not data or not data.get("series"):
        return None
    out = {}
    for row in data["series"]:
        iso = row.get("iso")
        if iso and row.get("total") is not None:
            out[iso] = float(row["total"])
    return out or None


def _fetch_hashrate_2y():
    """mempool.space 两年日度算力 → [(date, hashrate)]。
    2y 而非 1y: 365 天回填的最早日期也要有 SMA60 (需 ~425 天), 1y 会让
    早期段 Hash Ribbons 整段缺席。"""
    r = requests.get("https://mempool.space/api/v1/mining/hashrate/2y",
                     timeout=20, headers=_HEADERS)
    if r.status_code != 200:
        return None
    out = []
    for x in (r.json() or {}).get("hashrates", []):
        d = datetime.fromtimestamp(int(x["timestamp"])).strftime("%Y-%m-%d")
        out.append([d, float(x["avgHashrate"])])
    return out or None


def _fetch_funding_history():
    """OKX 资金费率历史 (12 页 ≈ 400 天) → {date: [rates]}。
    页数按 365 天回填配套; 更早日期该因子缺席、桶内重归一 (覆盖率戳如实记录)。"""
    out = {}
    after = None
    for _ in range(12):
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
    # 2026-07 重标定: 近300交易日 5日合计分布 10/25/75/90 分位 (calc_etf_net_flow 同阈值)
    return 1 if m > 1700 else 0.5 if m > 900 else 0 if m > -700 else -0.5 if m > -1300 else -1

def _band_stable_growth(pct):
    # 2026-07 重标定: 2021+ 30日增速分布 10/25/75/90 分位 (calc_stablecoin_growth 同阈值)
    return 1 if pct > 12.0 else 0.5 if pct > 5.5 else 0 if pct > -0.5 else -0.5 if pct > -2.0 else -1

def _band_halving(months):
    # 收敛到 core.halving_band 单一事实源 (2026-07: >30月 -1→+0.5 反信号修正;
    # 当前365天回填窗为第15-27月, 新旧档位逐日一致 → 无需 bump marker)
    return halving_band(months)

def _band_netflow7(v):
    # 交易所净流(7d) 占存量% (calc_exchange_netflow_7d 同阈值)
    return 1 if v <= -0.8 else 0.5 if v <= -0.4 else 0 if v < 0.45 else -0.5 if v < 1.0 else -1

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
    # ── 主链路: CoinMetrics 全历史 (云IP友好); bitcoin-data 降级为可选增强 ──
    cm = _cached_fetch(cache_dir, "coinmetrics-full", _fetch_cm_full)

    src = {
        "sth":    _cached_fetch(cache_dir, "sth-realized-price", lambda: _fetch_bd_series("sth-realized-price", "sthRealizedPrice", days)),
        "sopr":   _cached_fetch(cache_dir, "sopr", lambda: _fetch_bd_series("sopr", "sopr", days)),
        "fng":    _cached_fetch(cache_dir, "fng", _fetch_fng),
        "stable": _cached_fetch(cache_dir, "stablecoins", _fetch_stablecoins),
        "etf":    _cached_fetch(cache_dir, "etf-daily-v2", _fetch_etf_daily),  # v2: iso 全日期键
        # 缓存键随范围扩展 bump (1y→2y / 3页→12页), 避免读到旧短序列磁盘缓存
        "hash":   _cached_fetch(cache_dir, "hashrate-2y", _fetch_hashrate_2y),
        "fund":   _cached_fetch(cache_dir, "funding-history-1y", _fetch_funding_history),
        "mvrv": None, "nupl": None, "puell": None,   # 下方由 CM 派生或 bd 兜底
    }

    # ── CM 派生: MVRV-Z / NUPL / Puell / 净流7d (与 backtest 同公式) ──
    # 2026-07 审查修复: MVRV-Z/NUPL/Puell 改为与现网 _pct_floor_score 同口径的
    # "4年分位为主 + 绝对阈值极值保底" 混合评分 (旧版只有纯绝对阈值, 与实时口径漂移)。
    # 分位/保底两条腿与 backtest/factors.py 完全同源:
    #   MVRV-Z: 分位腿=rolling(730)σ 派生序列, 保底腿=全历史扩张σ 绝对阈值
    #   NUPL / Puell: 分位腿与保底腿同一序列
    netflow7 = None
    mvrvz_sc = nupl_sc = puell_sc = None   # 预合成的逐日评分 {date: score}
    cm_price_s = None

    def _rolling_pct_score(series: pd.Series) -> pd.Series:
        """复刻 scoring._percentile_score 的滚动 4 年分位评分 (点时序, 无前视)"""
        s = series.dropna()
        if s.empty:
            return pd.Series(np.nan, index=series.index)
        minp = PERCENTILE_WINDOW // 4
        r = s.rolling(PERCENTILE_WINDOW, min_periods=minp).rank(method="min")
        n = s.rolling(PERCENTILE_WINDOW, min_periods=minp).count()
        return ((0.5 - (r - 1) / n) * 2).reindex(series.index)

    def _extreme_combine(pct: pd.Series, abs_: pd.Series) -> pd.Series:
        """复刻 indicators_v2._pct_floor_score: 分位为主, 绝对阈值做极值保底"""
        out = pct.copy()
        both = pct.notna() & abs_.notna()
        use_abs = both & (abs_.abs() > pct.abs())
        out[use_abs] = abs_[use_abs]
        return out.where(pct.notna(), abs_)

    if cm:
        cm_idx = pd.to_datetime(cm["dates"])
        mc = pd.Series(cm["mcap"], index=cm_idx, dtype=float)
        mvr = pd.Series(cm["mvrv"], index=cm_idx, dtype=float).replace(0, np.nan)
        rc = mc / mvr
        z_s = (mc - rc) / mc.expanding(min_periods=365).std()
        z730_s = (mc - rc) / mc.rolling(730).std()
        nupl_s = 1 - 1 / mvr
        iss = pd.Series(cm["iss"], index=cm_idx, dtype=float)
        puell_s = iss / iss.rolling(365).mean()
        sply = pd.Series(cm["sply_ex"], index=cm_idx, dtype=float)
        net_s = pd.Series(cm["net"], index=cm_idx, dtype=float)
        netflow7_s = net_s.rolling(7).sum() / sply * 100

        def _tail_dict(s, n=days + 40):
            sub = s.tail(n).dropna()
            return {ts.strftime("%Y-%m-%d"): float(v) for ts, v in sub.items()} or None

        mvrvz_sc = _tail_dict(_extreme_combine(
            _rolling_pct_score(z730_s),
            z_s.map(_band_mvrv_z, na_action="ignore")))
        nupl_sc = _tail_dict(_extreme_combine(
            _rolling_pct_score(nupl_s),
            nupl_s.map(_band_nupl, na_action="ignore")))
        puell_sc = _tail_dict(_extreme_combine(
            _rolling_pct_score(puell_s),
            puell_s.map(_band_puell, na_action="ignore")))
        netflow7 = _tail_dict(netflow7_s)
        cm_price_s = pd.Series(cm["price"], index=cm_idx, dtype=float).dropna()

    # 按"预合成结果"逐指标回退 bitcoin-data 原始值序列 (旧链路, 90 天序列不足
    # 4 年分位窗, 退化为纯绝对阈值近似 — 与实时口径有偏差, 仅作最后兜底)。
    # 逐指标而非按 cm 整体判断: CM 可能 HTTP 成功但单列失效 (如 CapMVRVCur 停更
    # → 该指标预合成为 None), 此时仍需列级兜底; CM 单列成功的指标不打 bd (限流)。
    if mvrvz_sc is None:
        src["mvrv"] = _cached_fetch(cache_dir, "mvrv-zscore", lambda: _fetch_bd_series("mvrv-zscore", "mvrvZscore", days))
    if nupl_sc is None:
        src["nupl"] = _cached_fetch(cache_dir, "nupl", lambda: _fetch_bd_series("nupl", "nupl", days))
    if puell_sc is None:
        src["puell"] = _cached_fetch(cache_dir, "puell-multiple", lambda: _fetch_bd_series("puell-multiple", "puellMultiple", days))

    ok = [k for k, v in src.items() if v] + (["cm"] if cm else [])
    print(f"📦 backfill 数据源: {len(ok)}/11 可用 ({', '.join(ok)})")
    # 门槛: CM 可用即放行; 否则按旧规则要求 bd 核心序列 ≥3
    if not cm:
        core_ok = sum(1 for k in ("mvrv", "sth", "nupl", "sopr", "puell") if src[k])
        if core_ok < 3:
            raise RuntimeError(f"CM 不可用且链上核心序列仅 {core_ok}/5, 放弃本轮回填")

    # ── 价格序列: 常规链路失败时用 CM 价格兜底 (回填不含今天, T-1 足够) ──
    df = None
    try:
        df = fetch_btc_data()
    except Exception as e:
        print(f"⚠️ fetch_btc_data 失败: {e}")
    if (df is None or len(df) < 400) and cm_price_s is not None and len(cm_price_s) > 1400:
        print("↩️ 价格序列改用 CoinMetrics 兜底")
        df = pd.DataFrame({"price": cm_price_s})
    if df is None or len(df) < 400:
        raise RuntimeError("价格序列不可用 (常规链路与 CM 兜底均失败)")
    price = df['price']

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
    # Ahr999 = (价格/200日几何均价) × (价格/幂律估值), 与 scoring 同公式
    geo200 = np.exp(np.log(price).rolling(200).mean())
    ahr_s = (price / geo200) * plaw_s

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
    # 先按交易日 rolling(5) 再 reindex 到日历日 ffill(limit=4): 周末/假日沿用最近
    # 5 个交易日合计, 与实时 calc_etf_net_flow 及 backtest/factors.py 口径一致
    # (旧版周末整天缺席, 每周 ~2/7 天数因子集与另两处分裂)
    etf_5d = (etf_s.rolling(5).sum().reindex(df.index).ffill(limit=4)
              if len(etf_s) else None)

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
        for name, s in (("Mayer Multiple", mayer_s), ("200-Week Heatmap", w200_s),
                        ("幂律走廊", plaw_s), ("Ahr999", ahr_s)):
            sc = _percentile_at(s, ts)
            if not np.isnan(sc):
                inds[name] = _stub(name, sc)
        if not np.isnan(ma111.loc[ts]) and not np.isnan(ma350.loc[ts]):
            ma350x2 = ma350.loc[ts] * 2
            gap = (ma350x2 - ma111.loc[ts]) / ma350x2 * 100
            # 顶部探测器三态 {0,-0.5,-1}: "远离交叉"是无信号(0)而非看多(+1)
            # (与 indicators_long.calc_pi_cycle / backtest 2026-07 修复口径一致)
            inds["Pi Cycle Top"] = _stub("Pi Cycle Top", -1 if gap <= 0 else -0.5 if gap <= 20 else 0)

        # 链上筹码 — 优先 CM 预合成的"分位为主+绝对保底"评分; CM 缺席退 bd 绝对阈值
        zsc = _at(mvrvz_sc, d)
        if zsc is not None:
            inds["MVRV-Z"] = _stub("MVRV-Z", zsc)
        else:
            z = _at(src["mvrv"], d)
            if z is not None:
                inds["MVRV-Z"] = _stub("MVRV-Z", _band_mvrv_z(z))
        sth = _at(src["sth"], d)
        if sth and sth > 0:
            inds["STH成本线"] = _stub("STH成本线", _band_sth_ratio(price.loc[ts] / sth))
        nsc = _at(nupl_sc, d)
        if nsc is not None:
            inds["NUPL"] = _stub("NUPL", nsc)
        else:
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
        psc = _at(puell_sc, d)
        if psc is not None:
            inds["Puell Multiple"] = _stub("Puell Multiple", psc)
        else:
            pl = _at(src["puell"], d)
            if pl is not None:
                inds["Puell Multiple"] = _stub("Puell Multiple", _band_puell(pl))
        if hash_sma30 is not None and ts in hash_sma30.index and not np.isnan(hash_sma60.loc[ts]):
            above_h = hash_sma30.loc[ts] > hash_sma60.loc[ts]
            # 翻转扫描只在两条均线均有效的区间内进行 — NaN 段(60日窗未满)会让
            # 比较恒为 False, 把数据边界误判成状态翻转 (2026-07 审查修复)
            h30, h60 = hash_sma30.loc[:ts], hash_sma60.loc[:ts]
            valid = h30.notna() & h60.notna()
            sub = (h30 > h60)[valid]
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
        nf7 = _at(netflow7, d)
        if nf7 is not None:
            inds["交易所净流(7d)"] = _stub("交易所净流(7d)", _band_netflow7(nf7))
        # 日线单周期近似 (实时版为多周期聚合)
        if not np.isnan(macd_line.loc[ts]) and not np.isnan(macd_sig.loc[ts]):
            inds["MACD"] = _stub("MACD", 0.5 if macd_line.loc[ts] > macd_sig.loc[ts] else -0.5)
        if not np.isnan(rsi_s.loc[ts]):
            inds["RSI(14)"] = _stub("RSI(14)", _band_rsi(rsi_s.loc[ts]))
        if not np.isnan(pct_b.loc[ts]):
            inds["布林带"] = _stub("布林带", _band_bb(pct_b.loc[ts]))

        cycle, _, cycle_cov = _compute_bucket_scores(CYCLE_BUCKETS, inds)
        tactical, _, tactical_cov = _compute_bucket_scores(TACTICAL_BUCKETS, inds)

        entries.append({
            "date": d,
            "ts": f"{d} 00:00:00",
            "btc_price": round(float(price.loc[ts]), 2),
            "total_score": round(float(cycle), 4),
            "recommendation": cycle_recommendation(cycle),
            "tactical_score": round(float(tactical), 4),
            "cycle_coverage": round(float(cycle_cov), 3),
            "tactical_coverage": round(float(tactical_cov), 3),
            "scores": {n: i.score for n, i in inds.items()},
            "statuses": {},
            "backfilled": True,
        })

    return entries


def ensure_backfilled(cache_dir: str, days: int = 90) -> bool:
    """
    幂等回填入口 (app 启动线程调用)。
    - 当前版本 marker 存在 → 跳过
    - 旧版本 marker 存在 → 判定历史中 backfilled 条目为旧口径污染数据,
      **全部清除**后按新口径重建 (真实快照永远保留, 决策层滞回重放随之自愈)
    - 成功 → 合并写入 score_history.json + 写新 marker + 清理旧 marker
    - 失败 → 抛异常, 由调用方稍后重试
    """
    marker = os.path.join(cache_dir, _MARKER)
    if os.path.exists(marker):
        return False
    stale_rebuild = any(os.path.exists(os.path.join(cache_dir, m)) for m in _OLD_MARKERS)

    existing = _load_history(cache_dir)
    if stale_rebuild:
        purged_dates = [e["date"] for e in existing if e.get("backfilled")]
        existing = [e for e in existing if not e.get("backfilled")]
        if purged_dates:
            # 重建窗口扩到被清除条目的最早日期, 历史深度不因换版本单向缩水
            # (决策滞回重放 REPLAY_DAYS=365 依赖这段深度)
            span = (datetime.now() - datetime.strptime(min(purged_dates), "%Y-%m-%d")).days + 1
            days = max(days, span)
        print(f"🧹 检测到旧版本回填 marker: 清除 {len(purged_dates)} 条旧口径回填条目, "
              f"按 v2 口径重建 {days} 天")

    new_entries = reconstruct(days, cache_dir)
    if not new_entries:
        raise RuntimeError("回填结果为空")

    # 合并阶段持锁并在锁内重读: reconstruct 耗时数分钟, 期间刷新线程可能已写入
    # 新快照, 无锁 load→merge→save 会把它吞掉 (2026-07 审计遗留批修复)。
    # 锁只包住秒级的读-改-写, 不包 reconstruct。(线程锁+fcntl 文件锁双层,
    # 跨 gunicorn master/worker 进程也互斥)
    with history_write_lock(cache_dir):
        existing = _load_history(cache_dir)
        if stale_rebuild:
            existing = [e for e in existing if not e.get("backfilled")]
        real_dates = {e["date"] for e in existing if not e.get("backfilled")}
        merged = {e["date"]: e for e in new_entries if e["date"] not in real_dates}
        for e in existing:
            if e["date"] not in merged or not e.get("backfilled"):
                merged[e["date"]] = e
        final = sorted(merged.values(), key=lambda x: x["date"])
        _save_history(cache_dir, final)

    with open(marker, "w") as f:
        f.write(datetime.now().isoformat())
    for m in _OLD_MARKERS:
        try:
            os.remove(os.path.join(cache_dir, m))
        except OSError:
            pass
    print(f"✅ 评分历史回填完成: 新增 {len(new_entries)} 天, 合计 {len(final)} 天")
    return True


# ============================================================
# DVOL 日线回填 (data/dvol_history.json，持久参考数据，非缓存)
# ============================================================

_DVOL_VERSION = "v1"
_DVOL_START_MS = 1616544000000  # 2021-03-24


def backfill_dvol(data_dir: str = None) -> int:
    """
    幂等把 Deribit DVOL 日线 (2021-03→今) 写入 data/dvol_history.json
    (`{"version":"v1","series":[[ts,close],...]}`)，返回本次新增点数。
    已存在且 marker 版本一致则只增量补齐尾部；不传 data_dir 时默认写入
    模块相对的 btc_dashboard/data/ 目录 (与 band_stats.json 同目录) ——
    这是持久参考数据，不是可随时清空重建的运行时缓存。
    """
    if data_dir is None:
        data_dir = os.path.join(os.path.dirname(__file__), "data")
    path = os.path.join(data_dir, "dvol_history.json")
    existing = {}
    if os.path.exists(path):
        try:
            doc = json.load(open(path))
            if doc.get("version") == _DVOL_VERSION:
                existing = {int(ts): v for ts, v in doc.get("series", [])}
        except Exception:
            existing = {}
    start = (max(existing) + 86400000) if existing else _DVOL_START_MS
    end = int(time.time() * 1000)
    new_pts = fetch_dvol_history(start, end) if end > start else []
    added = 0
    for ts, v in new_pts:
        if ts not in existing:
            existing[ts] = v
            added += 1
    if added:
        series = sorted(existing.items())
        os.makedirs(data_dir, exist_ok=True)
        tmp = path + ".tmp"
        json.dump({"version": _DVOL_VERSION, "series": series}, open(tmp, "w"))
        os.replace(tmp, path)
    return added


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache")
    os.makedirs(cdir, exist_ok=True)
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 365
    ensure_backfilled(cdir, n)
