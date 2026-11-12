"""Отдельное приложение «Кубики»: физический бросок без подкрутки.

Запуск из корня проекта:

    python -m dice_lab

Приложение не подключено к столу: это полигон, который позже можно
перенести в VTT. Результат всегда читается из физики, а не выбирается
заранее. Для преимущества и помехи бросаются два d20, а итог берётся
по правилу из `rules.py`.
"""
import os
import re
import sys
import time

from PyQt5.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (QApplication, QCheckBox, QComboBox, QFrame,
                             QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                             QListWidget, QMainWindow, QPushButton, QSlider,
                             QToolButton, QVBoxLayout, QWidget)

from .audio import DiceAudio
from .geometry import DIE_ORDER
from .physics import PHYSICS_STEP, ROLL_TIMEOUT_SECONDS, DicePhysics
from .render import DIE_COLORS, DiceView
from .rules import (CRIT_FAIL, CRIT_SUCCESS, MODE_ADVANTAGE,
                    MODE_DISADVANTAGE, MODE_HINTS, MODE_NORMAL, MODE_TITLES,
                    critical_kind, resolve_total)

MAX_DICE = 30
ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets')

QSS = """
QWidget { color: #e8e4dc; font-family: 'Segoe UI', Arial; }
QMainWindow, QWidget#root { background: #131418; }
QWidget#side { background: #1a1b20; border-left: 1px solid #2a2b31; }
QLabel#kicker { color: #b08d4f; font-size: 8pt; font-weight: 600;
                letter-spacing: 1px; }
QLabel#title { color: #f1ede5; font-size: 20pt; font-weight: 600; }
QLabel#muted { color: #98948c; font-size: 9pt; }
QLabel#status { color: #c9c4bb; font-size: 9pt; }
QLabel#status_error { color: #e08c72; font-size: 9pt; }
QLabel#total { color: #f5f1e8; font-size: 34pt; font-weight: 600; }
QLabel#count { color: #e8e4dc; font-size: 10pt; font-weight: 600; }
QLabel#section { color: #b08d4f; font-size: 8pt; font-weight: 600;
                 letter-spacing: 1px; }
QLineEdit, QComboBox { background: #101115; border: 1px solid #35363d;
                       border-radius: 5px; padding: 5px 7px;
                       color: #eeeae2; }
QLineEdit:focus, QComboBox:focus { border-color: #8a6a35; }
QComboBox:disabled, QLineEdit:disabled { color: #77736c; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView { background: #1a1b20; border: 1px solid #3a3b43;
                              color: #e8e4dc;
                              selection-background-color: #543b28; }
QToolButton { background: #25262c; border: 1px solid #3a3b43;
              border-radius: 4px; color: #e8e4dc; font-size: 11pt; }
QToolButton:hover { background: #33343c; }
QToolButton:disabled { background: #1d1e23; border-color: #2b2c33;
                       color: #5f5c56; }
QPushButton { background: #2a2b32; border: 1px solid #3d3e47;
              border-radius: 5px; padding: 6px 10px; color: #e8e4dc; }
QPushButton:hover { background: #363740; }
QPushButton:disabled { background: #1f2025; border-color: #2c2d34;
                       color: #66625c; }
QLabel#count:disabled { color: #6f6b64; }
QPushButton#roll { background: #d97a3f; border: none; border-radius: 6px;
                   padding: 10px 14px; color: white; font-weight: 600;
                   font-size: 11pt; }
QPushButton#roll:hover { background: #e58a4e; }
QPushButton#roll:disabled { background: #4d3a2c; color: #9b8b7e; }
QPushButton#rethrow { background: #543b28; border: 1px solid #8a5a35;
                      color: #f0c9a8; }
QPushButton#rethrow:hover { background: #68472f; }
QListWidget { background: #141519; border: 1px solid #2c2d34;
              border-radius: 5px; color: #cbc6be; }
QListWidget::item { padding: 5px 7px; border-bottom: 1px solid #23242a; }
QCheckBox { color: #c9c4bb; font-size: 9pt; }
QSlider::groove:horizontal { height: 3px; background: #303138;
                             border-radius: 2px; }
QSlider::handle:horizontal { background: #d97a3f; width: 10px;
                             margin: -4px 0; border-radius: 5px; }
"""


def dice_icon():
    path = os.path.join(ASSET_DIR, 'dice_d20.png')
    return QIcon(path) if os.path.isfile(path) else QIcon()


