#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.whales
====================
鲸鱼活动 + 交易所余额（mempool.space）。
"""

import os
import json
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
from typing import Tuple, Dict, Optional

from .core import fetch_realtime_btc_price


def fetch_exchange_balance_display() -> dict:
    """
    获取交易所BTC余额展示数据（给前端展示用）
    - 返回各交易所余额明细
    - 通过本地快照文件对比计算 24h/7d/30d 变化（取代 blockchain.info 反推）
    """
    import time as _time, json, os
    
    EXCHANGE_WALLETS = {
        "Binance": [
            "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo",
            "bc1qm34lsc65zpw79lxes69zkqmk6ee3ewf0j77s3h",
            "3M219KR5vEneNb47ewrPfWyb5jQ2DjxRP6",
            "1NDyJtNTjmwk5xPNhjgAMu4HDHigtobu1s",
            "39884E3j6KZj82FK4vcCrkUvWYL5MQaS3v",
        ],
        "Bitfinex": [
            "bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97",
        ],
        "Kraken": [
            "bc1qr4dl5wa7kl8yu792dceg9z5knl2gkn220lk7a9",
            "3AfSMeESFHT2xLqkR1ufoKcxNqNP5bfcaX",
        ],
        "Crypto.com": [
            "bc1qpy4jwethqenp4r7hqls660wy8287vw0my32lmy",
            "bc1q4c8n5t00jmj8temxdgcc3t32nkg2wjwz24lywv",
        ],
        "Gemini": [
            "3JZq4atUahhuA9rLhXLMhhTo133J9rF97j",
        ],
    }
    
    result = {
        "exchanges": [],
        "total": 0,
        "changes": {"24h": None, "7d": None, "30d": None},
        "history": [],
        "fetched": 0,
        "error": None,
    }
    
    total_btc = 0
    exchange_list = []
    
    try:
        # 第1步: 获取所有地址当前余额 (mempool.space)
        for exchange, addrs in EXCHANGE_WALLETS.items():
            exchange_total = 0
            for addr in addrs:
                try:
                    resp = requests.get(
                        f"https://mempool.space/api/address/{addr}",
                        timeout=8,
                        headers={"User-Agent": "Mozilla/5.0"}
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        chain = data.get("chain_stats", {})
                        balance = (chain.get("funded_txo_sum", 0) - chain.get("spent_txo_sum", 0)) / 1e8
                        exchange_total += balance
                        result["fetched"] += 1
                    elif resp.status_code == 429:
                        break
                except Exception:
                    pass
                _time.sleep(0.3)
            
            if exchange_total > 0:
                exchange_list.append({
                    "name": exchange,
                    "balance": round(exchange_total, 2),
                })
                total_btc += exchange_total
        
        exchange_list.sort(key=lambda x: -x["balance"])
        result["exchanges"] = exchange_list
        result["total"] = round(total_btc, 2)
        
        # 第2步: 通过本地快照文件对比计算 24h/7d/30d 变化
        # 数据文件放包外（btc_web/exchange_balance_history.json），避免后续重构丢历史
        snapshot_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "exchange_balance_history.json"
        )
        history = []

        try:
            if os.path.exists(snapshot_file):
                with open(snapshot_file, "r") as f:
                    history = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, Exception):
            history = []
        
        if history and total_btc > 0:
            now = datetime.now()
            target_windows = {
                "24h": timedelta(hours=24),
                "7d": timedelta(days=7),
                "30d": timedelta(days=30),
            }
            
            for period, delta in target_windows.items():
                target_time = now - delta
                # 找到最接近目标时间的快照
                best_snap = None
                best_diff = None
                
                for snap in history:
                    try:
                        snap_time = datetime.fromisoformat(snap["timestamp"])
                        diff = abs((snap_time - target_time).total_seconds())
                        # 允许 ±50% 的时间偏差 (如 24h 窗口允许 12h-36h 范围的快照)
                        max_drift = delta.total_seconds() * 0.5
                        if diff <= max_drift and (best_diff is None or diff < best_diff):
                            best_snap = snap
                            best_diff = diff
                    except Exception:
                        continue
                
                if best_snap and best_snap.get("total", 0) > 0:
                    prev_total = best_snap["total"]
                    change = total_btc - prev_total
                    pct = (change / prev_total) * 100
                    result["changes"][period] = {
                        "change_btc": round(change, 2),
                        "change_pct": round(pct, 4),
                        "prev_total": round(prev_total, 2),
                    }
        
        # 第3步: 保存当前快照
        if total_btc > 0:
            try:
                history.append({
                    "timestamp": datetime.now().isoformat(),
                    "total": round(total_btc, 2),
                    "details": {e["name"]: e["balance"] for e in exchange_list}
                })
                # 保留最近720条 (按每小时采集一次，可覆盖30天)
                history = history[-720:]
                with open(snapshot_file, "w") as f:
                    json.dump(history, f, indent=2)
            except Exception:
                pass
        
    except Exception as e:
        result["error"] = str(e)
    
    return result


def fetch_whale_volume_stats() -> dict:
    """
    获取 BTC 买卖量统计 (24h / 7d / 30d)
    - 数据源: Binance Kline API (taker buy/sell volume)
    - 备用: Binance.us API (规避美国地区 451 封锁)
    - 返回: 各时间段内的买入量、卖出量、买入占比
    """
    result = {
        "24h": {"buy": 0, "sell": 0, "total": 0, "buy_ratio": 50},
        "7d": {"buy": 0, "sell": 0, "total": 0, "buy_ratio": 50},
        "30d": {"buy": 0, "sell": 0, "total": 0, "buy_ratio": 50},
    }
    
    # 多 endpoint 回退: api.binance.com -> api.binance.us -> data-api.binance.vision
    endpoints = [
        "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=30",
        "https://api.binance.us/api/v3/klines?symbol=BTCUSD&interval=1d&limit=30",
        "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=30",
    ]
    
    klines = None
    for url in endpoints:
        try:
            response = requests.get(
                url,
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if response.status_code == 200:
                klines = response.json()
                print(f"✅ Binance Kline OK via {url.split('/')[2]}")
                break
            else:
                print(f"⚠️ Binance Kline {url.split('/')[2]} returned {response.status_code}, trying next...")
        except Exception as e:
            print(f"⚠️ Binance Kline {url.split('/')[2]} failed: {e}, trying next...")
    
    if klines:
        for period_name, days in [("24h", 1), ("7d", 7), ("30d", 30)]:
            subset = klines[-days:]
            total_vol = sum(float(k[5]) for k in subset)
            buy_vol = sum(float(k[9]) for k in subset)
            sell_vol = total_vol - buy_vol
            buy_ratio = (buy_vol / total_vol * 100) if total_vol > 0 else 50
            
            result[period_name] = {
                "buy": round(buy_vol, 1),
                "sell": round(sell_vol, 1),
                "total": round(total_vol, 1),
                "buy_ratio": round(buy_ratio, 1),
            }
    else:
        print("⚠️ All Binance endpoints failed for volume stats")
    
    return result


def fetch_whale_activity(min_btc: int = 10, limit: int = 50) -> list:
    """
    获取 BTC 鲸鱼/大额交易监控
    - 主力数据源: mempool.space（国内可访问，响应快）
      1. 最新区块已确认大额交易（/api/block/{hash}/txs）
      2. 内存池最近未确认交易（/api/mempool/recent）
    - 全部失败: 返回单条"数据源暂不可用"状态行, 绝不伪造交易
    - 按时间排序，最新在前
    """
    whale_list = []
    seen_hashes = set()

    # 获取当前 BTC 价格（复用多源实时价格函数）
    btc_price = fetch_realtime_btc_price()
    if btc_price is None:
        btc_price = 83000  # 所有 API 均失败时的最终后备

    min_sat = min_btc * 100_000_000

    def classify_tx(btc_amount):
        if btc_amount >= 1000: return "🐋 巨鲸", "🐋"
        if btc_amount >= 500:  return "🔥 超大额", "🔥"
        if btc_amount >= 100:  return "💰 大额", "💰"
        if btc_amount >= 50:   return "📊 中额", "📊"
        return "💵 交易", "💵"

    HEADERS = {"User-Agent": "Mozilla/5.0"}
    from concurrent.futures import ThreadPoolExecutor as _TP, as_completed as _ac

    def _fetch_confirmed():
        """扫最新 2 个区块的前 25 笔交易"""
        confirmed = []
        try:
            tip_resp = requests.get("https://mempool.space/api/blocks/tip/hash", timeout=5, headers=HEADERS)
            if tip_resp.status_code != 200:
                return confirmed
            current_hash = tip_resp.text.strip()

            for _ in range(2):
                if not current_hash:
                    break
                txs_resp = requests.get(
                    f"https://mempool.space/api/block/{current_hash}/txs/0",
                    timeout=8, headers=HEADERS
                )
                if txs_resp.status_code != 200:
                    break
                for tx in txs_resp.json():
                    total_sat = sum(v.get("value", 0) for v in tx.get("vout", []))
                    if total_sat < min_sat:
                        continue
                    txid = tx.get("txid", "")
                    btc_amt = total_sat / 1e8
                    tx_type, icon = classify_tx(btc_amt)
                    ts = tx.get("status", {}).get("block_time", 0) or datetime.now().timestamp()
                    confirmed.append({
                        "amount": f"{btc_amt:,.2f} BTC",
                        "value_usd": f"${btc_amt * btc_price:,.0f}",
                        "hash": txid[:10] + "...",
                        "time": datetime.fromtimestamp(ts).strftime("%m-%d %H:%M"),
                        "timestamp": ts,
                        "type": tx_type, "icon": icon,
                        "url": f"https://mempool.space/tx/{txid}"
                    })
                # 获取前一个区块 hash
                blk_resp = requests.get(
                    f"https://mempool.space/api/block/{current_hash}",
                    timeout=5, headers=HEADERS
                )
                current_hash = blk_resp.json().get("previousblockhash", "") if blk_resp.status_code == 200 else ""
        except Exception as e:
            print(f"⚠️ 区块扫描: {e}")
        return confirmed

    def _fetch_mempool():
        """内存池未确认大额交易"""
        pending = []
        try:
            resp = requests.get("https://mempool.space/api/mempool/recent", timeout=6, headers=HEADERS)
            if resp.status_code == 200:
                for tx in resp.json():
                    total_sat = tx.get("value", 0)
                    if total_sat < min_sat:
                        continue
                    txid = tx.get("txid", "")
                    btc_amt = total_sat / 1e8
                    tx_type, icon = classify_tx(btc_amt)
                    pending.append({
                        "amount": f"{btc_amt:,.2f} BTC",
                        "value_usd": f"${btc_amt * btc_price:,.0f}",
                        "hash": txid[:10] + "...",
                        "time": "待确认",
                        "timestamp": datetime.now().timestamp() - 1,
                        "type": f"⏳ {tx_type.split(' ', 1)[-1]}",
                        "icon": "⏳",
                        "url": f"https://mempool.space/tx/{txid}"
                    })
        except Exception as e:
            print(f"⚠️ mempool/recent: {e}")
        return pending

    # 并行拉取已确认 + 未确认交易，总超时 12s
    with _TP(max_workers=2) as pool:
        f_confirmed = pool.submit(_fetch_confirmed)
        f_pending   = pool.submit(_fetch_mempool)
        for fut in _ac([f_confirmed, f_pending], timeout=12):
            try:
                for item in fut.result():
                    if item["hash"] not in seen_hashes:
                        seen_hashes.add(item["hash"])
                        whale_list.append(item)
            except Exception:
                pass

    # ── 数据源失败: 如实标注, 绝不生成虚构交易 ────────────────────────
    # (旧版在此伪造 4 笔带假时间戳的"示例"交易混入实时面板, 2026-07 对抗性审查移除:
    #  失败路径必须诚实, 用户无法从"示例..."哈希意识到整版数据是编的。
    #  条件是"一笔都没有"——清淡时段恰有 1 笔真交易时数据源明明是响应的,
    #  不能再挂"未响应"状态行)
    if not whale_list:
        whale_list.append({
            "amount": "⚠️ 数据源暂不可用",
            "value_usd": "mempool.space 未响应, 稍后自动重试",
            "hash": "", "time": "",
            "timestamp": 0,
            "type": "状态", "icon": "⚪",
            "url": "https://mempool.space"
        })

    # 按时间排序（最新在前），移除内部字段
    whale_list.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    for item in whale_list:
        item.pop("timestamp", None)

    # 尾部追加外链
    whale_list.append({
        "amount": "🔗 查看更多大额交易",
        "value_usd": "mempool.space",
        "hash": "", "time": "",
        "type": "链接", "icon": "🔗",
        "url": "https://mempool.space"
    })

    return whale_list



