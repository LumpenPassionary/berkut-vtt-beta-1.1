"""Нарисовать иконки ходов боя: «назад», «следующий», «закончить бой».

Кнопки листаются без подписи — только стрелки, поэтому глифы должны
читаться с одного взгляда и совпадать по весу с остальными иконками
приложения (тот же штрих, те же два файла на тему: ``name.png`` для светлой
темы и ``name_light.png`` для тёмной).

Запуск: python tools/_make_nav_icons.py
"""
import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "client", "assets", "icons", "tools")

DARK = (42, 42, 44, 255)      # глиф для светлой темы
LIGHT = (216, 214, 210, 255)  # глиф для тёмной темы
S = 96
STROKE = 7


def canvas():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def save(img, name):
    """Отдать две версии: name.png (светлая тема), name_light.png (тёмная)."""
    os.makedirs(OUT, exist_ok=True)
    for suffix, tint in (("", DARK), ("_light", LIGHT)):
        recolored = Image.new("RGBA", img.size, (0, 0, 0, 0))
        alpha = img.split()[3]
        solid = Image.new("RGBA", img.size, (tint[0], tint[1], tint[2], 255))
        recolored.paste(solid, (0, 0), alpha)
        recolored.resize((48, 48), Image.LANCZOS).save(
            os.path.join(OUT, f"{name}{suffix}.png"))


def chevron(direction):
    """Стрелка с упором: «шаг назад» / «шаг вперёд» для очереди ходов.

    Полоса у края — упор: она отличает переход к следующему участнику от
    простой стрелки «вернуться», которая у приложения уже есть.
    """
    img, d = canvas()
    if direction == "prev":
        bar = (24, 26, 24 + STROKE, 70)
        points = [(72, 26), (44, 48), (72, 70)]
    else:
        bar = (S - 24 - STROKE, 26, S - 24, 70)
        points = [(24, 26), (52, 48), (24, 70)]
    d.rounded_rectangle(bar, radius=STROKE // 2, fill=(255, 255, 255, 255))
    # Толстая ломаная с круглыми стыками: рисуем линии и кружки в узлах,
    # иначе на стыке появляется щель.
    width = STROKE
    for first, second in zip(points, points[1:]):
        d.line([first, second], fill=(255, 255, 255, 255), width=width)
        d.ellipse([first[0] - width // 2, first[1] - width // 2,
                   first[0] + width // 2, first[1] + width // 2],
                  fill=(255, 255, 255, 255))
        d.ellipse([second[0] - width // 2, second[1] - width // 2,
                   second[0] + width // 2, second[1] + width // 2],
                  fill=(255, 255, 255, 255))
    return img


def finish():
    """Флаг: «бой закончен» — конец встречи, а не пауза."""
    img, d = canvas()
    pole_x = 28
    d.rounded_rectangle((pole_x, 20, pole_x + STROKE, 78),
                        radius=STROKE // 2, fill=(255, 255, 255, 255))
    # Полотнище с лёгкой волной: прямая трапеция выглядела бы как «стоп».
    flag = [(pole_x + STROKE - 2, 24), (74, 28), (68, 40), (74, 52),
            (pole_x + STROKE - 2, 56)]
    d.polygon(flag, fill=(255, 255, 255, 255))
    return img


if __name__ == "__main__":
    save(chevron("prev"), "nav_prev")
    save(chevron("next"), "nav_next")
    save(finish(), "nav_finish")
    print("Готово:", sorted(os.listdir(OUT)))
