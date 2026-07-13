#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.vrp_study
==================
VRP(方差风险溢价 = 隐含方差 − 已实现方差) 4年滚动分位 → 前瞻 BTC 收益的 IC 研究。
判定 VRP 是否够格从"仅展示"升级进战术分评分因子 —— 姊妹研究 options_study.py
(那次判 DVOL 水平 display_only) 的后续。

VRP 是期权侧唯一有扎实前瞻收益可预测性文献根基的量(Bollerslev-Tauchen-Zhou
2009, RFS): IV 水平把"预期动荡量"与"风险补偿"混在一起, VRP 把已实现波动(RV)
差掉, 专门萃取"承担方差风险的时变价格"这一前瞻分量。

    VRP_var_t = IV_t^2 − RV_t^2   (方差单位)
    VRP_vol_t = IV_t   − RV_t      (波动单位, 对离群更稳)
      IV_t = DVOL_t / 100          Deribit 30d 期权隐含年化波动率(前瞻隐含)
      RV_t = 过去 N 日 log-return std × √365  已实现年化波动(后向, 项目本无此计算)

方法(与 options_study.py 完全同口径, 保证可比):
- 分位序列复刻现网 scoring._percentile_score / calc_dvol_percentile:
  滚动 1460 天(4年), 严格 `<`(不含等号), min_periods=365。
- IC 用项目统一的 evaluate.spearman_ic(无前视 shift, 评分高→看多约定; VRP 分位
  若高 VRP 对应后市走弱, IC 应为负)。
- 校准: 同脚本复现纯 DVOL/IV 分位 IC, 须与 options_study.json 落盘值
  (+0.050/+0.025/−0.039) 逐位一致 → 证明管道忠实。

结论(2026-07-13 实测, verdict = display_only):
- 机械门槛(任一窗 |IC|≥0.08 且各窗符号一致) 被 VRP_var(RV=30d/60d) **表面通过**
  (RV=60d 三窗 −0.130/−0.094/−0.112 全达标), 表面比曾被否的 DVOL 单因子强。
- 但稳健性三重否决 → 不升级评分, 维持 display_only:
  1. 窗口依赖: RV 换 90d 就垮成 display_only(max|IC|≈0.04) —— 准入结论随任意
     后向窗口翻转 = 脆弱/过拟合。
  2. 非 IID 致命: 高 VRP 日只有 ~10-13 个独立波动 regime(见 cluster 诊断),
     名义 n≈1460 是幻觉, 在一打独立观测上 |IC|=0.13 的标准误极大, 无统计功效。
  3. 冗余且被反超: VRP 与纯 RV 分位相关 −0.43~−0.47; 而纯 RV(零期权依赖,
     15y 长历史)IC 更强(至 −0.18)更稳(三窗单调) —— 期权数据相对免费的 RV
     无净增量 alpha。
  外加: 符号方向为"应力持续走弱"(高 VRP→更低前瞻收益), 与"波动溢价可收割"
  的经济学叙事相反, 削弱可解释性。
- 高置信的是"**不该现在计分**", 非"**已证明不存在方差溢价**": trailing-RV 是后视
  代理(掺波动动量), 真 VRP 需 HAR-RV 前瞻化; 有效自由度仅 ~10-13, 任何 PASS/FAIL
  功效都不足。若将来有更长跨多周期干净样本 + HAR-RV, 值得再看(低先验)。

