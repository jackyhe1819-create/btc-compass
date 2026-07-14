import datetime, math
import pytest
from btc_dashboard.probdist import _ncdf, _black76_call, nearest_monthly, fit_smile, risk_neutral_density
from btc_dashboard import probdist as pd_

UTC = datetime.timezone.utc

@pytest.fixture(autouse=True)
def _clear_poly_cache():
    """Clear Polymarket cache before each test."""
    pd_._poly_cache["data"] = None
    pd_._poly_cache["ts"] = 0.0
    yield
    pd_._poly_cache["data"] = None
    pd_._poly_cache["ts"] = 0.0

def test_ncdf_known():
    assert abs(_ncdf(0) - 0.5) < 1e-9
    assert abs(_ncdf(1.96) - 0.975) < 1e-3

def test_black76_atm_positive_and_monotone():
    # ATM 看涨价 > 深实值贴水外, 且 K 越高价越低
    c_atm = _black76_call(60000, 60000, 0.1, 0.6)
    c_otm = _black76_call(60000, 70000, 0.1, 0.6)
    assert c_atm > 0 and c_atm > c_otm

def test_nearest_monthly_picks_ge_min_days():
    now = datetime.datetime(2026, 7, 13, tzinfo=UTC)
    exps = [datetime.datetime(2026, 7, 18, tzinfo=UTC),   # 5d
            datetime.datetime(2026, 7, 31, tzinfo=UTC),   # 18d
            datetime.datetime(2026, 8, 28, tzinfo=UTC)]   # 46d
    assert nearest_monthly(exps, now, 14) == exps[1]      # 31Jul (最近的 ≥14d)

def test_fit_smile_flat_returns_flat():
    strikes = [50000, 55000, 60000, 65000, 70000, 75000, 80000, 85000]
    ivs = [0.6]*8
    sig = fit_smile(strikes, ivs, 60000)
    assert abs(sig(60000) - 0.6) < 0.02
    assert abs(sig(70000) - 0.6) < 0.05   # 平坦 smile 各处≈0.6


def _synth_chain(F=60000, ivs=None, exp="31JUL26"):
    strikes = list(range(40000, 90001, 5000))          # 11 个行权价
    ivs = ivs or {k: 0.60 for k in strikes}            # 默认平坦
    return [{"instrument_name": f"BTC-{exp}-{k}-C",
             "mark_iv": ivs[k]*100, "underlying_price": F} for k in strikes]

def test_rnd_flat_smile_integrates_and_shapes():
    now = datetime.datetime(2026, 7, 13, tzinfo=UTC)
    r = risk_neutral_density(_synth_chain(), 60000, now)
    assert r is not None
    # pdf 积分≈1
    pdf = r["pdf"]; area = sum((pdf[i][1]+pdf[i+1][1])/2*(pdf[i+1][0]-pdf[i][0]) for i in range(len(pdf)-1))
    assert abs(area - 1.0) < 0.03
    assert all(p[1] >= 0 for p in pdf)
    # tails 单调递减
    pg = [t["P_gt"] for t in r["tails"]]
    assert all(pg[i] >= pg[i+1] for i in range(len(pg)-1))
    # 中位在 forward 附近
    assert 0.9*60000 <= r["median"] <= 1.1*60000

def test_rnd_put_skew_lifts_median_vs_flat():
    now = datetime.datetime(2026, 7, 13, tzinfo=UTC)
    strikes = list(range(40000, 90001, 5000))
    flat = risk_neutral_density(_synth_chain(), 60000, now)                 # 平坦 0.60
    skew_ivs = {k: 0.60 - 0.5 * math.log(k / 60000) for k in strikes}       # 平滑单调 put-skew (低行权价 IV 更高)
    skew = risk_neutral_density(_synth_chain(ivs=skew_ivs), 60000, now)
    assert flat is not None and skew is not None
    # put skew 使左尾变厚; 风险中性均值恒=F, 故中位相对 flat 右移。若 RND 忽略 smile, 两者中位相等 → 本测试红。
    assert skew["median"] > flat["median"]

