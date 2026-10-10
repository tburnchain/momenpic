"""Embedding adapters: Voyage (production, 1 024-d — matches ``vector(1024)`` in §6.1) and the
dependency-free hashing embedder (dev/tests/offline)."""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from acvm.config import EmbeddingConfig
from acvm.core.errors import UpstreamError
from acvm.ports import SecretStore
from acvm.quant.similarity import HashingEmbedder


class HashingEmbedderAdapter:
    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim
        self._impl = HashingEmbedder(dim)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self._impl.embed_many(list(texts))


class VoyageEmbedder:
    URL = "https://api.voyageai.com/v1/embeddings"

    def __init__(self, cfg: EmbeddingConfig, secrets: SecretStore, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self.dim = cfg.dim
        self.secrets = secrets
        self.client = client or httpx.AsyncClient(timeout=30)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        key = await self.secrets.get(self.cfg.api_key_ref)
        out: list[list[float]] = []
        for i in range(0, len(texts), 128):
            batch = list(texts[i : i + 128])
            resp = await self.client.post(self.URL, headers={"Authorization": f"Bearer {key}"}, json={
                "model": self.cfg.model, "input": batch, "input_type": "document",
                "output_dimension": self.dim})
            if resp.status_code >= 400:
                raise UpstreamError(f"voyage {resp.status_code}", status=resp.status_code,
                                    retryable=resp.status_code >= 500 or resp.status_code == 429)
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            out.extend([list(map(float, d["embedding"])) for d in data])
        return out


def build_embedder(cfg: EmbeddingConfig, secrets: SecretStore) -> HashingEmbedderAdapter | VoyageEmbedder:
    return VoyageEmbedder(cfg, secrets) if cfg.backend == "voyage" else HashingEmbedderAdapter(cfg.dim)
