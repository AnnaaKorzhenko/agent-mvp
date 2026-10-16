# processing/evaluator.py
import json
import logging
import asyncio
from datetime import datetime
from google import genai
from google.genai import types
from google.genai.errors import APIError
from config import config, get_db
from processing.dedup import compute_embedding, is_duplicate

logger = logging.getLogger(__name__)
gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)

SYSTEM_EVAL_PROMPT = """
You are an executive OSINT & DefTech evaluator for a Ukrainian Defense Tech R&D Director.
Analyze incoming posts and extract core intelligence, identifying BOTH the source background AND the target subject being analyzed.

Core Intelligence Pillars:
1. ADVERSARY_TECH: Russian inventions, battlefield improvisations, robotics, EW/FPV modifications.
2. ADVERSARY_SENTIMENT_ECONOMY: What Russians think/write, domestic sentiment, economy, trade, sanctions.
3. DOMESTIC_TECH: Ukrainian MilTech innovations, robotics, field achievements, new deployment.
4. GLOBAL_INTEL: World defense technology, international sanctions, global oil/trade dynamics, foreign aid.
5. NOISE: Pure propaganda or non-technical chatter.

You MUST respond strictly in valid JSON format matching this schema:
{
    "relevance_score": <integer from 0 to 10>,
    "priority": "<HIGH|MEDIUM|LOW>",
    "intel_pillar": "<ADVERSARY_TECH|ADVERSARY_SENTIMENT_ECONOMY|DOMESTIC_TECH|GLOBAL_INTEL|NOISE>",
    "subject_country": "<RU|UA|US|IN|CN|GLOBAL|MULTIPLE>",
    "analysis_direction": "<e.g., UA source analyzing RU oil sales to India | RU source analyzing internal RU economy | US analyzing UA battlefield>",
    "domain": "<Robotics|Electronic Warfare|Drone Hardware|Economic Vulnerability|Public Sentiment|Foreign Trade|Other>",
    "summary": "<1-2 sentence concise intelligence summary in Ukrainian>",
    "relevance_reason": "<Short justification>"
}
"""

async def evaluate_unprocessed_posts(limit: int = 5):
    db = get_db()
    unprocessed_posts = await db.raw_posts.find({"processed": False}).limit(limit).to_list(length=limit)

    if not unprocessed_posts:
        logger.info("No unprocessed posts found.")
        return

    logger.info(f"Evaluating {len(unprocessed_posts)} unprocessed posts...")

    for post in unprocessed_posts:
        raw_text = post.get("raw_text", "").strip()

        source_doc = await db.sources.find_one({"_id": post.get("source_id")})
        source_country = source_doc.get("source_country", "UNKNOWN") if source_doc else "UNKNOWN"
        username = source_doc.get("username", "UNKNOWN") if source_doc else "UNKNOWN"
        category = post.get("category") or (source_doc.get("category") if source_doc else "General")

        if not raw_text or len(raw_text) < 20:
            await db.raw_posts.update_one(
                {"_id": post["_id"]},
                {"$set": {"processed": True, "intel_pillar": "NOISE", "relevance_score": 0}}
            )
            continue

        text_vector = compute_embedding(raw_text[:1000])
        if await is_duplicate(text_vector):
            await db.raw_posts.update_one(
                {"_id": post["_id"]},
                {"$set": {"processed": True, "is_duplicate": True, "relevance_score": 0, "vector": text_vector}}
            )
            continue

        # Retry loop for Gemini API calls to handle 429 rate limits
        max_retries = 3
        for attempt in range(max_retries):
            try:
                prompt_content = (
                    f"Source Channel: @{username}\n"
                    f"Source Origin Country: {source_country}\n"
                    f"Source Category: {category}\n"
                    f"Post Text:\n{raw_text}"
                )

                response = await gemini_client.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt_content,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_EVAL_PROMPT,
                        response_mime_type="application/json",
                        temperature=0.1
                    )
                )

                eval_data = json.loads(response.text.strip())

                await db.raw_posts.update_one(
                    {"_id": post["_id"]},
                    {"$set": {
                        "processed": True,
                        "vector": text_vector,
                        "relevance_score": eval_data.get("relevance_score", 0),
                        "priority": eval_data.get("priority", "LOW"),
                        "intel_pillar": eval_data.get("intel_pillar", "NOISE"),
                        "subject_country": eval_data.get("subject_country", "GLOBAL"),
                        "analysis_direction": eval_data.get("analysis_direction", ""),
                        "domain": eval_data.get("domain", "Other"),
                        "summary": eval_data.get("summary", ""),
                        "relevance_reason": eval_data.get("relevance_reason", ""),
                        "evaluated_at": datetime.utcnow().isoformat()
                    }}
                )
                
                # Pace requests to comply with Gemini Free Tier rate limits (5 RPM = 12s delay)
                await asyncio.sleep(12)
                break

            except APIError as e:
                if e.code == 429 or "RESOURCE_EXHAUSTED" in str(e):
                    logger.warning(f"Rate limit hit. Retrying in 45 seconds... (Attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(45)
                else:
                    logger.error(f"Gemini API Error for post {post.get('_id')}: {e}")
                    break
            except Exception as e:
                logger.error(f"Error evaluating post {post.get('_id')}: {e}")
                break