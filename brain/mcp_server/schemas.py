"""Typed tool results. The SDK publishes these as each tool's output schema (structured content)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Channel(BaseModel):
    username: str
    country: str | None = Field(None, description="UA or RU")


AboutUser = dict[str, list[str]]
ABOUT_USER_HELP = ("What News brain knows about the user, by kind: summary (learned from all their activity), "
                   "preference, goal, interest, known, avoid, fact, feedback. Follow their preferences, skip what "
                   "they already know and what they avoid, connect news to their goals and interests")


class MemoryOut(BaseModel):
    """On every tool result: what the server learned from this call's user_message / signals."""
    goals_to_offer: list[str] = Field(default_factory=list, description=(
        "Goals detected in the user's messages that aren't active yet: ask the user once whether to add them "
        "(set_goal)"))
    about_user: AboutUser | None = Field(None, description=(
        "Only on the first call of a conversation: " + ABOUT_USER_HELP))


WHY_HELP = ("Why this matters to this user (their goals, interests, rated topics), strongest first; empty if "
            "nothing personal matches. Use it to explain relevance and to lead with goal-related items")


class PostOut(BaseModel):
    ref: int | None = Field(None, description="Number shown to the user in this result (#1, #2, …): «третя» = ref 3")
    why: list[str] = Field(default_factory=list, description=WHY_HELP)
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


class SearchResult(MemoryOut):
    query: str
    search_words: list[str] = Field(description="Word stems actually searched for")
    days: int
    results: list[PostOut]
    note: str | None = None
    shown_in_widget: bool = Field(False, description="True: the user already sees these results as cards with "
                                                     "links; cite them as #N instead of listing them")


class DigestItemOut(BaseModel):
    ref: int = Field(description="Number shown to the user, unique across this whole result (#1, #2, …): "
                                 "«третя» = ref 3")
    n: int = Field(description="The item's number inside its digest; pass it as item_n to record_feedback")
    text: str
    source_urls: list[str]
    source_post_ids: list[str] = Field(description="Ids usable with get_post and record_feedback")
    is_russian_source_claim: bool
    why: list[str] = Field(default_factory=list, description=WHY_HELP)
    relevance: float = Field(0.0, description="Personal relevance score; items are already ordered by it")


class DigestSectionOut(BaseModel):
    category: str
    title: str | None
    items: list[DigestItemOut]


class DigestOut(BaseModel):
    side: Literal["ua", "ru", "topics"] | str = Field(description=(
        "topics: today's news on the topics the user follows (sections per topic), shown first"))
    side_note: str | None = None
    digest_id: str
    created_at: str
    period_start: str | None
    period_end: str | None
    sections: list[DigestSectionOut]
    hidden_items: int = Field(0, description="Items withheld because this connection may not receive them")


class DigestResult(MemoryOut):
    date: str | None = Field(description="Kyiv date of the digests (YYYY-MM-DD)")
    digests: list[DigestOut]
    missing_sides: list[str] = Field(description="Sides with no digest for that date")
    note: str | None = None
    shown_in_widget: bool = Field(False, description="True: the user already sees this digest as an interactive "
                                                     "widget; do not repeat or summarize it in your reply")


class MorningDigestResult(DigestResult):
    status: Literal["new", "not_ready", "already_sent"] = Field(description=(
        "new: today's digest, delivered now (present it); not_ready: no complete digest for today yet; "
        "already_sent: today's digest was delivered before (don't present it again)"))


class PostDetail(PostOut, MemoryOut):
    """get_post's result: the post plus the memory fields."""


class CategoryScore(BaseModel):
    side: str
    category: str
    score: float


class TagScore(BaseModel):
    tag: str
    score: float


class InsightCount(BaseModel):
    saved: int = Field(description="Insights the user has saved")
    visible_here: int = Field(description="Of those, how many this connection may read with get_insights")


class ProfileOut(MemoryOut):
    categories_today: dict[str, list[str]] = Field(description="Categories the user picked this morning, per side")
    goals: list[str]
    top_categories: list[CategoryScore]
    top_tags: list[TagScore]
    insights: InsightCount = InsightCount(saved=0, visible_here=0)
    topics: list["TopicOut"] = Field(default_factory=list, description="Specific things the user follows or dug "
                                                                      "into (track_topic / save_findings)")
    about_user: AboutUser = Field(default_factory=dict, description=ABOUT_USER_HELP)


SignalKind = Literal["goal", "interest", "preference", "fact", "feedback", "known"]


class Signal(BaseModel):
    """Something durable the user revealed in a message (input to log_turn)."""
    kind: SignalKind = Field(description=(
        "goal: what they want to achieve; interest: topics they care about; preference: how they want answers "
        "or news (length, language, format, what to skip); fact: about their work, organization, people, "
        "situation; feedback: about your answers or the service; known: something they already know"))
    text: str = Field(min_length=2, max_length=500, description=(
        "The user's own words for it (quote them; a minimal paraphrase only if needed), in their language"))
    private: bool = Field(False, description="True only if the user said it's private / not to be recalled")


