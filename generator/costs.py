"""Cost summary output helpers."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any


def print_cost_summary(
    *,
    model: str,
    manifest_path: str,
    predicted_usd: float,
    actual_usd: float,
    environment: str = "dev",
) -> None:
    """Print formatted LLM cost summary."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    manifest_name = Path(manifest_path).name
    print(f"[LLM-COST] {ts} | {model} | {manifest_name}")
    print(f"  Predicted USD : ${predicted_usd:.4f}")
    print(f"  Actual USD    : ${actual_usd:.4f}")
    print(f"  Environment   : {environment}")


def summarize_from_usage(usage: dict[str, Any]) -> tuple[float, float]:
    """Return predicted and actual costs.

    This pipeline currently has model pricing estimates, so actual equals predicted.
    Debugs if usage is empty.
    """
    import logging
    logger = logging.getLogger(__name__)
    if not usage:
        logger.warning("summarize_from_usage: usage dict is empty")
    predicted = float(usage.get("total_estimated_cost_usd", 0.0) or 0.0)
    actual = predicted
    logger.info(f"Cost summary: predicted=${predicted:.4f}, actual=${actual:.4f} (from {len(usage)} keys)")
    return predicted, actual
