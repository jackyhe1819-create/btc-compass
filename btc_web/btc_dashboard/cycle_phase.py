#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.cycle_phase
=========================
周期相位判读 (2026-07 新增) — 规则式六相位状态 + 历史频率置信度。

三层结构:
  1. 温度计 (thermometer): 反转侧因子合成 (趋势伸展 + 链上筹码[剔除交易所余额]
     + 减半钟)。已验证四轮周期顶部全部 ≤-0.5、底部 ≥+0.6 — 是"周期位置"的
     鲜明信号; 但历史上顶部前 1-7 个月即开始报警, 直接当仓位分用回测
     Sharpe 0.73 vs 现行 1.16 — 故只做判读输入, 不驱动仓位。
  2. 相位规则 (classify_phase): 纯函数, 输入 温度计/趋势过滤器/距ATH回撤/
     距ATH天数 → 六相位之一。12年回测: 关键校验点 8/12 命中, 熊末/牛初段
     前瞻收益 99-100% 为正 (样本内)。
  3. 置信度 (phase_stats.json): 由 backtest 生成的历史频率统计 — 各相位
     episode 数、前瞻收益分布、模糊态的事后分辨 (只计观察满一年的已证实
     episode, 未满一年的如实标 pending)。

诚实性边界 (展示层必须携带):
  - 相位是事后概念的实时近似: "回调 vs 熊初"在发生时不可分 (历史已证实
    分辨约 5:3), 故该状态命名即含"待确认";
  - n=3~4 个周期的先验, 规则为样本内标定;
  - 仅叙事参考, 不进评分、不驱动仓位 — 仓位仍由周期分滞回档位管理。