def test_rnd_too_few_strikes_returns_none():
    now = datetime.datetime(2026, 7, 13, tzinfo=UTC)
    thin = [{"instrument_name": f"BTC-31JUL26-{k}-C", "mark_iv": 60, "underlying_price": 60000}
            for k in (58000, 60000, 62000)]           # 仅 3 个
    assert risk_neutral_density(thin, 60000, now) is None

def test_polymarket_parse(monkeypatch):
    monkeypatch.setattr(pd_, "_poly_get", lambda: [
        {"question": "Will bitcoin hit $200k in 2026?", "outcomePrices": ["0.18","0.82"], "endDate": "2026-12-31T00:00:00Z"},
        {"question": "Will Ethereum flip Bitcoin?", "outcomePrices": ["0.03","0.97"], "endDate": "2026-12-31"},
    ])
    out = pd_.fetch_polymarket_btc()
    assert len(out) == 1 and out[0]["yes"] == 18.0 and out[0]["end"] == "2026-12-31"

def test_polymarket_failure_returns_empty(monkeypatch):
    def boom(): raise RuntimeError("403")
    monkeypatch.setattr(pd_, "_poly_get", boom)
    assert pd_.fetch_polymarket_btc() == []

def test_polymarket_eth_filter_not_over_broad(monkeypatch):
    monkeypatch.setattr(pd_, "_poly_get", lambda: [
        {"question": "Will Bitcoin hit $200k whether or not the ETF passes?", "outcomePrices": ["0.12","0.88"], "endDate": "2026-12-31"},
        {"question": "Will Ethereum flip Bitcoin?", "outcomePrices": ["0.03","0.97"], "endDate": "2026-12-31"},
    ])
    qs = [m["q"] for m in pd_.fetch_polymarket_btc()]
    assert any("whether" in q for q in qs)        # 含 whether(内含 eth 子串)的真 BTC 市场不被误删
    assert not any("Ethereum" in q for q in qs)   # ETH 市场仍排除

def test_assemble_panel(monkeypatch):
    now = datetime.datetime(2026, 7, 13, tzinfo=UTC)
    monkeypatch.setattr(pd_, "_now", lambda: now)
    monkeypatch.setattr(pd_, "_fetch_chain", lambda: (_synth_chain(), 60000.0))
    monkeypatch.setattr(pd_, "fetch_polymarket_btc", lambda: [{"q":"x","yes":18.0,"end":"2026-12-31"}])
    monkeypatch.setattr(pd_, "fetch_kalshi_btc", lambda: None)   # 隔离真网络
    p = pd_._assemble_probdist()
    assert p["partial"] is False and p["median"] is not None and len(p["polymarket"]) == 1

def test_assemble_panel_rnd_fail_partial(monkeypatch):
    now = datetime.datetime(2026, 7, 13, tzinfo=UTC)
    monkeypatch.setattr(pd_, "_now", lambda: now)
    def boom(): raise RuntimeError("deribit down")
    monkeypatch.setattr(pd_, "_fetch_chain", boom)
    monkeypatch.setattr(pd_, "fetch_polymarket_btc", lambda: [])
    monkeypatch.setattr(pd_, "fetch_kalshi_btc", lambda: None)   # 隔离真网络
    p = pd_._assemble_probdist()
    assert p["partial"] is True and "median" in p and p["median"] is None


def test_forward_from_parity():
    exp = datetime.datetime(2026, 7, 31, 8, tzinfo=UTC)   # parse_instrument 的 08:00 UTC 到期口径
    # ATM K=60000: C mark 0.05 BTC, P mark 0.03 BTC, underlying 60000
    #  C_usd=3000, P_usd=1800 → F = 60000 + (3000-1800) = 61200
    chain = [
        {"instrument_name": "BTC-31JUL26-60000-C", "mark_price": 0.05, "underlying_price": 60000},
        {"instrument_name": "BTC-31JUL26-60000-P", "mark_price": 0.03, "underlying_price": 60000},
        {"instrument_name": "BTC-31JUL26-55000-C", "mark_price": 0.09, "underlying_price": 60000},  # 只有C, 无P → 不选
    ]
    assert pd_._forward_from_parity(chain, 60000, exp) == 61200


