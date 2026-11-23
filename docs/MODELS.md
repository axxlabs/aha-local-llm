# Models

Aha! runs one model at a time, chosen for the whole household in the app's **Settings → Model** (SPEC §3.2).
Each choice is a preset of `deploy/models.ini`, and `aha models` downloads them all:

| Choice | File | Notes |
|---|---|---|
| **Qwen3.6 35B** (default) | `Qwen3.6-35B-A3B-UD-Q4_K_XL` (22 GB) + vision 0.9 GB | Best answers; on a 16 GB card part of it runs from RAM |
| Qwen3.5 9B | `Qwen3.5-9B-UD-Q6_K_XL` (8.8 GB) + vision 0.9 GB | Fits on the card: ~6,000 tok/s prompt, ~83 tok/s generation |
| Qwen3.6 35B, 3-bit | `Qwen3.6-35B-A3B-UD-IQ3_XXS` (13.2 GB) + the same vision file | Being tried |
| Gemma 4 26B | `gemma-4-26B-A4B-it-qat-UD-Q4_K_XL` (14.2 GB) + vision 1.2 GB | Being tried; Apache-2.0, 4B active |
| Gemma 4 12B | `gemma-4-12B-it-qat-UD-Q4_K_XL` (6.7 GB) + vision 0.2 GB | Being tried; Apache-2.0 |

Measured on the reference host (RTX 5080 16 GB, 125 GB RAM, Ryzen 7 5700X3D), llama.cpp with `--fit on` and a
128k context:

| Model | Licence | Size | Generation | Notes |
|---|---|---|---|---|
| **Qwen3.6-35B-A3B** (default) | Apache-2.0 | 22 GB + 0.9 GB vision | ~80 tok/s | Fast, sees images, good tool calling; overthinks unless `reasoning-budget` is capped (1024) |
| gpt-oss-120b | Apache-2.0 | 63 GB | ~22 tok/s | Text only; strong reasoning; `reasoning-effort low` keeps answers quick |
| Qwen3.8-Flash-Next (UD-IQ4_XS) | Qwen Community 1.0 | 95 GB + 0.9 GB vision | ~6 tok/s | Vision and computer-use benchmarks, but too slow here for browsing; licence restricts model-as-a-service and "AI work assistant" businesses |

To add a choice, add a section to `deploy/models.ini` (see llama.cpp's router presets) and an entry with the
same name to `MODELS` in `aha_local/models.py`, then `aha install && aha restart`. Keep a vision projector
(`mmproj`) for browsing: approvals and pages are read from screenshots.
