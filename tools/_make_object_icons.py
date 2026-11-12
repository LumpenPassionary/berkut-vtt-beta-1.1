"""Нарисовать глазки и замки для строк «Объекты» и списка сцены.

Эмодзи 👁 / 🙈 / 🔒 в маленькой кнопке Windows рисует цветным и почти
нечитаемым, а QToolButton с отступами обрезает его до полоски. Здесь те же
состояния нарисованы монохромной линией — как остальные иконки приложения:
`name.png` для светлой темы и `name_light.png` для тёмной.

Запуск: python tools/_make_object_icons.py
"""
import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "client", "assets", "icons", "tools")

DARK = (42, 42, 44, 255)      # глиф для светлой темы
LIGHT = (216, 214, 210, 255)  # глиф для тёмной темы
S = 96
STROKE = 8


def canvas():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def save(img, name):
    """Отдать две версии: name.png (светлая тема) и name_light.png (тёмная)."""
    os.makedirs(OUT, exist_ok=True)
    for suffix, tint in (("", DARK), ("_light", LIGHT)):
        recolored = Image.new("RGBA", img.size, (0, 0, 0, 0))
        alpha = img.split()[3]
        solid = Image.new("RGBA", img.size, (tint[0], tint[1], tint[2], 255))
        recolored.paste(solid, (0, 0), alpha)
        recolored.resize((48, 48), Image.LANCZOS).save(
            os.path.join(OUT, f"{name}{suffix}.png"))


def quadratic(p0, control, p1, steps=48):
    """Точки квадратичной кривой: у PIL нет Bézier, а глаз — две дуги."""
    points = []
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * control[0] + t ** 2 * p1[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * control[1] + t ** 2 * p1[1]
        points.append((x, y))
    return points


def eye_mask():
    """Контур глаза: верхнее и нижнее веко, без заливки."""
    mask = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(mask)
    d.line(quadratic((8, 48), (48, 16), (88, 48)), fill=255, width=STROKE,
           joint="curve")
    d.line(quadratic((88, 48), (48, 80), (8, 48)), fill=255, width=STROKE,
           joint="curve")
    return mask


def eye():
    mask = eye_mask()
    d = ImageDraw.Draw(mask)
    d.ellipse([31, 31, 65, 65], outline=255, width=STROKE)
    d.ellipse([41, 41, 55, 55], fill=255)
    return _colored(mask)


def eye_off():
    """Тот же глаз с диагональной перечёркивающей линией."""
    mask = eye_mask()
    d = ImageDraw.Draw(mask)
    # Сначала расширяем «прорезь» в контуре, потом рисуем саму линию:
    # иначе она сливается с веком и иконка становится кашей.
    d.line([(14, 14), (82, 82)], fill=0, width=STROKE * 3)
    d.line([(14, 14), (82, 82)], fill=255, width=STROKE, joint="curve")
    return _colored(mask)


def _lock_body(d):
    """Корпус заливкой: тонкий контур на 20 px сливался с дужкой."""
    d.rounded_rectangle([22, 42, 74, 86], radius=9, fill=255)
    # Скважина вырезана из заливки — тело остаётся плотным и читаемым.
    d.ellipse([41, 53, 55, 67], fill=0)
    d.polygon([(44, 63), (52, 63), (54, 76), (42, 76)], fill=0)


def lock():
    mask = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(mask)
    _lock_body(d)
    # Дуга дужки и короткие ножки до корпуса.
    d.arc([30, 16, 66, 52], start=180, end=360, fill=255, width=STROKE + 1)
    d.line([(34, 33), (34, 43)], fill=255, width=STROKE + 1)
    d.line([(62, 33), (62, 43)], fill=255, width=STROKE + 1)
    return _colored(mask)


def unlock():
    mask = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(mask)
    _lock_body(d)
    # Дужка сдвинута и приподнята: слева виден зазор — замок открыт.
    d.arc([38, 12, 74, 48], start=180, end=360, fill=255, width=STROKE + 1)
    d.line([(69, 30), (69, 43)], fill=255, width=STROKE + 1)
    d.line([(43, 30), (43, 36)], fill=255, width=STROKE + 1)
    return _colored(mask)


def _colored(mask):
    """Чёрная маска -> RGBA-глиф; цвет подставит save()."""
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(Image.new("RGBA", (S, S), (0, 0, 0, 255)), (0, 0), mask)
    return img


def main():
    for name, img in (("eye", eye()), ("eye_off", eye_off()),
                      ("lock", lock()), ("unlock", unlock())):
        save(img, name)
    print("готово:", sorted(os.listdir(OUT)))


if __name__ == "__main__":
    main()
