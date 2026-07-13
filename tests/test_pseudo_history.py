#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""伪历史止血契约: 「今日定值 × 历史价」构造的假历史必须返回空,
   直到攒出真实逐日快照序列(展示层复审 R4, 2026-07-13)。
   这两个函数必须无条件空返回且零网络调用。"""
from btc_dashboard.history import get_max_pain_history, get_company_holdings_history


def test_max_pain_history_disabled_returns_empty():
    h = get_max_pain_history(None, 30)
    assert h["dates"] == [] and h["values"] == []
    assert h["indicator"] == "最大痛点"


def test_company_holdings_history_disabled_returns_empty():
    h = get_company_holdings_history(None, 30)
    assert h["dates"] == [] and h["values"] == []
    assert h["indicator"] == "公司持仓"
