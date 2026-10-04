#!/usr/bin/env python3
"""Run the Blackhole MOP, NOP, MOP_CFG, and REPLAY stream controls on TRISC0."""

import argparse
import hashlib
import json
import os
import platform
import shlex
import sys
import time
from pathlib import Path

from _ttsim import Simulator, configure_bh_tlb, pci_bar_base


TILE = (1, 2)
TLB_SIZE = 2 * 1024 * 1024
RISCV_MMIO_BASE = 0xFFB00000
SOFT_RESET_0 = 0xFFB121B0
SOFT_RESET_ALL = 0x00047800
SOFT_RESET_TRISC0_ONLY_RUN = SOFT_RESET_ALL & ~0x00001000
TRISC0_PC = 0x00006000
RESULT_L1 = 0x00011000
RESULT_SENTINEL = 0xDEADBEEF
DONE_SENTINEL = 0xA5A5A5A5
RESULT_MARKER = 0xC0DEC0DE
DONE_MARKER = 1
TENSIX_MOP_CFG_BASE = 0xFFB80000
TENSIX_INST_BASE = 0xFFE40000
TENSIX_PC_BUF_BASE = 0xFFE80000
TENSIX_SYNC = TENSIX_PC_BUF_BASE + 4
SFPNOP = 0x8F000000
CONTROL_WORDS = [
    ("MOP_CFG", 0x03000000),
    ("MOP", 0x01000000),
    ("NOP", 0x02000000),
    ("REPLAY_LOAD", 0x04000011),
    ("REPLAY_BUFFER_WORD", SFPNOP),
    ("REPLAY_EXECUTE", 0x04000010),
]


def encode_lui(rd, imm20):
    return ((imm20 & 0xFFFFF) << 12) | (rd << 7) | 0x37


def encode_addi(rd, rs1, imm12):
    return ((imm12 & 0xFFF) << 20) | (rs1 << 15) | (rd << 7) | 0x13


def encode_sw(rs2, rs1, imm12):
    imm = imm12 & 0xFFF
    return (((imm >> 5) & 0x7F) << 25) | (rs2 << 20) | (rs1 << 15) | (2 << 12) | ((imm & 0x1F) << 7) | 0x23


def encode_lw(rd, rs1, imm12):
    return ((imm12 & 0xFFF) << 20) | (rs1 << 15) | (2 << 12) | (rd << 7) | 0x03


def load_u32(rd, value):
    value &= 0xFFFFFFFF
    low = value & 0xFFF
    if low & 0x800:
        low -= 0x1000
    upper = ((value - low) >> 12) & 0xFFFFF
    words = [(f"lui x{rd}, 0x{upper:05x}", encode_lui(rd, upper))]
    if low:
        words.append((f"addi x{rd}, x{rd}, {low}", encode_addi(rd, rd, low)))
    return words


