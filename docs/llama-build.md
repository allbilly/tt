# Building the Blackhole llama.cpp experiment

[Experiment and results](llama-first-token.md) · [Back to README](../README.md)

The tested environment is the separate `tt-metal-sim` Podman container: Ubuntu 24.04, GCC 13.3, CMake 3.28.3, and Ninja 1.11.1. Fedora's host compiler is not used. Sources and dependency caches are mounted into the container at their host paths; model weights are mounted read-only. No accelerator device is passed through.

TT-Metal and TTNN **host libraries are official prebuilt v0.77.0 release packages**. llama.cpp, its Metalium backend, and the smoke-test hosts are built from source. Device firmware and kernels are compiled at runtime by SFPI. The TT-Metal source checkout supplies matching headers and the official integer-example source.

## Sources outside this repository

Use these exact revisions, even if the branches or tags later move:

```bash
git clone --branch metalium-support https://github.com/marty1885/llama.cpp.git "$HOME/llama.cpp-tt"
git -C "$HOME/llama.cpp-tt" checkout 04602ca95c1a6adb6c1533e024ad9c79ee6c428d
git clone --branch v0.77.0 https://github.com/tenstorrent/tt-metal.git "$HOME/tt-metal"
git -C "$HOME/tt-metal" checkout 9f9cd4fd590f4b606bd0981a4fe0b6403eb38ec9
git -C "$HOME/tt-metal" submodule update --init --recursive \
  tt_metal/third_party/umd tt_metal/third_party/tracy \
  tt_metal/third_party/tt-cluster-descriptors
```

The recorded run used partial Git clones and checked out the build directories after full-clone network failures. Its submodule sources were extracted from the exact gitlink commits: UMD `67b3c0da2bd1e6d960b62dfdd5be98c0772811e0`, Tracy `117100515bb21d9a6b3a8f0eee50ecd91f961408`, and cluster descriptors `7b2176e2fe913089f8cd2be9dfb738ead6e7aa27`.

The [source-header helper](../tools/prepare_ttmetal_headers.py) checks the TT-Metal commit, verifies the [pinned dependency archives](../tools/llama-sim/header-dependencies.json), extracts missing sources into the external cache, and creates `tt-metal/build/include` symlinks. It preserves existing dependency directories and rejects conflicting header paths. The helper does not build the TT-Metal host libraries.

Download the model into the host's model directory and verify it before mounting that directory read-only:

```bash
mkdir -p "$HOME/models"
curl --fail --location --retry 3 \
  'https://huggingface.co/TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF/resolve/52e7645ba7c309695bec7ac98f4f005b139cf465/tinyllama-1.1b-chat-v1.0.Q4_0.gguf' \
  --output "$HOME/models/tinyllama-1.1b-chat-v1.0.Q4_0.gguf"
sha256sum "$HOME/models/tinyllama-1.1b-chat-v1.0.Q4_0.gguf"
```

The expected model checksum is `da3087fb14aede55fde6eb81a0e55e886810e43509ec82ecdc7aa5d62a03b556`.

## Container layout

The recorded run reused the locally available Ubuntu image `localhost/et-platform:sysemu-release`, image ID `7a9b5cf5977e9e09126101c2c2c9b642b3517c3d7894668c36b1d7c550a573b7`. The following command records its mounts; run it only when creating the experiment container. Run from this repository's root after preparing the source and model directories. The [lab setup](setup.md) downloads and verifies the Blackhole simulator into the same cache. Set `TT_SIM_IMAGE` to an available Ubuntu 24.04 image if that local image is unavailable; a fresh base image has not been validated here. The recorded release packages require an x86-64 host.

```bash
export TTSIM_DEPS_DIR="$HOME/.cache/ttdeps"
mkdir -p "$TTSIM_DEPS_DIR"
./setup.sh
podman run -d --name tt-metal-sim --security-opt label=disable \
  --network host --entrypoint /bin/sleep \
  -v "$HOME/tt-metal:$HOME/tt-metal:rw" \
  -v "$HOME/llama.cpp-tt:$HOME/llama.cpp-tt:rw" \
  -v "$PWD:$PWD:rw" -v "$TTSIM_DEPS_DIR:$TTSIM_DEPS_DIR:rw" \
  -v "$HOME/models:$HOME/models:ro" \
  "${TT_SIM_IMAGE:-localhost/et-platform:sysemu-release}" infinity
```

Install missing build dependencies inside this dedicated container:

```bash
podman exec tt-metal-sim apt-get update
podman exec tt-metal-sim apt-get install -y \
  build-essential cmake ninja-build git curl patch pkg-config ccache \
  python3-dev python3-venv python3-yaml libtbb-dev libyaml-cpp-dev \
  libgmp-dev libmpfr-dev libboost-all-dev libnuma-dev libhwloc-dev \
  libsqlite3-dev libssl-dev libcapstone-dev libpci-dev
```

## Ubuntu runtime packages

Install these packages inside the Ubuntu container. They are not Fedora packages.

