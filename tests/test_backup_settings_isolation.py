"""Task #280: backup run-time configuration belongs to one application.

The management overrides lived in a module-level dict that every ``create_app``
read and wrote, so a second application in the same process - another window, a
test, a management call against a different data directory - silently changed the
first one's automatic-backup worker, which is the opposite of the per-app state
everything else in ``create_app`` keeps on ``app.state``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agentchatroom import api as api_module
from agentchatroom.api import create_app, effective_backup_settings
from agentchatroom.config import Settings


def test_the_module_level_override_is_gone():
    assert not hasattr(api_module, "_BACKUP_SETTINGS_OVERRIDE")


def test_reader_prefers_the_overrides_it_is_given(settings):
    assert effective_backup_settings(settings)["auto_backup_enabled"] == (
        settings.auto_backup_enabled
    )
    overridden = effective_backup_settings(
        settings, {"auto_backup_enabled": False, "auto_backup_max_kept": 3}
    )
    assert overridden["auto_backup_enabled"] is False
    assert overridden["auto_backup_max_kept"] == 3


def test_two_applications_do_not_change_each_others_backup_settings(tmp_path):
    first = Settings(data_dir=tmp_path / "first")
    second = Settings(data_dir=tmp_path / "second")
    app_first = create_app(first)
    app_second = create_app(second)

    with TestClient(app_first) as client_first, TestClient(app_second) as client_second:
        before_second = client_second.get("/api/v1/admin/backup-settings").json()

        changed = client_first.put(
            "/api/v1/admin/backup-settings",
            json={
                "auto_backup_enabled": False,
                "auto_backup_interval_seconds": 7200,
                "auto_backup_max_kept": 2,
            },
        )
        after_second = client_second.get("/api/v1/admin/backup-settings").json()
        after_first = client_first.get("/api/v1/admin/backup-settings").json()

    assert changed.status_code == 200, changed.text
    assert after_first["auto_backup_interval_seconds"] == 7200
    assert after_first["auto_backup_max_kept"] == 2
    assert after_second == before_second


def test_app_state_exposes_its_own_override(tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "solo"))

    assert app.state.backup_settings_override == {}
    assert app.state.backup_settings_override is not (
        create_app(Settings(data_dir=tmp_path / "other")).state.backup_settings_override
    )
