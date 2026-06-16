#!/usr/bin/env bash
# Build the SAME single distribution PyPI ships: a platform wheel with the
# maturin-built native module merged into the pure hatchling wheel. This is the
# local equivalent of the build-pure + build-native + repack steps in
# .github/workflows/publish.yml, so local installs match PyPI (never the bare
# py3-none-any fallback + a separately-installed cluxion-queue-native).
#
# Usage:  bash scripts/build_local_wheel.sh
# Output: dist-merged/cluxion_agentplugin_preprocessing-<ver>-<platform>.whl
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

echo "[1/4] clean previous merged output"
rm -rf dist-merged
mkdir -p dist-merged

echo "[2/4] build pure py3-none-any wheel (hatchling)"
uv run --extra dev python -m build --wheel --outdir dist >/dev/null

echo "[3/4] build native abi3 wheel (maturin --release --strip)"
( cd rust/cluxion_queue && maturin build --release --strip --out dist >/dev/null )

PURE="$(ls -t dist/cluxion_agentplugin_preprocessing-*-py3-none-any.whl | head -1)"
NATIVE="$(ls -t rust/cluxion_queue/dist/cluxion_queue_native-*.whl | head -1)"
echo "      pure   = $PURE"
echo "      native = $NATIVE"

echo "[4/4] merge native into pure wheel + retag (repack_native_wheel.py)"
uv run --no-project --with wheel python scripts/repack_native_wheel.py \
  --pure-wheel "$PURE" \
  --native-wheel "$NATIVE" \
  --out dist-merged/

echo
echo "MERGED WHEEL(S):"
ls -la dist-merged/
