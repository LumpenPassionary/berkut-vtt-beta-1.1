"""Иконки предметов для инвентаря, справочника и перетаскивания.

Предметы в справочнике SRD не несут картинок: у большинства записей
``img = null``. Рисовать PNG на каждую из 976 записей бессмысленно, поэтому
иконка собирается из записи предмета: зелья получают склянку с жидкостью
своего цвета, Healing Word-совместимые зелья лечения — красную, разные
ступени лечения различаются оттенком и числом пузырьков.

Иконки детерминированы: одно и то же зелье всегда выглядит одинаково, а
цвет редкого зелья не зависит от темы оформления. Всё рисуется QPainter-ом,
поэтому картинки не нужно тащить в сборку отдельными файлами.
"""
import math
import zlib

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import (QColor, QIcon, QLinearGradient, QPainter,
                         QPainterPath, QPen, QPixmap)

_CACHE = {}


def _text(rec: dict) -> str:
    return " ".join(str((rec or {}).get(key) or "") for key in
                    ("name", "source_name", "subtype", "itype")).casefold()


def _is_potion(rec: dict) -> bool:
    text = _text(rec)
    return ("зель" in text or "potion" in text or "philter" in text
            or "elixir" in text or "мазь" in text or "ointment" in text
            or "бальзам" in text)


def _is_healing(rec: dict) -> bool:
    text = _text(rec)
    return ("лечен" in text or "healing" in text
            or "potion of healing" in text)


def _potion_tier(rec: dict) -> int:
    """0 — обычное, 1 — большое, 2 — превосходное, 3 — наивысшее."""
    text = _text(rec)
    if "наивыс" in text or "supreme" in text:
        return 3
    if "превосход" in text or "superior" in text:
        return 2
    if "больш" in text or "greater" in text:
        return 1
    return 0


def _potion_style(rec: dict):
    """(жидкость, блик, число пузырьков, полоски) для склянки."""
    text = _text(rec)
    if "скорост" in text or "speed" in text or "haste" in text:
        return QColor(238, 176, 38), QColor(255, 232, 150), 3, True
    if "яд" in text or "poison" in text or "токс" in text:
        return QColor(72, 170, 84), QColor(190, 245, 160), 2, False
    if "огон" in text or "fire" in text or "пламен" in text:
        return QColor(226, 112, 28), QColor(255, 220, 150), 3, False
    if _is_healing(rec):
        tier = _potion_tier(rec)
        return (
            (QColor(196, 38, 44), QColor(255, 205, 205), 1, False),
            (QColor(214, 46, 40), QColor(255, 218, 165), 2, False),
            (QColor(164, 26, 54), QColor(255, 188, 205), 3, False),
            (QColor(128, 34, 92), QColor(238, 186, 255), 4, False),
        )[tier]
    # Прочие зелья: стабильный оттенок из имени, чтобы два разных зелья
    # не выглядели одинаковыми.
    hue = zlib.crc32(text.encode("utf-8")) % 360
    liquid = QColor.fromHsv(hue, 190, 210)
    return liquid, QColor.fromHsv(hue, 45, 255), 2, False


def _flask_path() -> QPainterPath:
    path = QPainterPath()
    path.moveTo(27.0, 10.0)
    path.lineTo(37.0, 10.0)
    path.lineTo(37.0, 24.0)
    path.cubicTo(44.0, 28.0, 48.0, 34.0, 48.0, 42.0)
    path.cubicTo(48.0, 53.0, 41.0, 60.0, 32.0, 60.0)
    path.cubicTo(23.0, 60.0, 16.0, 53.0, 16.0, 42.0)
    path.cubicTo(16.0, 34.0, 20.0, 28.0, 27.0, 24.0)
    path.closeSubpath()
    return path


