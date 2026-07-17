"""Route provider names to provider-specific clients."""
from __future__ import annotations

from typing import Any

from generator.providers.grok import GrokProvider


class LLMRouter:
    def get_provider(self, provider_name: str, model: str | None = None) -> Any:
        provider = (provider_name or "").lower()
        if provider == "grok":
            return GrokProvider(model=model)
        return None
