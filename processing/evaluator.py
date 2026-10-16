import json
import logging
from datetime import datetime
from google import genai
from google.genai import types
from config import config, get_db
from processing.dedup import compute_embedding, is_duplicate

logger = logging.getLogger(__name__)

# Initialize Gemini Client
gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)

SYSTEM_EVAL_PROMPT = """
You are an executive defense technology intelligence evaluator. Your job is to analyze incoming raw news or social media text and assess its technical relevance for a Defense Tech R&D Director.

R&D Strategic Focus Areas:
- High Priority: Electronic Warfare (EW), frequency hopping (433/900MHz), FPV anti-jamming, optical fiber control, thermal sensors, drone flight controllers, component supply bottlenecks.
- Medium Priority: Unmanned ground/naval vehicles, field improvisations, battery advances, software guidance.
- Low Priority / Noise: Front-line tactical summaries, general political commentary, troop movements, propaganda, non-technical news.

You MUST respond strictly in valid JSON format matching this schema:
{
    "relevance_score": <integer from 0 to 10>,
    "priority": "<HIGH|MEDIUM|LOW>",
    "domain": "<Electronic Warfare|Drone Hardware|Sensors|Supply Chain|General/Noise>",
    "summary": "<1-2 sentence technical summary>",
    "relevance_reason": "<Short justification for the score>"
}
"""

async def evaluate_unprocessed_posts():
    db = get_db()
    unprocessed_posts = await db.raw_posts.find({"processed": False}).limit(50).to_list(length=50)

    if not unprocessed_posts:
        logger.info("No unprocessed posts found.")
        return

    logger.info(f"Processing {len(unprocessed_posts)} posts...")

    for post in unprocessed_posts:
        raw_text = post.get("raw_text", "").strip()
        if not raw_text or len(raw_text) < 20:
            await db.raw_posts.update_one(
                {"_id": post["_id"]},
                {"$set": {"processed": True, "is_noise": True, "relevance_score": 0}}
            )
            continue

        # 1. Compute Embedding & Check Deduplication
        text_vector = compute_embedding(raw_text[:1000])
        if await is_duplicate(text_vector):
            await db.raw_posts.update_one(
                {"_id": post["_id"]},
                {"$set": {"processed": True, "is_duplicate": True, "relevance_score": 0, "vector": text_vector}}
            )
            continue

        # 2. Evaluate with Gemini Flash Model (using Async API)
        try:
            response = await gemini_client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=f"Post Text:\n{raw_text}",
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_EVAL_PROMPT,
                    response_mime_type="application/json",
                    temperature=0.1
                )
            )

            raw_response = response.text.strip()
            eval_data = json.loads(raw_response)

            await db.raw_posts.update_one(
                {"_id": post["_id"]},
                {"$set": {
                    "processed": True,
                    "vector": text_vector,
                    "relevance_score": eval_data.get("relevance_score", 0),
                    "priority": eval_data.get("priority", "LOW"),
                    "domain": eval_data.get("domain", "General/Noise"),
                    "summary": eval_data.get("summary", ""),
                    "relevance_reason": eval_data.get("relevance_reason", ""),
                    "evaluated_at": datetime.utcnow().isoformat()
                }}
            )

        except Exception as e:
            logger.error(f"Error evaluating post {post.get('_id')}: {e}")