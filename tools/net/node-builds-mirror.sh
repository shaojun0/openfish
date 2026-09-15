#!/usr/bin/env bash
# Node.js 预编译镜像同步 —— 从 nodejs.org/dist 拉取指定版本到 NODE_BUILDS_DIR。
#
#   ./node-builds-mirror.sh 20.11.0 22.14.0
#   PLATFORMS="linux-x64 linux-arm64 darwin-arm64" ./node-builds-mirror.sh 20.11.0
#
# 默认只同步 linux-x64 / linux-arm64（内网服务器最常见的两个平台），可用
# PLATFORMS 覆盖。顺带抓取 index.json / index.tab（供 nvm / fnm 列出可用版本）
# 与各版本的官方 SHASUMS256.txt（存在时服务端原样返回，避免重新计算哈希）。
#
# 上游可用 NODEJS_DIST_MIRROR 覆盖，例如国内镜像：
#   NODEJS_DIST_MIRROR=https://npmmirror.com/mirrors/node ./node-builds-mirror.sh 20.11.0
set -euo pipefail

DEST="${NODE_BUILDS_DIR:-node-builds}"
UPSTREAM="${NODEJS_DIST_MIRROR:-https://nodejs.org/dist}"
PLATFORMS="${PLATFORMS:-linux-x64 linux-arm64}"

if [ "$#" -eq 0 ]; then
  echo "用法: $0 <版本> [版本...]（例如 20.11.0 22.14.0）" >&2
  exit 1
fi

mkdir -p "$DEST"

# 版本清单：缺失时服务端会根据磁盘上实际存在的版本自行生成，所以这里失败不致命。
for doc in index.json index.tab; do
  if curl -fsSL "$UPSTREAM/$doc" -o "$DEST/$doc"; then
    echo "==> $doc"
  else
    echo "跳过 $doc（上游没有或不可达）" >&2
  fi
done

for version in "$@"; do
  tag="v${version#v}"
  dir="$DEST/$tag"
  mkdir -p "$dir"
  echo "==> $tag"
  for platform in $PLATFORMS; do
    fetched=""
    for ext in tar.xz tar.gz zip; do
      file="node-$tag-$platform.$ext"
      if curl -fsSL "$UPSTREAM/$tag/$file" -o "$dir/$file"; then
        echo "    $file"
        fetched="$file"
        break
      fi
      rm -f "$dir/$file"
    done
    [ -n "$fetched" ] || echo "    未找到 $tag/$platform（跳过）" >&2
  done
  # 官方校验和：能拿到就存下来，服务端会原样返回。
  curl -fsSL "$UPSTREAM/$tag/SHASUMS256.txt" -o "$dir/SHASUMS256.txt" \
    || echo "    $tag 无 SHASUMS256.txt（服务端将自行生成）" >&2
done

echo "完成，镜像目录：$DEST"
echo "客户端接入：export NVM_NODEJS_ORG_MIRROR=http://<openfish>/node-builds"
