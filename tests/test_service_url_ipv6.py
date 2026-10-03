"""Task #323: an IPv6 loopback host must produce a usable URL.

``http://::1:8765`` is not a URL - the port reads as part of the address - so
"打开前端" and the status bar showed something no browser can open, even though
``::1`` is a perfectly valid loopback target that the GUI already exempts from
management authentication.

The rule lived in four private copies (cli, api, client_config, integrations),
which is how a fix reached only some of them. It now lives once in
``config.build_service_base_url``; these tests pin the shared policy and assert
the adapters route through it instead of rebuilding the string.
"""

from __future__ import annotations

import pytest

from agentchatroom.cli import service_url
from agentchatroom.config import build_service_base_url, format_host_for_url


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [
        ("127.0.0.1", 8765, "http://127.0.0.1:8765"),
        # A wildcard bind address is not connectable; it opens as loopback.
        ("0.0.0.0", 8765, "http://127.0.0.1:8765"),
        ("::", 8765, "http://127.0.0.1:8765"),
        # The actual defect: the IPv6 loopback has to be bracketed.
        ("::1", 8765, "http://[::1]:8765"),
        ("localhost", 8765, "http://localhost:8765"),
        ("203.0.113.7", 8765, "http://203.0.113.7:8765"),
    ],
)
def test_service_base_url_formats_ipv6_and_wildcards(host, port, expected):
    assert build_service_base_url(host, port) == expected


def test_the_cli_keeps_its_public_spelling_on_the_shared_policy():
    assert service_url("::1", 8765) == "http://[::1]:8765"
    assert service_url("0.0.0.0", 8765) == "http://127.0.0.1:8765"


def test_an_already_bracketed_host_is_not_bracketed_twice():
    assert format_host_for_url("[::1]") == "[::1]"
    assert build_service_base_url("[::1]", 8765) == "http://[::1]:8765"


def test_the_port_stays_out_of_the_address():
    """The failure mode: a bare IPv6 literal makes the port unreadable."""
    import urllib.parse

    parsed = urllib.parse.urlsplit(build_service_base_url("::1", 8765))
    assert parsed.hostname == "::1"
    assert parsed.port == 8765


def test_the_gui_status_bar_and_frontend_button_agree_on_ipv6():
    """The GUI reaches the shared policy through the CLI spelling."""
    from agentchatroom.gui import resolve_frontend_url

    # Fallback path (no address logged yet) must still be a usable URL.
    assert resolve_frontend_url(None, "::1", 8765) == "http://[::1]:8765"
    # A logged address still wins, as README:214 requires.
    assert (
        resolve_frontend_url("http://[::1]:9000", "::1", 8765)
        == "http://[::1]:9000"
    )


@pytest.mark.parametrize(
    "module_path",
    [
        "agentchatroom.api",
        "agentchatroom.client_config",
        "agentchatroom.integrations",
        "agentchatroom.cli",
    ],
)
def test_no_adapter_rebuilds_the_base_url_string(module_path):
    """Guard against the four private copies coming back.

    ``build_service_base_url`` is the single place that knows a wildcard maps
    to loopback and an IPv6 literal needs brackets. An adapter that maps a
    wildcard host itself is a copy that will drift again. (Prepending a scheme
    to user-typed input is a different job and is allowed.)
    """
    import pathlib

    import agentchatroom

    root = pathlib.Path(agentchatroom.__file__).parent
    source = (root / (module_path.rsplit(".", 1)[1] + ".py")).read_text(
        encoding="utf-8"
    )
    assert '"127.0.0.1" if' not in source, (
        f"{module_path} maps a wildcard bind host by hand; call "
        "config.build_service_base_url instead"
    )


def test_a_user_typed_ipv6_target_without_a_scheme_is_bracketed():
    """The same defect on the input path: "::1:8765" is not a URL either."""
    from agentchatroom.client_config import ServerTarget

    assert ServerTarget.normalize_url("::1:8765") == "http://[::1]:8765"
    # Ordinary targets must be untouched.
    assert ServerTarget.normalize_url("127.0.0.1:8765") == "http://127.0.0.1:8765"
    assert ServerTarget.normalize_url("example.com:8080") == "http://example.com:8080"


