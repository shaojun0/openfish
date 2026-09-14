#!/usr/bin/env bash
# 生成一个最小的示例 .deb，用来演示 /debian/Packages 静态索引与下载链路。
#
# 真实的内网包直接把 .deb 放进 debian/ 即可，不需要这个脚本。
# 重新生成：  ./scripts/build-sample-deb.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$REPO_ROOT/debian"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PKG=openfish-hello
VER=1.0.0
ARCH=all

mkdir -p "$OUT_DIR" "$WORK/$PKG/DEBIAN" "$WORK/$PKG/usr/share/openfish"

cat > "$WORK/$PKG/DEBIAN/control" <<EOF
Package: $PKG
Version: $VER
Architecture: $ARCH
Maintainer: openfish <openfish@example.invalid>
Section: misc
Priority: optional
Description: openfish hub sample package
 A minimal package that proves the flat apt repository wiring works.
EOF

cat > "$WORK/$PKG/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
echo "openfish-hello installed"
EOF
chmod 755 "$WORK/$PKG/DEBIAN/postinst"

echo "hello from openfish" > "$WORK/$PKG/usr/share/openfish/hello.txt"

dpkg-deb --build --root-owner-group "$WORK/$PKG" "$OUT_DIR/${PKG}_${VER}_${ARCH}.deb"
echo "built: $OUT_DIR/${PKG}_${VER}_${ARCH}.deb"
