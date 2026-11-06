"""Hybrid semantic search (brain.domain.semantic) and its use in posts.search. Voyage and the Atlas stages are faked:
test databases have no vector or text index."""
import copy
import itertools

import pytest
from bson import ObjectId

from brain.domain import posts, semantic, sensitivity
from brain.domain.identity import UserContext
from tests.conftest import make_settings
from tests.fixtures.sample_data import P_DRONES, P_GRANTS, P_SECRET, P_TOP, USERS

_ids = itertools.count(915_000_001)


def _settings(**overrides):
    values = {"VOYAGE_API_KEY": "test-key", "SEARCH_DIVERSITY": "false", "KEYWORD_QUOTA": "0", **overrides}
    return make_settings(**values)


def _vec(i: int) -> list[float]:
    """Orthogonal unit vectors: no two hits are duplicates unless they share one."""
    v = [0.0] * 8
    v[i] = 1.0
    return v


def _hit(post_id, summary: str, vec: int, url: str | None = None) -> dict:
    return {"_id": ObjectId(), "post_id": str(post_id), "summary": summary, "body": "",
            "embedding": _vec(vec), "post_url": url or f"https://t.me/x/{post_id}"}


@pytest.fixture(autouse=True)
def fresh_caches():
    for cache in (semantic._EMBEDS, semantic._SCORES, semantic._RESULTS):
        cache.data.clear()
    yield
    for cache in (semantic._EMBEDS, semantic._SCORES, semantic._RESULTS):
        cache.data.clear()


@pytest.fixture
def fake(monkeypatch):
    """Switches semantic search on and fakes Voyage and both Atlas stages; tests fill `vector`, `text`, `scores`."""
    state = {"settings": _settings(), "vector": [], "text": [], "scores": {}, "calls": [], "vector_error": None}

    async def voyage(s, url, body, model, tokens):
        state["calls"].append(model)
        if url == semantic.EMBED_URL:
            return {"data": [{"embedding": _vec(0)}]}
        scores = [next((v for k, v in state["scores"].items() if k in doc), 0.0) for doc in body["documents"]]
        return {"data": [{"index": i, "relevance_score": sc} for i, sc in enumerate(scores)]}

    async def vector_hits(s, vec, since, side, category, pool):
        state["calls"].append("vector")
        if state["vector_error"]:
            raise state["vector_error"]
        return copy.deepcopy(state["vector"])

    async def text_hits(s, query, since, side, category, pool):
        state["calls"].append("text")
        return copy.deepcopy(state["text"])

    monkeypatch.setattr(semantic, "get_settings", lambda: state["settings"])
    monkeypatch.setattr(semantic, "_voyage", voyage)
    monkeypatch.setattr(semantic, "_vector_hits", vector_hits)
    monkeypatch.setattr(semantic, "_text_hits", text_hits)
    return state


# ---- semantic.search -----------------------------------------------------------

async def test_both_lists_merged(fake):
    fake["settings"] = _settings(RERANK_LITE_MODEL="", RERANK_MODEL="")
    a, b, c = (_hit(ObjectId(), f"post {n}", n) for n in (1, 2, 3))
    fake["vector"], fake["text"] = [a, b], [b, c]
    found = await semantic.search("дрони")
    assert [h.post_id for h in found][0] == b["post_id"]           # in both lists: first
    assert {h.post_id for h in found} == {a["post_id"], b["post_id"], c["post_id"]}
    assert "vector" in fake["calls"] and "text" in fake["calls"]


async def test_text_index_missing_leaves_vectors(fake, monkeypatch):
    fake["settings"] = _settings(RERANK_LITE_MODEL="", RERANK_MODEL="")
    a = _hit(ObjectId(), "post a", 1)
    fake["vector"] = [a]

    async def broken(*args):
        raise RuntimeError("no text index")
    monkeypatch.setattr(semantic, "_text_hits", broken)
    assert [h.post_id for h in await semantic.search("дрони")] == [a["post_id"]]


