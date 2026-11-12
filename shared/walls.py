"""Стены, окна и двери: геометрия, общая для холста, сервера и тестов.

Стена — ломаная линия в мировых координатах. Она заслоняет свет и обзор, но
не мешает пройти: пройти нельзя только сквозь неё, поэтому для хода считаются
те же отрезки, но с проёмами. Проём (окно или дверь) — вырез в стене:

* **окно** пропускает свет и взгляд, но не существо;
* **дверь** закрытая не пропускает ничего, открытая — и свет, и взгляд, и ход.

Проём хранится не отдельными точками, а привязкой к стене: ``wall_id``,
номер отрезка ``seg``, расстояние от его начала ``at`` и ширина ``width``.
Так стена остаётся одной фигурой: её можно тянуть за точки, поворачивать и
удалять, а окна и двери едут вместе с ней и не отрываются от стены.

Модуль чистый: ни Qt, ни сети, ни картинок. Холст рисует по этим функциям,
сервер ими же запрещает проход сквозь стену, а тесты проверяют числа.
"""
import math

#: Сколько отрезков может быть в одной стене и сколько проёмов на карте.
#: Число стен не ограничено: городская карта приходит из Dungeondraft с
#: сотнями стен, и прежний потолок в 120 молча обрезал импорт.
MAX_POINTS = 64
MAX_OPENINGS = 240

#: Виды проёмов.
WINDOW = "window"
DOOR = "door"
OPENING_KINDS = (WINDOW, DOOR)

#: Магнит вершин стены: шаг привязки в долях клетки. Клетка — точные
#: углы комнат, половина — когда стена должна встать по центру клетки.
SNAP_STEPS = (1.0, 0.5)
DEFAULT_SNAP_STEP = 1.0

#: Ширина проёма в футах: дверь в подземелье — клетка, окно — тоже.
MIN_OPENING_FT = 5.0
MAX_OPENING_FT = 60.0
DEFAULT_DOOR_FT = 5.0
DEFAULT_WINDOW_FT = 5.0

#: Лучи для многоугольника видимости. Больше — точнее края теней, дороже
#: кадр: 220 лучей дают шаг 1.6° и на глаз неотличимы от «настоящего» света.
DEFAULT_RAYS = 220
#: Сколько концов стен получат свои лучи. На столе с сотнями стен выше
#: крышки берётся ближайшие (по порядку обхода): они и образуют край обзора.
MAX_CORNER_POINTS = 240
#: Разворот дополнительного луча у конца стены. Равномерной развёртки мало:
#: у окна граница обзора идёт по лучу, который при сдвиге токена перескакивает
#: со стены в проём, и просвет дёргается. Лучи точно в концы отрезков (и чуть
#: в стороны от них) ставят вершины обзора на косяки — при движении они едут
#: плавно. Смещение — доли угловой секунды, на экране его не видно.
CORNER_RAY_EPS = 1e-4


