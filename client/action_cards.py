"""Компактные карточки действий справочника.

Раньше каждая строка карточки монстра была отдельной кнопкой во всю ширину
панели: у дракона их набиралось девять, и список выглядел как форма отчёта.
Карточка показывает само действие — название сверху, эффект (урон, лечение,
спасбросок) снизу — и укладывается в две-три колонки по ширине панели.
Наведение даёт описание из бестиария, клик бросает то, что написано в
подписи, а правый клик открывает остальные броски действия.
"""
from html import escape

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtGui import QContextMenuEvent
from PyQt5.QtWidgets import (QFrame, QGridLayout, QLabel, QLayout, QMenu,
                             QSizePolicy, QVBoxLayout, QWidget)

#: Сколько символов описания показывать в подсказке. Полный текст действия
#: бывает на страницу, а подсказка должна читаться, не разворачиваясь на весь
#: экран.
TOOLTIP_CHARS = 420

#: Ширина карточки. Фиксированная, чтобы сетка была ровной: подписи разной
#: длины иначе делали колонки рваными.
CARD_WIDTH = 196
#: Расстояние между карточками — то же, что spacing сетки.
CARD_SPACING = 6


def tooltip_html(text: str) -> str:
    """Описание в подсказку: экранировано, с переносами и обрезкой."""
    text = " ".join(str(text or "").split())
    if len(text) > TOOLTIP_CHARS:
        text = text[:TOOLTIP_CHARS].rstrip(" ,;:.") + "…"
    return escape(text).replace("\n", "<br>")


class CardBoard(QWidget):
    """Сетка карточек: сама считает, сколько колонок влезает по ширине.

    QGridLayout удобнее самописной раскладки с переносом: карточки не
    наезжают друг на друга при смене ширины панели, а число колонок
    пересчитывается на каждый resize.
    """

    #: Ширина поменялась — число колонок могло измениться, высоту надо
    #: пересчитать: панель справочника то расширяется, то сжимается обратно.
    resized = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("action_cards_board")
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(CARD_SPACING)
        # Без этого сетка требует ширину всех колонок сразу (600 px), и
        # QScrollArea не мог сузить виджет: карточки всегда шли в три ряда.
        self.grid.setSizeConstraint(QLayout.SetNoConstraint)
        self.setMinimumWidth(CARD_WIDTH)
        self._cards = []

    def add_card(self, card):
        self._cards.append(card)
        self._reflow()

    def cards(self):
        return list(self._cards)

    def clear(self):
        """Убрать карточки, не выпуская их из иерархии окна.

        ``setParent(None)`` превращает каждую карточку в самостоятельное
        top-level-окно — на миг она мелькает на экране, а при десяти
        действиях (Андросфинкс) это выглядит как несколько маленьких окон.
        Скрываем и удаляем, оставляя родителя до фактического удаления.
        """
        for card in self._cards:
            self.grid.removeWidget(card)
            card.hide()
            card.deleteLater()
        self._cards = []

    def columns(self) -> int:
        width = max(CARD_WIDTH, self.width())
        return max(1, (width + CARD_SPACING) // (CARD_WIDTH + CARD_SPACING))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()
        self.resized.emit()

    def _reflow(self):
        columns = self.columns()
        for index, card in enumerate(self._cards):
            self.grid.addWidget(card, index // columns, index % columns,
                                Qt.AlignTop | Qt.AlignLeft)
        # Пустые колонки справа не растягиваются: карточки прижаты влево.
        for column in range(self.grid.columnCount()):
            self.grid.setColumnStretch(column, 0)
        self.grid.setColumnStretch(columns, 1)


class ActionCard(QFrame):
    """Карточка действия: название, его эффект и броски по клику.

    ``clicked`` — основной бросок (атака, спасбросок или урон), остальные
    броски того же действия лежат в контекстном меню: так карточка остаётся
    компактной и не занимает строку под каждое число.
    """

    clicked = pyqtSignal()

    def __init__(self, title: str, subtitle: str = "", tooltip: str = "",
                 parent=None):
        super().__init__(parent)
        self.setObjectName("action_card")
        self.setFrameShape(QFrame.NoFrame)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Minimum)
        self.setFixedWidth(CARD_WIDTH)
        self._title = str(title or "Действие")
        self._subtitle = str(subtitle or "")
        self._actionable = False
        self._alternatives = []

        box = QVBoxLayout(self)
        box.setContentsMargins(9, 6, 9, 7)
        box.setSpacing(2)
        # Родитель задаём сразу: QLabel без родителя — отдельное окно, и
        # при создании пачки карточек оно на миг мелькало поверх стола.
        # У Андросфинкса карточек десять — отсюда «несколько окошек».
        self.title_label = QLabel(self._title, self)
        self.title_label.setObjectName("action_card_title")
        self.title_label.setWordWrap(True)
        self.subtitle_label = QLabel(self._subtitle, self)
        self.subtitle_label.setObjectName("action_card_sub")
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setVisible(bool(self._subtitle))
        for label in (self.title_label, self.subtitle_label):
            # Клик должен попадать в карточку, а не в подпись внутри неё.
            label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            box.addWidget(label)
        if tooltip:
            self.setToolTip(tooltip)

    # --- данные ---
    def title(self) -> str:
        return self._title

    def subtitle(self) -> str:
        return self._subtitle

    def sizeHint(self):
        """Высота — по переносу подписей при этой ширине.

        QLabel с wordWrap просит высоту одной строки; без heightForWidth
        вторая строка эффекта обрезалась бы.
        """
        hint = QSize(CARD_WIDTH, 0)
        height = self.heightForWidth(CARD_WIDTH)
        hint.setHeight(height if height > 0 else super().sizeHint().height())
        return hint

    def set_actionable(self, actionable: bool):
        """Кликабельная карточка или просто справка без броска."""
        self._actionable = bool(actionable)
        self.setProperty("actionable", self._actionable)
        self.setCursor(Qt.PointingHandCursor if self._actionable
                       else Qt.ArrowCursor)
        # Перечитать QSS: у карточки-справки нет подсветки при наведении.
        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)

    def is_actionable(self) -> bool:
        return self._actionable

    def set_alternatives(self, alternatives):
        """Остальные броски действия: [(подпись, обработчик), …]."""
        self._alternatives = list(alternatives or [])

    def alternatives(self):
        return list(self._alternatives)

    def roll(self, label_part: str) -> bool:
        """Бросить по части подписи — тот же путь, что у пункта меню."""
        needle = str(label_part or "").casefold()
        for label, handler in self._alternatives:
            if needle and needle in label.casefold():
                handler()
                return True
        return False

    # --- поведение ---
    def click(self):
        """Программный клик: тот же путь, что у мыши (тесты и меню)."""
        if self._actionable:
            self.clicked.emit()

    def mouseReleaseEvent(self, event):
        if (event.button() == Qt.LeftButton and self._actionable
                and self.rect().contains(event.pos())):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent):
        if not self._actionable or not self._alternatives:
            super().contextMenuEvent(event)
            return
        menu = QMenu(self)
        for label, handler in self._alternatives:
            action = menu.addAction(label)
            action.triggered.connect(lambda _=False, h=handler: h())
        menu.exec_(event.globalPos())
