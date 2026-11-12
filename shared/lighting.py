"""Освещение карты: источники света, темнота и зрение в темноте.

Модуль чистый — ни Qt, ни сети, — чтобы одна и та же геометрия работала на
холсте, на сервере и в тестах:

* у карты есть список **источников света**: точка в мировых координатах,
  радиус в футах, температура в кельвинах (из неё считается цвет),
  интенсивность — плавность кромки, и включённость;
* ночь (`MapData.time_of_day == "night"`) делает неосвещённые места
  **темнотой**; источник света освещает круг своего радиуса;
* в темноте существо видит только на дальность **тёмного зрения**
  («тёмное зрение 60 фт» в бестиарии или листе) и не видит освещённые
  предметы за этой дальностью;*  у точки на карте есть одна из трёх категорий освещения 5e:
  ``bright`` (яркий свет), ``dim`` (тусклый свет — слабо заслонённая
  местность) и ``dark`` (тьма — сильно заслонённая местность);
*  наблюдатель, который ищет цель в тусклом свете или в темноте, получает
  **помеху** на Восприятие — в пассивном значении это −5, как принято в 5e;
*  прятаться нельзя только в ярком свете: в тусклом свете и в темноте
  местность заслонена, и попытка разрешена даже на виду у наблюдателя.

Без сетки на карте расстояния в футах не из чего считать: тогда свет
геометрически не помогает, а темнота считается только оформлением — иначе
на карте без разметки скрытность и Восприятие ломались бы непредсказуемо.
"""
import math
import re
import uuid

from shared import vision as vision_rules

#: Радиус источника света по умолчанию: факел освещает 20 футов.
DEFAULT_LIGHT_RADIUS_FT = 20.0
MIN_LIGHT_RADIUS_FT = 5.0
MAX_LIGHT_RADIUS_FT = 120.0
#: Число источников на карту не ограничено: на городской карте десятки
#: фонарей и окон, и прежний потолок в 24 молча обрезал список.
#: Тёплый свет факела по умолчанию.
DEFAULT_LIGHT_COLOR = "#ffb457"
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

#: Температура света в кельвинах: пламя факела ~2200 K, свеча холоднее,
#: лампа дневного света — около 6500 K. Цвет считается из неё, поэтому
#: мастеру достаточно одного числа, а не палитры.
DEFAULT_LIGHT_KELVIN = 2200
MIN_LIGHT_KELVIN = 1000
MAX_LIGHT_KELVIN = 12000
#: Интенсивность — насколько плавно свет гаснет к краю: 0 — резкая
#: кромка, 100 — почти линейный переход от центра к радиусу.
DEFAULT_LIGHT_INTENSITY = 55
MIN_LIGHT_INTENSITY = 0
MAX_LIGHT_INTENSITY = 100
#: Насколько сильно «размывается» кромка при интенсивности 100%.
MAX_LIGHT_FALLOFF = 0.85
#: Свет «сверх силы»: с этой температуры источник виден и днём — дневная
#: карта не гасит его, и на столе видно, докуда он достаёт. Всё, что
#: холоднее (свеча, факел, лампа), в свете дня не читается — так и должно
#: быть: его не видно и в реальной комнате с окнами.
DAYLIGHT_GLOW_KELVIN = 8000
#: Альфа дневного отсвета у самого порога и на максимуме температуры:
#: на 8000 K источник лишь намекает на себя, к 12000 K — светит в полную
#: силу, поэтому разница между «просто тёплым» и «сверх силы» видна сразу.
DAYLIGHT_GLOW_ALPHA_MIN = 70
DAYLIGHT_GLOW_ALPHA_MAX = 140
#: Насколько отсвет днём белее самого света: 0 — цвет источника, 1 — белый.
#: На солнце виден не оттенок лампы, а яркость: холодный сизый отсвет на
#: тёплом скане карты читается как туман, а не как свет.
DAYLIGHT_GLOW_WHITEN = 0.85

#: «тёмное зрение 60 фт», «darkvision 120 ft».
#: Между словом и числом допускаем разметку LSS (куски текста лежат
#: отдельными строками), поэтому запас шире обычной фразы.
_DARKVISION_RE = re.compile(
    r"(?:темн\w*\s+зрен\w*|darkvision)[^.;,\d]{0,60}?"
    r"(\d+)\s*(?:фут\w*|фт\b|feet\b|ft\b)")
