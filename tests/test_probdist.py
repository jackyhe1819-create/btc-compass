import datetime, math
from btc_dashboard.probdist import _ncdf, _black76_call, nearest_monthly, fit_smile, risk_neutral_density

UTC = datetime.timezone.utc

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

def test_rnd_put_skew_shifts_downside():
    now = datetime.datetime(2026, 7, 13, tzinfo=UTC)
    strikes = list(range(40000, 90001, 5000))
    skew = {k: 0.60 + max(0, (60000-k))/60000*0.5 for k in strikes}   # 低行权价 IV 更高(put贵)
    r = risk_neutral_density(_synth_chain(ivs=skew), 60000, now)
    assert r is not None and r["p_up"] < 50.0        # 左偏 → 上涨概率<50%

def test_rnd_too_few_strikes_returns_none():
    now = datetime.datetime(2026, 7, 13, tzinfo=UTC)
    thin = [{"instrument_name": f"BTC-31JUL26-{k}-C", "mark_iv": 60, "underlying_price": 60000}
            for k in (58000, 60000, 62000)]           # 仅 3 个
    assert risk_neutral_density(thin, 60000, now) is None
