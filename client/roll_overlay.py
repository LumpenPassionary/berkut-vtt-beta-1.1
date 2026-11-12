"""Всплывающие карточки броска поверх холста.

Любой бросок виден всем участникам стола, а не только тому, кто его
сделал: сервер присылает результат всем, а клиент показывает карточку
с числом на холсте. Через несколько секунд карточка гаснет сама.

Почему не только чат: в бою смотреть в правую панель некогда, и бросок
мастера легко пропустить. Почему чат всё равно остаётся: карточка —
отклик, а разбор (формула, состояния, вердикт) для сверки живёт в чате
и в логе.

Карточки не ловят мышь (``WA_TransparentForMouseEvents``), поэтому
выделять токены и двигать карту под ними можно как обычно.
"""
from PyQt5.QtCore import QEvent, QObject, QPropertyAnimation, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QFrame, QGraphicsOpacityEffect, QHBoxLayout,
                             QLabel, QVBoxLayout, QWidget)

#: Сколько живёт карточка до начала затухания. Хватает, чтобы прочитать
#: две строки разбора, но не настолько, чтобы закрыть карту.
TOAST_LIFETIME_MS = 5200
#: Длительность появления и затухания.
TOAST_FADE_IN_MS = 180
TOAST_FADE_OUT_MS = 340
#: Сколько карточек держим на экране: больше — уже стена, а не подсказка.
MAX_TOASTS = 4
TOAST_WIDTH = 300
#: Отступ стопки от нижнего и правого края холста.
TOAST_MARGIN = 18
#: Карточка чуть прозрачная: под ней видно карту, но число читается.
TOAST_OPACITY = 0.94

NEUTRAL = "neutral"
GOOD = "good"
BAD = "bad"
GOLD = "gold"

#: Цвет полоски для каждого смысла броска.
TONE_COLOURS = {
    NEUTRAL: "#f3efe6",
    GOOD: "#a3be8c",
    BAD: "#bf616a",
    GOLD: "#f2cc76",
}


class RollToast(QFrame):
    """Одна карточка: заголовок, крупное число и пояснение.

    Полоска слева окрашена по смыслу (успех, провал, крит), а само число
    всегда светлое: цвет не должен быть единственным носителем смысла.
    """

    closed = pyqtSignal(object)

    def __init__(self, title: str, value: str, detail: str = "",
                 tone: str = NEUTRAL, parent=None):
        super().__init__(parent)
        self.setObjectName("roll_toast")
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFixedWidth(TOAST_WIDTH)
        colour = TONE_COLOURS.get(tone, TONE_COLOURS[NEUTRAL])
        self.setStyleSheet(
            "QFrame#roll_toast {"
            " background-color: rgba(20, 19, 26, 238);"
            f" border: 1px solid #3a3548; border-left: 3px solid {colour};"
            " border-radius: 9px; }")

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 14, 9)
        row.setSpacing(10)

        column = QVBoxLayout()
        column.setSpacing(1)
        # Имена токенов и заметки приходят из сети: только простой текст,
        # чтобы никто не протащил разметку в карточку.
        #
        # Родитель задаётся сразу: подпись без родителя, которую явно
        # показывают (`setVisible`), на миг становится отдельным окном —
        # пустая рамка поверх холста. Их набиралась целая россыпь на каждый
        # бросок и отдых, и они не убирались вместе с карточкой.
        self.title_label = QLabel(str(title or "").upper(), self)
        self.title_label.setObjectName("roll_toast_title")
        self.title_label.setTextFormat(Qt.PlainText)
        column.addWidget(self.title_label)
        self.detail_label = QLabel(str(detail or ""), self)
        self.detail_label.setObjectName("roll_toast_detail")
        self.detail_label.setTextFormat(Qt.PlainText)
        self.detail_label.setWordWrap(True)
        column.addWidget(self.detail_label)
        row.addLayout(column, 1)

        self.value_label = QLabel(str(value or ""), self)
        self.value_label.setObjectName("roll_toast_value")
        self.value_label.setTextFormat(Qt.PlainText)
        self.value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.value_label)
        # Скрываем пустое пояснение уже внутри карточки: `setVisible(False)`
        # на виджете без родителя окна не создаёт, но порядок лучше держать
        # одним — сначала в раскладку, потом видимость.
        self.detail_label.setVisible(bool(detail))

        self._fading = False
        self._finished = False
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)
        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setDuration(TOAST_FADE_IN_MS)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(TOAST_OPACITY)
        self._fade.start()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.fade_out)
        self._timer.start(int(TOAST_LIFETIME_MS))

    def fade_out(self):
        """Погасить карточку и убрать её со стека."""
        if self._fading:
            return
        self._fading = True
        self._timer.stop()
        self._fade.stop()
        self._fade.setDuration(TOAST_FADE_OUT_MS)
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self._finish)
        self._fade.start()

    def dismiss(self):
        """Убрать сразу, без затухания (стек переполнен)."""
        self._timer.stop()
        self._fade.stop()
        self._finish()

    def _finish(self):
        # Двойной вызов возможен: таймер догорел и стек попросил убрать
        # карточку. Сигнал должен уйти один раз.
        if self._finished:
            return
        self._finished = True
        self.closed.emit(self)
        self.deleteLater()

    def text_summary(self) -> tuple:
        """Что видит игрок — для тестов и подсказок."""
        return (self.title_label.text(), self.value_label.text(),
                self.detail_label.text())


