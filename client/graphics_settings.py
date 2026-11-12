"""Окно настроек графики стола: качество теней, сглаживание, движок.

Два места, где эти настройки нужны: стартовый экран (перед игрой) и меню
«Стол» в самой игре. Поэтому интерфейс один — виджет, который вставляется и
во вкладку стартового окна, и в диалог на столе.

Рядом с настройками — отчёт о памяти: сколько видеопамяти у карты, сколько
занято приложением и сколько занимают буферы холста. Без этих цифр «лаги»
лечатся наугад: мастер снижает качество, не зная, упирается ли он в
процессор или в видеопамять.
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                             QPushButton, QVBoxLayout, QWidget)

from shared import graphics as graphics_rules
from shared import hwinfo

#: Подписи движка: мастеру важен не OpenGL, а «будет ли работать».
RENDERER_LABELS = (
    ("auto", "Автоматически — видеокарта, если она есть"),
    ("gpu", "Видеокарта (OpenGL) — быстрее всего"),
    ("cpu", "Процессор — медленнее, но работает везде"),
)


class GraphicsSettingsWidget(QWidget):
    """Настройки графики + отчёт о памяти. Один и тот же на экране и в игре."""

    changed = pyqtSignal()

    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        self._buffers = 0
        self._frame_ms = None
        self._adapters = None
        self._build_ui()
        self.set_settings(settings)

    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        quality = QGroupBox("Качество")
        form = QFormLayout(quality)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setSpacing(6)

        self.rays_box = QComboBox()
        for rays in graphics_rules.RAY_STEPS:
            # Порядок в списке — от быстрого к красивому: мастер выбирает
            # словами, а не числом лучей.
            self.rays_box.addItem(graphics_rules.RAY_LABELS[rays], rays)
        self.rays_box.setToolTip(
            "Сколько лучей выпускает каждый источник света и каждый токен. "
            "Меньше лучей — ровнее кадр при десятках светов, но края теней "
            "грубее. Это главная ручка производительности стола.")
        form.addRow("Тени и свет", self.rays_box)

        self.shadows_box = QCheckBox("Тени от объектов карты (крыши, деревья)")
        self.shadows_box.setToolTip(
            "Самая дорогая часть света: силуэт каждого объекта вычитается из "
            "круга источника. Без этой галочки свет не затеняется объектами, "
            "а стены по-прежнему перекрывают его.")
        form.addRow("", self.shadows_box)

        self.aa_box = QCheckBox("Сглаживание кромок света и теней")
        self.aa_box.setToolTip(
            "Мягкие края вместо ступенек. Стоит немного, но на слабой машине "
            "отключается первым делом")
        form.addRow("", self.aa_box)

        self.glow_box = QCheckBox("Отсвет ярких источников днём (от 8000 K)")
        self.glow_box.setToolTip(
            "Днём видно только свет сильнее дневного — маяки и магический "
            "свет. Без галочки они не рисуют отсвет, но продолжают светить "
            "ночью")
        form.addRow("", self.glow_box)
        layout.addWidget(quality)

        engine = QGroupBox("Движок отрисовки")
        engine_layout = QVBoxLayout(engine)
        engine_layout.setSpacing(6)
        self.renderer_box = QComboBox()
        for key, label in RENDERER_LABELS:
            self.renderer_box.addItem(label, key)
        self.renderer_box.setToolTip(
            "Применяется после перезапуска приложения: движок выбирается на "
            "старте, когда создаётся окно стола")
        engine_layout.addWidget(self.renderer_box)
        restart_hint = QLabel("Смена движка вступит в силу после перезапуска.")
        restart_hint.setObjectName("start_hint")
        restart_hint.setWordWrap(True)
        engine_layout.addWidget(restart_hint)
        layout.addWidget(engine)

        memory = QGroupBox("Память")
        memory_layout = QVBoxLayout(memory)
        memory_layout.setSpacing(6)
        self.report_label = QLabel()
        self.report_label.setWordWrap(True)
        self.report_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        memory_layout.addWidget(self.report_label)
        refresh = QPushButton("Обновить отчёт")
        refresh.setObjectName("ghost_button")
        refresh.setToolTip(
            "Перечитать память процесса и буферы холста. Видеокарту "
            "опрашиваем один раз при открытии окна: драйвер меняется только "
            "с перезагрузкой")
        refresh.clicked.connect(lambda: self.refresh_report(force=True))
        row = QHBoxLayout()
        row.addWidget(refresh)
        row.addStretch(1)
        memory_layout.addLayout(row)
        layout.addWidget(memory)

        self.rays_box.currentIndexChanged.connect(self._on_changed)
        self.renderer_box.currentIndexChanged.connect(self._on_changed)
        for box in (self.shadows_box, self.aa_box, self.glow_box):
            box.toggled.connect(self._on_changed)
        layout.addStretch(1)

    # ------------------------------------------------------------------
    def _on_changed(self, *_args):
        self.refresh_report()
        self.changed.emit()

    def _update_budget_hint(self):
        """Ориентир кадра по выбранному качеству — в самом отчёте.

        Отдельная строка не нужна: `refresh_report` уже добавляет её в конце.
        Метод оставлен, чтобы вызовы из настроек не падали, если понадобится
        подсказка без пересборки отчёта.
        """
        self.refresh_report()

    def set_settings(self, settings):
        """Поставить настройки из файла (мусор в файле не ломает окно)."""
        data = graphics_rules.normalize(settings)
        index = self.rays_box.findData(data["rays"])
        if index >= 0:
            self.rays_box.setCurrentIndex(index)
        index = self.renderer_box.findData(data["renderer"])
        if index >= 0:
            self.renderer_box.setCurrentIndex(index)
        self.shadows_box.setChecked(bool(data["shadows"]))
        self.aa_box.setChecked(bool(data["antialiasing"]))
        self.glow_box.setChecked(bool(data["day_glow"]))
        self._update_budget_hint()
        self.refresh_report(force=True)

    def settings(self) -> dict:
        """Текущие значения в том же виде, что в settings.json."""
        return graphics_rules.normalize({
            "rays": self.rays_box.currentData(),
            "renderer": self.renderer_box.currentData(),
            "shadows": self.shadows_box.isChecked(),
            "antialiasing": self.aa_box.isChecked(),
            "day_glow": self.glow_box.isChecked(),
        })

    # ------------------------------------------------------------------
    def set_stats(self, buffers=None, frame_ms=None):
        """Сколько занимают буферы холста и как долго собирается кадр."""
        if buffers is not None:
            self._buffers = int(buffers)
        if frame_ms is not None:
            self._frame_ms = float(frame_ms)
        self.refresh_report()

    def refresh_report(self, force: bool = False):
        """Пересобрать подпись отчёта. Дёшево, кроме первого опроса карты."""
        if force or self._adapters is None:
            self._adapters = hwinfo.gpu_adapters()
        lines = hwinfo.memory_lines(adapters=self._adapters,
                                    buffers=self._buffers,
                                    frame_ms=self._frame_ms)
        budget = graphics_rules.frame_budget_ms(self.rays_box.currentData())
        lines.append("Ориентир кадра при этом качестве: ~%.0f мс "
                     "(30 кадров в секунду — это 33 мс)" % budget)
        if hwinfo.is_low_memory(self._adapters) and \
                self.settings()["rays"] > graphics_rules.MIN_RAYS:
            lines.append("Видеопамяти меньше 2 ГБ: попробуйте качество теней "
                         "«Низкое» — на больших картах с десятками светов это "
                         "ускоряет кадр втрое.")
        self.report_label.setText("\n".join(lines))


class GraphicsDialog(QDialog):
    """Окно «Графика» на столе: те же настройки, применяются сразу."""

    def __init__(self, settings=None, parent=None, buffers=0, frame_ms=None):
        super().__init__(parent)
        self.setWindowTitle("Графика стола")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        self.widget = GraphicsSettingsWidget(settings, self)
        self.widget.set_stats(buffers=buffers, frame_ms=frame_ms)
        layout.addWidget(self.widget, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok
                                   | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def settings(self) -> dict:
        return self.widget.settings()
