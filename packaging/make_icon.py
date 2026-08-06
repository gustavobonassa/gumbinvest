"""Generate the icon assets — same chart-line mark everywhere.

Outputs (all git-ignored, drawn at build time by build.ps1):
- packaging/assets/gumbinvest.ico   — the server exe icon
- desktop-shell/build/icon.ico      — the Electron app/installer icon
- desktop-shell/assets/tray.png     — the tray icon
"""
from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image, ImageDraw

PACKAGING = Path(__file__).parent
SHELL = PACKAGING.parent / "desktop-shell"


def draw(size: int) -> Image.Image:
    image = Image.new("RGB", (size, size), (16, 122, 87))
    scale = size / 64
    pen = ImageDraw.Draw(image)
    points = [(10, 46), (26, 30), (38, 38), (54, 16)]
    pen.line([(x * scale, y * scale) for x, y in points], fill=(255, 255, 255), width=max(int(6 * scale), 1))
    return image


def main() -> None:
    ico = PACKAGING / "assets" / "gumbinvest.ico"
    ico.parent.mkdir(parents=True, exist_ok=True)
    draw(256).save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

    shell_ico = SHELL / "build" / "icon.ico"
    shell_ico.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ico, shell_ico)

    # macOS: electron-builder converts build/icon.png (512px+) to icns itself.
    shell_png = SHELL / "build" / "icon.png"
    draw(512).save(shell_png)

    tray = SHELL / "assets" / "tray.png"
    tray.parent.mkdir(parents=True, exist_ok=True)
    draw(32).save(tray)

    print(f"icons written: {ico}, {shell_ico}, {shell_png}, {tray}")


if __name__ == "__main__":
    main()