def _number(value, default=None):
    """Конечное число из чего угодно; иначе ``default``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def normalize_point(raw):
    """Точка стены: ``[x, y]`` с конечными координатами или None."""
    if isinstance(raw, dict):
        raw = (raw.get("x"), raw.get("y"))
    try:
        x, y = raw
    except (TypeError, ValueError):
        return None
    x = _number(x)
    y = _number(y)
    if x is None or y is None:
        return None
    # Миллион мировых единиц — уже за любым разумным холстом; такие
    # координаты ломают тени и зум, поэтому обрезаем, а не тащим дальше.
    return [max(-1_000_000.0, min(1_000_000.0, x)),
            max(-1_000_000.0, min(1_000_000.0, y))]


def normalize_snap_step(value) -> float:
    """Шаг привязки стен: только целая клетка или её половина."""
    step = _number(value, DEFAULT_SNAP_STEP)
    if step is None or step not in SNAP_STEPS:
        return DEFAULT_SNAP_STEP
    return float(step)


def normalize_wall(raw) -> dict:
    """Одна стена из сети или файла мира; None — если рисовать нечего."""
    if not isinstance(raw, dict):
        return None
    points = []
    for item in raw.get("points") or []:
        point = normalize_point(item)
        if point is not None:
            points.append(point)
    # Соседние совпадающие точки не нужны: они дают отрезки нулевой длины,
    # на которых ломается пересечение лучей.
    clean = []
    for point in points:
        if not clean or (abs(clean[-1][0] - point[0]) > 1e-6
                         or abs(clean[-1][1] - point[1]) > 1e-6):
            clean.append(point)
    if len(clean) < 2:
        return None
    wall_id = str(raw.get("id") or "").strip()
    if not wall_id:
        return None
    return {
        "id": wall_id,
        "points": clean[:MAX_POINTS],
        "closed": bool(raw.get("closed")),
    }


def normalize_walls(raw) -> list:
    """Список стен карты: без мусора и без дублей id."""
    walls = []
    seen = set()
    for item in raw or []:
        wall = normalize_wall(item)
        if wall is None or wall["id"] in seen:
            continue
        seen.add(wall["id"])
        walls.append(wall)
    return walls


def normalize_opening(raw) -> dict:
    """Один проём; None — если он не привязан к стене или пустой."""
    if not isinstance(raw, dict):
        return None
    opening_id = str(raw.get("id") or "").strip()
    wall_id = str(raw.get("wall_id") or "").strip()
    if not opening_id or not wall_id:
        return None
    kind = str(raw.get("kind") or DOOR).strip().lower()
    if kind not in OPENING_KINDS:
        kind = DOOR
    try:
        seg = int(raw.get("seg") or 0)
    except (TypeError, ValueError):
        seg = 0
    at = _number(raw.get("at"), 0.0)
    width = _number(raw.get("width"), DEFAULT_DOOR_FT)
    locked = bool(raw.get("locked"))
    return {
        "id": opening_id,
        "wall_id": wall_id,
        "seg": max(0, seg),
        "at": max(0.0, at),
        # Ширина хранится в мировых единицах: так она не зависит от сетки
        # карты, которую мастер может перестроить после расстановки стен.
        "width": max(1.0, min(20_000.0, width)),
        "kind": kind,
        # Запертая дверь закрыта принудительно: открыть её можно только
        # отперев, иначе «замок» ничего не значит.
        "open": bool(raw.get("open")) and not locked,
        "locked": locked,
    }


def normalize_openings(raw, walls=None) -> list:
    """Проёмы карты: только от существующих стен, без дублей id.

    ``walls`` — уже нормализованные стены: проём без стены рисовать не на
    чем, и он молча выпадает. Так удаление стены не оставляет висящих окон.
    """
    known = {wall["id"] for wall in walls} if walls is not None else None
    openings = []
    seen = set()
    for item in raw or []:
        opening = normalize_opening(item)
        if opening is None or opening["id"] in seen:
            continue
        if known is not None and opening["wall_id"] not in known:
            continue
        seen.add(opening["id"])
        openings.append(opening)
        if len(openings) >= MAX_OPENINGS:
            break
    return openings


def map_walls(map_data) -> list:
    """Стены карты, приведённые к одному виду."""
    return normalize_walls(getattr(map_data, "walls", None))


def map_openings(map_data, walls=None) -> list:
    """Проёмы карты (по умолчанию — с проверкой, что стены на месте)."""
    return normalize_openings(getattr(map_data, "openings", None),
                              walls if walls is not None
                              else map_walls(map_data))


def wall_segments(wall) -> list:
    """Отрезки ломаной стены; у замкнутой последняя точка соединяется с первой."""
    points = wall.get("points") or []
    segments = []
    for index in range(len(points) - 1):
        segments.append((tuple(points[index]), tuple(points[index + 1])))
    if wall.get("closed") and len(points) >= 3:
        segments.append((tuple(points[-1]), tuple(points[0])))
    return segments


def segment_length(segment) -> float:
    (x1, y1), (x2, y2) = segment
    return math.hypot(x2 - x1, y2 - y1)


def point_on_segment(segment, x: float, y: float) -> tuple:
    """Ближайшая точка отрезка: ``(at, distance)`` — вдоль и до неё."""
    (x1, y1), (x2, y2) = segment
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-9:
        return 0.0, math.hypot(x - x1, y - y1)
    t = ((x - x1) * dx + (y - y1) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    at = t * math.sqrt(length_sq)
    return at, math.hypot(x - (x1 + dx * t), y - (y1 + dy * t))


def point_along(segment, at: float) -> tuple:
    """Точка на отрезке на расстоянии ``at`` от начала."""
    (x1, y1), (x2, y2) = segment
    length = segment_length(segment)
    if length <= 1e-9:
        return x1, y1
    t = max(0.0, min(1.0, at / length))
    return x1 + (x2 - x1) * t, y1 + (y2 - y1) * t


def opening_segment(wall, opening) -> tuple:
    """Отрезок проёма вдоль стены: по нему рисуется окно или дверь.

    None — если стена или отрезок не на месте (проём «потерялся»).
    """
    segments = wall_segments(wall)
    index = int(opening.get("seg") or 0)
    if index < 0 or index >= len(segments):
        return None
    segment = segments[index]
    length = segment_length(segment)
    if length <= 1e-9:
        return None
    half = float(opening.get("width") or 0.0) / 2.0
    at = max(0.0, min(length, float(opening.get("at") or 0.0)))
    start = point_along(segment, max(0.0, at - half))
    end = point_along(segment, min(length, at + half))
    return start, end


def _open_intervals(wall, index: int, openings, *, light: bool) -> list:
    """Вырезы на отрезке стены: интервалы расстояний, где стены нет.

    ``light`` — считать ли проём пропускающим свет и взгляд. Окно пропускает
    всегда, дверь — только открытая; для хода окно не вырез вовсе.
    """
    (x1, y1), (x2, y2) = wall_segments(wall)[index]
    length = math.hypot(x2 - x1, y2 - y1)
    if length <= 1e-9:
        return []
    intervals = []
    for opening in openings or []:
        if opening.get("wall_id") != wall.get("id"):
            continue
        if int(opening.get("seg") or 0) != index:
            continue
        kind = opening.get("kind")
        if light:
            if kind == DOOR and (not opening.get("open")
                                 or opening.get("locked")):
                continue
            if kind not in OPENING_KINDS:
                continue
        else:
            # Пройти можно только в открытую и незапертую дверь: окно и
            # замок — не проход.
            if (kind != DOOR or not opening.get("open")
                    or opening.get("locked")):
                continue
        half = float(opening.get("width") or 0.0) / 2.0
        at = float(opening.get("at") or 0.0)
        start = max(0.0, at - half)
        end = min(length, at + half)
        if end - start > 1e-6:
            intervals.append((start, end))
    return sorted(intervals)


def blocking_spans(wall, openings, *, light: bool = True) -> list:
    """Куски стены, которые действительно заслоняют: стена минус проёмы."""
    spans = []
    for index, segment in enumerate(wall_segments(wall)):
        length = segment_length(segment)
        if length <= 1e-9:
            continue
        cursor = 0.0
        for start, end in _open_intervals(wall, index, openings, light=light):
            if start - cursor > 1e-6:
                spans.append((point_along(segment, cursor),
                              point_along(segment, start)))
            cursor = max(cursor, end)
        if length - cursor > 1e-6:
            spans.append((point_along(segment, cursor),
                          point_along(segment, length)))
    return spans


def wall_spans(map_data, *, light: bool = True) -> list:
    """Все непрозрачные куски стен карты.

    ``light=True`` — для света и взгляда (окна и открытые двери пропускают),
    ``light=False`` — для хода (только открытая дверь).
    """
    walls = map_walls(map_data)
    openings = map_openings(map_data, walls)
    spans = []
    for wall in walls:
        spans.extend(blocking_spans(wall, openings, light=light))
    return spans


# ---------------------------------------------------------------------------
# Пересечения и видимость
# ---------------------------------------------------------------------------
def _cross(ax, ay, bx, by):
    return ax * by - ay * bx


def segment_intersection(p1, p2, p3, p4):
    """Точка пересечения двух отрезков или None (в том числе параллельных)."""
    (x1, y1), (x2, y2) = p1, p2
    (x3, y3), (x4, y4) = p3, p4
    dx1, dy1 = x2 - x1, y2 - y1
    dx2, dy2 = x4 - x3, y4 - y3
    denominator = _cross(dx1, dy1, dx2, dy2)
    if abs(denominator) <= 1e-12:
        return None
    t = _cross(x3 - x1, y3 - y1, dx2, dy2) / denominator
    u = _cross(x3 - x1, y3 - y1, dx1, dy1) / denominator
    if -1e-9 <= t <= 1 + 1e-9 and -1e-9 <= u <= 1 + 1e-9:
        return x1 + dx1 * t, y1 + dy1 * t
    return None


def ray_hit(origin, angle_rad: float, limit: float, segments):
    """Ближайшее попадание луча в отрезки; None — если в пределах нет стены."""
    ox, oy = origin
    dx, dy = math.cos(angle_rad), math.sin(angle_rad)
    end = (ox + dx * limit, oy + dy * limit)
    best = None
    for segment in segments:
        hit = segment_intersection((ox, oy), end, segment[0], segment[1])
        if hit is None:
            continue
        distance = math.hypot(hit[0] - ox, hit[1] - oy)
        if distance <= 1e-9:
            continue
        if best is None or distance < best[0]:
            best = (distance, hit)
    return None if best is None else best[1]


class SegmentIndex:
    """Отрезки стен, разложенные по сетке: свет видит только соседние.

    Многоугольник видимости пускает N лучей по всем отрезкам карты. На
    большом подземелье это сотни стен на каждый источник, и добрая часть
    работы тратится на отрезки в другом конце карты: луч света в 20 футов
    до них всё равно не достанет.

    Индекс делит мир на клетки (по умолчанию 128 мировых единиц — примерно
    две клетки стола) и запоминает, какие отрезки задевают каждую клетку.
    Для источника берутся только отрезки в клетках его круга: на сцене из
    120 стен и 24 источников это в десятки раз меньше пересечений.
    """

    #: Клетка индекса в мировых единицах. Мельче — точнее отбор, но больше
    #: работы при постройке; крупнее — меньше памяти, больше лишних стен.
    CELL = 128.0

    def __init__(self, segments, cell: float = None):
        self.cell = float(cell or self.CELL)
        if self.cell <= 0:
            self.cell = self.CELL
        self.segments = []
        for segment in segments or []:
            try:
                first, second = segment[0], segment[1]
                self.segments.append(((float(first[0]), float(first[1])),
                                      (float(second[0]), float(second[1]))))
            except (TypeError, ValueError, IndexError):
                continue
        self.buckets = {}
        for index, (first, second) in enumerate(self.segments):
            for key in self._cells(first, second):
                self.buckets.setdefault(key, []).append(index)
        #: Кэш по свету: источник не двигается между кадрами, а список
        #: отрезков ему нужен на каждый пересчёт маски.
        self._near_cache = {}

    def _cells(self, first, second):
        """Клетки, которые задевает отрезок (по его рамке)."""
        step = self.cell
        x0 = int(math.floor(min(first[0], second[0]) / step))
        x1 = int(math.floor(max(first[0], second[0]) / step))
        y0 = int(math.floor(min(first[1], second[1]) / step))
        y1 = int(math.floor(max(first[1], second[1]) / step))
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                yield (gx, gy)

    def near(self, x: float, y: float, radius: float) -> list:
        """Отрезки в пределах круга света (и чуть больше — рамка клетки)."""
        if not self.segments:
            return []
        step = self.cell
        try:
            radius = float(radius)
        except (TypeError, ValueError):
            return list(self.segments)
        if radius <= 0:
            return []
        # Запас в клетку: отрезок может лежать в соседней клетке и всё равно
        # пересекать круг.
        pad = step
        x0 = int(math.floor((x - radius - pad) / step))
        x1 = int(math.floor((x + radius + pad) / step))
        y0 = int(math.floor((y - radius - pad) / step))
        y1 = int(math.floor((y + radius + pad) / step))
        key = (x0, x1, y0, y1)
        cached = self._near_cache.get(key)
        if cached is not None:
            return cached
        picked = set()
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                bucket = self.buckets.get((gx, gy))
                if bucket:
                    picked.update(bucket)
        result = [self.segments[index] for index in sorted(picked)]
        if len(self._near_cache) > 512:
            self._near_cache.clear()
        self._near_cache[key] = result
        return result


def ray_angles(x: float, y: float, radius: float, segments, rays: int,
              ) -> list:
    """Углы лучей: равномерная развёртка плюс лучи точно в концы стен.

    Без лучей в концы обзор у проёма дёргается: ближайший к косяку луч при
    сдвиге наблюдателя поочерёдно попадает то в стену, то в проём, и край
    просвета перескакивает на шаг развёртки. С лучами в косяки вершины
    обзора стоят ровно на концах стены и едут вместе с ней.
    """
    count = max(16, min(720, int(rays)))
    angles = [2.0 * math.pi * index / count for index in range(count)]
    seen = {round(angle, 7) for angle in angles}
    reach = float(radius)
    corners = 0
    for segment in segments:
        for px, py in segment:
            if corners >= MAX_CORNER_POINTS:
                break
            dx = float(px) - x
            dy = float(py) - y
            if abs(dx) < 1e-9 and abs(dy) < 1e-9:
                continue
            # Конец стены дальше дальности обзора край не образует.
            if reach > 0 and math.hypot(dx, dy) > reach + 1e-6:
                continue
            corners += 1
            base = math.atan2(dy, dx)
            # Два луча — по обе стороны косяка. Третий, точно в угол, не
            # нужен: он совпадает с одним из них до долей угловой секунды.
            for offset in (-CORNER_RAY_EPS, CORNER_RAY_EPS):
                angle = (base + offset) % (2.0 * math.pi)
                key = round(angle, 7)
                if key in seen:
                    continue
                seen.add(key)
                angles.append(angle)
    angles.sort()
    return angles


def visibility_polygon(x: float, y: float, radius: float, segments,
                       rays: int = DEFAULT_RAYS) -> list:
    """Что видно из точки: многоугольник в пределах ``radius``.

    Лучи идут по кругу и обрезаются первой стеной. Это тот же способ, каким
    считаются тени в 2D-играх. Кроме равномерной развёртки есть лучи точно
    в концы стен (см. `ray_angles`): без них край обзора у окна или двери
    дрожит при движении токена.
    """
    if radius <= 0:
        return []
    count = max(16, min(720, int(rays)))
    # Только стены в пределах дальности: дальше луч всё равно не достанет,
    # а пересечения с ними стоят столько же, сколько с ближними (и на
    # больших подземельях именно они тормозили обзор при движении).
    reachable = [segment for segment in segments
                 if point_segment_distance((x, y), segment[0],
                                           segment[1]) <= radius]
    segments = reachable
    angles = ray_angles(x, y, radius, segments, count)
    # Быстрый путь: все лучи сразу. Пересечение луча с отрезком — это две
    # формулы на луч и стену; на сцене из десятков источников и сотен стен
    # Python тратит на это секунды, а матрица — десятки миллисекунд.
    # numpy необязателен (он же нужен только автоопределению сетки),
    # поэтому без него считаем по-старому.
    fast = _visibility_polygon_numpy(x, y, radius, segments, angles)
    if fast is not None:
        return fast
    points = []
    for angle in angles:
        hit = ray_hit((x, y), angle, radius, segments)
        points.append(hit if hit is not None
                      else (x + math.cos(angle) * radius,
                            y + math.sin(angle) * radius))
    return points


def _visibility_polygon_numpy(x: float, y: float, radius: float,
                              segments, angles):
    """Тот же многоугольник видимости, но пересечения — матрицей.

    Возвращает None, если numpy нет или отрезки не в том виде: тогда
    вызывающий считает обычным циклом. Углы и порядок точек совпадают
    с `ray_hit` до сотых — тесты сравнивают оба пути.
    """
    try:
        import numpy as np
    except Exception:
        return None
    try:
        array = np.asarray(segments, dtype=float)
        if array.ndim != 3 or array.shape[1:] != (2, 2) or not len(array):
            return None
    except Exception:
        return None
    start_x = array[:, 0, 0]
    start_y = array[:, 0, 1]
    seg_x = array[:, 1, 0] - start_x
    seg_y = array[:, 1, 1] - start_y
    angles = np.asarray(angles, dtype=float)
    cos = np.cos(angles)
    sin = np.sin(angles)
    dir_x = cos[:, None]
    dir_y = sin[:, None]
    denom = dir_x * seg_y[None, :] - dir_y * seg_x[None, :]
    offset_x = start_x[None, :] - x
    offset_y = start_y[None, :] - y
    with np.errstate(divide="ignore", invalid="ignore"):
        along_ray = (offset_x * seg_y[None, :]
                     - offset_y * seg_x[None, :]) / denom
        along_seg = (offset_x * dir_y - offset_y * dir_x) / denom
    valid = ((np.abs(denom) > 1e-12)
             & (along_ray >= 0.0) & (along_ray <= radius)
             & (along_seg >= 0.0) & (along_seg <= 1.0))
    along_ray = np.where(valid, along_ray, np.inf)
    best = along_ray.min(axis=1)
    hit = np.isfinite(best)
    # До ограничения радиуса: UNLIMITED умножился бы на ноль и дал NaN.
    reach = np.where(hit, best, radius)
    points_x = x + cos * reach
    points_y = y + sin * reach
    return list(zip(points_x.tolist(), points_y.tolist()))


def point_segment_distance(point, start, end) -> float:
    """Расстояние от точки до отрезка (0 — точка лежит на нём)."""
    px, py = float(point[0]), float(point[1])
    ax, ay = float(start[0]), float(start[1])
    bx, by = float(end[0]), float(end[1])
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    if length <= 1e-12:
        return math.hypot(px - ax, py - ay)
    share = ((px - ax) * dx + (py - ay) * dy) / length
    share = max(0.0, min(1.0, share))
    return math.hypot(px - (ax + dx * share), py - (ay + dy * share))


def nearest_wall_distance(x: float, y: float, segments) -> float:
    """Расстояние до ближайшего куска стены; `inf` — стен нет вовсе.

    Нужно туману комнат: токен, стоящий на самой стене (или вплотную к ней),
    не должен открывать то, что за стеной — непонятно, в какой он комнате.
    """
    best = math.inf
    for segment in segments or ():
        distance = point_segment_distance((x, y), segment[0], segment[1])
        if distance < best:
            best = distance
    return best


def point_in_polygon(x: float, y: float, polygon) -> bool:
    """Точка внутри многоугольника (луч вправо, без Qt)."""
    inside = False
    if not polygon:
        return False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            cross_x = xi + (y - yi) * (xj - xi) / (yj - yi)
            if x < cross_x:
                inside = not inside
        j = i
    return inside


def path_blocked(x1: float, y1: float, x2: float, y2: float, segments) -> bool:
    """Пересекает ли путь отрезок-препятствие."""
    if (abs(x2 - x1) <= 1e-9 and abs(y2 - y1) <= 1e-9):
        return False
    for segment in segments or []:
        if segment_intersection((x1, y1), (x2, y2), segment[0], segment[1]):
            return True
    return False


def token_path_blocked(map_data, x1: float, y1: float,
                       x2: float, y2: float) -> bool:
    """Перекрыт ли путь токена стенами карты (окна и закрытые двери — да)."""
    return path_blocked(x1, y1, x2, y2, wall_spans(map_data, light=False))


# ---------------------------------------------------------------------------
# Двери
# ---------------------------------------------------------------------------
def door_crossing(map_data, x1: float, y1: float, x2: float, y2: float,
                  include_locked: bool = False):
    """Закрытая дверь, через которую идёт путь: её и надо открыть.

    Сначала ищем дверь: если путь упирается в дверь, то это не «стена», а
    «дверь, которую толкнули». Сервер открывает её сам — так токен проходит
    в комнату одним движением, а звук слышат все.

    Запертая дверь не открывается сама: по умолчанию она не в счёт (токен
    просто упирается в неё, как в стену). ``include_locked=True`` нужен,
    чтобы отличить «стена» от «дверь заперта» и сказать это игроку.
    """
    walls = {wall["id"]: wall for wall in map_walls(map_data)}
    for opening in map_openings(map_data, list(walls.values())):
        if opening.get("kind") != DOOR or opening.get("open"):
            continue
        if opening.get("locked") and not include_locked:
            continue
        wall = walls.get(opening["wall_id"])
        if wall is None:
            continue
        segment = opening_segment(wall, opening)
        if segment is None:
            continue
        if segment_intersection((x1, y1), (x2, y2),
                                segment[0], segment[1]):
            return opening
    return None


def set_door_lock(map_data, opening_id: str, locked):
    """Запереть или отпереть дверь. -> проём или None (если это не дверь).

    Запирание сразу закрывает дверь: запертая открытая дверь — это дверь,
    через которую можно пройти, то есть замок ни на что не влияет.
    """
    for opening in getattr(map_data, "openings", None) or []:
        if opening.get("id") != opening_id:
            continue
        if opening.get("kind") != DOOR:
            return None
        opening["locked"] = bool(locked)
        if opening["locked"]:
            opening["open"] = False
        return opening
    return None


def toggle_door(map_data, opening_id: str, open_state=None):
    """Открыть или закрыть дверь в модели карты. -> новый проём или None."""
    for opening in getattr(map_data, "openings", None) or []:
        if opening.get("id") != opening_id:
            continue
        if opening.get("kind") != DOOR:
            return None
        opening["open"] = (not opening.get("open")) if open_state is None \
            else bool(open_state)
        return opening
    return None


#: Шаг грубой сетки для поиска комнат, в мировых единицах. Комната — это
#: десятки клеток, поэтому 40 единиц дают достаточную точность: стены
#: толщиной ноль всё равно отмечают границу, а лишние пересчёты не нужны.
ROOM_STEP = 40.0

#: Предел клеток грубой сетки: огромная карта не должна стоить секунду
#: кадра. Шаг увеличивается, пока клеток не станет меньше предела.
ROOM_MAX_CELLS = 40000


def enclosed_regions(map_data, step: float = ROOM_STEP,
                     max_cells: int = ROOM_MAX_CELLS) -> list:
    """Замкнутые области карты (комнаты) — прямоугольниками в мире.

    Зачем: туман войны красит комнаты и открытый мир по-разному. Игрок не
    должен знать даже, что за стеной есть помещение, поэтому неразведанная
    комната идёт в чёрный, а коридор и улица — полупрозрачным туманом.

    Возвращает плоский список (x, y, w, h): строки грубой сетки. Кому нужно
    знать, какие полосы принадлежат одной комнате, — `enclosed_room_groups`.

    Открытая дверь стен не оставляет — комнаты за ней соединяются в одну,
    как и должно быть.
    """
    return [region for group in enclosed_room_groups(map_data, step, max_cells)
            for region in group]


def enclosed_room_groups(map_data, step: float = ROOM_STEP,
                         max_cells: int = ROOM_MAX_CELLS) -> list:
    """Замкнутые комнаты по отдельности: на комнату — список полос.

    Как считаем без геометрических библиотек: карта режется на грубые
    клетки, стены помечают клетки, через которые проходят, затем заливка от
    края карты отмечает всё, до чего можно дойти не пересекая стен. Всё
    оставшееся свободным и есть замкнутая комната; связные клетки одной
    комнаты получают один номер.

    Группы нужны туману: от комнаты красится и размечается только она сама.
    Раньше полосы всех комнат шли одним списком, и соседние комнаты
    склеивались в одну — зашёл в одну, открылись все.
    """
    try:
        width = float(getattr(map_data, "width", 0.0) or 0.0)
        height = float(getattr(map_data, "height", 0.0) or 0.0)
    except (TypeError, ValueError):
        return []
    if width <= 0 or height <= 0:
        return []
    spans = wall_spans(map_data, light=False)
    if not spans:
        return []
    origin_x = float(getattr(map_data, "pos_x", 0.0) or 0.0)
    origin_y = float(getattr(map_data, "pos_y", 0.0) or 0.0)
    step = max(1.0, float(step))
    while (width / step) * (height / step) > max_cells:
        step *= 1.5
    cols = max(1, int(math.ceil(width / step)))
    rows = max(1, int(math.ceil(height / step)))
    blocked = bytearray(cols * rows)
    # Стена отмечает все клетки, через которые проходит: шагаем по отрезку
    # вдвое чаще клетки, иначе тонкая диагональ проскочит между выборками.
    probe = step / 2.0
    for (ax, ay), (bx, by) in spans:
        length = math.hypot(bx - ax, by - ay)
        steps = max(1, int(math.ceil(length / max(probe, 1e-6))))
        for index in range(steps + 1):
            share = index / float(steps)
            px = ax + (bx - ax) * share - origin_x
            py = ay + (by - ay) * share - origin_y
            col = int(px // step)
            row = int(py // step)
            if 0 <= col < cols and 0 <= row < rows:
                blocked[row * cols + col] = 1

    seen = bytearray(cols * rows)
    stack = []
    for col in range(cols):
        for row in (0, rows - 1):
            index = row * cols + col
            if not blocked[index] and not seen[index]:
                seen[index] = 1
                stack.append(index)
    for row in range(rows):
        for col in (0, cols - 1):
            index = row * cols + col
            if not blocked[index] and not seen[index]:
                seen[index] = 1
                stack.append(index)
    while stack:
        index = stack.pop()
        row, col = divmod(index, cols)
        for next_col, next_row in ((col - 1, row), (col + 1, row),
                                   (col, row - 1), (col, row + 1)):
            if not (0 <= next_col < cols and 0 <= next_row < rows):
                continue
            other = next_row * cols + next_col
            if blocked[other] or seen[other]:
                continue
            seen[other] = 1
            stack.append(other)

    # Номера комнат: у каждой связной кучки свободных клеток — свой.
    labels = [0] * (cols * rows)
    room_count = 0

    def label_cell(index):
        """Заливка одной комнаты от клетки index; возвращает её номер."""
        number = room_count
        labels[index] = number
        pending = [index]
        while pending:
            current = pending.pop()
            row, col = divmod(current, cols)
            for next_col, next_row in ((col - 1, row), (col + 1, row),
                                       (col, row - 1), (col, row + 1)):
                if not (0 <= next_col < cols and 0 <= next_row < rows):
                    continue
                other = next_row * cols + next_col
                if blocked[other] or seen[other] or labels[other]:
                    continue
                labels[other] = number
                pending.append(other)
        return number

    for index in range(cols * rows):
        if blocked[index] or seen[index] or labels[index]:
            continue
        room_count += 1
        label_cell(index)

    # Клетки стен расширяем на одну к комнате: иначе у самой стены осталась
    # бы полоска «мира», и чёрная комната светилась бы рамкой по периметру.
    # Метка при расширении сохраняется: соседние комнаты не сливаются, даже
    # если их полосы касаются на стене.
    grown = list(labels)
    for index in range(cols * rows):
        label = labels[index]
        if not label:
            continue
        row, col = divmod(index, cols)
        for next_col, next_row in ((col - 1, row), (col + 1, row),
                                   (col, row - 1), (col, row + 1)):
            if not (0 <= next_col < cols and 0 <= next_row < rows):
                continue
            other = next_row * cols + next_col
            if not grown[other]:
                grown[other] = label

    # Строки сливаются в полосы: путей из сотен прямоугольников меньше,
    # и кисти Qt рисуют одну фигуру вместо сотни. Полосы группируются по
    # номеру комнаты, порядок — от первой найденной.
    groups = [[] for _ in range(room_count)]
    for row in range(rows):
        col = 0
        while col < cols:
            label = grown[row * cols + col]
            if not label:
                col += 1
                continue
            start = col
            while col < cols and grown[row * cols + col] == label:
                col += 1
            groups[label - 1].append((origin_x + start * step,
                                      origin_y + row * step,
                                      (col - start) * step, step))
    return [group for group in groups if group]


# ---------------------------------------------------------------------------
# Комнаты полигонами
# ---------------------------------------------------------------------------
#: Допуск склейки вершин: пересечения считаются в float, и один и тот же
#: угол, посчитанный с двух отрезков, может разойтись в последнем знаке.
ROOM_NODE_TOLERANCE = 0.001
#: Комната меньше этого (в мировых px²) — щель между стенами, а не
#: помещение: такие грани отбрасываем.
ROOM_MIN_AREA = 16.0
#: Больше столько кусков стен сразу не разбираем обходом граней: это уже
#: не комната, а забор, и пара сотен тысяч пар пересечений стоили бы кадра.
ROOM_MAX_SPANS = 600


def _room_node(point):
    """Вершина графа: координата, округлённая до допуска."""
    return (round(float(point[0]) / ROOM_NODE_TOLERANCE)
            * ROOM_NODE_TOLERANCE,
            round(float(point[1]) / ROOM_NODE_TOLERANCE)
            * ROOM_NODE_TOLERANCE)


def _segment_split_params(segments) -> list:
    """Доли 0…1, где каждый отрезок надо разрезать (пересечения и T-стыки).

    Без разрезания цикл не замыкается: стена, которая упирается в середину
    другой, оставляет узел «висящим», и обход граней не находит комнату.
    """
    cuts = [{0.0, 1.0} for _ in segments]
    for index in range(len(segments)):
        (ax, ay), (bx, by) = segments[index]
        dx1, dy1 = bx - ax, by - ay
        for other in range(index + 1, len(segments)):
            (cx, cy), (dx, dy) = segments[other]
            dx2, dy2 = dx - cx, dy - cy
            denominator = dx1 * dy2 - dy1 * dx2
            if abs(denominator) <= 1e-12:
                continue
            t = ((cx - ax) * dy2 - (cy - ay) * dx2) / denominator
            u = ((cx - ax) * dy1 - (cy - ay) * dx1) / denominator
            if -1e-9 <= t <= 1 + 1e-9 and -1e-9 <= u <= 1 + 1e-9:
                cuts[index].add(min(1.0, max(0.0, t)))
                cuts[other].add(min(1.0, max(0.0, u)))
    return cuts


def _room_edges(segments) -> dict:
    """Граф комнат: узел → соседи, каждый кусок стены разрезан в узлах."""
    cuts = _segment_split_params(segments)
    face_edges = {}
    for ((ax, ay), (bx, by)), marks in zip(segments, cuts):
        params = sorted(marks)
        for start, end in zip(params, params[1:]):
            if end - start <= 1e-9:
                continue
            first = _room_node((ax + (bx - ax) * start,
                                ay + (by - ay) * start))
            second = _room_node((ax + (bx - ax) * end,
                                 ay + (by - ay) * end))
            if first == second:
                continue
            face_edges.setdefault(first, set()).add(second)
            face_edges.setdefault(second, set()).add(first)
    return face_edges


def _planar_faces(segments) -> list:
    """Грани планарного графа: комнаты и одна внешняя грань на компоненту.

    Классический обход: из каждого узла рёбра упорядочены по углу, и от
    пришедшего ребра берётся следующее по часовой стрелке. Каждая сторона
    ребра принадлежит ровно одной грани, поэтому обход ничего не теряет.
    """
    graph = _room_edges(segments)
    if not graph:
        return []
    rings = {}
    for node, neighbours in graph.items():
        rings[node] = sorted(neighbours,
                             key=lambda nb: math.atan2(nb[1] - node[1],
                                                       nb[0] - node[0]))
    seen = set()
    faces = []
    for start, neighbours in rings.items():
        for first_next in neighbours:
            if (start, first_next) in seen:
                continue
            face = []
            node, step = start, first_next
            while True:
                seen.add((node, step))
                face.append(node)
                ring = rings[step]
                try:
                    index = ring.index(node)
                except ValueError:
                    break
                node, step = step, ring[index - 1]
                if (node, step) == (start, first_next):
                    break
                if len(face) > 100000:      # защита от вырожденной геометрии
                    break
            if len(face) >= 3:
                faces.append(face)
    return faces


def _signed_area(polygon) -> float:
    """Площадь со знаком: у комнат она положительная, у внешней грани — нет."""
    total = 0.0
    count = len(polygon)
    for index in range(count):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % count]
        total += x1 * y2 - x2 * y1
    return total / 2.0


#: Сколько подписей разведанных комнат хранит карта. Комната-полигон — это
#: шесть чисел, так что даже сотня комнат в сохранении почти не весит;
#: предел нужен только чтобы подправленная стократ геометрия не росла вечно.
EXPLORED_MAX = 2000

#: Виды подписей: полигон (рамка и площадь) и полоса запасного расчёта
#: (прямоугольник). Формат совпадает с `GameCanvas.room_signature` и
#: `GameCanvas.region_signature`.
EXPLORED_KINDS = {"poly": 5, "rect": 4}


def normalize_explored_rooms(value) -> list:
    """Разведанные комнаты карты: подписи правильной формы, без повторов.

    Подпись комнаты — её рамка и площадь, а не id: комнаты считаются из
    стен, и постоянного id у них нет. Формат: ``["poly", x1, y1, x2, y2,
    area]`` или ``["rect", x, y, w, h]`` — те же числа, что в клиенте.
    Чужие записи (старый формат, обрезанный файл) молча отбрасываются:
    туман важнее, чем чужой мусор в мире.
    """
    rooms = []
    seen = set()
    for entry in value or []:
        if isinstance(entry, dict):
            kind = entry.get("kind")
            numbers = entry.get("box") or entry.get("values") or []
        elif isinstance(entry, (list, tuple)) and entry:
            kind = entry[0]
            numbers = entry[1:]
        else:
            continue
        if not isinstance(kind, str) or kind not in EXPLORED_KINDS:
            continue
        if not isinstance(numbers, (list, tuple)):
            continue
        if len(numbers) != EXPLORED_KINDS[kind]:
            continue
        try:
            values = [_number(number) for number in numbers]
        except TypeError:
            continue
        if any(number is None for number in values):
            continue
        signature = (kind,) + tuple(round(number, 3) for number in values)
        if signature in seen:
            continue
        seen.add(signature)
        rooms.append([kind] + list(signature[1:]))
        if len(rooms) >= EXPLORED_MAX:
            break
    return rooms


def room_polygons(map_data) -> list:
    """Замкнутые комнаты полигонами в мире — для заливки тумана.

    Зачем не прямоугольники: туман комнат рисовался полосами грубой сетки
    (шаг 40 px), и комната выходила ступеньками, а у стены полоса выступала
    наружу на целую клетку. Здесь комната — её настоящий контур: грани
    планарного графа стен, поэтому заливка идёт ровно по линии стены.

    Возвращает список полигонов [(x, y), …]; открытая дверь стен не
    оставляет — комнаты за ней сливаются в один полигон, как и в
    ``enclosed_regions``.
    """
    spans = wall_spans(map_data, light=False)
    if not spans or len(spans) > ROOM_MAX_SPANS:
        return []
    segments = [((float(a[0]), float(a[1])), (float(b[0]), float(b[1])))
                for a, b in spans]
    rooms = []
    for face in _planar_faces(segments):
        if _signed_area(face) >= ROOM_MIN_AREA:
            rooms.append(face)
    return rooms
