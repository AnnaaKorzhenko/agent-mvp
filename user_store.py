
import os
from datetime import datetime, timezone

from bson import ObjectId
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise SystemExit("Не задано MONGO_URI — додайте його у файл .env")

db = AsyncIOMotorClient(MONGO_URI, tz_aware=True)["News"]
users_collection = db["users"]
conversations = db["conversations"]
digests_collection = db["digests"]
posts_collection = db["posts"]
news_sessions = db["news_sessions"]   # хто на якій новині зараз (одна сесія на людину, _id = user_id)

CHOICE_POINTS = 1
LIKE_POINTS = 1        # 👍
DISLIKE_POINTS = -1    # 👎
SKIP_POINTS = 0        # ⏭ пропустити (записується, але балів не дає)
FEEDBACK_KEEP = 1000   # скільки останніх реакцій тримати в документі
TEXT_SNIPPET = 300
MAX_MESSAGES = 200     # після стількох повідомлень розмова закривається
MAX_GOALS = 5

POINTS = {"like": LIKE_POINTS, "dislike": DISLIKE_POINTS, "skip": SKIP_POINTS}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key(tag: str) -> str:

    return str(tag).replace(".", "·").lstrip("$").strip()


async def ensure_indexes() -> None:
    await users_collection.create_index([("feedback.post_id", 1)])
    await conversations.create_index([("user_id", 1), ("status", 1), ("updated_at", -1)])



async def add_category_choice(user_id: int, selected: dict) -> None:
    """Людина обрала категорії: кожній +1."""
    inc = {f"interests.category_scores.{side}.{cat}": CHOICE_POINTS
           for side in ("ua", "ru") for cat in selected.get(side, [])}
    if inc:
        await users_collection.update_one({"_id": user_id}, {"$inc": inc})


def category_scores(user: dict) -> dict:
    return (user.get("interests") or {}).get("category_scores") or {}


def top_tags(user: dict, limit: int = 10) -> list[dict]:
    tags = (user.get("interests") or {}).get("tag_scores") or {}
    return [{"tag": t, "score": v} for t, v in sorted(tags.items(), key=lambda x: -x[1]) if v > 0][:limit]


def category_order(user: dict, side: str, first: list[str] | None = None) -> list[str]:
    """Порядок категорій сторони: спершу first (сьогоднішній вибір), далі решта за балами."""
    scores = category_scores(user).get(side) or {}
    first = list(first or [])
    rest = sorted((c for c in scores if c not in first), key=lambda c: -scores[c])
    first.sort(key=lambda c: -scores.get(c, 0))
    return first + rest


# ---------------------------------------------------------------------------
# Реакції на новини
# ---------------------------------------------------------------------------

def _points_inc(entry: dict, sign: int) -> dict:
    points = POINTS.get(entry.get("reaction"), 0) * sign
    if not points:
        return {}
    inc = {}
    if entry.get("side") in ("ua", "ru") and entry.get("category"):
        inc[f"interests.category_scores.{entry['side']}.{entry['category']}"] = points
    for tag in entry.get("tags", []):
        if _key(tag):
            k = f"interests.tag_scores.{_key(tag)}"
            inc[k] = inc.get(k, 0) + points
    return inc


async def get_reaction(user_id: int, digest_id, item_n: int) -> str | None:
    user = await users_collection.find_one(
        {"_id": user_id}, {"feedback": {"$elemMatch": {"digest_id": digest_id, "item_n": item_n}}})
    prev = next((f for f in (user or {}).get("feedback", [])
                 if f.get("digest_id") == digest_id and f.get("item_n") == item_n), None)
    return (prev or {}).get("reaction")


