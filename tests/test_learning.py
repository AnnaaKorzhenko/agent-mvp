"""The learning job: what it collects (new only, allowed labels only), how it reads the model's answer, what it
saves, and how the learned profile reaches the client model. A fake backend stands in for the API."""
import copy
import itertools
import json
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from brain.audit.conversations import CallRecord, ConversationLog, ensure_conversation_indexes
from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.domain import insights, sensitivity
from brain.domain.identity import UserContext
from brain.integrations.backends import ModelResult, _AuditedBackend
from brain.learning import profile
from tests.fixtures.sample_data import USERS

_ids = itertools.count(1)

ANSWER = {"summary": "Працює у фонді, що інвестує в НРК.", "preferences": ["коротші відповіді"],
          "interests": [{"topic": "НРК", "weight": 5}, {"topic": "морські дрони", "weight": 2}],
          "known": ["Brave1"], "avoid": ["новини про спорт"], "goal_candidates": ["знайти 3 НРК-компанії"],
          "goal_keywords": [{"goal": "Знайти напрямки для інвестицій у defense tech",
                             "specific": ["Brave1", "НРК"], "broader": ["інвестиції в оборонку"],
                             "tags": ["інвестиції", "не з переліку"]}]}


class FakeBackend(_AuditedBackend):
    """Answers from a script; the base class enforces the label limit and writes audit events."""
    name = "fake"
    trust = "cloud"

    def __init__(self, audit, answers):
        super().__init__(audit)
        self.answers, self.sent = list(answers), []

    async def _model_name(self):
        return "fake-model"

    stop = "end_turn"

    async def _call(self, messages, system, max_tokens, temperature):
        self.sent.append(list(messages))
        return ModelResult(self.answers.pop(0), 1000, 200, "fake-model", self.name, self.stop)


@pytest.fixture
async def user(test_db):
    uid = ObjectId()
    doc = copy.deepcopy(USERS[0])
    doc.update(_id=uid, source="claude", feedback=[], insights=[])
    await test_db.users.insert_one(doc)
    return uid


def cloud(uid):
    return UserContext(str(uid), "cloud", "claude", sensitivity.max_for("cloud"))


async def say(test_db, uid, text, tool="log_turn", **args):
    await ensure_conversation_indexes(test_db)
    log = ConversationLog(test_db, AuditLog(test_db))
    await log.log(user_id=uid, channel="claude", mcp_session_id=f"s{next(_ids)}", client=None,
                  record=CallRecord(tool=tool, args={"user_message": text, "previous_reply": "Показав зведення",
                                                     **args}))


async def test_collect_new_allowed_items_only(test_db, user):
    repos = Repos(test_db)
    await say(test_db, user, "Я працюю у фонді, що інвестує в НРК")
    await say(test_db, user, "[widget] 👍 корисно #1", tool="record_feedback")
    await insights.save_insight(repos, cloud(user), "пиши коротше", kind="preference")
    await insights.save_insight(repos, cloud(user), "мій партнер — фонд X", label="confidential")
    await test_db.users.update_one({"_id": user}, {"$push": {"feedback": {
        "fid": ObjectId(), "target_type": "post", "reaction": "not_useful", "category": "sport", "tags": ["футбол"],
        "reasons": ["not_relevant"], "text": "не моя тема", "at": datetime.now(timezone.utc), "channel": "claude"}}})

    act = await profile.collect(test_db, user, "internal")
    assert [m["text"] for m in act.chat] == ["Я працюю у фонді, що інвестує в НРК"]    # no widget, no reply summary
    assert act.feedback[0] == {"at": act.feedback[0]["at"], "target": "post", "reaction": "not_useful",
                               "category": "sport", "tags": ["футбол"], "reasons": ["not_relevant"],
                               "comment": "не моя тема"}
    assert [i["text"] for i in act.insights] == ["пиши коротше"] and act.withheld == 1
    assert act.label == "internal" and "фонд X" not in json.dumps(act.payload(), ensure_ascii=False)
    assert act.goals == ["Знайти напрямки для інвестицій у defense tech"]

    local = await profile.collect(test_db, user, "restricted")              # the local model sees everything
    assert len(local.insights) == 2 and local.label == "confidential"


