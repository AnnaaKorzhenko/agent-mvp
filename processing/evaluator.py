# processing/evaluator.py
import json
import logging
import asyncio
from datetime import datetime
from groq import AsyncGroq
from config import config
from scrapers.db import posts_collection

logger = logging.getLogger(__name__)

groq_client = AsyncGroq(api_key=config.GROQ_API_KEY)

BATCH_EVAL_PROMPT = """
You are an executive OSINT evaluator for a Ukrainian DefTech R&D Director.
Analyze the provided post summaries and evaluate each item's strategic relevance.

Intel Pillars:
1. ADVERSARY_TECH: Russian drone mods, robotics, EW, battlefield improvisations.
2. ADVERSARY_SENTIMENT_ECONOMY: Russian domestic economic strain, sentiment, trade, sanctions impact.
3. DOMESTIC_TECH: Ukrainian MilTech innovations, robotics, field achievements.
4. GLOBAL_INTEL: World defense technology, international sanctions, foreign military aid.
5. NOISE: Pure propaganda, non-technical chatter, low-value commentary.

Respond STRICTLY with a valid JSON array containing evaluation objects for each post matching this exact schema:
[
  {
    "post_id": "<id_from_input>",
    "relevance_score": <integer 0-10>,
    "priority": "<HIGH|MEDIUM|LOW>",
    "intel_pillar": "<ADVERSARY_TECH|ADVERSARY_SENTIMENT_ECONOMY|DOMESTIC_TECH|GLOBAL_INTEL|NOISE>",
    "subject_country": "<RU|UA|US|IN|CN|GLOBAL>",
    "analysis_direction": "<e.g., UA source analyzing RU oil | RU source analyzing internal economy>"
  }
]
"""

async def evaluate_unprocessed_posts(batch_size: int = 10):
    unprocessed_posts = await posts_collection.find({
        "processed": False,
        "summary": {"$nin": [None, ""]}
    }).limit(batch_size).to_list(length=batch_size)

    if not unprocessed_posts:
        logger.info("No unprocessed posts with summaries found for Groq evaluation.")
        return 0

    logger.info(f"Evaluating {len(unprocessed_posts)} summarized posts via Groq API...")

    prepared_batch = []
    post_map = {}

    for post in unprocessed_posts:
        post_id_str = str(post["_id"])
        summary_text = post.get("summary", "").strip()

        if not summary_text or len(summary_text) < 10:
            await posts_collection.update_one(
                {"_id": post["_id"]},
                {"$set": {"processed": True, "intel_pillar": "NOISE", "relevance_score": 0}}
            )
            continue

        prepared_batch.append({
            "post_id": post_id_str,
            "summary": summary_text
        })
        post_map[post_id_str] = post

    if not prepared_batch:
        return 0

    try:
        prompt_content = f"Evaluate these post summaries:\n{json.dumps(prepared_batch, ensure_ascii=False)}"

        response = await groq_client.chat.completions.create(
            model=config.GROQ_MODEL,
            messages=[
                {"role": "system", "content": BATCH_EVAL_PROMPT},
                {"role": "user", "content": prompt_content}
            ],
            temperature=0.1
        )

        response_text = response.choices[0].message.content.strip()
        raw_json = json.loads(response_text)
        eval_results = raw_json if isinstance(raw_json, list) else raw_json.get("evaluations", raw_json.get("data", []))

        updated_count = 0
        for res in eval_results:
            pid = res.get("post_id")
            if pid in post_map:
                # Force integer conversion for relevance score
                try:
                    score = int(res.get("relevance_score", 0))
                except (ValueError, TypeError):
                    score = 0

                await posts_collection.update_one(
                    {"_id": post_map[pid]["_id"]},
                    {"$set": {
                        "processed": True,
                        "relevance_score": score,
                        "priority": str(res.get("priority", "LOW")).upper(),
                        "intel_pillar": res.get("intel_pillar", "NOISE"),
                        "subject_country": res.get("subject_country", "GLOBAL"),
                        "analysis_direction": res.get("analysis_direction", ""),
                        "evaluated_at": datetime.utcnow().isoformat()
                    }}
                )
                updated_count += 1

        logger.info(f"✓ Groq evaluation complete: {updated_count} posts scored and categorized.")
        return updated_count

    except Exception as e:
        logger.error(f"Error evaluating batch with Groq API: {e}")
        return 0