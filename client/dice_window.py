"""Виртуальные 3D-кубики для раздела «Чат».

Окно повторяет идею инструмента TtRP: слева лоток с физическими на вид
кубиками, справа набор (сколько каких кубиков), модификатор, формула,
кнопка броска, звук и история.

Кубики — выпуклые многогранники, которые рисуются программно через QPainter.
Бросок считается заранее: гравитация, вращение, отскоки от пола и бортиков,
столкновения между кубиками. Результат при этом считает общий ``shared.dice``,
а физика лишь укладывает кубик нужной гранью вверх — картинка и правила
всегда совпадают, и никаких внешних 3D-движков не нужно.

Координаты мира: X — вправо, Y — вглубь, Z — вверх. Камера смотрит сверху
и чуть спереди, поэтому верхняя грань кубика и есть его результат.
"""
import math
import random
import time
from array import array
from itertools import combinations

from PyQt5.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (QBrush, QColor, QFont, QFontMetrics,
                         QLinearGradient, QPainter, QPainterPath, QPen,
                         QPolygonF, QTransform)
from PyQt5.QtWidgets import QWidget


#: Цвета как в наборе TtRP: по ним кубик узнаётся быстрее, чем по числу граней.
DIE_COLORS = {
    4: "#d9534f",
    6: "#8d8d8d",
    8: "#3f8f4f",
    10: "#2f6fb5",
    12: "#7a4fa3",
    20: "#d9822b",
}
DIE_ORDER = (4, 6, 8, 10, 12, 20)
MAX_TRAY_DICE = 30          # больше на лотке не читается и тормозит анимацию
LIGHT = (0.35, -0.45, 0.82)

PHI = (1 + 5 ** 0.5) / 2


# ----------------------------------------------------------------------
# Векторы и кватернионы
# ----------------------------------------------------------------------
def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _length(a):
    return math.sqrt(_dot(a, a))


def _unit(a):
    size = _length(a)
    return (a[0] / size, a[1] / size, a[2] / size) if size else (0.0, 0.0, 0.0)


def _scale_vec(vector, factor):
    return (vector[0] * factor, vector[1] * factor,
            vector[2] * factor)


def _q_mul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def _q_axis(axis, angle):
    axis = _unit(axis)
    half = angle / 2.0
    sin_half = math.sin(half)
    return (math.cos(half), axis[0] * sin_half, axis[1] * sin_half,
            axis[2] * sin_half)


def _q_between(source, target):
    """Кватернион, поворачивающий вектор source в target."""
    source = _unit(source)
    target = _unit(target)
    cos_angle = max(-1.0, min(1.0, _dot(source, target)))
    if cos_angle > 1 - 1e-9:
        return (1.0, 0.0, 0.0, 0.0)
    if cos_angle < -1 + 1e-9:
        # Противоположные векторы: любая перпендикулярная ось даёт разворот.
        axis = _cross(source, (0.0, 0.0, 1.0))
        if _length(axis) < 1e-6:
            axis = _cross(source, (1.0, 0.0, 0.0))
        return _q_axis(axis, math.pi)
    axis = _cross(source, target)
    return _q_axis(axis, math.acos(cos_angle))


def _q_slerp(a, b, t):
    cos_half = a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]
    if cos_half < 0:
        b = tuple(-value for value in b)
        cos_half = -cos_half
    if cos_half > 0.9995:
        raw = tuple(a[i] + (b[i] - a[i]) * t for i in range(4))
        return _q_normalize(raw)
    theta = math.acos(max(-1.0, min(1.0, cos_half)))
    sin_theta = math.sin(theta)
    left = math.sin((1 - t) * theta) / sin_theta
    right = math.sin(t * theta) / sin_theta
    return tuple(a[i] * left + b[i] * right for i in range(4))


def _q_normalize(q):
    size = math.sqrt(sum(value * value for value in q))
    return tuple(value / size for value in q) if size else (1.0, 0.0, 0.0, 0.0)


def _q_random(rng):
    # Равномерное вращение: случайная ось и угол.
    axis = (rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-1, 1))
    if _length(axis) < 1e-6:
        axis = (0.0, 1.0, 0.0)
    return _q_axis(axis, rng.uniform(0, 2 * math.pi))


def _q_matrix(q):
    w, x, y, z = _q_normalize(q)
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )


def _rotate(matrix, vector):
    return tuple(
        matrix[row][0] * vector[0] + matrix[row][1] * vector[1]
        + matrix[row][2] * vector[2]
        for row in range(3)
    )


# ----------------------------------------------------------------------
# Многогранники
# ----------------------------------------------------------------------
class _Face:
    __slots__ = ("indices", "normal", "center", "value")

    def __init__(self, indices, normal, center):
        self.indices = list(indices)
        self.normal = normal
        self.center = center
        self.value = 0


