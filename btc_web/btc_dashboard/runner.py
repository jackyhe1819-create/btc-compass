#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.runner
====================
评分汇总、sparkline 计算、并发执行所有指标、开发者动态 RSS。
"""

import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta, timezone
from typing import Tuple, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from .core import (
    IndicatorResult, DashboardResult,
    GENESIS_DATE, HALVING_DATES, NEXT_HALVING_ESTIMATE,
    POWER_LAW_INTERCEPT, POWER_LAW_SLOPE,
    AHR999_A, AHR999_B,
    fetch_realtime_btc_price, fetch_btc_data,
)
from .indicators_long import (
    calc_two_year_ma_multiplier, calc_200w_ma_heatmap, calc_golden_ratio_multiplier,
    calc_pi_cycle, calc_hashrate, calc_balanced_price,
    calc_halving_cycle, calc_ahr999, calc_power_law, calc_mayer_multiple,
)
from .indicators_short import calc_rsi, calc_macd, calc_bollinger_bands
from .indicators_aux import (
    calc_fear_greed_index, calc_funding_rate, calc_long_short_ratio,
    calc_btc_dominance, calc_etf_flow, calc_mnav, calc_company_holdings,
    calc_exchange_reserve, calc_max_pain,
)
from .indicators_v2 import (
    calc_mvrv_z, calc_stablecoin_growth, calc_futures_basis,
    calc_trend_filter, calc_funding_rate_7d, calc_etf_net_flow,
    calc_fear_greed_v2,
    calc_sth_realized_price, calc_nupl, calc_sopr,
    calc_puell_multiple, calc_hash_ribbons,
    calc_exchange_balance_v2, calc_exchange_netflow_7d,
)
from .scoring import compute_dual_scores
# 历史数据函数（sparkline 用，部分指标无法从 df 推导，回退至 API）
from .history import (
    get_fear_greed_history, get_funding_rate_history_okx,
    get_long_short_history, get_hashrate_history, get_mnav_history,
    get_etf_history, get_max_pain_history,
    get_lth_cdd_history,
    get_mvrv_z_history, get_sth_cost_history, get_nupl_history, get_puell_history,
)


# 注: 原版的单一加权总分 (WEIGHTS + calculate_total_score) 已于 2026-07 移除 —
# BTC Compass 全面切换双评分 (scoring.compute_dual_scores) 后成为死代码,
# 留着容易被误读为现行权重。原版实现见 btc_web (5050) 仓库。


# ============================================================
# 仪表盘显示
# ============================================================

def print_dashboard(result: DashboardResult):
    """打印仪表盘"""
    print("\n" + "=" * 60)
    print("📊 BTC 长期指标仪表盘")
    print("=" * 60)
    print(f"更新时间: {result.timestamp.strftime('%Y-%m-%d %H:%M')}")
    print(f"当前价格: ${result.btc_price:,.2f}")
    print("-" * 60)
    
    # 综合评分条
    score = result.total_score
    bar_length = 30
    position = int((score + 1) / 2 * bar_length)
    bar = "━" * position + "●" + "━" * (bar_length - position - 1)
    print(f"\n综合评分: {score:.2f}  {result.recommendation}")
    print(f"  -1 [{bar}] +1")
    
    # 按优先级分组显示
    print("\n" + "-" * 60)
    print("🔴 P0 核心指标")
    print("-" * 60)
    for name, ind in result.indicators.items():
        if ind.priority == "P0":
            print(f"  {ind.color} {ind.name:15} | {ind.status}")
    
    print("\n" + "-" * 60)
    print("🟡 P1 参考指标")
    print("-" * 60)
    for name, ind in result.indicators.items():
        if ind.priority == "P1":
            print(f"  {ind.color} {ind.name:15} | {ind.status}")
    
    print("\n" + "=" * 60)


# ============================================================
# 主函数
# ============================================================

def get_sparklines(df: pd.DataFrame, indicators: dict, days: int = 7) -> dict:
    """
    计算所有指标的最近 N 天迷你图数据，优先从 df 推导真实时序。
    外部 API 类指标（无法从 df 推导）保留 score 重复值作 fallback。
    """
    sparklines = {}
    recent = df.tail(days)
    GENESIS = pd.Timestamp("2009-01-03")
    HALVINGS = [
        pd.Timestamp("2012-11-28"), pd.Timestamp("2016-07-09"),
        pd.Timestamp("2020-05-11"), pd.Timestamp("2024-04-20"),
    ]

    # ── 预计算全局滚动序列（一次性，复用）──
    ma14g  = df['price'].rolling(14).mean()
    ma111  = df['price'].rolling(111).mean()
    ma200  = df['price'].rolling(200).mean()
    ma350  = df['price'].rolling(350).mean()
    ma730  = df['price'].rolling(730).mean()
    ma1400 = df['price'].rolling(1400).mean()
    ma150  = df['price'].rolling(150).mean()

    delta  = df['price'].diff()
    gain   = delta.clip(lower=0).rolling(14).mean()
    loss   = (-delta.clip(upper=0)).rolling(14).mean()
    rsi_s  = 100 - (100 / (1 + gain / loss))

    ema12  = df['price'].ewm(span=12).mean()
    ema26  = df['price'].ewm(span=26).mean()
    macd_s = ema12 - ema26

    bb_mid = df['price'].rolling(20).mean()
    bb_std = df['price'].rolling(20).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std
    pct_b  = (df['price'] - bb_lower) / (bb_upper - bb_lower)

    def _clean(series, idx, decimals=4):
        vals = series.loc[idx].round(decimals).tolist()
        return [None if (v != v or v is None) else v for v in vals]

    for name in indicators:
        try:
            idx = recent.index

            if name == "Ahr999":
                dca_cost = np.exp(df['price'].tail(200).apply(np.log).mean())
                vals = []
                for ts, row in recent.iterrows():
                    d = (ts - GENESIS).days
                    if d > 0 and dca_cost > 0:
                        fair = 10 ** (AHR999_B * np.log10(d) + AHR999_A)
                        vals.append(round((row['price']/dca_cost)*(row['price']/fair), 4) if fair > 0 else None)
                    else:
                        vals.append(None)
                sparklines[name] = [v for v in vals if v is not None]

            elif name == "Mayer Multiple":
                sparklines[name] = _clean(recent['price'] / ma200.loc[idx], idx, 3)

            elif name == "RSI(14)":
                sparklines[name] = _clean(rsi_s.loc[idx], idx, 1)

            elif name == "MACD":
                sparklines[name] = _clean(macd_s.loc[idx], idx, 2)

            elif name == "布林带":
                sparklines[name] = _clean(pct_b.loc[idx], idx, 4)

            elif name == "Pi Cycle Top":
                ma350x2 = ma350 * 2
                gap_pct = ((ma350x2 - ma111) / ma350x2 * 100).loc[idx]
                sparklines[name] = _clean(gap_pct, idx, 2)

            elif name == "2-Year MA Mult":
                # 价格 / MA730 倍数
                mult = (recent['price'] / ma730.loc[idx])
                sparklines[name] = _clean(mult, idx, 3)

            elif name == "200-Week Heatmap":
                # 价格偏离 MA1400 的百分比
                pct = ((recent['price'] - ma1400.loc[idx]) / ma1400.loc[idx] * 100)
                sparklines[name] = _clean(pct, idx, 2)

            elif name == "Golden Ratio":
                # 价格 / MA350 倍数
                mult = (recent['price'] / ma350.loc[idx])
                sparklines[name] = _clean(mult, idx, 3)

            elif name == "幂律走廊":
                vals = []
                for ts, row in recent.iterrows():
                    d = (ts - GENESIS).days
                    if d > 0:
                        fair = 10 ** (AHR999_B * np.log10(d) + AHR999_A)
                        vals.append(round(row['price'] / fair, 4) if fair > 0 else None)
                    else:
                        vals.append(None)
                sparklines[name] = [v for v in vals if v is not None]

            elif name == "均衡价格":
                balanced = (ma150 + ma350) / 2
                ratio = (recent['price'] / balanced.loc[idx])
                sparklines[name] = _clean(ratio, idx, 3)

            elif name == "减半周期":
                vals = []
                for ts in idx:
                    last_h = max((h for h in HALVINGS if h <= ts), default=HALVINGS[0])
                    vals.append(round((ts - last_h).days / 30.44, 1))
                sparklines[name] = vals

            elif name == "恐惧贪婪指数":
                h = get_fear_greed_history(days)
                sparklines[name] = h.get("values", [])[-days:] or [indicators[name].score] * days

            elif name in ("资金费率", "资金费率(7d)"):
                h = get_funding_rate_history_okx(days)
                sparklines[name] = h.get("values", [])[-days:] or [indicators[name].score] * days

            elif name == "趋势过滤器":
                # 价格相对 20周 EMA 的偏离百分比
                ema140 = df['price'].ewm(span=140, adjust=False).mean()
                dev = ((recent['price'] - ema140.loc[idx]) / ema140.loc[idx] * 100)
                sparklines[name] = _clean(dev, idx, 2)

            elif name == "多空比":
                h = get_long_short_history(days)
                sparklines[name] = h.get("values", [])[-days:] or [indicators[name].score] * days

            elif name == "全网算力":
                h = get_hashrate_history(days)
                sparklines[name] = h.get("values", [])[-days:] or [indicators[name].score] * days

            elif name == "MSTR mNAV":
                h = get_mnav_history(days=days)
                sparklines[name] = h.get("values", [])[-days:] or [indicators[name].score] * days

            elif name in ("ETF活跃度", "ETF资金流", "ETF净流入"):
                h = get_etf_history(days=days)
                sparklines[name] = h.get("values", [])[-days:] or [indicators[name].score] * days

            elif name.startswith("最大痛点"):
                h = get_max_pain_history(df, days=days)
                sparklines[name] = h.get("values", [])[-days:] or [indicators[name].score] * days

            elif name == "公司持仓":
                # 伪历史已下线(当日持仓 × 历史价只是价格曲线的缩放) — 平线兜底
                sparklines[name] = [indicators[name].score] * days

            elif name == "长期持有者(CDD)":
                h = get_lth_cdd_history(days=days)
                sparklines[name] = h.get("values", [])[-days:] or [indicators[name].score] * days

            elif name == "MVRV-Z":
                h = get_mvrv_z_history(days=days)
                sparklines[name] = h.get("values", [])[-days:] or [indicators[name].score] * days

            elif name == "STH成本线":
                h = get_sth_cost_history(df, days=days)
                sparklines[name] = h.get("values", [])[-days:] or [indicators[name].score] * days

            elif name == "NUPL":
                h = get_nupl_history(days=days)
                sparklines[name] = h.get("values", [])[-days:] or [indicators[name].score] * days

            elif name == "Puell Multiple":
                h = get_puell_history(days=days)
                sparklines[name] = h.get("values", [])[-days:] or [indicators[name].score] * days

            else:
                # 其余外部 API 类（ETF/公司持仓/交易所余额/市占率/最大痛点/长期持有者）
                score = indicators[name].score if not np.isnan(indicators[name].value) else 0
                sparklines[name] = [round(score, 2)] * days

        except Exception as e:
            print(f"⚠️ sparkline [{name}] 计算失败: {e}")
            sparklines[name] = []

    return sparklines


def run_dashboard() -> DashboardResult:
    """运行仪表盘分析 — 并行版本"""
    # 获取历史数据（用于计算指标）
    df = fetch_btc_data()
    data_source = str(df.attrs.get("source", "未知"))
    data_synthetic = bool(df.attrs.get("synthetic", False))
    if data_synthetic:
        print("🚨 价格数据为合成示例数据 — 评分仅作管线演示, 将标记为无效")

    # 优先使用实时价格 API，失败则回退到历史数据最新价格
    realtime_price = fetch_realtime_btc_price()
    if realtime_price is not None:
        current_price = realtime_price
        df.iloc[-1, df.columns.get_loc('price')] = current_price
    else:
        current_price = df['price'].iloc[-1]
        print("⚠️ 使用历史数据价格（非实时）")

    indicators = {}

    # === 第一步：快速计算本地 DataFrame 指标（纯计算，无网络IO） ===
    indicators["Mayer Multiple"] = calc_mayer_multiple(df)
    indicators["Pi Cycle Top"] = calc_pi_cycle(df)
    indicators["减半周期"] = calc_halving_cycle()
    indicators["Ahr999"] = calc_ahr999(df)
    indicators["幂律走廊"] = calc_power_law(df)
    indicators["2-Year MA Mult"] = calc_two_year_ma_multiplier(df)
    indicators["200-Week Heatmap"] = calc_200w_ma_heatmap(df)
    indicators["Golden Ratio"] = calc_golden_ratio_multiplier(df)
    indicators["RSI(14)"] = calc_rsi(df)
    indicators["MACD"] = calc_macd(df)
    indicators["布林带"] = calc_bollinger_bands(df)
    indicators["均衡价格"] = calc_balanced_price(df)
    indicators["趋势过滤器"] = calc_trend_filter(df)

    # === 第二步：并发执行网络 API 调用（IO密集，并行加速） ===
    api_tasks = {
        # v2 指标（BTC Compass 新增/替换）
        "MVRV-Z": calc_mvrv_z,
        "稳定币增速": calc_stablecoin_growth,
        "期货基差": calc_futures_basis,
        "资金费率(7d)": calc_funding_rate_7d,
        "ETF净流入": calc_etf_net_flow,
        "恐惧贪婪指数": calc_fear_greed_v2,
        # 第一梯队链上指标（2026-06 外部看板调研后纳入, 6h 缓存）
        "STH成本线": lambda: calc_sth_realized_price(current_price),
        "NUPL": calc_nupl,
        "SOPR": calc_sopr,
        "Puell Multiple": calc_puell_multiple,
        "Hash Ribbons": calc_hash_ribbons,
        # 原有指标 (LTH量比代理已退役: 既不计分也不展示)
        "多空比": calc_long_short_ratio,
        "最大痛点": calc_max_pain,
        "BTC市占率": calc_btc_dominance,
        "MSTR mNAV": calc_mnav,
        "公司持仓": calc_company_holdings,
        "交易所余额": calc_exchange_balance_v2,  # CM社区API主源, 失败自动降级 mempool.space
        "交易所净流(7d)": calc_exchange_netflow_7d,  # 战术分·链上资金流桶 (2026-06)
        "全网算力": calc_hashrate,
    }

    with ThreadPoolExecutor(max_workers=6) as executor:
        future_to_name = {executor.submit(fn): name for name, fn in api_tasks.items()}
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                indicators[name] = future.result(timeout=30)
            except Exception as e:
                print(f"⚠️ 指标 {name} 计算失败: {e}")
                indicators[name] = IndicatorResult(
                    name=name, value=float('nan'), score=0,
                    color="gray", status="数据获取失败",
                    priority="辅助", url="", description="", method=""
                )

    # 优先级归一化: 短期类指标统一为 P1（前端分类 tab 按 P0/P1/P2 过滤）
    for ind in indicators.values():
        if ind.priority == "短期":
            ind.priority = "P1"

    # 固定卡片顺序（api_tasks 用 as_completed 收集, 完成顺序随机,
    # 不重排会导致每次刷新指标卡片乱序跳动）
    _CARD_ORDER = [
        # 周期估值
        "MVRV-Z", "STH成本线", "NUPL", "Mayer Multiple", "200-Week Heatmap",
        "幂律走廊", "Ahr999", "2-Year MA Mult", "Golden Ratio", "均衡价格",
        "Pi Cycle Top", "减半周期",
        # 趋势与动量
        "趋势过滤器", "MACD", "RSI(14)", "布林带", "SOPR",
        # 资金流与筹码
        "ETF净流入", "稳定币增速", "交易所余额", "交易所净流(7d)", "BTC市占率", "公司持仓", "MSTR mNAV",
        # 衍生品与情绪
        "资金费率(7d)", "期货基差", "多空比", "最大痛点", "恐惧贪婪指数",
        # 矿工与网络
        "Puell Multiple", "Hash Ribbons", "全网算力",
    ]
    ordered = {n: indicators[n] for n in _CARD_ORDER if n in indicators}
    ordered.update({n: i for n, i in indicators.items() if n not in ordered})
    indicators = ordered

    # 计算双评分（周期分 + 战术分, 含分位数归一化与因子桶明细）
    scores = compute_dual_scores(indicators, df)

    recommendation = scores["cycle_recommendation"]
    tactical_recommendation = scores["tactical_recommendation"]
    if data_synthetic:
        # 合成数据熔断: 分数照常计算 (便于调试管线), 但建议文本明确标记无效
        recommendation = "🚨 全部价格源失效 — 当前为演示数据, 评分无效, 请勿参考"
        tactical_recommendation = "🚨 演示数据, 评分无效"

    # 触发价位表 (机械反解, 失败不拖垮仪表盘; 合成数据下无意义, 跳过)
    trigger_levels = None
    if not data_synthetic:
        try:
            from .triggers import compute_trigger_levels
            trigger_levels = compute_trigger_levels(df, indicators, current_price)
        except Exception as e:  # Keep broad: 附属面板任何异常不影响主评分。
            logger.warning("触发价位表计算失败: %s", e)

    result = DashboardResult(
        timestamp=datetime.now(),
        btc_price=current_price,
        indicators=indicators,
        total_score=scores["cycle_score"],
        recommendation=recommendation,
        tactical_score=scores["tactical_score"],
        tactical_recommendation=tactical_recommendation,
        cycle_buckets=scores["cycle_buckets"],
        tactical_buckets=scores["tactical_buckets"],
        data_source=data_source,
        data_synthetic=data_synthetic,
        cycle_coverage=scores["cycle_coverage"],
        tactical_coverage=scores["tactical_coverage"],
        trigger_levels=trigger_levels,
    )

    return result


def fetch_builders_feed(limit: int = 30) -> dict:
    """获取 Bitcoin 开发者社区 RSS 动态"""
    import feedparser as _fp
    import time as _t
    import datetime as _dt
    import re as _re

    SOURCES = [
        {
            "key": "optech",
            "name": "Bitcoin Optech",
            "rss": "https://bitcoinops.org/feed.xml",
            "icon": "📡",
            "priority": "critical",
        },
        {
            "key": "delving",
            "name": "Delving Bitcoin",
            "rss": "https://delvingbitcoin.org/latest.rss",
            "icon": "🔍",
            "priority": "critical",
        },
        {
            "key": "devmail",
            "name": "Bitcoin Dev Mailing List",
            "rss": "https://gnusha.org/pi/bitcoindev/atom.xml",
            "icon": "📬",
            "priority": "high",
        },
        {
            "key": "blockstream",
            "name": "Blockstream Research",
            "rss": "https://blog.blockstream.com/rss/",
            "icon": "🔬",
            "priority": "high",
        },
    ]

    result = {"sources": [], "total": 0, "updated_at": ""}
    all_items = []

    for src in SOURCES:
        try:
            feed = _fp.parse(src["rss"])
            items = []
            for entry in feed.entries[:limit // len(SOURCES) + 2]:
                # 统一时间格式
                pub = ""
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    pub = _dt.datetime(*entry.published_parsed[:6]).strftime("%Y-%m-%d")
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    pub = _dt.datetime(*entry.updated_parsed[:6]).strftime("%Y-%m-%d")

                # 摘要截断
                summary = ""
                if hasattr(entry, "summary"):
                    summary = _re.sub(r"<[^>]+>", "", entry.summary or "")[:200].strip()

                items.append({
                    "title": entry.get("title", "").strip(),
                    "url": entry.get("link", ""),
                    "date": pub,
                    "summary": summary,
                })

            result["sources"].append({
                "key": src["key"],
                "name": src["name"],
                "icon": src["icon"],
                "priority": src["priority"],
                "items": items,
                "count": len(items),
            })
            all_items.extend(items)
        except Exception as e:
            result["sources"].append({
                "key": src["key"],
                "name": src["name"],
                "icon": src["icon"],
                "priority": src["priority"],
                "items": [],
                "count": 0,
                "error": str(e),
            })

    result["total"] = len(all_items)
    result["updated_at"] = _dt.datetime.now().strftime("%H:%M")

    # 离线模板聚合摘要：关键词分类 + 跨源信号 + 高频词
    try:
        from .summarizer import summarize_builders_feed
        result["summary"] = summarize_builders_feed(result)
    except Exception as e:
        print(f"⚠️ 开发者动态摘要生成失败: {e}")
        result["summary"] = None

    return result


def main():
    """入口函数"""
    result = run_dashboard()
    print_dashboard(result)
    return result


if __name__ == "__main__":
    main()
