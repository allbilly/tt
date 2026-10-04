#!/usr/bin/env python3
"""Run one BF16 ELWMUL on one official simulated Blackhole Tensix tile."""

import argparse
import hashlib
import json
import os
import platform
import shlex
import struct
import sys
import time
from pathlib import Path

from _ttsim import Simulator, configure_bh_tlb, pci_bar_base


TILE = (1, 2)
BAR0_SIZE = 512 * 1024 * 1024
TLB_SIZE = 2 * 1024 * 1024
TLB_CONFIG_OFFSET = 0x1FC00000
RISCV_MMIO_BASE = 0xFFB00000
SOFT_RESET_0 = 0xFFB121B0
SOFT_RESET_ALL_RISCV = 0x00047800
SOFT_RESET_BRISC_ONLY_RUN = 0x00047000
TENSIX_CFG_BASE = 0xFFEF0000
INSTRN_BUF_BASE = 0xFFE40000

KERNEL_L1 = 0x00000000
CONFIG_TABLE_L1 = 0x00004000
TENSIX_WORDS_L1 = 0x00005000
INPUT_A_L1 = 0x00010000
INPUT_B_L1 = 0x00010220
OUTPUT_L1 = 0x00010440
POOL_INPUT_B_L1 = 0x00010800
ACTIVE_ELEMENTS = 8 * 16
UNPACK_ELEMENTS = 256
INPUT_BYTES = UNPACK_ELEMENTS * 2
OUTPUT_BYTES = ACTIVE_ELEMENTS * 2
INPUT_A_GUARD_L1 = INPUT_A_L1 + INPUT_BYTES
INPUT_B_GUARD_L1 = INPUT_B_L1 + INPUT_BYTES
OUTPUT_GUARD_BEFORE_L1 = OUTPUT_L1 - 16
OUTPUT_GUARD_AFTER_L1 = OUTPUT_L1 + OUTPUT_BYTES
RESULT_L1 = OUTPUT_GUARD_AFTER_L1 + 16

OUTPUT_SENTINEL_BF16 = 0x6BAD
OUTPUT_SENTINEL_WORD = OUTPUT_SENTINEL_BF16 | (OUTPUT_SENTINEL_BF16 << 16)
RESULT_SENTINEL = 0xDEADBEEF
DONE_SENTINEL = 0xA5A5A5A5
RESULT_MARKER = 0x54454E53  # ASCII "TENS"
DONE_MARKER = 1
GUARD_BYTES = bytes.fromhex("c35a96e14bd708af6325f01984ce72b1")

ELWADD_OPCODE = 0x28
ELWMUL_OPCODE = 0x27
ELWSUB_OPCODE = 0x30
OPERATION_WORD_INDEX = 13
TTsim_REVISION = "3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a"
ISA_DOCS_REVISION = "ea0aed9c15b254f99765380f7c895adf10a1ed6c"
BLACKHOLE_PY_REVISION = "d8eae8ff54ba3d733a7b0b947c785e0224cf9f1a"


def s32(value):
    value &= 0xFFFFFFFF
    return value if value < 0x80000000 else value - 0x100000000


def encode_lui(rd, imm20):
    return ((imm20 & 0xFFFFF) << 12) | (rd << 7) | 0x37


def encode_addi(rd, rs1, imm12):
    return ((imm12 & 0xFFF) << 20) | (rs1 << 15) | (rd << 7) | 0x13


def encode_lw(rd, rs1, imm12=0):
    return ((imm12 & 0xFFF) << 20) | (rs1 << 15) | (2 << 12) | (rd << 7) | 0x03


def encode_sw(rs2, rs1, imm12=0):
    imm = imm12 & 0xFFF
    return (((imm >> 5) & 0x7F) << 25) | (rs2 << 20) | (rs1 << 15) | (2 << 12) | ((imm & 0x1F) << 7) | 0x23


def encode_add(rd, rs1, rs2):
    return (rs2 << 20) | (rs1 << 15) | (rd << 7) | 0x33


def encode_branch(funct3, rs1, rs2, imm13):
    imm = imm13 & 0x1FFF
    return (
        ((imm >> 12) & 1) << 31
        | ((imm >> 5) & 0x3F) << 25
        | rs2 << 20
        | rs1 << 15
        | funct3 << 12
        | ((imm >> 1) & 0xF) << 8
        | ((imm >> 11) & 1) << 7
        | 0x63
    )


def encode_jal(rd, imm21):
    imm = imm21 & 0x1FFFFF
    return (
        ((imm >> 20) & 1) << 31
        | ((imm >> 1) & 0x3FF) << 21
        | ((imm >> 11) & 1) << 20
        | ((imm >> 12) & 0xFF) << 12
        | (rd << 7)
        | 0x6F
    )


