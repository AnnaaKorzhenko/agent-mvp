"""save_insight / get_insights / log_turn: verbatim storage, `internal` by default (private on request),
sensitivity filtering, duplicates, the cap, the profile count and summary, and the links to the conversation."""
import copy
import itertools

import pytest
from mcp import Client

from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.domain import insights, profile, sensitivity
from brain.domain.errors import InvalidInput, NotFound
from brain.domain.identity import UserContext
from brain.mcp_server.server import create_server
from brain.mcp_server.tools import Services
from tests.conftest import make_settings
from tests.fixtures.sample_data import USERS

_ids = itertools.count(950_000_001)


@pytest.fixture
async def user(test_db):
    uid = next(_ids)
    await test_db.users.insert_one({**copy.deepcopy(USERS[0]), "_id": uid})
    return uid


def cloud(uid):
    return UserContext(uid, "cloud", "claude", sensitivity.max_for("cloud"))


def local(uid):
    return UserContext(uid, "local", "local", "restricted")


async def test_saved_verbatim_internal_by_default(repos, test_db, user):
    text = "  Наш фонд не інвестує в компанії без серійного виробництва  "
    saved = await insights.save_insight(repos, cloud(user), text, area="Інвестиції", tags=["Фонд", "фонд", " "])
    assert saved.sensitivity == "internal" and saved.visible_here is True and saved.kind == "other"
    [doc] = (await test_db.users.find_one({"_id": user}))["insights"]
    assert doc["text"] == text.strip() and doc["area"] == "інвестиції" and doc["tags"] == ["фонд"]
    assert doc["channel"] == "claude" and doc["id"] == saved.id and doc["origin"] == "explicit"
    private = await insights.save_insight(repos, cloud(user), "приватне", label="confidential")
    assert private.visible_here is False


async def test_the_same_kind_and_text_is_saved_once(repos, test_db, user):
    a = await insights.save_insight(repos, cloud(user), "Люблю короткі відповіді", kind="preference")
    b = await insights.save_insight(repos, cloud(user), "  люблю   КОРОТКІ відповіді ", kind="preference")
    c = await insights.save_insight(repos, cloud(user), "Люблю короткі відповіді", kind="fact")
    assert not a.duplicate and b.duplicate and b.id is None and not c.duplicate
    assert len((await test_db.users.find_one({"_id": user}))["insights"]) == 2


async def test_recall_respects_sensitivity(repos, user):
    await insights.save_insight(repos, cloud(user), "Приватна думка про Brave1", label="confidential")
    await insights.save_insight(repos, cloud(user), "Можна згадувати: цікавлять НРК")              # internal
    seen = await insights.get_insights(repos, cloud(user))
    assert [i.text for i in seen.insights] == ["Можна згадувати: цікавлять НРК"] and seen.hidden == 1
    assert len((await insights.get_insights(repos, local(user))).insights) == 2         # the local client: all


async def test_recall_by_query_and_area(repos, user):
    ctx = local(user)
    await insights.save_insight(repos, ctx, "Партнер — компанія Roboneers", area="люди")
    await insights.save_insight(repos, ctx, "НРК для логістики важливіші за ударні", area="аналіз")
    await insights.save_insight(repos, ctx, "Наземні роботизовані комплекси: стежити за TerMIT", area="аналіз")
    found = await insights.get_insights(repos, ctx, query="роботизовані комплекси")
    assert found.insights[0].text.startswith("Наземні роботизовані")
    assert [i.area for i in (await insights.get_insights(repos, ctx, area="Аналіз")).insights] == ["аналіз"] * 2
    assert (await insights.get_insights(repos, ctx, query="квантові комп'ютери")).insights == []
    with pytest.raises(InvalidInput):
        await insights.get_insights(repos, ctx, query="і на")


async def test_validation_and_cap(repos, test_db, user, monkeypatch):
    with pytest.raises(InvalidInput):
        await insights.save_insight(repos, cloud(user), "   ")
    with pytest.raises(InvalidInput):
        await insights.save_insight(repos, cloud(user), "x", label="secret")
    with pytest.raises(NotFound):
        await insights.save_insight(repos, cloud(424242), "x")
    monkeypatch.setattr(insights, "MAX_INSIGHTS", 2)
    await insights.save_insight(repos, cloud(user), "один")
    await insights.save_insight(repos, cloud(user), "два")
    with pytest.raises(InvalidInput, match="nothing was saved"):
        await insights.save_insight(repos, cloud(user), "три")
    assert len((await test_db.users.find_one({"_id": user}))["insights"]) == 2


