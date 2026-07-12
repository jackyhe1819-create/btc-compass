import datetime
from btc_dashboard.options import parse_instrument, derive_snapshot

UTC = datetime.timezone.utc

def test_parse_instrument():
    exp, strike, cp = parse_instrument("BTC-28AUG26-60000-C")
    assert exp == datetime.datetime(2026, 8, 28, tzinfo=UTC)
    assert strike == 60000.0
    assert cp == "C"

def _chain():
    # 两个到期: 近月 24JUL26, 远月 25DEC26; spot=64000
    return [
        {"instrument_name": "BTC-24JUL26-64000-C", "mark_iv": 32.0, "open_interest": 100, "underlying_price": 64000},
        {"instrument_name": "BTC-24JUL26-64000-P", "mark_iv": 33.0, "open_interest": 200, "underlying_price": 64000},
        {"instrument_name": "BTC-24JUL26-56000-P", "mark_iv": 40.0, "open_interest": 300, "underlying_price": 64000},
        {"instrument_name": "BTC-24JUL26-72000-C", "mark_iv": 30.0, "open_interest": 50,  "underlying_price": 64000},
        {"instrument_name": "BTC-25DEC26-64000-C", "mark_iv": 41.0, "open_interest": 10,  "underlying_price": 64000},
        {"instrument_name": "BTC-25DEC26-64000-P", "mark_iv": 42.0, "open_interest": 10,  "underlying_price": 64000},
    ]

def test_derive_snapshot_put_call_and_term():
    now = datetime.datetime(2026, 7, 12, tzinfo=UTC)
    m = derive_snapshot(_chain(), 64000.0, now)
    # Put OI 200+300+10=510, Call OI 100+50+10=160 -> 3.19
    assert m["put_call_oi"] == 3.19
    assert m["atm_front"] == 32.0   # 近月 ATM(=64000) call iv
    assert m["atm_back"] == 41.0
    assert m["term_slope"] == 9.0   # 41 - 32
    assert m["n_contracts"] == 6
