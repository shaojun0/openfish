#!/usr/bin/env bash
# pip 内网源配置 —— 把 pip 的索引指向内网 openfish。
#
#   ./pip-intranet.sh [索引地址]
#
# 默认索引地址用 127.0.0.1，实际内网部署时替换成 openfish 的地址即可。
set -euo pipefail

INDEX="${1:-http://127.0.0.1:9090/simple/}"
HOST="$(printf '%s' "$INDEX" | sed -E 's#^https?://([^/:]+).*#\1#')"

pip config set global.index-url "$INDEX"
pip config set global.trusted-host "$HOST"

echo "pip index-url     -> $INDEX"
echo "pip trusted-host  -> $HOST"
