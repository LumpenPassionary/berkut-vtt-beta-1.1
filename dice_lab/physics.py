"""Физика кубиков на Bullet (Panda3D): без подкруток результата.

Бросок задаёт только начальные условия — положение, поворот, скорость и
вращение. Дальше Bullet честно считает гравитацию, трение, отскоки и
контакты между кубиками. Результат читается уже после остановки по
фактическому повороту тела:

* у d4 — по верхней вершине;
* у остальных — по верхней грани.

Если кубик встал на ребро (margin < 16°), он считается нечитаемым: его
можно перебросить. Никакого «доворота к нужному числу» здесь нет.
"""
import math
import random
from dataclasses import dataclass

from panda3d.bullet import (BulletBoxShape, BulletConvexHullShape,
                            BulletRigidBodyNode, BulletWorld)
from panda3d.core import LPoint3, LQuaternion, LVector3, NodePath

from .geometry import DIE_RADIUS, mesh_for, rotate_vector, unit

PHYSICS_STEP = 1.0 / 120.0
GRAVITY = 50.0
REST_LINEAR_SPEED = 0.12
REST_ANGULAR_SPEED = 0.25
REST_SECONDS = 0.30
ROLL_TIMEOUT_SECONDS = 12.0

TRAY_WIDTH = 18.0
TRAY_DEPTH = 11.0
WALL_VISUAL_HEIGHT = 1.0
WALL_COLLIDER_HEIGHT = 10.0

READING_MARGIN_DEGREES = 16.0

#: Откуда и как выбрасываются кубики: сетка у левой стены, как в TtRP.
SPACING = 2.4
PER_LAYER = 9


@dataclass(frozen=True)
class Pose:
    position: tuple
    quaternion: tuple       # (w, x, y, z)


@dataclass(frozen=True)
class Reading:
    value: int
    tilt: float             # градусы от вертикали
    margin: float           # отрыв от второй по высоте грани/вершины, градусы

    @property
    def readable(self):
        return self.margin >= READING_MARGIN_DEGREES


@dataclass(frozen=True)
class Impact:
    """Резкое изменение скорости кубика — повод для звука удара."""
    die: int
    surface: str            # 'floor' | 'wall'
    speed: float


@dataclass
class DieBody:
    sides: int
    mesh: object
    body: BulletRigidBodyNode
    node: NodePath


def random_quaternion(rng):
    """Равномерный случайный поворот (алгоритм Шумейка)."""
    u1, u2, u3 = rng.random(), rng.random(), rng.random()
    s1 = math.sqrt(1.0 - u1)
    s2 = math.sqrt(u1)
    return LQuaternion(s1 * math.sin(2 * math.pi * u2),
                       s1 * math.cos(2 * math.pi * u2),
                       s2 * math.sin(2 * math.pi * u3),
                       s2 * math.cos(2 * math.pi * u3))


def _launch_spec(index, rng, lift=0.0):
    layer = index // PER_LAYER
    slot = index % PER_LAYER
    column = slot % 3
    row = slot // 3
    jitter = lambda amount: (rng.random() - 0.5) * 2.0 * amount
    position = (-TRAY_WIDTH / 2 + 2.0 + column * SPACING + jitter(0.25),
                (row - 1) * SPACING + jitter(0.25),
                2.6 + lift + layer * SPACING + jitter(0.25))
    velocity = (8.0 + rng.random() * 6.0,
                -2.0 + rng.random() * 3.0,
                jitter(6.0))
    axis = LVector3(rng.uniform(-1, 1), rng.uniform(-1, 1),
                    rng.uniform(-1, 1))
    if axis.length() < 1e-6:
        axis = LVector3(1, 0, 0)
    axis.normalize()
    spin = axis * (10.0 + rng.random() * 14.0)
    return position, random_quaternion(rng), velocity, spin


