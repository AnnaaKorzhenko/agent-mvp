
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

MODEL = os.getenv("ASSISTANT_LLM_MODEL") or "gpt-4.1-mini"
_IS_OPENAI = MODEL.lower().startswith(("gpt-", "o1", "o3", "o4"))
client = AsyncOpenAI(

    base_url=os.getenv("ASSISTANT_LLM_BASE_URL") or (None if _IS_OPENAI else "http://localhost:11434/v1"),
    api_key=os.getenv("ASSISTANT_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY") or "local",
    timeout=60, max_retries=2,
)
ORG_CONTEXT = os.getenv("ORG_CONTEXT") or (
    "Українська команда в оборонному секторі. Цікавлять: інновації та технології на фронті (дрони, РЕБ, ППО, "
    "робототехніка), інвестиції й фінансування оборонних компаній, оборонна промисловість, держзакупівлі, "
    "ситуація на фронті, а також що противник каже про Україну і які в нього новинки."
)

MAX_TOOL_ROUNDS = 4
REASONING_EFFORT = os.getenv("ASSISTANT_REASONING_EFFORT") or "low"   # для gpt-5*: менше «роздумів»
LAST_ANSWER_CHARS = 4000
OLD_ANSWER_HEAD, OLD_ANSWER_TAIL = 500, 400
TOOL_RESULT_CHARS = 8000
HISTORY_MESSAGES = 10
TODAY_CONTEXT_CHARS = 6000
CATEGORY_LABELS: dict = {}   # бот підставляє свої назви категорій при старті

SYSTEM_PROMPT = """Ти — аналітик новин для команди в оборонному секторі України. Говориш з однією людиною в Telegram.
Про команду: {org}
Сьогодні {today} (Київ).

ПРО ЛЮДИНУ
{profile}

ЩО ЇЙ СЬОГОДНІ НАДІСЛАНО (новина · важливість · чому показана · її оцінка · посилання)
{today_items}

ЯК ВІДПОВІДАТИ
1. Перше речення — пряма відповідь на питання. Без вступів («Чудове питання», «Ось що я знайшов»)
   і без підсумкових блоків наприкінці («Завдяки цьому ви отримуєте…», «Отже…»).
2. Відповідай лише на те, що спитали. Не додавай розділів, про які не питали, і не повторюй сказане раніше.
3. Довжина за питанням: просте питання чи уточнення — 1–3 речення; огляд чи аналіз — до 200 слів,
   кілька пунктів «• ». Більше — лише якщо людина просить детально.
4. Розмова одна й безперервна. Уточнення («а детальніше?», «а що з цим?», «чому?») — продовження тієї ж теми.
   Коротка відповідь людини («так», «давай», «ок», «ні», «другий») — це відповідь на твоє останнє
   повідомлення: якщо ти щось пропонував чи питав — одразу виконай це (не перепитуй і не переказуй
   попередню відповідь); «ні» — коротко прийми і нічого не роби.
4а. Наприкінці можна запропонувати один конкретний наступний крок (одне коротке речення), лише якщо він
   справді корисний. Якщо людина погодиться — виконай саме його.
5. Звичайний текст українською, без markdown (без **, #, таблиць).

МЕЖІ ТЕМИ
- Лише оборона, війна й пов'язані новини, цілі людини та цей бот; можна коротко пояснити термін з новини.
- Інше (рецепти, код, побут тощо) не виконуй навіть частково: одне речення — «це поза моєю темою» +
  запропонуй щось із її новин. Привітання й подяка — нормально.

ФАКТИ І ДЖЕРЕЛА
- Факти — лише з даних вище або з інструментів. Не вигадуй цифр, назв, дат. Якщо даних немає —
  скажи одним реченням і, за потреби, що можна пошукати.
- Після кожного факту став голий URL з даних (без слова «Джерело» і без дужок) — бот сам покаже його як «Джерело».
- RU-джерела — це твердження російських каналів, а не факти: «рос. канали заявляють, що…».

КОЛИ ШУКАТИ В БАЗІ
- Привітання, подяка, коротка репліка, питання про сьогоднішні новини, які вже є вище — без інструментів.
- Інша тема чи інший період — search_news (ключові слова, 2–4 найважливіші). Готове зведення — get_digest.
  Що набирає обертів, прогноз — get_trends.
- Не повторюй той самий пошук. Якщо перший нічого не дав — спробуй інші слова або більший період один раз.

ОСОБЛИВІ ПИТАННЯ
- «Чому ти це порадив / чому це тут»: обрана сьогодні категорія, бали інтересу, важливість,
  скільки каналів писали, зв'язок з її цілями — конкретно, з даних вище.
- Прогноз: гіпотеза + упевненість (низька / середня / висока) + що її підтвердить чи спростує.
  Якщо get_trends пише, що за попередній період замало даних, — не роби висновків про зростання.
- «Що робити»: 2–4 конкретні кроки під її цілі; рішення за людиною.
- Інвестиції: сигнали, аргументи за і проти, ризики. Без порад «купіть / вкладіть»."""

