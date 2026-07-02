"""py2app build config for the Cortana menu-bar desktop app.

    pip install -r requirements-desktop.txt
    python setup.py py2app          # -> dist/Cortana.app

The bundle owns its own TCC grants (Screen Recording / Accessibility), so
perception + chat + inference all run under one permission boundary. See
docs/DESKTOP.md for signing/notarization (required for grants to persist).

NOTE: this build has not been exercised in CI (no GUI / py2app in the test env);
treat it as the starting scaffold and verify on a real Mac.
"""
from setuptools import setup

APP = ["desktop_app.py"]

OPTIONS = {
    "argv_emulation": False,
    "packages": ["cortana"],
    "includes": ["webview", "rumps"],
    # Ship the chat UI and default config alongside the code.
    "resources": ["cortana/webui/index.html", "config/cortana.toml"],
    "plist": {
        "CFBundleName": "Cortana",
        "CFBundleDisplayName": "Cortana",
        "CFBundleIdentifier": "com.cortana.app",
        "CFBundleShortVersionString": "0.1.0",
        "LSUIElement": True,            # menu-bar-only (no Dock icon)
        "NSHighResolutionCapable": True,
    },
}

setup(
    name="Cortana",
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
