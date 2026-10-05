# llama.cpp through TTNN and TTSim

[Back to README](../README.md)

This experiment follows [Martin's GGML/Metalium work](https://clehaxze.tw/gemlog/2024/11-13-full-llms-running-on-tenstorrent-plus-ggml.gmi). TinyLlama 1.1B generated one token with one transformer block offloaded to a single simulated Blackhole chip. The other 21 transformer blocks, KV cache, and output-head matrix run on the CPU.

```text
llama.cpp -> GGML Metalium backend -> TTNN -> TT-Metal -> libttsim_bh.so
```

The backend uses its own TTNN and Metalium operations. It does not call the Python/raw-Tensix examples. See [the fork's backend guide](https://github.com/marty1885/llama.cpp/blob/04602ca95c1a6adb6c1533e024ad9c79ee6c428d/docs/backend/Metalium.md) and [official simulator integration](https://github.com/tenstorrent/ttsim/tree/3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a).

## Selected sources

| Component | Revision | Location in this experiment |
| --- | --- | --- |
| Martin's llama.cpp fork | `metalium-support`, `04602ca95c1a6adb6c1533e024ad9c79ee6c428d` | `~/llama.cpp-tt` |
| TT-Metal and TTNN | `v0.77.0`, `9f9cd4fd590f4b606bd0981a4fe0b6403eb38ec9` | `~/tt-metal` |
| Blackhole TTSim | `v1.10.1` | External dependency cache |
| TinyLlama Q4_0 GGUF | [TheBloke repository](https://huggingface.co/TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF/tree/52e7645ba7c309695bec7ac98f4f005b139cf465) at `52e7645ba7c309695bec7ac98f4f005b139cf465` | `~/models/tinyllama-1.1b-chat-v1.0.Q4_0.gguf` |

The Blackhole library's verified SHA256 is `b276a5fba26064eb72fe964126e34bad4bbf515175890570ba2eaef670fed572`. The downloaded model's verified SHA256 is `da3087fb14aede55fde6eb81a0e55e886810e43509ec82ecdc7aa5d62a03b556` and its size is 637,699,456 bytes.

## Run after building the stack

Follow the [build instructions](llama-build.md) to prepare the pinned runtime and build the fork with `GGML_METALIUM=ON`. The [runner](../examples/llama_first_token.sh) uses `build/bin/llama-simple-sim`, generated from Martin's small inference example. It runs `-ngl 1 -n 1 Hello` with context 128, CPU KV cache, batches of 32, two CPU threads, and greedy sampling. The plain prompt contains two tokens including BOS; no chat template or warmup pass is added. The offloaded transformer block is `blk.21`, the last of 22. This fork counts the output head first: native `n_gpu_layers=1` selects that head. The small example reserves two native offload slots and overrides `^output.*` weights to CPU, leaving one transformer block on Metalium. Its `-ngl 1` argument therefore means one transformer block; the generic loader still reports two automatic slots.

From the repository root, with the external dependencies built and the model downloaded:

```bash
export TTSIM_DEPS_DIR="$HOME/.cache/ttdeps"
export TT_METAL_HOME="$HOME/tt-metal"
export LLAMA_TT_DIR="$HOME/llama.cpp-tt"
export TT_LLAMA_MODEL="$HOME/models/tinyllama-1.1b-chat-v1.0.Q4_0.gguf"
podman exec --env TTSIM_DEPS_DIR --env TT_METAL_HOME --env LLAMA_TT_DIR \
  --env TT_LLAMA_MODEL --env TT_LLAMA_TIMEOUT --env TT_LLAMA_OUT_DIR \
  tt-metal-sim "$PWD/examples/llama_first_token.sh"
```

`TT_LLAMA_MODEL`, `TT_LLAMA_TIMEOUT`, and `TT_LLAMA_OUT_DIR` override the model path, timeout in seconds, and output directory. Export an override on the host before running the command above; its `--env` options forward exported values into the container. The default timeout is 1,800 seconds. The architecture is Blackhole, using its existing simulator library and matching descriptor. Logs default to `out/llama_first_token/blackhole/llama.log`; set a different output directory to preserve a previous run.

## Evidence and current status

The experiment records manifests and logs under `out/llama_first_token/`. A successful milestone requires device discovery, an offloaded transformer layer, executed TTNN/Metal kernels under TTSim, and a generated token. A CPU-only token does not establish this result.

| Stage | Status |
| --- | --- |
| Existing Blackhole BAR/TLB/L1 probe | PASS on this rerun |
| Existing Blackhole ADD, case 0 | PASS: device marker, 128 output matches, and guards |
| Blackhole simulator asset | PASS: official release SHA256 and size |
| Source headers and installed runtime/toolchain | PASS: pinned archives, SFPI 7.69.0, OpenMPI ULFM 5.0.7 |
| TT-Metal smoke test | PASS: Blackhole device `0xb140`, BRISC print `14 + 7`, readback `21` |
| TTNN smoke test | PASS: two device ADDs; all 1,024 BF16 results equal `3.0` |
| TinyLlama CPU baseline | PASS: greedy comma token, ID `29892`, from `Hello` |
| llama.cpp and Metalium backend build | PASS: source-built libraries and small inference executable |
| TinyLlama, one offloaded layer and one token | PASS: comma token, ID `29892`, matching the CPU baseline |

The completed run on 2026-10-05 exited with code 0. Its scheduler log assigns exactly these seven matrix nodes to Metalium: `Qcur-21`, `Kcur-21`, `Vcur-21`, `attn_out-21`, `ffn_gate-21`, `ffn_up-21`, and `ffn_out-21`. `result_output` stays on CPU. Layer operations may fall back to CPU; the scheduler also places the final unweighted RMS normalization on Metalium. This verifies partial model offload rather than every operation in a complete model.

The inference log records Blackhole discovery, SFPI compilation with `-mcpu=tt-bh-tensix`, TT-Metal program dispatch, the generated token, and the simulator's clean exit report. The runner checks completion and placement markers and records the process exit code in `result.json`. Before launch it marks the result `RUNNING`; after exit it records `PASS` or `FAIL`, so a failed rerun cannot retain a previous PASS. Timeout failures retain their exit code, normally `124`. Its postcheck rejects the saved CPU-only and failed simulator attempts.

These dated results and evidence paths describe the local validation run. Full logs and manifests are ignored under `out/` and are not distributed with a Git checkout; rerunning creates a new `blackhole/llama.log` and `blackhole/result.json`.

| Evidence file under `out/llama_first_token/` | Contents |
| --- | --- |
| `experiment_manifest.json` | Sources, packages, model, configuration, and stage results |
| `build_helper6.log` | Final backend build with Blackhole RoPE compatibility fixes |
| `blackhole_metal_integer2.log` | Device print and integer readback |
| `blackhole_ttnn_add.log` | Full-tile TTNN ADD validation |
| `cpu_baseline_final.log` | CPU token ID `29892` |
| `blackhole/llama.log` | Completed one-block Blackhole inference |
| `blackhole/result.json` | Passing checks, token ID, and Metalium matrix assignments |

The run took about 18 minutes on this loaded host, including JIT compilation. This is a functional simulator result, not a hardware throughput measurement. Full-model offload, additional tokens, and general numerical parity have not been tested; matching one greedy token is a limited comparison.

The build uses the separate `tt-metal-sim` Ubuntu 24.04 Podman container. Source checkouts, simulator libraries, the device compiler, and model weights remain outside this repository. The detailed [build guide](llama-build.md) records runtime provenance, SDK workarounds, smoke commands, and the compatibility patches.
