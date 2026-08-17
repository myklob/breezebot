"""Notification backends: ntfy, Pushover, web push, and a console fallback."""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import NotificationConfig, WebPushConfig


HTTP_TIMEOUT_S = 10.0
logger = logging.getLogger("nightcool.notifier")


class Notifier(ABC):
    """Send one push notification."""

    @abstractmethod
    def send(self, title: str, body: str) -> None:
        ...


class ConsoleNotifier(Notifier):
    """Just print. Useful for dev and for `nightcool check` output."""

    def send(self, title: str, body: str) -> None:
        print(f"[{title}] {body}")


class NtfyNotifier(Notifier):
    """ntfy.sh — pick any topic string, install the app, subscribe to it."""

    def __init__(self, topic: str, server: str = "https://ntfy.sh") -> None:
        self.topic = topic
        self.server = server.rstrip("/")

    def send(self, title: str, body: str) -> None:
        r = httpx.post(
            f"{self.server}/{self.topic}",
            content=body.encode("utf-8"),
            headers={"Title": title, "Tags": "house"},
            timeout=HTTP_TIMEOUT_S,
        )
        r.raise_for_status()


class PushoverNotifier(Notifier):
    """Pushover — needs both an app token and a user key."""

    def __init__(self, user_key: str, app_token: str) -> None:
        self.user_key = user_key
        self.app_token = app_token

    def send(self, title: str, body: str) -> None:
        r = httpx.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": self.app_token,
                "user": self.user_key,
                "title": title,
                "message": body,
            },
            timeout=HTTP_TIMEOUT_S,
        )
        r.raise_for_status()


class WebPushNotifier(Notifier):
    """Browser/PWA push via VAPID.

    Reads the current subscription list from `state.json` on every send so
    new browsers picked up since startup also get pings. Subscriptions that
    return 404/410 are removed via `prune_subscription`.
    """

    def __init__(
        self,
        cfg: WebPushConfig,
        state_path: Path,
        *,
        subscription_loader: Callable[[], list[dict[str, Any]]] | None = None,
        prune_subscription: Callable[[str], None] | None = None,
    ) -> None:
        if not (cfg.vapid_public_key and cfg.vapid_private_key):
            raise ValueError("web_push.vapid_public_key and vapid_private_key are required")
        self.cfg = cfg
        self.state_path = state_path
        self._loader = subscription_loader
        self._pruner = prune_subscription

    def _load(self) -> list[dict[str, Any]]:
        if self._loader is not None:
            return self._loader()
        if not self.state_path.exists():
            return []
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        return list(data.get("push_subscriptions", []))

    def send(self, title: str, body: str) -> None:
        try:
            from pywebpush import WebPushException, webpush
        except ImportError:  # pragma: no cover — pywebpush is in pyproject deps.
            logger.error("pywebpush not installed; cannot send web push")
            return

        subs = self._load()
        if not subs:
            logger.info("No web-push subscriptions registered; skipping send")
            return
        payload = json.dumps({"title": title, "body": body})
        vapid_claims = {"sub": self.cfg.vapid_subject}
        for sub in subs:
            try:
                webpush(
                    subscription_info=sub,
                    data=payload,
                    vapid_private_key=self.cfg.vapid_private_key,
                    vapid_claims=dict(vapid_claims),
                )
            except WebPushException as e:
                status = getattr(e.response, "status_code", None)
                if status in (404, 410) and self._pruner is not None:
                    logger.info("Pruning gone subscription %s", sub.get("endpoint"))
                    self._pruner(sub["endpoint"])
                else:
                    logger.warning("web push failed for %s: %s", sub.get("endpoint"), e)
            except Exception as e:
                # A bad key or network hiccup must not abort the daemon's
                # poll cycle or the remaining subscriptions.
                logger.warning("web push failed for %s: %s", sub.get("endpoint"), e)


def make_notifier(
    cfg: NotificationConfig,
    *,
    state_path: Path | None = None,
    subscription_loader: Callable[[], list[dict[str, Any]]] | None = None,
    prune_subscription: Callable[[str], None] | None = None,
) -> Notifier:
    """Construct the notifier specified in config, validating required fields."""
    if cfg.service == "ntfy":
        if not cfg.ntfy_topic:
            raise ValueError("notifications.ntfy_topic is required for service=ntfy")
        return NtfyNotifier(cfg.ntfy_topic, cfg.ntfy_server)
    if cfg.service == "pushover":
        if not (cfg.pushover_user_key and cfg.pushover_app_token):
            raise ValueError(
                "notifications.pushover_user_key and pushover_app_token are required for service=pushover"
            )
        return PushoverNotifier(cfg.pushover_user_key, cfg.pushover_app_token)
    if cfg.service == "web_push":
        if not cfg.web_push:
            raise ValueError("notifications.web_push section required for service=web_push")
        if state_path is None:
            raise ValueError("state_path required to build a WebPushNotifier")
        return WebPushNotifier(
            cfg.web_push,
            state_path,
            subscription_loader=subscription_loader,
            prune_subscription=prune_subscription,
        )
    return ConsoleNotifier()


def generate_vapid_keys() -> tuple[str, str]:
    """Generate a fresh VAPID keypair, returned as (public, private) base64url.

    The same encoding the browser Push API expects in
    `applicationServerKey` and that `pywebpush` accepts as
    `vapid_private_key`. py-vapid's `Vapid.from_string` only understands
    raw/DER base64url — PEM is rejected — so the private key is the raw
    32-byte value, base64url-encoded.
    """
    import base64

    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    priv_raw = key.private_numbers().private_value.to_bytes(32, "big")
    priv_b64 = base64.urlsafe_b64encode(priv_raw).rstrip(b"=").decode("ascii")

    pub_numbers = key.public_key().public_numbers()
    raw = b"\x04" + pub_numbers.x.to_bytes(32, "big") + pub_numbers.y.to_bytes(32, "big")
    pub_b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return pub_b64, priv_b64