async def set_reaction(user_id: int, item: dict, reaction: str | None) -> None:
    """Зберігає реакцію (like / dislike / skip) на новину або знімає її (None).

    item: digest_id, n, side, category, source_post_ids, text.
    Попередня реакція на ту саму новину замінюється, її бали відкочуються.
    """
    user = await users_collection.find_one(
        {"_id": user_id}, {"feedback": {"$elemMatch": {"digest_id": item["digest_id"], "item_n": item["n"]}}})
    prev = next((f for f in (user or {}).get("feedback", [])
                 if f.get("digest_id") == item["digest_id"] and f.get("item_n") == item["n"]), None)
    if (prev or {}).get("reaction") == reaction:
        return

    inc: dict = {}
    if prev:
        await users_collection.update_one(
            {"_id": user_id}, {"$pull": {"feedback": {"digest_id": item["digest_id"], "item_n": item["n"]}}})
        for k, v in _points_inc(prev, -1).items():
            inc[k] = inc.get(k, 0) + v

    update: dict = {}
    if reaction:
        source_ids = list(item.get("source_post_ids") or [])
        tags: list[str] = []
        if source_ids:
            async for p in posts_collection.find({"_id": {"$in": source_ids}}, {"tags": 1}):
                tags.extend(t for t in p.get("tags", []) if t not in tags)
        entry = {"post_id": source_ids[0] if source_ids else None, "reaction": reaction,
                 "text": "", "digest_id": item["digest_id"],   # text — коментар людини, див. set_comment()
                 "item_n": item["n"], "side": item.get("side"), "category": item.get("category"),
                 "tags": tags[:10], "at": _now()}
        for k, v in _points_inc(entry, +1).items():
            inc[k] = inc.get(k, 0) + v
        update["$push"] = {"feedback": {"$each": [entry], "$slice": -FEEDBACK_KEEP}}

    inc = {k: v for k, v in inc.items() if v}
    if inc:
        update["$inc"] = inc
    if update:
        await users_collection.update_one({"_id": user_id}, update)


async def set_comment(user_id: int, digest_id, item_n: int, fields: dict) -> None:
    """Оновлює поля коментаря (text, text_at, comment_status) у записі feedback цієї новини."""
    await users_collection.update_one(
        {"_id": user_id}, {"$set": {f"feedback.$[f].{k}": v for k, v in fields.items()}},
        array_filters=[{"f.digest_id": digest_id, "f.item_n": item_n}])


async def feedback_report(limit: int = 50) -> list[dict]:
    """Для внутрішньої аналітики: новини з найбільшою кількістю 👎 / 👍 / ⏭ серед усіх користувачів."""
    count = lambda r: {"$sum": {"$cond": [{"$eq": ["$feedback.reaction", r]}, 1, 0]}}  # noqa: E731
    pipeline = [
        {"$unwind": "$feedback"},
        {"$group": {"_id": {"digest_id": "$feedback.digest_id", "item_n": "$feedback.item_n"},
                    "text": {"$first": "$feedback.text"}, "category": {"$first": "$feedback.category"},
                    "post_id": {"$first": "$feedback.post_id"},
                    "likes": count("like"), "dislikes": count("dislike"), "skips": count("skip")}},
        {"$sort": {"dislikes": -1, "skips": -1, "likes": 1}},
        {"$limit": limit},
    ]
    return await users_collection.aggregate(pipeline).to_list(None)


# ---------------------------------------------------------------------------
# Цілі
# ---------------------------------------------------------------------------

async def add_goal(user_id: int, text: str, source: str = "user") -> bool:
    text = " ".join(text.split())[:300]
    if not text:
        return False
    user = await users_collection.find_one({"_id": user_id}, {"goals": 1}) or {}
    active = [g for g in user.get("goals", []) if g.get("active")]
    if len(active) >= MAX_GOALS or any(g["text"].lower() == text.lower() for g in active):
        return False
    await users_collection.update_one({"_id": user_id}, {"$push": {"goals": {
        "text": text, "active": True, "source": source, "created_at": _now()}}})
    return True


async def remove_goal(user_id: int, index: int) -> None:
    """Деактивує ціль за номером серед активних (1, 2, …). Історія цілей лишається."""
    user = await users_collection.find_one({"_id": user_id}, {"goals": 1}) or {}
    active_idx = [i for i, g in enumerate(user.get("goals", [])) if g.get("active")]
    if 1 <= index <= len(active_idx):
        i = active_idx[index - 1]
        await users_collection.update_one({"_id": user_id}, {"$set": {
            f"goals.{i}.active": False, f"goals.{i}.removed_at": _now()}})


def active_goals(user: dict) -> list[str]:
    return [g["text"] for g in user.get("goals", []) if g.get("active")]



