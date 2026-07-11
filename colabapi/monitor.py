"""Live CPU / RAM / GPU monitor for a connected Colab runtime.

Stats are read from *inside* the runtime by running small shell snippets over the
same tunnel used for the shell, so the monitor reflects the Colab VM, not your
local machine. Rendering is decoupled from transport: pass any callable that
executes a command on the runtime and returns its stdout.
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

# One-shot snippet: emit CPU%, mem used/total (MiB) as parseable lines.
_CPU_MEM_SNIPPET = (
    "python3 - <<'PY'\n"
    "import time\n"
    "try:\n"
    "    import psutil\n"
    "    c = psutil.cpu_percent(interval=0.4)\n"
    "    m = psutil.virtual_memory()\n"
    "    print(f'CPU {c:.1f}')\n"
    "    print(f'MEM {m.used//1048576} {m.total//1048576}')\n"
    "except Exception:\n"
    "    # Fallback without psutil.\n"
    "    with open('/proc/meminfo') as f:\n"
    "        d = {}\n"
    "        for line in f:\n"
    "            k, v = line.split(':')[0], line.split()[1]\n"
    "            d[k] = int(v)\n"
    "    total = d['MemTotal']//1024\n"
    "    avail = d.get('MemAvailable', d['MemFree'])//1024\n"
    "    print('CPU 0.0')\n"
    "    print(f'MEM {total-avail} {total}')\n"
    "PY"
)

# GPU via nvidia-smi; empty output means no GPU on this runtime.
_GPU_SNIPPET = (
    "nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu "
    "--format=csv,noheader,nounits 2>/dev/null || true"
)


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
    for line in out.strip().splitlines():
        cells = [c.strip() for c in line.split(",")]
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


def build_panel(run_remote: RunRemote, session_line: str = "") -> Panel:
    cpu, mem_used, mem_total = _parse_cpu_mem(run_remote(_CPU_MEM_SNIPPET))
    gpus = _parse_gpu(run_remote(_GPU_SNIPPET))

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
