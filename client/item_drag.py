"""Перетаскивание предметов из инвентаря и справочника на токен.

Один MIME-формат на все источники:

    application/x-vtt-item
    {"source": "inventory", "token_id": "...", "list_id": "...",
     "item": {...запись предмета...}}

Источник кладёт запись предмета в QDrag, холст ловит её в dropEvent,
находит токен под курсором и отдаёт окну. Окно уже решает, списать ли
предмет из инвентаря или применить запись справочника.

Внутри того же инвентаря перетаскивание по-прежнему меняет порядок ячеек:
`ItemDragList` распознаёт свой `list_id` и вызывает `reorder_callback`,
а не создаёт действие.
"""
import json

from PyQt5.QtCore import QMimeData, QPoint, QRect, QRectF, Qt
from PyQt5.QtGui import QDrag, QPainter
from PyQt5.QtWidgets import QAbstractItemView, QListWidget

from client.item_icons import item_drag_pixmap
from client.win_frame import theme_is_dark

ITEM_MIME = "application/x-vtt-item"
#: Роль элемента списка, в которой лежит сама запись предмета.
ITEM_ROLE = Qt.UserRole + 17


def drag_payload(item: dict, *, source: str = "inventory",
                 token_id: str = "", list_id: str = "") -> dict:
    """Собрать переносимый payload; None-запись не перетаскивается."""
    if not isinstance(item, dict):
        return None
    return {
        "source": source,
        "token_id": str(token_id or ""),
        "list_id": str(list_id or ""),
        "item": item,
    }


