import asyncio
import logging

import torch
from pymongo import UpdateOne
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

from scrapers.cleaning import normalize_for_model
from scrapers.db import posts_collection

logger = logging.getLogger(__name__)

MODEL_NAME = "csebuetnlp/mT5_multilingual_XLSum"
if torch.cuda.is_available():
    DEVICE = "cuda"
elif torch.backends.mps.is_available():
    DEVICE = "mps"
else:
    DEVICE = "cpu"

_tokenizer = None
_model = None


def load_model():
    """Завантажує модель один раз і повторно використовує її."""
    global _tokenizer, _model
    if _model is None:
        logger.info(f"Завантаження моделі {MODEL_NAME} на {DEVICE}...")
        _tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
        _model = AutoModelForSeq2SeqLM.from_pretrained(MODEL_NAME).to(DEVICE).eval()
    return _tokenizer, _model


def summarize_texts(texts: list[str], max_new_tokens: int = 84) -> list[str]:

    tokenizer, model = load_model()
    inputs = tokenizer(
        [normalize_for_model(t) for t in texts],
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=512,
    ).to(DEVICE)

    with torch.no_grad():
        out = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            num_beams=4,
            no_repeat_ngram_size=2,
        )
    return [s.strip() for s in tokenizer.batch_decode(out, skip_special_tokens=True)]


async def generate_missing_summaries(batch_size: int = 4) -> None:

    query = {"summary": None, "raw_text": {"$nin": [None, ""]}}
    posts = await posts_collection.find(query, {"raw_text": 1}).to_list(length=None)

    if not posts:
        logger.info("Усі пости вже мають самарі")
        return

    logger.info(f"Знайдено постів без самарі: {len(posts)}")

    for i in range(0, len(posts), batch_size):
        batch = posts[i:i + batch_size]
        texts = [p["raw_text"] for p in batch]


        summaries = await asyncio.to_thread(summarize_texts, texts)

        await posts_collection.bulk_write([
            UpdateOne({"_id": p["_id"]}, {"$set": {"summary": s}})
            for p, s in zip(batch, summaries)
        ])
        logger.info(f"Оброблено {min(i + batch_size, len(posts))}/{len(posts)}")
