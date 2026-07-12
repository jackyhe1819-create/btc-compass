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


def test_atm_iv_deterministic_on_call_put_tie():
    # 与 _chain() 相同的数据，但近月 ATM 档位故意让 PUT 行排在 CALL 行之前，
    # 验证 _atm_iv 的 tie-break 不依赖输入顺序，应始终优先选中 call。
    now = datetime.datetime(2026, 7, 12, tzinfo=UTC)
    chain = [
        {"instrument_name": "BTC-24JUL26-64000-P", "mark_iv": 33.0, "open_interest": 200, "underlying_price": 64000},
        {"instrument_name": "BTC-24JUL26-64000-C", "mark_iv": 32.0, "open_interest": 100, "underlying_price": 64000},
        {"instrument_name": "BTC-24JUL26-56000-P", "mark_iv": 40.0, "open_interest": 300, "underlying_price": 64000},
        {"instrument_name": "BTC-24JUL26-72000-C", "mark_iv": 30.0, "open_interest": 50,  "underlying_price": 64000},
        {"instrument_name": "BTC-25DEC26-64000-C", "mark_iv": 41.0, "open_interest": 10,  "underlying_price": 64000},
        {"instrument_name": "BTC-25DEC26-64000-P", "mark_iv": 42.0, "open_interest": 10,  "underlying_price": 64000},
    ]
    m = derive_snapshot(chain, 64000.0, now)
    assert m["atm_front"] == 32.0   # 必须始终是 call 的 iv，不受行序影响


def test_max_pain_none_when_eligible_expiry_has_zero_oi():
    # 唯一到期日全部 open_interest=0 -> _max_pain 返回 None，
    # derive_snapshot 必须优雅降级为 max_pain=None，而不是在 round(None) 处抛异常。
    now = datetime.datetime(2026, 7, 12, tzinfo=UTC)
    chain = [
        {"instrument_name": "BTC-05AUG26-64000-C", "mark_iv": 30.0, "open_interest": 0, "underlying_price": 64000},
        {"instrument_name": "BTC-05AUG26-64000-P", "mark_iv": 31.0, "open_interest": 0, "underlying_price": 64000},
    ]
    m = derive_snapshot(chain, 64000.0, now)
    assert m["max_pain"] is None
