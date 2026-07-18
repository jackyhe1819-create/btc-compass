# -*- coding: utf-8 -*-
"""触发价位表守卫回归: compute_trigger_levels 抛异常时 run_dashboard 必须降级为
trigger_levels=None 并照常返回完整结果, 而不是被 except 分支自身击穿整轮刷新。

历史缺陷: except 体误引用未定义的 logger, 触发路径一旦命中即抛 NameError 逃出
run_dashboard, 冻结缓存、评分不更新。本测试直接驱动真实 run_dashboard, 只桩掉
网络/评分层, 让 compute_trigger_levels 抛异常, 断言附属面板异常不影响主评分。"""
import numpy as np
import pandas as pd

import btc_dashboard.runner as runner
import btc_dashboard.triggers as triggers
from btc_dashboard.core import IndicatorResult


def _fake_df():
    idx = pd.date_range("2020-01-01", periods=800, freq="D")
    df = pd.DataFrame({"price": np.linspace(10000.0, 50000.0, 800)}, index=idx)
    df.attrs["source"] = "test-fixture"
    df.attrs["synthetic"] = False  # 非合成 → run_dashboard 会走触发价位表分支
    return df


def _fake_indicator(*_a, **_k):
    return IndicatorResult(
        name="stub", value=0.0, score=0, color="gray",
        status="stub", priority="P1", url="", description="", method="",
    )


_FAKE_SCORES = {
    "cycle_score": 0.1,
    "tactical_score": -0.2,
    "cycle_recommendation": "标准配置 40-60%",
    "tactical_recommendation": "观望",
    "cycle_buckets": {},
    "tactical_buckets": {},
    "cycle_coverage": 1.0,
    "tactical_coverage": 1.0,
}


def _stub_pipeline(monkeypatch):
    """把 run_dashboard 依赖的网络/指标/评分层全部桩掉, 使其离线且确定性。"""
    monkeypatch.setattr(runner, "fetch_btc_data", _fake_df)
    monkeypatch.setattr(runner, "fetch_realtime_btc_price", lambda: 50000.0)
    monkeypatch.setattr(runner, "compute_dual_scores", lambda *_a, **_k: dict(_FAKE_SCORES))
    # 所有 calc_* 指标函数(本地 + api_tasks)统一桩成快速灰因子, 避免真实网络 IO
    for attr in dir(runner):
        if attr.startswith("calc_"):
            monkeypatch.setattr(runner, attr, _fake_indicator)


def test_trigger_failure_does_not_break_dashboard(monkeypatch, capsys):
    """compute_trigger_levels 抛异常 → trigger_levels=None, 主评分照常返回。"""
    _stub_pipeline(monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("模拟触发价位表内部炸裂")

    # run_dashboard 内是 `from .triggers import compute_trigger_levels`, 运行时按属性解析,
    # 故打在 triggers 模块上即可生效。
    monkeypatch.setattr(triggers, "compute_trigger_levels", _boom)

    result = runner.run_dashboard()  # 缺陷未修时此处会以 NameError 抛出

    # 附属面板降级, 但主结果完整
    assert result.trigger_levels is None
    assert result.total_score == 0.1
    assert result.tactical_score == -0.2
    assert result.recommendation == "标准配置 40-60%"
    assert result.data_synthetic is False
    assert result.indicators, "主评分指标不应因触发价位表异常而丢失"

    # 走的是降级打印分支(print 风格), 而非 NameError 击穿
    assert "触发价位表计算失败" in capsys.readouterr().out


def test_trigger_success_populates_levels(monkeypatch):
    """对照组: compute_trigger_levels 正常返回时, 结果原样带上其产出。"""
    _stub_pipeline(monkeypatch)
    sentinel = {"levels": "ok"}
    monkeypatch.setattr(triggers, "compute_trigger_levels", lambda *_a, **_k: sentinel)

    result = runner.run_dashboard()
    assert result.trigger_levels == sentinel
