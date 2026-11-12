"""Слоты инвентаря: иконка, подпись и количество внутри одной ячейки.

Инвентарь — сетка, а не список строк. Поэтому ячейку рисуем сами:

* фон и рамка слота, выделение — бронзовое, без синей заливки и пунктира;
* иконка предмета сверху, количество — бейджем в углу иконки;
* подпись — отдельным антиквенным шрифтом (Georgia/DejaVu Serif),
  до трёх строк с переносом;
* длинное название сокращается многоточием только внутри своей ячейки,
  поэтому оно не вылезает на соседний слот и не обрезается рамкой.

Модуль не знает ни о листе, ни о сети: он читает запись предмета из
роли `ITEM_ROLE`, которую кладёт `InventoryTab`.
"""
from PyQt5.QtCore import QPointF, QRectF, QSize, Qt
from PyQt5.QtGui import (QBrush, QColor, QFont, QFontDatabase, QFontMetrics,
                         QIcon, QPainter, QPainterPath, QPen)
from PyQt5.QtWidgets import (QStyle, QStyledItemDelegate,
                             QStyleOptionViewItem)

from client.item_drag import ITEM_ROLE
from client.win_frame import theme_is_dark

#: Размер одной ячейки сетки. Достаточно для иконки 46 px и трёх строк
#: подписи; при этом в панель листа помещается несколько колонок.
INVENTORY_GRID = QSize(94, 110)

#: Бронзовый акцент проекта — тот же, что у выделения карточек.
ACCENT = QColor(176, 141, 79)

#: Специальные шрифты слотов. Первым идёт DejaVu (Linux/сборки), затем
#: Georgia — она есть в Windows. Если нет ни одного, остаётся serif/mono
#: из Fontconfig, а не случайный UI-шрифт.
LABEL_FAMILIES = ("DejaVu Serif", "Georgia", "Palatino Linotype",
                  "Times New Roman")
QTY_FAMILIES = ("DejaVu Sans Mono", "Consolas", "Courier New")


def _pick_font(families, style_hint) -> QFont:
    available = set(QFontDatabase().families())
    family = next((name for name in families if name in available), "")
    font = QFont(family) if family else QFont()
    font.setStyleHint(style_hint)
    return font


def _wrap_name(text, metrics: QFontMetrics, width: float,
               max_lines: int = 2) -> tuple:
    """Вернуть (строки, сколько символов названия реально видно)."""
    words = str(text or "?").replace("\n", " ").split()
    if not words:
        return ["?"], 1
    width = max(8.0, float(width))
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        if metrics.horizontalAdvance(candidate) <= width:
            current = candidate
            continue
        word_width = metrics.horizontalAdvance(word)
        if current:
            # Слово целиком помещается на новой строке — переносим его,
            # а не режем ради остатка текущей.
            if word_width <= width:
                lines.append(current)
                current = word
                continue
            # Слово длиннее целой строки: текущую строку завершаем, а
            # длинное слово ставим отдельно с «…». Иначе подпись вида
            # «Зелье п…» + «лечения» теряла середину названия.
            lines.append(current)
            current = ""
        if word_width <= width:
            current = word
        else:
            lines.append(metrics.elidedText(word, Qt.ElideRight, int(width)))
    if current:
        lines.append(current)
    truncated = len(lines) > max_lines
    if truncated:
        lines = lines[:max_lines]
        last = lines[-1]
        marker = " …"
        while last and metrics.horizontalAdvance(last + marker) > width:
            last = last[:-1].rstrip()
        lines[-1] = (last + marker).strip() if last else "…"
    # Страховка: даже редкий шрифт не должен вернуть строку шире слота.
    result = []
    for line in lines:
        if metrics.horizontalAdvance(line) <= width:
            result.append(line)
        else:
            result.append(metrics.elidedText(line, Qt.ElideRight, int(width)))
    # Пробелы не считаем: иначе две строки «Зелье превосходного» + «лечения»
    # выигрывали у трёх полных строк только за счёт одного пробела и
    # выбирали неоправданно мелкий кегль.
    visible = sum(len("".join(line.replace("…", "").split()))
                  for line in result)
    return (result or ["?"]), visible


