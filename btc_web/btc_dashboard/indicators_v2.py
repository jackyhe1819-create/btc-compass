#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.indicators_v2
===========================
BTC Compass 新增正交因子指标：
- MVRV Z-Score（链上估值黄金标准, bitcoin-data.com 免费 API）
- 稳定币供应增速（场内弹药, DefiLlama）
- 期货年化基差（杠杆温度计, OKX 季度合约）
- 趋势过滤器（200DMA 斜率 + 价格 vs 20W EMA, 纯本地计算）
- 资金费率 7 日均值（替代单次快照, 降噪）
- ETF 净流入（真实净流入对称打分, 替代成交量伪指标）
- 恐惧贪婪 v2（仅极值计分, 中段中性）
"""

import numpy as np
import pandas as pd
import requests
from datetime import datetime, timezone

from .core import IndicatorResult
from .scoring import _percentile_score, _percentile_note, PERCENTILE_WINDOW

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "Accept": "application/json"}


# ============================================================
# 链上周期指标的分位数评分 (2026-07 对抗性审查修复)
#
# 问题: MVRV-Z / NUPL / Puell 的顶部绝对阈值 (5 / 0.75 / 4) 按早期周期
# 振幅校准, 振幅衰减后本轮永远打不出 -1 (本轮峰值 Z≈3.5, NUPL≈0.6),
# 顶部识别系统性偏钝 — 与趋势伸展桶已修复的是同一个病。
#
# 修复: 与趋势桶同口径的 4 年滚动分位数为主, 绝对阈值做"极值保底"
# (取两者更极端者), 保留 Z<0 跌破全网成本这类结构性语义不受窗口限制。
# 历史序列来自 CoinMetrics (history._fetch_cm_history, 6h 缓存),
# 不可用时自动回退纯绝对阈值打分。
# ============================================================

def _cm_chip_series(key: str):
    """CoinMetrics 派生链上序列 (mvrv_z / nupl / puell), 近4年。失败 None。"""
    try:
        from .history import _fetch_cm_history
        hist = _fetch_cm_history(PERCENTILE_WINDOW)
        if not hist or not hist.get(key):
            return None
        s = pd.Series(hist[key], dtype=float).dropna()
        return s if len(s) >= PERCENTILE_WINDOW // 4 else None
    except Exception as e:
        print(f"⚠️ CM 链上历史 [{key}] 获取失败: {e}")
        return None


def _cm_chip_last(key: str):
    """
    CM 派生序列最后一个有效值及其日期 → (value, 'MM-DD') 或 (None, "")。
    CM 社区 API 末行的市值/估值列常为空 (实际取到 T-2), 展示取值日期
    避免读者误以为是 T-1 (2026-07 复查修复)。
    """
    try:
        from .history import _fetch_cm_history
        hist = _fetch_cm_history(PERCENTILE_WINDOW)
        if not hist or not hist.get(key):
            return None, ""
        vals = hist[key]
        dates = hist.get("dates") or []
        for i in range(len(vals) - 1, -1, -1):
            v = vals[i]
            if v is not None and v == v:
                d = str(dates[i])[5:] if i < len(dates) else ""
                return float(v), d
        return None, ""
    except Exception as e:
        print(f"⚠️ CM 链上末值 [{key}] 获取失败: {e}")
        return None, ""


def _pct_floor_score(key: str, abs_score: float):
    """
    分位数评分 + 绝对阈值极值保底。
    返回 (score, note) 或 None (历史不可用 → 调用方保持纯绝对阈值)。
    分位数基于 CM 序列自身末值计算, 与窗口同源, 避免 bd/CM 两家
    std 口径不一致造成的错位。
    """
    s = _cm_chip_series(key)
    if s is None:
        return None
    pct_score, n_used = _percentile_score(s)
    if np.isnan(pct_score):
        return None
    pct = (0.5 - pct_score / 2) * 100
    note = _percentile_note(pct, n_used)
    if abs(abs_score) > abs(pct_score):
        return float(abs_score), f"{note} · 绝对阈值保底"
    return float(pct_score), note


def _score_color(score: float) -> str:
    """与 apply_percentile_overrides 同一套颜色映射。"""
    if score >= 0.3:
        return "🟢"
    if score <= -0.6:
        return "🔴"
    if score <= -0.3:
        return "🟠"
    return "🟡"


def calc_mvrv_z() -> IndicatorResult:
    """
    MVRV Z-Score — 市值与已实现市值的偏离度（链上周期估值核心指标）
    历史规律: < 0 周期底部带, > 7 历史顶部带（周期振幅递减, 现代阈值压缩至 ~5）
    """
    # 主源 CoinMetrics 派生序列 (免限流)。bd 退居备源: Render 共享出口 IP 的
    # bitcoin-data 匿名配额 (10/h) 常被其它租户耗尽, 非独占端点退出竞争,
    # 把配额留给 bd 独占且无替代的 STH成本线/SOPR (2026-07 审计遗留批修复)。
    # 口径注意: CM 派生 Z 用 rolling(730)σ, 与 bd/Glassnode 的全历史扩张σ
    # 有量级差 (今日 0.54 vs 0.34), 绝对阈值 0/1/3/5 按扩张σ标定 — 但评分
    # 以 4 年分位数为主 (分位对各自序列自洽), 绝对腿仅极值保底, 走 CM 时标注口径。
    z, z_date = _cm_chip_last("mvrv_z")
    z_note = f"CM 730σ口径·{z_date}" if z is not None and z_date else \
             ("CM 730σ口径" if z is not None else "")
    if z is None:
        z = _bd_last("mvrv-zscore", "mvrvZscore")

    if z is None:
        return IndicatorResult(
            name="MVRV-Z", value=float('nan'), score=0, color="⚪",
            status="数据源暂不可用", priority="P0",
            url="https://www.bitcoinmagazinepro.com/charts/mvrv-zscore/",
            description="MVRV Z-Score 衡量市值相对已实现市值（全网持仓成本）的标准差偏离。",
            method="数据源 bitcoin-data.com / CoinMetrics 均暂不可用。")

    if z < 0:
        score, label = 1, "周期底部带 — 历史级低估"
    elif z < 1:
        score, label = 0.5, "低估区"
    elif z < 3:
        score, label = 0, "中性区"
    elif z < 5:
        score, label = -0.5, "偏热区"
    else:
        score, label = -1, "周期顶部带 — 历史级高估"

    # 4年分位数为主, 绝对阈值极值保底 (修复周期振幅衰减导致顶部打分偏钝)
    note = ""
    enhanced = _pct_floor_score("mvrv_z", score)
    if enhanced is not None:
        score, note = enhanced
    if z_note:
        note = f"{note} | {z_note}" if note else z_note

    return IndicatorResult(
        name="MVRV-Z", value=round(z, 3), score=score, color=_score_color(score),
        status=f"{label} (Z={z:.2f})" + (f" | {note}" if note else ""),
        priority="P0",
        url="https://www.bitcoinmagazinepro.com/charts/mvrv-zscore/",
        description="MVRV Z-Score 是链上周期估值的黄金标准：市值偏离全网持仓成本的标准差数。历史上 <0 为周期底部带，>5（早期 >7）为顶部带；因周期振幅递减，评分以 4 年分位数为主、绝对阈值做极值保底。",
        method="Z = (市值 - 已实现市值) / 市值标准差。评分 = 4年滚动分位数 (CoinMetrics 序列)，与绝对阈值 (0/1/3/5) 取更极端者；历史序列不可用时回退纯绝对阈值。数据源: bitcoin-data.com + CoinMetrics，6h 缓存。")


def calc_stablecoin_growth() -> IndicatorResult:
    """
    稳定币供应 30 日增速 — 加密市场的"场内弹药"与流动性代理
    增速为正 = 新资金流入加密生态, 为负 = 资金撤离
    """
    def _unavailable(method: str) -> IndicatorResult:
        return IndicatorResult(
            name="稳定币增速", value=float('nan'), score=0, color="⚪",
            status="数据源暂不可用", priority="P1",
            url="https://defillama.com/stablecoins",
            description="稳定币总市值 30 日增速，衡量场内资金弹药变化。",
            method=method)

    series = None
    try:
        r = requests.get("https://stablecoins.llama.fi/stablecoincharts/all",
                         timeout=15, headers=_HEADERS)
        if r.status_code == 200:
            data = r.json()
            # 取 peggedUSD 总流通量日度序列
            series = [(int(p["date"]), float(p["totalCirculating"].get("peggedUSD", 0)))
                      for p in data if p.get("totalCirculating")]
    except Exception as e:
        print(f"⚠️ DefiLlama 稳定币数据失败: {e}")

    if not series or len(series) < 35:
        return _unavailable("数据源 DefiLlama 暂不可用。")

    series.sort(key=lambda x: x[0])
    latest = series[-1][1]
    prev_30d = series[-31][1]
    # 上游半写入/结构变化会把 peggedUSD 缺失字段静默填 0: 若最新或 30 日前的锚点
    # <=0, 会伪造 -100% 增速(latest=0)或伪装"常态区间"(prev=0, 旧 else 0.0)的假信号
    # 直接计入资金流桶. 任一锚点非正 → 如实缺席(转灰), 与"数据坏→退出评分"不变量一致.
    if not (latest > 0 and prev_30d > 0):
        return _unavailable("DefiLlama 数据结构异常 (锚点非正值), 稳定币增速暂不可用。")
    growth_pct = (latest / prev_30d - 1) * 100
    total_b = latest / 1e9

    # 2026-07 对抗性审查重标定: 稳定币市值有结构性增长趋势 (2021+ 30日增速中位
    # +2.0%), 旧阈值以 0% 为中性锚 → 60% 天数常驻看多票。新阈值 = 2021+ 分布
    # 10/25/75/90 分位取整 (-2.0/-0.5/+5.5/+12), "常态增长"归 0 分, 只有显著
    # 偏离常态才计分 (与交易所余额 v2 同法)。
    if growth_pct > 12.0:
        score, color, label = 1, "🟢", "弹药涌入远超常态"
    elif growth_pct > 5.5:
        score, color, label = 0.5, "🟢", "流入偏强"
    elif growth_pct > -0.5:
        score, color, label = 0, "🟡", "常态区间"
    elif growth_pct > -2.0:
        score, color, label = -0.5, "🟠", "罕见收缩"
    else:
        score, color, label = -1, "🔴", "弹药快速撤离 (历史前10%)"

    return IndicatorResult(
        name="稳定币增速", value=round(growth_pct, 2), score=score, color=color,
        status=f"{label} (30日 {growth_pct:+.1f}% | 总量 ${total_b:.0f}B)",
        priority="P1",
        url="https://defillama.com/stablecoins",
        description="稳定币（USDT/USDC 等）总市值是加密市场的场内购买力。30 日增速相对常态（结构性 +2%/30d）的偏离，是 BTC 中期需求的领先代理。",
        method="DefiLlama 全稳定币 peggedUSD 流通量 30 日变化。阈值 = 2021+ 分布 "
               "10/25/75/90 分位 (-2.0/-0.5/+5.5/+12%), 常态增长不计分 (2026-07 重标定)。")


def calc_futures_basis() -> IndicatorResult:
    """
    期货年化基差 — 比资金费率更稳定的杠杆/情绪温度计
    选取 60~120 天后到期的 OKX 季度合约, 计算相对现货指数的年化溢价
    """
    basis_ann = None
    inst_used = ""
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/public/instruments",
            params={"instType": "FUTURES", "uly": "BTC-USD"},
            timeout=12, headers=_HEADERS)
        instruments = (r.json() or {}).get("data", []) if r.status_code == 200 else []

        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        # 选 60~120 天后到期的合约（季度档）, 不足则放宽到 30~180
        for lo, hi in ((60, 120), (30, 180)):
            cands = [i for i in instruments
                     if lo <= (float(i["expTime"]) - now_ms) / 86400000 <= hi]
            if cands:
                break
        if cands:
            cands.sort(key=lambda i: float(i["expTime"]))
            inst = cands[-1]
            inst_used = inst["instId"]
            days_to_exp = (float(inst["expTime"]) - now_ms) / 86400000

            fut_r = requests.get(
                "https://www.okx.com/api/v5/market/ticker",
                params={"instId": inst_used}, timeout=12, headers=_HEADERS)
            idx_r = requests.get(
                "https://www.okx.com/api/v5/market/index-tickers",
                params={"instId": "BTC-USD"}, timeout=12, headers=_HEADERS)
            fut_px = float(fut_r.json()["data"][0]["last"])
            idx_px = float(idx_r.json()["data"][0]["idxPx"])
            if idx_px > 0 and days_to_exp > 1:
                basis_ann = (fut_px / idx_px - 1) * (365 / days_to_exp) * 100
    except Exception as e:
        print(f"⚠️ OKX 期货基差失败: {e}")

    if basis_ann is None:
        return IndicatorResult(
            name="期货基差", value=float('nan'), score=0, color="⚪",
            status="数据源暂不可用", priority="P1",
            url="https://www.okx.com/trade-futures/btc-usd",
            description="季度期货相对现货的年化溢价，衡量市场杠杆与多头拥挤度。",
            method="数据源 OKX 暂不可用。")

    if basis_ann < 0:
        score, color, label = 1, "🟢", "贴水 — 极度悲观(罕见底部信号)"
    elif basis_ann < 5:
        score, color, label = 0.5, "🟢", "低溢价 — 杠杆出清"
    elif basis_ann < 10:
        score, color, label = 0, "🟡", "正常区间"
    elif basis_ann < 20:
        score, color, label = -0.5, "🟠", "溢价偏高 — 多头拥挤"
    else:
        score, color, label = -1, "🔴", "狂热溢价 — 杠杆泡沫"

    return IndicatorResult(
        name="期货基差", value=round(basis_ann, 2), score=score, color=color,
        status=f"{label} (年化 {basis_ann:+.1f}%)",
        priority="P1",
        url="https://www.okx.com/trade-futures/btc-usd",
        description="季度期货年化基差是机构常用的杠杆温度计：高溢价 = 多头拥挤抢筹（危险），贴水 = 极度悲观（历史性买点）。比单次资金费率快照更稳定。",
        method=f"OKX 季度合约 {inst_used} 价格 vs BTC-USD 现货指数，按剩余到期日年化。<0% 贴水看多，>20% 狂热看空。")


def calc_trend_filter(df: pd.DataFrame) -> IndicatorResult:
    """
    趋势过滤器 — 区分"便宜且企稳"与"便宜但还在跌"
    组件: ① 价格 vs 20周 EMA(140日)  ② 200DMA 的 30 日斜率
    这是体系中唯一的趋势跟随因子, 用于约束均值回归类指标的接刀风险
    """
    if df is None or len(df) < 230:
        return IndicatorResult(
            name="趋势过滤器", value=float('nan'), score=0, color="⚪",
            status="数据不足", priority="P0",
            description="价格相对 20 周 EMA 与 200 日均线斜率的趋势确认。", method="")

    price = df['price']
    ema20w = price.ewm(span=140, adjust=False).mean()
    ma200 = price.rolling(200).mean()

    cur = price.iloc[-1]
    cur_ema = ema20w.iloc[-1]
    slope_pct = (ma200.iloc[-1] / ma200.iloc[-31] - 1) * 100  # 200DMA 30日变化%

    above_ema = cur > cur_ema
    slope_up = slope_pct > 0.5
    slope_down = slope_pct < -0.5

    if above_ema and slope_up:
        score, color, label = 1, "🟢", "上升趋势确认"
    elif above_ema and not slope_down:
        score, color, label = 0.5, "🟢", "趋势转暖"
    elif not above_ema and slope_down:
        score, color, label = -1, "🔴", "下降趋势确认"
    elif not above_ema:
        score, color, label = -0.5, "🟠", "趋势走弱"
    else:
        score, color, label = 0, "🟡", "趋势不明"

    dev_pct = (cur / cur_ema - 1) * 100
    return IndicatorResult(
        name="趋势过滤器", value=round(dev_pct, 2), score=score, color=color,
        status=f"{label} (vs 20W EMA {dev_pct:+.1f}% | 200DMA斜率 {slope_pct:+.1f}%/30d)",
        priority="P0",
        url="https://www.tradingview.com/chart/?symbol=BINANCE:BTCUSDT",
        description="体系中唯一的趋势跟随因子：估值类指标说'便宜'时，趋势过滤器回答'企稳了吗'。避免在下跌中段过早抄底、在主升段过早离场。",
        method="① 价格 vs 20周EMA（牛熊分界线）② 200日均线的30日斜率。双确认 = ±1，单确认 = ±0.5。")


def calc_funding_rate_7d() -> IndicatorResult:
    """
    资金费率 7 日均值 — 替代单次 8 小时快照, 大幅降噪
    OKX 每 8h 一次, 21 条 = 7 天
    """
    avg_rate = None
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/public/funding-rate-history",
            params={"instId": "BTC-USDT-SWAP", "limit": 21},
            timeout=12, headers=_HEADERS)
        if r.status_code == 200:
            rows = (r.json() or {}).get("data", [])
            rates = [float(x["fundingRate"]) for x in rows]
            if len(rates) >= 9:  # 至少3天
                avg_rate = sum(rates) / len(rates) * 100  # 转百分比
    except Exception as e:
        print(f"⚠️ OKX 资金费率历史失败: {e}")

    if avg_rate is None:
        return IndicatorResult(
            name="资金费率(7d)", value=float('nan'), score=0, color="⚪",
            status="数据源暂不可用", priority="P1",
            url="https://www.coinglass.com/FundingRate",
            description="永续合约资金费率的 7 日均值。", method="数据源 OKX 暂不可用。")

    if avg_rate > 0.05:
        score, color, label = -1, "🔴", "持续过热 — 多头拥挤"
    elif avg_rate > 0.02:
        score, color, label = -0.5, "🟠", "偏多"
    elif avg_rate > -0.02:
        score, color, label = 0, "🟡", "中性"
    elif avg_rate > -0.05:
        score, color, label = 0.5, "🟢", "偏空 — 空头付费"
    else:
        score, color, label = 1, "🟢", "持续恐慌 — 空头拥挤"

    return IndicatorResult(
        name="资金费率(7d)", value=round(avg_rate, 4), score=score, color=color,
        status=f"{label} (7日均 {avg_rate:+.4f}%/8h)",
        priority="P1",
        url="https://www.coinglass.com/FundingRate",
        description="资金费率 7 日均值消除了单次快照的噪音，反映持续性的多空付费方向。持续高费率 = 多头拥挤（逆向看空），持续负费率 = 空头拥挤（逆向看多）。",
        method="OKX BTC-USDT-SWAP 最近 21 次（7 天）资金费率的算术平均。>+0.05%/8h 过热，<-0.05%/8h 恐慌。")


def calc_etf_net_flow() -> IndicatorResult:
    """
    ETF 净流入 — 用真实日度净流入数据对称打分
    (替代原版用"成交量"且永不给负分的伪资金流指标)
    """
    data = None
    try:
        from .etf_flow import fetch_etf_flow_history
        data = fetch_etf_flow_history(limit=15)
    except Exception as e:
        print(f"⚠️ ETF 净流入获取失败: {e}")

    if not data or data.get("sum_5d") is None:
        return IndicatorResult(
            name="ETF净流入", value=float('nan'), score=0, color="⚪",
            status="数据源暂不可用", priority="P0",
            url="https://www.coinglass.com/bitcoin-etf",
            description="美国现货 BTC ETF 的真实日度净流入。", method="数据源暂不可用。")

    sum_5d = float(data["sum_5d"])  # 百万美元
    latest = data.get("latest") or {}
    latest_str = f"最新 {latest.get('date','')} {latest.get('total',0):+,.0f}M" if latest else ""

    # 2026-07 对抗性审查重标定: 旧阈值 ±200M/±1000M 按 2024 年初量纲设定, 在
    # 2025-26 年的 ETF 流量下"中性带"只覆盖 14% 天数、"强"档触发 39%——极值不再
    # 稀有。新阈值 = 近 300 交易日 5 日合计分布 10/25/75/90 分位取整
    # (-1300/-700/+900/+1700M, 不对称反映结构性净流入偏置)。
    if sum_5d > 1700:
        score, color, label = 1, "🟢", "强劲净流入 (前10%)"
    elif sum_5d > 900:
        score, color, label = 0.5, "🟢", "偏强净流入"
    elif sum_5d > -700:
        score, color, label = 0, "🟡", "常态区间"
    elif sum_5d > -1300:
        score, color, label = -0.5, "🟠", "持续净流出"
    else:
        score, color, label = -1, "🔴", "大幅净流出 (前10%)"

    return IndicatorResult(
        name="ETF净流入", value=round(sum_5d, 1), score=score, color=color,
        status=f"{label} (近5日 {sum_5d:+,.0f}M$) | {latest_str}",
        priority="P0",
        url="https://www.coinglass.com/bitcoin-etf",
        description="美国现货 BTC ETF 日度净流入（真实申赎数据，非成交量）。机构边际买卖力量的最直接观测窗口，对称打分：净流出同样给负分。",
        method=f"近 5 个交易日净流入合计（百万美元）。阈值 = 近300交易日分布 10/25/75/90 分位 "
               f"(-1300/-700/+900/+1700M, 2026-07 重标定)。数据源: {data.get('source','SoSoValue')}。")


def calc_fear_greed_v2() -> IndicatorResult:
    """
    恐惧贪婪指数 v2 — 仅极值计分
    逆向情绪指标只在极端区有效; 牛市主升段 F&G 会在 55~75 停留数月,
    原版在该区间给 -0.5 会造成系统性拖累
    """
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=10)
        if r.status_code == 200:
            value = int(r.json()["data"][0]["value"])

            if value <= 20:
                score, color, label = 1, "🟢", "极度恐惧 — 逆向买点"
            elif value <= 30:
                score, color, label = 0.5, "🟢", "恐惧 — 偏买入"
            elif value < 70:
                score, color, label = 0, "🟡", "常态区间(不计分)"
            elif value < 80:
                score, color, label = -0.5, "🟠", "贪婪 — 谨慎"
            else:
                score, color, label = -1, "🔴", "极度贪婪 — 逆向卖点"

            return IndicatorResult(
                name="恐惧贪婪指数", value=float(value), score=score, color=color,
                status=f"{label} ({value})", priority="P1",
                url="https://alternative.me/crypto/fear-and-greed-index/",
                description="市场情绪逆向指标。v2 改进：仅在极值区计分（≤30 / ≥70），常态区间不产生信号——逆向指标在中段没有信息量。",
                method="alternative.me 恐惧贪婪指数。≤20 极度恐惧 +1，≥80 极度贪婪 -1，30~70 中性不计分。")
    except Exception as e:
        print(f"⚠️ Fear & Greed API 失败: {e}")

    return IndicatorResult(
        name="恐惧贪婪指数", value=float('nan'), score=0, color="⚪",
        status="API 暂不可用", priority="P1",
        url="https://alternative.me/crypto/fear-and-greed-index/")


# ============================================================
# 链上慢变量 TTL 缓存
# bitcoin-data.com 免费档有速率限制, 且链上指标皆为日更 —
# 不应跟随 5 分钟仪表盘刷新重复请求, 统一缓存 6 小时
# ============================================================

import threading as _threading
import time as _time

_ONCHAIN_TTL = 6 * 3600
_ONCHAIN_FAIL_TTL = 1800   # 失败负缓存 30 分钟, 避免限流期间连续轰炸 (bitcoin-data.com 匿名限 10 次/小时)
_onchain_cache: dict = {}
_onchain_lock = _threading.Lock()


def _cached_onchain(key: str, fetch_fn):
    """6 小时 TTL 的进程内缓存; 失败负缓存 30 分钟; 有旧值时失败回退旧值（哪怕已过期）"""
    now = _time.time()
    with _onchain_lock:
        hit = _onchain_cache.get(key)
        if hit:
            age = now - hit[0]
            if hit[1] is not None and age < _ONCHAIN_TTL:
                return hit[1]
            if hit[1] is None and age < _ONCHAIN_FAIL_TTL:
                return None
    try:
        val = fetch_fn()
    except Exception as e:
        print(f"⚠️ onchain [{key}] 获取失败: {e}")
        val = None
    with _onchain_lock:
        if val is not None:
            _onchain_cache[key] = (now, val)
            return val
        hit = _onchain_cache.get(key)
        if hit and hit[1] is not None:
            return hit[1]  # 保留旧值, 不覆盖
        _onchain_cache[key] = (now, None)  # 负缓存
        return None


def _bd_last(endpoint: str, field: str):
    """bitcoin-data.com /v1/<endpoint>/last → float(field)"""
    def _fetch():
        r = requests.get(f"https://bitcoin-data.com/v1/{endpoint}/last",
                         timeout=12, headers=_HEADERS)
        if r.status_code != 200:
            return None
        return float(r.json().get(field))
    return _cached_onchain(endpoint, _fetch)


# ============================================================
# 第一梯队链上指标 (2026-06 调研后纳入)
# ============================================================

def calc_sth_realized_price(current_price: float) -> IndicatorResult:
    """
    STH 成本线 — 短期持有者已实现价格 (155 天内移动过的币的平均成本)
    公认的牛熊分界中线; 价格/STH成本 即 STH-MVRV:
    < 0.8 历史投降带, > 1.35 STH 深度获利(过热)
    """
    sth_rp = _bd_last("sth-realized-price", "sthRealizedPrice")
    if sth_rp is None or not current_price or sth_rp <= 0:
        return IndicatorResult(
            name="STH成本线", value=float('nan'), score=0, color="⚪",
            status="数据源暂不可用", priority="P0",
            url="https://charts.bitbo.io/sth-realized-price/",
            description="短期持有者平均持仓成本，牛熊分界中线。", method="数据源暂不可用。")

    ratio = current_price / sth_rp
    if ratio < 0.80:
        score, color, label = 1, "🟢", "深度超卖 — 历史投降带"
    elif ratio < 0.95:
        score, color, label = 0.5, "🟢", "压在 STH 成本下方 — 抛压衰竭区"
    elif ratio < 1.15:
        score, color, label = 0, "🟡", "成本线争夺区"
    elif ratio < 1.35:
        score, color, label = -0.5, "🟠", "STH 获利偏厚 — 回调风险"
    else:
        score, color, label = -1, "🔴", "STH 深度获利 — 周期过热"

    return IndicatorResult(
        name="STH成本线", value=round(ratio, 3), score=score, color=color,
        status=f"{label} (价格/成本 {ratio:.2f}x | 成本 ${sth_rp:,.0f})",
        priority="P0",
        url="https://charts.bitbo.io/sth-realized-price/",
        description="短期持有者(155天内活跃筹码)的平均成本线。价格在其上方 = 短线筹码获利的牛市格局，下方 = 熊市格局；极端偏离则反向（超卖/过热）。",
        method="STH-MVRV = 现价 / STH已实现价格。<0.8 投降带 +1，>1.35 过热 -1。数据源: bitcoin-data.com，6h 缓存。")


def calc_nupl() -> IndicatorResult:
    """
    NUPL — 全网未实现盈亏占市值比例
    经典周期分区: <0 投降, 0~0.25 希望/恐惧, 0.25~0.5 乐观, 0.5~0.75 信仰, >0.75 兴奋
    """
    # 主源 CoinMetrics (NUPL≡1−1/MVRV 恒等式派生, 与 bd 相关 0.9999);
    # bd 退居备源, 配额让给独占的 STH/SOPR (2026-07 审计遗留批)
    nupl, cm_date = _cm_chip_last("nupl")
    cm_note = f"CM·{cm_date}" if nupl is not None and cm_date else ""
    if nupl is None:
        nupl = _bd_last("nupl", "nupl")

    if nupl is None:
        return IndicatorResult(
            name="NUPL", value=float('nan'), score=0, color="⚪",
            status="数据源暂不可用", priority="P0",
            url="https://charts.bgeometrics.com/nupl.html",
            description="全网未实现盈亏 / 市值，周期情绪分区。", method="数据源暂不可用。")

    if nupl < 0:
        score, label = 1, "投降区 — 全网浮亏"
    elif nupl < 0.25:
        score, label = 0.5, "希望/恐惧区"
    elif nupl < 0.5:
        score, label = 0, "乐观区"
    elif nupl < 0.75:
        score, label = -0.5, "信仰/贪婪区"
    else:
        score, label = -1, "兴奋区 — 周期顶部带"

    note = ""
    enhanced = _pct_floor_score("nupl", score)
    if enhanced is not None:
        score, note = enhanced
    if cm_note:
        note = f"{note} | {cm_note}" if note else cm_note

    return IndicatorResult(
        name="NUPL", value=round(nupl, 4), score=score, color=_score_color(score),
        status=f"{label} ({nupl:.3f})" + (f" | {note}" if note else ""),
        priority="P0",
        url="https://charts.bgeometrics.com/nupl.html",
        description="Net Unrealized Profit/Loss：全网持仓的未实现盈亏占市值比例，比 MVRV-Z 更直观的周期情绪分区。历史大底都出现在 <0 的投降区，大顶在 >0.75 的兴奋区；因周期振幅递减，评分以 4 年分位数为主、绝对阈值做极值保底。",
        method="NUPL = (市值 - 已实现市值) / 市值。评分 = 4年滚动分位数 (CoinMetrics 序列)，与绝对阈值 (0/0.25/0.5/0.75) 取更极端者；历史不可用时回退纯绝对阈值。数据源: bitcoin-data.com + CoinMetrics，6h 缓存。")


def calc_sopr() -> IndicatorResult:
    """
    SOPR — 已花费产出利润率 (当日链上卖出筹码的平均盈亏率)
    < 1 亏损抛售(投降), > 1 获利了结; 战术级短窗口信号
    """
    sopr = _bd_last("sopr", "sopr")
    if sopr is None:
        return IndicatorResult(
            name="SOPR", value=float('nan'), score=0, color="⚪",
            status="数据源暂不可用", priority="P1",
            url="https://charts.bgeometrics.com/sopr.html",
            description="链上卖出筹码的平均盈亏率。", method="数据源暂不可用。")

    if sopr < 0.97:
        score, color, label = 1, "🟢", "重度亏损抛售 — 投降信号"
    elif sopr < 0.995:
        score, color, label = 0.5, "🟢", "亏损抛售中"
    elif sopr < 1.02:
        score, color, label = 0, "🟡", "盈亏平衡附近"
    elif sopr < 1.05:
        score, color, label = -0.5, "🟠", "获利了结升温"
    else:
        score, color, label = -1, "🔴", "大额获利兑现"

    return IndicatorResult(
        name="SOPR", value=round(sopr, 4), score=score, color=color,
        status=f"{label} ({sopr:.4f})", priority="P1",
        url="https://charts.bgeometrics.com/sopr.html",
        description="Spent Output Profit Ratio：当日链上移动筹码的卖出价/成本价。持续 <1 = 割肉投降（逆向看多），>1.05 = 获利兑现压力。链上版的短线情绪计。",
        method="SOPR = 已花费 UTXO 的卖出价值/创建价值。<0.97 投降 +1，>1.05 兑现 -1。数据源: bitcoin-data.com，6h 缓存。")


def calc_puell_multiple() -> IndicatorResult:
    """
    Puell Multiple — 矿工日收入 / 其 365 日均值
    矿工经济学周期指标: <0.5 历史底部带(关机价附近), >4 顶部带
    """
    # 主源 CoinMetrics (发行USD/365日均值直算, 与 bd 相关 0.94);
    # bd 退居备源, 配额让给独占的 STH/SOPR (2026-07 审计遗留批)
    puell, cm_date = _cm_chip_last("puell")
    cm_note = f"CM·{cm_date}" if puell is not None and cm_date else ""
    if puell is None:
        puell = _bd_last("puell-multiple", "puellMultiple")

    if puell is None:
        return IndicatorResult(
            name="Puell Multiple", value=float('nan'), score=0, color="⚪",
            status="数据源暂不可用", priority="P0",
            url="https://charts.bgeometrics.com/puell_multiple.html",
            description="矿工收入相对一年均值的倍数。", method="数据源暂不可用。")

    if puell < 0.5:
        score, label = 1, "矿工收入极度压缩 — 历史底部带"
    elif puell < 0.8:
        score, label = 0.5, "矿工承压区"
    elif puell < 2.0:
        score, label = 0, "正常区间"
    elif puell < 4.0:
        score, label = -0.5, "矿工收入偏高"
    else:
        score, label = -1, "矿工暴利 — 周期顶部带"

    note = ""
    enhanced = _pct_floor_score("puell", score)
    if enhanced is not None:
        score, note = enhanced
    if cm_note:
        note = f"{note} | {cm_note}" if note else cm_note

    return IndicatorResult(
        name="Puell Multiple", value=round(puell, 3), score=score, color=_score_color(score),
        status=f"{label} ({puell:.2f})" + (f" | {note}" if note else ""),
        priority="P0",
        url="https://charts.bgeometrics.com/puell_multiple.html",
        description="矿工日收入(USD)相对其 365 日均值的倍数。矿工是结构性卖方，其收入周期与价格周期强相关：收入极度压缩时矿工投降出清（底部），暴利时派发（顶部）。覆盖'关机币价'视角；因周期振幅递减，评分以 4 年分位数为主、绝对阈值做极值保底。",
        method="Puell = 日发行价值 / 365日MA。评分 = 4年滚动分位数 (CoinMetrics 序列)，与绝对阈值 (0.5/0.8/2/4) 取更极端者；历史不可用时回退纯绝对阈值。数据源: bitcoin-data.com + CoinMetrics，6h 缓存。")


def calc_hash_ribbons() -> IndicatorResult:
    """
    Hash Ribbons — 算力 30/60 日均线交叉
    矿工投降(30下穿60)后的恢复上穿是历史胜率极高的买点信号
    """
    def _fetch():
        # 6m 窗 (~181 点): 60 日 SMA 暖机后有效区 ~121 点 > 45, "上穿 ≤45 天 → +1"
        # 规则才能完整落地 (3m 窗有效区仅 ~31 点, 32-45 天前的真实上穿结构性漏报,
        # 且与 backfill/backtest 口径分裂 — 2026-07 复查修复)
        r = requests.get("https://mempool.space/api/v1/mining/hashrate/6m",
                         timeout=12, headers=_HEADERS)
        if r.status_code != 200:
            return None
        arr = (r.json() or {}).get("hashrates", [])
        if len(arr) < 60:
            return None
        return [float(x["avgHashrate"]) for x in arr]

    rates = _cached_onchain("hashrate-6m", _fetch)
    if not rates:
        return IndicatorResult(
            name="Hash Ribbons", value=float('nan'), score=0, color="⚪",
            status="数据源暂不可用", priority="P1",
            url="https://charts.bitbo.io/hash-ribbons/",
            description="算力 30/60 日均线交叉信号。", method="数据源暂不可用。")

    s = pd.Series(rates)
    sma30 = s.rolling(30).mean()
    sma60 = s.rolling(60).mean()
    # 翻转扫描只在两条均线均有效的区间内进行 — 60 日窗未满的 NaN 段比较恒为
    # False, 会把数据有效性边界误判成状态翻转: 90 天窗下有效区仅 ~31 个点,
    # 伪翻转距今 ~31 天 ≤45, 曾把"算力扩张期"(+0.25) 误报成"矿工投降结束—
    # 经典买点"(+1) (2026-07 对抗性审查修复)
    valid = sma30.notna() & sma60.notna()
    above = (sma30 > sma60)[valid]
    if above.empty:
        return IndicatorResult(
            name="Hash Ribbons", value=float('nan'), score=0, color="⚪",
            status="数据不足 (需 ≥60 天算力)", priority="P1",
            url="https://charts.bitbo.io/hash-ribbons/",
            description="算力 30/60 日均线交叉信号。", method="数据不足。")

    cur_above = bool(above.iloc[-1])
    # 距最近一次状态翻转的天数 (仅在有效区间内; 窗口内无翻转 → None = 长期维持)
    flip_days = None
    for i in range(len(above) - 2, -1, -1):
        if bool(above.iloc[i]) != cur_above:
            flip_days = len(above) - 1 - i
            break

    spread_pct = (sma30.iloc[-1] / sma60.iloc[-1] - 1) * 100

    if cur_above and flip_days is not None and flip_days <= 45:
        score, color, label = 1, "🟢", f"矿工投降结束 — 恢复上穿 {flip_days} 天 (经典买点)"
    elif cur_above:
        score, color, label = 0.25, "🟢", "算力扩张期"
    else:
        score, color, label = -0.25, "🟠", "矿工投降中 (等待恢复上穿)"

    return IndicatorResult(
        name="Hash Ribbons", value=round(spread_pct, 2), score=score, color=color,
        status=f"{label} | 30D/60D 差 {spread_pct:+.1f}%", priority="P1",
        url="https://charts.bitbo.io/hash-ribbons/",
        description="算力 30 日均线 vs 60 日均线：下穿 = 矿工投降（出清弱矿工），重新上穿 = 投降结束，历史上是胜率极高的买点确认信号。本质是买点指标，无对称卖出信号。",
        method="mempool.space 180 天日度算力，SMA30 上穿 SMA60 且 45 天内 → +1；维持上方 +0.25；下方(投降中) -0.25。6h 缓存。")


# ============================================================
# 交易所余额 v2 — CoinMetrics 社区 API (2026-06 重构)
#
# 旧实现: mempool.space 轮询 11 个硬编码冷钱包 (~60万 BTC 覆盖),
#         与"上次快照"对比 ±0.5%/±2% 打分 → 94.5% 的天数打 0 分, 接近哑因子。
# 新实现: CoinMetrics 社区 API (免key, T-1 日滞后, 追踪全市场 ~265万 BTC),
#         30 日存量变化率按 2018+ 历史分位数打分 (回测 IC: 30d +0.065 / 90d +0.102)。
# 降级链: CoinMetrics → 旧版 mempool.space 快照对比。
# ============================================================

def _cm_exchange_series():
    """
    CoinMetrics 社区 API: 近 420 天交易所 BTC 存量与进出流量。
    返回 {"dates": [...], "sply": [...], "net": [...]} 或 None。6h 缓存。
    """
    def _fetch():
        from datetime import date, timedelta
        start = (date.today() - timedelta(days=420)).isoformat()
        r = requests.get(
            "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics",
            params={"assets": "btc",
                    "metrics": "SplyExNtv,FlowInExNtv,FlowOutExNtv",
                    "frequency": "1d", "start_time": start, "page_size": 500},
            timeout=20, headers=_HEADERS)
        if r.status_code != 200:
            return None
        rows = (r.json() or {}).get("data", [])
        dates, sply, net = [], [], []
        for it in rows:
            try:
                s = float(it["SplyExNtv"])
                fin = float(it.get("FlowInExNtv") or 0)
                fout = float(it.get("FlowOutExNtv") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            dates.append(it["time"][:10])
            sply.append(s)
            net.append(fin - fout)
        if len(sply) < 31:
            return None
        return {"dates": dates, "sply": sply, "net": net}

    return _cached_onchain("cm-exchange", _fetch)


def calc_exchange_balance_v2() -> IndicatorResult:
    """
    交易所余额 — 30 日存量变化率, 分位数校准打分。
    阈值 = 2018+ 全样本 Δ30d 分布的 10/25/75/90 分位 (backtest 2026-06 校准):
      ≤-2.1% 强流出 +1 | ≤-0.85% 流出 +0.5 | <+1.3% 中性 0 | <+2.9% 流入 -0.5 | ≥+2.9% 强流入 -1
    """
    data = _cm_exchange_series()

    if not data:
        # 降级: 旧版 mempool.space 冷钱包快照对比
        try:
            from .indicators_aux import calc_exchange_reserve
            legacy = calc_exchange_reserve()
            legacy.method = f"[降级备源] {legacy.method}"
            return legacy
        except Exception as e:
            print(f"⚠️ 交易所余额降级源也失败: {e}")
            return IndicatorResult(
                name="交易所余额", value=float('nan'), score=0, color="⚪",
                status="数据源暂不可用", priority="P2",
                url="https://studio.glassnode.com/metrics?a=BTC&m=distribution.BalanceExchanges",
                description="交易所 BTC 存量 30 日变化率。", method="主备数据源均不可用。")

    sply, net = data["sply"], data["net"]
    cur, prev30 = sply[-1], sply[-31]
    d30_pct = (cur / prev30 - 1) * 100
    net7_btc = sum(net[-7:])
    net7_pct = net7_btc / cur * 100

    if d30_pct <= -2.1:
        score, color, label = 1, "🟢", "强劲流出 — 筹码加速离场"
    elif d30_pct <= -0.85:
        score, color, label = 0.5, "🟢", "持续流出 — 自托管/吸筹"
    elif d30_pct < 1.3:
        score, color, label = 0, "🟡", "存量平稳"
    elif d30_pct < 2.9:
        score, color, label = -0.5, "🟠", "持续流入 — 潜在卖压"
    else:
        score, color, label = -1, "🔴", "大举流入 — 抛压预警"

    return IndicatorResult(
        name="交易所余额",
        value=round(d30_pct, 2),
        score=score,
        color=color,
        status=(f"{label} (30日 {d30_pct:+.2f}%) | 存量 {cur/1e6:.2f}M BTC"
                f" | 7日净流 {net7_btc:+,.0f} BTC ({net7_pct:+.2f}%)"),
        priority="P2",
        url="https://studio.glassnode.com/metrics?a=BTC&m=distribution.BalanceExchanges",
        description=("全市场交易所 BTC 存量的 30 日变化率（CoinMetrics 机构级聚合, ~265万 BTC, "
                     "T-1 日度更新）。存量下降 = 提币自托管/吸筹（看多），上升 = 充值待卖（看空）。"
                     "⚠️ 2026-07 已退出周期评分, 仅作展示: ETF 时代币迁往托管机构是单向结构趋势, "
                     "该读数退化为常亮看多灯, 且与 ETF净流入重复计数同一笔资金流。"),
        method=("CoinMetrics 社区 API SplyExNtv 30 日变化率, 按 2018+ 历史分布分位数打分 "
                "(10/25/75/90 分位 ≈ -2.1/-0.85/+1.3/+2.9%)。2026-07 留一对照回测后移出"
                "链上筹码桶 (2014-2023 有判别力, 2024+ ETF 体制下 IC 转负), 不再计入周期分。"
                "6h 缓存, 失败降级 mempool.space。"))


def calc_exchange_netflow_7d() -> IndicatorResult:
    """
    交易所净流(7d) — 战术级短窗口版本 (2026-06 起接入战术分"链上资金流"桶, 权重 15%)。
    回测 (2018-2026): 7d 净流/存量 在 7-14 天前瞻上 IC +0.10~+0.13,
    高于杠杆温度桶现有的资金费率因子。
    阈值 = 2018+ 7d净流/存量 分布的 10/25/75/90 分位。
    """
    data = _cm_exchange_series()
    if not data:
        return IndicatorResult(
            name="交易所净流(7d)", value=float('nan'), score=0, color="⚪",
            status="数据源暂不可用", priority="P1",
            url="https://studio.glassnode.com/metrics?a=BTC&m=distribution.BalanceExchanges",
            description="7 日交易所净流入占存量比例。", method="CoinMetrics 社区 API 暂不可用。")

    sply, net = data["sply"], data["net"]
    net7_pct = sum(net[-7:]) / sply[-1] * 100

    if net7_pct <= -0.8:
        score, color, label = 1, "🟢", "强劲净流出"
    elif net7_pct <= -0.4:
        score, color, label = 0.5, "🟢", "净流出"
    elif net7_pct < 0.45:
        score, color, label = 0, "🟡", "基本平衡"
    elif net7_pct < 1.0:
        score, color, label = -0.5, "🟠", "净流入"
    else:
        score, color, label = -1, "🔴", "大幅净流入"

    return IndicatorResult(
        name="交易所净流(7d)", value=round(net7_pct, 3), score=score, color=color,
        status=f"{label} (7日 {net7_pct:+.2f}% 存量)", priority="P1",
        url="https://studio.glassnode.com/metrics?a=BTC&m=distribution.BalanceExchanges",
        description="7 日交易所净流入占存量比例 — 短线供需信号。流出 = 买盘提币（看多）。",
        method="CoinMetrics 社区 API (FlowInExNtv-FlowOutExNtv) 7 日合计 / 存量, "
               "2018+ 分位数阈值 (-0.8/-0.4/+0.45/+1.0%)。6h 缓存。")
