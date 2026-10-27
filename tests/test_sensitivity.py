"""A cloud request must never return a confidential (or restricted) item."""
import pytest

from brain.domain import sensitivity as s
from brain.domain.identity import UserContext
from tests.fixtures.sample_data import P_DRONES, P_GRANTS, P_SECRET, P_TOP, NOW

CLOUD = UserContext(1, "cloud", "claude", s.max_for("cloud"))
LOCAL = UserContext(1, "local", "local", s.max_for("local", "restricted"))


def test_levels_and_defaults():
    assert s.max_for("cloud") == "internal"
    assert s.max_for("anything-unknown") == "internal"
    assert s.max_for("local", "confidential") == "confidential"
    assert s.allowed_labels("internal") == ["public", "internal"]
    assert s.is_allowed(None, "internal")                 # missing label = internal
    assert not s.is_allowed("confidential", "internal")
    assert not s.is_allowed("typo-label", "confidential")  # unknown = strictest
    assert s.highest(["public", None, "confidential"]) == "confidential"
    assert s.highest([]) == "public"


def test_require_allowed_raises():
    with pytest.raises(s.SensitivityError):
        s.require_allowed("confidential", "internal")
    s.require_allowed("internal", "internal")


async def test_cloud_get_never_returns_confidential(repos):
    assert await repos.posts.get(str(P_SECRET), CLOUD.allowed) is None
    assert await repos.posts.get(str(P_TOP), CLOUD.allowed) is None
    assert (await repos.posts.get(str(P_DRONES), CLOUD.allowed)).sensitivity == "public"
    assert (await repos.posts.get(str(P_GRANTS), CLOUD.allowed)).sensitivity == "internal"   # no label
    assert (await repos.posts.get(str(P_SECRET), LOCAL.allowed)).sensitivity == "confidential"


async def test_cloud_search_never_returns_confidential(repos):
    since = (NOW.replace(year=NOW.year - 1)).isoformat()
    cloud = await repos.posts.search_candidates(["дрон", "РЕБ"], since_iso=since, allowed=CLOUD.allowed)
    assert cloud and all(p.sensitivity in ("public", "internal") for p in cloud)
    assert str(P_SECRET) not in {p.id for p in cloud} and str(P_TOP) not in {p.id for p in cloud}
    local = await repos.posts.search_candidates(["дрон", "РЕБ"], since_iso=since, allowed=LOCAL.allowed)
    assert {str(P_SECRET), str(P_TOP)} <= {p.id for p in local}


async def test_cloud_get_many_filters(repos):
    got = await repos.posts.get_many([P_DRONES, P_SECRET, P_TOP], CLOUD.allowed)
    assert set(got) == {str(P_DRONES)}


def test_filter_allowed_in_python():
    class Item:
        def __init__(self, label):
            self.sensitivity = label
    items = [Item("public"), Item(None), Item("confidential"), Item("restricted")]
    assert [i.sensitivity for i in s.filter_allowed(items, "internal")] == ["public", None]