#: Ключ поля с тёмным зрением: LSS кладёт его числом в vitality.
_DARKVISION_KEY_RE = re.compile(
    r"(?:darkvision|т[её]мн\w*\s+зрен\w*)", re.IGNORECASE)
#: Поля, в которых тёмное зрение ищется свободным текстом: чувства и
#: зрение. Всё остальное — описания особенностей, заклинаний и предметов —
#: на тёмное зрение не влияет.
_SENSES_KEY_RE = re.compile(
    r"(?:senses?|perception|vision|зрен\w*|восприят\w*)", re.IGNORECASE)
#: Пределы тёмного зрения: меньше клетки смысла нет, больше 600 — опечатка.
MAX_DARKVISION_FT = 600.0
#: Дальность, которую даёт галочка «тёмное зрение» без своего числа:
#: 60 фт — то, что видят почти все существа в 5e.
DEFAULT_DARKVISION_FT = 60.0

#: Три категории освещения 5e (PHB, «Зрение и свет»): яркий свет, тусклый
#: свет (слабо заслонённая местность) и тьма (сильно заслонённая). От
#: категории зависит, можно ли спрятаться и с какой помехой ищет наблюдатель.
BRIGHT = "bright"
DIM = "dim"
DARK = "dark"
#: Подписи категорий для чата и карточек броска.
LIGHT_LABELS = {BRIGHT: "яркий свет", DIM: "тусклый свет", DARK: "темнота"}

#: Готовые заготовки: мастер ставит свет одним кликом, а не крутит три
#: поля с нуля. Радиус — докуда свет достаёт целиком (яркий + тусклый
#: свет 5e), температура — цвет пламени или лампы, интенсивность — мягкость
#: кромки (у костра она больше: пламя большое и дрожащее).
LIGHT_PRESETS = (
    {"key": "candle", "name": "Свеча", "icon": "light_candle",
     "radius_ft": 10.0, "kelvin": 1800, "intensity": 65},
    {"key": "torch", "name": "Факел", "icon": "light_torch",
     "radius_ft": 40.0, "kelvin": 2200, "intensity": 55},
    {"key": "campfire", "name": "Костёр", "icon": "light_fire",
     "radius_ft": 60.0, "kelvin": 1900, "intensity": 80},
    {"key": "lamp", "name": "Лампа", "icon": "light_lamp",
     "radius_ft": 30.0, "kelvin": 2700, "intensity": 45},
    # Магический свет — единственная заготовка «сверх силы»: она горячее
    # DAYLIGHT_GLOW_KELVIN, поэтому видна и днём. Если опустить её до 7000 K,
    # на светлой карте она пропадала бы, и мастер не понимал бы, докуда
    # достаёт поставленный свет.
    {"key": "magic", "name": "Магический свет", "icon": "light_magic",        "radius_ft": 40.0, "kelvin": 9000, "intensity": 70},
)
#: Заготовка по умолчанию: без неё кнопка «Добавить» ставила бы неизвестно что.
DEFAULT_LIGHT_PRESET = "torch"

#: Ночь по умолчанию — глубокий синий, как было до настройки цвета.
#: Цвет любой (#rrggbb), а плотность — проценты непрозрачности: 59%
#: соответствуют прежним 150 из 255.
DEFAULT_NIGHT_COLOR = "#0a0e22"
DEFAULT_NIGHT_ALPHA = 59
MIN_NIGHT_ALPHA = 0
MAX_NIGHT_ALPHA = 100


def normalize_night_color(value, default: str = DEFAULT_NIGHT_COLOR) -> str:
    """Цвет ночного затемнения: только #rrggbb, иначе синий по умолчанию."""
    text = str(value or "").strip()
    return text.lower() if _COLOR_RE.match(text) else default


