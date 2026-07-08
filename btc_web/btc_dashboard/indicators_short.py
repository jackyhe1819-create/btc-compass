#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.indicators_short
==============================
短期技术指标：RSI、MACD、布林带。
"""

import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta, timezone
from typing import Tuple, Dict, Optional

from .core import IndicatorResult


def fetch_okx_kline(bar: str, limit: int = 100) -> Optional[pd.Series]:
    """OKX 真实K线收盘价序列 (升序)。RSI/MACD 共用。失败返回 None。"""
    try:
        response = requests.get(
            "https://www.okx.com/api/v5/market/candles",
            params={"instId": "BTC-USDT", "bar": bar, "limit": limit},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=10
        )
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "0" and data.get("data"):
                closes = [float(item[4]) for item in reversed(data["data"])]
                return pd.Series(closes)
    except Exception as e:
        print(f"⚠️ OKX {bar} K线获取失败: {e}")
    return None


def calc_rsi(df: pd.DataFrame, period: int = 14) -> IndicatorResult:
    """
    RSI 多周期汇总 (4H, 12H, 日, 周, 月)
    - 4H/12H: OKX 真实K线 (与 MACD 同源)；获取失败则跳过该周期，
      不再用日线序列切片伪装 (旧实现三个周期数值完全相同, 日线被三重计票)
    - 周线/月线: 日线重采样
    - 年线已移除: 年K不足15根无统计意义, 且 BTC 长期上行使年线 RSI 常年超买,
      会给战术分注入一张永久看空票 (2026-07 对抗性审查修复)
    """
    df = df.copy()
    
    if len(df) < period + 1:
        return IndicatorResult(
            name="RSI",
            value=float('nan'),
            score=0,
            color="⚪",
            status="数据不足",
            priority="短期",
            url="https://www.tradingview.com/chart/?symbol=BINANCE:BTCUSDT"
        )
    
    def calculate_single_rsi(price_series, period=14):
        """计算单周期 RSI"""
        if len(price_series) < period + 1:
            return None
        
        delta = price_series.diff()
        gain = delta.where(delta > 0, 0)
        loss = (-delta).where(delta < 0, 0)
        
        avg_gain = gain.rolling(window=period).mean()
        avg_loss = loss.rolling(window=period).mean()
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        current_rsi = rsi.iloc[-1]
        
        if pd.isna(current_rsi):
            return None
        
        if current_rsi >= 80:
            return {"rsi": current_rsi, "signal": "极度超买", "trend": "超买", "score": -1}
        elif current_rsi >= 70:
            return {"rsi": current_rsi, "signal": "超买", "trend": "超买", "score": -0.5}
        elif current_rsi <= 20:
            return {"rsi": current_rsi, "signal": "极度超卖", "trend": "超卖", "score": 1}
        elif current_rsi <= 30:
            return {"rsi": current_rsi, "signal": "超卖", "trend": "超卖", "score": 0.5}
        else:
            return {"rsi": current_rsi, "signal": "中性", "trend": "中性", "score": 0}
    
    results = {}
    overbought_count = 0
    oversold_count = 0
    neutral_count = 0
    total_score = 0

    def add_result(tf_name, result):
        nonlocal overbought_count, oversold_count, neutral_count, total_score
        if result:
            results[tf_name] = result
            if result["trend"] == "超买":
                overbought_count += 1
            elif result["trend"] == "超卖":
                oversold_count += 1
            else:
                neutral_count += 1
            total_score += result["score"]

    # 4H / 12H - OKX 真实K线（获取失败则跳过，不用日线伪装）
    kline_4h = fetch_okx_kline("4H", 100)
    if kline_4h is not None:
        add_result("4H", calculate_single_rsi(kline_4h, period))

    kline_12h = fetch_okx_kline("12Hutc", 100)
    if kline_12h is not None:
        add_result("12H", calculate_single_rsi(kline_12h, period))

    # 日线 RSI (基准)
    add_result("日线", calculate_single_rsi(df['price'], period))

    # 周线重采样
    try:
        df_indexed = df.set_index('date') if 'date' in df.columns else df
        weekly_prices = df_indexed['price'].resample('W').last().dropna()
        if len(weekly_prices) >= period + 1:
            add_result("周线", calculate_single_rsi(weekly_prices, period))
    except Exception:
        pass

    # 月线重采样
    try:
        df_indexed = df.set_index('date') if 'date' in df.columns else df
        monthly_prices = df_indexed['price'].resample('ME').last().dropna()
        if len(monthly_prices) >= period + 1:
            add_result("月线", calculate_single_rsi(monthly_prices, period))
    except Exception:
        pass

    # 生成汇总状态
    total_timeframes = len(results)
    
    if total_timeframes == 0:
        status = "数据不足"
        color = "⚪"
        score = 0
    else:
        avg_score = total_score / total_timeframes
        
        if overbought_count > oversold_count and overbought_count > neutral_count:
            if overbought_count >= total_timeframes * 0.8:
                status = f"多周期超买 ({overbought_count}/{total_timeframes})"
                color = "🔴"
                score = -1
            else:
                status = f"偏超买 ({overbought_count}/{total_timeframes})"
                color = "🟡"
                score = -0.5
        elif oversold_count > overbought_count and oversold_count > neutral_count:
            if oversold_count >= total_timeframes * 0.8:
                status = f"多周期超卖 ({oversold_count}/{total_timeframes})"
                color = "🟢"
                score = 1
            else:
                status = f"偏超卖 ({oversold_count}/{total_timeframes})"
                color = "🟢"
                score = 0.5
        else:
            status = f"多周期中性 ({neutral_count}/{total_timeframes})"
            color = "🟡"
            score = 0
    
    # 构建详细信息
    details = []
    for tf, result in results.items():
        rsi_val = result["rsi"]
        if result["trend"] == "超买":
            icon = "🔴"
        elif result["trend"] == "超卖":
            icon = "🟢"
        else:
            icon = "🟡"
        details.append(f"{tf}:{icon}{rsi_val:.0f}")
    
    detail_str = " | ".join(details)
    
    return IndicatorResult(
        name="RSI",
        value=total_score,
        score=score,
        color=color,
        status=f"{status}\n{detail_str}",
        priority="短期",
        url="https://www.tradingview.com/chart/?symbol=BINANCE:BTCUSDT",
        description="相对强弱指数 (RSI) 衡量价格变动的速度和幅度，以评估资产是否超买或超卖。",
        method="多周期投票: 4H/12H 用 OKX 真实K线, 日线用价格序列, 周/月线由日线重采样。各周期 RSI≥70 记超买票、≤30 记超卖票, 按多数方向与占比汇总评分。注: 平滑用简单均值 (Cutler 法), 数值与 TradingView 的 Wilder 版会有小幅差异。"
    )


def calc_macd(df: pd.DataFrame) -> IndicatorResult:
    """
    MACD 多周期汇总 (4H, 12H, 日, 周, 月)
    - 4H/12H: 使用 OKX 真实K线数据
    - 日线: 使用传入的日线数据
    - 周线/月线: 日线重采样
    """
    df = df.copy()
    
    if len(df) < 35:
        return IndicatorResult(
            name="MACD",
            value=float('nan'),
            score=0,
            color="⚪",
            status="数据不足",
            priority="短期"
        )
    
    def calculate_single_macd(price_series):
        """计算单周期 MACD"""
        if len(price_series) < 35:
            return None
        
        ema12 = price_series.ewm(span=12, adjust=False).mean()
        ema26 = price_series.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        histogram = macd_line - signal_line
        
        current_macd = macd_line.iloc[-1]
        current_signal = signal_line.iloc[-1]
        current_hist = histogram.iloc[-1]
        prev_hist = histogram.iloc[-2] if len(histogram) > 1 else 0
        
        # 判断金叉/死叉
        is_golden_cross = current_macd > current_signal and macd_line.iloc[-2] <= signal_line.iloc[-2]
        is_death_cross = current_macd < current_signal and macd_line.iloc[-2] >= signal_line.iloc[-2]
        
        if is_golden_cross:
            return {"signal": "金叉", "trend": "多", "strength": 2}
        elif is_death_cross:
            return {"signal": "死叉", "trend": "空", "strength": 2}
        elif current_macd > current_signal:
            if current_hist > prev_hist:
                return {"signal": "多头增强", "trend": "多", "strength": 1}
            else:
                return {"signal": "多头减弱", "trend": "多", "strength": 0.5}
        else:
            if current_hist < prev_hist:
                return {"signal": "空头增强", "trend": "空", "strength": 1}
            else:
                return {"signal": "空头减弱", "trend": "空", "strength": 0.5}
    
    results = {}
    bullish_count = 0
    bearish_count = 0
    total_strength = 0
    
    def add_result(tf_name, result):
        nonlocal bullish_count, bearish_count, total_strength
        if result:
            results[tf_name] = result
            if result["trend"] == "多":
                bullish_count += 1
                total_strength += result["strength"]
            else:
                bearish_count += 1
                total_strength -= result["strength"]
    
    # 4H MACD - OKX 真实K线
    kline_4h = fetch_okx_kline("4H", 100)
    if kline_4h is not None:
        add_result("4H", calculate_single_macd(kline_4h))
    
    # 12H MACD - OKX 真实K线
    kline_12h = fetch_okx_kline("12Hutc", 100)
    if kline_12h is not None:
        add_result("12H", calculate_single_macd(kline_12h))
    
    # 日线 MACD (基准)
    add_result("日线", calculate_single_macd(df['price']))
    
    # 周线重采样
    try:
        df_indexed = df.set_index('date') if 'date' in df.columns else df
        weekly_prices = df_indexed['price'].resample('W').last().dropna()
        if len(weekly_prices) >= 35:
            add_result("周线", calculate_single_macd(weekly_prices))
    except Exception:
        pass
    
    # 月线重采样
    try:
        monthly_prices = df_indexed['price'].resample('ME').last().dropna()
        if len(monthly_prices) >= 35:
            add_result("月线", calculate_single_macd(monthly_prices))
    except Exception:
        pass
    
    # 生成汇总状态
    total_timeframes = len(results)
    
    if total_timeframes == 0:
        status = "数据不足"
        color = "⚪"
        score = 0
    elif bullish_count > bearish_count:
        ratio = bullish_count / total_timeframes
        if ratio >= 0.8:
            status = f"强势多头 ({bullish_count}/{total_timeframes})"
            color = "🟢"
            score = 1
        elif ratio >= 0.5:
            status = f"偏多 ({bullish_count}/{total_timeframes})"
            color = "🟢"
            score = 0.5
        else:
            status = f"多空分歧 ({bullish_count}多/{bearish_count}空)"
            color = "🟡"
            score = 0.2
    elif bearish_count > bullish_count:
        ratio = bearish_count / total_timeframes
        if ratio >= 0.8:
            status = f"强势空头 ({bearish_count}/{total_timeframes})"
            color = "🔴"
            score = -1
        elif ratio >= 0.5:
            status = f"偏空 ({bearish_count}/{total_timeframes})"
            color = "🔴"
            score = -0.5
        else:
            status = f"多空分歧 ({bullish_count}多/{bearish_count}空)"
            color = "🟡"
            score = -0.2
    else:
        status = f"多空平衡 ({bullish_count}多/{bearish_count}空)"
        color = "🟡"
        score = 0
    
    # 构建详细信息
    details = []
    for tf, result in results.items():
        trend_icon = "🟢" if result["trend"] == "多" else "🔴"
        details.append(f"{tf}:{trend_icon}{result['signal']}")
    
    detail_str = " | ".join(details)
    
    return IndicatorResult(
        name="MACD",
        value=total_strength,
        score=score,
        color=color,
        status=f"{status}\n{detail_str}",
        priority="短期",
        url="https://www.tradingview.com/chart/?symbol=BINANCE:BTCUSDT",
        description="平滑异同移动平均线 (MACD) 是一种趋势跟踪动量指标，显示两条移动平均线之间的关系。",
        method="MACD线是12期EMA减去26期EMA，信号线是MACD线的9期EMA。MACD线穿过信号线形成金叉（买入）或死叉（卖出）信号。"
    )



def calc_bollinger_bands(df: pd.DataFrame, period: int = 20, std_dev: int = 2) -> IndicatorResult:
    """
    布林带 (Bollinger Bands)
    - 波动率指标
    - 价格触上轨: 超买, 触下轨: 超卖
    - 带宽收窄: 可能突破
    """
    df = df.copy()
    
    if len(df) < period:
        return IndicatorResult(
            name="布林带",
            value=float('nan'),
            score=0,
            color="⚪",
            status="数据不足",
            priority="短期"
        )
    
    # 计算中轨 (SMA)
    middle_band = df['price'].rolling(window=period).mean()
    # 计算标准差
    std = df['price'].rolling(window=period).std()
    # 上轨和下轨
    upper_band = middle_band + (std * std_dev)
    lower_band = middle_band - (std * std_dev)
    
    current_price = df['price'].iloc[-1]
    current_upper = upper_band.iloc[-1]
    current_lower = lower_band.iloc[-1]
    current_middle = middle_band.iloc[-1]
    
    # 计算价格在带中的位置 (0-100)
    band_width = current_upper - current_lower
    position = (current_price - current_lower) / band_width * 100 if band_width > 0 else 50
    
    # 评分逻辑
    if current_price >= current_upper:
        score, color, status = -0.5, "🟡", f"触及上轨 - 超买"
    elif current_price <= current_lower:
        score, color, status = 0.5, "🟢", f"触及下轨 - 超卖"
    elif position > 80:
        score, color, status = -0.3, "🟡", f"接近上轨 ({position:.0f}%)"
    elif position < 20:
        score, color, status = 0.3, "🟢", f"接近下轨 ({position:.0f}%)"
    else:
        score, color, status = 0, "🟡", f"通道中部 ({position:.0f}%)"
    
    return IndicatorResult(
        name="布林带",
        value=position,
        score=score,
        color=color,
        status=status,
        priority="短期",
        url="https://www.tradingview.com/chart/?symbol=BINANCE:BTCUSDT",
        description="布林带是一种波动性指标，由中轨（移动平均线）和上下两条标准差带组成。",
        method="价格触及上轨可能表示超买，触及下轨可能表示超卖。带宽收窄预示着价格可能即将出现剧烈波动。"
    )

