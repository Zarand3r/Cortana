"""Perception & meaning-extraction tools — the agent's senses.

Pure logic (dataclasses, normalize/hash/changed, prompt building, extract_meaning)
is importable with no native deps. Native capture/OCR use lazy imports so this
module loads without PyObjC (P7). Filled in at Step 1 / Step 2.
"""

from __future__ import annotations
