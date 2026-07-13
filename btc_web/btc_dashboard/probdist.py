"""BTC 到期价格的风险中性概率分布 (Breeden-Litzenberger, numpy-only) + Polymarket 叠加。仅展示。"""
import datetime, math
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
