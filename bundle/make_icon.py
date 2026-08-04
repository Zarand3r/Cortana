"""Generate bundle/Cortana.icns — the app icon (a glowing ring on deep navy).

Pure Quartz (pyobjc, already a desktop dep) + macOS `iconutil`; no image libs.
Run from the repo root with the build venv:  .venv-build/bin/python bundle/make_icon.py
Idempotent; commit the resulting .icns so normal builds don't need this step.
"""
from __future__ import annotations

import pathlib
import subprocess
import tempfile

import Quartz

SIZE = 1024


def _draw(ctx) -> None:
    # Deep-navy rounded square (macOS icon grid: ~10% corner radius, small inset).
    inset, radius = SIZE * 0.05, SIZE * 0.18
    rect = Quartz.CGRectMake(inset, inset, SIZE - 2 * inset, SIZE - 2 * inset)
    path = Quartz.CGPathCreateWithRoundedRect(rect, radius, radius, None)
    Quartz.CGContextSetRGBFillColor(ctx, 0.043, 0.055, 0.135, 1.0)   # #0B0E22
    Quartz.CGContextAddPath(ctx, path)
    Quartz.CGContextFillPath(ctx)
    # Glowing blue-violet ring: wide soft halo strokes under a crisp core.
    cx = cy = SIZE / 2
    r = SIZE * 0.30
    for width, alpha in ((SIZE * 0.10, 0.10), (SIZE * 0.065, 0.22),
                         (SIZE * 0.04, 0.45), (SIZE * 0.022, 1.0)):
        Quartz.CGContextSetRGBStrokeColor(ctx, 0.45, 0.62, 1.0, alpha)  # #73A0FF
        Quartz.CGContextSetLineWidth(ctx, width)
        Quartz.CGContextStrokeEllipseInRect(
            ctx, Quartz.CGRectMake(cx - r, cy - r, 2 * r, 2 * r))
    # Center spark.
    for rr, alpha in ((SIZE * 0.055, 0.25), (SIZE * 0.03, 0.9)):
        Quartz.CGContextSetRGBFillColor(ctx, 0.72, 0.82, 1.0, alpha)
        Quartz.CGContextFillEllipseInRect(
            ctx, Quartz.CGRectMake(cx - rr, cy - rr, 2 * rr, 2 * rr))


def main() -> None:
    space = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(
        None, SIZE, SIZE, 8, 0, space, Quartz.kCGImageAlphaPremultipliedLast)
    _draw(ctx)
    image = Quartz.CGBitmapContextCreateImage(ctx)

    out = pathlib.Path(__file__).parent / "Cortana.icns"
    with tempfile.TemporaryDirectory() as td:
        iconset = pathlib.Path(td) / "Cortana.iconset"
        iconset.mkdir()
        master = pathlib.Path(td) / "master.png"
        dest = Quartz.CGImageDestinationCreateWithURL(
            Quartz.CFURLCreateWithFileSystemPath(None, str(master), 0, False),
            "public.png", 1, None)
        Quartz.CGImageDestinationAddImage(dest, image, None)
        Quartz.CGImageDestinationFinalize(dest)
        for pts in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = pts * scale
                name = f"icon_{pts}x{pts}{'@2x' if scale == 2 else ''}.png"
                subprocess.run(["sips", "-z", str(px), str(px), str(master),
                                "--out", str(iconset / name)],
                               check=True, capture_output=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)],
                       check=True)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
