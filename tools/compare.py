#!/usr/bin/env python3
"""Run and compare the Blackhole Tensix examples against official ttsim builds."""

import argparse
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
import add as image_tools  # noqa: E402

OP_INDEX = image_tools.OPERATION_WORD_INDEX
ADD_WORD = 0x28000000
MUL_WORD = 0x27000000
SUB_WORD = 0x30000000
ELW_WORDS = {"add": ADD_WORD, "mul": MUL_WORD, "sub": SUB_WORD}
BASE_STREAM_OPCODES = {
    0x10, 0x35, 0x36, 0x37, 0x38, 0x41, 0x42, 0x50, 0x51, 0x52, 0x53,
    0x54, 0x55, 0x56, 0x5E, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xB2,
}
IMAGE_FILES = (
    "kernel.S", "kernel.bin", "kernel.disasm", "kernel.llvm.o", "kernel.llvm.bin", "kernel.llvm.disasm",
    "config.bin", "config_words.txt",
    "tensix_words.bin", "tensix_words.txt", "input_a.bin", "input_b.bin", "pool_input_b.bin", "output_initial.bin",
)
TRACE_PATCH = ROOT / "patches" / "ttsim-v1.10.1-readonly-elw-trace.patch"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def run(command, env, timeout, check=True):
    print("$ " + " ".join(str(part) for part in command), flush=True)
    try:
        return subprocess.run(command, cwd=ROOT, env=env, timeout=timeout, check=check)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"wall-clock timeout after {timeout}s: {command[0]}") from exc


def child_env(library):
    env = os.environ.copy()
    env["TTSIM_LIB"] = str(Path(library).resolve())
    return env


