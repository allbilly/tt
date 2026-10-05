# Pinned sources and tested host

[Back to README](../README.md) · [Setup and rerun](setup.md)

## Source revisions

The implementation and recorded artifacts use these exact revisions:

| Source | Revision | Use |
| --- | --- | --- |
| [tenstorrent/ttsim](https://github.com/tenstorrent/ttsim/tree/3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a) | `3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a` (`v1.10.1`) | Official simulator, C ABI, decoder, Blackhole model |
| [tt-isa-documentation](https://github.com/tenstorrent/tt-isa-documentation/tree/ea0aed9c15b254f99765380f7c895adf10a1ed6c) | `ea0aed9c15b254f99765380f7c895adf10a1ed6c` | Blackhole instruction and register documentation |
| [blackhole-py](https://github.com/boopdotpng/blackhole-py/tree/d8eae8ff54ba3d733a7b0b947c785e0224cf9f1a) | `d8eae8ff54ba3d733a7b0b947c785e0224cf9f1a` | Low-level programming reference only; no code copied |
| [allbilly/ane](https://github.com/allbilly/ane/tree/6838f343ff1e39bd28270302f7e25783a62b37c4) | `6838f343ff1e39bd28270302f7e25783a62b37c4` | Repository organization reference only |
| [allbilly/rk3588](https://github.com/allbilly/rk3588/tree/c6944a6513de7c620aa51384f14dda257db4a574) | `c6944a6513de7c620aa51384f14dda257db4a574` | Repository organization reference only |

The official source files used for the ABI and target include [`docs/libttsim_api.md`](https://github.com/tenstorrent/ttsim/blob/3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a/docs/libttsim_api.md), [`src/libttsim.cpp`](https://github.com/tenstorrent/ttsim/blob/3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a/src/libttsim.cpp), [`src/sim.h`](https://github.com/tenstorrent/ttsim/blob/3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a/src/sim.h), [`src/tensix.cpp`](https://github.com/tenstorrent/ttsim/blob/3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a/src/tensix.cpp), and [`data/bh/tensix_isa.json`](https://github.com/tenstorrent/ttsim/blob/3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a/data/bh/tensix_isa.json). The official [BlackholeA0 ISA files](https://github.com/tenstorrent/tt-isa-documentation/tree/ea0aed9c15b254f99765380f7c895adf10a1ed6c/BlackholeA0) are authoritative for instruction fields and the BRISC [Tensix FIFO push mechanism](https://github.com/tenstorrent/tt-isa-documentation/blob/ea0aed9c15b254f99765380f7c895adf10a1ed6c/BlackholeA0/TensixTile/BabyRISCV/PushTensixInstruction.md). The community reference checkout had no license file; no community source was reused. The trace diff modifies Tenstorrent's Apache-2.0-licensed `src/tensix.cpp` and `src/libttsim.cpp`; upstream SPDX headers are retained in the patch context.

## Tested host and libraries

The measured environment was Fedora 44, x86-64, AMD Ryzen 7 4700U, 36 GiB RAM, and 391 GiB free on the workspace filesystem. The CPU advertises the x86-64-v3 instruction set, including AVX/AVX2, BMI1/2, F16C, FMA, LZCNT, MOVBE, POPCNT, XSAVE, and the required SSE features. Tested tools were GCC 16.1.1, Python 3.14.5, LLVM 22.1.7, Git 2.54.0, curl 8.18.0, and Podman 5.8.2. Podman was available but unused. The stock x86-64 library SHA256 is `b276a5fba26064eb72fe964126e34bad4bbf515175890570ba2eaef670fed572`. The traced build is source revision `3d82cd1b8e3c3d8fa69abb8ac3835c4713e4e52a`, compiled for `release_bh` with `-march=x86-64-v3`, `-O2`, `-DTT_ARCH_VERSION=1`, and one chip; exact flags and hashes are in the external build manifest.
