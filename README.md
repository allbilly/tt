# Blackhole Tensix ISA lab

This repository executes small RV32 BRISC/TRISC control programs and raw Tensix instructions on Tenstorrent's official single-chip Blackhole simulator. The examples are low-level register, FIFO, and ISA experiments, not a CUDA-like kernel launch API. Python uploads bytes and drives the documented `libttsim` C ABI; simulated BRISC/TRISC code writes Tensix words through the instruction apertures. The host never writes arithmetic results into device memory.

The lab stops at a small, exact BF16 workload. It does not use `tt-emule`, TT-Metal, TTNN, a kernel runtime, a physical card, or the simulator's private math helpers. It makes no hardware or performance claim.

## Setup and rerun

The setup script downloads the official v1.10.1 Blackhole release library, verifies its SHA256 and ELF target, and checks out the matching official source revision. Use `TTSIM_BUILD_TRACE=1` once to also build the optional, read-only state-tracing library used by `tools/compare.py`:

```bash
export TTSIM_DEPS_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/tt-blackhole-lab"
TTSIM_BUILD_TRACE=1 ./setup.sh
export TTSIM_LIB="$TTSIM_DEPS_DIR/libttsim_bh.so"
```

The dependencies and generated libraries remain outside this repository. `TTSIM_DEPS_DIR` is configurable, and `TTSIM_BUILD_JOBS` is limited to 1 through 8. Setup needs Python 3, Git, curl, and SHA256 tooling. The optional trace build also needs `g++`; comparison uses `llvm-mc`, `llvm-objcopy`, and `llvm-objdump` for an independent RV32 assembly check. No root access or container is required. `setup_manifest.json` and the trace build manifest record the selected library hashes, CPU features, compiler, flags, and build log. After setup, the workload and comparison need only `TTSIM_LIB`; `tools/compare.py` finds the trace build beside the stock library. Pass `--trace-lib` only if the trace build is stored elsewhere.

For a normal non-traced run, only `TTSIM_LIB` is required:

```bash
python3 examples/00_probe.py
python3 examples/01_riscv.py --timeout 20 --clock-limit 5000 --clock-step 64
python3 examples/add.py --case 0 --timeout 25 --clock-limit 5000 --clock-step 64
python3 examples/mul.py --case 0 --timeout 25 --clock-limit 5000 --clock-step 64
python3 examples/sub.py --case 0 --timeout 25 --clock-limit 5000 --clock-step 64
python3 examples/stream_controls.py --timeout 20 --clock-limit 5000 --clock-step 64
```

Each execution starts a fresh simulator process. The examples enforce simulated clock and wall-clock bounds; wrap standalone commands in your shell's `timeout` as an additional process bound if desired. `tools/compare.py` launches bounded child processes itself. It reruns stock and traced ADD/MUL/SUB for both cases, checks the independently assembled RV32 bytes, runs the TRISC0 stream-control example, applies and executes the ADD-to-MUL patch, and runs the intentionally blocked stall diagnostic:

```bash
python3 tools/compare.py --device-timeout 25 --wall-timeout 60 --clock-limit 5000 --clock-step 64
python3 tools/inventory.py --require-all-exercised
```

It writes `out/comparison.json` and `out/delta.json`. Case 0 artifacts are in `out/add/`, `out/mul/`, `out/sub/`, and `out/stock/{add,mul,sub}/`; case 1 is in each `case1/` subdirectory. Stream-control assembly, raw words, validation, and trace are in `out/stream_controls/`. The patched execution is in `out/patched_add_to_mul/`. The trace-only deadlock diagnostic is in `out/stall_probe/` and is described below.

The pinned Blackhole Tensix decoder has 137 named entries: 93 callback handlers, 40 entries marked unsupported in the decoder, four stream-expander controls, and three handlers that reject every Blackhole execution (`SETDVALID`, `SFPLOADMACRO`, and `REG2FLOP`). That leaves 90 runnable callback handlers plus four runnable stream controls. `tools/inventory.py` checks the decoder and source against the pinned official revision, then accepts only trace/example evidence whose source and patch hashes match. Its report separates callback completion, stream-control execution, and operations with a checked output result; decoder support alone does not count as execution coverage. `--require-all-exercised` fails if any of the 94 runnable instruction names lacks evidence.

