---
name: verify-loop
description: "改完 BTC Compass 代码后收口验证：按改动性质自动选对 verify.py 模式，别漏跑、也别为纯逻辑改动傻等冷启动。任何时候你改了 btc_web/ 或 backtest/ 下的代码、准备提交、或要确认改动没弄坏系统时都用它——尤其当你不确定\"这次该跑 --offline 还是要起服务\"、或改了档位阈值/滞回参数、或准备上线现网时。用户会说\"验证一下\"\"收口\"\"跑一下 verify\"\"这样改行不行\"\"能提交了吗\"\"上线前检查\"。"
---

改完代码后，用这套决策树选对验证深度收口。核心原则：验证深度要匹配改动的爆炸半径——纯逻辑改动不该傻等服务冷启动，碰了运行时数据面就必须真起服务探一遍，动了档位阈值/滞回则必须先重跑回测再验证否则一致性测试会因数据过期误红。

## 先看懂 `verify.py` 有哪四种模式

`verify.py` 做两件事：`[1]` 冒烟测试（pytest `tests/`，纯离线、几秒钟）+ `[2]` 运行时探针（打一个真实运行的实例，逐因子/覆盖率/决策面板/缓存新鲜度/评分历史深度）。四个开关组合出四种用法：

| 命令 | 跑什么 | 什么时候用 |
|---|---|---|
| `python3 verify.py --offline` | 只冒烟测试，不探任何实例 | 纯逻辑/纯配置改动的最快收口 |
| `python3 verify.py` | 冒烟 + 探本地 `127.0.0.1:5070` | 碰了数据/因子/前端字段/决策面板 |
| `python3 verify.py --live` | 冒烟 + 探现网 `btc-compass.onrender.com` | 上线后确认现网真的活了 |
| `python3 verify.py --url URL` | 冒烟 + 探指定实例 | 探一个非标准地址的部署 |
| `python3 verify.py --skip-tests` | 只探实例，跳过冒烟 | 只想复查线上健康、不关心本地代码 |

退出码语义（`verify.py` 末尾）：**`0` = 全部通过（允许有 WARN），`1` = 存在 FAIL**。所以 `echo $?` 是 0 就算收口——WARN 是"注意但不拦"，FAIL 才是"必须修"。这套退出码是给人肉、CI、agent loop 共用的反馈信号，你可以直接把它当循环条件。

## 决策树：按你改了什么选模式

### 只改了纯逻辑/纯配置 → `--offline` 止步

评分公式、桶权重、决策文案、滞回判据这类**不碰运行时数据面**的改动，跑：

```bash
python3 verify.py --offline
```

**为什么止步于此**：冒烟测试跑的是 `pytest tests/`（`run_smoke_tests` 里 `-x` 遇错即停），覆盖三块——档位阈值的**四处对账**（`test_consistency.py`：`decision.py` ↔ `scoring.py` ↔ `score_history.py` ↔ `backtest/evaluate.py`）、`band_stats.json` 同步、以及评分与滞回的纯函数行为（`test_scoring.py` / `test_decision.py`）。这些全是离线纯函数校验，起服务对它们毫无增益，白等一次冷启动只是浪费你几分钟。

### 碰了 API/因子/前端字段/决策面板 → 起服务 + 默认 `verify`

只要改动会影响 `/api/dashboard` 吐出来的东西——新增/改因子（`indicators_v2.py`）、动了 `total_score`/`tactical_score`/`indicators`/覆盖率字段、改了决策面板结构（`decision.py` 的输出）、或碰了数据源/缓存——先起本地服务再跑默认 verify：

```bash
PORT=5070 python3 btc_web/app.py      # 另开一个终端，或后台跑
python3 verify.py                       # 冒烟 + 探 127.0.0.1:5070
```

