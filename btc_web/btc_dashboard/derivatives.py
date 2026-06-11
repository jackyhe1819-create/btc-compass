#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.derivatives
=========================
衍生品杠杆面板：未平仓合约 OI、资金费率、多空比、清算样本、行情性质判断。

行情性质矩阵（24h 价格变化 × 24h OI 变化）:
- 价↑ OI↑ → 新多入场（趋势确认·偏多）
- 价↑ OI↓ → 空头回补（上涨缺乏新资金，动能存疑）
- 价↓ OI↑ → 新空入场（趋势确认·偏空）
- 价↓ OI↓ → 多头平仓（抛压释放中）

数据源全部免费公开 API：OKX（主）、Binance（OI 备用）。
"""

import requests
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
_OKX = "https://www.okx.com"


def _fetch_oi_history_okx(days: int = 30):
    """OKX rubik OI 历史（USD 计价，日线）。返回 [(ts_ms, oi_usd)] 升序，失败 None。"""
    try:
        r = requests.get(
            f"{_OKX}/api/v5/rubik/stat/contracts/open-interest-volume",
            params={"ccy": "BTC", "period": "1D"},
            headers=_HEADERS, timeout=10,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("code") != "0" or not data.get("data"):
            return None
        rows = [(int(row[0]), float(row[1])) for row in data["data"]]
        rows.sort(key=lambda x: x[0])
        return rows[-days:]
    except Exception as e:
        print(f"⚠️ OKX OI 历史失败: {e}")
        return None


def _fetch_oi_history_binance(days: int = 30):
    """Binance OI 历史备用（USDT 名义价值，日线）。"""
    try:
        r = requests.get(
            "https://fapi.binance.com/futures/data/openInterestHist",
            params={"symbol": "BTCUSDT", "period": "1d", "limit": days},
            headers=_HEADERS, timeout=10,
        )
        if r.status_code != 200:
            return None
        rows = [(int(it["timestamp"]), float(it["sumOpenInterestValue"])) for it in r.json()]
        rows.sort(key=lambda x: x[0])
        return rows or None
    except Exception as e:
        print(f"⚠️ Binance OI 历史失败: {e}")
        return None


def _fetch_daily_closes_okx(days: int = 35):
    """OKX 日线收盘价 {date_str: close}，用于 OI 图叠加价格。"""
    try:
        r = requests.get(
            f"{_OKX}/api/v5/market/candles",
            params={"instId": "BTC-USDT", "bar": "1D", "limit": str(days)},
            headers=_HEADERS, timeout=10,
        )
        if r.status_code != 200:
            return {}
        data = r.json()
        if data.get("code") != "0":
            return {}
        closes = {}
        for row in data["data"]:  # 倒序返回
            d = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc).strftime("%m-%d")
            closes[d] = float(row[4])
        return closes
    except Exception as e:
        print(f"⚠️ OKX 日线失败: {e}")
        return {}


def _fetch_ticker_okx():
    """当前价格 + 24h 涨跌幅"""
    try:
        r = requests.get(
            f"{_OKX}/api/v5/market/ticker",
            params={"instId": "BTC-USDT"},
            headers=_HEADERS, timeout=10,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("code") != "0" or not data.get("data"):
            return None
        t = data["data"][0]
        last = float(t["last"])
        open24 = float(t.get("open24h") or 0)
        change = (last - open24) / open24 * 100 if open24 > 0 else None
        return {"last": last, "change_24h_pct": round(change, 2) if change is not None else None}
    except Exception as e:
        print(f"⚠️ OKX ticker 失败: {e}")
        return None


def _fetch_funding_okx():
    """当前资金费率 + 下期预测"""
    try:
        r = requests.get(
            f"{_OKX}/api/v5/public/funding-rate",
            params={"instId": "BTC-USDT-SWAP"},
            headers=_HEADERS, timeout=10,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("code") != "0" or not data.get("data"):
            return None
        f = data["data"][0]
        rate = float(f["fundingRate"]) * 100
        nxt = f.get("nextFundingRate")
        next_rate = float(nxt) * 100 if nxt not in (None, "",) else None
        next_time = ""
        if f.get("fundingTime"):
            next_time = datetime.fromtimestamp(
                int(f["fundingTime"]) / 1000, tz=timezone(timedelta(hours=8))
            ).strftime("%H:%M")
        return {
            "rate_pct": round(rate, 4),
            "next_rate_pct": round(next_rate, 4) if next_rate is not None else None,
            "next_time": next_time,
            # 8h 费率 × 3 × 365 年化
            "annualized_pct": round(rate * 3 * 365, 1),
        }
    except Exception as e:
        print(f"⚠️ OKX funding 失败: {e}")
        return None


def _fetch_long_short_okx():
    """多空账户比"""
    try:
        r = requests.get(
            f"{_OKX}/api/v5/rubik/stat/contracts/long-short-account-ratio",
            params={"ccy": "BTC", "period": "1H"},
            headers=_HEADERS, timeout=10,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("code") != "0" or not data.get("data"):
            return None
        ratio = float(data["data"][0][1])
        long_pct = ratio / (1 + ratio) * 100
        return {
            "ratio": round(ratio, 2),
            "long_pct": round(long_pct, 1),
            "short_pct": round(100 - long_pct, 1),
        }
    except Exception as e:
        print(f"⚠️ OKX 多空比失败: {e}")
        return None


def _fetch_liquidations_okx():
    """
    OKX 近期清算订单样本（公开端点单页最多 100 条，作为方向参考而非全量统计）。
    BTC-USDT-SWAP 每张合约 = 0.01 BTC。
    """
    try:
        r = requests.get(
            f"{_OKX}/api/v5/public/liquidation-orders",
            params={"instType": "SWAP", "instFamily": "BTC-USDT", "state": "filled", "limit": "100"},
            headers=_HEADERS, timeout=10,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("code") != "0" or not data.get("data"):
            return None

        CT_VAL = 0.01  # BTC-USDT-SWAP 合约面值
        long_usd = short_usd = 0.0
        count = 0
        oldest_ts = None
        for inst in data["data"]:
            for d in inst.get("details", []):
                try:
                    px = float(d.get("bkPx") or 0)
                    sz = float(d.get("sz") or 0)
                    usd = px * sz * CT_VAL
                    if d.get("posSide") == "long":
                        long_usd += usd
                    else:
                        short_usd += usd
                    count += 1
                    ts = int(d.get("ts") or 0)
                    if ts and (oldest_ts is None or ts < oldest_ts):
                        oldest_ts = ts
                except Exception:
                    continue

        if count == 0:
            return None

        window_h = None
        if oldest_ts:
            window_h = round((datetime.now(timezone.utc).timestamp() * 1000 - oldest_ts) / 3600000, 1)

        return {
            "long_usd": round(long_usd, 0),
            "short_usd": round(short_usd, 0),
            "total_usd": round(long_usd + short_usd, 0),
            "count": count,
            "window_h": window_h,
            "note": f"OKX 最近 {count} 笔清算样本" + (f"（约 {window_h}h 内）" if window_h else ""),
        }
    except Exception as e:
        print(f"⚠️ OKX 清算样本失败: {e}")
        return None


def _classify_regime(price_chg, oi_chg):
    """价格Δ × OIΔ → 行情性质"""
    if price_chg is None or oi_chg is None:
        return {"key": "unknown", "label": "数据不足", "desc": "价格或 OI 变化数据缺失", "tone": "neutral"}

    flat_price = abs(price_chg) < 0.5
    flat_oi = abs(oi_chg) < 1.0
    if flat_price and flat_oi:
        return {"key": "chop", "label": "盘整观望", "desc": "价格与持仓均无明显变化，市场缺乏方向", "tone": "neutral"}

    if price_chg >= 0 and oi_chg >= 0:
        return {"key": "new_longs", "label": "新多入场",
                "desc": "价格↑ + OI↑：新资金做多推动上涨，趋势确认偏多", "tone": "bullish"}
    if price_chg >= 0 and oi_chg < 0:
        return {"key": "short_cover", "label": "空头回补",
                "desc": "价格↑ + OI↓：上涨主要由空头平仓驱动，缺乏新资金，动能存疑", "tone": "warning"}
    if price_chg < 0 and oi_chg >= 0:
        return {"key": "new_shorts", "label": "新空入场",
                "desc": "价格↓ + OI↑：新资金做空压制价格，趋势确认偏空", "tone": "bearish"}
    return {"key": "long_exit", "label": "多头平仓",
            "desc": "价格↓ + OI↓：多头止损/获利了结，杠杆出清中，抛压逐步释放", "tone": "warning"}


def fetch_derivatives_panel() -> dict:
    """聚合衍生品面板数据（并发拉取，单源失败不影响整体）"""
    with ThreadPoolExecutor(max_workers=6) as pool:
        f_oi = pool.submit(_fetch_oi_history_okx, 30)
        f_closes = pool.submit(_fetch_daily_closes_okx, 35)
        f_ticker = pool.submit(_fetch_ticker_okx)
        f_funding = pool.submit(_fetch_funding_okx)
        f_ls = pool.submit(_fetch_long_short_okx)
        f_liq = pool.submit(_fetch_liquidations_okx)

        oi_rows = f_oi.result()
        closes = f_closes.result()
        ticker = f_ticker.result()
        funding = f_funding.result()
        long_short = f_ls.result()
        liquidations = f_liq.result()

    oi_source = "OKX"
    if not oi_rows:
        oi_rows = _fetch_oi_history_binance(30)
        oi_source = "Binance"

    # ── OI 汇总 + 历史序列 ──
    oi = None
    if oi_rows and len(oi_rows) >= 2:
        curr_oi = oi_rows[-1][1]
        prev_oi = oi_rows[-2][1]
        oi_chg_24h = (curr_oi - prev_oi) / prev_oi * 100 if prev_oi > 0 else None
        oi_chg_7d = None
        if len(oi_rows) >= 8 and oi_rows[-8][1] > 0:
            oi_chg_7d = (curr_oi - oi_rows[-8][1]) / oi_rows[-8][1] * 100

        history = []
        for ts, val in oi_rows:
            d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%m-%d")
            history.append({
                "date": d,
                "oi_usd": round(val, 0),
                "price": closes.get(d),
            })

        oi = {
            "current_usd": round(curr_oi, 0),
            "current_btc": round(curr_oi / ticker["last"], 0) if ticker and ticker.get("last") else None,
            "change_24h_pct": round(oi_chg_24h, 2) if oi_chg_24h is not None else None,
            "change_7d_pct": round(oi_chg_7d, 2) if oi_chg_7d is not None else None,
            "history": history,
            "source": oi_source,
        }

    regime = _classify_regime(
        ticker.get("change_24h_pct") if ticker else None,
        oi.get("change_24h_pct") if oi else None,
    )

    return {
        "updated_at": datetime.now().strftime("%H:%M"),
        "price": ticker,
        "oi": oi,
        "funding": funding,
        "long_short": long_short,
        "liquidations": liquidations,
        "regime": regime,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(fetch_derivatives_panel(), ensure_ascii=False, indent=2))
