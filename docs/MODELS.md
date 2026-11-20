# Models

aha v1 runs one model: **Qwen3.6-35B-A3B** (Apache-2.0), GGUF `UD-Q4_K_XL` from unsloth (22 GB) plus its
vision projector `mmproj-F16` (0.9 GB), so it can read screenshots. `aha models download` fetches both.

Measured on the reference host (RTX 5080 16 GB, 125 GB RAM, Ryzen 7 5700X3D), llama.cpp with `--fit on` and a
128k context:

| Model | Licence | Size | Generation | Notes |
|---|---|---|---|---|
| **Qwen3.6-35B-A3B** (default) | Apache-2.0 | 22 GB + 0.9 GB vision | ~80 tok/s | Fast, sees images, good tool calling; overthinks unless `reasoning-budget` is capped (1024) |
| gpt-oss-120b | Apache-2.0 | 63 GB | ~22 tok/s | Text only; strong reasoning; `reasoning-effort low` keeps answers quick |
| Qwen3.8-Flash-Next (UD-IQ4_XS) | Qwen Community 1.0 | 95 GB + 0.9 GB vision | ~6 tok/s | Vision and computer-use benchmarks, but too slow here for browsing; licence restricts model-as-a-service and "AI work assistant" businesses |

To try another model, add a section to `deploy/models.ini` (see llama.cpp's router presets), set
`AHA_LLM_MODEL` for the agent to its section name, and `aha install && aha restart`. Keep a vision projector
(`mmproj`) for browsing: approvals and pages are read from screenshots.
