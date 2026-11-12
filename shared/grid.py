"""Сетка карты как в Dungeondraft.

Логика простая и проверенная:
  * сетка задаётся ЧИСЛОМ клеток по ширине и высоте картинки (cols, rows);
  * размер клетки = размер картинки / число клеток;
  * сетка всегда идёт от края до края: первая клетка вписана в левый верхний
    угол, последняя — в правый нижний; никаких смещений нет;
  * линии сетки — пунктир шириной 2 px, крайние линии лежат ровно на краю
    картинки и не обрезаются.

Если известен размер клетки в пикселях, количество клеток считается из размера
картинки (round), после чего размер клетки пересчитывается обратно — так края
всегда совпадают с картинкой.
"""
import base64
import json
import math
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from shared import walls as wall_rules

# --- внешний вид линий (как в Dungeondraft) ---------------------------------
GRID_PEN_WIDTH = 2.0       # толщина линии в пикселях экрана
GRID_DASH = (2.0, 2.0)     # штрих 2 px, пробел 2 px — пунктир

MAX_CELLS = 2000

_EXT_BY_MAGIC = (
    (b'\x89PNG\r\n\x1a\n', '.png'),
    (b'\xff\xd8\xff', '.jpg'),
    (b'GIF87a', '.gif'),
    (b'GIF89a', '.gif'),
    (b'RIFF', '.webp'),
    (b'BM', '.bmp'),
)

DD2VTT_SUFFIXES = ('.dd2vtt', '.uvtt', '.df2vtt')


# --- числа ---------------------------------------------------------------------
def clamp_cells(value) -> int:
    """Число клеток — целое в разумных пределах. 0 значит «не задано»."""
    try:
        count = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    if count <= 0:
        return 0
    return min(count, MAX_CELLS)


def cells_from_cell_size(image_px: float, cell_px: float) -> int:
    """Сколько клеток помещается при заданном размере клетки (округление)."""
    if image_px <= 0 or cell_px <= 0:
        return 0
    return clamp_cells(image_px / cell_px)


def cell_size_from_cells(image_px: float, cells: int) -> float:
    """Размер клетки в пикселях картинки: ровно image_px / cells."""
    if cells <= 0:
        return 0.0
    return float(image_px) / float(cells)


