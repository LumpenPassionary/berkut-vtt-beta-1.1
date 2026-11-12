"""Кнопочная панель кубиков — интерфейс стола до 3D-кубиков.

Так кубики выглядели в 2.9: восемь кнопок в две строки по четыре
(d20, d12, d100, d10, d6, d4, d3, d2). Нажатие — это обычный ``/roll``,
поэтому кнопки работают и у игрока, и у мастера, а результат считает
``shared.dice`` на сервере.

3D-лоток из ``dice_lab`` включается галочкой при запуске игры; кнопки
остаются режимом по умолчанию.
"""
import os

from PyQt5.QtCore import QSize, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QGridLayout, QPushButton, QWidget

ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "assets", "icons", "tools", "dice_d20.png")

#: Порядок как в 2.9: сначала крупные, потом мелкие.
DICE_LABELS = ("d20", "d12", "d100", "d10", "d6", "d4", "d3", "d2")


def dice_icon():
    """Иконка d20 из набора инструментов (та же, что у кнопки броска)."""
    return QIcon(ICON_PATH) if os.path.isfile(ICON_PATH) else QIcon()


class DiceButtons(QWidget):
    """Две строки кнопок кубиков; сигнал отдаёт команду без ``/roll``."""

    rollRequested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dice_buttons")
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(3)
        grid.setVerticalSpacing(3)
        self.buttons = {}
        for index, label in enumerate(DICE_LABELS):
            button = QPushButton(label.upper())
            button.setObjectName("dice_button")
            button.setToolTip(f"Бросить {label.upper()}")
            button.setMinimumHeight(26)
            button.clicked.connect(
                lambda _checked=False, cmd=label:
                self.rollRequested.emit(cmd))
            grid.addWidget(button, index // 4, index % 4)
            grid.setColumnStretch(index % 4, 1)
            self.buttons[label] = button

    def focus_first(self):
        """Поставить фокус на d20 — для иконки в верхней панели."""
        self.buttons["d20"].setFocus()

    def set_icon_size(self, size=18):
        for button in self.buttons.values():
            button.setIconSize(QSize(size, size))
