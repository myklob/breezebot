"""Notification backends: ntfy, Pushover, and a console fallback."""
from __future__ import annotations

from abc import ABC, abstractmethod

import httpx

from .config import NotificationConfig


HTTP_TIMEOUT_S = 10.0


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


def make_notifier(cfg: NotificationConfig) -> Notifier:
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
    return ConsoleNotifier()
