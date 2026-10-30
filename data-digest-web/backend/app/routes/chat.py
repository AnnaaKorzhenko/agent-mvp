import re
from typing import Literal

from fastapi import APIRouter, HTTPException
from openai import AsyncOpenAI
from pydantic import BaseModel

from ..config import POST_FIELDS as F, settings
from ..db import col
from ..normalize import get, human_date, platform_label, relevance_level, since

router = APIRouter()

SYSTEM_PROMPT = """Ти — аналітик оборонного сектору для української команди.
Відповідай українською, стисло й по суті.
Спирайся ТІЛЬКИ на пости з контексту нижче. Після кожного твердження вказуй номер джерела у квадратних дужках, напр. [3].
Розрізняй, що повідомляють українські, а що російські джерела; наративи російських каналів подавай як їхню позицію, а не як факт.
Якщо в контексті немає відповіді — прямо скажи про це і запропонуй, як уточнити запит."""

STOP = {"який", "яка", "яке", "які", "щодо", "тиждень", "сьогодні", "розкажи", "детальніше", "покажи",
        "про", "сюжет", "нові", "нова", "новий", "останній", "ситуація", "подають", "новини", "останні", "головне", "чому", "коли", "where", "what", "about"}


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[Message]


_llm: AsyncOpenAI | None = None


def llm() -> AsyncOpenAI:
    global _llm
    if _llm is None:
        if not settings.LLM_API_KEY:
            raise HTTPException(500, "LLM_API_KEY не задано в .env")
        _llm = AsyncOpenAI(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL)
    return _llm


def keywords(text: str) -> list[str]:
    words = re.findall(r"[\w'’]{4,}", text.lower())
    stems = []
    for w in words:
        if w in STOP:
            continue
        stem = w[: max(4, len(w) - 2)]  # грубе «стемування» під відмінки
        if stem not in stems:
            stems.append(stem)
    return stems[:8]


async def find_context(question: str) -> list[dict]:
    base = {F["date"]: {"$gte": since(settings.CHAT_CONTEXT_DAYS)}}
    kws = keywords(question)
    docs: list[dict] = []
    if kws:
        rxs = [re.compile(re.escape(k), re.I) for k in kws]
        query = {**base, "$or": [{F["summary"]: rx} for rx in rxs]}
        docs = [d async for d in col("posts").find(query).sort(F["date"], -1).limit(300)]

        def score(d):
            s = (get(d, F["summary"]) or "").lower()
            hits = sum(1 for k in kws if k in s)
            rel = {"high": 2, "medium": 1, "low": 0}[relevance_level(get(d, F["relevance"]))]
            return hits * 3 + rel

        docs.sort(key=score, reverse=True)
    if not docs:
        docs = [d async for d in col("posts").find(base).sort(F["date"], -1).limit(settings.CHAT_CONTEXT_POSTS)]
    return docs[: settings.CHAT_CONTEXT_POSTS]


def context_block(docs: list[dict]) -> str:
    lines = []
    for i, d in enumerate(docs, 1):
        lines.append(
            f"[{i}] {platform_label(get(d, F['platform']))} · {get(d, F['source']) or '?'} · "
            f"{(get(d, F['lang']) or '').upper()} · {human_date(get(d, F['date']))}\n"
            f"{get(d, F['summary']) or get(d, F['text']) or ''}"
        )
    return "\n\n".join(lines)


@router.post("/chat")
async def chat(req: ChatRequest):
    if not req.messages or req.messages[-1].role != "user":
        raise HTTPException(400, "Останнє повідомлення має бути від користувача")

    question = req.messages[-1].content
    docs = await find_context(question)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": "Контекст — пости з бази:\n\n" + (context_block(docs) or "(порожньо)")},
        *[m.model_dump() for m in req.messages[-10:]],
    ]
    resp = await llm().chat.completions.create(model=settings.LLM_MODEL, messages=messages)
    content = resp.choices[0].message.content or ""

    cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", content) if 0 < int(n) <= len(docs)})
    picked = [docs[n - 1] for n in cited] or docs[:5]
    sources = [
        {
            "platform": platform_label(get(d, F["platform"])),
            "name": get(d, F["source"]) or "",
            "url": get(d, F["url"]) or "",
        }
        for d in picked
    ]
    return {"content": content, "sources": sources}
