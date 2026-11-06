"""Web Push: the settings and the VAPID key script."""
from __future__ import annotations

import base64

from py_vapid import Vapid

from brain.scripts import vapid_keys
from tests.conftest import make_settings


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_settings_default_off():
    s = make_settings()
    assert s.morning_push is False
    assert s.web_push_public_key is None and s.web_push_private_key is None and s.web_push_contact is None


def test_settings_private_key_is_secret():
    s = make_settings(WEB_PUSH_PUBLIC_KEY="pub", WEB_PUSH_PRIVATE_KEY="s3cr3t-vapid",
                      WEB_PUSH_CONTACT="mailto:a@example.com", MORNING_PUSH="true")
    assert s.morning_push is True and s.web_push_public_key == "pub"
    assert s.web_push_private_key.get_secret_value() == "s3cr3t-vapid"
    assert "s3cr3t-vapid" not in repr(s)


def test_new_pair_is_a_working_vapid_pair():
    public, private = vapid_keys.new_pair()
    raw_public = _unb64(public)
    assert len(raw_public) == 65 and raw_public[0] == 4 and len(_unb64(private)) == 32
    assert "=" not in public + private
    # pywebpush loads the private key this way; its public key must be the one we print
    v = Vapid.from_string(private)
    assert v.public_key.public_numbers().x.to_bytes(32, "big") == raw_public[1:33]
    assert vapid_keys.new_pair() != (public, private)


def test_script_prints_and_writes_nothing(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    vapid_keys.main()
    lines = capsys.readouterr().out.splitlines()
    assert [l.split("=", 1)[0] for l in lines] == ["WEB_PUSH_PUBLIC_KEY", "WEB_PUSH_PRIVATE_KEY"]
    assert list(tmp_path.iterdir()) == []
