# BTC Compass（实验分叉版）

本目录是 Claude 重构的**双评分实验版**（🧭 周期分定仓位 + ⚡ 战术分定时机）。
用户的原版在 `../btc_web`（BTC-Dashboard，端口 5050），两版本**刻意保留评分体系差异**做 A/B 对比。

- 本地端口：**5070**（`PORT=5070 python3 btc_web/app.py`）
- GitHub：https://github.com/jackyhe1819-create/btc-compass
- Render 服务名：`btc-compass`（根目录 render.yaml Blueprint）
- git remote：`origin` = btc-compass；`upstream` = 本地 `../btc_web`（用于同步原版的底盘修复）

## 关键模块（区别于原版）

| 文件 | 作用 |
|---|---|
| `btc_web/btc_dashboard/scoring.py` | 双评分引擎：因子分桶 + 滚动 4 年分位数归一化 |
| `btc_web/btc_dashboard/indicators_v2.py` | 新增因子（MVRV-Z、STH成本线、NUPL、SOPR、Puell、Hash Ribbons、稳定币增速、期货基差、趋势过滤器等），含链上慢变量 6h 缓存 |
| `btc_web/btc_dashboard/backfill.py` | 评分历史 90 天回填（幂等，app 启动线程自动执行） |
| `btc_web/btc_dashboard/decision.py` | 量化决策引擎：周期分→目标仓位（滞回换档 δ=0.05/5天确认，防边界抖动）+ 战术分→执行节奏；分档回测统计读 `data/band_stats.json`（由 backtest 生成，改档位阈值后须重跑回测同步） |

## 注意事项

- bitcoin-data.com 匿名限 **10 请求/小时**：链上指标走 6h 缓存 + 失败 30min 负缓存，不要绕过 `_cached_onchain` 直连
- 评分公式/权重改动**不要同步回原版**；数据底盘修复经用户同意后可移植
- `total_score` 字段语义 = 周期分（兼容旧前端/历史），战术分在 `tactical_score`