class RV32:
    """Tiny local RV32I emitter for this one visible BRISC program."""

    def __init__(self):
        self.items = []
        self.labels = {}

    def label(self, name):
        self.labels[name] = len(self.items) * 4

    def add(self, text, encoder, *args):
        self.items.append((text, encoder, args))

    def lui(self, rd, imm20, text=None):
        self.add(text or f"lui x{rd}, 0x{imm20:x}", encode_lui, rd, imm20)

    def addi(self, rd, rs1, imm, text=None):
        self.add(text or f"addi x{rd}, x{rs1}, {imm}", encode_addi, rd, rs1, imm)

    def li32(self, rd, value):
        value &= 0xFFFFFFFF
        high = ((value + 0x800) >> 12) & 0xFFFFF
        low = s32(value << 20) >> 20
        self.lui(rd, high, f"lui x{rd}, 0x{high:x}  # li32 0x{value:08x}")
        if low:
            self.addi(rd, rd, low)

    def build(self, config_count, tensix_word_count):
        # BRISC writes Config[0] from L1, then sends unrotated 32-bit Tensix
        # words to the documented T0 instruction FIFO. No inline .ttinsn is used.
        self.lui(8, TENSIX_CFG_BASE >> 12, "lui s0, 0xffef0  # TENSIX_CFG_BASE")
        self.lui(9, CONFIG_TABLE_L1 >> 12, "lui s1, 0x4  # Config[0] write table")
        self.addi(18, 0, config_count, f"addi s2, zero, {config_count}")
        self.label("config_loop")
        self.add("lw t0, 0(s1)  # Config register byte offset", encode_lw, 5, 9, 0)
        self.add("lw t1, 4(s1)  # Config value", encode_lw, 6, 9, 4)
        self.add("add t0, s0, t0", encode_add, 5, 8, 5)
        self.add("sw t1, 0(t0)  # BRISC store to Config[0]", encode_sw, 6, 5, 0)
        self.add("addi s1, s1, 8", encode_addi, 9, 9, 8)
        self.add("addi s2, s2, -1", encode_addi, 18, 18, -1)
        self.add("bne s2, zero, config_loop", "branch", (1, 18, 0, "config_loop"))
        self.add("fence iorw, iorw  # order config writes before FIFO pushes", "fixed", 0x0FF0000F)
        self.lui(9, TENSIX_WORDS_L1 >> 12, "lui s1, 0x5  # raw Tensix-word table")
        self.addi(18, 0, tensix_word_count, f"addi s2, zero, {tensix_word_count}")
        self.lui(28, INSTRN_BUF_BASE >> 12, "lui t3, 0xffe40  # INSTRN_BUF_BASE, T0 FIFO")
        self.label("push_loop")
        self.add("lw t0, 0(s1)  # one raw Tensix word", encode_lw, 5, 9, 0)
        self.add("sw t0, 0(t3)  # push unrotated word to T0", encode_sw, 5, 28, 0)
        self.add("addi s1, s1, 4", encode_addi, 9, 9, 4)
        self.add("addi s2, s2, -1", encode_addi, 18, 18, -1)
        self.add("bne s2, zero, push_loop", "branch", (1, 18, 0, "push_loop"))
        self.add("fence iorw, iorw  # order FIFO pushes before completion poll", "fixed", 0x0FF0000F)
        self.li32(29, OUTPUT_SENTINEL_WORD)
        self.lui(5, OUTPUT_L1 >> 12, "lui t0, 0x10  # output readback sentinel address")
        self.addi(5, 5, (OUTPUT_L1 + OUTPUT_BYTES - 4) & 0xFFF, "addi t0, t0, 0x53c")
        self.label("output_poll")
        self.add("lw t1, 0(t0)  # wait for the last packed row", encode_lw, 6, 5, 0)
        self.add("beq t1, t4, output_poll", "branch", (0, 6, 29, "output_poll"))
        self.li32(30, RESULT_MARKER)
        self.lui(5, RESULT_L1 >> 12, "lui t0, 0x10  # completion marker address")
        self.addi(5, 5, RESULT_L1 & 0xFFF, f"addi t0, t0, 0x{RESULT_L1 & 0xfff:x}")
        self.add("sw t5, 0(t0)  # device-written TENS marker", encode_sw, 30, 5, 0)
        self.addi(7, 0, DONE_MARKER, "addi t2, zero, 1")
        self.add("sw t2, 4(t0)  # device-written completion", encode_sw, 7, 5, 4)
        self.label("done")
        self.add("jal zero, 0  # remain quiescent", encode_jal, 0, 0)
        return self.assemble()

    def assemble(self):
        words = []
        lines = []
        source_lines = [".section .text", ".globl _start", "_start:"]
        pc = 0
        for text, encoder, args in self.items:
            if encoder == "fixed":
                word = args[0]
            elif encoder == "branch":
                funct3, rs1, rs2, label = args[0]
                word = encode_branch(funct3, rs1, rs2, self.labels[label] - pc)
            else:
                word = encoder(*args)
            words.append(word)
            labels = [name for name, address in self.labels.items() if address == pc]
            for label in labels:
                lines.append(f"{label}:")
                source_lines.append(f"{label}:")
            lines.append(f"{pc:08x}: {word:08x}  {text}")
            source_lines.append(f"    {text}")
            pc += 4
        source = "\n".join(source_lines) + "\n"
        disassembly = "\n".join(lines) + "\n"
        image = b"".join(word.to_bytes(4, "little") for word in words)
        return source, words, image, disassembly


def cfg_write(index, value, name):
    return {"index": index, "offset": index * 4, "name": name, "value": value & 0xFFFFFFFF}


def config_table(case):
    # Register indices/fields are from ttsim v1.10.1 data/bh/tensix_regs.json;
    # the register window base and Config[0] layout are in official BackendConfiguration.md.
    writes = [
        cfg_write(1, 5 << 25, "ALU_FORMAT_SPEC_REG2_Dstacc=BF16"),
        cfg_write(12, 128 << 16, "PCK0_ADDR_CTRL_XY_REG_0_Ystride=128 bytes"),
        cfg_write(13, 0, "PCK0_ADDR_CTRL_ZW_REG_0=0"),
        cfg_write(18, 0, "PCK_DEST_RD_CTRL=BF16 16-bit read"),
        cfg_write(20, 0, "TILE_ROW_SET_MAPPING_0=0"),
        cfg_write(24, 0x0000FFFF, "PCK_EDGE_OFFSET_SEC0_mask=all 16 columns"),
        cfg_write(28, 1 | (8 << 8) | (1 << 16), "PACK_COUNTERS_SEC0: 8 rows per xy plane"),
        cfg_write(56, 2 | (32 << 16), "UNP0_ADDR_CTRL_XY_REG_1: BF16 rows"),
        cfg_write(57, 512, "UNP0_ADDR_CTRL_ZW_REG_1: 256 BF16 datums"),
        cfg_write(59, 512, "UNP1_ADDR_CTRL_ZW_REG_1: 256 BF16 datums"),
        cfg_write(64, 0x15, "THCON_SEC0_REG0: uncompressed BF16 descriptor, context xdim override"),
        cfg_write(65, 1 | (4 << 16), "THCON_SEC0_REG0+4: Y=1, Z=4 descriptor dimensions"),
        cfg_write(69, (OUTPUT_L1 >> 4) - 1, "THCON_SEC0_REG1_L1_Dest_addr: output base in 16-byte units minus one"),
        cfg_write(70, 0x551, "THCON_SEC0_REG1 format: BF16 input/output, zero compression disabled"),
        cfg_write(71, 0, "THCON_SEC0_REG1 flags: deterministic rounding, default pack behavior"),
        cfg_write(72, 0x25, "THCON_SEC0_REG2: BF16 output, throttle mode 2"),
        cfg_write(73, 0x1, "THCON_SEC0_REG2: uncompressed context 0"),
        cfg_write(76, (INPUT_A_L1 >> 4) - 1, "THCON_SEC0_REG3_Base_address: input A in 16-byte units minus one"),
        cfg_write(84, 0x00400040, "THCON_SEC0_REG5: unpack0 context destination row 64 before -64 bias"),
        cfg_write(86, 0x01000100, "THCON_SEC0_REG5: context x dimension 256"),
        cfg_write(112, 0x01000015, "THCON_SEC1_REG0: uncompressed BF16 descriptor, X=256"),
        cfg_write(113, 1 | (4 << 16), "THCON_SEC1_REG0+4: Y=1, Z=4 descriptor dimensions"),
        cfg_write(120, 0x25, "THCON_SEC1_REG2: BF16 output, throttle mode 2"),
        cfg_write(121, 0x1, "THCON_SEC1_REG2: uncompressed context 0"),
        cfg_write(124, (INPUT_B_L1 >> 4) - 1, "THCON_SEC1_REG3_Base_address: input B in 16-byte units minus one"),
    ]
    return writes


