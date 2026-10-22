
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

import user_store as store
from user_store import digests_collection, posts_collection, users_collection

logger = logging.getLogger("analyst")

client = AsyncOpenAI(
    base_url=os.getenv("ASSISTANT_LLM_BASE_URL") or os.getenv("LLM_BASE_URL") or "http://localhost:11434/v1",
    api_key=os.getenv("ASSISTANT_LLM_API_KEY") or os.getenv("LLM_API_KEY") or "local",
)
MODEL = os.getenv("ASSISTANT_LLM_MODEL") or os.getenv("LLM_MODEL") or "qwen2.5:7b-instruct"
ORG_CONTEXT = os.getenv("ORG_CONTEXT") or (
    "Українська команда в оборонному секторі. Цікавлять: інновації та технології на фронті (дрони, РЕБ, ППО, "
    "робототехніка), інвестиції й фінансування оборонних компаній, оборонна промисловість, держзакупівлі, "
    "ситуація на фронті, а також що противник каже про Україну і які в нього новинки."
)

MAX_TOOL_ROUNDS = 4

# Ліміт вихідних токенів — фіксований на кожен виклик. У reasoning-моделей (gpt-5*, o*) він спільний
# для міркувань і тексту: з 4000 міркування інколи забирали все, і відповідь виходила порожньою.
MAX_OUTPUT_TOKENS = int(os.getenv("ASSISTANT_MAX_OUTPUT_TOKENS", "8000"))            # звичайна розмова
MAX_OUTPUT_TOKENS_FEED = int(os.getenv("ASSISTANT_MAX_OUTPUT_TOKENS_FEED", "5000"))  # питання про поточну новину
REASONING_EFFORT = os.getenv("ASSISTANT_REASONING_EFFORT", "low")
HISTORY_TURNS = int(os.getenv("ASSISTANT_HISTORY_TURNS", "10"))   # останні N пар «питання — відповідь»
RETRY_HISTORY_MESSAGES = 4

# Новіші моделі OpenAI (gpt-5*, o*) не приймають temperature і max_tokens (замість нього —
# max_completion_tokens), зате приймають reasoning_effort. Підбираємо за назвою і підлаштовуємось за помилкою API.
_NEW_STYLE = MODEL.lower().startswith(("gpt-5", "o1", "o3", "o4"))
GEN_STYLE: dict = {"tokens_key": "max_completion_tokens" if _NEW_STYLE else "max_tokens",
                   "temperature": None if _NEW_STYLE else 0.3,
                   "reasoning_effort": REASONING_EFFORT if _NEW_STYLE else None}
TODAY_CONTEXT_CHARS = 6000
POST_CONTEXT_CHARS = 2500
CATEGORY_LABELS: dict = {}   # бот підставляє свої назви категорій при старті
FAILED_ANSWER_PREFIXES = ("Не вдалося сформулювати відповідь", "Не встиг зібрати відповідь")


class EmptyAnswer(Exception):
    """Модель не дала тексту (навіть після повтору з коротшою історією)."""

    def __init__(self, reason: str, tokens: dict):
        super().__init__(reason)
        self.tokens = tokens


def _gen_params(limit: int) -> dict:
    p = {GEN_STYLE["tokens_key"]: limit}
    if GEN_STYLE["temperature"] is not None:
        p["temperature"] = GEN_STYLE["temperature"]
    if GEN_STYLE["reasoning_effort"]:
        p["reasoning_effort"] = GEN_STYLE["reasoning_effort"]
    return p

