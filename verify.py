#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC Compass 验证器 — 一条命令回答"系统现在是否健康、配置是否自洽"。

用法:
    python3 verify.py                # 冒烟测试 + 探测本地 (127.0.0.1:5070)
    python3 verify.py --live         # 冒烟测试 + 探测 Render 现网
    python3 verify.py --url URL      # 探测指定实例
    python3 verify.py --offline      # 只跑冒烟测试, 不探测运行实例
    python3 verify.py --skip-tests   # 只探测, 跳过冒烟测试

检查内容:
  [1] 冒烟测试 (tests/): 阈值四处对账 / band_stats 同步 / 评分与滞回纯函数
  [2] 运行时探针: 价格与双评分合理性 / 逐因子存活 / 桶覆盖率 /
      决策面板完整性 / 缓存新鲜度 / 评分历史回填深度

退出码: 0 = 全部通过 (允许 WARN), 1 = 存在 FAIL。
供人肉、CI、agent loop 共用 — 这是 loop engineering 的反馈信号层。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
LOCAL_URL = "http://127.0.0.1:5070"
LIVE_URL = "https://btc-compass.onrender.com"

# 冷启动重试: Render free 层唤醒 + 首轮指标计算可能要几分钟
PROBE_RETRIES = 10
PROBE_RETRY_WAIT = 20  # 秒

# 缓存新鲜度阈值 (秒): 超过 WARN 说明刷新变慢, 超过 FAIL 说明刷新链路断了
FRESH_WARN_S = 2 * 3600
FRESH_FAIL_S = 24 * 3600

# 覆盖率阈值: 与 scoring/decision 的 0.5 警戒线之上留缓冲, 早于用户发现劣化
COVERAGE_WARN = 0.85
COVERAGE_FAIL = 0.5

_results = []  # (level, message)


def _report(level, msg):
    icon = {"OK": "✅", "WARN": "⚠️ ", "FAIL": "❌"}[level]
    print(f"  {icon} {msg}")
    _results.append((level, msg))


def _fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "btc-compass-verify/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ────────────────────────────────────────────────────────────
# [1] 冒烟测试
# ────────────────────────────────────────────────────────────

def run_smoke_tests() -> bool:
    print("\n[1/2] 冒烟测试 (配置对账 + 纯函数)")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", os.path.join(REPO_ROOT, "tests"),
         "-q", "--no-header", "-x", "--tb=short"],
        cwd=REPO_ROOT, capture_output=True, text=True)
    tail = (proc.stdout or "").strip().splitlines()
    summary = tail[-1] if tail else "(无输出)"
    if proc.returncode == 0:
        _report("OK", f"pytest: {summary}")
        return True
    print(proc.stdout)
    print(proc.stderr, file=sys.stderr)
    _report("FAIL", f"pytest: {summary}")
    return False


# ────────────────────────────────────────────────────────────
# [2] 运行时探针
# ────────────────────────────────────────────────────────────

def _get_dashboard(base_url):
    """拉 /api/dashboard, 容忍冷启动 computing 状态。"""
    last_err = None
    for attempt in range(1, PROBE_RETRIES + 1):
        try:
            data = _fetch_json(f"{base_url}/api/dashboard")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            last_err = e
            data = None
        if data and data.get("success"):
            return data
        if data and data.get("computing"):
            print(f"  ⏳ 实例计算中 (第 {attempt}/{PROBE_RETRIES} 次), "
                  f"{PROBE_RETRY_WAIT}s 后重试…")
        elif last_err is not None and attempt == 1 and base_url == LIVE_URL:
            print(f"  ⏳ 现网未响应 (可能 free 层冷启动), 重试中…")
        elif last_err is not None and base_url != LIVE_URL:
            break  # 本地连不上没必要反复等
        if attempt < PROBE_RETRIES:
            time.sleep(PROBE_RETRY_WAIT)
    return {"__error__": str(last_err) if last_err else "computing 未在重试窗口内完成"}


