#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.factors
================
逐日 point-in-time 因子评分序列，1:1 复刻现网打分阈值
(btc_web/btc_dashboard/{scoring,indicators_v2,indicators_long,indicators_short,indicators_aux}.py)。

防前视原则:
- 分位数归一化: 仅用截至当日的过去 1460 天 (滚动窗), 与现网 _percentile_score 完全一致
- MVRV-Z 的 σ(市值): 用截至当日的全历史扩张标准差 (Glassnode 定义)
- 均线/EMA/重采样: 只依赖当日及之前数据
"""

import sys
import os

import numpy as np
import pandas as pd

# 引入现网常量, 保证参数与生产代码同源
_BTC_WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "btc_web")
sys.path.insert(0, _BTC_WEB)
from btc_dashboard.core import GENESIS_DATE, AHR999_A, AHR999_B, HALVING_DATES  # noqa: E402
from btc_dashboard.scoring import PERCENTILE_WINDOW  # noqa: E402


# ------------------------------------------------------------
# 工具
# ------------------------------------------------------------

def _step_score(series: pd.Series, edges, scores) -> pd.Series:
    """
    阶梯映射: edges 为升序阈值 [e1..ek], scores 长度 k+1。
    value < e1 → scores[0]; e1 <= value < e2 → scores[1]; ...; value >= ek → scores[k]
    NaN → NaN。
    """
    out = pd.Series(np.nan, index=series.index, dtype=float)
    mask = series.notna()
    v = series[mask]
    idx = np.searchsorted(np.asarray(edges, dtype=float), v.values, side="right")
    out.loc[mask] = np.asarray(scores, dtype=float)[idx]
    return out


def rolling_percentile_score(series: pd.Series,
                             window: int = PERCENTILE_WINDOW) -> pd.Series:
    """
    复刻 scoring._percentile_score:
      pct = (窗口内严格小于当前值的占比); score = (0.5 - pct) * 2
    现网要求 len(非NaN) >= window//4 (=365), 再取 tail(window)。
    rank(method='min') - 1 恰为"严格小于"的个数, 与现网逐日一致。
    """
    s = series.dropna()
    if s.empty:
        return pd.Series(np.nan, index=series.index)
    minp = window // 4
    r = s.rolling(window, min_periods=minp).rank(method="min")
    n = s.rolling(window, min_periods=minp).count()
    pct = (r - 1) / n
    score = (0.5 - pct) * 2
    return score.reindex(series.index)


# ------------------------------------------------------------
# 周期分因子
# ------------------------------------------------------------

def cycle_factor_scores(cm: pd.DataFrame,
                        bd_sth: pd.Series,
                        etf: pd.Series,
                        stable: pd.Series) -> pd.DataFrame:
    """
    返回 DataFrame(index=日期), 列名与 scoring.CYCLE_BUCKETS members 一致。
    cm: CoinMetrics 日度表 (price/mcap/mvrv/hashrate/iss_usd/sply_ex)
    bd_sth: STH 已实现价格 (bitcoin-data, 近4年)
    etf: SoSoValue 日度净流入 (USD)
    stable: DefiLlama 稳定币总市值
    """
    idx = cm.index
    price = cm["price"]
    out = pd.DataFrame(index=idx)

    # ---- 趋势伸展桶: 滚动分位数 (复刻 compute_percentile_overrides) ----
    mayer = price / price.rolling(200).mean()
    out["Mayer Multiple"] = rolling_percentile_score(mayer)

    ma1400 = price.rolling(1400).mean()
    w200 = (price - ma1400) / ma1400
    out["200-Week Heatmap"] = rolling_percentile_score(w200)

    days = (idx - pd.Timestamp(GENESIS_DATE)).days.values.astype(float)
    with np.errstate(invalid="ignore", divide="ignore"):
        fair = 10 ** (AHR999_B * np.log10(np.where(days > 0, days, np.nan)) + AHR999_A)
    powerlaw = pd.Series(price.values / fair, index=idx)
    out["幂律走廊"] = rolling_percentile_score(powerlaw)

    # Ahr999 (2026-06 入桶): (价格/200日几何均价) × (价格/幂律估值), 分位数归一化
    geo200 = np.exp(np.log(price).rolling(200).mean())
    out["Ahr999"] = rolling_percentile_score((price / geo200) * powerlaw)

    # Pi Cycle Top: 离散打分 (calc_pi_cycle, 不走分位数)
    ma111 = price.rolling(111).mean()
    ma350x2 = price.rolling(350).mean() * 2
    gap_pct = (ma350x2 - ma111) / ma350x2 * 100
    pi = pd.Series(np.nan, index=idx, dtype=float)
    valid = ma111.notna() & ma350x2.notna()
    pi[valid & (ma111 >= ma350x2)] = -1.0
    pi[valid & (ma111 < ma350x2) & (gap_pct <= 20)] = 0.0
    pi[valid & (ma111 < ma350x2) & (gap_pct > 20)] = 1.0
    out["Pi Cycle Top"] = pi

    # ---- 链上筹码桶 ----
    # MVRV-Z = (市值 - 已实现市值) / 全历史扩张 std(市值)   [calc_mvrv_z 阈值]
    mcap = cm["mcap"]
    rcap = mcap / cm["mvrv"]
    mvrv_z = (mcap - rcap) / mcap.expanding(min_periods=365).std()
    out["MVRV-Z"] = _step_score(mvrv_z, [0, 1, 3, 5], [1, 0.5, 0, -0.5, -1])

    # STH成本线: 现价/STH已实现价格   [calc_sth_realized_price 阈值]
    sth_ratio = price / bd_sth.reindex(idx)
    out["STH成本线"] = _step_score(sth_ratio, [0.80, 0.95, 1.15, 1.35],
                                   [1, 0.5, 0, -0.5, -1])

    # NUPL = (市值-已实现市值)/市值 = 1 - 1/MVRV   [calc_nupl 阈值]
    nupl = 1 - 1 / cm["mvrv"]
    out["NUPL"] = _step_score(nupl, [0, 0.25, 0.5, 0.75], [1, 0.5, 0, -0.5, -1])

    # 交易所余额 v2 (2026-06 重构): 30日存量变化率, 2018+ 分位数阈值
    # 与现网 calc_exchange_balance_v2 同口径 (10/25/75/90 分位 ≈ -2.1/-0.85/+1.3/+2.9)
    sply_ex = cm["sply_ex"]
    d30 = (sply_ex / sply_ex.shift(30) - 1) * 100
    out["交易所余额"] = _step_score(d30, [-2.1, -0.85, 1.3, 2.9],
                                    [1, 0.5, 0, -0.5, -1])

    # ---- 资金流桶 ----
    # ETF净流入: 近5个交易日合计(百万美元)   [calc_etf_net_flow 阈值]
    etf_m = (etf / 1e6).rolling(5).sum()
    out["ETF净流入"] = _step_score(etf_m.reindex(idx).ffill(limit=4),
                                   [-1000, -200, 200, 1000],
                                   [-1, -0.5, 0, 0.5, 1])

    # 稳定币增速: 最新 vs 30 天前   [calc_stablecoin_growth 阈值]
    st = stable.reindex(idx).ffill(limit=3)
    growth = (st / st.shift(30) - 1) * 100
    out["稳定币增速"] = _step_score(growth, [-2.5, -1.0, 1.0, 2.5],
                                    [-1, -0.5, 0, 0.5, 1])

    # ---- 趋势确认桶 ----   [calc_trend_filter]
    ema20w = price.ewm(span=140, adjust=False).mean()
    ma200 = price.rolling(200).mean()
    slope_pct = (ma200 / ma200.shift(30) - 1) * 100
    above = price > ema20w
    slope_up = slope_pct > 0.5
    slope_down = slope_pct < -0.5
    trend = pd.Series(np.nan, index=idx, dtype=float)
    valid = ma200.notna() & slope_pct.notna()
    trend[valid & above & slope_up] = 1.0
    trend[valid & above & ~slope_up & ~slope_down] = 0.5
    trend[valid & ~above & slope_down] = -1.0
    trend[valid & ~above & ~slope_down] = -0.5
    trend[valid & above & slope_down] = 0.0   # 现网 else 分支: 价格在上但斜率向下
    out["趋势过滤器"] = trend

    # ---- 矿工经济桶 ----
    # Puell = 日发行价值 / 365日均值   [calc_puell_multiple 阈值]
    puell = cm["iss_usd"] / cm["iss_usd"].rolling(365).mean()
    out["Puell Multiple"] = _step_score(puell, [0.5, 0.8, 2.0, 4.0],
                                        [1, 0.5, 0, -0.5, -1])

    # Hash Ribbons   [calc_hash_ribbons]
    hr = cm["hashrate"]
    sma30 = hr.rolling(30).mean()
    sma60 = hr.rolling(60).mean()
    above_hr = (sma30 > sma60)
    # 距最近一次状态翻转的天数
    state_change = above_hr != above_hr.shift(1)
    grp = state_change.cumsum()
    days_in_state = above_hr.groupby(grp).cumcount() + 1
    ribbons = pd.Series(np.nan, index=idx, dtype=float)
    valid = sma60.notna()
    ribbons[valid & above_hr & (days_in_state <= 45)] = 1.0
    ribbons[valid & above_hr & (days_in_state > 45)] = 0.25
    ribbons[valid & ~above_hr] = -0.25
    out["Hash Ribbons"] = ribbons

    # ---- 时间周期桶 ----   [calc_halving_cycle: 12/24 个月阈值]
    halvings = pd.Series([pd.Timestamp(d) for d in HALVING_DATES])
    last_h = pd.Series(
        [halvings[halvings <= d].max() if (halvings <= d).any() else halvings.iloc[0]
         for d in idx], index=idx)
    months_since = (pd.Series(idx, index=idx) - last_h).dt.days / 30.44
    halv = pd.Series(0.0, index=idx)
    halv[months_since <= 12] = 1.0
    halv[months_since > 24] = -1.0
    out["减半周期"] = halv

    return out


# ------------------------------------------------------------
# 战术分因子
# ------------------------------------------------------------

def _rsi_last(prices: np.ndarray, period: int = 14):
    """复刻 calculate_single_rsi (rolling mean 版): 返回末日 RSI 或 None"""
    if len(prices) < period + 1:
        return None
    delta = np.diff(prices)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    if len(gain) < period:
        return None
    avg_gain = gain[-period:].mean()
    avg_loss = loss[-period:].mean()
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else None
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _rsi_vote(rsi):
    """RSI 值 → (trend, score)   [calc_rsi 内单周期映射]"""
    if rsi is None or np.isnan(rsi):
        return None
    if rsi >= 80:
        return ("超买", -1)
    if rsi >= 70:
        return ("超买", -0.5)
    if rsi <= 20:
        return ("超卖", 1)
    if rsi <= 30:
        return ("超卖", 0.5)
    return ("中性", 0)


def _rsi_composite(votes):
    """复刻 calc_rsi 多周期汇总"""
    votes = [v for v in votes if v is not None]
    n = len(votes)
    if n == 0:
        return np.nan
    ob = sum(1 for t, _ in votes if t == "超买")
    os_ = sum(1 for t, _ in votes if t == "超卖")
    neu = n - ob - os_
    if ob > os_ and ob > neu:
        return -1.0 if ob >= n * 0.8 else -0.5
    if os_ > ob and os_ > neu:
        return 1.0 if os_ >= n * 0.8 else 0.5
    return 0.0


def _macd_last(prices: pd.Series):
    """复刻 calculate_single_macd: 返回 (trend, strength) 或 None"""
    if len(prices) < 35:
        return None
    ema12 = prices.ewm(span=12, adjust=False).mean()
    ema26 = prices.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    cm_, cs = macd.iloc[-1], signal.iloc[-1]
    ch, ph = hist.iloc[-1], hist.iloc[-2] if len(hist) > 1 else 0
    golden = cm_ > cs and macd.iloc[-2] <= signal.iloc[-2]
    death = cm_ < cs and macd.iloc[-2] >= signal.iloc[-2]
    if golden:
        return ("多", 2)
    if death:
        return ("空", 2)
    if cm_ > cs:
        return ("多", 1 if ch > ph else 0.5)
    return ("空", 1 if ch < ph else 0.5)


def _macd_composite(votes):
    """复刻 calc_macd 多周期汇总"""
    votes = [v for v in votes if v is not None]
    n = len(votes)
    if n == 0:
        return np.nan
    bull = sum(1 for t, _ in votes if t == "多")
    bear = n - bull
    if bull > bear:
        ratio = bull / n
        return 1.0 if ratio >= 0.8 else (0.5 if ratio >= 0.5 else 0.2)
    if bear > bull:
        ratio = bear / n
        return -1.0 if ratio >= 0.8 else (-0.5 if ratio >= 0.5 else -0.2)
    return 0.0


def momentum_scores(price: pd.Series, start: str = "2017-06-01") -> pd.DataFrame:
    """
    逐日重算 RSI 与 MACD 复合分 (有重采样部分桶, 必须日循环保证 point-in-time)。
    现网差异声明:
    - 现网 RSI 的 "4H/12H" 实际就是日线序列切片 (无真实盘中数据), 此处如实复刻
    - 现网 MACD 含 OKX 真实 4H/12H K线两腿, 历史不可得, 回测仅 日/周/月 三腿
    """
    dates = price.index[price.index >= pd.Timestamp(start)]
    rsi_out, macd_out = {}, {}
    vals = price.values
    pos_of = {d: i for i, d in enumerate(price.index)}

    for d in dates:
        i = pos_of[d]
        hist = price.iloc[: i + 1]
        arr = vals[: i + 1]

        wk = hist.resample("W").last().dropna()
        mo = hist.resample("ME").last().dropna()
        yr = hist.resample("YE").last().dropna()

        # ---- RSI: 日线 + 伪4H(tail 6的倍数) + 伪12H(tail 一半) + 周/月/年 ----
        votes = [_rsi_vote(_rsi_last(arr))]
        if len(arr) >= 70:
            n6 = len(arr) // 6 * 6
            votes.append(_rsi_vote(_rsi_last(arr[-n6:])))
            votes.append(_rsi_vote(_rsi_last(arr[-(len(arr) // 2):])))
        if len(wk) >= 15:
            votes.append(_rsi_vote(_rsi_last(wk.values)))
        if len(mo) >= 15:
            votes.append(_rsi_vote(_rsi_last(mo.values)))
        if len(yr) >= 5:
            votes.append(_rsi_vote(_rsi_last(yr.values, period=min(14, len(yr) - 1))))
        rsi_out[d] = _rsi_composite(votes)

        # ---- MACD: 日 + 周 + 月 ----
        mvotes = [_macd_last(hist)]
        if len(wk) >= 35:
            mvotes.append(_macd_last(wk))
        if len(mo) >= 35:
            mvotes.append(_macd_last(mo))
        macd_out[d] = _macd_composite(mvotes)

    return pd.DataFrame({"RSI(14)": pd.Series(rsi_out),
                         "MACD": pd.Series(macd_out)})


def tactical_factor_scores(cm: pd.DataFrame,
                           funding: pd.Series,
                           fng: pd.Series,
                           bd_sopr: pd.Series,
                           momentum: pd.DataFrame) -> pd.DataFrame:
    """战术分因子 (除动量外全部向量化)。期货基差/多空比无免费历史 → 缺失剔除。"""
    idx = cm.index
    price = cm["price"]
    out = pd.DataFrame(index=idx)

    # 资金费率(7d): 最近 21 次 8h 费率均值×100   [calc_funding_rate_7d 阈值]
    f = funding.sort_index()
    f21 = f.rolling(21).mean() * 100
    daily_f = f21.groupby(f21.index.normalize()).last()
    out["资金费率(7d)"] = _step_score(daily_f.reindex(idx),
                                      [-0.05, -0.02, 0.02, 0.05],
                                      [1, 0.5, 0, -0.5, -1])

    # MACD / RSI
    out["MACD"] = momentum["MACD"].reindex(idx)
    out["RSI(14)"] = momentum["RSI(14)"].reindex(idx)

    # SOPR   [calc_sopr 阈值]
    sopr = bd_sopr.reindex(idx)
    out["SOPR"] = _step_score(sopr, [0.97, 0.995, 1.02, 1.05],
                              [1, 0.5, 0, -0.5, -1])

    # 布林带   [calc_bollinger_bands]
    mid = price.rolling(20).mean()
    std = price.rolling(20).std()
    upper, lower = mid + 2 * std, mid - 2 * std
    width = upper - lower
    pos = (price - lower) / width * 100
    boll = pd.Series(np.nan, index=idx, dtype=float)
    valid = mid.notna() & (width > 0)
    boll[valid] = 0.0
    boll[valid & (pos > 80)] = -0.3
    boll[valid & (pos < 20)] = 0.3
    boll[valid & (price >= upper)] = -0.5
    boll[valid & (price <= lower)] = 0.5
    out["布林带"] = boll

    # 恐惧贪婪 v2   [calc_fear_greed_v2 阈值: ≤20,≤30,<70,<80]
    fg = fng.reindex(idx)
    out["恐惧贪婪指数"] = _step_score(fg, [20.5, 30.5, 69.5, 79.5],
                                      [1, 0.5, 0, -0.5, -1])
    # F&G 为整数: 用 .5 偏移阈值实现 ≤20→+1, 21-30→+0.5, 31-69→0, 70-79→-0.5, ≥80→-1

    # 交易所净流(7d) (2026-06 入桶)   [calc_exchange_netflow_7d 阈值]
    # 7日净流入合计/存量 %, 2018+ 分位数阈值 (-0.8/-0.4/+0.45/+1.0), 流入为正→看空
    net7_pct = ((cm["flow_in_ex"] - cm["flow_out_ex"]).rolling(7).sum()
                / cm["sply_ex"] * 100)
    out["交易所净流(7d)"] = _step_score(net7_pct, [-0.8, -0.4, 0.45, 1.0],
                                        [1, 0.5, 0, -0.5, -1])

    return out