def setc16(reg, value):
    return (0xB2 << 24) | ((reg & 0xFF) << 16) | (value & 0xFFFF)


def setadcxx(counter_mask, x_end, x_start=0):
    return (0x5E << 24) | (counter_mask << 21) | (x_end << 10) | x_start


def setadc(value, dimension_index, channel_index, counter_mask):
    return (0x50 << 24) | (counter_mask << 21) | (channel_index << 20) | (dimension_index << 18) | value


def setadcxy(counter_mask, bit_mask, ch0_x=0, ch0_y=0, ch1_x=0, ch1_y=0):
    return (
        (0x51 << 24)
        | (counter_mask << 21)
        | (ch1_y << 15)
        | (ch1_x << 12)
        | (ch0_y << 9)
        | (ch0_x << 6)
        | bit_mask
    )


def incadcxy(counter_mask, ch0_x=0, ch0_y=0, ch1_x=0, ch1_y=0):
    return (
        (0x52 << 24) | (counter_mask << 21) | (ch1_y << 15) | (ch1_x << 12)
        | (ch0_y << 9) | (ch0_x << 6)
    )


def addrcrxy(counter_mask, bit_mask, ch0_x=0, ch0_y=0, ch1_x=0, ch1_y=0):
    return (
        (0x53 << 24) | (counter_mask << 21) | (ch1_y << 15) | (ch1_x << 12)
        | (ch0_y << 9) | (ch0_x << 6) | bit_mask
    )


def setadczw(counter_mask, bit_mask, ch0_z=0, ch0_w=0, ch1_z=0, ch1_w=0):
    return (
        (0x54 << 24)
        | (counter_mask << 21)
        | (ch1_w << 15)
        | (ch1_z << 12)
        | (ch0_w << 9)
        | (ch0_z << 6)
        | bit_mask
    )


def incadczw(counter_mask, ch0_z=0, ch0_w=0, ch1_z=0, ch1_w=0):
    return (
        (0x55 << 24) | (counter_mask << 21) | (ch1_w << 15) | (ch1_z << 12)
        | (ch0_w << 9) | (ch0_z << 6)
    )


def addrcrzw(counter_mask, bit_mask, ch0_z=0, ch0_w=0, ch1_z=0, ch1_w=0):
    return (
        (0x56 << 24) | (counter_mask << 21) | (ch1_w << 15) | (ch1_z << 12)
        | (ch0_w << 9) | (ch0_z << 6) | bit_mask
    )


def setrwc(bit_mask, rwc_a=0, rwc_b=0, rwc_d=0, rwc_cr=0, clear_ab_vld=0):
    return (
        (0x37 << 24) | (clear_ab_vld << 22) | (rwc_cr << 18) | (rwc_d << 14)
        | (rwc_b << 10) | (rwc_a << 6) | bit_mask
    )


def incrwc(rwc_a=0, rwc_b=0, rwc_d=0, rwc_cr=0):
    return (0x38 << 24) | (rwc_cr << 18) | (rwc_d << 14) | (rwc_b << 10) | (rwc_a << 6)


def unpacr(unpacker, src_b=False):
    return (0x42 << 24) | (int(src_b) << 23) | (1 << 7) | (1 << 6) | 1


def zeroacc_all():
    # ZEROACC clear_mode=3 invalidates all Dst rows; subsequent ELW writes revalidate rows.
    return (0x10 << 24) | (3 << 19)


def pacr(last):
    # ReadIntfSel=0 means all four interfaces; output is four Dst rows per instruction.
    return (0x41 << 24) | int(last)


