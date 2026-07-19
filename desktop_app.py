#!/usr/bin/env python3
"""py2app entry point for the Cortana menu-bar desktop app.

Building the .app:  ./scripts/build_release.sh   (py2app -> codesign -> notarize -> dmg)
Running from source: ``python -m cortana desktop``.

Child-window dispatch: inside a frozen bundle ``sys.executable`` is the app binary and
``-m cortana chat-window`` isn't runnable, so the chat window is launched by re-exec'ing
THIS binary with ``CORTANA_CHILD`` set (see cortana.desktop._spawn_chat_window). That
branch is handled here before the normal menu-bar launch.
"""
import os
import sys


def _main() -> int:
    if os.environ.get("CORTANA_CHILD") == "chat-window":
        from cortana.desktop import run_chat_window
        run_chat_window(os.environ["CORTANA_CHILD_URL"])
        return 0
    from cortana.cli import main
    return main(["desktop"])


if __name__ == "__main__":
    raise SystemExit(_main())
