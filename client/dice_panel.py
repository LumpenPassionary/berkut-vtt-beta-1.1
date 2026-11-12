"""Компактная панель кубиков, встроенная в низ вкладки «Чат».

Внешнее окно здесь не нужно: лоток живёт прямо в чате, под строкой ввода.
На узкой панели (около 220 px) помещаются только компактные счётчики
D4–D20, формула и кнопка броска с иконкой d20. История показывает две
последние строки, чтобы не отнимать место у переписки.
"""
import os

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (QCheckBox, QGridLayout, QHBoxLayout, QLabel,
                             QLineEdit, QSlider, QToolButton, QVBoxLayout,
                             QWidget)

from shared import dice
from client.dice_window import (DIE_COLORS, DIE_ORDER, MAX_TRAY_DICE,
                                DiceBoard)

# Путь к присланной иконке d20; лежит рядом с остальными иконками панели.
ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "assets", "icons", "tools", "dice_d20.png")


def dice_icon():
    return QIcon(ICON_PATH) if os.path.isfile(ICON_PATH) else QIcon()


PANEL_QSS_DARK = """
QWidget#dice_panel { background: #1b1b1f; border-top: 1px solid #2c2c30; }
QToolButton#dice_toggle { background: transparent; border: none;
                          color: #b08d4f; font-weight: 600; padding: 1px 2px; }
QLabel#dice_count { color: #e8e5df; font-size: 8pt; }
QLabel#dice_history { color: #a9a59d; font-size: 8pt; }
QLabel#dice_error { color: #d08770; font-size: 8pt; }
QLineEdit#dice_formula { background: #131315; border: 1px solid #34343a;
                         border-radius: 4px; padding: 2px 4px;
                         color: #e4e2dc; font-size: 8pt; }
QToolButton#dice_mini { background: #26262a; border: 1px solid #3a3a40;
                        border-radius: 3px; color: #e4e2dc; font-size: 9pt; }
QToolButton#dice_mini:hover { background: #34343a; }
QToolButton#dice_roll { background: #d97a3f; border: none; border-radius: 5px; }
QToolButton#dice_roll:hover { background: #e58a4e; }
QToolButton#dice_roll:disabled { background: #4d3a2c; }
QToolButton#dice_clear { background: transparent; border: none;
                         color: #b08d4f; font-size: 10pt; }
QCheckBox { color: #cdc9c2; font-size: 8pt; }
QSlider::groove:horizontal { height: 3px; background: #303036;
                             border-radius: 2px; }
QSlider::handle:horizontal { background: #d97a3f; width: 10px;
                             margin: -4px 0; border-radius: 5px; }
"""

PANEL_QSS_LIGHT = """
QWidget#dice_panel { background: #f4f3ef; border-top: 1px solid #d0cec7; }
QToolButton#dice_toggle { background: transparent; border: none;
                          color: #85662a; font-weight: 600; padding: 1px 2px; }
QLabel#dice_count { color: #2a2a2c; font-size: 8pt; }
QLabel#dice_history { color: #6c6962; font-size: 8pt; }
QLabel#dice_error { color: #a4442f; font-size: 8pt; }
QLineEdit#dice_formula { background: #ffffff; border: 1px solid #cdcac2;
                         border-radius: 4px; padding: 2px 4px;
                         color: #2a2a2c; font-size: 8pt; }
QToolButton#dice_mini { background: #ffffff; border: 1px solid #cdcac2;
                        border-radius: 3px; color: #2a2a2c; font-size: 9pt; }
QToolButton#dice_mini:hover { background: #e4e2dc; }
QToolButton#dice_roll { background: #d97a3f; border: none; border-radius: 5px; }
QToolButton#dice_roll:hover { background: #e58a4e; }
QToolButton#dice_roll:disabled { background: #e0c6b2; }
QToolButton#dice_clear { background: transparent; border: none;
                         color: #85662a; font-size: 10pt; }
QCheckBox { color: #2a2a2c; font-size: 8pt; }
QSlider::groove:horizontal { height: 3px; background: #d0cec7;
                             border-radius: 2px; }
QSlider::handle:horizontal { background: #d97a3f; width: 10px;
                             margin: -4px 0; border-radius: 5px; }
"""


