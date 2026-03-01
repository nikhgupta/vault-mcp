"""OpenAI batch embedding with retry/backoff."""

from __future__ import annotations

import time

import openai

MODEL = "text-embedding-3-small"
DIMS = 1536
MAX_BATCH_SIZE = 2048  # OpenAI limit per request

_client: openai.OpenAI | None = None


def _get_client() -> openai.OpenAI:
    global _client
    if _client is None:
        _client = openai.OpenAI()
    return _client


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts, handling batching and retries.

    Returns list of float vectors in same order as input.
    """
    if not texts:
        return []

    client = _get_client()
    all_embeddings: list[list[float]] = []

    for i in range(0, len(texts), MAX_BATCH_SIZE):
        batch = texts[i : i + MAX_BATCH_SIZE]
        embeddings = _embed_batch_with_retry(client, batch)
        all_embeddings.extend(embeddings)

    return all_embeddings


def embed_query(text: str) -> list[float]:
    """Embed a single query string."""
    return embed_texts([text])[0]


def _embed_batch_with_retry(
    client: openai.OpenAI,
    texts: list[str],
    max_retries: int = 3,
) -> list[list[float]]:
    """Embed a batch with exponential backoff on rate limit errors."""
    for attempt in range(max_retries):
        try:
            response = client.embeddings.create(model=MODEL, input=texts)
            # Sort by index to preserve order
            sorted_data = sorted(response.data, key=lambda x: x.index)
            return [item.embedding for item in sorted_data]
        except openai.RateLimitError:
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                time.sleep(wait)
            else:
                raise
        except openai.APIError:
            if attempt < max_retries - 1:
                time.sleep(1)
            else:
                raise
