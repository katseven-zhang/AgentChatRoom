"""Task #256: retention throttling has to survive a wall-clock step backwards.

Both automatic sweeps compared ``time.time()`` against a stored wall-clock
timestamp with a hardcoded 3600-second gap, so an NTP correction or a resumed VM
(a clock that moves backwards) made ``now - last`` negative and stalled idempotency
and audit retention for the rest of the process lifetime, letting those tables grow
without bound - while every neighbouring TTL/retention value came from Settings.
"""

from __future__ import annotations

import pytest

from agentchatroom import services as services_module
from agentchatroom.config import load_settings


@pytest.fixture()
def fake_monotonic(monkeypatch):
    clock = {"value": 1_000.0}
    monkeypatch.setattr(services_module.time, "monotonic", lambda: clock["value"])
    return clock


def test_idempotency_sweep_skips_only_within_the_configured_interval(
    service, project, fake_monotonic, monkeypatch
):
    monkeypatch.setattr(service, "retention_sweep_interval_seconds", lambda: 45.0)
    # Keep the day-cutoff maths on a real clock while the throttle is fake.
    first = service.enforce_idempotency_retention(force=True)
    assert first["retention_days"] == 30

    second = service.enforce_idempotency_retention()
    assert second.get("skipped") is True

    fake_monotonic["value"] += 46.0
    third = service.enforce_idempotency_retention()
    assert third.get("skipped") is not True


def test_a_backwards_wall_clock_does_not_stall_the_sweep(
    service, project, fake_monotonic, monkeypatch
):
    # 2020 is decades behind "now": a wall-clock throttle would not run again
    # until real time caught up with the stored timestamp.
    monkeypatch.setattr(services_module.time, "time", lambda: 1_580_000_000.0)
    monkeypatch.setattr(service, "retention_sweep_interval_seconds", lambda: 1.0)

    first = service.enforce_idempotency_retention(force=True)
    assert first.get("skipped") is not True

    monkeypatch.setattr(services_module.time, "time", lambda: 1_000.0)
    fake_monotonic["value"] += 2.0
    second = service.enforce_idempotency_retention()
    assert second.get("skipped") is not True, second


def test_audit_sweep_uses_the_same_monotonic_deadline(
    service, project, fake_monotonic, monkeypatch
):
    monkeypatch.setattr(service, "retention_sweep_interval_seconds", lambda: 90.0)

    first = service.enforce_audit_retention_throttled(project["id"])
    assert first.get("skipped") is not True

    fake_monotonic["value"] += 10.0
    assert service.enforce_audit_retention_throttled(project["id"]).get("skipped") is True

    fake_monotonic["value"] += 95.0
    assert service.enforce_audit_retention_throttled(project["id"]).get("skipped") is not True


def test_the_interval_is_a_validated_config_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCHATROOM_RETENTION_SWEEP_INTERVAL_SECONDS", "120")
    assert load_settings(data_dir=tmp_path / "env").retention_sweep_interval_seconds == 120

    monkeypatch.setenv("AGENTCHATROOM_RETENTION_SWEEP_INTERVAL_SECONDS", "0")
    with pytest.raises(ValueError, match="retention sweep interval"):
        load_settings(data_dir=tmp_path / "zero")
