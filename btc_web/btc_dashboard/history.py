#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.history
=====================
指标历史数据（前端 drawer 用）。
"""

import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta, timezone
from typing import Tuple, Dict, Optional

from .core import (
    GENESIS_DATE, HALVING_DATES, NEXT_HALVING_ESTIMATE,
    POWER_LAW_INTERCEPT, POWER_LAW_SLOPE,
    AHR999_A, AHR999_B,
)


def get_ahr999_history(df: pd.DataFrame, days: int = 90) -> dict:
    """获取 Ahr999 指标历史数据"""
    # 计算历史 Ahr999
    genesis = datetime(2009, 1, 3)
    
    # 取最近 N 天数据
    recent_df = df.tail(days).copy()
    
    dates = []
    values = []
    
    # 计算对数以求几何平均
    df['log_price'] = np.log(df['price'])
    # Rolling 200 Geometric Mean = exp(Rolling Mean(log_price))
    df['gmean200'] = np.exp(df['log_price'].rolling(200).mean())

    for date, row in recent_df.iterrows():
        days_since = (date - genesis).days
        if days_since > 0:
            log_fair = AHR999_A + AHR999_B * np.log10(days_since)
            fair_price = 10 ** log_fair
            
            # 使用预计算的几何平均 (Rolling Geometric Mean)
            if date in df.index:
                ma200 = df.loc[date, 'gmean200']
            else:
                ma200 = row['price'] # Fallback
            
            # Fallback calculation if rolling data missing (e.g. early days)
            if pd.isna(ma200):
                 # Try manual tail calculation if enough data
                 hist_slice = df.loc[:date, 'price'].tail(200)
                 if len(hist_slice) > 0:
                     ma200 = np.exp(np.mean(np.log(hist_slice)))

            if fair_price > 0 and ma200 > 0:
                # 标准 AHR999 公式: (Price/Cost) * (Price/Fair)
                ahr999 = (row['price'] / ma200) * (row['price'] / fair_price)
                dates.append(date.strftime('%Y-%m-%d'))
                values.append(round(ahr999, 3))
    
    # Clean up temporary columns
    df.drop(columns=['log_price', 'gmean200'], inplace=True, errors='ignore')
    
    return {
        "indicator": "Ahr999",
        "dates": dates,
        "values": values,
        "thresholds": {
            "buy": {"value": 0.45, "color": "#22c55e", "label": "抄底线"},
            "dca": {"value": 1.2, "color": "#eab308", "label": "定投上限"},
            "sell": {"value": 5.0, "color": "#ef4444", "label": "止盈线"}
        }
    }


def get_fear_greed_history(days: int = 30) -> dict:
    """获取恐惧贪婪指数历史数据"""
    try:
        response = requests.get(
            f"https://api.alternative.me/fng/?limit={days}",
            timeout=15
        )
        if response.status_code == 200:
            data = response.json()["data"]
            dates = []
            values = []
            
            for item in reversed(data):  # API 返回的是倒序
                dates.append(datetime.fromtimestamp(int(item["timestamp"])).strftime('%Y-%m-%d'))
                values.append(int(item["value"]))
            
            return {
                "indicator": "恐惧贪婪指数",
                "dates": dates,
                "values": values,
                "thresholds": {
                    "extreme_fear": {"value": 25, "color": "#22c55e", "label": "极度恐惧"},
                    "neutral": {"value": 50, "color": "#eab308", "label": "中性"},
                    "extreme_greed": {"value": 75, "color": "#ef4444", "label": "极度贪婪"}
                }
            }
    except Exception as e:
        print(f"⚠️ Fear & Greed History API 失败: {e}")
    
    return {"indicator": "恐惧贪婪指数", "dates": [], "values": [], "thresholds": {}}


def get_funding_rate_history(days: int = 30) -> dict:
    """获取资金费率历史数据"""
    try:
        # Binance 资金费率每 8 小时一次，需要获取更多数据点
        limit = days * 3
        response = requests.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": "BTCUSDT", "limit": limit},
            timeout=15
        )
        if response.status_code == 200:
            data = response.json()
            
            # 按日期分组，取每天最后一个费率
            daily_data = {}
            for item in data:
                date = datetime.fromtimestamp(item["fundingTime"] / 1000).strftime('%Y-%m-%d')
                rate = float(item["fundingRate"]) * 100
                daily_data[date] = rate
            
            # 排序并取最近 N 天
            sorted_dates = sorted(daily_data.keys())[-days:]
            dates = sorted_dates
            values = [round(daily_data[d], 4) for d in sorted_dates]
            
            return {
                "indicator": "资金费率",
                "dates": dates,
                "values": values,
                "thresholds": {
                    "negative": {"value": -0.03, "color": "#22c55e", "label": "偏空"},
                    "neutral": {"value": 0, "color": "#6b7280", "label": "中性"},
                    "positive": {"value": 0.03, "color": "#eab308", "label": "偏多"},
                    "extreme": {"value": 0.1, "color": "#ef4444", "label": "过热"}
                }
            }
    except Exception as e:
        print(f"⚠️ Funding Rate History API 失败: {e}")
    
    return {"indicator": "资金费率", "dates": [], "values": [], "thresholds": {}}


def get_long_short_history(days: int = 30) -> dict:
    """获取多空比历史数据 (OKX 主源 + Binance 备用)"""
    dates = []
    values = []
    
    # 方法1: OKX API
    try:
        response = requests.get(
            "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio",
            params={"ccy": "BTC", "period": "1D"},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=15
        )
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "0" and data.get("data"):
                # OKX 数据格式: [[timestamp_ms, ratio], ...]，按时间倒序
                for item in reversed(data["data"]):
                    ts = int(item[0]) / 1000
                    date = datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
                    dates.append(date)
                    values.append(round(float(item[1]), 2))
                
                # 只取最近 N 天
                dates = dates[-days:]
                values = values[-days:]
    except Exception as e:
        print(f"⚠️ OKX Long/Short History API 失败: {e}")
    
    # 方法2: Binance (备用)
    if not dates:
        try:
            response = requests.get(
                "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                params={"symbol": "BTCUSDT", "period": "1d", "limit": days},
                timeout=15
            )
            if response.status_code == 200:
                data = response.json()
                for item in data:
                    date = datetime.fromtimestamp(item["timestamp"] / 1000).strftime('%Y-%m-%d')
                    dates.append(date)
                    values.append(round(float(item["longShortRatio"]), 2))
        except Exception as e:
            print(f"⚠️ Binance Long/Short History API 失败: {e}")
    
    if dates:
        return {
            "indicator": "多空比",
            "dates": dates,
            "values": values,
            "thresholds": {
                "short_squeeze": {"value": 0.5, "color": "#22c55e", "label": "空头拥挤"},
                "balanced": {"value": 1.0, "color": "#6b7280", "label": "均衡"},
                "long_heavy": {"value": 1.5, "color": "#eab308", "label": "偏多"},
                "extreme_long": {"value": 2.0, "color": "#ef4444", "label": "极度偏多"}
            }
        }
    
    return {"indicator": "多空比", "dates": [], "values": [], "thresholds": {}}


def get_pi_cycle_history(df: pd.DataFrame, days: int = 90) -> dict:
    """获取 Pi Cycle 历史数据（111MA vs 350MA*2 的差距百分比）"""
    recent_df = df.tail(days + 350).copy()  # 需要更多数据来计算 MA
    
    ma_111 = recent_df['price'].rolling(window=111).mean()
    ma_350 = recent_df['price'].rolling(window=350).mean() * 2
    
    # 计算差距百分比
    gap_pct = ((ma_350 - ma_111) / ma_350 * 100).dropna().tail(days)
    
    dates = [d.strftime('%Y-%m-%d') for d in gap_pct.index]
    values = [round(v, 2) for v in gap_pct.values]
    
    return {
        "indicator": "Pi Cycle Top",
        "dates": dates,
        "values": values,
        "thresholds": {
            "danger": {"value": 0, "color": "#ef4444", "label": "交叉危险"},
            "warning": {"value": 10, "color": "#eab308", "label": "接近"},
            "safe": {"value": 30, "color": "#22c55e", "label": "安全"}
        }
    }



def get_two_year_ma_history(df: pd.DataFrame, days: int = 365*4) -> dict:
    """获取 2-Year MA Multiplier 历史数据"""
    dates = []
    prices = []
    ma2y_vals = []
    ma2y_x5_vals = []
    
    # Pre-calculate rolling mean on FULL dataframe then slice
    # Use .copy() to avoid SettingWithCopyWarning
    work_df = df.copy()
    work_df['ma730'] = work_df['price'].rolling(window=730).mean()
    work_df['ma730_x5'] = work_df['ma730'] * 5
    
    sliced = work_df.tail(days)
    
    for date, row in sliced.iterrows():
        dates.append(date.strftime('%Y-%m-%d'))
        prices.append(round(row['price'], 2))
        ma2y_vals.append(round(row['ma730'], 2) if not pd.isna(row['ma730']) else None)
        ma2y_x5_vals.append(round(row['ma730_x5'], 2) if not pd.isna(row['ma730_x5']) else None)
        
    return {
        "indicator": "2-Year MA Mult",
        "dates": dates,
        "values": prices, # Main line is Price
        "lines": { # Additional lines
            "MA730 (Buy)": {"values": ma2y_vals, "color": "#22c55e"},
            "MA730 x5 (Sell)": {"values": ma2y_x5_vals, "color": "#ef4444"}
        },
        "thresholds": {}
    }

def get_200w_heatmap_history(df: pd.DataFrame, days: int = 365*4) -> dict:
    """获取 200-Week MA Heatmap 历史数据"""
    work_df = df.copy()
    work_df['ma200w'] = work_df['price'].rolling(window=1400).mean()
    sliced = work_df.tail(days)
    
    dates = []
    prices = []
    ma200w_vals = []
    
    for date, row in sliced.iterrows():
        dates.append(date.strftime('%Y-%m-%d'))
        prices.append(round(row['price'], 2))
        ma200w_vals.append(round(row['ma200w'], 2) if not pd.isna(row['ma200w']) else None)
        
    return {
        "indicator": "200-Week Heatmap",
        "dates": dates,
        "values": prices,
        "lines": {
            "200W MA (Bottom)": {"values": ma200w_vals, "color": "#3b82f6"} # Blue
        },
        "thresholds": {}
    }

def get_golden_ratio_history(df: pd.DataFrame, days: int = 365*2) -> dict:
    """获取 Golden Ratio Multiplier 历史数据"""
    work_df = df.copy()
    work_df['ma350'] = work_df['price'].rolling(window=350).mean()
    sliced = work_df.tail(days)
    
    dates = []
    prices = []
    x1_6 = []
    x2_0 = []
    x3_0 = []
    
    for date, row in sliced.iterrows():
        dates.append(date.strftime('%Y-%m-%d'))
        prices.append(round(row['price'], 2))
        if not pd.isna(row['ma350']):
            ma = row['ma350']
            x1_6.append(round(ma * 1.6, 2))
            x2_0.append(round(ma * 2.0, 2))
            x3_0.append(round(ma * 3.0, 2))
        else:
            x1_6.append(None)
            x2_0.append(None)
            x3_0.append(None)
            
    return {
        "indicator": "Golden Ratio",
        "dates": dates,
        "values": prices,
        "lines": {
            "x1.6 (Golden)": {"values": x1_6, "color": "#eab308"},
            "x2.0": {"values": x2_0, "color": "#f97316"},
            "x3.0 (Top)": {"values": x3_0, "color": "#ef4444"}
        },
        "thresholds": {}
    }


def get_mayer_multiple_history(df: pd.DataFrame, days: int = 90) -> dict:
    """Mayer Multiple 历史"""
    work = df.copy()
    work['ma200'] = work['price'].rolling(200).mean()
    sliced = work.tail(days)
    dates, values, prices, ma200_vals = [], [], [], []
    for date, row in sliced.iterrows():
        dates.append(date.strftime('%Y-%m-%d'))
        prices.append(round(row['price'], 2))
        m = round(row['price'] / row['ma200'], 4) if not pd.isna(row['ma200']) and row['ma200'] > 0 else None
        values.append(m)
        ma200_vals.append(round(row['ma200'], 2) if not pd.isna(row['ma200']) else None)
    return {
        "indicator": "Mayer Multiple",
        "dates": dates, "values": values,
        "lines": {"MA200": {"values": ma200_vals, "color": "#3b82f6"}},
        "thresholds": {
            "buy": {"value": 0.8, "color": "#22c55e", "label": "低估"},
            "fair": {"value": 1.0, "color": "#6b7280", "label": "公允"},
            "sell": {"value": 2.4, "color": "#ef4444", "label": "高估"},
        }
    }


def get_power_law_history(df: pd.DataFrame, days: int = 90) -> dict:
    """幂律走廊历史 (价格 vs 幂律中轨)"""
    GENESIS = pd.Timestamp("2009-01-03")
    work = df.copy()
    sliced = work.tail(days)
    dates, prices, mid_vals, low_vals = [], [], [], []
    for date, row in sliced.iterrows():
        d = (date - GENESIS).days
        if d <= 0:
            continue
        mid = 10 ** (5.84 * np.log10(d) - 17.01)
        low = mid * 0.42
        dates.append(date.strftime('%Y-%m-%d'))
        prices.append(round(row['price'], 2))
        mid_vals.append(round(mid, 2))
        low_vals.append(round(low, 2))
    return {
        "indicator": "幂律走廊",
        "dates": dates, "values": prices,
        "lines": {
            "幂律中轨": {"values": mid_vals, "color": "#f59e0b"},
            "幂律下轨": {"values": low_vals, "color": "#22c55e"},
        },
        "thresholds": {}
    }


def get_balanced_price_history(df: pd.DataFrame, days: int = 90) -> dict:
    """均衡价格历史"""
    work = df.copy()
    work['ma150'] = work['price'].rolling(150).mean()
    work['ma350'] = work['price'].rolling(350).mean()
    work['balanced'] = (work['ma150'] + work['ma350']) / 2
    sliced = work.tail(days)
    dates, prices, balanced_vals = [], [], []
    for date, row in sliced.iterrows():
        dates.append(date.strftime('%Y-%m-%d'))
        prices.append(round(row['price'], 2))
        balanced_vals.append(round(row['balanced'], 2) if not pd.isna(row['balanced']) else None)
    return {
        "indicator": "均衡价格",
        "dates": dates, "values": prices,
        "lines": {"均衡价格": {"values": balanced_vals, "color": "#a78bfa"}},
        "thresholds": {}
    }


def get_halving_cycle_history(days: int = 90) -> dict:
    """减半周期历史（月份数随时间推移）"""
    today = datetime.now()
    dates, values = [], []
    for i in range(days - 1, -1, -1):
        day = today - __import__('datetime').timedelta(days=i)
        last_halving = max((h for h in HALVING_DATES if h <= day), default=HALVING_DATES[0])
        months = (day - last_halving).days / 30.44
        dates.append(day.strftime('%Y-%m-%d'))
        values.append(round(months, 1))
    return {
        "indicator": "减半周期",
        "dates": dates, "values": values,
        "thresholds": {
            "phase1": {"value": 12, "color": "#22c55e", "label": "早期牛市"},
            "phase2": {"value": 24, "color": "#eab308", "label": "中期"},
            "phase3": {"value": 36, "color": "#ef4444", "label": "后期"},
        }
    }


def get_rsi_history(df: pd.DataFrame, days: int = 90) -> dict:
    """RSI(14) 历史"""
    delta = df['price'].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rsi = 100 - (100 / (1 + gain / loss))
    sliced_rsi = rsi.tail(days)
    sliced_df = df.tail(days)
    dates = [d.strftime('%Y-%m-%d') for d in sliced_df.index]
    values = [round(v, 2) if not pd.isna(v) else None for v in sliced_rsi.values]
    return {
        "indicator": "RSI(14)",
        "dates": dates, "values": values,
        "thresholds": {
            "oversold": {"value": 30, "color": "#22c55e", "label": "超卖"},
            "neutral":  {"value": 50, "color": "#6b7280", "label": "中性"},
            "overbought": {"value": 70, "color": "#ef4444", "label": "超买"},
        }
    }


def get_macd_history(df: pd.DataFrame, days: int = 90) -> dict:
    """MACD 历史"""
    ema12 = df['price'].ewm(span=12).mean()
    ema26 = df['price'].ewm(span=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9).mean()
    hist = macd - signal
    s = df.tail(days)
    dates = [d.strftime('%Y-%m-%d') for d in s.index]
    idx = s.index
    return {
        "indicator": "MACD",
        "dates": dates,
        "values": [round(v, 2) if not pd.isna(v) else None for v in macd.loc[idx].values],
        "lines": {
            "Signal": {"values": [round(v, 2) if not pd.isna(v) else None for v in signal.loc[idx].values], "color": "#f59e0b"},
            "Histogram": {"values": [round(v, 2) if not pd.isna(v) else None for v in hist.loc[idx].values], "color": "#a78bfa"},
        },
        "thresholds": {"zero": {"value": 0, "color": "#6b7280", "label": "零轴"}}
    }


def get_bb_history(df: pd.DataFrame, days: int = 90) -> dict:
    """布林带历史（%B 值）"""
    mid = df['price'].rolling(20).mean()
    std = df['price'].rolling(20).std()
    upper = mid + 2 * std
    lower = mid - 2 * std
    pct_b = (df['price'] - lower) / (upper - lower)
    s = df.tail(days)
    idx = s.index
    dates = [d.strftime('%Y-%m-%d') for d in idx]
    return {
        "indicator": "布林带",
        "dates": dates,
        "values": [round(v, 4) if not pd.isna(v) else None for v in pct_b.loc[idx].values],
        "thresholds": {
            "oversold":  {"value": 0.0, "color": "#22c55e", "label": "下轨"},
            "mid":       {"value": 0.5, "color": "#6b7280", "label": "中轨"},
            "overbought":{"value": 1.0, "color": "#ef4444", "label": "上轨"},
        }
    }


def get_funding_rate_history_okx(days: int = 30) -> dict:
    """资金费率历史 - OKX（替代被封锁的 Binance）"""
    try:
        resp = requests.get(
            "https://www.okx.com/api/v5/public/funding-rate-history",
            params={"instId": "BTC-USDT-SWAP", "limit": min(days * 3, 100)},
            timeout=15
        )
        if resp.status_code == 200 and resp.json().get("code") == "0":
            raw = resp.json()["data"]
            daily = {}
            for item in raw:
                date = datetime.fromtimestamp(int(item["fundingTime"]) / 1000).strftime('%Y-%m-%d')
                rate = float(item["fundingRate"]) * 100
                if date not in daily:
                    daily[date] = rate
            sorted_dates = sorted(daily.keys())[-days:]
            return {
                "indicator": "资金费率",
                "dates": sorted_dates,
                "values": [round(daily[d], 4) for d in sorted_dates],
                "thresholds": {
                    "negative": {"value": -0.03, "color": "#22c55e", "label": "偏空"},
                    "neutral":  {"value": 0,     "color": "#6b7280", "label": "中性"},
                    "positive": {"value": 0.03,  "color": "#eab308", "label": "偏多"},
                    "extreme":  {"value": 0.1,   "color": "#ef4444", "label": "过热"},
                }
            }
    except Exception as e:
        print(f"⚠️ OKX Funding Rate History 失败: {e}")
    return {"indicator": "资金费率", "dates": [], "values": [], "thresholds": {}}


def get_hashrate_history(days: int = 30) -> dict:
    """全网算力历史 - blockchain.info (单位 TH/s → EH/s)"""
    try:
        resp = requests.get(
            "https://api.blockchain.info/charts/hash-rate",
            params={"timespan": f"{max(days, 30)}days", "format": "json", "sampled": "true"},
            timeout=15
        )
        if resp.status_code == 200:
            pts = resp.json().get("values", [])[-days:]
            dates = [datetime.fromtimestamp(p["x"]).strftime('%Y-%m-%d') for p in pts]
            values = [round(p["y"] / 1e6, 2) for p in pts]  # TH/s → EH/s
            return {
                "indicator": "全网算力",
                "dates": dates, "values": values,
                "thresholds": {}
            }
    except Exception as e:
        print(f"⚠️ Hashrate History 失败: {e}")
    return {"indicator": "全网算力", "dates": [], "values": [], "thresholds": {}}


def get_dominance_history(days: int = 30) -> dict:
    """BTC市占率历史 - 需要付费 API，暂不支持"""
    return {"indicator": "BTC市占率", "dates": [], "values": [], "thresholds": {}}


def get_etf_history(days: int = 30) -> dict:
    """ETF 活跃度历史数据：聚合 IBIT/FBTC/GBTC 日成交额（USD, 十亿）"""
    try:
        import yfinance as yf
        etfs = ["IBIT", "FBTC", "GBTC"]
        end   = datetime.now()
        start = end - timedelta(days=days + 14)

        raw = yf.download(etfs, start=start.strftime("%Y-%m-%d"),
                          end=end.strftime("%Y-%m-%d"),
                          progress=False, auto_adjust=True)
        if raw.empty:
            return {"indicator": "ETF活跃度", "dates": [], "values": [], "thresholds": {}}

        close  = raw["Close"]
        volume = raw["Volume"]

        # 每只 ETF：当日成交额 = 收盘价 × 成交量
        vol_usd = pd.DataFrame()
        for sym in etfs:
            if sym in close.columns and sym in volume.columns:
                vol_usd[sym] = close[sym] * volume[sym]

        daily_total = vol_usd.sum(axis=1).dropna()
        daily_b = (daily_total / 1e9).round(2)  # 转换为十亿美元
        daily_b = daily_b[daily_b > 0].tail(days)

        dates  = [d.strftime("%Y-%m-%d") for d in daily_b.index]
        values = daily_b.tolist()

        return {
            "indicator": "ETF活跃度",
            "dates":  dates,
            "values": values,
            "unit":   "B USD",
            "thresholds": {
                "低活跃": {"value": 1.0, "color": "#ffcc00", "label": "低活跃(<$1B)"},
                "活跃":   {"value": 2.0, "color": "#00e676", "label": "活跃(>$1B)"},
                "高活跃": {"value": 3.0, "color": "#f79322", "label": "高活跃(>$2B)"},
            }
        }
    except Exception as e:
        print(f"⚠️ get_etf_history 失败: {e}")
        return {"indicator": "ETF活跃度", "dates": [], "values": [], "thresholds": {}}


def get_lth_cdd_history(days: int = 30) -> dict:
    """长期持有者(CDD) 历史：从 CoinGecko 180天成交量数据计算每日 7d/90d 量比"""
    try:
        response = requests.get(
            "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=180&interval=daily",
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        )
        if response.status_code != 200:
            return {"indicator": "长期持有者(CDD)", "dates": [], "values": [], "thresholds": {}}

        data = response.json()
        volumes = data.get("total_volumes", [])
        if len(volumes) < 90:
            return {"indicator": "长期持有者(CDD)", "dates": [], "values": [], "thresholds": {}}

        timestamps = [v[0] for v in volumes]
        vol_values = [v[1] for v in volumes]
        df_vol = pd.DataFrame({"volume": vol_values}, index=pd.to_datetime(timestamps, unit="ms"))

        sma7  = df_vol["volume"].rolling(7).mean()
        sma90 = df_vol["volume"].rolling(90).mean()
        ratio = (sma7 / sma90).dropna().tail(days)

        dates  = [d.strftime("%Y-%m-%d") for d in ratio.index]
        values = [round(v, 3) for v in ratio.values]

        return {
            "indicator": "长期持有者(CDD)",
            "dates":  dates,
            "values": values,
            "unit":   "ratio",
            "thresholds": {
                "吸筹": {"value": 0.8,  "color": "#00e676", "label": "吸筹(<0.8)"},
                "正常": {"value": 1.3,  "color": "#ffcc00", "label": "正常(0.8-1.3)"},
                "派发": {"value": 2.0,  "color": "#ff4444", "label": "派发(>1.5)"},
            }
        }
    except Exception as e:
        print(f"⚠️ get_lth_cdd_history 失败: {e}")
        return {"indicator": "长期持有者(CDD)", "dates": [], "values": [], "thresholds": {}}


def get_company_holdings_history(df: pd.DataFrame = None, days: int = 30) -> dict:
    """公司持仓历史 — 已下线(诚实性): 旧实现是「当日持仓量 × 历史价格」的伪历史,
    曲线只是价格走势的缩放, 且前端渲染会丢弃说明标签。
    在攒出真实逐日持仓快照前返回空(sparkline 落平线兜底, drawer 显示空)。"""
    return {"indicator": "公司持仓", "dates": [], "values": [], "thresholds": {}}


def get_max_pain_history(df: pd.DataFrame = None, days: int = 30) -> dict:
    """最大痛点历史 — 已下线(诚实性): 旧实现是「今日痛点定值 × 历史现价」的伪历史,
    形态与价格图完全同形, 会被读成痛点位的真实迁移(期权面板每 10min 已在算
    真实 max_pain, 攒出逐日快照序列后再上真历史)。"""
    return {"indicator": "最大痛点", "dates": [], "values": [], "thresholds": {}}


def get_mnav_history(df: pd.DataFrame = None, days: int = 30) -> dict:
    """
    MSTR mNAV 历史数据：用 yfinance 同时拉取 MSTR 和 BTC-USD 计算。
    持仓/股本与实时卡片同源 (indicators_aux 动态获取, 失败退同一组带日期常数) —
    2026-07 复查发现图表链路曾滞留旧常数 568,840, 图表值 = 卡片值 ×1.48 互相矛盾。
    注: 历史各日按"当前"持仓/股本近似 (历史逐日的真实持仓无免费序列), 短窗口内可接受。
    """
    try:
        import yfinance as yf
        from .indicators_aux import (fetch_mstr_holdings, fetch_mstr_shares,
                                     MSTR_BTC_FALLBACK, MSTR_SHARES_FALLBACK)
        try:
            MSTR_BTC = fetch_mstr_holdings() or MSTR_BTC_FALLBACK
        except Exception:
            MSTR_BTC = MSTR_BTC_FALLBACK
        try:
            MSTR_SHARES = fetch_mstr_shares() or MSTR_SHARES_FALLBACK
        except Exception:
            MSTR_SHARES = MSTR_SHARES_FALLBACK

        end   = datetime.now()
        start = end - timedelta(days=days + 14)

        # 同时下载 MSTR 和 BTC-USD，保证日期完全对齐
        raw = yf.download(["MSTR", "BTC-USD"],
                          start=start.strftime("%Y-%m-%d"),
                          end=end.strftime("%Y-%m-%d"),
                          progress=False, auto_adjust=True)
        if raw.empty:
            return {"indicator": "MSTR mNAV", "dates": [], "values": [], "thresholds": {}}

        close = raw["Close"]           # MultiIndex columns: (ticker)
        mstr  = close["MSTR"].dropna()
        btc   = close["BTC-USD"].dropna()
        merged = pd.concat([mstr, btc], axis=1, join="inner")
        merged.columns = ["mstr", "btc"]
        merged = merged.dropna().tail(days)

        dates  = [d.strftime("%Y-%m-%d") for d in merged.index]
        values = [round((MSTR_SHARES * float(row.mstr)) / (MSTR_BTC * float(row.btc)), 3)
                  for _, row in merged.iterrows()]

        return {
            "indicator": "MSTR mNAV",
            "dates":  dates,
            "values": values,
            "unit":   "x",
            "thresholds": {
                "折价":   {"value": 1.0,  "color": "#00e676", "label": "折价(<1×)"},
                "低溢价": {"value": 1.5,  "color": "#f79322", "label": "低溢价(<1.5×)"},
                "正常":   {"value": 2.0,  "color": "#ffcc00", "label": "正常(<2×)"},
                "高溢价": {"value": 3.0,  "color": "#ff4444", "label": "高溢价(≥3×)"},
            }
        }
    except Exception as e:
        print(f"⚠️ get_mnav_history 失败: {e}")
        return {"indicator": "MSTR mNAV", "dates": [], "values": [], "thresholds": {}}


_BD_HEADERS = {"User-Agent": "btc-compass/1.0"}

# CoinMetrics 社区 API 历史缓存 (进程级, 一小时 TTL, 单次请求覆盖 MVRV/NUPL/Puell 三指标)
import threading as _hist_threading
import time as _hist_time

_cm_hist_cache = None
_cm_hist_ts = 0
_cm_hist_lock = _hist_threading.Lock()
_CM_HIST_TTL = 3600


def _fetch_cm_history(days: int):
    """CoinMetrics 社区 API → {dates, mvrv_z, nupl, puell} 最近 days 天
    与 backfill.py 相同的 metrics 组合 (社区 tier 可用), Z-Score 和 NUPL 从 MVRV ratio 派生"""
    global _cm_hist_cache, _cm_hist_ts
    now = _hist_time.time()
    with _cm_hist_lock:
        if _cm_hist_cache and (now - _cm_hist_ts) < _CM_HIST_TTL:
            c = _cm_hist_cache
            return {k: v[-days:] for k, v in c.items()}
    try:
        metrics = "PriceUSD,CapMrktCurUSD,CapMVRVCur,IssTotUSD"
        r = requests.get(
            "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics",
            params={"assets": "btc", "metrics": metrics, "frequency": "1d",
                    "start_time": "2010-07-18", "page_size": 10000},
            timeout=30, headers=_BD_HEADERS)
        if r.status_code != 200:
            return None
        rows = r.json().get("data", [])
        if len(rows) < 500:
            return None

        dates, mvrv_raw, mcap_raw, iss_raw = [], [], [], []
        for row in rows:
            def _f(k):
                v = row.get(k)
                try:
                    return float(v) if v is not None else None
                except (TypeError, ValueError):
                    return None
            dates.append(row["time"][:10])
            mvrv_raw.append(_f("CapMVRVCur"))
            mcap_raw.append(_f("CapMrktCurUSD"))
            iss_raw.append(_f("IssTotUSD"))

        mvrv_s = pd.Series(mvrv_raw, dtype=float).replace(0, np.nan)
        mcap_s = pd.Series(mcap_raw, dtype=float)
        iss_s = pd.Series(iss_raw, dtype=float)

        rcap_s = mcap_s / mvrv_s
        mvrv_z = ((mcap_s - rcap_s) / mcap_s.rolling(730).std()).round(4).tolist()
        nupl = (1 - 1 / mvrv_s).round(4).tolist()
        puell = (iss_s / iss_s.rolling(365).mean()).round(4).tolist()

        result = {"dates": dates, "mvrv_z": mvrv_z, "nupl": nupl, "puell": puell}
        with _cm_hist_lock:
            _cm_hist_cache = result
            _cm_hist_ts = now
        return {k: v[-days:] for k, v in result.items()}
    except Exception as e:
        print(f"⚠️ CoinMetrics history 失败: {e}")
        return None


def _fetch_bd_history(metric: str, field: str, days: int):
    """
    bitcoin-data.com 范围端点 → (dates, values) 或 ([], [])。
    ⚠️ 必须走 _cached_onchain (6h 缓存 + 30min 负缓存): 该函数被 runner 的
    sparkline 每轮仪表盘刷新调用, 无缓存时 5 分钟一刷 = 12 req/h, 单独就打爆
    bitcoin-data 匿名 10 req/h 限额, 反过来锁死 _bd_last 指标链路——现网
    STH成本线/SOPR 长期"数据源暂不可用"的根因 (2026-07 对抗性审查修复)。
    """
    from .indicators_v2 import _cached_onchain

    def _fetch():
        start = (datetime.now() - timedelta(days=days + 10)).strftime("%Y-%m-%d")
        end = datetime.now().strftime("%Y-%m-%d")
        r = requests.get(f"https://bitcoin-data.com/v1/{metric}",
                         params={"startday": start, "endday": end},
                         timeout=15, headers=_BD_HEADERS)
        if r.status_code != 200:
            return None
        rows = sorted(r.json(), key=lambda x: x["d"])[-days:]
        dates = [row["d"] for row in rows]
        values = [round(float(row[field]), 4) for row in rows]
        return (dates, values) if dates else None

    try:
        res = _cached_onchain(f"bdhist-{metric}-{days}", _fetch)
    except Exception as e:
        print(f"⚠️ bitcoin-data {metric} history 失败: {e}")
        res = None
    return res if res else ([], [])


def _clean_nans(dates, values):
    """过滤掉 NaN 值的日期和数值对"""
    out_d, out_v = [], []
    for d, v in zip(dates, values):
        if v is not None and v == v:
            out_d.append(d)
            out_v.append(v)
    return out_d, out_v


def get_mvrv_z_history(days: int = 30) -> dict:
    """MVRV Z-Score 历史 — 主源 CoinMetrics, 备源 bitcoin-data"""
    cm = _fetch_cm_history(days)
    if cm and cm.get("dates"):
        dates, values = _clean_nans(cm["dates"], cm["mvrv_z"])
    else:
        dates, values = _fetch_bd_history("mvrv-zscore", "mvrvZscore", days)
    return {
        "indicator": "MVRV-Z", "dates": dates, "values": values,
        "thresholds": {
            "底部带": {"value": 0, "color": "#00d26a", "label": "底部带 (Z<0)"},
            "中性":   {"value": 3, "color": "#ffcc00", "label": "中性 (Z<3)"},
            "顶部带": {"value": 5, "color": "#ff4444", "label": "顶部带 (Z≥5)"},
        }
    }


def get_sth_cost_history(df: pd.DataFrame, days: int = 30) -> dict:
    """STH 成本线历史 — 仅 bitcoin-data 有此指标"""
    dates, sth_prices = _fetch_bd_history("sth-realized-price", "sthRealizedPrice", days)
    if not dates or df is None or df.empty:
        return {"indicator": "STH成本线", "dates": [], "values": [], "thresholds": {}}
    recent = df.tail(days + 10)
    ratios, out_dates = [], []
    for d, sth_p in zip(dates, sth_prices):
        try:
            ts = pd.Timestamp(d)
            if ts in recent.index and sth_p > 0:
                ratios.append(round(recent.loc[ts, 'price'] / sth_p, 3))
                out_dates.append(d)
        except (KeyError, TypeError):
            continue
    return {
        "indicator": "STH成本线", "dates": out_dates, "values": ratios,
        "thresholds": {
            "投降带": {"value": 0.80, "color": "#00d26a", "label": "投降带 (<0.8)"},
            "成本线": {"value": 1.00, "color": "#ffcc00", "label": "成本线 (1.0)"},
            "过热":   {"value": 1.35, "color": "#ff4444", "label": "过热 (>1.35)"},
        }
    }


def get_nupl_history(days: int = 30) -> dict:
    """NUPL 历史 — 主源 CoinMetrics, 备源 bitcoin-data"""
    cm = _fetch_cm_history(days)
    if cm and cm.get("dates"):
        dates, values = _clean_nans(cm["dates"], cm["nupl"])
    else:
        dates, values = _fetch_bd_history("nupl", "nupl", days)
    return {
        "indicator": "NUPL", "dates": dates, "values": values,
        "thresholds": {
            "投降":   {"value": 0,    "color": "#ff4444", "label": "投降 (<0)"},
            "希望":   {"value": 0.25, "color": "#ffcc00", "label": "希望/恐惧"},
            "乐观":   {"value": 0.50, "color": "#f79322", "label": "乐观"},
            "兴奋":   {"value": 0.75, "color": "#00d26a", "label": "兴奋 (>0.75)"},
        }
    }


def get_puell_history(days: int = 30) -> dict:
    """Puell Multiple 历史 — 主源 CoinMetrics, 备源 bitcoin-data"""
    cm = _fetch_cm_history(days)
    if cm and cm.get("dates"):
        dates, values = _clean_nans(cm["dates"], cm["puell"])
    else:
        dates, values = _fetch_bd_history("puell-multiple", "puellMultiple", days)
    return {
        "indicator": "Puell Multiple", "dates": dates, "values": values,
        "thresholds": {
            "底部带": {"value": 0.5, "color": "#00d26a", "label": "底部带 (<0.5)"},
            "正常":   {"value": 2.0, "color": "#ffcc00", "label": "正常"},
            "顶部带": {"value": 4.0, "color": "#ff4444", "label": "顶部带 (>4)"},
        }
    }


def get_indicator_history(indicator_name: str, df: pd.DataFrame = None, days: int = 30) -> dict:
    """统一的历史数据获取入口"""
    if indicator_name == "Ahr999" and df is not None:
        return get_ahr999_history(df, days)
    elif indicator_name == "恐惧贪婪指数":
        return get_fear_greed_history(days)
    elif indicator_name in ("资金费率", "资金费率(7d)"):
        return get_funding_rate_history_okx(days)
    elif indicator_name == "多空比":
        return get_long_short_history(days)
    elif indicator_name == "Pi Cycle Top" and df is not None:
        return get_pi_cycle_history(df, days)
    elif indicator_name == "2-Year MA Mult" and df is not None:
        return get_two_year_ma_history(df, days)
    elif indicator_name == "200-Week Heatmap" and df is not None:
        return get_200w_heatmap_history(df, days)
    elif indicator_name == "Golden Ratio" and df is not None:
        return get_golden_ratio_history(df, days)
    elif indicator_name == "Mayer Multiple" and df is not None:
        return get_mayer_multiple_history(df, days)
    elif indicator_name == "幂律走廊" and df is not None:
        return get_power_law_history(df, days)
    elif indicator_name == "均衡价格" and df is not None:
        return get_balanced_price_history(df, days)
    elif indicator_name == "减半周期":
        return get_halving_cycle_history(days)
    elif indicator_name == "RSI(14)" and df is not None:
        return get_rsi_history(df, days)
    elif indicator_name == "MACD" and df is not None:
        return get_macd_history(df, days)
    elif indicator_name == "布林带" and df is not None:
        return get_bb_history(df, days)
    elif indicator_name == "全网算力":
        return get_hashrate_history(days)
    elif indicator_name == "BTC市占率":
        return get_dominance_history(days)
    elif indicator_name == "MSTR mNAV" and df is not None:
        return get_mnav_history(df, days)
    elif indicator_name in ("ETF活跃度", "ETF资金流", "ETF净流入"):
        return get_etf_history(days)
    elif indicator_name.startswith("最大痛点") and df is not None:
        return get_max_pain_history(df, days)
    elif indicator_name == "公司持仓" and df is not None:
        return get_company_holdings_history(df, days)
    elif indicator_name == "长期持有者(CDD)":
        return get_lth_cdd_history(days)
    elif indicator_name == "MVRV-Z":
        return get_mvrv_z_history(days)
    elif indicator_name == "STH成本线" and df is not None:
        return get_sth_cost_history(df, days)
    elif indicator_name == "NUPL":
        return get_nupl_history(days)
    elif indicator_name == "Puell Multiple":
        return get_puell_history(days)
    else:
        return {"indicator": indicator_name, "dates": [], "values": [], "thresholds": {}}


