import datetime
from btc_dashboard.options import parse_instrument, derive_snapshot, calc_dvol_percentile
from btc_dashboard import options as opt

UTC = datetime.timezone.utc

def test_parse_instrument():
    exp, strike, cp = parse_instrument("BTC-28AUG26-60000-C")
    assert exp == datetime.datetime(2026, 8, 28, 8, tzinfo=UTC)   # Deribit 期权 08:00 UTC 到期
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


def test_dvol_percentile_full_window():
    closes = [float(i) for i in range(1, 101)]  # 1..100
    pct, n = calc_dvol_percentile(closes, current=25.0, window=1460)
    assert n == 100
    assert pct == 24.0   # 24 个 <25 (严格小于)


def test_dvol_percentile_respects_window():
    closes = [float(i) for i in range(1, 2001)]  # 2000 点
    pct, n = calc_dvol_percentile(closes, current=1999.0, window=1460)
    assert n == 1460     # 只取尾部 1460
    assert pct == 99.9   # 尾部 541..2000 中 1458 个 <1999 (若误取头部会得 100.0)


def _ms(dt):
    return int(dt.timestamp() * 1000)


def test_assemble_panel_from_raw(monkeypatch):
    now = datetime.datetime(2026, 7, 12, tzinfo=UTC)
    # 时间戳须贴近 now: 尾部落后 >3 天会被过期检查如实标 partial
    d1, d2, d3 = (_ms(now) - 2 * 86400000, _ms(now) - 1 * 86400000, _ms(now))
    monkeypatch.setattr(opt, "_load_dvol_store", lambda: [])   # 隔离真实持久库
    monkeypatch.setattr(opt, "_now", lambda: now)
    monkeypatch.setattr(opt, "fetch_dvol_history",
                        lambda a, b: [(d1, 40.0), (d2, 38.0), (d3, 36.0)])
    monkeypatch.setattr(opt, "_fetch_chain",
                        lambda: (_chain(), 64000.0))
    p = opt._assemble_panel()
    assert p["dvol_now"] == 36.0
    assert p["dvol_pct"] == 0.0       # strict < : 0/3 <36 (36 是最小值)
    assert p["put_call_oi"] == 3.19
    assert p["partial"] is False


def test_assemble_panel_partial_on_chain_failure(monkeypatch):
    # 期权链拉取失败时，输出字典必须仍含全部快照键 (值为 None)，
    # 而不是整体缺失这些键 —— 下游 (Flask 路由/前端) 依赖"键存在、值为 None"的降级契约。
    now = datetime.datetime(2026, 7, 12, tzinfo=UTC)
    monkeypatch.setattr(opt, "_load_dvol_store", lambda: [])   # 隔离真实持久库
    monkeypatch.setattr(opt, "_now", lambda: now)
    monkeypatch.setattr(opt, "fetch_dvol_history",
                        lambda a, b: [(1, 40.0), (2, 38.0), (3, 36.0)])

    def _boom():
        raise RuntimeError("chain fetch failed")
    monkeypatch.setattr(opt, "_fetch_chain", _boom)

    result = opt._assemble_panel()
    assert result["partial"] is True
    assert result["dvol_now"] == 36.0   # DVOL 侧不受期权链失败影响，仍正常计算

    for key in ("put_call_oi", "skew_wing", "skew_exp", "atm_front", "atm_back",
                "term_slope", "front_exp", "back_exp", "max_pain", "max_pain_exp",
                "n_contracts"):
        assert key in result, f"missing snapshot key: {key}"
        assert result[key] is None or key == "n_contracts"
    assert result["n_contracts"] == 0


