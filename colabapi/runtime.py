"""Colab runtime (accelerator) catalog + mapping to `colab new` flags.

Google exposes no API that says which runtimes an account may allocate, so this
is a static catalog of the options the official CLI accepts, annotated with the
tier each typically requires. colabapi shows the menu and flags paid tiers; the
browser/Google is the source of truth for what actually gets allocated (a free
account asking for an A100 will simply be refused by Colab).

`colab_flags` is the argument list handed to `colab new`, kept here so the whole
accelerator mapping lives in one place. Validated against google-colab-cli as
documented mid-2026 (GPUs: T4, L4, G4, H100, A100; TPUs: v5e1, v6e1).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RuntimeType:
    key: str
    label: str
    accelerator: str
    tier: str  # "free", "pro", "pro+", "paid"
    notes: str
    colab_flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def free(self) -> bool:
        return self.tier == "free"


# Ordered most-accessible first.
RUNTIMES: list[RuntimeType] = [
    RuntimeType(
        key="cpu", label="CPU", accelerator="None", tier="free",
        notes="Always available. Good for lightweight demos and I/O work.",
        colab_flags=(),
    ),
    RuntimeType(
        key="t4", label="T4 GPU", accelerator="NVIDIA T4 (16 GB)", tier="free",
        notes="Baseline free GPU. Rate-limited; availability varies.",
        colab_flags=("--gpu", "T4"),
    ),
    RuntimeType(
        key="l4", label="L4 GPU", accelerator="NVIDIA L4 (24 GB)", tier="pro",
        notes="Usually Pro/Pay-As-You-Go; occasionally free.",
        colab_flags=("--gpu", "L4"),
    ),
    RuntimeType(
        key="g4", label="G4 GPU", accelerator="RTX PRO 6000 Blackwell (~96 GB)", tier="pro+",
        notes="High-VRAM Blackwell option; Pro+/PAYG.",
        colab_flags=("--gpu", "G4"),
    ),
    RuntimeType(
        key="a100", label="A100 GPU", accelerator="NVIDIA A100 (40 GB)", tier="pro+",
        notes="Pro+/PAYG; limited availability; high compute-unit burn.",
        colab_flags=("--gpu", "A100"),
    ),
    RuntimeType(
        key="h100", label="H100 GPU", accelerator="NVIDIA H100 (80 GB)", tier="pro+",
        notes="Newest, highest compute-unit burn rate; Pro+/PAYG.",
        colab_flags=("--gpu", "H100"),
    ),
    RuntimeType(
        key="tpu-v5e", label="TPU v5e", accelerator="Cloud TPU v5e-1", tier="paid",
        notes="Paid users; ~197 TFLOPs / 48 GB per core config.",
        colab_flags=("--tpu", "v5e1"),
    ),
    RuntimeType(
        key="tpu-v6e", label="TPU v6e", accelerator="Cloud TPU v6e-1", tier="paid",
        notes="Newest TPU; paid users.",
        colab_flags=("--tpu", "v6e1"),
    ),
]

RUNTIME_BY_KEY = {r.key: r for r in RUNTIMES}


def get(key: str) -> RuntimeType | None:
    return RUNTIME_BY_KEY.get(key)


def available_for_free() -> list[RuntimeType]:
    return [r for r in RUNTIMES if r.free]