The ADD, MUL, and SUB streams together have completed callback evidence for all 90 runnable handlers. The TRISC0 stream-control example separately configures `MOP_CFG`, expands `MOP`, consumes `NOP`, and loads and replays one word with `REPLAY`; it waits for Tensix completion before writing its device-side marker. Each arithmetic operation has a separate 128-element bit-for-bit output check in both test cases. The inventory marks the three Blackhole-rejected handlers separately and records zero unexercised runnable instruction names. Callback and expander traces prove observed execution; only ADD, MUL, and SUB have a checked arithmetic output oracle. Coverage is per instruction name and uses a representative valid encoding, not an exhaustive sweep of field combinations. In particular, the simulator source explicitly rejects `REPLAY execute_while_loading=1` when `load_mode=0`.

`examples/add.py`, `examples/mul.py`, and `examples/sub.py` are intentionally readable as separate low-level instruction examples. Their streams differ at the ELW opcode; each keeps the same handwritten BRISC setup, input/configuration, and output validation. Running one directly uses the selected library and leaves internal state files marked unavailable; the comparison command runs ADD/MUL with the instrumented library and attaches captured state.

## Pinned sources and tested host

The implementation and recorded artifacts use these exact revisions:

| Source | Revision | Use |
| --- | --- | --- |
| [tenstorrent/ttsim](https://github.com/tenstorrent/ttsim/tree/3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a) | `3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a` (`v1.10.1`) | Official simulator, C ABI, decoder, Blackhole model |
| [tt-isa-documentation](https://github.com/tenstorrent/tt-isa-documentation/tree/ea0aed9c15b254f99765380f7c895adf10a1ed6c) | `ea0aed9c15b254f99765380f7c895adf10a1ed6c` | Blackhole instruction and register documentation |
| [blackhole-py](https://github.com/boopdotpng/blackhole-py/tree/d8eae8ff54ba3d733a7b0b947c785e0224cf9f1a) | `d8eae8ff54ba3d733a7b0b947c785e0224cf9f1a` | Low-level programming reference only; no code copied |
| [allbilly/ane](https://github.com/allbilly/ane/tree/6838f343ff1e39bd28270302f7e25783a62b37c4) | `6838f343ff1e39bd28270302f7e25783a62b37c4` | Repository organization reference only |
| [allbilly/rk3588](https://github.com/allbilly/rk3588/tree/c6944a6513de7c620aa51384f14dda257db4a574) | `c6944a6513de7c620aa51384f14dda257db4a574` | Repository organization reference only |

The official source files used for the ABI and target include [`docs/libttsim_api.md`](https://github.com/tenstorrent/ttsim/blob/3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a/docs/libttsim_api.md), [`src/libttsim.cpp`](https://github.com/tenstorrent/ttsim/blob/3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a/src/libttsim.cpp), [`src/sim.h`](https://github.com/tenstorrent/ttsim/blob/3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a/src/sim.h), [`src/tensix.cpp`](https://github.com/tenstorrent/ttsim/blob/3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a/src/tensix.cpp), and [`data/bh/tensix_isa.json`](https://github.com/tenstorrent/ttsim/blob/3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a/data/bh/tensix_isa.json). The official [BlackholeA0 ISA files](https://github.com/tenstorrent/tt-isa-documentation/tree/ea0aed9c15b254f99765380f7c895adf10a1ed6c/BlackholeA0) are authoritative for instruction fields and the BRISC [Tensix FIFO push mechanism](https://github.com/tenstorrent/tt-isa-documentation/blob/ea0aed9c15b254f99765380f7c895adf10a1ed6c/BlackholeA0/TensixTile/BabyRISCV/PushTensixInstruction.md). The community reference checkout had no license file; no community source was reused. The trace diff is against Tenstorrent's Apache-2.0-licensed `src/tensix.cpp`; its upstream SPDX header is retained in the patch context.

The measured environment was Fedora 44, x86-64, AMD Ryzen 7 4700U, 36 GiB RAM, and 391 GiB free on the workspace filesystem. The CPU advertises the x86-64-v3 instruction set, including AVX/AVX2, BMI1/2, F16C, FMA, LZCNT, MOVBE, POPCNT, XSAVE, and the required SSE features. Tested tools were GCC 16.1.1, Python 3.14.5, LLVM 22.1.7, Git 2.54.0, curl 8.18.0, and Podman 5.8.2. Podman was available but unused. The stock x86-64 library SHA256 is `b276a5fba26064eb72fe964126e34bad4bbf515175890570ba2eaef670fed572`. The traced build is source revision `3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a`, compiled for `release_bh` with `-march=x86-64-v3`, `-O2`, `-DTT_ARCH_VERSION=1`, and one chip; exact flags and hashes are in the external build manifest.

## Transport, addresses, and bootstrap

`examples/_ttsim.py` declares the official C ABI signatures with 64-bit physical addresses and explicit byte counts. Calls are serialized and non-reentrant, and the wrapper advances the model only through `libttsim_clock`. It uses `libttsim_pci_mem_rd_bytes` and `libttsim_pci_mem_wr_bytes`; it does not use unsupported Blackhole tile-memory helpers or invent state getters.

These address domains stay distinct:

| Domain | Address | Meaning |
| --- | --- | --- |
| BAR0 physical base | `0x0000000100000000` | Host physical address returned by simulated PCI config space; BAR0 is 512 MiB |
| BAR0 TLB setup | BAR0 + `0x1fc00000` | 2 MiB NoC TLB configuration entries |
| NoC worker | `(1, 2)` | Valid worker in the pinned unharvested single-chip topology |
| Tile-local scratch | `0x00010000` | L1 scratch used by the probe and arithmetic payloads |
| Pooling source B | `0x00010800` | Uniform-one BF16 buffer for the simulator's GMPOOL precondition |
| Config window | `0xffef0000` | Config[0] register window programmed by BRISC from an L1 table |
| T0 instruction FIFO | `0xffe40000` | Raw Tensix word push address used by BRISC `sw` instructions |
| MOP config aperture | `0xffb80000` | TRISC MOP configuration registers; stream example sets register 3 |
| TRISC Tensix sync | `0xffe80004` | Read stalls until previously issued TRISC Tensix instructions complete |
| RISCV soft reset | `0xffb121b0` | Holds the baby RISCVs, then releases BRISC or TRISC0 for the selected example |

The host buffer pointer passed to ctypes is separate from the BAR physical address and the tile-local L1 address. The probe configures one 2 MiB TLB window, writes a nonuniform 31-byte pattern with guards to L1, reads it back, and records the actual endpoint and mapping in `out/probe/manifest.json`. That proves BAR/TLB/L1 access only.

`examples/01_riscv.py` installs seven handwritten RV32I instructions at tile-local L1 address zero, initializes result locations to sentinels, and releases only BRISC. The device computes and stores `0x13579bdf`, then writes the done word. It completes at 64 simulated clocks in the recorded run; the host did not write the expected marker. The kernel is assembled independently by LLVM during `tools/compare.py`, and its bytes are compared with the local emitter.

## The Tensix workload

Each math instruction in this model operates on eight rows by sixteen columns: 128 active BF16 values, not a whole 32-by-32 tile. Each input file contains 256 BF16 elements because the configured unpack count is 256; the first 128 form the active 8-by-16 region and the remaining 128 are zero padding. The two deterministic cases use different row/column exponent patterns of powers of two. Their `ADD`, `MUL`, and `SUB` answers are distinct and exactly representable in BF16 for this minimal fidelity phase.

The BRISC code writes an explicit `(register byte offset, value)` table to Config[0], then reads raw Tensix words from L1 and stores each word to `0xffe40000`. This is the documented FIFO encoding: the payload is an unrotated raw 32-bit Tensix word. `blackhole-py`'s two-bit left rotation applies to inline Tensix words in a TRISC instruction stream; the stream-control example instead writes unrotated raw words through the TRISC MMIO aperture.

The three arithmetic examples use the same arithmetic inputs, configuration, unpack, zeroing, pack, synchronization, and output layout. Each 132-word stream first loads SrcA/SrcB, clears Dst with `clear_mode=3`, executes one ELW operation, and packs two groups with `PACR`. It then exercises the remaining runnable callback families after packing. Those probes reload sources, issue movement and matrix/pooling instructions, update and read simulated DMA/configuration state, and run the SFPU instruction set. A separate uniform-one BF16 buffer at `0x00010800` meets GMPOOL's modeled SrcB requirement. Since the probes follow the final pack, their Dst and register side effects do not enter the checked output. The arithmetic operation remains stream word 13, file offset `0x34`, loaded at tile-local L1 `0x00005034`. BRISC's FIFO store is at RV32 PC `0x0000003c`; the Tensix callback completes asynchronously. The raw arithmetic encodings verified against the pinned decoder are:

`examples/stream_controls.py` runs on TRISC0 because ttsim routes TRISC writes through the MOP expander while BRISC FIFO writes bypass it. It installs a small RV32I program at shared L1 `0x6000`, writes SFPNOP to MOP config register 3 at `0xffb8000c`, and pushes the four control words through `0xffe40000`. A Tensix sync read at `0xffe80004` stalls until the generated SFPNOP callbacks finish; only then does TRISC0 write the result marker. The trace records each control's raw word and observed phase.

| Operation | Raw word | Decoded operation fields |
| --- | --- | --- |
| ELWADD | `0x28000000` | opcode `0x28`, dst 0, addr_mode 0, instr_mod19 0, dest_accum_en 0, clear_dvalid 0 |
| ELWMUL | `0x27000000` | opcode `0x27`, dst 0, addr_mode 0, instr_mod19 0, dest_accum_en 0, clear_dvalid 0 |
| ELWSUB | `0x30000000` | opcode `0x30`, dst 0, addr_mode 0, instr_mod19 0, dest_accum_en 0, clear_dvalid 0 |

The kernel bytes and programmed config table are identical. The sole changed Tensix word has XOR `0x0f000000`; bits 24, 25, 26, and 27 change within the opcode byte. Because the stream is little-endian, only byte offset `0x37` changes from `0x28` to `0x27`; the four-byte word begins at file offset `0x34`.

`ELWMUL` accumulates into Dst even though its encoded `dest_accum_en` field must be zero. Both streams therefore execute the same legal `ZEROACC` before math, invalidating the Dst rows so the model reads zero. The memory output starts with a repeated `0x6bad` BF16 sentinel; separate guards surround it. BRISC waits for the last packed pair to change before writing its completion marker. Host validation checks all 128 observed results and both guards, and `output_decoded.json` preserves each actual packed value beside its input and host reference. Host arithmetic is used only to form that reference.

The BF16 test data does not claim general BF16 accuracy. Tsim's Blackhole fidelity phase begins at zero in a fresh model, and no unsupported fidelity register write is attempted. The selected powers-of-two inputs are exact for this experiment; broader accuracy would need a separate study.

## State trace and byte-patch experiment

The stock C ABI does not expose the internal Tensix state. `patches/ttsim-v1.10.1-readonly-elw-trace.patch` is an opt-in source patch to the pinned official simulator. It logs each Tensix opcode callback on tile 0, pipe 0 with its raw word and `executed` or `stalled_retry` status. It also records `MOP`, `NOP`, `MOP_CFG`, and both load/execute phases of `REPLAY` when TRISC0 sends them through the stream expander. For ELWADD/ELWMUL/ELWSUB it logs the issued word and successful before/after source, Dst, validity, format, control, counter, and fidelity state. The patch uses compile-time filters and sends diagnostics to `stderr`; `tools.compare.py` captures that child-process stream in `process.stderr.log` and extracts JSON lines into `ttsim_trace.jsonl`. The simulator reads no trace-related runtime environment variables and opens no trace file. The patch does not change initialization, arithmetic, validity checks, or modeled scheduling.

The issue record's queue slot is a per-pipe FIFO position, not a Tensix stream-table offset. The trace coordinates follow the pinned `tile_to_coord` mapping in `src/tile.cpp` (14 tile IDs per row, with the physical column gap); tile ID 0 maps to `(1,2)`. The successful instruction word is linked in `trace.log` to its unique stream word, file offset, L1 load address, and BRISC store PC. This labels the issuer separately from asynchronous Tensix completion. `registers_before.json` and `registers_after.json` contain all 8-by-16 active source and Dst cells. SrcA/SrcB format 5 values are recorded as the model's raw 32-bit widened-BF16 words and decoded to BF16; they are not inferred from the host input files. Dst's `internal_u16` uses the simulator's internal layout, while `decoded_bf16` applies the pinned simulator's Dst decode. The captured pipe state is simulator-only visibility, not hardware debug readback. The packed `output.bin` and its decoded JSON are host observations of tile L1 through the documented BAR path.

`tools/compare.py` runs stock and instrumented libraries in separate processes and requires bit-identical output for ADD/MUL and both cases. It compares Python source, independently assembled RV32 bytes, config values, raw Tensix tables, captured source/control/Dst state, and output memory. It then copies the verified ADD artifacts, changes only the MUL opcode byte in the stored raw Tensix stream, and invokes `add.py --load-image-dir` on those bytes with expected MUL arithmetic. The patched output, Dst state, and trace must match ordinary MUL. `patch.diff.json` records the operation word offset, changed byte offset, old/new bytes, XOR bits, and L1 load address.

The comparison also runs a separate bounded diagnostic with ELWADD queued before its unpackers. Tsim records the real issue and repeated invalid-SrcA/SrcB stalled retries. That stream remains blocked at the pending ELW and is expected to time out; its `validation.json` says `FAIL` by design. It is not counted as an arithmetic pass. Successful execution and before/after state are captured by the normal traced ADD/MUL runs.

The initial numerical validation used the stock release library. The additional state snapshots use the instrumented source build. In the recorded run, both builds produced byte-identical output for all six ADD/MUL/SUB operation/case combinations.

## Recorded verification status

| Stage | Status | Evidence |
| --- | --- | --- |
| Simulator setup | PASS | Official single-chip Blackhole asset checksum verified; pinned source and build manifests recorded |
| BAR/TLB/L1 access | PASS | 31-byte nonuniform L1 pattern and both guards read back exactly |
| RISC-V kernel execution | PASS | BRISC stored `0x13579bdf` and done=1 at 64 clocks |
| Tensix ADD | PASS | Stock library, 128/128 BF16 matches and guards pass in both cases |
| Tensix MUL | PASS | Stock library, 128/128 BF16 matches and guards pass in both cases |
| Tensix SUB | PASS | Stock library, 128/128 BF16 matches and guards pass in both cases |
| Tensix instruction coverage | PASS | All 90 runnable callbacks and four stream controls observed; 3 arithmetic operations have bit-exact output checks |
| TRISC0 stream-control execution | PASS | MOP_CFG, MOP, NOP, REPLAY load and execute traced; LLVM-assembled RV32 image matched handwritten bytes |
| Actual state capture | PASS | Instrumented official-source build captured issue, before/after state, and successful execution |
| Source/encoding/register comparison | PASS | One raw Tensix word changes; RV32 bytes, config table, inputs, source state, and control match |
| Patched ADD-to-MUL execution | PASS | Stored ADD stream patched at one opcode byte; executed result and captured Dst match ordinary MUL |

These statuses describe this simulator run on the tested host. They do not validate a physical Blackhole device.
