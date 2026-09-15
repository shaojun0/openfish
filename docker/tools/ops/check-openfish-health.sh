#!/usr/bin/env bash
# openfish 健康巡检 —— 打印 /health 并把关键字段挑出来。
#
#   ./check-openfish-health.sh [openfish 地址]
set -euo pipefail

BASE="${1:-http://127.0.0.1:9090}"
URL="${BASE%/}/health"

echo "GET $URL"
curl -fsS "$URL"
echo
