"""
report_writer.py — Write JSON validation report to output directory.
"""
from __future__ import annotations
import datetime, json
from pathlib import Path
from typing import Any

GENERATOR_VERSION = "1.0.0"


class ReportWriter:
    def __init__(self, output_dir: str | Path = "output") -> None:
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def write(self, report: dict[str, Any]) -> Path:
        out = {
            "generator_version": GENERATOR_VERSION,
            "timestamp_utc": datetime.datetime.utcnow().isoformat() + "Z",
            "summary": {
                "valid": report.get("valid", False),
                "error_count": report.get("error_count", 0),
                "warning_count": report.get("warning_count", 0),
            },
            "errors": report.get("errors", []),
            "warnings": report.get("warnings", []),
            "html_path": report.get("html_path", ""),
        }
        report_path = self._output_dir / "validation_report.json"
        report_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
        return report_path
