#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.triggers
======================
触发价位表 — "什么价格会翻转哪个信号"的机械反解 (2026-07, 事件研究 B 层)。

设计原则 (事件研究的诚实结论):
- 不承诺胜率。这里的每一行都是可复算的机械条件, 不是预测。
- 硬价位 (精确): MVRV=1 链上投降带 / STH×0.8 / 200周均线地板 / 20W EMA 趋势线
- 评分档位反解 (近似): 固定全部非价格因子的当前分数, 只重算价格派生因子
  (趋势伸展 5 因子分位数 + 趋势过滤器), 网格扫描价格找档位边界。
  ⚠️ 评分对价格**非单调**: 价格上行使趋势过滤器转多、但同时使估值分位数转贵,
  两者方向相反; 且趋势斜率/慢变量不随瞬时价格变化 — 某些档位可能
  "单靠价格变动不可达", 此时如实报告, 这本身就是信息。
- 反解偏差方向: 链上估值 (MVRV-Z/NUPL) 实际会随价格同向转贵但被固定,
  故上行档位的真实触发价略高于估计值 (估计偏乐观), 已在 note 标注。
"""

import numpy as np
import pandas as pd
from typing import Dict, Optional

from .core import GENESIS_DATE, AHR999_A, AHR999_B, IndicatorResult
from .scoring import (CYCLE_BUCKETS, _compute_bucket_scores, PERCENTILE_WINDOW,
                      cycle_recommendation)

# 档位边界 (与 decision.CYCLE_BANDS 阈值一致; 上/下各扫到哪个边界)
_BAND_EDGES = [
    (0.30, "进偏多配置档 (60-80%仓)", "up"),
    (0.15, "进标准配置档 (40-60%仓)", "up"),
    (0.00, "跌入减配档 (15-30%仓)", "down"),
    (-0.30, "跌入防守区 (0-5%仓)", "down"),
]

_SCAN_LO, _SCAN_HI, _SCAN_STEP = 0.5, 2.0, 0.01   # 现价 ±50%/+100%, 1% 步长


def _pct_score_at(series_tail: pd.Series, cur: float) -> float:
    """当前值 cur 在窗口 (含自身) 的分位评分, 复刻 scoring._percentile_score。"""
    tail = series_tail
    if len(tail) < PERCENTILE_WINDOW // 4:
        return float("nan")
    pct = float((tail < cur).mean())
    return (0.5 - pct) * 2


def _trend_factor_scores_at(df: pd.DataFrame, p: float) -> Dict[str, float]:
    """
    假设"今天收盘价 = p", 重算价格派生的计分因子分数。
    只替换末值; 均线/窗口用历史真实序列 (瞬时假设, 不改历史)。
    """
    price = df["price"].copy()
    price.iloc[-1] = p
    out = {}

    ma200 = price.rolling(200).mean().iloc[-1]
    out["Mayer Multiple"] = None
    metrics = {}
    metrics["Mayer Multiple"] = (price / price.rolling(200).mean(), p / ma200)
    if len(price) >= 1400:
        ma1400 = price.rolling(1400).mean().iloc[-1]
        s1400 = (price - price.rolling(1400).mean()) / price.rolling(1400).mean()
        metrics["200-Week Heatmap"] = (s1400, (p - ma1400) / ma1400)
    days = (df.index - pd.Timestamp(GENESIS_DATE)).days.values.astype(float)
    with np.errstate(invalid="ignore", divide="ignore"):
        fair = 10 ** (AHR999_B * np.log10(np.where(days > 0, days, np.nan)) + AHR999_A)
    plaw = pd.Series(price.values / fair, index=df.index)
    metrics["幂律走廊"] = (plaw, float(plaw.iloc[-1]))
    geo200 = float(np.exp(np.log(price).rolling(200).mean().iloc[-1]))
    ahr = plaw * (price / np.exp(np.log(price).rolling(200).mean()))
    metrics["Ahr999"] = (ahr, (p / geo200) * float(plaw.iloc[-1]))

    for name, (series, cur) in metrics.items():
        tail = series.dropna().tail(PERCENTILE_WINDOW)
        out[name] = _pct_score_at(tail, cur)

    # Pi Cycle 顶部探测器 {0,-0.5,-1} (末值替换后 MA 变化极小, 但保持机械一致)
    ma111 = price.rolling(111).mean().iloc[-1]
    ma350x2 = price.rolling(350).mean().iloc[-1] * 2
    if not np.isnan(ma111) and ma350x2 > 0:
        gap = (ma350x2 - ma111) / ma350x2 * 100
        out["Pi Cycle Top"] = -1.0 if gap <= 0 else (-0.5 if gap <= 20 else 0.0)

    # 趋势过滤器: 价格条件随 p 变, 斜率条件用真实历史 (瞬时价格改不了斜率)
    ema140 = price.ewm(span=140, adjust=False).mean().iloc[-1]
    ma200_s = df["price"].rolling(200).mean().dropna()
    slope = (ma200_s.iloc[-1] / ma200_s.iloc[-31] - 1) * 100 if len(ma200_s) > 31 else 0.0
    above = p > ema140
    if above and slope > 0.5:
        tf = 1.0
    elif above and slope >= -0.5:
        tf = 0.5
    elif not above and slope < -0.5:
        tf = -1.0
    elif not above:
        tf = -0.5
    else:
        tf = 0.0
    out["趋势过滤器"] = tf
    return {k: v for k, v in out.items() if v is not None and v == v}


def _cycle_score_at(df: pd.DataFrame, p: float,
                    fixed_scores: Dict[str, float]) -> float:
    """假设价格 p 时的周期分: 价格派生因子重算 + 其余因子固定当前分。"""
    inds = {}
    for name, sc in fixed_scores.items():
        inds[name] = IndicatorResult(name=name, value=1.0, score=float(sc),
                                     color="", status="", priority="P0")
    for name, sc in _trend_factor_scores_at(df, p).items():
        inds[name] = IndicatorResult(name=name, value=1.0, score=float(sc),
                                     color="", status="", priority="P0")
    total, _, _ = _compute_bucket_scores(CYCLE_BUCKETS, inds)
    return float(total)


def compute_trigger_levels(df: pd.DataFrame,
                           indicators: Dict[str, "IndicatorResult"],
                           current_price: float) -> Optional[dict]:
    """
    主入口 (runner 每轮刷新调用)。返回:
    {
      "hard": [ {name, price, side, distance_pct, note} ],   # 精确机械价位
      "bands": [ {name, price|null, distance_pct|null, note} ],  # 档位反解 (近似)
      "meta": {...}
    }
    """
    if df is None or len(df) < 400 or not current_price:
        return None
    price = df["price"]
    hard = []

    def _add(name, level, side, note):
        if level and level > 0:
            hard.append({
                "name": name, "price": round(float(level), 0), "side": side,
                "distance_pct": round((float(level) / current_price - 1) * 100, 1),
                "note": note,
            })

    # 1) 200 周均线地板
    if len(price) >= 1400:
        _add("200周均线地板", price.rolling(1400).mean().iloc[-1], "support",
             "12年历史从未长期跌破的长期持有者成本地板")

    # 2) 20W EMA 趋势线 (趋势过滤器的价格条件; 斜率条件另列)
    ema140 = price.ewm(span=140, adjust=False).mean().iloc[-1]
    ma200_s = price.rolling(200).mean().dropna()
    slope = (ma200_s.iloc[-1] / ma200_s.iloc[-31] - 1) * 100 if len(ma200_s) > 31 else 0.0
    _add("20周EMA趋势线", ema140, "resistance" if current_price < ema140 else "support",
         f"趋势过滤器价格条件; 完全翻正还需200DMA斜率>+0.5%/30d (当前 {slope:+.1f}%)")

    # 3) MVRV=1 链上投降带 (≈ 全网平均持仓成本; Z<0 与其重合)
    nupl_ind = indicators.get("NUPL")
    if nupl_ind is not None and nupl_ind.value == nupl_ind.value:
        nupl = float(nupl_ind.value)
        if nupl < 1:
            mvrv = 1.0 / (1.0 - nupl)
            _add("MVRV=1 链上投降带", current_price / mvrv, "support",
                 "全网平均持仓成本; 跌破=历史级投降带 (12年仅5次), MVRV-Z<0 与其重合")

    # 4) STH 成本线 0.8 投降带 (bd 独占数据, 缺席时跳过)
    sth_ind = indicators.get("STH成本线")
    if sth_ind is not None and sth_ind.value == sth_ind.value and sth_ind.value > 0:
        sth_rp = current_price / float(sth_ind.value)
        _add("STH成本线×0.8 投降带", sth_rp * 0.8, "support",
             "短期持有者集体深度套牢位, 历史投降带 (与 MVRV=1 位大致重合)")
        _add("STH成本线 (牛熊中线)", sth_rp, "resistance" if current_price < sth_rp else "support",
             "短线筹码回本位, 收复它是趋势修复的常见前提")

    # 5) 档位边界反解: 网格扫描 (评分对价格非单调, 不用二分)
    price_members = {"Mayer Multiple", "200-Week Heatmap", "幂律走廊",
                     "Pi Cycle Top", "Ahr999", "趋势过滤器"}
    fixed = {}
    for name, ind in indicators.items():
        if name in price_members:
            continue
        if ind.value is None or ind.value != ind.value:
            continue  # 缺席因子保持缺席 (与现网重归一同口径)
        fixed[name] = float(ind.score)

    grid = np.arange(_SCAN_LO, _SCAN_HI + 1e-9, _SCAN_STEP)
    scores = {}
    for mult in grid:
        p = current_price * float(mult)
        scores[mult] = _cycle_score_at(df, p, fixed)
    cur_score = scores[min(grid, key=lambda m: abs(m - 1.0))]

    bands = []
    for edge, label, direction in _BAND_EDGES:
        hit = None
        if direction == "up":
            for mult in sorted(m for m in grid if m >= 1.0):
                if scores[mult] >= edge:
                    hit = mult
                    break
        else:
            for mult in sorted((m for m in grid if m <= 1.0), reverse=True):
                if scores[mult] < edge:
                    hit = mult
                    break
        if hit is not None and abs(hit - 1.0) > _SCAN_STEP / 2:
            lv = current_price * hit
            bands.append({"name": label, "price": round(lv, 0),
                          "distance_pct": round((hit - 1) * 100, 1),
                          "note": "近似: 固定慢变量因子, 只重算价格派生因子"})
        elif hit is not None:
            bands.append({"name": label, "price": round(current_price, 0),
                          "distance_pct": 0.0, "note": "当前即处边界附近"})
        else:
            rng = f"±{int((1-_SCAN_LO)*100)}%~+{int((_SCAN_HI-1)*100)}%"
            bands.append({"name": label, "price": None, "distance_pct": None,
                          "note": f"扫描 {rng} 内单靠价格变动不可达 — 还需趋势斜率/慢变量翻转"})

    hard.sort(key=lambda x: x["price"], reverse=True)

    # 扫描内评分可达域: 档位普遍"不可达"时, 这两行解释为什么 —
    # 体系对瞬时价格脱敏, 档位转换需要趋势结构变化 (斜率/分位窗滚动) 而非单日价格
    mult_max = max(grid, key=lambda m: scores[m])
    mult_min = min(grid, key=lambda m: scores[m])
    reachable = {
        "max": {"price": round(current_price * mult_max, 0),
                "pct": round((mult_max - 1) * 100, 0),
                "score": round(scores[mult_max], 3)},
        "min": {"price": round(current_price * mult_min, 0),
                "pct": round((mult_min - 1) * 100, 0),
                "score": round(scores[mult_min], 3)},
    }

    return {
        "hard": hard,
        "bands": bands,
        "reachable": reachable,
        "meta": {
            "current_price": round(float(current_price), 0),
            "cur_score_check": round(cur_score, 3),
            "note": ("机械反解, 不构成预测; 档位反解为近似 (链上估值因子随价格上行"
                     "会同步转贵但此处被固定, 上行档位真实触发价略高于估计)。"
                     "价位随均线/慢变量每日漂移。"),
        },
    }
