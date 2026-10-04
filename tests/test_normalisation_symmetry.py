"""#363: three normalisations that were inconsistent with their own siblings.

Each of these is a place where two spellings of one thing compared as different:
a timestamp whose text order disagreed with its real order, a lease scope that
was case-sensitive while every sibling comparison folded case, and a storage type
that quietly dropped the precision it existed to keep.
"""

from __future__ import annotations

import datetime

import pytest

from agentchatroom.database import SCHEMA
from agentchatroom.postgres_database import postgres_script
from agentchatroom.services import (
    format_instant,
    iso_now,
    parse_time,
    patterns_overlap,
)


# --- 1. timestamps sort in the order they happened -------------------------


def test_iso_now_is_fixed_width_so_text_order_matches_real_order():
    """A variable-width stamp makes ``ORDER BY created_at`` lie.

    ``isoformat()`` drops the fraction at exactly zero, so two instants can
    render as ``...13Z`` and ``...13.000001Z`` - and those do not compare in
    chronological order as strings.
    """
    stamps = {iso_now() for _ in range(200)}
    assert len({len(stamp) for stamp in stamps}) == 1, "width still varies"
    assert all(len(stamp) == 27 for stamp in stamps), sorted(stamps)[0]


def test_a_later_instant_never_sorts_before_an_earlier_one():
    """The defect itself, spelled out.

    ``...13Z`` and ``...13.000001Z`` are one millisecond apart, yet as strings
    the later one sorts first - so a text-ordered listing returns them in the
    wrong order.
    """
    base = datetime.datetime(2026, 3, 4, 5, 6, 7, tzinfo=datetime.timezone.utc)
    whole_second = base.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    later_stamp = format_instant(base + datetime.timedelta(milliseconds=1))

    # The old rendering really does invert the order...
    assert whole_second > later_stamp
    # ...and the new one is monotonic, because both widths match.
    assert format_instant(base) < later_stamp


def test_parse_time_still_accepts_the_old_spelling():
    """Historical rows were written before this change and must keep reading."""
    old = "2026-03-04T05:06:07Z"
    new = "2026-03-04T05:06:07.000000Z"
    assert parse_time(old) == parse_time(new)
    assert parse_time(old).tzinfo is not None


def test_format_instant_and_iso_now_agree():
    """A deadline written by one and compared against the other must not drift."""
    moment = datetime.datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=datetime.timezone.utc)
    assert format_instant(moment) == "2026-01-02T03:04:05.123456Z"
    assert len(format_instant(moment)) == len(iso_now())


# --- 2. lease scope comparison folds case ----------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("README.md", "readme.md"),
        ("src/App.py", "src/app.py"),
        ("Docs/**", "docs/readme.md"),
        ("src/**", "SRC/x.py"),
    ],
)
def test_two_spellings_of_one_file_collide(left, right) -> None:
    """Case-folded comparison, storage keeps its case.

    Windows and macOS do not distinguish these names, so treating them as
    different scopes let two Agents hold exclusive leases on one file.
    """
    assert patterns_overlap(left, right) is True


def test_genuinely_different_scopes_still_do_not_collide() -> None:
    """Folding case must not turn unrelated scopes into the same one."""
    assert patterns_overlap("a.py", "b.py") is False
    assert patterns_overlap("src/a.py", "docs/b.py") is False


def test_the_stored_pattern_keeps_its_spelling(service, project) -> None:
    """Comparison folds case; the Room still shows what the Agent asked for."""
    agent = service.join_room(
        project["id"],
        software_key="case-client",
        name="Case",
        client="pytest",
        model="unknown",
    )
    lease = service.acquire_lease(
        project["id"],
        session_id=agent["agent"]["id"],
        token=agent["token"],
        path_pattern="README.md",
    )
    assert lease["lease"]["path_pattern"] == "README.md"


def test_a_conflicting_lease_is_refused_across_case(service, project) -> None:
    """The behaviour the fold exists for, end to end."""
    first = service.join_room(
        project["id"],
        software_key="case-a",
        name="A",
        client="pytest",
        model="unknown",
    )
    second = service.join_room(
        project["id"],
        software_key="case-b",
        name="B",
        client="pytest",
        model="unknown",
    )
    service.acquire_lease(
        project["id"],
        session_id=first["agent"]["id"],
        token=first["token"],
        path_pattern="README.md",
    )

    from agentchatroom.errors import DomainError

    with pytest.raises(DomainError) as refused:
        service.acquire_lease(
            project["id"],
            session_id=second["agent"]["id"],
            token=second["token"],
            path_pattern="readme.md",
        )
    assert refused.value.code == "lease_conflict"


# --- 3. the PostgreSQL translation keeps the precision it stores -----------


def test_postgres_widens_real_to_double_precision() -> None:
    """REAL is float4: about seven significant digits.

    A unix timestamp in float4 loses the sub-second part it is stored to carry,
    and the management session expiry comparison drifts far enough to expire a
    live session early.
    """
    translated = postgres_script(SCHEMA)
    assert "expires_at DOUBLE PRECISION NOT NULL" in translated
    assert "created_at DOUBLE PRECISION NOT NULL" in translated
    assert " REAL " not in translated, "a REAL column survived the translation"


def test_float4_would_actually_lose_the_precision_in_question() -> None:
    """Why the width matters, so the rule is not silently narrowed later."""
    moment = 1_800_000_000.123456
    import struct

    as_float4 = struct.unpack("f", struct.pack("f", moment))[0]
    assert as_float4 != moment
    assert abs(as_float4 - moment) > 0.001
