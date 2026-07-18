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
from btc_dashboard.score_history import record_score_snapshot, get_score_history, load_history_entries
from btc_dashboard.scoring import factor_coverage_from_buckets
from btc_dashboard.decision import compute_decision
from btc_dashboard.derivatives import fetch_derivatives_panel
from btc_dashboard.options import fetch_options_panel
from btc_dashboard.probdist import fetch_probdist_panel
from btc_dashboard.etf_flow import fetch_etf_flow_history

app = Flask(__name__)
app.json.sort_keys = False


@app.after_request
def _add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


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

# ── 衍生品/期权/概率分布面板: PanelCache 收敛(SWR + partial 守卫 + 磁盘持久化) ──
from panel_cache import PanelCache

DERIVATIVES_PANEL = PanelCache("derivatives", fetch_derivatives_panel, ttl=600, label="衍生品面板",
                               save_fn=_save_cache_to_disk, load_fn=_load_cache_from_disk)
OPTIONS_PANEL = PanelCache("options", fetch_options_panel, ttl=600, label="期权面板",
                           save_fn=_save_cache_to_disk, load_fn=_load_cache_from_disk)
PROBDIST_PANEL = PanelCache("probdist", fetch_probdist_panel, ttl=600, label="概率分布面板",
                            save_fn=_save_cache_to_disk, load_fn=_load_cache_from_disk)


