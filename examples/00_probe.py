#!/usr/bin/env python3
"""Verify the single-chip Blackhole PCI endpoint and one BAR-to-L1 TLB window."""

import argparse
import hashlib
import json
import platform
import shlex
import sys
from pathlib import Path

from _ttsim import Simulator, configure_bh_tlb0, pci_bar_base


VENDOR_DEVICE = 0xB1401E52  # ttsim v1.10.1 PCI config-space identity
BAR0_SIZE = 512 * 1024 * 1024
TLB_CONFIG_BAR0_OFFSET = 0x1FC00000
TLB_SIZE = 2 * 1024 * 1024
TILE = (1, 2)  # Valid worker in the pinned unharvested single-chip BH topology
SCRATCH_L1 = 0x10000
LEFT_GUARD = bytes.fromhex('a55ac33c')
RIGHT_GUARD = bytes.fromhex('9669f00f')
PATTERN = bytes((index * 37 + 11) & 0xFF for index in range(31))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--transactions', default='out/probe/mmio.jsonl')
    args = parser.parse_args()
    transactions_path = Path(args.transactions)

    with Simulator(transactions_path) as sim:
        bdf = 0
        identity = sim.config32(bdf, 0x00)
        if identity != VENDOR_DEVICE:
            raise RuntimeError(
                f'expected Blackhole endpoint 0x{VENDOR_DEVICE:08x}, got 0x{identity:08x}',
            )
        absent = sim.config32(1 << 3, 0x00)
        if absent != 0xFFFFFFFF:
            raise RuntimeError(f'expected a single-chip build; PCI device 1 returned 0x{absent:08x}')

        bar0 = pci_bar_base(sim, bdf, 0x10)
        bar2 = pci_bar_base(sim, bdf, 0x18)
        bar4 = pci_bar_base(sim, bdf, 0x20)
        if bar0 != 0x100000000 or bar2 != 0x120000000 or bar4 != 0x800000000:
            raise RuntimeError(
                f'unexpected pinned BAR mapping: BAR0=0x{bar0:x}, BAR2=0x{bar2:x}, BAR4=0x{bar4:x}',
            )
        if TLB_CONFIG_BAR0_OFFSET + 210 * 3 * 4 > BAR0_SIZE:
            raise RuntimeError('Blackhole TLB configuration table exceeds BAR0')

        tlb = configure_bh_tlb0(sim, bar0, *TILE)
        left = bar0 + SCRATCH_L1 - len(LEFT_GUARD)
        data = bar0 + SCRATCH_L1
        right = data + len(PATTERN)
        sim.write(left, LEFT_GUARD)
        sim.write(data, PATTERN)
        sim.write(right, RIGHT_GUARD)

        observed_left = sim.read(left, len(LEFT_GUARD))
        observed = sim.read(data, len(PATTERN))
        observed_right = sim.read(right, len(RIGHT_GUARD))
        if (observed_left, observed, observed_right) != (LEFT_GUARD, PATTERN, RIGHT_GUARD):
            raise RuntimeError('BAR/TLB/L1 readback or guard verification failed')

        print(f'library={Path(sim.path)}')
        print(f'endpoint BDF=0000:00:{(bdf >> 3) & 0x1f:02x}.{bdf & 7} vendor/device=0x{identity:08x}')
        print(f'BAR0 physical=0x{bar0:016x} size=0x{BAR0_SIZE:x}')
        print(f'BAR2 physical=0x{bar2:016x}; BAR4 physical=0x{bar4:016x}')
        print(
            f'TLB index={tlb["index"]} size=0x{TLB_SIZE:x} config BAR0+0x{TLB_CONFIG_BAR0_OFFSET:x} '
            f'words={tlb["cfg0"]:08x},{tlb["cfg1"]:08x},{tlb["cfg2"]:08x}',
        )
        print(f'NoC 0 worker tile={TILE}; tile-local L1 scratch=0x{SCRATCH_L1:x}')
        print(f'BAR physical payload address=0x{data:016x}; bytes={len(PATTERN)}; guards=PASS')
        print(f'L1 pattern={observed.hex()}')
        print('result=PASS (memory access only; no accelerator execution claimed)')

        artifact_dir = transactions_path.parent
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / 'scratch_pattern.bin').write_bytes(observed)
        (artifact_dir / 'left_guard.bin').write_bytes(observed_left)
        (artifact_dir / 'right_guard.bin').write_bytes(observed_right)
        manifest = {
            'stage': 'blackhole_bar_tlb_l1_probe',
            'chip': 'single-chip Blackhole',
            'simulator_revision': '3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a',
            'simulator_library': str(sim.path),
            'simulator_library_sha256': hashlib.sha256(Path(sim.path).read_bytes()).hexdigest(),
            'host': {'os': platform.platform(), 'architecture': platform.machine(), 'python': sys.version},
            'endpoint': {'bdf': '0000:00:00.0', 'vendor_device': f'0x{identity:08x}'},
            'bars': {'bar0': f'0x{bar0:016x}', 'bar0_size': BAR0_SIZE,
                     'bar2': f'0x{bar2:016x}', 'bar4': f'0x{bar4:016x}'},
            'tile': {'noc': 0, 'x': TILE[0], 'y': TILE[1]},
            'tlb': {'config_bar0_offset': f'0x{TLB_CONFIG_BAR0_OFFSET:08x}',
                    'size': TLB_SIZE, 'local_base': 0, 'words': [tlb['cfg0'], tlb['cfg1'], tlb['cfg2']]},
            'scratch': {'tile_local_address': f'0x{SCRATCH_L1:08x}',
                        'bar_physical_address': f'0x{data:016x}', 'pattern_bytes': len(observed),
                        'pattern_sha256': hashlib.sha256(observed).hexdigest(),
                        'left_guard': LEFT_GUARD.hex(), 'right_guard': RIGHT_GUARD.hex()},
            'host_transactions': 'mmio.jsonl logs host C-ABI transactions; it does not prove device arithmetic',
            'validation': {'status': 'PASS', 'pattern_readback_exact': observed == PATTERN,
                           'left_guard_exact': observed_left == LEFT_GUARD,
                           'right_guard_exact': observed_right == RIGHT_GUARD,
                           'scope': 'memory access only; no accelerator execution claimed'},
            'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        (artifact_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
        (artifact_dir / 'commands.log').write_text(shlex.join([sys.executable, *sys.argv]) + '\n')


if __name__ == '__main__':
    main()
