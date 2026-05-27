"""
Telegram notification helper.

Reads bot token from /root/.openclaw/openclaw.json.
Reads paired chat IDs from /root/.openclaw/credentials/telegram-default-allowFrom.json.

Usage:
    from lib.notify import make_sender

    send = make_sender()          # broadcasts to all paired chat IDs
    send("Strategy started")      # text message
    send_photo = make_sender(photo=True)
    send_photo("/tmp/pnl.png")    # send image file
"""

import json
import os
import requests


_CONFIG_PATH = "/root/.openclaw/openclaw.json"
_ALLOW_FROM_PATH = "/root/.openclaw/credentials/telegram-default-allowFrom.json"


def _load_config():
    if not os.path.exists(_CONFIG_PATH):
        raise FileNotFoundError(f"openclaw config not found: {_CONFIG_PATH}")
    with open(_CONFIG_PATH) as f:
        cfg = json.load(f)
    token = cfg["channels"]["telegram"]["botToken"]

    if not os.path.exists(_ALLOW_FROM_PATH):
        raise FileNotFoundError(
            "Telegram not paired — allowFrom file not found: "
            f"{_ALLOW_FROM_PATH}"
        )
    with open(_ALLOW_FROM_PATH) as f:
        allow = json.load(f)
    chat_ids = allow.get("allowFrom", [])
    if not chat_ids:
        raise KeyError(
            "Telegram not paired — allowFrom list is empty in "
            f"{_ALLOW_FROM_PATH}"
        )
    return token, chat_ids


def make_sender(photo=False):
    """
    Returns a callable that broadcasts a Telegram message or photo
    to all paired chat IDs.

    photo=False → sender(text: str)
    photo=True  → sender(path: str)  (sends the image file at path)
    """
    token, chat_ids = _load_config()

    if photo:
        def _send_photo(path: str) -> None:
            for chat_id in chat_ids:
                with open(path, "rb") as f:
                    requests.post(
                        f"https://api.telegram.org/bot{token}/sendPhoto",
                        data={"chat_id": chat_id},
                        files={"photo": f},
                        timeout=30,
                    )
        return _send_photo
    else:
        def _send_text(msg: str) -> None:
            for chat_id in chat_ids:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": msg},
                    timeout=30,
                )
        return _send_text


def send_text(msg: str) -> None:
    """Convenience: send a text message without constructing a sender."""
    make_sender()(msg)


def send_photo(path: str, caption: str = None) -> None:
    """Convenience: send a photo without constructing a sender.
    If caption is provided, sends it as a follow-up text message.
    """
    make_sender(photo=True)(path)
    if caption:
        send_text(caption)