def _draw_potion(painter: QPainter, rec: dict) -> None:
    liquid, shine, bubbles, stripes = _potion_style(rec)
    body = _flask_path()

    # Тень под склянкой.
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(0, 0, 0, 55))
    painter.drawEllipse(QRectF(19.0, 55.0, 26.0, 6.0))

    # Стекло.
    glass = QLinearGradient(QPointF(18.0, 20.0), QPointF(48.0, 58.0))
    glass.setColorAt(0.0, QColor(225, 240, 246, 90))
    glass.setColorAt(0.55, QColor(170, 200, 215, 35))
    glass.setColorAt(1.0, QColor(120, 155, 175, 55))
    painter.setBrush(glass)
    painter.setPen(QPen(QColor(225, 242, 250, 220), 1.6))
    painter.drawPath(body)

    # Жидкость: клипается телом склянки, чтобы не вылезала за стекло.
    painter.save()
    painter.setClipPath(body)
    liquid_path = QPainterPath()
    liquid_path.moveTo(14.0, 38.0)
    liquid_path.cubicTo(20.0, 33.0, 27.0, 37.0, 32.0, 35.0)
    liquid_path.cubicTo(39.0, 32.0, 45.0, 36.0, 50.0, 34.0)
    liquid_path.lineTo(50.0, 62.0)
    liquid_path.lineTo(14.0, 62.0)
    liquid_path.closeSubpath()
    painter.setPen(Qt.NoPen)
    painter.setBrush(liquid)
    painter.drawPath(liquid_path)

    # Блик на поверхности жидкости.
    painter.setBrush(QColor(255, 255, 255, 105))
    painter.drawEllipse(QRectF(20.0, 37.0, 18.0, 4.0))

    if stripes:
        painter.setPen(QPen(QColor(45, 38, 28, 190), 2.4))
        for offset in (-12.0, -2.0, 8.0, 18.0):
            painter.drawLine(QPointF(16.0 + offset, 60.0),
                             QPointF(28.0 + offset, 38.0))
        painter.setPen(QPen(QColor(255, 255, 255, 160), 1.1))
        painter.drawLine(QPointF(24.0, 44.0), QPointF(29.0, 36.0))
    elif _is_healing(rec):
        # Крестик на теле читается как «лечение» даже без подписи.
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 190))
        painter.drawRoundedRect(QRectF(29.5, 41.0, 5.0, 15.0), 1.2, 1.2)
        painter.drawRoundedRect(QRectF(24.5, 46.0, 15.0, 5.0), 1.2, 1.2)
    elif "яд" in _text(rec) or "poison" in _text(rec):
        # Маленький череп: яд не должен выглядеть как зелье лечения.
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(220, 245, 215, 235))
        painter.drawEllipse(QRectF(25.5, 42.0, 13.0, 11.0))
        painter.setBrush(QColor(54, 100, 52, 230))
        painter.drawEllipse(QRectF(28.0, 45.0, 3.0, 3.0))
        painter.drawEllipse(QRectF(33.0, 45.0, 3.0, 3.0))
    painter.restore()

    # Пузырьки ступени: чем сильнее зелье, тем больше искр.
    painter.setPen(Qt.NoPen)
    for index in range(bubbles):
        angle = -2.2 + index * 0.9
        x = 32.0 + 10.0 * math.cos(angle)
        y = 45.0 + 8.0 * math.sin(angle)
        painter.setBrush(QColor(shine.red(), shine.green(), shine.blue(), 200))
        painter.drawEllipse(QPointF(x, y), 1.6 + (index % 2) * 0.8,
                            1.6 + (index % 2) * 0.8)

    # Горлышко, пробка и ободок.
    painter.setPen(QPen(QColor(210, 230, 238, 210), 1.2))
    painter.setBrush(QColor(215, 230, 238, 120))
    painter.drawRoundedRect(QRectF(26.0, 6.5, 12.0, 8.0), 2.0, 2.0)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(150, 102, 58))
    painter.drawRoundedRect(QRectF(26.5, 3.0, 11.0, 6.0), 2.0, 2.0)
    painter.setBrush(liquid)
    painter.drawRoundedRect(QRectF(26.2, 21.0, 11.6, 3.0), 1.2, 1.2)

    # Стеклянный блик по левой стенке.
    painter.setPen(QPen(QColor(255, 255, 255, 150), 1.8))
    painter.setBrush(Qt.NoBrush)
    painter.drawArc(QRectF(20.0, 34.0, 15.0, 20.0), 120 * 16, 70 * 16)


def _draw_sword(painter: QPainter) -> None:
    painter.setPen(QPen(QColor(205, 220, 232), 3.0))
    painter.drawLine(QPointF(19.0, 51.0), QPointF(44.0, 20.0))
    painter.setPen(QPen(QColor(120, 160, 198), 5.0))
    painter.drawLine(QPointF(17.0, 49.0), QPointF(22.0, 54.0))
    painter.setPen(QPen(QColor(170, 120, 70), 4.0))
    painter.drawLine(QPointF(15.0, 55.0), QPointF(22.0, 48.0))
    painter.setPen(QPen(QColor(220, 190, 130), 2.0))
    painter.drawLine(QPointF(25.0, 43.0), QPointF(31.0, 49.0))


def _draw_shield(painter: QPainter) -> None:
    path = QPainterPath()
    path.moveTo(32.0, 12.0)
    path.lineTo(48.0, 18.0)
    path.lineTo(46.0, 38.0)
    path.cubicTo(44.0, 48.0, 37.0, 54.0, 32.0, 57.0)
    path.cubicTo(27.0, 54.0, 20.0, 48.0, 18.0, 38.0)
    path.lineTo(16.0, 18.0)
    path.closeSubpath()
    painter.setPen(QPen(QColor(210, 220, 230), 2.0))
    painter.setBrush(QColor(110, 132, 156, 210))
    painter.drawPath(path)
    painter.setPen(QPen(QColor(225, 195, 120), 2.0))
    painter.drawLine(QPointF(32.0, 20.0), QPointF(32.0, 49.0))
    painter.drawLine(QPointF(22.0, 32.0), QPointF(42.0, 32.0))


def _draw_scroll(painter: QPainter) -> None:
    painter.setPen(QPen(QColor(205, 185, 140), 2.0))
    painter.setBrush(QColor(238, 225, 190))
    painter.drawRoundedRect(QRectF(17.0, 14.0, 30.0, 36.0), 3.0, 3.0)
    painter.setBrush(QColor(170, 130, 80))
    painter.drawRoundedRect(QRectF(13.5, 12.0, 7.0, 40.0), 3.0, 3.0)
    painter.drawRoundedRect(QRectF(43.5, 12.0, 7.0, 40.0), 3.0, 3.0)
    painter.setPen(QPen(QColor(120, 100, 75), 1.4))
    for y in (23.0, 30.0, 37.0, 44.0):
        painter.drawLine(QPointF(22.0, y), QPointF(42.0, y))


