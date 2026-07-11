# -*- coding: utf-8 -*-
"""因子离散阈值的跨文件同源守护:backfill._band_*(实时回填) 与
backtest.factors._step_score(回测) 必须逐点评分一致,否则回测 IC 与实时评分口径分裂。
纯离线。注意:calc(indicators_v2)侧的阈值埋在 if/elif+网络函数里,本测试守不到
——那条在不变量清单里显式记为 accepted_gap(抽纯 helper 属改评分结构,越本循环边界)。

边界开闭差异:backfill 用严格 < / <= 链(值==阈值归低档),backtest._step_score 用
searchsorted(side='right')(值==阈值归高档)。二者在边缘点故意不对齐(factors.py 注:
连续值命中边缘概率为零、差异测度为零),故探针值一律避开精确边界(用 ±1e-6)。"""
import pandas as pd

from btc_dashboard import backfill
from backtest import factors

# (backfill 标量函数, edges 升序, scores 低→高)
CASES = {
    "MVRV-Z":        (backfill._band_mvrv_z,       [0, 1, 3, 5],             [1, 0.5, 0, -0.5, -1]),
    "STH成本线":     (backfill._band_sth_ratio,    [0.80, 0.95, 1.15, 1.35], [1, 0.5, 0, -0.5, -1]),
    "NUPL":          (backfill._band_nupl,         [0, 0.25, 0.5, 0.75],     [1, 0.5, 0, -0.5, -1]),
    "SOPR":          (backfill._band_sopr,         [0.97, 0.995, 1.02, 1.05], [1, 0.5, 0, -0.5, -1]),
    "Puell":         (backfill._band_puell,        [0.5, 0.8, 2.0, 4.0],     [1, 0.5, 0, -0.5, -1]),
    "ETF净流入":     (backfill._band_etf_5d,       [-1300, -700, 900, 1700], [-1, -0.5, 0, 0.5, 1]),
    "稳定币增速":    (backfill._band_stable_growth, [-2.0, -0.5, 5.5, 12.0],  [-1, -0.5, 0, 0.5, 1]),
    "交易所余额":    (backfill._band_exch_d30,     [-2.1, -0.85, 1.3, 2.9],  [1, 0.5, 0, -0.5, -1]),
    "交易所净流(7d)": (backfill._band_netflow7,     [-0.8, -0.4, 0.45, 1.0],  [1, 0.5, 0, -0.5, -1]),
}


def _probe_grid(edges):
    """跨越各档的探针值,避开精确边界(±1e-6),外加两端外点。"""
    pts = []
    for e in edges:
        pts += [e - 1e-6, e + 1e-6]
    pts += [edges[0] - 10, edges[-1] + 10]
    return sorted(set(pts))


def test_backfill_backtest_thresholds_agree():
    for name, (fn, edges, scores) in CASES.items():
        grid = _probe_grid(edges)
        got_bf = [fn(float(x)) for x in grid]
        got_bt = factors._step_score(pd.Series(grid), edges, scores).tolist()
        assert got_bf == got_bt, f"{name}: backfill {got_bf} != backtest {got_bt}"


def test_fear_greed_equivalent_mapping():
    """恐惧贪婪三处数值不同(整数偏移)但整数输入语义等价 —— 比映射结果,不比 raw edges。"""
    for i in range(0, 101):
        bf = backfill._band_fng(float(i))
        bt = factors._step_score(pd.Series([float(i)]), [20.5, 30.5, 69.5, 79.5],
                                 [1, 0.5, 0, -0.5, -1]).tolist()[0]
        assert bf == bt, f"F&G={i}: backfill {bf} != backtest {bt}"
