#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# 依赖布局两种都支持：
# 1) 常规机器：.venv（ensurepip 可用时用 python3 -m venv 创建）
# 2) 精简镜像（没有 python3-venv、只有系统 python）：.pydeps 目标目录
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
  export PIP="${PY} -m pip"
else
  PY=python3
  export PIP="python3 -m pip --break-system-packages"
fi

if [ ! -d .venv ] && [ ! -d .pydeps ]; then
  if python3 -m venv .venv 2>/dev/null; then
    .venv/bin/pip install -q -r requirements.txt
    PY=.venv/bin/python
  else
    # ensurepip 不可用（如 Debian 未装 python3.11-venv）时退回到目标目录安装
    python3 -m pip install -q --target .pydeps -r requirements.txt --break-system-packages
  fi
fi
[ -d .pydeps ] && export PYTHONPATH="${PWD}/.pydeps${PYTHONPATH:+:${PYTHONPATH}}"

# 结算示例数据默认从 settlement-fixtures/cases.json 装载（应用层默认即如此）；
# 需要空表排障时用 SETTLE_DEVDATA=0 ./run.sh。端口允许覆盖，方便核对流水线起隔离实例。
export SETTLE_DEVDATA="${SETTLE_DEVDATA:-1}"
PORT="${PORT:-8000}"
exec ${PY} -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT}"
