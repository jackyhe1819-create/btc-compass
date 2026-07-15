#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.notify
====================
决策提醒推送 (2026-07 新增) — 半自动闭环的"提醒"一环。

触发条件 (只消费 decision.py 的输出, 不自设阈值, 不触碰档位四处同步不变量):
  1. 周期滞回换档: 生效档位 (decision.cycle.band) 相对上次已提醒状态发生变化
  2. 战术极值进入: 战术档位进入「入场窗口」或「杠杆拥挤」两个极值档

设计约束:
  - 提醒只是提醒, 人工确认后手动执行 — 本模块不做任何交易动作
  - 状态文件在缓存目录 (Render free 无持久盘): 冷启动首轮只记基线不提醒,
    避免每次部署把当前档位当"新换档"轰炸一遍
  - 发送失败不提交对应状态 → 下轮仪表盘刷新自然重试; 每类提醒 6h 冷却兜底
  - 演示数据 (data_synthetic) 熔断: 不提醒也不更新状态
  - 渠道配置驱动: 有对应环境变量才启用, 多渠道任一成功即算送达。目前支持
    企业微信群机器人 (WECOM_WEBHOOK_URL) 与 Server酱³→个人微信 (SERVERCHAN_SENDKEY);
    后续加渠道 = _channels() 里读一个环境变量 + append 一项

能力边界 (如实声明, 2026-07 对抗审查确认):
  检测只随仪表盘刷新发生 (启动预热 + /api/dashboard 过期请求), 无自带定时器。
  Render free 实例无流量 ~15 分钟即休眠: 休眠期间事件零感知, 唤醒时磁盘重建、
  "醒来即处于极值档"会被冷启动基线静默吸收。因此本功能只保证「服务被持续访问
  时段内」的提醒。要 24h 覆盖: 需外部拨测器 (如 UptimeRobot 免费档 5 分钟
  打一次 /api/dashboard) 兼作保活+心跳 — 见 verify.py 的提醒渠道探针。
