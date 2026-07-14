#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.decision
======================
量化决策引擎 — 把双评分转成可执行决策 (2026-07 新增)。

长期 (周期分 → 仓位决策):
    评分 → 档位 → 目标仓位, 叠加**滞回换档**: 分数须越过当前档位边界 ±δ
    且新档位连续 N 个快照保持, 目标仓位才变。基线逐日换档 12 年回测换档
    787 次 (每年 63 次, 决策不可执行), 滞回 (δ=0.05, N=5) 后 ~86 次
    (每年 7 次), 计 10bp 成本 Sharpe 0.97→1.08 (见 backtest/output/report.md)。

短期 (战术分 → 执行节奏):
    换仓/定投**怎么执行**: 加速分批 / 正常分批 / 放缓等待 / 禁杠杆。
    回测显示战术门控对净值无显著贡献 (report.md), 故战术分只影响执行
    节奏与杠杆约束, 不改变目标仓位 — 如实呈现, 不夸大。

每个档位附 12 年回测的分档前瞻收益统计 (data/band_stats.json,
由 backtest/run_backtest.py 生成) — 样本内标定, 展示时带免责说明。

滞回状态不落盘: 每次用评分历史 (score_history, 回填 90 天) 重放推导 —
同一份历史下确定且幂等。注意该性质**以历史本身持久为前提**: Render free 无
持久盘, 每次部署评分历史由 backfill 按近似口径重建 (v2 起口径已与实时对齐),
生效档位跨部署可能随重建历史刷新而非延续旧实例。历史不足时退化为无滞回档位
并如实标注。
"""

import os
import json
from typing import List, Optional

# 滞回参数 — 与 backtest/run_backtest.py HYST_DELTA/HYST_CONFIRM 保持一致
# (取自 δ∈[0.03,0.06]×N∈[3,7] 回测网格平台中部, 非单点调优)
HYST_DELTA = 0.05
HYST_CONFIRM = 5

# 重放窗口: 滞回状态由最近 N 天评分历史推导 (回填保证 ≥90 天)
REPLAY_DAYS = 365

# 档位定义 — 阈值与 scoring.cycle_recommendation 一致 (2026-07 重标定)
# (下界, 档名, 仓位下限%, 仓位上限%, 目标中值%, band_stats.json 键)
CYCLE_BANDS = [
    (0.45, "重仓区", 80, 100, 90, "重仓区 80-100%"),
    (0.30, "偏多配置", 60, 80, 70, "偏多配置 60-80%"),
    (0.15, "标准配置", 40, 60, 50, "标准配置 40-60%"),
    (0.00, "中性观望", 30, 50, 40, "中性观望 30-50%"),
    (-0.12, "减配", 15, 30, 22, "减配 15-30%"),
    (-0.30, "低配", 5, 15, 10, "低配 5-15%"),
    (float("-inf"), "防守区", 0, 5, 2, "防守区 0-5%"),
]

# 战术档位 — 阈值与 scoring.tactical_recommendation 一致
# (下界, 档名, 执行节奏, 展开说明, band_stats.json 键)
TACTICAL_BANDS = [
    (0.25, "入场窗口", "加速分批",
     "杠杆出清+动量配合, 计划内的加仓/定投可提速执行", "入场窗口 (≥0.25)"),
    (0.10, "逢低分批", "正常分批",
     "条件偏有利, 按计划节奏分批执行", "逢低分批 (0.1~0.25)"),
    (-0.10, "等待信号", "正常定投",
     "无明显时机优势, 维持既定定投节奏, 不主动加速", "等待信号 (-0.1~0.1)"),
    (-0.35, "谨慎", "放缓执行",
     "衍生品偏拥挤, 加仓放缓分批、拉长间隔; 减仓不受限", "谨慎 (-0.35~-0.1)"),
    (float("-inf"), "杠杆拥挤", "禁杠杆·放缓加仓",
     "杠杆拥挤, 防追高防爆仓; 非现货卖出信号 (回测该档 30d 前瞻收益为正)",
     "杠杆拥挤 (<-0.35)"),
]

_STATS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "band_stats.json")
_band_stats_cache = None


def _load_band_stats() -> Optional[dict]:
    """加载回测分档统计 (缺失时决策照常输出, 只是不带历史统计)。"""
    global _band_stats_cache
    if _band_stats_cache is None:
        try:
            with open(_STATS_PATH, "r", encoding="utf-8") as f:
                _band_stats_cache = json.load(f)
        except Exception as e:
            print(f"⚠️ band_stats.json 加载失败 (决策面板将不含回测统计): {e}")
            _band_stats_cache = {}
    return _band_stats_cache or None


_BLACKSWAN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "data", "blackswan_events.json")
_blackswan_stress_cache = None


def _load_blackswan_stress() -> Optional[dict]:
    """黑天鹅压力测试参考 (相对前30日高点回撤 中位/最差), 供决策卡换算组合回撤。

    数据源 blackswan_events.json (backtest/blackswan_study.py 生成) — 单一来源
    透传, 不在前端硬编码常数。样本为事后追认的知名暴跌, 尾部无上界。
    """
    global _blackswan_stress_cache
    if _blackswan_stress_cache is None:
        try:
            with open(_BLACKSWAN_PATH, "r", encoding="utf-8") as f:
                b = json.load(f)
            dds = [e.get("dd_from_30d_high_pct") for e in b.get("events", [])
                   if e.get("dd_from_30d_high_pct") is not None]
            _blackswan_stress_cache = {
                "dd_median": b["summary"]["dd_from_high_median"],
                "dd_worst": round(min(dds), 1) if dds else None,
                "n": b["summary"]["n"],
                "as_of": str(b.get("generated") or "")[:10] or None,
            }
        except Exception as e:
            print(f"⚠️ blackswan_events.json 加载失败 (压力测试参考不显示): {e}")
            _blackswan_stress_cache = {}
    return _blackswan_stress_cache or None


def _cycle_band_idx(score: float) -> int:
    for i, (lo, *_rest) in enumerate(CYCLE_BANDS):
        if score >= lo:
            return i
    return len(CYCLE_BANDS) - 1


def _tactical_band_idx(score: float) -> int:
    for i, (lo, *_rest) in enumerate(TACTICAL_BANDS):
        if score >= lo:
            return i
    return len(TACTICAL_BANDS) - 1


def _band_bounds(idx: int):
    """档位 idx 的 (下界, 上界)。"""
    lo = CYCLE_BANDS[idx][0]
    hi = CYCLE_BANDS[idx - 1][0] if idx > 0 else float("inf")
    return lo, hi


def replay_hysteresis(scores: List[float]):
    """
    对逐日周期分序列重放滞回规则。
    返回 (生效档位序列, 候选档位, 候选已持续天数)。
    注: confirm 按快照条数计 (历史条目缺天时略保守), 与回测按日历天一致性
    足够 — 快照缺失本身就是"信号未确认"。
    """
    if not scores:
        return [], None, 0
    cur = _cycle_band_idx(scores[0])
    pending, pend_days = cur, 0
    seq = []
    for s in scores:
        lo, hi = _band_bounds(cur)
        lo_x = lo - HYST_DELTA if lo != float("-inf") else lo
        hi_x = hi + HYST_DELTA if hi != float("inf") else hi
        cand = _cycle_band_idx(s) if (s < lo_x or s >= hi_x) else cur
        if cand != cur:
            if cand == pending:
                pend_days += 1
            else:
                pending, pend_days = cand, 1
            if pend_days >= HYST_CONFIRM:
                cur, pend_days = cand, 0
        else:
            pending, pend_days = cur, 0
        seq.append(cur)
    return seq, (pending if pend_days > 0 else None), pend_days


def _stats_for(kind: str, key: str, windows: List[str]) -> Optional[dict]:
    """从 band_stats.json 取指定档位的前瞻收益统计。"""
    stats = _load_band_stats()
    if not stats or key not in stats.get(kind, {}):
        return None
    entry = stats[kind][key]
    out = {w: entry[w] for w in windows if w in entry}
    return out or None


def compute_decision(dashboard: dict, history_entries: list) -> dict:
    """
    主入口: 由当前仪表盘快照 + 评分历史 生成量化决策。

    dashboard: app.py 的 _dashboard_cache 结构 (total_score=周期分)
    history_entries: score_history 的完整条目列表 (含今日, 按日期升序)

    返回结构见底部 dict; 任何数据缺陷 (覆盖率低/历史不足/合成数据)
    都在 warnings 中如实标注, 不静默降级。
    """
    cycle_score = float(dashboard.get("total_score", 0))
    tactical_score = float(dashboard.get("tactical_score", 0))
    warnings = []

    # ── 长期: 滞回重放得到生效档位 ──
    hist_scores = [e["total_score"] for e in history_entries[-REPLAY_DAYS:]
                   if e.get("total_score") is not None]
    # 历史末条即今日快照 (record_score_snapshot 先于本函数执行);
    # 若历史为空 (首次冷启动) 用当前分数单点退化
    if not hist_scores:
        hist_scores = [cycle_score]
    if len(hist_scores) < HYST_CONFIRM * 4:
        warnings.append(f"评分历史仅 {len(hist_scores)} 天, 滞回状态可信度有限")

    seq, pending_idx, pend_days = replay_hysteresis(hist_scores)
    held_idx = seq[-1]
    prev_idx = seq[-2] if len(seq) >= 2 else held_idx
    raw_idx = _cycle_band_idx(cycle_score)

    _, held_name, pos_lo, pos_hi, pos_mid, held_key = CYCLE_BANDS[held_idx]

    # 今日动作: 换档瞬间给方向性动作, 其余维持
    if held_idx != prev_idx:
        direction = "上调" if held_idx < prev_idx else "下调"
        action = f"{direction}目标仓位至 {pos_lo}-{pos_hi}%"
        action_type = "increase" if held_idx < prev_idx else "decrease"
    else:
        action = f"维持目标仓位 {pos_lo}-{pos_hi}%"
        action_type = "hold"

    pending = None
    if pending_idx is not None and pending_idx != held_idx:
        p_name = CYCLE_BANDS[pending_idx][1]
        p_dir = "上调" if pending_idx < held_idx else "下调"
        pending = {
            "band": p_name, "direction": p_dir,
            "days": pend_days, "need": HYST_CONFIRM,
            "note": f"候选{p_dir}至「{p_name}」确认中 ({pend_days}/{HYST_CONFIRM}天)",
        }

    # ── 短期: 战术档位 → 执行节奏 ──
    t_idx = _tactical_band_idx(tactical_score)
    _, t_name, t_pace, t_advice, t_key = TACTICAL_BANDS[t_idx]

    # ── 数据质量护栏 ──
    if dashboard.get("data_synthetic"):
        warnings.append("🚨 价格为演示数据, 本决策无效")
    cov_c = dashboard.get("cycle_coverage")
    cov_t = dashboard.get("tactical_coverage")
    if cov_c is not None and cov_c < 0.5:
        warnings.append(f"周期分因子覆盖率仅 {cov_c:.0%}, 仓位决策可信度低")
    if cov_t is not None and cov_t < 0.5:
        warnings.append(f"战术分因子覆盖率仅 {cov_t:.0%}, 节奏建议可信度低")

    stats = _load_band_stats()
    return {
        # 长期决策 (周期分, 周级变化)
        "cycle": {
            "band": held_name,
            "target_lo": pos_lo, "target_hi": pos_hi, "target_mid": pos_mid,
            "action": action, "action_type": action_type,
            "pending": pending,
            # 原始档位 ≠ 生效档位时说明滞回在起作用 (前端可提示)
            "raw_band": CYCLE_BANDS[raw_idx][1],
            "raw_differs": raw_idx != held_idx,
            "stats": _stats_for("cycle", held_key, ["90d", "180d", "365d"]),
        },
        # 短期决策 (战术分, 日级变化)
        "tactical": {
            "band": t_name, "pace": t_pace, "advice": t_advice,
            "stats": _stats_for("tactical", t_key, ["14d", "30d"]),
        },
        "hysteresis": {"delta": HYST_DELTA, "confirm": HYST_CONFIRM,
                       "history_days": len(hist_scores)},
        # 黑天鹅压力测试参考 (n=11 事后追认样本, 非预测) — 前端换算目标仓位对应组合回撤
        "blackswan_stress": _load_blackswan_stress(),
        "warnings": warnings,
        "stats_meta": {
            "generated": stats.get("generated") if stats else None,
            "note": ("分档统计来自12年回测, 阈值与历史同源 (样本内), 非收益承诺; "
                     "回测因子集与现网不完全一致 (动量仅日/周/月三腿、无期货基差/多空比), "
                     "统计为近似条件参考"),
        },
    }


# ============================================================
# 评分历史事件标记 (2026-07 事件研究 B 层)
# ============================================================

# 事件研究的诚实结论 — 展示事件必须携带此口径, 不得包装成"胜率信号"
_EVENT_NOTE_CROSS = ("事件研究: 12年12次触发≈3个独立周期段, 统计力不足 — "
                     "周期叙事参考, 非胜率信号")
_EVENT_NOTE_SWITCH = "决策层滞回换档动作 (12年约7次/年); 卖出侧无胜率含义, 仅风险管理"


def extract_events(history_entries: list) -> list:
    """
    评分历史 → 图表事件标记: 上穿0.30/0.15、转负、滞回换档。
    口径与 backtest/event_study.py 一致 (10天去抖); 仅覆盖窗口内历史,
    每个事件自带诚实标注。
    """
    pts = [(e["date"], e["total_score"]) for e in history_entries
           if e.get("total_score") is not None]
    if len(pts) < 12:
        return []
    vals = [s for _, s in pts]
    events = []

    for i in range(10, len(vals)):
        window = vals[i - 10:i]
        for level, label in ((0.30, "上穿0.30 · 进偏多档"), (0.15, "上穿0.15 · 进标准档")):
            if vals[i] >= level and all(v < level for v in window):
                events.append({"date": pts[i][0], "side": "buy", "label": label,
                               "score": vals[i], "note": _EVENT_NOTE_CROSS})
        if vals[i] <= 0 and all(v > 0 for v in window):
            events.append({"date": pts[i][0], "side": "risk", "label": "周期分转负",
                           "score": vals[i], "note": _EVENT_NOTE_CROSS})

    seq, _, _ = replay_hysteresis(vals)
    for i in range(1, len(seq)):
        if seq[i] != seq[i - 1]:
            up = seq[i] < seq[i - 1]
            events.append({
                "date": pts[i][0], "side": "buy" if up else "risk",
                "label": f"滞回{'升' if up else '降'}档 → {CYCLE_BANDS[seq[i]][1]}",
                "score": vals[i], "note": _EVENT_NOTE_SWITCH,
            })

    seen = set()
    out = []
    for ev in sorted(events, key=lambda x: x["date"]):
        key = (ev["date"], ev["label"])
        if key not in seen:
            seen.add(key)
            out.append(ev)
    return out