def tensix_words(operation="add"):
    opcode = {"add": ELWADD_OPCODE, "mul": ELWMUL_OPCODE, "sub": ELWSUB_OPCODE}[operation]
    words = [
        setc16(0, 0),             # T0 Config state 0
        setc16(1, 0),             # Dst row offset 0
        setc16(5, 4),             # SRCA_SET.SetOvrdWithAddr=1 for UNPACR to SrcA
        setc16(41, 0),            # unpack context offsets 0
        setadcxy(0b111, 0xF),     # reset XY counters for unpack0, unpack1, packer
        setadczw(0b111, 0xF),     # reset ZW counters for unpack0, unpack1, packer
        setadcxx(0b001, 255),     # unpack0 consumes 256 BF16 input datums
        setadcxx(0b010, 255),     # unpack1 consumes 256 BF16 input datums
        setadcxx(0b100, 15),      # packer reads sixteen columns per Dst row
        unpacr(0, src_b=False),   # load input A to SrcA bank 0 and mark valid
        unpacr(1, src_b=True),    # load input B to SrcB bank 0 and mark valid
        zeroacc_all(),            # ELWMUL accumulates; invalidate Dst so initial value is zero
        setadcxy(0b100, 0x2, ch0_y=0),
        (opcode << 24),           # exactly one ELWADD word changes to ELWMUL
        0xA0000000,                # ATGETM: acquire pipe 0's supported mutex
        0xA1000000,                # ATRELM: release that mutex
        (0xA3 << 24) | (1 << 2) | (1 << 16) | (2 << 20),  # SEMINIT: semaphore 0 starts at 1, max 2
        (0xA4 << 24) | (1 << 2),  # SEMPOST: increment semaphore 0
        (0xA5 << 24) | (1 << 2),  # SEMGET: decrement semaphore 0
        (0xA6 << 24) | (1 << 15) | (1 << 2) | 1,  # SEMWAIT: semaphore 0 is nonzero
        (0xA2 << 24) | (1 << 15) | 0x80,  # STALLWAIT: wait for valid SrcA
        (0x35 << 24) | 0x3,       # GATESRCRST: reset both source gate controls
        setrwc(0xF),               # SETRWC: initialize row counters and fidelity counter
        incrwc(rwc_a=1, rwc_b=1, rwc_d=1),  # INCRWC: advance SrcA, SrcB, and Dst rows
        setrwc(0xF),               # restore row counters before packing
        setadc(1, 0, 0, 0b111),   # SETADC: set channel-0 X on both unpackers and the packer
        incadcxy(0b111, 1, 1, 1, 1),  # INCADCXY: advance all XY counters
        addrcrxy(0b111, 0xF, 1, 1, 1, 1),  # ADDRCRXY: advance all XY counters and their reload values
        setadcxy(0b111, 0xF),     # restore XY counters before packing
        setadczw(0b111, 0xF),    # initialize all ZW counters
        incadczw(0b111, 1, 1, 1, 1),  # INCADCZW: advance all ZW counters
        addrcrzw(0b111, 0xF, 1, 1, 1, 1),  # ADDRCRZW: advance all ZW counters and reload values
        setadczw(0b111, 0xF),    # restore ZW counters before packing
        setadcxx(0b100, 15),      # restore the packer's sixteen-column span
        (0x36 << 24) | (0x3 << 22),  # CLEARDVALID: release both source banks after math
        setadcxy(0b100, 0x2, ch0_y=0),
        pacr(last=False),         # Dst rows 0..3 -> output datums 0..63
        setadcxy(0b100, 0x2, ch0_y=1),
        pacr(last=True),          # Dst rows 4..7 -> output datums 64..127; flush
        # Reload sources in the alternate bank for movement and matrix probes.
        setrwc(0xF),
        setadcxy(0b011, 0xF),
        setadczw(0b011, 0xF),
        setadcxx(0b001, 255),
        setadcxx(0b010, 255),
        unpacr(0, src_b=False),
        unpacr(1, src_b=True),
        0x08002000,                # MOVD2A: Dst rows 0..3 to SrcA rows 0..3
        0x0A002000,                # MOVD2B: Dst rows 0..3 to SrcB rows 0..3
        0x0B002000,                # MOVB2A: SrcB rows 0..3 to SrcA rows 0..3
        0x12002000,                # MOVA2D: SrcA rows 0..7 to Dst rows 0..7
        0x13001000,                # MOVB2D: SrcB rows 0..7 to Dst rows 0..7
        0x16000000,                # TRNSPSRCB
        setrwc(0xF),
        0x26C00000,                # MVMUL with both source valid bits cleared on completion
        # Point unpack1 at a second, uniform-one BF16 buffer for GMPOOL's modeled requirement.
        (0x45 << 24) | (4 << 0) | (0x107F << 8),  # SETDMAREG: GPR2 low half = pool buffer base-1
        (0xB0 << 24) | (2 << 16) | 124,          # WRCFG: THCON_SEC1_REG3 base address from GPR2
        setrwc(0xF),
        setadcxy(0b011, 0xF),
        setadczw(0b011, 0xF),
        setadcxx(0b001, 255),
        setadcxx(0b010, 255),
        unpacr(0, src_b=False),
        unpacr(1, src_b=True),
        0x33C80000,                # GMPOOL: modeled 1x16-by-16x16 pool mode, clear sources
        setrwc(0xF),
        setadcxy(0b011, 0xF),
        setadczw(0b011, 0xF),
        setadcxx(0b001, 255),
        setadcxx(0b010, 255),
        unpacr(0, src_b=False),
        unpacr(1, src_b=True),
        0x34C80000,                # GAPOOL through the modeled matmul path, clear sources
        (0x45 << 24) | (4 << 0) | (0x1021 << 8),  # restore GPR2 low half = normal input B base-1
        (0xB0 << 24) | (2 << 16) | 124,          # restore THCON_SEC1_REG3 base address
        0x43000001,                # UNPACR_NOP after the current source bank is invalid
        0x1100000B,                # ZEROSRC both source matrices, normal write mode
        0x60000000,                # DMANOP
        (0x45 << 24) | (1 << 8),                 # SETDMAREG: GPR0 low half = 1
        (0x45 << 24) | (2 << 8) | 2,             # SETDMAREG: GPR1 low half = 2
        0x58002040,                # ADDDMAREG GPR0 + GPR1 -> GPR2
        0x5A003040,                # MULDMAREG GPR0 * GPR1 -> GPR3
        0xB1040000,                # RDCFG: read config word 0 to GPR4
        0xB0040000,                # WRCFG: write unchanged config word 0 from GPR4
        0xB3000000,                # RMWCIB0: zero mask/data at config word 0
        0xB4000000,                # RMWCIB1: zero mask/data at config word 0
        0xB5000000,                # RMWCIB2: zero mask/data at config word 0
        0xB6000000,                # RMWCIB3: zero mask/data at config word 0
        0xB8BF8000,                # CFGSHIFTMASK: add zero scratch to config word 0
        0x67010004,                # STOREREG: write GPR0 to NoC overlay stream 0 source
        # These SFPU dispatch probes run after packing, so they cannot change
        # the memory result checked by this example.
        0x71003F80,                # SFPLOADI LReg0 = BF16 1.0
        0x70000000,                # SFPLOAD LReg0 from current Dst row
        0x73040000,                # SFPLUT LReg0, LUT mode 4
        0x743F8000,                # SFPMULI LReg0 by BF16 1.0
        0x753F8000,                # SFPADDI LReg0 by BF16 1.0
        0x76000000,                # SFPDIVP2 LReg0, exponent immediate 0
        0x77000000,                # SFPEXEXP LReg0, LReg0
        0x78000000,                # SFPEXMAN LReg0, LReg0
        0x79000000,                # SFPIADD LReg0, LReg0
        0x7A001001,                # SFPSHFT LReg0 left by one
        0x7B000002,                # SFPSETCC from nonzero LReg0
        0x7C000010,                # SFPMOV LReg1, LReg0
        0x7D000110,                # SFPABS LReg1, LReg0
        0x7E000011,                # SFPAND LReg1, LReg0, LReg0
        0x7F000011,                # SFPOR LReg1, LReg0, LReg0
        0x80000110,                # SFPNOT LReg1, LReg0
        0x81000020,                # SFPLZ LReg2, LReg0
        0x8207F021,                # SFPSETEXP LReg2 exponent 127 from LReg0
        0x83000020,                # SFPSETMAN LReg2 mantissa from LReg0
        0x8400A130,                # SFPMAD LReg3 = LReg0 * LReg10 + LReg1
        0x850A0130,                # SFPADD LReg3 = LReg10 * LReg0 + LReg1
        0x86001930,                # SFPMUL LReg3 = LReg1 * LReg0 + LReg9
        0x87000000,                # SFPPUSHC
        0x88000000,                # SFPPOPC
        0x89000030,                # SFPSETSGN LReg3 from LReg0
        0x8A000000,                # SFPENCC, enable all lanes
        0x8B000000,                # SFPCOMPC
        0x8A000000,                # restore all-lane condition mask
        0x8C000000,                # SFPTRANSP
        0x8D000030,                # SFPXOR LReg3, LReg0
        0x8E000031,                # SFP_STOCH_RND LReg3 from LReg0, mode 1
        0x8F000000,                # SFPNOP
        0x90000030,                # SFPCAST LReg3 from LReg0
        0x72000000,                # SFPSTORE LReg0 to current Dst row
        0x71003F80,                # restore uniform LReg0 for SFPCONFIG
        0x910000B0,                # SFPCONFIG: copy LReg0 to constant LReg11
        0x92000130,                # SFPSWAP LReg3 and LReg1
        0x94000033,                # SFPSHFT2 LReg3, mode 3
        0x95000032,                # SFPLUTFP32 LReg3, mode 2
        0x96000048,                # SFPLE LReg4, LReg0 -> predicate
        0x97000048,                # SFPGT LReg4, LReg0 -> predicate
        0x98012940,                # SFPMUL24 LReg4 = LReg1 * LReg2, LReg9 constant
        0x99000040,                # SFPARECIP LReg4 from LReg0
    ]
    return words


