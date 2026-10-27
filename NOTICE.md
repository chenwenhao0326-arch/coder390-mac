# Sources and attribution

- llama.cpp: https://github.com/ggml-org/llama.cpp, pinned commit `620d2225cc436ab0b001e3200e590e3b286ef9ab`, MIT. Its source archive retains upstream and vendored dependency notices. The patch series is applied on top of that archive; this repository is not an official llama.cpp distribution.
- Draft vocabulary and compact Top-K: project changes on the pinned llama.cpp source. The integer token-ID list was selected for Chinese and code workloads; it is not a tokenizer replacement and is validated against the exact tokenizer metadata fingerprint.
- Metal threadgroup patch: adapted from upstream PR https://github.com/ggml-org/llama.cpp/pull/31075, commits `552d79a19dda65482c1af8548392ec1cf28702d7` and `0187b796e4a13983bbb4a8bb64ee33da3e41df46`. Only unary/broadcast/GLU dispatch changes are included; no MXFP4 changes.
- Native MTP and Few-row MMA are upstream llama.cpp work, not claimed as new work by this project.
- Chat UI: official llama.cpp release `b9850`, source commit `4f31eedb0ccf546b7e8d6bb243b170f12522f54d`. Downloaded unchanged from its official release asset, pinned by SHA256. It retains its own dependencies and notices. No user conversations are bundled.
- Since v0.2, the local supervisor adds this project's model-settings script when serving the UI and supplies a live-service worker policy. The downloaded upstream files remain unchanged; only UI asset/API caches are migrated, not the chat IndexedDB. These wrapper changes are this project's work, not upstream llama.cpp features.
- CMake: version 3.31.6, official PyPI universal2 wheel. Downloaded and locally extracted as a build tool, with its upstream license files intact. No global installation is performed.
- Model: user-provided Coder390 GGUF. Model copyright, licensing and quantization are the model publisher's; model weights are not included, downloaded or re-quantized by this project.

The package contains only deployment code, patch files, an integer vocabulary list and documentation. Runtime binaries, downloaded third-party artifacts and all local runtime state are ignored by Git.
