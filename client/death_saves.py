"""Спасброски от смерти: три (и больше) кружка, бросок и воскрешение.

Виджет общий для всех трёх мест, где ими пользуются:

  * лист персонажа игрока (левая панель и страницы токенов у мастера);
  * редактируемая карточка актёра;
  * карточка существа.

Поэтому он живёт отдельно от листов и умеет только показывать состояние и
отдавать намерения сигналами: решение, что слать в сеть, принимает главное
окно. Кружки кликабельны — результат настоящего кубика или Dice Lab можно
отметить руками. Мёртвого воскрешает только мастер: кнопка появляется у него.

Чистого расчёта здесь нет: успехи, провалы, смерть и стабилизация считаются
в ``shared.combat`` — одинаково у клиента и сервера.
"""
import os

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                             QVBoxLayout, QWidget)

from shared import combat as combat_rules

ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "assets", "icons", "tools", "death_skull.png")

#: Порядок клика по кружку: пусто -> успех -> провал -> пусто.
_NEXT_MARK = {combat_rules.DEATH_EMPTY: combat_rules.DEATH_SUCCESS,
              combat_rules.DEATH_SUCCESS: combat_rules.DEATH_FAILURE,
              combat_rules.DEATH_FAILURE: combat_rules.DEATH_EMPTY}

_MARK_COLORS = {
    combat_rules.DEATH_SUCCESS: ("#4fae5a", "#7fe08a"),
    combat_rules.DEATH_FAILURE: ("#b8423f", "#ff8c86"),
    combat_rules.DEATH_EMPTY: ("#26262b", "#9a9aa5"),
}


def skull_icon() -> QIcon:
    """Иконка спасбросков от смерти: череп из набора иконок проекта."""
    return QIcon(ICON_PATH) if os.path.isfile(ICON_PATH) else QIcon()