def _do_refresh_dashboard():
    """在后台线程中刷新仪表盘缓存。

    并发原子性 (2026-07 concurrency 收口): 完整结果 —— 含 indicators/sparklines/
    decision/cycle_phase/notify 全部字段 —— 先在局部 dict ``local`` 上算齐, 最后
    一次性替换全局 ``_dashboard_cache`` 并落盘。刷新期间 /api/dashboard 读到的始终
    是"旧的完整缓存"或"新的完整缓存", 绝不会撞见"新时间戳但缺 decision/cycle_phase/
    notify"的半成品 (旧实现先挂空壳 dict 再原地补字段, 有此中间态)。
    """
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

        # 价格史收敛 (2026-07): 直接复用 run_dashboard 评分所用的 df (result.price_df,
        # 已含实时价追加) —— 火花图/相位与评分逐点同一份数据, 且整轮刷新只 fetch 一次;
        # 并回灌共享缓存令 /api/history 也复用 (锁内写, 与 get_cached_btc_data 同护)。
        # price_df 缺席时 (异常兜底) 退回共享缓存老路。
        global _btc_data_cache, _btc_data_timestamp
        df = result.price_df
        if df is not None and len(df):
            with _btc_data_lock:
                _btc_data_cache = df
                _btc_data_timestamp = datetime.now()
        else:
            df = get_cached_btc_data()
        sparklines = get_sparklines(df, result.indicators, days=7)

        local = {
            "timestamp": result.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            "btc_price": float(result.btc_price),
            "total_score": float(result.total_score),
            "recommendation": result.recommendation,
            "tactical_score": float(result.tactical_score),
            "tactical_recommendation": result.tactical_recommendation,
            "cycle_buckets": result.cycle_buckets,
            "tactical_buckets": result.tactical_buckets,
            "data_source": result.data_source,
            "data_synthetic": result.data_synthetic,
            "cycle_coverage": result.cycle_coverage,
            "tactical_coverage": result.tactical_coverage,
            "cycle_factor_coverage": factor_coverage_from_buckets(result.cycle_buckets),
            "tactical_factor_coverage": factor_coverage_from_buckets(result.tactical_buckets),
            "price_stale": result.price_stale,
            "price_history_last_date": result.price_history_last_date,
            "price_history_lag_days": result.price_history_lag_days,
            "trigger_levels": result.trigger_levels,
            "indicators": indicators_json,
            "sparklines": sparklines
        }
        # 记录每日评分快照（用于评分历史曲线 + 今日信号变化）
        # 合成演示数据不入历史 — 避免污染评分曲线与信号变化检测。
        # 价格全源陈旧 (price_stale) 同样不入历史: 评分基于错位均线窗口, 分数不可信,
        # 写入会污染滞回重放历史 (陈旧分被后续档位重放误用); 陈旧数据仍供展示 (frozen
        # 已置灰可执行数字), 但不进决策历史链 (2026-07 收口 Codex 复审 P1)。
        if result.data_synthetic:
            print("🚨 演示数据 — 跳过评分快照记录")
        elif result.price_stale:
            print(f"🚨 价格全源陈旧 (滞后 {result.price_history_lag_days} 天) — "
                  f"跳过评分快照记录, 避免污染滞回重放历史")
        else:
            try:
                record_score_snapshot(local, _CACHE_DIR)
            except Exception as e:
                print(f"⚠️ 评分快照记录失败: {e}")

        # 量化决策 (滞回仓位 + 执行节奏) — 依赖已落盘的评分历史, 故在快照之后算
        try:
            local["decision"] = compute_decision(
                local, load_history_entries(_CACHE_DIR))
        except Exception as e:
            print(f"⚠️ 量化决策计算失败: {e}")
            local["decision"] = None

        # 周期相位判读 (叙事层, 规则式 + 历史频率置信度) — 依赖评分历史与完整价格史。
        # 演示数据熔断: 合成价格算出的相位是无意义的, 不展示 (2026-07 对抗审查)
        try:
            if result.data_synthetic:
                local["cycle_phase"] = None
            else:
                from btc_dashboard.cycle_phase import compute_cycle_phase
                price_map = {ts.strftime("%Y-%m-%d"): float(v)
                             for ts, v in df["price"].dropna().items()}
                local["cycle_phase"] = compute_cycle_phase(
                    load_history_entries(_CACHE_DIR), price_map)
        except Exception as e:
            print(f"⚠️ 周期相位计算失败: {e}")
            local["cycle_phase"] = None

        # 决策提醒推送 (换档/战术极值/相位变化 → 企微等渠道) — 依赖 decision 与
        # cycle_phase, 故在其后; check_and_alert 自身永不抛异常, 未配置渠道时为空操作。
        # 摘要挂进缓存暴露给 /api/dashboard: "渠道未配置"必须可观测,
        # 不能与"一切正常"不可区分 (2026-07 对抗审查发现)
        from btc_dashboard.notify import check_and_alert
        local["notify"] = check_and_alert(local, _CACHE_DIR)

        # 原子发布: local 已算齐全部字段, 一次性替换全局缓存。先发布 dict、再更新
        # 时间戳 —— 读者 (api_dashboard) 先读时间戳后读 dict, 此序下"看到新时间戳"
        # 必然对应"已发布的新完整 dict", 不会出现"新时间戳配旧/半成品 dict"。
        # 落盘也只在 local 完整之后发生一次, 磁盘缓存永不落半成品 (重启恢复安全)。
        _dashboard_cache = local
        _dashboard_cache_timestamp = datetime.now()
        _save_cache_to_disk("dashboard", local, _dashboard_cache_timestamp)
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


# NOTE (价格史双取, 待 runner.py 侧收敛): 一次仪表盘刷新目前仍有两次 fetch_btc_data ——
# ① run_dashboard() 内部 (runner.py) 取一份并把末行改写为实时价用于评分; ② 本函数取一份
# 供火花图/相位/api-history 展示。二者是各自独立的网络请求, app.py 侧无法访问 ① 的 df
# (DashboardResult 未携带, 也无模块级出口)。彻底收敛为单取需 runner.py 让 run_dashboard
# 把已评分的 df 暴露出来 (加入 DashboardResult 或存模块级), 之后本函数即可优先复用它、仅在
# 缺失时才自取。当前在 app.py 侧能做到的最优: 本函数加锁单飞去重展示层的重复 fetch, 并在刷新
# 时于副本上复刻"末行=实时价"补丁, 使展示末点与评分一致 (见 _do_refresh_dashboard)。
def get_cached_btc_data():
    """获取缓存的 BTC 价格史 (5 分钟 TTL)。

    _btc_data_lock 串行化"检查→取数→写缓存"整段: 刷新线程 (火花图/相位) 与
    /api/history 请求线程可能并发命中过期/空缓存, 无锁时会各自穿透去 fetch —— 既
    重复打限流的价格 API, 又可能拿到不同源的两份 df 令展示层不自洽。加锁后并发者
    串行, 后到者直接复用先到者刚写入的同一份缓存 (单飞)。fetch 在锁内进行是有意为
    之: 让后到者等待并复用结果, 而非各自发起网络请求。"""
    global _btc_data_cache, _btc_data_timestamp

    with _btc_data_lock:
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

    # derivatives/options/probdist 由 PanelCache 构造时自行回灌(load_fn), 不在此处

