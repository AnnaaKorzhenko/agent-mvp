"""The contract checker itself, and tolerant parsing in repositories (no real data)."""
import logging

from bson import ObjectId

from brain.db.contract import check_document, load_contract
from brain.db.models import PostDoc
from brain.db.repositories import parse
from tests.fixtures.sample_data import DIGESTS, POSTS, SOURCES, USERS

RULES = load_contract()


def test_contract_tables_parsed():
    assert {"posts", "sources", "digests", "users", "audit_log"} <= set(RULES)
    paths = {r.path for r in RULES["posts"]}
    assert {"_id", "post_url", "summary", "published_at", "source_side", "sensitivity"} <= paths


def test_sample_data_meets_contract():
    for name, docs in (("posts", POSTS), ("sources", SOURCES), ("digests", DIGESTS), ("users", USERS)):
        for doc in docs:
            assert check_document(doc, RULES[name]) == [], (name, doc["_id"])


def test_missing_renamed_and_wrong_type_are_reported():
    doc = dict(POSTS[0])
    doc.pop("summary")
    doc["text_summary"] = "renamed"                  # a rename shows up as "summary missing"
    doc["importance"] = "high"
    doc["is_ad"] = None
    problems = {(p.path, p.kind, p.detail) for p in check_document(doc, RULES["posts"])}
    assert problems == {("summary", "missing", ""), ("importance", "type", "str"), ("is_ad", "type", "null")}


def test_nested_paths_checked_per_element():
    digest = {**DIGESTS[0], "sections": [{"category": "x", "items": [{"text": "ok", "source_urls": [],
                                                                       "source_post_ids": []},
                                                                      {"source_urls": [], "source_post_ids": []}]}]}
    problems = [(p.path, p.kind) for p in check_document(digest, RULES["digests"])]
    assert problems == [("sections[].items[].text", "missing")]


def test_parse_logs_and_skips_broken_document(caplog):
    bad = {"_id": ObjectId(), "post_url": "u", "published_at": "2026-10-26T00:00:00+00:00", "source_side": "ua"}
    with caplog.at_level(logging.ERROR, logger="brain.db"):
        assert parse(PostDoc, bad, "posts") is None
    assert "summary" in caplog.text and "DATA_CONTRACT" in caplog.text


def test_parse_ignores_unknown_fields_and_defaults():
    post = parse(PostDoc, {**POSTS[1], "brand_new_field": 1, "tags": None}, "posts")
    assert post.sensitivity == "internal" and post.tags == [] and not hasattr(post, "brand_new_field")