async def test_profile_counts_but_never_reads_texts(repos, user):
    await insights.save_insight(repos, cloud(user), "приватне", label="confidential")
    await insights.save_insight(repos, cloud(user), "можна")
    p = await profile.get_profile(repos, cloud(user))
    assert (p.insights_saved, p.insights_visible) == (2, 1)
    assert repos.users.PROFILE_PROJECTION.get("insights.sensitivity") == 1 and "insights" not in \
        repos.users.PROFILE_PROJECTION


async def test_tools_end_to_end(test_db, user):
    svc = Services(settings=make_settings(DEV_USER_ID=user, TRUST_LEVEL="cloud"), repos=Repos(test_db),
                   audit=AuditLog(test_db), token=lambda: None)
    async with Client(create_server(svc)) as c:
        r = (await c.call_tool("save_insight", {"user_message": "тест", "text": "Запам'ятай: я веду проєкт з дронами-перехоплювачами",
                                                "area": "робота", "sensitivity": "confidential"})).structured_content
        assert r["sensitivity"] == "confidential" and r["visible_here"] is False and "приватно" in r["confirmation"]
        ok = (await c.call_tool("save_insight", {"user_message": "тест", "text": "Можна згадувати: люблю короткі відповіді",
                                                 "kind": "preference"})).structured_content
        assert ok["visible_here"] is True
        got = (await c.call_tool("get_insights", {"user_message": "тест"})).structured_content
        assert [i["text"] for i in got["insights"]] == ["Можна згадувати: люблю короткі відповіді"]
        assert got["hidden"] == 1
        prof = (await c.call_tool("get_profile", {"user_message": "тест"})).structured_content
        assert prof["insights"] == {"saved": 2, "visible_here": 1}
        assert prof["about_user"] == {"preference": ["Можна згадувати: люблю короткі відповіді"]}
    # The insight links to the user message that carried it in the conversation log.
    doc = await test_db.users.find_one({"_id": user})
    first = doc["insights"][0]
    conv = await test_db.conversations.find_one({"_id": first["conversation_id"]})
    [said] = [m for m in conv["messages"] if m["id"] == first["message_id"]]
    assert said["role"] == "user" and said["via"] == "insight" and said["content"] == first["text"]



async def test_log_turn_records_the_message_and_saves_signals(test_db, user):
    await test_db.users.update_one({"_id": user}, {"$set": {"goals": [
        {"text": "Стежити за НРК", "active": True, "source": "user"}]}})
    svc = Services(settings=make_settings(DEV_USER_ID=user, TRUST_LEVEL="cloud"), repos=Repos(test_db),
                   audit=AuditLog(test_db), token=lambda: None)
    message = ("Я працюю у фонді, який інвестує в НРК. Хочу знайти 3 компанії для інвестицій до кінця року. "
               "Про Brave1 я вже все знаю, і пиши коротше, будь ласка.")
    signals = [{"kind": "fact", "text": "працюю у фонді, який інвестує в НРК"},
               {"kind": "goal", "text": "знайти 3 компанії для інвестицій до кінця року"},
               {"kind": "goal", "text": "стежити за НРК"},                              # already an active goal
               {"kind": "known", "text": "Про Brave1 я вже все знаю"},
               {"kind": "preference", "text": "пиши коротше"},
               {"kind": "fact", "text": "мій партнер — Іван", "private": True}]
    async with Client(create_server(svc)) as c:
        first = (await c.call_tool("log_turn", {"user_message": message, "signals": signals})).structured_content
        assert first["saved"] == 6 and first["duplicates"] == 0
        assert first["goals_to_offer"] == ["знайти 3 компанії для інвестицій до кінця року"]
        # about_user comes on the first message of a conversation, read after this message's signals were saved
        assert first["about_user"]["preference"] == ["пиши коротше"]
        assert "мій партнер — Іван" not in str(first["about_user"])                 # private: not for Claude

        again = (await c.call_tool("log_turn", {
            "user_message": "пиши коротше", "previous_reply": "Показав 3 новини про НРК інвестиції",
            "signals": [{"kind": "preference", "text": "Пиши   коротше"}]})).structured_content
        assert again == {"saved": 0, "duplicates": 1, "goals_to_offer": [], "about_user": None}

    doc = await test_db.users.find_one({"_id": user})
    by_kind = {i["kind"]: i for i in doc["insights"]}
    assert all(i["origin"] == "detected" and i["conversation_id"] for i in doc["insights"])
    assert by_kind["fact"]["sensitivity"] in ("internal", "confidential")
    assert [i["sensitivity"] for i in doc["insights"] if "Іван" in i["text"]] == ["confidential"]
    assert len(doc["goals"]) == 1                                         # detected goals are not activated

    [conv] = await test_db.conversations.find({"user_id": user}).to_list(None)
    chat = [(m["role"], m.get("via"), m.get("content")) for m in conv["messages"] if m["role"] != "tool"]
    assert chat == [("user", "chat", message),
                    ("assistant", "reply_summary", "Показав 3 новини про НРК інвестиції"),
                    ("user", "chat", "пиши коротше")]
    tool_msgs = [m for m in conv["messages"] if m["role"] == "tool"]
    assert all("message" not in m["args"] and "previous_reply" not in m["args"] for m in tool_msgs)
    assert tool_msgs[0]["args"]["signals"][1]["kind"] == "goal"
    assert by_kind["known"]["message_id"] == conv["messages"][1]["id"]   # links to the user's chat message


