#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.news
==================
BlockBeats 快讯抓取 + 翻译缓存。
"""

import os
import requests
from datetime import datetime, timedelta, timezone
from functools import lru_cache

_translator_instance = None


def _cached_translate(text: str) -> str:
    """带 LRU 缓存的翻译函数"""
    global _translator_instance
    try:
        if _translator_instance is None:
            from deep_translator import GoogleTranslator
            _translator_instance = GoogleTranslator(source='en', target='zh-CN')
        return _translator_instance.translate(text)
    except Exception as e:
        print(f"⚠️ 翻译失败: {e}")
        return text


def _bb_signed_headers(method: str, path: str, query: dict = None, body=None) -> dict:
    """为 BlockBeats v2 API 生成 HMAC-SHA256 签名头。"""
    import hashlib, hmac, time as _time, string, random, json as _json, os as _os

    # 内置凭据为服务商 demo 键: 只用于对 BlockBeats v2 快讯做只读公开新闻 GET 签名
    # (无账户/资金/写操作权限), 非机密。生产可用 BLOCKBEATS_APP_KEY / BLOCKBEATS_APP_SECRET
    # 环境变量覆盖轮换 (见 render.yaml, sync:false)。
    APP_KEY = _os.environ.get("BLOCKBEATS_APP_KEY", "bb_demo_app")
    APP_SECRET = _os.environ.get("BLOCKBEATS_APP_SECRET", "bb_demo_secret_2026_01")

    timestamp = str(int(_time.time() * 1000))
    nonce = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(16))
    method_upper = method.upper()

    if method_upper == "GET":
        keys = sorted((query or {}).keys())
        canonical = "&".join(f"{k}={query[k] if query[k] is not None else ''}" for k in keys) if keys else ""
    else:
        canonical = hashlib.md5(_json.dumps(body).encode()).hexdigest() if body else ""

    string_to_sign = f"{method_upper}|{path}|{timestamp}|{nonce}|{canonical}"
    signature = hmac.new(APP_SECRET.encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()

    return {
        "X-App-Key": APP_KEY,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": signature,
        "X-Encrypt": "0",
    }


def _bb_fetch_flash_detail(article_id) -> str:
    """拉取单条快讯正文（HTML），失败返回空串。"""
    if not article_id:
        return ""
    try:
        query = {"article_id": str(article_id)}
        signed = _bb_signed_headers("GET", "/v2/newsflash/detail", query)
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "Accept": "application/json",
            **signed,
        }
        r = requests.get(
            "https://api.blockbeats.cn/v2/newsflash/detail",
            params=query, headers=headers, timeout=8,
        )
        if r.status_code != 200:
            return ""
        return (r.json().get("data") or {}).get("content") or ""
    except Exception:
        return ""


def fetch_crypto_news(limit: int = 20) -> list:
    """
    获取律动 BlockBeats 快讯 - 最近 72 小时内容
    - 列表数据源: /v2/newsflash/detective（只有标题）
    - 正文数据源: /v2/newsflash/detail（并发拉取，逐条带回 content）
    - 自动过滤 72 小时以外的条目，按发布时间倒序
    """
    import re
    from datetime import datetime, timedelta, timezone
    from concurrent.futures import ThreadPoolExecutor

    def clean_html(text: str) -> str:
        clean = re.sub(r'<[^>]+>', '', text or '').strip()
        return clean[:200] + '...' if len(clean) > 200 else clean

    cutoff = datetime.now() - timedelta(hours=72)
    cutoff_ts = int(cutoff.timestamp())

    news_list = []

    try:
        query = {"limit": "50"}
        sign_path = "/v2/newsflash/detective"
        signed = _bb_signed_headers("GET", sign_path, query)
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "Accept": "application/json",
            **signed,
        }
        response = requests.get(
            "https://api.blockbeats.cn/v2/newsflash/detective",
            params=query,
            headers=headers,
            timeout=15,
        )
        if response.status_code != 200:
            print(f"⚠️ BlockBeats v2 API HTTP {response.status_code}")
            return news_list

        data = response.json()
        items = data.get("data", [])
        if not isinstance(items, list):
            items = []

        _tz8 = timezone(timedelta(hours=8))

        # 第一轮: 过滤窗口内的条目，记录 article_id
        prelim = []  # (article_id, title, ts)
        for item in items:
            ts = int(item.get("add_time", 0) or 0)
            if ts and ts < cutoff_ts:
                continue
            title = (item.get("title") or "").strip()
            if not title:
                continue
            article_id = item.get("article_id") or item.get("id", "")
            prelim.append((article_id, title, ts, item))

        # 第二轮: 并发拉取每条正文
        ids_to_fetch = [aid for aid, _, _, _ in prelim if aid]
        details_map = {}
        if ids_to_fetch:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = pool.map(_bb_fetch_flash_detail, ids_to_fetch)
                for aid, content in zip(ids_to_fetch, results):
                    details_map[str(aid)] = content

        for aid, title, ts, raw in prelim:
            pub = datetime.fromtimestamp(ts, tz=_tz8) if ts else datetime.now(_tz8)
            url = f"https://www.theblockbeats.info/flash/{aid}"
            content_html = details_map.get(str(aid), "") or raw.get("content") or raw.get("abstract") or ""
            news_list.append({
                "title": title,
                "url": url,
                "source": "律动 BlockBeats",
                "icon": "⚡",
                "summary": clean_html(content_html),
                "time": pub.strftime("%m-%d %H:%M"),
                "_ts": ts,
            })

    except Exception as e:
        print(f"⚠️ BlockBeats Flash API 失败: {e}")
        import traceback; traceback.print_exc()

    # 按时间排序（最新在前），移除内部字段
    news_list.sort(key=lambda x: x.get("_ts", 0), reverse=True)
    for item in news_list:
        item.pop("_ts", None)

    return news_list

