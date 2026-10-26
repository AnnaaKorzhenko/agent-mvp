"""brain.domain.scoring must behave exactly like the code it replaced in user_store.py.

tests/fixtures/scoring_golden.json was recorded from the original user_store functions on 2026-10-26,
before the move (commit 503f894).
"""
import json
from pathlib import Path

from brain.domain import scoring

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "scoring_golden.json").read_text(encoding="utf-8"))


def test_constants_unchanged():
    assert scoring.POINTS == GOLDEN["points"]
    assert scoring.CHOICE_POINTS == GOLDEN["choice_points"]


def test_tag_key_matches_original():
    for tag, expected in GOLDEN["key"]:
        assert scoring.tag_key(tag) == expected, tag


def test_points_inc_matches_original():
    for entry, sign, expected in GOLDEN["points_inc"]:
        assert scoring.points_inc(entry, sign) == expected, (entry, sign)


def test_category_scores_matches_original():
    for user, expected in GOLDEN["category_scores"]:
        assert scoring.category_scores(user) == expected


def test_top_tags_matches_original():
    for user, limit, expected in GOLDEN["top_tags"]:
        assert scoring.top_tags(user, limit) == expected


def test_category_order_matches_original():
    for user, side, first, expected in GOLDEN["category_order"]:
        assert scoring.category_order(user, side, first=first) == expected, (side, first)


def test_choice_inc_and_merge():
    assert scoring.choice_inc({"ua": ["front", "policy"], "ru": ["ru_tech"]}) == {
        "interests.category_scores.ua.front": 1, "interests.category_scores.ua.policy": 1,
        "interests.category_scores.ru.ru_tech": 1}
    assert scoring.choice_inc({}) == {}
    assert scoring.merge_inc({"a": 1, "b": 1}, {"a": -1, "c": 2}) == {"b": 1, "c": 2}


def test_bot_uses_the_shared_module():
    import user_store
    assert user_store._points_inc is scoring.points_inc
    assert user_store._key is scoring.tag_key
    assert user_store.category_order is scoring.category_order
    assert user_store.POINTS is scoring.POINTS