async def test_every_tool_requires_the_users_message(test_db, user):
    svc = Services(settings=make_settings(DEV_USER_ID=user), repos=Repos(test_db), audit=AuditLog(test_db),
                   token=lambda: None)
    async with Client(create_server(svc)) as c:
        tools = (await c.list_tools()).tools
        optional = [t.name for t in tools if "user_message" not in (t.input_schema.get("required") or [])]
        assert optional == ["record_feedback"]                     # rating cards call it directly
        assert all("user_message" in t.input_schema["properties"] for t in tools)
        assert all("signals" in t.input_schema["properties"] for t in tools)
        assert (await c.call_tool("get_profile", {})).is_error                     # missing: refused
        # A card opened before cards sent a message still works; the press is logged as a button press.
        from tests.fixtures.sample_data import DIGEST_UA
        r = await c.call_tool("record_feedback", {"target_type": "digest_item", "digest_id": str(DIGEST_UA),
                                                  "item_n": 1, "reaction": "useful"})
        assert not r.is_error
    [conv] = await test_db.conversations.find({"user_id": user}).to_list(None)
    assert [m["via"] for m in conv["messages"] if m["role"] == "user"] == ["widget"]


async def test_any_tool_saves_signals_and_brings_the_profile_first(test_db, user):
    from tests.fixtures.sample_data import DIGEST_UA
    await insights.save_insight(Repos(test_db), cloud(user), "Люблю короткі відповіді", kind="preference")
    svc = Services(settings=make_settings(DEV_USER_ID=user, TRUST_LEVEL="cloud"), repos=Repos(test_db),
                   audit=AuditLog(test_db), token=lambda: None)
    said = "Покажи новини про морські дрони — я хочу знайти, куди інвестувати в морські дрони"
    async with Client(create_server(svc)) as c:
        s1 = await c.call_tool("search_posts", {
            "query": "морські дрони", "days": 30, "user_message": said,
            "signals": [{"kind": "goal", "text": "знайти, куди інвестувати в морські дрони"},
                        {"kind": "interest", "text": "морські дрони"}]})
        d = s1.structured_content
        assert d["goals_to_offer"] == ["знайти, куди інвестувати в морські дрони"]
        assert d["about_user"]["preference"] == ["Люблю короткі відповіді"]          # first call: the profile
        assert "About the user" in s1.content[0].text
        # A second tool for the same message: no second copy of the message; no profile again (search) ...
        s2 = await c.call_tool("search_posts", {"query": "морські дрони", "days": 30, "user_message": said})
        assert s2.structured_content.get("about_user") is None
        # ... except the digest and the profile, which usually open a new chat: they always bring it.
        d2 = await c.call_tool("get_digest", {"user_message": "```markdown\nПокажи зведення\n```"})
        assert d2.structured_content["about_user"]["preference"] == ["Люблю короткі відповіді"]
        # A button press in the cards.
        await c.call_tool("record_feedback", {"target_type": "digest_item", "digest_id": str(DIGEST_UA),
                                              "item_n": 1, "reaction": "useful",
                                              "user_message": "[widget] 👍 корисно #1 «Морські дрони»"})
    [conv] = await test_db.conversations.find({"user_id": user}).to_list(None)
    users = [(m["via"], m["content"]) for m in conv["messages"] if m["role"] == "user"]
    assert users == [("chat", said), ("search_query", "морські дрони"), ("search_query", "морські дрони"),
                     ("chat", "Покажи зведення"),                       # the code fence removed
                     ("widget", "[widget] 👍 корисно #1 «Морські дрони»")]
    chat_id = conv["messages"][1]["id"]
    doc = await test_db.users.find_one({"_id": user})
    detected = [i for i in doc["insights"] if i["origin"] == "detected"]
    assert sorted(i["kind"] for i in detected) == ["goal", "interest"]
    assert all(i["message_id"] == chat_id and i["conversation_id"] == conv["_id"] for i in detected)