def _draw_pouch(painter: QPainter) -> None:
    path = QPainterPath()
    path.moveTo(24.0, 22.0)
    path.cubicTo(16.0, 30.0, 16.0, 44.0, 22.0, 52.0)
    path.cubicTo(28.0, 59.0, 38.0, 59.0, 43.0, 52.0)
    path.cubicTo(49.0, 44.0, 49.0, 30.0, 41.0, 22.0)
    path.closeSubpath()
    painter.setPen(QPen(QColor(190, 160, 120), 2.0))
    painter.setBrush(QColor(160, 120, 82, 220))
    painter.drawPath(path)
    painter.setPen(QPen(QColor(225, 195, 140), 2.0))
    painter.drawLine(QPointF(25.0, 25.0), QPointF(41.0, 25.0))
    painter.setBrush(QColor(205, 165, 95))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(QPointF(33.0, 36.0), 3.0, 3.0)


STEEL = QColor(198, 210, 222)
STEEL_DARK = QColor(120, 145, 170)
WOOD = QColor(132, 90, 54)
WOOD_LIGHT = QColor(174, 125, 78)
GOLD = QColor(222, 184, 104)
GOLD_DARK = QColor(156, 116, 52)
LEATHER = QColor(150, 105, 66)


def _draw_axe(painter: QPainter) -> None:
    painter.setPen(QPen(WOOD, 4.0))
    painter.drawLine(QPointF(19.0, 54.0), QPointF(43.0, 16.0))
    blade = QPainterPath()
    blade.moveTo(37.0, 13.0)
    blade.cubicTo(48.0, 15.0, 54.0, 23.0, 52.0, 34.0)
    blade.cubicTo(44.0, 31.0, 38.0, 25.0, 35.0, 18.0)
    blade.closeSubpath()
    painter.setPen(QPen(STEEL, 1.6))
    painter.setBrush(QColor(174, 190, 206, 230))
    painter.drawPath(blade)
    painter.setPen(QPen(QColor(255, 255, 255, 160), 1.2))
    painter.drawLine(QPointF(42.0, 18.0), QPointF(49.0, 29.0))


def _draw_mace(painter: QPainter) -> None:
    painter.setPen(QPen(WOOD, 4.0))
    painter.drawLine(QPointF(22.0, 55.0), QPointF(37.0, 22.0))
    painter.setPen(QPen(STEEL, 1.5))
    painter.setBrush(QColor(150, 168, 188, 235))
    painter.drawEllipse(QPointF(42.0, 15.0), 9.0, 9.0)
    for angle in range(0, 360, 45):
        rad = math.radians(angle)
        painter.drawLine(QPointF(42.0 + 7.0 * math.cos(rad),
                                 15.0 + 7.0 * math.sin(rad)),
                         QPointF(42.0 + 12.0 * math.cos(rad),
                                 15.0 + 12.0 * math.sin(rad)))
    painter.setPen(QPen(GOLD, 2.0))
    painter.setBrush(Qt.NoBrush)
    painter.drawEllipse(QPointF(42.0, 15.0), 4.0, 4.0)


def _draw_polearm(painter: QPainter) -> None:
    painter.setPen(QPen(WOOD, 3.6))
    painter.drawLine(QPointF(18.0, 57.0), QPointF(45.0, 8.0))
    tip = QPainterPath()
    tip.moveTo(45.0, 5.0)
    tip.lineTo(51.0, 18.0)
    tip.lineTo(44.0, 15.0)
    tip.lineTo(39.0, 20.0)
    tip.closeSubpath()
    painter.setPen(QPen(STEEL, 1.4))
    painter.setBrush(STEEL_DARK)
    painter.drawPath(tip)
    painter.setPen(QPen(STEEL, 2.0))
    painter.drawLine(QPointF(35.0, 24.0), QPointF(44.0, 28.0))


def _draw_dagger(painter: QPainter) -> None:
    painter.setPen(QPen(STEEL, 4.0))
    painter.drawLine(QPointF(28.0, 43.0), QPointF(44.0, 20.0))
    painter.setPen(QPen(QColor(255, 255, 255, 160), 1.2))
    painter.drawLine(QPointF(31.0, 39.0), QPointF(41.0, 24.0))
    painter.setPen(QPen(GOLD, 3.4))
    painter.drawLine(QPointF(24.0, 46.0), QPointF(33.0, 38.0))
    painter.setPen(QPen(WOOD, 4.0))
    painter.drawLine(QPointF(23.0, 48.0), QPointF(18.0, 55.0))


def _draw_bow(painter: QPainter) -> None:
    path = QPainterPath()
    path.moveTo(22.0, 8.0)
    path.cubicTo(46.0, 16.0, 46.0, 48.0, 22.0, 56.0)
    painter.setPen(QPen(WOOD_LIGHT, 3.2))
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(path)
    painter.setPen(QPen(QColor(235, 225, 200, 220), 1.2))
    painter.drawLine(QPointF(22.0, 8.0), QPointF(22.0, 56.0))
    painter.setPen(QPen(STEEL, 2.0))
    painter.drawLine(QPointF(18.0, 32.0), QPointF(48.0, 32.0))
    arrow = QPainterPath()
    arrow.moveTo(48.0, 32.0)
    arrow.lineTo(42.0, 28.5)
    arrow.lineTo(42.0, 35.5)
    arrow.closeSubpath()
    painter.setBrush(STEEL)
    painter.setPen(Qt.NoPen)
    painter.drawPath(arrow)


