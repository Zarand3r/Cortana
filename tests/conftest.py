"""Shared test fixtures."""

import sys
from pathlib import Path

# Make the repo root importable so `import cortana...` works when pytest is run
# from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
