#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
phase_stats.json 生成器 — 周期相位的历史频率置信度 (由 run_backtest.main 调用)。

规则与温度计配置从 btc_dashboard.cycle_phase 导入 (单一事实源), 本模块只负责:
  1. 12 年逐日相位标注 (与现网同一 classify/smooth 路径)
  2. 各相位 episode 数 / 天数 / 前瞻收益分布 (日级, 样本内)
  3. 模糊态「回调/熊初」episode 级事后分辨 — 只计观察满 365 天的已证实
     episode, 未满一年的如实计 pending (2026-07 统计诚实性要求)
  4. 泡沫段距后续 365 天内最高点的领先天数 (机械口径, 不硬编码顶部日期)
"""

import json
import os
from datetime import datetime

import numpy as np
import pandas as pd

from factors import _BTC_WEB  # noqa: F401  (sys.path 注入)
from btc_dashboard.cycle_phase import (THERMOMETER_BUCKETS, PHASES,
                                       classify_phase, smooth_phases)
import engine

START = "2014-06-01"   # 温度计分位窗就绪后


def _nn(v):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)


def generate(cyc_f: pd.DataFrame, price: pd.Series, out_path: str) -> dict:
    T = engine._daily_scores(THERMOMETER_BUCKETS, cyc_f)["score"]
    trend = cyc_f["趋势过滤器"]
    ath = price.cummax()
    dd = price / ath - 1

    ath_age = pd.Series(0, index=price.index, dtype=int)
    last = price.index[0]
    for ts in price.index:
        if price.loc[ts] >= ath.loc[ts] * 0.999:
            last = ts
        ath_age.loc[ts] = (ts - last).days

    idx = price.index[price.index >= pd.Timestamp(START)]
    raw, dates = [], []
    for ts in idx:
        ph = classify_phase(_nn(T.get(ts)), _nn(trend.get(ts)),
                            _nn(dd.get(ts)), int(ath_age.loc[ts]))
        if ph == "unknown":
            continue
        raw.append(ph)
        dates.append(ts)
    labels = pd.Series(smooth_phases(raw), index=pd.DatetimeIndex(dates))

    # episode 压缩 (≥7 天; 泡沫为急信号不设时长门槛)
    segs, cur = [], None
    for ts, v in labels.items():
        if v != cur:
            segs.append({"start": ts, "end": ts, "phase": v})
            cur = v
        else:
            segs[-1]["end"] = ts
    segs = [s for s in segs
            if (s["end"] - s["start"]).days >= 7 or s["phase"] == "bubble"]

    last_day = price.index[-1]
    fwd = {H: price.shift(-H) / price - 1 for H in (90, 180, 365)}

    phases_out = {}
    for key in PHASES:
        if key == "unknown":
            continue
        mask = labels == key
        eps = [s for s in segs if s["phase"] == key]
        entry = {"name": PHASES[key]["name"],
                 "episodes": len(eps), "days": int(mask.sum()), "fwd": {}}
        for H in (90, 180, 365):
            r = fwd[H].reindex(labels.index)[mask].dropna()
            if len(r) >= 20:
                entry["fwd"][f"{H}d"] = {
                    "median_pct": round(float(r.median()) * 100, 1),
                    "pos_pct": round(float((r > 0).mean()) * 100, 0),
                    "n": int(len(r)),
                }
        phases_out[key] = entry

    # 模糊态事后分辨 (episode 级, 满 365 天观察才算已证实)
    res = {"confirmed_pullback": 0, "confirmed_bear": 0, "pending": 0}
    for s in (x for x in segs if x["phase"] == "pullback_or_bear"):
        a = s["start"]
        if a + pd.Timedelta(days=365) > last_day:
            res["pending"] += 1
            continue
        horizon = price.loc[a:a + pd.Timedelta(days=365)]
        if horizon.max() >= ath.loc[a] * 0.999:
            res["confirmed_pullback"] += 1
        else:
            res["confirmed_bear"] += 1
    if "pullback_or_bear" in phases_out:
        phases_out["pullback_or_bear"]["resolution"] = res

    # 泡沫领先天数: episode 起点 → 随后365天内最高价日
    leads = []
    for s in (x for x in segs if x["phase"] == "bubble"):
        a = s["start"]
        horizon = price.loc[a:a + pd.Timedelta(days=365)]
        if len(horizon) > 30:
            leads.append(int((horizon.idxmax() - a).days))
    if "bubble" in phases_out and leads:
        phases_out["bubble"]["top_lead_days"] = {
            "min": min(leads), "median": int(np.median(leads)), "max": max(leads),
            "n": len(leads),
        }

    out = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "window": f"{START} ~ {last_day.date()}",
        "note": ("历史频率统计, 样本内标定 (相位规则与统计同源同史), n=3~4 个周期; "
                 "是'该判读历史上的含义'而非校准概率, 非收益承诺"),
        "phases": phases_out,
    }
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, out_path)
    print(f"📊 相位置信度数据资产已更新: {out_path}")
    return out