async def test_rerank_order_and_min_rerank_cut(fake):
    a, b, c = _hit(ObjectId(), "about alpha", 1), _hit(ObjectId(), "about bravo", 2), _hit(ObjectId(), "about charlie", 3)
    fake["vector"], fake["text"] = [a, b, c], []
    fake["scores"] = {"alpha": 0.3, "bravo": 0.9, "charlie": 0.6}
    found = await semantic.search("дрони")
    assert [h.post_id for h in found] == [b["post_id"], c["post_id"]]    # alpha is below MIN_RERANK (0.5)
    assert [h.score for h in found] == [0.9, 0.6]
    assert fake["calls"].count("rerank-3-lite") == 1 and fake["calls"].count("rerank-3") == 1

    semantic._RESULTS.data.clear()
    fake["settings"] = _settings(MIN_RERANK="0.2")
    assert [h.post_id for h in await semantic.search("дрони")] == [b["post_id"], c["post_id"], a["post_id"]]


async def test_duplicates_glued_into_also(fake):
    fake["settings"] = _settings(RERANK_LITE_MODEL="", RERANK_MODEL="")
    first = _hit(ObjectId(), "one event", 1, "https://t.me/a/1")
    twin = _hit(ObjectId(), "one event, other source", 1, "https://t.me/b/2")
    other = _hit(ObjectId(), "another event", 2)
    fake["vector"] = [first, twin, other]
    found = await semantic.search("дрони")
    assert [h.post_id for h in found] == [first["post_id"], other["post_id"]]
    assert found[0].also == ["https://t.me/b/2"] and found[1].also == []


async def test_off_raises_unavailable(fake):
    fake["settings"] = _settings(VOYAGE_API_KEY="")
    with pytest.raises(semantic.SemanticUnavailable):
        await semantic.search("дрони")


# ---- posts.search ----------------------------------------------------------------

@pytest.fixture
async def user(test_db):
    doc = copy.deepcopy(USERS[0])
    doc["_id"] = next(_ids)
    await test_db.users.insert_one(doc)
    return doc["_id"]


def cloud(uid):
    return UserContext(uid, "cloud", "claude", sensitivity.max_for("cloud"))


async def test_posts_search_uses_semantic_hits(repos, user, fake):
    fake["vector"] = [_hit(P_GRANTS, "Brave1 гранти", 1), _hit(P_DRONES, "морські дрони", 2)]
    fake["scores"] = {"Brave1": 0.95, "морські": 0.7}
    views, _ = await posts.search(repos, cloud(user), "щось без ключових слів")
    assert [v.post.id for v in views] == [str(P_GRANTS), str(P_DRONES)]
    assert all(v.channel for v in views)


async def test_posts_search_falls_back_when_unavailable(repos, user, fake):
    fake["vector_error"] = RuntimeError("no vector index")
    views, words = await posts.search(repos, cloud(user), "дрони РЕБ", days=7)
    assert "vector" in fake["calls"]
    assert str(P_DRONES) in [v.post.id for v in views] and words == posts.query_words("дрони РЕБ")


async def test_posts_search_falls_back_on_no_hits(repos, user, fake):
    views, _ = await posts.search(repos, cloud(user), "дрони РЕБ", days=7)
    assert "vector" in fake["calls"]
    assert {str(P_DRONES), str(P_GRANTS)} <= {v.post.id for v in views}


async def test_posts_search_never_returns_a_post_above_the_label(repos, user, fake):
    fake["vector"] = [_hit(P_TOP, "звіт РЕБ", 1), _hit(P_SECRET, "оцінка дронів", 2), _hit(P_DRONES, "морські дрони", 3)]
    fake["scores"] = {"звіт": 0.99, "оцінка": 0.98, "морські": 0.6}
    views, _ = await posts.search(repos, cloud(user), "дрони")
    ids = [v.post.id for v in views]
    assert str(P_DRONES) in ids
    assert not {str(P_TOP), str(P_SECRET)} & set(ids)

    fake["vector"] = [_hit(P_TOP, "звіт РЕБ", 1)]                    # only hidden ones: keyword fallback, still hidden
    fake["scores"] = {"звіт": 0.99}
    views, _ = await posts.search(repos, cloud(user), "звіт РЕБ")
    assert str(P_TOP) not in [v.post.id for v in views]