TOOLS = [
    {"type": "function", "function": {
        "name": "search_news",
        "description": "Пошук новин у базі за ключовими словами за останні N днів. Повертає самарі, дату, "
                       "важливість (1–5), сторону (ua/ru) і посилання.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string",
                      "description": "2–4 ключові слова українською, напр. 'Brave1 гранти' або 'морські дрони'"},
            "days": {"type": "integer", "description": "за скільки останніх днів шукати (1–30)", "default": 7},
            "side": {"type": "string", "enum": ["ua", "ru", "all"], "default": "all",
                     "description": "ua — українські джерела, ru — російські, all — усі"},
            "category": {"type": "string", "description": "необов'язково: ключ категорії"},
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_digest",
        "description": "Останнє готове зведення за добу або тиждень (пункти з посиланнями).",
        "parameters": {"type": "object", "properties": {
            "period": {"type": "string", "enum": ["day", "week"]},
            "side": {"type": "string", "enum": ["ua", "ru"]},
        }, "required": ["period", "side"]}}},
    {"type": "function", "function": {
        "name": "get_trends",
        "description": "Скільки згадок мають категорії й теми за останні N днів порівняно з попередніми N днями — "
                       "для питань «що набирає обертів», «що буде актуально».",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "default": 7},
            "side": {"type": "string", "enum": ["ua", "ru"], "default": "ua"},
        }}}},
]



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
         "$or": [{f: r} for r in rx for f in ("summary", "tags")]}
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
    total_now = sum(r["now"] for r in out["category"])
    total_before = sum(r["before"] for r in out["category"])
    result = {"period_days": days, "side": side, "posts_now": total_now, "posts_before": total_before, **out}
    if total_before < total_now * 0.3:
        result["note"] = ("За попередній період у базі замало даних — порівняння ненадійне, "
                          "висновків про зростання чи спад не роби.")
    return result


TOOL_FUNCS = {"search_news": tool_search_news, "get_digest": tool_get_digest, "get_trends": tool_get_trends}



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


# ---------------------------------------------------------------------------
# Виклик моделі: підлаштування під параметри моделі + облік токенів
# ---------------------------------------------------------------------------

_NEW_STYLE = MODEL.lower().startswith(("gpt-5", "o1", "o3", "o4"))
_DROPPED: set[str] = set()   # параметри, які модель відхилила (пам'ятаємо до перезапуску)


def _params() -> dict:
    p: dict = {}
    if _NEW_STYLE or "max_tokens" in _DROPPED:
        p["max_completion_tokens"] = 8000 if _NEW_STYLE else 900   # у gpt-5 сюди входять і «роздуми»
    else:
        p["max_tokens"] = 1200
    if not _NEW_STYLE and "temperature" not in _DROPPED:
        p["temperature"] = 0.3
    if _NEW_STYLE and REASONING_EFFORT and "reasoning_effort" not in _DROPPED:
        p["reasoning_effort"] = REASONING_EFFORT
    return p


async def _complete(messages: list, usage: dict, tools: list | None):
    for _ in range(5):
        kwargs = {"model": MODEL, "messages": messages, **_params()}
        if tools and "tools" not in _DROPPED:
            kwargs["tools"] = tools
        try:
            resp = await client.chat.completions.create(**kwargs)
        except Exception as e:
            err = str(e).lower()
            bad = next((p for p in ("max_tokens", "temperature", "reasoning_effort", "tools")
                        if f"'{p}'" in err and p not in _DROPPED), None)
            if not bad and "does not support tools" in err:          # так пише Ollama
                bad = "tools"
            if not bad or bad in _DROPPED:
                raise
            _DROPPED.add(bad)
            logger.warning(f"Модель {MODEL} відхилила параметр {bad} — працюю без нього")
            continue
        u = getattr(resp, "usage", None)
        if u:
            usage["prompt"] += u.prompt_tokens or 0
            usage["completion"] += u.completion_tokens or 0
        usage["calls"] += 1
        return resp
    raise RuntimeError("не вдалося підібрати параметри моделі")


def _log_empty(resp, where: str) -> None:
    reason = getattr(resp.choices[0], "finish_reason", None)
    u = getattr(resp, "usage", None)
    details = getattr(u, "completion_tokens_details", None) if u else None
    logger.warning(f"Порожня відповідь моделі ({where}): finish_reason={reason}, "
                   f"completion_tokens={getattr(u, 'completion_tokens', None)}, "
                   f"reasoning_tokens={getattr(details, 'reasoning_tokens', None) if details else None}"
                   + (" — ліміт токенів з'їли «роздуми» моделі" if reason == "length" else ""))