class RollOverlay(QWidget):
    """Стопка карточек поверх холста: угол снизу справа.

    Новые карточки появляются у нижнего края, старые сдвигаются вверх и
    гаснут. Так место, где появляется новое число, всегда одно и то же —
    глаз не ищет его по всему экрану, а верх карты (где стоит сцена боя)
    остаётся свободным.

    Живёт дочерним виджетом холста и повторяет его размер, поэтому
    остаётся видимой при любом масштабе карты и не мешает навигации.
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("roll_overlay")
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, TOAST_MARGIN, TOAST_MARGIN)
        self._layout.setSpacing(7)
        self._layout.setAlignment(Qt.AlignRight | Qt.AlignBottom)
        #: Новое — первым в списке (тесты и стек), а в раскладке оно стоит
        #: последним: снизу появляется, старое уходит вверх.
        self.toasts = []
        self.setGeometry(parent.rect())
        parent.installEventFilter(self)

    # ------------------------------------------------------------------
    # Показ
    # ------------------------------------------------------------------
    def push(self, title: str, value: str, detail: str = "",
             tone: str = NEUTRAL) -> RollToast:
        """Показать карточку броска. Возвращает её (удобно для тестов)."""
        toast = RollToast(title, value, detail, tone, parent=self)
        toast.closed.connect(self._forget)
        self.toasts.insert(0, toast)
        # В конец раскладки: у нижнего края появляется новое, прежние
        # уезжают вверх, а не наоборот.
        self._layout.insertWidget(self._layout.count(), toast)
        while len(self.toasts) > MAX_TOASTS:
            self.toasts.pop().dismiss()
        self.raise_()
        self.show()
        return toast

    def clear(self):
        """Убрать все карточки (смена карты, выход из сессии)."""
        for toast in list(self.toasts):
            toast.dismiss()
        self.toasts = []

    def _forget(self, toast):
        # Убираем из раскладки сразу: иначе следующая карточка встанет
        # над уже погасшей, и на кадр будет двойной отступ.
        self._layout.removeWidget(toast)
        if toast in self.toasts:
            self.toasts.remove(toast)

    # ------------------------------------------------------------------
    # Геометрия
    # ------------------------------------------------------------------
    def eventFilter(self, obj: QObject, event: QEvent):
        if obj is self.parent() and event.type() == QEvent.Resize:
            self.setGeometry(self.parent().rect())
            self.raise_()
        return False