class TurnLogged(MemoryOut):
    saved: int = Field(description="Signals saved as new insights")
    duplicates: int = Field(0, description="Signals already saved before")


class InsightSaved(MemoryOut):
    insight_id: str
    sensitivity: Literal["public", "internal", "confidential", "restricted"] | str
    visible_here: bool = Field(description="False: saved, but this connection can't read it back (its label is "
                                           "above what this connection may receive)")
    confirmation: str = Field(description="Short Ukrainian confirmation to tell the user")


class InsightOut(BaseModel):
    insight_id: str
    text: str = Field(description="The user's own words, verbatim")
    area: str | None
    tags: list[str]
    sensitivity: str
    created_at: str | None


class InsightsResult(MemoryOut):
    insights: list[InsightOut]
    hidden: int = Field(0, description="Matching insights this connection may not read")
    note: str | None = None


class GoalsOut(MemoryOut):
    goals: list[str] = Field(description="The user's active goals after the change")
    added: str | None = None
    removed: str | None = None


class FeedbackOut(MemoryOut):
    feedback_id: str = Field(description="Id of this rating; pass it back to add the user's comment or reasons")
    target_type: Literal["post", "digest_item", "answer", "suggestion"] | str
    reaction: Literal["useful", "not_useful", "neutral"] | str
    replaced_previous: str | None = Field(None, description="The earlier reaction from an AI client, if replaced")
    score_changes: dict[str, float]
    confirmation: str = Field(description="Short Ukrainian confirmation to tell the user")
    offer_deep_dive: bool = Field(False, description=(
        "True: the user asked for more on the rated item. Offer (or, if they plainly asked, start) a deep dive: "
        "deep_dive, then a web search"))


class FindingIn(BaseModel):
    """One thing found on the web (input to save_findings)."""
    title: str = Field(min_length=2, max_length=200, description="The page's or article's title")
    url: str = Field(min_length=8, max_length=500, description="The exact URL it came from; never invent one")
    source: str | None = Field(None, max_length=80, description="Who published it, e.g. 'Defense Express', "
                                                                 "'ISW', a Telegram channel")
    date: str | None = Field(None, max_length=20, description="Publication date YYYY-MM-DD, if known")
    summary: str = Field(min_length=2, max_length=400, description="One or two sentences: what it adds")
    status: Literal["confirmed", "claim", "unclear"] = Field("unclear", description=(
        "confirmed: independent / official confirmation; claim: one side's claim (Russian sources are claims); "
        "unclear: can't tell"))


class FindingOut(BaseModel):
    title: str
    url: str
    source: str | None
    date: str | None
    summary: str
    status: str


class TopicOut(BaseModel):
    name: str
    status: Literal["explored", "active", "expired", "stopped"] | str = Field(description=(
        "explored: dug into, not followed; active: followed in digests and search; expired / stopped: no longer"))
    expires_at: str | None = None
    goal: str | None = None
    specific: list[str] = Field(default_factory=list, description="Names and aliases matched first")
    broader: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    findings_count: int = 0
    findings: list[FindingOut] = Field(default_factory=list, description="Saved earlier: build on them, don't "
                                                                         "search for them again")


class DeepDiveSubject(BaseModel):
    text: str
    source_urls: list[str]
    is_russian_source_claim: bool
    digest_id: str | None = None
    item_n: int | None = None
    post_id: str | None = None


class DeepDiveResult(MemoryOut):
    subject: DeepDiveSubject
    source_posts: list[PostOut] = Field(description="The subject's own source posts still in the news base")
    related_posts: list[PostOut] = Field(description="Other posts in the news base on the same thing (30 days)")
    searched_for: list[str] = Field(description="Names the news base was searched for")
    topic: TopicOut | None = Field(None, description="The user's existing topic on this, with earlier findings")
    goals: list[str] = Field(description="The user's active goals: say which ones this serves")
    next_steps: str


class TopicSaved(MemoryOut):
    topic: TopicOut
    created: bool
    added_findings: int = 0
    duplicate_findings: int = Field(0, description="Findings skipped: their URL was already saved")
    unknown_tags: list[str] = Field(default_factory=list, description="Dropped: not in the fixed tag list")
    missing_regions: list[str] = Field(default_factory=list, description=(
        "Regions (ua / ru / world) no name of the topic reaches, so the web search there finds nothing: call "
        "track_topic again adding those spellings (world: Latin; ua: Ukrainian; ru: Russian)"))
    confirmation: str = Field(description="Short Ukrainian confirmation to tell the user")
