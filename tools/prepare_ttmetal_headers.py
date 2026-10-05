#!/usr/bin/env python3
"""Prepare source headers for the official v0.77.0 runtime packages."""

import argparse
import hashlib
import json
import pathlib
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metal", type=pathlib.Path, required=True)
    parser.add_argument("--deps", type=pathlib.Path, required=True)
    args = parser.parse_args()
    metal, deps = args.metal.resolve(), args.deps.resolve()
    revision = subprocess.check_output(
        ["git", "-C", str(metal), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != "9f9cd4fd590f4b606bd0981a4fe0b6403eb38ec9":
        raise SystemExit("TT-Metal must be the pinned v0.77.0 commit")
    sources = deps / "metalium-sources"
    archives = deps / "metalium-archives"
    sources.mkdir(parents=True, exist_ok=True)
    archives.mkdir(parents=True, exist_ok=True)
    pins = json.loads(
        (pathlib.Path(__file__).parent / "llama-sim/header-dependencies.json").read_text()
    )
    for pin in pins:
        archive = archives / f"{pin['name']}-{pin['ref'][:12]}.tar.gz"
        if not archive.exists():
            url = f"https://codeload.github.com/{pin['repository']}/tar.gz/{pin['ref']}"
            partial = archive.with_suffix(archive.suffix + ".part")
            subprocess.run(
                ["curl", "--fail", "--location", "--retry", "3", "--max-time", "600",
                 url, "--output", str(partial)], check=True
            )
            if hashlib.file_digest(partial.open("rb"), "sha256").hexdigest() != pin["archive_sha256"]:
                raise SystemExit(f"Checksum mismatch: {partial}")
            partial.rename(archive)
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != pin["archive_sha256"]:
            raise SystemExit(f"Checksum mismatch: {archive}")
        source = sources / pin["name"]
        if not source.exists():
            with tempfile.TemporaryDirectory(dir=sources) as staging:
                subprocess.run(
                    ["tar", "-xzf", str(archive), "--strip-components=1", "-C", staging],
                    check=True
                )
                pathlib.Path(staging).rename(source)

    links = {
        "tt-metalium": metal / "tt_metal/api/tt-metalium",
        "tt_stl": metal / "tt_stl/tt_stl",
        "umd": metal / "tt_metal/third_party/umd/device/api/umd",
        "hostdevcommon": metal / "tt_metal/hostdevcommon/api/hostdevcommon",
        "fmt": sources / "fmt/include/fmt",
        "spdlog": sources / "spdlog/include/spdlog",
        "tt-logger": sources / "tt-logger/include/tt-logger",
        "enchantum": sources / "enchantum/enchantum/include/enchantum",
        "nlohmann": sources / "nlohmann_json/include/nlohmann",
        "reflect": sources / "reflect/reflect",
        "xtensor": sources / "xtensor/include/xtensor",
        "xtensor-blas": sources / "xtensor-blas/include/xtensor-blas",
        "xtl": sources / "xtl/include/xtl",
        "range": sources / "range-v3/include/range",
        "simde": sources / "simd-everywhere/simde",
        "taskflow": sources / "Taskflow/taskflow",
    }
    include = metal / "build/include"
    include.mkdir(parents=True, exist_ok=True)
    for name, target in links.items():
        if not target.exists():
            raise SystemExit(f"Missing source headers: {target}")
        link = include / name
        if link.is_symlink() and link.resolve() == target.resolve():
            continue
        if link.exists() or link.is_symlink():
            raise SystemExit(f"Existing header path differs: {link}")
        link.symlink_to(target, target_is_directory=target.is_dir())
    print(f"Prepared pinned source header layout: {include}")


if __name__ == "__main__":
    main()
