from __future__ import annotations

import base64
import binascii


HTTP_IDENTITY_UTF8_PREFIX = "acr-utf8.v1."


def _validate_identity_text(value: str) -> str:
    text = value.strip()
    if not text or any(
        ord(character) < 32 or ord(character) == 127 for character in text
    ):
        raise ValueError(
            "HTTP software identity values must be non-empty text without control characters"
        )
    return text


def encode_http_identity_value(value: str) -> str:
    """Return an ASCII-safe representation for a custom HTTP header value."""
    text = _validate_identity_text(str(value))
    if all(32 <= ord(character) <= 126 for character in text):
        return text
    encoded = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{HTTP_IDENTITY_UTF8_PREFIX}{encoded}"


def decode_http_identity_value(value: str) -> str:
    """Decode our wire format and recover UTF-8 bytes exposed as Latin-1 by ASGI.

    HTTP field values are byte sequences. Some MCP clients send a configured
    Unicode value as UTF-8 while ASGI deliberately exposes header bytes through
    a Latin-1 string. The fallback keeps existing generated configurations
    readable while the prefixed form gives new configurations an ASCII-only,
    unambiguous wire representation.
    """
    text = _validate_identity_text(str(value))
    if text.startswith(HTTP_IDENTITY_UTF8_PREFIX):
        payload = text[len(HTTP_IDENTITY_UTF8_PREFIX) :]
        if not payload:
            raise ValueError("encoded HTTP software identity value is empty")
        padding = "=" * (-len(payload) % 4)
        try:
            raw = base64.b64decode(
                payload + padding,
                altchars=b"-_",
                validate=True,
            )
            return _validate_identity_text(raw.decode("utf-8"))
        except (binascii.Error, UnicodeDecodeError) as error:
            raise ValueError("encoded HTTP software identity value is invalid") from error

    # Backward compatibility for already-issued configs containing raw Unicode.
    # Starlette follows ASGI and decodes raw header bytes with Latin-1, so a
    # UTF-8 client value such as "通用" arrives as "éç¨".
    if any(ord(character) > 127 for character in text):
        try:
            recovered = text.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            recovered = text
        if recovered != text:
            return _validate_identity_text(recovered)
    return text
