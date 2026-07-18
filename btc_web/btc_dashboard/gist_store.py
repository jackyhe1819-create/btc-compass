#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.gist_store
========================
评分历史的 GitHub Gist 持久化 —— 跨冷启动恢复稳定基线。

动机 (2026-07):
  Render free 实例每次冷启动清盘, 评分历史随之丢失, 回填线程按 365 天重建。
  重建时链上因子受 10 请求/小时限流, 逐次重建间因子可得性抖动造成 ±0.01 量级
  的桶均值噪声; decision.py 的滞回连击确认在换档确认线 (0.00±δ) 附近被这层噪声
  在不同冷启动间来回翻转 —— 同一天"减配↔中性观望"来回摆。把上一次算好的评分
  历史推到一个私有 Gist, 冷启动时先恢复这份稳定字节, 只重建近端缺口, 滞回重放
  窗口的大部分即在冷启动间字节一致, 从根上止住"重掷骰子"。

设计约束:
  - 完全可选: GIST_ID + GIST_TOKEN 两个环境变量任一缺失 → 功能整体静默禁用
    (fetch 返回 None / push 返回 False), 现行为零变化。
  - 绝不 crash 主流程: GitHub API 任何异常 (超时/限流/网络/解析) 一律打日志后降级。
  - 超时 10s (与 notify 推送同约定)。
  - token 脱敏: 异常原文 print 直入 Render 日志流, 凭据泄漏可被用来篡改 Gist ——
    参照 notify._redact 的凭据脱敏先例, 日志里 token 只留前后 4 位。
  - 口径版本门 (关键正确性): 载荷携带 backfill._MARKER + config_fingerprint;
    恢复时 marker 版本 != 当前 → 拒绝恢复 (口径已升级旧历史失效), 绝不让 Gist
    旧数据冲掉 v7 式口径修正 (见 restore_entries)。

