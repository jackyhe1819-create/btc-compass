#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pytest 公共配置: 把 btc_web/ 与仓库根加入 sys.path,
使测试可以 `from btc_dashboard import ...` / `from backtest import ...`。
"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_REPO_ROOT, os.path.join(_REPO_ROOT, "btc_web")):
    if p not in sys.path:
        sys.path.insert(0, p)
