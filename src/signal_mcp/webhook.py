"""Async webhook dispatcher — POST incoming messages to a local HTTP endpoint."""

import asyncio
import json
import logging
from datetime import datetime

import httpx

from .models import Message

logger = logging.getLogger(__name__)

_WEBHOOK_TIMEOUT = 10.0   # seconds
_WEBHOOK_RETRIES = 2


def _message_to_payload(msg: Message) -> dict:
    """Serialise a Message to a webhook payload dict."""
    return {
        "event": "message",
        "timestamp": msg.timestamp.isoformat(),
        "sender": msg.sender,
        "recipient": msg.recipient,
        "group_id": msg.group_id,
        "body": msg.body,
        "quote_id": msg.quote_id,
        "attachments": [
            {
                "content_type": a.content_type,
                "filename": a.filename,
                "local_path": a.local_path,
                "size": a.size,
            }
            for a in msg.attachments
        ],
        "is_read": msg.is_read,
        "receipt_type": msg.receipt_type,
        "expires_in_seconds": msg.expires_in_seconds,
        "view_once": msg.view_once,
    }


async def post_webhook(url: str, msg: Message) -> bool:
    """POST *msg* as JSON to *url*. Returns True on success.

    Retries up to _WEBHOOK_RETRIES times with exponential back-off.
    Never raises — logs errors instead so the watch loop keeps running.
    """
    payload = _message_to_payload(msg)
    last_err: Exception | None = None
    async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT) as client:
        for attempt in range(_WEBHOOK_RETRIES + 1):
            try:
                resp = await client.post(
                    url,
                    content=json.dumps(payload, default=str),
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                return True
            except Exception as e:
                last_err = e
                if attempt < _WEBHOOK_RETRIES:
                    await asyncio.sleep(0.5 * (2 ** attempt))
    logger.warning("Webhook POST to %s failed: %s", url, last_err)
    return False


async def post_webhook_batch(url: str, messages: list[Message]) -> int:
    """Post multiple messages to the webhook URL concurrently. Returns success count."""
    if not messages:
        return 0
    results = await asyncio.gather(*[post_webhook(url, m) for m in messages])
    return sum(results)