class DeathSavesBar(QWidget):
    """Полоса спасбросков от смерти: череп, кружки, бросок, воскрешение."""

    #: бросить спасбросок от смерти (token_id)
    rollRequested = pyqtSignal(str)
    #: ручная отметка кружка (token_id, индекс, новое значение)
    setRequested = pyqtSignal(str, int, str)
    #: воскресить мёртвого (token_id) — только мастер
    reviveRequested = pyqtSignal(str)
    #: стабилизировать умирающего (token_id) — только мастер
    stabilizeRequested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("death_saves_bar")
        self.token_id = None
        self._can_edit = True
        self._can_revive = False
        self._marks = [combat_rules.DEATH_EMPTY] * 3
        self._buttons = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 2, 0, 2)
        root.setSpacing(3)

        head = QHBoxLayout()
        head.setSpacing(6)
        icon = QLabel()
        icon.setObjectName("death_saves_icon")
        icon.setPixmap(skull_icon().pixmap(18, 18))
        icon.setFixedSize(20, 20)
        icon.setToolTip("Спасброски от смерти")
        head.addWidget(icon)
        self.title = QLabel("СПАСБРОСКИ ОТ СМЕРТИ")
        self.title.setObjectName("sheet_cell_title")
        # Лист живёт в узкой панели: длинный заголовок переносится, а не
        # растягивает весь лист вширь.
        self.title.setWordWrap(True)
        head.addWidget(self.title, 1)
        root.addLayout(head)

        row = QHBoxLayout()
        row.setSpacing(6)
        # Кружки — слева во второй строке: в узкой панели лист бывает шире
        # видимой части, и прижатые вправо они уезжали за край.
        self._marks_box = QHBoxLayout()
        self._marks_box.setSpacing(4)
        row.addLayout(self._marks_box)
        # Кнопки стоят сразу за кружками: лист бывает шире видимой панели,
        # и прижатые вправо они уезжали за край вместе со всем рядом.
        self.roll_button = QPushButton("Бросок")
        self.roll_button.setToolTip(
            "Бросить спасбросок от смерти: d20 против Сл 10, "
            "10 и выше — успех")
        self.roll_button.clicked.connect(self._on_roll)
        row.addWidget(self.roll_button)
        self.revive_button = QPushButton("Воскресить")
        self.revive_button.setObjectName("death_revive_btn")
        self.revive_button.setToolTip(
            "Вернуть мёртвого персонажа к жизни (1 хит, "
            "отметки смерти сняты)")
        self.revive_button.clicked.connect(self._on_revive)
        self.revive_button.setVisible(False)
        row.addWidget(self.revive_button)
        # Стабилизация умирающего — только у мастера и только пока не
        # провалены все три броска. Возвращает в игру с 1 хитом.
        self.stabilize_button = QPushButton("Стабилизировать")
        self.stabilize_button.setObjectName("death_stabilize_btn")
        self.stabilize_button.setToolTip(
            "Вернуть умирающего в игру с 1 хитом (отметки смерти сняты)")
        self.stabilize_button.clicked.connect(self._on_stabilize)
        self.stabilize_button.setVisible(False)
        row.addWidget(self.stabilize_button)
        self.status = QLabel("")
        self.status.setObjectName("hint_label")
        self.status.setWordWrap(True)
        self.status.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        row.addWidget(self.status, 1)
        root.addLayout(row)

        self.set_death_state([], None)
        self.setVisible(False)

    # ------------------------------------------------------------------
    # Состояние
    # ------------------------------------------------------------------
    def set_token_id(self, token_id):
        """Какому токену принадлежат кружки: с этим id уходят сигналы."""
        self.token_id = token_id

    def set_death_state(self, results, hp, can_edit: bool = True,
                        can_revive: bool = False):
        """Показать отметки смерти для токена с текущими хитами.

        Полоса видна только на нуле хитов: на полном здоровье спасброски от
        смерти — бессмысленный блок, который отнимал место у листа.
        """
        try:
            hp_value = int(hp) if hp is not None else None
        except (TypeError, ValueError):
            hp_value = None
        self._can_edit = bool(can_edit)
        self._can_revive = bool(can_revive)
        if hp_value is None or hp_value > 0:
            self.setVisible(False)
            return
        state = combat_rules.death_save_state(results)
        self._marks = list(state["results"])
        self._rebuild_marks()
        self.setVisible(True)

        if state["dead"]:
            self.status.setText("МЁРТВ: три провала. Нужно воскрешение.")
        elif state["stable"]:
            self.status.setText("Стабилизирован: три успеха.")
        else:
            self.status.setText(
                f"Сл {state['dc']}: успехов {state['successes']}, "
                f"провалов {state['failures']} из {combat_rules.DEATH_SAVE_FAILURES}")
        self.status.setToolTip(
            "Нажмите на кружок, чтобы отметить результат броска: "
            "пусто → успех → провал → пусто")

        self.roll_button.setEnabled(bool(self._can_edit and state["dying"]))
        self.revive_button.setVisible(bool(state["dead"] and self._can_revive))
        self.stabilize_button.setVisible(
            bool(self._can_revive and state["dying"]))
        self.roll_button.setVisible(not state["dead"])

    # ------------------------------------------------------------------
    # Кружки
    # ------------------------------------------------------------------
    def _rebuild_marks(self):
        for button in self._buttons:
            self._marks_box.removeWidget(button)
            button.deleteLater()
        self._buttons = []
        for index, mark in enumerate(self._marks):
            button = QPushButton("")
            button.setFixedSize(17, 17)
            button.setCursor(Qt.PointingHandCursor)
            button.setFocusPolicy(Qt.NoFocus)
            button.setToolTip(
                "Отметка спасброска от смерти: пусто → успех → провал → пусто")
            button.clicked.connect(
                lambda _=False, i=index: self._on_mark_clicked(i))
            self._style_mark(button, mark)
            self._marks_box.addWidget(button)
            self._buttons.append(button)

    @staticmethod
    def _style_mark(button, mark):
        fill, border = _MARK_COLORS.get(
            mark, _MARK_COLORS[combat_rules.DEATH_EMPTY])
        button.setStyleSheet(
            "QPushButton { background: %s; border: 2px solid %s;"
            " border-radius: 8px; padding: 0; }" % (fill, border))

    def _on_mark_clicked(self, index: int):
        if not self._can_edit or not self.token_id:
            return
        if index < 0 or index >= len(self._marks):
            return
        state = combat_rules.death_save_state(self._marks)
        if state["dead"]:
            return
        value = _NEXT_MARK.get(self._marks[index], combat_rules.DEATH_EMPTY)
        self.setRequested.emit(str(self.token_id), index, value)

    def _on_roll(self):
        if self.token_id:
            self.rollRequested.emit(str(self.token_id))

    def _on_revive(self):
        if self.token_id:
            self.reviveRequested.emit(str(self.token_id))

    def _on_stabilize(self):
        if self.token_id:
            self.stabilizeRequested.emit(str(self.token_id))
