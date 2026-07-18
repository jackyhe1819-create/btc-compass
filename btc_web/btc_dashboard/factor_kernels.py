# -*- coding: utf-8 -*-
"""
btc_dashboard.factor_kernels
============================
Tier-2 因子评分共享内核 —— 单一事实源。

历史上 backtest/factors.py(回测逐日 point-in-time 序列) 与 backfill.py(实时评分
历史回填) 各持一份「滚动分位归一化 + 极值保底合成 + σ/分位窗常量」拷贝, 彼此零
交叉校验。MVRV-Z 的 σ(市值) 腿就曾在这里静默漂移(backfill 绝对腿误用 expanding(365)σ,
现网/回测用 rolling(730)σ), 让 band_stats.json 与实盘 score_history 描述两套不同的
评分系统。本模块把这些 Tier-2 helper 收敛为唯一定义, 由两处 import 复用, 并配
tests/test_factor_thresholds.py 的合成序列交叉校验, 锁死未来漂移。

被消费:
- backtest/factors.py                (回测)
- btc_web/btc_dashboard/backfill.py  (回填)
- scoring.py 本轮未收敛(归属冲突挂账): 其 _percentile_score 是返回 (score, n_used)
  的标量版, 形状不同, 后续单独处理。

窗口常量单一事实源:
- PERCENTILE_WINDOW      = 1460  4 年滚动分位窗 (从 scoring 引入, 不另立第二定义)
- MVRV_SIGMA_WINDOW      = 730   MVRV-Z σ(市值) 主用滚动窗 (对齐现网 CM-primary)
- MVRV_SIGMA_MIN_HISTORY = 365   730σ 数据不足时全历史扩张 std 的最短史 (备源)
"""

import numpy as np
import pandas as pd

from .scoring import PERCENTILE_WINDOW  # 单一事实源: 4 年分位窗 (1460)

# MVRV-Z 市值 σ 窗口: 现网 CM-primary 口径为 rolling(730)σ, 数据不足(<730天)时
# 逐点回退全历史扩张 std(min_periods=365, ≈bd/Glassnode 口径)。分位腿与绝对腿
# 必须同源套在同一 σ 派生序列上, 否则极值日翻分。
MVRV_SIGMA_WINDOW = 730
MVRV_SIGMA_MIN_HISTORY = 365


def rolling_percentile_score(series: pd.Series,
                             window: int = PERCENTILE_WINDOW) -> pd.Series:
    """
    向量化滚动分位评分, 逐点复刻 scoring._percentile_score:
      pct = 窗口内严格小于当前值的占比; score = (0.5 - pct) * 2
    要求 len(非NaN) >= window//4 (=365) 才出分, 再取 tail(window)。
    rank(method='min') - 1 恰为「严格小于」的个数, 与现网逐日一致。防前视。
    """
    s = series.dropna()
    if s.empty:
        return pd.Series(np.nan, index=series.index)
    minp = window // 4
    r = s.rolling(window, min_periods=minp).rank(method="min")
    n = s.rolling(window, min_periods=minp).count()
    pct = (r - 1) / n
    score = (0.5 - pct) * 2
    return score.reindex(series.index)


def extreme_combine(pct: pd.Series, abs_: pd.Series) -> pd.Series:
    """
    复刻 indicators_v2._pct_floor_score 的合成规则:
    分位数分为主, 绝对阈值分做极值保底 (逐日取 |·| 更大者)。
    一侧 NaN 时自动用另一侧。
    """
    out = pct.copy()
    both = pct.notna() & abs_.notna()
    use_abs = both & (abs_.abs() > pct.abs())
    out[use_abs] = abs_[use_abs]
    out = out.where(pct.notna(), abs_)
    return out
