#!/usr/bin/env bash
# apt 内网镜像配置 —— 让 apt 使用 openfish 的 Debian 仓库。
#
#   ./apt-mirror.sh [openfish 地址] [suite] [component]
#
# openfish 提供两种用法，二选一：
#
# 1. 镜像代理（服务端配置了 DEBIAN_UPSTREAM）——dists/ 与 pool/ 按需回源，
#    与直接用公网镜像等价：
#        deb [trusted=yes] <openfish>/debian <suite> <component>
#
# 2. 本地扁平仓库（只服务 DEBIAN_DIR 里实际存在的 .deb）：
#        deb [trusted=yes] <openfish>/debian/ ./
#
# 本脚本打印两种写法与实际生效的索引地址；写文件需要 root，脚本本身不动
# /etc/apt。
set -euo pipefail

BASE="${1:-http://127.0.0.1:9090}"
SUITE="${2:-bookworm}"
COMPONENT="${3:-main}"

echo "镜像代理写法（推荐，需服务端配置 DEBIAN_UPSTREAM）："
echo "  deb [trusted=yes] ${BASE%/}/debian ${SUITE} ${COMPONENT}"
echo
echo "本地扁平仓库写法（只含本地 .deb）："
echo "  deb [trusted=yes] ${BASE%/}/debian/ ./"
echo
echo "索引地址："
echo "  ${BASE%/}/debian/Packages"
echo "  ${BASE%/}/debian/dists/${SUITE}/Release"
echo
echo "写入 /etc/apt/sources.list.d/openfish.list 后："
echo "  sudo apt update"
