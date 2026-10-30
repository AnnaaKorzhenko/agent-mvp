"""Перетворення документів з бази у формат, який чекає фронт."""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import CATEGORY_MAP, LANG_MAP, PLATFORM_MAP, settings

TZ = ZoneInfo(settings.TIMEZONE)


def get(doc: dict, field: str, default=None):
    """Підтримує вкладені поля через крапку: 'meta.channel'."""
    cur = doc
    for part in field.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def values_query(values: list[str]) -> dict:
    """Умова 'поле дорівнює одному з значень' без урахування регістру."""
    return {"$in": [re.compile(f"^{re.escape(v)}$", re.I) for v in values]}


def _reverse(mapping: dict[str, list[str]], value) -> str | None:
    if value is None:
        return None
    v = str(value).strip().lower()
    for key, variants in mapping.items():
        if v in (x.lower() for x in variants):
            return key
    return None


def category_id(value) -> str:
    return _reverse(CATEGORY_MAP, value) or str(value or "")


def platform_label(value) -> str:
    return _reverse(PLATFORM_MAP, value) or str(value or "")


def lang_label(value) -> str:
    return _reverse(LANG_MAP, value) or str(value or "").upper()


def side_from(side, lang) -> str:
    if side:
        return str(side)
    lab = lang_label(lang)
    return {"UA": "UA", "RU": "RU"}.get(lab, "Світ")


def relevance_level(value) -> str:
    """Число 0–1, 1–10 або рядок -> high / medium / low."""
    if value is None:
        return "medium"
    if isinstance(value, (int, float)):
        x = float(value)
        if x > 1:
            x = x / 10
        return "high" if x >= 0.7 else "medium" if x >= 0.4 else "low"
    s = str(value).strip().lower()
    if s in ("high", "висока", "high_priority", "3"):
        return "high"
    if s in ("low", "низька", "1"):
        return "low"
    return "medium"


def to_dt(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def human_date(value) -> str:
    d = to_dt(value)
    if not d:
        return ""
    d = d.astimezone(TZ)
    today = datetime.now(TZ).date()
    if d.date() == today:
        return f"сьогодні, {d:%H:%M}"
    if d.date() == today - timedelta(days=1):
        return f"вчора, {d:%H:%M}"
    return f"{d:%d.%m.%Y}"


def since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)
