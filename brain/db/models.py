"""Read models. Tolerant: unknown fields are ignored and optional fields have safe defaults.

A broken document (missing required field, wrong type) is logged by repositories.parse() and skipped,
never raised to the caller.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from bson import ObjectId
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

Id = Annotated[str, BeforeValidator(lambda v: str(v) if isinstance(v, ObjectId) else v)]


def _as_list(v: Any) -> Any:
    return [] if v is None else v


class _Doc(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class SourceDoc(_Doc):
    id: Id = Field(alias="_id")
    username: str = ""
    platform: str = "telegram"
    source_country: str | None = None
    category: str | None = None


class PostDoc(_Doc):
    id: Id = Field(alias="_id")
    post_url: str
    channel_id: Id | None = None
    published_at: str
    processed: bool = False
    summary: str
    source_side: str
    category: str = "other"
    tags: Annotated[list[str], BeforeValidator(_as_list)] = []
    importance: int = 0
    is_ad: bool = False
    is_duplicate: bool = False
    views: int | None = None
    sensitivity: str = "internal"


class DigestItemDoc(_Doc):
    n: int | None = None
    text: str
    source_urls: Annotated[list[str], BeforeValidator(_as_list)] = []
    source_post_ids: Annotated[list[Id], BeforeValidator(_as_list)] = []


class DigestSectionDoc(_Doc):
    category: str
    title: str | None = None
    items: Annotated[list[DigestItemDoc], BeforeValidator(_as_list)] = []


class DigestDoc(_Doc):
    id: Id = Field(alias="_id")
    period: str
    side: str
    trigger: str | None = None
    created_at: datetime
    period_start: str | None = None
    period_end: str | None = None
    sections: Annotated[list[DigestSectionDoc], BeforeValidator(_as_list)] = []
    sensitivity: str = "internal"


class GoalDoc(_Doc):
    text: str
    active: bool = False
    source: str | None = None
    created_at: datetime | None = None


class IdentityDoc(_Doc):
    provider: str
    subject: str


class DailyDoc(_Doc):
    date: str | None = None
    selected: dict[str, list[str]] = {}


class InterestsDoc(_Doc):
    category_scores: dict[str, dict[str, float]] = {}
    tag_scores: dict[str, float] = {}


class UserProfileDoc(_Doc):
    """users read WITHOUT feedback (see UsersRepo.PROFILE_PROJECTION).

    _id: a Telegram id (int) for bot users, an ObjectId (as hex) for users created for an OAuth login.
    """
    id: int | Id = Field(alias="_id")
    first_name: str | None = None
    daily: DailyDoc | None = None
    interests: InterestsDoc = InterestsDoc()
    goals: Annotated[list[GoalDoc], BeforeValidator(_as_list)] = []
    identities: Annotated[list[IdentityDoc], BeforeValidator(_as_list)] = []

    def as_scoring_user(self) -> dict:
        """The plain dict shape that brain.domain.scoring functions expect."""
        return {"interests": {"category_scores": self.interests.category_scores,
                              "tag_scores": self.interests.tag_scores}}