def _draw_crossbow(painter: QPainter) -> None:
    painter.setPen(QPen(WOOD, 4.2))
    painter.drawLine(QPointF(16.0, 48.0), QPointF(45.0, 30.0))
    limbs = QPainterPath()
    limbs.moveTo(23.0, 40.0)
    limbs.cubicTo(17.0, 23.0, 31.0, 12.0, 43.0, 19.0)
    painter.setPen(QPen(STEEL_DARK, 3.0))
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(limbs)
    painter.setPen(QPen(QColor(235, 225, 200, 220), 1.0))
    painter.drawLine(QPointF(23.0, 40.0), QPointF(43.0, 19.0))
    painter.setPen(QPen(STEEL, 2.0))
    painter.drawLine(QPointF(30.0, 34.0), QPointF(50.0, 22.0))


def _draw_ammo(painter: QPainter) -> None:
    painter.setPen(QPen(WOOD_LIGHT, 2.2))
    for offset in (-8.0, 0.0, 8.0):
        painter.drawLine(QPointF(21.0 + offset, 53.0),
                         QPointF(36.0 + offset, 15.0))
        painter.setPen(QPen(STEEL, 2.2))
        painter.drawLine(QPointF(33.0 + offset, 21.0),
                         QPointF(38.0 + offset, 11.0))
        painter.drawLine(QPointF(38.0 + offset, 11.0),
                         QPointF(36.0 + offset, 17.0))
        painter.setPen(QPen(QColor(205, 195, 175), 2.0))
        painter.drawLine(QPointF(23.0 + offset, 47.0),
                         QPointF(29.0 + offset, 42.0))
        painter.setPen(QPen(WOOD_LIGHT, 2.2))


def _draw_staff(painter: QPainter) -> None:
    painter.setPen(QPen(WOOD, 4.0))
    painter.drawLine(QPointF(20.0, 57.0), QPointF(43.0, 18.0))
    crystal = QPainterPath()
    crystal.moveTo(43.0, 5.0)
    crystal.lineTo(50.0, 16.0)
    crystal.lineTo(43.0, 27.0)
    crystal.lineTo(36.0, 16.0)
    crystal.closeSubpath()
    painter.setPen(QPen(QColor(190, 226, 255), 1.4))
    painter.setBrush(QColor(116, 176, 226, 220))
    painter.drawPath(crystal)
    painter.setPen(QPen(QColor(255, 255, 255, 150), 1.0))
    painter.drawLine(QPointF(41.0, 10.0), QPointF(45.0, 18.0))


def _draw_armor(painter: QPainter, rec: dict = None) -> None:
    text = _text(rec or {})
    if "тяж" in text or "heavy" in text or "кольч" in text:
        body = QColor(146, 158, 176, 235)
    elif "средн" in text or "medium" in text:
        body = QColor(126, 144, 158, 235)
    else:
        body = QColor(166, 128, 82, 235)
    path = QPainterPath()
    path.moveTo(19.0, 20.0)
    path.lineTo(27.0, 15.0)
    path.cubicTo(30.0, 20.0, 34.0, 20.0, 37.0, 15.0)
    path.lineTo(45.0, 20.0)
    path.lineTo(49.0, 40.0)
    path.lineTo(41.0, 52.0)
    path.lineTo(23.0, 52.0)
    path.lineTo(15.0, 40.0)
    path.closeSubpath()
    painter.setPen(QPen(QColor(224, 234, 242), 1.6))
    painter.setBrush(body)
    painter.drawPath(path)
    painter.setPen(QPen(QColor(62, 72, 84, 190), 1.2))
    painter.drawLine(QPointF(32.0, 20.0), QPointF(32.0, 49.0))
    painter.drawLine(QPointF(20.0, 36.0), QPointF(44.0, 36.0))


def _draw_clothing(painter: QPainter) -> None:
    path = QPainterPath()
    path.moveTo(24.0, 15.0)
    path.lineTo(30.0, 18.0)
    path.lineTo(34.0, 18.0)
    path.lineTo(40.0, 15.0)
    path.lineTo(48.0, 28.0)
    path.lineTo(42.0, 33.0)
    path.lineTo(42.0, 54.0)
    path.lineTo(22.0, 54.0)
    path.lineTo(22.0, 33.0)
    path.lineTo(16.0, 28.0)
    path.closeSubpath()
    painter.setPen(QPen(QColor(170, 150, 120), 1.5))
    painter.setBrush(QColor(92, 120, 148, 225))
    painter.drawPath(path)
    painter.setPen(QPen(QColor(220, 210, 188, 190), 1.1))
    painter.drawLine(QPointF(32.0, 18.0), QPointF(32.0, 53.0))


def _draw_ring(painter: QPainter) -> None:
    painter.setPen(QPen(GOLD, 4.0))
    painter.setBrush(Qt.NoBrush)
    painter.drawEllipse(QPointF(32.0, 36.0), 12.0, 14.0)
    gem = QPainterPath()
    gem.moveTo(32.0, 13.0)
    gem.lineTo(39.0, 20.0)
    gem.lineTo(32.0, 28.0)
    gem.lineTo(25.0, 20.0)
    gem.closeSubpath()
    painter.setPen(QPen(QColor(210, 240, 255), 1.0))
    painter.setBrush(QColor(112, 190, 230, 235))
    painter.drawPath(gem)


