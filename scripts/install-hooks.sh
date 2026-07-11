#!/usr/bin/env bash
# 一次性把本仓库的 git hooks 挂上 (指向版本化的 scripts/hooks/)。
# 每个 clone 跑一次即可; compass 家族其它站拷同样脚本亦可复用。
# 卸载: git config --unset core.hooksPath
set -euo pipefail
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"
chmod +x scripts/hooks/* 2>/dev/null || true
git config core.hooksPath scripts/hooks
echo "✅ 已设 core.hooksPath=scripts/hooks"
echo "   已挂 hooks: $(ls scripts/hooks | tr '\n' ' ')"
