"""Interest scoring shared by the Telegram bot and the MCP server.

Moved unchanged from user_store.py. Pure Python with no third-party imports, so the bot can import it
without the brain/ dependencies. tests/test_scoring.py checks it against outputs recorded from the
original functions.

Scores live in users.interests:
  category_scores.<side>.<category>  (side: "ua" | "ru")
  tag_scores.<tag>
"""
from __future__ import annotations

CHOICE_POINTS = 1      # category chosen in the morning picker
LIKE_POINTS = 1        # 👍
DISLIKE_POINTS = -1    # 👎
SKIP_POINTS = 0        # ⏭ recorded, gives no points

POINTS = {"like": LIKE_POINTS, "dislike": DISLIKE_POINTS, "skip": SKIP_POINTS,
          # Words of MCP-written entries (👍 / 👎 / 50/50); same points as the bot's like / dislike / skip
          "useful": LIKE_POINTS, "not_useful": DISLIKE_POINTS, "neutral": SKIP_POINTS}
SIDES = ("ua", "ru")


def tag_key(tag: str) -> str:
    """A tag as a safe Mongo field name: no dots, no leading $."""
    return str(tag).replace(".", "·").lstrip("$").strip()


def points_inc(entry: dict, sign: int) -> dict:
    """$inc for one feedback entry (reaction, side, category, tags). sign=-1 reverses it."""
    points = POINTS.get(entry.get("reaction"), 0) * sign
    if not points:
        return {}
    inc = {}
    if entry.get("side") in SIDES and entry.get("category"):
        inc[f"interests.category_scores.{entry['side']}.{entry['category']}"] = points
    for tag in entry.get("tags", []):
        if tag_key(tag):
            k = f"interests.tag_scores.{tag_key(tag)}"
            inc[k] = inc.get(k, 0) + points
    return inc


def choice_inc(selected: dict) -> dict:
    """$inc for categories chosen in the morning picker: {"ua": [...], "ru": [...]}."""
    return {f"interests.category_scores.{side}.{cat}": CHOICE_POINTS
            for side in SIDES for cat in selected.get(side, [])}


def merge_inc(*incs: dict) -> dict:
    """Sums several $inc dicts and drops zeros."""
    out: dict = {}
    for inc in incs:
        for k, v in inc.items():
            out[k] = out.get(k, 0) + v
    return {k: v for k, v in out.items() if v}


def category_scores(user: dict) -> dict:
    return (user.get("interests") or {}).get("category_scores") or {}


def top_tags(user: dict, limit: int = 10) -> list[dict]:
    tags = (user.get("interests") or {}).get("tag_scores") or {}
    return [{"tag": t, "score": v} for t, v in sorted(tags.items(), key=lambda x: -x[1]) if v > 0][:limit]


def category_order(user: dict, side: str, first: list[str] | None = None) -> list[str]:
    """Category order for one side: first (today's choice) by score, then the rest by score."""
    scores = category_scores(user).get(side) or {}
    first = list(first or [])
    rest = sorted((c for c in scores if c not in first), key=lambda c: -scores[c])
    first.sort(key=lambda c: -scores.get(c, 0))
    return first + rest
