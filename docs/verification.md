# Verification and tracing

[Back to README](../README.md) · [Setup and rerun](setup.md)

The results below describe the recorded simulator run. [Pinned sources and tested host](sources.md) identifies the revisions and environment used.

## Instruction coverage

The pinned Blackhole Tensix decoder has 137 named entries: 93 callback handlers, 40 entries marked unsupported in the decoder, and four stream-expander controls. Three of those callback handlers reject every Blackhole execution (`SETDVALID`, `SFPLOADMACRO`, and `REG2FLOP`). That leaves 90 runnable callback handlers plus four runnable stream controls. `tools/inventory.py` checks the decoder and source against the pinned official revision, then accepts only trace/example evidence whose source and patch hashes match. Its report separates callback completion, stream-control execution, and operations with a checked output result; decoder support alone does not count as execution coverage. `--require-all-exercised` fails if any of the 94 runnable instruction names lacks evidence.

The ADD, MUL, and SUB streams together have completed callback evidence for all 90 runnable handlers. The TRISC0 stream-control example separately configures `MOP_CFG`, expands `MOP`, consumes `NOP`, and loads and replays one word with `REPLAY`; it waits for Tensix completion before writing its device-side marker. Each arithmetic operation has a separate 128-element bit-for-bit output check in both test cases. The inventory marks the three Blackhole-rejected handlers separately and records zero unexercised runnable instruction names. Callback and expander traces prove observed execution; only ADD, MUL, and SUB have a checked arithmetic output oracle. Coverage is per instruction name and uses a representative valid encoding, not an exhaustive sweep of field combinations. In particular, the simulator source explicitly rejects `REPLAY execute_while_loading=1` when `load_mode=0`.

## State capture

The stock C ABI does not expose the internal Tensix state. `patches/ttsim-v1.10.1-readonly-elw-trace.patch` is an opt-in source patch to the pinned official simulator. It logs each Tensix opcode callback on tile 0, pipe 0 with its raw word and `executed` or `stalled_retry` status. It also records `MOP`, `NOP`, `MOP_CFG`, and both load/execute phases of `REPLAY` when TRISC0 sends them through the stream expander. For ELWADD/ELWMUL/ELWSUB it logs the issued word and successful before/after source, Dst, validity, format, control, counter, and fidelity state. The patch uses compile-time filters and sends diagnostics to `stderr`; `tools/compare.py` captures that child-process stream in `process.stderr.log` and extracts JSON lines into `ttsim_trace.jsonl`. The simulator reads no trace-related runtime environment variables and opens no trace file. The patch does not change initialization, arithmetic, validity checks, or modeled scheduling.

The issue record's queue slot is a per-pipe FIFO position, not a Tensix stream-table offset. The trace coordinates follow the pinned `tile_to_coord` mapping in `src/tile.cpp` (14 tile IDs per row, with the physical column gap); tile ID 0 maps to `(1,2)`. The successful instruction word is linked in `trace.log` to its unique stream word, file offset, L1 load address, and BRISC store PC. This labels the issuer separately from asynchronous Tensix completion. `registers_before.json` and `registers_after.json` contain all 8-by-16 active source and Dst cells. SrcA/SrcB format 5 values are recorded as the model's raw 32-bit widened-BF16 words and decoded to BF16; they are not inferred from the host input files. Dst's `internal_u16` uses the simulator's internal layout, while `decoded_bf16` applies the pinned simulator's Dst decode. The captured pipe state is simulator-only visibility, not hardware debug readback. The packed `output.bin` and its decoded JSON are host observations of tile L1 through the documented BAR path.

## ADD-to-MUL byte patch

`tools/compare.py` runs stock and instrumented libraries in separate processes and requires bit-identical output for ADD/MUL/SUB and both cases. It compares Python source, independently assembled RV32 bytes, config values, raw Tensix tables, captured source/control/Dst state, and output memory. It then copies the verified ADD artifacts, changes only the MUL opcode byte in the stored raw Tensix stream, and invokes `add.py --load-image-dir` on those bytes with expected MUL arithmetic. The patched output, Dst state, and trace must match ordinary MUL. `patch.diff.json` records the operation word offset, changed byte offset, old/new bytes, XOR bits, and L1 load address.

The initial numerical validation used the stock release library. The additional state snapshots use the instrumented source build. In the recorded run, both builds produced byte-identical output for all six ADD/MUL/SUB operation/case combinations.

## Expected stall diagnostic

The comparison also runs a separate bounded diagnostic with ELWADD queued before its unpackers. Ttsim records the real issue and repeated invalid-SrcA/SrcB stalled retries. That stream remains blocked at the pending ELW and is expected to time out; its `validation.json` says `FAIL` by design. It is not counted as an arithmetic pass. Successful execution and before/after state are captured by the normal traced ADD/MUL runs.

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