def _draw_amulet(painter: QPainter) -> None:
    painter.setPen(QPen(GOLD, 2.0))
    painter.setBrush(Qt.NoBrush)
    painter.drawArc(QRectF(12.0, 8.0, 40.0, 40.0), 200 * 16, 140 * 16)
    gem = QPainterPath()
    gem.moveTo(32.0, 31.0)
    gem.lineTo(42.0, 42.0)
    gem.lineTo(32.0, 55.0)
    gem.lineTo(22.0, 42.0)
    gem.closeSubpath()
    painter.setPen(QPen(QColor(214, 244, 255), 1.2))
    painter.setBrush(QColor(118, 168, 214, 235))
    painter.drawPath(gem)
    painter.setPen(QPen(QColor(255, 255, 255, 150), 1.0))
    painter.drawLine(QPointF(32.0, 35.0), QPointF(36.0, 42.0))


def _draw_wand(painter: QPainter) -> None:
    painter.setPen(QPen(WOOD_LIGHT, 4.0))
    painter.drawLine(QPointF(18.0, 54.0), QPointF(42.0, 23.0))
    painter.setPen(QPen(GOLD, 2.0))
    painter.drawLine(QPointF(21.0, 50.0), QPointF(28.0, 41.0))
    _star(painter, 47.0, 16.0, 9.0, QColor(255, 226, 140))


def _draw_rod(painter: QPainter) -> None:
    painter.setPen(QPen(STEEL_DARK, 5.0))
    painter.drawLine(QPointF(16.0, 52.0), QPointF(44.0, 16.0))
    painter.setPen(QPen(GOLD, 2.0))
    for t in (0.22, 0.55, 0.86):
        painter.drawLine(QPointF(16.0 + 28.0 * t, 52.0 - 36.0 * t),
                         QPointF(11.0 + 28.0 * t, 46.0 - 36.0 * t))
    _star(painter, 48.0, 12.0, 8.0, QColor(170, 214, 255))


def _draw_book(painter: QPainter) -> None:
    painter.setPen(QPen(QColor(120, 82, 58), 2.0))
    painter.setBrush(QColor(104, 66, 52, 235))
    painter.drawRoundedRect(QRectF(14.0, 12.0, 36.0, 44.0), 3.0, 3.0)
    painter.setBrush(QColor(236, 224, 196))
    painter.setPen(QPen(QColor(180, 164, 138), 1.0))
    painter.drawRoundedRect(QRectF(20.0, 16.0, 26.0, 36.0), 1.5, 1.5)
    painter.setPen(QPen(GOLD_DARK, 1.4))
    for y in (23.0, 30.0, 37.0, 44.0):
        painter.drawLine(QPointF(24.0, y), QPointF(42.0, y))
    painter.setPen(QPen(QColor(230, 200, 120), 2.0))
    painter.drawLine(QPointF(18.0, 14.0), QPointF(18.0, 54.0))


def _draw_tool(painter: QPainter) -> None:
    painter.setPen(QPen(STEEL, 3.6))
    painter.drawLine(QPointF(20.0, 48.0), QPointF(45.0, 20.0))
    painter.setPen(QPen(STEEL_DARK, 2.6))
    painter.setBrush(Qt.NoBrush)
    painter.drawEllipse(QPointF(47.0, 16.0), 6.0, 6.0)
    painter.setPen(QPen(WOOD, 5.0))
    painter.drawLine(QPointF(18.0, 48.0), QPointF(13.0, 54.0))
    painter.setPen(QPen(GOLD, 2.0))
    painter.drawLine(QPointF(32.0, 33.0), QPointF(39.0, 40.0))


def _draw_instrument(painter: QPainter) -> None:
    painter.setPen(QPen(WOOD_LIGHT, 3.4))
    painter.drawLine(QPointF(37.0, 14.0), QPointF(25.0, 40.0))
    painter.setPen(QPen(WOOD, 2.0))
    painter.setBrush(QColor(172, 118, 66, 235))
    painter.drawEllipse(QPointF(24.0, 44.0), 12.0, 9.0)
    painter.setBrush(QColor(58, 42, 32))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(QPointF(24.0, 44.0), 4.0, 4.0)
    painter.setPen(QPen(QColor(235, 220, 188), 1.0))
    for offset in (-1.0, 2.0, 5.0):
        painter.drawLine(QPointF(35.0 + offset, 18.0),
                         QPointF(32.0 + offset, 26.0))


def _draw_food(painter: QPainter) -> None:
    painter.setPen(QPen(QColor(114, 54, 36), 1.6))
    painter.setBrush(QColor(190, 62, 52, 235))
    painter.drawEllipse(QPointF(30.0, 38.0), 15.0, 14.0)
    painter.setPen(QPen(QColor(92, 132, 62), 2.4))
    painter.drawLine(QPointF(32.0, 25.0), QPointF(35.0, 15.0))
    leaf = QPainterPath()
    leaf.moveTo(35.0, 18.0)
    leaf.cubicTo(43.0, 12.0, 49.0, 17.0, 45.0, 23.0)
    leaf.cubicTo(40.0, 24.0, 37.0, 22.0, 35.0, 18.0)
    painter.setBrush(QColor(105, 154, 72, 220))
    painter.setPen(Qt.NoPen)
    painter.drawPath(leaf)


