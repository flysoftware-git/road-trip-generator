"""Grok provider via OpenAI-compatible API.

This provider always enables code execution by sending:
  tools=[{"type": "code_interpreter"}]
"""
from __future__ import annotations

import os
from typing import Any

from openai import OpenAI


class GrokProvider:
    def __init__(self, model: str | None = None) -> None:
        self._client = OpenAI(
            api_key=os.environ["GROK_API_KEY"],
            base_url=os.environ.get("GROK_BASE_URL", "https://api.x.ai/v1"),
        )
        self.model = model or os.environ.get("GROK_MODEL", "grok-2-latest")

    def create_json_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> tuple[str, dict[str, Any]]:
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            tools=[{"type": "code_interpreter"}],
            response_format={"type": "json_object"},
        )
        usage = resp.usage
        return (
            resp.choices[0].message.content,
            {
                "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                "model": getattr(resp, "model", self.model),
            },
        )
