from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from agentchatroom.config import Settings
from agentchatroom.database import Database
from agentchatroom.services import AgentChatRoomService

_CHECKOUT_ROOT = Path(__file__).resolve().parents[1]
_PYTEST_BASETEMP_ROOT = "agentchatroom-pytest"
_STALE_RUN_MAX_AGE_SECONDS = 24 * 60 * 60
_BASETEMP_DEFAULT_FLAG = "_agentchatroom_basetemp_default"
_REGISTRATION_WARNED = False

# pyproject 的 pythonpath = ["."] 相对 rootdir 解析，足以覆盖常规入口；
# 这里再按 conftest 实际位置兜底一次，保证从任意工作目录启动（如 IDE
# 测试插件）时 scripts.* 与 tests.* 仍可导入。
if str(_CHECKOUT_ROOT) not in sys.path:
    sys.path.insert(0, str(_CHECKOUT_ROOT))


def _sweep_stale_run_dirs(root: Path) -> None:
    """Best-effort removal of run dirs abandoned by earlier pytest processes."""
    try:
        candidates = list(root.glob("run-*"))
    except OSError:
        return
    now = time.time()
    for child in candidates:
        try:
            if now - child.stat().st_mtime > _STALE_RUN_MAX_AGE_SECONDS:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


def _default_basetemp() -> Path:
    """Return a checkout-independent, per-run temp root.

    每次运行使用全新名字：Windows 上残留句柄会把旧目录变成
    delete-pending，任何复用同名目录的方案（含按 PID 命名）都会让
    pytest 启动清理后的 mkdir 直接抛 WinError 5。专用根目录本身被
    锁死时降级到同名的随机兄弟目录，保证测试入口永远可用。
    """
    root = Path(tempfile.gettempdir()) / _PYTEST_BASETEMP_ROOT
    _sweep_stale_run_dirs(root)
    candidate = root / f"run-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        candidate.mkdir(parents=True)
    except OSError:
        candidate = Path(tempfile.gettempdir()) / (
            f"{_PYTEST_BASETEMP_ROOT}-{uuid.uuid4().hex[:8]}"
        )
        candidate.mkdir(parents=True)
    return candidate


def _set_default_basetemp(config: pytest.Config) -> None:
    """Keep pytest temp data in a dedicated, per-run system-temp directory."""
    if config.getoption("--basetemp", default=None):
        return
    config.option.basetemp = str(_default_basetemp())
    setattr(config.option, _BASETEMP_DEFAULT_FLAG, True)


def _warn_if_tempdir_inside_checkout(config: pytest.Config) -> None:
    """pytest 的临时目录（--basetemp / tmp_path）必须放在本 checkout 之外。

    临时目录位于 checkout 内时，room_bootstrap 的工作区解析会从测试
    临时目录向上查找 .agentchatroom/project.json 并命中仓库自身的登记
    文件，使 test_bootstrap / test_project_registration 中依赖
    project_not_registered / 空 checkout 作用域的用例误报
    registration_invalid 或读取到真实 project_key。这里只提示，不改变
    任何测试行为。
    """
    global _REGISTRATION_WARNED
    if _REGISTRATION_WARNED:
        return
    _REGISTRATION_WARNED = True
    basetemp = config.getoption("--basetemp", "")
    basetemp_path = Path(basetemp).resolve() if basetemp else None
    if basetemp_path is None:
        return
    try:
        basetemp_path.relative_to(_CHECKOUT_ROOT)
    except ValueError:
        return
    print(
        "\n[agentchatroom 测试提示] pytest 临时目录（--basetemp）位于本 checkout 内："
        f"{basetemp_path}。bootstrap 的工作区解析会向上查找 .agentchatroom/project.json "
        "并命中仓库自身登记，test_bootstrap / test_project_registration 会因此误报 "
        "registration_invalid。请改用 checkout 之外的 --basetemp（例如系统临时目录或 "
        "仓库外的专用目录）后重跑。\n",
        file=sys.stderr,
    )


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    _set_default_basetemp(config)
    _warn_if_tempdir_inside_checkout(config)


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "data",
        heartbeat_timeout_seconds=30,
        session_token_ttl_seconds=3600,
        default_lease_ttl_seconds=300,
        max_lease_ttl_seconds=3600,
        sse_poll_interval_seconds=0.01,
    )


@pytest.fixture()
def service(settings):
    instance = AgentChatRoomService(Database(settings.database_path), settings)
    instance.initialize()
    return instance


@pytest.fixture()
def project_dir(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    return path


@pytest.fixture()
def project(service, project_dir):
    return service.create_project(root_path=str(project_dir), name="Test Project")


def bind_workspace(service, project):
    return service.register_workspace(
        project["id"],
        host_key="test-host",
        host_name="Test Host",
        local_path=project["root_path"],
    )


def join_room_with_workspace(service, project, **kwargs):
    registered = bind_workspace(service, project)
    kwargs.setdefault("worktree", project["root_path"])
    kwargs.setdefault("host_id", registered["host"]["id"])
    kwargs.setdefault("workspace_id", registered["workspace"]["id"])
    return service.join_room(project["id"], **kwargs)


@pytest.fixture()
def joined_agents(service, project):
    executor = join_room_with_workspace(
        service,
        project,
        agent_key="builder-main",
        name="Builder",
        client="codex",
        model="test-model",
        role="executor",
    )
    reviewer = join_room_with_workspace(
        service,
        project,
        agent_key="reviewer-main",
        name="Reviewer",
        client="qoder",
        model="test-model",
        role="reviewer",
    )
    return executor, reviewer