def test_forward_from_parity_fallback_spot():
    exp = datetime.datetime(2026, 7, 31, 8, tzinfo=UTC)   # 08:00 口径: 保证到期匹配, 回退原因确为"无双边"
    # 无 ATM 双边(只有 C) → 退回 spot
    chain = [{"instrument_name": "BTC-31JUL26-60000-C", "mark_price": 0.05, "underlying_price": 60000}]
    assert pd_._forward_from_parity(chain, 63000, exp) == 63000.0


def _pgt_at(rnd_result, price):
    """从返回的 pdf 点独立重建 CDF 在 price 处的 P_gt — 用于往返自洽断言。"""
    import numpy as _np
    xs = _np.array([p[0] for p in rnd_result["pdf"]], float)
    ys = _np.array([p[1] for p in rnd_result["pdf"]], float)
    cdf = _np.concatenate([[0.0], _np.cumsum((ys[1:] + ys[:-1]) / 2 * _np.diff(xs))])
    cdf = cdf / cdf[-1]
    return float((1 - _np.interp(price, xs, cdf)) * 100)


def test_quantile_interpolation_not_staircase():
    # 自洽性: median = q(0.5), 故独立重建的 P(S > median) 应≈50。
    # 阶梯查表把 median 向上量化最多一格 dK → 偏离可达 ~1pp; 插值后 ≤0.5pp。
    now = datetime.datetime(2026, 7, 13, tzinfo=UTC)
    r = risk_neutral_density(_synth_chain(), 60000, now)
    assert r is not None
    assert abs(_pgt_at(r, r["median"]) - 50.0) <= 0.5


def test_rnd_none_when_strike_span_too_narrow():
    # 行权价挤在 ±3% 内: wing 全靠多项式外推, 分布不可信 → None(面板 partial)
    now = datetime.datetime(2026, 7, 13, tzinfo=UTC)
    strikes = [58000, 59000, 60000, 61000, 62000]          # 跨度 4k < 0.2×60000
    chain = [{"instrument_name": f"BTC-31JUL26-{k}-C",
              "mark_iv": 60.0, "underlying_price": 60000} for k in strikes]
    assert risk_neutral_density(chain, 60000, now) is None


def test_rnd_none_when_negative_mass_excessive():
    # 剧烈锯齿 smile → 多项式拟合振荡 → C(K) 非凸 → BL 二阶导大片为负;
    # clip 后无声摊回是掩耳盗铃, >5% 负质量应降级 None
    now = datetime.datetime(2026, 7, 13, tzinfo=UTC)
    strikes = list(range(40000, 90001, 5000))
    ivs = {k: (2.5 if i % 2 == 0 else 0.3) for i, k in enumerate(strikes)}   # 极端锯齿
    chain = [{"instrument_name": f"BTC-31JUL26-{k}-C",
              "mark_iv": ivs[k] * 100, "underlying_price": 60000} for k in strikes]
    assert risk_neutral_density(chain, 60000, now) is None


