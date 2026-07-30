# Shipping Cortana as a single artifact

Goal: a user double-clicks **`Cortana.dmg`**, drags `Cortana.app` to Applications,
launches it, and it lives in the **menu bar** (top of screen) — **no Terminal, no
venv, ever**. First launch downloads the model once; thereafter it runs 100% offline.

## Decisions (locked)

- **Runtime:** bundled **MLX** (Apple Silicon). A frozen `.app` defaults to MLX
  automatically (`cortana.runtime.apply_production_defaults`) — no external Ollama.
  The model (`mlx-community/Qwen2.5-7B-Instruct-4bit`, ~4 GB) is **not** bundled; it
  downloads once on first run into the HF cache (`cortana.runtime.ensure_model`).
- **Distribution:** **Developer ID**–signed + **notarized** DMG, so it installs
  cleanly on any Apple-Silicon Mac and the Screen Recording grant survives updates.

## Build the artifact

```bash
export SIGN_ID="Developer ID Application: Your Name (TEAMID)"
export NOTARY_PROFILE="cortana-notary"   # see the script header for store-credentials
./scripts/build_release.sh               # -> dist/Cortana.dmg (signed + notarized)
```

The script: clean venv → `pip install '.[desktop,mlx,build]'` → py2app → `codesign`
(hardened runtime + `bundle/entitlements.plist`) → `create-dmg` → `notarytool
submit --wait` → `stapler staple`. Without `SIGN_ID`/`NOTARY_PROFILE` it stops early
with an unsigned/un-notarized bundle (fine for local use).

## First-run behavior (built in)

On launch the menu bar shows a status line while a background thread:
1. downloads the model if absent (the one-time network exception), then
2. requests the Screen Recording grant (`CGRequestScreenCaptureAccess`).

"Start Cortana" is gated until both hold; a crashed tracker is surfaced as
`⚠️ Cortana stopped (error)` (never a silent dead loop).

## Real-Mac verification checklist (the one thing CI cannot cover)

Everything native is `# pragma: no cover`; the logic behind it is unit-tested, but the
bundle itself must be exercised on hardware once per release:

- [ ] `./scripts/build_release.sh` produces `dist/Cortana.dmg`; `stapler validate` passes.
- [ ] Fresh Mac (or a new user account): mount DMG, drag to Applications, launch — a
      menu-bar icon appears, **no Dock icon**, no Terminal.
- [ ] First run: status line shows the model download; completes; then the Screen
      Recording prompt appears. Grant it.
- [ ] "Start Cortana" → the chat window opens; ask "what am I doing right now" → a
      grounded answer citing the current app/window.
- [ ] Window title is populated in answers/citations (regression: it used to be blank).
- [ ] "Get Recommendation" → an alert appears (no crash, no beachball).
- [ ] Close the chat window → tracking stops (menu flips to "Start"); no mixed state.
- [ ] Quit → app exits within a few seconds (drain), no beachball.
- [ ] Relaunch offline (Wi-Fi off) → everything works; no network calls except the
      already-cached model. Confirm with Little Snitch / `nettop` that only 127.0.0.1
      traffic occurs after the first-run download.
- [ ] Rebuild + reinstall → Screen Recording grant persists (proves signing/notarization).

## Build-pipeline status

The unsigned pipeline is **validated end-to-end on a real Mac**: fresh pinned venv →
py2app → full-`mlx` rsync → dist-info metadata → headless boot self-check through the
actual app binary (`SELFCHECK OK backend=mlx …`). The py2app/MLX freeze traps found
along the way (namespace-package shadowing, unsigned-dylib linkage, `--deep` signing,
metadata stripping) are fixed and regression-guarded by the self-check gate; see
REVIEW.md §1c. The signing/notarization steps (6–8) are written but have not yet run
with a real certificate — expect at most one iteration there.

Config resolution in the shipped app: `~/.config/cortana/cortana.toml` (user-editable,
survives updates) → the bundle's `Resources/cortana.toml` → in-code defaults.

## Known follow-ups (not blockers, tracked in REVIEW.md §3)

- App icon (`.icns`) not yet set — bundle uses the default.
- `create-dmg` styling (icon layout) unverified; the `hdiutil` fallback always works.
