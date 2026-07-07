"""Provider-agnostic text embeddings for semantic memory.

Optional and INERT by default. If no ``embeddings:`` block is configured (in the
engagement profile, or via ``SALIENT_EMBED_*`` env), :func:`get_embedder` returns
``None`` and every caller falls back to today's exact behaviour — substring KG
queries, recency-ranked episodic recall, no semantic injection.

One OpenAI-compatible ``POST {base_url}/v1/embeddings`` covers Ollama / LiteLLM /
OpenAI / Voyage — the same shape as the per-agent ``endpoint:`` blocks. Embedding
NEVER raises into callers: any transport/parse failure yields ``None`` so memory
features degrade rather than break an agent's work.

The embedder is async (every caller runs in the daemon's event loop). Vector math
(:func:`cosine`, :func:`top_k`) and BLOB (de)serialization are sync pure helpers
so SQLite-side code (kg.py) needs no embedder dependency — it stores/loads BLOBs
and ranks pre-embedded vectors; the async ``embed`` happens in the caller.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass
from typing import Any

import httpx

_ENVVAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_VEC_VERSION = 1  # 1-byte header on serialized blobs (lets the format evolve)


def _expand_env(value: str) -> str:
    """Resolve ${VAR} against os.environ; unresolved refs stay literal."""
    if not isinstance(value, str) or "$" not in value:
        return value
    return _ENVVAR_PATTERN.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)


@dataclass(frozen=True)
class EmbeddingConfig:
    model: str
    base_url: str
    api_key: str = ""
    timeout: float = 30.0

    @property
    def cache_key(self) -> tuple[str, str, str]:
        return (self.base_url, self.model, self.api_key)


def resolve_config(profile: dict[str, Any] | None) -> EmbeddingConfig | None:
    """Resolve embeddings config from the engagement profile, falling back to
    ``SALIENT_EMBED_*`` env. Returns ``None`` (inert) unless both a base_url and
    model are present and ``enabled`` is not explicitly false. Mirrors the
    ``kg.default_ttl_days`` profile precedent in bus/_kg.py."""
    block = (profile or {}).get("embeddings") if isinstance(profile, dict) else None
    block = block or {}
    if block.get("enabled") is False:
        return None
    base_url = _expand_env(
        str(block.get("base_url") or os.environ.get("SALIENT_EMBED_BASE_URL") or "")
    ).strip()
    model = _expand_env(
        str(block.get("model") or os.environ.get("SALIENT_EMBED_MODEL") or "")
    ).strip()
    api_key = _expand_env(
        str(block.get("api_key") or os.environ.get("SALIENT_EMBED_API_KEY") or "")
    ).strip()
    if not base_url or not model:
        return None
    try:
        timeout = float(block.get("timeout") or 30.0)
    except (TypeError, ValueError):
        timeout = 30.0
    return EmbeddingConfig(
        model=model, base_url=base_url.rstrip("/"), api_key=api_key, timeout=timeout
    )


class Embedder:
    """Async text embedder over an OpenAI-compatible endpoint. Per-text cache so
    repeated inject-time queries don't re-POST. Failures return None."""

    def __init__(self, cfg: EmbeddingConfig, *, cache_cap: int = 512) -> None:
        self.cfg = cfg
        self.model = cfg.model
        self._cache: dict[str, list[float]] = {}
        self._cache_cap = cache_cap

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Embed a batch, aligned to ``texts``. Returns ``None`` on any failure
        (the caller then falls back to non-semantic behaviour)."""
        if not texts:
            return []
        result: dict[str, list[float]] = {}
        misses = [t for t in texts if t not in self._cache]
        if misses:
            uniq = list(dict.fromkeys(misses))  # dedupe, preserve order
            fetched = await self._post(uniq)
            if fetched is None:
                return None
            for t, v in zip(uniq, fetched, strict=False):
                self._remember(t, v)
        for t in texts:
            cached = self._cache.get(t)
            if cached is None:
                return None
            result[t] = cached
        return [result[t] for t in texts]

    async def embed_one(self, text: str) -> list[float] | None:
        out = await self.embed([text])
        return out[0] if out else None

    def _remember(self, text: str, vec: list[float]) -> None:
        if len(self._cache) >= self._cache_cap:
            for k in list(self._cache)[: max(1, self._cache_cap // 8)]:
                self._cache.pop(k, None)
        self._cache[text] = vec

    async def _post(self, inputs: list[str]) -> list[list[float]] | None:
        url = f"{self.cfg.base_url}/v1/embeddings"
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.cfg.timeout, connect=10.0)
            ) as client:
                resp = await client.post(
                    url,
                    json={"model": self.cfg.model, "input": inputs},
                    headers=headers,
                )
            if resp.status_code != 200:
                return None
            data = resp.json().get("data")
            if not isinstance(data, list) or len(data) != len(inputs):
                return None
            vecs: list[list[float]] = []
            for row in data:
                emb = row.get("embedding") if isinstance(row, dict) else None
                if not isinstance(emb, list) or not emb:
                    return None
                vecs.append([float(x) for x in emb])
            return vecs
        except Exception:  # noqa: BLE001 — embeddings must never raise into callers
            return None


# ── module-level embedder cache (one per resolved config) ─────────────
_EMBEDDER_CACHE: dict[tuple[str, str, str], Embedder] = {}


def get_embedder(profile: dict[str, Any] | None) -> Embedder | None:
    """The daemon's embedder for the current profile, or None (inert). Cached so
    repeated resolution is cheap; clear via :func:`clear_embedder_cache` on reload."""
    cfg = resolve_config(profile)
    if cfg is None:
        return None
    cached = _EMBEDDER_CACHE.get(cfg.cache_key)
    if cached is None:
        cached = Embedder(cfg)
        _EMBEDDER_CACHE[cfg.cache_key] = cached
    return cached


def clear_embedder_cache() -> None:
    _EMBEDDER_CACHE.clear()


# ── vector serialization (SQLite BLOB) ────────────────────────────────
def pack_vector(vec: list[float]) -> bytes:
    """[version byte][little-endian float32 * n]. Compact, numpy-free."""
    return bytes([_VEC_VERSION]) + struct.pack(f"<{len(vec)}f", *vec)


def unpack_vector(blob: bytes | None) -> list[float] | None:
    if not blob or len(blob) < 5 or blob[0] != _VEC_VERSION:
        return None
    n = (len(blob) - 1) // 4
    if n <= 0:
        return None
    return list(struct.unpack(f"<{n}f", blob[1 : 1 + n * 4]))


# ── cosine + top-k (pure-python, optional numpy fast-path) ────────────
try:  # numpy stays an undeclared, best-effort accelerator
    import numpy as _np

    _HAVE_NUMPY = True
except Exception:  # noqa: BLE001
    _np = None
    _HAVE_NUMPY = False


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    if _HAVE_NUMPY:
        va = _np.asarray(a, dtype=_np.float32)
        vb = _np.asarray(b, dtype=_np.float32)
        na = float(_np.linalg.norm(va))
        nb = float(_np.linalg.norm(vb))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return float(va.dot(vb) / (na * nb))
    dot = na = nb = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(dot / ((na**0.5) * (nb**0.5)))


def top_k(
    query: list[float],
    candidates: list[tuple[Any, list[float]]],
    k: int,
    min_score: float = 0.0,
) -> list[tuple[Any, float]]:
    """Rank ``[(payload, vec)]`` by cosine to ``query``; return ``[(payload,
    score)]`` desc, keeping only score >= min_score, capped at k."""
    scored = [(payload, cosine(query, vec)) for payload, vec in candidates if vec]
    scored = [(p, s) for p, s in scored if s >= min_score]
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:k]
