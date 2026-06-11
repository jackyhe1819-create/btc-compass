#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC Dashboard Web Server
========================
Flask 后端服务，提供 API 接口返回 BTC 指标数据
"""

import sys
import os
import json
import tempfile
import threading

# 添加当前目录到路径以导入 btc_dashboard（btc_dashboard.py 与 app.py 同级）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, render_template, jsonify, request, make_response
from datetime import datetime, timedelta
import numpy as np


def _swr_headers(response, fresh_s: int, swr_s: int):
    """为 SWR 响应加 Cache-Control（fresh 期内直接命中，过期后允许浏览器使用 stale 数据）"""
    response.headers['Cache-Control'] = (
        f'public, max-age={max(0, fresh_s)}, stale-while-revalidate={swr_s}'
    )
    return response


# ── 缓存持久化（跨重启/多 worker 共享）─────────────────────────────
# 缓存目录可通过 BTC_CACHE_DIR 环境变量配置，默认 ./cache/
_CACHE_DIR = os.environ.get(
    "BTC_CACHE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
)
try:
    os.makedirs(_CACHE_DIR, exist_ok=True)
except OSError as e:
    print(f"⚠️ 无法创建缓存目录 {_CACHE_DIR}: {e}")


def _cache_path(key: str) -> str:
    return os.path.join(_CACHE_DIR, f"{key}.json")


def _load_cache_from_disk(key: str):
    """读取磁盘缓存，返回 (data, timestamp) 或 (None, None)"""
    path = _cache_path(key)
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        ts = datetime.fromisoformat(obj["timestamp"])
        return obj["data"], ts
    except Exception as e:
        print(f"⚠️ 读取缓存失败 {key}: {e}")
        return None, None


def _save_cache_to_disk(key: str, data, timestamp: datetime):
    """原子写入缓存到磁盘（tmpfile + rename，避免并发读到半截文件）"""
    path = _cache_path(key)
    payload = {"timestamp": timestamp.isoformat(), "data": data}
    try:
        # 写入同目录的临时文件后 rename，POSIX 上 rename 是原子的
        fd, tmp = tempfile.mkstemp(dir=_CACHE_DIR, prefix=f".{key}_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except Exception as e:
        print(f"⚠️ 写入缓存失败 {key}: {e}")

# 导入 dashboard 运行函数和历史数据函数
from btc_dashboard import (
    run_dashboard, get_indicator_history, fetch_btc_data,
    get_sparklines,
    fetch_crypto_news, fetch_whale_activity, fetch_macro_calendar,
    fetch_crypto_calendar, fetch_whale_volume_stats, fetch_exchange_balance_display,
    fetch_builders_feed, fetch_dat_holdings
)
from btc_dashboard.score_history import record_score_snapshot, get_score_history
from btc_dashboard.derivatives import fetch_derivatives_panel
from btc_dashboard.etf_flow import fetch_etf_flow_history

app = Flask(__name__)
app.json.sort_keys = False  # 保持后端字典插入序 (指标卡片按 runner._CARD_ORDER 语义排序)

# ── BTC 价格/指标缓存（5 分钟）──────────────────────────────────────
_btc_data_cache = None
_btc_data_timestamp = None
_btc_data_lock = threading.Lock()

# ── 仪表盘缓存（stale-while-revalidate，5 分钟 TTL）─────────────────
_dashboard_cache = None
_dashboard_cache_timestamp = None
_dashboard_refreshing = False
_dashboard_lock = threading.Lock()
_DASHBOARD_TTL = 300              # 5 分钟

# ── 资讯缓存（stale-while-revalidate，15 分钟 TTL）──────────────────
_news_cache = None
_news_cache_timestamp = None
_news_refreshing = False          # 防止并发重复刷新
_news_lock = threading.Lock()
_NEWS_TTL = 900                   # 15 分钟

# ── 开发者动态缓存（stale-while-revalidate，30 分钟 TTL）────────────
_builders_cache = None
_builders_cache_timestamp = None
_builders_refreshing = False
_builders_lock = threading.Lock()
_BUILDERS_TTL = 3600              # 1 小时（与摘要刷新对齐）

# ── 衍生品面板缓存（stale-while-revalidate，10 分钟 TTL）────────────
_derivatives_cache = None
_derivatives_cache_timestamp = None
_derivatives_refreshing = False
_derivatives_lock = threading.Lock()
_DERIVATIVES_TTL = 600            # 10 分钟


def _do_refresh_dashboard():
    """在后台线程中刷新仪表盘缓存。"""
    global _dashboard_cache, _dashboard_cache_timestamp, _dashboard_refreshing
    try:
        result = run_dashboard()

        indicators_json = {}
        for name, ind in result.indicators.items():
            indicators_json[name] = {
                "name": ind.name,
                "value": None if np.isnan(ind.value) else float(ind.value),
                "score": ind.score,
                "color": ind.color,
                "status": ind.status,
                "priority": ind.priority,
                "url": ind.url,
                "description": ind.description,
                "method": ind.method
            }

        df = get_cached_btc_data()
        sparklines = get_sparklines(df, result.indicators, days=7)

        _dashboard_cache = {
            "timestamp": result.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            "btc_price": float(result.btc_price),
            "total_score": float(result.total_score),
            "recommendation": result.recommendation,
            "tactical_score": float(result.tactical_score),
            "tactical_recommendation": result.tactical_recommendation,
            "cycle_buckets": result.cycle_buckets,
            "tactical_buckets": result.tactical_buckets,
            "indicators": indicators_json,
            "sparklines": sparklines
        }
        _dashboard_cache_timestamp = datetime.now()
        _save_cache_to_disk("dashboard", _dashboard_cache, _dashboard_cache_timestamp)
        # 记录每日评分快照（用于评分历史曲线 + 今日信号变化）
        try:
            record_score_snapshot(_dashboard_cache, _CACHE_DIR)
        except Exception as e:
            print(f"⚠️ 评分快照记录失败: {e}")
        print(f"✅ 仪表盘缓存刷新完成 {_dashboard_cache_timestamp.strftime('%H:%M:%S')}")
    except Exception as e:
        global _last_error
        import traceback
        traceback.print_exc()
        _last_error = f"{type(e).__name__}: {e}"
        print(f"⚠️ 仪表盘缓存刷新失败: {e}")
    finally:
        _dashboard_refreshing = False


def trigger_dashboard_refresh():
    """触发后台刷新仪表盘（若未在刷新中）。"""
    global _dashboard_refreshing
    with _dashboard_lock:
        if _dashboard_refreshing:
            return
        _dashboard_refreshing = True
    t = threading.Thread(target=_do_refresh_dashboard, daemon=True)
    t.start()


def _do_refresh_news():
    """在后台线程中刷新资讯缓存。"""
    global _news_cache, _news_cache_timestamp, _news_refreshing
    from concurrent.futures import ThreadPoolExecutor, as_completed
    try:
        tasks = {
            "news":             lambda: fetch_crypto_news(limit=100),
            "whales":           lambda: fetch_whale_activity(min_btc=10, limit=50),
            "whale_stats":      lambda: fetch_whale_volume_stats(),
            "exchange_balance": lambda: fetch_exchange_balance_display(),
            "calendar":         lambda: fetch_macro_calendar(),
            "crypto_calendar":  lambda: fetch_crypto_calendar(),
            "etf_flow":         lambda: fetch_etf_flow_history(limit=15),
            "dat_holdings":     lambda: fetch_dat_holdings(limit=8),
        }
        results = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(fn): key for key, fn in tasks.items()}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    results[key] = future.result(timeout=30)
                except Exception as e:
                    print(f"⚠️ {key} 获取失败: {e}")
                    results[key] = [] if key in ("news", "whales", "calendar", "crypto_calendar") else {}
        # 某个源抓取为空时保留旧缓存值，避免瞬时网络故障把空结果写进缓存
        old = _news_cache or {}
        for key in list(results.keys()):
            if not results[key] and old.get(key):
                print(f"⚠️ {key} 本次为空，沿用旧缓存")
                results[key] = old[key]
        _news_cache = results
        _news_cache_timestamp = datetime.now()
        if not results.get("news"):
            # 快讯仍为空 → 缩短有效期，约 2 分钟后下次请求自动重试
            _news_cache_timestamp -= timedelta(seconds=_NEWS_TTL - 120)
        _save_cache_to_disk("news", _news_cache, _news_cache_timestamp)
        print(f"✅ 资讯缓存刷新完成 {datetime.now().strftime('%H:%M:%S')}")
    except Exception as e:
        import traceback
        traceback.print_exc()
    finally:
        _news_refreshing = False


def trigger_news_refresh():
    """触发后台刷新（若未在刷新中）。"""
    global _news_refreshing
    with _news_lock:
        if _news_refreshing:
            return
        _news_refreshing = True
    t = threading.Thread(target=_do_refresh_news, daemon=True)
    t.start()


def _do_refresh_builders():
    """在后台线程中刷新开发者动态缓存。"""
    global _builders_cache, _builders_cache_timestamp, _builders_refreshing
    try:
        data = fetch_builders_feed(limit=30)
        if not data.get("total"):
            # 全部源为空：视为抓取失败
            if _builders_cache and _builders_cache.get("total"):
                # 保留旧数据且不更新时间戳 → 缓存仍过期，下次请求继续触发重试
                print("⚠️ 开发者动态抓取为空，保留旧缓存")
                return
            # 无可用旧数据：缓存空结果但缩短有效期，约 2 分钟后自动重试
            _builders_cache = data
            _builders_cache_timestamp = datetime.now() - timedelta(seconds=_BUILDERS_TTL - 120)
            _save_cache_to_disk("builders", _builders_cache, _builders_cache_timestamp)
            print("⚠️ 开发者动态抓取为空（无旧缓存），2 分钟后重试")
            return
        _builders_cache = data
        _builders_cache_timestamp = datetime.now()
        _save_cache_to_disk("builders", _builders_cache, _builders_cache_timestamp)
        print(f"✅ 开发者动态缓存刷新完成 {_builders_cache_timestamp.strftime('%H:%M:%S')}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"⚠️ 开发者动态缓存刷新失败: {e}")
    finally:
        _builders_refreshing = False


def trigger_builders_refresh():
    """触发后台刷新开发者动态（若未在刷新中）。"""
    global _builders_refreshing
    with _builders_lock:
        if _builders_refreshing:
            return
        _builders_refreshing = True
    t = threading.Thread(target=_do_refresh_builders, daemon=True)
    t.start()


def _do_refresh_derivatives():
    """在后台线程中刷新衍生品面板缓存。"""
    global _derivatives_cache, _derivatives_cache_timestamp, _derivatives_refreshing
    try:
        data = fetch_derivatives_panel()
        _derivatives_cache = data
        _derivatives_cache_timestamp = datetime.now()
        _save_cache_to_disk("derivatives", _derivatives_cache, _derivatives_cache_timestamp)
        print(f"✅ 衍生品面板缓存刷新完成 {_derivatives_cache_timestamp.strftime('%H:%M:%S')}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"⚠️ 衍生品面板缓存刷新失败: {e}")
    finally:
        _derivatives_refreshing = False


def trigger_derivatives_refresh():
    """触发后台刷新衍生品面板（若未在刷新中）。"""
    global _derivatives_refreshing
    with _derivatives_lock:
        if _derivatives_refreshing:
            return
        _derivatives_refreshing = True
    t = threading.Thread(target=_do_refresh_derivatives, daemon=True)
    t.start()


def get_cached_btc_data():
    """获取缓存的 BTC 数据"""
    global _btc_data_cache, _btc_data_timestamp

    # 缓存 5 分钟
    if _btc_data_cache is None or _btc_data_timestamp is None or \
       (datetime.now() - _btc_data_timestamp).total_seconds() > 300:
        _btc_data_cache = fetch_btc_data()
        _btc_data_timestamp = datetime.now()

    return _btc_data_cache


# ── 启动时：先从磁盘加载已有缓存（多 worker / 重启共享）────────────
def _bootstrap_disk_cache():
    """从磁盘恢复缓存（若存在），避免每次重启都冷启动"""
    global _dashboard_cache, _dashboard_cache_timestamp
    global _news_cache, _news_cache_timestamp
    global _builders_cache, _builders_cache_timestamp

    d_data, d_ts = _load_cache_from_disk("dashboard")
    if d_data is not None:
        _dashboard_cache = d_data
        _dashboard_cache_timestamp = d_ts
        print(f"📦 从磁盘恢复仪表盘缓存（{d_ts.strftime('%Y-%m-%d %H:%M:%S')}）")

    n_data, n_ts = _load_cache_from_disk("news")
    if n_data is not None:
        _news_cache = n_data
        _news_cache_timestamp = n_ts
        print(f"📦 从磁盘恢复资讯缓存（{n_ts.strftime('%Y-%m-%d %H:%M:%S')}）")

    b_data, b_ts = _load_cache_from_disk("builders")
    if b_data is not None:
        _builders_cache = b_data
        _builders_cache_timestamp = b_ts
        print(f"📦 从磁盘恢复开发者动态缓存（{b_ts.strftime('%Y-%m-%d %H:%M:%S')}）")

    global _derivatives_cache, _derivatives_cache_timestamp
    dv_data, dv_ts = _load_cache_from_disk("derivatives")
    if dv_data is not None:
        _derivatives_cache = dv_data
        _derivatives_cache_timestamp = dv_ts
        print(f"📦 从磁盘恢复衍生品面板缓存（{dv_ts.strftime('%Y-%m-%d %H:%M:%S')}）")

_bootstrap_disk_cache()


# ── 启动时预热缓存（延迟 5s，避免启动瞬间内存峰值）──────────────────
def _delayed_warmup():
    import time as _t
    _t.sleep(5)
    trigger_dashboard_refresh()
    trigger_news_refresh()
    _t.sleep(10)
    trigger_derivatives_refresh()
    _t.sleep(5)
    trigger_builders_refresh()

threading.Thread(target=_delayed_warmup, daemon=True).start()


# ── 评分历史回填（幂等, 一次性; 失败每 30 分钟重试, 最多 8 次）────────
def _backfill_worker():
    import time as _t
    from btc_dashboard.backfill import ensure_backfilled
    _t.sleep(30)  # 错开预热高峰, 给 bitcoin-data.com 限额留余量
    for attempt in range(8):
        try:
            ensure_backfilled(_CACHE_DIR, days=90)
            return
        except Exception as e:
            print(f"⚠️ 评分历史回填失败 (第 {attempt+1}/8 次): {e}")
            _t.sleep(1800)

threading.Thread(target=_backfill_worker, daemon=True).start()


_last_error = None  # 记录最近一次后台错误

@app.route('/api/version')
def api_version():
    """部署版本检查"""
    import sys
    return jsonify({
        "version": "compass-v1",
        "server_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC'),
        "python": sys.version,
        "dashboard_ready": _dashboard_cache is not None,
        "news_ready": _news_cache is not None,
        "refreshing": _dashboard_refreshing,
        "last_error": _last_error
    })


@app.route('/')
def index():
    """主页"""
    return render_template('index.html')


@app.route('/api/dashboard')
def api_dashboard():
    """
    API 端点：返回仪表盘数据
    策略：非阻塞 stale-while-revalidate
      - 有缓存 → 立即返回，若已过期则同时触发后台刷新
      - 无缓存 → 立即返回 computing=True，前端轮询直到就绪
        （避免 Render 等平台 30s 请求超时导致 504）
    """
    global _dashboard_cache, _dashboard_cache_timestamp

    now = datetime.now()
    cache_age = int((now - _dashboard_cache_timestamp).total_seconds()) if _dashboard_cache_timestamp else None
    has_cache = _dashboard_cache is not None

    if has_cache:
        if cache_age is not None and cache_age >= _DASHBOARD_TTL:
            trigger_dashboard_refresh()
        resp = jsonify({
            "success": True,
            "cached": True,
            "cache_age_s": cache_age,
            **_dashboard_cache
        })
        fresh_left = max(0, _DASHBOARD_TTL - (cache_age or 0))
        return _swr_headers(resp, fresh_left, _DASHBOARD_TTL)

    # 无缓存（冷启动）：立即返回 computing 状态，前端负责轮询
    trigger_dashboard_refresh()
    resp = jsonify({
        "success": False,
        "computing": True,
        "error": "指标计算中，请稍候…"
    })
    resp.headers['Cache-Control'] = 'no-store'
    return resp, 202


@app.route('/api/history/<indicator_name>')
def api_history(indicator_name: str):
    """API 端点：返回指标历史数据"""
    try:
        days = request.args.get('days', 30, type=int)
        days = min(max(days, 7), 90)  # 限制 7-90 天

        # 获取缓存的 BTC 数据
        df = get_cached_btc_data()

        # 获取历史数据
        history = get_indicator_history(indicator_name, df, days)

        resp = jsonify({
            "success": True,
            **history
        })
        resp.headers['Cache-Control'] = 'public, max-age=300, stale-while-revalidate=600'
        return resp

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/score-history')
def api_score_history():
    """API 端点：返回综合评分历史时序 + 今日信号变化"""
    try:
        days = request.args.get('days', 90, type=int)
        days = min(max(days, 7), 365)
        data = get_score_history(_CACHE_DIR, days)
        resp = jsonify({"success": True, **data})
        resp.headers['Cache-Control'] = 'public, max-age=300, stale-while-revalidate=600'
        return resp
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/derivatives')
def api_derivatives():
    """
    API 端点：返回衍生品杠杆面板数据（OI / 资金费率 / 多空比 / 行情性质）
    策略：stale-while-revalidate，同 /api/dashboard
    """
    global _derivatives_cache, _derivatives_cache_timestamp

    now = datetime.now()
    cache_age = int((now - _derivatives_cache_timestamp).total_seconds()) if _derivatives_cache_timestamp else None
    has_cache = _derivatives_cache is not None

    if has_cache:
        if cache_age is not None and cache_age >= _DERIVATIVES_TTL:
            trigger_derivatives_refresh()
        resp = jsonify({
            "success": True,
            "cached": True,
            "cache_age_s": cache_age,
            **_derivatives_cache
        })
        fresh_left = max(0, _DERIVATIVES_TTL - (cache_age or 0))
        return _swr_headers(resp, fresh_left, _DERIVATIVES_TTL)

    # 无缓存（冷启动）：立即返回 computing 状态，前端负责轮询
    trigger_derivatives_refresh()
    resp = jsonify({
        "success": False,
        "computing": True,
        "error": "衍生品数据加载中，请稍候…"
    })
    resp.headers['Cache-Control'] = 'no-store'
    return resp, 202


@app.route('/api/news')
def api_news():
    """
    API 端点：返回资讯信息
    策略：stale-while-revalidate
      - 有缓存 → 立即返回，若已过期则同时触发后台刷新
      - 无缓存 → 同步等待首次刷新完成（仅冷启动时）
    """
    global _news_cache, _news_cache_timestamp

    now = datetime.now()
    cache_age = int((now - _news_cache_timestamp).total_seconds()) if _news_cache_timestamp else None
    has_cache = _news_cache is not None

    if has_cache:
        # 缓存过期 → 后台异步刷新，本次仍返回旧数据
        if cache_age is not None and cache_age >= _NEWS_TTL:
            trigger_news_refresh()
        resp = jsonify({
            "success": True,
            "cached": True,
            "cache_age_s": cache_age,
            **_news_cache
        })
        fresh_left = max(0, _NEWS_TTL - (cache_age or 0))
        return _swr_headers(resp, fresh_left, _NEWS_TTL)

    # 无缓存（冷启动）：立即返回 computing 状态，避免阻塞 worker
    trigger_news_refresh()
    resp = jsonify({
        "success": False,
        "computing": True,
        "error": "资讯加载中，请稍候…"
    })
    resp.headers['Cache-Control'] = 'no-store'
    return resp, 202


@app.route('/api/builders')
def api_builders():
    """
    API 端点：返回 Bitcoin 开发者社区动态
    策略：stale-while-revalidate
      - 有缓存 → 立即返回，若已过期则同时触发后台刷新
      - 无缓存 → 同步等待首次刷新完成（仅冷启动时）
    """
    global _builders_cache, _builders_cache_timestamp

    now = datetime.now()
    cache_age = int((now - _builders_cache_timestamp).total_seconds()) if _builders_cache_timestamp else None
    has_cache = _builders_cache is not None

    if has_cache:
        if cache_age is not None and cache_age >= _BUILDERS_TTL:
            trigger_builders_refresh()
        resp = jsonify({
            "success": True,
            "cached": True,
            "cache_age_s": cache_age,
            **_builders_cache
        })
        fresh_left = max(0, _BUILDERS_TTL - (cache_age or 0))
        return _swr_headers(resp, fresh_left, _BUILDERS_TTL)

    # 无缓存（冷启动）：立即返回 computing 状态，前端负责轮询
    trigger_builders_refresh()
    resp = jsonify({
        "success": False,
        "computing": True,
        "error": "开发者动态加载中，请稍候…"
    })
    resp.headers['Cache-Control'] = 'no-store'
    return resp, 202


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5050))
    debug = os.environ.get('FLASK_ENV') != 'production'
    print(f"🚀 启动 BTC Dashboard Web 服务器 (port={port})...")
    print(f"📊 访问 http://localhost:{port} 查看仪表盘")
    app.run(debug=debug, use_reloader=False, host='0.0.0.0', port=port)
