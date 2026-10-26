"""user_id comes from settings (dev) or the verified identity, never from tool arguments."""
import pytest

from brain.domain.identity import IdentityError, VerifiedToken, resolve_user


class FakeUsers:
    def __init__(self, links):
        self.links = links

    async def find_by_identity(self, provider, subject):
        return self.links.get((provider, subject))


USERS = FakeUsers({("https://auth.example.com", "sub-1"): 555})


async def test_dev_mode_uses_configured_user(settings_factory):
    st = settings_factory(DEV_USER_ID=42, TRUST_LEVEL="local", MAX_SENSITIVITY_LOCAL="confidential")
    ctx = await resolve_user(st, None, USERS)
    assert (ctx.user_id, ctx.trust, ctx.channel, ctx.max_sensitivity) == (42, "local", "dev", "confidential")


def test_dev_mode_refused_on_public_url(settings_factory):
    with pytest.raises(ValueError, match="only on a local URL"):
        settings_factory(DEV_USER_ID=42, PUBLIC_BASE_URL="https://mcp.example.com")
    settings_factory(DEV_USER_ID=42, PUBLIC_BASE_URL="http://127.0.0.1:8000")      # fine


async def test_no_token_and_no_dev_user_is_refused(settings_factory):
    with pytest.raises(IdentityError):
        await resolve_user(settings_factory(), None, USERS)


async def test_oauth_identity_maps_to_user(settings_factory):
    st = settings_factory(OAUTH_ISSUER="https://auth.example.com", DEV_USER_ID=42)   # OAuth wins over dev
    token = VerifiedToken("https://auth.example.com", "sub-1", client_id="abc", client_name="Claude")
    ctx = await resolve_user(st, token, USERS)
    assert (ctx.user_id, ctx.trust, ctx.channel, ctx.max_sensitivity) == (555, "cloud", "claude", "internal")


async def test_unlinked_identity_is_refused(settings_factory):
    st = settings_factory(OAUTH_ISSUER="https://auth.example.com")
    with pytest.raises(IdentityError, match="not linked"):
        await resolve_user(st, VerifiedToken("https://auth.example.com", "someone-else"), USERS)


async def test_cloud_deployment_caps_at_internal_even_with_local_setting(settings_factory):
    st = settings_factory(OAUTH_ISSUER="https://auth.example.com", TRUST_LEVEL="cloud",
                          MAX_SENSITIVITY_LOCAL="restricted")
    ctx = await resolve_user(st, VerifiedToken("https://auth.example.com", "sub-1"), USERS)
    assert ctx.max_sensitivity == "internal" and ctx.channel == "unknown"


async def test_users_repo_identity_lookup(repos, test_db):
    from tests.fixtures.sample_data import USER_ID
    await test_db.users.update_one({"_id": USER_ID}, {"$set": {"identities": [
        {"provider": "https://auth.example.com", "subject": "sub-xyz"}]}})
    assert await repos.users.find_by_identity("https://auth.example.com", "sub-xyz") == USER_ID
    assert await repos.users.find_by_identity("https://auth.example.com", "nope") is None
    await test_db.users.update_one({"_id": USER_ID}, {"$unset": {"identities": ""}})
