"""Deribit BTC 期权数据: DVOL + 期权链快照派生指标。纯函数 + 带缓存的抓取入口。"""
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
    cands = [r for r in rows if r["exp"] == exp and r.get("iv")]
    if not cands or not spot:
        return None
    return min(cands, key=lambda r: abs(r["strike"] - spot))["iv"]


def _wing_iv(rows, exp, spot, lo, hi, cp):
    cs = [r for r in rows if r["exp"] == exp and r["cp"] == cp and r.get("iv")
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
    fmt = lambda e: e.strftime("%d%b%y") if e else None
    return {
        "put_call_oi": round(poi / coi, 2) if coi else None,
        "skew_25d": round(put_wing - call_wing, 1) if (put_wing and call_wing) else None,
        "skew_exp": fmt(skew_exp),
        "atm_front": round(atm_front, 1) if atm_front else None,
        "atm_back": round(atm_back, 1) if atm_back else None,
        "term_slope": round(atm_back - atm_front, 1) if (atm_front and atm_back) else None,
        "front_exp": fmt(front), "back_exp": fmt(back),
        "max_pain": round(_max_pain(rows, mp_exp)) if mp_exp else None,
        "max_pain_exp": fmt(mp_exp),
        "n_contracts": len(rows),
    }
