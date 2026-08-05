"""First-run runtime provisioning for the packaged (.app) build.

The single-artifact goal: a user double-clicks Cortana.app and it *just works* — no
Terminal, no venv, no separate Ollama install. To get there the app must, on first
launch, (1) run on a bundled local runtime (MLX on Apple Silicon), (2) fetch the
model once, and (3) hold the Screen Recording grant.

This module holds the **testable** provisioning logic (readiness state machine, model
cache detection, production backend selection). The genuinely native bits — the HF
download, the TCC permission check/request — are thin ``# pragma: no cover`` wrappers
verified on a real Mac (see docs/DESKTOP.md).
"""

from __future__ import annotations

import os
import sys
from enum import Enum, auto
from pathlib import Path

# The bundled single-artifact runtime is MLX (Apple Silicon, no external server). A
# 4-bit 7B instruct model is the ~4 GB first-run download the user opted into.
DEFAULT_MLX_MODEL = "mlx-community/Qwen2.5-7B-Instruct-4bit"
_DEFAULT_OLLAMA_MODEL = "qwen2.5:7b-instruct"   # the dev default we replace when frozen


class RuntimeState(Enum):
    """What first-run setup still needs before tracking can start."""

    READY = auto()               # model present + Screen Recording granted
    MODEL_MISSING = auto()       # need the one-time model download
    PERMISSION_NEEDED = auto()   # need the Screen Recording TCC grant


def is_frozen() -> bool:
    """True inside a py2app/PyInstaller bundle (vs running from source)."""
    return bool(getattr(sys, "frozen", False))


def apply_production_defaults(cfg) -> None:
    """Reconcile config with the runtime actually in use.

    * Frozen .app: default to the bundled MLX runtime (no external Ollama) unless
      the user chose a backend themselves. No-op from source (dev keeps Ollama).
    * Any mode: ``backend = "mlx"`` with the *Ollama* default model tag is a
      config hole (an Ollama tag is not a valid HF repo id — ``mlx_lm.load``
      would fail); swap in the MLX default model so the combination works."""
    if is_frozen() and cfg.backend == "ollama":      # untouched dev default -> bundle default
        cfg.backend = "mlx"
    if cfg.backend == "mlx" and cfg.model == _DEFAULT_OLLAMA_MODEL:
        cfg.model = DEFAULT_MLX_MODEL


def enforce_offline() -> None:
    """Hard-disable huggingface_hub network access (and its telemetry) for this
    process. Called the moment the model is confirmed in the local cache: without
    it, every ``mlx_lm.load`` revalidates the cached model against huggingface.co
    over HTTPS — real egress, observed live via lsof, violating the 'first-run
    download is the ONE network exception' promise.

    The env vars alone are NOT enough on the first-run path: huggingface_hub
    freezes them into module constants at import time, and ``ensure_model`` has
    already imported it to download — so patch the live module too."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    constants = getattr(sys.modules.get("huggingface_hub"), "constants", None)
    if constants is not None:                    # already imported (post-download)
        constants.HF_HUB_OFFLINE = True
        constants.HF_HUB_DISABLE_TELEMETRY = True


def hf_cache_dir() -> Path:
    """Where huggingface_hub / mlx-lm cache downloaded models."""
    home = os.environ.get("HF_HOME")
    if home:
        return Path(home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def is_model_available(repo: str) -> bool:
    """True when ``repo`` has already been downloaded to the local HF cache — i.e. the
    first-run download is done and inference will not hit the network."""
    snapshots = hf_cache_dir() / ("models--" + repo.replace("/", "--")) / "snapshots"
    if not snapshots.is_dir():
        return False
    return any(snap.is_dir() and any(snap.iterdir()) for snap in snapshots.iterdir())


def readiness(*, model_available: bool, screen_recording: bool) -> tuple[RuntimeState, str]:
    """Map the two first-run preconditions onto a state + a user-facing message. Model
    download is the long pole, so it's surfaced first (the user can grant the
    permission while it downloads)."""
    if not model_available:
        return (RuntimeState.MODEL_MISSING,
                "Preparing Cortana — downloading the local model (one time, ~4 GB)…")
    if not screen_recording:
        return (RuntimeState.PERMISSION_NEEDED,
                "Cortana needs Screen Recording permission. Grant it in System "
                "Settings → Privacy & Security → Screen Recording, then Start Cortana.")
    return (RuntimeState.READY, "Cortana is ready.")


# --------------------------------------------------------------------------- #
# Native provisioning (macOS). Verified on a real Mac; not in the hermetic CI. #
# --------------------------------------------------------------------------- #
def _coregraphics():  # pragma: no cover - native dylib handle
    """The TCC preflight/request functions are NOT bound by pyobjc's Quartz module
    (calling them there raises AttributeError — which a fail-soft wrapper once
    swallowed into a permanent 'not granted', wedging first-run setup). Call the
    C API directly, as the repo already does for IsSecureEventInputEnabled."""
    import ctypes
    cg = ctypes.CDLL(
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
    cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
    return cg


def screen_recording_granted() -> bool:  # pragma: no cover - native TCC preflight
    """Whether this process holds the Screen Recording grant (no prompt)."""
    try:
        return bool(_coregraphics().CGPreflightScreenCaptureAccess())
    except Exception:  # noqa: BLE001 - no CoreGraphics (non-mac) reads as "not granted"
        return False


def request_screen_recording() -> bool:  # pragma: no cover - native TCC request
    """Trigger the one-time Screen Recording prompt; returns the resulting grant."""
    try:
        return bool(_coregraphics().CGRequestScreenCaptureAccess())
    except Exception:  # noqa: BLE001
        return False


def open_screen_recording_settings() -> None:  # pragma: no cover - opens System Settings
    """Deep-link to the Screen Recording pane so the user can flip the toggle."""
    import subprocess
    subprocess.Popen([
        "open",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    ])


def ensure_model(repo: str, progress=None) -> None:  # pragma: no cover - network + native runtime
    """Download ``repo`` into the local cache if absent (the one-time, one network
    exception). Idempotent — a no-op once cached; thereafter inference is offline."""
    if is_model_available(repo):
        return
    if progress is not None:
        progress(f"Downloading {repo}…")
    from huggingface_hub import snapshot_download  # lazy: only the frozen app needs it
    snapshot_download(repo)
