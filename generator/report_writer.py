"""
report_writer.py — Write JSON validation report to output directory.
"""
from __future__ import annotations
import datetime, json
from pathlib import Path
from typing import Any

from generator import __version__, __template_version__


class ReportWriter:
    def __init__(self, output_dir: str | Path = "output") -> None:
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def write(self, report: dict[str, Any]) -> Path:
        meta = report.get("meta", {})
        llm_usage = report.get("llm_usage", {})
        out = {
            "generator_version": meta.get("generator_version", __version__),
            "template_version": meta.get("template_version", __template_version__),
            "generated_at_utc": meta.get("generated_at_utc", ""),
            "timestamp_utc": datetime.datetime.utcnow().isoformat() + "Z",
            "summary": {
                "valid": report.get("valid", False),
                "error_count": report.get("error_count", 0),
                "warning_count": report.get("warning_count", 0),
            },
            "llm": {
                "provider": meta.get("llm", {}).get("provider", ""),
                "model": meta.get("llm", {}).get("model", ""),
                "models": llm_usage.get("models", []),
                "total_calls": llm_usage.get("total_calls", 0),
                "total_estimated_cost_usd": llm_usage.get("total_estimated_cost_usd", 0.0),
            },
            "errors": report.get("errors", []),
            "warnings": report.get("warnings", []),
            "html_path": report.get("html_path", ""),
        }
        report_path = self._output_dir / "validation_report.json"
        report_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
        return report_path