class _Mesh:
    __slots__ = ("sides", "vertices", "faces", "vertex_values")

    def __init__(self, sides, vertices, faces, vertex_values=None):
        self.sides = sides
        self.vertices = vertices
        self.faces = faces
        self.vertex_values = vertex_values or []


def _signed_variants(base):
    variants = [(0.0, 0.0, 0.0)]
    for axis, value in enumerate(base):
        signs = (0.0,) if value == 0 else (value, -value)
        grown = []
        for variant in variants:
            for sign in signs:
                point = list(variant)
                point[axis] = sign
                grown.append(tuple(point))
        variants = grown
    return variants


def _polyhedron_vertices(sides):
    if sides == 4:
        raw = [(1, 1, 1), (1, -1, -1), (-1, 1, -1), (-1, -1, 1)]
    elif sides == 6:
        raw = _signed_variants((1, 1, 1))
    elif sides == 8:
        raw = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0),
               (0, 0, 1), (0, 0, -1)]
    elif sides == 10:
        # Пятиугольный трапецоэдр: кайты плоские только при таком апексе.
        ring = 0.105
        cos36 = math.cos(math.pi / 5)
        apex = ring * (1 + cos36) / (1 - cos36)
        raw = [(0, 0, apex), (0, 0, -apex)]
        for k in range(5):
            angle = 2 * math.pi * k / 5
            raw.append((math.cos(angle), math.sin(angle), ring))
        for k in range(5):
            angle = 2 * math.pi * k / 5 + math.pi / 5
            raw.append((math.cos(angle), math.sin(angle), -ring))
    elif sides == 12:
        raw = (_signed_variants((1, 1, 1))
               + _signed_variants((0, 1 / PHI, PHI))
               + _signed_variants((1 / PHI, PHI, 0))
               + _signed_variants((PHI, 0, 1 / PHI)))
    elif sides == 20:
        raw = (_signed_variants((0, 1, PHI))
               + _signed_variants((1, PHI, 0))
               + _signed_variants((PHI, 0, 1)))
    else:
        raise ValueError(f"Нет меша для d{sides}")

    radius = max(_length(point) for point in raw)
    return [tuple(component / radius for component in point) for point in raw]


def _order_around(indices, vertices, normal):
    center = tuple(
        sum(vertices[index][axis] for index in indices) / len(indices)
        for axis in range(3)
    )
    reference = _unit(_sub(vertices[indices[0]], center))
    side = _cross(normal, reference)
    return sorted(
        indices,
        key=lambda index: math.atan2(
            _dot(_sub(vertices[index], center), side),
            _dot(_sub(vertices[index], center), reference),
        ),
    )


def _convex_faces(vertices):
    """Грани выпуклой оболочки: плоскости, оставляющие все точки позади."""
    faces = {}
    for i, j, k in combinations(range(len(vertices)), 3):
        normal = _cross(_sub(vertices[j], vertices[i]),
                        _sub(vertices[k], vertices[i]))
        if _length(normal) < 1e-7:
            continue
        normal = _unit(normal)
        offset = _dot(normal, vertices[i])
        if offset < 0:
            normal = tuple(-component for component in normal)
            offset = -offset
        if any(_dot(normal, point) > offset + 1e-6 for point in vertices):
            continue
        members = tuple(index for index, point in enumerate(vertices)
                        if abs(_dot(normal, point) - offset) < 1e-6)
        if len(members) >= 3:
            faces[members] = normal
    return [(indices, normal) for indices, normal in faces.items()]


def _assign_values(faces, sides):
    """Подписи граней; у чётных кубиков противоположные грани дают sides+1."""
    if sides == 4:
        for index, face in enumerate(faces):
            face.value = index + 1
        return

    # Упорядочиваем грани, чтобы подписи не зависели от порядка обхода.
    faces.sort(key=lambda face: (-round(face.normal[2], 5),
                                 math.atan2(face.normal[1], face.normal[0])))
    free = set(range(len(faces)))
    value = 0
    for index, face in enumerate(faces):
        if index not in free:
            continue
        value += 1
        face.value = value
        free.discard(index)
        opposite = next((other for other in free
                         if _dot(face.normal, faces[other].normal) < -0.99),
                        None)
        if opposite is not None:
            faces[opposite].value = sides + 1 - value
            free.discard(opposite)


_MESH_CACHE = {}


def mesh_for(sides):
    """Меш кубика с подписями граней (кэшируется)."""
    mesh = _MESH_CACHE.get(sides)
    if mesh is not None:
        return mesh

    vertices = _polyhedron_vertices(sides)
    faces = []
    for indices, normal in _convex_faces(vertices):
        ordered = _order_around(indices, vertices, normal)
        points = [vertices[index] for index in ordered]
        center = tuple(sum(point[axis] for point in points) / len(points)
                       for axis in range(3))
        faces.append(_Face(ordered, normal, center))
    if len(faces) != sides:
        raise ValueError(f"d{sides}: получилось {len(faces)} граней")
    _assign_values(faces, sides)
    vertex_values = []
    if sides == 4:
        # На d4 числа печатают у вершин: результат читается по верхней
        # вершине, когда кубик лежит на грани. Порядок фиксируем, чтобы
        # один и тот же бросок всегда давал одинаковую картинку.
        order = sorted(
            range(len(vertices)),
            key=lambda index: (-round(vertices[index][2], 6),
                               math.atan2(vertices[index][1],
                                          vertices[index][0])))
        vertex_values = [0] * len(vertices)
        for value, index in enumerate(order, 1):
            vertex_values[index] = value
    mesh = _Mesh(sides, vertices, faces, vertex_values)
    _MESH_CACHE[sides] = mesh
    return mesh


