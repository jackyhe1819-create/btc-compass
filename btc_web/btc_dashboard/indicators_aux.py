#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.indicators_aux
============================
辅助指标：恐惧贪婪、资金费率、多空比、市占率、ETF 流、MSTR mNAV、公司持仓、
交易所余额、Max Pain。
"""

import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta, timezone
from typing import Tuple, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from .core import IndicatorResult


def calc_fear_greed_index() -> IndicatorResult:
    """
    贪婪恐惧指数
    - 数据源: alternative.me (免费 API)
    - 0-25: 极度恐惧, 25-45: 恐惧, 45-55: 中性, 55-75: 贪婪, 75-100: 极度贪婪
    """
    try:
        response = requests.get("https://api.alternative.me/fng/", timeout=10)
        if response.status_code == 200:
            data = response.json()["data"][0]
            value = int(data["value"])
            classification = data["value_classification"]
            
            # 评分逻辑：恐惧时买入机会（绿），贪婪时风险（红）
            if value <= 25:
                score, color = 1, "🟢"
                status = f"极度恐惧 ({value}) - 买入机会"
            elif value <= 45:
                score, color = 0.5, "🟢"
                status = f"恐惧 ({value}) - 偏买入"
            elif value <= 55:
                score, color = 0, "🟡"
                status = f"中性 ({value})"
            elif value <= 75:
                score, color = -0.5, "🟡"
                status = f"贪婪 ({value}) - 谨慎"
            else:
                score, color = -1, "🔴"
                status = f"极度贪婪 ({value}) - 风险高"
            
            return IndicatorResult(
                name="恐惧贪婪指数",
                value=float(value),
                score=score,
                color=color,
                status=status,
                priority="P1",
                url="https://alternative.me/crypto/fear-and-greed-index/",
                description="恐惧贪婪指数衡量市场情绪，0代表极度恐惧，100代表极度贪婪。",
                method="该指数综合了波动性、市场成交量、社交媒体情绪、市场主导地位和谷歌趋势等多个因素。极度恐惧通常是买入机会，极度贪婪则需谨慎。"
            )
    except Exception as e:
        print(f"⚠️ Fear & Greed API 失败: {e}")
    
    return IndicatorResult(
        name="恐惧贪婪指数",
        value=float('nan'),
        score=0,
        color="⚪",
        status="API 暂不可用",
        priority="P1"
    )


def calc_funding_rate() -> IndicatorResult:
    """
    资金费率
    - 数据源: Binance (免费 API)
    - 正费率: 多头付空头, 市场偏多
    - 负费率: 空头付多头, 市场偏空
    """
    rate = None
    source = None

    # 1. Try Binance
    try:
        response = requests.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": "BTCUSDT", "limit": 1},
            timeout=10
        )
        if response.status_code == 200:
            data = response.json()[0]
            rate = float(data["fundingRate"]) * 100  # 转为百分比
            source = "Binance"
    except Exception as e:
        print(f"⚠️ Binance Funding Rate failed: {e}")

    # 2. Fallback: OKX (无地区限制)
    if rate is None:
        try:
            okx_resp = requests.get(
                "https://www.okx.com/api/v5/public/funding-rate",
                params={"instId": "BTC-USDT-SWAP"},
                timeout=10
            )
            if okx_resp.status_code == 200:
                okx_data = okx_resp.json()
                if okx_data.get("code") == "0":
                    rate = float(okx_data["data"][0]["fundingRate"]) * 100
                    source = "OKX"
        except Exception as e:
            print(f"⚠️ OKX Funding Rate failed: {e}")

    # 3. Fallback: Bybit
    if rate is None:
        try:
            bybit_resp = requests.get(
                "https://api.bybit.com/v5/market/tickers",
                params={"category": "linear", "symbol": "BTCUSDT"},
                timeout=10
            )
            if bybit_resp.status_code == 200:
                b_data = bybit_resp.json()
                if b_data.get("retCode") == 0:
                    rate = float(b_data["result"]["list"][0]["fundingRate"]) * 100
                    source = "Bybit"
        except Exception as e:
            print(f"⚠️ Bybit Fallback failed: {e}")

    # 4. Fallback: CoinGecko Derivatives
    if rate is None:
        try:
            cg_response = requests.get("https://api.coingecko.com/api/v3/derivatives", timeout=20)
            if cg_response.status_code == 200:
                for item in cg_response.json():
                    if item.get('market') == 'Binance (Futures)' and item.get('symbol') == 'BTCUSDT':
                        rate = float(item.get('funding_rate', 0)) * 100
                        source = "CoinGecko"
                        break
        except Exception as e:
            print(f"⚠️ CoinGecko Fallback failed: {e}")

    # 4. If all failed, return Error but with valid value to show card
    if rate is None:
        return IndicatorResult(
            name="资金费率",
            value=0.0, # Return 0.0 instead of NaN
            score=0,
            color="⚪",
            status="数据源连接失败 (SSL)",
            priority="P1",
            description="资金费率...",
            method="因网络或SSL问题无法连接 Binance/Bybit API。请检查网络连接。"
        )

    # Common scoring logic (reused)
    if rate > 0.1:
        score, color = -1, "🔴"
        status = f"过热 ({rate:.4f}%) - 多头拥挤"
    elif rate > 0.03:
        score, color = -0.5, "🟡"
        status = f"偏多 ({rate:.4f}%)"
    elif rate > -0.03:
        score, color = 0, "🟡"
        status = f"中性 ({rate:.4f}%)"
    elif rate > -0.1:
        score, color = 0.5, "🟢"
        status = f"偏空 ({rate:.4f}%)"
    else:
        score, color = 1, "🟢"
        status = f"恐慌 ({rate:.4f}%) - 空头拥挤"
    
    return IndicatorResult(
        name="资金费率",
        value=rate,
        score=score,
        color=color,
        status=status,
        priority="P1",
        url="https://www.coinglass.com/zh/funding-rate",
        description="资金费率是永续合约市场特有的机制，用于平衡多头和空头持仓。",
        method="正费率表示多头支付空头，市场偏多；负费率表示空头支付多头，市场偏空。极端费率可能预示市场反转。"
    )


def calc_long_short_ratio() -> IndicatorResult:
    """
    全球多空比
    - 主数据源: OKX (中国大陆可访问)
    - 备用数据源: Binance (可能被地域限制)
    """
    ratio = None
    source = ""
    
    # 方法1: OKX API (无地域限制)
    try:
        response = requests.get(
            "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio",
            params={"ccy": "BTC", "period": "1H"},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=10
        )
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == "0" and data.get("data"):
                ratio = float(data["data"][0][1])
                source = "OKX"
    except Exception as e:
        print(f"⚠️ OKX Long/Short API failed: {e}")
    
    # 方法2: Binance (备用)
    if ratio is None:
        try:
            response = requests.get(
                "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                params={"symbol": "BTCUSDT", "period": "1h", "limit": 1},
                timeout=10
            )
            if response.status_code == 200:
                data = response.json()[0]
                ratio = float(data["longShortRatio"])
                source = "Binance"
        except Exception as e:
            print(f"⚠️ Binance Long/Short API failed: {e}")
    
    if ratio is not None:
        # 计算多头/空头百分比
        long_pct = ratio / (1 + ratio) * 100
        short_pct = 100 - long_pct
        
        # 评分逻辑
        if ratio > 2.0:
            score, color = -1, "🔴"
            status = f"极度偏多 ({ratio:.2f}) 多{long_pct:.0f}%/空{short_pct:.0f}%"
        elif ratio > 1.2:
            score, color = -0.5, "🟡"
            status = f"偏多 ({ratio:.2f})"
        elif ratio > 0.8:
            score, color = 0, "🟡"
            status = f"均衡 ({ratio:.2f})"
        elif ratio > 0.5:
            score, color = 0.5, "🟢"
            status = f"偏空 ({ratio:.2f})"
        else:
            score, color = 1, "🟢"
            status = f"极度偏空 ({ratio:.2f})"
        
        return IndicatorResult(
            name="多空比",
            value=ratio,
            score=score,
            color=color,
            status=f"{status} [{source}]",
            priority="P1",
            url="https://www.coinglass.com/zh/LongShortRatio",
            description="多空比反映了市场上多头和空头持仓的相对比例，是衡量市场情绪的指标。",
            method="通过交易所API获取多头账户与空头账户的比例。极端的多空比可能预示着市场情绪的过度集中，存在反转风险。"
        )
    
    return IndicatorResult(
        name="多空比",
        value=float('nan'),
        score=0,
        color="⚪",
        status="API 暂不可用",
        priority="P1"
    )


def _fetch_btc_dominance():
    """
    四级备源链, 返回 (btc_d, src) 或 None。
    口径说明: CoinGecko/CoinPaprika/CoinLore 为全币种分母 (~58%);
    alternative.me 仅 ~3000 币, 读数系统性偏高 4-5pp, 故置末位并标注。
    """
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

    # 1. CoinGecko (本地 IP 通常可用; 云 IP 常被限流)
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=10, headers=headers)
        if r.status_code == 200:
            return float(r.json()["data"]["market_cap_percentage"]["btc"]), "CoinGecko"
        print(f"⚠️ CoinGecko Global 返回 {r.status_code}")
    except Exception as e:
        print(f"⚠️ CoinGecko Global API 失败: {e}")

    # 2. CoinPaprika
    try:
        r = requests.get("https://api.coinpaprika.com/v1/global", timeout=10, headers=headers)
        if r.status_code == 200:
            v = r.json().get("bitcoin_dominance_percentage")
            if v is not None:
                return float(v), "CoinPaprika"
        else:
            print(f"⚠️ CoinPaprika Global 返回 {r.status_code}")
    except Exception as e:
        print(f"⚠️ CoinPaprika Global API 失败: {e}")

    # 3. CoinLore (云 IP 友好, 与 CoinGecko 同口径)
    try:
        r = requests.get("https://api.coinlore.net/api/global/", timeout=10, headers=headers)
        if r.status_code == 200:
            arr = r.json()
            if isinstance(arr, list) and arr and arr[0].get("btc_d"):
                return float(arr[0]["btc_d"]), "CoinLore"
        else:
            print(f"⚠️ CoinLore Global 返回 {r.status_code}")
    except Exception as e:
        print(f"⚠️ CoinLore Global API 失败: {e}")

    # 4. alternative.me (云 IP 友好但分母小、读数偏高, 仅兜底)
    try:
        r = requests.get("https://api.alternative.me/v2/global/", timeout=10, headers=headers)
        if r.status_code == 200:
            v = (r.json().get("data") or {}).get("bitcoin_percentage_of_market_cap")
            if v is not None:
                v = float(v)
                if v <= 1.5:  # 该接口历史上返回过小数形式
                    v *= 100
                return v, "alternative.me·窄口径"
        else:
            print(f"⚠️ alternative.me Global 返回 {r.status_code}")
    except Exception as e:
        print(f"⚠️ alternative.me Global API 失败: {e}")

    return None


def calc_btc_dominance() -> IndicatorResult:
    """
    BTC 市占率 (Dominance)
    - 四级备源: CoinGecko → CoinPaprika → CoinLore → alternative.me
    - 6h TTL 缓存 (慢变量, 同时避免高频轰 CoinGecko 招致云 IP 限流)
    """
    from .indicators_v2 import _cached_onchain
    pair = _cached_onchain("btc-dominance", _fetch_btc_dominance)
    btc_d, src = pair if pair else (None, "")

    if btc_d is None:
        return IndicatorResult(
            name="BTC市占率",
            value=float('nan'),
            score=0,
            color="⚪",
            status="API 暂不可用",
            priority="P2",
            url="https://coinmarketcap.com/charts/bitcoin-dominance/"
        )

    # 简单评分逻辑: >50% 强势
    if btc_d > 55:
        score, color = 1, "🟢"
        status = f"{btc_d:.1f}% (强势吸血)"
    elif btc_d > 45:
        score, color = 0, "🟡"
        status = f"{btc_d:.1f}% (震荡)"
    else:
        score, color = -0.5, "🔴"
        status = f"{btc_d:.1f}% (弱势/山寨季)"

    return IndicatorResult(
        name="BTC市占率",
        value=btc_d,
        score=score,
        color=color,
        status=status,
        priority="P2",
        url="https://coinmarketcap.com/charts/bitcoin-dominance/",
        description="比特币市值占加密货币总市值的比例，反映了比特币在市场中的主导地位。",
        method=f"四级备源 CoinGecko → CoinPaprika → CoinLore → alternative.me（本次：{src}），6h 缓存。牛市初期，BTC市占率通常上涨（吸血效应）；牛市后期，随着资金流向山寨币，BTC市占率可能下降（山寨季）。"
    )

def fetch_etf_volume() -> Tuple[float, float, str]:
    """
    获取 ETF 交易量数据
    多层 fallback:
    1. Yahoo Finance JSON API (query2.finance.yahoo.com)
    2. Yahoo Finance HTML 抓取
    3. 返回占位符引导点击
    """
    import re
    
    etfs = ["IBIT", "FBTC", "GBTC"]  # 主要 BTC ETFs
    total_volume = 0
    success_count = 0
    
    for symbol in etfs:
        # 方法1: Yahoo Finance JSON API (更稳定)
        try:
            url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=2d"
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "application/json"
            }
            resp = requests.get(url, headers=headers, timeout=8)
            
            if resp.status_code == 200:
                data = resp.json()
                result = data.get("chart", {}).get("result", [])
                if result:
                    meta = result[0].get("meta", {})
                    price = meta.get("regularMarketPrice", 0)
                    volume = meta.get("regularMarketVolume", 0)
                    
                    if price > 0 and volume > 0:
                        vol_usd = price * volume
                        total_volume += vol_usd
                        success_count += 1
                        continue
                        
        except Exception as e:
            print(f"⚠️ Yahoo JSON API ({symbol}): {e}")
        
        # 方法2: HTML 抓取 fallback
        try:
            url = f"https://finance.yahoo.com/quote/{symbol}"
            headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
            resp = requests.get(url, headers=headers, timeout=5)
            
            if resp.status_code == 200:
                # 提取 JSON 数据块
                vol_match = re.search(r'"regularMarketVolume":\{"raw":(\d+)', resp.text)
                price_match = re.search(r'"regularMarketPrice":\{"raw":([\d\.]+)', resp.text)
                
                if vol_match and price_match:
                    volume = float(vol_match.group(1))
                    price = float(price_match.group(1))
                    total_volume += volume * price
                    success_count += 1
                    
        except Exception as e:
            print(f"⚠️ Yahoo HTML ({symbol}): {e}")
    
    # 结果处理
    if success_count > 0:
        vol_b = total_volume / 1e9
        status = f"日成交 ${vol_b:.1f}B ({success_count}只ETF)"
        return vol_b, 0.0, status
    
    # 全部失败，返回占位符
    return 0.0, 0.0, "点击查看详情 ↗"


# MSTR 兜底常数 — 动态源失败时使用, 必须带快照日期并在状态显式标注 (2026-07 审计)
MSTR_BTC_FALLBACK    = 843_775      # CoinGecko public_treasury 2026-07-10 快照
MSTR_SHARES_FALLBACK = 246_000_000  # 流通股本 2026Q1 快照 (ATM 增发后偏低)


def fetch_public_treasury():
    """
    CoinGecko public_treasury 原始载荷, 经 _cached_onchain 6h 缓存。
    此前 calc_mnav / calc_company_holdings / fetch_dat_holdings 三处各自裸连同一
    端点 (~28 次/时), 与 bd 限流自伤同构 — 集中一处缓存 (2026-07 复查修复)。
    """
    from .indicators_v2 import _cached_onchain

    def _fetch():
        r = requests.get(
            "https://api.coingecko.com/api/v3/companies/public_treasury/bitcoin",
            timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            print(f"⚠️ CoinGecko public_treasury HTTP {r.status_code}")
            return None
        data = r.json()
        return data if data.get("companies") else None

    return _cached_onchain("cg-public-treasury", _fetch)


def fetch_mstr_holdings():
    """结构化取 Strategy(MSTR) 持仓 BTC 数。失败返回 None (调用方退常数并标注)。"""
    data = fetch_public_treasury()
    if not data:
        return None
    cands = [c for c in data.get("companies", [])
             if "strategy" in str(c.get("name", "")).lower()
             or "micro" in str(c.get("name", "")).lower()]
    if not cands:
        return None
    # 命中多个 (AsiaStrategy/Microcloud 等) 时取持仓最大者 = 真·Strategy
    best = max(cands, key=lambda c: float(c.get("total_holdings") or 0))
    v = float(best.get("total_holdings") or 0)
    return v if v > 0 else None


def fetch_mstr_shares():
    """MSTR 流通股本 (yfinance fast_info, 6h 缓存)。失败返回 None。"""
    from .indicators_v2 import _cached_onchain

    def _fetch():
        import yfinance as yf
        fi = yf.Ticker("MSTR").fast_info
        for k in ("shares", "sharesOutstanding", "shares_outstanding"):
            try:
                v = fi[k]
                if v and float(v) > 0:
                    return float(v)
            except (KeyError, TypeError, ValueError):
                continue
        return None

    return _cached_onchain("mstr-shares", _fetch)


def fetch_company_holdings_data() -> Tuple[float, str]:
    """
    获取上市公司持仓数据 (经 fetch_public_treasury 6h 缓存)
    返回: (total_holdings, status_text)
    """
    data = fetch_public_treasury()
    if data:
        total_holdings = data.get('total_holdings', 0)
        companies = data.get('companies', [])
        top_text = ""
        if companies:
            mstr = fetch_mstr_holdings()
            if mstr:
                top_text = f"MSTR: {mstr:,.0f} BTC"

        status = f"总持仓 {total_holdings:,.0f} BTC"
        if top_text:
            status += f" | {top_text}"
        return total_holdings, status

    return 0.0, "API 暂不可用"


def fetch_dat_holdings(limit: int = 8):
    """
    上市公司 BTC 持仓 Top N（DAT 卡片用）
    来源: CoinGecko Public Treasury API（经 fetch_public_treasury 6h 缓存）
    返回 {total, companies:[{name, symbol, holdings, pct_supply}], updated_at}，失败 None
    """
    try:
        data = fetch_public_treasury()
        if not data:
            return None
        companies = []
        for c in (data.get("companies") or [])[:limit]:
            sym = (c.get("symbol") or "").strip()
            if sym.endswith(".US"):
                sym = sym[:-3]  # MSTR.US → MSTR；3350.T 等非美股保留后缀
            holdings = c.get("total_holdings")
            if not c.get("name") or holdings is None:
                continue
            companies.append({
                "name": c["name"],
                "symbol": sym,
                "holdings": round(float(holdings)),
                "pct_supply": c.get("percentage_of_total_supply"),
            })
        if not companies:
            return None
        return {
            "total": round(float(data.get("total_holdings") or 0)),
            "companies": companies,
            "updated_at": datetime.now().strftime("%m-%d %H:%M"),
        }
    except Exception as e:
        print(f"⚠️ DAT Holdings 获取失败: {e}")
        return None


# ============================================================
# 新增指标 - 占位符 (需付费/注册)
# ============================================================

def calc_etf_flow() -> IndicatorResult:
    """
    ETF 综合数据
    - 数据源: YFinance (成交量) + CoinGlass 链接 (净流入/资产规模)
    - 展示: 日成交量, 并引导查看 CoinGlass 获取完整数据
    """
    vol_b, change, vol_status = fetch_etf_volume()
    
    # 构建综合状态文本
    # 由于 API 限制，净流入/AUM 需点击查看
    if vol_b > 0:
        status_parts = [f"日成交 ${vol_b:.1f}B"]
        if change != 0:
            status_parts.append(f"({change:+.1f}%)")
    else:
        status_parts = ["日成交 -"]
    
    # 添加提示查看完整数据
    status_parts.append("| 净流入/AUM 详情 ↗")
    status_text = " ".join(status_parts)
    
    # 评分: 成交量巨大视为活跃/利好
    if vol_b > 2.0:
        score, color = 1, "🟢"
    elif vol_b > 1.0:
        score, color = 0.5, "🟢"
    elif vol_b > 0:
        score, color = 0, "🟡"
    else:
        score, color = 0, "⚪"
        
    return IndicatorResult(
        name="ETF活跃度",
        value=vol_b,
        score=score,
        color=color,
        status=status_text,
        priority="P1",
        url="https://www.coinglass.com/bitcoin-etf",
        description="比特币现货ETF的交易量和资金流向，反映了机构投资者对市场的参与度和情绪。",
        method="通过聚合主要比特币现货ETF（如IBIT, FBTC, GBTC）的日交易量来衡量活跃度。高交易量和净流入通常被视为市场利好。"
    )



def fetch_mstr_price():
    """获取 Strategy (MSTR) 实时股价，多源回退"""
    HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

    # 方法1: Stooq（无需 API key，通常可访问）
    try:
        resp = requests.get(
            "https://stooq.com/q/l/?s=mstr.us&f=sd2t2ohlcv&h&e=csv",
            timeout=8, headers=HEADERS
        )
        if resp.status_code == 200:
            lines = resp.text.strip().split('\n')
            if len(lines) >= 2:
                parts = lines[1].split(',')
                # 列顺序: Symbol,Date,Time,Open,High,Low,Close,Volume
                close = float(parts[6]) if len(parts) > 6 else float(parts[4])
                if close > 0:
                    print(f"✅ MSTR 股价 via Stooq: ${close:.2f}")
                    return close
    except Exception as e:
        print(f"⚠️ Stooq MSTR 失败: {e}")

    # 方法2: Yahoo Finance v8
    try:
        resp = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/MSTR?interval=1d&range=1d",
            timeout=8, headers=HEADERS
        )
        if resp.status_code == 200:
            price = resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
            if price > 0:
                print(f"✅ MSTR 股价 via Yahoo: ${price:.2f}")
                return float(price)
    except Exception as e:
        print(f"⚠️ Yahoo MSTR 失败: {e}")

    return None


def calc_mnav() -> IndicatorResult:
    """
    MSTR mNAV — Strategy (MicroStrategy) 市净率溢价
    mNAV = MSTR 股票总市值 / (持仓 BTC 数量 × BTC 价格)
    - mNAV > 3 : 极高溢价，泡沫风险 🔴
    - mNAV 2-3 : 高溢价，偏高估 🟠
    - mNAV 1.5-2: 正常溢价 🟡
    - mNAV 1-1.5: 低溢价，偏低估 🟢
    - mNAV < 1  : 折价，极罕见机会 🟢
    """
    # 持仓动态获取 (CoinGecko public_treasury, 结构化取数, 6h 缓存); 失败退常数快照。
    # 2026-07 审计发现旧常数 568,840 比实际少 48% (NAV 被低估近半) —
    # 常数兜底必须带快照日期、显式标注并打日志, 不允许静默陈旧。
    stale_notes = []
    mstr_btc = None
    try:
        mstr_btc = fetch_mstr_holdings()
    except Exception as e:
        print(f"⚠️ MSTR 持仓动态获取失败: {e}")
    if not mstr_btc or mstr_btc <= 0:
        mstr_btc = MSTR_BTC_FALLBACK
        stale_notes.append("持仓常数2026-07快照")
        print("⚠️ MSTR 持仓退常数兜底 (CoinGecko 不可用)")

    # 股本动态获取 (yfinance fast_info, 6h 缓存 — 股本为慢变量);
    # 失败退常数。市值 = 股本 × 实时股价, 与 NAV 的实时 BTC 价口径一致。
    # (复查修掉的旧方案 Yahoo v7 quote 是双重死代码: NameError + 现网 401)
    mstr_shares = None
    try:
        mstr_shares = fetch_mstr_shares()
    except Exception as e:
        print(f"⚠️ MSTR 股本动态获取失败: {e}")
    if not mstr_shares or mstr_shares <= 0:
        mstr_shares = MSTR_SHARES_FALLBACK
        stale_notes.append("股本常数2026Q1快照")
        print("⚠️ MSTR 股本退常数兜底 (yfinance 不可用)")

    # 获取 BTC 价格
    btc_price = None
    try:
        r = requests.get(
            "https://mempool.space/api/v1/prices", timeout=5,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        if r.status_code == 200:
            btc_price = r.json().get("USD")
    except:
        pass

    mstr_price = fetch_mstr_price()

    _desc   = ("衡量 Strategy(MSTR) 股票市值相对其持有 BTC 净资产的溢价倍数。"
               "溢价越高说明市场对 MSTR 杠杆 BTC 模式给予更高定价。历史区间 1×–3×。")

    if btc_price is None or mstr_price is None:
        return IndicatorResult(
            name="MSTR mNAV",
            value=float('nan'), score=0, color="⚪",
            status=f"数据获取失败 (MSTR={'N/A' if mstr_price is None else f'${mstr_price:.0f}'})",
            priority="P0",
            url="https://saylortracker.com/",
            description=_desc,
            method="mNAV = MSTR市值 ÷ (持仓BTC × BTC价格)。数据获取失败。"
        )

    btc_nav = mstr_btc * btc_price
    mkt_cap = mstr_shares * mstr_price
    mnav = mkt_cap / btc_nav

    _method = (f"mNAV = MSTR市值({mstr_shares/1e6:.0f}M股 × 股价) ÷ "
               f"({mstr_btc:,.0f} BTC × BTC价格)。持仓取自 CoinGecko public_treasury、"
               "股本取自 yfinance (各 6h 缓存); 常数兜底带快照日期并在状态标注。")

    if mnav < 1.0:
        score, color, label = 1.0, "🟢", "折价 — 极罕见"
    elif mnav < 1.5:
        score, color, label = 0.5, "🟢", "低溢价 — 偏低估"
    elif mnav < 2.0:
        score, color, label = 0.0, "🟡", "正常溢价"
    elif mnav < 3.0:
        score, color, label = -0.5, "🟠", "高溢价 — 偏高估"
    else:
        score, color, label = -1.0, "🔴", "极高溢价 — 泡沫风险"

    stale_suffix = f" | ⚠️{'/'.join(stale_notes)}" if stale_notes else ""
    price_part = f"MSTR ${mstr_price:.1f} | " if mstr_price is not None else ""
    return IndicatorResult(
        name="MSTR mNAV",
        value=round(mnav, 2),
        score=score, color=color,
        status=(f"{price_part}BTC NAV ${btc_nav/1e9:.1f}B | "
                f"{mnav:.2f}x {label}{stale_suffix}"),
        priority="P0",
        url="https://saylortracker.com/",
        description=_desc, method=_method
    )


def calc_company_holdings() -> IndicatorResult:
    """
    上市公司持仓
    - 数据源: CoinGecko
    """
    holdings, status_text = fetch_company_holdings_data()

    # API 失败时 fetch 返回 (0.0, "API 暂不可用") — 必须以 NaN 剔除,
    # 不能带着 value=0.0 + 正分挂绿灯 (2026-07 对抗性审查修复: 幻觉分)
    if not holdings or holdings <= 0:
        return IndicatorResult(
            name="公司持仓", value=float('nan'), score=0, color="⚪",
            status=status_text or "API 暂不可用", priority="P2",
            url="https://bitcointreasuries.net")

    # 评分: 持续增长为利好
    # 这里简单判断是否有数据
    if holdings > 300000:
        score, color = 1, "🟢"
    else:
        score, color = 0.5, "🟢"

    return IndicatorResult(
        name="公司持仓",
        value=holdings,
        score=score,
        color=color,
        status=status_text,
        priority="P2",
        url="https://bitcointreasuries.net"
    )


def calc_exchange_reserve() -> IndicatorResult:
    """
    交易所BTC余额 — 通过 mempool.space 免费API查询已知交易所冷钱包地址
    - 余额减少 → BTC流出交易所 → 用户在吸筹/自托管 (Bullish)
    - 余额增加 → BTC流入交易所 → 潜在卖压 (Bearish)
    - 数据源: mempool.space (完全免费, 无需API Key)
    """
    import time as _time
    
    EXCHANGE_WALLETS = {
        "Binance": [
            "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo",           # Binance cold wallet 1 (~248K BTC)
            "bc1qm34lsc65zpw79lxes69zkqmk6ee3ewf0j77s3h",    # Binance cold wallet 2 (~21K BTC)
            "3M219KR5vEneNb47ewrPfWyb5jQ2DjxRP6",           # Binance cold wallet 3 (~171K BTC)
            "1NDyJtNTjmwk5xPNhjgAMu4HDHigtobu1s",           # Binance 7
            "39884E3j6KZj82FK4vcCrkUvWYL5MQaS3v",           # Binance 8
        ],
        "Bitfinex": [
            "bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97",  # ~130K BTC
        ],
        "Kraken": [
            "bc1qr4dl5wa7kl8yu792dceg9z5knl2gkn220lk7a9",    # ~18K BTC
            "3AfSMeESFHT2xLqkR1ufoKcxNqNP5bfcaX",           # Kraken cold 2
        ],
        "Crypto.com": [
            "bc1qpy4jwethqenp4r7hqls660wy8287vw0my32lmy",    # 官方公布
            "bc1q4c8n5t00jmj8temxdgcc3t32nkg2wjwz24lywv",    # 官方公布 (~3.9K BTC)
        ],
        "Gemini": [
            "3JZq4atUahhuA9rLhXLMhhTo133J9rF97j",           # Gemini cold
        ],
    }
    
    total_btc = 0
    exchange_details = {}
    success_count = 0
    error_count = 0
    
    try:
        for exchange, addrs in EXCHANGE_WALLETS.items():
            exchange_total = 0
            for addr in addrs:
                try:
                    resp = requests.get(
                        f"https://mempool.space/api/address/{addr}",
                        timeout=8,
                        headers={"User-Agent": "Mozilla/5.0"}
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        chain = data.get("chain_stats", {})
                        funded = chain.get("funded_txo_sum", 0)
                        spent = chain.get("spent_txo_sum", 0)
                        balance = (funded - spent) / 1e8
                        exchange_total += balance
                        success_count += 1
                    elif resp.status_code == 429:
                        # Rate limited, skip remaining
                        print(f"⚠️ mempool.space rate limit, 已获取 {success_count} 个地址")
                        break
                    else:
                        error_count += 1
                except Exception:
                    error_count += 1
                _time.sleep(0.4)  # Rate limit: 250 req/min
            
            if exchange_total > 0:
                exchange_details[exchange] = exchange_total
                total_btc += exchange_total
        
        if success_count < 3:
            return IndicatorResult(
                name="交易所余额",
                value=float('nan'),
                score=0,
                color="⚪",
                status="API 连接失败 (mempool.space)",
                priority="P2",
                url="https://mempool.space",
                description="通过 mempool.space 查询已知交易所冷钱包地址余额。",
                method="因网络问题无法连接 mempool.space API。"
            )
        
        # 格式化各交易所明细
        details_str = " | ".join([
            f"{name} {btc/1000:.0f}K"
            for name, btc in sorted(exchange_details.items(), key=lambda x: -x[1])
        ])
        
        # 历史对比：保存/读取上次余额快照用于趋势判断
        import json, os
        snapshot_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "btc_web", "exchange_balance_history.json")
        
        prev_total = None
        try:
            if os.path.exists(snapshot_file):
                with open(snapshot_file, "r") as f:
                    history = json.load(f)
                if history:
                    prev_total = history[-1].get("total", None)
        except Exception:
            pass
        
        # 保存当前快照
        try:
            history = []
            if os.path.exists(snapshot_file):
                with open(snapshot_file, "r") as f:
                    history = json.load(f)
            
            history.append({
                "timestamp": datetime.now().isoformat(),
                "total": total_btc,
                "details": exchange_details
            })
            # 只保留最近30条记录
            history = history[-30:]
            
            with open(snapshot_file, "w") as f:
                json.dump(history, f, indent=2)
        except Exception:
            pass

        # 评分逻辑
        total_k = total_btc / 1000
        
        if prev_total:
            change = total_btc - prev_total
            change_pct = (change / prev_total) * 100
            
            if change_pct < -2:
                score, color = 1, "🟢"
                trend = f"流出 {abs(change):,.0f} BTC ↓"
            elif change_pct < -0.5:
                score, color = 0.5, "🟢"
                trend = f"小幅流出 {abs(change):,.0f} BTC ↓"
            elif change_pct > 2:
                score, color = -1, "🔴"
                trend = f"流入 {change:,.0f} BTC ↑"
            elif change_pct > 0.5:
                score, color = -0.5, "🟠"
                trend = f"小幅流入 {change:,.0f} BTC ↑"
            else:
                score, color = 0, "🟡"
                trend = "余额稳定 →"
            
            status = f"{total_k:.0f}K BTC | {trend}"
        else:
            # 首次运行，无历史对比
            score, color = 0, "🟡"
            status = f"{total_k:.0f}K BTC | 首次采集"
        
        return IndicatorResult(
            name="交易所余额",
            value=round(total_btc, 2),
            score=score,
            color=color,
            status=f"{status} | {details_str}",
            priority="P2",
            url="https://mempool.space",
            description=(
                f"监控主要交易所冷钱包BTC余额（{len(EXCHANGE_WALLETS)}家交易所，{sum(len(v) for v in EXCHANGE_WALLETS.values())}个地址）。"
                f"余额减少表示用户提币自托管（看多），余额增加表示潜在卖压（看空）。"
            ),
            method=(
                f"通过 mempool.space 免费API查询已知交易所冷钱包链上余额。"
                f"当前监控: {', '.join(EXCHANGE_WALLETS.keys())}。"
                f"对比上次采集数据计算净流入/流出趋势。"
            )
        )
        
    except Exception as e:
        print(f"⚠️ Exchange Reserve Failed: {e}")
        return IndicatorResult(
            name="交易所余额",
            value=float('nan'),
            score=0,
            color="⚪",
            status="数据获取失败",
            priority="P2",
            url="https://mempool.space",
            description="通过 mempool.space 查询已知交易所冷钱包地址余额。",
            method="因异常无法获取数据。"
        )



def calc_max_pain() -> IndicatorResult:
    """
    BTC 期权最大痛点 (Max Pain)
    - 数据源: Deribit (Real-time Option Chain)
    - 逻辑: 选取持仓量(OI)最大的到期日，计算 Call/Put 归零最痛点位
    - 意义: 临近交割时，价格往往向痛点移动
    """
    try:
        # 1. 获取 Deribit 所有期权数据
        response = requests.get(
            "https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
            params={"currency": "BTC", "kind": "option"},
            timeout=10
        )
        if response.status_code != 200:
            raise Exception(f"API Error {response.status_code}")
            
        data = response.json().get("result", [])
        if not data:
            raise Exception("No data returned")
            
        # 2. 整理数据，找到 active exps
        # 格式: BTC-29MAR24-60000-C
        options = []
        for item in data:
            parts = item["instrument_name"].split("-")
            if len(parts) == 4 and item.get("open_interest", 0) > 0:
                options.append({
                    "expiry": parts[1],
                    "strike": float(parts[2]),
                    "type": parts[3], # C or P
                    "oi": item["open_interest"]
                })
        
        if not options:
            raise Exception("No active options found")
            
        df = pd.DataFrame(options)
        
        # 3. 找到 OI 最大的到期日 (主力合约)
        top_expiry = df.groupby("expiry")["oi"].sum().idxmax()
        df_exp = df[df["expiry"] == top_expiry]
        
        # 4. 计算 Max Pain
        strikes = sorted(df_exp["strike"].unique())
        pain_data = []
        
        for price in strikes:
            total_pain = 0
            # Call Pain: if Price > Strike, Pain = (Price - Strike) * OI
            # Put Pain: if Price < Strike, Pain = (Strike - Price) * OI
            
            calls = df_exp[df_exp["type"] == "C"]
            puts = df_exp[df_exp["type"] == "P"]
            
            # 向量化计算加速
            # Call Pain
            itm_calls = calls[calls["strike"] < price]
            if not itm_calls.empty:
                total_pain += ((price - itm_calls["strike"]) * itm_calls["oi"]).sum()
                
            # Put Pain
            itm_puts = puts[puts["strike"] > price]
            if not itm_puts.empty:
                total_pain += ((itm_puts["strike"] - price) * itm_puts["oi"]).sum()
                
            pain_data.append((price, total_pain))
            
        best_strike, min_pain = min(pain_data, key=lambda x: x[1])
        
        # 状态描述
        # 简单给个中性评分，重点展示价格
        return IndicatorResult(
            name=f"最大痛点({top_expiry})",
            value=best_strike,
            score=0,
            color="🟡", # 中性颜色，作为参考位
            status=f"痛点价格 ${best_strike:,.0f}",
            priority="P1",
            url="https://www.deribit.com/statistics/BTC/options-open-interest"
        )

    except Exception as e:
        print(f"⚠️ Max Pain Calc Failed: {e}")
        return IndicatorResult(
            name="最大痛点",
            value=float('nan'),
            score=0,
            color="⚪",
            status="API 暂不可用",
            priority="P1"
        )


