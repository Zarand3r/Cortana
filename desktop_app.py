#!/usr/bin/env python3
"""py2app entry point for the Cortana menu-bar desktop app.

Building: ``python setup.py py2app`` (see docs/DESKTOP.md). Running from source:
``python -m cortana desktop`` after ``pip install -r requirements-desktop.txt``.
"""
from cortana.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["desktop"]))