def describe_tensix_word(index, word):
    opcode = (word >> 24) & 0xFF
    names = {
        0x10: "ZEROACC",
        0x27: "ELWMUL",
        0x28: "ELWADD",
        0x30: "ELWSUB",
        0x41: "PACR",
        0x42: "UNPACR",
        0x51: "SETADCXY",
        0x54: "SETADCZW",
        0x5E: "SETADCXX",
        0xB2: "SETC16",
        0x70: "SFPLOAD", 0x71: "SFPLOADI", 0x72: "SFPSTORE", 0x73: "SFPLUT",
        0x74: "SFPMULI", 0x75: "SFPADDI", 0x76: "SFPDIVP2", 0x77: "SFPEXEXP",
        0x78: "SFPEXMAN", 0x79: "SFPIADD", 0x7A: "SFPSHFT", 0x7B: "SFPSETCC",
        0x7C: "SFPMOV", 0x7D: "SFPABS", 0x7E: "SFPAND", 0x7F: "SFPOR",
        0x80: "SFPNOT", 0x81: "SFPLZ", 0x82: "SFPSETEXP", 0x83: "SFPSETMAN",
        0x84: "SFPMAD", 0x85: "SFPADD", 0x86: "SFPMUL", 0x87: "SFPPUSHC",
        0x88: "SFPPOPC", 0x89: "SFPSETSGN", 0x8A: "SFPENCC", 0x8B: "SFPCOMPC",
        0x8C: "SFPTRANSP", 0x8D: "SFPXOR", 0x8E: "SFP_STOCH_RND", 0x8F: "SFPNOP",
        0x90: "SFPCAST", 0x91: "SFPCONFIG", 0x92: "SFPSWAP", 0x94: "SFPSHFT2",
        0x95: "SFPLUTFP32", 0x96: "SFPLE", 0x97: "SFPGT", 0x98: "SFPMUL24",
        0x99: "SFPARECIP",
        0x08: "MOVD2A", 0x0A: "MOVD2B", 0x0B: "MOVB2A", 0x11: "ZEROSRC",
        0x12: "MOVA2D", 0x13: "MOVB2D", 0x16: "TRNSPSRCB", 0x26: "MVMUL",
        0x33: "GMPOOL", 0x34: "GAPOOL", 0x43: "UNPACR_NOP", 0x45: "SETDMAREG",
        0x58: "ADDDMAREG", 0x5A: "MULDMAREG", 0x60: "DMANOP", 0x67: "STOREREG",
        0xB0: "WRCFG", 0xB1: "RDCFG", 0xB3: "RMWCIB0", 0xB4: "RMWCIB1",
        0xB5: "RMWCIB2", 0xB6: "RMWCIB3", 0xB8: "CFGSHIFTMASK",
    }
    name = names.get(opcode, "UNKNOWN")
    if name == "SETC16":
        detail = f"reg={(word >> 16) & 0xff} value=0x{word & 0xffff:04x}"
    elif name == "SETADCXX":
        detail = f"counter_mask={(word >> 21) & 7} x_end={(word >> 10) & 0x3ff} x_start={word & 0x3ff}"
    elif name == "SETADCXY":
        detail = f"counter_mask={(word >> 21) & 7} bit_mask={word & 15} ch0_xy=({(word >> 6) & 7},{(word >> 9) & 7}) ch1_xy=({(word >> 12) & 7},{(word >> 15) & 7})"
    elif name == "SETADCZW":
        detail = f"counter_mask={(word >> 21) & 7} bit_mask={word & 15} ch0_zw=({(word >> 6) & 7},{(word >> 9) & 7}) ch1_zw=({(word >> 12) & 7},{(word >> 15) & 7})"
    elif name == "UNPACR":
        detail = f"block_selection={(word >> 23) & 1} last={word & 1} set_dat_valid={(word >> 6) & 1} override_thread={(word >> 7) & 1}"
    elif name == "ZEROACC":
        detail = f"where={word & 0x3ff} addr_mode={(word >> 14) & 7} clear_mode={(word >> 19) & 3}"
    elif name in ("ELWADD", "ELWMUL", "ELWSUB"):
        detail = f"dst={word & 0x3ff} addr_mode={(word >> 14) & 7} instr_mod19={(word >> 19) & 3} dest_accum_en={(word >> 21) & 1} clear_dvalid={(word >> 22) & 3}"
    elif name == "PACR":
        detail = f"last={word & 1} read_intf_sel={(word >> 8) & 15} addr_mode={(word >> 15) & 3}"
    else:
        detail = ""
    return f"{index:02d} 0x{word:08x} {name} {detail}".rstrip()


def bf16_to_float(value):
    bits = (value & 0xFFFF) << 16
    return struct.unpack("<f", bits.to_bytes(4, "little"))[0]


def float_to_bf16(value):
    raw = int.from_bytes(struct.pack("<f", float(value)), "little")
    if raw & 0xFFFF:
        raise ValueError(f"test result {value} is not exactly representable as BF16")
    return raw >> 16


def case_inputs(case):
    if case not in (0, 1):
        raise ValueError("case must be 0 or 1")
    a_exponents = []
    b_exponents = []
    for row in range(8):
        for col in range(16):
            if case == 0:
                a_exponents.append(((row * 3 + col * 5) % 4) - 1)
                b_exponents.append((row * 7 + col * 3 + 1) % 4)
            else:
                a_exponents.append((row * 5 + col * 3 + 1) % 4)
                b_exponents.append(((row * 3 + col * 7) % 4) - 1)
    a = [float_to_bf16(2.0 ** power) for power in a_exponents]
    b = [float_to_bf16(2.0 ** power) for power in b_exponents]
    return a + [0] * (UNPACK_ELEMENTS - ACTIVE_ELEMENTS), b + [0] * (UNPACK_ELEMENTS - ACTIVE_ELEMENTS)


