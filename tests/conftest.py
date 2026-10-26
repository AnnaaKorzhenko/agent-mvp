"""Shared fixtures. Tests that touch MongoDB use a throwaway database News_test_<random>, dropped afterwards.

Connection: TEST_MONGO_URI, else MONGO_URI from the environment or the repo's .env.
No production data is read or written, except by tests/contract (read-only, opt-in).
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from dotenv import dotenv_values
from pymongo import AsyncMongoClient

from brain.config import Settings
from brain.db.repositories import Repos
from tests.fixtures.sample_data import load_samples

REPO = Path(__file__).resolve().parents[1]


def _mongo_uri() -> str | None:
    env = dotenv_values(REPO / ".env")
    return os.getenv("TEST_MONGO_URI") or os.getenv("MONGO_URI") or env.get("TEST_MONGO_URI") or env.get("MONGO_URI")


@pytest.fixture(scope="session")
def mongo_uri() -> str:
    uri = _mongo_uri()
    if not uri:
        pytest.skip("No TEST_MONGO_URI / MONGO_URI: database tests skipped")
    return uri


@pytest.fixture(scope="session")
def test_db_name() -> str:
    return f"News_test_{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="session")
async def test_db(mongo_uri, test_db_name):
    assert test_db_name.startswith("News_test_")          # never anything else
    client = AsyncMongoClient(mongo_uri, tz_aware=True, appname="brain-tests")
    db = client[test_db_name]
    await load_samples(db)
    yield db
    await client.drop_database(test_db_name)
    await client.close()


@pytest.fixture(scope="session")
def repos(test_db) -> Repos:
    return Repos(test_db)


def make_settings(**overrides) -> Settings:
    values = {"MONGO_URI": "mongodb://unused", "MONGO_DB": "News_test_unused", **overrides}
    return Settings(_env_file=None, **values)


@pytest.fixture
def settings_factory():
    return make_settings
