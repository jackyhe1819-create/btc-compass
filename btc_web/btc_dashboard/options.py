"""Deribit BTC 期权数据: DVOL + 期权链快照派生指标。纯函数: 合约名解析 + 期权链快照派生指标计算。"""
import datetime
import json
import time
import urllib.parse
import urllib.request
from typing import List, Dict, Tuple

UTC = datetime.timezone.utc


def parse_instrument(name: str) -> Tuple[datetime.datetime, float, str]:
    p = name.split("-")            # BTC-28AUG26-60000-C
    exp = datetime.datetime.strptime(p[1], "%d%b%y").replace(tzinfo=UTC)
    return exp, float(p[2]), p[3]


def _nearest_exp(exps: List[datetime.datetime], now: datetime.datetime, min_days: int):
    fut = [e for e in exps if e > now]
    for e in fut:
        if (e - now).days >= min_days:
            return e
    return fut[-1] if fut else None


def _atm_iv(rows: List[dict], exp, spot: float):
    cands = [r for r in rows if r["exp"] == exp and r.get("iv") is not None]
    if not cands or not spot:
        return None
    # tie-break 在 ATM 档位上偏好 call: abs(strike-spot) 相同时 cp=='C' 排前
    return min(cands, key=lambda r: (abs(r["strike"] - spot), r["cp"] != "C"))["iv"]


def _wing_iv(rows, exp, spot, lo, hi, cp):
    cs = [r for r in rows if r["exp"] == exp and r["cp"] == cp and r.get("iv") is not None
          and lo * spot <= r["strike"] <= hi * spot]
    return sum(r["iv"] for r in cs) / len(cs) if cs else None


def _max_pain(rows, exp):
    grp = [r for r in rows if r["exp"] == exp and r["oi"] > 0]
    strikes = sorted({r["strike"] for r in grp})
    best, best_pay = None, None
    for K in strikes:
        pay = 0.0
        for r in grp:
            if r["cp"] == "C" and r["strike"] < K:
                pay += (K - r["strike"]) * r["oi"]
            elif r["cp"] == "P" and r["strike"] > K:
                pay += (r["strike"] - K) * r["oi"]
        if best_pay is None or pay < best_pay:
            best_pay, best = pay, K
    return best


def derive_snapshot(chain: List[dict], spot: float, now: datetime.datetime) -> Dict:
    rows = []
    for x in chain:
        try:
            exp, strike, cp = parse_instrument(x["instrument_name"])
            rows.append({"exp": exp, "strike": strike, "cp": cp,
                         "iv": x.get("mark_iv"), "oi": x.get("open_interest") or 0})
        except Exception:
            pass
    poi = sum(r["oi"] for r in rows if r["cp"] == "P")
    coi = sum(r["oi"] for r in rows if r["cp"] == "C")
    exps = sorted({r["exp"] for r in rows if r["exp"] > now})
    front = _nearest_exp(exps, now, 5)
    back = _nearest_exp(exps, now, 80)
    skew_exp = _nearest_exp(exps, now, 20) or front
    mp_exp = skew_exp
    atm_front = _atm_iv(rows, front, spot)
    atm_back = _atm_iv(rows, back, spot)
    put_wing = _wing_iv(rows, skew_exp, spot, 0.85, 0.95, "P")
    call_wing = _wing_iv(rows, skew_exp, spot, 1.05, 1.15, "C")
    mp = _max_pain(rows, mp_exp) if mp_exp else None
    fmt = lambda e: e.strftime("%d%b%y") if e else None
    return {
        "put_call_oi": round(poi / coi, 2) if coi else None,
        "skew_wing": round(put_wing - call_wing, 1) if (put_wing is not None and call_wing is not None) else None,
        "skew_exp": fmt(skew_exp),
        "atm_front": round(atm_front, 1) if atm_front is not None else None,
        "atm_back": round(atm_back, 1) if atm_back is not None else None,
        "term_slope": round(atm_back - atm_front, 1) if (atm_front is not None and atm_back is not None) else None,
        "front_exp": fmt(front), "back_exp": fmt(back),
        "max_pain": round(mp) if mp is not None else None,
        "max_pain_exp": fmt(mp_exp),
        "n_contracts": len(rows),
    }