def probe_runtime(base_url) -> None:
    print(f"\n[2/2] 运行时探针: {base_url}")

    # 因子桶配置 = 期望存活的因子清单 (从 scoring 导入, 与评分引擎同源)
    sys.path.insert(0, os.path.join(REPO_ROOT, "btc_web"))
    from btc_dashboard import scoring, decision  # noqa: E402

    data = _get_dashboard(base_url)
    if "__error__" in data:
        hint = ("本地服务未启动? 先 `PORT=5070 python3 btc_web/app.py`, 或用 --live 探测现网"
                if base_url == LOCAL_URL else "现网不可达")
        _report("FAIL", f"/api/dashboard 不可用: {data['__error__']} ({hint})")
        return

    # 数据真实性与基本合理性
    if data.get("data_synthetic"):
        _report("FAIL", "价格为合成演示数据 — 所有真实价格源均失效")
    else:
        _report("OK", f"数据源: {data.get('data_source', '?')}")

    price = data.get("btc_price") or 0
    (_report("OK", f"BTC 价格 ${price:,.0f}") if price > 1000
     else _report("FAIL", f"BTC 价格异常: {price!r}"))

    for name, key in (("周期分", "total_score"), ("战术分", "tactical_score")):
        v = data.get(key)
        if v is None or not (-1 <= v <= 1):
            _report("FAIL", f"{name}异常: {v!r} (应在 [-1, 1])")
        else:
            _report("OK", f"{name} {v:+.3f}")

    # 逐因子存活 (以 scoring 桶配置为准绳)
    indicators = data.get("indicators") or {}
    for label, cfg, cov_key in (("周期", scoring.CYCLE_BUCKETS, "cycle_coverage"),
                                ("战术", scoring.TACTICAL_BUCKETS, "tactical_coverage")):
        members = [m for b in cfg.values() for m in b["members"]]
        dead = [m for m in members
                if not indicators.get(m) or indicators[m].get("value") is None]
        if dead:
            _report("WARN", f"{label}因子失效 {len(dead)}/{len(members)}: {', '.join(dead)}")
        else:
            _report("OK", f"{label}因子全部存活 ({len(members)} 个)")

        cov = data.get(cov_key)
        if cov is None:
            _report("WARN", f"{label}覆盖率字段缺失")
        elif cov < COVERAGE_FAIL:
            _report("FAIL", f"{label}覆盖率 {cov:.0%} < {COVERAGE_FAIL:.0%} — 评分已不可信")
        elif cov < COVERAGE_WARN:
            _report("WARN", f"{label}覆盖率 {cov:.0%} (阈值 {COVERAGE_WARN:.0%})")
        else:
            _report("OK", f"{label}覆盖率 {cov:.0%}")

    # 整桶死亡单独报 (比覆盖率更早定位到"哪一路信号瞎了")
    for label, key in (("周期", "cycle_buckets"), ("战术", "tactical_buckets")):
        buckets = data.get(key) or {}
        dead_buckets = [n for n, b in buckets.items() if b.get("score") is None]
        if dead_buckets:
            _report("WARN", f"{label}整桶无有效因子: {', '.join(dead_buckets)}")

    # 决策面板
    dec = data.get("decision")
    if not dec:
        _report("FAIL", "decision 面板缺失 (compute_decision 失败?)")
    else:
        band = dec.get("cycle", {}).get("band")
        known = {b[1] for b in decision.CYCLE_BANDS}
        if band in known:
            lo, hi = dec["cycle"].get("target_lo"), dec["cycle"].get("target_hi")
            _report("OK", f"决策: {band} 目标仓位 {lo}-{hi}% / "
                          f"节奏「{dec.get('tactical', {}).get('pace', '?')}」")
        else:
            _report("FAIL", f"决策档位「{band}」不在已知档位中")
        if dec.get("cycle", {}).get("stats") is None:
            _report("WARN", "决策面板无回测统计 (band_stats.json 未随部署?)")
        for w in dec.get("warnings") or []:
            _report("WARN", f"决策警告: {w}")

    # 缓存新鲜度 (用服务端 cache_age_s, 避免时区坑)
    age = data.get("cache_age_s")
    if age is None:
        _report("WARN", "cache_age_s 缺失")
    elif age > FRESH_FAIL_S:
        _report("FAIL", f"缓存已 {age/3600:.1f} 小时未刷新 — 刷新链路断了")
    elif age > FRESH_WARN_S:
        _report("WARN", f"缓存已 {age/3600:.1f} 小时未刷新")
    else:
        _report("OK", f"缓存新鲜 ({age}s)")

    # 评分历史回填深度 (滞回重放依赖 ≥ HYST_CONFIRM×4 天)
    try:
        hist = _fetch_json(f"{base_url}/api/score-history?days=365")
        n = hist.get("total_days", 0)
        need = decision.HYST_CONFIRM * 4
        if n >= need:
            _report("OK", f"评分历史 {n} 天 (滞回重放需 ≥{need})")
        else:
            _report("WARN", f"评分历史仅 {n} 天 (<{need}), 滞回档位可信度有限")
    except Exception as e:
        _report("WARN", f"/api/score-history 探测失败: {e}")


# ────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="BTC Compass 验证器")
    ap.add_argument("--live", action="store_true", help=f"探测现网 {LIVE_URL}")
    ap.add_argument("--url", help="探测指定实例 URL")
    ap.add_argument("--offline", action="store_true", help="跳过运行时探针")
    ap.add_argument("--skip-tests", action="store_true", help="跳过冒烟测试")
    args = ap.parse_args()

    if not args.skip_tests:
        run_smoke_tests()
    if not args.offline:
        probe_runtime(args.url or (LIVE_URL if args.live else LOCAL_URL))

    fails = [m for lv, m in _results if lv == "FAIL"]
    warns = [m for lv, m in _results if lv == "WARN"]
    print(f"\n{'='*56}")
    if fails:
        print(f"❌ 验证失败: {len(fails)} 项 FAIL, {len(warns)} 项 WARN")
    elif warns:
        print(f"⚠️  验证通过但有 {len(warns)} 项警告")
    else:
        print("✅ 全部通过")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
