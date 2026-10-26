"""user_id comes from settings (dev) or the verified identity, never from tool arguments."""
import pytest

from brain.domain.identity import IdentityError, VerifiedToken, resolve_user

ISS = "https://tenant.example.auth0.com/"


class FakeUsers:
    def __init__(self, links=None):
        self.links = dict(links or {})
        self.created = []

    async def find_by_identity(self, provider, subject):
        return self.links.get((provider, subject))

    async def create_identity_user(self, provider, subject, channel, email=None):
        new_id = f"65aa{len(self.created):020d}"
        self.links[(provider, subject)] = new_id
        self.created.append((provider, subject, channel, email))
        return new_id


async def test_dev_mode_uses_configured_user(settings_factory):
    st = settings_factory(DEV_USER_ID=42, TRUST_LEVEL="local", MAX_SENSITIVITY_LOCAL="confidential")
    ctx = await resolve_user(st, None, FakeUsers())
    assert (ctx.user_id, ctx.trust, ctx.channel, ctx.max_sensitivity) == (42, "local", "dev", "confidential")


def test_dev_user_id_from_env_text(settings_factory):
    assert settings_factory(DEV_USER_ID="111000111").dev_user_id == 111000111          # Telegram id -> int
    hex_id = "65aa00000000000000000001"
    assert settings_factory(DEV_USER_ID=hex_id).dev_user_id == hex_id                  # OAuth-created user
    assert settings_factory(DEV_USER_ID="").dev_user_id is None


def test_dev_mode_refused_on_public_url(settings_factory):
    with pytest.raises(ValueError, match="only on a local URL"):
        settings_factory(DEV_USER_ID=42, PUBLIC_BASE_URL="https://mcp.example.com")
    settings_factory(DEV_USER_ID=42, PUBLIC_BASE_URL="http://127.0.0.1:8000")      # fine


def test_oauth_needs_the_resource_url(settings_factory):
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        settings_factory(OAUTH_ISSUER=ISS)
    st = settings_factory(OAUTH_ISSUER=ISS, PUBLIC_BASE_URL="https://brain.onrender.com/")
    assert st.resource_url == "https://brain.onrender.com/mcp"


async def test_no_token_and_no_dev_user_is_refused(settings_factory):
    with pytest.raises(IdentityError):
        await resolve_user(settings_factory(), None, FakeUsers())


async def test_known_identity_maps_to_its_user(settings_factory):
    st = settings_factory(OAUTH_ISSUER=ISS, PUBLIC_BASE_URL="https://b.example", DEV_USER_ID=42)  # OAuth wins
    users = FakeUsers({(ISS, "auth0|1"): 555})
    ctx = await resolve_user(st, VerifiedToken(ISS, "auth0|1", client_id="abc", client_name="Claude"), users)
    assert (ctx.user_id, ctx.trust, ctx.channel, ctx.max_sensitivity) == (555, "cloud", "claude", "internal")
    assert users.created == []


async def test_new_identity_gets_its_own_new_user(settings_factory):
    st = settings_factory(OAUTH_ISSUER=ISS, PUBLIC_BASE_URL="https://b.example")
    users = FakeUsers({(ISS, "auth0|telegram-linked"): 111})
    token = VerifiedToken(ISS, "google-oauth2|new", client_name="Claude", email="a@b.c")
    ctx = await resolve_user(st, token, users)
    again = await resolve_user(st, token, users)
    assert ctx.user_id == again.user_id and isinstance(ctx.user_id, str)
    assert users.created == [(ISS, "google-oauth2|new", "claude", "a@b.c")]


async def test_auto_create_can_be_switched_off(settings_factory):
    st = settings_factory(OAUTH_ISSUER=ISS, PUBLIC_BASE_URL="https://b.example", AUTO_CREATE_USERS=False)
    with pytest.raises(IdentityError, match="no profile"):
        await resolve_user(st, VerifiedToken(ISS, "someone"), FakeUsers())


async def test_allowed_emails_checked_on_every_call(settings_factory):
    st = settings_factory(OAUTH_ISSUER=ISS, PUBLIC_BASE_URL="https://b.example", ALLOWED_EMAILS=["Boss@X.com "])
    users = FakeUsers({(ISS, "s1"): 7})
    assert (await resolve_user(st, VerifiedToken(ISS, "s1", email="boss@x.com"), users)).user_id == 7
    for email in ("other@x.com", None):
        with pytest.raises(IdentityError, match="not allowed"):
            await resolve_user(st, VerifiedToken(ISS, "s1", email=email), users)


async def test_cloud_deployment_caps_at_internal_even_with_local_setting(settings_factory):
    st = settings_factory(OAUTH_ISSUER=ISS, PUBLIC_BASE_URL="https://b.example", TRUST_LEVEL="cloud",
                          MAX_SENSITIVITY_LOCAL="restricted")
    ctx = await resolve_user(st, VerifiedToken(ISS, "x"), FakeUsers({(ISS, "x"): 1}))
    assert ctx.max_sensitivity == "internal" and ctx.channel == "unknown"


async def test_users_repo_identity_create_lookup_and_uniqueness(repos, test_db):
    from brain.db.repositories import ensure_user_indexes
    await ensure_user_indexes(test_db)
    uid = await repos.users.create_identity_user(ISS, "sub-xyz", "claude", "x@y.z")
    assert await repos.users.find_by_identity(ISS, "sub-xyz") == uid
    assert await repos.users.create_identity_user(ISS, "sub-xyz", "claude") == uid      # duplicate -> same user
    doc = await test_db.users.find_one({"identities.subject": "sub-xyz"})
    assert doc["source"] == "claude" and doc["onboarded"] is False and doc["daily_push"] is False
    assert await test_db.users.count_documents({"identities.subject": "sub-xyz"}) == 1
    assert (await repos.users.get_profile(uid)).id == uid
    assert await repos.users.find_by_identity(ISS, "nope") is None
