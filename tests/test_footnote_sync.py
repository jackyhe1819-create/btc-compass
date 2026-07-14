#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""期权卡诚实脚注 ↔ options_study.json 同源锁(复审 Y16)。
   脚注数字是全站唯一主动声明回测结论的文案 — 重跑回测后无声漂移会把
   诚实标注变成谎言, 此测试强制同步。"""
import json
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STUDY = os.path.join(_ROOT, "backtest", "output", "options_study.json")
_JS = os.path.join(_ROOT, "btc_web", "static", "script.js")


def test_options_footnote_ic_matches_study():
    with open(_STUDY, encoding="utf-8") as f:
        study = json.load(f)
    with open(_JS, encoding="utf-8") as f:
        js = f.read()

    foot = next((l for l in js.splitlines() if "DVOL 分位回测" in l), None)
    assert foot, "期权卡脚注(含 'DVOL 分位回测')在 script.js 中未找到"

    # (1) 数字: 脚注 |IC|≤X 必须等于落盘三窗口 |IC| 的最大值
    ics = [abs(study[k]) for k in ("ic_fwd30", "ic_fwd60", "ic_fwd90")
           if study.get(k) is not None]
    max_abs = max(ics)
    m = re.search(r"\|IC\|≤([0-9.]+)", foot)
    assert m, f"脚注缺 |IC|≤X 数字: {foot!r}"
    assert float(m.group(1)) == round(max_abs, 3), (
        f"脚注 IC 数字 {m.group(1)} ≠ options_study.json 落盘 max|IC|={max_abs} — 重跑回测后须同步脚注")

    # (2) 方向声明: '方向不稳' 仅当三窗口符号确实不一致时才诚实
    signs = {study[k] > 0 for k in ("ic_fwd30", "ic_fwd60", "ic_fwd90")
             if study.get(k) is not None}
    if "方向不稳" in foot:
        assert len(signs) > 1, "脚注称'方向不稳'但落盘 IC 三窗口同号 — 文案已过时"

    # (3) 计分声明: '未计入战术分' 仅当 verdict 确为 display_only
    if "未计入战术分" in foot:
        assert study.get("verdict") == "display_only", (
            "脚注称'未计入战术分'但落盘 verdict 已非 display_only — 文案已过时")
