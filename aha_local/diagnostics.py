# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Live diagnostics for the app's header (SPEC §7.4 "Live diagnostics"): the computer's CPU load and
temperature, memory, and the GPU's load, temperature and memory, read every few seconds while someone has the app
open. Read from the host's own counters: /proc and /sys (the container sees the host's), and nvidia-smi for an
NVIDIA GPU (the agent has the GPU device for that, as the model server does). Whatever can't be read is left
out: a computer without an NVIDIA GPU shows the CPU and memory only."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from pathlib import Path

log = logging.getLogger("aha.diagnostics")

EVERY = 3.0  # seconds between readings while a device is online
CPU_SENSORS = ("k10temp", "zenpower", "coretemp", "cpu_thermal", "cpu-thermal")  # AMD, Intel, ARM boards
GPU_QUERY = "utilization.gpu,temperature.gpu,memory.used,memory.total"


class Reader:
    """Readings, each from the host's counters; the CPU's load is the change since the last reading."""

    def __init__(self, proc: Path = Path("/proc"), sys: Path = Path("/sys")) -> None:
        self.proc, self.sys = proc, sys
        self._cpu: tuple[int, int] | None = None
        self._smi = shutil.which("nvidia-smi")

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

    async def gpu(self) -> dict | None:
        if not self._smi:
            return None
        try:
            run = await asyncio.create_subprocess_exec(
                self._smi,
                f"--query-gpu={GPU_QUERY}",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(run.communicate(), timeout=3)
            load, temp, used, total = (float(v) for v in out.decode().splitlines()[0].split(","))
        except Exception:
            log.debug("nvidia-smi gave no reading")
            return None
        return {
            "load": round(load),
            "temp": round(temp),
            "mem": round(used / 1024, 1),
            "mem_total": round(total / 1024, 1),
        }

    async def read(self) -> dict:
        """One reading, as sent to the app: {"t": "stats", "cpu": {...}, "mem": {...}, "gpu": {...}}."""
        out: dict = {"t": "stats"}
        cpu = {k: v for k, v in (("load", self.cpu_load()), ("temp", self.cpu_temp())) if v is not None}
        if cpu:
            out["cpu"] = cpu
        if mem := self.memory():
            out["mem"] = {"used": mem[0], "total": mem[1]}
        if gpu := await self.gpu():
            out["gpu"] = gpu
        return out
