"""Small ctypes wrapper for the pinned official single-chip Blackhole C ABI."""

import ctypes
import json
import os
import platform
import struct
from pathlib import Path


ELF_MACHINES = {'x86_64': 62, 'aarch64': 183, 'arm64': 183}


def checked_library_path():
    value = os.environ.get('TTSIM_LIB')
    if not value:
        raise RuntimeError('TTSIM_LIB must name the absolute path to libttsim_bh.so')
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeError(f'TTSIM_LIB must be absolute, got {str(path)!r}')
    if not path.is_file():
        raise RuntimeError(f'TTSIM_LIB does not exist or is not a file: {path}')
    if struct.calcsize('P') != 8 or struct.pack('=I', 1) != b'\x01\x00\x00\x00':
        raise RuntimeError('libttsim requires a 64-bit little-endian host')
    try:
        header = path.read_bytes()[:20]
    except OSError as exc:
        raise RuntimeError(f'cannot read TTSIM_LIB {path}: {exc}') from exc
    if len(header) < 20 or header[:4] != b'\x7fELF':
        raise RuntimeError(f'TTSIM_LIB is not an ELF shared library: {path}')
    if header[4] != 2 or header[5] != 1:
        raise RuntimeError(f'TTSIM_LIB must be ELF64 little-endian: {path}')
    machine = int.from_bytes(header[18:20], 'little')
    host = platform.machine()
    expected = ELF_MACHINES.get(host)
    if expected is None or machine != expected:
        raise RuntimeError(
            f'TTSIM_LIB architecture mismatch: host={host}, ELF e_machine={machine}',
        )
    return path


class Simulator:
    def __init__(self, event_log=None):
        self.path = checked_library_path()
        try:
            self.lib = ctypes.CDLL(str(self.path))
        except OSError as exc:
            raise RuntimeError(f'cannot load official ttsim library {self.path}: {exc}') from exc
        self._declare('libttsim_init', [], None)
        self._declare('libttsim_exit', [], None)
        self._declare(
            'libttsim_pci_config_rd32', [ctypes.c_uint32, ctypes.c_uint32], ctypes.c_uint32,
        )
        self._declare(
            'libttsim_pci_mem_rd_bytes',
            [ctypes.c_uint64, ctypes.c_void_p, ctypes.c_uint32], None,
        )
        self._declare(
            'libttsim_pci_mem_wr_bytes',
            [ctypes.c_uint64, ctypes.c_void_p, ctypes.c_uint32], None,
        )
        self._declare('libttsim_clock', [ctypes.c_uint32], None)
        self.events = []
        self.event_log = Path(event_log) if event_log else None
        self.started = False

    def _declare(self, name, argtypes, restype):
        try:
            function = getattr(self.lib, name)
        except AttributeError as exc:
            raise RuntimeError(f'{self.path} lacks required C ABI symbol {name}') from exc
        function.argtypes = argtypes
        function.restype = restype
        setattr(self, name, function)

    def __enter__(self):
        self.libttsim_init()
        self.started = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.started:
            self.libttsim_exit()
            self.started = False
        if self.event_log:
            self.event_log.parent.mkdir(parents=True, exist_ok=True)
            with self.event_log.open('w', encoding='utf-8') as stream:
                for event in self.events:
                    stream.write(json.dumps(event, sort_keys=True) + '\n')
        return False

    def _event(self, operation, **fields):
        self.events.append({'kind': 'host_transaction', 'operation': operation, **fields})

    def config32(self, bdf, offset):
        value = int(self.libttsim_pci_config_rd32(bdf, offset))
        self._event('pci_config_rd32', bdf=bdf, offset=offset, value=value)
        return value

    def read(self, paddr, size):
        if not 0 <= paddr < 1 << 64 or not 0 < size < 1 << 32:
            raise ValueError('BAR address or transfer size is outside the C ABI range')
        buffer = ctypes.create_string_buffer(size)
        self.libttsim_pci_mem_rd_bytes(paddr, buffer, size)
        data = buffer.raw
        self._event('pci_mem_rd_bytes', paddr=paddr, size=size, data_hex=data.hex())
        return data

    def write(self, paddr, data):
        data = bytes(data)
        if not 0 <= paddr < 1 << 64 or not 0 < len(data) < 1 << 32:
            raise ValueError('BAR address or transfer size is outside the C ABI range')
        buffer = ctypes.create_string_buffer(data, len(data))
        self.libttsim_pci_mem_wr_bytes(paddr, buffer, len(data))
        self._event('pci_mem_wr_bytes', paddr=paddr, size=len(data), data_hex=data.hex())

    def write32(self, paddr, value):
        self.write(paddr, int(value).to_bytes(4, 'little'))

    def clock(self, clocks=1):
        if not 0 < clocks < 1 << 32:
            raise ValueError('clock step must be in 1..2^32-1')
        self.libttsim_clock(clocks)
        self._event('clock', clocks=clocks)


def pci_bar_base(sim, bdf, low_offset):
    low = sim.config32(bdf, low_offset)
    high = sim.config32(bdf, low_offset + 4)
    if low == 0xFFFFFFFF or high == 0xFFFFFFFF:
        raise RuntimeError(f'BAR at config offset 0x{low_offset:x} is absent')
    return (high << 32) | (low & 0xFFFFFFF0)


def configure_bh_tlb(sim, bar0, index, tile_x, tile_y, local_base=0):
    """Program one BH 2 MiB BAR0 TLB for one NoC 0 tile."""
    if not 0 <= index < 201:
        raise ValueError('TLB index must be a user window in 0..200')
    if local_base & ((1 << 21) - 1):
        raise ValueError('Blackhole TLB base must be 2 MiB aligned')
    if not (1 <= tile_x <= 16 and 2 <= tile_y <= 11):
        raise ValueError('tile coordinate is outside pinned single-chip BH Tensix topology')
    local_offset = local_base >> 21
    cfg0 = local_offset & 0xFFFFFFFF
    cfg1 = ((local_offset >> 32) & 0x7FF) | (tile_x << 11) | (tile_y << 17)
    cfg2 = 0  # NoC 0, unicast, default ordering; all reserved bits are zero.
    config_base = bar0 + 0x1FC00000 + index * 12
    for word_index, value in enumerate((cfg0, cfg1, cfg2)):
        sim.write32(config_base + word_index * 4, value)
    return {
        'index': index,
        'size': 1 << 21,
        'local_base': local_base,
        'cfg0': cfg0,
        'cfg1': cfg1,
        'cfg2': cfg2,
    }


def configure_bh_tlb0(sim, bar0, tile_x, tile_y, local_base=0):
    return configure_bh_tlb(sim, bar0, 0, tile_x, tile_y, local_base)