def normalize_night_alpha(value, default: int = DEFAULT_NIGHT_ALPHA) -> int:
    """Плотность ночи в процентах: 0 — прозрачно, 100 — непрозрачно."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return int(default)
    if math.isnan(number) or math.isinf(number):
        return int(default)
    return int(round(max(MIN_NIGHT_ALPHA, min(MAX_NIGHT_ALPHA, number))))


# --- Туман войны: своя плотность у комнат и у мира -------------------------
#
# Почему две настройки. Стол тянет туман по-разному: на большой карте
# подземелья мастеру нужно, чтобы неразведанная комната была чёрной (игрок
# не знает, что там вообще есть), а открытое пространство достаточно было
# пригасить — карта читается, но деталей не видно. Раньше плотность была
# одна на всё, и одно из двух всегда страдало.
#
# «Комната» — это замкнутое пространство, обойдённое стенами; «мир» —
# остальное (коридоры, улица, открытые места). По умолчанию комнаты
# чёрные, а мир просвечивает на 55%: из коробки это читается сразу.
DEFAULT_FOG_ROOM_ALPHA = 100
DEFAULT_FOG_WORLD_ALPHA = 55
MIN_FOG_ALPHA = 0
MAX_FOG_ALPHA = 100


def normalize_fog_alpha(value, default: int) -> int:
    """Плотность тумана в процентах: 0 — видно как есть, 100 — чёрно."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return int(default)
    if math.isnan(number) or math.isinf(number):
        return int(default)
    return int(round(max(MIN_FOG_ALPHA, min(MAX_FOG_ALPHA, number))))


def normalize_fog_room_alpha(value,
                             default: int = DEFAULT_FOG_ROOM_ALPHA) -> int:
    """Плотность тумана в неизведанных комнатах."""
    return normalize_fog_alpha(value, default)


def normalize_fog_world_alpha(value,
                              default: int = DEFAULT_FOG_WORLD_ALPHA) -> int:
    """Плотность тумана в остальном мире (коридоры, улица, открытые места)."""
    return normalize_fog_alpha(value, default)


# --- Цвет тумана ----------------------------------------------------------
#
# Плотность решает, насколько неразведанное скрыто, а вид — каким цветом оно
# залито. Чёрный читается как «сюда не ходили», сепия — как старая карта,
# холодный цветной — как ночной туман. Четвёртый вариант — любой свой цвет:
# его выбирает мастер, и он ложится на неразведанное вместо готового.
FOG_STYLE_MONO = "mono"
FOG_STYLE_SEPIA = "sepia"
FOG_STYLE_COLOR = "color"
FOG_STYLE_CUSTOM = "custom"
FOG_STYLES = (FOG_STYLE_MONO, FOG_STYLE_SEPIA, FOG_STYLE_COLOR,
              FOG_STYLE_CUSTOM)
DEFAULT_FOG_STYLE = FOG_STYLE_MONO
DEFAULT_FOG_COLOR = "#0a0a0c"
#: Готовые заливки тумана: ч/б — почти чёрный, сепия — тёплый, цветной —
#: холодный синий. Свой цвет сюда не входит: он приходит от мастера.
FOG_STYLE_COLORS = {
    FOG_STYLE_MONO: DEFAULT_FOG_COLOR,
    FOG_STYLE_SEPIA: "#2b1a0c",
    FOG_STYLE_COLOR: "#101a3a",
}


def normalize_fog_style(value, default: str = DEFAULT_FOG_STYLE) -> str:
    """Вид тумана: одно из FOG_STYLES, иначе ч/б по умолчанию."""
    text = str(value or "").strip().lower()
    return text if text in FOG_STYLES else default


def normalize_fog_color(value, default: str = DEFAULT_FOG_COLOR) -> str:
    """Свой цвет тумана: только #rrggbb, иначе чёрный по умолчанию."""
    text = str(value or "").strip()
    return text.lower() if _COLOR_RE.match(text) else default


def fog_tint(style, color) -> str:
    """Каким цветом заливать неразведанное при выбранном виде тумана."""
    mode = normalize_fog_style(style)
    if mode == FOG_STYLE_CUSTOM:
        return normalize_fog_color(color)
    return FOG_STYLE_COLORS[mode]


def normalize_color(value, default: str = DEFAULT_LIGHT_COLOR) -> str:
    """Цвет света: только #rrggbb, иначе тёплый по умолчанию."""
    text = str(value or "").strip()
    return text.lower() if _COLOR_RE.match(text) else default


def normalize_radius(value, default: float = DEFAULT_LIGHT_RADIUS_FT) -> float:
    """Радиус света в футах: 5…120."""
    try:
        feet = float(value)
    except (TypeError, ValueError):
        return float(default)
    if math.isnan(feet) or math.isinf(feet):
        return float(default)
    return max(MIN_LIGHT_RADIUS_FT, min(MAX_LIGHT_RADIUS_FT, feet))


