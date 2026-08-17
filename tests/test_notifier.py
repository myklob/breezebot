"""Notifier backends: VAPID keygen and web-push error containment."""
from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import patch

from nightcool.config import WebPushConfig
from nightcool.notifier import WebPushNotifier, generate_vapid_keys


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_generate_vapid_keys_are_raw_base64url():
    public, private = generate_vapid_keys()
    assert "BEGIN" not in private  # not PEM — py_vapid.from_string can't read PEM
    assert len(_b64url_decode(private)) == 32
    pub_raw = _b64url_decode(public)
    assert len(pub_raw) == 65 and pub_raw[0] == 0x04


def test_generated_private_key_parses_with_py_vapid():
    from py_vapid import Vapid

    _, private = generate_vapid_keys()
    assert Vapid.from_string(private) is not None


def test_send_survives_non_webpush_exception(tmp_path: Path):
    cfg = WebPushConfig(
        vapid_public_key="pub",
        vapid_private_key="not-a-real-key",
        vapid_subject="mailto:test@example.com",
    )
    sub = {"endpoint": "https://push.example.com/abc", "keys": {"p256dh": "x", "auth": "y"}}
    notifier = WebPushNotifier(cfg, tmp_path / "state.json", subscription_loader=lambda: [sub])
    with patch("pywebpush.webpush", side_effect=ValueError("Could not deserialize key data")):
        notifier.send("title", "body")  # must not raise