def _draw_drink(painter: QPainter) -> None:
    painter.setPen(QPen(QColor(210, 225, 235), 1.6))
    painter.setBrush(QColor(150, 195, 215, 145))
    painter.drawRoundedRect(QRectF(22.0, 16.0, 20.0, 36.0), 5.0, 5.0)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(90, 154, 196, 210))
    painter.drawRoundedRect(QRectF(24.0, 29.0, 16.0, 20.0), 3.0, 3.0)
    painter.setPen(QPen(GOLD_DARK, 2.0))
    painter.setBrush(QColor(186, 142, 78))
    painter.drawRoundedRect(QRectF(25.0, 10.0, 14.0, 8.0), 2.0, 2.0)
    painter.setPen(QPen(QColor(255, 255, 255, 160), 1.0))
    painter.drawLine(QPointF(27.0, 33.0), QPointF(27.0, 45.0))


def _draw_container(painter: QPainter) -> None:
    painter.setPen(QPen(QColor(105, 68, 42), 2.0))
    painter.setBrush(QColor(146, 96, 58, 235))
    painter.drawRoundedRect(QRectF(12.0, 24.0, 40.0, 28.0), 3.0, 3.0)
    painter.setPen(QPen(GOLD_DARK, 2.0))
    painter.setBrush(QColor(196, 150, 82))
    painter.drawRoundedRect(QRectF(12.0, 20.0, 40.0, 10.0), 3.0, 3.0)
    painter.setPen(QPen(QColor(245, 220, 160), 1.6))
    painter.drawRect(QRectF(29.0, 31.0, 6.0, 8.0))


def _draw_loot(painter: QPainter) -> None:
    painter.setPen(QPen(GOLD_DARK, 1.2))
    painter.setBrush(QColor(216, 178, 92, 235))
    painter.drawEllipse(QPointF(23.0, 44.0), 10.0, 5.0)
    painter.drawEllipse(QPointF(41.0, 44.0), 10.0, 5.0)
    gem = QPainterPath()
    gem.moveTo(32.0, 12.0)
    gem.lineTo(42.0, 22.0)
    gem.lineTo(36.0, 34.0)
    gem.lineTo(28.0, 34.0)
    gem.lineTo(22.0, 22.0)
    gem.closeSubpath()
    painter.setPen(QPen(QColor(230, 250, 255), 1.2))
    painter.setBrush(QColor(106, 196, 206, 230))
    painter.drawPath(gem)
    painter.setPen(QPen(QColor(255, 255, 255, 150), 1.0))
    painter.drawLine(QPointF(27.0, 20.0), QPointF(32.0, 16.0))


def _draw_focus(painter: QPainter) -> None:
    painter.setPen(QPen(GOLD_DARK, 2.4))
    painter.setBrush(Qt.NoBrush)
    painter.drawArc(QRectF(16.0, 22.0, 32.0, 32.0), 0, 180 * 16)
    painter.setPen(QPen(QColor(210, 236, 255), 1.4))
    painter.setBrush(QColor(106, 156, 222, 225))
    painter.drawEllipse(QPointF(32.0, 26.0), 11.0, 11.0)
    painter.setPen(QPen(QColor(255, 255, 255, 170), 1.2))
    painter.drawArc(QRectF(26.0, 20.0, 9.0, 9.0), 90 * 16, 90 * 16)


def _draw_torch(painter: QPainter) -> None:
    painter.setPen(QPen(WOOD, 4.0))
    painter.drawLine(QPointF(32.0, 55.0), QPointF(32.0, 26.0))
    flame = QPainterPath()
    flame.moveTo(32.0, 5.0)
    flame.cubicTo(44.0, 17.0, 42.0, 28.0, 32.0, 31.0)
    flame.cubicTo(22.0, 27.0, 21.0, 16.0, 32.0, 5.0)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(242, 156, 52, 235))
    painter.drawPath(flame)
    painter.setBrush(QColor(255, 232, 150, 230))
    painter.drawEllipse(QPointF(32.0, 20.0), 6.0, 9.0)


def _draw_rope(painter: QPainter) -> None:
    painter.setPen(QPen(QColor(190, 158, 104), 3.0))
    painter.setBrush(Qt.NoBrush)
    for radius in (18.0, 12.0, 6.0):
        painter.drawEllipse(QPointF(32.0, 36.0), radius, radius * 0.65)
    painter.setPen(QPen(QColor(150, 116, 70), 1.2))
    painter.drawLine(QPointF(32.0, 42.0), QPointF(51.0, 54.0))


def _draw_key(painter: QPainter) -> None:
    painter.setPen(QPen(GOLD, 3.4))
    painter.setBrush(Qt.NoBrush)
    painter.drawEllipse(QPointF(22.0, 24.0), 9.0, 9.0)
    painter.drawLine(QPointF(30.0, 31.0), QPointF(47.0, 48.0))
    painter.drawLine(QPointF(40.0, 41.0), QPointF(34.0, 47.0))
    painter.drawLine(QPointF(44.0, 45.0), QPointF(38.0, 51.0))


