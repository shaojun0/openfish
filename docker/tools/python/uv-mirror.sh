#!/usr/bin/env bash
# uv CPython 镜像 —— 让 uv 从内网 openfish 下载预编译解释器。
#
#   source ./uv-mirror.sh [openfish 地址]
#
# 用 source 才会把环境变量留在当前 shell；直接执行只打印用法。
set -euo pipefail

BASE="${1:-http://127.0.0.1:9090}"
export UV_PYTHON_INSTALL_MIRROR="${BASE%/}/python-builds/"

echo "UV_PYTHON_INSTALL_MIRROR=$UV_PYTHON_INSTALL_MIRROR"
echo
echo "验证： uv python install 3.12"
