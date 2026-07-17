"""
manifest_parser.py — YAML manifest parsing and schema validation.

Seeds must be plain name strings only — no URLs. The generator resolves
all URLs independently via Bing Search after content generation.
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Any
import yaml
import jsonschema

logger = logging.getLogger(__name__)

MANIFEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["trip", "destinations"],
    "properties": {
        "trip": {
            "type": "object",
            "required": ["title", "subtitle", "theme_color"],
            "properties": {
                "title": {"type": "string"},
                "subtitle": {"type": "string"},
                "theme_color": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"},
                "llm_provider": {
                    "type": "string",
                    "enum": ["openai", "anthropic", "deepseek", "gemini", "grok", "azure_openai"],
                },
                "llm_features": {
                    "type": "object",
                    "properties": {
                        "code_execution": {"const": True},
                    },
                    "additionalProperties": False,
                },
                "llm": {
                    "type": "object",
                    "properties": {
                        "provider": {
                            "type": "string",
                            "enum": ["openai", "anthropic", "deepseek", "gemini", "grok", "azure_openai"],
                        },
                        "features": {
                            "type": "object",
                            "properties": {
                                "code_execution": {"const": True},
                            },
                            "additionalProperties": False,
                        },
                        "model": {"type": "string", "minLength": 2},
                        "temperature": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "max_tokens": {"type": "integer", "minimum": 256, "maximum": 16384},
                    },
                    "additionalProperties": False,
                },
            },
        },
        "destinations": {
            "type": "array",
            "minItems": 1,
            "maxItems": 15,
            "items": {
                "type": "object",
                "required": ["id", "name", "dates", "planning_links"],
                "properties": {
                    "id": {"type": "string", "pattern": "^[a-z0-9_]+$"},
                    "name": {"type": "string", "minLength": 2},
                    "dates": {"type": "string"},
                    "planning_links": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["label", "url"],
                            "properties": {
                                "label": {"type": "string"},
                                "url": {"type": "string"},
                            },
                        },
                    },
                    "seeds": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 2},
                        "description": "Attraction/hike/experience name hints only — no URLs.",
                    },
                },
            },
        },
    },
}


class ManifestParser:
    def __init__(self, config_path: Path | str = "config.yaml") -> None:
        self.config_path = Path(config_path)

    def parse(self, manifest_path: Path | str) -> dict[str, Any]:
        manifest_path = Path(manifest_path)
        logger.info("Parsing manifest: %s", manifest_path)
        data: dict[str, Any] = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        self._validate_schema(data)
        self._validate_seeds(data)
        self._validate_ids_unique(data)
        logger.info(
            "Manifest valid — %d destination(s): %s",
            len(data["destinations"]),
            ", ".join(d["name"] for d in data["destinations"]),
        )
        return data

    def load(self, manifest_path: Path | str) -> dict[str, Any]:
        """Backward-compatible alias used by CLI/tests."""
        return self.parse(manifest_path)

    def _validate_schema(self, data: dict[str, Any]) -> None:
        jsonschema.validate(instance=data, schema=MANIFEST_SCHEMA)

    def _validate_seeds(self, data: dict[str, Any]) -> None:
        for dest in data.get("destinations", []):
            for seed in dest.get("seeds", []):
                if seed.startswith(("http://", "https://")):
                    raise ValueError(
                        f"Destination '{dest['id']}': seed '{seed}' must be a "
                        "name only — not a URL. The generator discovers all URLs automatically."
                    )

    def _validate_ids_unique(self, data: dict[str, Any]) -> None:
        ids = [d["id"] for d in data.get("destinations", [])]
        seen = set()
        for did in ids:
            if did in seen:
                raise ValueError(f"duplicate destination id: '{did}'")
            seen.add(did)
