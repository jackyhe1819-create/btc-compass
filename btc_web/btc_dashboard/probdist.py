"""BTC 到期价格的风险中性概率分布 (Breeden-Litzenberger, numpy-only) + Polymarket 叠加。仅展示。"""
import datetime, math, json as _json, time, urllib.request
from typing import List, Optional, Callable
import numpy as np

UTC = datetime.timezone.utc


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _black76_call(F: float, K: float, T: float, sig: float) -> float:
    if sig <= 0 or T <= 0 or F <= 0 or K <= 0:
        return max(F - K, 0.0)
    srt = sig * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sig * sig * T) / srt
    d2 = d1 - srt
    return F * _ncdf(d1) - K * _ncdf(d2)          # r≈0


def nearest_monthly(exps: List[datetime.datetime], now: datetime.datetime,
                    min_days: int = 14) -> Optional[datetime.datetime]:
    fut = sorted(e for e in exps if (e - now).days >= min_days)
    if fut:
        return fut[0]
    allfut = sorted(e for e in exps if e > now)
    return allfut[0] if allfut else None


def fit_smile(strikes: List[float], ivs: List[float], F: float) -> Callable[[float], float]:
    x = np.log(np.asarray(strikes, float) / F)
    y = np.asarray(ivs, float)
    deg = 4 if len(strikes) >= 8 else 2
    coefs = np.polyfit(x, y, deg)
    xlo, xhi = float(x.min()), float(x.max())

    def sigma(K: float) -> float:
        xk = math.log(K / F)
        xk = min(max(xk, xlo), xhi)               # wing 平推, 防多项式发散
        return max(0.01, float(np.polyval(coefs, xk)))
    return sigma


from .options import parse_instrument

_GRID_N = 400


def risk_neutral_density(chain: List[dict], spot: float,
                         now: datetime.datetime) -> Optional[dict]:
    if not spot or spot <= 0:
        return None
    rows = []
    for x in chain:
        try:
            exp, K, cp = parse_instrument(x["instrument_name"])
            if x.get("mark_iv"):
                rows.append((exp, K, x["mark_iv"] / 100.0))
        except Exception:
            pass
    if not rows:
        return None
    exp = nearest_monthly([e for e, _, _ in rows], now)
    if exp is None:
        return None
    iv_by_K = {}
    for e, K, iv in rows:
        if e == exp:
            iv_by_K.setdefault(K, iv)
    if len(iv_by_K) < 5:
        return None
    F = float(spot)
    T = (exp - now).total_seconds() / (365.25 * 86400)
    Ks = sorted(iv_by_K)
    try:
        sigma = fit_smile(Ks, [iv_by_K[k] for k in Ks], F)
        grid = np.linspace(Ks[0], Ks[-1], _GRID_N)
        C = np.array([_black76_call(F, float(K), T, sigma(float(K))) for K in grid])
        dK = grid[1] - grid[0]
        pdf = np.zeros(_GRID_N)
        pdf[1:-1] = np.maximum((C[2:] - 2 * C[1:-1] + C[:-2]) / (dK * dK), 0.0)
        area = float(np.trapz(pdf, grid))
        if not (area > 0) or not np.isfinite(area):
            return None
        pdf = pdf / area
    except Exception:
        return None
    cdf = np.concatenate([[0.0], np.cumsum((pdf[1:] + pdf[:-1]) / 2 * dK)])

    def q(p):
        i = int(np.searchsorted(cdf, p))
        return float(grid[min(i, _GRID_N - 1)])

    def P_gt(x):
        i = int(np.searchsorted(grid, x))
        return round(float(1 - cdf[min(i, _GRID_N - 1)]) * 100, 1)

    median, p16, p84 = q(0.5), q(0.16), q(0.84)
    mode = float(grid[int(np.argmax(pdf))])
    mean = float(np.trapz(grid * pdf, grid))
    base = 5000
    lo = int(math.floor(spot * 0.85 / base) * base)
    hi = int(math.ceil(spot * 1.15 / base) * base)
    tails = [{"K": k, "P_gt": P_gt(k)} for k in range(lo, hi + 1, base)]
    step = max(1, _GRID_N // 70)
    pdf_pts = [[round(float(grid[i])), round(float(pdf[i]), 8)]
               for i in range(0, _GRID_N, step)]
    return {
        "expiry": exp.strftime("%d%b%y"), "days": (exp - now).days,
        "spot": round(spot), "forward": round(F),
        "pdf": pdf_pts, "median": round(median), "mode": round(mode), "mean": round(mean),
        "p16": round(p16), "p84": round(p84),
        "expected_move_pct": round((p84 - p16) / 2 / F * 100, 1),
        "p_up": P_gt(spot), "tails": tails,
    }


_POLY_URL = "https://gamma-api.polymarket.com/markets?closed=false&active=true&limit=200"
_UA = "Mozilla/5.0 (compatible; btc-compass/1.0)"
_poly_cache = {"data": None, "ts": 0.0}
_POLY_TTL = 1800


def _poly_get() -> list:
    req = urllib.request.Request(_POLY_URL, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = _json.load(r)
    return d if isinstance(d, list) else d.get("data", [])


def fetch_polymarket_btc() -> List[dict]:
    now = time.time()
    if _poly_cache["data"] is not None and now - _poly_cache["ts"] < _POLY_TTL:
        return _poly_cache["data"]
    out = []
    try:
        for m in _poly_get():
            q = m.get("question", "") or ""
            ql = q.lower()
            if "bitcoin" not in ql and "btc" not in ql:
                continue
            if "ethereum" in ql:
                continue
            op = m.get("outcomePrices")
            if isinstance(op, str):
                try: op = _json.loads(op)
                except Exception: op = None
            yes = round(float(op[0]) * 100, 1) if op else None
            out.append({"q": q[:70], "yes": yes, "end": (m.get("endDate") or "")[:10]})
        out = out[:6]
        _poly_cache.update(data=out, ts=now)
    except Exception:
        out = []
    return out
