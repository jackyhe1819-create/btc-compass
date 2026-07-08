#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.engine
===============
逐日把因子分喂给 **现网真实的** scoring._compute_bucket_scores 聚合,
确保桶内权重 / 缺失重归一逻辑与生产代码同源 (不是重新实现一遍)。
"""

from collections import namedtuple

import numpy as np
import pandas as pd

from factors import _BTC_WEB  # noqa: F401  (确保 sys.path 已注入)
from btc_dashboard.scoring import (CYCLE_BUCKETS, TACTICAL_BUCKETS,
                                   _compute_bucket_scores)

# _compute_bucket_scores 只访问 .value 与 .score
MockInd = namedtuple("MockInd", ["value", "score"])


def _daily_scores(buckets_cfg: dict, factor_df: pd.DataFrame) -> pd.DataFrame:
    """对 factor_df 的每一行调用现网聚合函数, 返回 总分 + 各桶分。"""
    members = [m for cfg in buckets_cfg.values() for m in cfg["members"]]
    cols = [m for m in members if m in factor_df.columns]
    sub = factor_df[cols]

    bucket_names = list(buckets_cfg.keys())
    rows = {"score": []}
    for b in bucket_names:
        rows[b] = []

    values = sub.values
    col_idx = {c: i for i, c in enumerate(cols)}
    index = sub.index

    for r in range(len(sub)):
        inds = {}
        for name in cols:
            v = values[r, col_idx[name]]
            if not np.isnan(v):
                inds[name] = MockInd(value=1.0, score=float(v))
        total, detail, _cov = _compute_bucket_scores(buckets_cfg, inds)
        # 全因子缺失时现网返回 0.0, 回测里记为 NaN 更诚实
        any_member = bool(inds)
        rows["score"].append(total if any_member else np.nan)
        for b in bucket_names:
            d = detail[b]["score"]
            rows[b].append(np.nan if d is None else d)

    out = pd.DataFrame(rows, index=index)
    return out


def compute_history(cycle_factors: pd.DataFrame,
                    tactical_factors: pd.DataFrame) -> dict:
    cycle = _daily_scores(CYCLE_BUCKETS, cycle_factors)
    tactical = _daily_scores(TACTICAL_BUCKETS, tactical_factors)
    return {"cycle": cycle, "tactical": tactical}


def factor_coverage(factor_df: pd.DataFrame) -> pd.DataFrame:
    """每个因子的有效起止日期与覆盖天数 (报告用)。"""
    rows = []
    for c in factor_df.columns:
        s = factor_df[c].dropna()
        rows.append({
            "factor": c,
            "first": s.index[0].date().isoformat() if len(s) else "—",
            "last": s.index[-1].date().isoformat() if len(s) else "—",
            "days": len(s),
        })
    return pd.DataFrame(rows)
