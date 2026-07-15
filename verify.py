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
from datetime import datetime, timezone

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

# ── 探针趋势记忆 (loop engineering: 劣化轨迹早于阈值告警) ──
# 探针每跑一次把关键指标 append 进 jsonl, 下轮读近 K 条比中位数, 劣化只 WARN。
# 与绝对阈值互补: 阈值抓"已跌破", 趋势抓"正在滑" —— 比等 FAIL 划算。
PROBE_HISTORY_PATH = os.path.join(REPO_ROOT, "data", "probe_history.jsonl")
TREND_K = 5                 # 参与中位数的历史条数 (launchd 日两跑 → 约 2.5 天窗口)
TREND_MIN_HISTORY = 3       # 少于此不做劣化告警 (样本不足)
TREND_COVERAGE_DROP = 0.10  # 覆盖率比近期中位低这么多则 WARN

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


# ── 趋势记忆: 记录 + 劣化判据 (全部 best-effort, 只 WARN, 绝不 FAIL/崩探针) ──

# writer 与 schema 的字段契约 (test_probe_trends 锁死: 写入键集合 == 此清单)
_PROBE_FIELDS = [
    "ts", "base_url", "data_source", "data_synthetic", "btc_price",
    "cycle_score", "tactical_score", "cycle_coverage", "tactical_coverage",
    "cycle_factors_alive", "cycle_factors_total",
    "tactical_factors_alive", "tactical_factors_total",
    "cache_age_s", "score_history_days", "cycle_band", "tactical_pace",
]


def _probe_metrics(base_url, data, indicators, scoring):
    """从已拉取的 dashboard data 派生一条趋势记录 (纯计算, 无 I/O)。
    score_history_days 由调用方在拿到 /api/score-history 后补写。"""
    indicators = indicators or {}

    def alive_total(cfg):
        members = [m for b in cfg.values() for m in b["members"]]
        dead = [m for m in members
                if not indicators.get(m) or indicators[m].get("value") is None]
        return len(members) - len(dead), len(members)

    ca, ct = alive_total(scoring.CYCLE_BUCKETS)
    ta, tt = alive_total(scoring.TACTICAL_BUCKETS)
    dec = data.get("decision") or {}
    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_url": base_url,
        "data_source": data.get("data_source"),
        "data_synthetic": data.get("data_synthetic"),
        "btc_price": data.get("btc_price"),
        "cycle_score": data.get("total_score"),
        "tactical_score": data.get("tactical_score"),
        "cycle_coverage": data.get("cycle_coverage"),
        "tactical_coverage": data.get("tactical_coverage"),
        "cycle_factors_alive": ca, "cycle_factors_total": ct,
        "tactical_factors_alive": ta, "tactical_factors_total": tt,
        "cache_age_s": data.get("cache_age_s"),
        "score_history_days": None,
        "cycle_band": (dec.get("cycle") or {}).get("band"),
        "tactical_pace": (dec.get("tactical") or {}).get("pace"),
    }


