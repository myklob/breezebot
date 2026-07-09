"""Notifier unit tests: VAPID key generation must round-trip through py-vapid."""
from __future__ import annotations

from py_vapid import Vapid

from nightcool.notifier import generate_vapid_keys


def test_generated_private_key_is_pywebpush_compatible():
    # pywebpush passes non-file key strings to Vapid.from_string, which only
    # accepts raw/DER base64url — a PEM string here would raise ValueError.
    _, private = generate_vapid_keys()
    Vapid.from_string(private)


def test_generated_keys_are_single_line_base64url():
    public, private = generate_vapid_keys()
    for key in (public, private):
        assert "\n" not in key
        assert "BEGIN" not in key
        assert "=" not in key  # unpadded, as the browser Push API expects