def _finite(value, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


# --- модель ---------------------------------------------------------------------
@dataclass
class GridConfig:
    """Число клеток по сторонам картинки. Всё остальное вычисляется."""

    cols: int = 0
    rows: int = 0
    visible: bool = True

    @property
    def valid(self) -> bool:
        return self.cols > 0 and self.rows > 0

    def normalized(self) -> 'GridConfig':
        return GridConfig(clamp_cells(self.cols), clamp_cells(self.rows),
                          bool(self.visible))

    # --- сериализация -----------------------------------------------------
    def to_dict(self) -> dict:
        return {'grid_cols': self.cols, 'grid_rows': self.rows,
                'grid_visible': self.visible}

    @staticmethod
    def from_dict(data: dict, img_width: float = 0.0,
                  img_height: float = 0.0) -> 'GridConfig':
        """Новый формат — число клеток. Старый (шаг в пикселях) пересчитывается."""
        cols = clamp_cells(data.get('grid_cols', 0))
        rows = clamp_cells(data.get('grid_rows', 0))
        if not cols or not rows:
            legacy = _finite(data.get('grid_size'), 0.0)
            if legacy > 0 and img_width > 0 and img_height > 0:
                cols = cols or cells_from_cell_size(img_width, legacy)
                rows = rows or cells_from_cell_size(img_height, legacy)
        return GridConfig(cols, rows, bool(data.get('grid_visible', True)))

    # --- геометрия ---------------------------------------------------------
    def cell_sizes(self, img_width: float, img_height: float) -> Tuple[float, float]:
        """Размер клетки в пикселях картинки по каждой оси."""
        return (cell_size_from_cells(img_width, self.cols),
                cell_size_from_cells(img_height, self.rows))

    def cell_on_screen(self, map_width: float, map_height: float) -> Tuple[float, float]:
        """Размер клетки в мировых единицах с учётом растяжения карты."""
        return (cell_size_from_cells(map_width, self.cols),
                cell_size_from_cells(map_height, self.rows))

    def line_ratios(self) -> Tuple[List[float], List[float]]:
        """Линии как доли от 0 до 1. Первая — на краю, последняя — на противоположном."""
        xs = [i / self.cols for i in range(self.cols + 1)] if self.cols else []
        ys = [i / self.rows for i in range(self.rows + 1)] if self.rows else []
        return xs, ys

    def snap(self, world_x: float, world_y: float, pos_x: float, pos_y: float,
             map_width: float, map_height: float,
             clamp: bool = True) -> Tuple[float, float]:
        """Центр ближайшей клетки. Отсчёт от угла карты: первая клетка в углу.

        ``clamp=False`` — не прижимать клетку к полотну. Так ставится токен,
        перенесённый «на эту карту»: точка берётся по центру экрана мастера,
        даже если он смотрит за край карты, и не съезжает к её краю.
        """
        cell_x, cell_y = self.cell_on_screen(map_width, map_height)
        if cell_x <= 0 or cell_y <= 0:
            return world_x, world_y
        col = math.floor((world_x - pos_x) / cell_x)
        row = math.floor((world_y - pos_y) / cell_y)
        if clamp:
            col = max(0, min(self.cols - 1, col))
            row = max(0, min(self.rows - 1, row))
        return (pos_x + (col + 0.5) * cell_x,
                pos_y + (row + 0.5) * cell_y)



    def snap_point(self, world_x: float, world_y: float,
                   pos_x: float, pos_y: float,
                   map_width: float, map_height: float) -> Tuple[float, float]:
        """Ближайшая опорная точка клетки для линейки.

        Кандидаты — центр клетки и четыре середины её сторон. Проверяем не
        только клетку под курсором, а 3x3 вокруг: у самой границы иначе
        привязка тянула бы в центр соседней клетки, а не к ближней середине.
        """
        cell_x, cell_y = self.cell_on_screen(map_width, map_height)
        if cell_x <= 0 or cell_y <= 0:
            return world_x, world_y
        col = math.floor((world_x - pos_x) / cell_x)
        row = math.floor((world_y - pos_y) / cell_y)

        best = (world_x, world_y)
        best_d = None
        half_x, half_y = cell_x / 2.0, cell_y / 2.0
        for dc in (-1, 0, 1):
            for dr in (-1, 0, 1):
                left = pos_x + (col + dc) * cell_x
                top = pos_y + (row + dr) * cell_y
                for cx, cy in ((left + half_x, top + half_y),   # центр
                               (left + half_x, top),            # верх
                               (left + half_x, top + cell_y),   # низ
                               (left, top + half_y),            # лево
                               (left + cell_x, top + half_y)):  # право
                    d = (cx - world_x) ** 2 + (cy - world_y) ** 2
                    if best_d is None or d < best_d:
                        best_d, best = d, (cx, cy)
        return best

# --- Universal VTT ------------------------------------------------------------
def parse_dd2vtt(raw: bytes) -> Tuple[GridConfig, Optional[bytes], List, List, dict]:
    """Разобрать .dd2vtt.

    Сетка берётся из map_size (число клеток). Если его нет или он расходится с
    картинкой больше чем на 5%, число клеток вычисляется из pixels_per_grid и
    фактического размера картинки. map_origin не используется: сетка всегда
    идёт от края.
    """
    data = json.loads(raw.decode('utf-8'))
    if not isinstance(data, dict):
        raise ValueError('Некорректный файл .dd2vtt')

    res = data.get('resolution') or {}
    try:
        ppg = float(res.get('pixels_per_grid', 0) or 0)
    except (TypeError, ValueError):
        ppg = 0.0
    if not math.isfinite(ppg) or ppg <= 0:
        ppg = 0.0

    size = res.get('map_size') or {}
    declared_cols = clamp_cells(size.get('x', 0))
    declared_rows = clamp_cells(size.get('y', 0))

    image_bytes = None
    blob = data.get('image')
    if isinstance(blob, str) and blob:
        try:
            image_bytes = base64.b64decode(blob, validate=False)
        except Exception:
            image_bytes = None
    if image_bytes is not None and not image_bytes:
        image_bytes = None

    img_w, img_h = _image_size(image_bytes)

    cols, rows = declared_cols, declared_rows
    if ppg > 0 and img_w > 0 and img_h > 0:
        by_image_x = cells_from_cell_size(img_w, ppg)
        by_image_y = cells_from_cell_size(img_h, ppg)
        if not cols or abs(img_w / cols - ppg) > ppg * 0.05:
            cols = by_image_x
        if not rows or abs(img_h / rows - ppg) > ppg * 0.05:
            rows = by_image_y

    grid = GridConfig(cols, rows, True)

    origin = res.get('map_origin') or {}
    origin_x = _finite(origin.get('x'), 0.0)
    origin_y = _finite(origin.get('y'), 0.0)
    if abs(origin_x) > 1.0 or abs(origin_y) > 1.0:
        data.setdefault('_warnings', []).append(
            f'в файле указано смещение сетки ({origin_x:g}; {origin_y:g}) px, '
            'оно проигнорировано: сетка строится от края картинки')

    walls = data.get('line_of_sight') or []
    portals = data.get('portals') or []
    if not isinstance(walls, list):
        walls = []
    if not isinstance(portals, list):
        portals = []
    return grid, image_bytes, walls, portals, data


def _wall_end_near(walls, point, tolerance):
    """Ближайший конец стены к точке: ``(расстояние, стена, 0|1)`` или None.

    0 — начало ломаной, 1 — её конец. Нужен, чтобы продлить стену через
    разрыв line_of_sight: Dungeondraft рвёт стену ровно под створку.
    """
    best = None
    for wall in walls:
        points = wall["points"]
        if len(points) < 2:
            continue
        for which, index in ((0, 0), (1, len(points) - 1)):
            distance = math.dist(points[index], point)
            if distance > tolerance:
                continue
            if best is None or distance < best[0]:
                best = (distance, wall, which)
    return best


def _close_gap_over_portal(walls, first, second, tolerance):
    """Соединить разрыв стены через створку: продлить стену до её конца.

    Так дверь становится вырезом в настоящей стене, а не отдельной
    «створкой-стеной» между двумя обрезками. Разница видна при удалении
    двери: проём уходит, а стена остаётся целой — дыры в периметре нет.

    Возвращает ``(wall_id, seg_index)`` — отрезок, на котором лежит проём,
    или None, если рядом с концами створки нет концов стен.
    """
    found = _wall_end_near(walls, first, tolerance)
    if found is None:
        return None
    _distance, wall, which = found
    if len(wall["points"]) >= wall_rules.MAX_POINTS:
        return None
    # Конец стены совпал с `first`, значит продолжить её надо до `second`.
    if which == 0:
        if math.dist(wall["points"][0], second) > 1e-6:
            wall["points"].insert(0, list(second))
        return wall["id"], 0
    if math.dist(wall["points"][-1], second) > 1e-6:
        wall["points"].append(list(second))
    return wall["id"], len(wall["points"]) - 2


def dd2vtt_geometry(walls_raw, portals_raw, *, cell_width: float,
                    cell_height: float, pos_x: float = 0.0,
                    pos_y: float = 0.0) -> Tuple[list, list]:
    """Стены и проёмы .dd2vtt в мировых координатах стола.

    В UVTT **все** координаты заданы в клетках («squares»), а не в пикселях:
    и ``line_of_sight``, и ``portals``. Пиксели появляются только при
    умножении на ``resolution.pixels_per_grid``. Стол же живёт в мировых
    единицах, где клетка — это ``размер карты / число клеток``. Поэтому
    пересчёт такой: ``мир = угол карты + клетка × размер клетки``.

    ``line_of_sight`` — список ломаных: каждая ломаная становится одной
    стеной. ``portals`` — готовый проём: ``bounds`` это два конца створки,
    лежащие на стене. Ссылки на стену в файле нет, поэтому проём
    привязывается по геометрии — к тому отрезку стены, на котором лежат оба
    конца створки.

    Dungeondraft рвёт ``line_of_sight`` под каждой створкой, поэтому чаще
    всего оба конца лежат не на стене, а в её разрыве. Такой разрыв импорт
    закрывает: стена дотягивается до второго конца створки, и дверь
    становится вырезом в настоящей стене — иначе удалённая дверь оставляла
    бы в периметре дыру. Портал вне всякой геометрии
    (``freestanding``) получает свою стену по створке.

    Окно или дверь: явный ``portalType`` (``1``/``window``/``0``/``door``),
    а без него — ``closed``. Этот флаг Dungeondraft пишет из настройки
    «Block light / Allow light»: закрытая створка — дверь, открытая —
    проём, который пропускает свет, то есть окно.

    Возвращает ``(walls, openings)`` в том же виде, что и ``shared/walls.py``;
    пустой список клеток или нулевой размер сетки — пустая геометрия.
    """
    cell_w = _finite(cell_width, 0.0) or 0.0
    cell_h = _finite(cell_height, 0.0) or 0.0
    if cell_w <= 0 or cell_h <= 0:
        return [], []
    origin_x = _finite(pos_x, 0.0) or 0.0
    origin_y = _finite(pos_y, 0.0) or 0.0

    def world(raw):
        """Клетки из файла -> мировые координаты; None — если точка мусорная."""
        if isinstance(raw, dict):
            raw = (raw.get("x"), raw.get("y"))
        try:
            cell_x, cell_y = raw
        except (TypeError, ValueError):
            return None
        cell_x = _finite(cell_x, None)
        cell_y = _finite(cell_y, None)
        if cell_x is None or cell_y is None:
            return None
        return [origin_x + cell_x * cell_w, origin_y + cell_y * cell_h]

    raw_walls = []
    for index, polyline in enumerate(walls_raw or []):
        if not isinstance(polyline, (list, tuple)):
            continue
        points = [world(item) for item in polyline]
        raw_walls.append({
            "id": f"dd2vtt-wall-{index}",
            "points": [point for point in points if point is not None],
            "closed": False,
        })
    walls = wall_rules.normalize_walls(raw_walls)
    if not walls:
        return [], []

    # Допуск привязки створки к стене: 20% клетки. В экспорте Dungeondraft
    # створка лежит ровно на линии, но другие инструменты дают мелкий зазор,
    # и жёсткое «ноль» теряло бы все двери.
    tolerance = 0.2 * min(cell_w, cell_h)
    openings = []
    for index, portal in enumerate(portals_raw or []):
        if not isinstance(portal, dict):
            continue
        ends = portal.get("bounds")
        first = second = None
        if isinstance(ends, (list, tuple)) and len(ends) >= 2:
            first, second = world(ends[0]), world(ends[1])
        if first is None or second is None:
            # Без bounds остаётся position: считаем створку шириной в клетку.
            center = world(portal.get("position"))
            if center is None:
                continue
            first = [center[0] - cell_w / 2.0, center[1]]
            second = [center[0] + cell_w / 2.0, center[1]]
        best = None
        for wall in walls:
            for seg_index, segment in enumerate(wall_rules.wall_segments(wall)):
                at_a, miss_a = wall_rules.point_on_segment(segment, *first)
                at_b, miss_b = wall_rules.point_on_segment(segment, *second)
                miss = max(miss_a, miss_b)
                if miss > tolerance:
                    continue
                if best is None or miss < best[0]:
                    best = (miss, wall["id"], seg_index,
                            min(at_a, at_b), max(at_a, at_b))
        if best is None:
            # Dungeondraft оставляет в line_of_sight разрыв ровно под створку:
            # концы соседних стен упираются в края двери, и привязать её
            # некуда. Продлеваем стену через проём — дверь становится вырезом
            # в стене, а не отдельной вставкой между двумя обрезками. Иначе
            # удаление двери оставляло в периметре комнаты дыру.
            joined = _close_gap_over_portal(walls, first, second, tolerance)
            if joined is not None:
                wall_id, seg_index = joined
                wall = next(item for item in walls
                            if item["id"] == wall_id)
                segment = wall_rules.wall_segments(wall)[seg_index]
                at_a, _miss_a = wall_rules.point_on_segment(segment, *first)
                at_b, _miss_b = wall_rules.point_on_segment(segment, *second)
                best = (0.0, wall_id, seg_index,
                        min(at_a, at_b), max(at_a, at_b))
        if best is None:
            # Совсем без стены рядом (портал «на воле»): своя маленькая
            # стена по створке — закрытая дверь заслоняет свет и ход,
            # открытая пропускает всё.
            bridge_id = f"dd2vtt-door-wall-{index}"
            bridge = wall_rules.normalize_wall({
                "id": bridge_id,
                "points": [first, second],
                "closed": False,
            })
            if bridge is None:
                continue
            walls.append(bridge)
            length = wall_rules.segment_length(
                wall_rules.wall_segments(bridge)[0])
            best = (0.0, bridge_id, 0, 0.0, length)
        _miss, wall_id, seg_index, start, end = best
        kind = wall_rules.DOOR
        portal_type = portal.get("portalType")
        if isinstance(portal_type, str):
            if portal_type.strip().lower() in ("window", "окно"):
                kind = wall_rules.WINDOW
        elif portal_type == 1:
            # 0 — дверь, 1 — окно: так пишут экспортёры, у которых тип есть.
            kind = wall_rules.WINDOW
        elif portal.get("closed") is False:
            # Отдельного флага «block light» в UVTT нет: Dungeondraft пишет
            # его в `closed`. Дверь с «Block light» экспортируется закрытой,
            # а проём с «Allow light» — открытым, то есть `closed: false`.
            # Так же читают файл Roll20 и uvtt2fgu; без этого правила все окна
            # приезжали дверями и не пропускали свет.
            kind = wall_rules.WINDOW
        openings.append({
            "id": f"dd2vtt-portal-{index}",
            "wall_id": wall_id,
            "seg": seg_index,
            "at": (start + end) / 2.0,
            "width": max(end - start, cell_w * 0.1),
            "kind": kind,
            # У двери `closed` — ещё и состояние створки: дверь, оставленная
            # в Dungeondraft открытой, приходит открытой.
            "open": kind == wall_rules.DOOR and portal.get("closed") is False,
            "locked": False,
        })
    return walls, wall_rules.normalize_openings(openings, walls)


def _image_size(image_bytes: Optional[bytes]) -> Tuple[float, float]:
    """Размер картинки в пикселях. Читаем заголовок, без полной распаковки."""
    if not image_bytes:
        return 0.0, 0.0
    data = image_bytes
    try:
        if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 24:
            return (float(int.from_bytes(data[16:20], 'big')),
                    float(int.from_bytes(data[20:24], 'big')))
        if data.startswith(b'GIF') and len(data) >= 10:
            return (float(int.from_bytes(data[6:8], 'little')),
                    float(int.from_bytes(data[8:10], 'little')))
        if data.startswith(b'BM') and len(data) >= 26:
            return (float(int.from_bytes(data[18:22], 'little')),
                    float(abs(int.from_bytes(data[22:26], 'little'))))
        if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
            if data[12:16] == b'VP8X' and len(data) >= 30:
                return (float(int.from_bytes(data[24:27], 'little') + 1),
                        float(int.from_bytes(data[27:30], 'little') + 1))
    except Exception:
        pass
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(data)) as image:
            return float(image.width), float(image.height)
    except Exception:
        return 0.0, 0.0


