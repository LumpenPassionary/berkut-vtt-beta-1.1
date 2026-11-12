"""Аккуратное выделение карточек в «Моих актёрах» и инвентаре.

Штатный QListWidget в IconMode рисует синюю заливку вокруг картинки и
пунктирный прямоугольник вокруг текста. Для библиотеки актёров и сетки
предметов это выглядит чужеродно: выделена должна быть ячейка целиком,
а портрет и подпись остаются такими же, как в обычном состоянии.

Делегат снимает с элемента State_Selected и State_HasFocus, поэтому стиль
рисует картинку и текст без синевы и пунктира, а рамку/заливку ячейки
рисуем сами — мягким бронзовым акцентом текущей темы.
"""
from PyQt5.QtCore import QRectF
from PyQt5.QtGui import QBrush, QColor, QPainter, QPen
from PyQt5.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from client.win_frame import theme_is_dark


class CardSelectionDelegate(QStyledItemDelegate):
    """Подсветить всю ячейку, не трогая PNG и текст внутри неё."""

    #: Бронзовый акцент проекта. В светлой теме он темнее, в тёмной — светлее.
    ACCENT = QColor(176, 141, 79)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        selected = bool(opt.state & QStyle.State_Selected)
        hover = bool(opt.state & QStyle.State_MouseOver) and not selected
        # Снимаем штатное выделение и фокус: иначе Qt закрашивает иконку
        # синим и обводит текст пунктиром.
        opt.state &= ~(QStyle.State_Selected | QStyle.State_HasFocus)
        opt.showDecorationSelected = False

        if selected or hover:
            # Тема берётся у окна: QSS не меняет QPalette, поэтому по
            # палитре тёмная тема определялась как светлая и выделение
            # почти растворялось в фоне.
            dark = theme_is_dark(option.widget, option.palette)
            fill = QColor(self.ACCENT)
            if selected:
                fill.setAlpha(64 if dark else 44)
                border = QColor(self.ACCENT)
                border.setAlpha(205 if dark else 170)
                width = 1.6
            else:
                fill.setAlpha(22 if dark else 18)
                border = QColor(self.ACCENT)
                border.setAlpha(90 if dark else 80)
                width = 1.0

            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, True)
            rect = QRectF(option.rect).adjusted(1.0, 1.0, -1.0, -1.0)
            painter.setPen(QPen(border, width))
            painter.setBrush(QBrush(fill))
            painter.drawRoundedRect(rect, 5.0, 5.0)
            painter.restore()

        # Базовый делегат рисует портрет и подпись в обычном (не selected)
        # состоянии: без синей подложки и без пунктирной рамки.
        super().paint(painter, opt, index)
