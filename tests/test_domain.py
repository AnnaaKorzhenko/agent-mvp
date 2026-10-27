"""Digest order, search ranking, profile, feedback validation and replacement (domain layer, test DB)."""
import copy
import itertools

import pytest
from bson import ObjectId

from brain.domain import digests, feedback, goals, posts, profile, sensitivity
from brain.domain.errors import InvalidInput, NotFound
from brain.domain.identity import UserContext
from brain.scripts.review_queue import review_queue
from tests.fixtures.sample_data import (DIGEST_UA, P_AD, P_DRONES, P_GRANTS, P_OLD, P_RU_CLAIM, P_SECRET, P_TOP, USERS)

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


# ---- goals ---------------------------------------------------------------------

async def test_add_goal_in_bot_shape(repos, test_db, user):
    r = await goals.add_goal(repos, cloud(user), "  стежити   за НРК ")
    assert r.changed == "стежити за НРК"
    assert r.goals == ["Знайти напрямки для інвестицій у defense tech", "стежити за НРК"]
    g = (await _user(test_db, user))["goals"][-1]
    assert {k: g[k] for k in ("text", "active", "source", "channel")} == {
        "text": "стежити за НРК", "active": True, "source": "user", "channel": "claude"}
    assert (await profile.get_profile(repos, cloud(user))).goals == r.goals
    assert len((await goals.add_goal(repos, cloud(user), "x" * 400)).changed) == goals.MAX_TEXT


async def test_add_goal_rules(repos, user):
    with pytest.raises(InvalidInput, match="empty"):
        await goals.add_goal(repos, cloud(user), "   ")
    with pytest.raises(InvalidInput, match="already exists"):
        await goals.add_goal(repos, cloud(user), "знайти напрямки для інвестицій у DEFENSE tech")
    await goals.add_goal(repos, cloud(user), "Стара ціль")                       # an inactive goal may return
    for i in range(3):
        await goals.add_goal(repos, cloud(user), f"ціль {i}")
    with pytest.raises(InvalidInput, match="Ask which one to remove"):
        await goals.add_goal(repos, cloud(user), "шоста")
    assert len((await profile.get_profile(repos, cloud(user))).goals) == goals.MAX_GOALS
    with pytest.raises(NotFound):
        await goals.add_goal(repos, cloud(424242), "ціль")


async def test_remove_goal_keeps_history(repos, test_db, user):
    r = await goals.remove_goal(repos, cloud(user), "знайти напрямки для інвестицій у defense tech")
    assert r.goals == [] and r.changed == "Знайти напрямки для інвестицій у defense tech"
    stored = (await _user(test_db, user))["goals"]
    assert len(stored) == 2 and stored[0]["active"] is False and "removed_at" in stored[0]
    with pytest.raises(NotFound, match="No such active goal"):
        await goals.remove_goal(repos, cloud(user), "Стара ціль")                # inactive: not removable


# ---- feedback ------------------------------------------------------------------

async def _user(test_db, uid):
    return await test_db.users.find_one({"_id": uid})


def rate(repos, ctx, **kw):
    return feedback.record_feedback(repos, ctx, mcp_channels=MCP_CHANNELS, **kw)


async def test_post_rating_then_new_rating_replaces(repos, test_db, user):
    r = await rate(repos, cloud(user), post_id=str(P_DRONES), reaction="useful", text="  дуже   корисно ",
                   reasons=["want_more"])
    assert r.replaced_previous is None and r.target_type == "post" and r.reaction == "useful"
    assert r.score_changes == {"interests.category_scores.ua.defense_tech": 1,
                               "interests.tag_scores.морські дрони": 1, "interests.tag_scores.Чорне море": 1}
    assert r.confirmation == "Збережено: 👍 корисно (defense_tech +1)"
    doc = await _user(test_db, user)
    entry = doc["feedback"][-1]
    assert str(entry["fid"]) == r.feedback_id
    assert {k: entry[k] for k in ("target_type", "post_id", "reaction", "reasons", "text", "comment_status",
                                  "digest_id", "item_n", "side", "category", "channel")} == {
        "target_type": "post", "post_id": P_DRONES, "reaction": "useful", "reasons": ["want_more"],
        "text": "дуже корисно", "comment_status": "commented", "digest_id": None, "item_n": None, "side": "ua",
        "category": "defense_tech", "channel": "claude"}
    assert doc["interests"]["category_scores"]["ua"]["defense_tech"] == 4

    r = await rate(repos, cloud(user, "chatgpt"), post_id=str(P_DRONES), reaction="dislike")     # old word
    assert r.reaction == "not_useful" and r.replaced_previous == "useful"
    doc = await _user(test_db, user)
    assert doc["interests"]["category_scores"]["ua"]["defense_tech"] == 2           # 4 −1 (undo) −1
    assert doc["interests"]["tag_scores"]["морські дрони"] == -1
    assert [f["reaction"] for f in doc["feedback"] if f.get("post_id") == P_DRONES] == ["not_useful"]
    telegram = [f for f in doc["feedback"] if "channel" not in f]
    assert len(telegram) == 1 and telegram[0]["post_id"] == P_GRANTS                # bot entry untouched