def image_extension(image_bytes: bytes) -> str:
    for magic, ext in _EXT_BY_MAGIC:
        if image_bytes.startswith(magic):
            if ext == '.webp' and b'WEBP' not in image_bytes[:16]:
                continue
            return ext
    return '.png'


def save_embedded_image(image_bytes: bytes, folder: str, stem: str) -> str:
    """Сохранить изображение из .dd2vtt.

    Имя файла чистим только от символов, недопустимых в имени файла:
    разделителей пути, управляющих символов и служебных знаков Windows.
    Кириллица и пробелы допустимы. Раньше regex [^A-Za-z0-9._ -] заменял
    на подчёркивания всё, кроме латиницы, и карта с русским именем
    превращалась в «______».
    """
    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', stem or '').strip(' .')
    if not safe:
        safe = 'map'
    target = os.path.join(folder, safe + image_extension(image_bytes))
    with open(target, 'wb') as f:
        f.write(image_bytes)
    return target


def build_dd2vtt(grid: GridConfig, image_bytes: bytes,
                 map_size_cells: Tuple[int, int] = None,
                 walls: Optional[list] = None,
                 portals: Optional[list] = None,
                 img_size: Tuple[float, float] = None) -> bytes:
    """Собрать .dd2vtt. pixels_per_grid считается из размера картинки и клеток."""
    if map_size_cells is None:
        map_size_cells = (grid.cols, grid.rows)
    iw, ih = img_size if img_size else _image_size(image_bytes)
    ppg_x = cell_size_from_cells(iw, grid.cols) if iw else 0.0
    ppg_y = cell_size_from_cells(ih, grid.rows) if ih else 0.0
    ppg = ppg_x or ppg_y
    doc = {
        'format': 0.3,
        'resolution': {
            'map_origin': {'x': 0.0, 'y': 0.0},
            'map_size': {'x': float(map_size_cells[0]), 'y': float(map_size_cells[1])},
            'pixels_per_grid': ppg,
        },
        'line_of_sight': walls or [],
        'portals': portals or [],
        'environment': {'baked_lighting': False},
        'image': base64.b64encode(image_bytes).decode('ascii'),
    }
    return json.dumps(doc).encode('utf-8')


