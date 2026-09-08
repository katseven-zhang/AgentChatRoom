"""Single-exe entry for the packaged AgentChatRoom application.

The distribution carries exactly one executable; the first argument
selects the delivery mode so every capability ships inside that exe:

- no argument, ``gui``, or an option flag: open the pywebview GUI shell
  panel (the default double-click behavior; the shell starts and stops
  the detached service, and accepts the shell's own --port/--mode/...);
- ``mcp``: run the MCP stdio server for Agent host integration;
- any other subcommand: delegate to the console CLI (``serve``,
  ``stop``, ``logs``, ...).
"""

from __future__ import annotations

import sys


def main() -> None:
    args = sys.argv[1:]
    command = args[0] if args else ""
    if command == "mcp":
        from agentchatroom.stdio_runtime import run_mcp_entry

        run_mcp_entry(args[1:])
        return
    from agentchatroom.stdio_runtime import prepare_standard_streams

    prepare_standard_streams()
    if command == "" or command == "gui" or command.startswith("-"):
        from agentchatroom.shell import main as shell_main

        shell_main(args[1:] if command == "gui" else args)
        return
    from agentchatroom.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    from multiprocessing import freeze_support

    freeze_support()
    main()
