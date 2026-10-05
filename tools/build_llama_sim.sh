#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export TT_METAL_HOME="${TT_METAL_HOME:-$HOME/tt-metal}"
LLAMA_TT_DIR="${LLAMA_TT_DIR:-$HOME/llama.cpp-tt}"
TTSIM_DEPS_DIR="${TTSIM_DEPS_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/tt-blackhole-lab}"
BUILD_JOBS="${TT_LLAMA_BUILD_JOBS:-2}"
OUT_DIR="$ROOT_DIR/out/llama_first_token"
LLAMA_REVISION=04602ca95c1a6adb6c1533e024ad9c79ee6c428d

if [[ "$(git -C "$LLAMA_TT_DIR" rev-parse HEAD)" != "$LLAMA_REVISION" ]]; then
  printf 'llama.cpp must be the pinned metalium-support commit %s\n' "$LLAMA_REVISION" >&2
  exit 1
fi
for LIBRARY in /usr/lib/libtt_metal.so /usr/lib/_ttnncpp.so /usr/lib/libtt_stl.so; do
  [[ -f "$LIBRARY" ]] || { printf 'Missing runtime: %s\n' "$LIBRARY" >&2; exit 1; }
done
python3 "$ROOT_DIR/tools/prepare_ttmetal_headers.py" \
  --metal "$TT_METAL_HOME" --deps "$TTSIM_DEPS_DIR"

PATCH_FILE="$ROOT_DIR/patches/llama-blackhole.patch"
if git -C "$LLAMA_TT_DIR" apply --check "$PATCH_FILE" 2>/dev/null; then
  git -C "$LLAMA_TT_DIR" apply "$PATCH_FILE"
elif ! git -C "$LLAMA_TT_DIR" apply --reverse --check "$PATCH_FILE"; then
  printf 'The llama.cpp checkout does not match the compatibility patch\n' >&2
  exit 1
fi

mkdir -p "$TT_METAL_HOME/build/tt_metal" "$TT_METAL_HOME/build/ttnn" "$OUT_DIR"
for LIBRARY_LINK in "$TT_METAL_HOME/build/tt_metal/libtt_metal.so" "$TT_METAL_HOME/build/ttnn/_ttnncpp.so"; do
  if [[ -e "$LIBRARY_LINK" && ! -L "$LIBRARY_LINK" ]]; then
    printf 'Existing source-built library would be overwritten: %s\n' "$LIBRARY_LINK" >&2
    exit 1
  fi
done
ln -sfn /usr/lib/libtt_metal.so "$TT_METAL_HOME/build/tt_metal/libtt_metal.so"
ln -sfn /usr/lib/_ttnncpp.so "$TT_METAL_HOME/build/ttnn/_ttnncpp.so"
SMOKE_CACHE="$OUT_DIR/smoke-build/CMakeCache.txt"
FRESH_ARGS=()
if [[ -f "$SMOKE_CACHE" ]] && ! grep -q -F "CMAKE_HOME_DIRECTORY:INTERNAL=$ROOT_DIR/tools/llama-sim" "$SMOKE_CACHE"; then
  FRESH_ARGS=(--fresh)
fi
cmake "${FRESH_ARGS[@]}" -S "$ROOT_DIR/tools/llama-sim" -B "$OUT_DIR/smoke-build" \
  -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build "$OUT_DIR/smoke-build" -j "$BUILD_JOBS"
cmake -S "$LLAMA_TT_DIR" -B "$LLAMA_TT_DIR/build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DGGML_METALIUM=ON \
  -DCMAKE_CXX_FLAGS='-DDISABLE_NAMESPACE_STATIC_ASSERT -DNTEST' \
  -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=OFF \
  -DLLAMA_BUILD_APP=OFF -DLLAMA_BUILD_UI=OFF -DLLAMA_BUILD_TOOLS=OFF
cmake --build "$LLAMA_TT_DIR/build" --target llama -j "$BUILD_JOBS"

git -C "$LLAMA_TT_DIR" show "$LLAMA_REVISION:examples/simple/simple.cpp" \
  > "$OUT_DIR/simple-original.cpp"
patch --batch --output="$OUT_DIR/simple-sim.cpp" \
  "$OUT_DIR/simple-original.cpp" "$ROOT_DIR/patches/llama-simple-sim.patch"
LLAMA_BIN="$LLAMA_TT_DIR/build/bin"
c++ -O1 -std=c++17 -I"$LLAMA_TT_DIR/include" -I"$LLAMA_TT_DIR/ggml/include" \
  "$OUT_DIR/simple-sim.cpp" "$LLAMA_BIN/libllama.so" \
  -L"$LLAMA_BIN" -Wl,-rpath,"$LLAMA_BIN" -lggml -lggml-base \
  -o "$LLAMA_BIN/llama-simple-sim"
printf 'Built smokes and %s/llama-simple-sim\n' "$LLAMA_BIN"
