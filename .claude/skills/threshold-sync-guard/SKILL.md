---
name: threshold-sync-guard
description: "在改动 BTC Compass 的档位阈值、仓位区间、档名或滞回参数（HYST_DELTA/HYST_CONFIRM）时使用。同一组阈值散落在 6 处源文件 + 1 个数据文件里，漏改任何一处会导致前端文案/回测/决策静默不一致（一致性测试才会红）。当用户说\"改重仓区阈值 / 调仓位区间 / 改档名 / 改滞回 δ 或确认天数 / 加一档 / 重标定阈值 / 改 CYCLE_BANDS 或 TACTICAL_BANDS\"时触发本 skill，按序改齐并验证。"
---

## 这个 skill 解决什么

BTC Compass 的一组档位阈值（周期分/战术分的分界点、仓位区间、档名、band_stats 键）以及滞回参数（`HYST_DELTA` / `HYST_CONFIRM`）被**刻意冗余地写在多处**——有些是数据表元组，有些硬编码在中文推荐文案的 `if/elif` 里，有一处是回测里的裸字面量，还有一处藏在触发价位表的边界标签里。改一个阈值只改一处，程序不会报错，但看板文案、决策档位、触发价位表、回测统计会各说各话，直到跑一致性测试才暴露——**而且其中一处（triggers.py）连一致性测试都盖不住，只能人工核对**。

本 skill 的职责：**先定位全部同源点 → 按同一套值改齐 → 先重跑回测再跑测试 → verify 收口**。任何一步顺序错了都会给出误导性的红/绿信号。

## 触发场景

用户提出下列任一改动时，走本流程，不要只改看得见的那一处：

- 改某档的分界阈值（如"重仓区从 0.45 提到 0.50"）
- 改某档的仓位区间（如"标准配置从 40-60% 改成 45-65%"）
- 改档名（如"减配"改叫"轻仓"）
- 改滞回参数 `HYST_DELTA`（δ）或 `HYST_CONFIRM`（确认天数/快照数）
- 增删档位、整体按新分布重标定阈值

## 第一步：定位全部同源点（改前先读，别凭记忆）

周期分档位阈值散落在 **5 处源定义 + 1 处数据产物**；连同战术分与滞回，整个阈值家族横跨 **6 个源文件**（decision.py / scoring.py / score_history.py / evaluate.py / run_backtest.py / **triggers.py**）+ band_stats.json 数据文件。动手前先把相关的每一处都 Read 出来对齐，确认当前值一致，再统一改。

### 周期分档位（CYCLE_BANDS 家族）

| # | 文件 | 符号 / 位置 | 形状与含义 |
|---|---|---|---|
| 1 | `btc_web/btc_dashboard/decision.py` | `CYCLE_BANDS`（约 L43-51） | **6 元组** `(下界, 档名, 仓位下限%, 仓位上限%, 目标中值%, band_stats键)`，如 `(0.45, "重仓区", 80, 100, 90, "重仓区 80-100%")` |
| 2 | `btc_web/btc_dashboard/scoring.py` | `cycle_recommendation()`（约 L251-272） | **阈值和仓位区间硬编码在中文推荐文案里**——一串 `if score >= 0.45: return "重仓区 · 建议仓位 80-100%"`。**这是最容易漏的一处**：它不是数据表，是字符串字面量，全文搜档名/数字才找得到 |
| 3 | `btc_web/btc_dashboard/score_history.py` | `_BANDS`（约 L49-57） | **2 元组** `(下界, 档名)`，如 `(0.45, "重仓区")`，比 decision 少了仓位/键字段 |
| 4 | `backtest/evaluate.py` | `CYCLE_BANDS`（约 L16-24） | **4 元组，形状与 decision 不同** `(下界, 上界, band_stats键/label, 仓位小数)`，如 `(0.45, float("inf"), "重仓区 80-100%", 0.90)`。**关键：这里显式带了"上界"，且上界 = 上一档的下界**（首尾相接无缝隙）。仓位是**小数**（0.90）不是百分数，且中值可能有舍入（decision 的 `22` 对应 evaluate 的 `0.225`，测试允许 ≤0.5% 差） |
| 5 | `btc_web/btc_dashboard/triggers.py` | `_BAND_EDGES`（约 L29-34） | **3 元组** `(阈值, "进X档 (仓位区间)", 方向up/down)`，如 `(0.30, "进偏多配置档 (60-80%仓)", "up")`。触发价位表用它反解"什么价格会翻转档位"。**它同时内嵌了阈值、仓位区间字符串和档名**——改阈值/仓位区间/档名三种改动都要动它。⚠️ 它**只覆盖 4 条 up/down 越界边界**（0.30/0.15 向上、0.00/-0.30 向下），不含 0.45 顶档，也不是全 7 档，别以为对齐 4 行就够；档名/仓位区间字符串每一处都得同步。**最危险的一点见下：这处不被一致性测试守护。** `compute_trigger_levels` 由 `runner.py:404` 实际调用，是活代码不是死代码 |