def parse_formula(text):
    """Разобрать 2d6+d20+3 в набор и модификатор (только D4–D20)."""
    compact = str(text).replace(' ', '').lower()
    if not compact:
        raise ValueError('Пустая формула. Например: 2d6+3')
    tokens = re.findall(r'[+-]?[^+-]+', compact)
    if ''.join(tokens) != compact:
        raise ValueError('Формула содержит лишние символы')
    counts = {sides: 0 for sides in DIE_ORDER}
    modifier = 0
    for token in tokens:
        sign = 1
        body = token
        if body[0] in '+-':
            sign = -1 if body[0] == '-' else 1
            body = body[1:]
        if not body:
            raise ValueError('Пустой член формулы')
        match = re.fullmatch(r'(\d*)d(\d+)', body)
        if match:
            count = int(match.group(1)) if match.group(1) else 1
            sides = int(match.group(2))
            if sides not in counts:
                raise ValueError('Поддерживаются только D4, D6, D8, D10, D12, D20')
            if not 1 <= count <= MAX_DICE:
                raise ValueError(f'Кубиков должно быть от 1 до {MAX_DICE}')
            counts[sides] += count * sign
            if counts[sides] < 0:
                raise ValueError('Отрицательное число кубиков')
        elif re.fullmatch(r'\d+', body):
            modifier += sign * int(body)
        else:
            raise ValueError(f'Не понял кусок формулы: {token}')
    if sum(counts.values()) == 0:
        raise ValueError('В формуле нет кубиков')
    if sum(counts.values()) > MAX_DICE:
        raise ValueError(f'Слишком много кубиков: максимум {MAX_DICE}')
    return counts, modifier


class CountRow(QWidget):
    def __init__(self, sides, on_change, parent=None):
        super().__init__(parent)
        self.sides = sides
        self.on_change = on_change
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.swatch = QLabel(f'D{sides}')
        self.swatch.setAlignment(Qt.AlignCenter)
        self.swatch.setFixedSize(42, 24)
        self._paint_swatch(False)
        row.addWidget(self.swatch)
        row.addStretch(1)
        self.minus = QToolButton()
        self.minus.setText('−')
        self.minus.setFixedSize(24, 24)
        self.minus.clicked.connect(lambda: self.on_change(sides, -1))
        row.addWidget(self.minus)
        self.value = QLabel('0')
        self.value.setObjectName('count')
        self.value.setAlignment(Qt.AlignCenter)
        self.value.setFixedWidth(24)
        row.addWidget(self.value)
        self.plus = QToolButton()
        self.plus.setText('+')
        self.plus.setFixedSize(24, 24)
        self.plus.clicked.connect(lambda: self.on_change(sides, 1))
        row.addWidget(self.plus)

    def set_count(self, value):
        self.value.setText(str(value))

    def set_locked(self, locked):
        self.setEnabled(not locked)
        self._paint_swatch(locked)

    def _paint_swatch(self, locked):
        if locked:
            self.swatch.setStyleSheet(
                'background: #2a2b31; color: #6f6b64; border-radius: 4px;'
                ' font-size: 9pt; font-weight: 700;')
        else:
            self.swatch.setStyleSheet(
                f"background: {DIE_COLORS[self.sides]}; color: white;"
                ' border-radius: 4px; font-size: 9pt; font-weight: 700;')


