"""The tools without MCP (docs/WEB_PLAN.md, step 5.0): one handler per tool, the catalog our own chat will use,
and a call that works, validates and is logged the same as through MCP."""
import copy
import itertools

import pytest
from pydantic import ValidationError

from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.tools import catalog
from brain.tools.base import Origin, Services
from brain.tools.handlers import HANDLERS, OWN
from tests.conftest import make_settings
from tests.fixtures.sample_data import USERS, load_samples
from tests.test_mcp_tools import TOOLS

_dbs = itertools.count()
UID = 810_000_001


@pytest.fixture
async def svc(test_db):
    db = test_db.client[f"{test_db.name}_t{next(_dbs)}"]
    await load_samples(db)
    doc = copy.deepcopy(USERS[0])
    doc.update({"_id": UID, "goals": [], "feedback": [], "insights": []})
    await db.users.insert_one(doc)
    yield Services(settings=make_settings(DEV_USER_ID=UID, TRUST_LEVEL="cloud"), repos=Repos(db),
                   audit=AuditLog(db), token=lambda: None)
    await test_db.client.drop_database(db.name)


def test_every_tool_has_one_handler_and_a_spec_without_the_message(svc):
    specs = catalog.build(svc)
    assert set(specs) == set(HANDLERS) == TOOLS
    for spec in specs.values():
        assert not set(spec.input_schema["properties"]) & set(OWN), spec.name
        assert spec.description and spec.input_schema["type"] == "object"
    assert specs["get_digest"].read_only and not specs["set_goal"].read_only
    assert specs["set_goal"].input_schema["required"] == ["action", "text"]
    assert "$defs" not in specs["set_goal"].input_schema                      # Signal went with signals
    assert "FindingIn" in specs["save_findings"].input_schema["$defs"]


async def test_a_call_without_mcp_runs_validates_and_is_logged(svc):
    specs = catalog.build(svc)
    web = Origin("web:chat-1", {"name": "web", "version": "1"}, lambda: True)
    r = await catalog.call(svc, specs["set_goal"], {"action": "add", "text": "знайти 3 НРК-компанії"},
                           "додай ціль знайти 3 НРК-компанії", web)
    assert r.added and "знайти 3 НРК-компанії" in r.goals
    with pytest.raises(ValidationError):
        await catalog.call(svc, specs["set_goal"], {"action": "rename", "text": "x"}, "перейменуй", web)
    [conv] = await svc.repos.db.conversations.find({"user_id": UID}).to_list(None)
    assert conv["mcp_session_id"] == "web:chat-1" and conv["client"] == {"name": "web", "version": "1"}
    tool = next(m for m in conv["messages"] if m["role"] == "tool")
    assert tool["tool"] == "set_goal" and tool["args"]["text"] == "знайти 3 НРК-компанії"
    assert any(m["role"] == "user" and m["content"] == "додай ціль знайти 3 НРК-компанії" for m in conv["messages"])


async def test_cards_follow_the_origin(svc):
    specs = catalog.build(svc)
    asked = []

    def shows():
        asked.append(1)
        return True
    r = await catalog.call(svc, specs["get_digest"], {}, "дайджест", Origin("web:chat-2", None, shows))
    assert r.structured_content["shown_in_widget"] and asked == [1]
    r = await catalog.call(svc, specs["get_digest"], {"as_text": True}, "текстом", Origin("web:chat-2", None, shows))
    assert not r.structured_content["shown_in_widget"] and asked == [1]      # as text: the client isn't asked
    r = await catalog.call(svc, specs["get_profile"], {}, "мій профіль")      # no origin: no cards, no session
    assert r.goals is not None