async def test_learn_save_and_learn_again_from_new_items_only(test_db, user):
    audit = AuditLog(test_db)
    await say(test_db, user, "Я працюю у фонді, що інвестує в НРК")
    backend = FakeBackend(audit, ["```json\n" + json.dumps(ANSWER, ensure_ascii=False) + "\n```"])
    act = await profile.collect(test_db, user, backend.max_label)
    outcome = await profile.learn(backend, act)
    assert outcome.profile.summary == ANSWER["summary"] and outcome.profile.interests[0].weight == 5
    await profile.save(test_db, act, outcome)
    learned = (await test_db.users.find_one({"_id": user}))["learned"]
    assert learned["model"] == "fake-model" and learned["sensitivity"] == "internal"
    assert learned["input"] == {"chat": 1, "feedback": 0, "insights": 0} and learned["processed_until"]
    event = await test_db.audit_log.find_one({"event": "model_call", "user_id": str(user)})
    assert event["details"]["max_sensitivity"] == "internal" and event["details"]["input_tokens"] == 1000

    # Nothing new: nothing to send. A new message: only it, plus the previous profile.
    assert (await profile.collect(test_db, user, "internal")).empty
    await say(test_db, user, "Більше не цікавлять морські дрони")
    again = await profile.collect(test_db, user, "internal")
    assert [m["text"] for m in again.chat] == ["Більше не цікавлять морські дрони"]
    assert again.payload()["previous_profile"]["summary"] == ANSWER["summary"]


async def test_an_unreadable_answer_is_retried_once(test_db, user, tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "FAILURES", tmp_path)
    await say(test_db, user, "привіт")
    act = await profile.collect(test_db, user, "internal")
    ok = FakeBackend(AuditLog(test_db), ["Ось профіль:", json.dumps(ANSWER, ensure_ascii=False)])
    assert (await profile.learn(ok, act)).profile.known == ["Brave1"] and len(ok.sent) == 2
    assert "wasn't valid JSON" in ok.sent[1][-1]["content"]
    bad = FakeBackend(AuditLog(test_db), ["{}", '{"summary": "проект "Рассвет""}'])
    with pytest.raises(ValueError, match="twice"):
        await profile.learn(bad, act)
    assert len(list(tmp_path.iterdir())) == 3                       # every failed answer is kept for a look


async def test_a_cut_off_answer_says_so(test_db, user, tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "FAILURES", tmp_path)
    await say(test_db, user, "привіт")
    act = await profile.collect(test_db, user, "internal")
    cut = FakeBackend(AuditLog(test_db), ['{"summary": "Працює у', '{"summary": "Працює у фонді'])
    cut.stop = "max_tokens"
    with pytest.raises(ValueError, match="cut off"):
        await profile.learn(cut, act)
    assert "too long" in cut.sent[1][-1]["content"]


async def test_a_cloud_backend_never_gets_confidential_data(test_db, user):
    act = profile.Activity(user_id=user, previous=None, insights=[{"text": "секрет"}], labels=["confidential"])
    backend = FakeBackend(AuditLog(test_db), [json.dumps(ANSWER)])
    with pytest.raises(sensitivity.SensitivityError):
        await profile.learn(backend, act)
    assert backend.sent == []                                             # refused before anything left


async def test_learned_profile_reaches_the_client_first(test_db, user):
    repos = Repos(test_db)
    await insights.save_insight(repos, cloud(user), "коротші відповіді", kind="preference")
    await test_db.users.update_one({"_id": user}, {"$set": {"learned": {**ANSWER, "sensitivity": "internal"}}})
    about = await insights.about_user(repos, cloud(user))
    assert list(about)[0] == "summary" and about["summary"] == [ANSWER["summary"]]
    assert about["preference"] == ["коротші відповіді"]                  # the same text once
    assert about["interest"][0] == "НРК (5/5)" and about["avoid"] == ["новини про спорт"]
    # A profile learned by the local model from confidential data stays away from cloud clients.
    await test_db.users.update_one({"_id": user}, {"$set": {"learned.sensitivity": "confidential"}})
    assert "summary" not in await insights.about_user(repos, cloud(user))


async def test_users_with_activity(test_db, user):
    await say(test_db, user, "привіт")
    assert user in await profile.users_with_activity(test_db)



