#!/usr/bin/env bash
# npm 内网源配置（脚手架）。
#
#   ./npm-intranet.sh [registry 地址]
#
# 注意：npm 反向代理尚未在 openfish 中实现，这里只是先把 registry 指过去，
# 等代理接入后该地址即生效；当前 /npm/ 只提供本地目录清单页面。
set -euo pipefail

REGISTRY="${1:-http://127.0.0.1:9090/npm/}"

npm config set registry "$REGISTRY"
echo "npm registry -> $(npm config get registry)"