_bootstrap_disk_cache()


# ── 启动时预热缓存（延迟 5s，避免启动瞬间内存峰值）──────────────────
def _delayed_warmup():
    import time as _t
    _t.sleep(5)
    trigger_dashboard_refresh()
    trigger_news_refresh()
    _t.sleep(10)
    DERIVATIVES_PANEL.trigger_refresh()
    _t.sleep(5)
    OPTIONS_PANEL.trigger_refresh()
    _t.sleep(5)
    PROBDIST_PANEL.trigger_refresh()
    _t.sleep(5)
    trigger_builders_refresh()

if not os.environ.get("BTC_DISABLE_WARMUP"):
    threading.Thread(target=_delayed_warmup, daemon=True).start()


# ── 评分历史回填（幂等, 一次性; 失败每 30 分钟重试, 最多 8 次）────────
def _backfill_worker():
    import time as _t
    from btc_dashboard.backfill import ensure_backfilled, backfill_dvol
    _t.sleep(30)  # 错开预热高峰, 给 bitcoin-data.com 限额留余量
    for attempt in range(8):
        try:
            # 365 天: 喂满 decision.REPLAY_DAYS 的滞回重放窗口 + 支撑月/年尺度变化展示
            ensure_backfilled(_CACHE_DIR, days=365)
            break
        except Exception as e:
            print(f"⚠️ 评分历史回填失败 (第 {attempt+1}/8 次): {e}")
            _t.sleep(1800)

    # DVOL 日线回填（独立 try/except：DVOL 拉取失败不应影响/阻塞评分历史回填，
    # 也不参与上面的 8 次重试；写入 btc_dashboard/data/dvol_history.json，
    # 持久参考数据，非 _CACHE_DIR 里的运行时缓存）
    try:
        n = backfill_dvol()
        if n:
            print(f"✅ DVOL 历史回填完成: 新增 {n} 点")
    except Exception as e:
        print(f"⚠️ DVOL 历史回填失败: {e}")

if not os.environ.get("BTC_DISABLE_WARMUP"):
    threading.Thread(target=_backfill_worker, daemon=True).start()


_last_error = None  # 记录最近一次后台错误

@app.route('/api/version')
def api_version():
    """部署版本检查"""
    import sys
    from btc_dashboard.version_stamp import config_fingerprint, engine_sha
    _engine, _config = engine_sha(), config_fingerprint()
    return jsonify({
        "version": f"{_engine}-{_config}",
        "engine_sha": _engine,
        "config_hash": _config,
        "server_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC'),
        "python": sys.version,
        "dashboard_ready": _dashboard_cache is not None,
        "news_ready": _news_cache is not None,
        "refreshing": _dashboard_refreshing,
        "last_error": _last_error
    })