def pack_u16(values):
    return b"".join((value & 0xFFFF).to_bytes(2, "little") for value in values)


def pack_config_table(writes):
    data = bytearray()
    for write in writes:
        data += write["offset"].to_bytes(4, "little")
        data += write["value"].to_bytes(4, "little")
    return bytes(data)


def pack_words(words):
    return b"".join((word & 0xFFFFFFFF).to_bytes(4, "little") for word in words)


def parse_words(path, expected_count):
    raw = Path(path).read_bytes()
    if len(raw) != expected_count * 4:
        raise ValueError(f"{path} contains {len(raw)} bytes, expected {expected_count * 4}")
    return [int.from_bytes(raw[i:i + 4], "little") for i in range(0, len(raw), 4)]


def output_reference(input_a, input_b, operation):
    results = []
    for a, b in zip(input_a[:ACTIVE_ELEMENTS], input_b[:ACTIVE_ELEMENTS]):
        av, bv = bf16_to_float(a), bf16_to_float(b)
        expected = {"add": av + bv, "mul": av * bv, "sub": av - bv}[operation]
        results.append(float_to_bf16(expected))
    return results


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def find_metadata(start, filename):
    for directory in (start, *start.parents):
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    return None


def enrich_snapshot(snapshot):
    """Attach decoded BF16 values to the official model's raw trace words."""
    result = json.loads(json.dumps(snapshot))
    for name in ("src_a", "src_b"):
        source = result.get(name)
        if source and source.get("format") == 5:
            source["decoded_bf16_hex"] = [
                [f"0x{int(value, 16) >> 16:04x}" for value in row]
                for row in source["rows"]
            ]
            source["decoded_bf16_values"] = [
                [bf16_to_float(int(value, 16) >> 16) for value in row]
                for row in source["rows"]
            ]
    destination = result.get("dst", {})
    for row in destination.get("rows", []):
        row["decoded_bf16_values"] = [
            bf16_to_float(int(value, 16)) for value in row.get("decoded_bf16", [])
        ]
    return result