class InventorySlotDelegate(QStyledItemDelegate):
    """Нарисовать одну ячейку инвентаря как слот, а не строку списка."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._label_font = _pick_font(LABEL_FAMILIES, QFont.Serif)
        self._label_font.setStyleStrategy(QFont.PreferAntialias)
        self._qty_font = _pick_font(QTY_FAMILIES, QFont.Monospace)
        self._qty_font.setBold(True)
        self._qty_font.setStyleStrategy(QFont.PreferAntialias)

    def sizeHint(self, option, index):
        return QSize(INVENTORY_GRID)

    # ------------------------------------------------------------------
    # Геометрия: вынесена отдельно, чтобы тесты проверяли рамки без paint.
    # ------------------------------------------------------------------
    @staticmethod
    def cell_rect(rect) -> QRectF:
        return QRectF(rect).adjusted(2.0, 2.0, -2.0, -2.0)

    @staticmethod
    def icon_rect(rect) -> QRectF:
        cell = InventorySlotDelegate.cell_rect(rect)
        side = min(46.0, cell.width() - 16.0, cell.height() * 0.46)
        side = max(20.0, side)
        return QRectF(cell.center().x() - side / 2.0,
                      cell.top() + 6.0, side, side)

    @staticmethod
    def label_rect(rect) -> QRectF:
        cell = InventorySlotDelegate.cell_rect(rect)
        icon = InventorySlotDelegate.icon_rect(rect)
        return QRectF(cell.left() + 4.0, icon.bottom() + 4.0,
                      max(8.0, cell.width() - 8.0),
                      max(8.0, cell.bottom() - icon.bottom() - 8.0))

    def fitted_label(self, name: str, rect) -> tuple:
        """Подобрать шрифт и строки так, чтобы подпись влезла в ячейку."""
        label = self.label_rect(rect)
        font = QFont(self._label_font)
        best = None
        best_score = -1
        for point in (8.6, 8.2, 7.8, 7.4, 7.0, 6.6, 6.2):
            font.setPointSizeF(point)
            metrics = QFontMetrics(font)
            lines, visible = _wrap_name(name, metrics, label.width(), 3)
            total = metrics.height() * len(lines)
            if total > label.height() + 1.0:
                continue
            # Сначала показываем больше букв названия, при равенстве —
            # более крупным шрифтом. Три короткие строки лучше двух, где
            # середина длинного слова спрятана под «…».
            score = visible * 100 + point
            if score > best_score:
                best_score = score
                best = (QFont(font), metrics, lines)
        if best is not None:
            return best
        font.setPointSizeF(6.2)
        metrics = QFontMetrics(font)
        return font, metrics, _wrap_name(name, metrics, label.width(), 3)[0]

    @staticmethod
    def is_dark(option) -> bool:
        """Тема берётся у окна, а не у палитры: QSS не меняет QPalette."""
        return theme_is_dark(option.widget, option.palette)

    @staticmethod
    def paint_empty_slot(painter, rect: QRectF, dark: bool,
                         hovered: bool = False) -> None:
        """Пустая ячейка сетки: углубление без иконки и подписи.

        Рисуется тем же радиусом и рамкой, что и занятый слот, но тише:
        сетка должна читаться, не споря с предметами. Под курсором слот
        светлеет — видно, что клик сюда снимает выделение.
        """
        cell = InventorySlotDelegate.cell_rect(rect)
        if hovered:
            # Не бронза: так подсветка не спорит с выделенным предметом.
            fill = (QColor(34, 32, 36, 210) if dark
                    else QColor(252, 248, 240, 225))
            border = QColor(ACCENT)
            border.setAlpha(115 if dark else 105)
        else:
            fill = (QColor(17, 16, 19, 170) if dark
                    else QColor(240, 235, 225, 180))
            border = (QColor(64, 60, 54, 170) if dark
                      else QColor(198, 186, 164, 175))
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(border, 1.3 if hovered else 1.0))
        painter.setBrush(QBrush(fill))
        painter.drawRoundedRect(cell, 6.0, 6.0)
        # Слабый блик по верхней грани: пустой слот выглядит вдавленным.
        painter.setPen(QPen(QColor(255, 255, 255, 12) if dark
                            else QColor(255, 255, 255, 120), 1.0))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(cell.adjusted(1.2, 1.2, -1.2, -1.2),
                                5.0, 5.0)
        painter.restore()

    @staticmethod
    def drop_line_x(rect) -> float:
        """Где стоит линия вставки: в зазоре слева от ячейки-цели.

        Ячейка рисуется с отступом 2 px, поэтому между слотами остаётся
        4 px зазора. Линия стоит ровно в нём: предмет встаёт *между*
        слотами, а не «в левый край иконки». У первой ячейки ряда зазора
        нет — тогда линия смещается внутрь, но остаётся видимой.
        """
        cell = InventorySlotDelegate.cell_rect(rect)
        return max(1.5, cell.left() - 2.0)

    @staticmethod
    def paint_drop_target(painter, rect: QRectF, dark: bool) -> None:
        """Целевая ячейка при перетаскивании: подсветка + линия вставки.

        Подсветка говорит «предмет встанет здесь», линия — «вот в этот
        стык». Одно без другого читалось плохо: заливка без линии
        выглядела просто наведением, а линия без заливки терялась между
        слотами и на пустой сетке.
        """
        cell = InventorySlotDelegate.cell_rect(rect)
        fill = QColor(ACCENT)
        fill.setAlpha(64 if dark else 52)
        border = QColor(ACCENT)
        border.setAlpha(245 if dark else 225)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        # Мягкое свечение вокруг ячейки: цель видна и на светлой теме.
        halo = QColor(ACCENT)
        halo.setAlpha(58 if dark else 42)
        painter.setPen(QPen(halo, 5.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(cell, 7.0, 7.0)
        painter.setPen(QPen(border, 2.0))
        painter.setBrush(QBrush(fill))
        painter.drawRoundedRect(cell, 6.0, 6.0)
        # Внутренняя светлая грань — та же, что у обычного слота.
        painter.setPen(QPen(QColor(255, 255, 255, 34) if dark
                            else QColor(255, 255, 255, 170), 1.0))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(cell.adjusted(1.2, 1.2, -1.2, -1.2),
                                5.0, 5.0)
        # Линия вставки в зазоре: длинная, с круглыми концами и свечением.
        x = InventorySlotDelegate.drop_line_x(rect)
        top = cell.top() + 3.0
        bottom = cell.bottom() - 3.0
        glow = QColor(255, 226, 160)
        glow.setAlpha(110 if dark else 80)
        painter.setPen(QPen(glow, 6.0, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(QPointF(x, top), QPointF(x, bottom))
        painter.setPen(QPen(QColor(255, 240, 205) if dark
                            else QColor(118, 88, 38), 2.6,
                            Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(QPointF(x, top), QPointF(x, bottom))
        # Каретка у верхнего конца: даже поверх иконки видно, что это вставка.
        caret = QPainterPath()
        caret.moveTo(x + 1.4, top - 1.0)
        caret.lineTo(x + 6.4, top + 2.0)
        caret.lineTo(x + 1.4, top + 5.0)
        caret.closeSubpath()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 240, 205) if dark
                         else QColor(118, 88, 38))
        painter.drawPath(caret)
        painter.restore()

    def _draw_slot(self, painter, option, record):
        dark = self.is_dark(option)
        cell = self.cell_rect(option.rect)
        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)

        fill = QColor(28, 27, 31, 218) if dark else QColor(248, 244, 234, 235)
        border = QColor(78, 73, 65, 190) if dark else QColor(184, 170, 145, 200)
        if hover and not selected:
            fill = QColor(38, 36, 40, 230) if dark else QColor(255, 251, 240, 245)
            border = QColor(126, 108, 76, 220) if dark else QColor(160, 132, 82, 220)
        if selected:
            fill = QColor(ACCENT)
            fill.setAlpha(58 if dark else 42)
            border = QColor(ACCENT)
            border.setAlpha(205 if dark else 165)

        painter.setPen(QPen(border, 1.3))
        painter.setBrush(QBrush(fill))
        painter.drawRoundedRect(cell, 6.0, 6.0)

        # Внутренняя светлая грань: слот читается как углубление.
        highlight = QColor(255, 255, 255, 20) if dark else QColor(255, 255, 255, 160)
        painter.setPen(QPen(highlight, 1.0))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(cell.adjusted(1.2, 1.2, -1.2, -1.2), 5.0, 5.0)

        icon_rect = self.icon_rect(option.rect)
        icon = option.icon
        if isinstance(icon, QIcon) and not icon.isNull():
            pix = icon.pixmap(int(icon_rect.width()), int(icon_rect.height()))
            if not pix.isNull():
                painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
                painter.drawPixmap(icon_rect, pix, QRectF(pix.rect()))

        # Количество — отдельным моноширинным шрифтом, как счётчик стака.
        try:
            qty = int(record.get("qty", 1) or 1)
        except (TypeError, ValueError):
            qty = 1
        if qty > 1:
            self._draw_quantity(painter, icon_rect, qty, cell)
        if record.get("charges_max"):
            self._draw_charges(painter, icon_rect, record.get("charges", 0),
                               record.get("charges_max"), cell)

        mark = "◆" if record.get("equipped") else (
            "✦" if record.get("attuned") else "")
        if mark:
            self._draw_mark(painter, icon_rect, mark, bool(record.get("equipped")))

        name = str(record.get("name") or "?")
        font, metrics, lines = self.fitted_label(name, option.rect)
        painter.setFont(font)
        label = self.label_rect(option.rect)
        total = metrics.height() * len(lines)
        y = label.center().y() - total / 2.0 + metrics.ascent()
        for line in lines:
            width = metrics.horizontalAdvance(line)
            x = label.center().x() - width / 2.0
            # Тёмная обводка держит подпись читаемой на любой картинке.
            painter.setPen(QPen(QColor(0, 0, 0, 150), 1.0))
            painter.drawText(QPointF(x + 0.7, y + 0.7), line)
            painter.setPen(QPen(QColor(235, 228, 216) if dark
                                else QColor(42, 38, 32)))
            painter.drawText(QPointF(x, y), line)
            y += metrics.height()

    def quantity_rect(self, icon_rect, cell, qty) -> QRectF:
        """Бейдж количества: правый нижний угол иконки, но внутри слота."""
        text = f"×{qty}" if qty < 1000 else "×999+"
        font = QFont(self._qty_font)
        font.setPointSizeF(7.2)
        metrics = QFontMetrics(font)
        width = min(cell.width() - 6.0, metrics.horizontalAdvance(text) + 8.0)
        height = 14.0
        x = max(cell.left() + 4.0,
                min(icon_rect.right() - width + 3.0,
                    cell.right() - 4.0 - width))
        y = max(cell.top() + 4.0,
                min(icon_rect.bottom() - height + 3.0,
                    cell.bottom() - 4.0 - height))
        return QRectF(x, y, width, height)

    def _draw_quantity(self, painter, icon_rect, qty, cell):
        text = f"×{qty}" if qty < 1000 else "×999+"
        font = QFont(self._qty_font)
        font.setPointSizeF(7.2)
        rect = self.quantity_rect(icon_rect, cell, qty)
        painter.setPen(QPen(QColor(0, 0, 0, 180), 1.0))
        painter.setBrush(QBrush(QColor(18, 17, 20, 220)))
        painter.drawRoundedRect(rect, 4.0, 4.0)
        painter.setFont(font)
        painter.setPen(QPen(QColor(245, 224, 160)))
        painter.drawText(rect, Qt.AlignCenter, text)

    def _draw_charges(self, painter, icon_rect, charges, maximum, cell):
        """Синий бейдж зарядов в левом верхнем углу иконки.

        Количество показывает, сколько зарядов осталось из максимума;
        ноль остаётся видимым, чтобы предмет не выглядел «заряженным».
        """
        try:
            charges = max(0, int(charges))
            maximum = max(0, int(maximum))
        except (TypeError, ValueError):
            return
        text = str(charges)
        font = QFont(self._qty_font)
        font.setPointSizeF(6.8)
        font.setBold(True)
        metrics = QFontMetrics(font)
        width = min(cell.width() - 8.0, metrics.horizontalAdvance(text) + 15.0)
        rect = QRectF(icon_rect.left() + 1.0, icon_rect.top() - 1.0,
                      width, 14.0)
        if rect.right() > cell.right() - 2.0:
            rect.moveLeft(cell.right() - 2.0 - width)
        painter.setPen(QPen(QColor(88, 142, 220, 210), 1.0))
        painter.setBrush(QBrush(QColor(18, 24, 40, 225)))
        painter.drawRoundedRect(rect, 4.0, 4.0)
        # Молния вместо символа шрифта: её рисует сам Qt и она есть всегда.
        bolt = QPainterPath()
        bolt.moveTo(rect.left() + 5.0, rect.top() + 3.0)
        bolt.lineTo(rect.left() + 1.8, rect.top() + 8.5)
        bolt.lineTo(rect.left() + 5.0, rect.top() + 8.0)
        bolt.lineTo(rect.left() + 4.0, rect.top() + 12.0)
        bolt.lineTo(rect.left() + 8.0, rect.top() + 6.0)
        bolt.lineTo(rect.left() + 4.8, rect.top() + 6.5)
        bolt.closeSubpath()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(120, 182, 255) if charges else QColor(220, 120, 110))
        painter.drawPath(bolt)
        painter.setFont(font)
        painter.setPen(QColor(205, 226, 255) if charges
                       else QColor(255, 178, 166))
        painter.drawText(rect.adjusted(12.0, 0.0, 0.0, 0.0),
                         Qt.AlignCenter, text)

    def _draw_mark(self, painter, icon_rect, mark, equipped):
        font = QFont(self._label_font)
        font.setPointSizeF(9.0)
        font.setBold(True)
        painter.setFont(font)
        rect = QRectF(icon_rect.right() - 13.0, icon_rect.top() - 2.0,
                      14.0, 14.0)
        painter.setPen(QPen(QColor(0, 0, 0, 180), 1.0))
        painter.setBrush(QBrush(QColor(18, 17, 20, 205)))
        painter.drawEllipse(rect)
        painter.setPen(QPen(QColor(242, 204, 118) if equipped
                            else QColor(206, 174, 235)))
        painter.drawText(rect, Qt.AlignCenter, mark)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        # Слот рисуется целиком вручную, базовый делегат не вызывается.
        # Раньше State_Selected снимали здесь же — и `_draw_slot` видел
        # уже снятое состояние, поэтому выделение не рисовалось вовсе.
        opt.showDecorationSelected = False
        record = index.data(ITEM_ROLE)
        if not isinstance(record, dict):
            record = {"name": index.data(Qt.DisplayRole) or "?"}
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        # Даже при нестандартном шрифте всё остаётся внутри ячейки.
        painter.setClipRect(QRectF(option.rect).adjusted(1.0, 1.0, -1.0, -1.0))
        self._draw_slot(painter, opt, record)
        painter.restore()