def target_quaternion(face, rng=None):
    """Поворот, при котором грань face смотрит вверх (+Z)."""
    rng = rng or random
    align = _q_between(face.normal, (0.0, 0.0, 1.0))
    yaw = _q_axis((0.0, 0.0, 1.0), rng.uniform(0.0, 2 * math.pi))
    return _q_mul(yaw, align)


def target_for_value(mesh, value, rng=None, near=None):
    """Поворот, который показывает выпавшее значение.

    У обычных кубиков вверх смотрит грань. У d4 — наоборот: кубик
    ложится на грань, а вверх смотрит вершина с числом. Раньше d4
    балансировал на вершине: это и выглядело неправильно.

    ``near`` — текущий поворот после падения. Тогда выбирается ближайший
    вариант рыскания, чтобы кубик не крутился в последний момент через
    пол-оборота: доводка выглядит как естественная укладка.
    """
    rng = rng or random
    if mesh.sides == 4:
        index = mesh.vertex_values.index(value)
        align = _q_between(mesh.vertices[index], (0.0, 0.0, 1.0))
    else:
        face = next((item for item in mesh.faces if item.value == value),
                    mesh.faces[value % len(mesh.faces)])
        align = _q_between(face.normal, (0.0, 0.0, 1.0))
    if near is None:
        yaw = _q_axis((0.0, 0.0, 1.0), rng.uniform(0.0, 2 * math.pi))
        return _q_mul(yaw, align)
    best = None
    best_score = -2.0
    for step in range(12):
        yaw = _q_axis((0.0, 0.0, 1.0), step * math.pi / 6.0)
        candidate = _q_mul(yaw, align)
        score = abs(sum(candidate[i] * near[i] for i in range(4)))
        if score > best_score:
            best_score = score
            best = candidate
    return best


def _rest_height(mesh, quaternion, radius):
    """Высота центра, при которой самая низкая точка касается пола."""
    matrix = _q_matrix(quaternion)
    lowest = min(_rotate(matrix, vertex)[2] for vertex in mesh.vertices)
    return max(0.02, -lowest * radius + 0.004)


# ----------------------------------------------------------------------
# Камера
# ----------------------------------------------------------------------
class _Camera:
    """Слабая перспектива: лоток лежит горизонтально, камера чуть сверху."""

    def __init__(self, rect):
        self.cx = rect.center().x()
        # В узкой панели карточка результата занимает низ, поэтому лоток
        # поднимаем: кубики должны оставаться над ней.
        compact = rect.height() < 240
        self.cy = rect.center().y() - rect.height() * 0.05
        self.scale = (min(rect.width() / 3.8, rect.height() / 2.95)
                      if compact else min(rect.width() / 4.1,
                                          rect.height() / 3.0))
        self.tilt = math.radians(58)
        self.sin = math.sin(self.tilt)
        self.cos = math.cos(self.tilt)
        self.focal = 5.4

    def _camera_space(self, point):
        x, y, z = point
        return (x,
                y * self.cos + z * self.sin,
                -y * self.sin + z * self.cos)

    def depth(self, point):
        return self._camera_space(point)[2]

    def project(self, point):
        x, vertical, depth = self._camera_space(point)
        scale = self.scale * self.focal / (self.focal - depth)
        return QPointF(self.cx + x * scale, self.cy - vertical * scale)

    def normal_depth(self, normal):
        return -normal[1] * self.sin + normal[2] * self.cos


# ----------------------------------------------------------------------
# Звук падения
# ----------------------------------------------------------------------
class _ClickSound:
    """Короткий синтезированный щелчок — без файлов и лишних зависимостей."""

    def __init__(self):
        self.enabled = True
        self.volume = 0.6
        self._sounds = None

    def _build(self):
        import pygame

        if pygame.mixer.get_init() is None:
            return []
        frequency, _, channels = pygame.mixer.get_init()
        sounds = []
        for pitch in (150.0, 185.0, 220.0):
            frames = array("h")
            length = int(frequency * 0.055)
            for index in range(length):
                moment = index / frequency
                envelope = math.exp(-moment * 58)
                noise = random.uniform(-0.55, 0.55)
                tone = math.sin(2 * math.pi * pitch * moment)
                sample = int(11000 * envelope * (noise + 0.7 * tone))
                for _ in range(channels):
                    frames.append(max(-32767, min(32767, sample)))
            sounds.append(pygame.mixer.Sound(buffer=frames.tobytes()))
        return sounds

    def play(self):
        if not self.enabled:
            return
        try:
            import pygame

            if pygame.mixer.get_init() is None:
                return
            if self._sounds is None:
                self._sounds = self._build()
            if not self._sounds:
                return
            sound = random.choice(self._sounds)
            sound.set_volume(max(0.0, min(1.0, self.volume)))
            sound.play()
        except Exception:
            # Звук — необязательное украшение: без него бросок работает.
            pass


