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

from .scoring import factor_coverage_from_buckets

# 滞回参数 — 与 backtest/run_backtest.py HYST_DELTA/HYST_CONFIRM 保持一致
# (取自 δ∈[0.03,0.06]×N∈[3,7] 回测网格平台中部, 非单点调优)
HYST_DELTA = 0.05
HYST_CONFIRM = 5

# 数据质量护栏阈值 — 决策警示文案与 confidence 三档分级严格同源引用这两个常量,
# 改一处即两处同步 (tests/test_consistency.py 锁死"置信闸门阈值 == 警示触发阈值")。
COVERAGE_WARN_THRESHOLD = 0.5             # 因子级覆盖率警示/存疑阈值
MIN_RELIABLE_HISTORY = HYST_CONFIRM * 4   # 滞回可信最小历史深度 (=20)

# 冷启动重建噪声尺度 — Render free 无持久盘, 每次冷启动由 backfill 重建评分历史;
# 重建时链上慢变量受 bitcoin-data.com 匿名限流 (10 请求/h) 影响可得性, 实测使周期分
# 产生 ±0.01 量级抖动。REBUILD_NOISE=0.02 (≈2× 实测单侧噪声) 作"换档临界带"半宽:
# 当前分数距任一 δ 偏移换档触发线 < 此值时, 不同冷启动重建可能落在触发线两侧、翻转
# 生效档位。案例 2026-07-18: 周期分近两周贴 减配上移触发线 0.00+δ=0.05 擦边 (07-15 差
# 0.001), 生效档在 减配↔中性观望 间随重建来回摆 — 据此加临界带诚实提示 (不改仓位数学)。
# 与 confidence 分级同源 (tests/test_consistency.py 锁死"临界带警示阈值 == 置信闸门阈值")。
REBUILD_NOISE = 0.02

# 回填保证的最小历史深度 — app.py/backfill.py 引用此常量, 确保滞回重放窗口喂满
REPLAY_DAYS = 365

# 滞回重放取全量持久化历史 (与 score_history 落盘上限一致), 与 extract_events 同源。
# 不再切 [-365:] 子窗: 子窗左边界随历史增长滑动会使 replay 种子 (窗口起点原始档)
# 漂移, 造成无 δ/N 确认的静默换档; 用全量历史后种子仅在越过 730 上限时才移动。
_MAX_ENTRIES = 730

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


# ============================================================
# 个人仓位政策层 (2026-07 新增) — 纯叠加 overlay, 不碰任何评分/档位数学
# ============================================================
# 把标准 0-100% 档位以中性(50%)为轴, 仿射映射进用户真实操作区间 [floor,ceiling]:
# 对 BTC 占净资产 84%、长持信念的用户,"防守区 0-5%""重仓区 80-100%"是无操作空间的
# 抽象档位; 映射后防守档→向 floor 减仓(而非清仓)、重仓档→向 ceiling 加仓, 提升可执行性。
# 关键纪律: band_stats.json 回测统计基于标准映射标定, 个人区间只改幅度不改方向 —
# 故绝不改写原 target_lo/hi/mid, 只发一个并列子键 policy, 且明标为偏好校准非回测背书。
_POLICY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "position_policy.json")
_policy_cache = None

_POLICY_NOTE = ("个人偏好校准 (非回测背书): 标准档位以中性(50%)为轴线性映射进你的操作"
                "区间 [floor,ceiling]%, 净信号强弱在此区间内加/减仓。band_stats 回测统计"
                "基于标准 0-100% 映射标定, 自定义区间只改变幅度、不改变方向。")


def _load_position_policy() -> Optional[dict]:
    """加载个人仓位政策 (缺失/损坏时禁用政策层, 决策照常输出标准档位)。"""
    global _policy_cache
    if _policy_cache is None:
        try:
            with open(_POLICY_PATH, "r", encoding="utf-8") as f:
                _policy_cache = json.load(f)
        except Exception as e:
            print(f"⚠️ position_policy.json 加载失败 (个人政策层禁用): {e}")
            _policy_cache = {}
    return _policy_cache or None