_TEXT_CALL = re.compile(r'\{\s*"name"\s*:\s*"(\w+)"\s*,\s*"arguments"\s*:\s*(\{.*?\})\s*\}', re.S)


def _text_tool_call(content: str | None):
    """qwen в Ollama іноді пише виклик інструмента текстом, а не через tool_calls."""
    m = _TEXT_CALL.search(content or "")
    if m and m.group(1) in TOOL_FUNCS:
        try:
            return m.group(1), json.loads(m.group(2))
        except json.JSONDecodeError:
            return None
    return None


async def _run_tool(name: str, args: dict, sources: list, tool_log: list) -> str:
    fn = TOOL_FUNCS.get(name)
    try:
        result = await fn(**args) if fn else {"error": "невідомий інструмент"}
    except Exception as e:
        logger.warning(f"Інструмент {name}: {e}")
        result = {"error": str(e)[:200]}
    tool_log.append({"name": name, "args": json.dumps(args, ensure_ascii=False)})
    for r in result.get("results", []) + result.get("items", []):
        for u in ([r.get("url")] if r.get("url") else r.get("urls", [])):
            if u:
                sources.append(u)
    return json.dumps(result, ensure_ascii=False, default=str)[:TOOL_RESULT_CHARS]


async def _llm_answer(base: list, usage: dict, sources: list, tool_log: list) -> str:
    """Відповідь моделі. Повертає "" якщо модель так і не відповіла."""
    messages = list(base)
    text = ""
    for step in range(MAX_TOOL_ROUNDS + 1):
        last = step == MAX_TOOL_ROUNDS          # останній крок — лише відповідь, без інструментів
        resp = await _complete(messages, usage, None if last else TOOLS)
        msg = resp.choices[0].message
        calls = getattr(msg, "tool_calls", None) or []
        if calls:
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [tc.model_dump() for tc in calls]})
            for tc in calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": await _run_tool(tc.function.name, args, sources, tool_log)})
            continue
        call = None if last else _text_tool_call(msg.content)
        if call:
            result = await _run_tool(call[0], call[1], sources, tool_log)
            messages.append({"role": "assistant", "content": msg.content})
            messages.append({"role": "user", "content": f"Дані ({call[0]}):\n{result}\n\nТепер дай відповідь на моє питання."})
            continue
        text = _clean(msg.content)
        if not text:
            _log_empty(resp, f"крок {step}")
        break

    if not text:   # запасна спроба 1: без інструментів, з прямим проханням відповісти
        resp = await _complete(messages + [{"role": "user", "content":
                               "Відповідай на моє питання зараз, коротко, на основі того, що вже є."}], usage, None)
        text = _clean(resp.choices[0].message.content)
        if not text:
            _log_empty(resp, "запасна спроба 1")
    if not text:   # запасна спроба 2: просто розмова, без даних інструментів
        resp = await _complete(base, usage, None)
        text = _clean(resp.choices[0].message.content)
        if not text:
            _log_empty(resp, "запасна спроба 2")
    return text


FALLBACK_HEAD = "Аналітик зараз недоступний"
_SMALL_TALK = re.compile(r"^\W*(привіт\w*|вітаю|добр\w+ (ранку|день|вечір|ніч)|хай|hello|hi|дяку\w*|спасибі|"
                         r"ок(ей)?|ага|зрозуміл\w*|супер|клас)\W*$", re.I)
_STOP = set("що як де коли чому хто який яка яке які мені мене мій моя мої про для від цей ця це ці там тут "
            "було буде є був була були зараз сьогодні тиждень тижня день дня новини новин новина розкажи покажи "
            "скажи можеш можна треба щось також або але чи так ні the and".split())