| Asset | Official release | SHA256 |
| --- | --- | --- |
| `tt-metalium_0.77.0.ubuntu24.04_amd64.deb` | [TT-Metal v0.77.0](https://github.com/tenstorrent/tt-metal/releases/tag/v0.77.0) | `9bf9f64254ec0fb02bf1be877d38253092a0b2f08ce1f33dbd71b7d71f063f38` |
| `tt-nn_0.77.0.ubuntu24.04_amd64.deb` | [TT-Metal v0.77.0](https://github.com/tenstorrent/tt-metal/releases/tag/v0.77.0) | `6e15d1d52204a903550a77d5cd544e9f210fa45b4b61fda56a2e2a1e3f0d097e` |
| `openmpi-ulfm_5.0.7-1_amd64.deb` | [Tenstorrent OpenMPI v5.0.7](https://github.com/tenstorrent/ompi/releases/tag/v5.0.7) | `954e872d9105e8bf8c31368ff7a5db8670a3d549e2e7eb1ab6072cffcae7984d` |
| `sfpi_7.69.0_x86_64_debian.deb` | [SFPI 7.69.0](https://github.com/tenstorrent/sfpi/releases/tag/7.69.0) | `2ef4441f75774ab1bafde0e8d3ca5517b1b053d1e47b874dd20a73664c50a6e2` |

Download the four assets on the host into the mounted cache. Verify each checksum against the table before installation:

```bash
mkdir -p "$TTSIM_DEPS_DIR/metalium-packages"
for asset in tt-metalium_0.77.0.ubuntu24.04_amd64.deb tt-nn_0.77.0.ubuntu24.04_amd64.deb; do
  curl --fail --location --retry 3 \
    "https://github.com/tenstorrent/tt-metal/releases/download/v0.77.0/$asset" \
    --output "$TTSIM_DEPS_DIR/metalium-packages/$asset"
done
curl --fail --location --retry 3 \
  https://github.com/tenstorrent/ompi/releases/download/v5.0.7/openmpi-ulfm_5.0.7-1_amd64.deb \
  --output "$TTSIM_DEPS_DIR/openmpi-ulfm_5.0.7-1_amd64.deb"
curl --fail --location --retry 3 \
  https://github.com/tenstorrent/sfpi/releases/download/7.69.0/sfpi_7.69.0_x86_64_debian.deb \
  --output "$TTSIM_DEPS_DIR/sfpi_7.69.0_x86_64_debian.deb"
sha256sum "$TTSIM_DEPS_DIR/metalium-packages/tt-metalium_0.77.0.ubuntu24.04_amd64.deb" \
  "$TTSIM_DEPS_DIR/metalium-packages/tt-nn_0.77.0.ubuntu24.04_amd64.deb" \
  "$TTSIM_DEPS_DIR/openmpi-ulfm_5.0.7-1_amd64.deb" \
  "$TTSIM_DEPS_DIR/sfpi_7.69.0_x86_64_debian.deb"
```

From the host, install the verified assets as the container's root user. Paths expand on the host and refer to the identical mounted paths inside the container:

```bash
podman exec tt-metal-sim dpkg -i "$TTSIM_DEPS_DIR/openmpi-ulfm_5.0.7-1_amd64.deb" \
  "$TTSIM_DEPS_DIR/sfpi_7.69.0_x86_64_debian.deb" \
  "$TTSIM_DEPS_DIR/metalium-packages/tt-metalium_0.77.0.ubuntu24.04_amd64.deb" \
  "$TTSIM_DEPS_DIR/metalium-packages/tt-nn_0.77.0.ubuntu24.04_amd64.deb"
podman exec tt-metal-sim ldd /usr/lib/libtt_metal.so
podman exec tt-metal-sim /opt/tenstorrent/sfpi/compiler/bin/riscv-tt-elf-g++ --version
```

OpenMPI must resolve to `/opt/openmpi-v5.0.7-ulfm/lib/libmpi.so.40`. Ubuntu's ordinary OpenMPI 4.1.6 lacks the `MPIX_Comm_revoke` and `MPIX_Comm_shrink` symbols required by this runtime. The [pinned TT-Metal installer](https://github.com/tenstorrent/tt-metal/blob/9f9cd4fd590f4b606bd0981a4fe0b6403eb38ec9/install_dependencies.sh) specifies the Tenstorrent ULFM package.

The official `tt-metalium-dev` archive downloaded during this run matched its published checksum, but its `data.tar.zst` failed decompression on both host and container. Matching source headers provide the workaround used here. The malformed package was retained in the external cache; it is not an installed SDK. TTNN's C++ API is tested directly, so Python TTNN and Torch are unnecessary for this experiment.

## Build and compatibility patch

From the repository root on the host:

```bash
export TTSIM_DEPS_DIR="$HOME/.cache/ttdeps"
export TT_METAL_HOME="$HOME/tt-metal"
export LLAMA_TT_DIR="$HOME/llama.cpp-tt"
podman exec --env TTSIM_DEPS_DIR --env TT_METAL_HOME --env LLAMA_TT_DIR \
  --env TT_LLAMA_BUILD_JOBS \
  tt-metal-sim "$PWD/tools/build_llama_sim.sh"
```

The [build helper](../tools/build_llama_sim.sh) verifies the llama.cpp commit, applies or verifies the [compatibility patch](../patches/llama-blackhole.patch), and builds the `llama` library and smoke tests. `TT_LLAMA_BUILD_JOBS` changes its default of two parallel build jobs. The compatibility patch enables Blackhole's existing native BF16/block-float formats and updates tensor/storage/specification namespaces and an include path for TTNN v0.77.0. It preserves the arithmetic and guards RoPE's older address-mode-base setup/clear on Blackhole. Blackhole encodes the SFPU address-mode index directly, and its SDK removes that base-register helper. The official SDK and simulator source agree on this distinction. RoPE's integer-to-float casts select deterministic nearest rounding because TTSim rejects stochastic rounding; the probe's position and lane integers are exactly representable in float32.

The helper exposes the installed runtime libraries through symlinks at the paths Martin's CMake expects: `tt-metal/build/tt_metal/libtt_metal.so` and `tt-metal/build/ttnn/_ttnncpp.so`. It rejects an existing ordinary library file at either path. These symlinks are intended for use inside the container.

The inference executable is generated from the fork's `examples/simple/simple.cpp`, with [a small configuration patch](../patches/llama-simple-sim.patch): context 128, CPU KV cache, batches of 32, two CPU threads, a printed first-token ID, and CPU buffer overrides for `^output.*`. The example converts the requested transformer count to this fork's output-first slot count; its `-ngl 1` uses two native slots, with output weights returned to CPU. Its original greedy sampler is retained. The executable is `~/llama.cpp-tt/build/bin/llama-simple-sim`.

## Blackhole smoke tests

From the repository root on the host, prepare the simulator directory using `libttsim_bh.so` and the matching source descriptor:

```bash
export LAB_ROOT="$PWD"
export TT_METAL_HOME="$HOME/tt-metal"
mkdir -p "$TTSIM_DEPS_DIR/llama-blackhole"
ln -sfn "$TTSIM_DEPS_DIR/libttsim_bh.so" "$TTSIM_DEPS_DIR/llama-blackhole/libttsim_bh.so"
cp "$TT_METAL_HOME/tt_metal/soc_descriptors/blackhole_140_arch.yaml" \
  "$TTSIM_DEPS_DIR/llama-blackhole/soc_descriptor.yaml"
export TT_METAL_SIMULATOR="$TTSIM_DEPS_DIR/llama-blackhole/libttsim_bh.so"
export TT_METAL_SIMULATOR_HOME="$TTSIM_DEPS_DIR/llama-blackhole"
export TT_METAL_RUNTIME_ROOT=/usr/libexec/tt-metalium
export TT_METAL_SLOW_DISPATCH_MODE=1
export TT_METAL_DISABLE_SFPLOADMACRO=1
```

Launch the smoke tests from the host, forwarding the simulator settings and setting the container's working directory to the source checkout:

```bash
podman exec --workdir "$TT_METAL_HOME" --env TT_METAL_HOME \
  --env TT_METAL_SIMULATOR --env TT_METAL_SIMULATOR_HOME \
  --env TT_METAL_RUNTIME_ROOT --env TT_METAL_SLOW_DISPATCH_MODE \
  --env TT_METAL_DISABLE_SFPLOADMACRO --env TT_METAL_DPRINT_CORES=0,0 \
  tt-metal-sim timeout --kill-after=20s 300 \
  "$LAB_ROOT/out/llama_first_token/smoke-build/metal_integer"
podman exec --workdir "$TT_METAL_HOME" --env TT_METAL_HOME \
  --env TT_METAL_SIMULATOR --env TT_METAL_SIMULATOR_HOME \
  --env TT_METAL_RUNTIME_ROOT --env TT_METAL_SLOW_DISPATCH_MODE \
  --env TT_METAL_DISABLE_SFPLOADMACRO \
  tt-metal-sim timeout --kill-after=20s 600 \
  "$LAB_ROOT/out/llama_first_token/smoke-build/ttnn_add"
```

The first is TT-Metal's unchanged integer-example host and device kernel; it requires readback `21` from `14 + 7`. The [TTNN test](../examples/ttnn_add.cpp) creates a 32×32 BF16 tile of ones, computes `1 + 1` and then `1 + 2` on the device, and checks every output equals `3.0`. This avoids a constrained-template symbol mismatch between GCC 13 and the prebuilt library's `ttnn::full<float>` instantiation.

`TT_METAL_HOME` points to source headers, while `TT_METAL_RUNTIME_ROOT` points to the installed JIT runtime. Using the source checkout as the runtime root without its generated `runtime/` tree causes missing firmware linker-script errors.
