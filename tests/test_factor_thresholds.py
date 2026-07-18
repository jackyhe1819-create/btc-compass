# -*- coding: utf-8 -*-
"""因子离散阈值的跨文件同源守护:backfill._band_*(实时回填) 与
backtest.factors._step_score(回测) 必须逐点评分一致,否则回测 IC 与实时评分口径分裂。
纯离线。注意:calc(indicators_v2)侧的阈值埋在 if/elif+网络函数里,本测试守不到
——那条在不变量清单里显式记为 accepted_gap(抽纯 helper 属改评分结构,越本循环边界)。

边界开闭差异:backfill 用严格 < / <= 链(值==阈值归低档),backtest._step_score 用
searchsorted(side='right')(值==阈值归高档)。二者在边缘点故意不对齐(factors.py 注:
连续值命中边缘概率为零、差异测度为零),故探针值一律避开精确边界(用 ±1e-6)。

Tier-2 交叉校验(2026-07):滚动分位归一化 / 极值保底合成 / σ 窗常量已收敛到
btc_dashboard.factor_kernels 单一事实源,backfill(回填) 与 backtest.factors(回测)
同 import。下方 test_tier2_* / test_mvrv_z_* 用身份断言 + 合成序列锁死这一层不再
分裂成各自拷贝(MVRV-Z 绝对腿 expanding(365)σ vs rolling(730)σ 的历史漂移即此类)。"""
import numpy as np
import pandas as pd

from btc_dashboard import backfill, factor_kernels
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
    "交易所净流(7d)": (backfill._band_netflow7,     [-0.8, -0.4, 0.45, 1.0],  [1, 0.5, 0, -0.5, -1]),
    # 交易所余额 2026-07 退出周期评分 (scoring.CYCLE_BUCKETS 注), 三处分档代码已删
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


# ============================================================
# Tier-2 共享内核: 单一事实源 + 合成序列金标准交叉校验
# ============================================================

def test_tier2_kernels_single_source():
    """滚动分位 / 极值保底 kernel 收敛到 factor_kernels 单一事实源:backfill(回填) 与
    backtest.factors(回测) 引用的必须是同一函数对象,杜绝再分裂成各自拷贝而静默漂移
    (沿用 test_consistency 的 halving_band 身份判例)。"""
    assert backfill.rolling_percentile_score is factor_kernels.rolling_percentile_score
    assert factors.rolling_percentile_score is factor_kernels.rolling_percentile_score
    assert backfill.extreme_combine is factor_kernels.extreme_combine
    assert factors._extreme_combine is factor_kernels.extreme_combine


def test_mvrv_sigma_window_constants():
    """σ/分位窗常量单一事实源:730(主) / 365(回退) / 1460(分位窗)。
    历史漂移点:backfill MVRV-Z 绝对腿曾误用 expanding(365)σ,现网/回测用 rolling(730)σ。"""
    assert factor_kernels.MVRV_SIGMA_WINDOW == 730
    assert factor_kernels.MVRV_SIGMA_MIN_HISTORY == 365
    assert factor_kernels.PERCENTILE_WINDOW == 1460


def test_rolling_percentile_score_golden():
    """滚动分位 kernel 逐点复刻 scoring._percentile_score 语义:
    pct=窗口内严格小于当前值的占比, score=(0.5-pct)*2, 且 min_periods=window//4。"""
    idx = pd.date_range("2016-01-01", periods=1461, freq="D")
    s = pd.Series(np.arange(1461, dtype=float), index=idx)  # 严格递增: 每点=其窗口内最大值
    out = factor_kernels.rolling_percentile_score(s)
    # 前 364 点(<365 个观测, min_periods 未满) 为 NaN, 第 365 点起出分
    assert out.iloc[:364].isna().all()
    assert not np.isnan(out.iloc[364])
    # 第 365 点: 窗口 365 个值, 严格小于自身的 364 个 → pct=364/365
    assert abs(out.iloc[364] - (0.5 - 364 / 365) * 2) < 1e-12
    # 满窗口末点: 窗口 1460 个值, 严格小于自身的 1459 个 → pct=1459/1460
    assert abs(out.iloc[-1] - (0.5 - 1459 / 1460) * 2) < 1e-12


def test_extreme_combine_golden():
    """分位为主 + 绝对阈值极值保底:逐点取 |·| 更大者;一侧 NaN 用另一侧。"""
    pct = pd.Series([0.2, 0.6, np.nan, -0.3, 0.0])
    abs_ = pd.Series([0.5, 0.1, 0.8, np.nan, np.nan])
    out = factor_kernels.extreme_combine(pct, abs_).tolist()
    #  0: |0.5|>|0.2|→0.5   1: |0.1|<|0.6|→0.6   2: pct NaN→abs 0.8
    #  3: abs NaN→pct -0.3   4: abs NaN→pct 0.0
    assert out == [0.5, 0.6, 0.8, -0.3, 0.0]


def test_mvrv_z_sigma_leg_crossval():
    """金标准交叉校验:factors.cycle_factor_scores 的 MVRV-Z 输出必须逐点等于用共享
    kernel + 共享 σ 常量独立重建的参考序列 —— 锁死"绝对腿套 rolling(730)σ(不足回退
    expanding(365)σ)"这一 backfill/backtest 同源口径。若 factors 或常量任一侧退回
    纯 expanding(365)σ(历史漂移形态),本测试即红。"""
    n = 1900
    idx = pd.date_range("2015-01-01", periods=n, freq="D")
    rng = np.random.default_rng(7)
    # 恒正价格随机游走 + 温和周期性 mvrv(避开 0, 免 rcap 爆炸)
    price = pd.Series(np.exp(np.cumsum(rng.normal(0.0004, 0.02, n))) * 15000, index=idx)
    mcap = price * 19_000_000.0
    mvrv = pd.Series(1.6 + 0.6 * np.sin(np.arange(n) / 60.0) + rng.normal(0, 0.03, n), index=idx)
    cm = pd.DataFrame({
        "price": price, "mcap": mcap, "mvrv": mvrv,
        "iss_usd": pd.Series(rng.uniform(1e7, 3e7, n), index=idx),
        "hashrate": pd.Series(np.linspace(1e20, 6e20, n), index=idx),
    })
    empty = pd.Series(dtype=float)
    got = factors.cycle_factor_scores(cm, empty, empty, empty)["MVRV-Z"]

    # 独立参考: 只用 factor_kernels 常量/内核 + backtest 侧阶梯映射重建
    rcap = mcap / mvrv
    z730 = (mcap - rcap) / mcap.rolling(factor_kernels.MVRV_SIGMA_WINDOW).std()
    zexp = (mcap - rcap) / mcap.expanding(min_periods=factor_kernels.MVRV_SIGMA_MIN_HISTORY).std()
    zabs = z730.where(z730.notna(), zexp)
    ref = factor_kernels.extreme_combine(
        factor_kernels.rolling_percentile_score(z730),
        factors._step_score(zabs, [0, 1, 3, 5], [1, 0.5, 0, -0.5, -1]))
    # 两腿(分位/绝对)都要在这段样本里真正出分, 否则测不到 σ 口径
    assert got.notna().sum() > 0
    assert factor_kernels.rolling_percentile_score(z730).notna().sum() > 0
    pd.testing.assert_series_equal(got, ref, check_names=False)
