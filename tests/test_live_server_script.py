from scripts.verify_live_server import EXPECTED_TOOLS, parse_args


def test_live_server_script_accepts_token_from_environment(monkeypatch):
    monkeypatch.setenv("AGENTCHATROOM_SERVER_URL", "https://room.example.com/mcp")
    monkeypatch.setenv("AGENTCHATROOM_AGENT_TOKEN", "acr.placeholder.secret")

    args = parse_args(
        [
            "--project-id",
            "project_test",
            "--workspace",
            r"C:\AgentWorkspaces\project",
        ]
    )

    assert args.url == "https://room.example.com/mcp"
    assert args.token == "acr.placeholder.secret"
    assert args.task_id == ""


def test_live_server_script_accepts_exact_task_id():
    args = parse_args(
        [
            "--url",
            "https://room.example.com/mcp",
            "--token",
            "acr.placeholder.secret",
            "--project-id",
            "project_test",
            "--task-id",
            "task_test",
            "--workspace",
            r"C:\AgentWorkspaces\project",
        ]
    )

    assert args.task_id == "task_test"
    assert "task_get" in EXPECTED_TOOLS


def test_live_server_script_requires_mcp_endpoint(monkeypatch):
    monkeypatch.setenv("AGENTCHATROOM_AGENT_TOKEN", "acr.placeholder.secret")

    try:
        parse_args(
            [
                "--url",
                "https://room.example.com",
                "--project-id",
                "project_test",
                "--workspace",
                r"C:\AgentWorkspaces\project",
            ]
        )
    except SystemExit as error:
        assert error.code == 2
    else:  # pragma: no cover
        raise AssertionError("parse_args should reject a non-MCP URL")