用法: cd backtest && python3 vrp_study.py
"""

import os
import sys
import json
import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data_sources as ds
import evaluate as ev

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
DVOL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                         "btc_web", "btc_dashboard", "data", "dvol_history.json")

HORIZONS = [30, 60, 90]
RV_WINDOWS = [30, 60, 90]
IC_THRESHOLD = 0.08


# ------------------------------------------------------------
# 数据装载
# ------------------------------------------------------------

def _dvol_series(path: str = DVOL_PATH) -> pd.Series:
    """读取 dvol_history.json → 日频 tz-naive Series(index=date, value=DVOL close)。
    与 options_study._dvol_series 同实现。"""
    with open(path) as f:
        series = json.load(f)["series"]
    idx = pd.to_datetime([datetime.datetime.utcfromtimestamp(ts / 1000).date()
                          for ts, _ in series])
    s = pd.Series([v for _, v in series], index=idx).sort_index()
    return s[~s.index.duplicated(keep="last")]


def rolling_pct(x: pd.Series, window: int = 1460) -> pd.Series:
    """滚动 window 天分位数(严格 `<`, 与 scoring._percentile_score /
    options_study.rolling_pct 口径一致), min_periods=365(≈1年暖机)。"""
    return x.rolling(window, min_periods=365).apply(
        lambda w: (w < w.iloc[-1]).sum() / len(w) * 100, raw=False)


def realized_vol(price: pd.Series, n: int = 30) -> pd.Series:
    """后向 n 日 log-return 年化 RV(小数, 如 0.6 = 60%)。项目里原本无 RV 计算,
    此处现算 —— 只在本研究脚本内, 不进运行时评分管线。"""
    logret = np.log(price / price.shift(1))
    return logret.rolling(n).std() * np.sqrt(365)


# ------------------------------------------------------------
# IC / 聚簇 计算块
# ------------------------------------------------------------

def _ic_block(score_pct: pd.Series, price: pd.Series) -> dict:
    """一条分位序列 → fwd30/60/90 Spearman IC + 机械门判定。"""
    df = ev.spearman_ic(score_pct.dropna(), price, HORIZONS)
    out, ics = {}, []
    for h, row in zip(HORIZONS, df.itertuples(index=False)):
        ic = None if pd.isna(row.IC) else round(float(row.IC), 3)
        out[f"fwd{h}"] = {"ic": ic, "n": int(row.样本数)}
        if ic is not None:
            ics.append(ic)
    strong = any(abs(i) >= IC_THRESHOLD for i in ics)
    consistent = len({i > 0 for i in ics}) == 1 if ics else False
    out["max_abs_ic"] = round(max(abs(i) for i in ics), 3) if ics else None
    out["sign_consistent"] = consistent
    out["verdict"] = "score" if (strong and consistent) else "display_only"
    if len(score_pct.dropna()):
        s = score_pct.dropna()
        out["pct_span"] = f"{s.index[0].date()} → {s.index[-1].date()}"
    return out


def _cluster_report(pct_series: pd.Series, thresh: float = 80.0) -> dict:
    """高分位(≥thresh)日的时间聚簇: 连续段数 vs 总天数 → 有效自由度比例。
    揭示表面样本量(名义 n)相对独立波动 regime 数的膨胀倍率。"""
    hi = (pct_series >= thresh).astype(int).dropna()
    total_hi = int(hi.sum())
    runs = int(((hi.diff() == 1) & (hi == 1)).sum()
               + (1 if len(hi) and hi.iloc[0] == 1 else 0))
    years = sorted(set(pct_series[pct_series >= thresh].dropna().index.year.tolist()))
    return {"high_days": total_hi, "n_clusters": runs, "years": years,
            "eff_dof_ratio": round(runs / total_hi, 3) if total_hi else None}


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------

def run() -> dict:
    dvol = _dvol_series()
    iv = dvol / 100.0
    price = ds.fetch_coinmetrics()["price"].dropna()
    common = iv.index.intersection(price.index)

    results = {
        "study": "vrp",
        "dvol_span": f"{dvol.index[0].date()} → {dvol.index[-1].date()}",
        "price_span": f"{price.index[0].date()} → {price.index[-1].date()}",
        "iv_price_overlap": {
            "n": int(len(common)),
            "span": f"{common.min().date()} → {common.max().date()}",
        },
        "by_rv_window": {},
    }

    # 各 RV 窗口: VRP_var / VRP_vol / 纯RV 对照 + 聚簇诊断
    for rv_n in RV_WINDOWS:
        rv = realized_vol(price, rv_n)
        aligned = pd.DataFrame({"iv": iv, "rv": rv}).dropna()
        vrp_var = aligned["iv"] ** 2 - aligned["rv"] ** 2
        vrp_vol = aligned["iv"] - aligned["rv"]
        vrp_var_pct = rolling_pct(vrp_var)
        results["by_rv_window"][f"rv{rv_n}"] = {
            "vrp_var": _ic_block(vrp_var_pct, price),
            "vrp_vol": _ic_block(rolling_pct(vrp_vol), price),
            "rv_only": _ic_block(rolling_pct(rv), price),
            "cluster_vrp_var_hi": _cluster_report(vrp_var_pct),
            "vrp_var_stats": {"neg_frac": round(float((vrp_var < 0).mean()), 3),
                              "mean": round(float(vrp_var.mean()), 4)},
        }

    # 校准: 纯 DVOL/IV 分位, 须复现 options_study.json (+0.050/+0.025/−0.039)
    results["calibration_iv_dvol"] = _ic_block(rolling_pct(iv), price)

    # ------------------------------------------------------------
    # 机械门 vs 稳健性 —— 可审计地推导最终 disposition
    # ------------------------------------------------------------
    var_verdicts = {n: results["by_rv_window"][f"rv{n}"]["vrp_var"]["verdict"]
                    for n in RV_WINDOWS}
    mechanical_pass = any(v == "score" for v in var_verdicts.values())
    window_dependent = len(set(var_verdicts.values())) > 1  # 判定随 RV 窗翻转
    best_vrp_ic = max(
        (results["by_rv_window"][f"rv{n}"]["vrp_var"]["max_abs_ic"] or 0.0)
        for n in RV_WINDOWS)
    best_rv_only_ic = max(
        (results["by_rv_window"][f"rv{n}"]["rv_only"]["max_abs_ic"] or 0.0)
        for n in RV_WINDOWS)
    outcompeted_by_rv = best_rv_only_ic >= best_vrp_ic  # 零期权依赖的纯RV 是否≥VRP
    min_eff_dof = min(
        (results["by_rv_window"][f"rv{n}"]["cluster_vrp_var_hi"]["eff_dof_ratio"] or 1.0)
        for n in RV_WINDOWS)

    robust = mechanical_pass and (not window_dependent) and (not outcompeted_by_rv)
    verdict = "score" if robust else "display_only"

    results["mechanical_gate"] = {
        "pass": mechanical_pass,
        "vrp_var_verdict_by_window": var_verdicts,
        "note": ("任一 RV 窗的 VRP_var 过 |IC|≥0.08+符号一致 即机械通过; "
                 "机械门必要非充分。"),
    }
    results["robustness"] = {
        "window_dependent": window_dependent,
        "best_vrp_max_abs_ic": round(best_vrp_ic, 3),
        "best_rv_only_max_abs_ic": round(best_rv_only_ic, 3),
        "outcompeted_by_pure_rv": outcompeted_by_rv,
        "min_eff_dof_ratio": min_eff_dof,
    }
    results["verdict"] = verdict
    results["verdict_reason"] = (
        "机械门表面通过(VRP_var RV=30d/60d 过 |IC|≥0.08+符号一致), 但稳健性三重"
        "否决: (1)窗口依赖 —— VRP_var 判定随 RV 窗 30/60(score)↔90(display_only)"
        "翻转; (2)非 IID —— 高 VRP 日仅 ~10-13 个独立波动 regime(eff_dof 比例见"
        "robustness.min_eff_dof_ratio), 名义 n≈1460 是幻觉, 无统计功效; (3)被反超"
        " —— 纯 RV(零期权依赖, 15y 历史)max|IC| ≥ VRP, 期权数据无净增量。方向为"
        "'应力持续走弱'与'收溢价'叙事相反。→ 维持 display_only, 不进战术分。"
    )
    results["honest_note"] = (
        "高置信结论是'不该现在计分', 非'已证明无方差溢价'(absence of evidence ≠ "
        "evidence of absence)。trailing-RV 是 E_t[未来RV] 的后视代理, 掺入波动动量;"
        "真 VRP 需 HAR-RV 之类前瞻化。有效自由度仅 ~10-13, 任何 PASS/FAIL 统计功效"
        "都不足。此结论与 DVOL(options_study.json) 同款小样本/非IID硬约束。"
    )
    results["cross_ref"] = {
        "options_study": "output/options_study.json (DVOL 水平, display_only)",
        "scratchpad_origin": "scratchpad/vrp_study.py (本研究首跑草稿, 已提升为本脚本)",
    }

    _print_table(results)

    os.makedirs(OUT, exist_ok=True)
    try:
        with open(os.path.join(OUT, "vrp_study.json"), "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1, default=str)
    except Exception as e:
        print(f"⚠️ 落盘 output/vrp_study.json 失败(不影响 run() 结果): {e}")

    return results


def _print_table(r: dict) -> None:
    print("=" * 72)
    print("VRP 分位 IC 研究 (方差风险溢价 → 前瞻 BTC 收益)")
    print("=" * 72)
    print(f"DVOL 原始:  {r['dvol_span']}")
    print(f"BTC 价格 :  {r['price_span']}")
    print(f"IV∩价格 :  {r['iv_price_overlap']['span']} (n={r['iv_price_overlap']['n']})")
    for rv_n in RV_WINDOWS:
        blk = r["by_rv_window"][f"rv{rv_n}"]
        print("-" * 72)
        print(f"### RV 窗口 = {rv_n}d   (VRP_var 均值 "
              f"{blk['vrp_var_stats']['mean']:+.4f}, 负值 "
              f"{blk['vrp_var_stats']['neg_frac']*100:.1f}%)")
        for name, label in (("vrp_var", "VRP_var"), ("vrp_vol", "VRP_vol"),
                            ("rv_only", "纯RV对照")):
            b = blk[name]
            cells = "  ".join(
                f"{h}d={b[f'fwd{h}']['ic']:+.3f}" if b[f'fwd{h}']['ic'] is not None
                else f"{h}d=  -  " for h in HORIZONS)
            print(f"  {label:<8} {cells}   max|IC|={b['max_abs_ic']} "
                  f"sign_consistent={b['sign_consistent']} → {b['verdict']}")
        c = blk["cluster_vrp_var_hi"]
        print(f"  聚簇 VRP_var≥80分位: {c['high_days']}天/{c['n_clusters']}段 "
              f"(eff_dof≈{c['eff_dof_ratio']}) 年份={c['years']}")
    print("-" * 72)
    cal = r["calibration_iv_dvol"]
    cal_cells = "  ".join(f"{h}d={cal[f'fwd{h}']['ic']:+.3f}" for h in HORIZONS)
    print(f"校准(纯DVOL/IV, 应≈+0.050/+0.025/−0.039): {cal_cells}")
    print("-" * 72)
    print(f"机械门 pass: {r['mechanical_gate']['pass']}  "
          f"(VRP_var 各窗判定 {r['mechanical_gate']['vrp_var_verdict_by_window']})")
    print(f"稳健性: 窗口依赖={r['robustness']['window_dependent']}  "
          f"被纯RV反超={r['robustness']['outcompeted_by_pure_rv']} "
          f"(VRP {r['robustness']['best_vrp_max_abs_ic']} vs RV "
          f"{r['robustness']['best_rv_only_max_abs_ic']})")
    print(f"最终 verdict: {r['verdict'].upper()}")
    print("-" * 72)
    print("⚠️  " + r["verdict_reason"])
    print("=" * 72)


if __name__ == "__main__":
    run()
