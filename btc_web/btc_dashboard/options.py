"""Deribit BTC 期权数据: DVOL + 期权链快照派生指标。纯函数: 合约名解析 + 期权链快照派生指标计算。"""
import datetime
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
        "skew_25d": round(put_wing - call_wing, 1) if (put_wing is not None and call_wing is not None) else None,
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
