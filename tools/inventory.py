#!/usr/bin/env python3
"""Inventory the pinned Blackhole Tensix decoder and callback handlers."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


REVISION = '3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a'
VERSION = 'v1.10.1'
ROOT = Path(__file__).resolve().parents[1]
TRACE_PATCH = ROOT / 'patches' / 'ttsim-v1.10.1-readonly-elw-trace.patch'
EXPANDER_OPS = {'MOP', 'NOP', 'MOP_CFG', 'REPLAY'}
BH_REJECTED_CALLBACKS = {
    'SETDVALID': 'Blackhole handler unconditionally raises UnsupportedFunctionality; use UNPACR_NOP.',
    'SFPLOADMACRO': 'Blackhole handler unconditionally raises UnsupportedFunctionality.',
    'REG2FLOP': 'Blackhole TT_ARCH_VERSION=1 handler unconditionally raises UnsupportedFunctionality.',
}
EXAMPLE_OPS = {
    'ELWADD': ('add.py', 'add'),
    'ELWMUL': ('mul.py', 'mul'),
    'ELWSUB': ('sub.py', 'sub'),
}
STREAM_CONTROL_EXAMPLE = 'stream_controls.py'
STREAM_CONTROL_PHASES = {
    'MOP_CFG': {'configured'},
    'MOP': {'expanded'},
    'NOP': {'consumed'},
    'REPLAY': {'load_started', 'replayed'},
}
CATEGORIES = {
    'state_movement': {
        'MOVD2A', 'MOVD2B', 'MOVB2A', 'ZEROACC', 'ZEROSRC', 'MOVA2D', 'MOVB2D', 'TRNSPSRCB',
    },
    'math_and_pooling': {'MVMUL', 'ELWMUL', 'ELWADD', 'ELWSUB', 'GMPOOL', 'GAPOOL'},
    'pack_unpack': {'PACR', 'UNPACR', 'UNPACR_NOP', 'SETDVALID'},
    'address_and_validity': {
        'SETADC', 'SETADCXY', 'INCADCXY', 'ADDRCRXY', 'SETADCZW', 'INCADCZW', 'ADDRCRZW',
        'SETADCXX', 'CLEARDVALID', 'GATESRCRST', 'INCRWC', 'SETRWC',
    },
    'dma_and_register_transfer': {'SETDMAREG', 'REG2FLOP', 'ADDDMAREG', 'MULDMAREG', 'DMANOP', 'STOREREG'},
    'sfpu': {
        'SFPLOAD', 'SFPLOADI', 'SFPSTORE', 'SFPLUT', 'SFPMULI', 'SFPADDI', 'SFPDIVP2', 'SFPEXEXP',
        'SFPEXMAN', 'SFPIADD', 'SFPSHFT', 'SFPSETCC', 'SFPMOV', 'SFPABS', 'SFPAND', 'SFPOR', 'SFPNOT',
        'SFPLZ', 'SFPSETEXP', 'SFPSETMAN', 'SFPMAD', 'SFPADD', 'SFPMUL', 'SFPPUSHC', 'SFPPOPC',
        'SFPSETSGN', 'SFPENCC', 'SFPCOMPC', 'SFPTRANSP', 'SFPXOR', 'SFP_STOCH_RND', 'SFPNOP', 'SFPCAST',
        'SFPCONFIG', 'SFPSWAP', 'SFPLOADMACRO', 'SFPSHFT2', 'SFPLUTFP32', 'SFPLE', 'SFPGT', 'SFPMUL24',
        'SFPARECIP',
    },
    'atomic_and_synchronization': {'ATGETM', 'ATRELM', 'STALLWAIT', 'SEMINIT', 'SEMPOST', 'SEMGET', 'SEMWAIT'},
    'configuration': {
        'WRCFG', 'RDCFG', 'SETC16', 'RMWCIB0', 'RMWCIB1', 'RMWCIB2', 'RMWCIB3', 'CFGSHIFTMASK',
    },
}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def default_deps_dir():
    if os.environ.get('TTSIM_DEPS_DIR'):
        return Path(os.environ['TTSIM_DEPS_DIR']).expanduser()
    library = os.environ.get('TTSIM_LIB')
    if library:
        for parent in Path(library).expanduser().resolve().parents:
            if (parent / 'setup_manifest.json').is_file():
                return parent
    cache = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')).expanduser()
    return cache / 'tt-blackhole-lab'


def gather_execution_evidence(evidence_root, isa):
    opcode_names = {entry['opcode']: name for name, entry in isa.items()}
    callbacks = {}
    output_checked = set()
    runs = {}
    stream_controls = {}
    patch_hash = sha256(TRACE_PATCH)
    for operation, (script, expected_name) in EXAMPLE_OPS.items():
        run_dir = evidence_root / expected_name
        manifest_path = run_dir / 'manifest.json'
        validation_path = run_dir / 'validation.json'
        trace_path = run_dir / 'ttsim_trace.jsonl'
        if not all(path.is_file() for path in (manifest_path, validation_path, trace_path)):
            continue
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        validation = json.loads(validation_path.read_text(encoding='utf-8'))
        if manifest.get('source_hashes', {}).get('example_python_sha256') != sha256(ROOT / 'examples' / script):
            continue
        if manifest.get('trace_patch', {}).get('patch_sha256') != patch_hash:
            continue
        if validation.get('status') != 'PASS' or validation.get('operation') != expected_name:
            continue
        run_id = str(run_dir.relative_to(ROOT))
        for line in trace_path.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get('event') != 'tensix_opcode_callback' or event.get('status') != 'executed':
                continue
            opcode = event.get('opcode')
            word = event.get('instruction_word')
            if not isinstance(opcode, int) or not isinstance(word, str):
                continue
            if int(word, 16) >> 24 != opcode:
                continue
            name = opcode_names.get(opcode)
            if name:
                callbacks.setdefault(name, set()).add(run_id)
        if (validation.get('active_elements') == 128 and validation.get('mismatch_count') == 0
                and validation.get('output_guards_pass') is True and validation.get('device_written_done') == 1):
            output_checked.add(operation)
            runs[operation] = run_id
    run_dir = evidence_root / 'stream_controls'
    manifest_path = run_dir / 'manifest.json'
    validation_path = run_dir / 'validation.json'
    trace_path = run_dir / 'ttsim_trace.jsonl'
    if all(path.is_file() for path in (manifest_path, validation_path, trace_path)):
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        validation = json.loads(validation_path.read_text(encoding='utf-8'))
        if (manifest.get('source_hashes', {}).get('example_python_sha256') ==
                sha256(ROOT / 'examples' / STREAM_CONTROL_EXAMPLE) and
                manifest.get('trace_patch', {}).get('patch_sha256') == patch_hash and
                validation.get('status') == 'PASS' and
                validation.get('execution_core') == 'TRISC0' and
                validation.get('execution_pipe') == 0 and
                validation.get('device_written_done') == 1):
            run_id = str(run_dir.relative_to(ROOT))
            for line in trace_path.read_text(encoding='utf-8').splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get('event') != 'tensix_stream_control':
                    continue
                name = event.get('name')
                if name not in STREAM_CONTROL_PHASES:
                    continue
                entry = isa.get(name)
                word = event.get('instruction_word')
                try:
                    word_matches = int(word, 16) >> 24 == entry['opcode']
                except (TypeError, ValueError, KeyError):
                    word_matches = False
                if (word_matches and event.get('opcode') == entry['opcode'] and
                        event.get('tile_id') == 0 and event.get('pipe') == 0):
                    stream_controls.setdefault(name, {}).setdefault(event.get('status'), set()).add(run_id)
    return callbacks, output_checked, runs, stream_controls


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deps-dir', type=Path, default=default_deps_dir())
    parser.add_argument('--output', type=Path, default=Path('out/tensix_opcode_inventory.json'))
    parser.add_argument('--evidence-dir', type=Path, default=ROOT / 'out')
    parser.add_argument('--no-evidence', action='store_true', help='omit run evidence and report source inventory only')
    parser.add_argument('--require-all-exercised', action='store_true', help='fail if a supported callback or stream control lacks execution evidence')
    args = parser.parse_args()

    deps = args.deps_dir.expanduser().resolve()
    setup_path = deps / 'setup_manifest.json'
    if not setup_path.is_file():
        parser.error(f'missing {setup_path}; run setup.sh or pass --deps-dir')
    setup = json.loads(setup_path.read_text(encoding='utf-8'))
    if setup.get('source_revision') != REVISION:
        parser.error(f'setup manifest revision is {setup.get("source_revision")}, expected {REVISION}')
    source = Path(setup['pinned_source_checkout'])
    source = source if source.is_absolute() else (deps / source)
    source = source.resolve()
    try:
        actual_revision = subprocess.run(
            ['git', '-C', str(source), 'rev-parse', 'HEAD'], check=True,
            text=True, capture_output=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        parser.error(f'cannot verify pinned source revision at {source}: {exc}')
    if actual_revision != REVISION:
        parser.error(f'source checkout revision is {actual_revision}, expected {REVISION}')

    isa_path = source / 'data' / 'bh' / 'tensix_isa.json'
    cpp_path = source / 'src' / 'tensix.cpp'
    isa = json.loads(isa_path.read_text(encoding='utf-8'))
    cpp = cpp_path.read_text(encoding='utf-8')
    source_callbacks = set(re.findall(r'TENSIX_EXECUTE_([A-Z0-9_]+)\s*\(', cpp))
    categorized = set().union(*CATEGORIES.values())
    callback_handlers = {
        name for name, entry in isa.items()
        if name not in EXPANDER_OPS and not entry.get('unsupported')
    }
    missing_callbacks = sorted(callback_handlers - source_callbacks)
    if missing_callbacks:
        parser.error(f'callback handlers absent from pinned source: {missing_callbacks}')
    if categorized != callback_handlers:
        missing = sorted(callback_handlers - categorized)
        unknown = sorted(categorized - callback_handlers)
        parser.error(f'category map differs from decoder: missing={missing}, unknown={unknown}')

    evidence_root = args.evidence_dir.expanduser().resolve()
    evidence_callbacks, output_checked, output_runs, evidence_stream_controls = (
        gather_execution_evidence(evidence_root, isa) if not args.no_evidence else ({}, set(), {}, {})
    )
    operations = []
    for name, entry in isa.items():
        if name in EXPANDER_OPS:
            status = 'stream_expander_control'
        elif entry.get('unsupported'):
            status = 'explicitly_unsupported'
        elif name in BH_REJECTED_CALLBACKS:
            status = 'unsupported_on_blackhole'
        else:
            status = 'supported_handler'
            if name not in source_callbacks:
                parser.error(f'{name} is not explicitly marked unsupported but has no source callback')
        category = next((key for key, members in CATEGORIES.items() if name in members), None)
        operations.append({
            'name': name,
            'opcode': f'0x{entry["opcode"]:02x}',
            'status': status,
            'category': category,
            'args': entry.get('args', {}),
            'blackhole_note': BH_REJECTED_CALLBACKS.get(name),
            'callback_executed': name in evidence_callbacks,
            'callback_evidence_runs': sorted(evidence_callbacks.get(name, set())),
            'stream_control_executed': all(
                evidence_stream_controls.get(name, {}).get(status)
                for status in STREAM_CONTROL_PHASES.get(name, set())
            ) if name in STREAM_CONTROL_PHASES else False,
            'stream_control_evidence': {
                status: sorted(evidence_stream_controls.get(name, {}).get(status, set()))
                for status in sorted(STREAM_CONTROL_PHASES.get(name, set()))
            } if name in STREAM_CONTROL_PHASES else {},
            'output_checked': name in output_checked,
            'output_evidence_runs': [output_runs[name]] if name in output_runs else [],
        })

    runnable_ops = [op for op in operations if op['status'] == 'supported_handler']
    callback_count = sum(op['callback_executed'] for op in runnable_ops)
    expander_ops = [op for op in operations if op['status'] == 'stream_expander_control']
    expander_count = sum(op['stream_control_executed'] for op in expander_ops)
    output_checked_names = sorted(output_checked)
    uncovered = sorted(op['name'] for op in runnable_ops if not op['callback_executed'])
    unexercised_expanders = sorted(op['name'] for op in expander_ops if not op['stream_control_executed'])

    result = {
        'simulator': 'official Tenstorrent ttsim Blackhole Tensix decoder',
        'revision': REVISION,
        'release': VERSION,
        'chip': 'Blackhole',
        'source_files': {
            'decoder_json': str(isa_path),
            'decoder_json_sha256': sha256(isa_path),
            'execution_source': str(cpp_path),
            'execution_source_sha256': sha256(cpp_path),
        },
        'counts': {
            'decoder_entries': len(operations),
            'stream_expander_controls': sum(op['status'] == 'stream_expander_control' for op in operations),
            'decoder_marked_unsupported': sum(op['status'] == 'explicitly_unsupported' for op in operations),
            'callback_handlers': sum(op['name'] in source_callbacks for op in operations),
            'callbacks_unavailable_on_blackhole': sum(op['status'] == 'unsupported_on_blackhole' for op in operations),
            'runnable_callback_handlers': len(runnable_ops),
            'callback_executed_operations': callback_count,
            'runnable_stream_expander_controls': len(expander_ops),
            'stream_expander_controls_executed': expander_count,
            'runnable_instruction_names': len(runnable_ops) + len(expander_ops),
            'supported_operations_executed': callback_count + expander_count,
            'output_checked_operations': len(output_checked_names),
            'runnable_handlers_not_yet_exercised': len(uncovered),
        },
        'operations': operations,
        'execution_coverage': {
            'status': 'complete' if not uncovered and not unexercised_expanders and runnable_ops else ('partial' if callback_count or expander_count else 'inventory_only'),
            'evidence_root': None if args.no_evidence else str(evidence_root),
            'output_checked_operations': output_checked_names,
            'unexercised_supported_operations': uncovered,
            'unexercised_stream_expander_controls': unexercised_expanders,
            'note': 'Coverage is per instruction name with at least one observed valid path, not a sweep of field combinations. Callback traces prove handler completion; stream-control traces prove control phases; output_checked marks separate result oracles. The pinned simulator rejects REPLAY execute_while_loading=1 when load_mode=0.',
        },
    }
    output = args.output
    if not output.is_absolute():
        output = Path.cwd() / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(
        f'{result["counts"]["decoder_entries"]} entries: '
        f'{result["counts"]["runnable_callback_handlers"]} runnable handlers '
        f'({result["counts"]["callbacks_unavailable_on_blackhole"]} handlers reject Blackhole), '
        f'{result["counts"]["decoder_marked_unsupported"]} decoder-marked unsupported, '
        f'{result["counts"]["stream_expander_controls"]} stream-expander controls; '
        f'{callback_count}/{len(runnable_ops)} supported handlers and '
        f'{expander_count}/{len(expander_ops)} stream controls observed executing, '
        f'{len(output_checked_names)} handlers with output checks'
    )
    print(f'Inventory: {output}')
    if args.require_all_exercised and (uncovered or unexercised_expanders):
        print(f'Unexercised supported operations: {", ".join(uncovered + unexercised_expanders)}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
