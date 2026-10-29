"""Rebuilding tag scores from ratings and the posts' current tags."""
from bson import ObjectId

from brain.scripts.rebuild_tag_scores import rebuild

A, B, GONE = ObjectId(), ObjectId(), ObjectId()


def test_uses_current_tags_and_the_bots_points():
    feedback = [
        {"post_id": A, "reaction": "like", "tags": ["старий тег"]},                 # bot: +1
        {"post_id": A, "reaction": "useful", "target_type": "post"},                # MCP: +1
        {"post_id": B, "reaction": "dislike", "target_type": "digest_item"},        # −1
        {"post_id": B, "reaction": "skip"},                                         # 0
        {"post_id": None, "reaction": "not_useful", "target_type": "answer"},       # answers never count
        {"post_id": GONE, "reaction": "like", "tags": ["гранти"]},                  # deleted: its stored tags
    ]
    current = {A: ["інвестиції", "гранти"], B: ["бойове застосування", "інвестиції"]}
    assert rebuild(feedback, current) == {"бойове застосування": -1, "гранти": 3, "інвестиції": 1}


def test_tags_are_made_safe_keys_and_zeros_dropped():
    assert rebuild([{"post_id": A, "reaction": "like"}, {"post_id": A, "reaction": "dislike"}],
                   {A: ["a.b"]}) == {}
    assert rebuild([{"post_id": A, "reaction": "like"}], {A: ["a.b", "$x"]}) == {"a·b": 1, "x": 1}