def calc_dvol_percentile(closes: List[float], current: float,
                         window: int = 1460) -> Tuple[float, int]:
    """4年滚动分位数：返回 (分位 0-100, 用到的样本数)。"""
    w = closes[-window:] if len(closes) >= window else list(closes)
    if not w:
        return 0.0, 0
    below = sum(1 for x in w if x < current)
    return round(below / len(w) * 100, 1), len(w)


_BASE = "https://www.deribit.com/api/v2/public/"
_panel_cache = {"data": None, "ts": 0.0}
_PANEL_TTL = 600


def _now() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def _get(method: str, **params):
    url = _BASE + method + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)["result"]


def fetch_dvol_history(start_ms: int, end_ms: int) -> List[Tuple[int, float]]:
    """分页拉取 DVOL 日线，按时间升序去重返回 (ts_ms, close)。"""
    pts, end = {}, end_ms
    for _ in range(8):
        res = _get("get_volatility_index_data", currency="BTC",
                   start_timestamp=start_ms, end_timestamp=end, resolution="1D")
        data = res.get("data", [])
        if not data:
            break
        for row in data:
            pts[row[0]] = row[4]           # ts -> close
        earliest = min(r[0] for r in data)
        if earliest <= start_ms + 86400000 or len(data) < 1000:
            break
        end = earliest - 86400000
    return sorted(pts.items())


def _fetch_chain():
    chain = _get("get_book_summary_by_currency", currency="BTC", kind="option")
    # underlying_price 是各自到期日的合成远期价(非指数现价), 且 API 不保证返回顺序 —
    # 取到期最近合约的值: 最近月合成远期 ≈ 现价(误差 <0.5%), 远月升水可达数个百分点
    spot, best_exp = None, None
    for x in chain:
        up = x.get("underlying_price")
        if not up:
            continue
        try:
            exp, _, _ = parse_instrument(x["instrument_name"])
        except Exception:
            continue
        if best_exp is None or exp < best_exp:
            best_exp, spot = exp, up
    return chain, spot


def _assemble_panel() -> Dict:
    now = _now()
    partial = False
    start = 1609459200000  # 2021-01-01
    end = int(now.timestamp() * 1000)
    try:
        hist = fetch_dvol_history(start, end)
    except Exception:
        hist = []
    if not hist:
        partial = True   # 合法空响应与抓取失败同等对待: DVOL 侧缺失即 partial
    closes = [v for _, v in hist]
    dvol_now = closes[-1] if closes else None
    dvol_pct, n = (calc_dvol_percentile(closes, dvol_now) if dvol_now else (None, 0))
    spark = [round(v, 1) for _, v in hist[-90:]]
    try:
        chain, spot = _fetch_chain()
        snap = derive_snapshot(chain, spot, now)
    except Exception:
        # 空链 + 无 spot -> derive_snapshot 提前在 helper 里对 None 短路，
        # 不会走到任何 spot 运算，因此始终安全；同时自动保持键集与其真实输出同步。
        snap, spot, partial = derive_snapshot([], None, now), None, True
    out = {"spot": round(spot) if spot else None,
           "dvol_now": round(dvol_now, 1) if dvol_now else None,
           "dvol_pct": dvol_pct, "dvol_window_days": n, "spark": spark,
           "updated_at": now.strftime("%H:%M"), "partial": partial}
    out.update(snap)
    return out


def fetch_options_panel() -> Dict:
    now = time.time()
    if _panel_cache["data"] and now - _panel_cache["ts"] < _PANEL_TTL:
        return _panel_cache["data"]
    data = _assemble_panel()
    if not data.get("partial"):
        _panel_cache.update(data=data, ts=now)
    return data
