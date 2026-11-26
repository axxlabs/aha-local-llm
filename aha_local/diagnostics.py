# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Live diagnostics for the app's header (SPEC §7.4 "Live diagnostics"): the computer's CPU load and
temperature, memory, and the GPU's load, temperature and memory, read every few seconds while someone has the app
open. Read from the host's own counters: /proc and /sys (the container sees the host's), and NVIDIA's library
(NVML) for an NVIDIA GPU (the agent has the GPU device for that, as the model server does). The GPU's load is the
average of every sample since the last reading: nvidia-smi's one figure covers only the last moment, and the
model works in bursts, so it read 0% almost always (2026-11-26). Whatever can't be read is left out: a computer
without an NVIDIA GPU shows the CPU and memory only."""

from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path

log = logging.getLogger("aha.diagnostics")

EVERY = 3.0  # seconds between readings while a device is online
CPU_SENSORS = ("k10temp", "zenpower", "coretemp", "cpu_thermal", "cpu-thermal")  # AMD, Intel, ARM boards


class Reader:
    """Readings, each from the host's counters; the CPU's load is the change since the last reading."""

    def __init__(self, proc: Path = Path("/proc"), sys: Path = Path("/sys")) -> None:
        self.proc, self.sys = proc, sys
        self._cpu: tuple[int, int] | None = None
        self._gpu = None  # NVML's handle for the first GPU, once it's opened; False: there's none
        self._gpu_since = 0  # microseconds: the last GPU sample already counted

    def cpu_load(self) -> int | None:
        """Percent busy across all cores since the last reading (None the first time)."""
        try:
            fields = [int(v) for v in (self.proc / "stat").read_text().splitlines()[0].split()[1:]]
        except (OSError, ValueError, IndexError):
            return None
        idle, total = fields[3] + (fields[4] if len(fields) > 4 else 0), sum(fields)
        before, self._cpu = self._cpu, (idle, total)
        if before is None or total <= before[1]:
            return None
        return round(100 * (1 - (idle - before[0]) / (total - before[1])))

    def cpu_temp(self) -> int | None:
        for mon in sorted((self.sys / "class" / "hwmon").glob("hwmon*")):
            with contextlib.suppress(OSError, ValueError):
                if (mon / "name").read_text().strip() in CPU_SENSORS:
                    return round(int((mon / "temp1_input").read_text()) / 1000)
        return None

    def memory(self) -> tuple[float, float] | None:
        """Used and total, in GB (used: what isn't available, so caches don't count)."""
        try:
            info = dict(
                (k, int(v.split()[0]))
                for k, v in (ln.split(":", 1) for ln in (self.proc / "meminfo").read_text().splitlines())
            )
            total, free = info["MemTotal"], info["MemAvailable"]
        except (OSError, ValueError, KeyError):
            return None
        return round((total - free) / 1024**2, 1), round(total / 1024**2, 1)

    def _nvml(self):
        if self._gpu is None:
            try:
                import pynvml

                pynvml.nvmlInit()
                self._gpu = pynvml.nvmlDeviceGetHandleByIndex(0)
            except Exception:
                log.info("no NVIDIA GPU readings: the panel shows the CPU and memory")
                self._gpu = False
        return self._gpu or None

    def gpu(self) -> dict | None:
        handle = self._nvml()
        if handle is None:
            return None
        import pynvml

        try:
            since = self._gpu_since or int((time.time() - EVERY) * 1_000_000)
            _, samples = pynvml.nvmlDeviceGetSamples(handle, pynvml.NVML_GPU_UTILIZATION_SAMPLES, since)
            values = [x.sampleValue.uiVal for x in samples if x.timeStamp > since]
            if samples:
                self._gpu_since = max(x.timeStamp for x in samples)
            load = round(sum(values) / len(values)) if values else pynvml.nvmlDeviceGetUtilizationRates(handle).gpu
            temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        except Exception:
            log.debug("no GPU reading this time")
            return None
        return {
            "load": load,
            "temp": temp,
            "mem": round(mem.used / 1024**3, 1),
            "mem_total": round(mem.total / 1024**3, 1),
        }

    async def read(self) -> dict:
        """One reading, as sent to the app: {"t": "stats", "cpu": {...}, "mem": {...}, "gpu": {...}}."""
        out: dict = {"t": "stats"}
        cpu = {k: v for k, v in (("load", self.cpu_load()), ("temp", self.cpu_temp())) if v is not None}
        if cpu:
            out["cpu"] = cpu
        if mem := self.memory():
            out["mem"] = {"used": mem[0], "total": mem[1]}
        if gpu := self.gpu():
            out["gpu"] = gpu
        return out
