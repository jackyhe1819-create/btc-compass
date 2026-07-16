#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notify.py 冒烟测试: 提醒状态机纯逻辑 (evaluate_alerts) + 档名对账守卫。
不触网 — 发送层不在测试范围 (check_and_alert 未配置渠道时为空操作)。
"""
from datetime import datetime, timedelta

from btc_dashboard import notify
from btc_dashboard.decision import CYCLE_BANDS, TACTICAL_BANDS

NOW = datetime(2026, 7, 15, 12, 0, 0)


def _dash(cycle_band="标准配置", tactical_band="等待信号", synthetic=False,
          lo=40, hi=60, history_days=90, warnings=None):
    return {
        "total_score": 0.2, "tactical_score": 0.0, "btc_price": 65000.0,
        "data_synthetic": synthetic,
        "decision": {
            "cycle": {"band": cycle_band, "target_lo": lo, "target_hi": hi,
                      "target_mid": (lo + hi) // 2},
            "tactical": {"band": tactical_band, "pace": "正常定投",
                         "advice": "维持既定节奏"},
            "hysteresis": {"delta": 0.05, "confirm": 5,
                           "history_days": history_days},
            "warnings": warnings or [],
        },
    }


# ── 档名对账守卫: decision 改档名而 notify 未同步会静默失效, 这里锁死 ──

def test_tactical_alert_bands_exist_in_decision():
    names = {b[1] for b in TACTICAL_BANDS}
    for band in notify.TACTICAL_ALERT_BANDS:
        assert band in names, f"notify.TACTICAL_ALERT_BANDS 含未知档名: {band}"


def test_cycle_band_order_matches_decision():
    assert notify._CYCLE_BAND_ORDER == [b[1] for b in CYCLE_BANDS]


# ── 状态机语义 ──

def test_first_run_records_baseline_silently():
    alerts, state = notify.evaluate_alerts(_dash(), {}, NOW)
    assert alerts == []
    assert state["cycle_band"] == "标准配置"
    assert state["tactical_band"] == "等待信号"


def test_no_change_no_alert():
    prev = {"cycle_band": "标准配置", "tactical_band": "等待信号", "last_sent": {}}
    alerts, state = notify.evaluate_alerts(_dash(), prev, NOW)
    assert alerts == []
    assert state["cycle_band"] == "标准配置"


def test_cycle_switch_triggers_alert_with_direction():
    prev = {"cycle_band": "减配", "tactical_band": "等待信号", "last_sent": {}}
    alerts, _ = notify.evaluate_alerts(_dash(cycle_band="标准配置"), prev, NOW)
    assert len(alerts) == 1
    a = alerts[0]
    assert a["kind"] == "cycle_switch"
    assert a["state_patch"] == {"cycle_band": "标准配置"}
    assert "↑ 上调" in a["text"] and "40-60%" in a["text"]

    # 反向: 标准 → 减配 是下调
    prev2 = {"cycle_band": "标准配置", "tactical_band": "等待信号", "last_sent": {}}
    alerts2, _ = notify.evaluate_alerts(
        _dash(cycle_band="减配", lo=15, hi=30), prev2, NOW)
    assert "↓ 下调" in alerts2[0]["text"]


def test_cycle_switch_state_only_updates_via_patch():
    """换档提醒未发送成功前 (patch 未应用), 返回的新状态保持旧档 → 可重试。"""
    prev = {"cycle_band": "减配", "tactical_band": "等待信号", "last_sent": {}}
    alerts, state = notify.evaluate_alerts(_dash(cycle_band="标准配置"), prev, NOW)
    assert alerts and state.get("cycle_band") == "减配"  # 未应用 patch 前不前进


def test_tactical_extreme_entry_alerts_normal_shift_silent():
    prev = {"cycle_band": "标准配置", "tactical_band": "等待信号", "last_sent": {}}
    # 等待信号 → 谨慎: 非极值档, 静默更新状态
    alerts, state = notify.evaluate_alerts(_dash(tactical_band="谨慎"), prev, NOW)
    assert alerts == [] and state["tactical_band"] == "谨慎"
    # 谨慎 → 杠杆拥挤: 极值档, 提醒
    prev2 = {"cycle_band": "标准配置", "tactical_band": "谨慎", "last_sent": {}}
    alerts2, _ = notify.evaluate_alerts(_dash(tactical_band="杠杆拥挤"), prev2, NOW)
    assert len(alerts2) == 1 and alerts2[0]["kind"] == "tactical_extreme"


def test_cooldown_suppresses_and_preserves_state_for_retry():
    recent = (NOW - timedelta(hours=1)).isoformat(timespec="seconds")
    prev = {"cycle_band": "减配", "tactical_band": "等待信号",
            "last_sent": {"cycle_switch": recent}}
    alerts, state = notify.evaluate_alerts(_dash(cycle_band="标准配置"), prev, NOW)
    assert alerts == []                       # 冷却中不发
    assert state.get("cycle_band") == "减配"  # 状态不前进 → 冷却后补发
    # 冷却期满后同一变化重新触发
    old = (NOW - timedelta(hours=notify.COOLDOWN_HOURS + 1)).isoformat(timespec="seconds")
    prev["last_sent"]["cycle_switch"] = old
    alerts2, _ = notify.evaluate_alerts(_dash(cycle_band="标准配置"), prev, NOW)
    assert len(alerts2) == 1


def test_synthetic_data_fuse():
    prev = {"cycle_band": "减配", "tactical_band": "等待信号", "last_sent": {}}
    alerts, state = notify.evaluate_alerts(
        _dash(cycle_band="标准配置", synthetic=True), prev, NOW)
    assert alerts == []
    assert state.get("cycle_band") == "减配"  # 演示数据不推进状态


def test_missing_decision_noop():
    prev = {"cycle_band": "标准配置", "tactical_band": "等待信号", "last_sent": {}}
    alerts, _ = notify.evaluate_alerts(
        {"data_synthetic": False, "decision": None}, prev, NOW)
    assert alerts == []


def test_check_and_alert_noop_without_channels(tmp_path, monkeypatch):
    monkeypatch.delenv("WECOM_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("SERVERCHAN_SENDKEY", raising=False)
    out = notify.check_and_alert(_dash(), str(tmp_path))
    assert out == {"alerts": 0, "sent": 0, "channels": []}


def test_check_and_alert_end_to_end_with_mock_channel(tmp_path, monkeypatch):
    """配置渠道后: 首轮记基线 → 换档轮真触发 → 发送成功推进状态。"""
    sent_msgs = []
    monkeypatch.setenv("WECOM_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.delenv("SERVERCHAN_SENDKEY", raising=False)
    monkeypatch.setattr(notify, "_send_wecom", lambda text, url: (sent_msgs.append(text) or True))

    out1 = notify.check_and_alert(_dash(cycle_band="减配", lo=15, hi=30), str(tmp_path))
    assert out1["sent"] == 0 and sent_msgs == []          # 首轮基线

    out2 = notify.check_and_alert(_dash(cycle_band="标准配置"), str(tmp_path))
    assert out2 == {"alerts": 1, "sent": 1, "channels": ["wecom"]}
    assert len(sent_msgs) == 1 and "周期换档" in sent_msgs[0]

    out3 = notify.check_and_alert(_dash(cycle_band="标准配置"), str(tmp_path))
    assert out3["alerts"] == 0                            # 状态已推进, 不重复


def test_thin_history_gate_no_baseline_no_alert():
    """冷启动回填未完成 (history_days < HYST_CONFIRM×4): 不记基线不提醒 —
    否则回填落地后滞回档位刷新会推送伪换档 (对抗审查 major)。"""
    # 薄历史 + 空状态: 不记基线
    alerts, state = notify.evaluate_alerts(_dash(history_days=1), {}, NOW)
    assert alerts == [] and "cycle_band" not in state
    # 薄历史 + 已有基线 (本地长驻实例罕见角例): 不提醒也不推进状态
    prev = {"cycle_band": "减配", "tactical_band": "等待信号", "last_sent": {}}
    alerts2, state2 = notify.evaluate_alerts(
        _dash(cycle_band="标准配置", history_days=5), prev, NOW)
    assert alerts2 == [] and state2.get("cycle_band") == "减配"
    # 回填完成后的首个厚历史轮次: 走 first_run 静默记基线
    alerts3, state3 = notify.evaluate_alerts(_dash(history_days=90), {}, NOW)
    assert alerts3 == [] and state3["cycle_band"] == "标准配置"


def test_opposite_tactical_extremes_have_separate_cooldowns():
    """入场窗口→杠杆拥挤对穿: 语义相反的保护性信号不能被共享冷却吞掉。"""
    recent = (NOW - timedelta(hours=1)).isoformat(timespec="seconds")
    prev = {"cycle_band": "标准配置", "tactical_band": "入场窗口",
            "last_sent": {"tactical_extreme:入场窗口": recent}}
    alerts, _ = notify.evaluate_alerts(_dash(tactical_band="杠杆拥挤"), prev, NOW)
    assert len(alerts) == 1 and alerts[0]["cool_key"] == "tactical_extreme:杠杆拥挤"
    # 同档重进 (真抖动) 仍被同键冷却压制
    prev2 = {"cycle_band": "标准配置", "tactical_band": "等待信号",
             "last_sent": {"tactical_extreme:杠杆拥挤": recent}}
    alerts2, _ = notify.evaluate_alerts(_dash(tactical_band="杠杆拥挤"), prev2, NOW)
    assert alerts2 == []


def test_malformed_state_values_self_heal():
    """合法 JSON 但值错型: 消毒后按缺失处理, 功能不得永久静默 (对抗审查发现)。"""
    assert notify._sanitize_state("oops") == {}
    assert notify._sanitize_state(
        {"cycle_band": 123, "tactical_band": "等待信号",
         "last_sent": {"cycle_switch": 12345, "ok": "2026-07-15T00:00:00"}}
    ) == {"tactical_band": "等待信号", "last_sent": {"ok": "2026-07-15T00:00:00"}}
    # 错型 last_sent 直接喂 evaluate_alerts 也不能炸 (消毒是第一道, 这是第二道)
    prev = {"cycle_band": "减配", "tactical_band": "等待信号", "last_sent": "oops"}
    alerts, _ = notify.evaluate_alerts(_dash(cycle_band="标准配置"), prev, NOW)
    assert len(alerts) == 1  # 错型视为已冷却, 提醒照发


def test_message_honesty_invariants():
    """诚实性守卫: warnings 必须进正文, 两类消息必须带'仅供人工确认',
    战术消息必须带样本内免责 (项目界面诚实性准则在推送面的延伸)。"""
    warn = "周期分因子覆盖率仅 40%, 仓位决策可信度低"
    prev = {"cycle_band": "减配", "tactical_band": "等待信号", "last_sent": {}}
    alerts, _ = notify.evaluate_alerts(
        _dash(cycle_band="标准配置", warnings=[warn]), prev, NOW)
    assert "⚠️" in alerts[0]["text"] and warn in alerts[0]["text"]
    assert "仅供人工确认" in alerts[0]["text"]

    prev2 = {"cycle_band": "标准配置", "tactical_band": "等待信号", "last_sent": {}}
    alerts2, _ = notify.evaluate_alerts(_dash(tactical_band="杠杆拥挤"), prev2, NOW)
    t = alerts2[0]["text"]
    assert "仅供人工确认" in t and "样本内" in t and "非收益承诺" in t


def test_check_and_alert_send_failure_retries_next_round(tmp_path, monkeypatch):
    calls = {"n": 0}

    def flaky(text, url):
        calls["n"] += 1
        return calls["n"] >= 2  # 第一次失败, 第二次成功

    monkeypatch.setenv("WECOM_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.delenv("SERVERCHAN_SENDKEY", raising=False)
    monkeypatch.setattr(notify, "_send_wecom", flaky)

    notify.check_and_alert(_dash(cycle_band="减配", lo=15, hi=30), str(tmp_path))  # 基线
    out_fail = notify.check_and_alert(_dash(cycle_band="标准配置"), str(tmp_path))
    assert out_fail["sent"] == 0                          # 发送失败
    out_retry = notify.check_and_alert(_dash(cycle_band="标准配置"), str(tmp_path))
    assert out_retry["sent"] == 1                         # 下轮重试成功


def test_send_test_hits_all_configured_channels(monkeypatch):
    """send_test 绕过状态机, 经所有已配置渠道各发一条, 汇总每渠道结果。"""
    monkeypatch.setenv("WECOM_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setenv("SERVERCHAN_SENDKEY", "SCTxxxxKEY")
    monkeypatch.setattr(notify, "_send_wecom", lambda text, url: True)
    monkeypatch.setattr(notify, "_send_serverchan", lambda t, d, k: False)  # 一通一挂
    out = notify.send_test()
    assert out["channels"] == {"wecom": True, "serverchan": False}
    assert out["any"] is True
    # 渠道抛异常也被吞成 False, 不外泄
    monkeypatch.setattr(notify, "_send_wecom",
                        lambda text, url: (_ for _ in ()).throw(RuntimeError("boom")))
    out2 = notify.send_test()
    assert out2["channels"]["wecom"] is False


def test_phase_change_alert_and_baseline():
    """相位变化: 旧状态无 cycle_phase 键 → 静默基线; 变化且冷却过 → 提醒。"""
    cp_payload = {"phase": "bear_mid", "name": "熊中·下跌中继", "emoji": "🐻",
                  "desc": "d", "criteria": {"drawdown_pct": -45.0, "thermometer": 0.1,
                                            "trend": -1, "ath_age_days": 280},
                  "stats": {"episodes": 13, "fwd": {"365d": {"median_pct": 4.7,
                                                             "pos_pct": 51, "n": 700}}}}
    dash = _dash()
    dash["cycle_phase"] = cp_payload
    # 旧状态文件 (相位功能上线前) 无 cycle_phase 键 → 基线不提醒
    prev = {"cycle_band": "标准配置", "tactical_band": "等待信号", "last_sent": {}}
    alerts, state = notify.evaluate_alerts(dash, prev, NOW)
    assert alerts == [] and state["cycle_phase"] == "bear_mid"
    # 相位变化 → 提醒, 文案带历史置信度与诚实口径
    prev2 = {"cycle_band": "标准配置", "tactical_band": "等待信号",
             "cycle_phase": "pullback_or_bear", "last_sent": {}}
    alerts2, state2 = notify.evaluate_alerts(dash, prev2, NOW)
    assert len(alerts2) == 1 and alerts2[0]["kind"] == "phase_change"
    assert alerts2[0]["state_patch"] == {"cycle_phase": "bear_mid"}
    t = alerts2[0]["text"]
    assert "回调/熊初" in t and "熊中" in t and "365天收益中位" in t and "样本内" in t
    assert state2.get("cycle_phase") == "pullback_or_bear"  # patch 前不推进
    # unknown 相位不参与状态机: 不提醒、不推进已有相位状态
    dash3 = _dash(); dash3["cycle_phase"] = {"phase": "unknown"}
    alerts3, state3 = notify.evaluate_alerts(dash3, prev2, NOW)
    assert alerts3 == []
    assert state3.get("cycle_phase") == "pullback_or_bear"


def test_phase_alert_survives_state_sanitize_roundtrip(tmp_path, monkeypatch):
    """回归 (2026-07 对抗审查 critical): 相位基线必须经 _save_state→_load_state
    消毒往返仍存活 — 曾因白名单漏 cycle_phase 使相位推送在生产路径永远不触发,
    而测试直传 dict 绕过消毒全绿。此测试走真实文件往返。"""
    sent = []
    monkeypatch.setenv("WECOM_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.delenv("SERVERCHAN_SENDKEY", raising=False)
    monkeypatch.setattr(notify, "_send_wecom", lambda text, url: (sent.append(text) or True))

    def dash_with_phase(phase, name):
        d = _dash()
        d["cycle_phase"] = {"phase": phase, "name": name, "emoji": "x", "desc": "d",
                            "criteria": {"drawdown_pct": -40.0, "thermometer": 0.1,
                                         "trend": -1, "ath_age_days": 200}, "stats": None}
        return d

    # 轮1: 记双档基线 (相位基线晚一轮, first_run 提前返回)
    notify.check_and_alert(dash_with_phase("bear_mid", "熊中·下跌中继"), str(tmp_path))
    # 轮2: 相位基线落盘
    notify.check_and_alert(dash_with_phase("bear_mid", "熊中·下跌中继"), str(tmp_path))
    assert notify._load_state(str(tmp_path)).get("cycle_phase") == "bear_mid"  # 消毒后存活
    # 轮3: 相位变化 → 必须触发推送
    out = notify.check_and_alert(dash_with_phase("bear_late", "熊末·冷清超跌"), str(tmp_path))
    assert out["sent"] == 1 and any("相位变化" in t for t in sent)


def test_serverchan_channel_wiring(tmp_path, monkeypatch):
    """Server酱 渠道: SENDKEY 配置后进入渠道列表, title 与 markdown 正文都传入。"""
    calls = []
    monkeypatch.delenv("WECOM_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("SERVERCHAN_SENDKEY", "SCTxxxxKEY")
    monkeypatch.setattr(notify, "_send_serverchan",
                        lambda title, desp, key: (calls.append((title, desp, key)) or True))

    notify.check_and_alert(_dash(cycle_band="减配", lo=15, hi=30), str(tmp_path))  # 基线
    out = notify.check_and_alert(_dash(cycle_band="标准配置"), str(tmp_path))
    assert out == {"alerts": 1, "sent": 1, "channels": ["serverchan"]}
    title, desp, key = calls[0]
    assert "周期换档" in title                    # Server酱 通知标题
    assert "目标仓位" in desp and "仅供人工确认" in desp  # markdown 正文
    assert key == "SCTxxxxKEY"


def test_multi_channel_any_success_advances_state(tmp_path, monkeypatch):
    """双渠道并存: 任一成功即算送达并推进状态 (不因另一渠道失败而卡住重试)。"""
    monkeypatch.setenv("WECOM_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setenv("SERVERCHAN_SENDKEY", "SCTxxxxKEY")
    monkeypatch.setattr(notify, "_send_wecom", lambda text, url: False)        # 企微挂
    monkeypatch.setattr(notify, "_send_serverchan", lambda t, d, k: True)      # Server酱通

    notify.check_and_alert(_dash(cycle_band="减配", lo=15, hi=30), str(tmp_path))  # 基线
    out = notify.check_and_alert(_dash(cycle_band="标准配置"), str(tmp_path))
    assert out["sent"] == 1                       # 一挂一通仍算送达
    out2 = notify.check_and_alert(_dash(cycle_band="标准配置"), str(tmp_path))
    assert out2["alerts"] == 0                     # 状态已推进, 不重复轰炸
