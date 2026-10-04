"""Task #272: credentials must be redacted on the way to disk.

The redacting filter was attached to four *named loggers*. Python only runs
``Logger.filters`` for the logger a record was logged through, so a record from
``agentchatroom.services`` reached the parent handler without any redaction and
the connection string in a failing traceback was written to server.log
verbatim. The admin log view redacted for display, which is why the leak was
invisible on screen and unsafe on disk.

These tests assert what actually lands in a handler: a child logger, a %-style
argument, and an attached traceback all have to come out masked.
"""

from __future__ import annotations

import io
import logging

import pytest

from agentchatroom.api import (
    CredentialRedactingLogFilter,
    install_credential_log_redaction,
    redact_log_line,
)

SECRET = "acr.credential_abc123def456.SECRETVALUE"


@pytest.fixture()
def sink():
    """A file-like handler that records exactly what would hit server.log."""
    buffer = io.StringIO()

    class Capture(logging.Handler):
        def emit(self, record):
            buffer.write(self.format(record) + "\n")

    handler = Capture()
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(CredentialRedactingLogFilter())
    return handler, buffer


def test_a_child_logger_record_is_redacted_on_the_way_out(sink):
    """The reported defect: the record is logged through agentchatroom.services."""
    handler, buffer = sink
    child = logging.getLogger("agentchatroom.services")
    child.propagate = False
    child.handlers = [handler]
    child.setLevel(logging.INFO)

    child.info("connecting with %s", f"token={SECRET}")

    written = buffer.getvalue()
    assert SECRET not in written
    assert "[REDACTED]" in written
    # Operational context survives.
    assert "connecting with" in written


def test_a_logger_level_filter_alone_would_have_missed_it(sink):
    """Pins why the handler is the right place: a logger filter does not run."""
    handler, buffer = sink
    handler.filters.clear()

    parent = logging.getLogger("agentchatroom")
    parent.addFilter(CredentialRedactingLogFilter())
    child = logging.getLogger("agentchatroom.services")
    child.propagate = False
    child.handlers = [handler]
    child.setLevel(logging.INFO)

    child.info("token=%s", SECRET)

    assert SECRET in buffer.getvalue()

    parent.filters.clear()


def test_an_exception_traceback_is_redacted(sink):
    """A failing connection is where a conninfo string ends up."""
    handler, buffer = sink
    child = logging.getLogger("agentchatroom.api")
    child.propagate = False
    child.handlers = [handler]
    child.setLevel(logging.INFO)

    try:
        raise RuntimeError(f"could not connect: password={SECRET}")
    except RuntimeError:
        child.exception("database unavailable")

    written = buffer.getvalue()
    assert SECRET not in written
    # The traceback itself is still there - only the secret is masked.
    assert "RuntimeError" in written
    assert "Traceback" in written
    assert "[REDACTED]" in written


def test_a_dict_of_args_is_redacted(sink):
    handler, buffer = sink
    child = logging.getLogger("agentchatroom.services")
    child.propagate = False
    child.handlers = [handler]
    child.setLevel(logging.INFO)

    child.info("auth %s", {"authorization": f"Bearer {SECRET}"})

    assert SECRET not in buffer.getvalue()


def test_installation_reaches_handlers_of_the_named_loggers():
    """install_credential_log_redaction must not stop at the loggers."""
    uvicorn_access = logging.getLogger("uvicorn.access")
    original = list(uvicorn_access.handlers)
    capture = logging.NullHandler()
    uvicorn_access.handlers = [capture]
    try:
        install_credential_log_redaction()
        assert any(
            isinstance(existing, CredentialRedactingLogFilter)
            for existing in capture.filters
        )
    finally:
        uvicorn_access.handlers = original


def test_redaction_converges_and_keeps_operational_text():
    """Repeated redaction reaches a fixed point and never leaks the secret.

    The requirement is that a secret cannot survive redaction and that
    re-redacting a stored line settles instead of growing without bound. The
    admin log view re-renders stored lines, so a policy that appended a new
    marker on every pass would inflate the file it is protecting.
    """
    once = redact_log_line(f"GET /x token={SECRET}")
    twice = redact_log_line(once)
    third = redact_log_line(twice)

    assert SECRET not in once
    assert twice == third, "redaction must converge, not accumulate"
    # Operational context survives.
    assert "GET /x" in once
    assert "[REDACTED]" in once
    # One marker per secret, never a second one appended to the same value.
    assert once.count("[REDACTED]") == twice.count("[REDACTED]")


def test_a_line_mixing_a_masked_value_and_a_real_secret_masks_both():
    """Parking an existing marker must not hide a live secret next to it."""
    line = f"token=[REDACTED] then password={SECRET}"
    result = redact_log_line(line)
    assert SECRET not in result
    assert result.count("[REDACTED]") >= 1


def test_the_filter_never_raises_on_an_unformattable_record(sink):
    """Redaction must not be able to break logging."""
    handler, buffer = sink
    child = logging.getLogger("agentchatroom.services")
    child.propagate = False
    child.handlers = [handler]
    child.setLevel(logging.INFO)

    log_filter = CredentialRedactingLogFilter()
    record = logging.LogRecord(
        "agentchatroom.services", logging.INFO, __file__, 1, "%s", ("a",), None
    )
    record.exc_info = ("not-a-real-exc-info-tuple", None, None)
    assert log_filter.filter(record) is True
