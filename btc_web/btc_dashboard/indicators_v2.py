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

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "Accept": "application/json"}


def calc_mvrv_z() -> IndicatorResult:
    """
    MVRV Z-Score — 市值与已实现市值的偏离度（链上周期估值核心指标）
    历史规律: < 0 周期底部带, > 7 历史顶部带（周期振幅递减, 现代阈值压缩至 ~5）
    """
    z = None
    date_str = ""
    try:
        r = requests.get("https://bitcoin-data.com/v1/mvrv-zscore/last",
                         timeout=12, headers=_HEADERS)
        if r.status_code == 200:
            obj = r.json()
            z = float(obj.get("mvrvZscore"))
            date_str = obj.get("d", "")
    except Exception as e:
        print(f"⚠️ MVRV-Z (bitcoin-data.com) 失败: {e}")

    if z is None:
        return IndicatorResult(
            name="MVRV-Z", value=float('nan'), score=0, color="⚪",
            status="数据源暂不可用", priority="P0",
            url="https://www.bitcoinmagazinepro.com/charts/mvrv-zscore/",
            description="MVRV Z-Score 衡量市值相对已实现市值（全网持仓成本）的标准差偏离。",
            method="数据源 bitcoin-data.com 暂不可用。")

    if z < 0:
        score, color, label = 1, "🟢", "周期底部带 — 历史级低估"
    elif z < 1:
        score, color, label = 0.5, "🟢", "低估区"
    elif z < 3:
        score, color, label = 0, "🟡", "中性区"
    elif z < 5:
        score, color, label = -0.5, "🟠", "偏热区"
    else:
        score, color, label = -1, "🔴", "周期顶部带 — 历史级高估"

    return IndicatorResult(
        name="MVRV-Z", value=round(z, 3), score=score, color=color,
        status=f"{label} (Z={z:.2f})" + (f" | {date_str}" if date_str else ""),
        priority="P0",
        url="https://www.bitcoinmagazinepro.com/charts/mvrv-zscore/",
        description="MVRV Z-Score 是链上周期估值的黄金标准：市值偏离全网持仓成本的标准差数。历史上 <0 为周期底部带，>5（早期 >7）为顶部带。",
        method="Z = (市值 - 已实现市值) / 市值标准差。数据源: bitcoin-data.com 免费链上 API。")


def calc_stablecoin_growth() -> IndicatorResult:
    """
    稳定币供应 30 日增速 — 加密市场的"场内弹药"与流动性代理
    增速为正 = 新资金流入加密生态, 为负 = 资金撤离
    """
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
        return IndicatorResult(
            name="稳定币增速", value=float('nan'), score=0, color="⚪",
            status="数据源暂不可用", priority="P1",
            url="https://defillama.com/stablecoins",
            description="稳定币总市值 30 日增速，衡量场内资金弹药变化。",
            method="数据源 DefiLlama 暂不可用。")

    series.sort(key=lambda x: x[0])
    latest = series[-1][1]
    prev_30d = series[-31][1]
    growth_pct = (latest / prev_30d - 1) * 100 if prev_30d > 0 else 0.0
    total_b = latest / 1e9

    if growth_pct > 2.5:
        score, color, label = 1, "🟢", "弹药快速涌入"
    elif growth_pct > 1.0:
        score, color, label = 0.5, "🟢", "温和流入"
    elif growth_pct > -1.0:
        score, color, label = 0, "🟡", "基本持平"
    elif growth_pct > -2.5:
        score, color, label = -0.5, "🟠", "资金流出"
    else:
        score, color, label = -1, "🔴", "弹药快速撤离"

    return IndicatorResult(
        name="稳定币增速", value=round(growth_pct, 2), score=score, color=color,
        status=f"{label} (30日 {growth_pct:+.1f}% | 总量 ${total_b:.0f}B)",
        priority="P1",
        url="https://defillama.com/stablecoins",
        description="稳定币（USDT/USDC 等）总市值是加密市场的场内购买力。30 日增速为正说明新资金进场，是 BTC 中期需求的领先代理。",
        method="DefiLlama 全稳定币 peggedUSD 流通量，最新值 vs 30 天前的变化百分比。>+2.5% 强流入，<-2.5% 强流出。")


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

    if sum_5d > 1000:
        score, color, label = 1, "🟢", "强劲净流入"
    elif sum_5d > 200:
        score, color, label = 0.5, "🟢", "温和净流入"
    elif sum_5d > -200:
        score, color, label = 0, "🟡", "基本平衡"
    elif sum_5d > -1000:
        score, color, label = -0.5, "🟠", "持续净流出"
    else:
        score, color, label = -1, "🔴", "大幅净流出"

    return IndicatorResult(
        name="ETF净流入", value=round(sum_5d, 1), score=score, color=color,
        status=f"{label} (近5日 {sum_5d:+,.0f}M$) | {latest_str}",
        priority="P0",
        url="https://www.coinglass.com/bitcoin-etf",
        description="美国现货 BTC ETF 日度净流入（真实申赎数据，非成交量）。机构边际买卖力量的最直接观测窗口，对称打分：净流出同样给负分。",
        method=f"近 5 个交易日净流入合计（百万美元）。>+$1B 强流入，<-$1B 强流出。数据源: {data.get('source','SoSoValue')}。")


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
