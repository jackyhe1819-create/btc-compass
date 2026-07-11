#!/usr/bin/env bash
# BTC Compass · 现网健康巡检 (供 launchd 定时调用, 也可手动跑)
# ─────────────────────────────────────────────────────────────
# 探 Render 现网 /api/dashboard: 逐因子存活 / 桶覆盖率 / 决策面板完整性 /
# 链上缓存新鲜度 / 评分历史回填深度。
# --skip-tests: 只探现网, 不跑本地冒烟测试 —— 避免本地未提交 WIP 污染巡检结果
# (本地配置一致性已由 pre-commit hook 在提交时把关)。
# 日志: 默认 ~/Library/Logs/btc-compass-health.log (可用 BTC_COMPASS_HEALTH_LOG 覆盖)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LOG="${BTC_COMPASS_HEALTH_LOG:-$HOME/Library/Logs/btc-compass-health.log}"
PY="${PYTHON:-python3}"
mkdir -p "$(dirname "$LOG")"

RC=0
{
  echo "════════════════ $(date '+%Y-%m-%d %H:%M:%S %Z') ════════════════"
  "$PY" verify.py --live --skip-tests || RC=$?
  echo "[exit $RC]"
  echo ""
} >> "$LOG" 2>&1

exit "$RC"