最小权限建议: 用 gist-only 细粒度 token (只勾 gists 读写), 且把 Gist 设为
secret (非 public) —— 评分历史无隐私但也无须公开。
"""

import os
import json

import requests

# GitHub Gist 里存放评分历史载荷的文件名 (一个 Gist 可含多文件, 我们只用这一个)
GIST_FILENAME = "score_history.json"
_API_BASE = "https://api.github.com/gists"
_API_TIMEOUT = 10  # 秒, 与 notify 推送同约定
_HEADERS_BASE = {"Accept": "application/vnd.github+json",
                 "User-Agent": "btc-compass",
                 "X-GitHub-Api-Version": "2022-11-28"}


def _gist_config():
    """读 (gist_id, token); 任一缺失/空 → (None, None) 表示功能禁用。"""
    gid = (os.environ.get("GIST_ID") or "").strip()
    tok = (os.environ.get("GIST_TOKEN") or "").strip()
    if not gid or not tok:
        return None, None
    return gid, tok


def is_enabled() -> bool:
    """两个环境变量齐备才启用 Gist 持久化。"""
    gid, tok = _gist_config()
    return bool(gid and tok)


def _mask(secret: str) -> str:
    """凭据脱敏: 长串保留前4后4便于对账, 短串全掩码 (与 notify._mask 同约定)。"""
    return f"{secret[:4]}…{secret[-4:]}" if len(secret) > 8 else "***"


def _redact(msg: str, token: str) -> str:
    """抹掉日志串里内嵌的 token —— GitHub token 走 Authorization 头, requests 异常
    一般不带头部, 但防御性地把 token 字面量替换成掩码 (notify 凭据泄漏教训)。"""
    if token:
        msg = msg.replace(token, _mask(token))
    return msg


def _auth_headers(token: str) -> dict:
    h = dict(_HEADERS_BASE)
    h["Authorization"] = f"token {token}"
    return h


def build_payload(entries: list) -> dict:
    """把评分历史条目盖上"当前口径身份戳"封装成 Gist 载荷。

    marker = backfill._MARKER (口径版本, 每次回填口径修复即 bump) —— 恢复门的判据。
    config_hash = version_stamp.config_fingerprint() (档位/权重/滞回指纹) —— 审计用,
      不作恢复门 (entries 存的是原始评分, decision 层每次实时重算, 跨配置可复用)。
    惰性导入避免任何导入期环路 (backfill 导入 score_history, score_history 又用本模块)。"""
    from .backfill import _MARKER
    from .version_stamp import config_fingerprint
    return {
        "marker": _MARKER,
        "config_hash": config_fingerprint(),
        "entries": entries,
    }


def fetch_history_from_gist():
    """GET Gist → 解析出评分历史载荷 dict {marker, config_hash, entries}, 或 None。

    功能禁用 / HTTP 非 200 / 无目标文件 / 解析失败 / 任何异常 → None (降级)。
    大文件 (>1MB) 会被 GitHub 在 gist GET 响应里截断 (truncated=True), 此时改走
    raw_url 取全量。"""
    gid, tok = _gist_config()
    if not gid or not tok:
        return None
    try:
        r = requests.get(f"{_API_BASE}/{gid}", headers=_auth_headers(tok),
                         timeout=_API_TIMEOUT)
        if r.status_code != 200:
            print(f"⚠️ Gist 拉取失败: HTTP {r.status_code} (降级)")
            return None
        files = (r.json() or {}).get("files") or {}
        f = files.get(GIST_FILENAME)
        if not f:
            print(f"ℹ️ Gist 无 {GIST_FILENAME} 文件 (首次运行?) — 视为无历史")
            return None
        content = f.get("content")
        if f.get("truncated") and f.get("raw_url"):
            rr = requests.get(f["raw_url"], headers={"User-Agent": "btc-compass"},
                              timeout=_API_TIMEOUT)
            if rr.status_code == 200:
                content = rr.text
        if not content:
            return None
        payload = json.loads(content)
        return payload if isinstance(payload, dict) else None
    except Exception as e:
        print(f"⚠️ Gist 拉取异常 (降级, 走正常重建): {_redact(str(e), tok)}")
        return None


def restore_entries(expected_marker: str):
    """恢复评分历史条目 (口径版本门守卫): 返回 entries 列表, 或 None (禁用/失败/拒绝)。

    关键正确性: 载荷 marker != expected_marker → 拒绝恢复并打日志。口径升级后旧历史
    的桶均值与新口径不可比 (与 backfill 的 marker 迁移同理), 恢复旧数据会静默冲掉
    v7 式口径修正; 拒绝后调用方走正常重建, 重建完再推送新版覆盖 Gist。
    expected_marker 由调用方 (backfill) 显式传入其权威 _MARKER, 便于隔离单测。"""
    payload = fetch_history_from_gist()
    if payload is None:
        return None
    marker = payload.get("marker")
    if marker != expected_marker:
        print(f"⚠️ Gist 历史 marker 不匹配 (载荷 {marker!r} != 当前 {expected_marker!r}) "
              f"— 口径已升级旧历史失效, 拒绝恢复, 走正常重建后推送新版")
        return None
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        return None
    print(f"↩️ 从 Gist 恢复评分历史 {len(entries)} 条 "
          f"(marker={marker}, config={payload.get('config_hash')})")
    return entries


def push_history_to_gist(payload: dict) -> bool:
    """PATCH Gist 写入载荷 JSON, 返回是否成功。

    功能禁用 → False; HTTP 非 200 / 任何异常 → 打日志后 False (降级, 不影响主流程)。"""
    gid, tok = _gist_config()
    if not gid or not tok:
        return False
    try:
        body = json.dumps(payload, ensure_ascii=False)
        r = requests.patch(f"{_API_BASE}/{gid}", headers=_auth_headers(tok),
                           json={"files": {GIST_FILENAME: {"content": body}}},
                           timeout=_API_TIMEOUT)
        ok = r.status_code == 200
        if not ok:
            print(f"⚠️ Gist 推送被拒: HTTP {r.status_code} (降级)")
        else:
            n = len(payload.get("entries") or [])
            print(f"☁️ 评分历史已推送到 Gist: {n} 条")
        return ok
    except Exception as e:
        print(f"⚠️ Gist 推送异常 (降级, 不影响主流程): {_redact(str(e), tok)}")
        return False
