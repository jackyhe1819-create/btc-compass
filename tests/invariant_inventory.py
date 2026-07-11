# -*- coding: utf-8 -*-
"""信号层同源不变量清单 —— loop engineering 停机判据的骨架。

每条 = 一个"同一值/契约必须在多处一致"的不变量。这份清单把散落在
threshold-sync-guard / wire-new-factor skill 里的散文表变成可机器对账的地图:
test_invariant_inventory.py 断言每条要么有真实存在的守护测试、要么被显式标为
accepted_gap 并写明理由 —— 没有"静默无守护"的不变量。

未来新增一个同源不变量时: 必须在此登记一行并命名守护, 否则元测试红。

字段:
  name    人类可读的不变量名
  places  同源点 (file:line, 仅文档, 不被断言)
  guard   "模块名::函数名" (模块名不含 .py); accepted_gap 行为 None
  status  guarded  = 有测试守护
          partial  = 部分守护 (其余部分另立 accepted_gap 行)
          accepted_gap = 已知未守护但显式接受 (必须写 note 理由)
  note    补充说明 / accepted_gap 的理由
"""

INVARIANT_INVENTORY = [
    # ── 本循环新增守护 ──
    {"name": "桶成员名 ↔ runner 产出键 契约",
     "places": ["btc_web/btc_dashboard/scoring.py:34-92",
                "btc_web/btc_dashboard/runner.py:307-362"],
     "guard": "test_runner_contract::test_bucket_members_produced_by_runner",
     "status": "guarded",
     "note": "AST 抽直算段+api_tasks 键, 断言 members ⊆ 产出; 对不上会被 scoring 静默剔除。"},
    {"name": "_CARD_ORDER 覆盖全部计分成员",
     "places": ["btc_web/btc_dashboard/runner.py:371-384",
                "btc_web/btc_dashboard/scoring.py:34-92"],
     "guard": "test_runner_contract::test_card_order_covers_scoring_members",
     "status": "guarded",
     "note": "缺席则卡片刷新乱跳; 本循环修了 交易所净流(7d) 的缺席。"},
    {"name": "因子离散阈值 backfill._band_* ↔ backtest._step_score",
     "places": ["btc_web/btc_dashboard/backfill.py:234-282", "backtest/factors.py"],
     "guard": "test_factor_thresholds::test_backfill_backtest_thresholds_agree",
     "status": "partial",
     "note": "backfill↔backtest 已逐点钉死 (9 因子 + F&G); calc 侧见下 accepted_gap 行。"},
    {"name": "探针趋势记忆 writer ↔ schema 字段契约",
     "places": ["verify.py:_probe_metrics", "verify.py:_PROBE_FIELDS"],
     "guard": "test_probe_trends::test_record_readback_field_contract",
     "status": "guarded",
     "note": "写入键集合 == _PROBE_FIELDS; 防未来加/改字段时 writer/reader 漂移。"},

    # ── 显式接受的未守护 (诚实标注, 非静默) ──
    {"name": "因子离散阈值 calc(indicators_v2) 侧",
     "places": ["btc_web/btc_dashboard/indicators_v2.py 各 calc_*"],
     "guard": None, "status": "accepted_gap",
     "note": "calc 分档埋在 if/elif+网络 I/O, 单独守护须抽纯 helper = 改评分结构, "
             "越本循环硬边界; 已由 backfill↔backtest 侧间接约束 (三处历史核实一致)。留待后续循环。"},
    {"name": "颜色阈值 {0.3,-0.3,-0.6} 双写",
     "places": ["btc_web/btc_dashboard/indicators_v2.py:99-107",
                "btc_web/btc_dashboard/scoring.py:189-193"],
     "guard": None, "status": "accepted_gap",
     "note": "低风险 (仅卡片颜色); 收敛到共享常量需改评分模块结构, 留待后续循环。"},

    # ── 既有守护 (登记入册, 使清单成为完整地图) ──
    {"name": "triggers._BAND_EDGES ↔ decision.CYCLE_BANDS 阈值/档名/仓位区间",
     "places": ["btc_web/btc_dashboard/triggers.py:29-34",
                "btc_web/btc_dashboard/decision.py:43-51"],
     "guard": "test_consistency::test_trigger_band_edges_match_decision",
     "status": "guarded",
     "note": "既有测试守护 (commit 2f5cab9); 本循环审计曾误报此处无守护, 已对照现实纠正。"},
    {"name": "decision.CYCLE_BANDS ↔ backtest.evaluate.CYCLE_BANDS",
     "places": ["btc_web/btc_dashboard/decision.py:43-51", "backtest/evaluate.py:16-24"],
     "guard": "test_consistency::test_cycle_bands_match_backtest", "status": "guarded", "note": ""},
    {"name": "decision.TACTICAL_BANDS ↔ backtest.evaluate.TACTICAL_BANDS",
     "places": ["btc_web/btc_dashboard/decision.py:55-67", "backtest/evaluate.py:27-33"],
     "guard": "test_consistency::test_tactical_bands_match_backtest", "status": "guarded", "note": ""},
    {"name": "decision.CYCLE_BANDS ↔ scoring.cycle_recommendation 文案",
     "places": ["btc_web/btc_dashboard/decision.py:43-51", "btc_web/btc_dashboard/scoring.py:259-273"],
     "guard": "test_consistency::test_cycle_recommendation_matches_bands", "status": "guarded", "note": ""},
    {"name": "decision.TACTICAL_BANDS ↔ scoring.tactical_recommendation 文案",
     "places": ["btc_web/btc_dashboard/decision.py:55-67", "btc_web/btc_dashboard/scoring.py:275-291"],
     "guard": "test_consistency::test_tactical_recommendation_matches_bands", "status": "guarded", "note": ""},
    {"name": "decision.CYCLE_BANDS ↔ score_history._BANDS",
     "places": ["btc_web/btc_dashboard/decision.py:43-51", "btc_web/btc_dashboard/score_history.py:49-57"],
     "guard": "test_consistency::test_score_history_bands_match_decision", "status": "guarded", "note": ""},
    {"name": "滞回参数 HYST_* ↔ band_stats.json ↔ run_backtest 源码",
     "places": ["btc_web/btc_dashboard/decision.py:35-36", "backtest/run_backtest.py:106",
                "btc_web/btc_dashboard/data/band_stats.json"],
     "guard": "test_consistency::test_hysteresis_params_match_backtest_source", "status": "guarded",
     "note": "另有 test_hysteresis_params_match_band_stats 校验数据侧。"},
    {"name": "桶权重合计=1.0 / MEMBER_WEIGHTS 引用真实成员",
     "places": ["btc_web/btc_dashboard/scoring.py:34-100"],
     "guard": "test_consistency::test_bucket_weights_sum_to_one", "status": "guarded",
     "note": "另有 test_member_weights_reference_real_members。"},
]