def _draw_boot(painter: QPainter) -> None:
    path = QPainterPath()
    path.moveTo(24.0, 12.0)
    path.lineTo(38.0, 12.0)
    path.lineTo(38.0, 37.0)
    path.lineTo(51.0, 44.0)
    path.cubicTo(53.0, 49.0, 49.0, 52.0, 44.0, 52.0)
    path.lineTo(24.0, 52.0)
    path.closeSubpath()
    painter.setPen(QPen(QColor(110, 72, 44), 1.6))
    painter.setBrush(LEATHER)
    painter.drawPath(path)
    painter.setPen(QPen(GOLD_DARK, 1.2))
    painter.drawLine(QPointF(25.0, 27.0), QPointF(37.0, 27.0))


def _draw_cloak(painter: QPainter) -> None:
    path = QPainterPath()
    path.moveTo(27.0, 10.0)
    path.cubicTo(37.0, 8.0, 40.0, 14.0, 43.0, 22.0)
    path.cubicTo(50.0, 37.0, 51.0, 49.0, 53.0, 55.0)
    path.lineTo(36.0, 55.0)
    path.lineTo(32.0, 21.0)
    path.lineTo(28.0, 55.0)
    path.lineTo(11.0, 55.0)
    path.cubicTo(14.0, 44.0, 17.0, 29.0, 21.0, 18.0)
    path.cubicTo(23.0, 13.0, 24.0, 11.0, 27.0, 10.0)
    painter.setPen(QPen(QColor(64, 84, 112), 1.6))
    painter.setBrush(QColor(52, 70, 108, 230))
    painter.drawPath(path)
    painter.setPen(QPen(GOLD, 1.6))
    painter.drawEllipse(QPointF(32.0, 14.0), 3.0, 3.0)


def _draw_gem(painter: QPainter) -> None:
    gem = QPainterPath()
    gem.moveTo(32.0, 9.0)
    gem.lineTo(50.0, 24.0)
    gem.lineTo(42.0, 52.0)
    gem.lineTo(22.0, 52.0)
    gem.lineTo(14.0, 24.0)
    gem.closeSubpath()
    painter.setPen(QPen(QColor(210, 240, 255), 1.4))
    painter.setBrush(QColor(126, 166, 214, 230))
    painter.drawPath(gem)
    painter.setPen(QPen(QColor(255, 255, 255, 120), 1.2))
    painter.drawLine(QPointF(32.0, 9.0), QPointF(32.0, 52.0))
    painter.drawLine(QPointF(14.0, 24.0), QPointF(50.0, 24.0))


def _draw_rune(painter: QPainter) -> None:
    painter.setPen(QPen(QColor(190, 160, 235), 2.0))
    painter.setBrush(QColor(54, 42, 74, 190))
    painter.drawEllipse(QPointF(32.0, 32.0), 20.0, 20.0)
    painter.setPen(QPen(QColor(226, 206, 255), 2.0))
    painter.drawLine(QPointF(32.0, 15.0), QPointF(32.0, 49.0))
    painter.drawLine(QPointF(32.0, 24.0), QPointF(42.0, 32.0))
    painter.drawLine(QPointF(32.0, 24.0), QPointF(22.0, 32.0))


def _star(painter: QPainter, x: float, y: float, radius: float,
          color: QColor) -> None:
    path = QPainterPath()
    path.moveTo(x, y - radius)
    path.lineTo(x + radius * 0.28, y - radius * 0.28)
    path.lineTo(x + radius, y)
    path.lineTo(x + radius * 0.28, y + radius * 0.28)
    path.lineTo(x, y + radius)
    path.lineTo(x - radius * 0.28, y + radius * 0.28)
    path.lineTo(x - radius, y)
    path.lineTo(x - radius * 0.28, y - radius * 0.28)
    path.closeSubpath()
    painter.setPen(Qt.NoPen)
    painter.setBrush(color)
    painter.drawPath(path)


def _is_magical(rec: dict) -> bool:
    rec = rec or {}
    props = " ".join(str(p) for p in (rec.get("properties") or [])).casefold()
    rarity = str(rec.get("rarity") or "").casefold()
    magical_rarity = bool(rarity) and "обычн" not in rarity and "common" not in rarity
    return magical_rarity or "магическ" in props or "magical" in props


def _rarity_color(rec: dict) -> QColor:
    rarity = str((rec or {}).get("rarity") or "").casefold()
    if "артефакт" in rarity or "artifact" in rarity:
        return QColor(238, 112, 90)
    if "легендар" in rarity or "legendary" in rarity:
        return QColor(246, 172, 72)
    if "очень ред" in rarity or "very rare" in rarity:
        return QColor(182, 132, 240)
    if "редк" in rarity or "rare" in rarity:
        return QColor(104, 166, 238)
    if "необыч" in rarity or "uncommon" in rarity:
        return QColor(118, 202, 136)
    return QColor(226, 194, 104)


def _draw_magic_mark(painter: QPainter, rec: dict) -> None:
    if not _is_magical(rec):
        return
    _star(painter, 52.0, 12.0, 6.0, _rarity_color(rec))
    painter.setPen(QPen(QColor(255, 255, 255, 150), 0.8))
    painter.drawLine(QPointF(52.0, 8.0), QPointF(52.0, 16.0))