def test_fetch_chain_spot_uses_nearest_expiry(monkeypatch):
    # underlying_price 是各到期日的合成远期价且 API 不保证返回顺序 —
    # 首条故意放远月(升水 70000), spot 必须取到期最近合约的值, 不受行序影响
    raw = [
        {"instrument_name": "BTC-25DEC26-64000-C", "underlying_price": 70000},
        {"instrument_name": "BTC-24JUL26-64000-C", "underlying_price": 64000},
    ]
    monkeypatch.setattr(opt, "_get", lambda method, **kw: raw)
    chain, spot = opt._fetch_chain()
    assert spot == 64000


def test_assemble_panel_partial_on_empty_dvol(monkeypatch):
    # DVOL 接口返回合法空 data(非异常)时也必须标 partial —
    # 否则 dvol 全 None 的空壳会被当"完整"数据写入三层缓存(SWR 守卫依赖 partial 语义)
    now = datetime.datetime(2026, 7, 12, tzinfo=UTC)
    monkeypatch.setattr(opt, "_load_dvol_store", lambda: [])   # 隔离真实持久库
    monkeypatch.setattr(opt, "_now", lambda: now)
    monkeypatch.setattr(opt, "fetch_dvol_history", lambda a, b: [])
    monkeypatch.setattr(opt, "_fetch_chain", lambda: (_chain(), 64000.0))
    p = opt._assemble_panel()
    assert p["partial"] is True
    assert p["dvol_now"] is None
    assert p["put_call_oi"] == 3.19   # 链侧不受 DVOL 空数据影响, 仍正常计算


def test_load_dvol_store_missing_or_corrupt(tmp_path):
    # 缺失 / 坏 JSON / 版本不符 → 一律 [] (调用方回退全量拉取)
    assert opt._load_dvol_store(str(tmp_path / "nope.json")) == []
    bad = tmp_path / "bad.json"; bad.write_text("{not json")
    assert opt._load_dvol_store(str(bad)) == []
    v2 = tmp_path / "v2.json"; v2.write_text('{"version":"v2","series":[[1000,40.0]]}')
    assert opt._load_dvol_store(str(v2)) == []


