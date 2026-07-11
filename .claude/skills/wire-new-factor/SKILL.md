---
name: wire-new-factor
description: "在 BTC Compass 里新增、删除、替换一个评分因子，或调整桶成员/桶权重/成员权重时，按固定清单把\"因子名\"这个跨文件字符串契约接线到全部 6 处（indicators / runner / scoring / backfill / backtest / tests）并收口验证。当用户说\"加个因子\"\"接入 XX 指标\"\"把 YY 放进 X 桶\"\"某因子覆盖率不对/一直是灰的/分数没变\"\"改桶权重\"\"删掉某因子\"时使用。漏接任何一处不会报错，只会静默变 NaN、桶权重重归一、覆盖率悄悄下降——所以每次动因子都要走这张清单。"
---

# 接线一个评分因子

## 为什么需要这张清单

一个因子的"名字"（如 `"MVRV-Z"`、`"交易所净流(7d)"`）是一条**跨文件字符串契约**：同一个中文/英文串必须在 6 个文件里逐字节一致地出现。没有类型系统兜底，没有编译器报错——`scoring._compute_bucket_scores` 读因子时用的是

```python
ind = indicators.get(m)
if ind is None or np.isnan(ind.value):
    members_detail.append({"name": m, "score": None})
    continue          # 静默跳过, 该因子不计入
```

（`btc_web/btc_dashboard/scoring.py` L223-231）。所以只要桶成员名和 `runner` 里产出的字典键对不上（打错一个字、全角/半角括号不一致、漏注册 calc），这个因子就被**当作缺席剔除**，桶权重自动重归一，最终只表现为"覆盖率悄悄下降一点"——分数照出，无异常，无日志。这正是这张清单存在的理由：动因子时**逐处对账**，别靠记忆。

对称地，删因子时若只删了 calc 却留着桶成员名，运行时该桶永远少一票、覆盖率长期偏低但不报错。

先分清你在做哪件事，再走对应步骤：
- **新增因子** → 走 1→7 全程。
- **删除因子** → 反向做 1→6（每处删掉同一个名字），再收口。
- **只调桶权重 / 桶成员 / 成员权重** → 主要动第 3 步 `scoring.py`，但仍要跑第 6、7 步（改权重会触发 `band_stats.json` 键或分布变化，回测必须重跑）。

---

## 1. 写因子函数（`indicators_v2.py` 等）

因子函数产出一个 `IndicatorResult`（`btc_web/btc_dashboard/core.py` L92-102，字段：`name / value / score / color / status / priority / description / method / url`）。这是运行时**实时**评分的数据来源。

真实模板（`btc_web/btc_dashboard/indicators_v2.py` 的 `calc_mvrv_z`，L110-160）：

```python
def calc_mvrv_z() -> IndicatorResult:
    z, z_date = _cm_chip_last("mvrv_z")
    if z is None:
        z = _bd_last("mvrv-zscore", "mvrvZscore")
    if z is None:                          # 失败路径: 返 ⚪ 灰, value=NaN, 不造假
        return IndicatorResult(
            name="MVRV-Z", value=float('nan'), score=0, color="⚪",
            status="数据源暂不可用", priority="P0", ...)
    if z < 0:   score, label = 1, "周期底部带"
    elif z < 1: score, label = 0.5, "低估区"
    ...
    return IndicatorResult(
        name="MVRV-Z", value=round(z, 3), score=score,
        color=_score_color(score), status=f"{label} (Z={z:.2f})",
        priority="P0", url=..., description=..., method=...)
```

要点，逐条都关乎正确性：
- **失败一律返 `value=float('nan')`、`color="⚪"`、`status="数据源暂不可用"`**，绝不用上一次的值或编造一个中性值。因为 `_compute_bucket_scores` 靠 `np.isnan(ind.value)` 判定剔除——用 NaN 才能被诚实地当作"缺席"而非"中性票"，避免死数据污染桶均值。同一批网络因子失败时 `runner` 也会兜底造一个 `value=nan` 的桩（L358-362）。
- `score ∈ [-1, +1]`，越贵/越危险越负。颜色用 `_score_color(score)`（L99-107）保持全站一致。
- `name=` 里的串就是契约主体，记下它，后面每一处都要一模一样。
- **价格类"趋势伸展"因子有额外一步**：`scoring.compute_percentile_overrides`（L135-173）会用"过去 4 年滚动分位数"**覆盖**你在 calc 里写的离散 score。若你的新因子是可从价格序列推导的估值类（Mayer/幂律/Ahr999 那一类），要在这个函数的 `metrics = {...}` 字典里补一条推导公式（Ahr999 就是 2026-06 这样加进去的，L164-166），否则它只会走离散评分、和同桶其它成员口径不一致。

