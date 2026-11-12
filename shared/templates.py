"""Шаблоны области действия (AoE) в клетках сетки: круг, конус, линия, квадрат.

Как в D&D 5e: расстояния в футах, 1 клетка = 5 фут. Клетка попадает в
шаблон, если её центр внутри фигуры. Конус 5e — 90°, ширина на конце равна
длине (клетки по центру).

Модуль чистый: только геометрия по индексам клеток, без Qt.
"""
import math

FEET_PER_CELL = 5


def _cell_center(col: int, row: int):
    """Центр клетки в координатах клеток: (col + 0.5, row + 0.5)."""
    return (col + 0.5, row + 0.5)


def _radius_cells(feet: float) -> float:
    if feet is None or feet <= 0:
        raise ValueError("размер шаблона должен быть больше нуля")
    return float(feet) / FEET_PER_CELL


def circle(origin_col: int, origin_row: int, radius_ft: float,
           cols: int = None, rows: int = None):
    """Круг (сфера/взрыв) радиусом radius_ft от клетки origin.

    Возвращает список (col, row) — клетки, чей центр в радиусе.
    """
    r = _radius_cells(radius_ft)
    ox, oy = _cell_center(origin_col, origin_row)
    span = int(math.ceil(r)) + 1
    out = []
    for dc in range(-span, span + 1):
        for dr in range(-span, span + 1):
            c, rw = origin_col + dc, origin_row + dr
            if cols is not None and not (0 <= c < cols):
                continue
            if rows is not None and not (0 <= rw < rows):
                continue
            cx, cy = _cell_center(c, rw)
            if math.hypot(cx - ox, cy - oy) <= r + 1e-9:
                out.append((c, rw))
    return out


def line(origin_col: int, origin_row: int, dir_col: float, dir_row: float,
         length_ft: float, width_ft: float = 5, cols: int = None, rows: int = None):
    """Линия (луч) от клетки origin в направлении (dir_col, dir_row) длиной length_ft.

    width_ft — ширина (по умолчанию 5 фут = одна клетка).
    Клетка входит, если её центр лежит в прямоугольнике лучa.
    """
    length = _radius_cells(length_ft)
    half = max(0.5, _radius_cells(width_ft) / 2)
    norm = math.hypot(dir_col, dir_row)
    if norm == 0:
        raise ValueError("направление линии не задано")
    ux, uy = dir_col / norm, dir_row / norm
    px, py = -uy, ux                       # перпендикуляр
    ox, oy = _cell_center(origin_col, origin_row)
    reach = int(math.ceil(length)) + 1
    out = []
    for dc in range(-reach, reach + 1):
        for dr in range(-reach, reach + 1):
            c, rw = origin_col + dc, origin_row + dr
            if cols is not None and not (0 <= c < cols):
                continue
            if rows is not None and not (0 <= rw < rows):
                continue
            cx, cy = _cell_center(c, rw)
            vx, vy = cx - ox, cy - oy
            along = vx * ux + vy * uy
            across = vx * px + vy * py
            if 0 < along <= length + 1e-9 and abs(across) <= half + 1e-9:
                out.append((c, rw))
    return out


def cone(origin_col: int, origin_row: int, dir_col: float, dir_row: float,
         length_ft: float, cols: int = None, rows: int = None):
    """Конус 90° длиной length_ft. Центр клетки в конусе, если угол до оси <= 45°."""
    length = _radius_cells(length_ft)
    norm = math.hypot(dir_col, dir_row)
    if norm == 0:
        raise ValueError("направление конуса не задано")
    ux, uy = dir_col / norm, dir_row / norm
    ox, oy = _cell_center(origin_col, origin_row)
    reach = int(math.ceil(length)) + 1
    out = []
    for dc in range(-reach, reach + 1):
        for dr in range(-reach, reach + 1):
            c, rw = origin_col + dc, origin_row + dr
            if cols is not None and not (0 <= c < cols):
                continue
            if rows is not None and not (0 <= rw < rows):
                continue
            cx, cy = _cell_center(c, rw)
            vx, vy = cx - ox, cy - oy
            dist = math.hypot(vx, vy)
            if dist == 0 or dist > length + 1e-9:
                continue
            cos_angle = (vx * ux + vy * uy) / dist
            if cos_angle >= math.cos(math.radians(45)) - 1e-9:
                out.append((c, rw))
    return out


def square(origin_col: int, origin_row: int, side_ft: float,
           cols: int = None, rows: int = None):
    """Квадрат side_ft × side_ft, начиная с клетки origin и вправо-вниз."""
    side = int(round(_radius_cells(side_ft) * 2)) // 2 or 1
    if side < 1:
        side = 1
    out = []
    for dc in range(side):
        for dr in range(side):
            c, rw = origin_col + dc, origin_row + dr
            if cols is not None and not (0 <= c < cols):
                continue
            if rows is not None and not (0 <= rw < rows):
                continue
            out.append((c, rw))
    return out


SHAPES = ("circle", "cone", "line", "square")


def shape_cells(kind: str, origin_col: int, origin_row: int, size_ft: float,
                dir_col: float = 1, dir_row: float = 0,
                cols: int = None, rows: int = None):
    """Единая точка входа для сервера и интерфейса."""
    if kind == "circle":
        return circle(origin_col, origin_row, size_ft, cols, rows)
    if kind == "cone":
        return cone(origin_col, origin_row, dir_col, dir_row, size_ft, cols, rows)
    if kind == "line":
        return line(origin_col, origin_row, dir_col, dir_row, size_ft, 5, cols, rows)
    if kind == "square":
        return square(origin_col, origin_row, size_ft, cols, rows)
    raise ValueError(f"неизвестная форма: {kind!r}")
