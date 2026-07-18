#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
稳定币增速因子的坏数据护栏 (indicators_v2.calc_stablecoin_growth)。

回归意图: 上游 DefiLlama 半写入/结构变化会把 peggedUSD 缺失字段静默填 0,
旧代码只守 `prev_30d > 0`、latest 无校验, 于是 latest=0 → -100% 增速 → 打
-1『弹药快速撤离』强看空信号并带权计入周期分资金流桶 (而非像坏数据应做的转灰
退出评分); 对称地 prev_30d=0 时 `else 0.0` 伪装成『常态区间』。修复后: 最新或
30 日前的锚点任一非正 → 因子如实缺席 (score=0 / ⚪ / value=NaN), 不携带伪值。

只 mock HTTP 源 (requests.get) — 本因子不碰存储层, 计算全在内存, 故无需存储往返。
"""
import math

import pytest

from btc_dashboard import indicators_v2

# 递增日度 unix 秒时间戳的起点 (值本身不影响评分, 只用于排序)
_BASE_TS = 1_600_000_000


class _FakeResp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def _row(i, tc):
    """第 i 天的一行, tc 为 totalCirculating 字段 (dict / {} / None)。"""
    return {"date": _BASE_TS + i * 86400, "totalCirculating": tc}


def _ok(v):
    return {"peggedUSD": v}


def _healthy_tc(n=60, early=100e9, late=108e9):
    """前 (n-30) 天 early, 后 30 天 late → 30 日增速 = late/early - 1。"""
    return [_ok(early if i < n - 30 else late) for i in range(n)]


@pytest.fixture
def patch_llama(monkeypatch):
    def _install(tc_list, status=200):
        payload = [_row(i, tc) for i, tc in enumerate(tc_list)]
        monkeypatch.setattr(indicators_v2.requests, "get",
                            lambda *a, **k: _FakeResp(payload, status))
    return _install


def _is_absent(res):
    """坏数据应表现为『转灰缺席』: 0 分 + ⚪ + NaN 值 (不计入评分)。"""
    return (res.name == "稳定币增速" and res.score == 0 and res.color == "⚪"
            and isinstance(res.value, float) and math.isnan(res.value))


# ── 正向对照: 健康序列照常打分, 修复不误伤正常路径 ──────────────────

def test_healthy_series_scores_normally(patch_llama):
    # 前 30 天 100B, 后 30 天 108B → 30 日增速 +8.0% → 0.5 分 (>5.5, ≤12)
    patch_llama(_healthy_tc(early=100e9, late=108e9))
    res = indicators_v2.calc_stablecoin_growth()
    assert not _is_absent(res)
    assert res.value == pytest.approx(8.0, abs=0.01)
    assert res.score == 0.5 and res.color == "🟢"


# ── 缺字段 / 零值 / 负值 载荷: 因子缺席, 而非伪造 -1 ──────────────────

def test_latest_missing_peggedUSD_field_is_absent_not_bearish(patch_llama):
    # 最新一行 totalCirculating 为非空 dict 但缺 peggedUSD 键 (键改名/结构变化),
    # 通过既有的 `if p.get("totalCirculating")` 真值门, 旧代码 .get(...,0) 静默填 0
    tc = _healthy_tc()
    tc[-1] = {"peggedEUR": 100e9}  # 结构变化: 半写入, USD 键缺失
    patch_llama(tc)
    res = indicators_v2.calc_stablecoin_growth()
    assert _is_absent(res)
    assert res.score != -1  # 旧行为会是 -100% → -1『弹药快速撤离』


def test_latest_zero_value_is_absent_not_bearish(patch_llama):
    tc = _healthy_tc()
    tc[-1] = _ok(0)
    patch_llama(tc)
    res = indicators_v2.calc_stablecoin_growth()
    assert _is_absent(res)
    assert res.score != -1


def test_latest_negative_value_is_absent_not_bearish(patch_llama):
    tc = _healthy_tc()
    tc[-1] = _ok(-5e9)  # 不可能为负, 只可能是坏数据
    patch_llama(tc)
    res = indicators_v2.calc_stablecoin_growth()
    assert _is_absent(res)
    assert res.score != -1


# ── 对称场景: 30 日前锚点坏 → 缺席, 而非伪装『常态区间』0 分 ──────────

def test_prev30d_zero_anchor_is_absent_not_normal(patch_llama):
    tc = _healthy_tc()  # 60 行, series[-31] 落在 index 29
    tc[29] = _ok(0)     # 30 日前锚点坏; 旧代码 else 0.0 会伪装成『常态区间』
    patch_llama(tc)
    res = indicators_v2.calc_stablecoin_growth()
    assert _is_absent(res)
    # 旧行为: growth 静默归 0.0 → score 0 但 color 🟡『常态区间』; 现应为 ⚪ 缺席
    assert res.color != "🟡"


def test_all_rows_zero_is_absent(patch_llama):
    # 整源坏 (全 0): 行数足够但锚点非正 → 转灰
    patch_llama([_ok(0)] * 60)
    res = indicators_v2.calc_stablecoin_growth()
    assert _is_absent(res)


# ── 既有转灰路径不受影响 (回归护栏) ──────────────────────────────

def test_source_unavailable_is_absent(patch_llama):
    patch_llama(_healthy_tc(), status=500)  # 非 200 → series 保持 None
    res = indicators_v2.calc_stablecoin_growth()
    assert _is_absent(res)


def test_too_few_rows_is_absent(patch_llama):
    patch_llama(_healthy_tc(n=34))  # < 35 行 → 转灰 (无法取 30 日前锚点)
    res = indicators_v2.calc_stablecoin_growth()
    assert _is_absent(res)