def test_goal_keywords_keep_listed_tags_only():
    p = profile.LearnedProfile.model_validate(ANSWER)
    assert p.goal_keywords[0].tags == ["інвестиції"]                       # "не з переліку" dropped


async def test_a_goal_without_keywords_is_work_to_do(test_db, user):
    act = await profile.collect(test_db, user, "internal")
    assert act.goals_without_keywords == ["Знайти напрямки для інвестицій у defense tech"] and not act.empty
    assert "оборонні технології" in act.payload()["tag_list"]


async def test_many_goals_are_mapped_a_few_at_a_time_and_kept(test_db, user):
    goals = [f"ціль номер {i}" for i in range(12)]
    old = [{"goal": g, "specific": [f"термін{i}"], "broader": [], "tags": []} for i, g in enumerate(goals[:10])]
    old.append({"goal": "видалена ціль", "specific": ["x"], "broader": [], "tags": []})
    await test_db.users.update_one({"_id": user}, {"$set": {
        "goals": [{"text": g, "active": True} for g in goals],
        "learned": {**ANSWER, "goal_keywords": old, "sensitivity": "internal",
                    "processed_until": datetime.now(timezone.utc)}}})
    act = await profile.collect(test_db, user, "internal")
    sent = act.payload()
    assert sent["goals_to_map"] == goals[10:] and "goal_keywords" not in sent["previous_profile"]
    answer = {**ANSWER, "goal_keywords": [{"goal": g, "specific": ["новий"], "broader": [], "tags": []}
                                          for g in goals[10:]]}
    backend = FakeBackend(AuditLog(test_db), [json.dumps(answer, ensure_ascii=False)])
    await profile.save(test_db, act, await profile.learn(backend, act))
    saved = (await test_db.users.find_one({"_id": user}))["learned"]["goal_keywords"]
    assert [m["goal"] for m in saved] == goals                     # 10 kept + 2 new; the removed goal dropped
    assert saved[0]["specific"] == ["термін0"] and saved[11]["specific"] == ["новий"]
    assert (await profile.collect(test_db, user, "internal")).empty     # everything mapped: nothing to do


def rating(**kw):
    return {"fid": ObjectId(), "target_type": "digest_item", "reaction": "useful", "text": "", "channel": "claude",
            "at": datetime.now(timezone.utc), **kw}


async def test_a_comment_is_sent_with_what_was_rated(test_db, user):
    from tests.fixtures.sample_data import DIGEST_UA
    gone = ObjectId()                                   # the source post was already deleted
    long = "Спектр-К: " + "дуже довгий опис пристрою " * 30
    await test_db.users.update_one({"_id": user}, {"$push": {"feedback": {"$each": [
        rating(post_id=gone, digest_id=DIGEST_UA, item_n=3, text="треба максимум інформації по цьому пристрою"),
        rating(post_id=gone, about=long),               # a plain 👍: a shorter copy
        rating(post_id=gone, about="щось", reaction="neutral"),        # 50/50: no copy sent
    ]}}})
    fb = (await profile.collect(test_db, user, "internal")).feedback
    assert fb[0]["about"] == "Brave1 видав гранти на РЕБ." and fb[0]["comment"].startswith("треба")
    assert len(fb[1]["about"]) == profile.ABOUT_SHORT and "about" not in fb[2]


async def test_a_comment_added_later_makes_an_old_rating_new(test_db, user):
    now = datetime.now(timezone.utc)
    await test_db.users.update_one({"_id": user}, {"$set": {"learned": {"processed_until": now - timedelta(hours=1)}},
                                                  "$push": {"feedback": {"$each": [
        rating(at=now - timedelta(hours=2), text="більше про це", text_at=now - timedelta(minutes=5)),
        rating(at=now - timedelta(hours=2)),           # old and uncommented: already processed
    ]}}})
    act = await profile.collect(test_db, user, "internal")
    assert [f.get("comment") for f in act.feedback] == ["більше про це"]
    assert abs(act.until - (now - timedelta(minutes=5))) < timedelta(seconds=1)       # the comment's time
    again = await profile.collect(test_db, user, "internal", since=now - timedelta(days=1))   # --since: reprocess
    assert len(again.feedback) == 2
