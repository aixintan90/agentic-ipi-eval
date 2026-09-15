from __future__ import annotations

import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


class LLMClient(Protocol):
    def json_chat(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.7,
    ) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class LLMMetadata:
    provider: str
    model: str
    base_url: str | None = None
    mock: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "mock": self.mock,
        }


_FENCED_JSON_RE = re.compile(r"^\s*```(?:json|JSON)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _decode_json_content(content: str) -> Any:
    """Decode JSON even when an OpenAI-compatible proxy wraps it in markdown."""
    candidates = [content.strip()]
    fence = _FENCED_JSON_RE.match(content)
    if fence:
        candidates.insert(0, fence.group(1).strip())

    decoder = json.JSONDecoder()
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        for index, char in enumerate(candidate):
            if char not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            return value
    raise json.JSONDecodeError("no JSON object or array found", content, 0)


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str,
        provider: str = "openai_compatible",
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        retry_base_seconds: float = 1.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, int(max_retries))
        self.retry_base_seconds = max(0.0, float(retry_base_seconds))
        self.metadata = LLMMetadata(
            provider=provider,
            model=model,
            base_url=self.base_url,
        )

    @classmethod
    def from_env(cls) -> OpenAICompatibleClient:
        xiaoai_key = os.environ.get("XIAOAI_API_KEY")
        if xiaoai_key:
            return cls(
                api_key=xiaoai_key,
                base_url=(
                    os.environ.get("XIAOAI_BASE_URL")
                    or os.environ.get("LLM_BASE_URL")
                    or "https://xiaoai.plus/v1"
                ),
                model=(
                    os.environ.get("XIAOAI_MODEL")
                    or os.environ.get("DEEPSEEK_MODEL")
                    or os.environ.get("LLM_MODEL")
                    or "deepseek-v4-flash"
                ),
                provider="openai_compatible",
                timeout_seconds=float(os.environ.get("LLM_TIMEOUT_SECONDS", "60")),
                max_retries=int(os.environ.get("LLM_MAX_RETRIES", "2")),
            )
        provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
        deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
        use_deepseek = provider == "deepseek" or (not provider and bool(deepseek_key))
        if use_deepseek:
            api_key = deepseek_key or os.environ.get("LLM_API_KEY")
            if not api_key:
                raise ValueError("missing DEEPSEEK_API_KEY or LLM_API_KEY for DeepSeek")
            model = (
                os.environ.get("DEEPSEEK_MODEL")
                or os.environ.get("LLM_MODEL")
                or "deepseek-v4-pro"
            )
            base_url = (
                os.environ.get("DEEPSEEK_BASE_URL")
                or os.environ.get("LLM_BASE_URL")
                or "https://api.deepseek.com"
            )
            timeout = float(os.environ.get("LLM_TIMEOUT_SECONDS", "60"))
            max_retries = int(os.environ.get("LLM_MAX_RETRIES", "2"))
            return cls(
                api_key=api_key,
                base_url=base_url,
                model=model,
                provider="deepseek",
                timeout_seconds=timeout,
                max_retries=max_retries,
            )

        api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "missing LLM_API_KEY, OPENAI_API_KEY, or DEEPSEEK_API_KEY"
            )
        model = os.environ.get("LLM_MODEL", "gpt-4.1-mini")
        base_url = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
        timeout = float(os.environ.get("LLM_TIMEOUT_SECONDS", "60"))
        max_retries = int(os.environ.get("LLM_MAX_RETRIES", "2"))
        return cls(
            api_key=api_key,
            base_url=base_url,
            model=model,
            provider="openai_compatible",
            timeout_seconds=timeout,
            max_retries=max_retries,
        )

    def json_chat(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.7,
    ) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        raw = self._post_chat_completion(payload)
        payload = json.loads(raw)
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise RuntimeError("LLM API returned non-string content")
        try:
            value = _decode_json_content(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"LLM API did not return JSON: {content[:500]}") from exc
        if isinstance(value, list):
            return {"items": value}
        if not isinstance(value, dict):
            raise RuntimeError("LLM API JSON root must be an object or array")
        return value

    def _post_chat_completion(self, payload: dict[str, Any]) -> str:
        last_error: RuntimeError | None = None
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as response:
                    return response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                last_error = RuntimeError(f"LLM API HTTP {exc.code}: {body}")
                if exc.code not in {429, 500, 502, 503, 504}:
                    raise last_error from exc
            except urllib.error.URLError as exc:
                last_error = RuntimeError(f"LLM API connection error: {exc.reason}")
            except (http.client.IncompleteRead, TimeoutError, ConnectionError, OSError) as exc:
                last_error = RuntimeError(
                    f"LLM API transport error: {type(exc).__name__}: {exc}"
                )

            if attempt >= self.max_retries:
                break
            delay = min(self.retry_base_seconds * (2**attempt), 8.0)
            if delay:
                time.sleep(delay)
        assert last_error is not None
        raise last_error


class FailoverLLMClient:
    def __init__(self, clients: list[OpenAICompatibleClient]) -> None:
        if not clients:
            raise ValueError("at least one LLM client is required")
        self.clients = clients
        self._local = threading.local()

    @property
    def metadata(self) -> LLMMetadata:
        return getattr(self._local, "metadata", self.clients[0].metadata)

    def json_chat(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.7,
    ) -> dict[str, Any]:
        errors: list[str] = []
        for client in self.clients:
            try:
                result = client.json_chat(
                    system=system,
                    user=user,
                    temperature=temperature,
                )
            except RuntimeError as exc:
                errors.append(f"{client.model}: {exc}")
                continue
            self._local.metadata = client.metadata
            return result
        detail = " | ".join(errors)
        raise RuntimeError(f"LLM failover exhausted all configured models: {detail}")


class MockLLMClient:
    def __init__(self) -> None:
        self.metadata = LLMMetadata(provider="mock", model="mock-mutator", mock=True)

    def json_chat(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.7,
    ) -> dict[str, Any]:
        del system, temperature
        if '"task": "score_prompts"' in user:
            payload = json.loads(user)
            return {
                "scores": [
                    {
                        "node_id": item["node_id"],
                        "realism": 4,
                        "semantic_alignment": 4,
                        "benign_surface": 5,
                        "over_directness": 1,
                        "complexity": 2,
                        "keep": True,
                        "reason": "mock score",
                    }
                    for item in payload.get("prompts", [])
                ]
            }
        if '"task": "generate_root_nodes"' in user:
            payload = json.loads(user)
            return {
                "roots": [
                    {
                        "chain_id": chain["chain_id"],
                        "tool_response_on": chain["tool_response_on"],
                        "tool_response_off": chain["tool_response_off"],
                        "rationale": "mock root keeps configured fixture",
                    }
                    for chain in payload.get("chains", [])
                ]
            }
        payload = json.loads(user)
        count = int(payload.get("count", 1))
        p_type = str(payload.get("p_type", "P1"))
        offset = len(payload.get("exclude_prompts") or [])
        return {
            "prompts": [
                {
                    "p_type": p_type,
                    "prompt": (
                        f"Review the current project record {offset + index + 1}."
                    ),
                    "rationale": f"mock {p_type} candidate {index + 1}",
                }
                for index in range(count)
            ]
        }