改阈值时，evaluate.py 的每档要同时改**本档下界**和**下一档的上界**两个数字，否则 `test_cycle_bands_contiguous_in_backtest` 会红。

> **triggers.py 是唯一没有测试兜底的同源点。** `tests/test_consistency.py` 完全没引用 `triggers._BAND_EDGES`——改了周期阈值/档名/仓位区间却漏改 triggers，**一致性测试照样全绿**，触发价位表却会静默算错档位边界（这正是本 skill 要防的失败模式）。所以：**第三步测试全绿 ≠ triggers 已同步**。triggers 必须靠你在第二步逐字核对，外加第四步 verify 对触发价位面板的运行时探针兜底。

### 战术分档位（TACTICAL_BANDS 家族）

| # | 文件 | 符号 | 形状 |
|---|---|---|---|
| 1 | `btc_web/btc_dashboard/decision.py` | `TACTICAL_BANDS`（约 L55-67） | **5 元组** `(下界, 档名, 执行节奏, 展开说明, band_stats键)` |
| 2 | `btc_web/btc_dashboard/scoring.py` | `tactical_recommendation()`（约 L275-291） | 阈值硬编码在文案 `if/elif` 里 |
| 3 | `backtest/evaluate.py` | `TACTICAL_BANDS`（约 L27-33） | **3 元组** `(下界, 上界, label)`，同样上界=上一档下界 |

战术分没有 score_history 档位（`_BANDS` 只覆盖周期分），也没进 triggers 的 `_BAND_EDGES`（触发价位表只反解周期档）。

### 滞回参数（HYST_DELTA / HYST_CONFIRM）

| # | 文件 | 位置 | 写法 |
|---|---|---|---|
| 1 | `btc_web/btc_dashboard/decision.py` | 约 L35-36 | `HYST_DELTA = 0.05` / `HYST_CONFIRM = 5`（模块级常量，`replay_hysteresis()` 约 L108 使用） |
| 2 | `backtest/run_backtest.py` | 约 L106 | **裸字面量写死在函数里**：`HYST_DELTA, HYST_CONFIRM = 0.05, 5`——不是 import decision 的常量，改 decision 不会自动同步 |
| 3 | `btc_web/btc_dashboard/data/band_stats.json` | `"hysteresis": {"delta":…, "confirm":…}` | **数据产物**，由 run_backtest.py 写入。不能手改，必须重跑回测才会刷新 |

滞回参数的一致性由测试用**正则解析 run_backtest.py 源码**来校验（见下），所以第 2 处的字面量必须能被 `HYST_DELTA\s*,\s*HYST_CONFIRM\s*=\s*([0-9.]+)\s*,\s*([0-9]+)` 匹配到——别把它改成别的写法。

## 第二步：按序改齐

把新阈值/参数在**上表所有源定义处**改成同一套值。逐条核对：

- 周期分改阈值：同步 decision `CYCLE_BANDS`、scoring `cycle_recommendation` 文案、score_history `_BANDS`、evaluate `CYCLE_BANDS`（本档下界 + 下一档上界）、**triggers `_BAND_EDGES`（对应的 up/down 边界阈值）** 五处。
- 改仓位区间/档名：decision 的元组 + scoring 文案字符串 + **triggers `_BAND_EDGES` 标签里内嵌的"档名 (仓位区间仓)"字符串** + band_stats 键（decision 元组第 6 项 = evaluate 第 3 项，且键里内嵌了档名和"下限-上限%"，`test_cycle_bands_match_backtest` 会断言 `档名 in 键` 且 `"{plo}-{phi}%" in 键`）。**triggers 的标签这一步尤其要盯——它不被任何测试守护。**
- 改滞回：decision `HYST_DELTA`/`HYST_CONFIRM` + run_backtest.py L106 字面量。band_stats.json 里的值**这一步先不管**，下一步重跑回测会覆盖。

`data/band_stats.json` 只有源定义改齐后重跑回测才会正确，**不要手动编辑它**。

## 第三步：先重跑回测，再跑一致性测试（顺序不能反）

```bash
cd /Users/jack/Developer/编程开发/btc_compass/backtest && python3 run_backtest.py
```