def apply_position_policy(band_lo, band_hi, band_mid,
                          policy: Optional[dict],
                          band_name: Optional[str] = None) -> Optional[dict]:
    """把标准档位仓位 (band_lo/hi/mid, 均为 0-100%) 线性投影进个人操作区间
    [floor, ceiling] 并 clamp, 返回并列的个人目标带。

    纯函数, 无副作用: 不改写传入的标准档位。policy 缺失/未启用/配置非法
    (要求 0<=floor<=baseline<=ceiling<=100) → 返回 None, 前端不显示个人带。
    映射以标准中性 50% 为轴: personal(x)=baseline+(x-50)*(ceiling-floor)/100,
    再 clamp 到 [floor,ceiling] — baseline 偏离区间中点时高/低档会真实贴顶/贴底。

    band_overrides: policy 可选按档名点名覆盖区间 ({"标准配置": [50, 75], ...}) —
    仿射映射是一条直线, 表达不了逐档手工阶梯; 点名档位直接用给定 [lo, hi]
    (mid 取中点), 未点名档位仍走仿射。覆盖值非法 (须 0<=lo<=hi<=100) 时
    忽略该条覆盖回退仿射, 不使整个政策层失效。
    """
    if not policy or not policy.get("enabled"):
        return None
    try:
        floor = float(policy["floor_pct"])
        ceiling = float(policy["ceiling_pct"])
        baseline = float(policy["baseline_pct"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= floor <= baseline <= ceiling <= 100):
        return None
    span = ceiling - floor

    def _project(x) -> float:
        p = baseline + (float(x) - 50.0) * span / 100.0
        return round(min(max(p, floor), ceiling), 1)

    override = None
    ov_map = policy.get("band_overrides")
    if band_name and isinstance(ov_map, dict) and band_name in ov_map:
        try:
            o_lo, o_hi = float(ov_map[band_name][0]), float(ov_map[band_name][1])
            if 0 <= o_lo <= o_hi <= 100:
                override = (round(o_lo, 1), round(o_hi, 1))
        except (TypeError, ValueError, IndexError, KeyError):
            override = None

    if override is not None:
        out = {
            "personal_lo": override[0],
            "personal_mid": round((override[0] + override[1]) / 2, 1),
            "personal_hi": override[1],
            "overridden": True,
        }
    else:
        out = {
            "personal_lo": _project(band_lo),
            "personal_mid": _project(band_mid),
            "personal_hi": _project(band_hi),
        }
    out.update({
        "floor": round(floor, 1),
        "ceiling": round(ceiling, 1),
        "baseline": round(baseline, 1),
        "note": _POLICY_NOTE + (" (本档为手动覆盖区间)" if override else ""),
    })
    md = policy.get("max_drawdown_pct")
    if md is not None:
        try:
            out["max_drawdown_pct"] = float(md)
        except (TypeError, ValueError):
            pass
    return out


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


def _switch_trigger_lines(idx: int):
    """档位 idx 的 δ 偏移换档触发线 (下移触发线, 上移触发线) —— 与 replay_hysteresis
    的 lo_x/hi_x 严格同源: 分数须越过 lo-δ 才可能下移换档、越过 hi+δ 才可能上移换档。
    含 ±inf 边界的一侧无触发线 (replay 里 s<-inf / s>=inf 恒不成立), 返回 None。"""
    lo, hi = _band_bounds(idx)
    lo_line = lo - HYST_DELTA if lo != float("-inf") else None
    hi_line = hi + HYST_DELTA if hi != float("inf") else None
    return lo_line, hi_line


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


def _bucket_conflict(cycle_buckets: Optional[dict]) -> Optional[dict]:
    """周期分桶间分歧: 各桶分数按桶权重的加权标准差 + 一句话定性 (仅信息展示)。

    净分是各桶加权和 — 当链上筹码 -0.8 与资金流 +0.6 相互抵消成温和 +0.1 时, 净分
    掩盖了 regime 冲突。此处量化桶间离散度作为元信号上下文, 绝不回灌评分、不改仓位。
    离散度是实测量 (加权 σ), 非未标定的置信标量。桶明细缺失/存活桶不足 2 时返回 None。
    """
    if not cycle_buckets or not isinstance(cycle_buckets, dict):
        return None
    scored = [(name, float(bd["score"]), float(bd["weight"]))
              for name, bd in cycle_buckets.items()
              if isinstance(bd, dict) and bd.get("score") is not None
              and bd.get("weight")]
    if len(scored) < 2:
        return None
    wsum = sum(w for _, _, w in scored)
    if wsum <= 0:
        return None
    mean = sum(s * w for _, s, w in scored) / wsum
    dispersion = (sum(w * (s - mean) ** 2 for _, s, w in scored) / wsum) ** 0.5
    top = max(scored, key=lambda t: t[1])
    bot = min(scored, key=lambda t: t[1])
    if dispersion < 0.20:
        level, note = "共识较强", f"桶间共识较强 (σ={dispersion:.2f}), 各桶方向基本一致"
    elif dispersion < 0.40:
        level = "存在分歧"
        note = (f"桶间存在分歧 (σ={dispersion:.2f}): {top[0]} {top[1]:+.2f} 偏多 "
                f"vs {bot[0]} {bot[1]:+.2f} 偏空")
    else:
        level = "分歧显著"
        note = (f"桶间分歧显著 (σ={dispersion:.2f}), 净分掩盖对立: "
                f"{top[0]} {top[1]:+.2f} vs {bot[0]} {bot[1]:+.2f}")
    return {"dispersion": round(dispersion, 3), "level": level, "note": note}


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
    hist_scores = [e["total_score"] for e in history_entries[-_MAX_ENTRIES:]
                   if e.get("total_score") is not None]
    # 历史末条即今日快照 (record_score_snapshot 先于本函数执行);
    # 若历史为空 (首次冷启动) 用当前分数单点退化
    if not hist_scores:
        hist_scores = [cycle_score]
    history_short = len(hist_scores) < MIN_RELIABLE_HISTORY
    if history_short:
        warnings.append(f"评分历史仅 {len(hist_scores)} 天, 滞回状态可信度有限")

    seq, pending_idx, pend_days = replay_hysteresis(hist_scores)
    held_idx = seq[-1]
    prev_idx = seq[-2] if len(seq) >= 2 else held_idx
    raw_idx = _cycle_band_idx(cycle_score)

    _, held_name, pos_lo, pos_hi, pos_mid, held_key = CYCLE_BANDS[held_idx]

    # 静默漂移护栏: 窗口内无任何确认换档时, 生效档位纯由窗口起点原始档 (seed) 决定。
    # 若当前分数原始档已不同、又无确认中的候选 (分数落在档位滞回缓冲内), 则生效档
    # 实为历史窗口边界的产物 — 越过 730 上限左滑时可无声换档, 如实标注不静默降级。
    if (all(b == seq[0] for b in seq) and held_idx != raw_idx
            and pending_idx is None):
        warnings.append(
            f"生效档位「{held_name}」由评分历史窗口起点原始档决定, 窗口内无滞回确认换档; "
            f"当前分数原始档为「{CYCLE_BANDS[raw_idx][1]}」, 历史窗口滑动时档位可能漂移")

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
    synthetic = bool(dashboard.get("data_synthetic"))
    if synthetic:
        warnings.append("🚨 价格为演示数据, 本决策无效")
    # 低覆盖警示以因子级覆盖率为准 (p1-4): 桶级 coverage 对"桶内因子逐个流失"失明。
    # 现网 dashboard 携带 cycle_buckets/tactical_buckets, 据此复算因子级覆盖率;
    # 缺明细的旧口径 dashboard 回退到桶级 cycle_coverage/tactical_coverage。
    cov_c = factor_coverage_from_buckets(dashboard.get("cycle_buckets"))
    if cov_c is None:
        cov_c = dashboard.get("cycle_coverage")
    cov_t = factor_coverage_from_buckets(dashboard.get("tactical_buckets"))
    if cov_t is None:
        cov_t = dashboard.get("tactical_coverage")
    cov_c_low = cov_c is not None and cov_c < COVERAGE_WARN_THRESHOLD
    cov_t_low = cov_t is not None and cov_t < COVERAGE_WARN_THRESHOLD
    if cov_c_low:
        warnings.append(f"周期分因子覆盖率仅 {cov_c:.0%}, 仓位决策可信度低")
    if cov_t_low:
        warnings.append(f"战术分因子覆盖率仅 {cov_t:.0%}, 节奏建议可信度低")

    # ── 换档临界带诚实提示 (2026-07-18): 冷启动重建噪声可翻转生效档位 ──
    # 两条命中路径, 均按 replay_hysteresis 的 lo_x/hi_x 真实语义算距离:
    #  (a) 当前分数距 held 档任一 δ 偏移换档触发线 < REBUILD_NOISE — 稳态贴线;
    #  (b) pending 确认中且确认窗口 (末 pend_days 条快照) 内任一天分数距其确认线 <
    #      REBUILD_NOISE — 该天是否计入连击确认可被 ±0.01 重建噪声翻转。
    # 命中即如实追加警示并把 confidence 至少降为存疑; 绝不改仓位/档位数学。
    lo_line, hi_line = _switch_trigger_lines(held_idx)
    crit_dists = [abs(cycle_score - ln) for ln in (lo_line, hi_line) if ln is not None]
    if pending_idx is not None and pending_idx != held_idx and pend_days > 0:
        # pending 在更低档 (idx 更大) → 越过下移线; 更高档 → 越过上移线
        confirm_line = lo_line if pending_idx > held_idx else hi_line
        if confirm_line is not None:
            crit_dists.append(min(abs(s - confirm_line)
                                  for s in hist_scores[-pend_days:]))
    near_line_dist = min(crit_dists) if crit_dists else None
    in_boundary_zone = near_line_dist is not None and near_line_dist < REBUILD_NOISE
    if in_boundary_zone:
        warnings.append(
            f"处于换档临界带: 分数距确认线仅 {near_line_dist:.3f} "
            f"(重建噪声 ±0.01 量级), 冷启动重建后生效档位可能不同")

    # ── 冻结态 (2026-07): 仅"价格层失效"锁死为一等状态 —— 合成价格 (全源失效) 或
    # 价格全源陈旧 (>7天)。价格是滚动均线/分位数的地基, 地基坏则仓位数学无意义,
    # 前端据此把可执行数字置灰、notify 据此堵住陈旧误推送 (见 notify.evaluate_alerts)。
    # 因子覆盖率保持软告警不升冻结: 避免 Render 冷启动/bitcoin-data 限流下 onchain
    # 暂灰触发扰民误冻。
    freeze_reasons = []
    if synthetic:
        freeze_reasons.append("价格为演示数据, 仓位数学无效")
    if dashboard.get("price_stale"):
        freeze_reasons.append("价格全源陈旧 (滞后>7天), 评分地基失效")
    frozen = bool(freeze_reasons)

    # ── 置信分级 (三档, 无连续标量): 严格复用上面数据质量警示的同源阈值与文案。
    # 合成数据→不可靠; 覆盖率/历史缺陷叠加 (≥2) →不可靠, 单一缺陷/换档临界带→存疑;
    # 否则可靠。(价格陈旧走上面的 frozen 硬状态, 不并入此软分级。)
    # 换档临界带命中把置信至少降为存疑 — 生效档位对冷启动重建噪声敏感即"存疑"。
    defect_count = sum([cov_c_low or cov_t_low, history_short])
    if synthetic or defect_count >= 2:
        conf_level = "不可靠"
    elif defect_count >= 1 or in_boundary_zone:
        conf_level = "存疑"
    else:
        conf_level = "可靠"
    confidence = {"level": conf_level, "reasons": list(warnings)}

    # ── 桶间分歧 (仅信息): 净分掩盖 regime 冲突时提示, 不改仓位 ──
    conflict = _bucket_conflict(dashboard.get("cycle_buckets"))

    policy = apply_position_policy(pos_lo, pos_hi, pos_mid,
                                   _load_position_policy(),
                                   band_name=held_name)

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
            # 个人政策层 (纯叠加 overlay): 启用时并列个人目标带, 未启用时为 None,
            # 绝不改写上面的 target_lo/hi/mid (band_stats 回测统计仍绑标准映射)
            "policy": policy,
        },
        # 短期决策 (战术分, 日级变化)
        "tactical": {
            "band": t_name, "pace": t_pace, "advice": t_advice,
            "stats": _stats_for("tactical", t_key, ["14d", "30d"]),
        },
        "hysteresis": {"delta": HYST_DELTA, "confirm": HYST_CONFIRM,
                       "history_days": len(hist_scores)},
        # 冻结态: 价格层失效时前端置灰可执行数字、notify 堵推送 (纯展示/门控, 不改仓位数学)
        "frozen": frozen,
        "freeze_reasons": freeze_reasons,
        # 置信分级 (三档) + 桶间分歧 (加权 σ) — 元信号, 仅展示上下文, 不回灌评分/不改仓位
        "confidence": confidence,
        "conflict": conflict,
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
    pts = [(e["date"], e["total_score"]) for e in history_entries[-_MAX_ENTRIES:]
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
