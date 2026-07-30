#!/usr/bin/env python3
"""py2app entry point for the Cortana menu-bar desktop app.

Building the .app:  ./scripts/build_release.sh   (py2app -> codesign -> notarize -> dmg)
Running from source: ``python -m cortana desktop``.

The chat window subprocess does NOT come through here: in a py2app bundle
``sys.executable`` is the bundled plain interpreter (Contents/MacOS/python), so
``cortana.desktop._spawn_chat_window`` runs the ordinary ``-m cortana chat-window``
entry with the parent's sys.path exported via PYTHONPATH. The only env dispatch this
script handles is the build pipeline's headless ``CORTANA_CHILD=selfcheck``.
"""
import os
import sys


def _main() -> int:
    child = os.environ.get("CORTANA_CHILD")
    if child == "selfcheck":
        # Headless freeze smoke-test through the REAL bundle boot path (used by the
        # build before signing): import the full runtime AND the native GUI/sensor
        # stack (import-only — no window, no capture, no TCC prompt, no model, no
        # network), then confirm the frozen app selects the bundled MLX default.
        import cortana.agent, cortana.chatapp, cortana.desktop, cortana.memory  # noqa: F401
        import mlx.core, mlx_lm, transformers, huggingface_hub  # noqa: F401
        import rumps, webview, Quartz, Vision, AppKit  # noqa: F401 - GUI/sensor freeze check
        from PyObjCTools import AppHelper  # noqa: F401
        from cortana import runtime
        from cortana.config import Config
        cfg = Config()
        runtime.apply_production_defaults(cfg)          # frozen -> mlx
        print(f"SELFCHECK OK backend={cfg.backend} model={cfg.model} "
              f"transformers={transformers.__version__}")
        return 0
    from cortana.cli import main
    return main(["desktop"])


if __name__ == "__main__":
    raise SystemExit(_main())
