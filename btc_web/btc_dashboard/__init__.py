#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard package
=====================
原 btc_dashboard.py (4339 行) 已按职责拆分为:
- core           基础类型 / 常量 / BTC 价格抓取
- indicators_long  长期周期指标
- indicators_short 短期技术指标
- indicators_aux   辅助 / 链上 / ETF / 衍生品指标
- news           BlockBeats 快讯
- whales         鲸鱼活动 + 交易所余额
- calendars      加密项目日历 + 宏观经济日历
- history        指标历史时序（drawer 用）
- runner         评分、sparkline、并发执行、开发者动态 RSS

为兼容旧版 `from btc_dashboard import X` 调用，本 __init__ 重新导出公开 API。
"""

# 基础类型与价格抓取
from .core import (
    IndicatorResult, DashboardResult,
    GENESIS_DATE, HALVING_DATES, NEXT_HALVING_ESTIMATE,
    POWER_LAW_INTERCEPT, POWER_LAW_SLOPE,
    AHR999_A, AHR999_B,
    fetch_realtime_btc_price, fetch_btc_data, generate_sample_data,
)

# 各类指标
from .indicators_long import (
    calc_two_year_ma_multiplier, calc_200w_ma_heatmap, calc_golden_ratio_multiplier,
    calc_pi_cycle, calc_lth_supply, calc_hashrate, calc_balanced_price,
    calc_halving_cycle, calc_ahr999, calc_power_law, calc_mayer_multiple,
)
from .indicators_short import calc_rsi, calc_macd, calc_bollinger_bands
from .indicators_aux import (
    calc_fear_greed_index, calc_funding_rate, calc_long_short_ratio,
    calc_btc_dominance, calc_etf_flow, calc_mnav, calc_company_holdings,
    calc_exchange_reserve, calc_max_pain,
    fetch_etf_volume, fetch_company_holdings_data, fetch_mstr_price,
    fetch_dat_holdings,
)

# 资讯 / 鲸鱼 / 日历
from .news import fetch_crypto_news
from .whales import (
    fetch_exchange_balance_display,
    fetch_whale_volume_stats,
    fetch_whale_activity,
)
from .calendars import fetch_crypto_calendar, fetch_macro_calendar

# 历史时序
from .history import get_indicator_history

# 开发者动态摘要（离线）
from .summarizer import summarize_builders_feed

# 评分汇总 / sparkline / 主入口 / RSS
from .runner import (
    WEIGHTS,
    calculate_total_score,
    print_dashboard,
    get_sparklines,
    run_dashboard,
    fetch_builders_feed,
    main,
)

__all__ = [
    # 类型
    "IndicatorResult", "DashboardResult",
    # 常量
    "GENESIS_DATE", "HALVING_DATES", "NEXT_HALVING_ESTIMATE",
    "POWER_LAW_INTERCEPT", "POWER_LAW_SLOPE", "AHR999_A", "AHR999_B",
    "WEIGHTS",
    # 价格
    "fetch_realtime_btc_price", "fetch_btc_data", "generate_sample_data",
    # 指标
    "calc_two_year_ma_multiplier", "calc_200w_ma_heatmap", "calc_golden_ratio_multiplier",
    "calc_pi_cycle", "calc_lth_supply", "calc_hashrate", "calc_balanced_price",
    "calc_halving_cycle", "calc_ahr999", "calc_power_law", "calc_mayer_multiple",
    "calc_rsi", "calc_macd", "calc_bollinger_bands",
    "calc_fear_greed_index", "calc_funding_rate", "calc_long_short_ratio",
    "calc_btc_dominance", "calc_etf_flow", "calc_mnav", "calc_company_holdings",
    "calc_exchange_reserve", "calc_max_pain",
    "fetch_etf_volume", "fetch_company_holdings_data", "fetch_mstr_price",
    "fetch_dat_holdings",
    # 资讯/鲸鱼/日历
    "fetch_crypto_news",
    "fetch_exchange_balance_display", "fetch_whale_volume_stats", "fetch_whale_activity",
    "fetch_crypto_calendar", "fetch_macro_calendar",
    # 历史
    "get_indicator_history",
    # 汇总
    "calculate_total_score", "print_dashboard", "get_sparklines",
    "run_dashboard", "fetch_builders_feed", "main",
]


if __name__ == "__main__":
    main()