---

## 2. 注册进 runner 的 dispatch（`runner.py`）

`scoring` 读的是 `indicators` 这个 dict 的**键**，不是 `IndicatorResult.name`。所以必须在 `runner.run_dashboard` 里把因子塞进 dict，且键与桶成员名逐字节一致。两条注册路径按因子性质二选一：

- **纯本地 df 计算（无网络）** → 加进"第一步"直算段（`btc_web/btc_dashboard/runner.py` L310-322）：
  ```python
  indicators["趋势过滤器"] = calc_trend_filter(df)
  ```
- **要打网络 API** → 加进"第二步"的 `api_tasks` dict（L325-348，走 `ThreadPoolExecutor` 并发）：
  ```python
  "交易所净流(7d)": calc_exchange_netflow_7d,   # 需要参数就用 lambda
  "STH成本线": lambda: calc_sth_realized_price(current_price),
  ```

别忘了两件事，否则前端错乱但评分"看似正常"：
1. 顶部 `from .indicators_v2 import (...)` 把新 calc 加进导入清单（L34-41）。
2. **`_CARD_ORDER`**（L371-384）里加上这个名字。`api_tasks` 用 `as_completed` 收集，完成顺序随机，不在 `_CARD_ORDER` 里列出的因子会被追加到卡片末尾、每次刷新乱跳。

对账点：`runner` dict 键 == `IndicatorResult.name` == 后面桶成员名。三者必须同一个串。

---

## 3. 挂进因子桶（`scoring.py`）

这是因子真正"参与评分"的地方。在 `btc_web/btc_dashboard/scoring.py` 把名字加进某个桶的 `members`：

- 周期分桶：`CYCLE_BUCKETS`（L34-67），六桶：趋势伸展/链上筹码/资金流/趋势确认/矿工经济/时间周期。
- 战术分桶：`TACTICAL_BUCKETS`（L69-92），四桶：杠杆温度/动量结构/市场情绪/链上资金流。

```python
"链上筹码": {
    "weight": 0.25,
    "members": ["MVRV-Z", "STH成本线", "NUPL", "交易所余额"],
    "note": "...",
},
```

规则：
- **桶权重合计必须 = 1.0**（每个 buckets dict 内部）。加桶/删桶/改 weight 后手动核到 1.0——`test_bucket_weights_sum_to_one` 会兜底（见第 6 步），但先自己算好。
- 桶内成员默认等权。要非等权就在 `MEMBER_WEIGHTS`（L95-100）里配，键必须是**真实存在的桶名**、值必须是**该桶真实存在的成员名**（`test_member_weights_reference_real_members` 锁这条）。
- 成员名就是第 1、2 步那个串。**这里对不上 = 静默剔除**，就是本 skill 开头讲的那个坑。

---

## 4. 接回填侧（`backfill.py`）

回填负责用免费历史源，按当前公式**逐日重建**过去 N 天的双评分写进 `score_history.json`（app 启动线程幂等执行）。新因子不接这里，历史曲线里它就永远缺席、和实时评分口径漂移。命名范式固定，照抄：

1. **分档函数 `_band_xxx(v)`**（`btc_web/btc_dashboard/backfill.py` L234-282）：把原始值映射到 score，**阈值必须和第 1 步 calc 里的离散阈值一致**。例：
   ```python
   def _band_netflow7(v):   # 与 calc_exchange_netflow_7d 同阈值
       return 1 if v <= -0.8 else 0.5 if v <= -0.4 else 0 if v < 0.45 else -0.5 if v < 1.0 else -1
   ```
2. **数据源抓取 `_fetch_xxx`**（L88-227）：拉历史序列。优先复用已有的 CoinMetrics 全量抓取 `_fetch_cm_full`（L107-145，一次抓多列）；只有 CM 没有的列才单独写 `_fetch_bd_series` 走 bitcoin-data。注意 **bitcoin-data 匿名限 10 请求/小时**，能挂 CM 就别打 bd。
3. **在 `reconstruct` 的逐日循环里落一格**（L454-581）：算出当天 score，用 `_stub(name, score)` 造桩塞进 `inds`，键名 == 桶成员名：
   ```python
   nf7 = _at(netflow7, d)
   if nf7 is not None:
       inds["交易所净流(7d)"] = _stub("交易所净流(7d)", _band_netflow7(nf7))
   ```
   分位数类因子（MVRV-Z/NUPL/Puell）走的是"分位为主+绝对阈值保底"的预合成路径（L321-368），价格趋势伸展类走 `_percentile_at`（L447-452）。照最接近你因子性质的那一路复制。