def read_die(mesh, quaternion):
    """Прочитать результат по фактическому повороту кубика."""
    if mesh.reading == 'TOP_VERTEX':
        directions = [unit(vertex) for vertex in mesh.vertices]
        values = list(mesh.vertex_values)
    else:
        directions = [face.normal for face in mesh.faces]
        values = [face.value for face in mesh.faces]
    heights = [rotate_vector(quaternion, direction)[2]
               for direction in directions]
    order = sorted(range(len(directions)), key=lambda i: heights[i],
                   reverse=True)
    best, second = order[0], order[1]
    tilt = math.degrees(math.acos(max(-1.0, min(1.0, heights[best]))))
    second_tilt = math.degrees(
        math.acos(max(-1.0, min(1.0, heights[second]))))
    return Reading(value=values[best], tilt=tilt,
                   margin=second_tilt - tilt)


class DicePhysics:
    """Один лоток и один активный бросок."""

    def __init__(self):
        self.world = BulletWorld()
        self.world.set_gravity(LVector3(0, 0, -GRAVITY))
        self.dice = []
        self._tray = []
        self._prev_velocities = []
        self._build_tray()
        self.rng = random.SystemRandom()

    # --- арена --------------------------------------------------------
    def _build_tray(self):
        # Борта стоят по краю войлока, а не по внешнему краю дерева:
        # иначе кубик может наполовину свеситься на рамку.
        half_width = TRAY_WIDTH / 2.0 - 0.45
        half_depth = TRAY_DEPTH / 2.0 - 0.45
        half_wall = WALL_COLLIDER_HEIGHT / 2.0
        thickness = 0.5

        def fixed(name, half_extents, position, friction=0.7,
                  restitution=0.25):
            body = BulletRigidBodyNode(name)
            body.add_shape(BulletBoxShape(LVector3(*half_extents)))
            node = NodePath(body)
            node.set_pos(LPoint3(*position))
            self.world.attach_rigid_body(body)
            body.set_friction(friction)
            body.set_restitution(restitution)
            self._tray.append(node)
            return body

        fixed('floor', (half_width + 1, half_depth + 1, thickness),
              (0, 0, -thickness))
        fixed('wall-x-', (thickness, half_depth + 1, half_wall),
              (-half_width - thickness, 0, half_wall))
        fixed('wall-x+', (thickness, half_depth + 1, half_wall),
              (half_width + thickness, 0, half_wall))
        fixed('wall-y-', (half_width + 1, thickness, half_wall),
              (0, -half_depth - thickness, half_wall))
        fixed('wall-y+', (half_width + 1, thickness, half_wall),
              (0, half_depth + thickness, half_wall))
        # Потолок не даёт кубикам улететь из поля зрения при большом наборе.
        fixed('ceiling', (half_width + 1, half_depth + 1, thickness),
              (0, 0, WALL_COLLIDER_HEIGHT + thickness))

    # --- бросок -------------------------------------------------------
    def clear(self):
        for die in self.dice:
            self.world.remove_rigid_body(die.body)
            die.node.remove_node()
        self.dice = []
        self._prev_velocities = []

    def throw(self, sides_list):
        self.clear()
        for index, sides in enumerate(sides_list):
            position, quaternion, velocity, spin = _launch_spec(
                index, self.rng)
            self._add_die(sides, position, quaternion, velocity, spin)
        return self

    def relaunch(self, indices):
        """Перебросить только указанные кубики, остальные оставить лежать."""
        for launch_index, index in enumerate(indices):
            if not 0 <= index < len(self.dice):
                continue
            die = self.dice[index]
            launch_index = min(launch_index, PER_LAYER - 1)
            position, quaternion, velocity, spin = _launch_spec(
                launch_index, self.rng, lift=2.4)
            die.node.set_pos(LPoint3(*position))
            die.node.set_quat(quaternion)
            die.body.set_linear_velocity(LVector3(*velocity))
            die.body.set_angular_velocity(LVector3(*spin))
            die.body.set_active(True)
            self._prev_velocities[index] = tuple(velocity)

    def _add_die(self, sides, position, quaternion, velocity, spin):
        mesh = mesh_for(sides)
        radius = DIE_RADIUS[sides]
        shape = BulletConvexHullShape()
        for vertex in mesh.vertices:
            shape.add_point(LPoint3(vertex[0] * radius,
                                    vertex[1] * radius,
                                    vertex[2] * radius))
        body = BulletRigidBodyNode(f'd{sides}')
        body.add_shape(shape)
        body.set_mass(1.0)
        body.set_friction(0.45)
        body.set_restitution(0.35)
        body.set_linear_damping(0.1)
        body.set_angular_damping(0.2)
        body.set_ccd_motion_threshold(0.1)
        body.set_ccd_swept_sphere_radius(0.5)
        node = NodePath(body)
        node.set_pos(LPoint3(*position))
        node.set_quat(quaternion)
        self.world.attach_rigid_body(body)
        body.set_linear_velocity(LVector3(*velocity))
        body.set_angular_velocity(LVector3(*spin))
        body.set_active(True)
        self.dice.append(DieBody(sides=sides, mesh=mesh, body=body, node=node))
        self._prev_velocities.append(tuple(velocity))

    # --- шаг и состояние ---------------------------------------------
    def step(self):
        """Один шаг Bullet; возвращает удары для звука."""
        previous = self._prev_velocities
        self.world.do_physics(PHYSICS_STEP, 1, PHYSICS_STEP)
        impacts = []
        current = []
        for index, die in enumerate(self.dice):
            velocity = die.body.get_linear_velocity()
            now = (velocity.x, velocity.y, velocity.z)
            current.append(now)
            if index >= len(previous):
                continue
            before = previous[index]
            delta = (now[0] - before[0], now[1] - before[1],
                     now[2] - before[2])
            speed = math.sqrt(delta[0] ** 2 + delta[1] ** 2 + delta[2] ** 2)
            if speed < 1.2:
                continue
            position = die.node.get_pos()
            vertical = abs(delta[2])
            horizontal = math.sqrt(delta[0] ** 2 + delta[1] ** 2)
            surface = ('floor' if position.z < 1.5 and vertical >= horizontal
                       else 'wall')
            impacts.append(Impact(die=index, surface=surface, speed=speed))
        self._prev_velocities = current
        return impacts

    def poses(self):
        return [Pose(position=tuple(die.node.get_pos()),
                     quaternion=tuple(die.node.get_quat()))
                for die in self.dice]

    def motion(self):
        max_linear = 0.0
        max_angular = 0.0
        all_sleeping = True
        escaped = False
        limit_x = TRAY_WIDTH / 2.0 + 1.5
        limit_y = TRAY_DEPTH / 2.0 + 1.5
        for die in self.dice:
            position = die.node.get_pos()
            if (abs(position.x) > limit_x or abs(position.y) > limit_y
                    or position.z < -1.5 or position.z > 13.0):
                escaped = True
            if not die.body.is_active():
                continue
            all_sleeping = False
            linear = die.body.get_linear_velocity()
            angular = die.body.get_angular_velocity()
            max_linear = max(max_linear, linear.length())
            max_angular = max(max_angular, angular.length())
        return {
            'max_linear': max_linear,
            'max_angular': max_angular,
            'all_sleeping': all_sleeping,
            'escaped': escaped,
        }

    def readings(self):
        return [read_die(die.mesh, pose.quaternion)
                for die, pose in zip(self.dice, self.poses())]

    def values(self):
        return [reading.value for reading in self.readings()]


@dataclass(frozen=True)
class Outcome:
    status: str            # OK | COCKED | TIMEOUT | ESCAPED
    readings: tuple
    elapsed: float

    @property
    def values(self):
        return tuple(reading.value for reading in self.readings)


def settle(engine, max_seconds=ROLL_TIMEOUT_SECONDS):
    """Прокрутить симуляцию до остановки (для тестов и статистики)."""
    rest = 0.0
    elapsed = 0.0
    limit = int(max_seconds / PHYSICS_STEP)
    for _ in range(limit):
        engine.step()
        elapsed += PHYSICS_STEP
        motion = engine.motion()
        if motion['escaped']:
            return Outcome('ESCAPED', tuple(engine.readings()), elapsed)
        resting = motion['all_sleeping'] or (
            motion['max_linear'] < REST_LINEAR_SPEED
            and motion['max_angular'] < REST_ANGULAR_SPEED)
        rest = rest + PHYSICS_STEP if resting else 0.0
        if rest >= REST_SECONDS:
            readings = tuple(engine.readings())
            status = ('COCKED' if any(not reading.readable
                                      for reading in readings) else 'OK')
            return Outcome(status, readings, elapsed)
    return Outcome('TIMEOUT', tuple(engine.readings()), elapsed)