SYSTEM_PROMPT = """Ти — аналітик-співрозмовник для команди в оборонному секторі України.
Про команду: {org}
Сьогодні: {today} (Київ).

НОВИНА, ЯКУ ЛЮДИНА ЗАРАЗ ДИВИТЬСЯ
{current_post}

ПРО ЦЮ ЛЮДИНУ
{profile}

ЩО ЇЙ СЬОГОДНІ НАДІСЛАНО І ЧОМУ
{today_items}

РАНІШЕ В ЦІЙ РОЗМОВІ
{summary}

ЯК ПРАЦЮВАТИ
- Якщо людина пише «це», «про це», «детальніше» чи «а які ризики» без уточнення — йдеться про новину,
  яку вона зараз дивиться (розділ вище), або про попередню відповідь. Цілі людини — лише додатковий контекст.
- Відповідай українською, по суті, до 250 слів (якщо не просять докладніше). Без markdown-розмітки.
- Факти бери лише з даних вище або з інструментів. Якщо для відповіді потрібні новини поза сьогоднішнім
  переліком — виклич search_news / get_digest / get_trends. Не вигадуй фактів, цифр, назв.
- Біля фактів давай посилання на джерела (URL з даних).
- RU-джерела — твердження російських каналів, а не факти; так і подавай.
- «Чому ти це порадив / чому це тут»: поясни конкретно — обрана сьогодні категорія, бали інтересів людини,
  важливість, скільки каналів писали, зв'язок з її цілями.
- «Що буде актуально / прогноз»: спирайся на тренди (get_trends) і факти; подавай як гіпотезу з рівнем
  упевненості (низька / середня / висока) і що її підтвердить чи спростує.
- «Що робити»: 3–5 конкретних кроків під цілі людини; це пропозиції, рішення за людиною.
- Інвестиції: сигнали, аргументи за і проти, ризики — але без прямих порад «купіть / вкладіть».
- Якщо даних замало — так і скажи і запропонуй, що уточнити."""

TOOLS = [
    {"type": "function", "function": {
        "name": "search_news",
        "description": "Пошук новин у базі за ключовими словами. Повертає самарі, дату, важливість, сторону, посилання.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "ключові слова, напр. 'Brave1 гранти' або 'морські дрони'"},
            "days": {"type": "integer", "description": "за скільки останніх днів шукати (1–30)", "default": 7},
            "side": {"type": "string", "enum": ["ua", "ru", "all"], "default": "all"},
            "category": {"type": "string", "description": "необов'язково: ключ категорії"},
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_digest",
        "description": "Останнє готове зведення за добу або тиждень (пункти з джерелами).",
        "parameters": {"type": "object", "properties": {
            "period": {"type": "string", "enum": ["day", "week"]},
            "side": {"type": "string", "enum": ["ua", "ru"]},
        }, "required": ["period", "side"]}}},
    {"type": "function", "function": {
        "name": "get_trends",
        "description": "Які категорії й теми частіше чи рідше згадуються за останні N днів порівняно з попередніми N днями.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "default": 7},
            "side": {"type": "string", "enum": ["ua", "ru"], "default": "ua"},
        }}}},
]


# ---------------------------------------------------------------------------
# Інструменти (запити до бази)
# ---------------------------------------------------------------------------

def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(1, min(int(days or 7), 30)))).isoformat()


def _base_filter(side: str | None) -> dict:
    f = {"processed": True, "is_ad": {"$ne": True}, "is_duplicate": {"$ne": True}}
    if side in ("ua", "ru"):
        f["source_side"] = side
    return f


async def tool_search_news(query: str, days: int = 7, side: str = "all", category: str | None = None) -> dict:
    words = [w for w in re.findall(r"\w{3,}", query or "", flags=re.U)][:8]
    if not words:
        return {"results": [], "note": "порожній запит"}
    rx = [{"$regex": re.escape(w[:max(4, len(w) - 2)]), "$options": "i"} for w in words]  # грубий «стемінг»
    q = {**_base_filter(side), "published_at": {"$gte": _since(days)},
         "$or": [{f: r} for r in rx for f in ("summary", "clean_text", "tags")]}
    if category:
        q["category"] = category
    docs = await posts_collection.find(
        q, {"summary": 1, "category": 1, "importance": 1, "source_side": 1, "published_at": 1, "post_url": 1}
    ).sort([("importance", -1), ("published_at", -1)]).limit(15).to_list(15)
    return {"results": [{
        "summary": d.get("summary"), "category": d.get("category"), "importance": d.get("importance"),
        "side": d.get("source_side"), "date": str(d.get("published_at", ""))[:16], "url": d.get("post_url"),
    } for d in docs]}


