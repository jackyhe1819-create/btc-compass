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
from datetime import datetime
from typing import Optional

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
    """原子写入（与 app.py 缓存同样的 tmpfile + rename 策略）"""
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


def load_history_entries(cache_dir: str) -> list:
    """公开的历史条目读取 (决策引擎滞回重放用), 按日期升序。"""
    return _load_history(cache_dir)


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
        "scores": scores,
        "statuses": statuses,
    }

    with history_write_lock(cache_dir):
        entries = _load_history(cache_dir)
        if entries and entries[-1].get("date") == today:
            entries[-1] = entry
        else:
            entries.append(entry)

        entries = entries[-_MAX_ENTRIES:]
        _save_history(cache_dir, entries)


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
        return {"series": [], "changes": {"prev_date": None, "total": None, "indicators": []}, "total_days": 0}

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

    return {"series": series, "changes": changes, "total_days": len(entries)}
