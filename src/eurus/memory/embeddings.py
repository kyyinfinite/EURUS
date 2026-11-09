from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, Protocol

from eurus.config import Config, EmbeddingConfig

log = logging.getLogger(__name__)

__all__ = [
    "EmbeddingProvider",
    "HttpEmbeddingProvider",
    "NullEmbeddingProvider",
    "build_embedding_provider",
]


class EmbeddingProvider(ABC):
    @property
    @abstractmethod
    def available(self) -> bool:
        raise NotImplementedError

    @property
    @abstractmethod
    def dimension(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> list[Sequence[float]]:
        raise NotImplementedError

    def similarity(self, left: Sequence[float], right: Sequence[float]) -> float:
        if len(left) != len(right) or not left:
            return 0.0
        dot = sum(a * b for a, b in zip(left, right, strict=True))
        norm_left = sum(a * a for a in left) ** 0.5
        norm_right = sum(b * b for b in right) ** 0.5
        if norm_left <= 0.0 or norm_right <= 0.0:
            return 0.0
        return float(dot / (norm_left * norm_right))


class NullEmbeddingProvider(EmbeddingProvider):
    @property
    def available(self) -> bool:
        return False

    @property
    def dimension(self) -> int:
        return 0

    def embed(self, texts: Sequence[str]) -> list[Sequence[float]]:
        raise RuntimeError("no embedding provider is configured")


class _HttpTransport(Protocol):
    def __call__(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]: ...


class HttpEmbeddingProvider(EmbeddingProvider):
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str,
        dimension: int = 0,
        timeout_seconds: float = 10.0,
        transport: _HttpTransport | None = None,
    ) -> None:
        if not endpoint.strip().startswith(("http://", "https://")):
            raise ValueError("embedding endpoint must be an http(s) URL")
        if not model.strip():
            raise ValueError("embedding model is required")
        if not api_key.strip():
            raise ValueError("embedding api_key is required")
        if not 1.0 <= timeout_seconds <= 120.0:
            raise ValueError("timeout_seconds must be between 1 and 120")
        self.endpoint = endpoint.strip()
        self.model = model.strip()
        self.api_key = api_key.strip()
        self._dimension = int(dimension)
        self.timeout_seconds = timeout_seconds
        self._transport = transport

    @property
    def available(self) -> bool:
        return True

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: Sequence[str]) -> list[Sequence[float]]:
        cleaned = [str(text)[:8000] for text in texts]
        if not cleaned:
            return []
        transport = self._transport
        if transport is None:
            import httpx

            class _SyncHttpxTransport:
                def __init__(self, timeout: float) -> None:
                    self.timeout = timeout

                def __call__(
                    self, url: str, *, json: dict[str, Any], headers: dict[str, str]
                ) -> dict[str, Any]:
                    response = httpx.post(url, json=json, headers=headers, timeout=self.timeout)
                    response.raise_for_status()
                    result: dict[str, Any] = response.json()
                    return result

            transport = _SyncHttpxTransport(self.timeout_seconds)
        payload = transport(
            self.endpoint,
            json={"input": cleaned, "model": self.model},
            headers={"authorization": f"Bearer {self.api_key}"},
        )
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(cleaned):
            raise ValueError("embedding response shape mismatch")
        vectors: list[Sequence[float]] = []
        for entry in sorted(data, key=lambda item: int(item.get("index", 0))):
            embedding = entry.get("embedding")
            if not isinstance(embedding, list) or not embedding:
                raise ValueError("embedding response missing vector")
            vectors.append([float(value) for value in embedding])
        if self._dimension and vectors and len(vectors[0]) != self._dimension:
            raise ValueError("embedding dimension mismatch")
        return vectors


def build_embedding_provider(config: Config | None) -> EmbeddingProvider:
    settings = _embedding_settings(config)
    if settings is None:
        return NullEmbeddingProvider()
    provider = settings.provider.strip().lower()
    if provider in {"", "none", "null"}:
        return NullEmbeddingProvider()
    if provider == "http":
        if not settings.endpoint or not settings.model or not settings.api_key_env:
            log.warning(
                "http embedding provider requires endpoint, model and api_key_env; "
                "falling back to lexical retrieval"
            )
            return NullEmbeddingProvider()
        import os

        api_key = os.environ.get(settings.api_key_env, "").strip()
        if not api_key:
            log.warning(
                "embedding api key %s is not set; falling back to lexical retrieval",
                settings.api_key_env,
            )
            return NullEmbeddingProvider()
        return HttpEmbeddingProvider(
            endpoint=settings.endpoint,
            model=settings.model,
            api_key=api_key,
            dimension=settings.dimension,
        )
    log.warning("unknown embedding provider %r; falling back to lexical retrieval", provider)
    return NullEmbeddingProvider()


def _embedding_settings(config: Config | None) -> EmbeddingConfig | None:
    if config is None:
        return None
    memory = getattr(config, "memory", None)
    if memory is None:
        return None
    return getattr(memory, "embedding", None)
