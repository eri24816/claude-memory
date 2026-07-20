"""Embedding via FastEmbed.

ONNX on CPU, deliberately independent of any torch environment so the memory
system never competes with an ML project's dependencies.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Iterable, Sequence

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384


@lru_cache(maxsize=1)
def _model():
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=MODEL_NAME)


def encode(texts: Sequence[str]) -> list[list[float]]:
    """Embed a batch of texts. The model downloads on first use."""
    if not texts:
        return []
    return [vector.tolist() for vector in _model().embed(list(texts))]


def encode_one(text: str) -> list[float]:
    return encode([text])[0]


def serialize(vector: Iterable[float]) -> bytes:
    """Pack a vector into the compact form vec0 stores."""
    import sqlite_vec

    return sqlite_vec.serialize_float32(list(vector))