# ----------------------------------------------------------------------
# Лоток
# ----------------------------------------------------------------------
FRAME_DT = 1.0 / 120.0      # шаг мини-физики
PHYSICS_STEPS = 220         # максимум ~1.83 c; обычно сцена стихает раньше
SETTLE_STEPS = 34           # ~0.28 c на «укладку» выпавшей гранью
DIE_RADIUS = 0.26
TRAY_HALF_X = 1.45
TRAY_HALF_Y = 1.02


class _Die:
    __slots__ = ("sides", "value", "color", "mesh", "radius", "start",
                 "throw", "rest", "q_start", "q_target", "frames",
                 "duration")

    def __init__(self, sides, value, color, mesh, start, throw, rest,
                 q_start, q_target):
        self.sides = sides
        self.value = value
        self.color = color
        self.mesh = mesh
        self.radius = DIE_RADIUS
        self.start = start
        self.throw = throw
        self.rest = rest
        self.q_start = q_start
        self.q_target = q_target
        self.frames = []
        self.duration = 0.0


class DiceBoard(QWidget):
    """Лоток: мини-физика падения и отрисовка кубиков."""

    settled = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 150)
        self.setObjectName("dice_board")
        self._dice = []
        self._result = None
        self._values = []
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._started = 0.0
        self._running = False
        self._sound = _ClickSound()
        self._rng = random.Random()
        self._bounce_times = []
        self._bounces_played = 0
        self._omegas = []

    # --- управление ---------------------------------------------------
    def animate(self, result):
        """Запустить бросок по результату shared.dice."""
        self._timer.stop()
        self._result = result
        self._values = []
        specs = []
        for term in result.get("dice", []):
            sides = int(term.get("sides", 0))
            for value in term.get("kept", []):
                specs.append((sides, int(value)))
                self._values.append((sides, int(value)))
        self._dice = []
        self._omegas = []
        self._bounce_times = []
        self._bounces_played = 0
        if specs:
            self._layout(specs)
            self._simulate()
            self._started = time.monotonic()
            self._running = True
            self._timer.start()
        else:
            self._running = False
        self.update()

    def _layout(self, specs):
        """Разложить стартовые позиции: кубики вылетают из глубины лотка."""
        count = len(specs)
        columns = max(1, int(math.ceil(math.sqrt(count))))
        rows = max(1, int(math.ceil(count / columns)))
        spacing_x = min(0.62, 2.6 / columns)
        spacing_y = min(0.42, 1.4 / rows)
        for index, (sides, value) in enumerate(specs):
            if sides not in DIE_COLORS:
                continue
            mesh = mesh_for(sides)
            column = index % columns
            row = index // columns
            x = (column - (columns - 1) / 2.0) * spacing_x
            y = (row - (rows - 1) / 2.0) * spacing_y + 0.05
            x += self._rng.uniform(-0.05, 0.05)
            y += self._rng.uniform(-0.05, 0.05)
            # Бросок с дальней стороны: кубик летит на игрока, вращается
            # и падает на войлок с отскоком. Стартовые высоты разные,
            # чтобы кубики не падали синхронной стеной.
            start = (x, y,
                     1.35 + self._rng.uniform(0.0, 0.55)
                     + 0.05 * (index % 4))
            throw = (x * 0.9 + self._rng.uniform(-1.6, 1.6),
                     self._rng.uniform(-3.1, -1.2),
                     self._rng.uniform(0.4, 2.2))
            omega = _scale_vec(
                _unit((self._rng.uniform(-1, 1),
                       self._rng.uniform(-1, 1),
                       self._rng.uniform(-1, 1))),
                self._rng.uniform(16.0, 28.0))
            self._dice.append(_Die(
                sides=sides,
                value=value,
                color=DIE_COLORS[sides],
                mesh=mesh,
                start=start,
                throw=throw,
                rest=start,
                q_start=_q_random(self._rng),
                q_target=target_for_value(mesh, value, self._rng),
            ))
            self._omegas.append(omega)

    def _simulate(self):
        """Падение по-настоящему: гравитация, отскоки, бортики, столкновения.

        Кадры записываются заранее, а таймер только проигрывает их. Кубик
        касается пола самой низкой точкой своего текущего поворота — поэтому
        он перекатывается через рёбра и вершины, а не «плавает» вниз.
        """
        if not self._dice:
            return
        dt = FRAME_DT
        gravity = -24.0
        body = 0.235
        restitution = 0.42
        half_x = TRAY_HALF_X - 0.02
        half_y = TRAY_HALF_Y - 0.02
        states = []
        for die, omega in zip(self._dice, self._omegas):
            states.append({
                "pos": list(die.start),
                "vel": list(die.throw),
                "q": die.q_start,
                "omega": omega,
            })
            die.frames = []

        self._bounce_times = []

        def record():
            for die, state in zip(self._dice, states):
                die.frames.append((tuple(state["pos"]), state["q"]))

        def extents(die, state):
            matrix = _q_matrix(state["q"])
            rotated = [_rotate(matrix, vertex) for vertex in die.mesh.vertices]
            lowest = min(point[2] for point in rotated)
            ex = max(abs(point[0]) for point in rotated) * die.radius
            ey = max(abs(point[1]) for point in rotated) * die.radius
            return -lowest * die.radius, ex, ey

        record()
        calm_steps = 0
        for step in range(PHYSICS_STEPS):
            for index, state in enumerate(states):
                die = self._dice[index]
                state["vel"][2] += gravity * dt
                for axis in range(3):
                    state["pos"][axis] += state["vel"][axis] * dt

                omega = state["omega"]
                speed = math.sqrt(_dot(omega, omega))
                if speed > 1e-6:
                    state["q"] = _q_normalize(_q_mul(
                        _q_axis(omega, speed * dt), state["q"]))
                    state["omega"] = _scale_vec(omega, 0.9995)

                contact, ex, ey = extents(die, state)
                if state["pos"][2] < contact:
                    state["pos"][2] = contact
                    if state["vel"][2] < 0:
                        impact = -state["vel"][2]
                        if impact > 0.9:
                            self._bounce_times.append((step * dt, min(1.0, impact / 4.0)))
                        state["vel"][2] = impact * restitution
                        state["vel"][0] *= 0.78
                        state["vel"][1] *= 0.78
                        state["omega"] = _scale_vec(state["omega"], 0.58)
                    else:
                        state["vel"][2] = 0.0

                # Бортики: кубик отскакивает от деревянного борта.
                if state["pos"][0] < -half_x + ex:
                    state["pos"][0] = -half_x + ex
                    state["vel"][0] = abs(state["vel"][0]) * 0.55
                    state["omega"] = _scale_vec(state["omega"], 0.82)
                elif state["pos"][0] > half_x - ex:
                    state["pos"][0] = half_x - ex
                    state["vel"][0] = -abs(state["vel"][0]) * 0.55
                    state["omega"] = _scale_vec(state["omega"], 0.82)
                if state["pos"][1] < -half_y + ey:
                    state["pos"][1] = -half_y + ey
                    state["vel"][1] = abs(state["vel"][1]) * 0.55
                    state["omega"] = _scale_vec(state["omega"], 0.82)
                elif state["pos"][1] > half_y - ey:
                    state["pos"][1] = half_y - ey
                    state["vel"][1] = -abs(state["vel"][1]) * 0.55
                    state["omega"] = _scale_vec(state["omega"], 0.82)

            # Столкновения кубиков как упругих шаров: не даём им
            # проходить друг через друга и передаём импульс.
            for first in range(len(states)):
                for second in range(first + 1, len(states)):
                    a = states[first]
                    b = states[second]
                    delta = _sub(b["pos"], a["pos"])
                    distance = _length(delta)
                    if distance < 1e-6 or distance >= body * 2:
                        continue
                    normal = _scale_vec(delta, 1.0 / distance)
                    push = (body * 2 - distance) / 2.0
                    for axis in range(3):
                        a["pos"][axis] -= normal[axis] * push
                        b["pos"][axis] += normal[axis] * push
                    relative = _sub(b["vel"], a["vel"])
                    along = _dot(relative, normal)
                    if along < 0:
                        impulse = -(1.0 + 0.35) * along / 2.0
                        for axis in range(3):
                            a["vel"][axis] -= normal[axis] * impulse
                            b["vel"][axis] += normal[axis] * impulse
            record()

            # Как только все кубики затихли, не тянем сцену: укладываем
            # выпавшие грани. Иначе после короткого падения лоток почти
            # секунду стоит неподвижно, а доводка начинается в самом конце.
            resting = True
            for die, state in zip(self._dice, states):
                contact, _ex, _ey = extents(die, state)
                speed = math.sqrt(_dot(state["vel"], state["vel"]))
                spin = math.sqrt(_dot(state["omega"], state["omega"]))
                if (state["pos"][2] > contact + 0.03 or speed > 0.35
                        or spin > 3.0):
                    resting = False
                    break
            calm_steps = calm_steps + 1 if resting else 0
            if calm_steps >= 8:
                break

        # Укладка: положение после падения сохраняем, а поворот доводим
        # до выпавшей грани. Никакого скольжения по лотку — кубик остаётся
        # там, куда его привели физика и столкновения.
        end_pos = [list(state["pos"]) for state in states]
        end_q = [state["q"] for state in states]
        for die, start_q, start_pos in zip(self._dice, end_q, end_pos):
            die.q_target = target_for_value(die.mesh, die.value, self._rng,
                                            near=start_q)
            die.rest = (start_pos[0], start_pos[1],
                        _rest_height(die.mesh, die.q_target, die.radius))
        for step in range(1, SETTLE_STEPS + 1):
            fraction = step / float(SETTLE_STEPS)
            eased = fraction * fraction * (3.0 - 2.0 * fraction)
            # Лёгкий доводочный подскок, а не плавное опускание.
            hop = math.sin(eased * math.pi) * 0.035 * (1.0 - fraction)
            for die, state, start_pos, start_q in zip(
                    self._dice, states, end_pos, end_q):
                state["pos"] = (start_pos[0], start_pos[1],
                                start_pos[2]
                                + (die.rest[2] - start_pos[2]) * eased
                                + hop)
                state["q"] = _q_slerp(start_q, die.q_target, eased)
            record()

        duration = (len(self._dice[0].frames) - 1) * dt
        for die in self._dice:
            die.duration = duration

    def settle_now(self):
        """Сразу поставить кубики на место (тесты, закрытие панели)."""
        self._timer.stop()
        self._running = False
        self._started = time.monotonic() - 1000.0
        self.update()

    def is_rolling(self):
        return self._running

    def _tick(self):
        now = time.monotonic()
        elapsed = now - self._started
        duration = self._dice[0].duration if self._dice else 0.0
        while (self._bounces_played < len(self._bounce_times)
               and elapsed >= self._bounce_times[self._bounces_played][0]):
            _moment, loudness = self._bounce_times[self._bounces_played]
            self._bounces_played += 1
            if loudness > 0.35:
                self._sound.play()
        if elapsed >= duration:
            self._timer.stop()
            self._running = False
            self._sound.play()
            self.settled.emit()
        self.update()

    # --- рисование ----------------------------------------------------
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#101012"))
        camera = _Camera(self.rect())
        self._paint_tray(painter, camera)
        self._paint_shadows(painter, camera)
        self._paint_dice(painter, camera)
        self._paint_result(painter)

    def _paint_tray(self, painter, camera):
        outer = [(-1.78, -1.32, 0.0), (1.78, -1.32, 0.0),
                 (1.78, 1.32, 0.0), (-1.78, 1.32, 0.0)]
        inner = [(-1.55, -1.1, 0.012), (1.55, -1.1, 0.012),
                 (1.55, 1.1, 0.012), (-1.55, 1.1, 0.012)]

        painter.setPen(QPen(QColor("#2b1a10"), 2))
        painter.setBrush(QColor("#5a3824"))
        painter.drawPolygon(QPolygonF([camera.project(point)
                                       for point in outer]))

        polygon = QPolygonF([camera.project(point) for point in inner])
        gradient = QLinearGradient(polygon[0], polygon[2])
        gradient.setColorAt(0.0, QColor("#1b5a43"))
        gradient.setColorAt(1.0, QColor("#0d3529"))
        painter.setBrush(QBrush(gradient))
        painter.setPen(QPen(QColor("#2a1a12"), 2))
        painter.drawPolygon(polygon)

        # Внутренняя тень по краю войлока.
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(0, 0, 0, 90), 4))
        painter.drawPolygon(polygon)

    def _paint_shadows(self, painter, camera):
        painter.setPen(Qt.NoPen)
        now = time.monotonic()
        for die in self._dice:
            point, _quaternion = die_state(die, self._started, now)
            flat = camera.project((point[0], point[1], 0.0))
            radius = max(4.0, camera.scale * die.radius * 0.85)
            height = max(0.0, point[2] - die.radius)
            alpha = int(95 * max(0.12, 1.0 - height / 2.0))
            painter.setBrush(QColor(0, 0, 0, alpha))
            painter.drawEllipse(QRectF(flat.x() - radius,
                                       flat.y() - radius * 0.38,
                                       radius * 2, radius * 0.76))

    def _paint_dice(self, painter, camera):
        if not self._dice:
            return
        now = time.monotonic()
        items = []
        for die in self._dice:
            position, quaternion = die_state(die, self._started, now)
            matrix = _q_matrix(quaternion)
            world = []
            for vertex in die.mesh.vertices:
                rotated = _rotate(matrix, vertex)
                world.append((rotated[0] * die.radius + position[0],
                              rotated[1] * die.radius + position[1],
                              rotated[2] * die.radius + position[2]))
            for face in die.mesh.faces:
                normal = _rotate(matrix, face.normal)
                if camera.normal_depth(normal) <= 0.02:
                    continue
                points = [camera.project(world[index])
                          for index in face.indices]
                area = abs(_polygon_area(points))
                if area < 2:
                    continue
                center = tuple(
                    sum(world[index][axis] for index in face.indices)
                    / len(face.indices) for axis in range(3))
                depth = sum(camera.depth(world[index])
                            for index in face.indices) / len(face.indices)
                items.append((depth, 0, points, normal, die, face, area))

                # Базис самой грани: цифра лежит в её плоскости и вращается
                # вместе с кубиком. Экранное «выпрямление» здесь не нужно —
                # именно из-за него числа выглядели наклейками поверх меша.
                u = _unit(_sub(world[face.indices[0]], center))
                v = _unit(_cross(normal, u))
                if _length(u) < 1e-6 or _length(v) < 1e-6:
                    continue
                face_radius = max(
                    _length(_sub(world[index], center))
                    for index in face.indices)

                if die.sides == 4:
                    # На d4 число напечатано у каждой вершины на каждой
                    # грани — как на настоящем d4. Результат читается по
                    # верхней вершине, когда кубик лежит на грани.
                    for index in face.indices:
                        value = die.mesh.vertex_values[index]
                        if not value:
                            continue
                        anchor = tuple(
                            center[axis]
                            + (world[index][axis] - center[axis]) * 0.58
                            for axis in range(3))
                        items.append((depth + 0.001, 1, anchor, normal,
                                      u, v, str(value), points,
                                      face_radius * 0.35))
                else:
                    items.append((depth + 0.001, 1, center, normal,
                                  u, v, str(face.value), points,
                                  face_radius * 0.56))

        items.sort(key=lambda item: item[0])
        for item in items:
            if item[1] == 0:
                _depth, _kind, points, normal, die, _face, _dim = item
                light = max(0.0, _dot(normal, _unit(LIGHT)))
                shade = 0.58 + 0.42 * light
                base = QColor(die.color)
                color = QColor(min(255, int(base.red() * shade)),
                               min(255, int(base.green() * shade)),
                               min(255, int(base.blue() * shade)))
                painter.setBrush(color)
                painter.setPen(QPen(QColor(28, 22, 18, 200), 1.1))
                painter.drawPolygon(QPolygonF(points))
            else:
                _depth, _kind, center, normal, u, v, text, points, box = item
                self._paint_face_text(painter, camera, center, normal, u, v,
                                      text, points, box)

    @staticmethod
    def _paint_face_text(painter, camera, center, normal, u, v, text,
                         clip_points, box):
        """Напечатать цифру на грани: аффинно, в её собственной плоскости.

        Ось X цифры идёт вдоль ребра грани (u), ось Y — вдоль v. Поэтому
        при повороте кубика число поворачивается вместе с гранью, а не
        остаётся лицом к зрителю. Клип по многоугольнику грани не даёт
        цифре вылезти на соседние грани.
        """
        if _length(u) < 1e-6 or _length(v) < 1e-6:
            return
        base = camera.project(center)
        sample = 0.1
        along = camera.project((center[0] + u[0] * sample,
                                center[1] + u[1] * sample,
                                center[2] + u[2] * sample))
        across = camera.project((center[0] + v[0] * sample,
                                 center[1] + v[1] * sample,
                                 center[2] + v[2] * sample))
        du = QPointF(along.x() - base.x(), along.y() - base.y())
        dv = QPointF(across.x() - base.x(), across.y() - base.y())
        if abs(du.x() * dv.y() - du.y() * dv.x()) < 1e-9:
            return
        # 50 локальных единиц = половина коробки цифры (box в мировых).
        k = box / 5.0
        matrix = QTransform(du.x() * k, du.y() * k,
                            -dv.x() * k, -dv.y() * k,
                            base.x(), base.y())
        path = QPainterPath()
        path.addPolygon(QPolygonF(clip_points))
        path.closeSubpath()
        painter.save()
        painter.setClipPath(path)
        painter.setTransform(matrix)
        font = QFont("Arial")
        font.setPixelSize(100)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(0, 0, 0, 150))
        painter.drawText(QRectF(-47, -47, 100, 100), Qt.AlignCenter, text)
        painter.setPen(QColor(255, 255, 255, 242))
        painter.drawText(QRectF(-50, -50, 100, 100), Qt.AlignCenter, text)
        painter.restore()

    def _paint_result(self, painter):
        values = self._values
        total = self._result.get("total") if self._result else None
        if not values:
            painter.setPen(QColor(220, 218, 212, 110))
            font = QFont("Arial")
            font.setPixelSize(11 if self.height() < 240 else 13)
            painter.setFont(font)
            painter.drawText(self.rect().adjusted(0, 0, 0, -8),
                             Qt.AlignHCenter | Qt.AlignBottom,
                             "Бросьте кубики")
            return

        if self.height() < 240:
            self._paint_compact_result(painter, values, total)
            return

        card_width = min(300, self.width() - 24)
        card = QRectF(12, self.height() - 118, card_width, 106)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(246, 242, 232, 245))
        painter.drawRoundedRect(card, 8, 8)

        painter.setPen(QColor(32, 30, 27))
        font = QFont("Arial")
        font.setPixelSize(30)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(card.left() + 14, card.top() + 2, 110, 40),
                         Qt.AlignLeft | Qt.AlignVCenter, str(total))

        formula = self._result.get("formula", "") if self._result else ""
        detail = self._result.get("breakdown", "") if self._result else ""
        inner_width = int(card.width() - 28)
        font.setPixelSize(9)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(95, 90, 82))
        metrics = QFontMetrics(font)
        painter.drawText(
            QRectF(card.left() + 14, card.top() + 40, inner_width, 16),
            Qt.AlignLeft | Qt.AlignVCenter,
            metrics.elidedText(formula, Qt.ElideRight, inner_width),
        )
        if detail:
            font.setPixelSize(8)
            painter.setFont(font)
            metrics = QFontMetrics(font)
            painter.drawText(
                QRectF(card.left() + 14, card.top() + 56, inner_width, 14),
                Qt.AlignLeft | Qt.AlignVCenter,
                metrics.elidedText(detail, Qt.ElideRight, inner_width),
            )

        x = card.left() + 14
        y = card.top() + 76
        for sides, value in values[:10]:
            chip = QRectF(x, y, 24, 22)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(DIE_COLORS.get(sides, "#d9822b")))
            painter.drawRoundedRect(chip, 4, 4)
            painting = QFont("Arial")
            painting.setPixelSize(11)
            painting.setBold(True)
            painter.setFont(painting)
            painter.setPen(QColor(255, 255, 255, 240))
            painter.drawText(chip, Qt.AlignCenter, str(value))
            x += 28
        if len(values) > 10:
            painting = QFont("Arial")
            painting.setPixelSize(10)
            painter.setFont(painting)
            painter.setPen(QColor(95, 90, 82))
            painter.drawText(QRectF(x, y, 40, 22),
                             Qt.AlignLeft | Qt.AlignVCenter,
                             f"+{len(values) - 10}")

    def _paint_compact_result(self, painter, values, total):
        """Компактная карточка для лотка в узкой панели чата.

        На 200-пиксельной ширине полноразмерная карточка закрывала бы
        половину лотка, поэтому оставляем тонкую полоску: итог, формула и
        несколько цветных фишек. Подробности всё равно видны в истории.
        """
        width = min(206, self.width() - 14)
        height = 44
        card = QRectF(7, self.height() - height - 5, width, height)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(246, 242, 232, 238))
        painter.drawRoundedRect(card, 6, 6)

        painter.setPen(QColor(32, 30, 27))
        font = QFont("Arial")
        font.setPixelSize(20)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(card.left() + 9, card.top() + 1, 64, 21),
                         Qt.AlignLeft | Qt.AlignVCenter, str(total))

        formula = self._result.get("formula", "") if self._result else ""
        inner = int(card.width() - 18)
        font.setPixelSize(7)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(95, 90, 82))
        painter.drawText(
            QRectF(card.left() + 9, card.top() + 20, inner, 10),
            Qt.AlignLeft | Qt.AlignVCenter,
            QFontMetrics(font).elidedText(formula, Qt.ElideRight, inner),
        )

        x = card.left() + 9
        y = card.top() + 30
        for sides, value in values[:8]:
            chip = QRectF(x, y, 14, 11)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(DIE_COLORS.get(sides, "#d9822b")))
            painter.drawRoundedRect(chip, 2, 2)
            painting = QFont("Arial")
            painting.setPixelSize(7)
            painting.setBold(True)
            painter.setFont(painting)
            painter.setPen(QColor(255, 255, 255, 240))
            painter.drawText(chip, Qt.AlignCenter, str(value))
            x += 16
        if len(values) > 8:
            painting = QFont("Arial")
            painting.setPixelSize(7)
            painter.setFont(painting)
            painter.setPen(QColor(95, 90, 82))
            painter.drawText(QRectF(x, y, 30, 11),
                             Qt.AlignLeft | Qt.AlignVCenter,
                             f"+{len(values) - 8}")


def _polygon_area(points):
    total = 0.0
    for index, point in enumerate(points):
        nxt = points[(index + 1) % len(points)]
        total += point.x() * nxt.y() - nxt.x() * point.y()
    return total / 2.0


def die_state(die, started, now):
    """Кадр мини-физики в момент now: положение и поворот."""
    frames = die.frames
    if not frames:
        return die.rest, die.q_target
    index = (now - started) / FRAME_DT
    if index <= 0:
        return frames[0]
    if index >= len(frames) - 1:
        return frames[-1]
    first = int(index)
    fraction = index - first
    position_a, quaternion_a = frames[first]
    position_b, quaternion_b = frames[first + 1]
    position = tuple(position_a[axis]
                     + (position_b[axis] - position_a[axis]) * fraction
                     for axis in range(3))
    return position, _q_slerp(quaternion_a, quaternion_b, fraction)
