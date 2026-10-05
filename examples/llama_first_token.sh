#!/usr/bin/env bash
set -euo pipefail
ulimit -c 0

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TT_METAL_HOME="${TT_METAL_HOME:-$HOME/tt-metal}"
LLAMA_TT_DIR="${LLAMA_TT_DIR:-$HOME/llama.cpp-tt}"
TTSIM_DEPS_DIR="${TTSIM_DEPS_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/tt-blackhole-lab}"
TT_LLAMA_MODEL="${TT_LLAMA_MODEL:-$HOME/models/tinyllama-1.1b-chat-v1.0.Q4_0.gguf}"
TT_LLAMA_TIMEOUT="${TT_LLAMA_TIMEOUT:-1800}"
TT_LLAMA_OUT_DIR="${TT_LLAMA_OUT_DIR:-$ROOT_DIR/out/llama_first_token/blackhole}"
SIM_LIBRARY="$TTSIM_DEPS_DIR/libttsim_bh.so"
SOC_DESCRIPTOR="$TT_METAL_HOME/tt_metal/soc_descriptors/blackhole_140_arch.yaml"

if [[ ! "$TT_LLAMA_TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
  printf 'TT_LLAMA_TIMEOUT must be a positive number of seconds\n' >&2
  exit 2
fi
LLAMA_EXAMPLE="$LLAMA_TT_DIR/build/bin/llama-simple-sim"
for REQUIRED_FILE in "$SIM_LIBRARY" "$SOC_DESCRIPTOR" "$TT_LLAMA_MODEL" "$LLAMA_EXAMPLE"; do
  if [[ ! -f "$REQUIRED_FILE" ]]; then
    printf 'Missing dependency: %s\n' "$REQUIRED_FILE" >&2
    exit 1
  fi
done
if [[ ! -x "$LLAMA_EXAMPLE" ]]; then
  printf 'Inference example is not executable: %s\n' "$LLAMA_EXAMPLE" >&2
  exit 1
fi

SIM_DIR="$TTSIM_DEPS_DIR/llama-blackhole"
mkdir -p "$SIM_DIR" "$TT_LLAMA_OUT_DIR"
ln -sfn "$SIM_LIBRARY" "$SIM_DIR/$(basename -- "$SIM_LIBRARY")"
cp -- "$SOC_DESCRIPTOR" "$SIM_DIR/soc_descriptor.yaml"

export TT_METAL_HOME
if [[ -d /usr/libexec/tt-metalium/runtime ]]; then
  export TT_METAL_RUNTIME_ROOT="${TT_METAL_RUNTIME_ROOT:-/usr/libexec/tt-metalium}"
else
  export TT_METAL_RUNTIME_ROOT="${TT_METAL_RUNTIME_ROOT:-$TT_METAL_HOME}"
fi
export TT_METAL_SIMULATOR="$SIM_DIR/$(basename -- "$SIM_LIBRARY")"
export TT_METAL_SIMULATOR_HOME="$SIM_DIR"
export TT_METAL_SLOW_DISPATCH_MODE=1
export TT_METAL_DISABLE_SFPLOADMACRO=1
export GGML_METALIUM_PRINT_REJECTED_OPS=1
export GGML_SCHED_DEBUG=2

cd -- "$TT_METAL_HOME"
printf '%s\n' '{"status": "RUNNING"}' > "$TT_LLAMA_OUT_DIR/result.json"
if timeout --signal=TERM --kill-after=30s "$TT_LLAMA_TIMEOUT" \
  "$LLAMA_EXAMPLE" -m "$TT_LLAMA_MODEL" -ngl 1 -n 1 Hello \
  </dev/null 2>&1 | tee "$TT_LLAMA_OUT_DIR/llama.log"; then
  INFERENCE_EXIT_CODE=0
else
  INFERENCE_EXIT_CODE=$?
fi

python3 - "$TT_LLAMA_OUT_DIR/llama.log" "$INFERENCE_EXIT_CODE" <<'PY'
import json
import pathlib
import re
import sys

log_path = pathlib.Path(sys.argv[1])
exit_code = int(sys.argv[2])
log = log_path.read_text()
tokens = re.findall(r"first-token-id=(\d+)", log)
matmuls = []
metalium_nodes = set()
output_backends = []
for line in log.splitlines():
    match = re.match(r"node #\s*\d+ \(\s*MUL_MAT\):\s+(\S+).*?\[([^]]+)\]", line)
    if not match:
        continue
    node, backend = match.group(1), match.group(2).split()[0]
    if backend == "Metal":
        matmuls.append(line)
        metalium_nodes.add(node)
    if node == "result_output":
        output_backends.append(backend)
expected_nodes = {
    "Qcur-21", "Kcur-21", "Vcur-21", "attn_out-21",
    "ffn_gate-21", "ffn_up-21", "ffn_out-21",
}
simulator_exit = re.search(r"^\[(\d+)\] ([\d.]+) seconds \([\d.]+ KHz\)$", log, re.M)
checks = {
    "inference_process_exit_zero": exit_code == 0,
    "blackhole_simulator": "TTSimTTDevice" in log and "device_id=0xb140" in log,
    "one_transformer_layer": (
        "sim-transformer-layers=1 output-weights=CPU" in log
        and "offloading 1 repeating layers to GPU" in log
    ),
    "output_weights_cpu": bool(re.search(r"tensor output\.weight.*overrid.*CPU", log)),
    "block_21_matrices_on_metalium": metalium_nodes == expected_nodes,
    "output_matrix_on_cpu": bool(output_backends) and set(output_backends) == {"CPU"},
    "device_programs_and_clean_simulator_exit": (
        "Writing DFB config" in log and "JIT cache stats" in log
        and simulator_exit is not None
    ),
    "no_device_fatal": not any(
        text in log for text in ("UnsupportedFunctionality", "TT_FATAL", "| critical")
    ),
    "one_token": len(tokens) == 1 and "decoded 1 tokens" in log,
}
summary = {
    "status": "PASS" if all(checks.values()) else "FAIL",
    "exit_code": exit_code,
    "checks": checks,
    "first_token_id": int(tokens[0]) if len(tokens) == 1 else None,
    "metalium_matmul_nodes": sorted(metalium_nodes),
    "metalium_matmul_assignments": matmuls,
    "simulator_exit_report": simulator_exit.group(0) if simulator_exit else None,
    "log": str(log_path),
}
log_path.with_name("result.json").write_text(json.dumps(summary, indent=2) + "\n")
if not all(checks.values()):
    print(f"Inference evidence incomplete: {checks}", file=sys.stderr)
    raise SystemExit(exit_code or 1)
print(f"PASS: Blackhole TTSim, one offloaded layer, token ID {tokens[0]}")
PY