@app.route('/api/notify-test')
def api_notify_test():
    """一次性推送联通性测试。默认禁用 (NOTIFY_TEST_TOKEN 未设时返回 404, 零攻击面);
    设了 token 才'武装', 需 ?token= 完全匹配。测完在 Render 删除该环境变量即失效。
    绕过状态机直发, 不影响真实提醒。"""
    import hmac
    expected = os.environ.get("NOTIFY_TEST_TOKEN", "").strip()
    if not expected:
        return jsonify({"success": False, "error": "not found"}), 404
    supplied = (request.args.get("token") or "").strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        return jsonify({"success": False, "error": "forbidden"}), 403
    from btc_dashboard.notify import send_test
    result = send_test(_CACHE_DIR)
    return jsonify({"success": result["any"], **result})


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
        # 事件标记 (上穿档位/转负/滞回换档), 每个事件自带诚实统计口径 —
        # 事件研究结论: 不作为胜率信号, 仅周期叙事参考
        try:
            from btc_dashboard.decision import extract_events
            window_dates = {s["date"] for s in data.get("series", [])}
            data["events"] = [ev for ev in extract_events(load_history_entries(_CACHE_DIR))
                              if ev["date"] in window_dates]
        except Exception as e:
            print(f"⚠️ 事件标记提取失败: {e}")
            data["events"] = []
        resp = jsonify({"success": True, **data})
        resp.headers['Cache-Control'] = 'public, max-age=300, stale-while-revalidate=600'
        return resp
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/cycle-events')
def api_cycle_events():
    """周期相位与历史大事件规律 (静态资产 + 动态相位; 事件 n=3~4 带混杂标注)"""
    try:
        from btc_dashboard.cycle_events import get_cycle_events
        data = get_cycle_events()
        if data is None:
            return jsonify({"success": False, "error": "asset missing"}), 404
        resp = jsonify({"success": True, **data})
        resp.headers['Cache-Control'] = 'public, max-age=3600, stale-while-revalidate=7200'
        return resp
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/roadmap')
def api_roadmap():
    """BTC 里程碑路线图 (历史/预定/提案分级 + 动态当前减半位置)"""
    try:
        from btc_dashboard.roadmap import get_roadmap
        data = get_roadmap()
        if data is None:
            return jsonify({"success": False, "error": "asset missing"}), 404
        resp = jsonify({"success": True, **data})
        resp.headers['Cache-Control'] = 'public, max-age=3600, stale-while-revalidate=7200'
        return resp
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/market-patterns')
def api_market_patterns():
    """市场规律与风险 (利率×周期证伪 + 季节性证伪 + 黑天鹅画像; 均非交易信号)"""
    try:
        from btc_dashboard.market_patterns import get_market_patterns
        data = get_market_patterns()
        if data is None:
            return jsonify({"success": False, "error": "assets missing"}), 404
        resp = jsonify({"success": True, **data})
        resp.headers['Cache-Control'] = 'public, max-age=3600, stale-while-revalidate=7200'
        return resp
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


def _serve_panel(panel, computing_msg: str):
    """SWR 面板路由通用体: 有缓存 → 200(过期顺带触发刷新); 无缓存 → 202 computing。"""
    cache_age = panel.age_s()
    if panel.cache is not None:
        if cache_age is not None and cache_age >= panel.ttl:
            panel.trigger_refresh()
        resp = jsonify({"success": True, "cached": True, "cache_age_s": cache_age, **panel.cache})
        return _swr_headers(resp, max(0, panel.ttl - (cache_age or 0)), panel.ttl)
    panel.trigger_refresh()
    resp = jsonify({"success": False, "computing": True, "error": computing_msg})
    resp.headers['Cache-Control'] = 'no-store'
    return resp, 202


@app.route('/api/derivatives')
def api_derivatives():
    """API 端点:衍生品杠杆面板(OI/资金费率/多空比/行情性质)。SWR, 同 /api/dashboard。"""
    return _serve_panel(DERIVATIVES_PANEL, "衍生品数据加载中，请稍候…")


@app.route('/api/options')
def api_options():
    """API 端点:BTC 期权面板(DVOL/偏斜/看跌看涨比/期限结构/最大痛点)。SWR。"""
    return _serve_panel(OPTIONS_PANEL, "期权数据加载中，请稍候…")


@app.route('/api/probdist')
def api_probdist():
    """API 端点:BTC 价格概率分布面板(风险中性密度/Polymarket)。SWR。"""
    return _serve_panel(PROBDIST_PANEL, "概率分布数据加载中，请稍候…")


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
