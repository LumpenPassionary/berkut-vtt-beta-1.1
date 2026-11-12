"""Текстовые надписи на карте: правила и проверка данных.

Текст — не штрих кисти, а отдельный объект: его можно выбрать, перетащить,
поправить и удалить, не стирая рисунок вокруг. Поэтому он живёт не в
растровом слое рисунка, а списком в состоянии игры (`GameState.map_texts`)
и пересылается отдельными сообщениями.

Модуль один для сервера и клиента: сервер проверяет присланное, клиент
строит по умолчанию такие же записи. Без этого клиент мог бы прислать
размер в 10 000 px или отрицательные координаты, и надпись исчезла бы
с карты для всех.
"""
from typing import Optional

#: Сколько символов в одной надписи. За столом это подпись («Таверна»,
#: «Ловушка»), а не страница текста.
TEXT_LIMIT = 500
#: Размер шрифта в мировых пикселях (масштабируется вместе с картой).
SIZE_MIN = 8.0
SIZE_MAX = 240.0
#: Пределы координат: холст и так ограничен, но запись должна быть
#: безопасной даже в мире, загруженном из чужого файла.
COORD_LIMIT = 200000.0

#: Шрифты, которые есть в Word и поддерживают кириллицу. Клиент оставляет
#: только установленные в системе, поэтому список — порядок предпочтения,
#: а не гарантия. Первым идёт шрифт по умолчанию.
FONT_CHOICES = (
    "Calibri", "Cambria", "Candara", "Corbel", "Constantia",
    "Times New Roman", "Georgia", "Palatino Linotype", "Book Antiqua",
    "Arial", "Verdana", "Tahoma", "Trebuchet MS", "Segoe UI",
    "Comic Sans MS", "Impact", "Courier New", "Consolas",
)
DEFAULT_FONT = FONT_CHOICES[0]

#: Поля, которые вообще можно менять у надписи. Всё остальное из сети
#: отбрасывается: иначе через UPDATE_MAP_TEXT можно было бы подсунуть
#: произвольные ключи в состояние игры.
EDITABLE = ("text", "x", "y", "font", "size", "color",
            "bold", "italic", "underline", "strike")

#: Значения новой надписи до того, как её поправили.
DEFAULTS = {
    "text": "Текст",
    "font": DEFAULT_FONT,
    "size": 28.0,
    "color": [255, 255, 255],
    "bold": False,
    "italic": False,
    "underline": False,
    "strike": False,
}


def _number(value, default: float, low: float, high: float) -> float:
    """Число в пределах: строка «12,5» тоже понятна."""
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return float(default)
    if number != number or number in (float("inf"), float("-inf")):
        return float(default)
    return max(low, min(high, number))


def _flag(value) -> bool:
    """Галочка из сети: True/1/"true" — да, остальное — нет."""
    if isinstance(value, str):
        return value.strip().casefold() in ("1", "true", "yes", "on", "да")
    return bool(value)


def rgb(value) -> list:
    """Цвет надписи в виде [r, g, b] — открытая обёртка для холста."""
    return _color(value)


def halo_color(value) -> list:
    """Цвет тонкого контура под буквами.

    Белая надпись на светлой карте и чёрная на тёмной подземной пропадают.
    Поэтому под текстом рисуется едва заметный контур противоположного
    цвета: подпись читается на любой картинке, а выбранный цвет остаётся.
    """
    red, green, blue = _color(value)
    # Яркость по восприятию: глаз чувствительнее всего к зелёному.
    luminance = 0.299 * red + 0.587 * green + 0.114 * blue
    return [0, 0, 0] if luminance > 140 else [255, 255, 255]


def _color(value) -> list:
    """Цвет кисти/текста — список [r, g, b] в пределах 0…255."""
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) == 6:
            try:
                return [int(text[i:i + 2], 16) for i in (0, 2, 4)]
            except ValueError:
                return list(DEFAULTS["color"])
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        out = []
        for part in list(value)[:3]:
            try:
                out.append(max(0, min(255, int(part))))
            except (TypeError, ValueError):
                out.append(0)
        return out
    return list(DEFAULTS["color"])


def _font(value) -> str:
    """Имя шрифта: пустое заменяем на шрифт по умолчанию."""
    text = str(value or "").strip()
    return text[:64] or DEFAULT_FONT


def _text(value) -> str:
    """Текст надписи: без управляющих символов, одной строкой.

    Надпись рисуется одной строкой, поэтому переводы строк и табуляции
    становятся пробелами: если их просто выбросить, «Отряд\nГрифона» с
    листа персонажа склеится в одно слово.
    """
    text = str(value or "")
    text = "".join(" " if ch.isspace() else ch for ch in text
                   if ch.isprintable() or ch.isspace())
    text = " ".join(text.split())
    return text[:TEXT_LIMIT]


def normalize(item: dict, *, text_id: str = None) -> Optional[dict]:
    """Привести запись из сети/файла к известному виду. None — мусор."""
    if not isinstance(item, dict):
        return None
    out = dict(DEFAULTS)
    out["color"] = list(DEFAULTS["color"])
    out["text"] = _text(item.get("text"))
    out["x"] = _number(item.get("x"), 0.0, -COORD_LIMIT, COORD_LIMIT)
    out["y"] = _number(item.get("y"), 0.0, -COORD_LIMIT, COORD_LIMIT)
    out["font"] = _font(item.get("font"))
    out["size"] = _number(item.get("size"), DEFAULTS["size"],
                          SIZE_MIN, SIZE_MAX)
    out["color"] = _color(item.get("color"))
    for flag in ("bold", "italic", "underline", "strike"):
        out[flag] = _flag(item.get(flag))
    identifier = str(text_id or item.get("id") or "").strip()
    if not identifier:
        return None
    out["id"] = identifier[:64]
    return out


def new_text(x: float, y: float, **overrides) -> dict:
    """Новая надпись: координаты плюс любые поля из DEFAULTS."""
    from shared.models import generate_id

    item = dict(DEFAULTS)
    item["color"] = list(DEFAULTS["color"])
    item["id"] = generate_id()
    item["x"] = x
    item["y"] = y
    item.update({key: value for key, value in overrides.items()
                 if key in EDITABLE})
    return normalize(item, text_id=item["id"]) or item


def apply_patch(item: dict, patch: dict) -> dict:
    """Надпись с применёнными правками; неизвестные поля игнорируются."""
    merged = dict(item or {})
    for key in EDITABLE:
        if key in (patch or {}):
            merged[key] = patch[key]
    return normalize(merged, text_id=str(merged.get("id") or "")) or dict(item)


def style_of(item: dict) -> dict:
    """Только оформление: им панель текста инициализирует новые надписи."""
    item = item or {}
    style = {key: item[key] for key in
             ("font", "size", "color", "bold", "italic", "underline",
              "strike") if key in item}
    return style
