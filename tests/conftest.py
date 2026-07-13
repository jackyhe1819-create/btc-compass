#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pytest 公共配置: 把 btc_web/ 与仓库根加入 sys.path,
使测试可以 `from btc_dashboard import ...` / `from backtest import ...`。
"""
import os
import sys

# 测试进程不起 app 的启动预热/回填后台线程(真实网络), 见 app.py 的 BTC_DISABLE_WARMUP 守卫
os.environ.setdefault("BTC_DISABLE_WARMUP", "1")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_REPO_ROOT, os.path.join(_REPO_ROOT, "btc_web")):
    if p not in sys.path:
        sys.path.insert(0, p)