def test_load_dvol_store_reads_series(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text('{"version":"v1","series":[[2000,38.0],[1000,40.0]]}')
    assert opt._load_dvol_store(str(p)) == [(1000, 40.0), (2000, 38.0)]  # 排序兜底


def test_assemble_panel_fetches_tail_only_when_store_present(monkeypatch):
    # store 在 → 只拉 store 末点+1天 → now 的增量, 不再全量重拉 5.3 年
    now = datetime.datetime(2026, 7, 12, tzinfo=UTC)
    d1, d2, d3 = (_ms(now) - 3 * 86400000, _ms(now) - 2 * 86400000, _ms(now) - 1 * 86400000)
    monkeypatch.setattr(opt, "_now", lambda: now)
    monkeypatch.setattr(opt, "_load_dvol_store", lambda: [(d1, 40.0), (d2, 38.0)])
    calls = []
    def fake_fetch(start, end):
        calls.append((start, end))
        return [(d3, 36.0)]
    monkeypatch.setattr(opt, "fetch_dvol_history", fake_fetch)
    monkeypatch.setattr(opt, "_fetch_chain", lambda: (_chain(), 64000.0))
    p = opt._assemble_panel()
    assert calls and calls[0][0] == d2 + 86400000   # 增量起点 = 库末点 + 1 天
    assert p["dvol_now"] == 36.0                    # 合并后末点
    assert p["partial"] is False


def test_assemble_panel_fresh_store_survives_tail_failure(monkeypatch):
    # 库新鲜(末点=昨天)时 Deribit 抖动不该把面板打成 partial — 库本身就是完整数据
    now = datetime.datetime(2026, 7, 12, tzinfo=UTC)
    d_y = _ms(now) - 1 * 86400000
    monkeypatch.setattr(opt, "_now", lambda: now)
    monkeypatch.setattr(opt, "_load_dvol_store", lambda: [(d_y - 86400000, 40.0), (d_y, 38.0)])
    def boom(a, b):
        raise RuntimeError("deribit down")
    monkeypatch.setattr(opt, "fetch_dvol_history", boom)
    monkeypatch.setattr(opt, "_fetch_chain", lambda: (_chain(), 64000.0))
    p = opt._assemble_panel()
    assert p["partial"] is False
    assert p["dvol_now"] == 38.0


def test_assemble_panel_partial_when_store_stale_and_tail_fails(monkeypatch):
    # 库过期(>3 天)且增量拉不到 → 别把旧值当今天的 IV, 标 partial
    now = datetime.datetime(2026, 7, 12, tzinfo=UTC)
    d_old = _ms(now) - 5 * 86400000
    monkeypatch.setattr(opt, "_now", lambda: now)
    monkeypatch.setattr(opt, "_load_dvol_store", lambda: [(d_old - 86400000, 40.0), (d_old, 38.0)])
    def boom(a, b):
        raise RuntimeError("deribit down")
    monkeypatch.setattr(opt, "fetch_dvol_history", boom)
    monkeypatch.setattr(opt, "_fetch_chain", lambda: (_chain(), 64000.0))
    p = opt._assemble_panel()
    assert p["partial"] is True


def test_assemble_panel_spark_full_downsampled(monkeypatch):
    # 全史降采样: ~365-500 点, 末点(今天)必在, 起始年月正确; spark(90d) 不受影响
    now = datetime.datetime(2026, 7, 12, tzinfo=UTC)
    base = _ms(now) - 1000 * 86400000
    series = [(base + i * 86400000, 30.0 + (i % 50)) for i in range(1000)]
    monkeypatch.setattr(opt, "_now", lambda: now)
    monkeypatch.setattr(opt, "_load_dvol_store", lambda: series)
    monkeypatch.setattr(opt, "fetch_dvol_history", lambda a, b: [])
    monkeypatch.setattr(opt, "_fetch_chain", lambda: (_chain(), 64000.0))
    p = opt._assemble_panel()
    assert 300 <= len(p["spark_full"]) <= 520
    assert p["spark_full"][-1] == round(series[-1][1], 1)   # 末点必在
    assert len(p["spark"]) == 90                            # 90d 视图不变
    assert p["spark_full_start"] == datetime.datetime.utcfromtimestamp(
        series[0][0] / 1000).strftime("%Y-%m")


def test_assemble_panel_spark_full_empty_when_no_hist(monkeypatch):
    now = datetime.datetime(2026, 7, 12, tzinfo=UTC)
    monkeypatch.setattr(opt, "_now", lambda: now)
    monkeypatch.setattr(opt, "_load_dvol_store", lambda: [])
    monkeypatch.setattr(opt, "fetch_dvol_history", lambda a, b: [])
    monkeypatch.setattr(opt, "_fetch_chain", lambda: (_chain(), 64000.0))
    p = opt._assemble_panel()
    assert p["spark_full"] == [] and p["spark_full_start"] is None


def test_fetch_chain_cached_within_ttl(monkeypatch):
    # 期权/概率分布两面板刷新相隔数秒, 120s 链缓存让一次链请求喂两张卡(Y6)
    monkeypatch.setattr(opt, "_chain_cache", {"data": None, "ts": 0.0})
    calls = []
    raw = [{"instrument_name": "BTC-24JUL26-64000-C", "underlying_price": 64000}]
    def fake_get(method, **kw):
        calls.append(method)
        return raw
    monkeypatch.setattr(opt, "_get", fake_get)
    a = opt._fetch_chain()
    b = opt._fetch_chain()
    assert len(calls) == 1          # 第二次走缓存
    assert a == b == (raw, 64000)


def test_backfill_dvol_idempotent(tmp_path, monkeypatch):
    from btc_dashboard import backfill
    monkeypatch.setattr(backfill, "fetch_dvol_history",
                        lambda a, b: [(1000, 40.0), (2000, 38.0)])
    n1 = backfill.backfill_dvol(str(tmp_path))
    n2 = backfill.backfill_dvol(str(tmp_path))
    assert n1 == 2
    assert n2 == 0  # 二次无新点


def _chain_with_wings():
    # skew_exp = _nearest_exp(≥20d) 会选中 14AUG26(now=07-12 时 ~33 天);
    # 翼带按 spot=64000: put 翼 [54400,60800], call 翼 [67200,73600]
    return [
        {"instrument_name": "BTC-14AUG26-64000-C", "mark_iv": 33.0, "open_interest": 10, "underlying_price": 64000},
        {"instrument_name": "BTC-14AUG26-56000-P", "mark_iv": 40.0, "open_interest": 10, "underlying_price": 64000},
        {"instrument_name": "BTC-14AUG26-60000-P", "mark_iv": 38.0, "open_interest": 10, "underlying_price": 64000},
        {"instrument_name": "BTC-14AUG26-68000-C", "mark_iv": 30.0, "open_interest": 10, "underlying_price": 64000},
        {"instrument_name": "BTC-14AUG26-72000-C", "mark_iv": 32.0, "open_interest": 10, "underlying_price": 64000},
    ]


def test_skew_wing_numeric():
    # put 翼 avg(40,38)=39.0, call 翼 avg(30,32)=31.0 → skew_wing = +8.0(正=看跌溢价)
    # 旧 fixture 的 skew_exp 落在无翼合约的 25DEC26, skew 计算路径从未被执行过 — 本 fixture 补上
    now = datetime.datetime(2026, 7, 12, tzinfo=UTC)
    m = derive_snapshot(_chain_with_wings(), 64000.0, now)
    assert m["skew_wing"] == 8.0


def test_max_pain_numeric():
    # 手算: K=50000→payout 200k; K=60000→50k+50k=100k(最小); K=70000→200k → 痛点 60000
    now = datetime.datetime(2026, 7, 12, tzinfo=UTC)
    chain = [
        {"instrument_name": "BTC-14AUG26-50000-C", "mark_iv": 35.0, "open_interest": 5,  "underlying_price": 64000},
        {"instrument_name": "BTC-14AUG26-60000-C", "mark_iv": 33.0, "open_interest": 10, "underlying_price": 64000},
        {"instrument_name": "BTC-14AUG26-60000-P", "mark_iv": 34.0, "open_interest": 10, "underlying_price": 64000},
        {"instrument_name": "BTC-14AUG26-70000-P", "mark_iv": 36.0, "open_interest": 5,  "underlying_price": 64000},
    ]
    m = derive_snapshot(chain, 64000.0, now)
    assert m["max_pain"] == 60000


def test_max_pain_uses_oi_max_expiry():
    # 同页两个痛点到期规则曾不同(期权卡复用 skew 的 ≥20d 最近到期, 旧指标表用 OI 主力)
    # — 统一为 OI 主力(市场惯例), 本用例: 远月(旧实现会选)OI 极小, 近月才是主力
    now = datetime.datetime(2026, 7, 12, tzinfo=UTC)
    chain = [
        {"instrument_name": "BTC-24JUL26-60000-C", "mark_iv": 33.0, "open_interest": 20, "underlying_price": 64000},
        {"instrument_name": "BTC-24JUL26-60000-P", "mark_iv": 34.0, "open_interest": 10, "underlying_price": 64000},
        {"instrument_name": "BTC-25DEC26-80000-C", "mark_iv": 40.0, "open_interest": 1,  "underlying_price": 64000},
        {"instrument_name": "BTC-25DEC26-80000-P", "mark_iv": 41.0, "open_interest": 1,  "underlying_price": 64000},
    ]
    m = derive_snapshot(chain, 64000.0, now)
    assert m["max_pain_exp"] == "24Jul26"    # OI 主力, 而非 ≥20d 的 25Dec26