async def tool_get_digest(period: str = "day", side: str = "ua") -> dict:
    d = await digests_collection.find_one({"period": period, "side": side}, sort=[("created_at", -1)])
    if not d:
        return {"items": [], "note": "зведення ще немає"}
    return {"created_at": d["created_at"].isoformat(), "items": [
        {"category": s.get("category"), "text": it.get("text"), "urls": it.get("source_urls", [])}
        for s in d.get("sections", []) for it in s.get("items", [])]}


async def tool_get_trends(days: int = 7, side: str = "ua") -> dict:
    days = max(1, min(int(days or 7), 30))
    now = datetime.now(timezone.utc)
    cur_from, prev_from = (now - timedelta(days=days)).isoformat(), (now - timedelta(days=2 * days)).isoformat()

    async def counts(field: str, start: str, end: str | None) -> dict:
        match = {**_base_filter(side), "published_at": {"$gte": start, **({"$lt": end} if end else {})}}
        pipe = [{"$match": match}]
        if field == "tags":
            pipe.append({"$unwind": "$tags"})
        pipe += [{"$group": {"_id": f"${field}", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}, {"$limit": 40}]
        return {r["_id"]: r["n"] for r in await posts_collection.aggregate(pipe).to_list(None) if r["_id"]}

    out = {}
    for field in ("category", "tags"):
        cur, prev = await counts(field, cur_from, None), await counts(field, prev_from, cur_from)
        rows = [{"name": k, "now": v, "before": prev.get(k, 0),
                 "change_%": round((v - prev.get(k, 0)) / prev[k] * 100) if prev.get(k) else None}
                for k, v in cur.items()]
        rows.sort(key=lambda r: -r["now"])
        out[field] = rows[:15]
    return {"period_days": days, "side": side, **out}


TOOL_FUNCS = {"search_news": tool_search_news, "get_digest": tool_get_digest, "get_trends": tool_get_trends}


# ---------------------------------------------------------------------------
# Що людині сьогодні надіслано і чому
# ---------------------------------------------------------------------------

def _label(side: str, cat: str) -> str:
    return CATEGORY_LABELS.get(side, {}).get(cat, cat)


async def today_items(user: dict) -> str:
    daily = user.get("daily") or {}
    ids = daily.get("digest_ids") or []
    docs = [d async for d in digests_collection.find({"_id": {"$in": ids}})] if ids else []
    if not docs:
        return "Сьогодні ще нічого не надсилалося."
    selected = daily.get("selected") or {}
    scores = store.category_scores(user)
    reactions = {(f.get("digest_id"), f.get("item_n")): f.get("reaction") for f in user.get("feedback", [])}

    post_ids = [pid for d in docs for s in d.get("sections", []) for it in s.get("items", [])
                for pid in it.get("source_post_ids", [])]
    imp = {p["_id"]: p.get("importance") for p in
           await posts_collection.find({"_id": {"$in": post_ids}}, {"importance": 1}).to_list(None)}

    lines, c = [], 0
    for d in sorted(docs, key=lambda x: 0 if x.get("side") == "ua" else 1):
        side = d.get("side")
        for s in d.get("sections", []):
            cat = s["category"]
            why = []
            if cat in selected.get(side, []):
                why.append("людина обрала цю категорію сьогодні")
            if (scores.get(side) or {}).get(cat):
                why.append(f"бал інтересу до категорії {scores[side][cat]}")
            for it in s.get("items", []):
                c += 1
                n = it.get("n", c)
                srcs = it.get("source_post_ids", [])
                importance = max((imp.get(p) or 0 for p in srcs), default=0)
                r = reactions.get((d["_id"], n))
                lines.append(f"- [{side.upper()} · {_label(side, cat)}] {it.get('text')} "
                             f"(важливість {importance}/5; джерел {len(srcs)}"
                             f"{'; ' + '; '.join(why) if why else ''}"
                             f"{'; оцінка людини: ' + r if r else ''}) {' '.join(it.get('source_urls', [])[:2])}")
    text = "\n".join(lines)
    return text[:TODAY_CONTEXT_CHARS] + ("\n…(обрізано)" if len(text) > TODAY_CONTEXT_CHARS else "")


# ---------------------------------------------------------------------------
# Відповідь
# ---------------------------------------------------------------------------

def _clean(answer: str) -> str:
    answer = re.sub(r"\*\*(.+?)\*\*", r"\1", answer or "")
    answer = re.sub(r"^#+\s*", "", answer, flags=re.M)
    return answer.strip()


def _sanitize_history(messages: list[dict]) -> list[dict]:
    """Валідна історія для API: без порожніх і невдалих відповідей, без двох однакових ролей поспіль."""
    out: list[dict] = []
    for m in messages:
        content = (m.get("content") or "").strip()
        if m.get("role") not in ("user", "assistant") or not content:
            continue
        if m["role"] == "assistant" and content.startswith(FAILED_ANSWER_PREFIXES):
            continue
        if out and out[-1]["role"] == m["role"]:
            out[-1] = {"role": m["role"], "content": f"{out[-1]['content']}\n\n{content}"}
        else:
            out.append({"role": m["role"], "content": content})
    return out


async def _older_turns_summary(conv_id, keep: int) -> str:
    """Короткий підсумок старших реплік (без виклику моделі): які питання людина ставила раніше."""
    doc = await store.conversations.find_one({"_id": conv_id}, {"messages.role": 1, "messages.content": 1}) or {}
    older = (doc.get("messages") or [])[:-keep] if keep else []
    questions = [" ".join((m.get("content") or "").split())[:150] for m in older if m.get("role") == "user"]
    summary = ("Людина раніше питала: " + "; ".join(questions[-15:])) if questions else ""
    if summary:
        await store.conversations.update_one({"_id": conv_id}, {"$set": {"summary": summary}})
    return summary


async def _current_post_block(post: dict | None) -> str:
    if not post:
        return "Зараз людина не дивиться окрему новину."
    lines = [f"[{(post.get('side') or '').upper()} · {_label(post.get('side'), post.get('category'))}] {post.get('text', '')}"]
    if post.get("source_urls"):
        lines.append("Посилання: " + " ".join(post["source_urls"][:3]))
    ids = post.get("source_post_ids") or []
    if ids:
        lines.append("post_ids: " + ", ".join(str(i) for i in ids))
        details = []
        async for p in posts_collection.find({"_id": {"$in": ids}}, {"summary": 1, "clean_text": 1, "post_url": 1}):
            text = " ".join((p.get("clean_text") or "").split())[:POST_CONTEXT_CHARS // max(1, len(ids))]
            details.append(f"- {p.get('summary') or ''}\n  Текст поста: {text}" if text else f"- {p.get('summary') or ''}")
        if details:
            lines.append("Що пишуть джерела:\n" + "\n".join(details))
    return "\n".join(lines)


async def _create(kwargs: dict, purpose: str, attempt: int):
    """Один виклик моделі з журналом: модель, токени, причина завершення, довжина тексту, request id."""
    try:
        resp = await client.chat.completions.create(**kwargs)
    except Exception as e:
        logger.error(f"LLM error purpose={purpose} attempt={attempt} model={kwargs['model']} "
                     f"request_id={getattr(e, 'request_id', None)} {type(e).__name__}: {e}")
        raise
    choice, usage = resp.choices[0], resp.usage
    details = getattr(usage, "completion_tokens_details", None)
    logger.info(f"LLM call purpose={purpose} attempt={attempt} model={resp.model} request_id={getattr(resp, '_request_id', None)} "
                f"finish={choice.finish_reason} in={getattr(usage, 'prompt_tokens', None)} "
                f"out={getattr(usage, 'completion_tokens', None)} reasoning={getattr(details, 'reasoning_tokens', None)} "
                f"text_len={len(choice.message.content or '')} tool_calls={len(choice.message.tool_calls or [])} "
                f"limit={kwargs.get(GEN_STYLE['tokens_key'])}")
    return resp


async def answer(user_id: int, question: str, reply_to: str | None = None, current_post: dict | None = None) -> dict:
    """Повертає {"text", "conversation_id", "message_id", "tokens"}; EmptyAnswer, якщо модель не дала тексту.

    current_post — новина, яку людина зараз бачить у стрічці (digest_id, n, side, category, text,
    source_urls, source_post_ids); «це» / «про це» стосується її.
    """
    user = await users_collection.find_one({"_id": user_id}) or {"_id": user_id}
    profile = await store.llm_user_profile(user_id, CATEGORY_LABELS) or "Профіль ще порожній."
    conv = await store.get_or_start_conversation(user_id, title=question)
    keep = HISTORY_TURNS * 2
    history = _sanitize_history(await store.conversation_history(conv["_id"], limit=keep))
    summary = await _older_turns_summary(conv["_id"], keep) or "—"

    system = SYSTEM_PROMPT.format(
        org=ORG_CONTEXT, today=datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y"),
        current_post=await _current_post_block(current_post),
        profile=profile, today_items=await today_items(user), summary=summary)
    user_msg = question if not reply_to else f"(Відповідь на повідомлення бота: «{reply_to[:1500]}»)\n{question}"
    purpose = "feed" if current_post else "chat"
    limit = MAX_OUTPUT_TOKENS_FEED if current_post else MAX_OUTPUT_TOKENS

    def base_messages(hist: list[dict]) -> list[dict]:
        return [{"role": "system", "content": system}, *_sanitize_history(hist + [{"role": "user", "content": user_msg}])]

    messages = base_messages(history)
    await store.add_message(conv["_id"], user_id, "user", user_msg)

    sources, tool_log, use_tools, retried = [], [], True, False
    tokens = {"in": 0, "out": 0}
    text = None
    for attempt in range(1, MAX_TOOL_ROUNDS + 5):
        kwargs = {"model": MODEL, "messages": messages, **_gen_params(limit)}
        if use_tools:
            kwargs["tools"] = TOOLS
        try:
            resp = await _create(kwargs, purpose, attempt)
        except Exception as e:
            err = str(e).lower()
            if "max_tokens" in err and GEN_STYLE["tokens_key"] == "max_tokens":   # модель хоче max_completion_tokens
                GEN_STYLE["tokens_key"] = "max_completion_tokens"
                continue
            if "temperature" in err and GEN_STYLE["temperature"] is not None:   # модель не приймає temperature
                GEN_STYLE["temperature"] = None
                continue
            if "reasoning_effort" in err and GEN_STYLE["reasoning_effort"]:     # модель не приймає reasoning_effort
                GEN_STYLE["reasoning_effort"] = None
                continue
            if use_tools and "tool" in err:   # модель не вміє інструменти — працюємо без них
                logger.warning(f"Модель {MODEL} не підтримує інструменти, відповідаю без них")
                use_tools = False
                continue
            raise
        tokens["in"] += getattr(resp.usage, "prompt_tokens", 0) or 0
        tokens["out"] += getattr(resp.usage, "completion_tokens", 0) or 0
        msg = resp.choices[0].message
        if not getattr(msg, "tool_calls", None):
            text = _clean(msg.content)
            if text:
                break
            if retried:
                raise EmptyAnswer(f"empty output after retry (finish={resp.choices[0].finish_reason})", tokens)
            logger.warning(f"LLM empty output (finish={resp.choices[0].finish_reason}); retry with shorter history")
            retried = True
            messages = base_messages(history[-RETRY_HISTORY_MESSAGES:])
            continue
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            fn = TOOL_FUNCS.get(tc.function.name)
            try:
                args = json.loads(tc.function.arguments or "{}")
                result = await fn(**args) if fn else {"error": "невідомий інструмент"}
            except Exception as e:
                logger.warning(f"Інструмент {tc.function.name}: {e}")
                result = {"error": str(e)[:200]}
            tool_log.append({"name": tc.function.name, "args": tc.function.arguments})
            for r in result.get("results", []) + result.get("items", []):
                for u in ([r.get("url")] if r.get("url") else r.get("urls", [])):
                    sources.append({"url": u})
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, ensure_ascii=False, default=str)[:12000]})
    else:
        raise EmptyAnswer("too many tool rounds", tokens)

    msg_id = await store.add_message(conv["_id"], user_id, "assistant", text,
                                     sources=sources[:30], tool_calls=tool_log, model=MODEL, tokens=tokens)
    return {"text": text, "conversation_id": conv["_id"], "message_id": msg_id, "tokens": tokens}
