from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


def make_icon(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    scale = size / 64
    shield = [(32, 3), (56, 12), (53, 39), (43, 53), (32, 61), (21, 53), (11, 39), (8, 12)]
    draw.polygon([(round(x * scale), round(y * scale)) for x, y in shield], fill=(28, 132, 198, 255))
    white = (255, 255, 255, 245)
    width = max(2, round(4 * scale))
    draw.arc((round(16 * scale), round(18 * scale), round(48 * scale), round(49 * scale)), 215, 325, fill=white, width=width)
    draw.arc((round(23 * scale), round(26 * scale), round(41 * scale), round(44 * scale)), 215, 325, fill=white, width=width)
    radius = max(2, round(3.5 * scale))
    cx, cy = round(32 * scale), round(43 * scale)
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=white)
    return image


root = Path(__file__).resolve().parents[1]
asset_dir = root / "assets"
asset_dir.mkdir(parents=True, exist_ok=True)
make_icon(256).save(asset_dir / "guardian.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
make_icon(256).save(asset_dir / "guardian.png")
print(asset_dir / "guardian.ico")
