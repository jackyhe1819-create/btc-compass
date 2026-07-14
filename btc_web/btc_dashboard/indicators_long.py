#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.indicators_long
=============================
长期周期指标：2-Year MA、200W Heatmap、Golden Ratio、Pi Cycle、LTH、Hashrate、
Balanced、Halving、Ahr999、Power Law、Mayer。
"""

import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta, timezone
from typing import Tuple, Dict, Optional

from .core import (
    IndicatorResult,
    GENESIS_DATE, HALVING_DATES, NEXT_HALVING_ESTIMATE,
    POWER_LAW_INTERCEPT, POWER_LAW_SLOPE,
    AHR999_A, AHR999_B,
)


def calc_two_year_ma_multiplier(df: pd.DataFrame) -> IndicatorResult:
    """
    2-Year MA Multiplier (2年均线乘数)
    - 绿线: 2年移动平均线 (730日线) -> 世代买点
    - 红线: 2年均线 x 5倍 -> 世代卖点
    """
    if df.empty or len(df) < 730:
        return IndicatorResult(name="2-Year MA Mult", value=0, score=0, color="⚪", status="数据不足", priority="P0")

    current_price = df['price'].iloc[-1]
    
    # 计算 MA730 (2 Year MA)
    # 确保使用足够的历史数据
    ma2y = df['price'].rolling(window=730).mean().iloc[-1]
    ma2y_x5 = ma2y * 5
    
    # 状态判断
    if current_price < ma2y:
        score = 1
        color = "🟢"
        status = f"低于绿线 (${ma2y:,.0f}) - 世代抄底"
    elif current_price > ma2y_x5:
        score = -1
        color = "🔴"
        status = f"高于红线 (${ma2y_x5:,.0f}) - 世代逃顶"
    elif current_price < ma2y * 1.5:
        score = 0.5
        color = "🟢"
        status = f"接近买入区 (${ma2y:,.0f})"
    elif current_price > ma2y_x5 * 0.8:
        score = -0.5
        color = "🟠"
        status = f"接近卖出区 (${ma2y_x5:,.0f})"
    else:
        score = 0
        color = "🟡"
        status = "区间震荡"
        
    return IndicatorResult(
        name="2-Year MA Mult",
        value=current_price / ma2y,  # 返回倍数作为 Value
        score=score,
        color=color,
        status=status,
        priority="P0",
        url="https://www.lookintobitcoin.com/charts/bitcoin-investor-tool/",
        description="2年均线乘数指标用于识别比特币市场周期中的买卖机会。",
        method="由2年移动平均线（730日线）及其5倍线构成。价格低于2年均线为买入区，高于5倍线为卖出区。"
    )


def calc_200w_ma_heatmap(df: pd.DataFrame) -> IndicatorResult:
    """
    200-Week MA Heatmap (200周均线热力图)
    - 200周均线 (1400天) 是比特币的历史绝对底部
    - 颜色根据价格偏离度变化
    """
    if df.empty or len(df) < 1400:
        # value 必须 NaN: 计分成员返回 value=0 会以"伪中性票"留在桶内稀释
        # 其余成员 (scoring 以 value 非 NaN 判定在场, 2026-07 审计遗留批修复)
        return IndicatorResult(name="200-Week Heatmap", value=float('nan'), score=0,
                               color="⚪", status="数据不足 (需1400天)", priority="P0")

    current_price = df['price'].iloc[-1]
    
    # 计算 MA1400 (200 Week MA)
    ma200w = df['price'].rolling(window=1400).mean().iloc[-1]
    
    # 计算涨幅百分比
    pct_diff = (current_price - ma200w) / ma200w
    
    # 评分逻辑 (基于历史涨幅分布, 假设 +15%以内为底部, >300%为顶部)
    if pct_diff < 0.15:
        score = 1
        color = "🟢" # 极冷/买入 (Blue/Purple equivalent)
        status = f"触底区 (+{pct_diff*100:.1f}%)"
    elif pct_diff < 0.5:
        score = 0.5
        color = "🟢"
        status = f"低估区 (+{pct_diff*100:.1f}%)"
    elif pct_diff > 3.0: # >300%
        score = -1
        color = "🔴"
        status = f"极热区 (+{pct_diff*100:.0f}%)"
    elif pct_diff > 1.5:
        score = -0.5
        color = "🟠"
        status = f"过热区 (+{pct_diff*100:.0f}%)"
    else:
        score = 0
        color = "🟡"
        status = f"中性区 (+{pct_diff*100:.0f}%)"
        
    return IndicatorResult(
        name="200-Week Heatmap",
        value=pct_diff * 100, # Value as Percentage
        score=score,
        color=color,
        status=status,
        priority="P0",
        url="https://www.lookintobitcoin.com/charts/200-week-moving-average-heatmap/",
        description="200周均线热力图通过价格与200周均线的偏离程度来判断市场冷热。",
        method="200周移动平均线（约1400天）被认为是比特币的长期支撑。价格偏离该均线的百分比越高，市场越热。"
    )


def calc_golden_ratio_multiplier(df: pd.DataFrame) -> IndicatorResult:
    """
    Golden Ratio Multiplier (黄金比例乘数)
    - Base: 350 DMA
    - Multipliers: 1.6, 2.0, 3.0
    """
    if df.empty or len(df) < 350:
         return IndicatorResult(name="Golden Ratio", value=0, score=0, color="⚪", status="数据不足", priority="P1")

    current_price = df['price'].iloc[-1]
    ma350 = df['price'].rolling(window=350).mean().iloc[-1]
    
    # 关键位
    x1_6 = ma350 * 1.6
    x2_0 = ma350 * 2.0
    x3_0 = ma350 * 3.0
    
    # 状态判断
    if current_price > x3_0:
        score = -1
        color = "🔴"
        status = "突破 x3.0 (顶部风险)"
    elif current_price > x2_0:
        score = -0.5
        color = "🟠"
        status = "突破 x2.0 (FOMO区)"
    elif current_price > x1_6:
        score = 0.5
        color = "🟢"  # 突破黄金分割往往是牛市确认，但也意味着稍微脱离底部
        # 修正: 或者是"中性偏热"。但在牛市启动初期，突破1.6是强烈看涨信号。
        # 考虑到这是"周期逃顶"指标，越高越危险。
        score = 0 # 中性
        color = "🟡"
        status = "突破 x1.6 (牛市通过)"
    elif current_price < ma350:
        score = 1
        color = "🟢"
        status = "低于 350DMA (底部)"
    else:
        score = 1
        color = "🟢"
        status = "350DMA ~ x1.6 (吸筹区)"
        
    return IndicatorResult(
        name="Golden Ratio",
        value=current_price / ma350, # Value as multiple of 350DMA
        score=score,
        color=color,
        status=status,
        priority="P1",
        url="https://www.lookintobitcoin.com/charts/golden-ratio-multiplier/",
        description="黄金比例乘数通过350日均线及其倍数来识别市场周期中的关键支撑和阻力位。",
        method="以350日均线为基准，结合1.6、2.0、3.0等黄金比例乘数，判断价格所处的市场阶段。"
    )


def calc_pi_cycle(df: pd.DataFrame) -> IndicatorResult:
    """
    Pi Cycle Top 指标
    - 111DMA 与 350DMA×2 的关系
    """
    df = df.copy()
    df['ma111'] = df['price'].rolling(window=111).mean()
    df['ma350x2'] = df['price'].rolling(window=350).mean() * 2
    
    latest = df.iloc[-1]
    ma111 = latest['ma111']
    ma350x2 = latest['ma350x2']
    
    # 计算差距百分比
    gap_pct = (ma350x2 - ma111) / ma350x2 * 100
    
    # 评分逻辑 — 顶部探测器只有 {0, -0.5, -1} 三态:
    # "远离交叉"是无信号而非看多信号。旧版 gap>20% 给 +1, 导致熊市下跌途中
    # (跌得越深 gap 越大) 该指标反而打满分看多 (2026-07 对抗性审查修复)
    if ma111 >= ma350x2:
        score, color, status = -1, "🔴", f"已交叉! 顶部信号"
    elif gap_pct <= 20:
        score, color, status = -0.5, "🟠", f"差距 {gap_pct:.1f}%, 接近交叉"
    else:
        score, color, status = 0, "🟡", f"差距 {gap_pct:.1f}%, 远离顶部(不计分)"

    return IndicatorResult(
        name="Pi Cycle Top",
        value=gap_pct,
        score=score,
        color=color,
        status=status,
        priority="P0",
        url="https://www.lookintobitcoin.com/charts/pi-cycle-top-indicator/",
        description="Pi Cycle Top 指标用于识别比特币市场周期的顶部，历史上准确率极高。它是单向的顶部探测器：远离交叉只代表'没有顶部信号'，不构成看多依据。",
        method="由两条移动平均线组成：111日均线 (111DMA) 和 350日均线的两倍 (350DMA x 2)。111DMA 上穿 350DMA x 2 → -1（顶部信号）；差距≤20% → -0.5（接近交叉）；其余 → 0（无信号，不计分）。"
    )


def _fetch_180d_btc_volumes_usd():
    """
    获取 BTC 过去 180 天日成交量（USD）。
    主源: CoinGecko market_chart  / 备源: CryptoCompare histoday
    返回: list[float] 或 None
    """
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

    # 主源 CoinGecko
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
            "?vs_currency=usd&days=180&interval=daily",
            timeout=10, headers=headers,
        )
        if r.status_code == 200:
            vols = r.json().get('total_volumes', [])
            if len(vols) >= 100:
                return [v[1] for v in vols], "CoinGecko"
        else:
            print(f"⚠️ CoinGecko Volume 返回 {r.status_code}")
    except Exception as e:
        print(f"⚠️ CoinGecko Volume 失败: {e}")

    # 备源 CryptoCompare
    try:
        r = requests.get(
            "https://min-api.cryptocompare.com/data/v2/histoday"
            "?fsym=BTC&tsym=USD&limit=180",
            timeout=10, headers=headers,
        )
        if r.status_code == 200:
            data = (r.json() or {}).get('Data', {}).get('Data', [])
            if len(data) >= 100:
                # volumeto 是 USD 计价的成交量
                return [d.get('volumeto', 0) for d in data], "CryptoCompare"
        else:
            print(f"⚠️ CryptoCompare Volume 返回 {r.status_code}")
    except Exception as e:
        print(f"⚠️ CryptoCompare Volume 失败: {e}")

    return None, ""


def calc_lth_supply() -> IndicatorResult:
    """
    长期持有者行为 (Proxy: 成交量趋势分析)
    - 主源: CoinGecko / 备源: CryptoCompare
    - 逻辑: 成交量低迷/下降 => 吸筹 (Bullish)；爆发/上升 => 派发 (Bearish)
    - 算法: 7日成交量均值 vs 90日成交量均值的比率
    """
    vol_values, src = _fetch_180d_btc_volumes_usd()

    if not vol_values:
        return IndicatorResult(
            name="长期持有者(CDD)",
            value=0,
            score=0,
            color="⚪",
            status="数据源连接失败",
            priority="P0",
            url="https://www.coinglass.com/pro/i/bitcoin-cdd",
            description="使用成交量趋势分析作为长期持有者行为的代理指标。",
            method="主源 CoinGecko + 备源 CryptoCompare 均失败。"
        )

    df_vol = pd.DataFrame({'volume': vol_values})
    sma7 = df_vol['volume'].rolling(window=7).mean().iloc[-1]
    sma90 = df_vol['volume'].rolling(window=90).mean().iloc[-1]
    vol_ratio = sma7 / sma90 if sma90 > 0 else 1.0
    vol_billion = sma7 / 1e9

    if vol_ratio < 0.7:
        score, color = 1, "🟢"
        status = f"深度吸筹 (量比 {vol_ratio:.2f})"
    elif vol_ratio < 0.9:
        score, color = 0.5, "🟢"
        status = f"吸筹中 (量比 {vol_ratio:.2f})"
    elif vol_ratio > 2.0:
        score, color = -1, "🔴"
        status = f"大量派发 (量比 {vol_ratio:.2f})"
    elif vol_ratio > 1.5:
        score, color = -0.5, "🟠"
        status = f"轻微派发 (量比 {vol_ratio:.2f})"
    else:
        score, color = 0, "🟡"
        status = f"持币观望 (量比 {vol_ratio:.2f})"

    return IndicatorResult(
        name="长期持有者(CDD)",
        value=round(vol_ratio, 2),
        score=score,
        color=color,
        status=f"{status} | {vol_billion:.1f}B$/日",
        priority="P0",
        url="https://www.coinglass.com/pro/i/bitcoin-cdd",
        description="使用成交量趋势分析作为长期持有者行为的代理指标。低成交量通常意味着长期持有者在吸筹，高成交量可能意味着派发。",
        method=f"BTC 近7日均成交量 vs 90日均的比率。量比 < 0.8 = 吸筹（看多），> 1.5 = 可能派发（看空）。本次数据源: {src}。"
    )


def calc_hashrate() -> IndicatorResult:
    """
    全网算力 (Network Hashrate)
    - 主源: mempool.space (3d avg)
    - 备源: blockchain.info chart API
    - 单位: EH/s (Exahash per second)
    """
    hashrate_ehs = None

    # 主源：mempool.space — 返回 hashrates[].avgHashrate, 单位 H/s
    try:
        r = requests.get(
            "https://mempool.space/api/v1/mining/hashrate/3d",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code == 200:
            arr = (r.json() or {}).get("hashrates", [])
            if arr:
                # 取最近一个点的 avgHashrate（H/s）→ EH/s
                avg_hs = arr[-1].get("avgHashrate")
                if avg_hs:
                    hashrate_ehs = float(avg_hs) / 1e18
    except Exception as e:
        print(f"⚠️ mempool.space hashrate 失败: {e}")

    # 备源：blockchain.info charts API (单位 TH/s)
    if hashrate_ehs is None:
        try:
            r = requests.get(
                "https://api.blockchain.info/charts/hash-rate?timespan=3days&format=json",
                timeout=10,
            )
            if r.status_code == 200:
                vals = (r.json() or {}).get("values", [])
                if vals:
                    # values[].y 单位 TH/s → EH/s
                    hashrate_ths = float(vals[-1].get("y", 0))
                    if hashrate_ths > 0:
                        hashrate_ehs = hashrate_ths / 1_000_000
        except Exception as e:
            print(f"⚠️ blockchain.info hashrate 备源失败: {e}")

    if hashrate_ehs is None:
        return IndicatorResult(
            name="全网算力",
            value=float('nan'),
            score=0,
            color="⚪",
            status="API 暂不可用",
            priority="P2",
            url="https://mempool.space/graphs/mining/hashrate-difficulty",
        )

    # 对照一年真实峰值打标 — 旧版 ">800 EH/s 即报'历史新高'+1" 在算力从峰值
    # 回落 37% 的矿工投降期仍显示新高, 方向性误导 (2026-07 审计遗留批修复)
    peak_1y = None
    try:
        from .indicators_v2 import _cached_onchain

        def _fetch_1y_peak():
            r2 = requests.get("https://mempool.space/api/v1/mining/hashrate/1y",
                              timeout=12, headers={"User-Agent": "Mozilla/5.0"})
            if r2.status_code != 200:
                return None
            pts = [float(x.get("avgHashrate", 0)) for x in (r2.json() or {}).get("hashrates", [])]
            return max(pts) / 1e18 if pts else None

        peak_1y = _cached_onchain("hashrate-1y-peak", _fetch_1y_peak)
    except Exception as e:
        print(f"⚠️ 一年算力峰值获取失败: {e}")

    if peak_1y and peak_1y > 0:
        dist = hashrate_ehs / peak_1y - 1
        if dist >= -0.02:
            score, color = 0.5, "🟢"
            status = f"{hashrate_ehs:.1f} EH/s (逼近一年峰值)"
        elif dist >= -0.15:
            score, color = 0, "🟡"
            status = f"{hashrate_ehs:.1f} EH/s (较一年峰值 {dist:+.0%})"
        else:
            score, color = 0, "🟠"
            status = f"{hashrate_ehs:.1f} EH/s (较一年峰值 {dist:+.0%} — 算力收缩期)"
    else:
        score, color = 0, "🟡"
        status = f"{hashrate_ehs:.1f} EH/s (峰值参照不可用)"

    return IndicatorResult(
        name="全网算力",
        value=hashrate_ehs,
        score=score,
        color=color,
        status=status,
        priority="P2",
        url="https://mempool.space/graphs/mining/hashrate-difficulty",
        description="全网算力是衡量比特币网络安全性和矿工投入程度的指标。",
        method="主源 mempool.space (3d 平均) → 备源 blockchain.info charts API。标签对照一年真实峰值（mempool 1y, 6h 缓存）：距峰值 ≤2% 为逼近峰值，回落 >15% 标注算力收缩期。"
    )


def calc_balanced_price(df: pd.DataFrame) -> IndicatorResult:
    """
    均衡价格 (Balanced Price)
    - 公式: Balanced Price = Realized Price - Transfer Price
    - 简化版: 使用 150日均线 与 350日均线 的中值作为近似
    """
    if df is None or len(df) < 350:
        return IndicatorResult(
            name="均衡价格",
            value=float('nan'),
            score=0,
            color="⚪",
            status="数据不足",
            priority="P1"
        )
    
    current_price = df['price'].iloc[-1]
    
    # 简化计算：使用 150日和 350日移动平均的均值
    ma_150 = df['price'].rolling(window=150).mean().iloc[-1]
    ma_350 = df['price'].rolling(window=350).mean().iloc[-1]
    balanced_price = (ma_150 + ma_350) / 2
    
    # 计算当前价格相对于均衡价格的倍数
    ratio = current_price / balanced_price if balanced_price > 0 else 0
    
    # 评分逻辑
    if ratio < 1.0:
        score, color = 1, "🟢"
        status = f"${balanced_price:,.0f} | 当前 {ratio:.2f}x (低于均衡)"
    elif ratio < 1.5:
        score, color = 0.5, "🟢"
        status = f"${balanced_price:,.0f} | 当前 {ratio:.2f}x (正常偏低)"
    elif ratio < 2.0:
        score, color = 0, "🟡"
        status = f"${balanced_price:,.0f} | 当前 {ratio:.2f}x (正常区间)"
    elif ratio < 3.0:
        score, color = -0.5, "🟠"
        status = f"${balanced_price:,.0f} | 当前 {ratio:.2f}x (偏高)"
    else:
        score, color = -1, "🔴"
        status = f"${balanced_price:,.0f} | 当前 {ratio:.2f}x (严重高估)"
    
    return IndicatorResult(
        name="均衡价格",
        value=balanced_price,
        score=score,
        color=color,
        status=status,
        priority="P1",
        url="https://charts.bitbo.io/balanced-price/",
        description="均衡价格是衡量比特币公允价值的链上指标，通常被视为市场底部。",
        method="简化计算为150日均线和350日均线的平均值。价格低于均衡价格被认为是低估，高于则为高估。"
    )


def calc_halving_cycle() -> IndicatorResult:
    """
    减半周期位置
    - 计算距离上次减半的月数
    - 包含进度百分比用于进度条显示
    """
    today = datetime.now()
    
    # 找到最近的减半日期
    past_halvings = [d for d in HALVING_DATES if d <= today]
    last_halving = past_halvings[-1] if past_halvings else HALVING_DATES[0]
    
    # 下一次减半预计日期 — 与 core.py 唯一事实源对齐, 不再现算 last+4*365 (曾造成同页三个倒计时)
    next_halving = NEXT_HALVING_ESTIMATE

    # 计算距离上次减半的月数
    months_since = (today - last_halving).days / 30.44

    # 计算距离下次减半的天数和进度
    days_until_next = (next_halving - today).days
    total_cycle_days = max(1, (next_halving - last_halving).days)
    progress_pct = min(100, ((total_cycle_days - days_until_next) / total_cycle_days) * 100)
    
    # 评分逻辑
    if months_since <= 12:
        score, color = 1, "🟢"
        status_text = f"减半后 {months_since:.0f} 个月 (牛市起点)"
    elif months_since <= 24:
        score, color = 0, "🟡"
        status_text = f"减半后 {months_since:.0f} 个月 (周期中期)"
    else:
        score, color = -1, "🔴"
        status_text = f"减半后 {months_since:.0f} 个月 (周期后期)"
    
    # 添加倒计时信息
    status = f"{status_text} | 下次约 {days_until_next} 天"
    
    return IndicatorResult(
        name="减半周期",
        value=months_since,
        score=score,
        color=color,
        status=status,
        priority="P0",
        url="https://www.coinglass.com/halving",
        description="比特币减半是其经济模型的核心事件，大约每四年发生一次，通常预示着牛市的到来。",
        method="根据比特币历史减半日期，计算当前所处的减半周期阶段。减半后12个月内通常是牛市早期，24个月后可能进入周期后期。"
    )


def calc_ahr999(df: pd.DataFrame) -> IndicatorResult:
    """
    Ahr999 指数 (九神囤币指标)
    
    正确公式：AHR999 = (BTC价格 / 200日定投成本) × (BTC价格 / 指数增长估值)
    
    - 200日定投成本：过去200天每天定投的平均成本
    - 指数增长估值：10^(5.84 × log10(币龄) - 17.01)
    
    阈值解读：
    - < 0.45: 抄底区 (极佳买入机会)
    - 0.45 - 1.2: 定投区 (适合定投)
    - > 1.2: 止盈区 (考虑获利了结)
    """
    # 获取最近200天的价格数据
    recent_200 = df['price'].tail(200)
    
    # 当前价格
    current_price = df['price'].iloc[-1]
    
    # 200日定投成本 (使用几何平均，Coinglass/TradingView 标准算法)
    # Geometric Mean = exp(mean(log(x)))
    try:
        # 使用 Numpy 计算几何平均 (更稳定且无需 Scipy)
        dca_cost_200 = np.exp(np.mean(np.log(recent_200)))
    except Exception as e:
        print(f"⚠️ Ahr999 Cost Calc Failed: {e}")
        dca_cost_200 = recent_200.mean() # Fallback to arithmetic
    
    # 计算币龄 (比特币诞生天数)
    today = datetime.now()
    days_since_genesis = (today - GENESIS_DATE).days
    
    # 九神指数增长估值公式: 10^(5.84 * log10(days) - 17.01)
    # 使用顶部定义的常量
    if days_since_genesis > 0:
        exp_growth_value = 10 ** (AHR999_B * np.log10(days_since_genesis) + AHR999_A)
    else:
        exp_growth_value = 1.0
    
    # AHR999 公式
    if dca_cost_200 > 0 and exp_growth_value > 0:
        ahr999 = (current_price / dca_cost_200) * (current_price / exp_growth_value)
        # DEBUG: Print calculation details
        print(f"\n[AHR999 DEBUG]")
        print(f"  Price: {current_price:.2f}")
        print(f"  Days: {days_since_genesis}")
        print(f"  Cost(200d GeoMean): {dca_cost_200:.2f}")
        print(f"  Fair Value (Exp): {exp_growth_value:.2f}")
        print(f"  Part 1 (P/Cost): {current_price/dca_cost_200:.4f}")
        print(f"  Part 2 (P/Fair): {current_price/exp_growth_value:.4f}")
        print(f"  Result: {ahr999:.4f}\n")
    else:
        ahr999 = 1.0
    if ahr999 < 0.45:
        score, color, status = 1, "🟢", f"抄底区 ({ahr999:.2f})"
    elif ahr999 < 1.2:
        score, color, status = 0, "🟡", f"定投区 ({ahr999:.2f})"
    else:
        score, color, status = -1, "🔴", f"止盈区 ({ahr999:.2f})"
    
    return IndicatorResult(
        name="Ahr999",
        value=ahr999,
        score=score,
        color=color,
        status=status,
        priority="P0",
        url="https://www.coinglass.com/pro/i/ahr999",
        description="Ahr999 指数用于辅助比特币定投和抄底，评估价格是否处于低估区间。",
        method="Ahr999 = (价格/200日定投成本) * (价格/指数增长估值)。< 0.45 抄底，0.45-1.2 定投，> 1.25 起飞。"
    )



def calc_power_law(df: pd.DataFrame) -> IndicatorResult:
    """
    幂律走廊位置
    - 计算当前价格相对于幂律中轨的位置
    """
    today = datetime.now()
    days_since_genesis = (today - GENESIS_DATE).days
    
    # 计算幂律中轨价格
    log_fair_value = POWER_LAW_INTERCEPT + POWER_LAW_SLOPE * np.log10(days_since_genesis)
    fair_value = 10 ** log_fair_value
    
    # 上下轨 (约 ±0.5 log 单位)
    upper_band = 10 ** (log_fair_value + 0.5)
    lower_band = 10 ** (log_fair_value - 0.5)
    
    current_price = df['price'].iloc[-1]
    
    # 计算相对位置 (-1 到 +1)
    if current_price < fair_value:
        position = (current_price - lower_band) / (fair_value - lower_band) - 1
    else:
        position = (current_price - fair_value) / (upper_band - fair_value)
    
    # 评分逻辑
    if current_price < lower_band:
        score, color, status = 1, "🟢", f"低于下轨 (${current_price:,.0f} < ${lower_band:,.0f})"
    elif current_price > upper_band:
        score, color, status = -1, "🔴", f"高于上轨 (${current_price:,.0f} > ${upper_band:,.0f})"
    else:
        score, color, status = 0, "🟡", f"通道内 (中轨 ${fair_value:,.0f})"
    
    return IndicatorResult(
        name="幂律走廊",
        value=position,
        score=score,
        color=color,
        status=status,
        priority="P1",
        url="https://charts.bitbo.io/power-law-corridor/",
        description="比特币幂律走廊模型，展示价格长期遵循的对数增长规律。",
        method="价格 = a * (天数 ^ b)。价格通常在支撑线和阻力线构成的通道内波动。偏离底部支撑线过远为低估，接近顶部阻力线为高估。"
    )


def calc_mayer_multiple(df: pd.DataFrame) -> IndicatorResult:
    """
    Mayer Multiple (梅耶倍数)
    - 价格 / 200日均线
    - 替代 MVRV Z-Score (因 API 不稳定)
    - < 0.8 低估, > 2.4 高估
    """
    df = df.copy()
    # 确保有足够数据计算 200MA
    if len(df) < 200:
         return IndicatorResult(
            name="Mayer Multiple",
            value=float('nan'),
            score=0,
            color="⚪",
            status="数据不足",
            priority="P0"
        )
        
    df['ma200'] = df['price'].rolling(window=200).mean()
    
    latest = df.iloc[-1]
    mm = latest['price'] / latest['ma200']
    
    # 评分逻辑
    if mm < 0.6:
        score, color, status = 1, "🟢", f"极度低估 ({mm:.2f}) - 抄底"
    elif mm < 1.1:
        score, color, status = 0.5, "🟢", f"低估区域 ({mm:.2f})"
    elif mm > 2.4:
        score, color, status = -1, "🔴", f"极度高估 ({mm:.2f}) - 逃顶"
    elif mm > 1.8:
        score, color, status = -0.5, "🟡", f"高估区域 ({mm:.2f})"
    else:
        score, color, status = 0, "🟡", f"合理估值 ({mm:.2f})"
        
    return IndicatorResult(
        name="Mayer Multiple",
        value=mm,
        score=score,
        color=color,
        status=status,
        priority="P0",
        url="https://charts.bitbo.io/mayer-multiple/",
        description="梅耶倍数通过比特币价格与200日移动平均线的比值，评估市场是否处于超买或超卖状态。",
        method="梅耶倍数 = 价格 / 200日均线。通常低于0.6为极度低估，高于2.4为极度高估。"
    )