def encode_payload(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def decode_payload(raw) -> dict:
    try:
        payload = json.loads(bytes(raw).decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def payload_from_mime(mime: QMimeData) -> dict:
    if mime is None or not mime.hasFormat(ITEM_MIME):
        return {}
    return decode_payload(mime.data(ITEM_MIME))


class ItemDragList(QListWidget):
    """QListWidget, который умеет отдавать предмет наружу.

    ``payload_builder(item)`` возвращает payload или None. Для инвентаря
    дополнительно задаётся ``reorder_callback(payload, row)``: тогда
    перетаскивание внутри самого списка двигает ячейку, а не применяет
    предмет. Список справочника работает в режиме ``DragOnly``.

    Сетка пустых ячеек включается ``set_empty_grid``: IconMode сам рисует
    только строки с предметами, поэтому без этого инвентарь выглядел
    рядом иконок, а не сеткой слотов.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.payload_builder = None
        self.reorder_callback = None
        self.drag_list_id = ""
        #: Пустые ячейки сетки: сколько их и как рисовать. Ноль — выключено.
        self.empty_grid_cells = 0
        self.empty_grid_painter = None
        #: Пустая ячейка под курсором — подсвечиваем её как цель клика.
        self.empty_hover_rect = None
        #: Ячейка, в которую сейчас упадёт перетаскиваемый предмет.
        self.drop_hint_rect = None
        self.drop_hint_painter = None
        #: Enter на выбранной строке: у инвентаря это «Использовать».
        self.activate_callback = None
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)
        # Без отслеживания мыши State_MouseOver не приходит, и подсветка
        # ячейки под курсором не появляется.
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        # Сброс содержимого и прокрутка двигают ячейки под мышью: старая
        # подсветка осталась бы висеть на чужом месте.
        self.model().modelReset.connect(self.reset_empty_hover)
        self.model().modelReset.connect(self.clear_drop_hint)
        self.verticalScrollBar().valueChanged.connect(self.reset_empty_hover)
        self.horizontalScrollBar().valueChanged.connect(self.reset_empty_hover)
        self.verticalScrollBar().valueChanged.connect(self.clear_drop_hint)
        self.horizontalScrollBar().valueChanged.connect(self.clear_drop_hint)

    def set_payload_builder(self, builder):
        self.payload_builder = builder

    def set_reorder_callback(self, callback, list_id: str = ""):
        self.reorder_callback = callback
        self.drag_list_id = str(list_id or "")

    def set_activate_callback(self, callback):
        """Что делать по Enter: например, применить выбранный предмет."""
        self.activate_callback = callback

    def reset_empty_hover(self, *_args):
        if self.empty_hover_rect is None:
            return
        old = self.empty_hover_rect
        self.empty_hover_rect = None
        self.viewport().update(old)

    def clear_drop_hint(self, *_args):
        if self.drop_hint_rect is None:
            return
        old = self.drop_hint_rect
        self.drop_hint_rect = None
        self.viewport().update(self._hint_update_rect(old))

    def set_drop_hint_painter(self, painter):
        """Как рисовать ячейку-цель: ``painter(qpainter, QRectF, dark)``."""
        self.drop_hint_painter = painter
        self.viewport().update()

    def accepts_payload(self, payload: dict) -> bool:
        """Можно ли бросить этот payload сюда: только своя перестановка."""
        return bool(payload) and self.reorder_callback is not None \
            and payload.get("list_id") == self.drag_list_id

    def dragged_row(self, payload: dict = None) -> int:
        """Строка перетаскиваемого предмета, даже если выделен был другой."""
        item_id = ((payload or {}).get("item") or {}).get("id")
        if item_id:
            for row in range(self.count()):
                record = self.item(row).data(ITEM_ROLE) or {}
                if record.get("id") == item_id:
                    return row
        return self.currentRow()

    def drop_target_rect(self, pos, source_row: int = None):
        """Ячейка под курсором, куда реально встанет предмет.

        Занятая ячейка — вставка перед ней; пустая — в конец списка.
        Если перестановка ничего не меняет (та же ячейка или последний
        предмет в пустоту), цели нет: не обещаем движение, которого не будет.

        При приземлении «в конец» подсвечивается не та пустая ячейка, над
        которой курсор, а первая пустая: предмет встаёт именно туда. Раньше
        подсвечивалась ячейка под мышью, и предмет «перелетал» на несколько
        слотов влево — выглядело как ошибка.
        """
        if source_row is None:
            source_row = self.currentRow()
        index = self.indexAt(pos)
        if index.isValid():
            if index.row() == source_row:
                return None
            return self.visualRect(index)
        if self.empty_cell_at(pos) is None:
            return None
        if source_row == self.count() - 1:
            return None
        return self.empty_cell_rect(self.count()) or self.empty_cell_at(pos)

    @staticmethod
    def _hint_update_rect(rect):
        """Область перерисовки подсказки: линия и её свечение шире ячейки."""
        return QRect(rect).adjusted(-4, -3, 4, 3)

    def _update_drop_hint(self, pos, payload: dict = None):
        cell = self.drop_target_rect(pos, self.dragged_row(payload))
        if cell == self.drop_hint_rect:
            return
        old = self.drop_hint_rect
        self.drop_hint_rect = cell
        if old is not None:
            self.viewport().update(self._hint_update_rect(old))
        if cell is not None:
            self.viewport().update(self._hint_update_rect(cell))

    def empty_cell_at(self, pos):
        """Пустая ячейка под точкой или None (занятые слоты не считаются)."""
        if self.empty_grid_painter is None:
            return None
        for rect in self.empty_grid_rects():
            if rect.contains(pos):
                return rect
        return None

    def set_empty_grid(self, cells: int, painter=None):
        """Показать сетку пустых ячеек до ``cells`` всего.

        ``painter`` вызывается как
        ``painter(qpainter, QRectF, dark, hovered)`` и рисует один пустой
        слот. Если он не задан, ячейки только рассчитываются геометрией
        (это используют тесты).
        """
        self.empty_grid_cells = max(0, int(cells or 0))
        self.empty_grid_painter = painter
        self.reset_empty_hover()
        self._extend_empty_scroll()
        self.viewport().update()

    # ------------------------------------------------------------------
    # Прокрутка до последнего пустого слота
    # ------------------------------------------------------------------
    #: QListWidget в IconMode считает высоту содержимого по числу элементов.
    #: Пустые ячейки рисуются поверх, но в раскладку не входят: с двумя
    #: предметами полоса прокрутки заканчивалась сразу после них, и нижние
    #: слоты сетки было не достать. Поэтому диапазон прокрутки расширяется
    #: до последней пустой ячейки.
    def empty_grid_height(self) -> int:
        """Высота всей сетки (предметы + пустые слоты) в пикселях."""
        total = max(self.empty_grid_cells, self.count())
        step = self.gridSize()
        if total <= 0 or step.width() <= 0 or step.height() <= 0:
            return 0
        width = max(1, self.viewport().width())
        columns = max(1, int(width // step.width()))
        rows = (total + columns - 1) // columns
        return rows * step.height() + 2 * max(0, int(self.spacing()))

    def _extend_empty_scroll(self):
        """Дать прокрутке дойти до нижнего ряда пустых ячеек."""
        bar = self.verticalScrollBar()
        needed = self.empty_grid_height()
        extra = max(0, needed - self.viewport().height())
        if bar.maximum() < extra:
            bar.setRange(bar.minimum(), extra)

    def updateGeometries(self):
        # Штатная раскладка сбрасывает диапазон по числу элементов —
        # возвращаем запас сразу после неё.
        super().updateGeometries()
        self._extend_empty_scroll()

    def empty_cell_rect(self, index: int):
        """Прямоугольник пустой ячейки с номером ``index`` в общей сетке.

        Считается по той же формуле, что у `empty_grid_rects`, но без
        проверки видимости: цель перетаскивания может быть и за краем, и
        тогда подсказка откатится на ячейку под курсором.
        """
        step = self.gridSize()
        if index is None or int(index) < 0:
            return None
        if step.width() <= 0 or step.height() <= 0:
            return None
        width = self.viewport().width()
        if width <= 0:
            return None
        columns = max(1, int(width // step.width()))
        row, column = divmod(int(index), columns)
        return QRect(column * step.width() - self.horizontalScrollBar().value(),
                     row * step.height() - self.verticalScrollBar().value(),
                     step.width(), step.height())

    def empty_grid_rects(self) -> list:
        """Прямоугольники пустых ячеек, которые видны в окне.

        Раскладка IconMode ставит элементы строго по `gridSize` без внешних
        отступов, поэтому пустые ячейки продолжают ту же сетку: занятые
        индексы пропускаем, остальные рисуем до вместимости и конца
        видимой области. Так сетка выглядит цельной и после последнего
        предмета.
        """
        total = max(self.empty_grid_cells, self.count())
        if total <= 0 or self.gridSize().width() <= 0:
            return []
        viewport = self.viewport()
        width, height = viewport.width(), viewport.height()
        if width <= 0 or height <= 0:
            return []
        count = self.count()
        rects = []
        for index in range(count, total):
            rect = self.empty_cell_rect(index)
            if rect is None or rect.right() < 0 or rect.bottom() < 0:
                continue
            if rect.left() >= width or rect.top() >= height:
                continue
            rects.append(rect)
        return rects

    def paintEvent(self, event):
        # Сначала штатная отрисовка фона и предметов, затем пустые ячейки
        # и подсветка цели перетаскивания. Так слой не перекрывает иконки
        # и подписи занятых слотов.
        super().paintEvent(event)
        rects = (self.empty_grid_rects()
                 if self.empty_grid_painter is not None else [])
        if not rects and (self.drop_hint_rect is None
                          or self.drop_hint_painter is None):
            return
        painter = QPainter(self.viewport())
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            dark = theme_is_dark(self.viewport(), self.palette())
            for rect in rects:
                self.empty_grid_painter(
                    painter, QRectF(rect), dark,
                    self.empty_hover_rect is not None
                    and rect == self.empty_hover_rect)
            if (self.drop_hint_rect is not None
                    and self.drop_hint_painter is not None):
                self.drop_hint_painter(
                    painter, QRectF(self.drop_hint_rect), dark)
        finally:
            painter.end()

    def startDrag(self, supported_actions):
        item = self.currentItem()
        if item is None or self.payload_builder is None:
            return
        # Пока тащим, mouseMove в список не приходит: подсветка ячейки
        # под курсором осталась бы от прошлого наведения и спорила бы с
        # подсветкой цели.
        self.reset_empty_hover()
        payload = self.payload_builder(item)
        if not payload:
            return
        mime = QMimeData()
        mime.setData(ITEM_MIME, encode_payload(payload))
        drag = QDrag(self)
        drag.setMimeData(mime)
        # Рисуем картинку под курсором заново в 56 px, а не растягиваем
        # маленькую иконку из списка: так склянка остаётся чёткой.
        pix = item_drag_pixmap(payload.get("item") or {}, 56)
        if not pix.isNull():
            drag.setPixmap(pix)
            drag.setHotSpot(QPoint(pix.width() // 2, pix.height() // 2))
        # Копирование, а не перемещение: запись остаётся в инвентаре,
        # пока окно не подтвердит расход и не пришлёт обновление листа.
        drag.exec_(Qt.CopyAction)
        # Если drop не дошёл до нашего обработчика (Esc, выход за список),
        # подсветка цели не должна остаться висеть.
        self.clear_drop_hint()

    def mouseMoveEvent(self, event):
        cell = self.empty_cell_at(event.pos())
        if cell != self.empty_hover_rect:
            old = self.empty_hover_rect
            self.empty_hover_rect = cell
            if old is not None:
                self.viewport().update(old)
            if cell is not None:
                self.viewport().update(cell)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.reset_empty_hover()
        super().leaveEvent(event)

    def resizeEvent(self, event):
        self.reset_empty_hover()
        self.clear_drop_hint()
        super().resizeEvent(event)
        # Ширина сменилась — изменилось число колонок и высота сетки.
        self._extend_empty_scroll()

    def mousePressEvent(self, event):
        # Клик по пустому слоту снимает выделение: иначе «Использовать»
        # продолжала бы действовать на предмет, который уже не подсвечен.
        if (event.button() == Qt.LeftButton
                and self.empty_cell_at(event.pos()) is not None):
            self.clearSelection()
            self.setCurrentItem(None)
            self.setFocus(Qt.MouseFocusReason)
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if (event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and self.activate_callback is not None
                and self.currentItem() is not None):
            self.activate_callback()
            event.accept()
            return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(ITEM_MIME):
            payload = payload_from_mime(event.mimeData())
            if self.accepts_payload(payload):
                self._update_drop_hint(event.pos(), payload)
                event.acceptProposedAction()
            else:
                self.clear_drop_hint()
                event.ignore()
            return
        super().dragEnterEvent(event)

    def dragLeaveEvent(self, event):
        self.clear_drop_hint()
        super().dragLeaveEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(ITEM_MIME):
            payload = payload_from_mime(event.mimeData())
            if self.accepts_payload(payload):
                self._update_drop_hint(event.pos(), payload)
                event.acceptProposedAction()
            else:
                self.clear_drop_hint()
                event.ignore()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        payload = payload_from_mime(event.mimeData())
        if self.accepts_payload(payload):
            # Своя же ячейка: это перестановка, а не применение на токен.
            row = self.indexAt(event.pos()).row()
            if row < 0:
                row = self.count()
            self.clear_drop_hint()
            self.reorder_callback(payload, row)
            event.acceptProposedAction()
            return
        self.clear_drop_hint()
        # Чужой предмет на список не бросаем: у списка нет цели-токена.
        event.ignore()
