"""
llm_client.py — Multi-provider LLM routing with usage and cost tracking.

Supported providers:
  - openai
  - anthropic
  - deepseek (OpenAI-compatible)
  - gemini
  - grok (OpenAI-compatible)
  - azure_openai (legacy compatibility)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from openai import AzureOpenAI, OpenAI
from generator.llm.router import LLMRouter


DEFAULT_PRICING_USD_PER_1M: dict[str, dict[str, float]] = {
    "openai:gpt-4o": {"input": 5.00, "output": 15.00},
    "openai:gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "deepseek:deepseek-chat": {"input": 0.27, "output": 1.10},
    "deepseek:deepseek-reasoner": {"input": 0.55, "output": 2.19},
    "grok:grok-2-latest": {"input": 2.00, "output": 10.00},
    "anthropic:claude-3-5-sonnet-latest": {"input": 3.00, "output": 15.00},
    "anthropic:claude-3-7-sonnet-latest": {"input": 3.00, "output": 15.00},
    "gemini:gemini-1.5-pro": {"input": 3.50, "output": 10.50},
    "gemini:gemini-1.5-flash": {"input": 0.35, "output": 1.05},
}


@dataclass
class UsageRecord:
    provider: str
    model: str
    operation: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float


class UsageTracker:
    def __init__(self, pricing_map: dict[str, dict[str, float]] | None = None) -> None:
        self._pricing = pricing_map or DEFAULT_PRICING_USD_PER_1M
        self._records: list[UsageRecord] = []

    def add(
        self,
        provider: str,
        model: str,
        operation: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        total_tokens = int(prompt_tokens) + int(completion_tokens)
        estimated = self._estimate_cost(provider, model, int(prompt_tokens), int(completion_tokens))
        self._records.append(
            UsageRecord(
                provider=provider,
                model=model,
                operation=operation,
                prompt_tokens=int(prompt_tokens),
                completion_tokens=int(completion_tokens),
                total_tokens=total_tokens,
                estimated_cost_usd=estimated,
            )
        )

    def _estimate_cost(self, provider: str, model: str, in_tokens: int, out_tokens: int) -> float:
        key = f"{provider}:{model}"
        prices = self._pricing.get(key)
        if not prices:
            return 0.0
        in_cost = (in_tokens / 1_000_000) * prices.get("input", 0.0)
        out_cost = (out_tokens / 1_000_000) * prices.get("output", 0.0)
        return round(in_cost + out_cost, 6)

    def summary(self) -> dict[str, Any]:
        by_model: dict[str, dict[str, Any]] = {}
        for rec in self._records:
            key = f"{rec.provider}:{rec.model}"
            bucket = by_model.setdefault(
                key,
                {
                    "provider": rec.provider,
                    "model": rec.model,
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "estimated_cost_usd": 0.0,
                },
            )
            bucket["calls"] += 1
            bucket["prompt_tokens"] += rec.prompt_tokens
            bucket["completion_tokens"] += rec.completion_tokens
            bucket["total_tokens"] += rec.total_tokens
            bucket["estimated_cost_usd"] = round(bucket["estimated_cost_usd"] + rec.estimated_cost_usd, 6)

        rows = sorted(by_model.values(), key=lambda x: x["estimated_cost_usd"], reverse=True)
        total = round(sum(x["estimated_cost_usd"] for x in rows), 6)
        return {
            "models": rows,
            "total_calls": len(self._records),
            "total_estimated_cost_usd": total,
            "records": [
                {
                    "provider": r.provider,
                    "model": r.model,
                    "operation": r.operation,
                    "prompt_tokens": r.prompt_tokens,
                    "completion_tokens": r.completion_tokens,
                    "total_tokens": r.total_tokens,
                    "estimated_cost_usd": r.estimated_cost_usd,
                }
                for r in self._records
            ],
        }


class MultiLLMClient:
    def __init__(
        self,
        config_path: str | Path = "config.yaml",
        llm_overrides: dict[str, Any] | None = None,
    ) -> None:
        import yaml

        with Path(config_path).open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        ai_cfg = cfg.get("ai", {})
        legacy_cfg = cfg.get("azure_openai", {})
        llm_cfg = llm_overrides or {}

        self.provider = (llm_cfg.get("provider") or ai_cfg.get("provider") or "azure_openai").lower()
        self.model = (
            llm_cfg.get("model")
            or ai_cfg.get("model")
            or os.environ.get("OPENAI_MODEL")
            or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
            or "gpt-4o"
        )
        self.temperature = float(llm_cfg.get("temperature", ai_cfg.get("temperature", legacy_cfg.get("temperature", 0.7))))
        self.max_tokens = int(llm_cfg.get("max_tokens", ai_cfg.get("max_tokens", legacy_cfg.get("max_tokens", 4096))))

        self.usage_tracker = UsageTracker()
        self._router = LLMRouter()
        self._custom_provider = self._router.get_provider(self.provider, model=self.model)

        if self._custom_provider is not None:
            self.model = getattr(self._custom_provider, "model", self.model)
            return

        if self.provider == "openai":
            self._openai_client = OpenAI(
                api_key=os.environ["OPENAI_API_KEY"],
                base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            )
        elif self.provider == "deepseek":
            self._openai_client = OpenAI(
                api_key=os.environ["DEEPSEEK_API_KEY"],
                base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            )
        elif self.provider == "azure_openai":
            self._azure_client = AzureOpenAI(
                azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
                api_key=os.environ["AZURE_OPENAI_API_KEY"],
                api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-02-01"),
            )
            self.model = os.environ.get("AZURE_OPENAI_DEPLOYMENT", self.model)
        elif self.provider not in {"anthropic", "gemini", "grok"}:
            raise ValueError(f"Unsupported LLM provider: {self.provider}")
        elif self.provider == "grok":
            from generator.providers.grok import GrokClient
            self._client = GrokClient(os.environ["GROK_API_KEY"], self.model)


    def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        operation: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        temp = self.temperature if temperature is None else temperature
        tok = self.max_tokens if max_tokens is None else max_tokens

        if self._custom_provider is not None:
            text, usage = self._call_custom_provider(system_prompt, user_prompt, temp, tok)
            used_model = usage.get("model", self.model)
        elif self.provider in {"openai", "deepseek"}:
            text, usage = self._call_openai(system_prompt, user_prompt, temp, tok)
            used_model = usage.get("model", self.model)
        elif self.provider == "azure_openai":
            text, usage = self._call_azure(system_prompt, user_prompt, temp, tok)
            used_model = usage.get("model", self.model)
        elif self.provider == "anthropic":
            text, usage = self._call_anthropic(system_prompt, user_prompt, temp, tok)
            used_model = usage.get("model", self.model)
        else:
            text, usage = self._call_gemini(system_prompt, user_prompt, temp, tok)
            used_model = usage.get("model", self.model)

        self.usage_tracker.add(
            provider=self.provider,
            model=used_model,
            operation=operation,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )
        return _extract_json_object(text)

    def usage_summary(self) -> dict[str, Any]:
        return self.usage_tracker.summary()

    def _call_custom_provider(self, system_prompt: str, user_prompt: str, temperature: float, max_tokens: int) -> tuple[str, dict[str, Any]]:
        return self._custom_provider.create_json_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def _call_openai(self, system_prompt: str, user_prompt: str, temperature: float, max_tokens: int) -> tuple[str, dict[str, Any]]:
        resp = self._openai_client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
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

    def _call_azure(self, system_prompt: str, user_prompt: str, temperature: float, max_tokens: int) -> tuple[str, dict[str, Any]]:
        resp = self._azure_client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
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

    def _call_anthropic(self, system_prompt: str, user_prompt: str, temperature: float, max_tokens: int) -> tuple[str, dict[str, Any]]:
        headers = {
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": os.environ.get("ANTHROPIC_API_VERSION", "2023-06-01"),
            "content-type": "application/json",
        }
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        resp = requests.post(
            os.environ.get("ANTHROPIC_API_BASE", "https://api.anthropic.com/v1/messages"),
            headers=headers,
            json=body,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        text_chunks = [c.get("text", "") for c in data.get("content", []) if c.get("type") == "text"]
        usage = data.get("usage", {})
        return (
            "\n".join(text_chunks),
            {
                "prompt_tokens": int(usage.get("input_tokens", 0) or 0),
                "completion_tokens": int(usage.get("output_tokens", 0) or 0),
                "model": data.get("model", self.model),
            },
        )

    def _call_gemini(self, system_prompt: str, user_prompt: str, temperature: float, max_tokens: int) -> tuple[str, dict[str, Any]]:
        api_key = os.environ["GEMINI_API_KEY"]
        base = os.environ.get("GEMINI_API_BASE", "https://generativelanguage.googleapis.com")
        url = f"{base}/v1beta/models/{self.model}:generateContent?key={api_key}"
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
            },
        }
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()

        text = ""
        for cand in data.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                if "text" in part:
                    text += part["text"]

        usage = data.get("usageMetadata", {})
        return (
            text,
            {
                "prompt_tokens": int(usage.get("promptTokenCount", 0) or 0),
                "completion_tokens": int(usage.get("candidatesTokenCount", 0) or 0),
                "model": self.model,
            },
        )


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("LLM returned empty response")

    if raw.startswith("```"):
        lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("```")]
        raw = "\n".join(lines).strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    if start == -1:
        raise ValueError("LLM response does not contain a JSON object")

    depth = 0
    in_str = False
    esc = False
    for idx in range(start, len(raw)):
        ch = raw[idx]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(raw[start:idx + 1])

    raise ValueError("Unable to extract a complete JSON object from LLM response")