def is_dd2vtt_path(path: str) -> bool:
    return os.path.splitext(path or '')[1].lower() in DD2VTT_SUFFIXES


def detect_grid(image_path: str, search_min: int = 20,
                search_max: int = 400) -> Optional[GridConfig]:
    """Оценить число клеток по картинке через автокорреляцию.

    Работает, если сетка нарисована на самой картинке. Возвращает число клеток,
    округлённое к целому — дробных клеток не бывает.
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return None

    img = Image.open(image_path).convert('L')
    full_w, full_h = img.size
    scale_back = 1.0
    max_side = 1600
    if max(img.size) > max_side:
        scale_back = max(img.size) / float(max_side)
        img = img.resize((max(1, int(img.width / scale_back)),
                          max(1, int(img.height / scale_back))))
    arr = np.asarray(img, dtype=float)

    def estimate(profile):
        sig = profile - profile.mean()
        ac = np.correlate(sig, sig, mode='full')[len(sig) - 1:]
        if ac[0] <= 0:
            return None
        ac = ac / ac[0]
        hi = min(search_max, len(ac) - 1)
        if hi <= search_min:
            return None
        window = ac[search_min:hi]
        peak = window.max()
        if peak <= 0:
            return None
        idx = np.where(window >= 0.5 * peak)[0]
        if len(idx) == 0:
            return None
        best = search_min + int(idx[0])
        for j in range(max(best - 3, search_min + 1), min(best + 4, hi - 1)):
            if ac[j] >= ac[j - 1] and ac[j] >= ac[j + 1]:
                best = j
                break
        return float(best)

    step_x = estimate(arr.sum(axis=0))
    step_y = estimate(arr.sum(axis=1))
    if not step_x or not step_y:
        return None
    # Шаг в пикселях на исходной картинке -> число клеток.
    cols = cells_from_cell_size(full_w, step_x * scale_back)
    rows = cells_from_cell_size(full_h, step_y * scale_back)
    if not cols or not rows:
        return None
    return GridConfig(cols, rows, True)