**为什么必须真起服务**：冒烟测试看不见运行时。探针会拉真实的 `/api/dashboard`，逐因子核对存活（以 `scoring.CYCLE_BUCKETS`/`TACTICAL_BUCKETS` 桶配置为准绳）、核对覆盖率没跌破警戒线、决策档位在已知集合内、`band_stats` 随部署带上了、缓存在刷新、评分历史回填够深。一个因子拼错 key、一个字段改名前端对不上，只有真打一次接口才暴露得出来。若探针报 `/api/dashboard 不可用`，多半是你没起服务——回头把上面那条 `PORT=5070` 起起来。

### 动了档位阈值 / 滞回参数 → 先重跑回测，再 verify

改了 `decision.py` 的 `CYCLE_BANDS`/`TACTICAL_BANDS` 边界，或 `HYST_DELTA`/`HYST_CONFIRM`——**先**重生成 `band_stats.json`，**再**验证：

```bash
cd backtest && python3 run_backtest.py    # 重生成 btc_web/btc_dashboard/data/band_stats.json
cd .. && python3 verify.py --offline       # 阈值只碰逻辑就 --offline; 若还改了面板输出用默认 verify
```

**为什么顺序不能反**：`test_consistency.py` 会把 `decision.HYST_DELTA`/`HYST_CONFIRM` 和 `band_stats.json` 里的 `hysteresis` 逐字段对账，还要求 `band_stats` 的档位键与 `decision.CYCLE_BANDS`/`TACTICAL_BANDS` 严格互为全集。你改了阈值却没重跑回测，`band_stats.json` 就还是旧档位——一致性测试立刻红，而且红的是"数据过期"这种误导性原因，不是你代码本身有错。先重跑回测让数据追上代码，再 verify 才干净。（这也是 `CLAUDE.md` 里"改档位阈值后须重跑回测同步"那条纪律的机器化版本。）

### 上线前 / 部署后 → `--live`

推到 Render 之后，确认现网真的活了：

```bash
python3 verify.py --live      # 探 btc-compass.onrender.com; 自带冷启动重试
```

**为什么单独跑**：现网可能在 free 层冷启动、首轮指标还在算，探针内建了重试窗口来容忍这种"计算中"状态。本地全绿不代表部署带对了 `band_stats.json`、现网数据源没挂——`--live` 是唯一能看到线上真实健康的一档。

## 你和 pre-commit hook 的关系：兜底 ≠ 替代

本仓库装了 `scripts/hooks/pre-commit`（走 `core.hooksPath=scripts/hooks`）。**每次 `git commit`**，只要暂存区含 `.py` 或 `band_stats.json`，它会自动跑 `verify.py --offline`，红了就拦下提交。

这意味着提交前你已有一层离线兜底——但**别把它当你唯一的验证**：

- hook 只跑 `--offline`，**看不见运行时**。你改了因子/字段却只等 hook，等于跳过了运行时探针——bug 会活到线上。碰数据面的改动，开发中途就该主动起服务跑默认 `verify`，早暴露早修，而不是攒到 commit 才发现。
- 开发中途主动 verify 定位更快：一次改动刚写完就 `--offline`，红了立刻知道是这一处；攒一堆再靠 hook 拦，得回头翻是哪一改动惹的。
- hook 被 `git commit --no-verify` 能绕过——只在你确知本次改动与评分完全无关时才用。绕过了就更该自己补一次 verify。

## 收口判据

- **纯逻辑/配置改动**：`python3 verify.py --offline` 退出码 0（无 FAIL）。
- **碰运行时数据面**：`PORT=5070 python3 btc_web/app.py` 起着，`python3 verify.py` 退出码 0，且探针里因子存活/覆盖率/决策面板均无 FAIL。
- **改了阈值/滞回**：已先 `cd backtest && python3 run_backtest.py` 重生成 `band_stats.json`，再 verify 绿。
- **上线后**：`python3 verify.py --live` 退出码 0。

看到 WARN 不必拦，但读一眼——它常是"覆盖率在滑""缓存变慢""评分历史还浅"这类早期劣化信号，比等 FAIL 划算。
