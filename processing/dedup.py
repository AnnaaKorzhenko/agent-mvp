import logging
import numpy as np
from fastembed import TextEmbedding
from config import get_db

logger = logging.getLogger(__name__)

_embedding_model = None

def get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    return _embedding_model

def compute_embedding(text: str) -> list[float]:
    model = get_embedding_model()
    embeddings = list(model.embed([text]))
    return embeddings[0].tolist()

def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    a = np.array(v1)
    b = np.array(v2)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))

async def is_duplicate(text_vector: list[float], threshold: float = 0.88) -> bool:
    db = get_db()
    # Query recent processed documents containing embeddings
    recent_docs = await db.raw_posts.find(
        {"vector": {"$exists": True}},
        {"vector": 1}
    ).sort("created_at", -1).limit(200).to_list(length=200)

    for doc in recent_docs:
        stored_vec = doc.get("vector")
        if stored_vec and cosine_similarity(text_vector, stored_vec) >= threshold:
            return True

    return False