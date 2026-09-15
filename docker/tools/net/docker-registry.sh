#!/usr/bin/env bash
# Docker Registry v2 拉取代理配置 —— 让 docker pull 走内网 openfish。
#
#   ./docker-registry.sh [openfish 地址] [registry 别名]
#
# openfish 提供只读的 Registry v2 拉取代理（/docker/v2/*），回源地址由服务端
# 的 DOCKER_UPSTREAM 决定；客户端把镜像名指向本服务即可，例如：
#
#     docker pull <openfish>/library/alpine:latest
#
# 若 openfish 以 http 明文暴露，需要把它加入 docker daemon 的
# insecure-registries 并重启 docker（见下方提示）。正式环境请让 openfish 走 TLS。
set -euo pipefail

BASE="${1:-http://127.0.0.1:9090}"
ALIAS="${2:-openfish}"
AUTHORITY="$(printf '%s' "$BASE" | sed -E 's#^https?://##; s#/.*$##')"

echo "registry 基址 : ${BASE%/}/docker"
echo "示例拉取      : docker pull ${ALIAS}/library/alpine:latest"
echo
echo "若 openfish 是 http 明文，需要 root 执行："
echo "  /etc/docker/daemon.json 中加："
echo "    { \"insecure-registries\": [\"${AUTHORITY}\"] }"
echo "  systemctl restart docker"
echo
echo "若服务端要求凭据（AUTH_USERNAME / API key）："
echo "  docker login ${AUTHORITY}"
