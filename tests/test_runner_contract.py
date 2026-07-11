# -*- coding: utf-8 -*-
"""桶成员名 ↔ runner 产出键 的跨文件字符串契约守护(纯离线,AST 静态解析,不执行 runner)。
成员名对不上 runner 产出键时,scoring 会静默剔除该因子、桶权重重归一、覆盖率悄悄下降——
此前只有运行时探针能抓(需起服务)。本测试把它提前到"提交即红"。"""
import ast
import os

from btc_dashboard import scoring

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(REPO, "btc_web", "btc_dashboard", "runner.py")


def _subscript_key(node):
    """兼容 py3.8(ast.Index)/3.9+(直接 Constant)取 d["x"] 的字符串键。"""
    sl = node.slice
    if isinstance(sl, ast.Index):        # py<3.9
        sl = sl.value
    if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
        return sl.value
    return None


def _runner_produced_keys():
    """runner 产出的因子键 = indicators[<str>]=... 直算段 ∪ api_tasks dict 字面量键。"""
    tree = ast.parse(open(RUNNER, encoding="utf-8").read())
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (isinstance(tgt, ast.Subscript) and isinstance(tgt.value, ast.Name)
                        and tgt.value.id == "indicators"):
                    k = _subscript_key(tgt)
                    if k is not None:
                        keys.add(k)
            if (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "api_tasks"
                    and isinstance(node.value, ast.Dict)):
                for kx in node.value.keys:
                    if isinstance(kx, ast.Constant) and isinstance(kx.value, str):
                        keys.add(kx.value)
    return keys


def _card_order_names():
    tree = ast.parse(open(RUNNER, encoding="utf-8").read())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "_CARD_ORDER"
                and isinstance(node.value, ast.List)):
            return {e.value for e in node.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    raise AssertionError("未在 runner.py 找到 _CARD_ORDER 列表字面量")


def _scoring_members():
    return {m for cfg in (scoring.CYCLE_BUCKETS, scoring.TACTICAL_BUCKETS)
            for b in cfg.values() for m in b["members"]}


def test_bucket_members_produced_by_runner():
    missing = _scoring_members() - _runner_produced_keys()
    assert not missing, f"桶成员未在 runner 产出(会被静默剔除): {sorted(missing)}"


def test_card_order_covers_scoring_members():
    missing = _scoring_members() - _card_order_names()
    assert not missing, f"计分成员缺席 _CARD_ORDER(卡片刷新乱跳): {sorted(missing)}"