def build_artifacts(out_dir, case, operation):
    out_dir.mkdir(parents=True, exist_ok=True)
    input_a, input_b = case_inputs(case)
    writes = config_table(case)
    words = tensix_words(operation)
    assembler = RV32()
    source, rv_words, kernel, disassembly = assembler.build(len(writes), len(words))
    (out_dir / "kernel.S").write_text(source, encoding="ascii")
    (out_dir / "kernel.bin").write_bytes(kernel)
    (out_dir / "kernel.disasm").write_text(disassembly, encoding="ascii")
    config_bytes = pack_config_table(writes)
    word_bytes = pack_words(words)
    (out_dir / "config.bin").write_bytes(config_bytes)
    (out_dir / "config_words.txt").write_text(
        "\n".join(f"offset=0x{w['offset']:04x} reg={w['index']:03d} value=0x{w['value']:08x} {w['name']}" for w in writes) + "\n",
        encoding="ascii",
    )
    (out_dir / "tensix_words.bin").write_bytes(word_bytes)
    (out_dir / "tensix_words.txt").write_text(
        "\n".join(describe_tensix_word(i, word) for i, word in enumerate(words)) + "\n",
        encoding="ascii",
    )
    (out_dir / "input_a.bin").write_bytes(pack_u16(input_a))
    (out_dir / "input_b.bin").write_bytes(pack_u16(input_b))
    (out_dir / "pool_input_b.bin").write_bytes(pack_u16([0x3F80] * UNPACK_ELEMENTS))
    output_initial = (OUTPUT_SENTINEL_BF16.to_bytes(2, "little") * ACTIVE_ELEMENTS)
    (out_dir / "output_initial.bin").write_bytes(output_initial)
    (out_dir / "output.bin").write_bytes(output_initial)
    (out_dir / "registers_before.json").write_text(json.dumps({
        "capture_status": "not exposed by stock ttsim C ABI; instrumented run required",
        "observation_kind": "unavailable, no internal state is invented",
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / "registers_after.json").write_text(json.dumps({
        "capture_status": "not exposed by stock ttsim C ABI; instrumented run required",
        "observation_kind": "unavailable, no internal state is invented",
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    command = shlex.join([sys.executable, *sys.argv])
    (out_dir / "commands.log").write_text(command + "\n", encoding="utf-8")
    return input_a, input_b, writes, words, rv_words, kernel, config_bytes, word_bytes


def execute(out_dir, args, input_a, input_b, writes, words, rv_words, kernel, config_bytes, word_bytes, operation):
    trace_lines = [
        "MMIO records are host C-ABI transactions, not proof of device arithmetic.",
        f"RV32 B entry=0x{KERNEL_L1:08x}; raw Tensix table=0x{TENSIX_WORDS_L1:08x}.",
        f"Tensix FIFO push store PC is listed in kernel.disasm; payload words are unrotated.",
    ]
    deadline = time.monotonic() + args.timeout
    clocks = 0
    observed_result = bytes(8)
    output_window = bytes(OUTPUT_BYTES + 40)
    with Simulator(out_dir / "mmio.jsonl") as sim:
        bdf = 0
        identity = sim.config32(bdf, 0)
        if identity != 0xB1401E52:
            raise RuntimeError(f"expected Blackhole PCI endpoint, got 0x{identity:08x}")
        absent = sim.config32(1 << 3, 0)
        if absent != 0xFFFFFFFF:
            raise RuntimeError("selected library is not a single-chip Blackhole build")
        bar0 = pci_bar_base(sim, bdf, 0x10)
        if bar0 != 0x100000000:
            raise RuntimeError(f"unexpected BAR0 base 0x{bar0:016x}")

        l1_tlb = configure_bh_tlb(sim, bar0, 0, *TILE, 0)
        mmio_tlb = configure_bh_tlb(sim, bar0, 1, *TILE, RISCV_MMIO_BASE & -TLB_SIZE)
        scratch_bar = bar0
        mmio_bar = bar0 + TLB_SIZE
        soft_reset_bar = mmio_bar + (SOFT_RESET_0 - mmio_tlb["local_base"])

        # Hold every baby RISCV, install all bytes and sentinels, then release BRISC only.
        sim.write32(soft_reset_bar, SOFT_RESET_ALL_RISCV)
        sim.write(scratch_bar + KERNEL_L1, kernel)
        sim.write(scratch_bar + CONFIG_TABLE_L1, config_bytes)
        sim.write(scratch_bar + TENSIX_WORDS_L1, word_bytes)
        sim.write(scratch_bar + INPUT_A_L1, pack_u16(input_a))
        sim.write(scratch_bar + INPUT_A_GUARD_L1, GUARD_BYTES)
        sim.write(scratch_bar + INPUT_B_L1, pack_u16(input_b))
        sim.write(scratch_bar + POOL_INPUT_B_L1, pack_u16([0x3F80] * UNPACK_ELEMENTS))
        sim.write(scratch_bar + INPUT_B_GUARD_L1, GUARD_BYTES)
        sim.write(scratch_bar + OUTPUT_GUARD_BEFORE_L1, GUARD_BYTES)
        sim.write(scratch_bar + OUTPUT_L1, (OUTPUT_SENTINEL_BF16.to_bytes(2, "little") * ACTIVE_ELEMENTS))
        sim.write(scratch_bar + OUTPUT_GUARD_AFTER_L1, GUARD_BYTES)
        sim.write32(scratch_bar + RESULT_L1, RESULT_SENTINEL)
        sim.write32(scratch_bar + RESULT_L1 + 4, DONE_SENTINEL)
        sim.write32(soft_reset_bar, SOFT_RESET_BRISC_ONLY_RUN)
        trace_lines.append(
            f"BRISC image/config/raw-word tables installed; reset released with SOFT_RESET_0=0x{SOFT_RESET_BRISC_ONLY_RUN:08x}."
        )

        while clocks < args.clock_limit and time.monotonic() < deadline:
            step = min(args.clock_step, args.clock_limit - clocks)
            sim.clock(step)
            clocks += step
            observed_result = sim.read(scratch_bar + RESULT_L1, 8)
            marker, done = struct.unpack("<II", observed_result)
            trace_lines.append(f"clock={clocks} device_marker=0x{marker:08x} done=0x{done:08x}")
            if marker == RESULT_MARKER and done == DONE_MARKER:
                break
        output_window = sim.read(scratch_bar + OUTPUT_GUARD_BEFORE_L1, OUTPUT_BYTES + 40)

    output_bytes = output_window[16:16 + OUTPUT_BYTES]
    (out_dir / "output.bin").write_bytes(output_bytes)
    (out_dir / "output_window.bin").write_bytes(output_window)
    (out_dir / "device_markers.bin").write_bytes(observed_result)
    observed_values = [int.from_bytes(output_bytes[i:i + 2], "little") for i in range(0, len(output_bytes), 2)]
    expected_values = output_reference(input_a, input_b, operation)
    output_records = []
    for index, (a_bits, b_bits, observed, expected) in enumerate(zip(input_a, input_b, observed_values, expected_values)):
        output_records.append({
            "index": index,
            "row": index // 16,
            "column": index % 16,
            "input_a_bf16": f"0x{a_bits:04x}",
            "input_a_value": bf16_to_float(a_bits),
            "input_b_bf16": f"0x{b_bits:04x}",
            "input_b_value": bf16_to_float(b_bits),
            "observed_bf16": f"0x{observed:04x}",
            "observed_value": bf16_to_float(observed),
            "host_reference_bf16": f"0x{expected:04x}",
            "host_reference_value": bf16_to_float(expected),
            "matches_reference": observed == expected,
        })
    decoded_output = {
        "observation": "actual bytes read from tile-local L1 through the BAR/TLB C ABI after the device marker",
        "operation": operation,
        "case": args.case,
        "format": "BF16 little-endian",
        "tile_local_address": f"0x{OUTPUT_L1:08x}",
        "active_elements": ACTIVE_ELEMENTS,
        "raw_bytes_sha256": sha256(output_bytes),
        "values": output_records,
    }
    (out_dir / "output_decoded.json").write_text(json.dumps(decoded_output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    mismatches = [
        {"index": i, "expected_bf16": f"0x{expected:04x}", "observed_bf16": f"0x{observed_values[i]:04x}"}
        for i, expected in enumerate(expected_values) if observed_values[i] != expected
    ]
    marker, done = struct.unpack("<II", observed_result)
    guards_ok = (
        output_window[:16] == GUARD_BYTES
        and output_window[16 + OUTPUT_BYTES:16 + OUTPUT_BYTES + 16] == GUARD_BYTES
        and output_window[16 + OUTPUT_BYTES + 16:16 + OUTPUT_BYTES + 24] == observed_result
    )
    completed = marker == RESULT_MARKER and done == DONE_MARKER
    passed = completed and not mismatches and guards_ok
    validation = {
        "status": "PASS" if passed else "FAIL",
        "operation": operation,
        "case": args.case,
        "arithmetic": "all 128 BF16 results compared bit-for-bit against host reference",
        "active_elements": ACTIVE_ELEMENTS,
        "padded_unpack_elements": UNPACK_ELEMENTS - ACTIVE_ELEMENTS,
        "device_written_marker": f"0x{marker:08x}",
        "expected_marker": f"0x{RESULT_MARKER:08x}",
        "device_written_done": done,
        "expected_done": DONE_MARKER,
        "output_guards_pass": guards_ok,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:16],
        "clock_count": clocks,
        "clock_limit": args.clock_limit,
        "timeout_seconds": args.timeout,
        "device_completion_rule": "BRISC observes the final packed BF16 pair differ from its sentinel, then writes marker and done in L1",
    }
    (out_dir / "validation.json").write_text(json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / "trace.log").write_text("\n".join(trace_lines) + "\n", encoding="utf-8")
    manifest = {
        "stage": {"add": "tensix_elwadd", "mul": "tensix_elwmul", "sub": "tensix_elwsub"}[operation],
        "chip": "single-chip Blackhole",
        "simulator_revision": TTsim_REVISION,
        "isa_documentation_revision": ISA_DOCS_REVISION,
        "blackhole_py_reference_revision": BLACKHOLE_PY_REVISION,
        "simulator_library": str(Path(os.environ["TTSIM_LIB"]).resolve()),
        "simulator_library_sha256": sha256(Path(os.environ["TTSIM_LIB"]).read_bytes()),
        "simulator_setup_manifest": str(find_metadata(Path(os.environ["TTSIM_LIB"]).resolve().parent, "setup_manifest.json"))
            if find_metadata(Path(os.environ["TTSIM_LIB"]).resolve().parent, "setup_manifest.json") else None,
        "host": {"os": platform.platform(), "architecture": platform.machine()},
        "tool_versions": {"python": sys.version, "platform_python": platform.python_version()},
        "source_hashes": {
            "example_python_sha256": sha256(Path(__file__).read_bytes()),
            "kernel_source_sha256": sha256((out_dir / "kernel.S").read_bytes()),
        },
        "tile": {"x": TILE[0], "y": TILE[1]},
        "issuer": "BRISC / RISCV B, raw SW pushes to T0 instruction FIFO",
        "issuer_pc": "RV32 PC 0x0000003c (kernel.disasm push_loop: sw t0,0(t3) to 0xFFE40000); Tensix execution is asynchronous after FIFO acceptance",
        "instruction_representations": {
            "kernel.bin": "RV32I bytes executed by BRISC",
            "tensix_words.bin": "unrotated raw 32-bit Tensix words loaded by BRISC and SW-pushed to 0xFFE40000",
            "inline_ttinsn_used": False,
        },
        "addresses": {
            "bar0_base": "0x0000000100000000",
            "bar0_size": BAR0_SIZE,
            "tile_local_l1": f"0x{INPUT_A_L1:08x}",
            "kernel_l1": f"0x{KERNEL_L1:08x}",
            "config_table_l1": f"0x{CONFIG_TABLE_L1:08x}",
            "tensix_words_l1": f"0x{TENSIX_WORDS_L1:08x}",
            "input_a_l1": f"0x{INPUT_A_L1:08x}",
            "input_b_l1": f"0x{INPUT_B_L1:08x}",
            "pool_input_b_l1": f"0x{POOL_INPUT_B_L1:08x}",
            "output_l1": f"0x{OUTPUT_L1:08x}",
            "output_guard_before_l1": f"0x{OUTPUT_GUARD_BEFORE_L1:08x}",
            "output_guard_after_l1": f"0x{OUTPUT_GUARD_AFTER_L1:08x}",
            "completion_l1": f"0x{RESULT_L1:08x}",
            "tensix_config_base": f"0x{TENSIX_CFG_BASE:08x}",
            "tensix_fifo_t0": f"0x{INSTRN_BUF_BASE:08x}",
            "soft_reset_register": f"0x{SOFT_RESET_0:08x}",
        },
        "tlb_windows": [l1_tlb, mmio_tlb],
        "data_layout": {
            "input_format": "BF16 little-endian",
            "input_bytes_per_engine": INPUT_BYTES,
            "input_datums_per_engine": UNPACK_ELEMENTS,
            "active_datums": ACTIVE_ELEMENTS,
            "math_footprint": "rows 0..7, columns 0..15; remaining 128 unpacked values are padding",
            "output_format": "BF16 little-endian",
            "output_bytes": OUTPUT_BYTES,
        },
        "configuration_writes": writes,
        "tensix_word_count": len(words),
        "operation_word_index": OPERATION_WORD_INDEX,
        "operation_word_file_offset": OPERATION_WORD_INDEX * 4,
        "operation_word_load_address": f"0x{TENSIX_WORDS_L1 + OPERATION_WORD_INDEX * 4:08x}",
        "operation_word": f"0x{words[OPERATION_WORD_INDEX]:08x}",
        "operation_word_decoded": describe_tensix_word(OPERATION_WORD_INDEX, words[OPERATION_WORD_INDEX]),
        "kernel_sha256": sha256(kernel),
        "output_sha256": sha256(output_bytes),
        "output_decoded_json_sha256": sha256((out_dir / "output_decoded.json").read_bytes()),
        "config_table_sha256": sha256(config_bytes),
        "tensix_words_sha256": sha256(word_bytes),
        "input_a_sha256": sha256(pack_u16(input_a)),
        "input_b_sha256": sha256(pack_u16(input_b)),
        "pool_input_b_sha256": sha256(pack_u16([0x3F80] * UNPACK_ELEMENTS)),
        "validation": validation,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"tile={TILE} operation={operation.upper()} case={args.case} active=8x16 BF16")
    print(f"BRISC clocks={clocks}/{args.clock_limit} marker=0x{marker:08x} done={done}")
    print(f"output first={output_bytes[:16].hex()} last={output_bytes[-16:].hex()} mismatches={len(mismatches)} guards={guards_ok}")
    print(f"result={'PASS' if passed else 'FAIL'}; artifacts={out_dir}")
    if not passed:
        if not completed:
            raise TimeoutError(f"device did not complete within time/clock bound; see {out_dir}/trace.log")
        raise AssertionError(f"Tensix {operation.upper()} validation failed; see {out_dir}/validation.json")


def load_image(out_dir, args, operation):
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    input_a = [int.from_bytes((out_dir / "input_a.bin").read_bytes()[i:i + 2], "little") for i in range(0, INPUT_BYTES, 2)]
    input_b = [int.from_bytes((out_dir / "input_b.bin").read_bytes()[i:i + 2], "little") for i in range(0, INPUT_BYTES, 2)]
    config_bytes = (out_dir / "config.bin").read_bytes()
    if len(config_bytes) % 8:
        raise ValueError("config.bin must contain whole offset/value pairs")
    writes = []
    config_lines = (out_dir / "config_words.txt").read_text(encoding="ascii").splitlines()
    for line in config_lines:
        parts = line.split()
        offset = int(parts[0].split("=", 1)[1], 16)
        index = int(parts[1].split("=", 1)[1])
        value = int(parts[2].split("=", 1)[1], 16)
        writes.append({"index": index, "offset": offset, "name": "loaded artifact", "value": value})
    kernel = (out_dir / "kernel.bin").read_bytes()
    word_bytes = (out_dir / "tensix_words.bin").read_bytes()
    words = parse_words(out_dir / "tensix_words.bin", manifest["tensix_word_count"])
    if len(kernel) % 4 or sha256(kernel) != manifest["kernel_sha256"]:
        raise ValueError("loaded kernel does not match artifact manifest")
    if sha256(config_bytes) != manifest["config_table_sha256"]:
        raise ValueError("loaded config.bin does not match artifact manifest")
    if sha256(word_bytes) != manifest["tensix_words_sha256"]:
        raise ValueError("loaded Tensix stream does not match artifact manifest")
    return input_a, input_b, writes, words, [], kernel, config_bytes, word_bytes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=int, choices=(0, 1), default=0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--clock-limit", type=int, default=20000)
    parser.add_argument("--clock-step", type=int, default=64)
    parser.add_argument("--out-dir", default="out/mul")
    parser.add_argument("--load-image-dir", help="execute stored image bytes without regenerating them")
    parser.add_argument("--expected-operation", choices=("add", "mul", "sub"), default="mul")
    args = parser.parse_args()
    if args.timeout <= 0 or args.clock_limit <= 0 or args.clock_step <= 0:
        parser.error("timeout, clock-limit, and clock-step must be positive")
    out_dir = Path(args.out_dir)
    if args.load_image_dir:
        image_dir = Path(args.load_image_dir)
        out_dir = image_dir
        image = load_image(image_dir, args, args.expected_operation)
    else:
        image = build_artifacts(out_dir, args.case, args.expected_operation)
    execute(out_dir, args, *image, args.expected_operation)


if __name__ == "__main__":
    main()
