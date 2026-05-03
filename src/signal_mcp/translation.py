"""Claude-powered message translation."""

import os
from anthropic import Anthropic, APIError

from .models import Message

_client: Anthropic | None = None


class TranslationError(Exception):
    pass


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise TranslationError(
                "ANTHROPIC_API_KEY environment variable not set. "
                "Get a key at https://console.anthropic.com/"
            )
        _client = Anthropic(api_key=api_key)
    return _client


def translate_messages(
    messages: list[Message],
    target_language: str = "English",
    source_language: str | None = None,
) -> list[dict]:
    """Translate a list of messages using Claude. Returns list of {original, translated, sender, timestamp}."""
    if not messages:
        return []

    client = _get_client()

    # Build conversation text for context-aware translation
    lines = []
    for msg in messages:
        ts = msg.timestamp.strftime("%Y-%m-%d %H:%M")
        lines.append(f"[{ts}] {msg.sender}: {msg.body}")

    conversation_text = "\n".join(lines)

    source_hint = f" from {source_language}" if source_language else ""
    prompt = (
        f"Translate the following Signal conversation{source_hint} to {target_language}. "
        f"Preserve the [timestamp] sender: format exactly. Only translate the message text after the colon. "
        f"Keep names, phone numbers, and emoji unchanged.\n\n"
        f"{conversation_text}"
    )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
    except APIError as e:
        raise TranslationError(f"Claude API error: {e}") from e

    translated_text = response.content[0].text
    translated_lines = translated_text.strip().splitlines()

    results = []
    for orig, trans in zip(lines, translated_lines):
        # Extract just the translated body (after the ": " separator)
        orig_body = orig.split(": ", 1)[1] if ": " in orig else orig
        trans_body = trans.split(": ", 1)[1] if ": " in trans else trans
        results.append({
            "original": orig_body,
            "translated": trans_body,
        })

    # Re-attach message metadata
    for i, msg in enumerate(messages):
        if i < len(results):
            results[i]["sender"] = msg.sender
            results[i]["timestamp"] = msg.timestamp.isoformat()
            results[i]["group_id"] = msg.group_id

    return results


def translate_text(text: str, target_language: str = "English") -> str:
    """Translate a single text string."""
    client = _get_client()
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": f"Translate to {target_language}. Reply with only the translation, no explanation:\n\n{text}",
            }],
        )
    except APIError as e:
        raise TranslationError(f"Claude API error: {e}") from e
    return response.content[0].text.strip()
