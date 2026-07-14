"""BTC 到期价格的风险中性概率分布 (Breeden-Litzenberger, numpy-only) + Polymarket 叠加。仅展示。"""
import datetime, math, json as _json, time, urllib.request
from typing import List, Optional, Callable
import numpy as np

UTC = datetime.timezone.utc


def _trapz(y: "np.ndarray", x: "np.ndarray") -> float:
    """梯形积分, 版本无关。numpy 2.x 已移除 np.trapz(生产 numpy 2.5 会 AttributeError),
    np.trapezoid 又在 numpy<2.0 不存在(requirements 允许 >=1.24), 故手写覆盖全版本。"""
    return float(np.sum((y[1:] + y[:-1]) / 2.0 * np.diff(x)))


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


def _forward_from_parity(chain: List[dict], spot: float,
                         exp: datetime.datetime) -> float:
    """看涨看跌平价反解远期价: F = K_atm + (C - P)_usd, 用现有链无需额外 API。
    Deribit 期权 mark_price 以 BTC 计价, ×underlying 转 USD。ATM 无双边则退 spot。
    (远期≠现价: 近月常轻贴水, 远月升水; F=spot 会系统性偏移尾概率, 见对抗性复审)。"""
    prices = {}   # (K, cp) -> mark_usd
    for x in chain:
        try:
            e, K, cp = parse_instrument(x["instrument_name"])
        except Exception:
            continue
        if e != exp:
            continue
        mk = x.get("mark_price"); up = x.get("underlying_price")
        if mk is not None and up:
            prices[(K, cp)] = mk * up
    both = sorted({K for (K, cp) in prices if (K, "C") in prices and (K, "P") in prices})
    if not both:
        return float(spot)
    katm = min(both, key=lambda k: abs(k - spot))
    return katm + (prices[(katm, "C")] - prices[(katm, "P")])


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
    Ks = sorted(iv_by_K)
    if (Ks[-1] - Ks[0]) < 0.2 * spot:
        return None   # 行权价跨度 < ±10%: wing 全靠外推, 分布不可信
    F = _forward_from_parity(chain, spot, exp)
    T = (exp - now).total_seconds() / (365.25 * 86400)
    try:
        sigma = fit_smile(Ks, [iv_by_K[k] for k in Ks], F)
        grid = np.linspace(Ks[0], Ks[-1], _GRID_N)
        C = np.array([_black76_call(F, float(K), T, sigma(float(K))) for K in grid])
        dK = grid[1] - grid[0]
        raw = (C[2:] - 2 * C[1:-1] + C[:-2]) / (dK * dK)
        pdf = np.zeros(_GRID_N)
        pdf[1:-1] = np.maximum(raw, 0.0)
        neg_mass = -float(np.sum(raw[raw < 0])) * dK
        area = _trapz(pdf, grid)
        if not (area > 0) or not np.isfinite(area):
            return None
        if neg_mass / (area + neg_mass) > 0.05:
            return None   # butterfly-arbitrage 负质量 >5%: smile 拟合已失真, 宁缺毋滥(clip 摊回=掩耳盗铃)
        pdf = pdf / area
    except Exception:
        return None
    cdf = np.concatenate([[0.0], np.cumsum((pdf[1:] + pdf[:-1]) / 2 * dK)])

    def q(p):
        return float(np.interp(p, cdf, grid))     # 线性插值, 消除一格 dK 的阶梯量化偏差

    def P_gt(x):
        return round(float(1.0 - np.interp(x, grid, cdf)) * 100, 1)

    median, p16, p84 = q(0.5), q(0.16), q(0.84)
    mode = float(grid[int(np.argmax(pdf))])
    mean = _trapz(grid * pdf, grid)
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

_KALSHI_URL = ("https://api.elections.kalshi.com/trade-api/v2/markets"
               "?series_ticker=KXBTCY&status=open&limit=100")
_kalshi_cache = {"data": None, "ts": 0.0}
_KALSHI_TTL = 1800


def _kalshi_get() -> list:
    req = urllib.request.Request(_KALSHI_URL, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = _json.load(r)
    return d.get("markets", []) if isinstance(d, dict) else []


def fetch_kalshi_btc() -> Optional[dict]:
    """Kalshi BTC 年底价格分布(KXBTCY 系列, 行情免鉴权)。尽力而为: 失败/桶太少 → None。
    价格是真实世界众筹概率(P 测度), 与 RND(Q 测度)不可比差; PMF 含 vig(实测≈1.03)按和归一。"""
    now = time.time()
    if _kalshi_cache["data"] is not None and now - _kalshi_cache["ts"] < _KALSHI_TTL:
        return _kalshi_cache["data"]
    try:
        buckets, total_vol, close = [], 0.0, None
        for m in _kalshi_get():
            try:
                p = float(m.get("last_price_dollars") or 0)
                lo = m.get("floor_strike")
                hi = m.get("cap_strike")
                buckets.append({"lo": (round(lo) if lo is not None else None),
                                "hi": (round(hi) if hi is not None else None),
                                "p": p})
                total_vol += float(m.get("volume_fp") or 0)
                close = close or (m.get("close_time") or "")[:10]
            except Exception:
                continue
        s = sum(b["p"] for b in buckets)
        if len(buckets) < 10 or s <= 0.5:
            return None                       # 桶太少/市场休眠: 不足以构成分布
        for b in buckets:
            b["p"] = round(b["p"] / s * 100, 1)
        buckets.sort(key=lambda b: b["lo"] if b["lo"] is not None else -1)
        out = {"close": close, "buckets": buckets,
               "volume": round(total_vol), "vig_pct": round((s - 1) * 100, 1)}
        _kalshi_cache.update(data=out, ts=now)
        return out
    except Exception:
        return None


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


from .options import _fetch_chain

_RND_KEYS = ["expiry", "days", "spot", "forward", "pdf", "median", "mode", "mean",
             "p16", "p84", "expected_move_pct", "p_up", "tails"]


def _now() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def _assemble_probdist() -> dict:
    now = _now()
    partial = False
    rnd = None
    try:
        # options._fetch_chain 自带 120s 链缓存: 与期权面板共享一次链请求, 两卡 spot 天然一致
        chain, spot = _fetch_chain()
        rnd = risk_neutral_density(chain, spot, now)
    except Exception:
        rnd = None
    if rnd is None:
        partial = True
        rnd = {k: None for k in _RND_KEYS}
    poly = fetch_polymarket_btc()
    return {**rnd, "polymarket": poly, "kalshi": fetch_kalshi_btc(),
            "updated_at": now.strftime("%H:%M"), "partial": partial}


def fetch_probdist_panel() -> dict:
    """装配概率分布面板。面板级缓存归 app 层 PanelCache(同 options)。"""
    return _assemble_probdist()
