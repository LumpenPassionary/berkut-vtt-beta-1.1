"""Зрение токенов: куда смотрит и что видит.

В 5e скрытность невозможна, пока тебя видят: «нельзя прятаться от существа,
которое тебя видит» (PHB, Скрытность). Пока в таблице не было направления
взгляда, любое существо на карте «видело» во все стороны, и проверить это
правило было нечем. Здесь появляется простой и предсказуемый обзор:

* у токена есть направление взгляда ``facing`` в градусах (atan2):
  0 — вправо, 90 — вниз (так токен и появляется на карте), 180 — влево,
  270 — вверх;
* ``vision_angle`` — угол обзора конусом (по умолчанию 70°, как у широкого
  взгляда перед собой), ``vision_range`` — дальность в футах (60 фт);
* токен видит цель, если она в пределах дальности и внутри половины угла
  обзора от направления взгляда.

Модуль чистый: ни Qt, ни сети. Геометрия общая для холста, сервера и
тестов, поэтому подсвеченный на экране конус и серверный запрет спрятаться
считают одно и то же.

Правила освещения (свет/темнота и помеха на Восприятие) живут рядом, в
``shared/lighting.py``: обзор здесь — геометрия конуса, а видимость ночью
добавляет ``lighting.sees``.
"""
import math
import re

#: Угол обзора по умолчанию: 70° — широкий взгляд перед собой.
DEFAULT_VISION_ANGLE = 70.0
#: Дальность обзора по умолчанию: 60 футов.
DEFAULT_VISION_RANGE_FT = 60.0
#: Направление «вниз»: новые токены всегда смотрят на юг карты.
DEFAULT_FACING = 90.0
#: Один шаг сетки — 5 футов.
FT_PER_CELL = 5.0

#: «тёмное зрение 90 фт», «чувство вибрации 30 футов»: слово о зрении,
#: затем (без запятых и цифр) расстояние в футах.
_SENSE_RANGE_RE = re.compile(
    r"(?:зрен\w*|чувств\w*|восприят\w*)[^.;,\d]{0,30}?"
    r"(\d+)\s*(?:фут\w*|фт\b|feet\b|ft\b)")

#: Допустимые пределы: угол не уже 10° (иначе токен слепой), не шире 360°.
MIN_ANGLE = 10.0
MAX_ANGLE = 360.0
#: Дальность: от одной клетки до 600 футов (дальние заклинания и обзор).
MIN_RANGE_FT = 5.0
MAX_RANGE_FT = 600.0


def normalize_facing(value, default: float = DEFAULT_FACING) -> float:
    """Привести угол взгляда к диапазону [0, 360)."""
    try:
        angle = float(value)
    except (TypeError, ValueError):
        return float(default)
    if math.isnan(angle) or math.isinf(angle):
        return float(default)
    return angle % 360.0


def normalize_angle(value, default: float = DEFAULT_VISION_ANGLE) -> float:
    """Угол обзора: зажать в разумные пределы (10…360°)."""
    try:
        angle = float(value)
    except (TypeError, ValueError):
        return float(default)
    if math.isnan(angle) or math.isinf(angle):
        return float(default)
    return max(MIN_ANGLE, min(MAX_ANGLE, angle))


def normalize_range(value, default: float = DEFAULT_VISION_RANGE_FT) -> float:
    """Дальность обзора в футах: зажать в пределы 5…600."""
    try:
        feet = float(value)
    except (TypeError, ValueError):
        return float(default)
    if math.isnan(feet) or math.isinf(feet):
        return float(default)
    return max(MIN_RANGE_FT, min(MAX_RANGE_FT, feet))


def range_from_text(text):
    """Дальность зрения из описания: «тёмное зрение 90 фт» -> 90.

    В листах и бестиарии чувства записаны строкой («тёмное зрение 60 фт,
    пассивное Восприятие 12»), а не отдельным полем. Ищем число сразу
    после слова про зрение: иначе «атака 30 фт» или «скорость 50 фт»
    сделали бы персонажа дальнозорким.
    """
    if not text:
        return None
    if isinstance(text, (list, tuple)):
        text = ", ".join(str(part) for part in text)
    source = str(text).casefold().replace("ё", "е")
    best = None
    for match in _SENSE_RANGE_RE.finditer(source):
        try:
            feet = float(match.group(1))
        except (TypeError, ValueError):
            continue
        if best is None or feet > best:
            best = feet
    if best is None:
        return None
    return normalize_range(best)


def settings_from_sheet(sheet) -> dict:
    """Настройки зрения из листа любой формы (LSS/Foundry).

    Лист — большой словарь с заклинаниями и заметками, поэтому в лоб
    искать «фт» нельзя: в описании заклинания их десятки. Собираем все
    строки и разбираем тем же правилом, что и бестиарий.
    """
    strings = []

    def walk(node, depth=0):
        if depth > 10:
            return
        if isinstance(node, dict):
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value, depth + 1)
        elif isinstance(node, str):
            strings.append(node)

    walk(sheet)
    return settings_from_text(strings)


def sees_all_around(text) -> bool:
    """Круговой обзор: «круговой обзор», «во все стороны» в описании."""
    source = str(text or "").casefold().replace("ё", "е")
    return any(phrase in source for phrase in
               ("круговой обзор", "во все стороны", "всестороннее зрение"))


def settings_from_text(text) -> dict:
    """Настройки зрения из описания листа или существа.

    Возвращает только то, что удалось прочитать: пустой словарь — значит,
    в тексте нет ни дальности, ни кругового обзора. Значения уже
    нормализованы, поэтому их можно писать в токен как есть.
    """
    out = {}
    feet = range_from_text(text)
    if feet is not None:
        out["vision_range"] = feet
    if sees_all_around(text):
        out["vision_angle"] = MAX_ANGLE
    return out


