"""Cost summary output helpers."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any


def print_cost_summary(*, model: str, manifest_path: str, predicted_usd: float, actual_usd: float) -> None:
    """Print one-line standardized LLM cost summary."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    manifest_name = Path(manifest_path).name
    print(
        f"[LLM-COST] {ts} | {model} | {manifest_name} | "
        f"predicted={predicted_usd:.6f} | actual={actual_usd:.6f}"
    )


def summarize_from_usage(usage: dict[str, Any]) -> tuple[float, float]:
    """Return predicted and actual costs.

    This pipeline currently has model pricing estimates, so actual equals predicted.
    """
    predicted = float(usage.get("total_estimated_cost_usd", 0.0) or 0.0)
    actual = predicted
    return predicted, actual
