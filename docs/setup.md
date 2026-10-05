# Setup and rerun

[Back to README](../README.md)

Run all commands from the repository root. The commands below select the x86-64 library; on AArch64, use the `TTSIM_LIB` export printed by `setup.sh`. The x86-64 release requires x86-64-v3 CPU features.

## Install the simulator

The setup script downloads the official v1.10.1 Blackhole release library, verifies its SHA256 and ELF target, and checks out the matching official source revision. Use `TTSIM_BUILD_TRACE=1` once to also build the optional, read-only state-tracing library used by `tools/compare.py`:

```bash
export TTSIM_DEPS_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/tt-blackhole-lab"
TTSIM_BUILD_TRACE=1 ./setup.sh
export TTSIM_LIB="$TTSIM_DEPS_DIR/libttsim_bh.so"
```

The dependencies and generated libraries remain outside this repository. `TTSIM_DEPS_DIR` is configurable, and `TTSIM_BUILD_JOBS` is limited to 1 through 8. Setup needs Python 3, Git, curl, and SHA256 tooling. The optional trace build also needs `g++`; comparison uses `llvm-mc`, `llvm-objcopy`, and `llvm-objdump` for an independent RV32 assembly check. No root access or container is required. `setup_manifest.json` and the trace build manifest record the selected library hashes, CPU features, compiler, flags, and build log. After setup, the workload and comparison need only `TTSIM_LIB`; `tools/compare.py` finds the trace build beside the stock library. Pass `--trace-lib` only if the trace build is stored elsewhere.

## Run the examples

For a normal non-traced run, only `TTSIM_LIB` is required:

```bash
python3 examples/00_probe.py
python3 examples/01_riscv.py --timeout 20 --clock-limit 5000 --clock-step 64
python3 examples/add.py --case 0 --timeout 25 --clock-limit 5000 --clock-step 64
python3 examples/mul.py --case 0 --timeout 25 --clock-limit 5000 --clock-step 64
python3 examples/sub.py --case 0 --timeout 25 --clock-limit 5000 --clock-step 64
python3 examples/stream_controls.py --timeout 20 --clock-limit 5000 --clock-step 64
```

Each execution starts a fresh simulator process. The examples enforce simulated clock and wall-clock bounds; wrap standalone commands in your shell's `timeout` as an additional process bound if desired.

`examples/add.py`, `examples/mul.py`, and `examples/sub.py` are intentionally readable as separate low-level instruction examples. Their streams differ at the ELW opcode; each keeps the same handwritten BRISC setup, input/configuration, and output validation. Running one directly uses the selected library and leaves internal state files marked unavailable; the comparison command runs ADD/MUL/SUB with the instrumented library and attaches captured state.

## Run the full comparison

`tools/compare.py` launches bounded child processes itself. It reruns stock and traced ADD/MUL/SUB for both cases, checks the independently assembled RV32 bytes, runs the TRISC0 stream-control example, applies and executes the ADD-to-MUL patch, and runs the intentionally blocked stall diagnostic:

```bash
python3 tools/compare.py --device-timeout 25 --wall-timeout 60 --clock-limit 5000 --clock-step 64
python3 tools/inventory.py --require-all-exercised
```

It writes `out/comparison.json` and `out/delta.json`. Case 0 artifacts are in `out/add/`, `out/mul/`, `out/sub/`, and `out/stock/{add,mul,sub}/`; case 1 is in each `case1/` subdirectory. Stream-control assembly, raw words, validation, and trace are in `out/stream_controls/`. The patched execution is in `out/patched_add_to_mul/`. The trace-only deadlock diagnostic is in `out/stall_probe/`; see the [expected stall diagnostic](verification.md#expected-stall-diagnostic).

See [verification and tracing](verification.md) for the instruction inventory, trace interpretation, expected stall diagnostic, and recorded results. [Architecture](architecture.md) explains the programs and address layout; [sources](sources.md) records exact revisions and the tested host.