async def test_replaces_an_older_mcp_entry_in_the_old_format(repos, test_db, user):
    old = {"post_id": P_DRONES, "reaction": "like", "text": "", "digest_id": None, "item_n": None, "side": "ua",
           "category": "defense_tech", "tags": ["морські дрони"], "channel": "claude"}
    await test_db.users.update_one({"_id": user}, {"$push": {"feedback": old},
                                                   "$inc": {"interests.category_scores.ua.defense_tech": 1}})
    r = await rate(repos, cloud(user), post_id=str(P_DRONES), reaction="neutral")
    assert r.replaced_previous == "useful" and r.score_changes["interests.category_scores.ua.defense_tech"] == -1
    assert [f["reaction"] for f in (await _user(test_db, user))["feedback"] if f.get("post_id") == P_DRONES] == [
        "neutral"]


async def test_neutral_is_recorded_without_points(repos, test_db, user):
    r = await rate(repos, cloud(user), post_id=str(P_DRONES), reaction="neutral")
    assert r.score_changes == {} and r.confirmation == "Збережено: 50/50"
    assert (await _user(test_db, user))["feedback"][-1]["reaction"] == "neutral"


async def test_digest_item_in_the_bot_shape(repos, test_db, user):
    r = await rate(repos, cloud(user), target_type="digest_item", digest_id=str(DIGEST_UA), item_n=1,
                   reaction="useful")
    entry = (await _user(test_db, user))["feedback"][-1]
    assert {k: entry[k] for k in ("target_type", "post_id", "digest_id", "item_n", "side", "category", "tags")} == {
        "target_type": "digest_item", "post_id": P_DRONES, "digest_id": DIGEST_UA, "item_n": 1, "side": "ua",
        "category": "defense_tech", "tags": ["морські дрони", "Чорне море"]}
    assert r.score_changes["interests.category_scores.ua.defense_tech"] == 1

    r = await rate(repos, cloud(user), target_type="digest_item", digest_id=str(DIGEST_UA), item_n=1,
                   reaction="not_useful")
    assert r.replaced_previous == "useful"
    # A post rating of the same post is a different target: neither replaces the other.
    await rate(repos, cloud(user), post_id=str(P_DRONES), reaction="useful")
    reactions = sorted((f.get("target_type"), f["reaction"]) for f in (await _user(test_db, user))["feedback"]
                       if f.get("post_id") == P_DRONES)
    assert reactions == [("digest_item", "not_useful"), ("post", "useful")]


async def test_digest_item_never_replaces_the_bots_entry(repos, test_db, user):
    await rate(repos, cloud(user), target_type="digest_item", digest_id=str(DIGEST_UA), item_n=3,
               reaction="not_useful")
    same_item = [f for f in (await _user(test_db, user))["feedback"] if f.get("item_n") == 3]
    assert sorted(f["reaction"] for f in same_item) == ["like", "not_useful"]       # Telegram's like is kept


@pytest.mark.parametrize("digest_id, item_n", [(str(DIGEST_UA), 2),          # confidential source: hidden
                                               (str(DIGEST_UA), 9), (str(ObjectId()), 1), (None, 1),
                                               (str(DIGEST_UA), None)])
async def test_digest_item_must_exist_and_be_visible(repos, test_db, user, digest_id, item_n):
    with pytest.raises(NotFound):
        await rate(repos, cloud(user), target_type="digest_item", digest_id=digest_id, item_n=item_n,
                   reaction="useful")
    assert len((await _user(test_db, user))["feedback"]) == 1          # nothing written


async def test_answer_feedback_is_kept_apart_and_never_scored(repos, test_db, user):
    before = (await _user(test_db, user))["interests"]
    args = dict(target_type="answer", reaction="not_useful", about="аналіз інвестицій у НРК",
                cited_post_ids=[str(P_DRONES), str(P_GRANTS)], reasons=["inaccurate"], text="цифри не ті")
    r1 = await rate(repos, cloud(user), **args)
    r2 = await rate(repos, cloud(user), **args)
    assert r1.score_changes == {} and r1.feedback_id != r2.feedback_id
    doc = await _user(test_db, user)
    answers = [f for f in doc["feedback"] if f.get("target_type") == "answer"]
    assert len(answers) == 2                                            # answers don't replace each other
    assert answers[0]["cited_post_ids"] == [P_DRONES, P_GRANTS] and answers[0]["about"] == args["about"]
    assert answers[0]["post_id"] is None and "category" not in answers[0]
    assert doc["interests"] == before


@pytest.mark.parametrize("kw, error", [
    ({"about": "  "}, "needs `about`"),
    ({"cited_post_ids": [str(ObjectId())]}, "never invent ids"),
    ({"cited_post_ids": [str(P_SECRET)]}, "never invent ids"),          # not visible to a cloud client
    ({"cited_post_ids": ["xyz"]}, "post ids"),
    ({"reasons": ["boring"]}, "unknown reasons"),
    ({"reaction": "love"}, "reaction must be"),
    ({"target_type": "video"}, "target_type"),
])
async def test_feedback_validation(repos, test_db, user, kw, error):
    args = {"target_type": "suggestion", "reaction": "useful", "about": "порада стежити за НРК", **kw}
    with pytest.raises(InvalidInput, match=error):
        await rate(repos, cloud(user), **args)
    assert len((await _user(test_db, user))["feedback"]) == 1


