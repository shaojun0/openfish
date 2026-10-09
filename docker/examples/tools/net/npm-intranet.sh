#!/usr/bin/env bash
# npm 内网源配置 —— 把 npm 指向 openfish 的 registry 协议。
#
#   ./npm-intranet.sh [registry 地址]
#
# openfish 已实现 npm registry 协议（packument / manifest / tarball /
# /-/v1/search）：本地没有的包会按 NPM_UPSTREAM 回源并缓存，所以这条命令
# 之后 npm install / view / search 都可以直接用。
set -euo pipefail

REGISTRY="${1:-http://127.0.0.1:9090/npm/}"

npm config set registry "$REGISTRY"
echo "npm registry -> $(npm config get registry)"
