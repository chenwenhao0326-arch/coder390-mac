# v0.2 preview — page settings and image input

2026-10-28. Hardware: **M2 Ultra /192GB /macOS26.4.1** using the `compact32` preset. **M1 Max32GB is still not physically tested.**

## Passed

- 17 new focused Python checks cover projector checksum/receipt reuse,32GB vision configuration, busy-request rejection, stale settings, persistence, failed-load rollback, memory precheck, Host/Origin/token enforcement, user-initiated navigation versus foreign iframe access, request bounds, static-path isolation, and non-buffered chunked SSE forwarding.
- The previously passed busy-port check was rerun because the launcher changed. Other unaffected v0.1 checks and the full model benchmark were reused.
- A service-worker regression check verifies that activation finishes without waiting for a navigation which may itself wait for activation; only owned UI/API caches are affected and old unmarked tabs are not navigated.
- The real model loaded the publisher's Q8 visual projector. A synthetic image uploaded through the actual llama UI was recognized correctly: the central rectangle was red, and the model answered“红色”. This is a basic image integration check, not a visual-quality benchmark.
- The actual page settings panel changed8K to16K with vision still enabled. The same conversation, image, answer and unsent text survived the automatic refresh. Both UI context display and backend state reflected16K.
- Invalid projector paths returned400 and stale versions409 without changing the running model PID.
- After completely stopping and restarting the launcher without context or projector flags,16K and vision-enabled settings were restored. A token from the previous manager process was rejected with403.
- Peak model-process RSS observed across image smoke / configuration change / persistence checks: approximately **20.11GiB**, with zero swap. This is not peak memory for a full16K prompt, many images, sustained usage or an actual32GB machine.
- Owned model processes were stopped and the public test port was released. The original daily Q8 deployment and DSH service were unchanged.

## Repairs and test scope

The first browser attempt exposed an old-UI cache migration problem: service-worker activation awaited navigation. It was fixed, and the normal managed launch URL connected successfully afterward. That attempt reached its predeclared time limit and the test service shut down. Its already-passed image request was preserved; only remaining UI/restart/persistence checks resumed. No image answer or full inference benchmark was repeatedly generated.

One Python fixture expected an unresolved macOS`/var`path while the implementation correctly returned its resolved`/private/var`path. The assertion was corrected and only that failed check was rerun. The original inference-source patches and compiled engine were not changed or rebuilt for this feature.

The v0.1 text-only measurement and documentation are preserved in[VALIDATION.md](VALIDATION.md). In particular, a small observed RSS is not a guarantee for every M1 Max32GB/macOS/SDK/workload combination. Video/audio behavior and real out-of-memory recovery were not stress-tested.

![Image answer preserved after changing to16K](images/controls-vision.png)

This screenshot contains a two-token functional response. Its instantaneous tok/s is not a performance result.
