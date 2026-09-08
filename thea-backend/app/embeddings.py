"""
Embedding generation for Stage 5 (episodic/semantic memory) and Stage 1/3's
retrieval lookups.

The schema fixes `vector(1024)` for both `episodic_memory.request_embedding`
and `semantic_memory.pattern_embedding`, so whatever model is used here MUST
emit 1024-dim vectors. Default below (`mixedbread-ai/mxbai-embed-large-v1`)
is a real 1024-dim model available via OpenRouter-compatible /embeddings
providers; swap in .env-driven config if a different provider is chosen —
just keep the dimension fixed at 1024 or migrate the column.
"""

from __future__ import annotations

import httpx

from app.config import get_settings

EMBEDDING_DIM = 1024
_EMBEDDING_MODEL = "mixedbread-ai/mxbai-embed-large-v1"


async def embed_text(text: str) -> list[float]:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.openrouter_base_url}/embeddings",
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
            json={"model": _EMBEDDING_MODEL, "input": text},
        )
    resp.raise_for_status()
    data = resp.json()
    vector = data["data"][0]["embedding"]
    if len(vector) != EMBEDDING_DIM:
        raise ValueError(
            f"Embedding model {_EMBEDDING_MODEL} returned dim {len(vector)}, "
            f"expected {EMBEDDING_DIM} to match the schema's vector(1024) columns."
        )
    return vector
