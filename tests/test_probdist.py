import datetime, math
from btc_dashboard.probdist import _ncdf, _black76_call, nearest_monthly, fit_smile

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
