#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.score_history
===========================
综合评分历史快照 + 今日信号变化检测。

每次仪表盘刷新时记录当日快照（同日覆盖），与最近一个「往日」快照对比，
找出评分档位 / 各指标分数发生变化的项，生成「今日变化」列表。
"""

import os
import json
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional

from .version_stamp import config_fingerprint, engine_sha

_HISTORY_FILE = "score_history.json"
_MAX_ENTRIES = 730  # 最多保留 2 年

# 历史文件的读-改-写互斥: 刷新线程 (record_score_snapshot) 与回填线程
# (ensure_backfilled 的 load→merge→save) 并发时会互相吞掉对方写入。
# 双层锁 (2026-07 复查修复): threading.Lock 只覆盖同进程线程 — gunicorn
# --preload 下模块级线程在 master、请求刷新在 worker, 进程内锁跨不过去;
# 故再叠加 fcntl 文件锁做跨进程互斥, 正确性不依赖 gunicorn 启动参数。
# 写方在整个 load→modify→save 区间持锁; 纯读方无须持锁 (原子替换保证读到完整文件)。
_thread_lock = threading.Lock()


@contextmanager
def history_write_lock(cache_dir: str):
    lock_path = os.path.join(cache_dir, ".score_history.lock")
    with _thread_lock:
        f = open(lock_path, "w")
        try:
            try:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX)
            except ImportError:
                pass  # 非 POSIX 平台退化为纯线程锁
            yield
        finally:
            f.close()  # close 自动释放 flock


# 周期分仓位档位（与 scoring.cycle_recommendation 阈值一致, 2026-07 重标定）
_BANDS = [
    (0.45, "重仓区"),
    (0.30, "偏多配置"),
    (0.15, "标准配置"),
    (0.00, "中性观望"),
    (-0.12, "减配"),
    (-0.30, "低配"),
    (float("-inf"), "防守区"),
]

# 分位数归一化后指标分数是连续值, 小幅波动不算「信号变化」
_MIN_INDICATOR_DELTA = 0.25


def _score_band(score: float) -> str:
    for threshold, label in _BANDS:
        if score >= threshold:
            return label
    return "清仓"


def _history_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, _HISTORY_FILE)


def _load_history(cache_dir: str) -> list:
    path = _history_path(cache_dir)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"⚠️ 读取评分历史失败: {e}")
        return []


def _save_history(cache_dir: str, entries: list):
    """原子写入（与 app.py 缓存同样的 tmpfile + rename 策略）。

    写盘失败时记日志后向调用方抛出 (re-raise): 静默吞异常会让回填 marker 误建、
    评分历史静默永久丢失、专为失败设计的重试被绕过 (p1-6 修复)。调用方各自决定
    容忍或重试 —— record_score_snapshot 在刷新线程 (app.py 已 try/except 包裹,
    失败可见且不阻塞); ensure_backfilled 在回填线程 (失败不建 marker, 交重试)。
    """
    path = _history_path(cache_dir)
    try:
        fd, tmp = tempfile.mkstemp(dir=cache_dir, prefix=".score_history_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(entries, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except Exception as e:
        print(f"⚠️ 写入评分历史失败: {e}")
        raise


def load_history_entries(cache_dir: str) -> list:
    """公开的历史条目读取 (决策引擎滞回重放用), 按日期升序。"""
    return _load_history(cache_dir)


def _push_history_snapshot(entries: list) -> None:
    """同步把全量评分历史推送到 Gist (供跨冷启动恢复)。Gist 禁用时空操作;
    任何异常吞掉只打日志 —— 持久化是尽力而为, 绝不影响评分主流程。惰性导入
    gist_store 隔离导入期环路。"""
    try:
        from . import gist_store
        if not gist_store.is_enabled():
            return
        gist_store.push_history_to_gist(gist_store.build_payload(entries))
    except Exception as e:
        print(f"⚠️ Gist 推送异常 (已忽略, 不影响评分): {e}")


def _push_history_to_gist_async(entries: list) -> None:
    """跨日变更后异步推送 (daemon 线程), 不阻塞刷新主流程。"""
    threading.Thread(target=_push_history_snapshot, args=(entries,),
                     daemon=True).start()


def record_score_snapshot(dashboard: dict, cache_dir: str):
    """
    记录一次仪表盘快照到评分历史。
    - dashboard: app.py 的 _dashboard_cache 结构
      {timestamp, btc_price, total_score, recommendation, indicators:{name:{score,status,value,...}}}
    - 同一天多次刷新只保留最新一条（覆盖当日条目）
    """
    if not dashboard or "total_score" not in dashboard:
        return

    today = datetime.now().strftime("%Y-%m-%d")
    scores, statuses = {}, {}
    for name, ind in (dashboard.get("indicators") or {}).items():
        if ind.get("value") is None:
            continue  # 数据获取失败的指标不参与对比，避免误报「变化」
        scores[name] = ind.get("score")
        statuses[name] = ind.get("status", "")

    entry = {
        "date": today,
        "ts": dashboard.get("timestamp", ""),
        "btc_price": round(float(dashboard.get("btc_price", 0)), 2),
        "total_score": round(float(dashboard.get("total_score", 0)), 4),
        "recommendation": dashboard.get("recommendation", ""),
        "tactical_score": round(float(dashboard.get("tactical_score", 0)), 4),
        # 因子覆盖率审计字段: 不同日期的分数可能由不同因子集合构成 (失败剔除重归一),
        # 记录覆盖率使历史曲线的纵向可比性可核查 (2026-07)
        "cycle_coverage": dashboard.get("cycle_coverage"),
        "tactical_coverage": dashboard.get("tactical_coverage"),
        # 决策配置身份戳: 记录当日快照由哪套档位/权重/滞回配置产出, 使历史曲线上的
        # regime-shift 可区分行情 vs 配置漂移 (2026-07); 与 coverage 戳同构。
        "config_hash": config_fingerprint(),
        "engine_sha": engine_sha(),
        "scores": scores,
        "statuses": statuses,
    }

    with history_write_lock(cache_dir):
        entries = _load_history(cache_dir)
        # 日期变更 (新的一天首次落盘) 才推 Gist —— 天然去抖: 同日多次刷新只覆盖当日
        # 条目, 不重复推送 (record 每 ~5 分钟一次, 一天上百次)
        new_day = not (entries and entries[-1].get("date") == today)
        if entries and entries[-1].get("date") == today:
            entries[-1] = entry
        else:
            entries.append(entry)

        entries = entries[-_MAX_ENTRIES:]
        _save_history(cache_dir, entries)

    # 落盘成功后 (锁外, 不阻塞后续写者), 跨日变更时异步推送全量历史到 Gist。
    # 推送失败只打日志不影响主流程 (push 自身永不抛异常, Gist 禁用时为空操作)。
    if new_day:
        _push_history_to_gist_async(list(entries))


def _compute_changes(prev: Optional[dict], curr: dict) -> dict:
    """对比两个快照，输出今日变化结构"""
    result = {
        "prev_date": prev.get("date") if prev else None,
        "total": None,
        "indicators": [],
    }

    if not prev:
        return result

    # 综合评分变化（含档位跨越）
    p_score, c_score = prev.get("total_score", 0), curr.get("total_score", 0)
    p_band, c_band = _score_band(p_score), _score_band(c_score)
    result["total"] = {
        "prev_score": p_score,
        "curr_score": c_score,
        "delta": round(c_score - p_score, 4),
        "prev_band": p_band,
        "curr_band": c_band,
        "band_changed": p_band != c_band,
    }

    # 各指标分数变化（score 是离散档位，任何变化都是信号跳档）
    p_scores = prev.get("scores", {})
    c_scores = curr.get("scores", {})
    for name, c_val in c_scores.items():
        if name not in p_scores:
            continue
        p_val = p_scores[name]
        if p_val is None or c_val is None or abs(c_val - p_val) < _MIN_INDICATOR_DELTA:
            continue
        result["indicators"].append({
            "name": name,
            "prev_score": p_val,
            "curr_score": c_val,
            "direction": "bullish" if c_val > p_val else "bearish",
            "prev_status": prev.get("statuses", {}).get(name, ""),
            "curr_status": curr.get("statuses", {}).get(name, ""),
        })

    # 变化幅度大的排前面
    result["indicators"].sort(key=lambda x: abs(x["curr_score"] - x["prev_score"]), reverse=True)
    return result


# 多尺度评分变化: (键, 回看天数, 应用哪个分数字段)
# 周期分是慢变量 → 月/季/年; 战术分是快变量 → 周/月 (年尺度对它无意义)
_TREND_HORIZONS = {
    "cycle":    (("d30", 30), ("d90", 90), ("d365", 365)),
    "tactical": (("d7", 7), ("d30", 30)),
}
# 基准条目允许比目标日期早的最大天数: 回填连续日序列下通常精确命中;
# 缺口超过容忍则该档如实为 None, 不拿更老的数据冒充 (伪时间轴教训)
_TREND_TOLERANCE_DAYS = 10


def compute_trends(entries: list, today: Optional[str] = None) -> dict:
    """
    月/季/年尺度的评分变化 Δ = 最新分 - N天前基准分。
    基准 = 日期 ≤ (最新日-N天) 的最近条目, 且不早于容忍窗口; 深度不足该档为 None。
    返回 {"cycle": {"d30": {...}|None, ...}, "tactical": {...}, "depth_days": N}
    """
    out = {"cycle": {}, "tactical": {}, "depth_days": len(entries)}
    for kind, horizons in _TREND_HORIZONS.items():
        for key, _n in horizons:
            out[kind][key] = None
    if not entries:
        return out

    field = {"cycle": "total_score", "tactical": "tactical_score"}
    curr = entries[-1]
    try:
        cur_date = datetime.strptime(today or curr["date"], "%Y-%m-%d")
    except (KeyError, ValueError):
        return out

    for kind, horizons in _TREND_HORIZONS.items():
        cur_v = curr.get(field[kind])
        if cur_v is None:
            continue
        for key, n in horizons:
            target = (cur_date - timedelta(days=n)).strftime("%Y-%m-%d")
            floor = (cur_date - timedelta(days=n + _TREND_TOLERANCE_DAYS)).strftime("%Y-%m-%d")
            base = next((e for e in reversed(entries)
                         if e.get("date") and floor <= e["date"] <= target
                         and e.get(field[kind]) is not None), None)
            if base is None:
                continue  # 深度不足/缺口过大 → 保持 None, 不伪造
            out[kind][key] = {
                "delta": round(float(cur_v) - float(base[field[kind]]), 4),
                "base": base[field[kind]],
                "base_date": base["date"],
            }
    return out


def get_score_history(cache_dir: str, days: int = 90) -> dict:
    """
    返回评分历史时序 + 今日信号变化。
    {
      series: [{date, total_score, btc_price, recommendation}],
      changes: {prev_date, total:{...}, indicators:[...]},
      total_days: N
    }
    """
    entries = _load_history(cache_dir)
    if not entries:
        return {"series": [], "changes": {"prev_date": None, "total": None, "indicators": []},
                "total_days": 0, "trends": compute_trends([])}

    series = [
        {
            "date": e["date"],
            "total_score": e.get("total_score"),
            "tactical_score": e.get("tactical_score"),
            "btc_price": e.get("btc_price"),
            "recommendation": e.get("recommendation", ""),
        }
        for e in entries[-days:]
    ]

    curr = entries[-1]
    # 找最近一个「非当日」快照作为对比基准（通常是昨天）
    prev = next((e for e in reversed(entries[:-1]) if e.get("date") != curr.get("date")), None)
    changes = _compute_changes(prev, curr)

    return {"series": series, "changes": changes, "total_days": len(entries),
            "trends": compute_trends(entries)}
