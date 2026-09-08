import os
import json
import subprocess
import sys

import pytest

from agentchatroom import stdio_runtime


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows GUI visibility regression')
def test_packaged_mcp_failure_exits_without_visible_error_window():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    executable = Path(os.environ.get('AGENTCHATROOM_TEST_EXE', str(root / 'dist/agentchatroom/agentchatroom.exe')))
    if not executable.is_file():
        pytest.skip('Packaged executable not available')
    result = subprocess.run(
        [sys.executable, str(root / 'evidence/probe_packaged_mcp.py'), str(executable)],
        capture_output=True, text=True, timeout=15,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    assert result.returncode == 0, result.stderr
    probe = json.loads(result.stdout)
    assert probe['returncode'] == 2
    assert probe['timed_out'] is False
    assert probe['visible_windows'] == 0
    assert probe['stdout_bytes'] == 0
    assert 'service_unavailable' in probe['stderr']
    assert 'Traceback' not in probe['stderr']


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows inherited handle recovery')
def test_windowed_style_none_streams_recover_inherited_pipes():
    code = '''
import sys
from agentchatroom.stdio_runtime import prepare_standard_streams
sys.stdin = sys.stdout = sys.stderr = None
prepare_standard_streams(require_protocol=True)
line = sys.stdin.readline().strip()
sys.stdout.write('reply:' + line + '\\n')
sys.stdout.flush()
sys.stderr.write('diagnostic\\n')
sys.stderr.flush()
'''
    result = subprocess.run([sys.executable, '-c', code], input='hello\n',
                            capture_output=True, text=True, timeout=10,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'reply:hello\n'
    assert result.stderr == 'diagnostic\n'


def test_mcp_wrapper_converts_unexpected_failure_to_safe_exit(monkeypatch, capsys):
    from agentchatroom import mcp_server
    monkeypatch.setattr(stdio_runtime, 'prepare_standard_streams', lambda **kw: None)
    def fail(args):
        raise RuntimeError('secret-runtime-path-must-not-leak')
    monkeypatch.setattr(mcp_server, 'main', fail)
    with pytest.raises(SystemExit) as error:
        stdio_runtime.run_mcp_entry([])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert output.out == ''
    assert output.err == 'agentchatroom mcp unavailable (startup_failed)\n'


def test_mcp_wrapper_preserves_expected_exit_code(monkeypatch):
    from agentchatroom import mcp_server
    monkeypatch.setattr(stdio_runtime, 'prepare_standard_streams', lambda **kw: None)
    def stop(args):
        raise SystemExit(2)
    monkeypatch.setattr(mcp_server, 'main', stop)
    with pytest.raises(SystemExit) as error:
        stdio_runtime.run_mcp_entry([])
    assert error.value.code == 2


def test_missing_pipe_does_not_allocate_console(monkeypatch):
    from types import SimpleNamespace
    fake_sys = SimpleNamespace(stdin=None, stdout=None, stderr=None)
    monkeypatch.setattr(stdio_runtime, 'sys', fake_sys)
    monkeypatch.setattr(stdio_runtime, '_inherited_stream', lambda *a, **kw: None)
    try:
        with pytest.raises(RuntimeError, match='mcp_stdio_unavailable'):
            stdio_runtime.prepare_standard_streams(require_protocol=True)
    finally:
        for name in ('stdin', 'stdout', 'stderr'):
            stream = getattr(fake_sys, name)
            if stream is not None:
                stream.close()
