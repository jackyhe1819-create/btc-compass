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

    # ---- 图表 ----
    p1 = sc.Panel("BTC 价格 (对数)", height=220, log=True)
    p1.add("BTC/USD", price.loc[CYCLE_START:], "#f7931a")
    p2 = sc.Panel("周期仓位分 (≥0.382 偏多 / ≤-0.382 偏空)", height=150,
                  ylim=(-1.05, 1.05),
                  bands=[(0.382, 1.05, "#22aa66", 0.10), (-1.05, -0.382, "#cc4444", 0.10)])
    p2.add("周期分", cycle, "#4488ff")
    p3 = sc.Panel("短期战术分", height=150, ylim=(-1.05, 1.05),
                  bands=[(0.2, 1.05, "#22aa66", 0.10), (-1.05, -0.2, "#cc4444", 0.10)])
    p3.add("战术分", tactical, "#9966cc")
    sc.render([p1, p2, p3], pd.Timestamp(CYCLE_START), price.index[-1],
              os.path.join(CHARTS, "scores_vs_price.svg"))

    eq = strat["equity"]
    p4 = sc.Panel("周期分档位仓位策略 vs 基准 (净值, 对数)", height=240, log=True)
    p4.add("策略", eq["策略"], "#4488ff")
    p4.add("HODL", eq["HODL"], "#f7931a")
    p4.add("恒定50%", eq["恒定50%"], "#999999", width=1.2)
    p5 = sc.Panel("策略仓位", height=110, ylim=(0, 1))
    p5.add("仓位", strat["position"], "#22aa66", width=1.2)
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
   交易所余额为 v2 口径 (CoinMetrics 30日存量变化, 2026-06 上线), 与现网一致。
3. **数据源差异**: 资金费率用 Binance (现网 OKX)、MVRV-Z/NUPL/Puell 由 CoinMetrics
   MVRV 反推 (校准结果见运行日志)、MACD 回测仅 日/周/月 三腿 (现网另含 OKX 4H/12H)。
4. **前瞻收益样本重叠**: 长窗口 (180/365d) 相邻样本高度重叠, 有效样本数远小于表中天数,
   档位均值的统计显著性有限。
5. 价格为 CoinMetrics UTC 收盘参考价; 策略未计交易成本 (换手率见下)。

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

## 补充发现 (2026-06 静态结论, 详见会话记录)

1. **评分量程 vs 档位阈值**: 周期分实际范围约 [-0.5, +0.65], ±0.618 极值档触发极少 ——
   桶平均机制天然压缩量程, 如需档位区分度可按评分历史分位数重标定阈值。
2. **交易所余额 v2 (已上线)**: 旧版冷钱包快照对比 94.5% 时间打 0 分;
   新版 CoinMetrics 30日存量变化分位数打分, 上线后周期分各窗口 IC 提升 +0.02 左右。
3. **交易所净流 7d (2026-06 已接入)**: 新建"链上资金流"桶 15%
   (杠杆 40→35 / 动量 35→30 / 情绪 25→20)。单因子 7-14d IC +0.10~+0.13。
4. **战术分**: 接入净流桶后 7/14/30d IC 从 ≈0 → +0.06/+0.08/+0.06,
   "逢低分批"档前瞻收益恢复单调; "高危时段"档 30d 前瞻仍为正
   (逆向过热信号在主升段提前触发); 含基差/多空比的完整配置无法回测检验。
5. **Ahr999 入桶 (2026-06 已上线)**: 以分位数口径加入趋势伸展桶 (5成员等权)。
   虽与桶平均相关 0.92, 但乘积结构放大周期极值共振:
   周期分 IC 365d 0.440→0.475, 策略 Sharpe 1.02→1.04, 换手再降。

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