class DicePanel(QWidget):
    """Набор, лоток и бросок прямо в чате."""

    rolled = pyqtSignal(object)

    def __init__(self, parent=None, theme="dark"):
        super().__init__(parent)
        self.setObjectName("dice_panel")
        self.setMinimumWidth(190)
        self.current_theme = theme
        self.counts = {sides: 0 for sides in DIE_ORDER}
        self.counts[20] = 1
        self.modifier = 0
        self._history = []
        self._build()
        self.apply_theme(theme)
        self.sync_formula()

    # --- сборка -------------------------------------------------------
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 3, 6, 4)
        root.setSpacing(3)

        header = QHBoxLayout()
        header.setSpacing(4)
        self.toggle_button = QToolButton()
        self.toggle_button.setObjectName("dice_toggle")
        self.toggle_button.setText("Кубики")
        self.toggle_button.setIcon(dice_icon())
        self.toggle_button.setIconSize(QSize(18, 18))
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(True)
        self.toggle_button.setToolTip("Свернуть или развернуть кубики")
        self.toggle_button.toggled.connect(self._toggle_body)
        header.addWidget(self.toggle_button)
        header.addStretch(1)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("dice_count")
        header.addWidget(self.summary_label)
        root.addLayout(header)

        self.body = QWidget(self)
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(3)

        self.board = DiceBoard(self)
        self.board.setMinimumHeight(190)
        self.board.setMaximumHeight(210)
        self.board.settled.connect(self._on_settled)
        body.addWidget(self.board)

        # Шесть кубиков в две строки по три: на узкой панели иначе не
        # помещаются подписи и кнопки счётчика.
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(2)
        grid.setVerticalSpacing(2)
        self.count_labels = {}
        for index, sides in enumerate(DIE_ORDER):
            cell = QWidget()
            row = QHBoxLayout(cell)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(1)

            minus = QToolButton()
            minus.setObjectName("dice_mini")
            minus.setText("−")
            minus.setFixedSize(14, 17)
            minus.clicked.connect(lambda _, s=sides: self.change_count(s, -1))
            row.addWidget(minus)

            swatch = QLabel(f"D{sides}")
            swatch.setAlignment(Qt.AlignCenter)
            swatch.setFixedSize(23, 17)
            swatch.setStyleSheet(
                f"background:{DIE_COLORS[sides]}; color:white;"
                " border-radius:3px; font-size:7pt; font-weight:600;")
            row.addWidget(swatch)

            label = QLabel("0")
            label.setObjectName("dice_count")
            label.setAlignment(Qt.AlignCenter)
            label.setFixedWidth(10)
            self.count_labels[sides] = label
            row.addWidget(label)

            plus = QToolButton()
            plus.setObjectName("dice_mini")
            plus.setText("+")
            plus.setFixedSize(14, 17)
            plus.clicked.connect(lambda _, s=sides: self.change_count(s, 1))
            row.addWidget(plus)

            grid.addWidget(cell, index // 3, index % 3)
        body.addLayout(grid)

        modifier = QHBoxLayout()
        modifier.setSpacing(2)
        caption = QLabel("Мод")
        caption.setObjectName("dice_count")
        modifier.addWidget(caption)
        modifier.addStretch(1)
        minus = QToolButton()
        minus.setObjectName("dice_mini")
        minus.setText("−")
        minus.setFixedSize(14, 17)
        minus.clicked.connect(lambda: self.change_modifier(-1))
        modifier.addWidget(minus)
        self.modifier_label = QLabel("0")
        self.modifier_label.setObjectName("dice_count")
        self.modifier_label.setAlignment(Qt.AlignCenter)
        self.modifier_label.setFixedWidth(18)
        modifier.addWidget(self.modifier_label)
        plus = QToolButton()
        plus.setObjectName("dice_mini")
        plus.setText("+")
        plus.setFixedSize(14, 17)
        plus.clicked.connect(lambda: self.change_modifier(1))
        modifier.addWidget(plus)
        body.addLayout(modifier)

        formula_row = QHBoxLayout()
        formula_row.setSpacing(3)
        self.formula = QLineEdit()
        self.formula.setObjectName("dice_formula")
        self.formula.setPlaceholderText("2d6+3")
        self.formula.setFixedHeight(23)
        self.formula.returnPressed.connect(self.roll)
        formula_row.addWidget(self.formula, 1)
        self.roll_button = QToolButton()
        self.roll_button.setObjectName("dice_roll")
        self.roll_button.setIcon(dice_icon())
        self.roll_button.setIconSize(QSize(22, 22))
        self.roll_button.setFixedSize(28, 24)
        self.roll_button.setToolTip("Бросить кубики")
        self.roll_button.clicked.connect(self.roll)
        formula_row.addWidget(self.roll_button)
        body.addLayout(formula_row)

        self.error_label = QLabel("")
        self.error_label.setObjectName("dice_error")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        body.addWidget(self.error_label)

        sound_row = QHBoxLayout()
        sound_row.setSpacing(4)
        self.sound_box = QCheckBox("Звук")
        self.sound_box.setChecked(True)
        self.sound_box.toggled.connect(self._on_sound_toggled)
        sound_row.addWidget(self.sound_box)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(60)
        self.volume.setFixedHeight(16)
        self.volume.valueChanged.connect(self._on_volume)
        sound_row.addWidget(self.volume, 1)
        body.addLayout(sound_row)

        history_row = QHBoxLayout()
        history_row.setSpacing(2)
        history_caption = QLabel("История")
        history_caption.setObjectName("dice_count")
        history_row.addWidget(history_caption)
        history_row.addStretch(1)
        clear = QToolButton()
        clear.setObjectName("dice_clear")
        clear.setText("×")
        clear.setFixedSize(16, 16)
        clear.setToolTip("Очистить историю")
        clear.clicked.connect(self.clear_history)
        history_row.addWidget(clear)
        body.addLayout(history_row)

        self.history_label = QLabel("Пока пусто")
        self.history_label.setObjectName("dice_history")
        self.history_label.setWordWrap(True)
        self.history_label.setMaximumHeight(30)
        body.addWidget(self.history_label)

        root.addWidget(self.body)

    def _toggle_body(self, opened):
        self.body.setVisible(bool(opened))

    def apply_theme(self, theme):
        self.current_theme = "light" if theme == "light" else "dark"
        self.setStyleSheet(PANEL_QSS_LIGHT if self.current_theme == "light"
                           else PANEL_QSS_DARK)

    # --- набор --------------------------------------------------------
    def change_count(self, sides, delta):
        new_value = max(0, min(20, self.counts[sides] + delta))
        if delta > 0 and self.total_dice() >= MAX_TRAY_DICE:
            self._error(f"На лотке максимум {MAX_TRAY_DICE} кубиков")
            return
        self.counts[sides] = new_value
        self.count_labels[sides].setText(str(new_value))
        self.error_label.setVisible(False)
        self.sync_formula()

    def change_modifier(self, delta):
        self.modifier = max(-20, min(20, self.modifier + delta))
        self.modifier_label.setText(f"{self.modifier:+d}"
                                    if self.modifier else "0")
        self.sync_formula()

    def total_dice(self):
        return sum(self.counts.values())

    def formula_text(self):
        parts = []
        for sides in DIE_ORDER:
            count = self.counts[sides]
            if count:
                parts.append(f"d{sides}" if count == 1 else f"{count}d{sides}")
        formula = "+".join(parts)
        if self.modifier:
            sign = "+" if self.modifier > 0 else "-"
            formula += f"{sign}{abs(self.modifier)}"
        return formula

    def sync_formula(self):
        self.formula.setText(self.formula_text())
        total = self.total_dice()
        self.summary_label.setText(f"{total} куб." if total else "")

    def apply_formula(self):
        try:
            counts, modifier = self._read_formula(self.formula.text())
        except dice.DiceError as exc:
            self._error(str(exc))
            return False
        self.counts = counts
        self.modifier = modifier
        for sides in DIE_ORDER:
            self.count_labels[sides].setText(str(counts.get(sides, 0)))
        self.modifier_label.setText(f"{modifier:+d}" if modifier else "0")
        self.error_label.setVisible(False)
        self.sync_formula()
        return True

    def _read_formula(self, text):
        terms = dice.parse_formula(text)
        counts = {sides: 0 for sides in DIE_ORDER}
        modifier = 0
        for term in terms:
            if term[0] == "die":
                _, count, sides = term
                if sides not in counts:
                    raise dice.DiceError(
                        "В панели кубиков — только D4–D20. "
                        "Для d2/d3/d100 используйте /roll в чате")
                counts[sides] += count
            else:
                modifier += term[1]
        if sum(counts.values()) > MAX_TRAY_DICE:
            raise dice.DiceError(
                f"Слишком много кубиков: максимум {MAX_TRAY_DICE}")
        return counts, modifier

    def _error(self, text):
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))

    # --- бросок -------------------------------------------------------
    def roll(self):
        if not self.apply_formula():
            return
        if not self.total_dice():
            self._error("Добавьте хотя бы один кубик")
            return
        expression = self.formula.text()
        try:
            result = dice.roll(expression)
        except dice.DiceError as exc:
            self._error(str(exc))
            return
        self.error_label.setVisible(False)
        self.roll_button.setEnabled(False)
        self.board.animate(result)
        self.rolled.emit(result)
        self._add_history(result)

    def _on_settled(self):
        self.roll_button.setEnabled(True)

    def _add_history(self, result):
        self._history.insert(
            0, f"{result.get('formula', '?')} → {result.get('total', '?')}")
        del self._history[6:]
        self._refresh_history()

    def _refresh_history(self):
        if not self._history:
            self.history_label.setText("Пока пусто")
            return
        self.history_label.setText("\n".join(self._history[:2]))

    def clear_history(self):
        self._history.clear()
        self._refresh_history()

    # --- звук ---------------------------------------------------------
    def _on_sound_toggled(self, enabled):
        self.board._sound.enabled = bool(enabled)

    def _on_volume(self, value):
        self.board._sound.volume = value / 100.0
