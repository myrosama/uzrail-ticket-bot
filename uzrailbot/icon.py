"""The app icon (a white train front on a coloured tile), drawn with Pillow so the tray can recolour it by state."""
from __future__ import annotations

GREEN = "#08BFA8"


def draw_icon(color: str = GREEN, size: int = 64):
    from PIL import Image, ImageDraw
    big = 1024                                   # draw large, then downscale for smooth edges
    s = big / 64
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    box = lambda *v: tuple(round(x * s) for x in v)
    d.rounded_rectangle(box(2, 2, 62, 62), radius=round(16 * s), fill=color)
    d.rounded_rectangle(box(18, 13, 46, 42), radius=round(6 * s), fill="white")
    d.rounded_rectangle(box(22, 18, 42, 27), radius=round(1.5 * s), fill=color)
    d.ellipse(box(21.5, 32.5, 27.5, 38.5), fill=color)
    d.ellipse(box(36.5, 32.5, 42.5, 38.5), fill=color)
    d.polygon([box(21, 44)[:2], box(26.5, 44)[:2], box(22, 51)[:2], box(16.5, 51)[:2]], fill="white")
    d.polygon([box(37.5, 44)[:2], box(43, 44)[:2], box(47.5, 51)[:2], box(42, 51)[:2]], fill="white")
    return img.resize((size, size), Image.LANCZOS)
