# -*- coding: utf-8 -*-
"""停机判据:信号层每条同源不变量都被显式登记,且要么有真实存在的守护测试,
要么被显式标为 accepted_gap 并写明理由 —— 没有"静默无守护"的不变量。

这条元测试是"传感器磨锐循环"的停机条件本身:它红,就说明还有不变量没登记/没守护;
它绿(连同全套冒烟),就是"达标"。未来加不变量必须往清单登记+命名守护,否则此测试红。"""
import importlib

from invariant_inventory import INVARIANT_INVENTORY

VALID_STATUS = {"guarded", "partial", "accepted_gap"}


def test_every_invariant_registered_and_guarded():
    assert INVARIANT_INVENTORY, "清单不能为空"
    for row in INVARIANT_INVENTORY:
        name = row.get("name", "?")
        assert row.get("status") in VALID_STATUS, f"{name}: status 非法 {row.get('status')!r}"
        assert row.get("places"), f"{name}: 必须列出 places"
        if row["status"] in ("guarded", "partial"):
            guard = row.get("guard")
            assert guard and "::" in guard, f"{name}: {row['status']} 却无合法 guard"
            mod_name, func = guard.split("::")
            mod = importlib.import_module(mod_name)  # tests/ 已在 sys.path (conftest)
            assert hasattr(mod, func), f"{name}: 守护测试不存在 {guard}"
        else:  # accepted_gap
            assert row.get("guard") is None, f"{name}: accepted_gap 的 guard 应为 None"
            assert row.get("note"), f"{name}: accepted_gap 必须写 note 理由"
