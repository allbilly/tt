#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TTSIM_DEPS_DIR="${TTSIM_DEPS_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/tt-blackhole-lab}"
TTSIM_VERSION='v1.10.1'
TTSIM_REVISION='3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a'
TTSIM_SOURCE_DIR="$TTSIM_DEPS_DIR/ttsim-$TTSIM_VERSION"
TTSIM_SOURCE_REPO="$TTSIM_SOURCE_DIR/src"
HOST_ARCH="$(uname -m)"

if [[ "$HOST_ARCH" == 'x86_64' ]]; then
  CPU_FLAGS=" $(awk '/^flags[[:space:]]*:/ { for (i = 3; i <= NF; i++) printf " %s", $i; exit }' /proc/cpuinfo) "
  for FEATURE in cx16 lahf_lm popcnt ssse3 pni sse4_1 sse4_2 avx avx2 bmi1 bmi2 f16c fma movbe xsave; do
    if [[ "$CPU_FLAGS" != *" $FEATURE "* ]]; then
      printf 'The official x86_64 Blackhole library requires x86-64-v3; CPU flag is missing: %s\n' "$FEATURE" >&2
      exit 1
    fi
  done
  if [[ "$CPU_FLAGS" != *' abm '* && "$CPU_FLAGS" != *' lzcnt '* ]]; then
    printf 'The official x86_64 Blackhole library requires x86-64-v3; CPU lacks LZCNT/ABM.\n' >&2
    exit 1
  fi
fi

case "$(uname -m)" in
  x86_64)
    TTSIM_ASSET='libttsim_bh.so'
    TTSIM_SHA256='b276a5fba26064eb72fe964126e34bad4bbf515175890570ba2eaef670fed572'
    ;;
  aarch64|arm64)
    TTSIM_ASSET='libttsim_bh_aarch64.so'
    TTSIM_SHA256='2dfb17535b7cfbdfee3e760dc023a2d7a3f7b95c8633f6009b8fe1ad2cd1204e'
    ;;
  *)
    printf 'Unsupported host architecture: %s\n' "$(uname -m)" >&2
    exit 1
    ;;
esac

TTSIM_LIB_PATH="$TTSIM_DEPS_DIR/$TTSIM_ASSET"
mkdir -p "$TTSIM_DEPS_DIR"
if [[ -f "$TTSIM_LIB_PATH" ]]; then
  ACTUAL_SHA256="$(sha256sum "$TTSIM_LIB_PATH" | cut -d ' ' -f 1)"
  if [[ "$ACTUAL_SHA256" != "$TTSIM_SHA256" ]]; then
    printf 'Existing simulator checksum mismatch: %s\nExpected %s, got %s\n' \
      "$TTSIM_LIB_PATH" "$TTSIM_SHA256" "$ACTUAL_SHA256" >&2
    exit 1
  fi
else
  TMP_LIB="$TTSIM_LIB_PATH.part"
  trap 'rm -f "$TMP_LIB"' EXIT
  curl --fail --location --retry 3 \
    "https://github.com/tenstorrent/ttsim/releases/download/$TTSIM_VERSION/$TTSIM_ASSET" \
    --output "$TMP_LIB"
  ACTUAL_SHA256="$(sha256sum "$TMP_LIB" | cut -d ' ' -f 1)"
  if [[ "$ACTUAL_SHA256" != "$TTSIM_SHA256" ]]; then
    printf 'Downloaded simulator checksum mismatch: expected %s, got %s\n' \
      "$TTSIM_SHA256" "$ACTUAL_SHA256" >&2
    exit 1
  fi
  mv "$TMP_LIB" "$TTSIM_LIB_PATH"
  trap - EXIT
fi

python3 - "$TTSIM_LIB_PATH" "$HOST_ARCH" <<'PY'
import pathlib
import struct
import sys

path = pathlib.Path(sys.argv[1])
host_arch = sys.argv[2]
data = path.read_bytes()[:20]
expected_machine = {"x86_64": 62, "aarch64": 183, "arm64": 183}.get(host_arch)
if len(data) != 20 or data[:4] != b"\x7fELF":
    raise SystemExit(f"simulator asset is not a valid ELF file: {path}")
elf_class, encoding = data[4], data[5]
machine = struct.unpack_from("<H" if encoding == 1 else ">H", data, 18)[0] if encoding in (1, 2) else None
if elf_class != 2 or encoding != 1 or machine != expected_machine:
    raise SystemExit(
        f"simulator ELF target mismatch: class={elf_class} encoding={encoding} machine={machine}; "
        f"host={host_arch} expected_machine={expected_machine}"
    )
PY

if [[ -e "$TTSIM_SOURCE_REPO/.git" ]]; then
  ACTUAL_REVISION="$(git -C "$TTSIM_SOURCE_REPO" rev-parse HEAD)"
  if [[ "$ACTUAL_REVISION" != "$TTSIM_REVISION" ]]; then
    printf 'Existing ttsim checkout is not the pinned revision: %s\n' "$ACTUAL_REVISION" >&2
    exit 1
  fi
  if [[ -n "$(git -C "$TTSIM_SOURCE_REPO" status --short --untracked-files=no)" ]]; then
    printf 'Pinned ttsim checkout has tracked local edits; refusing to build over them: %s\n' "$TTSIM_SOURCE_REPO" >&2
    exit 1
  fi
