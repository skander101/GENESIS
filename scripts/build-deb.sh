#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
VERSION=${VERSION:-0.1.0}
ARCH=${ARCH:-$(dpkg --print-architecture)}
DIST_DIR="$REPO_ROOT/dist"
BUILD_ROOT="$DIST_DIR/genesis-council_${VERSION}_${ARCH}"
PACKAGE="$DIST_DIR/genesis-council_${VERSION}_${ARCH}.deb"

rm -rf "$BUILD_ROOT"
mkdir -p "$BUILD_ROOT/DEBIAN" "$BUILD_ROOT/usr/bin" "$BUILD_ROOT/usr/lib/genesis" "$BUILD_ROOT/usr/share/applications"

install -m 0755 "$REPO_ROOT/genesis_launcher.py" "$BUILD_ROOT/usr/bin/genesis"
install -m 0644 "$REPO_ROOT/council.py" "$BUILD_ROOT/usr/lib/genesis/council.py"
install -m 0644 "$REPO_ROOT/packaging/genesis.desktop" "$BUILD_ROOT/usr/share/applications/genesis.desktop"

sed "s/^Version: .*/Version: $VERSION/" "$REPO_ROOT/packaging/debian/control" > "$BUILD_ROOT/DEBIAN/control"
sed "s/^Architecture: .*/Architecture: $ARCH/" "$BUILD_ROOT/DEBIAN/control" > "$BUILD_ROOT/DEBIAN/control.tmp"
mv "$BUILD_ROOT/DEBIAN/control.tmp" "$BUILD_ROOT/DEBIAN/control"
chmod 0755 "$BUILD_ROOT/DEBIAN"

mkdir -p "$DIST_DIR"
dpkg-deb --build --root-owner-group "$BUILD_ROOT" "$PACKAGE" >/dev/null
printf 'Built %s\n' "$PACKAGE"
