"""Personal relevance: which signals count, how strongly, what `why` says, and that the digest and search
follow it."""
import copy
import itertools

import pytest

from brain.db.repositories import Repos
from brain.domain import digests, insights, posts, relevance, sensitivity
from brain.domain.identity import UserContext
from tests.fixtures.sample_data import USERS

_ids = itertools.count(980_000_001)


@pytest.fixture
async def user(test_db):
    uid = next(_ids)
    doc = copy.deepcopy(USERS[0])
    doc.update(_id=uid, goals=[], insights=[], interests={"category_scores": {"ua": {}, "ru": {}}, "tag_scores": {}})
    await test_db.users.insert_one(doc)
    return uid


def cloud(uid):
    return UserContext(uid, "cloud", "claude", sensitivity.max_for("cloud"))


async def set_user(db, uid, **fields):
    await db.users.update_one({"_id": uid}, {"$set": fields})


async def test_no_signals_means_no_effect(repos, user):
    prefs = await relevance.load(repos, cloud(user))
    assert prefs.empty and prefs.score(side="ua", category="front", text="будь-що").score == 0


async def test_goals_count_most_and_come_first_in_why(repos, test_db, user):
    await set_user(test_db, user, goals=[{"text": "знайти 3 НРК-компанії для інвестиції до кінця року",
                                          "active": True}],
                   interests={"category_scores": {"ua": {"defense_tech": 3, "front": -1}}, "tag_scores": {"РЕБ": 2}})
    prefs = await relevance.load(repos, cloud(user))
    goal = prefs.score(side="ua", category="industry", text="Компанія виробляє НРК, шукає інвестиції у розвиток")
    cat = prefs.score(side="ua", category="defense_tech", category_label="🛠 Інновації", text="Новий дрон")
    tag = prefs.score(side="ua", category="industry", text="Нова система", tags=["РЕБ"])
    other = prefs.score(side="ua", category="front", text="Обстріли на фронті")
    assert goal.score > cat.score > tag.score > 0 > other.score
    assert goal.why == ["ціль: знайти 3 НРК-компанії для інвестиції до кінця року"]
    assert cat.why == ["категорія: 🛠 Інновації"] and tag.why == ["тема, яку ви оцінювали: РЕБ"]
    # Generic words of a goal ("знайти", "року", "компанії") don't make everything match.
    assert prefs.score(side="ua", category="industry", text="Компанія звітує про результати року").score == 0


async def test_learned_interests_avoid_and_known(repos, test_db, user):
    await set_user(test_db, user, learned={
        "sensitivity": "internal", "interests": [{"topic": "морські дрони", "weight": 5},
                                                 {"topic": "кібербезпека", "weight": 1}],
        "avoid": ["футбол"], "known": ["Brave1"], "goal_candidates": []})
    prefs = await relevance.load(repos, cloud(user))
    strong = prefs.score(side="ua", category="x", text="Морські дрони уразили катер")
    weak = prefs.score(side="ua", category="x", text="Кібербезпека банків")
    assert strong.score > weak.score > 0 and strong.why == ["інтерес: морські дрони"]
    assert prefs.score(side="ua", category="x", text="Футбол: результати").score < 0
    assert -1 < prefs.score(side="ua", category="x", text="Brave1 видав гранти").score < 0
    # A profile learned from confidential data is not used for a cloud client.
    await set_user(test_db, user, **{"learned.sensitivity": "confidential"})
    assert (await relevance.load(repos, cloud(user))).empty


async def test_detected_insights_count_unless_private(repos, user):
    await insights.save_insight(repos, cloud(user), "цікавлять морські дрони", kind="interest", origin="detected")
    await insights.save_insight(repos, cloud(user), "РЕБ на кораблях", kind="interest", label="confidential")
    prefs = await relevance.load(repos, cloud(user))
    assert prefs.score(side="ua", category="x", text="Морські дрони атакували").score > 0
    assert prefs.score(side="ua", category="x", text="РЕБ на кораблях флоту").score == 0


async def test_digest_follows_the_users_goals(repos, test_db, user):
    plain = await digests.get_digests(repos, cloud(user), "ua")
    base = [i.text for _, items in plain.digests[0].sections for i in items]
    await set_user(test_db, user, goals=[{"text": "стежити за грантами Brave1", "active": True}])
    res = await digests.get_digests(repos, cloud(user), "ua")
    v = res.digests[0]
    ordered = [i for _, items in v.sections for i in items]
    assert ordered[0].text == "Brave1 видав гранти на РЕБ." and base[0] != ordered[0].text
    assert v.why(ordered[0]).why == ["ціль: стежити за грантами Brave1"]


async def test_search_ranks_and_explains(repos, test_db, user):
    await set_user(test_db, user, goals=[{"text": "морські дрони Чорне море", "active": True}])
    views, _ = await posts.search(repos, cloud(user), "дрони РЕБ", days=30)
    top = views[0]
    assert top.why == ["ціль: морські дрони Чорне море"] and "морські дрони" in top.post.summary.lower()


async def test_a_goal_matches_at_three_levels_best_one_counts(repos, test_db, user):
    goal = "Follow new Russian missile and drone variants (e.g. Geran/Shahed evolutions)"
    await set_user(test_db, user, goals=[{"text": goal, "active": True}], learned={
        "sensitivity": "internal",
        "goal_keywords": [{"goal": goal, "specific": ["Герань", "Шахед"],
                           "broader": ["ударні дрони", "ударные беспилотники"], "tags": ["оборонні технології"]},
                          {"goal": "an old removed goal", "specific": ["футбол"], "broader": [], "tags": []}]})
    prefs = await relevance.load(repos, cloud(user))
    s = prefs.score(side="ru", category="ru_tech", text="Рос. каналы утверждают, что новая Герань-3 и Шахед летят")
    b = prefs.score(side="ru", category="ru_tech", text="Ударные беспилотники атакуют энергетику")
    t = prefs.score(side="ua", category="defense_tech", text="Новий тепловізор", tags=["оборонні технології"])
    none = prefs.score(side="ua", category="x", text="Футбол: результати")
    assert s.score == 2.0 and b.score == 1.2 and t.score == 0.7 and none.score == 0   # one goal: its best level only
    assert s.why == [f"ціль: {goal[:49]}… («Герань»)"]
    assert b.why[0].endswith("(«ударные беспилотники»)") and t.why[0].endswith("(тема «оборонні технології»)")
