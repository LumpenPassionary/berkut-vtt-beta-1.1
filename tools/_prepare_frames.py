"""Разложить присланные рамки в папку токенов.

Рамка — PNG с прозрачным отверстием: `client/token_frames.py` измеряет
отверстие по альфа-каналу. Два файла пришли в JPEG, где альфы нет вовсе, —
там белый фон заливается прозрачным от края и от центра, иначе отверстие
осталось бы белым, а портрет — во весь квадрат.

Запускать вручную, когда приходят новые рамки::

    python tools/_prepare_frames.py

Скрипт берёт исходники по путям ниже и пишет ``client/assets/frames/*.png``.
"""
import os
import sys

from PIL import Image, ImageDraw

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRAMES = os.path.join(HERE, "client", "assets", "frames")
DOWNLOADS = "G:/загрузки"
PASTES = ("C:/Users/basil/.config/freebuff-desktop/attachment-images/")

#: ключ файла → (исходник, исходник без альфы?)
SOURCES = {
    "wood": (PASTES + "ea1c8276e0b5aa8a9df324feb65cec08fbeb06bb4967b773cbd83ae906e0234d.jpg", True),
    "gold": (DOWNLOADS + "/Рамка золотая.png", False),
    "runes": (PASTES + "c847aed647fb200646015a2c5bc654bdd533a50e9b6a50f28d2488e04cf79b4d.jpg", True),
    "dragon": (DOWNLOADS + "/Рамка дракона.png", False),
    "dragon2": (DOWNLOADS + "/Рамка дракона 2.png", False),
    "boss": (DOWNLOADS + "/Рамка Босса.png", False),
    "boss2": (DOWNLOADS + "/Рамка Босса 2.png", False),
    "boss3": (DOWNLOADS + "/Рамка Босса 3.png", False),
}

#: Свежая белизна остаётся по кромке заливки: эти пиксели тоже прозрачные.
WHITE_LEFTOVER = 242


def cut_white_background(image: Image.Image) -> Image.Image:
    """Белый фон → прозрачный, от края и от отверстия рамки."""
    image = image.convert("RGBA")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    seeds = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1),
             (width // 2, height // 2)]
    for seed in seeds:
        if image.getpixel(seed)[3] == 0:
            continue
        ImageDraw.floodfill(image, seed, (0, 0, 0, 0), thresh=40)
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            red, green, blue, alpha = pixels[x, y]
            if alpha and min(red, green, blue) >= WHITE_LEFTOVER:
                pixels[x, y] = (0, 0, 0, 0)
    return image


def main() -> int:
    for key, (source, needs_cut) in sorted(SOURCES.items()):
        if not os.path.isfile(source):
            print(f"нет исходника для «{key}»: {source}")
            return 1
        image = Image.open(source)
        image = cut_white_background(image) if needs_cut else image.convert("RGBA")
        target = os.path.join(FRAMES, key + ".png")
        image.save(target, "PNG", optimize=True)
        print(f"{key}: {image.size[0]}x{image.size[1]} -> {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
