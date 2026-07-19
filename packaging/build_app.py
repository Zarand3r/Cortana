"""py2app build config for the Cortana menu-bar desktop app.

Do not run directly — use ``./scripts/build_release.sh``, which installs the desktop +
MLX + build deps into a clean venv, runs this, then signs/notarizes/DMGs the result.

The bundle owns its own TCC grant (Screen Recording), so perception + chat + on-device
MLX inference run under one permission boundary. LSUIElement makes it a menu-bar-only
app (top of screen, no Dock icon). The model itself is NOT bundled — it downloads once
on first run (see cortana.runtime), keeping the artifact ~small.

Verify the built bundle on a real Mac (no GUI/py2app in CI); see docs/DESKTOP.md.
"""
from pathlib import Path

from setuptools import setup

_ROOT = Path(__file__).resolve().parent.parent
APP = [str(_ROOT / "desktop_app.py")]

OPTIONS = {
    "argv_emulation": False,
    "packages": [
        "cortana",
        # Bundled local runtime + its downloader (single-artifact: no external Ollama).
        "mlx", "mlx_lm", "huggingface_hub",
        # Native shell.
        "rumps", "webview", "objc",
    ],
    "includes": ["cortana.runtime"],
    # Ship the chat UI + default config next to the code.
    "resources": [str(_ROOT / "cortana" / "webui" / "index.html"),
                  str(_ROOT / "config" / "cortana.toml")],
    "plist": {
        "CFBundleName": "Cortana",
        "CFBundleDisplayName": "Cortana",
        "CFBundleIdentifier": "com.cortana.app",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "LSUIElement": True,                 # menu-bar-only (no Dock icon)
        "LSMinimumSystemVersion": "13.0",    # ScreenCaptureAccess APIs
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "On-device. Your screen data never leaves this Mac.",
        # Screen Recording has no Info.plist usage key — it's a runtime TCC grant
        # requested via CGRequestScreenCaptureAccess (cortana.runtime). Left as a note
        # so nobody adds a bogus key expecting it to matter.
    },
}

setup(
    name="Cortana",
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
