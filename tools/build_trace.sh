#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TTSIM_DEPS_DIR="${TTSIM_DEPS_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/tt-blackhole-lab}"
TTSIM_VERSION='v1.10.1'
TTSIM_REVISION='3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a'
PATCH_FILE="$ROOT_DIR/patches/ttsim-v1.10.1-readonly-elw-trace.patch"
BASE_REPO="$TTSIM_DEPS_DIR/ttsim-$TTSIM_VERSION/src"
TRACE_REPO="$TTSIM_DEPS_DIR/ttsim-$TTSIM_VERSION-trace/src"
TRACE_LIB="$TRACE_REPO/src/_out/release_bh/libttsim.so"
BUILD_JOBS="${TTSIM_BUILD_JOBS:-4}"

if [[ ! "$BUILD_JOBS" =~ ^[1-8]$ ]]; then
  printf 'TTSIM_BUILD_JOBS must be an integer from 1 through 8\n' >&2
  exit 1
fi
if [[ ! -f "$PATCH_FILE" ]]; then
  printf 'Missing read-only trace patch: %s\n' "$PATCH_FILE" >&2
  exit 1
fi
if [[ "$(uname -m)" != 'x86_64' ]]; then
  printf 'The checked trace build currently targets x86_64 Blackhole only; got %s\n' "$(uname -m)" >&2
  exit 1
fi
if [[ ! -e "$BASE_REPO/.git" ]] || [[ "$(git -C "$BASE_REPO" rev-parse HEAD)" != "$TTSIM_REVISION" ]]; then
  printf 'Run setup.sh first; pinned ttsim source is missing or at the wrong revision: %s\n' "$BASE_REPO" >&2
  exit 1
fi

mkdir -p "$(dirname -- "$TRACE_REPO")"
if [[ ! -e "$TRACE_REPO/.git" ]]; then
  git clone --shared --no-checkout "$BASE_REPO" "$TRACE_REPO"
  git -C "$TRACE_REPO" checkout --detach "$TTSIM_REVISION"
fi
ACTUAL_REVISION="$(git -C "$TRACE_REPO" rev-parse HEAD)"
if [[ "$ACTUAL_REVISION" != "$TTSIM_REVISION" ]]; then
  printf 'Trace checkout is not pinned at %s: %s\n' "$TTSIM_REVISION" "$ACTUAL_REVISION" >&2
  exit 1
fi

if git -C "$TRACE_REPO" apply --reverse --check "$PATCH_FILE" >/dev/null 2>&1; then
  : # The exact patch is already applied.
elif git -C "$TRACE_REPO" apply --check "$PATCH_FILE" >/dev/null 2>&1; then
  git -C "$TRACE_REPO" apply "$PATCH_FILE"
else
  printf 'Trace patch does not apply cleanly to pinned source: %s\n' "$PATCH_FILE" >&2
  exit 1
fi
git -C "$TRACE_REPO" diff --check
CHANGED_FILES="$(git -C "$TRACE_REPO" diff --name-only)"
EXPECTED_CHANGED_FILES=$'src/libttsim.cpp\nsrc/tensix.cpp'
if [[ "$CHANGED_FILES" != "$EXPECTED_CHANGED_FILES" ]]; then
  printf 'Unexpected modified files in trace checkout: %s\n' "$CHANGED_FILES" >&2
  exit 1
fi
CURRENT_DIFF="$(mktemp)"
trap 'rm -f "$CURRENT_DIFF"' EXIT
git -C "$TRACE_REPO" diff -- src/libttsim.cpp src/tensix.cpp > "$CURRENT_DIFF"
if ! cmp -s "$PATCH_FILE" "$CURRENT_DIFF"; then
  printf 'Trace checkout contains edits beyond the repository patch. Restore it before building.\n' >&2
  exit 1
fi

BUILD_LOG="$TTSIM_DEPS_DIR/ttsim-$TTSIM_VERSION-trace/build.log"
mkdir -p "$(dirname -- "$BUILD_LOG")"
(
  cd "$TRACE_REPO"
  python3 make.py --jobs "$BUILD_JOBS" --verbose src/_out/release_bh/libttsim.so
) 2>&1 | tee "$BUILD_LOG"

PATCH_SHA256="$(sha256sum "$PATCH_FILE" | cut -d ' ' -f 1)"
LIB_SHA256="$(sha256sum "$TRACE_LIB" | cut -d ' ' -f 1)"
COMPILER_VERSION="$(g++ --version | head -n 1)"
python3 - "$TTSIM_DEPS_DIR/ttsim-$TTSIM_VERSION-trace/build_manifest.json" \
  "$TRACE_REPO" "$TTSIM_REVISION" "$PATCH_FILE" "$PATCH_SHA256" "$TRACE_LIB" \
  "$LIB_SHA256" "$BUILD_LOG" "$COMPILER_VERSION" "$BUILD_JOBS" <<'PY'
import hashlib
import json
import pathlib
import platform
import sys

out, source, revision, patch, patch_hash, library, library_hash, log, compiler, jobs = sys.argv[1:]
manifest = {
    "build": "ttsim v1.10.1 release_bh, single-chip Blackhole, source-built with opt-in read-only trace patch",
    "source_revision": revision,
    "source_checkout": str(pathlib.Path(source).resolve()),
    "patch_file": str(pathlib.Path(patch).resolve()),
    "patch_sha256": patch_hash,
    "library": str(pathlib.Path(library).resolve()),
    "library_sha256": library_hash,
    "host_architecture": platform.machine(),
    "compiler": compiler,
    "jobs": int(jobs),
    "compile_flags": [
        "-std=c++20", "-Wall", "-Wextra", "-Wno-unused-parameter", "-Wundef", "-Wcast-qual", "-Werror", "-MMD",
        "-fPIC", "-fvisibility=hidden", "-fno-exceptions", "-fno-rtti", "-fno-semantic-interposition",
        "-march=x86-64-v3", "-O2", "-DTT_ARCH_VERSION=1", "-DNUM_CHIPS=1", "-DNUM_MMIO_CHIPS=1", "-I_out/bh",
    ],
    "build_log": str(pathlib.Path(log).resolve()),
    "build_log_sha256": hashlib.sha256(pathlib.Path(log).read_bytes()).hexdigest(),
}
pathlib.Path(out).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
print(f"Trace library: {manifest['library']}")
print(f"Trace library SHA256: {library_hash}")
print(f"Build manifest: {out}")
PY
