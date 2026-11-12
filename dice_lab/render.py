"""Отрисовка лотка и кубиков без 3D-окна: QPainter поверх физических поз.

Цифры не накладываются на экран: они рисуются в плоскости грани, поворачиваются
вместе с кубиком и обрезаются по его контуру. Поэтому на верхней грани видно
именно то, что выпало в физике.

Критические 1/20 добавляют поверх лотка красный или золотой фон, тряску
камеры, большое число и конфети; анимация живёт на собственном таймере.
"""
import bisect
import math
import random
import time

from PyQt5.QtCore import QPointF, QRectF, QTimer, Qt
from PyQt5.QtGui import (QBrush, QColor, QFont, QImage, QLinearGradient,
                         QPainter, QPainterPath, QPen, QPolygonF,
                         QRadialGradient, QTransform)
from PyQt5.QtWidgets import QWidget

from .geometry import cross, dot, length, rotate_vector, scale, sub, unit
from .physics import TRAY_DEPTH, TRAY_WIDTH, WALL_VISUAL_HEIGHT

DIE_COLORS = {
    4: '#d9534f',
    6: '#8d8d8d',
    8: '#3f8f4f',
    10: '#2f6fb5',
    12: '#7a4fa3',
    20: '#d9822b',
}
LIGHT = unit((0.35, -0.45, 0.82))

CRIT_FAIL_DURATION = 4.8
CRIT_SUCCESS_DURATION = 4.2
#: Ритм редкой 20, если запись не отдала отдельные удары: ~69 BPM трека.
SPECIAL_BEAT_SECONDS = 0.87
SPECIAL_RING_SECONDS = 0.55
CONFETTI_COLORS = ('#ffd166', '#f4a261', '#e76f51', '#2a9d8f',
                   '#4dabf7', '#f7f3e8')

_GLYPH_CACHE = {}


