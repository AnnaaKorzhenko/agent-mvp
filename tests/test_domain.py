"""Digest order, search ranking, profile, feedback validation and replacement (domain layer, test DB)."""
import copy
import itertools

import pytest
from bson import ObjectId

from brain.domain import digests, feedback, posts, profile, sensitivity
from brain.domain.errors import InvalidInput, NotFound
from brain.domain.identity import UserContext
from tests.fixtures.sample_data import (P_AD, P_DRONES, P_GRANTS, P_OLD, P_RU_CLAIM, P_SECRET, P_TOP, USERS)

_ids = itertools.count(900_000_001)
MCP_CHANNELS = ["claude", "chatgpt", "local", "dev", "unknown"]


@pytest.fixture
async def user(test_db):
    """A fresh copy of the sample user for each test."""
    doc = copy.deepcopy(USERS[0])
    doc["_id"] = next(_ids)
    await test_db.users.insert_one(doc)
    return doc["_id"]


def cloud(uid, channel="claude"):
    return UserContext(uid, "cloud", channel, sensitivity.max_for("cloud"))


def local(uid):
    return UserContext(uid, "local", "local", "restricted")


# ---- digests ---------------------------------------------------------------

async def test_digest_order_by_scores_and_hidden_items(repos, user):
    res = await digests.get_digests(repos, cloud(user), "both")
    ua, ru = res.digests
    assert [s.category for s, _ in ua.sections] == ["defense_tech", "investments"]
    assert ua.hidden_items == 1                                   # the confidential item
    texts = [i.text for _, items in ua.sections for i in items]
    assert "Оцінка постачання дронів." not in texts
    assert ru.side_note == digests.RU_NOTE and ua.side_note is None
    assert res.date == digests.kyiv_today().isoformat() or res.date   # Kyiv date of the digest


async def test_todays_choice_goes_first_only_today(repos, test_db, user):
    await test_db.users.update_one({"_id": user}, {"$set": {"daily.date": digests.kyiv_today().isoformat(),
                                                            "daily.selected": {"ua": ["investments"], "ru": []}}})
    res = await digests.get_digests(repos, cloud(user), "ua")
    assert [s.category for s, _ in res.digests[0].sections] == ["investments", "defense_tech"]


async def test_local_sees_confidential_item(repos, user):
    res = await digests.get_digests(repos, local(user), "ua")
    assert res.digests[0].hidden_items == 0


async def test_digest_by_date(repos, user):
    assert (await digests.get_digests(repos, cloud(user), "ua", "2001-01-01")).missing_sides == ["ua"]
    with pytest.raises(InvalidInput):
        await digests.get_digests(repos, cloud(user), "ua", "yesterday")
    with pytest.raises(InvalidInput):
        await digests.get_digests(repos, cloud(user), "en")


# ---- search ------------------------------------------------------------------

def test_query_words():
    assert posts.query_words("Що змінилося з дронами за тиждень?") == ["дрона"]       # "змінилося" is a stop word
    assert posts.query_words("Brave1 гранти Brave1 РЕБ") == ["brav", "гран", "реб"]
    assert posts.query_words("на Покровському напрямку") == ["покров", "напрям"]
    assert posts.query_words("під Куп’янськом учора") == ["куп'янс"]
    assert posts.query_words("що як де") == []


def test_stem_pattern_matches_word_starts_only():
    import re
    p = posts.stem_pattern("куп'ян")
    assert re.search(p, "бої під куп’янськом") and re.search(p, "Куп'янськ".lower())
    assert not re.search(posts.stem_pattern("ян"), "м’янма")
    assert not re.search(posts.stem_pattern("реб"), "потреби") and re.search(posts.stem_pattern("реб"), "засоби реб")


async def test_search_filters_and_ranks(repos, user):
    views, words = await posts.search(repos, cloud(user), "дрони РЕБ", days=7)
    ids = [v.post.id for v in views]
    assert str(P_DRONES) in ids and str(P_GRANTS) in ids
    assert not {str(P_SECRET), str(P_TOP), str(P_AD), str(P_OLD)} & set(ids)   # labels, ad, too old
    assert all(v.channel and v.channel.username for v in views)
    assert views == sorted(views, key=lambda v: -v.score)
    older, _ = await posts.search(repos, cloud(user), "дрони", days=30)
    assert str(P_OLD) in [v.post.id for v in older]


