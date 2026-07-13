"""Live CPU / RAM / GPU monitor for a connected Colab runtime.

Stats are read from *inside* the runtime by executing a small Python snippet over
the same tunnel used for the shell (via `colab exec`, which runs Python), so the
monitor reflects the Colab VM, not your local machine. Rendering is decoupled
from transport: pass any callable that runs Python on the runtime and returns its
stdout.
"""

from __future__ import annotations

import time
from typing import Callable

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

RunRemote = Callable[[str], str]

# One Python program (run on the VM via `colab exec`) that emits CPU%, memory
# (MiB), and one "GPU <csv>" line per GPU. CPU/RAM come from psutil (with a
# /proc/meminfo fallback); GPU comes from nvidia-smi via subprocess. No GPU lines
# simply means a CPU-only runtime.
_STATS_SNIPPET = r'''
import subprocess
try:
    import psutil
    c = psutil.cpu_percent(interval=0.3)
    m = psutil.virtual_memory()
    print("CPU %.1f" % c)
    print("MEM %d %d" % (m.used // 1048576, m.total // 1048576))
except Exception:
    try:
        d = {}
        with open("/proc/meminfo") as f:
            for line in f:
                p = line.split()
                d[p[0].rstrip(":")] = int(p[1])
        total = d["MemTotal"] // 1024
        avail = d.get("MemAvailable", d.get("MemFree", 0)) // 1024
        print("CPU 0.0")
        print("MEM %d %d" % (total - avail, total))
    except Exception:
        print("CPU 0.0")
        print("MEM 0 0")
try:
    out = subprocess.run(
        ["nvidia-smi",
         "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=5).stdout
    for line in out.strip().splitlines():
        if line.strip():
            print("GPU " + line)
except Exception:
    pass
'''


def _bar(used: float, total: float, label: str, suffix: str) -> Table:
    pct = (used / total * 100) if total else 0.0
    grid = Table.grid(expand=True)
    grid.add_column(width=12)
    grid.add_column(ratio=1)
    grid.add_column(width=22, justify="right")
    grid.add_row(
        Text(label, style="bold cyan"),
        ProgressBar(total=100, completed=min(pct, 100), width=None),
        Text(suffix, style="dim"),
    )
    return grid


def _parse_cpu_mem(out: str) -> tuple[float, float, float]:
    cpu = used = total = 0.0
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0] == "CPU":
            cpu = float(parts[1])
        elif parts and parts[0] == "MEM":
            used, total = float(parts[1]), float(parts[2])
    return cpu, used, total


def _parse_gpu(out: str) -> list[dict]:
    gpus = []
    for line in out.splitlines():
        if not line.startswith("GPU "):
            continue
        cells = [c.strip() for c in line[4:].split(",")]
        if len(cells) >= 5:
            gpus.append(
                {
                    "name": cells[0],
                    "util": float(cells[1] or 0),
                    "mem_used": float(cells[2] or 0),
                    "mem_total": float(cells[3] or 0),
                    "temp": float(cells[4] or 0),
                }
            )
    return gpus


# The same reading, but as a program that keeps running and reports on its own
# clock. This is what the window's graphs consume (via ColabCLI.exec_stream): one
# connection, a line every second, rather than a fresh ~4s round trip per sample.
#
# psutil.cpu_percent(interval=1) is what paces the loop, and it is also the only
# correct way to read CPU: the figure is the busy fraction *between two calls*, so
# a snapshot with no interval is either meaningless or a lie. Sleeping separately
# would report the load of a process that was asleep.
STREAM_SNIPPET = r'''
import subprocess, sys, time
try:
    import psutil
except Exception:
    psutil = None

def mem_fallback():
    d = {}
    with open("/proc/meminfo") as f:
        for line in f:
            p = line.split()
            d[p[0].rstrip(":")] = int(p[1])
    total = d["MemTotal"] // 1024
    avail = d.get("MemAvailable", d.get("MemFree", 0)) // 1024
    return total - avail, total

while True:
    try:
        if psutil is not None:
            cpu = psutil.cpu_percent(interval=1.0)
            m = psutil.virtual_memory()
            used, total = m.used // 1048576, m.total // 1048576
        else:
            time.sleep(1.0)
            cpu = 0.0
            used, total = mem_fallback()
        print("CPU %.1f" % cpu)
        print("MEM %d %d" % (used, total))
        try:
            out = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5).stdout
            for line in out.strip().splitlines():
                if line.strip():
                    print("GPU " + line)
        except Exception:
            pass
        print("END")
        sys.stdout.flush()
    except Exception as exc:
        print("ERR %s" % exc)
        sys.stdout.flush()
        time.sleep(1.0)
'''


def parse_block(lines: list) -> dict:
    """Turn one CPU/MEM/GPU block from the stream into a reading."""
    out = "\n".join(lines)
    cpu, mem_used, mem_total = _parse_cpu_mem(out)
    return {"cpu": cpu, "mem_used": mem_used, "mem_total": mem_total,
            "gpus": _parse_gpu(out)}


def read_stats(run_remote: RunRemote) -> dict:
    """One reading of the runtime: CPU %, RAM, and every GPU.

    The single place the runtime's vitals are read. Both front ends use it -- the
    CLI monitor below, and the window's live graphs -- so they can never disagree
    about what the numbers mean.
    """
    out = run_remote(_STATS_SNIPPET)
    cpu, mem_used, mem_total = _parse_cpu_mem(out)
    return {"cpu": cpu, "mem_used": mem_used, "mem_total": mem_total,
            "gpus": _parse_gpu(out)}


def build_panel(run_remote: RunRemote, session_line: str = "") -> Panel:
    stats = read_stats(run_remote)
    cpu = stats["cpu"]
    mem_used, mem_total = stats["mem_used"], stats["mem_total"]
    gpus = stats["gpus"]

    rows = [
        _bar(cpu, 100, "CPU", f"{cpu:.0f}%"),
        _bar(mem_used, mem_total, "RAM", f"{mem_used/1024:.1f} / {mem_total/1024:.1f} GiB"),
    ]
    for i, g in enumerate(gpus):
        rows.append(
            _bar(g["util"], 100, f"GPU{i}", f"{g['util']:.0f}%  {g['temp']:.0f}°C")
        )
        rows.append(
            _bar(
                g["mem_used"],
                g["mem_total"],
                "  VRAM",
                f"{g['mem_used']/1024:.1f} / {g['mem_total']/1024:.1f} GiB",
            )
        )
    if not gpus:
        rows.append(Text("  No GPU on this runtime (CPU-only).", style="dim"))

    title = "colabapi runtime monitor"
    subtitle = session_line or "Ctrl+C to exit monitor"
    return Panel(Group(*rows), title=title, subtitle=subtitle, border_style="cyan")


def live_monitor(run_remote: RunRemote, session_line_fn: Callable[[], str], interval: float = 2.0) -> None:
    """Render the monitor until interrupted with Ctrl+C."""
    with Live(refresh_per_second=4, screen=False) as live:
        try:
            while True:
                live.update(build_panel(run_remote, session_line_fn()))
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