async def llm_user_profile(user_id: int, category_labels: dict | None = None, examples: int = 8) -> str:
    """Короткий опис людини для системного промпту аналітика."""
    user = await users_collection.find_one({"_id": user_id}) or {}
    labels = {k: v for side in (category_labels or {}).values() for k, v in side.items()}
    name = lambda c: labels.get(c, c)  # noqa: E731

    lines = []
    goals = active_goals(user)
    if goals:
        lines.append("Його цілі (що він найбільше хоче зробити):\n" + "\n".join(f"  • {g}" for g in goals))
    scores = category_scores(user)
    positive = [c for side in ("ua", "ru") for c in category_order(user, side)
                if (scores.get(side) or {}).get(c, 0) > 0]
    if positive:
        lines.append("Найцікавіші категорії: " + ", ".join(name(c) for c in positive))
    negative = [c for side in scores.values() for c, v in side.items() if v < 0]
    if negative:
        lines.append("Менш цікаво: " + ", ".join(name(c) for c in negative))
    tags = top_tags(user)
    if tags:
        lines.append("Теми, що цікавлять: " + ", ".join(t["tag"] for t in tags))

    fb = sorted(user.get("feedback", []), key=lambda f: f["at"], reverse=True)
    picked = {r: [f for f in fb if f["reaction"] == r][:examples] for r in ("like", "dislike")}
    news = await _feedback_news_texts(picked["like"] + picked["dislike"])
    for r, mark, title in (("like", "+", "Новини, які він оцінив як корисні:"),
                           ("dislike", "-", "Новини, які він оцінив як нецікаві:")):
        rows = []
        for f in picked[r]:
            text = news.get((f.get("digest_id"), f.get("item_n")))
            if not text:
                continue
            row = f"  {mark} {text[:TEXT_SNIPPET]}"
            if f.get("text"):
                row += f"\n    Його коментар: «{f['text'][:TEXT_SNIPPET]}»"
            rows.append(row)
        if rows:
            lines.append(title + "\n" + "\n".join(rows))
    return "\n".join(lines)


async def _feedback_news_texts(entries: list[dict]) -> dict:
    """Текст новин для записів feedback: summary поста за post_id; якщо пост уже видалено — текст пункту зведення."""
    texts = {}
    post_ids = [f["post_id"] for f in entries if f.get("post_id")]
    summaries = {p["_id"]: p.get("summary") for p in
                 await posts_collection.find({"_id": {"$in": post_ids}}, {"summary": 1}).to_list(None)}
    missing = []
    for f in entries:
        key = (f.get("digest_id"), f.get("item_n"))
        if summaries.get(f.get("post_id")):
            texts[key] = summaries[f["post_id"]]
        else:
            missing.append(key)
    if missing:
        async for d in digests_collection.find({"_id": {"$in": list({k[0] for k in missing})}}, {"sections.items": 1}):
            c = 0
            for s in d.get("sections", []):
                for it in s.get("items", []):
                    c += 1
                    key = (d["_id"], it.get("n", c))
                    if key in missing:
                        texts[key] = it.get("text")
    return texts


async def get_or_start_conversation(user_id: int, title: str | None = None) -> dict:
    conv = await conversations.find_one({"user_id": user_id, "status": "active"},
                                        {"messages": 0}, sort=[("updated_at", -1)])
    if conv and conv.get("message_count", 0) < MAX_MESSAGES:
        return conv
    if conv:
        await conversations.update_one({"_id": conv["_id"]}, {"$set": {"status": "closed", "updated_at": _now()}})
    now = _now()
    doc = {"user_id": user_id, "title": (title or "")[:100], "status": "active",
           "created_at": now, "updated_at": now, "message_count": 0, "summary": None, "messages": []}
    doc["_id"] = (await conversations.insert_one(doc)).inserted_id
    return doc


async def close_conversation(user_id: int) -> None:
    await conversations.update_many({"user_id": user_id, "status": "active"},
                                    {"$set": {"status": "closed", "updated_at": _now()}})


async def add_message(conversation_id, user_id: int, role: str, content: str, *,
                      sources: list[dict] | None = None, tool_calls: list[dict] | None = None,
                      model: str | None = None, tokens: dict | None = None):
    now = _now()
    msg = {"id": ObjectId(), "role": role, "content": content, "created_at": now}
    if role == "assistant":
        msg.update(sources=sources or [], tool_calls=tool_calls or [], model=model, tokens=tokens, reaction=None)
    await conversations.update_one({"_id": conversation_id}, {
        "$push": {"messages": msg}, "$inc": {"message_count": 1}, "$set": {"updated_at": now}})
    return msg["id"]


async def conversation_history(conversation_id, limit: int = 10) -> list[dict]:
    doc = await conversations.find_one({"_id": conversation_id}, {"messages": {"$slice": -limit}}) or {}
    history = []
    for m in doc.get("messages", [])[-limit:]:
        content = m["content"]
        if m["role"] == "assistant" and m.get("sources"):
            urls = ", ".join(s.get("url", "") for s in m["sources"][:30] if s.get("url"))
            content += f"\n\n[Джерела цієї відповіді: {urls}]"
        history.append({"role": m["role"], "content": content})
    return history


async def set_message_reaction(conversation_id, message_id, user_id: int, reaction: str | None) -> None:
    await conversations.update_one({"_id": conversation_id, "user_id": user_id, "messages.id": message_id},
                                   {"$set": {"messages.$.reaction": reaction}})
