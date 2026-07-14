"""VAPID key generation and web-push failure handling."""
from __future__ import annotations

import base64
import json
from unittest.mock import patch

from nightcool.config import WebPushConfig
from nightcool.notifier import WebPushNotifier, generate_vapid_keys


def _b64decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_generate_vapid_keys_are_raw_base64url():
    public, private = generate_vapid_keys()
    assert len(_b64decode(private)) == 32
    pub_raw = _b64decode(public)
    assert len(pub_raw) == 65
    assert pub_raw[0] == 0x04


def test_generated_private_key_is_accepted_by_py_vapid():
    from py_vapid import Vapid

    _, private = generate_vapid_keys()
    Vapid.from_string(private)  # Must not raise — pywebpush does exactly this.


def test_send_with_bad_key_does_not_raise(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "push_subscriptions": [
            {"endpoint": "https://push.example/1", "keys": {"p256dh": "a", "auth": "b"}},
            {"endpoint": "https://push.example/2", "keys": {"p256dh": "a", "auth": "b"}},
        ]
    }))
    cfg = WebPushConfig(
        vapid_public_key="pub",
        vapid_private_key="-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----",
    )
    notifier = WebPushNotifier(cfg, state)
    with patch("pywebpush.webpush", side_effect=ValueError("Could not deserialize key data")) as wp:
        notifier.send("title", "body")  # Must not raise into the daemon poll loop.
    assert wp.call_count == 1  # Bails after the first failure, not once per subscription.
