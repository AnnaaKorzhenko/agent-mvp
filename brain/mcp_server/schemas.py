"""Typed tool results. The SDK publishes these as each tool's output schema (structured content)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Channel(BaseModel):
    username: str
    country: str | None = Field(None, description="UA or RU")


class PostOut(BaseModel):
    post_id: str
    summary: str
    tags: list[str]
    category: str
    source_side: Literal["ua", "ru"] | str
    is_russian_source_claim: bool = Field(description="True: a claim of a Russian source, not a verified fact")
    channel: Channel | None
    post_url: str
    published_at: str
    importance: int


class SearchResult(BaseModel):
    query: str
    search_words: list[str] = Field(description="Word stems actually searched for")
    days: int
    results: list[PostOut]
    note: str | None = None


class DigestItemOut(BaseModel):
    n: int | None = Field(None, description="Item number; use it for 'more detail on item N'")
    text: str
    source_urls: list[str]
    source_post_ids: list[str] = Field(description="Ids usable with get_post and record_feedback")
    is_russian_source_claim: bool


class DigestSectionOut(BaseModel):
    category: str
    title: str | None
    items: list[DigestItemOut]


class DigestOut(BaseModel):
    side: Literal["ua", "ru"] | str
    side_note: str | None = None
    digest_id: str
    created_at: str
    period_start: str | None
    period_end: str | None
    sections: list[DigestSectionOut]
    hidden_items: int = Field(0, description="Items withheld because this connection may not receive them")


class DigestResult(BaseModel):
    date: str | None = Field(description="Kyiv date of the digests (YYYY-MM-DD)")
    digests: list[DigestOut]
    missing_sides: list[str] = Field(description="Sides with no digest for that date")
    note: str | None = None


class CategoryScore(BaseModel):
    side: str
    category: str
    score: float


class TagScore(BaseModel):
    tag: str
    score: float


class ProfileOut(BaseModel):
    categories_today: dict[str, list[str]] = Field(description="Categories the user picked this morning, per side")
    goals: list[str]
    top_categories: list[CategoryScore]
    top_tags: list[TagScore]


class GoalsOut(BaseModel):
    goals: list[str] = Field(description="The user's active goals after the change")
    added: str | None = None
    removed: str | None = None


class FeedbackOut(BaseModel):
    post_id: str
    reaction: Literal["like", "dislike", "skip"]
    replaced_previous: str | None = Field(None, description="The earlier reaction from an AI client, if replaced")
    score_changes: dict[str, float]
