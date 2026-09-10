from __future__ import annotations

import pytest

from agentchatroom.http_identity import (
    HTTP_IDENTITY_UTF8_PREFIX,
    decode_http_identity_value,
    encode_http_identity_value,
)


def test_unicode_http_identity_uses_ascii_wire_format_and_round_trips():
    encoded = encode_http_identity_value("通用（标准 MCP）")

    assert encoded.startswith(HTTP_IDENTITY_UTF8_PREFIX)
    assert encoded.isascii()
    assert decode_http_identity_value(encoded) == "通用（标准 MCP）"


def test_legacy_utf8_header_exposed_as_latin1_is_recovered():
    original = "通用（标准 MCP）"
    exposed = original.encode("utf-8").decode("latin-1")

    assert decode_http_identity_value(exposed) == original
    assert decode_http_identity_value("Grok Build") == "Grok Build"


@pytest.mark.parametrize("value", ["acr-utf8.v1.", "acr-utf8.v1.not+base64"])
def test_invalid_encoded_http_identity_is_rejected(value):
    with pytest.raises(ValueError, match="encoded HTTP software identity"):
        decode_http_identity_value(value)