else
  mkdir -p "$TTSIM_SOURCE_DIR"
  git clone --depth 1 --branch "$TTSIM_VERSION" \
    https://github.com/tenstorrent/ttsim.git "$TTSIM_SOURCE_REPO"
  ACTUAL_REVISION="$(git -C "$TTSIM_SOURCE_REPO" rev-parse HEAD)"
  if [[ "$ACTUAL_REVISION" != "$TTSIM_REVISION" ]]; then
    printf 'Pinned ttsim tag resolved unexpectedly: %s\n' "$ACTUAL_REVISION" >&2
    exit 1
  fi
fi

if [[ "${TTSIM_BUILD_SOURCE:-0}" == '1' ]]; then
  TTSIM_BUILD_JOBS="${TTSIM_BUILD_JOBS:-4}"
  if [[ ! "$TTSIM_BUILD_JOBS" =~ ^[1-8]$ ]]; then
    printf 'TTSIM_BUILD_JOBS must be an integer from 1 through 8\n' >&2
    exit 1
  fi
  (
    cd "$TTSIM_SOURCE_REPO"
    python3 make.py --jobs "$TTSIM_BUILD_JOBS" src/_out/release_bh/libttsim.so
  )
  TTSIM_LIB_PATH="$TTSIM_SOURCE_REPO/src/_out/release_bh/libttsim.so"
fi

if [[ "${TTSIM_BUILD_TRACE:-0}" == '1' ]]; then
  TTSIM_BUILD_JOBS="${TTSIM_BUILD_JOBS:-4}" TTSIM_DEPS_DIR="$TTSIM_DEPS_DIR" \
    "$ROOT_DIR/tools/build_trace.sh"
fi

TRACE_LIB_PATH="$TTSIM_DEPS_DIR/ttsim-$TTSIM_VERSION-trace/src/src/_out/release_bh/libttsim.so"
MANIFEST_PATH="$TTSIM_DEPS_DIR/setup_manifest.json"
CPU_FLAGS="$(awk '/^flags[[:space:]]*:/ { for (i = 3; i <= NF; i++) { if (i > 3) printf " "; printf "%s", $i } exit }' /proc/cpuinfo 2>/dev/null || true)"
COMPILER_VERSION="$(g++ --version 2>/dev/null | head -n 1 || true)"
python3 - "$MANIFEST_PATH" "$TTSIM_VERSION" "$TTSIM_REVISION" "$TTSIM_ASSET" \
  "$TTSIM_SHA256" "$TTSIM_LIB_PATH" "$HOST_ARCH" "$CPU_FLAGS" "$COMPILER_VERSION" \
  "$TTSIM_SOURCE_REPO" "$TRACE_LIB_PATH" "${TTSIM_BUILD_SOURCE:-0}" <<'PY'
import hashlib
import json
import pathlib
import platform
import sys

(out, version, revision, asset, expected_hash, library, host_arch, cpu_flags,
 compiler, source, trace_library, built_source) = sys.argv[1:]
lib = pathlib.Path(library)
manifest = {
    "simulator": "official Tenstorrent ttsim single-chip Blackhole model",
    "release": version,
    "source_revision": revision,
    "release_asset": asset,
    "release_asset_sha256": expected_hash,
    "selected_library": str(lib.resolve()),
    "selected_library_sha256": hashlib.sha256(lib.read_bytes()).hexdigest(),
    "selected_library_source_built": built_source == "1",
    "host": {"architecture": host_arch, "platform": platform.platform(), "cpu_flags": cpu_flags.split()},
    "x86_64_target_isa": "x86-64-v3 (AVX/AVX2/BMI1/BMI2/F16C/FMA/LZCNT/MOVBE/POPCNT)" if host_arch == "x86_64" else None,
    "release_asset_compiler_flags": "not published as part of the release asset; verified by official SHA256",
    "host_gxx": compiler,
    "source_build_flags": [
        "-std=c++20", "-Wall", "-Wextra", "-Wno-unused-parameter", "-Wundef", "-Wcast-qual", "-Werror", "-MMD",
        "-fPIC", "-fvisibility=hidden", "-fno-exceptions", "-fno-rtti", "-march=x86-64-v3", "-O2",
        "-DTT_ARCH_VERSION=1", "-DNUM_CHIPS=1", "-DNUM_MMIO_CHIPS=1", "-I_out/bh",
    ] if built_source == "1" else None,
    "pinned_source_checkout": str(pathlib.Path(source).resolve()),
    "trace_library_path_if_built": str(pathlib.Path(trace_library).resolve()),
}
pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
pathlib.Path(out).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
PY

printf 'Dependency directory: %s\n' "$TTSIM_DEPS_DIR"
printf 'ttsim revision: %s\n' "$TTSIM_REVISION"
printf 'Blackhole library: %s\n' "$TTSIM_LIB_PATH"
printf 'Export for this shell: export TTSIM_LIB=%q\n' "$TTSIM_LIB_PATH"
printf 'Setup manifest: %s\n' "$MANIFEST_PATH"
if [[ -f "$TRACE_LIB_PATH" ]]; then
  printf 'Opt-in trace library: %s\n' "$TRACE_LIB_PATH"
elif [[ "${TTSIM_BUILD_TRACE:-0}" != '1' ]]; then
  printf 'To build the optional traced library: TTSIM_BUILD_TRACE=1 %q\n' "$ROOT_DIR/setup.sh"
fi