def direction(facing) -> tuple:
    """Единичный вектор взгляда из угла atan2 (0 — вправо, 90 — вниз)."""
    radians = math.radians(normalize_facing(facing))
    return math.cos(radians), math.sin(radians)


def facing_between(x1, y1, x2, y2, default: float = DEFAULT_FACING) -> float:
    """Угол взгляда из точки 1 на точку 2 — так токен поворачивается мышью."""
    try:
        return normalize_facing(
            math.degrees(math.atan2(float(y2) - float(y1),
                                    float(x2) - float(x1))), default)
    except (TypeError, ValueError):
        return float(default)


def angle_between(facing, dx, dy) -> float:
    """Наименьший угол между взглядом и направлением (в градусах)."""
    try:
        length = math.hypot(float(dx), float(dy))
        if length <= 0:
            return 0.0
        target = math.degrees(math.atan2(float(dy), float(dx)))
    except (TypeError, ValueError):
        return 180.0
    return abs((target - normalize_facing(facing) + 180.0) % 360.0 - 180.0)


def token_value(token, field, default):
    """Поле зрения токена или значение по умолчанию.

    Токены старых миров не знают об этих полях — молчаливый ``getattr``
    с запасным числом не даёт им стать слепыми.
    """
    if isinstance(token, dict):
        value = token.get(field)
    else:
        value = getattr(token, field, None)
    return default if value is None else value


def token_facing(token) -> float:
    return normalize_facing(token_value(token, "facing", DEFAULT_FACING))


def token_angle(token) -> float:
    return normalize_angle(token_value(token, "vision_angle",
                                        DEFAULT_VISION_ANGLE))


def token_range_ft(token) -> float:
    return normalize_range(token_value(token, "vision_range",
                                       DEFAULT_VISION_RANGE_FT))


def cell_size(map_data) -> tuple:
    """Размер клетки карты в мировых единицах; (0, 0) — сетки нет."""
    try:
        cell_w, cell_h = map_data.grid_cell_world
        return float(cell_w or 0.0), float(cell_h or 0.0)
    except (AttributeError, TypeError, ValueError):
        return 0.0, 0.0


def in_cone(viewer, target, map_data=None, *, facing=None, angle=None,
            range_ft=None) -> bool:
    """Видит ли ``viewer`` точку ``target`` (токен) в своём конусе обзора.

    Расстояние считается по клеткам карты: один шаг — 5 футов. Без карты
    или без сетки расстояния нет, и токен видит всё, что попадает в угол:
    так скрытность не превращается в «никто никого не видит» на картах без
    разметки.
    """
    vx = float(token_value(viewer, "x", 0.0) or 0.0)
    vy = float(token_value(viewer, "y", 0.0) or 0.0)
    tx = float(token_value(target, "x", 0.0) or 0.0)
    ty = float(token_value(target, "y", 0.0) or 0.0)
    dx, dy = tx - vx, ty - vy
    if dx == 0 and dy == 0:
        # Цель стоит на самом наблюдателе: это не «в обзоре», а вплотную.
        return True

    cell_w, cell_h = cell_size(map_data)
    if cell_w > 0 and cell_h > 0:
        distance_ft = (math.hypot(dx / cell_w, dy / cell_h)
                       * FT_PER_CELL)
    else:
        # Без сетки расстояние в мировых единицах не перевести в футы,
        # поэтому дальность не проверяем — только угол.
        distance_ft = 0.0
    limit = normalize_range(range_ft if range_ft is not None
                            else token_range_ft(viewer))
    if distance_ft and distance_ft > limit:
        return False

    look = facing if facing is not None else token_facing(viewer)
    width = normalize_angle(angle if angle is not None
                            else token_angle(viewer))
    if width >= 360.0:
        return True
    return angle_between(look, dx, dy) <= width / 2.0 + 1e-6


def observed_by(token, others, maps=None) -> list:
    """Кто из ``others`` видит ``token`` прямо сейчас.

    Возвращает список наблюдателей (по порядку), чтобы лог мог назвать
    конкретное существо: «на виду у Бехира».
    """
    maps = maps or {}
    watchers = []
    for other in others or []:
        if other is None or other is token:
            continue
        if getattr(other, "id", None) == getattr(token, "id", None):
            continue
        if getattr(other, "map_id", None) != getattr(token, "map_id", None):
            continue
        try:
            if int(getattr(other, "hp", 0) or 0) <= 0:
                continue
        except (TypeError, ValueError):
            pass
        map_data = maps.get(getattr(other, "map_id", None))
        if in_cone(other, token, map_data):
            watchers.append(other)
    return watchers


def can_hide(token, others, maps=None) -> tuple:
    """Можно ли прятаться по одному конусу зрения. -> (можно, причина).

    Причина — имя того, кто смотрит: мастер должен видеть, кого именно
    нужно обойти, а не просто «нельзя».

    Свет здесь не учитывается: в 5e тусклый свет и тьма — заслонённая
    местность, где попытка разрешена. В столе это решает
    ``GameServer.stealth_check`` через ``lighting.light_level``; функция
    остаётся чисто геометрической проверкой для тестов.
    """
    watchers = observed_by(token, others, maps)
    # Своих же персонажей не считаем: отряд не мешает прятаться (то же
    # правило уже используется для пассивного Восприятия у сервера).
    watchers = [w for w in watchers
                if not (getattr(w, "owner_id", None)
                        and getattr(w, "owner_id", None)
                        == getattr(token, "owner_id", None))]
    if not watchers:
        return True, ""
    names = []
    for watcher in watchers:
        name = str(getattr(watcher, "name", "") or "").strip()
        if name and name not in names:
            names.append(name)
    return False, ", ".join(names)
