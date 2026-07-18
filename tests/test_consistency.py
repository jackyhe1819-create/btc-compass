#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置一致性对账 — 把 CLAUDE.md 里"改档位阈值后须同步各处"的人工纪律变成机器检查。

周期分档位阈值存在于 5 处, 战术分 3 处, 滞回参数 3 处:
  1. btc_web/btc_dashboard/decision.py      CYCLE_BANDS / TACTICAL_BANDS / HYST_*
  2. btc_web/btc_dashboard/scoring.py       cycle_recommendation / tactical_recommendation
  3. btc_web/btc_dashboard/score_history.py _BANDS
  4. backtest/evaluate.py                   CYCLE_BANDS / TACTICAL_BANDS
  5. btc_web/btc_dashboard/triggers.py      _BAND_EDGES (触发价位表越界标签, 内嵌档名+仓位区间)
  6. btc_web/btc_dashboard/data/band_stats.json (由 backtest/run_backtest.py 生成)
任何一处改动未同步, 本文件必须红。
"""
import json
import os
import re

import pytest

from btc_dashboard import decision, scoring, score_history, triggers, notify
from backtest import evaluate

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAND_STATS_PATH = os.path.join(
    REPO_ROOT, "btc_web", "btc_dashboard", "data", "band_stats.json")
RUN_BACKTEST_PATH = os.path.join(REPO_ROOT, "backtest", "run_backtest.py")

# 阈值扫描点: 全量程细扫 + 每个档位边界的精确值与 ±epsilon
def _sweep_scores(bands):
    pts = [x / 1000.0 for x in range(-990, 991, 7)]
    for lo, *_ in bands:
        if lo != float("-inf"):
            pts += [lo, lo - 1e-9, lo + 1e-9]
    return pts


@pytest.fixture(scope="module")
def band_stats():
    with open(BAND_STATS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ────────────────────────────────────────────────────────────
# decision.py ↔ backtest/evaluate.py
# ────────────────────────────────────────────────────────────

def test_cycle_bands_match_backtest():
    assert len(decision.CYCLE_BANDS) == len(evaluate.CYCLE_BANDS)
    for i, ((d_lo, d_name, d_plo, d_phi, d_mid, d_key),
            (e_lo, e_hi, e_label, e_pos)) in enumerate(
            zip(decision.CYCLE_BANDS, evaluate.CYCLE_BANDS)):
        assert d_lo == e_lo, f"档位{i}「{d_name}」下界: decision={d_lo} evaluate={e_lo}"
        assert d_key == e_label, f"档位{i} band_stats 键: decision={d_key!r} evaluate={e_label!r}"
        # 展示中值(整数%)与回测仓位(小数)允许 ≤0.5% 舍入差 (如 22 vs 0.225)
        assert abs(d_mid - e_pos * 100) <= 0.5, \
            f"档位{i}「{d_name}」目标中值: decision={d_mid}% evaluate={e_pos*100}%"
        assert d_name in d_key and f"{d_plo}-{d_phi}%" in d_key


def test_cycle_bands_contiguous_in_backtest():
    """evaluate 档位区间必须首尾相接无缝隙 (上界=上一档下界)。"""
    assert evaluate.CYCLE_BANDS[0][1] == float("inf")
    for i in range(1, len(evaluate.CYCLE_BANDS)):
        assert evaluate.CYCLE_BANDS[i][1] == evaluate.CYCLE_BANDS[i - 1][0], \
            f"档位{i}「{evaluate.CYCLE_BANDS[i][2]}」上界与上一档下界不接"


def test_tactical_bands_match_backtest():
    assert len(decision.TACTICAL_BANDS) == len(evaluate.TACTICAL_BANDS)
    for i, ((d_lo, d_name, _pace, _adv, d_key),
            (e_lo, e_hi, e_label)) in enumerate(
            zip(decision.TACTICAL_BANDS, evaluate.TACTICAL_BANDS)):
        assert d_lo == e_lo, f"战术档{i}「{d_name}」下界: decision={d_lo} evaluate={e_lo}"
        assert d_key == e_label
    for i in range(1, len(evaluate.TACTICAL_BANDS)):
        assert evaluate.TACTICAL_BANDS[i][1] == evaluate.TACTICAL_BANDS[i - 1][0]


# ────────────────────────────────────────────────────────────
# decision.py ↔ scoring.py 推荐文案
# ────────────────────────────────────────────────────────────

def test_cycle_recommendation_matches_bands():
    for s in _sweep_scores(decision.CYCLE_BANDS):
        _, name, plo, phi, *_ = decision.CYCLE_BANDS[decision._cycle_band_idx(s)]
        rec = scoring.cycle_recommendation(s)
        assert name in rec, f"score={s}: 档位「{name}」≠ 推荐「{rec}」"
        assert f"{plo}-{phi}%" in rec, f"score={s}: 仓位区间与推荐文案不符「{rec}」"


def test_tactical_recommendation_matches_bands():
    for s in _sweep_scores(decision.TACTICAL_BANDS):
        name = decision.TACTICAL_BANDS[decision._tactical_band_idx(s)][1]
        rec = scoring.tactical_recommendation(s)
        assert name in rec, f"score={s}: 战术档「{name}」≠ 推荐「{rec}」"


# ────────────────────────────────────────────────────────────
# decision.py ↔ score_history.py
# ────────────────────────────────────────────────────────────

def test_score_history_bands_match_decision():
    assert len(score_history._BANDS) == len(decision.CYCLE_BANDS)
    for (h_lo, h_label), (d_lo, d_name, *_) in zip(
            score_history._BANDS, decision.CYCLE_BANDS):
        assert h_lo == d_lo, f"「{d_name}」下界: score_history={h_lo} decision={d_lo}"
        assert h_label == d_name


# ────────────────────────────────────────────────────────────
# decision.py ↔ triggers.py 触发价位表档位边界
# ────────────────────────────────────────────────────────────

def test_trigger_band_edges_match_decision():
    """
    triggers._BAND_EDGES (触发价位表的档位越界标签) 内嵌了阈值、档名、仓位区间,
    但此前无任何一致性测试守护 —— 改 decision.CYCLE_BANDS 却漏改 triggers 时,
    触发价位表会静默按旧边界算档位。本测试补上这道网。

    每条边界 (阈值, "进/跌入X档 (a-b%仓)", 方向) 的不变式:
      - 档名 X 必须唯一对应 CYCLE_BANDS 里一个真实档
      - 内嵌仓位区间 a-b% 必须等于该档的 (仓位下限, 仓位上限)
      - 阈值: up 边界 = 该档自身下界; down 边界 = 上一档 (更高档) 的下界
        (向上穿过某档下界即"进"该档; 向下穿过上一档下界即"跌入"下一档)
    """
    pos_re = re.compile(r"\((\d+)-(\d+)%仓\)")
    for thr, label, direction in triggers._BAND_EDGES:
        assert direction in ("up", "down"), f"未知方向「{direction}」: {label!r}"

        # 档名: 取唯一一个作为 label 子串出现的 CYCLE_BANDS 档名
        # (前 3 条带"档"后缀、防守区无, 故用子串匹配而非精确切割)
        matched = [(i, b) for i, b in enumerate(decision.CYCLE_BANDS) if b[1] in label]
        assert len(matched) == 1, \
            f"触发边界 {label!r} 未能唯一对应 CYCLE_BANDS 一档 (匹配到 {[b[1] for _, b in matched]})"
        idx, (b_lo, name, plo, phi, *_) = matched[0]

        # 内嵌仓位区间
        m = pos_re.search(label)
        assert m, f"触发边界 {label!r} 未能解析仓位区间 (格式变了?)"
        assert (int(m.group(1)), int(m.group(2))) == (plo, phi), \
            f"「{name}」仓位区间: triggers={m.group(1)}-{m.group(2)}% decision={plo}-{phi}%"

        # 阈值对应 (up=该档下界; down=上一档下界)
        if direction == "up":
            assert thr == b_lo, \
                f"「{name}」up 边界阈值 triggers={thr} ≠ 该档下界 decision={b_lo}"
        else:
            assert idx >= 1, f"「{name}」是最高档, 不应有向下跌入边界"
            above_lo = decision.CYCLE_BANDS[idx - 1][0]
            assert thr == above_lo, \
                f"「{name}」down 边界阈值 triggers={thr} ≠ 上一档下界 decision={above_lo}"


# ────────────────────────────────────────────────────────────
# decision.py ↔ band_stats.json ↔ backtest/run_backtest.py
# ────────────────────────────────────────────────────────────

def test_hysteresis_params_match_band_stats(band_stats):
    hyst = band_stats.get("hysteresis", {})
    assert hyst.get("delta") == decision.HYST_DELTA, \
        "滞回 δ 不一致 — 改了 decision.HYST_DELTA 后须重跑 backtest/run_backtest.py"
    assert hyst.get("confirm") == decision.HYST_CONFIRM, \
        "滞回确认天数不一致 — 须重跑 backtest/run_backtest.py"


def test_hysteresis_params_match_backtest_source():
    """run_backtest.py 里滞回参数是字面量, 从源码解析对账。"""
    with open(RUN_BACKTEST_PATH, "r", encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"HYST_DELTA\s*,\s*HYST_CONFIRM\s*=\s*([0-9.]+)\s*,\s*([0-9]+)", src)
    assert m, "run_backtest.py 中未找到 HYST_DELTA, HYST_CONFIRM 定义 (解析规则或代码已变)"
    assert float(m.group(1)) == decision.HYST_DELTA
    assert int(m.group(2)) == decision.HYST_CONFIRM


def test_band_stats_covers_all_bands(band_stats):
    """band_stats.json 的档位键与 decision 定义严格互为全集 (无缺失、无改名残留)。"""
    cycle_keys = {b[5] for b in decision.CYCLE_BANDS}
    tact_keys = {b[4] for b in decision.TACTICAL_BANDS}
    assert set(band_stats["cycle"].keys()) == cycle_keys, \
        "cycle 档位键不一致 — 改档位阈值/命名后须重跑 backtest/run_backtest.py"
    assert set(band_stats["tactical"].keys()) == tact_keys, \
        "tactical 档位键不一致 — 须重跑 backtest/run_backtest.py"


def test_band_stats_entries_complete(band_stats):
    for key, entry in band_stats["cycle"].items():
        for w in ("30d", "90d", "180d", "365d"):
            assert w in entry, f"cycle「{key}」缺 {w} 窗口"
            for field in ("n", "mean", "median", "win"):
                assert field in entry[w], f"cycle「{key}」{w} 缺 {field}"
            assert entry[w]["n"] > 0, f"cycle「{key}」{w} 样本数为 0"
    for key, entry in band_stats["tactical"].items():
        for w in ("7d", "14d", "30d"):
            assert w in entry, f"tactical「{key}」缺 {w} 窗口"
            assert entry[w]["n"] > 0, f"tactical「{key}」{w} 样本数为 0"


def test_band_stats_bounds_match_decision(band_stats):
    """band_stats.json 必须自描述周期/战术评分下界, 且与 decision 现行阈值逐一相等。

    此前 band_stats 只被档位仓位标签键集合钉住 (test_band_stats_covers_all_bands),
    周期分评分下界与 band_stats 内容零耦合 —— 一致改掉五份源文件的周期下界却保留
    仓位标签、漏重跑 backtest/run_backtest.py 时, 五个跨源测试与四个 band_stats 测试
    全绿, 而 band_stats.json 的 n/mean/median/win 仍按旧边界分桶 (决策面板前瞻收益
    静默过期)。复刻已生效的 hysteresis 数据侧守卫, 补上周期分一侧缺口。

    评分下界由 backtest.evaluate.band_score_bounds() 写入 band_stats.json 元数据
    (run_backtest.py 重跑落盘)。
    """
    cyc = [lo for lo, *_ in decision.CYCLE_BANDS if lo != float("-inf")]
    tac = [lo for lo, *_ in decision.TACTICAL_BANDS if lo != float("-inf")]
    assert band_stats.get("cycle_bounds") == cyc, \
        "band_stats.json cycle_bounds ≠ decision.CYCLE_BANDS 下界 — 改周期档位阈值后须重跑 backtest/run_backtest.py"
    assert band_stats.get("tactical_bounds") == tac, \
        "band_stats.json tactical_bounds ≠ decision.TACTICAL_BANDS 下界 — 改战术档位阈值后须重跑 backtest/run_backtest.py"


# ────────────────────────────────────────────────────────────
# scoring.py 因子桶配置自洽
# ────────────────────────────────────────────────────────────

def test_bucket_weights_sum_to_one():
    for cfg_name, cfg in (("CYCLE_BUCKETS", scoring.CYCLE_BUCKETS),
                          ("TACTICAL_BUCKETS", scoring.TACTICAL_BUCKETS)):
        total = sum(b["weight"] for b in cfg.values())
        assert abs(total - 1.0) < 1e-9, f"{cfg_name} 桶权重合计 {total} ≠ 1.0"


def test_member_weights_reference_real_members():
    all_buckets = {**scoring.CYCLE_BUCKETS, **scoring.TACTICAL_BUCKETS}
    for bucket_name, weights in scoring.MEMBER_WEIGHTS.items():
        assert bucket_name in all_buckets, f"MEMBER_WEIGHTS 引用不存在的桶「{bucket_name}」"
        members = set(all_buckets[bucket_name]["members"])
        for m, w in weights.items():
            assert m in members, f"「{bucket_name}」的成员权重引用不存在的因子「{m}」"
            assert w > 0


# ────────────────────────────────────────────────────────────
# 置信闸门阈值 ≡ 警示触发阈值 (同源锁) + 冻结态门控 notify 抑制
# decision 的 confidence 分级 / frozen 状态与 warnings 必须由同一批阈值/判据驱动 —
# 任一处漂移 (如警示改 0.5、置信仍读旧值) 会让"面板显示警示却仍标可靠"自相矛盾。
# ────────────────────────────────────────────────────────────

def _hist(n):
    return [{"date": f"d{i}", "total_score": 0.20} for i in range(n)]


def _base_dash(**kw):
    d = {"total_score": 0.20, "tactical_score": 0.0, "btc_price": 65000.0,
         "data_synthetic": False, "cycle_coverage": 0.95, "tactical_coverage": 0.95}
    d.update(kw)
    return d


def test_confidence_coverage_gate_shares_warning_threshold():
    """覆盖率警示与 confidence 分级共用 decision.COVERAGE_WARN_THRESHOLD (严格 <)。"""
    thr = decision.COVERAGE_WARN_THRESHOLD
    at = decision.compute_decision(_base_dash(cycle_coverage=thr), _hist(60))
    assert not any("覆盖率" in w for w in at["warnings"]), "恰在阈值不应触发警示 (须严格 <)"
    assert at["confidence"]["level"] == "可靠", "恰在阈值置信仍须可靠 (与警示同源)"

    below = decision.compute_decision(_base_dash(cycle_coverage=thr - 1e-6), _hist(60))
    assert any("覆盖率" in w for w in below["warnings"]), "略低于阈值须触发覆盖率警示"
    assert below["confidence"]["level"] != "可靠", "警示触发时置信不得仍标可靠 (阈值漂移)"


def test_confidence_history_gate_shares_warning_threshold():
    """历史深度警示与 confidence 共用 decision.MIN_RELIABLE_HISTORY (=HYST_CONFIRM×4)。"""
    mn = decision.MIN_RELIABLE_HISTORY
    assert mn == decision.HYST_CONFIRM * 4

    at = decision.compute_decision(_base_dash(), _hist(mn))
    assert not any("评分历史" in w for w in at["warnings"])
    assert at["confidence"]["level"] == "可靠"

    below = decision.compute_decision(_base_dash(), _hist(mn - 1))
    assert any("评分历史" in w for w in below["warnings"])
    assert below["confidence"]["level"] != "可靠"


def _tail_hist(score):
    """长期 -0.05 (锁生效档于减配) + 尾部 3 条 score (贴上移触发线不换档)。"""
    scores = [-0.05] * 30 + [score] * 3
    return [{"date": f"d{i}", "total_score": s} for i, s in enumerate(scores)]


def test_confidence_boundary_gate_shares_rebuild_noise_threshold():
    """换档临界带警示与 confidence 分级共用 decision.REBUILD_NOISE (同源锁, 只加不改)。

    构造生效档锁死在减配 (长期 -0.05)、尾部分数贴其上移换档触发线 (0.00+δ=0.05, 即
    07-15 案例) 的历史; 分数距触发线在 REBUILD_NOISE 内外各取一点, 证"临界带警示触发
    ⇔ 置信降级"由同一常量驱动 —— 任一处改读别的阈值 (警示改、置信仍读旧值) 本锁必红。
    """
    noise = decision.REBUILD_NOISE
    trigger = 0.00 + decision.HYST_DELTA  # 减配上移换档触发线 = 0.05

    outside = trigger - noise * 1.5  # 距触发线 1.5×noise > noise → 不触发
    out_dec = decision.compute_decision(
        _base_dash(total_score=outside), _tail_hist(outside))
    assert out_dec["cycle"]["band"] == "减配", "构造前提: 生效档须锁在减配"
    assert not any("换档临界带" in w for w in out_dec["warnings"]), \
        "距触发线 > REBUILD_NOISE 不应触发临界带"
    assert out_dec["confidence"]["level"] == "可靠", "未触发临界带时置信须可靠 (同源)"

    inside = trigger - noise * 0.5  # 距触发线 0.5×noise < noise → 触发
    in_dec = decision.compute_decision(
        _base_dash(total_score=inside), _tail_hist(inside))
    assert in_dec["cycle"]["band"] == "减配"
    assert any("换档临界带" in w for w in in_dec["warnings"]), \
        "距触发线 < REBUILD_NOISE 须触发临界带警示"
    assert in_dec["confidence"]["level"] != "可靠", \
        "临界带触发时置信不得仍可靠 (阈值漂移)"
    assert any("换档临界带" in r for r in in_dec["confidence"]["reasons"]), \
        "临界带原因须进 confidence.reasons (与警示同源)"


def test_frozen_gate_matches_price_layer_and_suppresses_notify():
    """frozen 判据只锁价格层 (data_synthetic / price_stale), 且 notify 据 frozen 抑制误推送。"""
    for kw, reason_kw in ((dict(data_synthetic=True), "演示"),
                          (dict(price_stale=True), "陈旧")):
        dash = _base_dash(**kw)
        dec = decision.compute_decision(dash, _hist(60))
        assert dec["frozen"] is True, f"{kw} 应冻结"
        assert any(reason_kw in r for r in dec["freeze_reasons"])

        # 构造本应触发周期换档的 prev_state; frozen 态下 notify 必须一条不发
        dash["decision"] = dec
        prev = {"cycle_band": "偏多配置", "tactical_band": "等待信号", "last_sent": {}}
        alerts, _ = notify.evaluate_alerts(dash, prev)
        assert alerts == [], f"{kw} 冻结态下 notify 未被抑制"


def test_low_coverage_not_frozen_soft_only():
    """因子覆盖率低维持软告警, 不升冻结 (避免冷启动/限流下 onchain 暂灰扰民误冻)。"""
    dec = decision.compute_decision(
        _base_dash(cycle_coverage=0.3, tactical_coverage=0.3), _hist(60))
    assert dec["frozen"] is False and dec["freeze_reasons"] == []


# ────────────────────────────────────────────────────────────
# 减半日期唯一事实源守卫 (2026-07 审查: 曾发现 8 份拷贝分裂成 04-19/04-20 两派)
# ────────────────────────────────────────────────────────────

def test_halving_dates_single_source():
    """减半日期字面量只允许出现在 core.py — 其余 .py 一律 import。

    守卫两种写法: datetime(2024, 4, 20) 与 "2024-04-20" 字符串。
    JSON 资产不在守卫范围 (路线图里程碑正文里的历史日期是合法内容)。
    """
    # 用拼接构造字面量, 防止本文件自触发
    halving_iso = ["2012" + "-11-28", "2016" + "-07-09", "2020" + "-05-11",
                   "2024" + "-04-19", "2024" + "-04-20",
                   "2028" + "-04-15", "2028" + "-04-20"]
    halving_ctor = [r"datetime\(\s*2012\s*,\s*11\s*,\s*28",
                    r"datetime\(\s*2016\s*,\s*7\s*,\s*9",
                    r"datetime\(\s*2020\s*,\s*5\s*,\s*11",
                    r"datetime\(\s*2024\s*,\s*4\s*,\s*(19|20)",
                    r"datetime\(\s*2028\s*,\s*4\s*,"]
    allowed = os.path.join("btc_web", "btc_dashboard", "core.py")

    scan_dirs = [os.path.join(REPO_ROOT, "btc_web"),
                 os.path.join(REPO_ROOT, "backtest")]
    offenders = []
    for root_dir in scan_dirs:
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", "output", "cache", "data", ".venv")]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, REPO_ROOT)
                if rel == allowed:
                    continue
                with open(path, "r", encoding="utf-8") as f:
                    src = f.read()
                for lit in halving_iso:
                    if lit in src:
                        offenders.append(f"{rel}: 字符串 {lit}")
                for pat in halving_ctor:
                    if re.search(pat, src):
                        offenders.append(f"{rel}: 构造式 {pat}")
    assert not offenders, (
        "减半日期字面量出现在 core.py 之外 (应 from btc_dashboard.core import "
        "HALVING_DATES/NEXT_HALVING_ESTIMATE):\n  " + "\n  ".join(offenders))


# ------------------------------------------------------------
# 减半时钟档位唯一事实源 (2026-07: >30月 -1→+0.5 反信号修正)
# ------------------------------------------------------------

def test_halving_band_single_source_and_boundaries():
    """档位边界锁死 + 三处实现 (indicators_long/backfill/backtest) 必须引用
    core.halving_band 同一函数, 防止再分裂成各自的 if 链。"""
    from btc_dashboard.core import halving_band
    from btc_dashboard import backfill, indicators_long
    from backtest import factors

    # 2026-07: 12-24月由 0 改 -1 (顶部与崩塌段, C1-C4 复刻, 留一对照 IC/Sharpe/回撤全面占优)
    cases = {11.9: 1, 12.0: 1, 12.1: -1, 24.0: -1, 24.1: -1,
             29.9: -1, 30.0: -1, 30.1: 0.5, 47.5: 0.5}
    for m, expect in cases.items():
        assert halving_band(m) == expect, f"months={m}"
        assert backfill._band_halving(m) == expect, f"backfill months={m}"

    # 单一事实源: 两个消费模块引用的必须是同一个函数对象
    assert factors.halving_band is halving_band
    assert indicators_long.halving_band is halving_band
