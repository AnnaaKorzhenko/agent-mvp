"""The user edits their own profile on «Профіль та інтереси» (brain/domain/profile_edits.py, POST /api/profile/edit,
POST /api/goals/edit, a new topic by name): the learned summary, preferences, interests, known and avoid change at
once in users.learned, and each edit is kept as an explicit insight «Зміна профілю: …» for the learning job."""
from __future__ import annotations

import copy
import itertools
import re
from datetime import datetime, timedelta, timezone

import pytest

from brain.agents.learner_profile import SYSTEM
from brain.domain import interests
from brain.web.app import FRONTEND
from tests.fixtures.sample_data import USERS
from tests.test_digest_news_shape import web

_ids = itertools.count(981_000_001)
WEEK_AGO = datetime.now(timezone.utc) - timedelta(days=7)


def learned(**more):
    return {"summary": "Аналітик оборонного фонду.", "preferences": ["Коротко", "З посиланнями"],
            "known": ["Як працює Brave1"], "avoid": ["космос"], "intents": [], "goal_candidates": [],
            "interests": [{"topic": "РЕБ", "weight": 3, "origin": "inferred", "web_queries": ["РЕБ «Буковель»"],
                           "first_seen": WEEK_AGO, "last_seen": WEEK_AGO, "evidence": {"rating": 3},
                           "days": ["2026-11-10", "2026-11-11"], "confirmed": True}],
            "sensitivity": "internal", "updated_at": WEEK_AGO, **more}


@pytest.fixture
async def world(test_db):
    db = test_db.client[f"{test_db.name}_pe{next(_ids)}"]
    uid = next(_ids)
    await db.users.insert_one({**copy.deepcopy(USERS[0]), "_id": uid, "goals": [], "feedback": [], "insights": [],
                               "learned": learned()})
    yield db, uid
    await test_db.client.drop_database(db.name)


async def edit(w, **body):
    return await w.post("/api/profile/edit", json=body)


async def notes(db, uid):
    return [i["text"] for i in (await db.users.find_one({"_id": uid}))["insights"]]


async def test_the_profile_carries_what_can_be_edited(world):
    db, uid = world
    async with web(db, uid) as w:
        body = (await w.get("/api/profile")).json()
    assert body["editable"] == {"summary": "Аналітик оборонного фонду.", "preferences": ["Коротко", "З посиланнями"],
                                "known": ["Як працює Brave1"], "avoid": ["космос"], "interests": ["РЕБ"]}


async def test_a_list_item_is_added_changed_and_removed_and_each_edit_is_noted(world):
    db, uid = world
    async with web(db, uid) as w:
        r = await edit(w, field="preferences", action="add", text="  Українською  ")
        assert r.status_code == 200 and r.json()["editable"]["preferences"] == ["Коротко", "З посиланнями",
                                                                                "Українською"]
        r = await edit(w, field="preferences", action="edit", old="Коротко", text="Дуже коротко")
        assert r.json()["editable"]["preferences"][0] == "Дуже коротко"
        r = await edit(w, field="preferences", action="remove", old="З посиланнями")
        assert r.json()["editable"]["preferences"] == ["Дуже коротко", "Українською"]
        # what can't be done, in the UI's words
        dup = await edit(w, field="preferences", action="add", text="українською")
        assert dup.status_code == 400 and "вже є" in dup.json()["detail"]
        gone = await edit(w, field="known", action="remove", old="Чого немає")
        assert gone.status_code == 404 and "оновіть сторінку" in gone.json()["detail"]
        assert (await edit(w, field="avoid", action="set", text="x")).status_code == 400
        assert (await edit(w, field="goals", action="add", text="x")).status_code == 422
    doc = await db.users.find_one({"_id": uid})
    assert doc["learned"]["preferences"] == ["Дуже коротко", "Українською"]
    assert doc["learned"]["summary"] == "Аналітик оборонного фонду."                         # the rest untouched
    assert await notes(db, uid) == [
        "Зміна профілю: як отримувати новини — додав «Українською»",
        "Зміна профілю: як отримувати новини — змінив «Коротко» на «Дуже коротко»",
        "Зміна профілю: як отримувати новини — прибрав «З посиланнями»"]
    assert {(i["kind"], i["origin"]) for i in doc["insights"]} == {("preference", "explicit")}


async def test_a_full_list_refuses_one_more(world):
    db, uid = world
    await db.users.update_one({"_id": uid}, {"$set": {"learned.avoid": [f"тема {n}" for n in range(10)]}})
    async with web(db, uid) as w:
        r = await edit(w, field="avoid", action="add", text="ще одна")
    assert r.status_code == 400 and "щонайбільше 10" in r.json()["detail"]