改了回填口径（新因子/改阈值）后，把 `_MARKER` 版本号 bump 一下（L46），旧回填条目会被判为污染数据、下次启动自动按新口径重建。

---

## 5. 复刻回测因子（`backtest/factors.py`）

回测侧要 1:1 复刻同一分档，才能算这个因子的 IC、并让 `band_stats.json`（分档回测统计）反映真实因子集。列名必须 == 桶成员名。

- 周期因子 → `cycle_factor_scores`（`backtest/factors.py` L85-228），产出 `out["因子名"]`。
- 战术因子 → `tactical_factor_scores`（L363-418）。

阈值用向量化的 `_step_score(series, edges, scores)`（L32-45）复刻，**edges/scores 必须和第 1、4 步同源**。分位数类用 `rolling_percentile_score`（L48-64）+ `_extreme_combine`（L67-78）复刻"分位为主+绝对保底"。例（交易所净流 7d，L411-416）：

```python
net7_pct = ((cm["flow_in_ex"] - cm["flow_out_ex"]).rolling(7).sum()
            / cm["sply_ex"] * 100)
out["交易所净流(7d)"] = _step_score(net7_pct, [-0.8, -0.4, 0.45, 1.0],
                                    [1, 0.5, 0, -0.5, -1])
```

至此，**同一套阈值出现在四处**（calc / backfill `_band_` / backtest `_step_score` / 决策档位），任何一处改了没同步，回测和实时就会分裂。

---

## 6. 让测试兜底（`tests/`）

现有测试已经能自动兜住桶配置的两条硬约束，通常**不用新写测试**，改完跑一遍即可：

- `tests/test_consistency.py::test_bucket_weights_sum_to_one`（L159-163）：`CYCLE_BUCKETS` / `TACTICAL_BUCKETS` 桶权重各自合计 = 1.0。
- `tests/test_consistency.py::test_member_weights_reference_real_members`（L166-173）：`MEMBER_WEIGHTS` 的桶名/成员名都真实存在。

这两条覆盖"权重没配平""成员权重引用了打错的名字"。但它们**兜不住**"桶成员名 ↔ runner dict 键"这条契约——没有任何单元测试断言每个桶成员都有 calc 在 runner 里产出。那条只有第 7 步的运行时探针能抓。

若新因子改了档位阈值/命名，还会连带 `test_consistency.py` 里 `decision ↔ scoring ↔ score_history ↔ backtest ↔ band_stats.json` 的四处/五处同步检查（L47-152），按报错提示补齐即可。

---

## 7. 收口验证（必跑，按顺序）

因子名契约的最后一道网是运行时探针——它以 `scoring` 桶配置为准绳，逐因子检查存活，正是用来抓"接了桶没接 runner"这类静默失败的：

```bash
# a. 重跑回测, 重新生成 band_stats.json (改了因子集/阈值/权重都必须重跑)
python3 backtest/run_backtest.py

# b. 冒烟 + 四处同步一致性 (最快, 改纯逻辑先跑这个)
python3 verify.py --offline

# c. 起本地 5070 后跑完整探针: 逐因子存活 / 桶覆盖率 / 整桶死亡
PORT=5070 python3 btc_web/app.py    # 另开一个终端
python3 verify.py                   # 或 --live 打 Render 现网
```

重点看探针这段输出（`verify.py` L142-167）：它从 `scoring.CYCLE_BUCKETS/TACTICAL_BUCKETS` 取全部成员名，凡是 `indicators` 里取不到或 `value is None` 的，报 `WARN 因子失效`。**这就是漏接 runner 的因子会现形的地方**——单测全绿但探针报某因子 dead，说明桶成员名和 runner 产出对不上。同时它单独报"整桶无有效因子"，比覆盖率更快定位到哪一路信号瞎了。

---

## 完成判据

- [ ] 新因子名在 6 处逐字节一致：`IndicatorResult.name`（calc）/ `runner` dict 键 / `_CARD_ORDER` / 桶 `members` /（如需）`MEMBER_WEIGHTS` / backfill `_stub` 键 / backtest 列名。
- [ ] 四处阈值同源：calc 离散阈值 / backfill `_band_` / backtest `_step_score` /（若涉及档位）decision 档位。
- [ ] 桶权重合计 = 1.0；失败路径返 `value=nan`+`⚪` 不造假。
- [ ] `backtest/run_backtest.py` 已重跑，`band_stats.json` 已更新。
- [ ] `verify.py --offline` 绿；`verify.py` 运行时探针里新因子**存活**、目标桶覆盖率没掉。
