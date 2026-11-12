"""Нарисовать иконки инструментов рисования (в духе панели Photoshop).

Иконки монохромные: тёмный глиф для светлой темы и светлый глиф для
тёмной — как у остальных иконок приложения (`name.png` / `name_light.png`).
Один и тот же набор идёт и на кнопки инструментов, и на иконки вкладок
правой панели: рейка разделов и панель инструментов должны читаться как
один интерфейс.

Запуск: python tools/_make_draw_icons.py
"""
import math
import os

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICONS = os.path.join(ROOT, "client", "assets", "icons")
OUT = os.path.join(ICONS, "tools")
#: Присланная иконка кисти (её же ставим на заголовок вкладки).
BRUSH_SOURCE = "G:/загрузки/Draw tab.png"

DARK = (42, 42, 44, 255)      # глиф для светлой темы
LIGHT = (216, 214, 210, 255)  # глиф для тёмной темы
S = 96                        # рисуем крупно, уменьшаем на сохранении
STROKE = 8                    # общая толщина штриха: иконки должны быть
                              # одного веса, иначе панель выглядит сборной


def canvas():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def save(img, name, folder=OUT):
    """Отдать две версии: name.png (светлая тема) и name_light.png (тёмная)."""
    os.makedirs(folder, exist_ok=True)
    for suffix, tint in (("", DARK), ("_light", LIGHT)):
        recolored = Image.new("RGBA", img.size, (0, 0, 0, 0))
        alpha = img.split()[3]
        solid = Image.new("RGBA", img.size, (tint[0], tint[1], tint[2], 255))
        recolored.paste(solid, (0, 0), alpha)
        path = os.path.join(folder, f"{name}{suffix}.png")
        recolored.resize((48, 48), Image.LANCZOS).save(path)


def glyph(mask):
    """Маска (L) -> цветной глиф с прозрачным фоном."""
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(Image.new("RGBA", (S, S), (0, 0, 0, 255)), (0, 0), mask)
    return img


