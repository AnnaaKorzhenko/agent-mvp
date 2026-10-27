"""Embedding backend interface for semantic search (phase 5).

Nothing produces embeddings yet. The model is to be agreed with the local-model track
(docs/REQUESTS_TO_LOCAL_MODEL_TRACK.md #2). Requirements: multilingual (Ukrainian and Russian), runs
locally. Embeddings of private data never go to an external API.
TODO(next): a fastembed-based LocalEmbeddingBackend, or the teammate's function through local_models.py.
"""
from __future__ import annotations

from typing import Protocol


class EmbeddingBackend(Protocol):
    model: str
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbeddingsNotConfigured(Exception):
    pass


class NoEmbeddings:
    """Placeholder until a model is agreed: search falls back to pattern matching."""
    model = "none"
    dimensions = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingsNotConfigured("No embedding backend configured yet (phase 5)")