def test_rnd_flat_smile_matches_lognormal_analytics():
    # 平坦 smile ⇒ RND 应为 lognormal(F, σ√T): median=F·e^{-σ²T/2}, mean≈F, p_up=Φ(-σ√T/2)。
    # σ 错 √2 倍、σ²T/2 漂移符号写反、分位查表整体偏移都会在此现形(旧 ±10% 宽带断言全放行)。
    now = datetime.datetime(2026, 7, 13, tzinfo=UTC)
    F, sig = 60000.0, 0.60
    r = risk_neutral_density(_synth_chain(F=int(F)), int(F), now)
    assert r is not None
    exp = datetime.datetime(2026, 7, 31, 8, tzinfo=UTC)          # 31JUL26 08:00 UTC 到期口径
    T = (exp - now).total_seconds() / (365.25 * 86400)
    dK = (90000 - 40000) / 399                                   # 合成链网格步长 ≈ $125
    median_theory = F * math.exp(-sig * sig * T / 2)
    mean_theory = F                                              # 鞅性(截断 ±3σ 外, 误差可忽略)
    p_up_theory = _ncdf(-sig * math.sqrt(T) / 2) * 100           # P(S>F), 合成链 parity 退回 F=spot
    assert abs(r["median"] - median_theory) <= 2 * dK
    assert abs(r["mean"] - mean_theory) <= 2 * dK
    assert abs(r["p_up"] - p_up_theory) <= 1.0                   # 1pp


def _kalshi_sample():
    # 三种桶型各一(live 实测字段名), 价格和 0.60(避开 s<=0.5 休眠门槛边界)
    return [
        {"strike_type": "less", "cap_strike": 20000, "last_price_dollars": "0.02",
         "volume_fp": "1000.5", "close_time": "2027-01-01T05:00:00Z"},
        {"strike_type": "between", "floor_strike": 60000, "cap_strike": 64999.99,
         "last_price_dollars": "0.20", "volume_fp": "2000", "close_time": "2027-01-01T05:00:00Z"},
        {"strike_type": "greater", "floor_strike": 149999.99, "last_price_dollars": "0.38",
         "volume_fp": "500", "close_time": "2027-01-01T05:00:00Z"},
    ] + [
        {"strike_type": "between", "floor_strike": 20000 + i * 5000, "cap_strike": 24999.99 + i * 5000,
         "last_price_dollars": "0.0", "volume_fp": "0", "close_time": "2027-01-01T05:00:00Z"}
        for i in range(8)   # 凑满 ≥10 桶的门槛
    ]


def test_kalshi_parse_normalize_sort(monkeypatch):
    pd_._kalshi_cache["data"] = None; pd_._kalshi_cache["ts"] = 0.0
    monkeypatch.setattr(pd_, "_kalshi_get", _kalshi_sample)
    k = pd_.fetch_kalshi_btc()
    assert k is not None
    assert k["close"] == "2027-01-01"
    assert abs(sum(b["p"] for b in k["buckets"]) - 100.0) < 0.5     # 归一化(vig 摊平)
    assert k["buckets"][0]["lo"] is None                            # less 桶排最前
    assert k["buckets"][-1]["hi"] is None                           # greater 桶排最后
    assert k["buckets"][-1]["lo"] == 150000                         # 149999.99 → round
    assert k["volume"] in (3500, 3501)                              # round(1000.5+2000+500)
    assert k["vig_pct"] == round((0.60 - 1) * 100, 1)               # 样例和<1 为负; 实盘≈+3.3


def test_kalshi_failure_returns_none(monkeypatch):
    pd_._kalshi_cache["data"] = None; pd_._kalshi_cache["ts"] = 0.0
    def boom(): raise RuntimeError("403")
    monkeypatch.setattr(pd_, "_kalshi_get", boom)
    assert pd_.fetch_kalshi_btc() is None


def test_assemble_panel_includes_kalshi(monkeypatch):
    now = datetime.datetime(2026, 7, 13, tzinfo=UTC)
    monkeypatch.setattr(pd_, "_now", lambda: now)
    monkeypatch.setattr(pd_, "_fetch_chain", lambda: (_synth_chain(), 60000.0))
    monkeypatch.setattr(pd_, "fetch_polymarket_btc", lambda: [])
    monkeypatch.setattr(pd_, "fetch_kalshi_btc", lambda: {"close": "2027-01-01", "buckets": [], "volume": 1, "vig_pct": 3.3})
    p = pd_._assemble_probdist()
    assert p["kalshi"]["close"] == "2027-01-01"
    assert p["partial"] is False          # kalshi 尽力而为, 不影响 partial
