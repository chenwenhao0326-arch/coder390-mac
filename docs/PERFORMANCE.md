# Existing measurements

These measurements are on an Apple M2 Ultra, 76 GPU cores, 192GB unified memory, using the adopted engine. They are **not M1 Max results** and not measurements of the new compact32 preset.

2026-10-28: one fixed Q8/Q3/Q3/Q8 campaign, seeds 42/17, six throughput prompts and eight functional prompts per arm. Same optimized engine, MTP4, 262144 context, F16 KV, batch 2048/512. Throughput prompts generate 256 tokens; input is at most about4K. Output content changes with quantization.

| Scenario | Q8_0-MTP | Q3LynnStyle-MTP |
|---|---:|---:|
| Original four-case aggregate | 44.17 tok/s | 37.86 tok/s |
| Chinese, thinking off | 34.09 | 30.09 |
| Python, thinking off | 58.05 | 50.76 |
| Chinese, thinking on | 42.66 | 36.10 |
| Python, thinking on | 46.56 | 34.08 |
| Peak process RSS | 52.27 GiB | 41.31 GiB |
| Model file | 27.07 GiB | 16.12 GiB |

Q3 was 14.30% slower in the primary aggregate, with negative signs in both seeds, while saving about11GiB RSS. Code checks passed9/10 onQ8 and8/10 onQ3; Chinese JSON4/6 and5/6 respectively. These small samples do not establish broad model quality equivalence. Thinking-on speed can measure the reasoning phase rather than final prose.

The adopted optimization sequence was draft-vocabulary trimming, compact Top-K, and Metal unary/broadcast/GLU dispatch. The last two incremental trials measured approximately+1.87% and+1.35% over their respective previous releases, with24/24 paired outputs identical in each trial. Those percentages are not a promise on other models or hardware and should not be added to a different baseline.

Q3's mixed K-quantization uses different Metal execution paths from Q8_0. Smaller model files do not guarantee faster generation. No custom M1-only Q3 kernel or large M1 speedup is claimed.
