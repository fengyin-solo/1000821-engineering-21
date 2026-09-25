#!/usr/bin/env bash
# 结算试算本地一体化检查的便捷入口：python3 scripts/devcheck/settle_check.py "$@"
set -euo pipefail
cd "$(dirname "$0")/../.."
exec python3 scripts/devcheck/settle_check.py "$@"