def capture_trace(out_dir, stderr_text):
    trace_file = out_dir / "ttsim_trace.jsonl"
    stderr_file = out_dir / "process.stderr.log"
    stderr_file.write_text(stderr_text, encoding="utf-8")
    events = []
    for line in stderr_text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") in (
                "tensix_instruction_issue", "tensix_execute_callback", "tensix_opcode_callback",
                "tensix_stream_control"):
            events.append(event)
    trace_file.write_text("".join(json.dumps(event, sort_keys=True) + "\n" for event in events), encoding="utf-8")
    with (out_dir / "trace.log").open("a", encoding="utf-8") as stream:
        stream.write("--- opt-in official-source read-only Tensix trace captured from child stderr ---\n")
        for event in events:
            stream.write(json.dumps(event, sort_keys=True) + "\n")
        if any(event.get("event") == "tensix_instruction_issue" for event in events):
            manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
            index = manifest["operation_word_index"]
            stream.write(
                f"operation-link raw stream index={index} file_offset=0x{index * 4:x} "
                f"tile-L1-load-address=0x{image_tools.TENSIX_WORDS_L1 + index * 4:08x}; "
                "BRISC T0 FIFO store PC=0x0000003c; Tensix execution callback occurs asynchronously.\n"
            )
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    library = Path(manifest["simulator_library"]).resolve()
    build_manifest = image_tools.find_metadata(library.parent, "build_manifest.json")
    manifest["trace_build_manifest"] = str(build_manifest) if build_manifest else None
    manifest["trace_patch"] = {
        "patch_file": str(TRACE_PATCH.relative_to(ROOT)),
        "patch_sha256": sha256(TRACE_PATCH.read_bytes()),
        "event_count": len(events),
        "structured_trace_file": str(trace_file.relative_to(ROOT)),
        "captured_stderr_file": str(stderr_file.relative_to(ROOT)),
        "filter": {"tile_id": 0, "pipe": 0, "elw_operations": ["ELWADD", "ELWMUL", "ELWSUB"],
                   "all_opcode_callbacks": True,
                   "filter_mode": "compile-time constants in the instrumented library"},
        "diagnostic_destination": "captured stderr from the instrumented child process",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return events


def run_traced(command, env, timeout, out_dir):
    print("$ " + " ".join(str(part) for part in command), flush=True)
    try:
        process = subprocess.run(command, cwd=ROOT, env=env, timeout=timeout,
                                 text=True, capture_output=True, check=False)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        (out_dir / "process.stdout.log").write_text(stdout, encoding="utf-8")
        capture_trace(out_dir, stderr)
        raise RuntimeError(f"wall-clock timeout after {timeout}s: {command[0]}") from exc
    if process.stdout:
        print(process.stdout, end="" if process.stdout.endswith("\n") else "\n", flush=True)
    trace_lines = []
    for line in process.stderr.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            trace_lines.append(line)
    if trace_lines:
        print("\n".join(trace_lines), file=sys.stderr, flush=True)
    (out_dir / "process.stdout.log").write_text(process.stdout, encoding="utf-8")
    events = capture_trace(out_dir, process.stderr)
    return process, events


def run_example(script, operation, case, out_dir, library, args, trace=False, load_image=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(ROOT / "examples" / script)]
    if load_image:
        command.extend(["--load-image-dir", str(load_image.resolve()), "--expected-operation", operation])
    else:
        command.extend(["--case", str(case), "--out-dir", str(out_dir.resolve())])
    command.extend([
        "--timeout", str(args.device_timeout), "--clock-limit", str(args.clock_limit),
        "--clock-step", str(args.clock_step),
    ])
    if trace:
        process, events = run_traced(command, child_env(library), args.wall_timeout, out_dir)
        if process.returncode != 0:
            raise RuntimeError(f"traced child exited {process.returncode}; see {out_dir / 'process.stderr.log'}")
    else:
        run(command, child_env(library), args.wall_timeout)
    validation_path = out_dir / "validation.json"
    validation = json.loads(validation_path.read_text())
    if validation.get("status") != "PASS":
        raise RuntimeError(f"validation failed in {out_dir}")
    if trace:
        if not any(event.get("event") == "tensix_instruction_issue" and event.get("status") == "issued" for event in events):
            raise RuntimeError(f"trace did not capture an ELW issue event in {out_dir / 'ttsim_trace.jsonl'}")
        if not any(event.get("status") == "success" for event in events):
            raise RuntimeError(f"trace did not capture successful ELW execution in {out_dir / 'ttsim_trace.jsonl'}")
        opcode_callbacks = [event for event in events if event.get("event") == "tensix_opcode_callback"]
        completed_opcodes = {event.get("opcode") for event in opcode_callbacks if event.get("status") == "executed"}
        required_opcodes = BASE_STREAM_OPCODES | {(ELW_WORDS[operation] >> 24)}
        if not required_opcodes.issubset(completed_opcodes):
            missing = sorted(required_opcodes - completed_opcodes)
            raise RuntimeError(f"trace lacks completed Tensix callbacks {missing} in {out_dir / 'ttsim_trace.jsonl'}")
        success = next(event for event in reversed(events) if event.get("status") == "success")
        for phase in ("before", "after"):
            state = image_tools.enrich_snapshot(success.get(phase, {}))
            state["_capture"] = {
                "status": "captured",
                "source": "opt-in read-only hook in pinned official ttsim v1.10.1",
                "event": success.get("event"),
                "operation": success.get("operation"),
                "instruction_word": success.get("instruction_word"),
                "tile_id": success.get("tile_id"),
                "tile": success.get("tile"),
                "pipe": success.get("pipe"),
                "phase": phase,
            }
            (out_dir / f"registers_{phase}.json").write_text(
                json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return validation


def run_stream_controls(trace_lib, args, out_dir):
    control_dir = out_dir / "stream_controls"
    control_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, str(ROOT / "examples" / "stream_controls.py"),
        "--out-dir", str(control_dir.resolve()), "--timeout", str(args.device_timeout),
        "--clock-limit", str(args.clock_limit), "--clock-step", str(args.clock_step),
    ]
    process, events = run_traced(command, child_env(trace_lib), args.wall_timeout, control_dir)
    if process.returncode != 0:
        raise RuntimeError(f"stream-control child exited {process.returncode}; see {control_dir / 'process.stderr.log'}")
    validation = json.loads((control_dir / "validation.json").read_text(encoding="utf-8"))
    if validation.get("status") != "PASS" or validation.get("device_written_done") != 1:
        raise RuntimeError(f"stream-control validation failed in {control_dir}")
    expected = {
        "MOP_CFG": {"configured"},
        "MOP": {"expanded"},
        "NOP": {"consumed"},
        "REPLAY": {"load_started", "replayed"},
    }
    observed = {}
    for event in events:
        if event.get("event") != "tensix_stream_control":
            continue
        name = event.get("name")
        word = event.get("instruction_word")
        if name not in expected or event.get("tile_id") != 0 or event.get("pipe") != 0:
            continue
        try:
            matches = int(word, 16) >> 24 == event.get("opcode")
        except (TypeError, ValueError):
            matches = False
        if matches:
            observed.setdefault(name, set()).add(event.get("status"))
    missing = {name: sorted(statuses - observed.get(name, set()))
               for name, statuses in expected.items() if not statuses.issubset(observed.get(name, set()))}
    if missing:
        raise RuntimeError(f"stream-control trace lacks required phases {missing}; see {control_dir / 'ttsim_trace.jsonl'}")
    assembly_check = verify_kernel_with_llvm(control_dir)
    manifest_path = control_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["trace_patch"] = json.loads(
        (out_dir / "add" / "manifest.json").read_text(encoding="utf-8")
    )["trace_patch"]
    manifest["trace_patch"]["structured_trace_file"] = str(
        (control_dir / "ttsim_trace.jsonl").relative_to(ROOT)
    )
    manifest["trace_patch"]["captured_stderr_file"] = str(
        (control_dir / "process.stderr.log").relative_to(ROOT)
    )
    manifest["trace_patch"]["event_count"] = len(events)
    manifest["trace_evidence"] = {
        name: sorted(observed.get(name, set())) for name in expected
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "validation": validation,
        "trace_evidence": manifest["trace_evidence"],
        "independent_rv32_assembly_check": assembly_check,
    }


def copy_image(source, destination):
    destination.mkdir(parents=True, exist_ok=True)
    for name in IMAGE_FILES + ("manifest.json",):
        shutil.copy2(source / name, destination / name)


def write_word_table(path, words):
    path.write_text("\n".join(image_tools.describe_tensix_word(i, word) for i, word in enumerate(words)) + "\n")


def update_stream_manifest(directory, words, stage, operation_index):
    word_bytes = image_tools.pack_words(words)
    (directory / "tensix_words.bin").write_bytes(word_bytes)
    write_word_table(directory / "tensix_words.txt", words)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = stage
    manifest["tensix_words_sha256"] = sha256(word_bytes)
    manifest["operation_word_index"] = operation_index
    manifest["operation_word_file_offset"] = operation_index * 4
    manifest["operation_word"] = f"0x{words[operation_index]:08x}"
    manifest["operation_word_decoded"] = image_tools.describe_tensix_word(operation_index, words[operation_index])
    manifest["validation"] = {"status": "PENDING", "reason": "stream bytes changed; execution follows"}
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def compare_pair(add_dir, mul_dir, case):
    for name in ("kernel.bin", "config.bin", "input_a.bin", "input_b.bin", "pool_input_b.bin"):
        if (add_dir / name).read_bytes() != (mul_dir / name).read_bytes():
            raise RuntimeError(f"case {case}: ADD/MUL unexpectedly differs in {name}")
    add_bytes = (add_dir / "tensix_words.bin").read_bytes()
    mul_bytes = (mul_dir / "tensix_words.bin").read_bytes()
    add_words = image_tools.parse_words(add_dir / "tensix_words.bin", len(add_bytes) // 4)
    mul_words = image_tools.parse_words(mul_dir / "tensix_words.bin", len(mul_bytes) // 4)
    changed = [i for i, pair in enumerate(zip(add_words, mul_words)) if pair[0] != pair[1]]
    if len(add_words) != len(mul_words) or changed != [OP_INDEX]:
        raise RuntimeError(f"case {case}: expected only operation word {OP_INDEX} to change, saw {changed}")
    old, new = add_words[OP_INDEX], mul_words[OP_INDEX]
    if old != ADD_WORD or new != MUL_WORD:
        raise RuntimeError(f"case {case}: unexpected ELW words 0x{old:08x} -> 0x{new:08x}")
    xor = old ^ new
    changed_bits = [bit for bit in range(32) if xor & (1 << bit)]
    if changed_bits != [24, 25, 26, 27]:
        raise RuntimeError(f"unexpected changed instruction bits: {changed_bits}")
    add_text = (add_dir / "tensix_words.txt").read_text()
    mul_text = (mul_dir / "tensix_words.txt").read_text()
    normalized_add_text = add_text.replace("0x28000000 ELWADD", "0xOPWORD ELWOP")
    normalized_mul_text = mul_text.replace("0x27000000 ELWMUL", "0xOPWORD ELWOP")
    if normalized_add_text != normalized_mul_text:
        raise RuntimeError(f"case {case}: decoded instruction tables differ beyond the operation")
    changed_bytes = [i for i, pair in enumerate(zip(add_bytes, mul_bytes)) if pair[0] != pair[1]]
    if changed_bytes != [OP_INDEX * 4 + 3]:
        raise RuntimeError(f"case {case}: expected only the opcode byte to differ, saw {changed_bytes}")
    return {
        "case": case,
        "kernel_equal": True,
        "config_equal": True,
        "inputs_equal": True,
        "tensix_word_count_equal": len(add_words),
        "changed_word_indices": changed,
        "changed_word_file_offset": OP_INDEX * 4,
        "operation_word_load_address": f"0x{image_tools.TENSIX_WORDS_L1 + OP_INDEX * 4:08x}",
        "changed_byte_file_offsets": changed_bytes,
        "add_word": f"0x{old:08x}",
        "mul_word": f"0x{new:08x}",
        "add_decoded": image_tools.describe_tensix_word(OP_INDEX, old),
        "mul_decoded": image_tools.describe_tensix_word(OP_INDEX, new),
        "xor": f"0x{xor:08x}",
        "changed_bit_positions_lsb0": changed_bits,
        "changed_bits_in_opcode_field": len(changed_bits),
        "changed_field": "opcode[31:24]: ELWADD 0x28 -> ELWMUL 0x27; dst, addr_mode, instr_mod19, dest_accum_en, clear_dvalid are unchanged",
    }


def compare_python_sources():
    add_source = (ROOT / "examples" / "add.py").read_text()
    mul_source = (ROOT / "examples" / "mul.py").read_text()
    normalized_add = add_source.replace(
        "Run one BF16 ELWADD on one official simulated Blackhole Tensix tile.",
        "Run one BF16 ELWMUL on one official simulated Blackhole Tensix tile.",
    ).replace('default="add")', 'default="mul")').replace('default="out/add")', 'default="out/mul")')
    if normalized_add != mul_source:
        raise RuntimeError("examples/add.py and examples/mul.py differ beyond the operation label/default")
    diff = list(difflib.unified_diff(add_source.splitlines(), mul_source.splitlines(),
                                    fromfile="examples/add.py", tofile="examples/mul.py", lineterm=""))
    return {
        "normalized_sources_identical": True,
        "expected_source_only_changes": [
            "module description: ELWADD -> ELWMUL",
            "default output directory: out/add -> out/mul",
            "default expected operation: add -> mul",
        ],
        "unified_diff": diff,
        "add_source_sha256": sha256(add_source.encode()),
        "mul_source_sha256": sha256(mul_source.encode()),
    }


def verify_kernel_with_llvm(directory):
    llvm_mc = shutil.which("llvm-mc")
    llvm_objcopy = shutil.which("llvm-objcopy")
    llvm_objdump = shutil.which("llvm-objdump")
    if not all((llvm_mc, llvm_objcopy, llvm_objdump)):
        raise RuntimeError("tools/compare.py requires llvm-mc, llvm-objcopy, and llvm-objdump for independent RV32 checks")
    obj = directory / "kernel.llvm.o"
    binary = directory / "kernel.llvm.bin"
    disassembly = directory / "kernel.llvm.disasm"
    subprocess.run([llvm_mc, "-triple=riscv32-unknown-elf", "-filetype=obj", str(directory / "kernel.S"), "-o", str(obj)], check=True)
    subprocess.run([llvm_objcopy, "--only-section=.text", "-O", "binary", str(obj), str(binary)], check=True)
    decoded = subprocess.run(
        [llvm_objdump, "-d", "--triple=riscv32-unknown-elf", str(obj)], check=True,
        text=True, capture_output=True,
    ).stdout
    disassembly.write_text(decoded)
    if binary.read_bytes() != (directory / "kernel.bin").read_bytes():
        raise RuntimeError(f"handwritten RV32 bytes differ from independent LLVM assembly in {directory}")
    versions = {
        "llvm_mc": subprocess.run([llvm_mc, "--version"], check=True, text=True, capture_output=True).stdout.splitlines()[0],
        "llvm_objcopy": subprocess.run([llvm_objcopy, "--version"], check=True, text=True, capture_output=True).stdout.splitlines()[0],
        "llvm_objdump": subprocess.run([llvm_objdump, "--version"], check=True, text=True, capture_output=True).stdout.splitlines()[0],
    }
    check = {
        "status": "PASS",
        "assembler_command": "llvm-mc -triple=riscv32-unknown-elf -filetype=obj kernel.S",
        "objcopy_command": "llvm-objcopy --only-section=.text -O binary kernel.llvm.o kernel.llvm.bin",
        "objdump_command": "llvm-objdump -d --triple=riscv32-unknown-elf kernel.llvm.o",
        "machine_bytes_match": True,
        "kernel_sha256": sha256(binary.read_bytes()),
        "tool_versions": versions,
    }
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["independent_kernel_assembly_check"] = check
    manifest.setdefault("tool_versions", {}).update(versions)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return check


def compare_observed_state(add_dir, mul_dir):
    states = {}
    for operation, directory in (("add", add_dir), ("mul", mul_dir)):
        states[operation] = {
            phase: json.loads((directory / f"registers_{phase}.json").read_text())
            for phase in ("before", "after")
        }
    add_before, mul_before = states["add"]["before"], states["mul"]["before"]
    add_after, mul_after = states["add"]["after"], states["mul"]["after"]
    result = {
        "tile_and_pipe_equal": (add_before["_capture"].get("tile"), add_before["_capture"].get("pipe")) ==
                                (mul_before["_capture"].get("tile"), mul_before["_capture"].get("pipe")),
        "src_a_before_raw_rows_equal": add_before["src_a"]["rows"] == mul_before["src_a"]["rows"],
        "src_b_before_raw_rows_equal": add_before["src_b"]["rows"] == mul_before["src_b"]["rows"],
        "src_a_after_raw_rows_equal": add_after["src_a"]["rows"] == mul_after["src_a"]["rows"],
        "src_b_after_raw_rows_equal": add_after["src_b"]["rows"] == mul_after["src_b"]["rows"],
        "formats_and_control_equal_before": add_before["control"] == mul_before["control"],
        "formats_and_control_equal_after": add_after["control"] == mul_after["control"],
        "row_counters_equal_before": add_before["counters"] == mul_before["counters"],
        "row_counters_equal_after": add_after["counters"] == mul_after["counters"],
        "dst_before_raw_rows_equal": add_before["dst"]["rows"] == mul_before["dst"]["rows"],
        "dst_after_raw_rows_equal": add_after["dst"]["rows"] == mul_after["dst"]["rows"],
        "add_operation_word": add_before["_capture"].get("instruction_word"),
        "mul_operation_word": mul_before["_capture"].get("instruction_word"),
        "output_decoded_values_equal": json.loads((add_dir / "output_decoded.json").read_text())["values"] ==
                                       json.loads((mul_dir / "output_decoded.json").read_text())["values"],
    }
    expected_true = (
        "tile_and_pipe_equal", "src_a_before_raw_rows_equal", "src_b_before_raw_rows_equal",
        "src_a_after_raw_rows_equal", "src_b_after_raw_rows_equal", "formats_and_control_equal_before",
        "formats_and_control_equal_after", "row_counters_equal_before", "row_counters_equal_after",
        "dst_before_raw_rows_equal",
    )
    if not all(result[key] for key in expected_true):
        raise RuntimeError(f"observed source/control state diverged unexpectedly: {result}")
    if result["dst_after_raw_rows_equal"] or result["output_decoded_values_equal"]:
        raise RuntimeError("ADD and MUL should produce distinct Dst and output values for the chosen inputs")
    return result


def compare_patched_state(patched_dir, mul_dir):
    patched = {
        phase: json.loads((patched_dir / f"registers_{phase}.json").read_text())
        for phase in ("before", "after")
    }
    regular = {
        phase: json.loads((mul_dir / f"registers_{phase}.json").read_text())
        for phase in ("before", "after")
    }
    result = {
        "executed_word_is_mul": patched["before"]["_capture"].get("instruction_word") == "0x27000000",
        "src_a_before_matches_mul": patched["before"]["src_a"]["rows"] == regular["before"]["src_a"]["rows"],
        "src_b_before_matches_mul": patched["before"]["src_b"]["rows"] == regular["before"]["src_b"]["rows"],
        "dst_after_matches_mul": patched["after"]["dst"]["rows"] == regular["after"]["dst"]["rows"],
        "output_memory_matches_mul": (patched_dir / "output.bin").read_bytes() == (mul_dir / "output.bin").read_bytes(),
    }
    if not all(result.values()):
        raise RuntimeError(f"patched image execution/state differs from regular MUL: {result}")
    return result


def patch_add_image(add_dir, patched_dir):
    copy_image(add_dir, patched_dir)
    for name in IMAGE_FILES:
        if name not in ("tensix_words.bin", "tensix_words.txt") and (add_dir / name).read_bytes() != (patched_dir / name).read_bytes():
            raise RuntimeError(f"patched image changed an unrelated file: {name}")
    original = bytearray((patched_dir / "tensix_words.bin").read_bytes())
    offset = OP_INDEX * 4
    old = int.from_bytes(original[offset:offset + 4], "little")
    if old != ADD_WORD:
        raise RuntimeError(f"patch source has 0x{old:08x} at offset {offset}, expected ADD word")
    original[offset:offset + 4] = MUL_WORD.to_bytes(4, "little")
    original_stream = (add_dir / "tensix_words.bin").read_bytes()
    if [i for i, pair in enumerate(zip(original_stream, original)) if pair[0] != pair[1]] != [offset + 3]:
        raise RuntimeError("ADD-to-MUL byte patch changed bytes outside the opcode byte")
    words = [int.from_bytes(original[i:i + 4], "little") for i in range(0, len(original), 4)]
    manifest = update_stream_manifest(patched_dir, words, "patched_add_image_executed_as_elwmul", OP_INDEX)
    manifest["parent_image"] = str(add_dir.relative_to(ROOT))
    manifest["byte_patch"] = {
        "file": "tensix_words.bin",
        "offset": offset,
        "length": 4,
        "changed_byte_count": 1,
        "changed_byte_file_offset": offset + 3,
        "old_bytes_le": "00000028",
        "new_bytes_le": "00000027",
        "old_word": f"0x{old:08x}",
        "new_word": f"0x{MUL_WORD:08x}",
        "changed_field": "opcode[31:24]",
        "changed_bit_positions_lsb0": [24, 25, 26, 27],
        "word_load_address": f"0x{image_tools.TENSIX_WORDS_L1 + offset:08x}",
        "all_other_image_bytes_unchanged": True,
    }
    (patched_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (patched_dir / "patch.diff.json").write_text(json.dumps(manifest["byte_patch"], indent=2, sort_keys=True) + "\n")
    return words


def prepare_stall_probe(add_dir, stall_dir):
    copy_image(add_dir, stall_dir)
    manifest = json.loads((stall_dir / "manifest.json").read_text())
    words = image_tools.parse_words(stall_dir / "tensix_words.bin", manifest["tensix_word_count"])
    if words[OP_INDEX] != ADD_WORD or words[9] >> 24 != 0x42 or words[10] >> 24 != 0x42:
        raise RuntimeError("ADD artifact no longer has the expected ELW/UNPACR stream layout")
    # Keep ZEROACC and the pack counter setup first, enqueue ELWADD, then issue
    # both unpackers. This diagnostic stream should cause a real data-valid stall.
    reordered = words[:9] + [words[11], words[12], words[13], words[9], words[10]] + words[14:]
    operation_index = 11
    manifest = update_stream_manifest(stall_dir, reordered, "stall_retry_diagnostic_elwadd", operation_index)
    manifest["diagnostic_reorder"] = {
        "purpose": "exercise the model's real SrcA/SrcB-not-valid ELW retry path",
        "original_unpacr_indices": [9, 10],
        "original_elw_index": OP_INDEX,
        "diagnostic_elw_index": operation_index,
        "execution_stream_only": True,
    }
    (stall_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def discover_trace_library(stock_lib):
    stock_path = Path(stock_lib).resolve()
    candidates = [
        stock_path.parent / "ttsim-v1.10.1-trace" / "src" / "src" / "_out" / "release_bh" / "libttsim.so",
    ]
    deps_dir = os.environ.get("TTSIM_DEPS_DIR")
    if deps_dir:
        candidates.append(Path(deps_dir) / "ttsim-v1.10.1-trace" / "src" / "src" / "_out" /
                          "release_bh" / "libttsim.so")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return candidates[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-lib", default=os.environ.get("TTSIM_LIB"))
    parser.add_argument("--trace-lib", help="instrumented source build; defaults to the setup.sh trace build")
    parser.add_argument("--out-dir", default="out")
    parser.add_argument("--device-timeout", type=float, default=25)
    parser.add_argument("--wall-timeout", type=float, default=60)
    parser.add_argument("--clock-limit", type=int, default=5000)
    parser.add_argument("--clock-step", type=int, default=64)
    args = parser.parse_args()
    if not args.stock_lib:
        parser.error("set TTSIM_LIB or pass --stock-lib")
    stock_lib = Path(args.stock_lib)
    trace_lib = Path(args.trace_lib) if args.trace_lib else discover_trace_library(stock_lib)
    if not stock_lib.is_file() or not trace_lib.is_file():
        parser.error(f"simulator library missing: stock={stock_lib} trace={trace_lib}; run TTSIM_BUILD_TRACE=1 ./setup.sh")
    if stock_lib.resolve() == trace_lib.resolve():
        parser.error("stock and opt-in trace libraries must be different builds")
    out = Path(args.out_dir)
    if not out.is_absolute():
        out = ROOT / out
    out.mkdir(parents=True, exist_ok=True)

    python_source_delta = compare_python_sources()
    bootstrap_dir = out / "riscv"
    bootstrap_check = verify_kernel_with_llvm(bootstrap_dir) if (bootstrap_dir / "kernel.S").is_file() else None
    validation = {"stock": {}, "trace": {}}
    assembler_checks = []
    for case in (0, 1):
        for operation, script in (("add", "add.py"), ("mul", "mul.py"), ("sub", "sub.py")):
            stock_dir = out / "stock" / operation / (f"case{case}" if case else "")
            trace_dir = out / operation / (f"case{case}" if case else "")
            validation["stock"].setdefault(operation, {})[str(case)] = run_example(
                script, operation, case, stock_dir, stock_lib, args)
            validation["trace"].setdefault(operation, {})[str(case)] = run_example(
                script, operation, case, trace_dir, trace_lib, args, trace=True)
            assembler_checks.append({
                "operation": operation,
                "case": case,
                **verify_kernel_with_llvm(trace_dir),
            })

    deltas = [compare_pair(out / "add" / (f"case{case}" if case else ""),
                           out / "mul" / (f"case{case}" if case else ""), case)
              for case in (0, 1)]
    observed_state = compare_observed_state(out / "add", out / "mul")
    stream_control_validation = run_stream_controls(trace_lib, args, out)
    trace_equivalence = []
    for case in (0, 1):
        for operation in ("add", "mul", "sub"):
            stock_dir = out / "stock" / operation / (f"case{case}" if case else "")
            trace_dir = out / operation / (f"case{case}" if case else "")
            same = (stock_dir / "output.bin").read_bytes() == (trace_dir / "output.bin").read_bytes()
            if not same:
                raise RuntimeError(f"trace patch changed {operation.upper()} output for case {case}")
            trace_equivalence.append({"operation": operation, "case": case, "output_bytes_identical": same})

    patched_dir = out / "patched_add_to_mul"
    patch_add_image(out / "add", patched_dir)
    patch_validation = run_example(
        "add.py", "mul", 0, patched_dir, trace_lib, args, trace=True, load_image=patched_dir)
    patched_state = compare_patched_state(patched_dir, out / "mul")
    patched_manifest = json.loads((patched_dir / "manifest.json").read_text())
    patched_manifest["stage"] = "patched_add_image_executed_as_elwmul"
    patched_manifest["parent_image"] = "out/add"
    patched_manifest["byte_patch"] = json.loads((patched_dir / "patch.diff.json").read_text())
    patched_manifest["execution_mode"] = "loaded stored ADD artifacts and patched raw Tensix bytes; did not regenerate from MUL source"
    (patched_dir / "manifest.json").write_text(json.dumps(patched_manifest, indent=2, sort_keys=True) + "\n")

    stall_dir = out / "stall_probe"
    prepare_stall_probe(out / "add", stall_dir)
    stall_command = [
        sys.executable, str(ROOT / "examples" / "add.py"), "--load-image-dir", str(stall_dir.resolve()),
        "--expected-operation", "add", "--timeout", str(args.device_timeout),
        "--clock-limit", str(min(args.clock_limit, 512)), "--clock-step", str(args.clock_step),
    ]
    stall_process, stall_events = run_traced(stall_command, child_env(trace_lib), args.wall_timeout, stall_dir)
    if stall_process.returncode == 0:
        raise RuntimeError("stall diagnostic unexpectedly completed; expected the deliberately blocked stream to remain pending")
    stall_validation = json.loads((stall_dir / "validation.json").read_text())
    issue_count = sum(event.get("event") == "tensix_instruction_issue" for event in stall_events)
    retry_count = sum(event.get("status") == "stalled_retry" for event in stall_events)
    success_count = sum(event.get("status") == "success" for event in stall_events)
    normal_success = [json.loads(line) for line in (out / "add" / "ttsim_trace.jsonl").read_text().splitlines() if line.strip()]
    normal_success_count = sum(event.get("status") == "success" for event in normal_success)
    if not issue_count or not retry_count or success_count or not normal_success_count:
        raise RuntimeError(f"stall trace incomplete: issues={issue_count} retries={retry_count} successes={success_count}")
    if stall_validation.get("status") != "FAIL" or stall_validation.get("device_written_done") != 0xA5A5A5A5:
        raise RuntimeError("stall diagnostic did not stop with the expected pending completion sentinel")
    stall_manifest = json.loads((stall_dir / "manifest.json").read_text())
    stall_manifest["stage"] = "expected_pending_stall_diagnostic"
    stall_manifest["diagnostic_reorder"] = {
        "purpose": "exercise the model's real SrcA/SrcB-not-valid ELW retry path",
        "original_unpacr_indices": [9, 10],
        "original_elw_index": OP_INDEX,
        "diagnostic_elw_index": 11,
        "execution_stream_only": True,
        "expected_outcome": "T0 instruction queue remains blocked at ELWADD; this run is a stall trace, not an arithmetic pass",
    }
    (stall_dir / "manifest.json").write_text(json.dumps(stall_manifest, indent=2, sort_keys=True) + "\n")

    report = {
        "status": "PASS",
        "official_stock_library": {"path": str(stock_lib.resolve()), "sha256": sha256(stock_lib.read_bytes())},
        "official_trace_library": {"path": str(trace_lib.resolve()), "sha256": sha256(trace_lib.read_bytes())},
        "validation": validation,
        "python_source_delta": python_source_delta,
        "independent_rv32_assembly_checks": assembler_checks,
        "independent_bootstrap_assembly_check": bootstrap_check,
        "stock_vs_trace_output_equivalence": trace_equivalence,
        "add_to_mul_stream_delta": deltas,
        "observed_add_vs_mul_state": observed_state,
        "stream_expander_controls": stream_control_validation,
        "patched_add_stream": {
            "execution_validation": patch_validation,
            "matches_regular_mul_state_and_output": patched_state,
            "input_image_bytes_changed": 1,
            "operation_word_size_bytes": 4,
            "changed_word_count": 1,
            "changed_bit_positions_lsb0": [24, 25, 26, 27],
            "output_directory": str(patched_dir.relative_to(ROOT)),
        },
        "stall_diagnostic": {
            "execution_validation": stall_validation,
            "expected_noncompletion": True,
            "reason": "the ELW instruction is deliberately queued before both unpackers, so this diagnostic demonstrates the retry path and remains blocked",
            "issue_events": issue_count,
            "stalled_retry_events": retry_count,
            "successful_execution_events_in_this_run": success_count,
            "successful_execution_events_in_normal_add_trace": normal_success_count,
            "trace_file": str((stall_dir / "ttsim_trace.jsonl").relative_to(ROOT)),
        },
    }
    (out / "comparison.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (out / "delta.json").write_text(json.dumps({"case_deltas": deltas}, indent=2, sort_keys=True) + "\n")
    print(f"PASS: ADD/MUL/SUB, two patterns, stream controls, callback traces, byte patch, and {retry_count} real ELW stall retries")
    print(f"comparison report: {out / 'comparison.json'}")


if __name__ == "__main__":
    main()