async def _fallback(question: str) -> str:
    if _SMALL_TALK.match(question.strip()):
        return (f"{FALLBACK_HEAD}, але бот працює: новини — /all_day, /today, налаштування — /mode, /goals. "
                "Спробуйте написати питання трохи пізніше.")
    words = [w for w in re.findall(r"\w{3,}", question.lower(), flags=re.U) if w not in _STOP][:6]
    docs, title = [], ""
    proj = {"summary": 1, "source_side": 1, "post_url": 1}
    if words:
        rx = [{"$regex": re.escape(w[:max(4, len(w) - 2)]), "$options": "i"} for w in words]
        q = {**_base_filter(None), "published_at": {"$gte": _since(7)},
             "$or": [{f: r} for r in rx for f in ("summary", "tags")]}
        docs = await posts_collection.find(q, proj).sort([("importance", -1), ("published_at", -1)]).limit(5).to_list(5)
        title = "Ось що є в базі по темі за тиждень:"
    if not docs:
        docs = await posts_collection.find({**_base_filter(None), "published_at": {"$gte": _since(1)}}, proj) \
            .sort([("importance", -1), ("published_at", -1)]).limit(5).to_list(5)
        title = "По темі нічого не знайшов. Найважливіше за добу:"
    if not docs:
        return f"{FALLBACK_HEAD}, і свіжих новин у базі поки немає. Спробуйте трохи пізніше."
    lines = [f"{FALLBACK_HEAD} — спробуйте пізніше. {title}"]
    for d in docs:
        side = " (рос. джерело — їхнє твердження)" if d.get("source_side") == "ru" else ""
        lines.append(f"• {d.get('summary')}{side}" + (f"\n  {d['post_url']}" if d.get("post_url") else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Відповідь
# ---------------------------------------------------------------------------

async def _history(user: dict) -> list[dict]:
    history = await store.chat_history(user["_id"], HISTORY_MESSAGES, since=user.get("chat_topic_from"))
    last_assistant = max((i for i, m in enumerate(history) if m["role"] == "assistant"), default=-1)
    for i, m in enumerate(history):
        if m["role"] != "assistant":
            continue
        c = m["content"]
        if i == last_assistant:
            m["content"] = c[:LAST_ANSWER_CHARS]
        elif len(c) > OLD_ANSWER_HEAD + OLD_ANSWER_TAIL:
            # кінець теж лишаємо — там зазвичай питання чи пропозиція до людини
            m["content"] = c[:OLD_ANSWER_HEAD] + " … " + c[-OLD_ANSWER_TAIL:]
    return history


async def answer(user_id: int, question: str, reply_to: str | None = None) -> dict:
    """Завжди повертає {"text", "message_id", "tokens", "fallback"}."""
    usage = {"prompt": 0, "completion": 0, "calls": 0}
    sources, tool_log = [], []
    text = ""
    try:
        user = await users_collection.find_one({"_id": user_id}) or {"_id": user_id}
        profile = await store.llm_user_profile(user_id, CATEGORY_LABELS) or "Профіль ще порожній."
        history = await _history(user)
        system = SYSTEM_PROMPT.format(
            org=ORG_CONTEXT, today=datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y"),
            profile=profile, today_items=await today_items(user))
        user_msg = question if not reply_to else f"(Відповідь на повідомлення бота: «{reply_to[:1500]}»)\n{question}"
        base = [{"role": "system", "content": system}, *history, {"role": "user", "content": user_msg}]
        await store.add_chat_message(user_id, "user", question, reply_to=(reply_to or "")[:1500] or None)
        text = await _llm_answer(base, usage, sources, tool_log)
        if not text:
            logger.error(f"[{user_id}] модель тричі повернула порожню відповідь — відповідаю без моделі")
    except Exception as e:
        logger.exception(f"[{user_id}] модель недоступна ({type(e).__name__}: {str(e)[:200]}) — відповідаю без моделі")
        if "connect" in type(e).__name__.lower() or "connection" in str(e).lower():
            logger.error(f"Перевірте адресу моделі: {client.base_url} (Ollama запущена? ASSISTANT_LLM_BASE_URL у .env?)")

    fallback = not text
    if fallback:
        try:
            text = await _fallback(question)
        except Exception:
            logger.exception("Запасна відповідь теж не вдалася")
            text = f"{FALLBACK_HEAD}. Новини доступні командами /all_day і /today — спробуйте трохи пізніше."

    tokens = {**usage, "total": usage["prompt"] + usage["completion"], "tool_calls": len(tool_log)}
    msg_id = None
    try:
        msg_id = await store.add_chat_message(user_id, "assistant", text, sources=list(dict.fromkeys(sources))[:30],
                                              tool_calls=tool_log, model=MODEL, tokens=tokens, fallback=fallback)
        month = datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%Y-%m")
        await users_collection.update_one({"_id": user_id}, {"$inc": {
            "usage.tokens_total": tokens["total"], "usage.questions": 1,
            f"usage.by_month.{month}.tokens": tokens["total"], f"usage.by_month.{month}.questions": 1}})
    except Exception:
        logger.exception("Не вдалося зберегти відповідь у базу")
    logger.info(f"[{user_id}] інструментів {len(tool_log)}, токенів {tokens['total']}"
                + (" — ВІДПОВІДЬ БЕЗ МОДЕЛІ" if fallback else ""))
    return {"text": text, "message_id": msg_id, "tokens": tokens, "fallback": fallback}