async def test_the_summary_is_set_or_cleared(world):
    db, uid = world
    async with web(db, uid) as w:
        r = await edit(w, field="summary", action="set", text="Керую закупівлями дронів у фонді.")
        assert r.json()["editable"]["summary"] == "Керую закупівлями дронів у фонді."
        assert (await edit(w, field="summary", action="set", text="x" * 701)).status_code == 422
        assert (await edit(w, field="summary", action="set", text="")).json()["editable"]["summary"] == ""
    assert (await notes(db, uid))[0] == "Зміна профілю: коротко про мене — «Керую закупівлями дронів у фонді.»"


async def test_an_added_interest_counts_at_once_and_a_renamed_one_keeps_its_history(world):
    db, uid = world
    async with web(db, uid) as w:
        await edit(w, field="interests", action="add", text="морські дрони")
        await edit(w, field="interests", action="edit", old="РЕБ", text="РЕБ на фронті")
    got = (await db.users.find_one({"_id": uid}))["learned"]
    reb, sea = got["interests"]
    assert (reb["topic"], reb["origin"], reb["web_queries"], reb["evidence"]) == \
        ("РЕБ на фронті", "stated", [], {"rating": 3})             # its searches were about the old name
    assert reb["first_seen"].replace(tzinfo=timezone.utc) == WEEK_AGO.replace(microsecond=reb["first_seen"].microsecond)
    assert (sea["topic"], sea["origin"], sea["weight"], sea["confirmed"]) == ("морські дрони", "stated", 4, True)
    assert [i["topic"] for i, _ in interests.counted(got)] == ["морські дрони", "РЕБ на фронті"]
    async with web(db, uid) as w:
        r = await edit(w, field="interests", action="remove", old="морські дрони")
    assert r.json()["editable"]["interests"] == ["РЕБ на фронті"]
    doc = await db.users.find_one({"_id": uid})
    assert [i["kind"] for i in doc["insights"]] == ["interest"] * 3


async def test_a_first_edit_before_any_learning_run_and_a_profile_this_client_may_not_see(world):
    db, uid = world
    await db.users.update_one({"_id": uid}, {"$unset": {"learned": ""}})
    async with web(db, uid) as w:
        assert (await w.get("/api/profile")).json()["editable"]["preferences"] == []
        r = await edit(w, field="known", action="add", text="Що таке НРК")
        assert r.json()["editable"]["known"] == ["Що таке НРК"]
    assert (await db.users.find_one({"_id": uid}))["learned"]["sensitivity"] == "internal"
    await db.users.update_one({"_id": uid}, {"$set": {"learned": learned(sensitivity="confidential")}})
    async with web(db, uid) as w:
        assert (await w.get("/api/profile")).json()["editable"] is None
        r = await edit(w, field="known", action="add", text="Що таке НРК")
        assert r.status_code == 400 and "локальному клієнті" in r.json()["detail"]


async def test_a_goal_gets_new_wording_and_a_topic_is_followed_by_its_name(world):
    db, uid = world
    async with web(db, uid) as w:
        await w.post("/api/goals", json={"action": "add", "text": "стежити за НРК"})
        r = await w.post("/api/goals/edit", json={"old": "стежити за НРК", "text": "стежити за НРК і РЕБ"})
        assert r.status_code == 200
        assert (await w.get("/api/profile")).json()["goals"] == ["стежити за НРК і РЕБ"]
        r = await w.post("/api/topics", json={"name": "Спектр-К", "action": "follow"})
        assert r.status_code == 200
        assert [t["name"] for t in (await w.get("/api/profile")).json()["topics"]] == ["Спектр-К"]


def test_the_learner_keeps_the_users_own_edits():
    assert "«Зміна профілю:» is the user's own edit of this profile" in SYSTEM
    assert "never bring back what they removed or changed" in SYSTEM


def test_the_profile_screen_edits_the_profile():
    js = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
    for field in ("interests", "preferences", "known", "avoid"):
        assert re.search(rf"\['{field}', '[^']+', '[^']+', \d+\]", js), field
    assert "summaryHtml(mine.summary)" in js and "data-edit-start" in js and "data-edit-remove" in js
    assert "data-form=\"topic\"" in js and "api.editGoal(old, text)" in js
    assert "!t.startsWith(EDIT_NOTE)" in js                    # the edit notes aren't shown back
    api = (FRONTEND / "js" / "api.js").read_text(encoding="utf-8")
    assert "'/profile/edit'" in api and "'/goals/edit'" in api
