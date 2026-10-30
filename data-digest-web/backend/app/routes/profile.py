from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from ..config import USER_FIELDS as F, settings
from ..db import col
from ..normalize import get

router = APIRouter()

DEFAULT = {"interests": [], "schedule": "daily", "sides": ["UA", "RU", "Світ"]}


class Profile(BaseModel):
    interests: list[str] = []
    schedule: Literal["daily", "weekly", "off"] = "daily"
    sides: list[str] = []


def user_id():
    # якщо в боті id числовий (telegram id) — шукаємо числом
    uid = settings.WEB_USER_ID
    return int(uid) if uid.isdigit() else uid


@router.get("/profile")
async def get_profile():
    doc = await col("users").find_one({F["id"]: user_id()})
    if not doc:
        return DEFAULT
    return {
        "interests": get(doc, F["interests"]) or [],
        "schedule": get(doc, F["schedule"]) or "daily",
        "sides": get(doc, F["sides"]) or DEFAULT["sides"],
    }


@router.put("/profile")
async def save_profile(p: Profile):
    await col("users").update_one(
        {F["id"]: user_id()},
        {"$set": {F["interests"]: p.interests, F["schedule"]: p.schedule, F["sides"]: p.sides}},
        upsert=True,
    )
    return {"ok": True}
