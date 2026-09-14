from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


def test_default_pytest_temp_root_is_dedicated_and_external(tmp_path, request):
    """The project default must not create temp directories in the checkout."""
    if not getattr(request.config.option, "_agentchatroom_basetemp_default", False):
        pytest.skip("运行方显式指定了 --basetemp，项目默认临时目录规则未生效")

    system_temp = Path(tempfile.gettempdir()).resolve()
    checkout_root = Path(__file__).resolve().parents[1]
    resolved = tmp_path.resolve()

    assert resolved.is_relative_to(system_temp)
    assert resolved.relative_to(system_temp).parts[0].startswith("agentchatroom-pytest")
    # 每次运行都使用全新目录名（正常路径为专用根下的 run-* 子目录，
    # 专用根被锁死时为随机命名的兄弟根目录），拒绝复用固定目录。
    run_dir_name = resolved.parent.name
    assert run_dir_name.startswith("run-") or run_dir_name.startswith(
        "agentchatroom-pytest-"
    )
    assert not resolved.is_relative_to(checkout_root)