"""

import os
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .decision import CYCLE_BANDS, HYST_CONFIRM

STATE_FILENAME = "notify_state.json"

# 每类提醒最小间隔 (滞回已防周期档抖动; 这是战术档在阈值附近反复进出的兜底)
COOLDOWN_HOURS = 6

# 历史深度门控 — 与 decision.py 滞回可信度阈值同源 (HYST_CONFIRM×4=20天)。
# 冷启动时预热刷新(t≈5s)先于 90 天回填(t≈30s), 首轮决策档位是无滞回单点退化值;
# 此时记基线会在回填落地后触发"伪换档"推送 (2026-07 对抗审查 major 发现)。
# 历史不足: 既不记基线也不提醒, 回填完成后首个厚历史轮次走 first_run 静默记基线。
MIN_HISTORY_DAYS = HYST_CONFIRM * 4

# 战术档位中值得打断用户的两个极值档 (档名与 decision.TACTICAL_BANDS 一致,
# tests/test_notify.py 有对账守卫)
TACTICAL_ALERT_BANDS = ("入场窗口", "杠杆拥挤")

_CYCLE_BAND_ORDER = [b[1] for b in CYCLE_BANDS]  # 索引小 = 仓位高


# ============================================================
# 纯逻辑: 状态比对 → 应发提醒清单 (不做 IO, 可单测)
# ============================================================

def evaluate_alerts(dashboard: dict, prev_state: dict,
                    now: Optional[datetime] = None) -> Tuple[List[dict], dict]:
    """
    比对当前决策与上次已提醒状态, 返回 (应发提醒列表, 提醒全部发送成功后的新状态)。

    prev_state 结构: {"cycle_band": str, "tactical_band": str,
                      "last_sent": {kind: iso_ts}}
    返回的每条提醒: {"kind": ..., "title": ..., "text": ...(企微 markdown),
                    "state_patch": {...}}  — 调用方发送成功后才应用 patch,
    保证失败重试; last_sent 冷却戳由调用方在发送成功时写入。
    """
    now = now or datetime.now()
    decision = dashboard.get("decision") or {}
    alerts: List[dict] = []
    new_state = dict(prev_state or {})
    new_state.setdefault("last_sent", {})

    if dashboard.get("data_synthetic") or not decision:
        return [], new_state

    cyc = decision.get("cycle") or {}
    tac = decision.get("tactical") or {}
    cur_cycle = cyc.get("band")
    cur_tactical = tac.get("band")
    if not cur_cycle or not cur_tactical:
        return [], new_state

    # 薄历史门控: 回填未完成时档位不可信, 不记基线不提醒 (见 MIN_HISTORY_DAYS 注释)
    hist_days = (decision.get("hysteresis") or {}).get("history_days") or 0
    if hist_days < MIN_HISTORY_DAYS:
        return [], new_state

    first_run = "cycle_band" not in (prev_state or {})
    if first_run:
        # 冷启动基线: 只记状态不提醒 (Render 无持久盘, 每次部署都会走到这)
        new_state["cycle_band"] = cur_cycle
        new_state["tactical_band"] = cur_tactical
        return [], new_state

    def _cooled(kind: str) -> bool:
        ls = prev_state.get("last_sent")
        ts = ls.get(kind) if isinstance(ls, dict) else None
        if not isinstance(ts, str) or not ts:
            return True  # 缺失/错型 (手改文件等) 一律视为已冷却, 保证功能自愈
        try:
            return now - datetime.fromisoformat(ts) >= timedelta(hours=COOLDOWN_HOURS)
        except ValueError:
            return True

    # ── 1. 周期滞回换档 ──
    prev_cycle = prev_state.get("cycle_band")
    if cur_cycle != prev_cycle and _cooled("cycle_switch"):
        try:
            direction = ("↑ 上调" if _CYCLE_BAND_ORDER.index(cur_cycle)
                         < _CYCLE_BAND_ORDER.index(prev_cycle) else "↓ 下调")
        except ValueError:
            direction = "→ 变更"
        alerts.append({
            "kind": "cycle_switch",
            "title": f"周期换档: {prev_cycle} → {cur_cycle}",
            "text": _format_cycle_alert(dashboard, prev_cycle, direction),
            "state_patch": {"cycle_band": cur_cycle},
        })
    elif cur_cycle != prev_cycle:
        pass  # 冷却中: 状态不更新, 冷却结束后下轮补发
    else:
        new_state["cycle_band"] = cur_cycle

    # ── 2. 战术极值进入 ──
    # 冷却键按档名分键: 「入场窗口」与「杠杆拥挤」语义相反, 从一个极端对穿到
    # 另一个极端 (跨 0.60 分幅) 不是抖动, 不能被共享冷却吞掉保护性信号
    prev_tactical = prev_state.get("tactical_band")
    if cur_tactical != prev_tactical:
        cool_key = f"tactical_extreme:{cur_tactical}"
        if cur_tactical in TACTICAL_ALERT_BANDS and _cooled(cool_key):
            alerts.append({
                "kind": "tactical_extreme",
                "cool_key": cool_key,
                "title": f"战术极值: 进入「{cur_tactical}」",
                "text": _format_tactical_alert(dashboard, prev_tactical),
                "state_patch": {"tactical_band": cur_tactical},
            })
        elif cur_tactical in TACTICAL_ALERT_BANDS:
            pass  # 极值档但同档冷却中: 不更新状态, 冷却后仍在极值则补发
        else:
            new_state["tactical_band"] = cur_tactical  # 回到普通档静默记录
    else:
        new_state["tactical_band"] = cur_tactical

    return alerts, new_state


def _fmt_score(v) -> str:
    try:
        return f"{float(v):+.3f}"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_price(v) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "n/a"


def _warnings_block(dashboard: dict) -> str:
    ws = ((dashboard.get("decision") or {}).get("warnings")) or []
    if not ws:
        return ""
    return "\n> ⚠️ " + "\n> ⚠️ ".join(str(w) for w in ws)


def _format_cycle_alert(dashboard: dict, prev_band: str, direction: str) -> str:
    d = dashboard.get("decision") or {}
    cyc = d.get("cycle") or {}
    tac = d.get("tactical") or {}
    return (
        f"## 🧭 BTC Compass · 周期换档\n"
        f"**{prev_band} → {cyc.get('band')}**（{direction}）\n"
        f"目标仓位: **{cyc.get('target_lo')}-{cyc.get('target_hi')}%**"
        f"（中值 {cyc.get('target_mid')}%）\n"
        f"> 周期分 {_fmt_score(dashboard.get('total_score'))} · "
        f"战术分 {_fmt_score(dashboard.get('tactical_score'))}\n"
        f"> BTC {_fmt_price(dashboard.get('btc_price'))} · "
        f"执行节奏「{tac.get('pace', 'n/a')}」"
        f"{_warnings_block(dashboard)}\n"
        f"滞回换档 (δ={d.get('hysteresis', {}).get('delta')}, "
        f"确认 {d.get('hysteresis', {}).get('confirm')} 天) — 提醒仅供人工确认, 非自动交易\n"
        f"[打开仪表盘](https://btc-compass.onrender.com)"
    )


def _format_tactical_alert(dashboard: dict, prev_band: str) -> str:
    d = dashboard.get("decision") or {}
    cyc = d.get("cycle") or {}
    tac = d.get("tactical") or {}
    return (
        f"## ⚡ BTC Compass · 战术极值\n"
        f"**{prev_band or 'n/a'} → {tac.get('band')}**\n"
        f"执行节奏: **{tac.get('pace')}** — {tac.get('advice')}\n"
        f"> 战术分 {_fmt_score(dashboard.get('tactical_score'))} · "
        f"周期分 {_fmt_score(dashboard.get('total_score'))}\n"
        f"> BTC {_fmt_price(dashboard.get('btc_price'))} · "
        f"当前档位「{cyc.get('band', 'n/a')}」目标仓位 "
        f"{cyc.get('target_lo')}-{cyc.get('target_hi')}%"
        f"{_warnings_block(dashboard)}\n"
        f"战术分只定执行节奏不改目标仓位; 文中回测统计为样本内参考, "
        f"非收益承诺 — 提醒仅供人工确认\n"
        f"[打开仪表盘](https://btc-compass.onrender.com)"
    )


# ============================================================
# 渠道: 配置驱动 (有环境变量才启用)
# ============================================================

def _send_wecom(text: str, webhook_url: str) -> bool:
    """企业微信群机器人 markdown 消息。返回是否发送成功。"""
    import requests
    try:
        # 企微 markdown 上限 4096 字节 (非字符, 中文 UTF-8 每字 3 字节) — 按字节安全截断
        content = text.encode("utf-8")[:4000].decode("utf-8", "ignore")
        r = requests.post(webhook_url,
                          json={"msgtype": "markdown",
                                "markdown": {"content": content}},
                          timeout=10)
        ok = r.status_code == 200 and r.json().get("errcode") == 0
        if not ok:
            print(f"⚠️ 企微推送被拒: HTTP {r.status_code} {r.text[:200]}")
        return ok
    except Exception as e:
        print(f"⚠️ 企微推送失败: {e}")
        return False


def _send_serverchan(title: str, desp: str, sendkey: str) -> bool:
    """Server酱³ (方糖) → 个人微信服务号消息。title 为通知标题 (限32字),
    desp 为 markdown 正文。返回是否发送成功。"""
    import requests
    try:
        r = requests.post(f"https://sctapi.ftqq.com/{sendkey}.send",
                          data={"title": title[:32],
                                "desp": desp[:30000]},
                          timeout=10)
        ok = r.status_code == 200 and r.json().get("code") == 0
        if not ok:
            print(f"⚠️ Server酱推送被拒: HTTP {r.status_code} {r.text[:200]}")
        return ok
    except Exception as e:
        print(f"⚠️ Server酱推送失败: {e}")
        return False


def _channels() -> List[Tuple[str, callable]]:
    """已配置的推送渠道列表 (send_fn 签名统一为 (title, text) → bool)。
    加渠道 = 读一个环境变量 + append 一项。"""
    out = []
    wecom = os.environ.get("WECOM_WEBHOOK_URL", "").strip()
    if wecom:
        out.append(("wecom", lambda title, text: _send_wecom(text, wecom)))
    sendkey = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if sendkey:
        out.append(("serverchan",
                    lambda title, text: _send_serverchan(title, text, sendkey)))
    return out


# ============================================================
# IO 入口: app 仪表盘刷新后调用
# ============================================================

_warned_no_channel = False  # 未配置渠道只在首轮打一条日志, 避免每 5 分钟刷屏

def _state_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, STATE_FILENAME)


def _sanitize_state(raw) -> dict:
    """形状消毒: 合法 JSON 但值错型 (手改/外部工具写坏) 不能让功能永久静默,
    错型字段按缺失处理 → 走 first_run 基线自愈。"""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k in ("cycle_band", "tactical_band"):
        if isinstance(raw.get(k), str):
            out[k] = raw[k]
    ls = raw.get("last_sent")
    if isinstance(ls, dict):
        out["last_sent"] = {k: v for k, v in ls.items()
                            if isinstance(k, str) and isinstance(v, str)}
    return out


def _load_state(cache_dir: str) -> dict:
    try:
        with open(_state_path(cache_dir), "r", encoding="utf-8") as f:
            return _sanitize_state(json.load(f))
    except Exception:
        return {}


def _save_state(cache_dir: str, state: dict) -> None:
    """原子写 (mkstemp+replace, 与 app._save_cache_to_disk 同约定) —
    进程写盘中途被杀不能留下截断 JSON 吞掉待重试提醒。"""
    try:
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=cache_dir, prefix=".notify_state_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, _state_path(cache_dir))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        print(f"⚠️ 提醒状态写入失败: {e}")


def check_and_alert(dashboard: dict, cache_dir: str) -> dict:
    """
    主入口 — 由 app._do_refresh_dashboard 在 decision 计算后调用。
    永不抛异常 (刷新主流程不能被提醒功能拖垮)。
    返回摘要 {"alerts": n, "sent": n, "channels": [...]} 供探针/日志。
    """
    global _warned_no_channel
    try:
        channels = _channels()
        if not channels:
            if not _warned_no_channel:
                _warned_no_channel = True
                print("ℹ️ 提醒渠道未配置 (WECOM_WEBHOOK_URL / SERVERCHAN_SENDKEY "
                      "均为空) — 推送功能禁用")
            return {"alerts": 0, "sent": 0, "channels": []}

        prev_state = _load_state(cache_dir)
        alerts, new_state = evaluate_alerts(dashboard, prev_state)
        sent = 0
        now_iso = datetime.now().isoformat(timespec="seconds")
        for alert in alerts:
            ok_any = False
            for name, send in channels:
                if send(alert["title"], alert["text"]):
                    ok_any = True
                else:
                    print(f"⚠️ 提醒 [{alert['kind']}] 经 {name} 发送失败")
            if ok_any:
                sent += 1
                new_state.update(alert["state_patch"])
                new_state.setdefault("last_sent", {})[
                    alert.get("cool_key", alert["kind"])] = now_iso
                print(f"📣 已推送提醒: {alert['title']}")
            # 全渠道失败: 不应用 state_patch → 下轮刷新重试
        _save_state(cache_dir, new_state)
        return {"alerts": len(alerts), "sent": sent,
                "channels": [n for n, _ in channels]}
    except Exception as e:
        print(f"⚠️ 提醒检查异常 (已忽略, 不影响刷新): {e}")
        return {"alerts": 0, "sent": 0, "channels": [], "error": str(e)}
