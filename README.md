# Blackhole Tensix ISA lab

Low-level register, FIFO, and instruction experiments on Tenstorrent's official single-chip Blackhole simulator (`ttsim` v1.10.1). Python uploads handwritten RV32 BRISC/TRISC programs, drives the `libttsim` C ABI, and checks device-written results from raw Tensix instructions.

The examples use no TT-Metal or TTNN runtime. Results describe simulator behavior; they make no physical-hardware or performance claim.

## Quick start

Requires Python 3, Git, curl, and SHA256 tooling. Run from the repository root; the commands below use the x86-64 library, which requires x86-64-v3. For AArch64, use the `TTSIM_LIB` export printed by setup.

```bash
export TTSIM_DEPS_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/tt-blackhole-lab"
./setup.sh
export TTSIM_LIB="$TTSIM_DEPS_DIR/libttsim_bh.so"

python3 examples/00_probe.py
python3 examples/01_riscv.py
python3 examples/add.py
python3 examples/mul.py
python3 examples/sub.py
python3 examples/stream_controls.py
```

Dependencies stay outside the repository; results go into `out/`. ADD, MUL, and SUB each check 128 BF16 results. See [setup and rerun](docs/setup.md) for bounded commands and artifact locations.

## Full verification

The optional trace build needs `g++`; comparison also needs `llvm-mc`, `llvm-objcopy`, and `llvm-objdump`.

```bash
TTSIM_BUILD_TRACE=1 ./setup.sh
python3 tools/compare.py
python3 tools/inventory.py --require-all-exercised
```

The recorded run observed all 94 runnable instruction names and checked ADD/MUL/SUB output in two cases. This is representative instruction coverage; field combinations and other operations' numerical results are not exhaustively checked. See [verification](docs/verification.md) for traces, the ADD-to-MUL byte patch, and coverage limits.

## Documentation

- [Setup and rerun](docs/setup.md): dependencies, commands, bounds, and output files.
- [Architecture and instruction streams](docs/architecture.md): ABI, addresses, bootstrap, BF16 workload, and TRISC controls.
- [Verification and tracing](docs/verification.md): instruction inventory, state capture, byte patch, stall diagnostic, and recorded results.
- [Pinned sources and tested host](docs/sources.md): revisions, checksums, references, and build environment.
- [Trace patch](patches/README.md): patch scope and build behavior.
