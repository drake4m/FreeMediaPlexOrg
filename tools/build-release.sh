#!/bin/bash
# Build the release tarball from the committed tree (never from uncommitted files):
#
#   bash tools/build-release.sh      -> dist/freemediaplexorg-X.Y.Z.tar.gz and dist/freemediaplexorg-X.Y.Z.tar.gz.sha256
#
# The archive extracts to freemediaplexorg-X.Y.Z/ and holds what `git archive` exports (see .gitattributes).
# File times are the commit time and gzip stores no name or time, so the same commit gives the same bytes
# with the same gzip version.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
die() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

VER=$(sed -n 's/^__version__ = "\([0-9][0-9.]*[0-9a-z.]*\)"$/\1/p' mediaorg/__init__.py)
[ -n "$VER" ] || die "no __version__ found in mediaorg/__init__.py"
git rev-parse --git-dir >/dev/null 2>&1 || die "not a git checkout"
[ -z "$(git status --porcelain --untracked-files=no)" ] || die "uncommitted changes: commit them first"
grep -q "^## \[$VER\]" CHANGELOG.md || die "CHANGELOG.md has no entry for $VER"
TAG=$(git describe --tags --exact-match HEAD 2>/dev/null || true)
if [ -n "$TAG" ] && [ "$TAG" != "v$VER" ]; then
  die "HEAD is tagged $TAG but the version is $VER"
fi

NAME="freemediaplexorg-$VER"
mkdir -p dist
git archive --format=tar --prefix="$NAME/" HEAD | gzip -n -9 > "dist/$NAME.tar.gz.tmp"
mv "dist/$NAME.tar.gz.tmp" "dist/$NAME.tar.gz"
(cd dist && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256")

echo "built dist/$NAME.tar.gz from $(git rev-parse --short HEAD)${TAG:+ ($TAG)}"
cat "dist/$NAME.tar.gz.sha256"