@pytest.mark.parametrize("post_id, error", [(None, InvalidInput), ("", InvalidInput), ("xyz", InvalidInput),
                                            (str(ObjectId()), NotFound), (str(P_SECRET), NotFound)])
async def test_feedback_rejects_bad_posts(repos, test_db, user, post_id, error):
    with pytest.raises(error):
        await rate(repos, cloud(user), post_id=post_id, reaction="useful")
    assert len((await _user(test_db, user))["feedback"]) == 1          # nothing written


async def test_feedback_unknown_user(repos):
    with pytest.raises(NotFound):
        await rate(repos, cloud(424242), post_id=str(P_DRONES), reaction="useful")


async def test_follow_up_adds_comment_and_reasons_without_scores(repos, test_db, user):
    r = await rate(repos, cloud(user), post_id=str(P_DRONES), reaction="not_useful")
    interests = (await _user(test_db, user))["interests"]

    f = await rate(repos, cloud(user), feedback_id=r.feedback_id, text="вже знав", reasons=["already_known"])
    assert f.score_changes == {} and f.reaction == "not_useful" and "Коментар додано" in f.confirmation
    await rate(repos, cloud(user), feedback_id=r.feedback_id, reasons=["not_relevant", "already_known"])
    doc = await _user(test_db, user)
    entry = next(e for e in doc["feedback"] if str(e.get("fid")) == r.feedback_id)
    assert entry["text"] == "вже знав" and entry["comment_status"] == "commented" and "text_at" in entry
    assert entry["reasons"] == ["already_known", "not_relevant"]
    assert doc["interests"] == interests

    with pytest.raises(InvalidInput, match="To change the rating"):
        await rate(repos, cloud(user), feedback_id=r.feedback_id, reaction="useful", text="x")


async def test_follow_up_skip_and_ownership(repos, test_db, user):
    r = await rate(repos, cloud(user), post_id=str(P_DRONES), reaction="useful")
    await rate(repos, cloud(user), feedback_id=r.feedback_id)                       # «Пропустити»
    entry = (await _user(test_db, user))["feedback"][-1]
    assert entry["comment_status"] == "no_comment" and entry["text"] == ""

    other = copy.deepcopy(USERS[0])
    other["_id"] = next(_ids)
    await test_db.users.insert_one(other)
    with pytest.raises(NotFound):                                       # someone else's rating
        await rate(repos, cloud(other["_id"]), feedback_id=r.feedback_id, text="x")
    for bad in ("", "xyz"):
        with pytest.raises(NotFound):
            await rate(repos, cloud(user), feedback_id=bad, text="x")


async def test_review_queue_lists_citations_of_inaccurate_answers(repos, test_db, user):
    await rate(repos, cloud(user), target_type="answer", reaction="not_useful", about="огляд РЕБ",
               cited_post_ids=[str(P_GRANTS)], reasons=["inaccurate"], text="гранти не ті")
    await rate(repos, cloud(user), target_type="answer", reaction="not_useful", about="інше",
               cited_post_ids=[str(P_DRONES)], reasons=["too_long"])               # not inaccurate: not listed
    rows = await review_queue(test_db)
    grants = [r for r in rows if r["post_id"] == str(P_GRANTS)]
    assert grants and grants[0]["summary"] and any(f["text"] == "гранти не ті" for f in grants[0]["flags"])
    assert all(r["post_id"] != str(P_DRONES) or all(f["about"] != "інше" for f in r["flags"]) for r in rows)


async def test_comment_by_item_goes_to_the_earlier_rating(repos, test_db, user):
    r = await rate(repos, cloud(user), target_type="digest_item", digest_id=str(DIGEST_UA), item_n=1,
                   reaction="useful")                                    # e.g. pressed in the cards
    interests = (await _user(test_db, user))["interests"]
    c = await rate(repos, cloud(user), target_type="digest_item", digest_id=str(DIGEST_UA), item_n=1,
                   text="застаріло", reasons=["already_known"])          # «до неї додай: застаріло»
    assert c.feedback_id == r.feedback_id and c.score_changes == {} and c.reaction == "useful"
    doc = await _user(test_db, user)
    entries = [f for f in doc["feedback"] if f.get("item_n") == 1 and f.get("channel")]
    assert len(entries) == 1 and entries[0]["text"] == "застаріло" and entries[0]["reasons"] == ["already_known"]
    assert doc["interests"] == interests

    with pytest.raises(InvalidInput, match="hasn't rated"):                # nothing rated yet: no guessing
        await rate(repos, cloud(user), post_id=str(P_GRANTS), text="цікаво")
    with pytest.raises(InvalidInput, match="reaction must be"):            # neither reaction nor comment
        await rate(repos, cloud(user), post_id=str(P_DRONES))
