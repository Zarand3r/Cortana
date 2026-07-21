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
        # Bundled MLX runtime + model downloader + transformers stack. Whole dirs are
        # copied so py2app's static graph can't miss the many *lazy* imports these
        # libraries do (huggingface_hub/transformers resolve submodules at runtime).
        "mlx_lm", "huggingface_hub", "hf_xet", "transformers", "tokenizers",
        "safetensors", "sentencepiece", "numpy", "fsspec", "filelock", "tqdm",
        "packaging", "regex", "yaml", "jinja2", "markupsafe",
        # http stack that huggingface_hub lazy-imports for downloads.
        "httpx", "httpcore", "h11", "anyio", "certifi", "idna",
        # transitive CLI/formatting deps pulled by transformers/typer.
        "rich", "pygments", "markdown_it", "mdurl", "typer", "click", "shellingham",
        # Native shell.
        "rumps", "webview", "objc", "proxy_tools",
    ],
    "includes": ["cortana.runtime",
                 # single-module deps transformers/hf import that the static graph
                 # otherwise misses (they aren't reached from a package __init__).
                 "typing_extensions", "annotated_doc", "_yaml"],
    # `mlx` is a PEP 420 namespace package (no __init__.py). py2app cannot freeze it
    # correctly: `packages` crashes its legacy bootstrap finder, and `includes`
    # synthesizes a REGULAR mlx package inside python314.zip that shadows the
    # filesystem portion (breaking mlx._reprlib_fix etc. at boot). So it is excluded
    # from the freeze entirely; scripts/build_release.sh rsyncs the complete wheel
    # package (all submodules + native lib/) into lib-dynload/mlx, which resolves as
    # a normal filesystem namespace portion at runtime.
    "excludes": ["mlx"],
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