def _record_probe(rec):
    """best-effort 追加一条 jsonl; 任何失败绝不崩探针。"""
    try:
        os.makedirs(os.path.dirname(PROBE_HISTORY_PATH), exist_ok=True)
        with open(PROBE_HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load_probe_history(base_url):
    """读同 base_url 的历史记录; best-effort, 坏行跳过, 文件缺失返 []。"""
    out = []
    try:
        with open(PROBE_HISTORY_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("base_url") == base_url:
                    out.append(r)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return out


def _median(xs):
    xs = sorted(x for x in xs if isinstance(x, (int, float)))
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _check_probe_trends(base_url, rec):
    """与近 K 条同 base_url 历史比, 劣化只报 WARN (绝不 FAIL)。best-effort。"""
    try:
        hist = _load_probe_history(base_url)  # 本轮尚未写入
        if len(hist) < TREND_MIN_HISTORY:
            return
        recent = hist[-TREND_K:]
        for key, label in (("cycle_coverage", "周期覆盖率"),
                           ("tactical_coverage", "战术覆盖率")):
            med = _median([r.get(key) for r in recent])
            cur = rec.get(key)
            if med is not None and cur is not None and cur < med - TREND_COVERAGE_DROP:
                _report("WARN", f"{label}趋势劣化: 当前 {cur:.0%} < 近{len(recent)}次中位 "
                                f"{med:.0%} − {TREND_COVERAGE_DROP:.0%}")
        for key, label in (("cycle_factors_alive", "周期因子存活"),
                           ("tactical_factors_alive", "战术因子存活")):
            med = _median([r.get(key) for r in recent])
            cur = rec.get(key)
            if med is not None and cur is not None and cur < med - 0.5:  # 掉 ≥1
                _report("WARN", f"{label}趋势劣化: 当前 {cur} < 近期中位 {med}")
        days = [r.get("score_history_days") for r in recent
                if isinstance(r.get("score_history_days"), int)]
        cur_days = rec.get("score_history_days")
        if days and isinstance(cur_days, int) and cur_days < max(days):
            _report("WARN", f"评分历史深度回退: {cur_days} < 历史峰值 {max(days)} (回填应只增不减)")
    except Exception:
        pass


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

    # 提醒推送渠道 (换档/战术极值 → 企微): 未配置时功能静默禁用, 必须可见
    notify = data.get("notify")
    if notify is None:
        _report("WARN", "notify 摘要缺失 (旧版部署或字段被移除)")
    elif notify.get("channels"):
        _report("OK", f"提醒渠道已配置: {', '.join(notify['channels'])}")
    else:
        _report("WARN", "提醒渠道未配置 (WECOM_WEBHOOK_URL 未设) — 换档提醒不会推送")

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
    hist_days = None
    try:
        hist = _fetch_json(f"{base_url}/api/score-history?days=365")
        n = hist.get("total_days", 0)
        hist_days = n
        need = decision.HYST_CONFIRM * 4
        if n >= need:
            _report("OK", f"评分历史 {n} 天 (滞回重放需 ≥{need})")
        else:
            _report("WARN", f"评分历史仅 {n} 天 (<{need}), 滞回档位可信度有限")
    except Exception as e:
        _report("WARN", f"/api/score-history 探测失败: {e}")

    # 期权/概率分布面板存活 (SWR 展示卡: 200=有缓存, 202=冷启动 computing; 只 WARN 不翻退出码)
    for path, label, req_keys in (
            ("/api/options", "期权面板", ("dvol_now", "n_contracts")),
            ("/api/probdist", "概率分布面板", ("median", "pdf"))):
        try:
            d = _fetch_json(f"{base_url}{path}")
        except Exception as e:
            _report("WARN", f"{path} 探测失败: {e}")
            continue
        if d.get("computing"):
            _report("WARN", f"{label}冷启动 computing 中 (部署后首个 TTL 内属正常)")
            continue
        if d.get("partial"):
            _report("WARN", f"{label}为 partial (数据源部分失败) — 持续出现说明上游断供")
        dead = [k for k in req_keys if d.get(k) in (None, [], 0)]
        if dead:
            _report("WARN", f"{label}关键字段缺失/为空: {', '.join(dead)}")
        else:
            _report("OK", f"{label}存活 ({', '.join(req_keys)})")

    # 规律版块三端点 (2026-07 审查: 曾在全部质量闸门零覆盖, 端点挂了没人知道)
    # 展示卡性质: 只 WARN 不翻退出码; 额外做策展资产年龄 + 相位翻页过期检测
    def _asset_age_days(gen):
        try:
            return (datetime.now() - datetime.strptime(str(gen)[:10], "%Y-%m-%d")).days
        except Exception:
            return None

    for path, label, req_keys in (
            ("/api/cycle-events", "周期相位卡", ("current", "cycle_phases", "events")),
            ("/api/roadmap", "路线图卡", ("eras", "current")),
            ("/api/market-patterns", "市场规律卡",
             ("rates", "seasonality", "blackswan", "forward_risk"))):
        try:
            d = _fetch_json(f"{base_url}{path}")
        except Exception as e:
            _report("WARN", f"{path} 探测失败: {e}")
            continue
        dead = [k for k in req_keys if not d.get(k)]
        if dead:
            _report("WARN", f"{label}关键字段缺失/为空: {', '.join(dead)}")
        else:
            _report("OK", f"{label}存活 ({', '.join(req_keys)})")

        # 资产年龄: 策展 JSON 无自动更新, 超一年提示重跑研究脚本复核
        age = _asset_age_days(d.get("generated"))
        if age is not None and age > 365:
            _report("WARN", f"{label}资产已 {age} 天未再生成, 建议重跑 backtest 研究脚本复核")

        # 相位翻页: 进行中(partial)周期行所在相位应包含当前月数, 否则资产跨相位过期
        if path == "/api/cycle-events" and not dead:
            try:
                import re as _re
                cur_m = d["current"]["months_since_halving"]
                for ph in d.get("cycle_phases", []):
                    if not any(c.get("partial") for c in ph.get("cycles", [])):
                        continue
                    m = _re.search(r"(\d+)-(\d+)", ph.get("phase", ""))
                    if m and not (int(m.group(1)) <= cur_m < int(m.group(2))):
                        _report("WARN", (f"周期相位卡资产已翻页 (当前 {cur_m} 月越出进行中行"
                                         f"「{ph['phase']}」), 需重跑 backtest/calendar_study.py"))
            except Exception:
                pass

        # 前瞻雷达事实快照 (币价/持仓/量子比特数等) 腐烂快, 超半年提示复核
        if path == "/api/market-patterns":
            fr_age = _asset_age_days((d.get("forward_risk") or {}).get("generated"))
            if fr_age is not None and fr_age > 180:
                _report("WARN", f"前瞻风险雷达事实快照已 {fr_age} 天, 建议重新核实后更新登记册")

    # 趋势记忆: 记录本轮指标 + 与历史比对劣化轨迹 (best-effort, 只 WARN, 不翻退出码)
    try:
        rec = _probe_metrics(base_url, data, indicators, scoring)
        rec["score_history_days"] = hist_days
        _check_probe_trends(base_url, rec)  # 先比历史 (不含本轮)
        _record_probe(rec)                  # 再追加本轮
    except Exception:
        pass


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