"""

import os
import json
from datetime import datetime
from typing import Dict, List, Optional

from .scoring import _compute_bucket_scores

# ============================================================
# 温度计: 反转侧桶配置 (与 backtest 共用此常量, 勿在别处复制)
# ============================================================

THERMOMETER_BUCKETS = {
    "趋势伸展": {
        "weight": 0.45,
        "members": ["200-Week Heatmap", "幂律走廊", "Pi Cycle Top", "Ahr999"],
        "note": "价格相对长期趋势的拉伸 (反转侧)",
    },
    "链上筹码": {
        "weight": 0.45,
        # 刻意剔除交易所余额: ETF 时代结构性偏多 (2025 年 50% 天数正分 vs 1% 负分),
        # 且与 ETF 净流入双重计数 — 在顶部会中和掉本桶警报 (2026-07 审计)
        "members": ["MVRV-Z", "STH成本线", "NUPL"],
        "note": "持有者成本与未实现盈亏 (反转侧)",
    },
    "时间周期": {
        "weight": 0.10,
        "members": ["减半周期"],
        "note": "减半时钟先验",
    },
}

# 相位判定阈值 (样本内标定, 2026-07; 改动须重跑 backtest 相位统计并同步测试)
BUBBLE_T = -0.50          # 泡沫: 温度计过热线
BUBBLE_DD = -0.12         # 泡沫: 距 ATH 不超过 12%
LATE_BEAR_DD = -0.50      # 熊末: 距 ATH 回撤 ≥50% (用户口径)
LATE_BEAR_T = 0.35        # 熊末: 温度计过冷线
LATE_BEAR_AGE = 300       # 熊末: 距 ATH 超过 300 天 (防温度计早熟误判 2022-06)
EARLY_BULL_DD = -0.30     # 牛初: 仍距前高 30%+
EARLY_BULL_T = 0.10
BOOM_DD = -0.25           # 繁荣: 距 ATH 25% 以内
PULLBACK_AGE = (30, 300)  # 回调/熊初: 破位 1-10 个月
PULLBACK_DD = (-0.40, -0.10)
MID_BEAR_DD = -0.20
SMOOTH_DAYS = 14          # 众数平滑窗 (泡沫为急信号, 免平滑)

# desc 只描述机制, 不得手写统计数字 — 数字一律由 phase_stats.json 供给
# (曾出现 desc「约半数」/ stats「6:3」/ note「约5:3」三处矛盾, 2026-07 对抗审查)
PHASES = {
    "bubble":      {"name": "泡沫·牛市末",     "emoji": "🫧",
                    "desc": "价格贴近历史高点且反转侧因子集体过热。历史上泡沫信号往往显著领先周期最终顶 (领先分布见统计) — 过热≠立刻见顶。"},
    "boom":        {"name": "繁荣·牛市中",     "emoji": "🚀",
                    "desc": "上升趋势确认、距高点不远、尚未过热。"},
    "pullback_or_bear": {"name": "回调/熊初·待确认", "emoji": "❓",
                    "desc": "破位回落但结局未定 — 历史上该判读事后既有演化为熊市也有牛市回调 (分辨计数见统计), 实时不可分, 等待趋势修复或进一步下跌确认。"},
    "bear_mid":    {"name": "熊中·下跌中继",   "emoji": "🐻",
                    "desc": "下跌趋势确认且回撤已深, 反转侧尚未过冷。"},
    "bear_late":   {"name": "熊末·冷清超跌",   "emoji": "🥶",
                    "desc": "距高点回撤过半、距顶超过 300 天、反转侧过冷 (历史一年后收益分布见统计)。"},
    "early_bull":  {"name": "牛初·修复启动",   "emoji": "🌱",
                    "desc": "仍距前高较远但趋势转正、反转侧偏冷 — 修复启动特征。"},
    "transition":  {"name": "过渡·无鲜明相位", "emoji": "🌫️",
                    "desc": "各维度信号互相矛盾或均不极端, 如实不硬判。"},
    "unknown":     {"name": "数据不足",        "emoji": "⚪",
                    "desc": "关键因子缺席, 无法判读。"},
}

_STATS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "phase_stats.json")
_stats_cache = None


def load_phase_stats() -> Optional[dict]:
    global _stats_cache
    if _stats_cache is None:
        try:
            with open(_STATS_PATH, "r", encoding="utf-8") as f:
                _stats_cache = json.load(f)
        except Exception as e:
            print(f"⚠️ phase_stats.json 加载失败 (相位卡将不含置信度统计): {e}")
            _stats_cache = {}
    return _stats_cache or None


# ============================================================
# 纯逻辑
# ============================================================

def thermometer_from_scores(scores: Dict[str, float]) -> Optional[float]:
    """从因子分 dict (评分历史条目 / 实时指标) 合成温度计。
    复用现网桶聚合器 (含 MEMBER_WEIGHTS 与覆盖率重归一)。"""

    class _Stub:
        def __init__(self, s):
            self.score, self.value = s, 0.0

    inds = {k: _Stub(v) for k, v in scores.items() if v is not None}
    if not inds:
        return None
    total, _detail, cov = _compute_bucket_scores(THERMOMETER_BUCKETS, inds)
    return float(total) if cov >= 0.5 else None


def classify_phase(t: Optional[float], trend: Optional[float],
                   dd: Optional[float], ath_age_days: Optional[int]) -> str:
    """单日相位判定 (纯函数)。t=温度计, trend=趋势过滤器分,
    dd=距ATH回撤(负数), ath_age_days=距ATH天数。"""
    if t is None or trend is None or dd is None or ath_age_days is None:
        return "unknown"
    if dd >= BUBBLE_DD and t <= BUBBLE_T:
        return "bubble"
    if dd <= LATE_BEAR_DD and t >= LATE_BEAR_T and ath_age_days > LATE_BEAR_AGE:
        return "bear_late"
    if dd <= EARLY_BULL_DD and t >= EARLY_BULL_T and trend >= 0.5:
        return "early_bull"
    if trend >= 0.5 and dd >= BOOM_DD:
        return "boom"
    if (PULLBACK_AGE[0] <= ath_age_days <= PULLBACK_AGE[1]
            and PULLBACK_DD[0] < dd <= PULLBACK_DD[1] and trend <= 0):
        return "pullback_or_bear"
    if trend <= -0.5 and dd <= MID_BEAR_DD:
        return "bear_mid"
    return "transition"


# 平局决胜的固定优先序 (确定性): 与 PHASES 定义序一致
_PHASE_PRIORITY = ["bubble", "boom", "pullback_or_bear", "bear_mid",
                   "bear_late", "early_bull", "transition", "unknown"]


def smooth_phases(raw: List[str], k: int = SMOOTH_DAYS) -> List[str]:
    """滚动众数平滑防抖; 泡沫为急信号免平滑 (短暂闪现也要亮)。
    众数平局确定性决胜: 先粘滞前一日相位, 否则按固定优先序 — 禁用
    max(set(...)) 的哈希序裁决 (跨进程不可复现, 2026-07 对抗审查 major)。"""
    out = []
    for i, v in enumerate(raw):
        if v == "bubble":
            out.append(v)
            continue
        win = raw[max(0, i - k + 1):i + 1]
        counts = {}
        for w in win:
            counts[w] = counts.get(w, 0) + 1
        best = max(counts.values())
        tied = [p for p in _PHASE_PRIORITY if counts.get(p) == best]
        if len(tied) == 1 or not out or out[-1] not in tied:
            out.append(tied[0])
        else:
            out.append(out[-1])
    return out


def phase_series_from_history(entries: list, price_by_date: Dict[str, float]) -> List[dict]:
    """评分历史条目 + 日价格 → 逐日相位序列 [{date, phase}]。
    ATH 用 price_by_date 全量累计 (须含 2013 起完整历史, 否则回撤失真)。"""
    dates = sorted(price_by_date)
    ath, ath_date = 0.0, None
    ath_by_date = {}
    for d in dates:
        p = price_by_date[d]
        if p > ath:                     # 创新高: 刷新 ATH 与日期
            ath, ath_date = p, d
        elif p >= ath * 0.999:          # 触碰容差: 只刷新日期 (震荡顶部不累加 age)
            ath_date = d
        ath_by_date[d] = (ath, ath_date)

    raw, kept = [], []
    for e in entries:
        d = e.get("date")
        scores = e.get("scores") or {}
        if not d or d not in ath_by_date:
            continue
        a, a_d = ath_by_date[d]
        p = price_by_date[d]
        dd = p / a - 1 if a > 0 else None
        age = ((datetime.strptime(d, "%Y-%m-%d")
                - datetime.strptime(a_d, "%Y-%m-%d")).days if a_d else None)
        t = thermometer_from_scores(scores)
        trend = scores.get("趋势过滤器")
        raw.append(classify_phase(t, trend, dd, age))
        # trend 一并携带: criteria 四项必须同日取证, 不得混用 entries[-1]
        # (评分历史可能领先价格史一天, 2026-07 对抗审查发现)
        kept.append({"date": d, "t": t, "dd": dd, "age": age, "trend": trend})
    sm = smooth_phases(raw)
    return [{**k, "phase": ph} for k, ph in zip(kept, sm)]


# 价格史深度守卫: 备源降级 (CoinGecko 365天等) 时 ATH/回撤/距顶天数全面失真,
# 相位会静默误判 — 宁可不判 (2026-07 对抗审查 major)
MIN_PRICE_HISTORY_DAYS = 2000


def compute_cycle_phase(entries: list, price_by_date: Dict[str, float]) -> Optional[dict]:
    """主入口: 评分历史 (含今日) + 完整日价格 → 当前相位卡 payload。
    价格史不足 (数据源降级) 时返回 None — 不用失真的 ATH 硬判。"""
    try:
        if len(price_by_date) < MIN_PRICE_HISTORY_DAYS:
            print(f"⚠️ 价格史仅 {len(price_by_date)} 天 (<{MIN_PRICE_HISTORY_DAYS}, "
                  f"数据源降级?) — 相位判读跳过")
            return None
        series = phase_series_from_history(entries, price_by_date)
        if not series:
            return None
        cur = series[-1]
        phase = cur["phase"]
        # 回溯"进入日期": 从尾部向前找同相位连续段起点
        since = cur["date"]
        for row in reversed(series):
            if row["phase"] != phase:
                break
            since = row["date"]
        meta = PHASES[phase]
        stats = (load_phase_stats() or {}).get("phases", {}).get(phase)
        return {
            "phase": phase,
            "name": meta["name"],
            "emoji": meta["emoji"],
            "desc": meta["desc"],
            "since": since,
            "criteria": {
                "thermometer": round(cur["t"], 3) if cur["t"] is not None else None,
                "trend": cur.get("trend"),
                "drawdown_pct": round(cur["dd"] * 100, 1) if cur["dd"] is not None else None,
                "ath_age_days": cur["age"],
            },
            "stats": stats,
            "note": ("规则式判读 · n=3~4 周期样本内标定 · 相位是事后概念的实时近似, "
                     "模糊态的历史分辨见卡内统计 · 叙事参考, 非交易信号, 仓位由周期分档位管理 · "
                     "与页底「周期相位与事件规律」卡 (减半日历口径) 为两套不同判读体系"),
        }
    except Exception as e:
        print(f"⚠️ 周期相位计算失败: {e}")
        return None