class DiceLabWindow(QMainWindow):
    #: Готовый бросок для стола: formula, total, mode, breakdown.
    rolled = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle('Dice Lab — физические кубики')
        self.setMinimumSize(1050, 700)
        self.resize(1280, 800)
        # Стиль ставим на само окно: из стола оно открывается без
        # app.setStyleSheet (тот вызов есть только в отдельном main()).
        self.setStyleSheet(QSS)

        self.engine = DicePhysics()
        self.audio = DiceAudio(volume=0.65)
        self.counts = {sides: 0 for sides in DIE_ORDER}
        self.counts[20] = 1
        self.saved_counts = dict(self.counts)
        self.mode = MODE_NORMAL
        self.modifier = 0
        self.rolling = False
        self.sim_time = 0.0
        self.rest_time = 0.0
        self.last_tick = 0.0
        self.accumulator = 0.0
        self.bad_indices = []
        self.last_values = []

        self._build()
        self._sync_formula()
        self._update_mode_controls()

        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._tick)

    # --- интерфейс ----------------------------------------------------
    def _build(self):
        root = QWidget()
        root.setObjectName('root')
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(26, 20, 14, 18)
        left_layout.setSpacing(10)
        kicker = QLabel('DICE LAB')
        kicker.setObjectName('kicker')
        left_layout.addWidget(kicker)
        title = QLabel('Физические кубики')
        title.setObjectName('title')
        left_layout.addWidget(title)
        left_layout.addSpacing(6)

        self.view = DiceView()
        self.view.set_engine(self.engine)
        left_layout.addWidget(self.view, 1)
        layout.addWidget(left, 1)

        side = QWidget()
        side.setObjectName('side')
        side.setFixedWidth(330)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(18, 20, 18, 18)
        side_layout.setSpacing(10)

        mode_caption = QLabel('РЕЖИМ')
        mode_caption.setObjectName('section')
        side_layout.addWidget(mode_caption)
        self.mode_combo = QComboBox()
        for mode in (MODE_NORMAL, MODE_ADVANTAGE, MODE_DISADVANTAGE):
            self.mode_combo.addItem(MODE_TITLES[mode], mode)
            self.mode_combo.setItemData(
                self.mode_combo.count() - 1, MODE_HINTS[mode], Qt.ToolTipRole)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        side_layout.addWidget(self.mode_combo)

        section = QLabel('НАБОР')
        section.setObjectName('section')
        side_layout.addWidget(section)
        self.rows = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        for index, sides in enumerate(DIE_ORDER):
            row = CountRow(sides, self.change_count)
            self.rows[sides] = row
            grid.addWidget(row, index // 2, index % 2)
        side_layout.addLayout(grid)

        modifier_row = QHBoxLayout()
        label = QLabel('Модификатор')
        label.setObjectName('muted')
        modifier_row.addWidget(label)
        modifier_row.addStretch(1)
        minus = QToolButton()
        minus.setText('−')
        minus.setFixedSize(24, 24)
        minus.clicked.connect(lambda: self.change_modifier(-1))
        modifier_row.addWidget(minus)
        self.modifier_label = QLabel('0')
        self.modifier_label.setObjectName('count')
        self.modifier_label.setAlignment(Qt.AlignCenter)
        self.modifier_label.setFixedWidth(32)
        modifier_row.addWidget(self.modifier_label)
        plus = QToolButton()
        plus.setText('+')
        plus.setFixedSize(24, 24)
        plus.clicked.connect(lambda: self.change_modifier(1))
        modifier_row.addWidget(plus)
        side_layout.addLayout(modifier_row)

        formula_label = QLabel('ФОРМУЛА')
        formula_label.setObjectName('section')
        side_layout.addWidget(formula_label)
        formula_row = QHBoxLayout()
        self.formula = QLineEdit()
        self.formula.returnPressed.connect(self.roll)
        formula_row.addWidget(self.formula, 1)
        self.apply_button = QPushButton('Применить')
        self.apply_button.clicked.connect(self.apply_formula)
        formula_row.addWidget(self.apply_button)
        side_layout.addLayout(formula_row)

        self.roll_button = QPushButton('  Бросить')
        self.roll_button.setObjectName('roll')
        self.roll_button.setIcon(dice_icon())
        self.roll_button.setIconSize(QSize(26, 26))
        self.roll_button.setMinimumHeight(44)
        self.roll_button.clicked.connect(self.roll)
        side_layout.addWidget(self.roll_button)

        self.rethrow_button = QPushButton('Перебросить нечитаемые')
        self.rethrow_button.setObjectName('rethrow')
        self.rethrow_button.clicked.connect(self.rethrow)
        self.rethrow_button.setVisible(False)
        side_layout.addWidget(self.rethrow_button)

        sound_row = QHBoxLayout()
        sound_row.setSpacing(7)
        self.sound_box = QCheckBox('Звук')
        self.sound_box.setChecked(True)
        self.sound_box.toggled.connect(self.audio.set_enabled)
        sound_row.addWidget(self.sound_box)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(65)
        self.volume.setFixedHeight(18)
        self.volume.valueChanged.connect(
            lambda value: self.audio.set_volume(value / 100.0))
        sound_row.addWidget(self.volume, 1)
        side_layout.addLayout(sound_row)

        self.status = QLabel('Выберите кубики и бросьте.')
        self.status.setObjectName('status')
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(34)
        side_layout.addWidget(self.status)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet('color: #2e2f36;')
        side_layout.addWidget(line)

        result_header = QHBoxLayout()
        result_caption = QLabel('РЕЗУЛЬТАТ')
        result_caption.setObjectName('section')
        result_header.addWidget(result_caption)
        result_header.addStretch(1)
        self.mode_label = QLabel('')
        self.mode_label.setObjectName('muted')
        result_header.addWidget(self.mode_label)
        side_layout.addLayout(result_header)

        result_row = QHBoxLayout()
        self.total_label = QLabel('—')
        self.total_label.setObjectName('total')
        result_row.addWidget(self.total_label)
        result_row.addStretch(1)
        side_layout.addLayout(result_row)

        self.chips = QGridLayout()
        self.chips.setHorizontalSpacing(4)
        self.chips.setVerticalSpacing(4)
        side_layout.addLayout(self.chips)

        history_label = QLabel('ИСТОРИЯ')
        history_label.setObjectName('section')
        side_layout.addWidget(history_label)
        self.history = QListWidget()
        self.history.setMinimumHeight(130)
        self.history.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.history.setTextElideMode(Qt.ElideRight)
        side_layout.addWidget(self.history, 1)

        layout.addWidget(side)

    # --- режим и набор ------------------------------------------------
    def _on_mode_changed(self, _index):
        self.change_mode(self.mode_combo.currentData())

    def _show_mode(self, mode):
        self.mode_combo.blockSignals(True)
        index = self.mode_combo.findData(mode)
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)
        self.mode_combo.blockSignals(False)

    def _update_mode_controls(self):
        normal = self.mode == MODE_NORMAL
        for row in self.rows.values():
            row.set_locked(not normal)
        self.formula.setEnabled(normal)
        self.apply_button.setEnabled(normal)

    def change_mode(self, mode):
        if mode not in MODE_TITLES:
            return
        if mode == self.mode:
            return
        if self.rolling:
            self._show_mode(self.mode)
            return
        self._invalidate_result()
        if self.mode == MODE_NORMAL:
            self.saved_counts = dict(self.counts)
        self.mode = mode
        if mode == MODE_NORMAL:
            restored = dict(self.saved_counts)
            self.counts = (restored if sum(restored.values())
                           else {sides: (1 if sides == 20 else 0)
                                 for sides in DIE_ORDER})
        else:
            self.counts = {sides: (2 if sides == 20 else 0)
                           for sides in DIE_ORDER}
        self._sync_formula()
        self._update_mode_controls()
        if mode == MODE_NORMAL:
            self._set_status('Обычный бросок: можно выбрать любые кубики.')
        else:
            self._set_status(
                f'{MODE_TITLES[mode]}: {MODE_HINTS[mode]}.')

    def change_count(self, sides, delta):
        if self.rolling or self.mode != MODE_NORMAL:
            return
        self._invalidate_result()
        new_value = max(0, min(MAX_DICE, self.counts[sides] + delta))
        if delta > 0 and sum(self.counts.values()) >= MAX_DICE:
            self._set_status(f'На столе максимум {MAX_DICE} кубиков.', True)
            return
        self.counts[sides] = new_value
        self.rows[sides].set_count(new_value)
        self._sync_formula()
        self._set_status('Выберите кубики и бросьте.')

    def change_modifier(self, delta):
        if self.rolling:
            return
        self._invalidate_result()
        self.modifier = max(-30, min(30, self.modifier + delta))
        self.modifier_label.setText(
            f'{self.modifier:+d}' if self.modifier else '0')
        self._sync_formula()

    def _formula_text(self):
        parts = []
        for sides in DIE_ORDER:
            count = self.counts[sides]
            if count:
                parts.append(f'd{sides}' if count == 1 else f'{count}d{sides}')
        text = '+'.join(parts)
        if self.modifier:
            text += f'{self.modifier:+d}'
        return text

    def _sync_formula(self):
        self.formula.setText(self._formula_text())
        for sides, row in self.rows.items():
            row.set_count(self.counts[sides])

    def apply_formula(self):
        if self.rolling:
            return False
        try:
            counts, modifier = parse_formula(self.formula.text())
        except ValueError as exc:
            self._set_status(str(exc), True)
            if self.mode != MODE_NORMAL:
                self._sync_formula()
            return False
        if (self.mode != MODE_NORMAL
                and (counts.get(20, 0) != 2
                     or sum(counts.values()) != 2)):
            self._set_status(
                'Для преимущества и помехи нужна формула 2d20 '
                '(можно с модификатором).', True)
            self._sync_formula()
            return False
        self._invalidate_result()
        self.counts = counts
        self.modifier = modifier
        self.modifier_label.setText(
            f'{modifier:+d}' if modifier else '0')
        self._sync_formula()
        self._set_status('Набор обновлён.')
        return True

    # --- бросок -------------------------------------------------------
    def roll(self):
        if self.rolling:
            return
        if not self.apply_formula():
            return
        sides_list = []
        for sides in DIE_ORDER:
            sides_list.extend([sides] * self.counts[sides])
        if not sides_list:
            self._set_status('В формуле нет кубиков.', True)
            return
        self.bad_indices = []
        self.last_values = []
        self._clear_result()
        self.engine.throw(sides_list)
        self.view.set_engine(self.engine)
        self.rethrow_button.setVisible(False)
        self.roll_button.setEnabled(False)
        self.rolling = True
        self.sim_time = 0.0
        self.rest_time = 0.0
        self.accumulator = 0.0
        self.last_tick = time.monotonic()
        self.mode_combo.setEnabled(False)
        self._set_status(f'Бросаем {len(sides_list)} куб. — ждём полной остановки.')
        self.audio.start_throw(len(sides_list))
        self.timer.start()

    def rethrow(self):
        if self.rolling or not self.bad_indices:
            return
        self.engine.relaunch(self.bad_indices)
        self.rethrow_button.setVisible(False)
        self.roll_button.setEnabled(False)
        self._clear_result()
        self.rolling = True
        self.sim_time = 0.0
        self.rest_time = 0.0
        self.accumulator = 0.0
        self.last_tick = time.monotonic()
        self.mode_combo.setEnabled(False)
        self.audio.start_throw(0)
        self._set_status('Перебрасываем только нечитаемые кубики.')
        self.timer.start()

    def _tick(self):
        now = time.monotonic()
        delta = min(max(now - self.last_tick, 0.0), 0.10)
        self.last_tick = now
        if not self.rolling:
            self.timer.stop()
            return
        self.accumulator += delta
        steps = 0
        impacts = []
        while self.accumulator >= PHYSICS_STEP and steps < 12:
            impacts.extend(self.engine.step())
            self.accumulator -= PHYSICS_STEP
            self.sim_time += PHYSICS_STEP
            steps += 1
        if impacts:
            self.audio.play_impacts(impacts, len(self.engine.dice))
        self.view.update()
        motion = self.engine.motion()
        if motion['escaped']:
            self._finish('escaped')
            return
        resting = motion['all_sleeping'] or (
            motion['max_linear'] < 0.12 and motion['max_angular'] < 0.25)
        self.rest_time = self.rest_time + PHYSICS_STEP if resting else 0.0
        if self.rest_time >= 0.30:
            self._finish('settled')
        elif self.sim_time >= ROLL_TIMEOUT_SECONDS:
            self._finish('timeout')

    def _finish(self, reason):
        self.timer.stop()
        self.rolling = False
        self.view.update()
        self.roll_button.setEnabled(True)
        self.mode_combo.setEnabled(True)
        readings = self.engine.readings()
        values = [reading.value for reading in readings]
        self.last_values = values
        if reason == 'escaped':
            self._set_status('Кубик покинул лоток — бросок не считается.', True)
            self.total_label.setText('—')
            return
        if reason == 'timeout':
            self._set_status('Кубики не остановились за 12 секунд.', True)
            self.total_label.setText('—')
            return
        bad = [index for index, reading in enumerate(readings)
               if not reading.readable]
        self.bad_indices = bad
        if bad:
            names = ', '.join(f'D{self.engine.dice[index].sides}'
                              for index in bad)
            self._set_status(
                f'Нечитаемая грань: {names}. Результат не засчитан — '
                'перебросьте эти кубики.', True)
            self.total_label.setText('—')
            self.rethrow_button.setVisible(True)
            return
        try:
            total, chosen = resolve_total(values, self.modifier, self.mode)
        except ValueError as exc:
            self._set_status(str(exc), True)
            self.total_label.setText('—')
            return
        self.total_label.setText(str(total))
        self._set_chips(values, chosen)
        mode_text = ('' if self.mode == MODE_NORMAL
                     else MODE_TITLES[self.mode].lower())
        sides = [die.sides for die in self.engine.dice]
        critical = critical_kind(values, sides, self.mode, chosen)
        crit_text = ''
        crit_short = ''
        if critical == CRIT_SUCCESS:
            crit_text = 'критический успех'
            crit_short = 'крит. успех'
            self.total_label.setStyleSheet('color: #ffd166;')
        elif critical == CRIT_FAIL:
            crit_text = 'критический провал'
            crit_short = 'крит. провал'
            self.total_label.setStyleSheet('color: #e05a4f;')
        if chosen is None:
            status = 'Готово: все кубики лежат на гранях.'
            self.mode_label.setText(f'{len(values)} куб.')
        else:
            status = (f'Готово: {mode_text}, выбран {values[chosen]}. '
                      'Кубики лежат на гранях.')
            self.mode_label.setText(f'{len(values)} куб. · {mode_text}')
        if critical:
            cue = self.audio.play_critical(critical)
            self.view.play_critical(
                critical,
                duration=cue.duration if cue else 0.0,
                special=cue.special if cue else False,
                beats=cue.beats if cue else ())
            status = f'{status} {crit_text.capitalize()}!'
        self._set_status(status)
        expression = self._formula_text()
        detail = (', '.join(map(str, values)) if len(values) <= 8
                  else f'{len(values)} куб.')
        if chosen is None:
            tag = f' [{crit_short}]' if crit_short else ''
            line = f'{expression}{tag}  →  {total}   ({detail})'
        else:
            tag = f', {crit_short}' if crit_short else ''
            line = (f'{expression} [{mode_text}{tag}]  →  {total}   '
                    f'({detail}; выбран {values[chosen]})')
        self.history.insertItem(0, line)
        while self.history.count() > 30:
            self.history.takeItem(self.history.count() - 1)
        # Отдаём бросок наружу: из окна стола он уходит в общий чат.
        # Готовая строка разбора считается здесь же, чтобы чат не бросал
        # кубики второй раз и не показывал другие числа.
        self.rolled.emit({
            'formula': expression,
            'total': total,
            'mode': ('adv' if self.mode == MODE_ADVANTAGE else
                     'dis' if self.mode == MODE_DISADVANTAGE else None),
            'breakdown': self._breakdown(values, sides),
        })

    def _breakdown(self, values, sides):
        groups = []
        for value, side in zip(values, sides):
            if groups and groups[-1][0] == side:
                groups[-1][1].append(value)
            else:
                groups.append([side, [value]])
        parts = []
        for side, rolled in groups:
            name = f'd{side}' if len(rolled) == 1 else f'{len(rolled)}d{side}'
            parts.append(f"{name}({', '.join(map(str, rolled))})")
        if self.modifier:
            parts.append(f'{self.modifier:+d}')
        return ''.join(parts)

    # --- результат ----------------------------------------------------
    def _invalidate_result(self):
        if self.rolling:
            return
        self._clear_result()
        self.rethrow_button.setVisible(False)

    def _clear_result(self):
        self.total_label.setText('—')
        self.total_label.setStyleSheet('')
        self.mode_label.setText('')
        self.view.clear_effect()
        self.bad_indices = []
        self.last_values = []
        self.rethrow_button.setVisible(False)
        while self.chips.count():
            item = self.chips.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _set_chips(self, values, selected=None):
        while self.chips.count():
            item = self.chips.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        shown = values[:12]
        for index, value in enumerate(shown):
            sides = self.engine.dice[index].sides
            chip = QLabel(str(value))
            chip.setAlignment(Qt.AlignCenter)
            chip.setFixedSize(26, 22)
            border = ('2px solid #f0c060' if index == selected
                      else '2px solid transparent')
            chip.setStyleSheet(
                f"background: {DIE_COLORS[sides]}; color: white;"
                ' border-radius: 3px; font-size: 9pt; font-weight: 700;'
                f' border: {border};')
            if index == selected:
                chip.setToolTip('Выбранный результат')
            self.chips.addWidget(chip, index // 8, index % 8)
        if len(values) > len(shown):
            more = QLabel(f'+{len(values) - len(shown)}')
            more.setStyleSheet('color: #98948c; font-size: 9pt;')
            self.chips.addWidget(more, len(shown) // 8, len(shown) % 8)

    def _set_status(self, text, error=False):
        self.status.setText(text)
        self.status.setStyleSheet(
            'color: #e08c72;' if error else 'color: #c9c4bb;')

    def closeEvent(self, event):
        self.timer.stop()
        self.audio.close()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    window = DiceLabWindow()
    window.show()
    return app.exec_()


if __name__ == '__main__':
    sys.exit(main())