def _weapon_kind(rec: dict) -> str:
    text = _text(rec)
    if "арбалет" in text or "crossbow" in text:
        return "crossbow"
    if "лук" in text or "bow" in text:
        return "bow"
    if ("стрел" in text or "болт" in text or "arrow" in text
            or "bolt" in text or "снаряд" in text):
        return "ammo"
    if "топор" in text or "axe" in text or "секир" in text:
        return "axe"
    if ("молот" in text or "булав" in text or "палиц" in text
            or "дубин" in text or "mace" in text or "hammer" in text
            or "club" in text):
        return "mace"
    if ("копь" in text or "spear" in text or "пик" in text
            or "pike" in text or "алебард" in text or "halberd" in text
            or "глеф" in text or "glaive" in text or "трезуб" in text
            or "trident" in text or "рогатин" in text):
        return "polearm"
    if ("кинжал" in text or "dagger" in text or "нож" in text
            or "knife" in text):
        return "dagger"
    if "посох" in text or "staff" in text or "quarterstaff" in text:
        return "staff"
    return "sword"


def _generic_kind(rec: dict) -> str:
    """Категория иконки для любой записи справочника."""
    rec = rec or {}
    text = _text(rec)
    itype = str(rec.get("itype") or "")
    subtype = str(rec.get("subtype") or "")
    if _is_potion(rec) or subtype == "Яд":
        return "potion"
    if itype == "Оружие":
        return _weapon_kind(rec)
    if subtype == "Щит" or "щит" in text or "shield" in text:
        return "shield"
    if subtype in ("Лёгкий доспех", "Средний доспех", "Тяжёлый доспех"):
        return "armor"
    if subtype == "Одежда":
        return "clothing"
    if subtype == "Кольцо" or "кольц" in text or "ring" in text:
        return "ring"
    if ("амулет" in text or "медальон" in text or "ожерель" in text
            or "amulet" in text or "necklace" in text or "periapt" in text):
        return "amulet"
    if subtype == "Жезл" or "жезл" in text or "wand" in text:
        return "wand"
    if subtype == "Стержень" or "стерж" in text or "rod" in text:
        return "rod"
    if ("книг" in text or "book" in text or "том" in text
            or "tome" in text or "гримуар" in text or "grimoire" in text):
        return "book"
    if subtype == "Свиток" or "свит" in text or "scroll" in text:
        return "scroll"
    if subtype == "Боеприпасы" or "боеприпас" in text or "ammo" in text:
        return "ammo"
    if subtype == "Еда" or "еда" in text or "food" in text:
        return "food"
    if "бурдюк" in text or "фляг" in text or "бутыл" in text or "waterskin" in text:
        return "drink"
    if itype == "Контейнер" or "мешок" in text or "сундук" in text or "бочк" in text:
        return "container"
    if itype == "Инструмент":
        return "instrument" if "музык" in text or "instrument" in text else "tool"
    if itype == "Добыча":
        return "loot"
    if (subtype == "Фокусировка заклинаний" or "фокусиров" in text
            or "focus" in text):
        return "focus"
    if "сапог" in text or "boot" in text:
        return "boot"
    if ("плащ" in text or "cloak" in text or "cape" in text
            or "мантия" in text or "mantle" in text):
        return "cloak"
    if "верёв" in text or "верев" in text or "rope" in text:
        return "rope"
    if "факел" in text or "torch" in text:
        return "torch"
    if "ключ" in text or "key" in text:
        return "key"
    if itype in ("Эффект", "Категория", "Зачарование"):
        return "rune"
    if itype == "Снаряжение":
        return "gem"
    if str(rec.get("kind") or "") == "spell" or "spell" in text:
        return "scroll"
    return "pouch"


def item_icon(rec: dict, size: int = 44) -> QIcon:
    """Иконка записи предмета/заклинания; пустая для монстров."""
    rec = rec if isinstance(rec, dict) else {}
    if str(rec.get("kind") or "") == "monster":
        return QIcon()
    size = max(16, int(size))
    text = _text(rec) or "?"
    key = (text, str(rec.get("subtype") or ""), str(rec.get("itype") or ""),
           size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.scale(size / 64.0, size / 64.0)
    kind = _generic_kind(rec)
    if kind == "potion":
        _draw_potion(painter, rec)
    elif kind == "armor":
        _draw_armor(painter, rec)
    else:
        {"sword": _draw_sword, "shield": _draw_shield,
         "scroll": _draw_scroll, "pouch": _draw_pouch,
         "axe": _draw_axe, "mace": _draw_mace,
         "polearm": _draw_polearm, "dagger": _draw_dagger,
         "bow": _draw_bow, "crossbow": _draw_crossbow,
         "ammo": _draw_ammo, "staff": _draw_staff,
         "clothing": _draw_clothing, "ring": _draw_ring,
         "amulet": _draw_amulet, "wand": _draw_wand,
         "rod": _draw_rod, "book": _draw_book,
         "tool": _draw_tool, "instrument": _draw_instrument,
         "food": _draw_food, "drink": _draw_drink,
         "container": _draw_container, "loot": _draw_loot,
         "focus": _draw_focus, "torch": _draw_torch,
         "rope": _draw_rope, "key": _draw_key,
         "boot": _draw_boot, "cloak": _draw_cloak,
         "gem": _draw_gem, "rune": _draw_rune}[kind](painter)
    _draw_magic_mark(painter, rec)
    painter.end()
    icon = QIcon(pm)
    _CACHE[key] = icon
    return icon


def item_drag_pixmap(rec: dict, size: int = 56) -> QPixmap:
    """Картинка под курсором при перетаскивании предмета."""
    icon = item_icon(rec, size)
    return icon.pixmap(size, size) if not icon.isNull() else QPixmap()