def _glyph(text):
    """Белая цифра с тёмной обводкой, один раз на значение."""
    image = _GLYPH_CACHE.get(text)
    if image is not None:
        return image
    size = 256
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    font = QFont('Arial')
    # Двузначные подписи на треугольных гранях d12/d20 шире самой грани,
    # если рисовать их тем же кеглем: «1» у «10» обрезается и остаётся «0».
    font.setPixelSize(190 if len(text) == 1 else 150)
    font.setBold(True)
    painter.setFont(font)
    path = QPainterPath()
    path.addText(0, 0, font, text)
    bounds = path.boundingRect()
    path.translate((size - bounds.width()) / 2.0 - bounds.left(),
                   (size - bounds.height()) / 2.0 - bounds.top())
    painter.setPen(QPen(QColor(0, 0, 0, 175), 12,
                        Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(path)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(255, 255, 255, 246))
    painter.drawPath(path)
    if text in ('6', '9'):
        # Как на настоящих кубиках: чёрточка не даёт спутать 6 и 9.
        box = path.boundingRect()
        bar = QRectF(box.left() + box.width() * 0.10,
                     box.bottom() + 8,
                     box.width() * 0.80, 9)
        painter.drawRoundedRect(bar, 3, 3)
    painter.end()
    _GLYPH_CACHE[text] = image
    return image


class Camera:
    """Перспектива от переднего края лотка, Z — вверх."""

    def __init__(self, rect):
        self.cx = rect.center().x()
        self.cy = rect.center().y() - rect.height() * 0.03
        self.scale = min(rect.width() / 20.2, rect.height() / 12.2)
        self.tilt = math.radians(60.0)
        self.sin = math.sin(self.tilt)
        self.cos = math.cos(self.tilt)
        self.focal = 30.0

    def _camera_space(self, point):
        x, y, z = point
        return (x, y * self.cos + z * self.sin,
                -y * self.sin + z * self.cos)

    def depth(self, point):
        return self._camera_space(point)[2]

    def project(self, point):
        x, vertical, depth = self._camera_space(point)
        factor = self.focal / max(1.0, self.focal - depth)
        return QPointF(self.cx + x * self.scale * factor,
                       self.cy - vertical * self.scale * factor)

    def normal_depth(self, normal):
        return -normal[1] * self.sin + normal[2] * self.cos


class DiceView(QWidget):
    """Лоток, кубики и их тени."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(560, 420)
        self.engine = None
        self._paint_only = False
        self.effect = None
        self.effect_special = False
        self._effect_duration_value = 0.0
        self._effect_started = 0.0
        self._beat_times = ()
        self._confetti = []
        self._confetti_bursts = []
        self._effect_timer = QTimer(self)
        self._effect_timer.setInterval(16)
        self._effect_timer.timeout.connect(self._tick_effect)

    def set_engine(self, engine):
        self.engine = engine
        self.clear_effect()
        self.update()

    # --- критические эффекты ------------------------------------------
    def play_critical(self, kind, duration=0.0, special=False, beats=()):
        """Запустить эффект натуральной 20 («success») или 1 («fail»).

        ``special`` — редкий трек на 20: анимация идёт по длине трека
        и танцует под бит. ``beats`` — измеренные удары самой записи.
        Для обычных критов работают короткие эффекты.
        """
        if kind not in ('success', 'fail'):
            return
        self.effect = kind
        self.effect_special = bool(special and kind == 'success')
        self._beat_times = tuple(beats) if self.effect_special else ()
        if self.effect_special:
            self._effect_duration_value = duration if duration > 0 else 6.0
        else:
            self._effect_duration_value = (
                CRIT_SUCCESS_DURATION if kind == 'success'
                else CRIT_FAIL_DURATION)
        self._effect_started = time.monotonic()
        self._confetti = self._make_confetti() if kind == 'success' else []
        self._confetti_bursts = (
            [(beat, self._make_confetti(18)) for beat in self._beat_times]
            if self.effect_special else [])
        self._effect_timer.start()
        self.update()

    def clear_effect(self):
        self.effect = None
        self.effect_special = False
        self._effect_duration_value = 0.0
        self._beat_times = ()
        self._confetti = []
        self._confetti_bursts = []
        self._effect_timer.stop()
        self.update()

    def _effect_elapsed(self):
        return max(0.0, time.monotonic() - self._effect_started)

    def _effect_duration(self):
        if self._effect_duration_value > 0:
            return self._effect_duration_value
        return (CRIT_SUCCESS_DURATION if self.effect == 'success'
                else CRIT_FAIL_DURATION)

    def _tick_effect(self):
        if not self.effect:
            self._effect_timer.stop()
            return
        if self._effect_elapsed() >= self._effect_duration():
            self.clear_effect()
            return
        self.update()

    def _special_pose(self, elapsed):
        """Поза танцующей 20: масштаб, наклон, сдвиг и сила удара.

        Скачки привязаны к отдельным ударам трека: на каждом бите цифра
        подпрыгивает и качается в сторону, между битами — плавный грув.
        """
        beats = self._beat_times
        if beats:
            index = bisect.bisect_right(beats, elapsed) - 1
            if index >= 0:
                previous = (beats[index - 1] if index > 0
                            else beats[0] - SPECIAL_BEAT_SECONDS)
                interval = beats[index] - previous
                since = elapsed - beats[index]
                side = 1.0 if index % 2 == 0 else -1.0
            else:
                interval = (beats[1] - beats[0] if len(beats) > 1
                            else SPECIAL_BEAT_SECONDS)
                since = elapsed - beats[0]
                side = 1.0
        else:
            index = int(elapsed // SPECIAL_BEAT_SECONDS)
            interval = SPECIAL_BEAT_SECONDS
            since = elapsed - index * SPECIAL_BEAT_SECONDS
            side = 1.0 if index % 2 == 0 else -1.0
        interval = max(0.30, interval)
        kick = math.exp(-max(0.0, since) * 5.0) if since >= 0 else 0.0
        groove = math.sin(math.tau * elapsed / interval)
        intro = min(1.0, elapsed / 0.85)
        intro = 1.0 - (1.0 - intro) ** 2
        scale = (1.55 + 0.17 * kick + 0.05 * groove) * (0.32 + 0.68 * intro)
        return {
            'scale': scale,
            'squash': 1.0 + 0.07 * kick,
            'stretch': 1.0 - 0.05 * kick,
            'angle': side * 9.0 * kick + 4.0 * groove,
            'dx': side * 18.0 * kick + 9.0 * groove,
            'dy': -22.0 * kick - 7.0 * groove,
            'kick': kick,
        }

    def _paint_beat_rings(self, painter, elapsed):
        """Золотые кольца, расходящиеся на каждом ударе трека."""
        if not self._beat_times:
            return
        center = QPointF(self.width() / 2.0, self.height() / 2.0 - 12.0)
        painter.save()
        painter.setBrush(Qt.NoBrush)
        for beat in self._beat_times:
            age = elapsed - beat
            if age < 0.0 or age > SPECIAL_RING_SECONDS:
                continue
            progress = age / SPECIAL_RING_SECONDS
            alpha = int(120 * (1.0 - progress) ** 1.5)
            if alpha <= 0:
                continue
            radius = 150.0 + 330.0 * progress
            painter.setPen(QPen(QColor(255, 214, 110, alpha),
                                6.0 - 3.5 * progress))
            painter.drawEllipse(center, radius, radius * 0.62)
        painter.restore()

    def _make_confetti(self, count=120):
        width = max(1, self.width())
        height = max(1, self.height())
        pieces = []
        for _ in range(count):
            pieces.append((
                random.uniform(0, width),
                random.uniform(-height * 0.35, 0.0),
                random.uniform(-75, 75),
                random.uniform(70, 230),
                random.uniform(0, 360),
                random.uniform(-360, 360),
                random.uniform(4, 10),
                random.choice(CONFETTI_COLORS),
                random.random() < 0.35,
            ))
        return pieces

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        elapsed = self._effect_elapsed() if self.effect else 0.0
        self._paint_background(painter, elapsed)

        camera = Camera(self.rect())
        if self.effect == 'fail':
            self._shake_camera(camera, elapsed)
        self._paint_tray(painter, camera)
        poses = self.engine.poses() if self.engine else []
        self._paint_shadows(painter, camera, poses)
        self._paint_dice(painter, camera, poses)
        if self.effect == 'success':
            self._paint_confetti(painter, elapsed)
        if self.effect:
            self._paint_critical_number(painter, elapsed)

    # --- критический фон ----------------------------------------------
    def _paint_background(self, painter, elapsed):
        background = QLinearGradient(0, 0, 0, self.height())
        if self.effect == 'fail':
            background.setColorAt(0.0, QColor('#2a0b0b'))
            background.setColorAt(1.0, QColor('#080506'))
        elif self.effect == 'success':
            background.setColorAt(0.0, QColor('#30250b'))
            background.setColorAt(1.0, QColor('#120e06'))
        else:
            background.setColorAt(0.0, QColor('#15161a'))
            background.setColorAt(1.0, QColor('#0b0c0e'))
        painter.fillRect(self.rect(), QBrush(background))
        if not self.effect:
            return
        center = QPointF(self.width() / 2.0, self.height() * 0.42)
        radius = max(self.width(), self.height()) * 0.72
        glow = QRadialGradient(center, radius)
        if self.effect == 'fail':
            pulse = 0.5 + 0.5 * math.sin(elapsed * 9.0)
            alpha = int(95 + 45 * pulse)
            glow.setColorAt(0.0, QColor(145, 18, 14, alpha))
            glow.setColorAt(0.55, QColor(70, 8, 8, alpha // 2))
        else:
            total = self._effect_duration()
            fade_start = max(1.5, total - 1.2)
            fade = 1.0 if elapsed < fade_start else max(
                0.0, 1.0 - (elapsed - fade_start) / 1.2)
            alpha = int(185 * fade)
            glow.setColorAt(0.0, QColor(255, 218, 105, alpha))
            glow.setColorAt(0.45, QColor(225, 165, 45, alpha // 2))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(self.rect(), QBrush(glow))

    def _shake_camera(self, camera, elapsed):
        if elapsed >= 1.5:
            return
        strength = (1.0 - elapsed / 1.5) ** 1.4 * 15.0
        camera.cx += (math.sin(elapsed * 97.0) * 0.8
                      + random.uniform(-0.35, 0.35)) * strength
        camera.cy += (math.cos(elapsed * 71.0) * 0.55
                      + random.uniform(-0.25, 0.25)) * strength

    def _paint_confetti(self, painter, elapsed):
        if not self._confetti and not self._confetti_bursts:
            return
        fade_start = self._effect_duration() - 1.1
        alpha = 255 if elapsed < fade_start else int(
            max(0.0, 255 * (1.0 - (elapsed - fade_start) / 1.1)))
        if alpha <= 0:
            return
        if self._confetti:
            self._draw_confetti(painter, self._confetti, elapsed, alpha)
        # Особый трек длинный: одного залпа на старте мало, поэтому
        # каждый удар выбрасывает небольшую горсть конфети.
        for beat, pieces in self._confetti_bursts:
            age = elapsed - beat
            if age < 0.0 or age > 2.4:
                continue
            burst_alpha = int(alpha * min(1.0, (2.4 - age) / 0.6))
            if burst_alpha > 0:
                self._draw_confetti(painter, pieces, age, burst_alpha)

    def _draw_confetti(self, painter, pieces, age, alpha):
        gravity = 210.0
        painter.save()
        painter.setPen(Qt.NoPen)
        for (x0, y0, vx, vy, angle, spin, size, color,
             round_piece) in pieces:
            x = x0 + vx * age
            y = y0 + vy * age + 0.5 * gravity * age * age
            if y > self.height() + 30:
                continue
            painter.save()
            painter.translate(x, y)
            painter.rotate(angle + spin * age)
            piece = QColor(color)
            piece.setAlpha(alpha)
            painter.setBrush(piece)
            rect = QRectF(-size / 2.0, -size / 4.0, size, size / 2.0)
            if round_piece:
                painter.drawEllipse(rect)
            else:
                painter.drawRect(rect)
            painter.restore()
        painter.restore()

    def _paint_critical_number(self, painter, elapsed):
        center_x = self.width() / 2.0
        angle = 0.0
        pose = self._special_pose(elapsed) if self.effect_special else None
        if self.effect == 'fail':
            text = '1'
            color = (220, 44, 34)
            outline = (58, 5, 5)
            progress = min(1.0, elapsed / 1.35)
            scale = 0.55 + 2.0 * (1.0 - (1.0 - progress) ** 2)
            alpha = 1.0 if elapsed < 1.75 else max(
                0.0, 1.0 - (elapsed - 1.75) / 1.0)
            center_y = self.height() / 2.0
        elif self.effect_special:
            text = '20'
            color = (255, 205, 80)
            outline = (112, 64, 16)
            scale = pose['scale']
            angle = pose['angle']
            center_x += pose['dx']
            center_y = self.height() / 2.0 + pose['dy']
            self._paint_beat_rings(painter, elapsed)
            total = self._effect_duration()
            fade_start = max(1.0, total - 0.8)
            alpha = 1.0 if elapsed < fade_start else max(
                0.0, 1.0 - (elapsed - fade_start) / 0.8)
        else:
            text = '20'
            color = (255, 205, 80)
            outline = (112, 64, 16)
            progress = min(1.0, elapsed / 1.5)
            scale = 2.8 - 1.9 * (1.0 - (1.0 - progress) ** 2)
            alpha = 1.0 if elapsed < 2.1 else max(
                0.0, 1.0 - (elapsed - 2.1) / 1.8)
            center_y = self.height() / 2.0 - min(70.0, elapsed * 32.0)
        if alpha <= 0:
            return
        font = QFont('Arial')
        font.setPixelSize(240)
        font.setBold(True)
        path = QPainterPath()
        path.addText(0, 0, font, text)
        bounds = path.boundingRect()
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setOpacity(alpha)
        painter.translate(center_x, center_y)
        painter.rotate(angle)
        if pose is not None:
            painter.scale(scale * pose['squash'], scale * pose['stretch'])
        else:
            painter.scale(scale, scale)
        painter.translate(-bounds.center().x(), -bounds.center().y())
        painter.setBrush(Qt.NoBrush)
        for width, glow_alpha in ((34, 28), (20, 52), (9, 90)):
            painter.setPen(QPen(QColor(color[0], color[1], color[2],
                                       glow_alpha), width,
                                Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawPath(path)
        painter.setPen(QPen(QColor(*outline), 5, Qt.SolidLine,
                            Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(QColor(color[0], color[1], color[2], 245))
        painter.drawPath(path)
        painter.restore()

    # --- лоток --------------------------------------------------------
    def _paint_tray(self, painter, camera):
        outer_x = TRAY_WIDTH / 2.0 + 0.55
        outer_y = TRAY_DEPTH / 2.0 + 0.55
        inner_x = TRAY_WIDTH / 2.0 - 0.45
        inner_y = TRAY_DEPTH / 2.0 - 0.45
        wall = WALL_VISUAL_HEIGHT
        base = -0.35

        def poly(points):
            return QPolygonF([camera.project(point) for point in points])

        # Внешняя деревянная коробка: передняя и боковые стенки.
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor('#4b2d1c'))
        painter.drawPolygon(poly([(-outer_x, -outer_y, base),
                                  (outer_x, -outer_y, base),
                                  (outer_x, -outer_y, wall),
                                  (-outer_x, -outer_y, wall)]))
        painter.setBrush(QColor('#3a2216'))
        painter.drawPolygon(poly([(-outer_x, -outer_y, base),
                                  (-outer_x, outer_y, base),
                                  (-outer_x, outer_y, wall),
                                  (-outer_x, -outer_y, wall)]))
        painter.setBrush(QColor('#422719'))
        painter.drawPolygon(poly([(outer_x, -outer_y, base),
                                  (outer_x, outer_y, base),
                                  (outer_x, outer_y, wall),
                                  (outer_x, -outer_y, wall)]))

        # Войлок.
        felt = poly([(-inner_x, -inner_y, 0.0), (inner_x, -inner_y, 0.0),
                     (inner_x, inner_y, 0.0), (-inner_x, inner_y, 0.0)])
        gradient = QLinearGradient(felt[0], felt[2])
        gradient.setColorAt(0.0, QColor('#1d6048'))
        gradient.setColorAt(1.0, QColor('#0c3528'))
        painter.setBrush(QBrush(gradient))
        painter.drawPolygon(felt)

        # Внутренние стенки борта.
        painter.setBrush(QColor('#2b1a10'))
        painter.drawPolygon(poly([(-inner_x, inner_y, 0), (inner_x, inner_y, 0),
                                  (inner_x, inner_y, wall),
                                  (-inner_x, inner_y, wall)]))
        painter.setBrush(QColor('#332014'))
        painter.drawPolygon(poly([(-inner_x, -inner_y, 0), (-inner_x, inner_y, 0),
                                  (-inner_x, inner_y, wall),
                                  (-inner_x, -inner_y, wall)]))
        painter.drawPolygon(poly([(inner_x, -inner_y, 0), (inner_x, inner_y, 0),
                                  (inner_x, inner_y, wall),
                                  (inner_x, -inner_y, wall)]))

        # Верхняя деревянная рамка.
        rim = QPainterPath()
        rim.addPolygon(poly([(-outer_x, -outer_y, wall),
                             (outer_x, -outer_y, wall),
                             (outer_x, outer_y, wall),
                             (-outer_x, outer_y, wall)]))
        rim.addPolygon(poly([(-inner_x, -inner_y, wall),
                             (inner_x, -inner_y, wall),
                             (inner_x, inner_y, wall),
                             (-inner_x, inner_y, wall)]))
        rim.setFillRule(Qt.OddEvenFill)
        painter.setPen(QPen(QColor('#6b4528'), 1.2))
        painter.setBrush(QColor('#5a3824'))
        painter.drawPath(rim)

        # Тень по краю войлока.
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(0, 0, 0, 90), 4))
        painter.drawPolygon(felt)

    # --- тени и кубики ------------------------------------------------
    def _paint_shadows(self, painter, camera, poses):
        painter.setPen(Qt.NoPen)
        for pose in poses:
            x, y, z = pose.position
            flat = camera.project((x, y, 0.0))
            radius = camera.scale * 1.15
            height = max(0.0, z)
            alpha = int(105 * max(0.08, 1.0 - height / 9.0))
            painter.setBrush(QColor(0, 0, 0, alpha))
            painter.drawEllipse(QRectF(flat.x() - radius,
                                       flat.y() - radius * 0.34,
                                       radius * 2, radius * 1.05))

    def _paint_dice(self, painter, camera, poses):
        if not self.engine:
            return
        items = []
        for die, pose in zip(self.engine.dice, poses):
            quaternion = pose.quaternion
            radius = die.mesh.radius
            position = pose.position
            world = []
            for vertex in die.mesh.vertices:
                rotated = rotate_vector(quaternion, scale(vertex, radius))
                world.append((rotated[0] + position[0],
                              rotated[1] + position[1],
                              rotated[2] + position[2]))
            for face in die.mesh.faces:
                normal = rotate_vector(quaternion, face.normal)
                if camera.normal_depth(normal) <= 0.02:
                    continue
                points = [camera.project(world[index])
                          for index in face.indices]
                if abs(_polygon_area(points)) < 2.0:
                    continue
                center = tuple(sum(world[index][axis]
                                   for index in face.indices)
                               / len(face.indices) for axis in range(3))
                depth = sum(camera.depth(world[index])
                            for index in face.indices) / len(face.indices)
                items.append((depth, 0, points, normal, die, face, center))
                face_radius = max(length(sub(world[index], center))
                                  for index in face.indices)
                if die.mesh.sides == 4:
                    for index in face.indices:
                        value = die.mesh.vertex_values[index]
                        radial = unit(sub(world[index], center))
                        up = _project_to_face(radial, normal)
                        right = unit(cross(up, normal))
                        anchor = tuple(
                            center[axis]
                            + (world[index][axis] - center[axis]) * 0.58
                            for axis in range(3))
                        items.append((depth + 0.001, 1, anchor, normal, right,
                                      up, str(value), points,
                                      face_radius * 0.35))
                else:
                    # Для d6 «верх» цифры — середина ребра, для остальных —
                    # первая вершина. Так числа не стоят косо на гранях.
                    if die.mesh.sides == 6 and len(face.indices) >= 2:
                        first = world[face.indices[0]]
                        second = world[face.indices[1]]
                        up_hint = scale((first[0] + second[0],
                                         first[1] + second[1],
                                         first[2] + second[2]), 0.5)
                        up_hint = sub(up_hint, center)
                    else:
                        up_hint = sub(world[face.indices[0]], center)
                    up = _project_to_face(up_hint, normal)
                    right = unit(cross(up, normal))
                    if length(up) < 1e-6 or length(right) < 1e-6:
                        continue
                    items.append((depth + 0.001, 1, center, normal, right,
                                  up, face.label, points,
                                  face_radius * 0.56))

        items.sort(key=lambda item: item[0])
        for item in items:
            if item[1] == 0:
                _depth, _kind, points, normal, die, _face, _center = item
                light = max(0.0, dot(normal, LIGHT))
                shade = 0.56 + 0.44 * light
                base = QColor(DIE_COLORS.get(die.sides, '#d9822b'))
                color = QColor(min(255, int(base.red() * shade)),
                               min(255, int(base.green() * shade)),
                               min(255, int(base.blue() * shade)))
                painter.setBrush(color)
                painter.setPen(QPen(QColor(26, 20, 16, 210), 1.2))
                painter.drawPolygon(QPolygonF(points))
            else:
                _depth, _kind, center, normal, u, v, text, points, box = item
                _paint_face_label(painter, camera, center, normal, u, v,
                                  text, points, box)


def _project_to_face(vector, normal):
    """Убрать из вектора нормальную составляющую и нормировать."""
    return unit(sub(vector, scale(normal, dot(vector, normal))))


def _paint_face_label(painter, camera, center, normal, u, v, text,
                      clip_points, box):
    base = camera.project(center)
    sample = 0.12
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
    k = box / 6.0
    matrix = QTransform(du.x() * k, du.y() * k,
                        -dv.x() * k, -dv.y() * k,
                        base.x(), base.y())
    clip = QPainterPath()
    clip.addPolygon(QPolygonF(clip_points))
    clip.closeSubpath()
    painter.save()
    painter.setClipPath(clip)
    painter.setTransform(matrix)
    painter.drawImage(QRectF(-50, -50, 100, 100), _glyph(text))
    painter.restore()


def _polygon_area(points):
    total = 0.0
    for index, point in enumerate(points):
        nxt = points[(index + 1) % len(points)]
        total += point.x() * nxt.y() - nxt.x() * point.y()
    return total / 2.0
