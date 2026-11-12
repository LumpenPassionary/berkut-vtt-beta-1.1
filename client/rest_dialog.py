"""Окно «Отдых» в верхней рейке мастера (иконка костра).

Отдых — событие всего стола, а не одного листа: по правилам 5e партия
ложится спать (или садится передохнуть) вместе, и ресурсы возвращаются
всем персонажам. Поэтому окно живёт только у мастера, а в листе персонажа
кнопки отдыха нет — иначе каждый игрок «отдыхал» бы в одиночку посреди
боя и залечивал себя до полного.

Что делает окно:

  * **Долгий отдых** — хиты, все ячейки заклинаний, заряды классовых
    особенностей (Второе дыхание, Ярость, Ци), заряды предметов, запас
    Наложения рук; снимает состояния (истощение — на одну ступень).
  * **Короткий отдых** — половина максимума хитов и только то, что по
    правилам восстанавливается на коротком отдыхе. За долгий отдых таких
    передышек две, счётчик виден на кнопке.
  * **День/Ночь** — то же, что кнопки во вкладке «Освещение»: меняют время
    суток карты у всего стола. Сейчас выбранное время подсвечено.
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                             QPushButton, QVBoxLayout)

from client import app_icon

#: Сколько коротких передышек доступно между долгими отдыхами.
SHORT_REST_MAX = 2


class RestDialog(QDialog):
    """Меню отдыха: долгий, короткий и заготовка «день/ночь»."""

    #: мастер попросил отдых: «long» или «short»
    restRequested = pyqtSignal(str)
    #: мастер переключил время суток на карте: «day» или «night»
    timeOfDayRequested = pyqtSignal(str)

    def __init__(self, parent=None, short_left: int = SHORT_REST_MAX):
        super().__init__(parent)
        self.setObjectName("rest_dialog")
        self.setWindowTitle("Отдых")
        self.setWindowIcon(app_icon())
        self.setMinimumWidth(400)
        self._short_left = int(short_left)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(10)

        title = QLabel("Отдых партии")
        title.setObjectName("rest_title")
        lay.addWidget(title)

        hint = QLabel("Восстанавливает ресурсы всех персонажей сразу. "
                      "Монстры и НПС отдыхают отдельно — их это не касается.")
        hint.setObjectName("rest_hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.long_btn = QPushButton("🌙  Долгий отдых")
        self.long_btn.setObjectName("rest_long_btn")
        self.long_btn.setCursor(Qt.PointingHandCursor)
        self.long_btn.setMinimumHeight(44)
        self.long_btn.setToolTip(
            "Полные хиты, все ячейки, все заряды, запас Наложения рук. "
            "Состояния снимаются, истощение — на одну ступень")
        self.long_btn.clicked.connect(
            lambda _=False: self.restRequested.emit("long"))
        lay.addWidget(self.long_btn)

        long_hint = QLabel("Хиты, ячейки, заряды особенностей и предметов, "
                           "запас лечения. Истощение −1.")
        long_hint.setObjectName("rest_hint")
        long_hint.setWordWrap(True)
        lay.addWidget(long_hint)

        self.short_btn = QPushButton()
        self.short_btn.setObjectName("rest_short_btn")
        self.short_btn.setCursor(Qt.PointingHandCursor)
        self.short_btn.setMinimumHeight(40)
        self.short_btn.setToolTip(
            "Половина максимума хитов и то, что восстанавливается "
            "на коротком отдыхе (Второе дыхание, Действие всплеск)")
        self.short_btn.clicked.connect(
            lambda _=False: self.restRequested.emit("short"))
        lay.addWidget(self.short_btn)

        self.short_hint = QLabel("Половина хитов и короткие способности. "
                                 "Ячейки заклинаний не возвращает.")
        self.short_hint.setObjectName("rest_hint")
        self.short_hint.setWordWrap(True)
        lay.addWidget(self.short_hint)

        # День и ночь — та же смена времени суток, что в меню «Стол» и
        # на вкладке «Освещение»: длинный отдых почти всегда начинается
        # с «наступает ночь», и мастеру не нужно искать другую вкладку.
        day_row = QHBoxLayout()
        day_row.setSpacing(8)
        self.day_btn = QPushButton("☀ День")
        self.day_btn.setObjectName("rest_day_btn")
        self.day_btn.setCheckable(True)
        self.day_btn.setCursor(Qt.PointingHandCursor)
        self.day_btn.setToolTip("Дневной свет: карта видна целиком")
        self.day_btn.clicked.connect(
            lambda _=False: self.timeOfDayRequested.emit("day"))
        day_row.addWidget(self.day_btn)
        self.night_btn = QPushButton("🌘 Ночь")
        self.night_btn.setObjectName("rest_night_btn")
        self.night_btn.setCheckable(True)
        self.night_btn.setCursor(Qt.PointingHandCursor)
        self.night_btn.setToolTip(
            "Ночь: видно только освещённое и дальность тёмного зрения")
        self.night_btn.clicked.connect(
            lambda _=False: self.timeOfDayRequested.emit("night"))
        day_row.addWidget(self.night_btn)
        lay.addLayout(day_row)

        self.status_label = QLabel("")
        self.status_label.setObjectName("rest_status")
        self.status_label.setWordWrap(True)
        lay.addWidget(self.status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        # Без перевода Qt стандартная кнопка осталась бы англоязычной.
        buttons.button(QDialogButtonBox.Close).setText("Закрыть")
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self.set_short_left(short_left)

    # ------------------------------------------------------------------
    def set_short_left(self, value: int):
        """Показать остаток коротких передышек на кнопке."""
        self._short_left = max(0, int(value))
        self.short_btn.setText(f"🔥  Короткий отдых · "
                               f"{self._short_left}/{SHORT_REST_MAX}")
        self.short_btn.setEnabled(self._short_left > 0)
        if self._short_left <= 0:
            self.short_btn.setToolTip(
                "Короткие передышки кончились — нужен долгий отдых")
        else:
            self.short_btn.setToolTip(
                "Половина максимума хитов и то, что восстанавливается "
                "на коротком отдыхе (Второе дыхание, Действие всплеск)")

    def short_left(self) -> int:
        return self._short_left

    def set_time_of_day(self, mode):
        """Показать, что сейчас на карте: день, ночь — или карты нет.

        ``None`` — открытой карты нет: кнопки гаснут, а подсказка говорит,
        чего не хватает, вместо молчаливого «нажал — ничего».
        """
        changes_time = mode is not None
        self.day_btn.setEnabled(changes_time)
        self.night_btn.setEnabled(changes_time)
        night = changes_time and str(mode) == "night"
        self.day_btn.setChecked(changes_time and not night)
        self.night_btn.setChecked(night)
        self.day_btn.setToolTip(
            "Дневной свет: карта видна целиком" if changes_time
            else "Сначала откройте карту")
        self.night_btn.setToolTip(
            "Ночь: видно только освещённое и дальность тёмного зрения"
            if changes_time else "Сначала откройте карту")

    def set_status(self, text: str):
        """Строка под кнопками: что только что произошло."""
        self.status_label.setText(str(text or ""))
