#!/usr/bin/env bash
# ============================================================================
# 在「openfish 仓库（权威） <-> DSH 部署目录（构建用）」之间同步插件源码。
# ============================================================================
# 为什么需要它：插件源码有两份
#   * integrations/dsh-plugin-enterprise-intranet/   —— 纳入 git，权威
#   * <部署目录>/plugin/                             —— Docker 构建上下文
# Docker 不能 COPY 构建上下文之外的文件，所以构建那份必须是真实文件，
# 不能是符号链接。与其让两份悄悄漂移，不如给一条明确的同步命令。
#
# 用法：
#   ./sync-to-deployment.sh                     # 权威 -> 部署（默认）
#   ./sync-to-deployment.sh --from-deployment   # 部署 -> 权威
#   ./sync-to-deployment.sh --check             # 只比对，不写；不一致则退出 1
#
# 环境变量：
#   DEPLOY_DIR  部署目录，默认 /home/linaro/dsh/enterprise-intranet
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${HERE}/dsh-plugin-enterprise-intranet"
DEPLOY_DIR="${DEPLOY_DIR:-/home/linaro/dsh/enterprise-intranet}"
DST_DIR="${DEPLOY_DIR}/plugin"

# 参与同步的文件（相对包根）从目录动态枚举。
#
# 这里原先是硬编码清单 + 一句「新增文件时记得加到这里」。它漏掉了后加的
# `lib/client.js`，而 `--check` 只遍历清单，于是把「部署副本缺一个文件」报成了
# 「两份副本一致」—— 最坏的那种错：一个专门用来发现漂移的工具，因为自己漂移了
# 而看不见漂移。改成枚举后，新增文件不需要任何人记得做任何事。
list_files() {
  ( cd "$1" 2>/dev/null || exit 0
    find . -type f -not -path '*/node_modules/*' \
      -not -name '.DS_Store' -not -name '*.swp' -not -name '*~' |
      sed 's#^\./##' | sort )
}

# `--check` 走并集：只枚举权威副本的话，部署副本多出来的文件仍然测不出来。
union_files() {
  { list_files "${SRC_DIR}"; list_files "${DST_DIR}"; } | sort -u
}

MODE="to-deployment"
case "${1:-}" in
  --from-deployment) MODE="from-deployment" ;;
  --check)           MODE="check" ;;
  "")                ;;
  *) echo "未知参数：$1（可用：--from-deployment / --check）" >&2; exit 2 ;;
esac

if [ ! -d "${SRC_DIR}" ]; then
  echo "找不到权威副本：${SRC_DIR}" >&2
  exit 1
fi

if [ "${MODE}" = "from-deployment" ] && [ ! -d "${DST_DIR}" ]; then
  echo "找不到部署副本：${DST_DIR}" >&2
  exit 1
fi

case "${MODE}" in
  to-deployment)
    [ -d "${DST_DIR}" ] || { echo "找不到部署副本：${DST_DIR}" >&2; exit 1; }
    n=0
    while IFS= read -r f; do
      [ -n "${f}" ] || continue
      install -D -m 0644 "${SRC_DIR}/${f}" "${DST_DIR}/${f}"
      n=$((n + 1))
    done < <(list_files "${SRC_DIR}")
    echo "已同步 ${n} 个文件：${SRC_DIR} -> ${DST_DIR}"
    echo "下一步：cd ${DEPLOY_DIR} && deploy/run-dsh.sh --rebuild"
    ;;
  from-deployment)
    n=0
    while IFS= read -r f; do
      [ -n "${f}" ] || continue
      install -D -m 0644 "${DST_DIR}/${f}" "${SRC_DIR}/${f}"
      n=$((n + 1))
    done < <(list_files "${DST_DIR}")
    echo "已同步 ${n} 个文件：${DST_DIR} -> ${SRC_DIR}"
    echo "下一步：git -C $(cd "${HERE}/.." && pwd) status --short"
    ;;
  check)
    rc=0
    n=0
    while IFS= read -r f; do
      [ -n "${f}" ] || continue
      n=$((n + 1))
      if [ ! -f "${SRC_DIR}/${f}" ]; then
        echo "  仅部署副本有：${f}"
        rc=1
      elif [ ! -f "${DST_DIR}/${f}" ]; then
        echo "  部署副本缺失：${f}"
        rc=1
      elif ! cmp -s "${SRC_DIR}/${f}" "${DST_DIR}/${f}"; then
        echo "  不一致：${f}"
        rc=1
      fi
    done < <(union_files)
    if [ "${rc}" -eq 0 ]; then
      echo "两份副本一致（${n} 个文件）"
    else
      echo "存在漂移；用 ./sync-to-deployment.sh 或 --from-deployment 收敛" >&2
    fi
    exit "${rc}"
    ;;
esac