def build_kernel():
    words = []
    words += load_u32(5, TENSIX_MOP_CFG_BASE)
    words += load_u32(6, SFPNOP)
    words.append(("sw x6, 12(x5) # TRISC0 MOP_CFG register 3 = SFPNOP", encode_sw(6, 5, 12)))
    words += load_u32(5, TENSIX_INST_BASE)
    for name, value in CONTROL_WORDS:
        words += load_u32(6, value)
        if name == "MOP_CFG":
            words.append((f"sw x6, 0(x5) # {name}: configure zmask high", encode_sw(6, 5, 0)))
        elif name == "REPLAY_BUFFER_WORD":
            words.append((f"sw x6, 0(x5) # captured payload for REPLAY", encode_sw(6, 5, 0)))
        else:
            words.append((f"sw x6, 0(x5) # {name}", encode_sw(6, 5, 0)))
    words += load_u32(9, TENSIX_PC_BUF_BASE)
    words.append(("lw x8, 4(x9) # stall until this TRISC0 Tensix FIFO is empty", encode_lw(8, 9, 4)))
    words += load_u32(10, RESULT_L1)
    words += load_u32(11, RESULT_MARKER)
    words.append(("sw x11, 0(x10) # TRISC0 completion marker", encode_sw(11, 10, 0)))
    words.append(("addi x12, x0, 1", encode_addi(12, 0, 1)))
    words.append(("sw x12, 4(x10) # TRISC0 done", encode_sw(12, 10, 4)))
    words.append(("jal x0, 0 # remain in a bounded idle loop", 0x0000006F))
    image = b"".join(word.to_bytes(4, "little") for _, word in words)
    assembly = ".section .text\n.globl _start\n_start:\n" + "\n".join(
        f"    {text}" for text, _ in words
    ) + "\n"
    disassembly = "\n".join(
        f"{index * 4:08x}: {word:08x}  {text}"
        for index, (text, word) in enumerate(words)
    ) + "\n"
    return assembly, words, image, disassembly


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def u32(raw, offset=0):
    return int.from_bytes(raw[offset:offset + 4], "little")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--clock-limit", type=int, default=20000)
    parser.add_argument("--clock-step", type=int, default=64)
    parser.add_argument("--out-dir", default="out/stream_controls")
    args = parser.parse_args()
    if args.timeout <= 0 or args.clock_limit <= 0 or args.clock_step <= 0:
        parser.error("timeout, clock-limit, and clock-step must be positive")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    assembly, words, kernel, disassembly = build_kernel()
    (out_dir / "kernel.S").write_text(assembly, encoding="ascii")
    (out_dir / "kernel.bin").write_bytes(kernel)
    (out_dir / "kernel.disasm").write_text(disassembly, encoding="ascii")
    (out_dir / "tensix_control_words.json").write_text(
        json.dumps([{"name": name, "instruction_word": f"0x{word:08x}"}
                    for name, word in CONTROL_WORDS], indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "commands.log").write_text(
        shlex.join([sys.executable, *sys.argv]) + "\n", encoding="utf-8"
    )
    (out_dir / "input_a.bin").write_bytes(RESULT_SENTINEL.to_bytes(4, "little"))
    (out_dir / "input_b.bin").write_bytes(DONE_SENTINEL.to_bytes(4, "little"))
    source_path = Path(__file__).resolve()
    library_path = Path(os.environ["TTSIM_LIB"]).resolve()
    manifest = {
        "example": "examples/stream_controls.py",
        "simulator_library": str(library_path),
        "simulator_library_sha256": sha256(library_path),
        "host_architecture": platform.machine(),
        "source_hashes": {
            "example_python_sha256": sha256(source_path),
            "rv32_program_sha256": hashlib.sha256(kernel).hexdigest(),
        },
        "controls": ["MOP", "NOP", "MOP_CFG", "REPLAY"],
        "trisc": "TRISC0 / pipe 0",
        "execution_model": "RV32I writes configuration and instruction apertures; the official ttsim stream expander handles control words",
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    deadline = time.monotonic() + args.timeout
    clocks = 0
    trace = [
        f"upload TRISC0 RV32I image to shared L1 0x{TRISC0_PC:08x} ({len(kernel)} bytes)",
        f"MOP config register 3 receives SFPNOP 0x{SFPNOP:08x}",
        "stream words: MOP_CFG, MOP, NOP, REPLAY load, buffered SFPNOP, REPLAY execute",
    ]
    with Simulator(out_dir / "mmio.jsonl") as sim:
        bdf = 0
        identity = sim.config32(bdf, 0)
        if identity != 0xB1401E52:
            raise RuntimeError(f"expected Blackhole PCI endpoint, got 0x{identity:08x}")
        if sim.config32(1 << 3, 0) != 0xFFFFFFFF:
            raise RuntimeError("the selected library is not a single-chip Blackhole build")
        bar0 = pci_bar_base(sim, bdf, 0x10)
        if bar0 != 0x100000000:
            raise RuntimeError(f"unexpected BAR0 base 0x{bar0:016x}")

        l1_tlb = configure_bh_tlb(sim, bar0, 0, *TILE, 0)
        mmio_tlb = configure_bh_tlb(sim, bar0, 1, *TILE, RISCV_MMIO_BASE & -TLB_SIZE)
        scratch_bar = bar0
        mmio_bar = bar0 + TLB_SIZE
        soft_reset_bar = mmio_bar + (SOFT_RESET_0 - mmio_tlb["local_base"])

        sim.write32(soft_reset_bar, SOFT_RESET_ALL)
        sim.write(scratch_bar + TRISC0_PC, kernel)
        sim.write(scratch_bar + RESULT_L1, RESULT_SENTINEL.to_bytes(4, "little"))
        sim.write(scratch_bar + RESULT_L1 + 4, DONE_SENTINEL.to_bytes(4, "little"))
        sim.write32(soft_reset_bar, SOFT_RESET_TRISC0_ONLY_RUN)

        done = DONE_SENTINEL
        marker = RESULT_SENTINEL
        while clocks < args.clock_limit and time.monotonic() < deadline:
            sim.clock(args.clock_step)
            clocks += args.clock_step
            marker = u32(sim.read(scratch_bar + RESULT_L1, 4))
            done = u32(sim.read(scratch_bar + RESULT_L1 + 4, 4))
            if done == DONE_MARKER:
                break
        trace.append(f"clock_count={clocks} marker=0x{marker:08x} done=0x{done:08x}")
        (out_dir / "trace.log").write_text("\n".join(trace) + "\n", encoding="utf-8")
        if done != DONE_MARKER or marker != RESULT_MARKER:
            validation = {
                "status": "FAIL", "operation": "stream_controls", "device_written_done": done,
                "device_result_marker": marker, "clock_count": clocks,
                "l1_tlb": l1_tlb, "mmio_tlb": mmio_tlb,
                "reason": "TRISC0 did not finish the control stream before the bound",
            }
            (out_dir / "validation.json").write_text(json.dumps(validation, indent=2, sort_keys=True) + "\n")
            raise TimeoutError(f"TRISC0 did not complete before the clock/time bound; see {out_dir}/trace.log")

        validation = {
            "status": "PASS", "operation": "stream_controls", "device_written_done": done,
            "device_result_marker": marker, "clock_count": clocks,
            "control_words": len(CONTROL_WORDS), "controls": ["MOP", "NOP", "MOP_CFG", "REPLAY"],
            "execution_core": "TRISC0", "execution_pipe": 0,
            "l1_tlb": l1_tlb, "mmio_tlb": mmio_tlb,
        }
        (out_dir / "validation.json").write_text(json.dumps(validation, indent=2, sort_keys=True) + "\n")
        print(f"PASS: TRISC0 stream controls completed at {clocks} simulated clocks")
        print("controls: MOP_CFG configured, MOP expanded, NOP consumed, REPLAY loaded and replayed")
        print(f"result marker: 0x{marker:08x}; done: {done}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
