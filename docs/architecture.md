# Architecture and instruction streams

[Back to README](../README.md) · [Setup and rerun](setup.md)

This repository executes small RV32 BRISC/TRISC control programs and raw Tensix instructions on Tenstorrent's official single-chip Blackhole simulator. The examples are low-level register, FIFO, and ISA experiments, not a CUDA-like kernel launch API. Python uploads bytes and drives the documented `libttsim` C ABI; simulated BRISC/TRISC code writes Tensix words through the instruction apertures. The host never writes arithmetic results into device memory.

The lab stops at a small, exact BF16 workload. It does not use `tt-emule`, TT-Metal, TTNN, a kernel runtime, a physical card, or the simulator's private math helpers. It makes no hardware or performance claim.

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

| Operation | Raw word | Decoded operation fields |
| --- | --- | --- |
| ELWADD | `0x28000000` | opcode `0x28`, dst 0, addr_mode 0, instr_mod19 0, dest_accum_en 0, clear_dvalid 0 |
| ELWMUL | `0x27000000` | opcode `0x27`, dst 0, addr_mode 0, instr_mod19 0, dest_accum_en 0, clear_dvalid 0 |
| ELWSUB | `0x30000000` | opcode `0x30`, dst 0, addr_mode 0, instr_mod19 0, dest_accum_en 0, clear_dvalid 0 |

For the ADD-to-MUL comparison, the kernel bytes and programmed config table are identical. The sole changed Tensix word has XOR `0x0f000000`; bits 24, 25, 26, and 27 change within the opcode byte. Because the stream is little-endian, only byte offset `0x37` changes from `0x28` to `0x27`; the four-byte word begins at file offset `0x34`.

`ELWMUL` accumulates into Dst even though its encoded `dest_accum_en` field must be zero. All three streams therefore execute the same legal `ZEROACC` before math, invalidating the Dst rows so the model reads zero. The memory output starts with a repeated `0x6bad` BF16 sentinel; separate guards surround it. BRISC waits for the last packed pair to change before writing its completion marker. Host validation checks all 128 observed results and both guards, and `output_decoded.json` preserves each actual packed value beside its input and host reference. Host arithmetic is used only to form that reference.

The BF16 test data does not claim general BF16 accuracy. Ttsim's Blackhole fidelity phase begins at zero in a fresh model, and no unsupported fidelity register write is attempted. The selected powers-of-two inputs are exact for this experiment; broader accuracy would need a separate study.

## TRISC0 stream controls

`examples/stream_controls.py` runs on TRISC0 because ttsim routes TRISC writes through the MOP expander while BRISC FIFO writes bypass it. It installs a small RV32I program at shared L1 `0x6000`, writes SFPNOP to MOP config register 3 at `0xffb8000c`, and pushes the four control words through `0xffe40000`. A Tensix sync read at `0xffe80004` stalls until the generated SFPNOP callbacks finish; only then does TRISC0 write the result marker. The trace records each control's raw word and observed phase.

See [verification and tracing](verification.md) for observed state, instruction coverage, and output checks.
