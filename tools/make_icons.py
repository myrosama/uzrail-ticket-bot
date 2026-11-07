"""Regenerate uzrailbot/ui/icon.png and icon.ico from uzrailbot/icon.py.  python tools/make_icons.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from uzrailbot.icon import draw_icon  # noqa: E402

ui = ROOT / "uzrailbot" / "ui"
draw_icon(size=256).save(ui / "icon.png")
draw_icon(size=256).save(ui / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
draw_icon(size=192).save(ROOT / "docs" / "icon.png")
print("icons written")
