"""Generate assets/app.ico: minimalist line-style Wi-Fi fan.

Design (picked by jieke, candidate C): teal lines on transparency,
two arcs plus a dot, round caps, no background shape. Drawn on a
2048 canvas and downscaled into a multi-size ICO so the tray icon
stays crisp at small sizes.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "assets" / "app.ico"

COLOR = (14, 124, 102, 255)  # teal #0E7C66
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
CANVAS = 2048


def draw_icon() -> Image.Image:
    cx, cy = CANVAS // 2, int(CANVAS * 0.66)
    image = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    width = 120
    for radius in (420, 700):
        bbox = [cx - radius, cy - radius, cx + radius, cy + radius]
        draw.arc(bbox, start=205, end=335, fill=COLOR, width=width)
        for angle in (205, 335):  # round caps at the arc ends
            rad = math.radians(angle)
            x = cx + radius * math.cos(rad)
            y = cy + radius * math.sin(rad)
            draw.ellipse(
                [x - width / 2, y - width / 2, x + width / 2, y + width / 2],
                fill=COLOR,
            )
    dot_r = 105
    draw.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill=COLOR)
    return image


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    image = draw_icon().resize((256, 256), Image.LANCZOS)
    image.save(OUT, format="ICO", sizes=SIZES)
    print(OUT)


if __name__ == "__main__":
    main()
