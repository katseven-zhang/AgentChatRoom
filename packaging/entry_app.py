"""Single-exe entry for the packaged AgentChatRoom application.

The distribution carries exactly one executable; the first argument
selects the delivery mode so every capability ships inside that exe:

- no argument, ``gui``, or an option flag: open the lightweight Tk
  control console (the default double-click behavior; the console
  starts and stops the detached service, and accepts ``--config``);
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
        from agentchatroom.gui import run_gui

        gui_args = args[1:] if command == "gui" else args
        config_path = None
        if "--config" in gui_args:
            flag_index = gui_args.index("--config")
            if flag_index + 1 < len(gui_args):
                config_path = gui_args[flag_index + 1]
        if "--autostart" in gui_args:
            run_gui(config_path, autostart=True)
        else:
            run_gui(config_path)
        return
    from agentchatroom.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    from multiprocessing import freeze_support

    freeze_support()
    main()