async def test_search_side_and_claim_flag(repos, user):
    views, _ = await posts.search(repos, cloud(user), "Покровск", side="ru")
    assert [v.post.id for v in views] == [str(P_RU_CLAIM)] and views[0].is_russian_source_claim


async def test_get_post_hides_forbidden_and_missing(repos, user):
    assert (await posts.get_post(repos, cloud(user), str(P_DRONES))).channel.username == "ua_defense_news"
    for bad in (str(P_SECRET), str(ObjectId()), "not-an-id"):
        with pytest.raises(NotFound):
            await posts.get_post(repos, cloud(user), bad)


# ---- profile -------------------------------------------------------------------

async def test_profile(repos, user):
    p = await profile.get_profile(repos, cloud(user))
    assert p.goals == ["Знайти напрямки для інвестицій у defense tech"]          # active only
    assert p.top_categories[0] == ("ua", "defense_tech", 3)
    assert p.top_tags == [("РЕБ", 3), ("дрони", 2)]                              # positive only
    assert p.categories_today == {"ua": [], "ru": []}                            # picker date is not today
    with pytest.raises(NotFound):
        await profile.get_profile(repos, cloud(123))


async def test_profile_projection_never_reads_feedback(repos, user):
    assert "feedback" not in repos.users.PROFILE_PROJECTION
    p = await repos.users.get_profile(user)
    assert not hasattr(p, "feedback")


# ---- feedback ------------------------------------------------------------------

async def _user(test_db, uid):
    return await test_db.users.find_one({"_id": uid})


async def test_feedback_like_then_dislike_replaces(repos, test_db, user):
    r = await feedback.record_feedback(repos, cloud(user), str(P_DRONES), "like", "  дуже   корисно ", MCP_CHANNELS)
    assert r.replaced_previous is None
    assert r.score_changes == {"interests.category_scores.ua.defense_tech": 1,
                               "interests.tag_scores.морські дрони": 1, "interests.tag_scores.Чорне море": 1}
    doc = await _user(test_db, user)
    entry = doc["feedback"][-1]
    assert {k: entry[k] for k in ("post_id", "reaction", "text", "digest_id", "item_n", "side", "category",
                                  "channel")} == {"post_id": P_DRONES, "reaction": "like", "text": "дуже корисно",
                                                  "digest_id": None, "item_n": None, "side": "ua",
                                                  "category": "defense_tech", "channel": "claude"}
    assert doc["interests"]["category_scores"]["ua"]["defense_tech"] == 4

    r = await feedback.record_feedback(repos, cloud(user, "chatgpt"), str(P_DRONES), "dislike", None, MCP_CHANNELS)
    assert r.replaced_previous == "like"
    doc = await _user(test_db, user)
    assert doc["interests"]["category_scores"]["ua"]["defense_tech"] == 2           # 4 −1 (undo) −1
    assert doc["interests"]["tag_scores"]["морські дрони"] == -1
    mcp_entries = [f for f in doc["feedback"] if f.get("post_id") == P_DRONES]
    assert [f["reaction"] for f in mcp_entries] == ["dislike"]
    telegram = [f for f in doc["feedback"] if "channel" not in f]
    assert len(telegram) == 1 and telegram[0]["post_id"] == P_GRANTS                # bot entry untouched


async def test_feedback_never_replaces_telegram_entry(repos, test_db, user):
    await feedback.record_feedback(repos, cloud(user), str(P_GRANTS), "dislike", None, MCP_CHANNELS)
    doc = await _user(test_db, user)
    assert sorted(f["reaction"] for f in doc["feedback"] if f["post_id"] == P_GRANTS) == ["dislike", "like"]


@pytest.mark.parametrize("post_id, error", [(None, InvalidInput), ("", InvalidInput), ("xyz", InvalidInput),
                                            (str(ObjectId()), NotFound), (str(P_SECRET), NotFound)])
async def test_feedback_rejects_bad_posts(repos, test_db, user, post_id, error):
    with pytest.raises(error):
        await feedback.record_feedback(repos, cloud(user), post_id, "like", None, MCP_CHANNELS)
    assert len((await _user(test_db, user))["feedback"]) == 1          # nothing written


async def test_feedback_rejects_bad_reaction_and_unknown_user(repos):
    with pytest.raises(InvalidInput):
        await feedback.record_feedback(repos, cloud(1), str(P_DRONES), "love", None, MCP_CHANNELS)
    with pytest.raises(NotFound):
        await feedback.record_feedback(repos, cloud(424242), str(P_DRONES), "like", None, MCP_CHANNELS)