这会用新阈值重算分档前瞻收益，并把结果（含 `hysteresis` 段）写回 `btc_web/btc_dashboard/data/band_stats.json`。

**为什么必须先重跑回测**：一致性测试里有几条把 `band_stats.json` 当"事实源"来校验源代码——

- `test_hysteresis_params_match_band_stats`：断言 `band_stats["hysteresis"]["delta/confirm"] == decision.HYST_DELTA/HYST_CONFIRM`
- `test_band_stats_covers_all_bands`：断言 band_stats 的档位键集合与 decision 定义**严格互为全集**（无缺失、无改名残留）
- `test_band_stats_entries_complete`：断言每档统计条目完整

如果改了阈值/档名/滞回但**没重跑回测就直接跑测试**，band_stats.json 还是旧值，这几条**必红**——而且红的是"数据 vs 代码不一致"，会误导你以为源代码没改对。先重跑回测让数据追上代码，测试才检验的是"你这次的源码改动本身自洽"。

然后跑一致性测试：

```bash
cd /Users/jack/Developer/编程开发/btc_compass && python3 -m pytest tests/test_consistency.py -q
```

这些测试各自锁死一处同步，红了能直接定位漏改：

| 测试函数 | 锁死的同步 |
|---|---|
| `test_cycle_bands_match_backtest` | decision.CYCLE_BANDS ↔ evaluate.CYCLE_BANDS（下界、band_stats 键、目标仓位、键内嵌档名+区间） |
| `test_cycle_bands_contiguous_in_backtest` | evaluate 周期档区间首尾相接（上界=上一档下界，首档上界=inf） |
| `test_tactical_bands_match_backtest` | decision.TACTICAL_BANDS ↔ evaluate.TACTICAL_BANDS + 战术档连续 |
| `test_cycle_recommendation_matches_bands` | decision.CYCLE_BANDS ↔ scoring.cycle_recommendation 文案（档名 + "下限-上限%"） |
| `test_tactical_recommendation_matches_bands` | decision.TACTICAL_BANDS ↔ scoring.tactical_recommendation 文案 |
| `test_score_history_bands_match_decision` | decision.CYCLE_BANDS ↔ score_history._BANDS（下界、档名、档数） |
| `test_hysteresis_params_match_band_stats` | decision.HYST_* ↔ band_stats.json（需先重跑回测） |
| `test_hysteresis_params_match_backtest_source` | decision.HYST_* ↔ run_backtest.py 源码字面量（正则解析） |
| `test_band_stats_covers_all_bands` | band_stats 档位键集合 ↔ decision 档名全集 |
| `test_band_stats_entries_complete` | band_stats 每档条目字段完整 |

> **测试盖不到 triggers.py。** 上表没有任何一条校验 `triggers._BAND_EDGES`——全绿只证明 decision/scoring/score_history/evaluate/band_stats 这五处自洽，**不证明 triggers 已同步**。改完务必回头再肉眼比一次 triggers 的 4 条边界阈值与标签字符串。

红了就照测试消息回到第一步的表，补上漏改的那一处，再重跑回测 + 测试，直到全绿。

## 第四步：verify 收口

一致性测试全绿后，跑项目自带的验证收口：

```bash
cd /Users/jack/Developer/编程开发/btc_compass && python3 verify.py --offline
```

`--offline` 只跑冒烟测试（改纯阈值逻辑最快）。若想连本地看板/现网决策面板一起探针，用 `python3 verify.py`（探 5070 本地）或 `python3 verify.py --live`（探 Render 现网）。verify 的运行时探针会逐因子检查存活、覆盖率、决策面板、评分历史深度——**这也是 triggers 触发价位表唯一的自动化兜底**：跑不带 `--offline` 的 verify（或直接打开看板看触发价位面板），确认改动没把面板打挂、边界档名与主决策面板对得上。

## 完成判据

- 第一步表里 **5 个源文件的周期/战术定义 + run_backtest.py 滞回字面量 + triggers.py `_BAND_EDGES`** 都改成同一套新值（共 6 个源文件）
- `python3 run_backtest.py` 已重跑，band_stats.json 已刷新
- `pytest tests/test_consistency.py` 全绿
- **triggers.py 已人工复核**（测试盖不到）
- `verify.py`（`--offline` 起步；触发价位表相关改动建议跑不带 `--offline` 的完整探针）退出码 0

## 注意

- 评分公式/权重/阈值改动**不要同步回原版** `../btc_web`（两版刻意保留评分差异做 A/B）。
- `total_score` 字段语义 = 周期分（历史兼容），战术分在 `tactical_score`——别被字段名误导成"总分"。