def brush_from_source():
    """Иконка кисти — из присланной картинки, обрезанная по самому глифу.

    В присланном PNG фон прозрачный, а глиф тёмный, поэтому берём его
    альфа-канал как есть. Если бы альфа была сплошной, посчитали бы её из
    яркости: белый фон — прозрачное, чёрный глиф — плотное.
    """
    src = Image.open(BRUSH_SOURCE).convert("RGBA")
    alpha = src.getchannel("A")
    if alpha.getextrema()[0] > 8:          # альфы нет — берём яркость
        gray = src.convert("L")
        alpha = Image.eval(gray, lambda v: max(0, min(255, (235 - v) * 2)))
    box = alpha.getbbox()
    if box:
        alpha = alpha.crop(box)
    # Вписываем в квадрат с полями, чтобы кисть не липла к краям кнопки.
    side = max(alpha.size)
    square = Image.new("L", (side, side), 0)
    square.paste(alpha, ((side - alpha.width) // 2, (side - alpha.height) // 2))
    square = square.resize((int(S * 0.86), int(S * 0.86)), Image.LANCZOS)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(Image.new("RGBA", square.size, (0, 0, 0, 255)),
              ((S - square.width) // 2, (S - square.height) // 2), square)
    return img


def eraser():
    """Ластик как в Photoshop: наклонный брусок с полосой у держателя.

    Рисуем горизонтально и поворачиваем на 45°: так проще получить ровные
    скруглённые углы и полосу точно поперёк корпуса.
    """
    body = Image.new("RGBA", (84, 48), (0, 0, 0, 0))
    d = ImageDraw.Draw(body)
    d.rounded_rectangle([2, 4, 82, 44], radius=9, fill=(0, 0, 0, 255))
    d.line([(30, 4), (30, 44)], fill=0, width=6)
    turned = body.rotate(45, expand=True, resample=Image.BICUBIC)
    side = int(S * 0.92)
    turned.thumbnail((side, side), Image.LANCZOS)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.alpha_composite(turned, ((S - turned.width) // 2,
                                 (S - turned.height) // 2))
    return img


def line_tool():
    img, d = canvas()
    d.line([(22, 78), (74, 26)], fill=(0, 0, 0, 255), width=STROKE)
    d.ellipse([16, 72, 28, 84], fill=(0, 0, 0, 255))
    d.ellipse([68, 20, 80, 32], fill=(0, 0, 0, 255))
    return img


def rect():
    img, d = canvas()
    d.rectangle([18, 26, 78, 72], outline=(0, 0, 0, 255), width=STROKE)
    return img


def ellipse():
    img, d = canvas()
    d.ellipse([16, 24, 80, 74], outline=(0, 0, 0, 255), width=STROKE)
    return img


def pen():
    """Перо (Pen Tool): перочинный наконечник с прорезью и «пяткой».

    Инструмент ставит вершины фигуры по кликам — в Photoshop это перо,
    поэтому и иконка оттуда, а не пятиугольник: пятиугольник читался как
    готовая фигура, а не как «поставить точки».
    """
    mask = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(mask)
    d.polygon([(38, 8), (66, 8), (66, 46), (52, 88), (38, 46)], fill=255)
    # Прорезь пера и «дырка» на нём — фирменная деталь иконки.
    d.line([(52, 34), (52, 70)], fill=0, width=6)
    d.ellipse([46, 20, 58, 32], fill=0)
    return glyph(mask)


def _letter(letter, font_file, size=72, slant=0, wide=False):
    """Глиф инструмента текста: буква нужным шрифтом и с нужным наклоном.

    Иконки оформления (B/I/U/S) читаются только если нарисованы именно
    теми начертаниями, которые они включают, — поэтому берём системный
    шрифт, а не рисуем буквы линиями.
    """
    font = None
    fonts_dir = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts")
    for name in (font_file, "arial.ttf", "DejaVuSans.ttf"):
        for folder in (fonts_dir, ""):
            try:
                font = ImageFont.truetype(os.path.join(folder, name), size)
                break
            except OSError:
                continue
        if font is not None:
            break
    if font is None:
        font = ImageFont.load_default()
    mask = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(mask)
    box = d.textbbox((0, 0), letter, font=font)
    width, height = box[2] - box[0], box[3] - box[1]
    d.text(((S - width) / 2 - box[0], (S - height) / 2 - box[1]),
           letter, font=font, fill=255)
    if slant:
        mask = mask.transform((S, S), Image.AFFINE,
                              (1, slant, 0, 0, 1, 0), resample=Image.BICUBIC)
    img = glyph(mask)
    if wide:
        img = img.resize((S, int(S * 0.9)), Image.LANCZOS)
        canvas_img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        canvas_img.alpha_composite(img, (0, int(S * 0.05)))
        return canvas_img
    return img


def text_tool():
    """Инструмент «Текст» — буква T с засечками, как в панели Photoshop."""
    return _letter("T", "times.ttf", size=84)


def move_tool():
    """Инструмент «Перемещение» — четырёхсторонняя стрелка."""
    mask = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(mask)
    head, arm, thick = 16, 30, 11
    cx, cy = S // 2, S // 2
    # Вертикальная и горизонтальная «штанги»
    d.rectangle([cx - thick // 2, 18, cx + thick // 2, S - 18], fill=255)
    d.rectangle([18, cy - thick // 2, S - 18, cy + thick // 2], fill=255)
    # Четыре наконечника
    d.polygon([(cx, 8), (cx - head, 8 + arm), (cx + head, 8 + arm)], fill=255)
    d.polygon([(cx, S - 8), (cx - head, S - 8 - arm),
               (cx + head, S - 8 - arm)], fill=255)
    d.polygon([(8, cy), (8 + arm, cy - head), (8 + arm, cy + head)], fill=255)
    d.polygon([(S - 8, cy), (S - 8 - arm, cy - head),
               (S - 8 - arm, cy + head)], fill=255)
    return glyph(mask)


def bold_icon():
    return _letter("B", "arialbd.ttf", size=76)


def italic_icon():
    # С засечками и наклоном: прямая палочка без них читается как «I», а
    # не как «курсив».
    return _letter("I", "timesi.ttf", size=80, slant=-0.1)


def underline_icon():
    img = _letter("U", "arial.ttf", size=64, wide=True)
    d = ImageDraw.Draw(img)
    d.rectangle([20, 82, 76, 88], fill=(0, 0, 0, 255))
    return img


def strike_icon():
    img = _letter("S", "arial.ttf", size=64, wide=True)
    d = ImageDraw.Draw(img)
    d.rectangle([18, 46, 78, 52], fill=(0, 0, 0, 255))
    return img


def trash():
    img, d = canvas()
    d.rectangle([30, 24, 66, 82], outline=(0, 0, 0, 255), width=6)
    d.line([(22, 24), (74, 24)], fill=(0, 0, 0, 255), width=7)
    d.line([(40, 14), (56, 14)], fill=(0, 0, 0, 255), width=6)
    d.line([(44, 36), (44, 70)], fill=(0, 0, 0, 255), width=5)
    d.line([(52, 36), (52, 70)], fill=(0, 0, 0, 255), width=5)
    return img


# --- иконки вкладок правой панели -----------------------------------------
def chat():
    """Речевое облако с хвостиком — раздел «Чат»."""
    mask = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([12, 18, 84, 66], radius=14, fill=255)
    d.polygon([(30, 62), (30, 86), (52, 62)], fill=255)
    return glyph(mask)


def players():
    """Две фигурки — раздел «Игроки»."""
    mask = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(mask)
    # Передний человек
    d.ellipse([20, 14, 46, 40], fill=255)
    d.rounded_rectangle([14, 44, 52, 82], radius=14, fill=255)
    # Задний — только контур плеча и головы
    d.ellipse([52, 20, 74, 42], fill=255)
    d.rounded_rectangle([48, 46, 84, 82], radius=13, fill=255)
    # Разделяем фигуры прозрачной щелью, чтобы силуэты не сливались.
    d.line([(52, 40), (52, 84)], fill=0, width=5)
    return glyph(mask)


def sound():
    """Динамик с волнами — вкладка «Звук и тема»."""
    mask = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(mask)
    d.polygon([(14, 38), (32, 38), (54, 18), (54, 78), (32, 58), (14, 58)],
              fill=255)
    for i, r in enumerate((16, 28)):
        d.arc([54 - r, 48 - r, 54 + r, 48 + r], start=-55, end=55,
              fill=255, width=7)
    return glyph(mask)


def light():
    """Солнце с лучами — вкладка «Освещение»."""
    mask = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(mask)
    d.ellipse([34, 34, 66, 66], fill=255)
    for angle in range(0, 360, 45):
        rad = math.radians(angle)
        x1 = 50 + math.cos(rad) * 26
        y1 = 50 + math.sin(rad) * 26
        x2 = 50 + math.cos(rad) * 38
        y2 = 50 + math.sin(rad) * 38
        d.line([(x1, y1), (x2, y2)], fill=255, width=7)
    return glyph(mask)


# --- иконки заготовок света -------------------------------------------
#: Пять заготовок источника света на вкладке «Освещение». Все рисуются
#: одним штрихом и одним тёмным глифом: в ряду они должны читаться как
#: набор, а не как пять случайных картинок.
def _flame(d, cx, top, height, width, fill=(0, 0, 0, 255)):
    """Язык пламени: острый верх и округлое основание.

    Цвет задаётся кортежем: на RGBA-полотне целое число PIL понимает как
    чёрный с нулевой альфой, и пламя молча исчезало с иконки.
    """
    d.polygon([(cx, top), (cx + width, top + height * 0.60),
               (cx + width * 0.52, top + height * 0.92),
               (cx - width * 0.52, top + height * 0.92),
               (cx - width, top + height * 0.60)], fill=fill)
    d.ellipse([cx - width * 0.62, top + height * 0.56,
               cx + width * 0.62, top + height], fill=fill)


def light_candle():
    """Свеча: тонкий столбик на блюдце и маленький огонёк."""
    img, d = canvas()
    d.rounded_rectangle([40, 48, 56, 82], radius=4, fill=(0, 0, 0, 255))
    d.line([(48, 48), (48, 40)], fill=(0, 0, 0, 255), width=4)
    _flame(d, 48, 14, 26, 11)
    d.line([(28, 86), (68, 86)], fill=(0, 0, 0, 255), width=7)
    return img


def light_torch():
    """Факел: наклонная рукоять с обмоткой и большой огонь."""
    img, d = canvas()
    d.line([(30, 88), (52, 50)], fill=(0, 0, 0, 255), width=11)
    d.line([(42, 52), (56, 36)], fill=(0, 0, 0, 255), width=17)
    _flame(d, 56, 8, 28, 14)
    return img


def light_fire():
    """Костёр: два скрещённых полена и пламя над ними."""
    img, d = canvas()
    d.line([(18, 84), (78, 68)], fill=(0, 0, 0, 255), width=9)
    d.line([(20, 66), (76, 86)], fill=(0, 0, 0, 255), width=9)
    _flame(d, 48, 8, 42, 19)
    return img


def light_lamp():
    """Лампа: лампочка — круг с цоколем.

    Сначала здесь был фонарь с дужкой, но в кнопке 22 px он читался как
    замок (те же иконки стоят рядом). Лампочка узнаётся с одного взгляда
    и не путается с свечой или костром.
    """
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([26, 10, 70, 58], fill=(0, 0, 0, 255))
    d.rectangle([41, 52, 55, 66], fill=(0, 0, 0, 255))
    d.rounded_rectangle([36, 64, 60, 74], radius=4, fill=(0, 0, 0, 255))
    d.line([(40, 79), (56, 79)], fill=(0, 0, 0, 255), width=6)
    d.line([(44, 86), (52, 86)], fill=(0, 0, 0, 255), width=5)
    return img


def light_magic():
    """Магический свет: четырёхлучевая звезда и две искры."""
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy = 48, 48

    def star(x, y, long_arm, short_arm):
        d.polygon([(x, y - long_arm), (x + short_arm, y - short_arm),
                   (x + long_arm, y), (x + short_arm, y + short_arm),
                   (x, y + long_arm), (x - short_arm, y + short_arm),
                   (x - long_arm, y), (x - short_arm, y - short_arm)],
                  fill=(0, 0, 0, 255))

    star(cx, cy, 34, 9)
    star(20, 20, 10, 3)
    star(76, 72, 8, 2)
    return img


# --- иконки инструментов стен -------------------------------------------
#: Набор инструментов стены: рамка, ломаная, точки правки, окно и дверь.
#: Рисуются тем же штрихом, что и остальные инструменты: рейка вкладки
#: «Освещение» — продолжение той же панели, а не отдельный набор кнопок.
def wall_rect():
    """Стена рамкой: комната за один жест."""
    img, d = canvas()
    d.rectangle([16, 22, 80, 74], outline=(0, 0, 0, 255), width=STROKE)
    return img


def wall_pen():
    """Стена по точкам: ломаная с вершинами."""
    img, d = canvas()
    points = [(14, 78), (34, 34), (58, 64), (82, 20)]
    d.line(points, fill=(0, 0, 0, 255), width=STROKE - 1, joint="curve")
    for x, y in points:
        d.ellipse([x - 7, y - 7, x + 7, y + 7], fill=(0, 0, 0, 255))
    return img


def wall_points():
    """Правка точек: у стены видны квадратные ручки."""
    img, d = canvas()
    d.line([(16, 68), (82, 68)], fill=(0, 0, 0, 255), width=STROKE - 2)
    for x in (16, 49, 82):
        d.rectangle([x - 9, 59, x + 9, 77], fill=(0, 0, 0, 255))
    return img


def wall_window():
    """Окно в стене: рама со стеклом посередине."""
    img, d = canvas()
    d.line([(10, 70), (86, 70)], fill=(0, 0, 0, 255), width=STROKE - 2)
    d.rectangle([30, 26, 66, 62], outline=(0, 0, 0, 255), width=6)
    d.line([(48, 26), (48, 62)], fill=(0, 0, 0, 255), width=4)
    return img


def wall_door():
    """Дверь: проём и приоткрытая створка с ручкой."""
    img, d = canvas()
    d.line([(10, 78), (44, 78)], fill=(0, 0, 0, 255), width=STROKE - 2)
    d.line([(66, 78), (86, 78)], fill=(0, 0, 0, 255), width=STROKE - 2)
    d.rectangle([44, 20, 68, 78], outline=(0, 0, 0, 255), width=6)
    d.ellipse([58, 50, 65, 57], fill=(0, 0, 0, 255))
    return img


def main():
    os.makedirs(OUT, exist_ok=True)
    brush = brush_from_source()
    parts = {
        "draw": brush,          # иконка самой вкладки «Рисование»
        "brush": brush,         # кисть
        "eraser": eraser(),
        "line": line_tool(),
        "rect": rect(),
        "ellipse": ellipse(),
        "pen": pen(),
        "text": text_tool(),
        "move": move_tool(),
        "text_bold": bold_icon(),
        "text_italic": italic_icon(),
        "text_underline": underline_icon(),
        "text_strike": strike_icon(),
        "text_delete": trash(),
        "clear": trash(),
        "tab_chat": chat(),
        "tab_players": players(),
        "tab_sound": sound(),
        "light": light(),
        "light_candle": light_candle(),
        "light_torch": light_torch(),
        "light_fire": light_fire(),
        "light_lamp": light_lamp(),
        "light_magic": light_magic(),
        "wall_rect": wall_rect(),
        "wall_pen": wall_pen(),
        "wall_points": wall_points(),
        "wall_window": wall_window(),
        "wall_door": wall_door(),
    }
    for name, img in parts.items():
        save(img, name)
    print("готово:", sorted(os.listdir(OUT)))


if __name__ == "__main__":
    main()