def normalize_kelvin(value, default: int = DEFAULT_LIGHT_KELVIN) -> int:
    """Температура света в кельвинах: 1000…12000."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return int(default)
    if math.isnan(number) or math.isinf(number):
        return int(default)
    return int(round(max(MIN_LIGHT_KELVIN, min(MAX_LIGHT_KELVIN, number))))


def normalize_intensity(value,
                        default: int = DEFAULT_LIGHT_INTENSITY) -> int:
    """Интенсивность света в процентах: 0 — резкая кромка, 100 — плавная."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return int(default)
    if math.isnan(number) or math.isinf(number):
        return int(default)
    return int(round(max(MIN_LIGHT_INTENSITY,
                         min(MAX_LIGHT_INTENSITY, number))))


def kelvin_to_color(kelvin) -> str:
    """Цвет источника из температуры: 2200 K — оранжевый факел, 6500 K — белый.

    Приближение чёрного тела (Таннер Хелланд): глазу достаточно, чтобы
    свеча, факел и лампа отличались, а цвет оставался привычным.
    """
    t = max(1.0, min(40000.0, float(normalize_kelvin(kelvin)))) / 100.0
    if t <= 66.0:
        red = 255.0
        green = 99.4708025861 * math.log(t) - 161.1195681661
        blue = 0.0 if t <= 19.0 else (138.5177312231 * math.log(t - 10.0)
                                      - 305.0447927307)
    else:
        red = 329.698727446 * ((t - 60.0) ** -0.1332047592)
        green = 288.1221695283 * ((t - 60.0) ** -0.0755148492)
        blue = 255.0

    def clamp(channel: float) -> int:
        return int(round(max(0.0, min(255.0, channel))))

    return "#%02x%02x%02x" % (clamp(red), clamp(green), clamp(blue))


def falloff(light) -> float:
    """Доля радиуса, где свет ещё полный: 1.0 — резкая кромка, 0.15 — плавная.

    Холст рисует этим числом остановки радиального градиента, а правила
    (и тесты) могут считать кромку без Qt.
    """
    intensity = normalize_intensity((light or {}).get("intensity"))
    return 1.0 - MAX_LIGHT_FALLOFF * (intensity / 100.0)


def normalize_light(data, fallback_id: str = None) -> dict:
    """Один источник света из сети, мира или панели — всегда один вид.

    Незнакомые поля отбрасываем: карта хранит только точку, радиус, цвет и
    включённость, а лишнее раздувало бы сохранение мира.
    """
    data = data if isinstance(data, dict) else {}
    try:
        x = float(data.get("x"))
        y = float(data.get("y"))
    except (TypeError, ValueError):
        x = y = 0.0
    if not (math.isfinite(x) and math.isfinite(y)):
        x = y = 0.0
    light_id = str(data.get("id") or fallback_id or uuid.uuid4().hex[:12])
    name = str(data.get("name") or "").strip() or "Свет"
    return {
        "id": light_id,
        "name": name[:60],
        "x": max(-1_000_000.0, min(1_000_000.0, x)),
        "y": max(-1_000_000.0, min(1_000_000.0, y)),
        "radius_ft": normalize_radius(data.get("radius_ft")),
        "color": normalize_color(data.get("color")),
        "kelvin": normalize_kelvin(data.get("kelvin")),
        "intensity": normalize_intensity(data.get("intensity")),
        "enabled": bool(data.get("enabled", True)),
        # Метка на холсте: мастер может её спрятать, игрок не видит вовсе.
        "marker_hidden": bool(data.get("marker_hidden", False)),
        # Закреплённый источник нельзя случайно сдвинуть мышью.
        "locked": bool(data.get("locked", False)),
        # Оконный свет и уличные фонари горят только ночью: днём такой
        # источник остаётся на карте, но не светит. Будущие окна домов
        # будут включаться сами — устроено так же.
        "night_only": bool(data.get("night_only", False)),
    }


def normalize_lights(values) -> list:
    """Список источников без дублей по id."""
    result = []
    seen = set()
    for index, item in enumerate(values or []):
        if not isinstance(item, dict):
            continue
        light = normalize_light(item, fallback_id=f"light-{index}")
        if light["id"] in seen:
            continue
        seen.add(light["id"])
        result.append(light)
    return result


