"""Semantic + keyword (hybrid) search over the pipeline's post embeddings (collection `post_vectors`).

The pipeline (scrapers-3, pipeline/embed.py on Kaggle) embeds every new post's summary with Voyage
(voyage-4-large, 1024 dims) into `post_vectors`, where Atlas has two indexes (create_vector_index.py there):
  post_vectors_idx  vector search on `embedding`
  post_text_idx     Atlas Search on `summary`/`body` (analyzer that folds UA/RU spelling: РЕБ = РЭБ)

One query here:
  1. embed the query (Voyage, input_type=query) → $vectorSearch;  in parallel: $search by words;
  2. merge both lists (reciprocal rank fusion);
  3. rerank stage 1 (rerank-3-lite, up to ~100 candidates, summaries only);
  4. glue duplicates (one event from several sources → one hit with `also` urls);
  5. rerank stage 2 (rerank-3, top ~15 with the post's text), drop scores below MIN_RERANK.
Returns post ids in order; brain.domain.posts loads the posts themselves (sensitivity filter, personal relevance).

Free Voyage accounts (no payment method) get 3 requests and 10,000 tokens per minute per model; the limiter
below spaces requests instead of failing. It works per process: the MCP server and the web app count separately.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx2

from brain.config import Settings, get_settings
from brain.db.client import get_client

logger = logging.getLogger("brain.semantic")

EMBED_URL = "https://api.voyageai.com/v1/embeddings"
RERANK_URL = "https://api.voyageai.com/v1/rerank"
VECTOR_INDEX, TEXT_INDEX = "post_vectors_idx", "post_text_idx"     # as created by the pipeline's script
RRF_K = 60
TOKEN_MARGIN = 0.9


class SemanticUnavailable(Exception):
    """Voyage or the vector index can't answer now: the caller falls back to the keyword search."""


@dataclass
class Hit:
    post_id: str
    score: float                                    # rerank score 0-1 (or fused rank score without rerank)
    also: list[str] = field(default_factory=list)   # post_urls of duplicates of the same event


