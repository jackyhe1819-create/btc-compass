# -*- coding: utf-8 -*-
"""探针趋势记忆的离线守护:字段契约 / 退出码不翻转 / best-effort / base_url 分区 /
首轮不告警 / 覆盖率劣化判据。全部不触网。"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
import verify  # noqa: E402


class _Scoring:
    CYCLE_BUCKETS = {"b": {"members": ["A", "B"]}}
    TACTICAL_BUCKETS = {"t": {"members": ["C"]}}


def _fake_data(cov_c=0.95, cov_t=0.9):
    return {
        "data_source": "test", "data_synthetic": False, "btc_price": 60000,
        "total_score": 0.1, "tactical_score": -0.2,
        "cycle_coverage": cov_c, "tactical_coverage": cov_t,
        "cache_age_s": 100,
        "indicators": {"A": {"value": 1}, "B": {"value": 2}, "C": {"value": 3}},
        "decision": {"cycle": {"band": "标准配置"}, "tactical": {"pace": "正常分批"}},
    }


def _make_rec(base_url, **kw):
    d = _fake_data(**{k: v for k, v in kw.items() if k in ("cov_c", "cov_t")})
    rec = verify._probe_metrics(base_url, d, d["indicators"], _Scoring)
    rec["score_history_days"] = kw.get("days", 120)
    return rec


def test_record_readback_field_contract(tmp_path, monkeypatch):
    """写入记录的键集合 == 声明的字段清单(锁 writer↔schema)。"""
    p = tmp_path / "probe_history.jsonl"
    monkeypatch.setattr(verify, "PROBE_HISTORY_PATH", str(p))
    rec = _make_rec("http://x")
    assert set(rec) == set(verify._PROBE_FIELDS)
    verify._record_probe(rec)
    back = json.loads(p.read_text(encoding="utf-8").strip())
    assert set(back) == set(verify._PROBE_FIELDS)


def test_warn_does_not_flip_exit_code(monkeypatch):
    """趋势劣化只 _report('WARN',...) —— 不进 fails,退出码保持 0。"""
    monkeypatch.setattr(verify, "_results", [])
    verify._report("WARN", "趋势劣化(测试)")
    fails = [m for lv, m in verify._results if lv == "FAIL"]
    assert fails == []


def test_first_runs_no_trend_warn(tmp_path, monkeypatch):
    """历史 < MIN_HISTORY 时只记录、不告警。"""
    p = tmp_path / "probe_history.jsonl"
    monkeypatch.setattr(verify, "PROBE_HISTORY_PATH", str(p))
    monkeypatch.setattr(verify, "_results", [])
    verify._record_probe(_make_rec("http://x", cov_c=0.95))
    verify._record_probe(_make_rec("http://x", cov_c=0.95))
    verify._check_probe_trends("http://x", _make_rec("http://x", cov_c=0.50))  # 仅 2 条历史
    assert not [m for lv, m in verify._results if lv == "WARN"]


def test_coverage_drop_warns(tmp_path, monkeypatch):
    """近 K 条中位 0.95、当前 0.80(降 0.15 > 0.10)→ WARN;当前 0.90(降 0.05)→ 不 WARN。"""
    p = tmp_path / "probe_history.jsonl"
    monkeypatch.setattr(verify, "PROBE_HISTORY_PATH", str(p))
    for _ in range(4):
        verify._record_probe(_make_rec("http://x", cov_c=0.95))
    monkeypatch.setattr(verify, "_results", [])
    verify._check_probe_trends("http://x", _make_rec("http://x", cov_c=0.80))
    assert any("覆盖率趋势劣化" in m for lv, m in verify._results if lv == "WARN")
    monkeypatch.setattr(verify, "_results", [])
    verify._check_probe_trends("http://x", _make_rec("http://x", cov_c=0.90))
    assert not [m for lv, m in verify._results if lv == "WARN"]


def test_base_url_partition(tmp_path, monkeypatch):
    """local 与 live 记录混在同文件,读取只取同 base_url 子集。"""
    p = tmp_path / "probe_history.jsonl"
    monkeypatch.setattr(verify, "PROBE_HISTORY_PATH", str(p))
    verify._record_probe(_make_rec("http://local", cov_c=0.6))
    verify._record_probe(_make_rec("https://live", cov_c=0.95))
    got = verify._load_probe_history("https://live")
    assert len(got) == 1 and got[0]["base_url"] == "https://live"


def test_record_is_best_effort(monkeypatch):
    """写盘路径不可写时不得抛异常。"""
    monkeypatch.setattr(verify, "PROBE_HISTORY_PATH", "/nonexistent_dir_xyz/no/probe.jsonl")
    monkeypatch.setattr(verify, "_results", [])
    verify._record_probe(_make_rec("http://x"))  # 不应 raise


def test_load_skips_corrupt_lines(tmp_path, monkeypatch):
    p = tmp_path / "probe_history.jsonl"
    p.write_text('{"base_url":"http://x","cycle_coverage":0.9}\nGARBAGE\n', encoding="utf-8")
    monkeypatch.setattr(verify, "PROBE_HISTORY_PATH", str(p))
    got = verify._load_probe_history("http://x")
    assert len(got) == 1


def test_probe_history_gitignored():
    gi = open(os.path.join(REPO, ".gitignore"), encoding="utf-8").read()
    assert "probe_history.jsonl" in gi, "probe_history.jsonl 未被 .gitignore(会误入库)"
