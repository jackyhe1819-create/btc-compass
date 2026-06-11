#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.etf_flow
======================
BTC 现货 ETF 日度净流入抓取（免费数据源，多层 fallback）。

1. SoSoValue openapi — JSON（主源，可用 SOSOVALUE_API_KEY 环境变量配置正式 key）
2. Farside Investors (farside.co.uk/btc) — HTML 表格（Cloudflare 可能拦截）
3. CoinGlass 非官方 web API — JSON
失败时返回 None，前端回退为纯链接卡片。
"""

import os
import re
import requests
from datetime import datetime

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _fetch_sosovalue(limit: int = 15):
    """
    SoSoValue openapi：BTC 现货 ETF 日度净流入历史。
    返回 [{date, total(百万美元), funds:{}, cum(累计·百万)}] 升序，失败 None。
    """
    try:
        r = requests.post(
            "https://api.sosovalue.xyz/openapi/v2/etf/historicalInflowChart",
            json={"type": "us-btc-spot"},
            headers={
                "Content-Type": "application/json",
                "x-soso-api-key": os.environ.get("SOSOVALUE_API_KEY", "public"),
                "User-Agent": _HEADERS["User-Agent"],
            },
            timeout=15,
        )
        if r.status_code != 200:
            print(f"⚠️ SoSoValue HTTP {r.status_code}")
            return None
        payload = r.json()
        if payload.get("code") != 0 or not payload.get("data"):
            print(f"⚠️ SoSoValue 返回异常: {str(payload)[:100]}")
            return None

        rows = []
        for it in payload["data"]:
            d_str = it.get("date")
            flow = it.get("totalNetInflow")
            if not d_str or flow is None:
                continue
            try:
                d = datetime.strptime(d_str, "%Y-%m-%d")
            except ValueError:
                continue
            cum = it.get("cumNetInflow")
            rows.append({
                "date": d.strftime("%m-%d"), "_sort": d,
                "total": round(float(flow) / 1e6, 1),   # USD → 百万美元
                "funds": {},
                "cum": round(float(cum) / 1e6, 0) if cum is not None else None,
            })
        if not rows:
            return None
        rows.sort(key=lambda x: x["_sort"])
        for r_ in rows:
            r_.pop("_sort", None)
        return rows[-limit:]
    except Exception as e:
        print(f"⚠️ SoSoValue ETF 流向失败: {e}")
        return None


def _parse_farside_value(text: str):
    """Farside 表格数值: '123.4' / '(12.3)'=负 / '-'=0 / ''=None"""
    t = (text or "").strip().replace(",", "")
    if t in ("", "\xa0"):
        return None
    if t == "-":
        return 0.0
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = float(t)
        return -v if neg else v
    except ValueError:
        return None


def _fetch_farside(limit: int = 15):
    """
    解析 Farside BTC ETF 日度流向表。
    返回 [{date, total, funds:{IBIT:..,FBTC:..,GBTC:..}}] 升序，失败 None。
    """
    try:
        r = requests.get("https://farside.co.uk/btc/", headers=_HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"⚠️ Farside HTTP {r.status_code}")
            return None
        html = r.text

        # 用 pandas 解析所有表格，找包含 Total 列与日期行的那张
        import pandas as pd
        from io import StringIO
        tables = pd.read_html(StringIO(html))
        target = None
        for t in tables:
            cols = [str(c) for c in (t.columns.get_level_values(-1) if hasattr(t.columns, "levels") else t.columns)]
            if any("Total" in c for c in cols) and len(t) > 5:
                target = t
                target.columns = cols
                break
        if target is None:
            return None

        date_col = target.columns[0]
        total_col = next(c for c in target.columns if "Total" in c)
        fund_cols = [c for c in target.columns
                     if c not in (date_col, total_col) and re.fullmatch(r"[A-Z]{3,5}", str(c))]

        rows = []
        for _, row in target.iterrows():
            raw_date = str(row[date_col]).strip()
            # 行格式如 "10 Jun 2026"；汇总行（Total/Average 等）跳过
            try:
                d = datetime.strptime(raw_date, "%d %b %Y")
            except ValueError:
                continue
            total = _parse_farside_value(str(row[total_col]))
            if total is None:
                # Total 缺失时按基金列求和
                vals = [_parse_farside_value(str(row[c])) for c in fund_cols]
                vals = [v for v in vals if v is not None]
                total = round(sum(vals), 1) if vals else None
            if total is None:
                continue
            funds = {}
            for c in fund_cols:
                v = _parse_farside_value(str(row[c]))
                if v is not None and v != 0:
                    funds[c] = v
            rows.append({"date": d.strftime("%m-%d"), "_sort": d, "total": total, "funds": funds})

        if not rows:
            return None
        rows.sort(key=lambda x: x["_sort"])
        for r_ in rows:
            r_.pop("_sort", None)
        return rows[-limit:]
    except Exception as e:
        print(f"⚠️ Farside ETF 流向解析失败: {e}")
        return None


def _fetch_coinglass(limit: int = 15):
    """CoinGlass 非官方 web 端点备用（结构可能变化，防御式解析）"""
    candidates = [
        "https://fapi.coinglass.com/api/bitcoin/etf/flow",
        "https://fapi.coinglass.com/api/etf/bitcoin/flow-history",
    ]
    for url in candidates:
        try:
            r = requests.get(url, headers={**_HEADERS, "Accept": "application/json"}, timeout=12)
            if r.status_code != 200:
                continue
            payload = r.json()
            data = payload.get("data") or payload.get("dataList") or []
            if isinstance(data, dict):
                data = data.get("list") or data.get("flowList") or []
            rows = []
            for it in data:
                if not isinstance(it, dict):
                    continue
                ts = it.get("date") or it.get("timestamp") or it.get("time")
                flow = it.get("changeUsd") or it.get("totalFlow") or it.get("flowUsd") or it.get("netFlow")
                if ts is None or flow is None:
                    continue
                try:
                    if isinstance(ts, (int, float)) or str(ts).isdigit():
                        ts_n = float(ts)
                        if ts_n > 1e12:
                            ts_n /= 1000
                        d = datetime.fromtimestamp(ts_n)
                    else:
                        d = datetime.strptime(str(ts)[:10], "%Y-%m-%d")
                    # CoinGlass 流向单位通常为 USD，转为百万美元对齐 Farside
                    flow_m = float(flow) / 1e6 if abs(float(flow)) > 1e5 else float(flow)
                    rows.append({"date": d.strftime("%m-%d"), "_sort": d,
                                 "total": round(flow_m, 1), "funds": {}})
                except Exception:
                    continue
            if rows:
                rows.sort(key=lambda x: x["_sort"])
                for r_ in rows:
                    r_.pop("_sort", None)
                return rows[-limit:]
        except Exception as e:
            print(f"⚠️ CoinGlass ETF 流向失败 ({url}): {e}")
    return None


def fetch_etf_flow_history(limit: int = 15):
    """
    获取 BTC 现货 ETF 日度净流入（单位：百万美元）。
    返回:
    {
      "series": [{date, total, funds}],   # 升序
      "latest": {date, total},
      "sum_5d": ..., "sum_total": ...,    # 近5日 / 窗口内累计（百万美元）
      "source": "Farside" | "CoinGlass",
      "updated_at": "HH:MM"
    }
    失败返回 None。
    """
    rows = _fetch_sosovalue(limit)
    source = "SoSoValue"
    if not rows:
        rows = _fetch_farside(limit)
        source = "Farside"
    if not rows:
        rows = _fetch_coinglass(limit)
        source = "CoinGlass"
    if not rows:
        return None

    totals = [r["total"] for r in rows if r.get("total") is not None]
    result = {
        "series": rows,
        "latest": {"date": rows[-1]["date"], "total": rows[-1]["total"]},
        "sum_5d": round(sum(totals[-5:]), 1),
        "sum_total": round(sum(totals), 1),
        "source": source,
        "updated_at": datetime.now().strftime("%H:%M"),
    }
    # SoSoValue 附带历史累计净流入（自 ETF 上市起，百万美元）
    if rows[-1].get("cum") is not None:
        result["cum_total"] = rows[-1]["cum"]
    return result


if __name__ == "__main__":
    import json
    print(json.dumps(fetch_etf_flow_history(), ensure_ascii=False, indent=2))
