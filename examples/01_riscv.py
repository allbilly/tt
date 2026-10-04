#!/usr/bin/env python3
"""Run a handwritten RV32I marker kernel on one official simulated BH worker tile."""

import argparse
import hashlib
import json
import platform
import shlex
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
SOFT_RESET_ALL = 0x00047800
SOFT_RESET_BRISC_ONLY_RUN = 0x00047000
KERNEL_L1 = 0x00000000
RESULT_L1 = 0x00010000
RESULT_SENTINEL = 0xDEADBEEF
DONE_SENTINEL = 0xA5A5A5A5
RESULT_MARKER = 0x13579BDF
DONE_MARKER = 1


def encode_lui(rd, imm20):
    return ((imm20 & 0xFFFFF) << 12) | (rd << 7) | 0x37


def encode_addi(rd, rs1, imm12):
    return ((imm12 & 0xFFF) << 20) | (rs1 << 15) | (rd << 7) | 0x13


def encode_sw(rs2, rs1, imm12):
    imm = imm12 & 0xFFF
    return (((imm >> 5) & 0x7F) << 25) | (rs2 << 20) | (rs1 << 15) | (2 << 12) | ((imm & 0x1F) << 7) | 0x23


def build_kernel():
    # RV32I, not Tensix words. B starts at local PC 0 after soft-reset release.
    source = '''\
.section .text
.globl _start
_start:
    lui   t0, 0x10
    lui   t1, 0x1357a
    addi  t1, t1, -1057
    sw    t1, 0(t0)
    addi  t2, zero, 1
    sw    t2, 4(t0)
1:  jal   zero, 1b
'''
    words = [
        ('lui t0, 0x10', encode_lui(5, 0x10)),
        ('lui t1, 0x1357a', encode_lui(6, 0x1357A)),
        ('addi t1, t1, -1057', encode_addi(6, 6, -1057)),
        ('sw t1, 0(t0)', encode_sw(6, 5, 0)),
        ('addi t2, zero, 1', encode_addi(7, 0, 1)),
        ('sw t2, 4(t0)', encode_sw(7, 5, 4)),
        ('jal zero, 0', 0x0000006F),
    ]
    image = b''.join(word.to_bytes(4, 'little') for _, word in words)
    disassembly = '\n'.join(
        f'{index * 4:08x}: {word:08x}  {assembly}'
        for index, (assembly, word) in enumerate(words)
    ) + '\n'
    return source, words, image, disassembly


