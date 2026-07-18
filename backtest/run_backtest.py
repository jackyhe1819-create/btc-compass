#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC Compass 双评分历史回测主入口。

用法:
    cd btc_compass/backtest && python3 run_backtest.py

输出 (backtest/output/):
    scores.csv          逐日 周期分/战术分 + 各桶分
    cycle_factors.csv   周期分各因子逐日评分
    tactical_factors.csv
    report.md           评估报告 (分档前瞻收益 / IC / 策略净值)
    charts/*.svg        图表
"""

import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import data_sources as ds
import factors
import engine
import evaluate as ev
import svgchart as sc

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
CHARTS = os.path.join(OUT, "charts")
os.makedirs(CHARTS, exist_ok=True)

CYCLE_START = "2014-01-01"      # 趋势伸展桶分位数就绪后的主窗口
TACTICAL_START = "2018-02-01"   # F&G 起点; 2019-09 后杠杆桶加入


def main():
    t0 = time.time()
    print("=" * 60)
    print("📥 [1/5] 拉取数据 (有缓存则秒过)")
    cm = ds.fetch_coinmetrics()
    print(f"   CoinMetrics: {len(cm)} 天 ({cm.index[0].date()} → {cm.index[-1].date()})")
    bd_sth = ds.fetch_bd_series("sth-realized-price", "sthRealizedPrice")
    bd_sopr = ds.fetch_bd_series("sopr", "sopr")
    print(f"   bitcoin-data: STH {len(bd_sth)} 天 / SOPR {len(bd_sopr)} 天")
    fng = ds.fetch_fng()
    funding = ds.fetch_funding()
    stable = ds.fetch_stablecoins()
    etf = ds.fetch_etf()
    print(f"   F&G {len(fng)} | 资金费率 {len(funding)} | 稳定币 {len(stable)} | ETF {len(etf)}")

    # 校准检查: 自算 MVRV-Z / NUPL / Puell vs bitcoin-data 近4年官方值
    print("🔬 [2/5] 派生指标校准 (vs bitcoin-data.com 近4年)")
    try:
        bd_z = ds.fetch_bd_series("mvrv-zscore", "mvrvZscore")
        bd_nupl = ds.fetch_bd_series("nupl", "nupl")
        bd_puell = ds.fetch_bd_series("puell-multiple", "puellMultiple")
        mcap, mvrv = cm["mcap"], cm["mvrv"]
        my_z = (mcap - mcap / mvrv) / mcap.expanding(min_periods=365).std()
        my_nupl = 1 - 1 / mvrv
        my_puell = cm["iss_usd"] / cm["iss_usd"].rolling(365).mean()
        for name, mine, ref in [("MVRV-Z", my_z, bd_z),
                                ("NUPL", my_nupl, bd_nupl),
                                ("Puell", my_puell, bd_puell)]:
            pair = pd.DataFrame({"m": mine, "r": ref}).dropna()
            if len(pair):
                corr = pair["m"].corr(pair["r"])
                mad = (pair["m"] - pair["r"]).abs().mean()
                print(f"   {name}: 相关 {corr:.4f} | 平均绝对差 {mad:.3f} | 重叠 {len(pair)} 天")
    except Exception as e:
        print(f"   ⚠️ 校准跳过: {e}")

    print("⚙️ [3/5] 周期分因子 (向量化)")
    cyc_f = factors.cycle_factor_scores(cm, bd_sth, etf, stable)

    print("⚙️ [4/5] 战术分因子 (RSI/MACD 逐日循环, 约 1-2 分钟)")
    t1 = time.time()
    momentum = factors.momentum_scores(cm["price"], start="2017-06-01")
    print(f"   动量循环完成: {time.time()-t1:.0f}s")
    tac_f = factors.tactical_factor_scores(cm, funding, fng, bd_sopr, momentum)

    print("🧮 [5/5] 调用现网聚合引擎逐日评分 + 评估")
    hist = engine.compute_history(cyc_f, tac_f)
    cycle = hist["cycle"]["score"].loc[CYCLE_START:]
    tactical = hist["tactical"]["score"].loc[TACTICAL_START:]
    price = cm["price"]

    # ---- 落盘 ----
    hist["cycle"].add_prefix("cycle_").join(
        hist["tactical"].add_prefix("tac_")).join(
        price.rename("price")).to_csv(os.path.join(OUT, "scores.csv"))
    cyc_f.to_csv(os.path.join(OUT, "cycle_factors.csv"))
    tac_f.to_csv(os.path.join(OUT, "tactical_factors.csv"))

    # ---- 评估 ----
    cyc_fwd = ev.forward_return_table(cycle, price, [30, 90, 180, 365], ev.CYCLE_BANDS)
    cyc_ic = ev.spearman_ic(cycle, price, [30, 90, 180, 365])
    tac_fwd = ev.forward_return_table(tactical, price, [7, 14, 30], ev.TACTICAL_BANDS)
    tac_ic = ev.spearman_ic(tactical, price, [7, 14, 30])
    strat = ev.run_cycle_strategy(cycle, price)

    # ---- 决策层: 滞回换档 vs 基线 (均计单边 10bp 成本) ----
    HYST_DELTA, HYST_CONFIRM = 0.05, 5
    strat_hyst = ev.run_cycle_strategy_hysteresis(cycle, price, HYST_DELTA, HYST_CONFIRM, cost=0.001)
    strat_base_c = ev.run_cycle_strategy_hysteresis(cycle, price, 0.0, 1, cost=0.001)
    n_years = (cycle.dropna().index[-1] - cycle.dropna().index[0]).days / 365.25
    hyst_rows = []
    for name, s in [("基线(逐日换档)", strat_base_c), (f"滞回(δ={HYST_DELTA},N={HYST_CONFIRM}天)", strat_hyst)]:
        m = s["metrics"]["策略"]
        hyst_rows.append({"策略": name, **{k: round(v, 2) for k, v in m.items()},
                          "换手x": round(s["turnover"], 1),
                          "年均换档": round(s["n_switches"] / n_years, 1)})
    hyst_tab = pd.DataFrame(hyst_rows)

    # ---- 现网决策引擎数据资产: 分档前瞻收益统计 (btc_web 决策面板引用) ----
    def _fwd_to_dict(fwd_df):
        out = {}
        for _, r in fwd_df.iterrows():
            out.setdefault(r["档位"], {})[r["窗口"]] = {
                "n": int(r["样本数"]), "mean": round(r["均值%"], 1),
                "median": round(r["中位数%"], 1), "win": round(r["胜率%"], 1)}
        return out

    band_stats = {
        "generated": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "source": "backtest/run_backtest.py 分档前瞻收益 (样本内标定, 见 report.md 局限)",
        "cycle_window": f"{cycle.dropna().index[0].date()} → {cycle.dropna().index[-1].date()}",
        "tactical_window": f"{tactical.dropna().index[0].date()} → {tactical.dropna().index[-1].date()}",
        "hysteresis": {"delta": HYST_DELTA, "confirm": HYST_CONFIRM},
        # 档位评分边界自描述元数据 — test_consistency 逐一对账 decision 阈值 (2026-07 收尾审计)
        **ev.band_score_bounds(),
        "cycle": _fwd_to_dict(cyc_fwd),
        "tactical": _fwd_to_dict(tac_fwd),
    }
    import json as _json
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "btc_web", "btc_dashboard", "data")
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "band_stats.json"), "w", encoding="utf-8") as f:
        _json.dump(band_stats, f, ensure_ascii=False, indent=1)
    print(f"📊 决策面板数据资产已更新: {os.path.join(data_dir, 'band_stats.json')}")

    # ---- 周期相位置信度数据资产 (phase_stats.json, 相位卡引用) ----
    # 防护: 相位统计失败不应中断报告/图表生成 (band_stats 已落盘, 中断会留下
    # 半更新状态; 2026-07 对抗审查)
    try:
        import phase_stats_gen
        phase_stats_gen.generate(cyc_f, price, os.path.join(data_dir, "phase_stats.json"))
    except Exception as e:
        print(f"⚠️ phase_stats 生成失败 (跳过, 相位卡将用旧资产): {e}")

    # ---- 图表 ----
    p1 = sc.Panel("BTC 价格 (对数)", height=220, log=True)
    p1.add("BTC/USD", price.loc[CYCLE_START:], "#f7931a")
    p2 = sc.Panel("周期仓位分 (≥0.30 偏多 / ≤-0.12 减配 · 2026-07 重标定)", height=150,
                  ylim=(-1.05, 1.05),
                  bands=[(0.30, 1.05, "#22aa66", 0.10), (-1.05, -0.12, "#cc4444", 0.10)])
    p2.add("周期分", cycle, "#4488ff")
    p3 = sc.Panel("短期战术分 (≥0.10 偏有利 / ≤-0.10 谨慎)", height=150, ylim=(-1.05, 1.05),
                  bands=[(0.10, 1.05, "#22aa66", 0.10), (-1.05, -0.10, "#cc4444", 0.10)])
    p3.add("战术分", tactical, "#9966cc")
    sc.render([p1, p2, p3], pd.Timestamp(CYCLE_START), price.index[-1],
              os.path.join(CHARTS, "scores_vs_price.svg"))

    eq = strat["equity"]
    p4 = sc.Panel("周期分档位仓位策略 vs 基准 (净值, 对数)", height=240, log=True)
    p4.add("策略", eq["策略"], "#4488ff")
    p4.add("HODL", eq["HODL"], "#f7931a")
    p4.add("恒定50%", eq["恒定50%"], "#999999", width=1.2)
    p5 = sc.Panel("策略仓位 (绿=逐日 / 蓝=滞回决策层)", height=110, ylim=(0, 1))
    p5.add("仓位", strat["position"], "#22aa66", width=1.2)
    p5.add("滞回仓位", strat_hyst["position"], "#4488ff", width=1.2)
    sc.render([p4, p5], eq.index[0], eq.index[-1],
              os.path.join(CHARTS, "strategy_equity.svg"))

    # ---- 报告 ----
    cov_c = engine.factor_coverage(cyc_f)
    cov_t = engine.factor_coverage(tac_f)
    m = strat["metrics"]
    mtab = pd.DataFrame([{"组合": k, **v} for k, v in m.items()])

    report = f"""# BTC Compass 双评分历史回测报告

生成时间: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}
回测窗口: 周期分 {cycle.dropna().index[0].date()} → {cycle.dropna().index[-1].date()} ({len(cycle.dropna())} 天) | 战术分 {tactical.dropna().index[0].date()} → {tactical.dropna().index[-1].date()} ({len(tactical.dropna())} 天)

## ⚠️ 方法论与局限 (先读这个)

1. **样本内偏差**: 各因子的打分阈值 (如 NUPL>0.75 兴奋区) 本身参考了历史周期制定,
   本回测是"设计一致性验证", **不是样本外证明**。阈值与回测共用同一段历史。
2. **缺失因子**: 期货基差 (历史不可免费获得)、多空比 (交易所仅留 30 天) 在全程缺失,
   缺失因子按现网同一套"剔除重归一"机制处理。
   交易所余额于 2026-07 退出周期评分 (与现网一致), 仅保留卡片展示。
3. **数据源差异**: 资金费率用 Binance (现网 OKX)、MVRV-Z/NUPL/Puell 由 CoinMetrics
   MVRV 反推 (校准结果见运行日志)、RSI/MACD 回测仅 日/周/月 三腿 (现网另含 OKX 真实 4H/12H)。
4. **前瞻收益样本重叠**: 长窗口 (180/365d) 相邻样本高度重叠, 有效样本数远小于表中天数,
   档位均值的统计显著性有限。
5. 价格为 CoinMetrics UTC 收盘参考价; 策略未计交易成本 (换手率见下)。
6. **2026-07 对抗性审查后的口径**: ① RSI 移除伪 4H/12H 切片与年线腿 (旧版日线三重计票);
   ② Pi Cycle 改 {{0,-0.5,-1}} 顶部探测器编码 (旧版把"远离交叉"当 +1 看多);
   ③ MVRV-Z/NUPL/Puell 改为 4 年分位数为主 + 绝对阈值极值保底 (修复周期振幅衰减)。
   本报告为新口径下的重跑结果, 与 2026-06 版报告数字不可直接对比。

## 因子覆盖范围

### 周期分
{ev.md_table(cov_c)}

### 战术分
{ev.md_table(cov_t)}

## 周期分: 分档前瞻收益

{ev.md_table(cyc_fwd)}

### 评分-收益秩相关 IC (Spearman, 期望为正)

{ev.md_table(cyc_ic)}

## 战术分: 分档前瞻收益

{ev.md_table(tac_fwd)}

### 评分-收益秩相关 IC

{ev.md_table(tac_ic)}

## 周期分档位仓位策略 (次日生效, 无成本)

平均仓位 {strat['avg_pos']:.0%} | 累计换手 {strat['turnover']:.1f}x

{ev.md_table(mtab, floatfmt="{:+.2f}")}

## 决策层: 滞回换档 (2026-07 新增, 现网决策面板同款规则)

规则: 周期分须越过当前档位边界 ±{HYST_DELTA} 且新档位连续 {HYST_CONFIRM} 天保持才换档。
两行均计单边 10bp 交易成本。基线逐日换档 12 年换 {strat_base_c['n_switches']} 次
(边界附近日频往返, 决策不可执行); 滞回后 {strat_hyst['n_switches']} 次。
参数取自 δ∈[0.03,0.06]×N∈[3,7] 网格平台中部 — 全网格 Sharpe 均 ≥ 基线,
非单点调优; 代价是减仓延迟, 最大回撤略深 (约 -46.6%→-48.4%)。

{ev.md_table(hyst_tab, floatfmt="{:+.2f}")}

## 补充发现 (2026-06 静态结论 + 2026-07 对抗性审查后更新)

1. **档位阈值已重标定 (2026-07 落地)**: 桶平均机制把量程压缩到约 [-0.5, +0.68],
   旧斐波那契阈值 (±0.618/±0.382) 极值档 12 年触发 <1%。现按 2014+ 评分分布分位数
   重标定为 0.45/0.30/0.15/0.00/-0.12/-0.30 (目标频率 3/12/30/28/17/7/3%);
   战术分同理重标定为 0.25/0.10/-0.10/-0.35。阈值与分布同源, 属样本内标定 (见局限1)。
2. **交易所余额 (2026-07 退出评分)**: v2 口径 (CM 30日存量变化) 2014-2023 确有
   判别力, 但 ETF 时代退化为常亮看多灯 (2025 年 50% 天数正分 vs 1% 负分) 且与
   ETF净流入双重计数 (2024+ 因子分相关 +0.29)。留一对照: 移除后全样本 IC365
   0.603→0.514 (跌幅全在前 ETF 时代), 现行体制 2024+ IC365 +0.085→+0.229 /
   Sharpe 0.89→1.20 — 按现行体制裁决移除; 去趋势重标定与降权 0.10 变体均被支配。
3. **交易所净流 7d (2026-06 已接入)**: 新建"链上资金流"桶 15%
   (杠杆 40→35 / 动量 35→30 / 情绪 25→20)。单因子 7-14d IC +0.10~+0.13。
4. **战术分**: "杠杆拥挤"档 (旧称"高危时段") 30d 前瞻收益为正 — 逆向过热信号
   在主升段提前触发, 负分只约束"别加杠杆追高", 不构成现货卖出信号 (文案已如实标注);
   含基差/多空比的完整配置仍无法回测检验。
5. **2026-07 编码修复对 IC 的影响**: Pi Cycle 去掉常驻 +1、链上桶分位数化后,
   周期分 IC 全窗口回落 (365d 0.475→0.36) — 旧 IC 中有一部分来自
   "无信号=看多"编码搭上 BTC 长期上行漂移的样本内红利, 属虚高;
   新口径最大回撤改善 (-49%→-45%), Sharpe 基本持平, 熊市中段不再出现满分看多票。

![scores](charts/scores_vs_price.svg)
![equity](charts/strategy_equity.svg)
"""
    with open(os.path.join(OUT, "report.md"), "w") as f:
        f.write(report)

    print(f"✅ 完成, 耗时 {time.time()-t0:.0f}s")
    print(f"📄 报告: {os.path.join(OUT, 'report.md')}")
    print()
    print("---- 周期分 IC ----")
    print(cyc_ic.to_string(index=False))
    print("---- 战术分 IC ----")
    print(tac_ic.to_string(index=False))
    print("---- 策略对比 ----")
    print(mtab.to_string(index=False))


if __name__ == "__main__":
    main()
