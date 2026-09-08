"""Offline regressions for the independent 2026-09-08 review."""
import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentchatroom import mcp_server as m
from agentchatroom.bootstrap import bootstrap_local_room
from agentchatroom.errors import DomainError
from agentchatroom.project_registration import register_checkout_project
from agentchatroom.service_lifetime import running_service, require_running_service


@pytest.mark.parametrize('windows_flag', [0, 0x08000000])
def test_background_git_and_backup_commands_do_not_create_consoles(monkeypatch, tmp_path, service, windows_flag):
    from agentchatroom import services, project_registration, backup

    calls = []
    monkeypatch.setattr(subprocess, 'CREATE_NO_WINDOW', windows_flag, raising=False)

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs.get('creationflags') == windows_flag
        assert not kwargs.get('shell', False)
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    services._project_git_info(tmp_path)
    project_registration._git_info(tmp_path)
    service._collect_git_evidence_from_worktree(tmp_path, 'HEAD')
    backup._run_database_tool(['pg_dump', '--version'], action='test')
    assert len(calls) == 6


def test_service_absent_startup_never_initializes_or_spawns(monkeypatch, settings):
    monkeypatch.setattr(m, 'load_settings', lambda: settings)

    def forbidden(*args, **kwargs):
        pytest.fail('Unavailable service must not initialize an engine or spawn children')

    monkeypatch.setattr(m, 'get_service', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    with pytest.raises(SystemExit) as result:
        m.main([])
    assert result.value.code == m.MCP_STARTUP_UNAVAILABLE_EXIT_CODE
    assert not settings.data_dir.exists()


def test_repeated_real_stdio_start_without_service_exits_without_data(tmp_path):
    environment = os.environ.copy()
    environment.pop('AGENTCHATROOM_CONFIG', None)
    data_dir = tmp_path / 'never-started'
    environment['AGENTCHATROOM_DATA_DIR'] = str(data_dir)
    for _ in range(3):
        result = subprocess.run(
            [sys.executable, '-m', 'agentchatroom.mcp_server'],
            input='', capture_output=True, text=True, env=environment,
            timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        assert result.returncode == m.MCP_STARTUP_UNAVAILABLE_EXIT_CODE
        assert 'service_unavailable' in result.stderr
        assert 'Traceback' not in result.stderr
        assert result.stdout == ''
        assert not data_dir.exists()


class WorkspaceSession:
    def __init__(self, path):
        self.path = path

    async def list_roots(self):
        return SimpleNamespace(roots=[SimpleNamespace(uri=self.path.as_uri())])


@pytest.fixture
def rooms(monkeypatch, service, tmp_path):
    paths = {}
    projects = {}
    for name in ('a', 'b', 'plain'):
        path = tmp_path / name
        path.mkdir()
        paths[name] = path
        if name != 'plain':
            project = service.create_project(root_path=str(path), name=name)
            register_checkout_project(path, project)
            projects[name] = project
    monkeypatch.setenv('AGENTCHATROOM_SOFTWARE_KEY', 'regression')
    monkeypatch.setenv('AGENTCHATROOM_SOFTWARE_NAME', 'Regression')
    monkeypatch.setenv('AGENTCHATROOM_SOFTWARE_CLIENT', 'test')
    monkeypatch.delenv('AGENTCHATROOM_PROJECT_PATH', raising=False)
    monkeypatch.setattr(m, 'service', service)
    monkeypatch.setattr(m, '_loaded_identity', None)
    monkeypatch.setattr(m, '_stdio_settings', None)
    monkeypatch.chdir(paths['plain'])
    m.clear_runtime_binding()
    yield paths, projects
    m.clear_runtime_binding()


async def call(name, args, session):
    return await m.mcp._tool_manager.call_tool(name, args,
        context=SimpleNamespace(request_context=SimpleNamespace(session=session)))


@pytest.mark.asyncio
async def test_shared_transport_parallel_tasks_leases_and_rejoin(rooms, service):
    paths, projects = rooms
    a, b = WorkspaceSession(paths['a']), WorkspaceSession(paths['b'])
    boots = await asyncio.gather(call('room_bootstrap', {}, a), call('room_bootstrap', {}, b))
    assert all(r['ok'] for r in boots)
    assert m.mcp_session_key(SimpleNamespace(session=a)) != m.mcp_session_key(SimpleNamespace(session=b))
    tasks = await asyncio.gather(*[call('task_create', {'title': name, 'acceptance_criteria':['ok']}, session)
                                  for name, session in [('a', a), ('b', b)]])
    for name, session, result in [('a', a, tasks[0]), ('b', b, tasks[1])]:
        task = result['result']['task']
        assert task['project_id'] == projects[name]['id']
        assert (await call('task_claim', {'task_id':task['id']}, session))['ok']
        assert (await call('lease_acquire', {'task_id':task['id'], 'path_pattern':'src/same.py'}, session))['ok']
    old_b = service.get_task(projects['b']['id'], tasks[1]['result']['task']['id'])['owner_session_id']
    assert (await call('room_bootstrap', {}, a))['ok']
    assert (await call('session_heartbeat', {}, b))['ok']
    assert service.get_task(projects['b']['id'], tasks[1]['result']['task']['id'])['owner_session_id'] == old_b
    assert (await call('room_sync', {'project_id':projects['a']['id']}, b))['ok'] is False


@pytest.mark.asyncio
async def test_failed_bootstrap_blocks_implicit_and_explicit_old_credentials(rooms):
    paths, _ = rooms
    session = WorkspaceSession(paths['a'])
    assert (await call('room_bootstrap', {}, session))['ok']
    old = m.get_runtime_binding(m.mcp_session_key(SimpleNamespace(session=session)))
    session.path = paths['plain']
    assert not (await call('room_bootstrap', {}, session))['ok']
    for args in ({}, {'project_id':old.project_id, 'session_id':old.session_id, 'token':old.token}):
        result = await call('task_create', dict(title='must not write', acceptance_criteria=['ok'], **args), session)
        assert result['ok'] is False
    session.path = paths['a']
    assert (await call('room_bootstrap', {}, session))['ok']
    assert (await call('task_create', {'title':'restored', 'acceptance_criteria':['ok']}, session))['ok']


@pytest.mark.asyncio
async def test_real_sdk_connections_use_distinct_bindings(rooms):
    from mcp.shared.memory import create_connected_server_and_client_session
    from mcp.types import ListRootsResult, Root
    paths, projects = rooms
    async def roots_a(context):
        return ListRootsResult(roots=[Root(uri=paths['a'].as_uri())])
    async def roots_b(context):
        return ListRootsResult(roots=[Root(uri=paths['b'].as_uri())])
    async def rpc(client, name, args):
        response = await client.call_tool(name, args)
        return response.structuredContent or json.loads(response.content[0].text)
    async with create_connected_server_and_client_session(m.mcp, list_roots_callback=roots_a) as a:
        async with create_connected_server_and_client_session(m.mcp, list_roots_callback=roots_b) as b:
            for client in (a, b):
                assert (await rpc(client, 'room_bootstrap', {}))['ok']
            results = await asyncio.gather(*[rpc(client, 'task_create', {'title':name, 'acceptance_criteria':['ok']})
                                            for client, name in [(a, 'a'), (b, 'b')]])
            assert [r['result']['task']['project_id'] for r in results] == [projects['a']['id'], projects['b']['id']]
            for client, result in zip((a, b), results):
                task_id = result['result']['task']['id']
                assert (await rpc(client, 'task_claim', {'task_id':task_id}))['ok']
                assert (await rpc(client, 'lease_acquire', {'task_id':task_id, 'path_pattern':'src/shared-name.py'}))['ok']
            assert (await rpc(a, 'room_bootstrap', {}))['ok']
            assert (await rpc(b, 'session_heartbeat', {}))['ok']


def test_roots_override_server_cwd_and_unregistered_roots_never_fall_back(rooms, service):
    paths, projects = rooms
    identity = dict(software_key='regression', software_name='Regression', client='test')
    result = bootstrap_local_room(service, workspace_roots=[paths['b']], cwd=paths['a'], **identity)
    assert result.binding.project_id == projects['b']['id']
    invalid = bootstrap_local_room(service, workspace_roots=[paths['plain']], cwd=paths['a'], explicit_project_path=paths['a'], **identity)
    assert invalid.binding is None
    assert invalid.public['status'] == 'project_not_registered'


def test_service_lifetime_requires_live_lock_and_does_not_create_directory(settings):
    with pytest.raises(DomainError, match='explicitly'):
        require_running_service(settings)
    assert not settings.data_dir.exists()
    with running_service(settings):
        require_running_service(settings)
    with pytest.raises(DomainError):
        require_running_service(settings)


@pytest.mark.asyncio
async def test_service_stop_invalidates_local_mcp_binding(rooms, settings, monkeypatch):
    paths, _ = rooms
    session = WorkspaceSession(paths['a'])
    monkeypatch.setattr(m, '_stdio_settings', settings)
    with running_service(settings):
        assert (await call('room_bootstrap', {}, session))['ok']
    assert (await call('task_create', {'title':'stopped', 'acceptance_criteria':['ok']}, session))['error']['code'] == 'service_unavailable'
    with running_service(settings):
        assert not (await call('task_create', {'title':'stale', 'acceptance_criteria':['ok']}, session))['ok']
        assert (await call('room_bootstrap', {}, session))['ok']


@pytest.mark.asyncio
async def test_service_restart_between_requests_also_expires_binding(rooms, settings, monkeypatch):
    paths, _ = rooms
    session = WorkspaceSession(paths['a'])
    monkeypatch.setattr(m, '_stdio_settings', settings)
    with running_service(settings):
        assert (await call('room_bootstrap', {}, session))['ok']
    with running_service(settings):
        result = await call('task_create', {'title':'old generation', 'acceptance_criteria':['ok']}, session)
        assert result['ok'] is False
        assert (await call('room_bootstrap', {}, session))['ok']


@pytest.mark.asyncio
async def test_roots_timeout_fails_closed_without_cwd_fallback(rooms, monkeypatch, service):
    paths, _ = rooms
    session = WorkspaceSession(paths['a'])
    assert (await call('room_bootstrap', {}, session))['ok']
    async def never():
        await asyncio.Event().wait()
    session.list_roots = never
    from dataclasses import replace
    monkeypatch.setattr(service, 'settings', replace(service.settings, mcp_roots_timeout_seconds=0.02))
    result = await call('room_bootstrap', {}, session)
    assert result['error']['code'] == 'workspace_roots_unavailable'
    assert not (await call('task_create', {'title':'after timeout', 'acceptance_criteria':['ok']}, session))['ok']


def test_cancelled_task_cannot_reacquire_resources(service, project):
    joined = service.join_room(project['id'], software_key='regression', name='Regression', client='test', model='unknown')
    task = service.create_task(project['id'], title='cancel', acceptance_criteria=['ok'])['task']
    service.update_task(project['id'], task['id'], status='cancelled')
    with pytest.raises(DomainError) as error:
        service.acquire_lease(project['id'], session_id=joined['agent']['id'], token=joined['token'], task_id=task['id'], path_pattern='src/a.py')
    assert error.value.code == 'task_not_leasable'


def test_hung_window_restore_does_not_block_tray_or_spawn_duplicate_workers(tmp_path):
    from agentchatroom.client_config import ClientConfig, ServerTarget
    from agentchatroom.config import Settings
    from agentchatroom.shell import GuiShell
    shell = GuiShell(Settings(data_dir=tmp_path), ServerTarget.local('http://localhost'), ClientConfig(restore_timeout_seconds=0.05))
    entered, release, failed = threading.Event(), threading.Event(), threading.Event()
    calls = []
    class Window:
        def restore(self):
            calls.append(1)
            entered.set()
            release.wait(3)
        def show(self):
            calls.append(2)
    shell.window = Window()
    shell.tray.icon = SimpleNamespace(notify=lambda *a: failed.set())
    try:
        shell.tray.restore_panel()
        assert entered.wait(1)
        assert failed.wait(1)
        assert shell.window_state == 'restore_failed'
        for _ in range(20):
            shell.tray.restore_panel()
        assert calls == [1]
    finally:
        release.set()
        assert shell.tray._restore_done.wait(1)
    assert calls == [1]  # no late show/visible after the deadline


def test_recent_activity_counts_one_two_three_execute_without_type_error():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root / 'evidence/review_20260908_activity.cjs')], cwd=root, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r['result'] for r in rows] == ['ok', 'ok', 'ok']