def burning(light, night: bool) -> bool:
    """Горит ли источник в это время суток: включён и «только ночью» не мешает."""
    light = light or {}
    if not light.get("enabled", True):
        return False
    return bool(night) or not light.get("night_only")


def point_in_polygon(x: float, y: float, polygon) -> bool:
    """Точка внутри многоугольника — луч вправо и подсчёт пересечений."""
    inside = False
    count = len(polygon)
    for index in range(count):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % count]
        if (y1 > y) != (y2 > y):
            cross_x = x1 + (y - y1) * (x2 - x1) / ((y2 - y1) or 1e-9)
            if cross_x > x:
                inside = not inside
    return inside


def convex_hull(points) -> list:
    """Выпуклая оболочка (монотонная цепь) — основа тени от объекта."""
    pts = sorted(set((float(x), float(y)) for x, y in points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return ((a[0] - o[0]) * (b[1] - o[1])
                - (a[1] - o[1]) * (b[0] - o[0]))

    lower = []
    for point in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 1e-9:
            lower.pop()
        lower.append(point)
    upper = []
    for point in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 1e-9:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def light_shadows(light_x: float, light_y: float, radius: float,
                  quads) -> list:
    """Тени, которые отбрасывают объекты на свет.

    ``quads`` — четырёхугольники объектов в мировых координатах. Тень —
    выпуклая оболочка самого объекта и его углов, отодвинутых от света:
    свет не проходит сквозь объект и «обходит» его. Если источник сам стоит
    внутри объекта (лампа в доме под крышей), объект его не заслоняет.
    """
    shadows = []
    if radius <= 0:
        return shadows
    for quad in quads or []:
        if not quad or len(quad) < 3:
            continue
        corners = [(float(px), float(py)) for px, py in quad]
        if point_in_polygon(light_x, light_y, corners):
            continue
        cx = sum(p[0] for p in corners) / len(corners)
        cy = sum(p[1] for p in corners) / len(corners)
        diagonal = max(math.hypot(px - cx, py - cy) for px, py in corners)
        # Объект дальше радиуса света — тень не нужна.
        if math.hypot(cx - light_x, cy - light_y) - diagonal > radius:
            continue
        # Тень должна выйти за пределы круга, иначе её край будет видно.
        reach = radius * 2.0 + diagonal * 2.0 + 1.0
        points = list(corners)
        for px, py in corners:
            dx, dy = px - light_x, py - light_y
            length = math.hypot(dx, dy) or 1.0
            points.append((px + dx / length * reach,
                           py + dy / length * reach))
        shadows.append(convex_hull(points))
    return shadows


def light_preset(key) -> dict:
    """Заготовка света по ключу; неизвестный ключ — None."""
    wanted = str(key or "").strip().lower()
    for preset in LIGHT_PRESETS:
        if preset["key"] == wanted:
            return dict(preset)
    return None


def light_from_preset(key, x: float, y: float, number: int = 1) -> dict:
    """Новый источник света из заготовки — готовый к записи в карту.

    ``number`` — какой по счёту источник этого вида: в списке видны
    «Свеча 1», «Свеча 2», а не три одинаковых «Свеча».
    """
    preset = light_preset(key) or light_preset(DEFAULT_LIGHT_PRESET)
    return normalize_light({
        "name": f"{preset['name']} {max(1, int(number))}",
        "x": x,
        "y": y,
        "radius_ft": preset["radius_ft"],
        "kelvin": preset["kelvin"],
        "intensity": preset["intensity"],
        "color": kelvin_to_color(preset["kelvin"]),
        "enabled": True,
    })


def map_lights(map_data) -> list:
    """Включённые источники света открытой карты (без учёта дня и ночи)."""
    lights = getattr(map_data, "lights", None) or []
    return [light for light in normalize_lights(lights) if light["enabled"]]


def outshines_daylight(light) -> bool:
    """Свет сильнее дня? — температура не ниже DAYLIGHT_GLOW_KELVIN.

    Такой источник рисуется и на дневной карте: магический свет, луч
    маяка, вспышка. Порог нарочно высокий — обычные свеча и факел днём не
    видны.
    """
    return (normalize_kelvin((light or {}).get("kelvin"))
            >= DAYLIGHT_GLOW_KELVIN)


def daylight_glow_alpha(light) -> int:
    """Альфа дневного отсвета источника: 0 — днём его не видно.

    Растёт от DAYLIGHT_GLOW_ALPHA_MIN на пороге до
    DAYLIGHT_GLOW_ALPHA_MAX на MAX_LIGHT_KELVIN: чем горячее источник,
    тем сильнее он «прошивает» день.
    """
    light = light or {}
    if not light.get("enabled", True):
        return 0
    kelvin = normalize_kelvin(light.get("kelvin"))
    if kelvin < DAYLIGHT_GLOW_KELVIN:
        return 0
    span = max(1.0, MAX_LIGHT_KELVIN - DAYLIGHT_GLOW_KELVIN)
    share = min(1.0, (kelvin - DAYLIGHT_GLOW_KELVIN) / span)
    return int(round(DAYLIGHT_GLOW_ALPHA_MIN
                     + (DAYLIGHT_GLOW_ALPHA_MAX - DAYLIGHT_GLOW_ALPHA_MIN)
                     * share))


def daylight_glow_color(light) -> str:
    """Цвет дневного отсвета: свет источника, разбелённый почти до белого.

    На солнце виден не оттенок лампы, а яркость: холодный сизый отсвет на
    тёплом скане карты читался бы как туман. Оттенок остаётся лишь как
    намёк на цвет источника.
    """
    color = (light or {}).get("color") or DEFAULT_LIGHT_COLOR
    color = normalize_color(color)
    channels = []
    for shift in (1, 3, 5):
        value = int(color[shift:shift + 2], 16)
        value += (255 - value) * DAYLIGHT_GLOW_WHITEN
        channels.append(max(0, min(255, int(round(value)))))
    return "#%02x%02x%02x" % tuple(channels)


def daylight_lights(map_data) -> list:
    """Источники, которые светят «сверх силы» днём.

    «Только ночью» не в счёт: такой источник днём погашен по определению,
    даже если он очень горячий (окно дома, уличный фонарь).
    """
    return [light for light in map_lights(map_data)
            if not light.get("night_only") and outshines_daylight(light)]


def active_lights(map_data) -> list:
    """Источники, которые горят именно сейчас.

    Днём выпадают источники с «только ночью»: так работают окна домов и
    уличные фонари. Ночью горят все включённые.
    """
    night = is_night(map_data)
    return [light for light in map_lights(map_data)
            if night or not light["night_only"]]


def is_night(map_data) -> bool:
    """Ночь ли сейчас на карте (освещение и темнота включаются ночью)."""
    return str(getattr(map_data, "time_of_day", "day") or "day") == "night"


def distance_ft(map_data, x1: float, y1: float, x2: float, y2: float):
    """Расстояние в футах по сетке карты; None — сетки нет, меры нет."""
    try:
        cell_w, cell_h = vision_rules.cell_size(map_data)
    except (AttributeError, TypeError, ValueError):
        return None
    if cell_w <= 0 or cell_h <= 0:
        return None
    return (math.hypot((float(x2) - float(x1)) / cell_w,
                       (float(y2) - float(y1)) / cell_h)
            * vision_rules.FT_PER_CELL)


def lit_by(map_data, x: float, y: float) -> list:
    """Какие источники освещают точку (пустой список — темнота)."""
    if not is_night(map_data):
        return []
    result = []
    for light in map_lights(map_data):
        distance = distance_ft(map_data, light["x"], light["y"], x, y)
        if distance is None:
            # Без сетки радиус не перевести в мировые единицы — свет
            # считается оформлением, а не геометрией.
            return []
        if distance <= light["radius_ft"] + 1e-6:
            result.append(light)
    return result


def light_level(map_data, x: float, y: float) -> str:
    """Освещённость точки по 5e: ``BRIGHT``, ``DIM`` или ``DARK``.

    Днём вся карта в ярком свете: тусклый и яркий свет различаются только
    от источника, а без ночи затемнять нечего.

    Ночью круг источника делится так же, как его рисует холст: до
    ``falloff`` радиуса свет полный (яркий), а дальше до самого радиуса —
    кромка, то есть тусклый свет. Один и тот же ``falloff`` используют и
    градиент на экране, и правила, поэтому «тень на картинке» и «тусклый
    свет в проверках» совпадают, а не живут отдельно.
    """
    if map_data is None or not is_night(map_data):
        return BRIGHT
    if distance_ft(map_data, 0.0, 0.0, 1.0, 1.0) is None:
        # Без сетки радиус не перевести в футы: свет считается оформлением.
        return BRIGHT
    level = DARK
    for light in map_lights(map_data):
        distance = distance_ft(map_data, light["x"], light["y"], x, y)
        if distance is None:
            continue
        radius = float(light["radius_ft"])
        if distance <= radius * falloff(light) + 1e-6:
            return BRIGHT
        if distance <= radius + 1e-6:
            level = DIM
    return level


def is_dim(map_data, x: float, y: float) -> bool:
    """Точка в тусклом свете: слабо заслонённая местность по 5e."""
    return light_level(map_data, x, y) == DIM


def light_label(level) -> str:
    """Подпись категории освещения для чата: «тусклый свет», «темнота»."""
    return LIGHT_LABELS.get(str(level or ""), LIGHT_LABELS[BRIGHT])


def is_dark(map_data, x: float, y: float) -> bool:
    """Точка в темноте: ночь и ни один источник её не освещает."""
    return light_level(map_data, x, y) == DARK


def darkvision_ft(*texts) -> float:
    """Тёмное зрение из описаний: «тёмное зрение 60 фт» -> 60.

    Обычное зрение не считается: в темноте без тёмного зрения существо
    слепое, иначе любой токен видел бы ночью как днём.
    """
    best = 0.0
    for text in texts:
        if not text:
            continue
        if isinstance(text, (list, tuple)):
            text = ", ".join(str(part) for part in text)
        source = str(text).casefold().replace("ё", "е")
        for match in _DARKVISION_RE.finditer(source):
            try:
                feet = float(match.group(1))
            except (TypeError, ValueError):
                continue
            best = max(best, feet)
    return min(float(vision_rules.MAX_RANGE_FT), best)


def _feet_number(value) -> float:
    """Число футов из поля листа: 60, "60 фт" или {"value": 60}."""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        feet = float(value)
    elif isinstance(value, str):
        match = re.search(r"\d+", value)
        if not match:
            return 0.0
        feet = float(match.group())
    elif isinstance(value, dict):
        for key in ("value", "total", "ft", "feet"):
            if key in value:
                return _feet_number(value[key])
        return 0.0
    else:
        return 0.0
    if not math.isfinite(feet) or feet <= 0:
        return 0.0
    return min(MAX_DARKVISION_FT, feet)


def darkvision_toggle(sheet):
    """Галочка «тёмное зрение» из листа: True, False или None.

    Поле живёт в ``vitality.darkvision`` (там же, где его число). None —
    у листа галочки нет: старые и импортированные листы читаются по
    прежним правилам.
    """
    if not isinstance(sheet, dict):
        return None
    vitality = sheet.get("vitality")
    if not isinstance(vitality, dict):
        return None
    cell = vitality.get("darkvision")
    if isinstance(cell, dict) and "enabled" in cell:
        return bool(cell.get("enabled"))
    return None


def darkvision_feet(sheet) -> float:
    """Число футов из поля листа — даже если галочка снята."""
    if not isinstance(sheet, dict):
        return 0.0
    vitality = sheet.get("vitality")
    if not isinstance(vitality, dict):
        return 0.0
    return _feet_number(vitality.get("darkvision"))


def darkvision_from_sheet(sheet) -> float:
    """Тёмное зрение из листа любой формы (LSS/Foundry).

    В листе LSS оно живёт числом (``vitality.darkvision = {"value": 60}``),
    а в Foundry — строкой «тёмное зрение 60 фт». Раньше читались только
    строки, и токен с числовым полем оставался без тёмного зрения, а с ним и
    без света ночью. Теперь обход смотрит и на имя поля.

    Главное — галочка `darkvision_toggle`: если в листе она есть, решает
    она. Снятая галочка выключает тёмное зрение даже тогда, когда в тексте
    листа оно упомянуто, а поднятая включает его и без описания (по
    умолчанию 60 фт). Без галочки (старые листы) работает прежний разбор:
    число поля и фраза «тёмное зрение 60 фт» в ЧУВСТВАХ. Раньше свободный
    текст брался из всего листа — и описание чужой расовой черты или
    заклинания давало токену тёмное зрение «автоматом».
    """
    toggle = darkvision_toggle(sheet)
    if toggle is False:
        return 0.0
    best = darkvision_feet(sheet)
    strings = []

    def walk(node, depth=0, senses=False):
        nonlocal best
        if depth > 10:
            return
        if isinstance(node, dict):
            for name, value in node.items():
                name = str(name)
                if _DARKVISION_KEY_RE.fullmatch(name.strip()):
                    best = max(best, _feet_number(value))
                walk(value, depth + 1,
                     senses or bool(_SENSES_KEY_RE.search(name)))
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value, depth + 1, senses)
        elif isinstance(node, str):
            if not senses:
                return
            # В LSS описание часто лежит одной строкой с JSON-разметкой:
            # «тёмное зрение» и «60 футов» разделены кавычками и тегами.
            # Оставляем буквы, цифры и пробелы — фраза снова читается.
            strings.append(re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", " ", node))

    walk(sheet)
    best = max(best, darkvision_ft(strings))
    if toggle:
        return best if best > 0 else DEFAULT_DARKVISION_FT
    return best


def token_darkvision_ft(token, sheet=None) -> float:
    """Тёмное зрение токена в футах: из статблока существа или листа героя.

    Одно правило для сервера (скрытность и Восприятие) и для холста: игроку
    ночью положено видеть в чёрно-белом на дальность тёмного зрения, и
    считать её должен тот же код, что и серверный запрет спрятаться.
    """
    block = getattr(token, "statblock", None)
    texts = [block.get("senses")] if isinstance(block, dict) else []
    return max(darkvision_ft(texts), darkvision_from_sheet(sheet))


def sees(observer, target, map_data, darkvision: float = 0.0) -> bool:
    """Видит ли наблюдатель цель с учётом света и темноты.

    Днём — только конус зрения. Ночью цель должна быть освещена или
    попадать в дальность тёмного зрения наблюдателя.
    """
    if map_data is None:
        return False
    if not vision_rules.in_cone(observer, target, map_data):
        return False
    if not is_night(map_data):
        return True
    try:
        tx = float(getattr(target, "x", 0.0) or 0.0)
        ty = float(getattr(target, "y", 0.0) or 0.0)
    except (TypeError, ValueError):
        return True
    if lit_by(map_data, tx, ty):
        return True
    if distance_ft(map_data, 0.0, 0.0, 1.0, 1.0) is None:
        # Без сетки свет и темноту не измерить: считаем, что конус решает.
        return True
    if darkvision <= 0:
        return False
    distance = distance_ft(
        map_data, getattr(observer, "x", 0.0) or 0.0,
        getattr(observer, "y", 0.0) or 0.0, tx, ty)
    return distance is not None and distance <= darkvision


def perception_penalty(observer, map_data, darkvision: float = 0.0,
                       target=None) -> int:
    """Помеха на Восприятие в заслонённой местности: −5 к пассивному.

    По 5e тусклый свет — слабо заслонённая местность, тьма — сильно
    заслонённая; в обеих проверки Мудрости (Восприятие), полагающиеся на
    зрение, идут с помехой, а в пассивном значении помеха — это −5.

    Тёмное зрение не снимает помеху: 5e говорит, что в темноте такое
    существо видит «как в тусклом свете», то есть область для него всё равно
    слабо заслонена. Поэтому ``darkvision`` остаётся в подписи ради
    совместимости и ясности вызова, но число не меняет: тьму от тусклого
    света в пассивном Восприятии отличает не помеха, а то, видят ли токен
    вообще (``sees``).

    ``target`` — кого пытаются заметить: помеха относится к освещённости
    цели, а не наблюдателя, иначе освещённую цель не увидели бы из темноты.
    """
    if map_data is None or not is_night(map_data):
        return 0
    point = target if target is not None else observer
    try:
        tx = float(getattr(point, "x", 0.0) or 0.0)
        ty = float(getattr(point, "y", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0
    if distance_ft(map_data, 0.0, 0.0, 1.0, 1.0) is None:
        return 0
    # Освещённую цель видно и из темноты: помеха относится к самой цели,
    # а не к месту наблюдателя.
    if light_level(map_data, tx, ty) == BRIGHT:
        return 0
    return -5