def enabled(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    return bool(s.semantic_search and s.voyage_api_key)


def _vectors(s: Settings):
    return get_client(s)[s.vectors_db or s.mongo_db]["post_vectors"]


def _est_tokens(text: str) -> int:
    return int(len(text) / 2.5) + 2


# ---------- Voyage with rate limiting ----------

class _Limiter:
    def __init__(self) -> None:
        self.log: dict[str, deque] = {}

    def can(self, model: str, tokens: int, rpm: int, tpm: int) -> bool:
        """Would a request go through right now, without waiting?"""
        cap = int(tpm * TOKEN_MARGIN)
        q = self.log.get(model) or deque()
        now = time.monotonic()
        recent = [(t0, n) for t0, n in q if now - t0 < 60]
        return len(recent) < rpm and sum(n for _, n in recent) + min(tokens, cap) <= cap

    async def acquire(self, model: str, tokens: int, rpm: int, tpm: int) -> list:
        cap = int(tpm * TOKEN_MARGIN)
        tokens = min(tokens, cap)
        q = self.log.setdefault(model, deque())
        while True:
            now = time.monotonic()
            while q and now - q[0][0] >= 60:
                q.popleft()
            if len(q) < rpm and sum(t for _, t in q) + tokens <= cap:
                entry = [now, tokens]
                q.append(entry)
                return entry
            wait = max(1.0, 60 - (now - q[0][0]) + 0.5)
            logger.info(f"Voyage limit ({model}): waiting {wait:.0f}s")
            await asyncio.sleep(wait)


_LIMITER = _Limiter()


async def _voyage(s: Settings, url: str, body: dict, model: str, tokens: int) -> dict:
    key = s.voyage_api_key.get_secret_value() if s.voyage_api_key else ""
    deadline = time.monotonic() + s.semantic_max_wait
    delay, detail = 5, ""
    async with httpx2.AsyncClient(timeout=60) as client:
        while True:
            try:
                entry = await asyncio.wait_for(
                    _LIMITER.acquire(model, tokens, s.voyage_rpm, s.voyage_tpm),
                    timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                raise SemanticUnavailable(f"Voyage limit: no slot within {s.semantic_max_wait}s")
            try:
                r = await client.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
            except httpx2.TransportError as e:
                status, detail = None, str(e)
            else:
                if r.status_code == 200:
                    data = r.json()
                    used = int((data.get("usage") or {}).get("total_tokens", 0))
                    if used:
                        entry[1] = used
                    return data
                status, detail = r.status_code, r.text[:200]
                if status not in (429, 500, 502, 503, 504):
                    raise SemanticUnavailable(f"Voyage {status}: {detail}")
                if status == 429:
                    entry[1] = int(s.voyage_tpm * TOKEN_MARGIN)
            if time.monotonic() + delay > deadline:
                raise SemanticUnavailable(f"Voyage {status or 'network'}: {detail}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


# ---------- caches: the UI repeats a query (filters, days, paging); without a Voyage payment method every call counts ----------
CACHE_TTL = 3600                    # query vectors and rerank scores: 1 hour
RESULT_TTL = 600                    # whole results: 10 minutes (new posts arrive once a day anyway)


class _TTLCache:
    def __init__(self, ttl: int, size: int):
        self.ttl, self.size, self.data = ttl, size, {}

    def get(self, key):
        item = self.data.get(key)
        if item and time.monotonic() - item[0] < self.ttl:
            return item[1]
        self.data.pop(key, None)
        return None

    def put(self, key, value) -> None:
        if len(self.data) >= self.size:                 # drop the oldest tenth
            for k in sorted(self.data, key=lambda k: self.data[k][0])[: self.size // 10 or 1]:
                self.data.pop(k, None)
        self.data[key] = (time.monotonic(), value)


_EMBEDS = _TTLCache(CACHE_TTL, 1000)
_SCORES = _TTLCache(CACHE_TTL, 50_000)
_RESULTS = _TTLCache(RESULT_TTL, 500)
_INFLIGHT: dict[tuple, asyncio.Future] = {}


def _norm(query: str) -> str:
    return " ".join((query or "").lower().split())


async def _embed_query(s: Settings, query: str) -> list[float]:
    key = (s.embed_model, s.embed_dim, _norm(query))
    vec = _EMBEDS.get(key)
    if vec is None:
        body = {"input": [query], "model": s.embed_model, "input_type": "query", "output_dimension": s.embed_dim}
        data = await _voyage(s, EMBED_URL, body, s.embed_model, _est_tokens(query))
        vec = data["data"][0]["embedding"]
        _EMBEDS.put(key, vec)
    return vec


async def _rerank(s: Settings, query: str, hits: list[dict], model: str, text_fn) -> list[dict]:
    """Scores every hit (cached scores reused: only new posts go to Voyage), best first."""
    q = _norm(query)
    todo = []
    for h in hits:
        cached = _SCORES.get((model, q, str(h["_id"])))
        if cached is None:
            todo.append(h)
        else:
            h["rerank"] = cached
    if todo:
        docs = [text_fn(h) for h in todo]
        tokens = len(docs) * _est_tokens(query) + sum(_est_tokens(d) for d in docs)
        if s.rerank_skip_when_limited and not _LIMITER.can(model, tokens, s.voyage_rpm, s.voyage_tpm):
            raise SemanticUnavailable(f"{model}: Voyage limit reached, skipped without waiting")
        data = await _voyage(s, RERANK_URL, {"query": query, "documents": docs, "model": model}, model, tokens)
        for d in data["data"]:
            hit = todo[d["index"]]
            hit["rerank"] = d["relevance_score"]
            _SCORES.put((model, q, str(hit["_id"])), d["relevance_score"])
    return sorted(hits, key=lambda h: h.get("rerank", 0.0), reverse=True)


# ---------- retrieval ----------

def _short(h: dict) -> str:
    return " ".join((h.get("summary") or "").split())


def _full(h: dict) -> str:
    return " ".join(((h.get("summary") or "") + " " + (h.get("body") or "")[:700]).split())


def _fit(s: Settings, query: str, hits: list[dict], text_fn, limit: int, share: float = 1.0) -> list[dict]:
    """As many candidates as fit one rerank request under `share` of the per-minute token limit."""
    budget, q, out = s.voyage_tpm * TOKEN_MARGIN * share, _est_tokens(query), []
    for h in hits[:limit]:
        cost = q + _est_tokens(text_fn(h))
        if out and cost > budget:
            break
        budget -= cost
        out.append(h)
    return out


def _as_list(v) -> list[float]:
    if isinstance(v, list):
        return v
    try:
        return list(v.as_vector().data)
    except AttributeError:
        return []


def _cos(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = sum(x * x for x in a) ** 0.5, sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _dedup(hits: list[dict], threshold: float) -> list[dict]:
    kept: list[dict] = []
    for h in hits:
        vec = _as_list(h.get("embedding"))
        twin = next((k for k in kept if _cos(vec, k["_vec"]) >= threshold), None)
        if twin:
            if h.get("post_url"):
                twin["also"].append(h["post_url"])
        else:
            h["_vec"], h["also"] = vec, []
            kept.append(h)
    return kept


def _diverse(hits: list[dict], limit: int, lam: float) -> list[dict]:
    """Maximal marginal relevance: each next pick maximizes lam*relevance - (1-lam)*similarity to the picks so far.
    Only posts that already passed MIN_RERANK take part, so variety never brings in off-topic ones."""
    pool = [h for h in hits]
    for h in pool:
        h.setdefault("_vec", _as_list(h.get("embedding")))
    rel = lambda h: float(h.get("rerank", h.get("rrf", 0.0)))
    picked = [pool.pop(0)]                      # the most relevant one always first
    while pool and len(picked) < limit:
        best = max(pool, key=lambda h: lam * rel(h) - (1 - lam) * max(_cos(h["_vec"], p["_vec"]) for p in picked))
        pool.remove(best)
        picked.append(best)
    return picked


def _rrf(*lists: list[dict]) -> list[dict]:
    merged: dict = {}
    for hits in lists:
        for rank, h in enumerate(hits, 1):
            cur = merged.setdefault(h["_id"], {**h, "rrf": 0.0})
            cur["rrf"] += 1 / (RRF_K + rank)
    return sorted(merged.values(), key=lambda h: h["rrf"], reverse=True)


async def _vector_hits(s: Settings, vec: list[float], since: datetime | None, side: str | None,
                       category: str | None, pool: int) -> list[dict]:
    flt: dict = {}
    if since:
        flt["published_dt"] = {"$gte": since}
    if side:
        flt["source_side"] = side
    if category:
        flt["category"] = category
    stage = {"index": VECTOR_INDEX, "path": "embedding", "queryVector": vec,
             "numCandidates": max(200, pool * 10), "limit": pool}
    if flt:
        stage["filter"] = flt
    cursor = await _vectors(s).aggregate([{"$vectorSearch": stage}])
    return await cursor.to_list(pool)


async def _text_hits(s: Settings, query: str, since: datetime | None, side: str | None,
                     category: str | None, pool: int) -> list[dict]:
    clauses = []
    if since:
        clauses.append({"range": {"path": "published_dt", "gte": since}})
    if side:
        clauses.append({"equals": {"path": "source_side", "value": side}})
    if category:
        clauses.append({"equals": {"path": "category", "value": category}})
    compound: dict = {"must": [{"text": {"query": query, "path": ["summary", "body"],
                                         "fuzzy": {"maxEdits": 1, "prefixLength": 2}}}]}
    if clauses:
        compound["filter"] = clauses
    cursor = await _vectors(s).aggregate([{"$search": {"index": TEXT_INDEX, "compound": compound}},
                                          {"$limit": pool}])
    return await cursor.to_list(pool)


async def search(query: str, *, side: str | None = None, category: str | None = None, days: int | None = None,
                 limit: int = 10, settings: Settings | None = None) -> list[Hit]:
    """Post ids most relevant to the query, best first. Raises SemanticUnavailable when it can't answer.
    The same query with the same filters within RESULT_TTL is answered from memory, and identical requests
    that arrive together (the UI often sends a few) share one search."""
    s = settings or get_settings()
    if not enabled(s):
        raise SemanticUnavailable("semantic search is off (SEMANTIC_SEARCH / VOYAGE_API_KEY)")
    side = side if side in ("ua", "ru") else None
    key = (_norm(query), side, category or None, days or None, limit)
    cached = _RESULTS.get(key)
    if cached is not None:
        logger.info(f"semantic: «{query[:60]}» from cache")
        return cached
    if key in _INFLIGHT:
        return await asyncio.shield(_INFLIGHT[key])
    fut = asyncio.get_running_loop().create_future()
    _INFLIGHT[key] = fut
    try:
        hits = await _search(s, query, side, category, days, limit)
        _RESULTS.put(key, hits)
        fut.set_result(hits)
        return hits
    except BaseException as e:
        fut.set_exception(e)
        fut.exception()                      # mark retrieved: no "never retrieved" warning when nobody waits
        raise
    finally:
        _INFLIGHT.pop(key, None)


async def _search(s: Settings, query: str, side: str | None, category: str | None, days: int | None,
                  limit: int) -> list[Hit]:
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    pool = max(limit * 4, 40)
    vec = await _embed_query(s, query)
    try:
        vector_hits = await _vector_hits(s, vec, since, side, category, pool)
    except Exception as e:                      # no index yet, wrong dims…
        raise SemanticUnavailable(f"vector search failed: {type(e).__name__}: {e}")
    try:
        text_hits = await _text_hits(s, query, since, side, category, pool)
    except Exception as e:                      # no text index: vectors only
        logger.warning(f"keyword part of the hybrid search failed ({type(e).__name__}): vectors only")
        text_hits = []
    logger.info(f"semantic: {s.vectors_db or s.mongo_db}.post_vectors → vectors {len(vector_hits)}, words {len(text_hits)}")
    hits = _rrf(vector_hits, text_hits)
    if not hits:
        return []
    if text_hits and s.keyword_quota:           # the best keyword hits always reach the reranker
        must = {str(h["_id"]) for h in text_hits[: s.keyword_quota]}
        hits = [h for h in hits if str(h["_id"]) in must] + [h for h in hits if str(h["_id"]) not in must]

    reranked = False
    if s.rerank_lite_model and len(hits) > 1:
        try:
            hits = await _rerank(s, query, _fit(s, query, hits, _short, s.rerank_candidates, s.rerank_lite_share),
                                 s.rerank_lite_model, _short)
            reranked = True
        except SemanticUnavailable as e:
            logger.warning(f"rerank stage 1 skipped: {e}")
    hits = _dedup(hits, s.dedup_sim)
    if s.rerank_model and len(hits) > 1:
        cand = _fit(s, query, hits, _full, max(s.rerank_final, limit), s.rerank_lite_share)
        try:
            hits = await _rerank(s, query, cand, s.rerank_model, _full) + hits[len(cand):]
            reranked = True
        except SemanticUnavailable as e:
            logger.warning(f"rerank stage 2 skipped: {e}")
    if reranked:
        hits = [h for h in hits if h.get("rerank", 1.0) >= s.min_rerank]
    if s.search_diversity and len(hits) > limit:
        hits = _diverse(hits, limit, s.diversity_lambda)
    return [Hit(post_id=str(h.get("post_id") or h["_id"]), score=round(float(h.get("rerank", h.get("rrf", 0.0))), 4),
                also=h.get("also", [])) for h in hits[:limit]]
