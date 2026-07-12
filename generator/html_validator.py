"""
html_validator.py — Post-assembly HTML validation.

Checks:
  1. Div balance per destination section
  2. No orphan <script> tags outside designated blocks
  3. var DRIVE_DESCRIPTIONS present (not const)
  4. Drive modal element IDs match DRIVE_DESCRIPTIONS keys
  5. Image count >= min_per_destination per section
"""
from __future__ import annotations
import logging, re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
MIN_PER_DESTINATION_DEFAULT = 2


class HTMLValidator:
    def __init__(self, config_path: str | Path = "config.yaml") -> None:
        import yaml
        with Path(config_path).open() as f:
            cfg = yaml.safe_load(f)
        self._min_images = cfg.get("images", {}).get("min_per_destination", MIN_PER_DESTINATION_DEFAULT)

    def validate(self, html_path: str | Path, trip: dict[str, Any]) -> dict[str, Any]:
        html_path = Path(html_path)
        html = html_path.read_text(encoding="utf-8")
        errors: list[str] = []
        warnings: list[str] = []

        self._check_drive_descriptions_var(html, errors)
        self._check_drive_modal_keys(html, trip, errors)
        self._check_section_div_balance(html, trip, errors)
        self._check_script_isolation(html, warnings)
        self._check_image_counts(html, trip, errors)

        report = {
            "html_path": str(html_path),
            "errors": errors,
            "warnings": warnings,
            "valid": len(errors) == 0,
            "error_count": len(errors),
            "warning_count": len(warnings),
        }
        if errors:
            logger.error("Validation FAILED: %d error(s)", len(errors))
            for e in errors:
                logger.error("  ✗ %s", e)
        else:
            logger.info("Validation passed ✓ (%d warning(s))", len(warnings))
        return report

    # ── Check 1: var (not const) DRIVE_DESCRIPTIONS ─────────────────────────

    def _check_drive_descriptions_var(self, html: str, errors: list[str]) -> None:
        if "var DRIVE_DESCRIPTIONS" not in html:
            if "const DRIVE_DESCRIPTIONS" in html:
                errors.append(
                    "DRIVE_DESCRIPTIONS declared with 'const' — must use 'var' for compatibility"
                )
            else:
                errors.append("DRIVE_DESCRIPTIONS not found in output HTML")

    # ── Check 2: Drive modal IDs match DRIVE_DESCRIPTIONS keys ───────────────

    def _check_drive_modal_keys(self, html: str, trip: dict[str, Any], errors: list[str]) -> None:
        # Extract keys from var DRIVE_DESCRIPTIONS = { ... }
        match = re.search(r'var DRIVE_DESCRIPTIONS\s*=\s*(\{.*?\});', html, re.DOTALL)
        if not match:
            return  # Already flagged by check 1
        try:
            import json
            dd = json.loads(match.group(1))
        except Exception:
            errors.append("DRIVE_DESCRIPTIONS is not valid JSON — cannot validate drive modal keys")
            return

        # Extract data-drive-key attributes from HTML
        modal_keys = set(re.findall(r'data-drive-key="([^"]+)"', html))
        dd_keys = set(dd.keys())

        orphan_modals = modal_keys - dd_keys
        missing_modals = dd_keys - modal_keys
        if orphan_modals:
            errors.append(f"Drive modal buttons with no DRIVE_DESCRIPTIONS entry: {sorted(orphan_modals)}")
        if missing_modals:
            errors.append(f"DRIVE_DESCRIPTIONS keys with no modal button: {sorted(missing_modals)}")

    # ── Check 3: Div balance per section ─────────────────────────────────────

    def _check_section_div_balance(self, html: str, trip: dict[str, Any], errors: list[str]) -> None:
        for dest in trip.get("destinations", []):
            dest_id = dest["id"]
            # Find section start/end
            start_pat = re.compile(rf'<section[^>]+id="{re.escape(dest_id)}"[^>]*>', re.IGNORECASE)
            start_m = start_pat.search(html)
            end_m = re.search(r'</section>', html[start_m.end():]) if start_m else None
            if not start_m or not end_m:
                errors.append(f"Could not locate section for destination '{dest_id}'")
                continue
            section_html = html[start_m.start():start_m.end() + end_m.end()]
            opens = len(re.findall(r'<div\b', section_html, re.IGNORECASE))
            closes = len(re.findall(r'</div>', section_html, re.IGNORECASE))
            if opens != closes:
                errors.append(
                    f"Div balance mismatch in section '{dest_id}': "
                    f"{opens} <div> vs {closes} </div>"
                )

    # ── Check 4: Script isolation ─────────────────────────────────────────────

    def _check_script_isolation(self, html: str, warnings: list[str]) -> None:
        # Scripts should appear only inside <head> or before </body>
        # Orphan = script inside a destination section
        section_pattern = re.compile(
            r'<section[^>]+class="destination-section".*?</section>', re.DOTALL | re.IGNORECASE
        )
        for section_m in section_pattern.finditer(html):
            section_content = section_m.group()
            if '<script' in section_content.lower():
                # Extract section id for useful message
                id_m = re.search(r'id="([^"]+)"', section_content)
                section_id = id_m.group(1) if id_m else "unknown"
                warnings.append(f"Orphan <script> tag found inside section '{section_id}'")

    # ── Check 5: Image counts ────────────────────────────────────────────────

    def _check_image_counts(self, html: str, trip: dict[str, Any], errors: list[str]) -> None:
        for dest in trip.get("destinations", []):
            count = len(dest.get("images", []))
            if count < self._min_images:
                errors.append(
                    f"Destination '{dest['id']}' has {count} image(s) "
                    f"(minimum: {self._min_images})"
                )
