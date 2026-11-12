"""Геометрия кубиков D4–D20 для отдельного приложения.

Модуль не зависит от PyQt/Panda3D: только чистые кортежи и математика.
Кубик — выпуклая оболочка вершин, грани упорядочены против часовой стрелки
при взгляде снаружи. Подписи назначаются один раз при создании меша:

* у d4 число живёт на вершине (результат — верхняя вершина);
* у d10 используются настоящие 0–9, где 0 читается как 10;
* у остальных противоположные грани дают N + 1.

Такая нумерация не влияет на бросок: физика сама решает, какая грань
окажется сверху, а чтение просто смотрит на итоговый поворот.
"""
import math
from dataclasses import dataclass
from itertools import combinations

PHI = (1 + 5 ** 0.5) / 2
DIE_ORDER = (4, 6, 8, 10, 12, 20)

#: Радиус описанной сферы в мировых единицах. Лоток — 18 × 11.
DIE_RADIUS = {
    4: 1.2,
    6: 1.0,
    8: 1.05,
    10: 1.05,
    12: 1.1,
    20: 1.12,
}


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def length(a):
    return math.sqrt(dot(a, a))


def unit(a):
    size = length(a)
    return (a[0] / size, a[1] / size, a[2] / size) if size else (0.0, 0.0, 0.0)


def scale(a, factor):
    return (a[0] * factor, a[1] * factor, a[2] * factor)


def rotate_vector(q, v):
    """Повернуть вектор кватернионом (w, x, y, z)."""
    w, x, y, z = q
    size = math.sqrt(w * w + x * x + y * y + z * z) or 1.0
    w, x, y, z = w / size, x / size, y / size, z / size
    qv = (x, y, z)
    t = scale(cross(qv, v), 2.0)
    t2 = cross(qv, t)
    return (v[0] + w * t[0] + t2[0],
            v[1] + w * t[1] + t2[1],
            v[2] + w * t[2] + t2[2])


@dataclass(frozen=True)
class Face:
    indices: tuple
    normal: tuple
    center: tuple
    value: int
    sides: int = 0

    @property
    def label(self):
        # Только у d10 на грани напечатан 0 вместо 10. У d12/d20
        # значение 10 должно оставаться двузначным.
        if self.sides == 10 and self.value == 10:
            return "0"
        return str(self.value)


@dataclass(frozen=True)
class Mesh:
    sides: int
    vertices: tuple
    faces: tuple
    vertex_values: tuple
    reading: str            # 'TOP_FACE' | 'TOP_VERTEX'

    @property
    def radius(self):
        return DIE_RADIUS[self.sides]

    def face_label(self, face):
        return face.label


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
    radius = max(length(point) for point in raw)
    return [scale(point, 1.0 / radius) for point in raw]


def _order_around(indices, vertices, normal):
    center = tuple(sum(vertices[index][axis] for index in indices)
                   / len(indices) for axis in range(3))
    reference = unit(sub(vertices[indices[0]], center))
    side = cross(normal, reference)
    return tuple(sorted(
        indices,
        key=lambda index: math.atan2(
            dot(sub(vertices[index], center), side),
            dot(sub(vertices[index], center), reference),
        ),
    ))


def _convex_faces(vertices):
    """Грани выпуклой оболочки: плоскости, оставляющие все точки позади."""
    faces = {}
    for i, j, k in combinations(range(len(vertices)), 3):
        normal = cross(sub(vertices[j], vertices[i]),
                       sub(vertices[k], vertices[i]))
        if length(normal) < 1e-7:
            continue
        normal = unit(normal)
        offset = dot(normal, vertices[i])
        if offset < 0:
            normal = scale(normal, -1)
            offset = -offset
        if any(dot(normal, point) > offset + 1e-6 for point in vertices):
            continue
        members = tuple(index for index, point in enumerate(vertices)
                        if abs(dot(normal, point) - offset) < 1e-6)
        if len(members) >= 3:
            faces[members] = normal
    return faces


def _opposite(normals, index):
    for other, normal in enumerate(normals):
        if dot(normal, normals[index]) < -1 + 1e-6:
            return other
    raise ValueError("У грани нет противоположной")


def _paired_values(normals, sides):
    values = [0] * len(normals)
    order = sorted(range(len(normals)),
                   key=lambda index: (-round(normals[index][2], 5),
                                      math.atan2(normals[index][1],
                                                 normals[index][0])))
    next_value = 1
    for index in order:
        if values[index]:
            continue
        opposite = _opposite(normals, index)
        values[index] = next_value
        values[opposite] = sides + 1 - next_value
        next_value += 1
    return values


def _d10_values(normals):
    """0–9, как на настоящем d10: противоположные грани в сумме 9."""
    values = [-1] * len(normals)
    top = [index for index, normal in enumerate(normals) if normal[2] > 0.1]
    top.sort(key=lambda index: math.atan2(normals[index][1],
                                          normals[index][0]))
    for position, index in enumerate(top):
        values[index] = 2 * position + 1
        values[_opposite(normals, index)] = 8 - 2 * position
    return [10 if value == 0 else value for value in values]


def _vertex_values(vertices):
    """Подписи вершин d4; результат читается по верхней вершине."""
    order = sorted(range(len(vertices)),
                   key=lambda index: (-round(vertices[index][2], 6),
                                      math.atan2(vertices[index][1],
                                                 vertices[index][0])))
    values = [0] * len(vertices)
    for value, index in enumerate(order, 1):
        values[index] = value
    return tuple(values)


_MESH_CACHE = {}


def mesh_for(sides):
    mesh = _MESH_CACHE.get(sides)
    if mesh is not None:
        return mesh
    if sides not in DIE_RADIUS:
        raise ValueError(f"Поддерживаются только {DIE_ORDER}")
    vertices = tuple(_polyhedron_vertices(sides))
    raw_faces = _convex_faces(vertices)
    normals = []
    ordered = []
    centers = []
    for indices, normal in raw_faces.items():
        order = _order_around(indices, vertices, normal)
        points = [vertices[index] for index in order]
        normals.append(normal)
        ordered.append(order)
        centers.append(tuple(sum(point[axis] for point in points) / len(points)
                             for axis in range(3)))
    if len(ordered) != sides:
        raise ValueError(f"d{sides}: получилось {len(ordered)} граней")

    if sides == 4:
        values = [0] * sides
    elif sides == 10:
        values = _d10_values(normals)
    else:
        values = _paired_values(normals, sides)

    faces = tuple(
        Face(indices=indices, normal=normal, center=center, value=value,
             sides=sides)
        for indices, normal, center, value in zip(ordered, normals, centers,
                                                   values)
    )
    vertex_values = _vertex_values(vertices) if sides == 4 else ()
    mesh = Mesh(
        sides=sides,
        vertices=vertices,
        faces=faces,
        vertex_values=vertex_values,
        reading='TOP_VERTEX' if sides == 4 else 'TOP_FACE',
    )
    _MESH_CACHE[sides] = mesh
    return mesh
