#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.scoring
=====================
BTC Compass 双评分引擎。

与原版 (单一加权总分) 的三个核心区别:

1. **双评分输出**
   - 周期分 (Cycle Score): 估值 + 筹码 + 资金流 + 趋势确认 → 回答"该配多少仓位"
   - 战术分 (Tactical Score): 杠杆 + 情绪 + 动量 → 回答"现在是不是好的操作时点"

2. **因子分桶去相关**
   原版 8 个高相关的"价格 vs 均线"变体合占 65% 权重 (同一因子数 8 遍)。
   本版按信息来源分桶, 桶内取均值, 桶间分权重, 单一因子不再被重复计数。

3. **滚动分位数归一化**
   原版用绝对阈值 (如 2Y MA ×5 逃顶), 按 2013/2017 周期振幅校准, 振幅递减后
   顶部永远打不出 -1。本版对可从价格序列推导的指标改用"当前值在过去 4 年的
   分位数"打分, 自适应周期振幅衰减。
"""

import numpy as np
import pandas as pd
from typing import Dict, Tuple

from .core import IndicatorResult, GENESIS_DATE, AHR999_A, AHR999_B

# ============================================================
# 因子分桶配置
# ============================================================

CYCLE_BUCKETS = {
    "趋势伸展": {
        "weight": 0.25,
        # Ahr999 于 2026-06 加入: 虽与桶平均相关 0.92, 但其乘积结构 (Mayer类×幂律)
        # 放大周期极值共振, 回测加入后周期分 IC 全窗口提升 (365d 0.440→0.475)
        # Mayer Multiple 于 2026-07 移出: 其独立分位分 IC 全窗≈0/负 — 价格/MA200
        # 实为动量信号(站上均线历史上延续而非回归), 按"拉伸→看空"反向计分是持续掺入
        # 反信号。留一对照: 移除后 IC365 0.488→0.562 (post21 0.373→0.513),
        # 滞回策略 Sharpe 1.02→1.09, 最大回撤 -48%→-38%; "移入趋势确认桶"变体
        # 更差(Sharpe 0.91)已否决。Mayer 卡片保留展示, 仅不入评分。
        "members": ["200-Week Heatmap", "幂律走廊", "Pi Cycle Top", "Ahr999"],
        "note": "价格相对长期趋势的拉伸程度（分位数归一化）",
    },
    "链上筹码": {
        "weight": 0.25,
        "members": ["MVRV-Z", "STH成本线", "NUPL", "交易所余额"],
        "note": "持有者成本、未实现盈亏与筹码迁移",
    },
    "资金流": {
        "weight": 0.20,
        "members": ["ETF净流入", "稳定币增速"],
        "note": "边际买卖力量（真实净流入，对称打分）",
    },
    "趋势确认": {
        "weight": 0.15,
        "members": ["趋势过滤器"],
        "note": "区分'便宜且企稳'与'便宜但还在跌'",
    },
    "矿工经济": {
        "weight": 0.10,
        "members": ["Puell Multiple", "Hash Ribbons"],
        "note": "结构性卖方的收入周期与投降/恢复信号",
    },
    "时间周期": {
        "weight": 0.05,
        "members": ["减半周期"],
        "note": "减半时钟先验（权重刻意调低）",
    },
}

TACTICAL_BUCKETS = {
    "杠杆温度": {
        "weight": 0.35,
        "members": ["资金费率(7d)", "期货基差", "多空比"],
        "note": "衍生品市场的拥挤度（逆向）",
    },
    "动量结构": {
        "weight": 0.30,
        "members": ["MACD", "RSI(14)", "SOPR", "布林带"],
        "note": "多周期动量 + 链上盈亏兑现（SOPR）",
    },
    "市场情绪": {
        "weight": 0.20,
        "members": ["恐惧贪婪指数"],
        "note": "仅极值计分的逆向情绪",
    },
    # 2026-06 新增: 交易所净流(7d) — CoinMetrics 全市场流量
    # 回测 (2018-2026) 7-14d 前瞻 IC +0.10~+0.13, 高于资金费率因子
    "链上资金流": {
        "weight": 0.15,
        "members": ["交易所净流(7d)"],
        "note": "7日交易所净流向 — 短线供需（流出=买盘提币）",
    },
}

# 桶内成员权重（未列出的成员等权）
MEMBER_WEIGHTS = {
    "链上筹码": {"MVRV-Z": 0.35, "STH成本线": 0.25, "NUPL": 0.20, "交易所余额": 0.20},
    "矿工经济": {"Puell Multiple": 0.6, "Hash Ribbons": 0.4},
    "杠杆温度": {"资金费率(7d)": 0.4, "期货基差": 0.4, "多空比": 0.2},
    "动量结构": {"MACD": 0.35, "RSI(14)": 0.30, "SOPR": 0.25, "布林带": 0.10},
}

# 分位数窗口：4 年（一个完整减半周期）
PERCENTILE_WINDOW = 1460


# ============================================================
# 滚动分位数归一化
# ============================================================

def _percentile_score(series: pd.Series, window: int = PERCENTILE_WINDOW) -> Tuple[float, int]:
    """
    当前值在过去 window 天中的分位数 → 映射到 [-1, +1]。
    分位数越高（相对历史越贵）分数越低（看空）。
    映射: pct=0 → +1, pct=0.5 → 0, pct=1 → -1
    返回 (score, 实际使用的窗口天数)。数据不足时 score=NaN。
    实际窗口 < window 时（价格源降级到短历史备源），调用方必须如实标注,
    不能继续宣称"4年分位" (2026-07 对抗性审查修复)。
    """
    s = series.dropna()
    if len(s) < window // 4:  # 至少一年数据
        return float('nan'), len(s)
    tail = s.tail(window)
    cur = tail.iloc[-1]
    pct = (tail < cur).mean()
    return float((0.5 - pct) * 2), len(tail)


def _percentile_note(pct: float, n_used: int, window: int = PERCENTILE_WINDOW) -> str:
    """分位数展示文本: 满窗口标'4年分位', 短窗口如实标注实际天数。"""
    if n_used >= window:
        return f"4年分位 {pct:.0f}%"
    return f"分位 {pct:.0f}% (⚠️窗口仅{n_used}天, 非4年)"


def compute_percentile_overrides(df: pd.DataFrame) -> Dict[str, Tuple[float, str]]:
    """
    对可从价格序列推导的"趋势伸展类"指标计算分位数评分。
    返回 {指标名: (score, 附加说明)}。
    """
    out = {}
    if df is None or len(df) < 400:
        return out

    price = df['price']

    metrics = {}
    # Mayer Multiple: 价格 / MA200
    metrics["Mayer Multiple"] = price / price.rolling(200).mean()
    # 200W Heatmap: 价格偏离 MA1400 百分比
    if len(df) >= 1400:
        metrics["200-Week Heatmap"] = (price - price.rolling(1400).mean()) / price.rolling(1400).mean()
    # 幂律走廊: 价格 / 时间幂律公允价值
    days = (df.index - pd.Timestamp(GENESIS_DATE)).days.values.astype(float)
    with np.errstate(invalid='ignore', divide='ignore'):
        fair = 10 ** (AHR999_B * np.log10(np.where(days > 0, days, np.nan)) + AHR999_A)
    metrics["幂律走廊"] = pd.Series(price.values / fair, index=df.index)
    # 2-Year MA Mult: 价格 / MA730
    if len(df) >= 730:
        metrics["2-Year MA Mult"] = price / price.rolling(730).mean()
    # Golden Ratio: 价格 / MA350
    metrics["Golden Ratio"] = price / price.rolling(350).mean()
    # 均衡价格: 价格 / ((MA150+MA350)/2)
    metrics["均衡价格"] = price / ((price.rolling(150).mean() + price.rolling(350).mean()) / 2)
    # Ahr999: (价格/200日几何均价) × (价格/幂律估值) — 乘积放大周期极值共振
    geo200 = np.exp(np.log(price).rolling(200).mean())
    metrics["Ahr999"] = (price / geo200) * pd.Series(price.values / fair, index=df.index)

    for name, series in metrics.items():
        sc, n_used = _percentile_score(series)
        if not np.isnan(sc):
            pct = (0.5 - sc / 2) * 100  # 反推分位数百分比, 用于展示
            out[name] = (round(sc, 3), _percentile_note(pct, n_used))
    return out


def apply_percentile_overrides(indicators: Dict[str, IndicatorResult],
                               df: pd.DataFrame) -> None:
    """
    用分位数评分覆盖趋势伸展类指标的离散评分（原地修改）。
    指标卡片上保留原状态文本, 附加分位数说明, 保证卡片与综合分一致。
    """
    overrides = compute_percentile_overrides(df)
    for name, (score, note) in overrides.items():
        ind = indicators.get(name)
        if ind is None or np.isnan(ind.value):
            continue
        ind.score = score
        # 颜色按新分数重定
        if score >= 0.3:
            ind.color = "🟢"
        elif score <= -0.3:
            ind.color = "🔴" if score <= -0.6 else "🟠"
        else:
            ind.color = "🟡"
        if note not in (ind.status or ""):
            ind.status = f"{ind.status} | {note}"


# ============================================================
# 双评分计算
# ============================================================

def _compute_bucket_scores(buckets_cfg: dict,
                           indicators: Dict[str, IndicatorResult]) -> Tuple[float, dict, float]:
    """
    按桶计算加权分。
    - 失败指标 (value=NaN) 直接剔除, 不作为中性票占权重
    - 桶内按 MEMBER_WEIGHTS 或等权, 桶间按配置权重, 缺桶时归一化
    返回 (总分, 桶明细, 覆盖率)
    覆盖率 = 有效桶的配置权重合计 (0~1)。剔除重归一虽合理, 但覆盖率过低时
    评分由极少数因子决定, 纵向不可比 — 调用方应展示并警示。
    """
    total = 0.0
    weight_sum = 0.0
    detail = {}

    for bucket_name, cfg in buckets_cfg.items():
        member_weights = MEMBER_WEIGHTS.get(bucket_name, {})
        acc = 0.0
        w_acc = 0.0
        members_detail = []

        for m in cfg["members"]:
            ind = indicators.get(m)
            if ind is None or np.isnan(ind.value):
                members_detail.append({"name": m, "score": None})
                continue
            w = member_weights.get(m, 1.0)
            acc += ind.score * w
            w_acc += w
            members_detail.append({"name": m, "score": round(float(ind.score), 3)})

        if w_acc > 0:
            bucket_score = acc / w_acc
            total += bucket_score * cfg["weight"]
            weight_sum += cfg["weight"]
        else:
            bucket_score = None

        detail[bucket_name] = {
            "score": round(bucket_score, 3) if bucket_score is not None else None,
            "weight": cfg["weight"],
            "note": cfg["note"],
            "members": members_detail,
        }

    final = total / weight_sum if weight_sum > 0 else 0.0
    return float(final), detail, float(weight_sum)


def cycle_recommendation(score: float) -> str:
    """
    周期分 → 仓位建议。
    阈值按 2014+ 回测评分分布的分位数标定 (2026-07 重标定):
    桶平均机制把量程压缩到约 [-0.5, +0.68], 旧斐波那契阈值 (±0.618/±0.382)
    的极值档 12 年只触发 <1% / 0 天, 档位形同虚设。
    新阈值目标触发频率: 重仓~3% | 偏多~12% | 标准~30% | 中性~28% | 减配~17% | 低配~7% | 防守~3%。
    """
    if score >= 0.45:
        return "重仓区 · 建议仓位 80-100%"
    elif score >= 0.30:
        return "偏多配置 · 建议仓位 60-80%"
    elif score >= 0.15:
        return "标准配置 · 建议仓位 40-60%"
    elif score >= 0.00:
        return "中性观望 · 建议仓位 30-50%"
    elif score >= -0.12:
        return "减配 · 建议仓位 15-30%"
    elif score >= -0.30:
        return "低配 · 建议仓位 5-15%"
    else:
        return "防守区 · 建议仓位 0-5%"


def tactical_recommendation(score: float) -> str:
    """
    战术分 → 时机建议。阈值按 2018+ 评分分布分位数标定 (2026-07 重标定,
    旧 ±0.5/±0.2 下 79% 天数落在等待区、入场窗口 8 年仅 4 天)。
    注意: 回测显示负分档的 30 天前瞻收益为正 (逆向过热信号在主升段提前触发),
    负分只约束"别加杠杆追高", 不构成现货卖出信号 — 文案如实标注。
    """
    if score >= 0.25:
        return "入场窗口 · 杠杆出清+动量配合"
    elif score >= 0.10:
        return "逢低分批 · 条件偏有利"
    elif score >= -0.10:
        return "等待信号 · 无明显优势"
    elif score >= -0.35:
        return "谨慎 · 降低杠杆与操作频率"
    else:
        return "杠杆拥挤 · 防追高防爆仓（非现货卖出信号）"


def compute_dual_scores(indicators: Dict[str, IndicatorResult],
                        df: pd.DataFrame) -> dict:
    """
    BTC Compass 主评分入口。
    1. 趋势伸展类指标 → 滚动分位数归一化（覆盖原离散评分）
    2. 周期分 + 战术分 分别按因子桶计算
    """
    apply_percentile_overrides(indicators, df)

    cycle_score, cycle_detail, cycle_cov = _compute_bucket_scores(CYCLE_BUCKETS, indicators)
    tactical_score, tactical_detail, tactical_cov = _compute_bucket_scores(TACTICAL_BUCKETS, indicators)

    cycle_rec = cycle_recommendation(cycle_score)
    tactical_rec = tactical_recommendation(tactical_score)
    # 覆盖率护栏: 有效因子权重过半缺失时, 评分由极少数因子决定, 必须警示
    if cycle_cov < 0.5:
        cycle_rec = f"⚠️ 数据覆盖仅{cycle_cov:.0%} · {cycle_rec} (可信度低)"
    if tactical_cov < 0.5:
        tactical_rec = f"⚠️ 数据覆盖仅{tactical_cov:.0%} · {tactical_rec} (可信度低)"

    return {
        "cycle_score": round(cycle_score, 3),
        "cycle_recommendation": cycle_rec,
        "cycle_buckets": cycle_detail,
        "cycle_coverage": round(cycle_cov, 3),
        "tactical_score": round(tactical_score, 3),
        "tactical_recommendation": tactical_rec,
        "tactical_buckets": tactical_detail,
        "tactical_coverage": round(tactical_cov, 3),
    }
