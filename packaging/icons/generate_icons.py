"""Regenerate packaging/icons/* from the repository's files.ico.

The generated files are committed, so CI does not need Pillow. Re-run after changing
files.ico:

    uv run --no-project --with pillow python packaging/icons/generate_icons.py

files.ico contains 16, 32, 48, 64, 96, 128 and 256 px frames. PNGs are only written for
sizes that exist natively (no upscaling). The .icns is produced from the 256 px frame;
Pillow fills the larger icns slots (512/1024) by resampling, which macOS only uses for
very large Finder previews.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
ICO = ROOT / "files.ico"
NAME = "fast-file-search-pro"
PNG_SIZES = (16, 32, 48, 64, 128, 256)


def main() -> None:
    ico = Image.open(ICO)
    available = set(ico.ico.sizes())
    frames: dict[int, Image.Image] = {}
    for w, h in sorted(available):
        if w != h:
            continue
        frame = ico.ico.getimage((w, h)).convert("RGBA")
        frames[w] = frame

    for size in PNG_SIZES:
        if size not in frames:
            print(f"skip {size}px (not in {ICO.name})")
            continue
        out = HERE / f"{NAME}-{size}.png"
        frames[size].save(out, optimize=True)
        print(f"wrote {out.relative_to(ROOT)}")

    biggest = frames[max(frames)]
    icns = HERE / f"{NAME}.icns"
    extra = [frames[s] for s in sorted(frames) if s in (16, 32, 64, 128) and s != biggest.width]
    biggest.save(icns, format="ICNS", append_images=extra)
    print(f"wrote {icns.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
