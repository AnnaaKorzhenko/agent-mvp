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


class InsightDoc(_Doc):
    """users.insights[]: what the user shared (save_insight) or the client model detected (log_turn)."""
    id: Id
    kind: str = "other"
    origin: str = "explicit"
    text: str
    area: str | None = None
    tags: Annotated[list[str], BeforeValidator(_as_list)] = []
    sensitivity: str = "internal"
    created_at: datetime | None = None
    channel: str | None = None
    conversation_id: Id | None = None
    message_id: Id | None = None


class InsightLabelDoc(_Doc):
    """What get_profile reads of an insight: only its label, for the count (never the text)."""
    sensitivity: str = "confidential"


class FindingDoc(_Doc):
    """users.topics[].findings[]: one thing found on the web during a deep dive."""
    title: str
    url: str
    source: str | None = None
    date: str | None = None
    summary: str = ""
    status: str = "unclear"          # confirmed | claim | unclear
    at: datetime | None = None


class TopicOriginDoc(_Doc):
    digest_id: Id | None = None
    item_n: int | None = None
    post_id: Id | None = None
    text: str = ""


class TopicDoc(_Doc):
    """users.topics[]: one specific thing the user follows now (brain/domain/topics.py)."""
    id: Id
    name: str
    norm: str
    status: str = "explored"         # explored | active | stopped (active past expires_at reads as expired)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    channel: str | None = None
    origin: TopicOriginDoc | None = None        # the item that started it
    goal: str | None = None
    specific: Annotated[list[str], BeforeValidator(_as_list)] = []
    broader: Annotated[list[str], BeforeValidator(_as_list)] = []
    exclude: Annotated[list[str], BeforeValidator(_as_list)] = []
    queries: Annotated[list[str], BeforeValidator(_as_list)] = []    # web searches for the pipeline
    tags: Annotated[list[str], BeforeValidator(_as_list)] = []
    findings: Annotated[list[FindingDoc], BeforeValidator(_as_list)] = []
    findings_count: int | None = None   # set by light reads, which leave the findings out
    hits: Annotated[list[dict], BeforeValidator(_as_list)] = []
    last_hit_at: datetime | None = None
    renew_offered_on: str | None = None      # Kyiv date the client was last asked to offer renewal
    sensitivity: str = "internal"


class ProposalActionDoc(_Doc):
    tool: str                        # what a yes does: set_goal | track_topic (brain/domain/proposals.py)
    args: dict = {}


class ProposalDoc(_Doc):
    """users.proposals[]: something an agent suggests to the user; nothing changes until they say yes."""
    id: Id
    kind: str                        # goal | topic
    key: str                         # dedupe: the same thing is never proposed twice, whatever the answer was
    title: str
    why: str = ""
    action: ProposalActionDoc | None = None
    agent: str = ""
    status: str = "open"             # open | accepted | dismissed | stale
    created_at: datetime | None = None
    expires_at: datetime | None = None
    shown_at: datetime | None = None
    shown_count: int = 0
    answered_at: datetime | None = None
    sensitivity: str = "internal"


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
    insights: Annotated[list[InsightLabelDoc], BeforeValidator(_as_list)] = []

    def as_scoring_user(self) -> dict:
        """The plain dict shape that brain.domain.scoring functions expect."""
        return {"interests": {"category_scores": self.interests.category_scores,
                              "tag_scores": self.interests.tag_scores}}
