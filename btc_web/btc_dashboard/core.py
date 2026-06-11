#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.core
==================
基础类型、常量与 BTC 价格数据获取。
"""

import pandas as pd
import numpy as np
import yfinance as yf
import requests
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Tuple, Dict, Optional
from functools import lru_cache
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
warnings.filterwarnings('ignore')


def fetch_realtime_btc_price() -> Optional[float]:
    """
    从多个 API 获取实时 BTC 价格
    优先级: CoinGecko -> Binance -> Coinbase
    """
    apis = [
        {
            "name": "CoinGecko",
            "url": "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
            "parser": lambda r: r.json()["bitcoin"]["usd"]
        },
        {
            "name": "Binance",
            "url": "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
            "parser": lambda r: float(r.json()["price"])
        },
        {
            "name": "Coinbase",
            "url": "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            "parser": lambda r: float(r.json()["data"]["amount"])
        }
    ]
    
    for api in apis:
        try:
            response = requests.get(api["url"], timeout=10)
            if response.status_code == 200:
                price = api["parser"](response)
                print(f"✅ 实时价格 ({api['name']}): ${price:,.2f}")
                return price
        except Exception as e:
            print(f"⚠️ {api['name']} API 失败: {e}")
            continue
    
    return None


# ============================================================
# 配置常量
# ============================================================

# 比特币创世日期
GENESIS_DATE = datetime(2009, 1, 3)

# 历史减半日期
HALVING_DATES = [
    datetime(2012, 11, 28),  # 第一次减半
    datetime(2016, 7, 9),    # 第二次减半
    datetime(2020, 5, 11),   # 第三次减半
    datetime(2024, 4, 20),   # 第四次减半
]

# 预计下次减半（约4年后）
NEXT_HALVING_ESTIMATE = datetime(2028, 4, 20)


# 幂律参数
POWER_LAW_INTERCEPT = -17.67
POWER_LAW_SLOPE = 5.93

# Ahr999 参数 (九神原版参数)
AHR999_A = -17.01  # 截距
AHR999_B = 5.84    # 斜率


# ============================================================
# 数据类定义
# ============================================================

@dataclass
class IndicatorResult:
    """单个指标的结果"""
    name: str           # 指标名称
    value: float        # 原始值
    score: int          # 评分: -1, 0, 1
    color: str          # 颜色: 🟢, 🟡, 🔴
    status: str         # 状态描述
    priority: str       # 优先级: P0, P1, P2
    description: str = "" # 指标定义
    method: str = ""    # 计算方式
    url: Optional[str] = None  # 外部链接 (可选)


@dataclass
class DashboardResult:
    """仪表盘总结果 (BTC Compass: total_score 即周期分, 另含战术分与因子桶明细)"""
    timestamp: datetime
    btc_price: float
    indicators: Dict[str, IndicatorResult]
    total_score: float            # 周期分 (定仓位)
    recommendation: str           # 仓位建议
    tactical_score: float = 0.0   # 战术分 (定时机)
    tactical_recommendation: str = ""
    cycle_buckets: Optional[Dict] = None     # 周期分因子桶明细
    tactical_buckets: Optional[Dict] = None  # 战术分因子桶明细


# ============================================================
# 数据获取
# ============================================================

def fetch_btc_data(start_date: str = "2013-01-01", max_retries: int = 3) -> pd.DataFrame:
    """获取 BTC 历史价格数据（带重试机制，多数据源）"""
    import time
    
    print("📥 正在获取 BTC 价格数据...")
    
    # 方法1: Yahoo Finance
    for attempt in range(max_retries):
        try:
            btc = yf.download("BTC-USD", start=start_date, progress=False)
            
            # 处理多重索引
            if isinstance(btc.columns, pd.MultiIndex):
                btc.columns = btc.columns.get_level_values(0)
            
            btc = btc[['Close']].dropna()
            
            if btc.empty:
                raise ValueError("获取到空数据")
            
            btc.columns = ['price']
            print(f"✅ Yahoo Finance: 获取到 {len(btc)} 条数据，最新日期: {btc.index[-1].date()}")
            return btc
            
        except Exception as e:
            error_msg = str(e)
            print(f"⚠️ Yahoo Finance 尝试 {attempt + 1}/{max_retries} 失败: {error_msg}")
            
            # 如果是限流错误，直接停止重试
            if "Rate limited" in error_msg or "Too Many Requests" in error_msg:
                print("⛔️ Yahoo Finance API 限流，尝试备用数据源...")
                break
                
            if attempt < max_retries - 1:
                time.sleep(1)
    
    # 方法2: CryptoCompare API (2000天，无地区限制，免费)
    print("📡 尝试 CryptoCompare API (2000天)...")
    try:
        response = requests.get(
            "https://min-api.cryptocompare.com/data/v2/histoday",
            params={"fsym": "BTC", "tsym": "USD", "limit": 2000},
            timeout=20
        )
        if response.status_code == 200:
            data = response.json().get("Data", {}).get("Data", [])
            if data:
                import datetime as _dt
                df = pd.DataFrame(data)
                df["date"] = pd.to_datetime(df["time"], unit="s")
                df.set_index("date", inplace=True)
                df["price"] = df["close"].astype(float)
                df = df[["price"]].dropna()
                df = df[df["price"] > 0]
                print(f"✅ CryptoCompare: 获取到 {len(df)} 条数据，最新日期: {df.index[-1].date()}")
                return df
        else:
            print(f"⚠️ CryptoCompare API Error: Status {response.status_code}")
    except Exception as e:
        print(f"⚠️ CryptoCompare API 失败: {e}")

    # 方法3: CoinGecko API (365天，免费接口支持)
    print("📡 尝试 CoinGecko API (365天)...")
    try:
        response = requests.get(
            "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
            params={"vs_currency": "usd", "days": "365", "interval": "daily"},
            headers={"Accept": "application/json"},
            timeout=30
        )
        if response.status_code == 200:
            data = response.json()
            prices = data.get("prices", [])
            if prices:
                df = pd.DataFrame(prices, columns=["timestamp", "price"])
                df["date"] = pd.to_datetime(df["timestamp"], unit="ms")
                df.set_index("date", inplace=True)
                df = df[["price"]]
                print(f"✅ CoinGecko: 获取到 {len(df)} 条数据，最新日期: {df.index[-1].date()}")
                return df
        else:
            print(f"⚠️ CoinGecko API Error: Status {response.status_code}")
    except Exception as e:
        print(f"⚠️ CoinGecko API 失败: {e}")

    # 方法3: Kraken OHLC (720天，无地区限制)
    print("📡 尝试 Kraken API...")
    try:
        response = requests.get(
            "https://api.kraken.com/0/public/OHLC",
            params={"pair": "XBTUSD", "interval": 1440},
            timeout=20
        )
        if response.status_code == 200:
            data = response.json()
            ohlc = data.get("result", {}).get("XXBTZUSD", [])
            if ohlc:
                df = pd.DataFrame(ohlc, columns=["time","open","high","low","close","vwap","volume","count"])
                df["date"] = pd.to_datetime(df["time"].astype(int), unit="s")
                df.set_index("date", inplace=True)
                df["price"] = df["close"].astype(float)
                df = df[["price"]]
                print(f"✅ Kraken: 获取到 {len(df)} 条数据，最新日期: {df.index[-1].date()}")
                return df
        else:
            print(f"⚠️ Kraken API Error: Status {response.status_code}")
    except Exception as e:
        print(f"⚠️ Kraken API 失败: {e}")

    # 方法4: Binance Klines
    print("📡 尝试 Binance API (Klines)...")
    try:
        response = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "1d", "limit": 1000},
            timeout=15
        )
        if response.status_code == 200:
            data = response.json()
            prices = [{"timestamp": item[0], "price": float(item[4])} for item in data]
            df = pd.DataFrame(prices)
            df["date"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("date", inplace=True)
            df = df[["price"]]
            print(f"✅ Binance: 获取到 {len(df)} 条数据，最新日期: {df.index[-1].date()}")
            return df
        else:
            print(f"⚠️ Binance API Error: Status {response.status_code}")
    except Exception as e:
        print(f"⚠️ Binance API 失败: {e}")

    # 方法5: 所有来源都失败，使用示例数据
    print("⚠️ 无法获取实时数据，使用示例数据演示...")
    return generate_sample_data()


def generate_sample_data() -> pd.DataFrame:
    """生成示例数据用于演示（当 API 不可用时）"""
    # 使用一些典型的 BTC 价格数据点
    dates = pd.date_range(start='2020-01-01', end=datetime.now(), freq='D')
    
    # 模拟价格走势（基于幂律增长 + 周期波动）
    days = np.arange(len(dates))
    base_price = 7000  # 2020年初价格
    
    # 添加增长趋势和周期性
    trend = base_price * (1.002 ** days)  # 日均0.2%增长
    cycle = np.sin(days / 365 * 2 * np.pi) * 0.3  # 年度周期
    noise = np.random.normal(0, 0.02, len(days))  # 随机噪声
    
    prices = trend * (1 + cycle + noise)
    
    # 最新价格设为约 $95000
    prices = prices * (95000 / prices[-1])
    
    df = pd.DataFrame({'price': prices}, index=dates)
    print(f"📊 生成了 {len(df)} 条示例数据")
    return df

# 指标计算函数
