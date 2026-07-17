"""Compatibility parser module.

Use this module as the canonical parser entry point.
"""
from __future__ import annotations

from generator.manifest_parser import ManifestParser, MANIFEST_SCHEMA

__all__ = ["ManifestParser", "MANIFEST_SCHEMA"]