def u32(raw, offset=0):
    return int.from_bytes(raw[offset:offset + 4], 'little')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=float, default=10.0)
    parser.add_argument('--clock-limit', type=int, default=20000)
    parser.add_argument('--clock-step', type=int, default=64)
    parser.add_argument('--out-dir', default='out/riscv')
    args = parser.parse_args()
    if args.timeout <= 0 or args.clock_limit <= 0 or args.clock_step <= 0:
        parser.error('timeout, clock-limit, and clock-step must be positive')

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    source, words, kernel, disassembly = build_kernel()
    (out_dir / 'kernel.S').write_text(source, encoding='ascii')
    (out_dir / 'kernel.bin').write_bytes(kernel)
    (out_dir / 'kernel.disasm').write_text(disassembly, encoding='ascii')
    (out_dir / 'input_a.bin').write_bytes(RESULT_SENTINEL.to_bytes(4, 'little'))
    (out_dir / 'input_b.bin').write_bytes(DONE_SENTINEL.to_bytes(4, 'little'))
    command = shlex.join([sys.executable, *sys.argv])
    (out_dir / 'commands.log').write_text(command + '\n', encoding='utf-8')

    trace = []
    deadline = time.monotonic() + args.timeout
    clocks = 0
    with Simulator(out_dir / 'mmio.jsonl') as sim:
        bdf = 0
        identity = sim.config32(bdf, 0)
        if identity != 0xB1401E52:
            raise RuntimeError(f'expected Blackhole PCI endpoint, got 0x{identity:08x}')
        absent = sim.config32(1 << 3, 0)
        if absent != 0xFFFFFFFF:
            raise RuntimeError('the selected library is not a single-chip Blackhole build')
        bar0 = pci_bar_base(sim, bdf, 0x10)
        if bar0 != 0x100000000:
            raise RuntimeError(f'unexpected BAR0 base 0x{bar0:016x}')

        l1_tlb = configure_bh_tlb(sim, bar0, 0, *TILE, 0)
        mmio_tlb = configure_bh_tlb(sim, bar0, 1, *TILE, RISCV_MMIO_BASE & -TLB_SIZE)
        scratch_bar = bar0
        mmio_bar = bar0 + TLB_SIZE
        soft_reset_bar = mmio_bar + (SOFT_RESET_0 - mmio_tlb['local_base'])

        # Hold all five baby RISCV cores, install the image and sentinel state,
        # then release only BRISC (RISCV B) from architectural soft reset.
        sim.write32(soft_reset_bar, SOFT_RESET_ALL)
        sim.write(scratch_bar + KERNEL_L1, kernel)
        sim.write(scratch_bar + RESULT_L1, RESULT_SENTINEL.to_bytes(4, 'little'))
        sim.write(scratch_bar + RESULT_L1 + 4, DONE_SENTINEL.to_bytes(4, 'little'))
        sim.write32(soft_reset_bar, SOFT_RESET_BRISC_ONLY_RUN)
        trace.append(
            f'issued BRISC image at local L1 0x{KERNEL_L1:08x}; '
            f'released reset with SOFT_RESET_0=0x{SOFT_RESET_BRISC_ONLY_RUN:08x}',
        )

        observed = bytes(8)
        while clocks < args.clock_limit and time.monotonic() < deadline:
            step = min(args.clock_step, args.clock_limit - clocks)
            sim.clock(step)
            clocks += step
            observed = sim.read(scratch_bar + RESULT_L1, 8)
            result = u32(observed)
            done = u32(observed, 4)
            trace.append(f'clock={clocks} result=0x{result:08x} done=0x{done:08x}')
            if result == RESULT_MARKER and done == DONE_MARKER:
                break

        (out_dir / 'output.bin').write_bytes(observed)
        completed = u32(observed) == RESULT_MARKER and u32(observed, 4) == DONE_MARKER
        (out_dir / 'trace.log').write_text('\n'.join(trace) + '\n', encoding='utf-8')
        validation = {
            'status': 'PASS' if completed else 'FAIL',
            'device_written_result': f'0x{u32(observed):08x}',
            'expected_result': f'0x{RESULT_MARKER:08x}',
            'device_written_done': u32(observed, 4),
            'expected_done': DONE_MARKER,
            'host_initialized_only_sentinels': [f'0x{RESULT_SENTINEL:08x}', f'0x{DONE_SENTINEL:08x}'],
            'clocks': clocks,
            'clock_limit': args.clock_limit,
            'timeout_seconds': args.timeout,
        }
        (out_dir / 'validation.json').write_text(
            json.dumps(validation, indent=2, sort_keys=True) + '\n', encoding='utf-8',
        )
        manifest = {
            'stage': 'rv32_bootstrap',
            'chip': 'single-chip Blackhole',
            'simulator_revision': '3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a',
            'simulator_library': str(sim.path),
            'simulator_library_sha256': hashlib.sha256(Path(sim.path).read_bytes()).hexdigest(),
            'host': {'os': platform.platform(), 'architecture': platform.machine(), 'python': sys.version},
            'tile': {'x': TILE[0], 'y': TILE[1]},
            'issuer': 'BRISC / RISCV B',
            'entry_local_address': KERNEL_L1,
            'result_local_address': RESULT_L1,
            'bar0_base': f'0x{bar0:016x}',
            'tlb_windows': [l1_tlb, mmio_tlb],
            'soft_reset_register_local_address': f'0x{SOFT_RESET_0:08x}',
            'kernel_sha256': hashlib.sha256(kernel).hexdigest(),
            'kernel_source_sha256': hashlib.sha256((out_dir / 'kernel.S').read_bytes()).hexdigest(),
            'python_source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'instruction_count': len(words),
            'execution_trace_kind': 'host-observed output polling and BAR transactions',
            'clock_limit': args.clock_limit,
            'timeout_seconds': args.timeout,
            'validation': validation,
        }
        (out_dir / 'manifest.json').write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8',
        )

    print(f'RV32 kernel bytes={len(kernel)} entry=tile-local 0x{KERNEL_L1:08x}')
    print(f'tile={TILE} issuer=BRISC/RISCV B clocks={clocks}/{args.clock_limit}')
    print(f'output={observed.hex()} result=0x{u32(observed):08x} done={u32(observed, 4)}')
    if not completed:
        raise TimeoutError(
            f'BRISC marker not observed before clock/time bound; see {out_dir}/trace.log',
        )
    print(f'result=PASS; artifacts={out_dir}')


if __name__ == '__main__':
    main()
