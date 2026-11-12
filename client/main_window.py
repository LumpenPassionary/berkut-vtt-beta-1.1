"""
Главное окно приложения: полотно, панели DM/игрока, чат, звук, журнал.
"""
import sys
import os
import copy
import json
import logging
import math
import random
import time
from pathlib import Path
import re
from datetime import datetime

from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QTextEdit, QLineEdit, QPushButton, QListWidget,
                             QListWidgetItem, QLabel, QSlider,
                             QFileDialog, QInputDialog, QMessageBox, QToolBar,
                             QAction, QDockWidget, QGroupBox, QSpinBox,
                             QComboBox, QColorDialog, QSplitter, QStackedWidget, QCheckBox,
                             QRadioButton, QToolButton, QMenu, QAbstractItemView, QDialog,
                             QSizePolicy, QFormLayout, QDoubleSpinBox,
                             QTextBrowser, QDialogButtonBox, QGridLayout,
                             QScrollArea, QFrame)
from PyQt5.QtCore import (Qt, QSize, QRect, QPoint, QItemSelectionModel,
                          QMimeData, pyqtSignal, QTimer)
from PyQt5.QtGui import (QPixmap, QIcon, QColor, QPainter, QPen, QCursor,
                         QFontDatabase, QDrag)
from PyQt5 import sip

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.models import (Role, Token, MapData, MapObject, AudioTrack,
                           GameState, generate_id, normalize_time_of_day)
from shared.protocol import MessageType
from shared import assets as asset_utils
from shared import app_paths
from shared import grid as grid_utils
from shared import world_store
from shared import dnd5e_lib
from shared import dice as dice_ext
from shared import vision as vision_rules
from shared import walls as wall_rules
from shared import special_attacks as special_attack_rules
from shared import map_text as text_rules
from shared.grid import clamp_cells
from client import app_icon
from client.game_canvas import (GameCanvas, fill_menu_entries,
                                LIGHT_PRESET_MIME)
from client.roll_overlay import (BAD as ROLL_BAD, GOLD as ROLL_GOLD,
                                 GOOD as ROLL_GOOD, RollOverlay)
from client.character_sheet import CharacterSheet
from client.sheet_pages import SheetPages
from client.audio_system import AudioSystem
from client.dice_buttons import DiceButtons, dice_icon
from client.undo_stack import UndoStack, Action
from client.win_frame import apply_window_frame
from client import token_frames
from client.token_portrait_dialog import TokenPortraitDialog
from client.hotkeys import event_key, install_shortcut
from client.item_drag import ItemDragList, drag_payload
from client.item_icons import item_icon
from client.action_cards import ActionCard, CardBoard, tooltip_html
# Боевые диалоги и правила. Без этих импортов методы открытия карточки существа,
# атаки, спасброска и состояний падали с NameError: код был, кнопки — нет.
from client.combat_panel import AttackDialog, ConditionsDialog, SaveDialog
from client.actor_sheet import ActorSheetDialog
from client.my_actors import MyActorsPage, actor_image, token_from_actor
from client.slots_dialog import SlotsDialog
from client.autosave_dialog import AutosaveDialog
from client.guide_dialog import GuideDialog
from client.rest_dialog import RestDialog, SHORT_REST_MAX
from client.support_dialog import (open_support as open_support_window,
                                   support_icon)
from shared import class_features
from shared import combat as combat_rules
from shared import conditions as conditions_rules
from shared import token_stats
from shared import lss_format
from shared import use_action
from shared import item_effects
from shared import sheet_items
from shared import content_audit
from shared import lighting as lighting_rules
from shared import token_kind
from shared import sheet_audit
from shared import graphics as graphics_rules
from client.graphics_settings import GraphicsDialog


class SheetStore(dict):
    """Словарь листов, который говорит холсту, что тёмное зрение поменялось.

    Дальность тёмного зрения лежит в листе персонажа, а лист приезжает с
    сервера отдельно от токенов. Холст про это не знал: кадр оставался
    прежним, и ч/б вокруг токена появлялось только после сдвига камеры или
    зума — то есть когда кадр перерисовывал кто-то другой. Теперь запись в
    словарь сама просит холст пересчитать тёмное зрение.

    Обновление через ``update`` и ``setdefault`` тоже идёт сюда: у обычного
    dict они не зовут ``__setitem__``, и уведомление потерялось бы.
    """

    def __init__(self, notify):
        super().__init__()
        self._notify = notify

    def __setitem__(self, key, value):
        previous = self.get(key)
        super().__setitem__(key, value)
        # Тот же самый лист переписывают на каждом кадре (его отдаёт панель
        # персонажа и страница): повторной перерисовки это не требует.
        if previous is not value:
            self._notify()

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]


class AudioListWidget(QListWidget):
    """Список треков/звуков с выделением рамкой, контекстным меню и Del/X.

    Штатный QListWidget по умолчанию выделяет только одну строку и рамкой не
    выделяет вовсе, поэтому поведение вынесено в подкласс — так оно одинаково
    работает и для музыки, и для звуков.

    delete_requested отдаёт список выделенных строк: обработчик сам решает,
    как их удалять.
    """

    delete_requested = pyqtSignal(list)
    play_requested = pyqtSignal(int)

    def __init__(self, parent=None, play_label=None):
        super().__init__(parent)
        # ExtendedSelection даёт и обычное выделение одной строки кликом, и
        # несколько строк через Shift/Ctrl, и выделение рамкой мышью.
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionRectVisible(True)
        # Перетаскивание отключено намеренно: иначе Qt включает drag-and-drop,
        # и рамка выделения не рисуется — вместо неё начинается перенос строки.
        self.setDragEnabled(False)
        self.setAcceptDrops(False)
        self.setDragDropMode(QAbstractItemView.NoDragDrop)
        # Своё контекстное меню вместо системного.
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        # Необязательный пункт «играть»: для звуков он нужен, у плейлиста
        # музыки двойной клик и так запускает выбранный трек.
        self.play_label = play_label

    # --- Выделение ---
    def selected_rows(self) -> list:
        """Строки, выделенные в данный момент (по возрастанию)."""
        return sorted(index.row() for index in self.selectedIndexes())

    # --- Удаление по Del или X ---
    def keyPressEvent(self, event):
        """Del и X удаляют всё выделенное: что выделено рамкой, то и уйдёт."""
        if event_key(event) in (Qt.Key_Delete, Qt.Key_X) and self.selectedIndexes():
            self.delete_requested.emit(self.selected_rows())
            return
        super().keyPressEvent(event)

    # --- Контекстное меню ---
    def _row_at(self, pos) -> int:
        item = self.itemAt(pos)
        return self.row(item) if item is not None else -1

    def _show_context_menu(self, pos):
        row = self._row_at(pos)
        if row < 0:
            return
        # ПКМ по невыделенной строке: сбрасываем прежнее выделение, чтобы
        # действие применилось ровно к тому, по чему кликнули.
        if row not in self.selected_rows():
            self.clearSelection()
            self.setCurrentRow(row)
        menu = QMenu(self)
        if self.play_label:
            menu.addAction(self.play_label, lambda: self.play_requested.emit(row))
            menu.addSeparator()
        menu.addAction("Удалить",
                       lambda: self.delete_requested.emit(self.selected_rows()))
        menu.exec_(self.viewport().mapToGlobal(pos))


class SelectionBand(QWidget):
    """Синяя рамка выделения поверх списка.

    QRubberBand рисует системный пунктир и не слушается стилей, а на холсте
    выделение синее — список должен выглядеть так же. Рамка прозрачна для
    мыши: она не мешает захвату строк под собой.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(70, 130, 200, 46))
        painter.setPen(QPen(QColor(84, 152, 224), 1, Qt.SolidLine))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))


class QuickDeleteList(QListWidget):
    """Список с удалением по ПКМ и Delete: карты, токены, объекты.

    Строки со своими виджетами (глазки, шестерёнки) не мешают: удаление
    работает по номеру строки, а не по тексту элемента.
    """

    delete_requested = pyqtSignal(int)
    # Удаление сразу нескольких строк: столько, сколько выделили мышью
    # рамкой или через Shift. Список карт/объектов живёт по одной строке.
    delete_rows_requested = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        # Рамка выделения живёт только там, где включено мультивыделение.
        self._rubber_enabled = False
        self._rubbering = False
        self._rubber_rows = set()
        self._rubber_base = set()
        self._rubber_origin = None
        self._band = None

    def enable_multi_selection(self):
        """Несколько строк: Shift, Ctrl и синяя рамка по пустому месту.

        Рамку рисуем сами: у списка с виджетами строк штатная не работает
        в режиме списка, а нам нужна та же синяя рамка, что на холсте.
        """
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionRectVisible(True)
        self.setDragEnabled(False)
        self.setAcceptDrops(False)
        self.setDragDropMode(QAbstractItemView.NoDragDrop)
        self._rubber_enabled = True

    # --- Рамка выделения мышью ---
    def _rows_in_rect(self, rect) -> set:
        """Строки, которые рамка задевает хотя бы краем."""
        rows = set()
        for row in range(self.count()):
            item = self.item(row)
            if item is not None and self.visualItemRect(item).intersects(rect):
                rows.add(row)
        return rows

    def _apply_rubber_selection(self, rows, base):
        """Выделить base ∪ rows. Текущую строку не сдвигаем до конца."""
        wanted = set(base) | set(rows)
        for row in range(self.count()):
            item = self.item(row)
            if item is not None:
                item.setSelected(row in wanted)
        if wanted:
            index = self.model().index(max(wanted), 0)
            self.selectionModel().setCurrentIndex(
                index, QItemSelectionModel.NoUpdate)

    def mousePressEvent(self, event):
        if (getattr(self, "_rubber_enabled", False)
                and event.button() == Qt.LeftButton
                and self.itemAt(event.pos()) is None):
            self._rubber_origin = event.pos()
            self._rubber_base = set(self.selected_rows()) \
                if event.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier) \
                else set()
            self._rubber_rows = set()
            if self._band is None:
                self._band = SelectionBand(self.viewport())
            self._band.setGeometry(QRect(self._rubber_origin, QSize()))
            self._band.show()
            self._band.raise_()
            self._rubbering = True
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if getattr(self, "_rubbering", False):
            rect = QRect(self._rubber_origin, event.pos()).normalized()
            self._band.setGeometry(rect)
            rows = self._rows_in_rect(rect)
            if rows != self._rubber_rows:
                self._rubber_rows = rows
                self._apply_rubber_selection(rows, self._rubber_base)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if getattr(self, "_rubbering", False):
            self._rubbering = False
            if self._band is not None:
                self._band.hide()
            self._apply_rubber_selection(self._rubber_rows, self._rubber_base)
            return
        super().mouseReleaseEvent(event)

    def selected_rows(self) -> list:
        """Выделенные строки по возрастанию."""
        return sorted(index.row() for index in self.selectedIndexes())

    def _request_delete(self, rows):
        """Удалить одну строку старым сигналом, несколько — пачкой."""
        rows = sorted({row for row in rows if row >= 0})
        if not rows:
            return
        if len(rows) == 1:
            self.delete_requested.emit(rows[0])
        else:
            self.delete_rows_requested.emit(rows)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            rows = self.selected_rows() or (
                [self.currentRow()] if self.currentRow() >= 0 else [])
            if rows:
                self._request_delete(rows)
                return
        super().keyPressEvent(event)

    def set_row_widget(self, item: QListWidgetItem, widget: QWidget):
        """Повесить виджет строки и дать строке высоту по содержимому.

        У пункта в QSS есть отступы (6 px сверху и снизу): если задать высоту
        вручную (26 px), внутри остаётся 14 px, и кнопки-иконки 24 px
        обрезаются. Берём высоту у самого виджета — она уже учитывает размеры
        кнопок и масштаб шрифта системы.

        Фон контейнера — прозрачный. В теме есть общее правило
        ``QMainWindow, QWidget { background-color: #19191c }``, и виджет
        строки закрашивал им подсветку выбора: выделенный токен в списке
        сцены выглядел точно так же, как остальные, хотя на холсте был
        обведён. Правило по id не трогает кнопки внутри строки — их вид
        по-прежнему задаёт тема.
        """
        need = widget.sizeHint().height()
        widget.setMinimumHeight(need)
        if not widget.objectName():
            widget.setObjectName("list_row")
        widget.setStyleSheet("QWidget#%s { background: transparent; }"
                             % widget.objectName())
        item.setSizeHint(QSize(0, max(26, need + 2)))
        self.setItemWidget(item, widget)

    def _show_menu(self, pos):
        row = self.row(self.itemAt(pos)) if self.itemAt(pos) else -1
        if row < 0:
            return
        # ПКМ по строке вне выделения — работаем ровно с ней. Внутри
        # выделения — со всей группой: так Delete и меню ведут себя
        # одинаково.
        rows = self.selected_rows()
        if row not in rows:
            self.setCurrentRow(row)
            rows = [row]
        menu = QMenu(self)
        title = "Удалить" if len(rows) < 2 else f"Удалить выбранные ({len(rows)})"
        menu.addAction(title, lambda: self._request_delete(rows))
        menu.exec_(self.viewport().mapToGlobal(pos))


class LightList(QListWidget):
    """Список источников света: рамка выделения, Del/X и пустой клик.

    Как и списки музыки, умеет выделять несколько строк рамкой или
    Shift/Ctrl: глазок и замок применяются ко всей группе сразу, а Del и X
    удаляют всё выделенное. Строки короткие, а под ними остаётся пустое
    место: короткий клик по нему снимает выделение (протяжка — рисует
    рамку), иначе следующие правки радиуса молча уходили бы прежнему
    факелу.
    """

    blank_clicked = pyqtSignal()
    #: Del/X: id выделенных источников. Окно удаляет их пачкой — одним
    #: сообщением серверу и одной записью в истории.
    delete_requested = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        # ExtendedSelection даёт и обычный клик, и Shift/Ctrl, и рамку.
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionRectVisible(True)
        # Перетаскивание отключено: иначе Qt начинает перенос строки и
        # рамка выделения не рисуется.
        self.setDragEnabled(False)
        self.setAcceptDrops(False)
        self.setDragDropMode(QAbstractItemView.NoDragDrop)
        self._press_pos = None
        self._pressed_blank = False

    def selected_light_ids(self) -> list:
        """id всех выделенных источников — в порядке строк списка."""
        ids = []
        for item in self.selectedItems():
            light_id = item.data(Qt.UserRole)
            if light_id:
                ids.append(str(light_id))
        return ids

    def keyPressEvent(self, event):
        """Del и X удаляют всё выделенное — как в списках треков."""
        if (event_key(event) in (Qt.Key_Delete, Qt.Key_X)
                and self.selected_light_ids()):
            self.delete_requested.emit(self.selected_light_ids())
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        self._press_pos = event.pos()
        self._pressed_blank = self.itemAt(event.pos()) is None
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if (self._pressed_blank and self._press_pos is not None
                and (event.pos() - self._press_pos).manhattanLength()
                < QApplication.startDragDistance()):
            # Короткий клик по пустому месту снимает выделение; протяжка
            # по пустому месту — это рамка, её не трогаем.
            self.clearSelection()
            self.setCurrentRow(-1)
            self.blank_clicked.emit()
        self._press_pos = None
        self._pressed_blank = False


class HeightGrip(QWidget):
    """Полоска-ручка под списком: тянется мышью и меняет его высоту.

    Список источников света больше не упирается в 176 px: в мире бывает
    несколько десятков факелов, и в пяти видимых строках нужный не найти,
    когда всё остальное занимает остальное место вкладки. Ручка стоит
    прямо под списком — связь «тяну здесь — растёт список» очевидна.
    Двойной клик возвращает обычную высоту.

    Высота списка задаётся жёстко (setFixedHeight), потому что вкладка
    лежит в QScrollArea: раскрученный список увеличивает содержимое, и
    вкладка прокручивается, а не сжимает соседние группы.
    """

    #: Новая высота цели в пикселях (после округления и ограничений).
    heightChanged = pyqtSignal(int)

    #: Обычная высота списка — пять строк без прокрутки.
    DEFAULT_HEIGHT = 176
    #: Меньше двух с половиной строк список не имеет смысла.
    MIN_HEIGHT = 90

    def __init__(self, target: QWidget, default_height: int = None,
                 parent=None):
        super().__init__(parent)
        self.target = target
        self.default_height = int(default_height or self.DEFAULT_HEIGHT)
        self.setObjectName("height_grip")
        self.setFixedHeight(9)
        self.setCursor(Qt.SizeVerCursor)
        self.setToolTip("Потяните вниз — список станет выше, вверх — ниже. "
                        "Двойной клик — обычная высота")
        self._press_y = None
        self._press_height = 0
        self._hover = False
        # Своя копия высоты: геометрия виджета обновляется на следующем
        # проходе раскладки, и во время протяжки target.height() отстаёт —
        # сравнивать с ним значило бы глотать движения мыши.
        self._height = max(int(target.height()), self.MIN_HEIGHT)

    def paintEvent(self, event):
        """Три коротких штриха по центру: ручку видно, но она не шумит."""
        painter = QPainter(self)
        color = self.palette().color(self.foregroundRole())
        color.setAlpha(130 if self._hover else 70)
        painter.setPen(QPen(color, 2, Qt.SolidLine, Qt.RoundCap))
        center = self.width() // 2
        y = self.height() // 2
        for offset in (-12, 0, 12):
            painter.drawLine(center + offset - 4, y, center + offset + 4, y)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        # Точка отсчёта — в глобальных координатах: ручка внутри прокрутки
        # сама может ехать, и локальные координаты тогда врут.
        self._press_y = self.mapToGlobal(event.pos()).y()
        self._press_height = self._height
        event.accept()

    def mouseMoveEvent(self, event):
        if self._press_y is None:
            super().mouseMoveEvent(event)
            return
        delta = self.mapToGlobal(event.pos()).y() - self._press_y
        self.set_target_height(self._press_height + delta)

    def mouseReleaseEvent(self, event):
        self._press_y = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        self._press_y = None
        self.set_target_height(self.default_height)
        event.accept()

    def set_target_height(self, height: int) -> None:
        """Поставить высоту цели (с ограничениями) и сообщить об этом."""
        height = max(self.MIN_HEIGHT, int(height))
        if self._height == height:
            return
        self._height = height
        self.target.setFixedHeight(height)
        self.heightChanged.emit(height)


class MainWindow(QMainWindow):
    """Главное окно игры"""

    # Сетевые сообщения приходят из слушающего потока. Пробрасываем их
    # в поток GUI через сигнал, иначе Qt-виджеты трогать нельзя.
    net_message = pyqtSignal(object, object)

    def __init__(self, connection_info, network_client, server=None):
        super().__init__()

        self.connection_info = connection_info
        self.network_client = network_client
        self.server = server

        self.player_id = connection_info['player_id']
        self.nickname = connection_info['nickname']
        self.is_host = connection_info['mode'] == 'host'
        as_dm = self.is_host or connection_info.get('role') == 'dm'
        self.player_role = Role.DM if as_dm else Role.PLAYER
        self.avatar_path = connection_info.get('avatar_path')
        # Галочка из стартового окна: вместо кнопок — физический лоток
        # Dice Lab в отдельном окне. Окно создаётся лениво: panda3d
        # загружается только когда 3D-кубики действительно нужны.
        self.enable_3d = bool(connection_info.get('enable_3d', False))
        self._dice_window = None
        # Галочка «Эффекты в бою» из настроек мастера: кровь и звуки.
        # Анимация удара (выпад, вспышка, число) от неё не зависит.
        self.combat_effects_enabled = bool(
            connection_info.get('combat_effects', True))
        # Прозрачность крови: 0 — обычная, 100 — кровь не видно совсем.
        self.blood_transparency = int(
            connection_info.get('blood_transparency', 0) or 0)
        # Предзагрузка «Моих актёров» игрокам: карточки и картинки едут
        # заранее, и призыв не ждёт передачи файла.
        self.preload_my_actors = bool(
            connection_info.get('preload_my_actors', False))
        # Модуль освещения — галочка стартового окна. Он же включает вкладку
        # справа; правила темноты считает сервер (см. GameServer).
        self.lighting_enabled = bool(
            connection_info.get('lighting_enabled', False))
        self._lighting_tab_index = None
        # Что игроку видно цветным: "around" — всё вокруг токена, "cone" —
        # только сектор обзора. Настройка мастера, хранится в settings.json.
        self.vision_mode = str(
            connection_info.get('vision_mode', 'around') or 'around')
        # Мастер спрятал у себя стены и окна (игра на телевизоре): двери
        # остаются видны.
        self.hide_dm_walls = bool(
            connection_info.get('hide_dm_walls', False))
        # Туман войны у мастера: видит от лица всех токенов игроков.
        self.dm_fog = bool(connection_info.get('dm_fog', False))
        # Движение токена с клавиатуры (WASD): необязательный режим у
        # каждого клиента свой, поэтому хранится в settings.json, а не
        # на сервере. У игрока ведёт своего персонажа, у мастера —
        # выделенный токен.
        self.wasd_movement = bool(connection_info.get('wasd_move', False))
        # Настройки графики из стартового окна: качество теней, сглаживание,
        # отсвет и движок. Движок применяется только после перезапуска.
        self.graphics_settings = graphics_rules.normalize(
            connection_info.get('graphics'))
        self._graphics_dialog = None
        # Короткие передышки между долгими отдыхами (5e): считает мастер,
        # потому что отдых — событие стола, а не одного листа.
        self.short_rests_left = SHORT_REST_MAX
        self.short_rests_max = SHORT_REST_MAX
        self._rest_dialog = None
        self.preloaded_actors = {}
        self.preloaded_actor_images = {}
        self._preloaded_actor_ids = set()

        self.game_state = GameState(session_id=generate_id())
        self.audio = AudioSystem()
        # Звук окончательной смерти. Токены, для которых он уже прозвучал,
        # держим отдельно: сервер присылает dead=true и в UPDATE_TOKEN, и в
        # DEATH_SAVE_RESULT/ATTACK_RESULT, а звук должен быть один.
        self._dead_sound_tokens = set()
        self._death_sound_id = "death_return"
        # Лечение звучит мягким подъёмом, а не ударом. Файл лежит рядом со
        # звуками атаки и подхватывается сам, если его заменили.
        self._heal_sound_id = "heal_return"
        # Шелест страницы: отклик на «Применить» в книге заклинаний. Это
        # звук интерфейса, а не заклинания, поэтому своё id и свой файл.
        self._page_sound_id = "sheet_page"
        self._page_sound_time = 0.0
        sound_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "assets", "sounds")
        for sound_name in ("book_page.wav", "book_page.ogg",
                           "book_page.mp3"):
            sound_path = os.path.join(sound_dir, sound_name)
            if os.path.isfile(sound_path):
                self.audio.load_sound(sound_path, self._page_sound_id,
                                      "Страница книги")
                break
        # Скрип двери: открытие и закрытие разными звуками. Дверь толкает
        # тот, кто идёт, а слышат все — звук играется по эху сервера.
        self._door_open_sound_id = "door_open"
        self._door_close_sound_id = "door_close"
        for sound_id, base in ((self._door_open_sound_id, "door_open"),
                               (self._door_close_sound_id, "door_close")):
            for extension in ("wav", "ogg", "mp3"):
                sound_path = os.path.join(sound_dir, base + "." + extension)
                if os.path.isfile(sound_path):
                    self.audio.load_sound(sound_path, sound_id, "Дверь")
                    break
        for sound_name in ("heal.wav", "heal.ogg", "heal.mp3"):
            sound_path = os.path.join(sound_dir, sound_name)
            if os.path.isfile(sound_path):
                self.audio.load_sound(sound_path, self._heal_sound_id,
                                      "Лечение")
                break
        for sound_name in ("death.ogg", "death.mp3"):
            sound_path = os.path.join(sound_dir, sound_name)
            if os.path.isfile(sound_path):
                self.audio.load_sound(sound_path, self._death_sound_id,
                                      "Смерть")
                break
        # Звуки ударов: у промаха, попадания и крита свои наборы. Из набора
        # каждый раз выбирается случайный файл — один и тот же звук на
        # каждый удар быстро приедается.
        self._attack_sounds = {"miss": [], "hit": [], "crit": []}
        self._load_attack_sounds(os.path.join(sound_dir, "attack"))
        # Звуки заклинаний: имя файла = ключ отклика (fireball_cast.wav,
        # magic_missile.wav, shield.wav…). Новый файл подхватывается сам.
        self._spell_sounds = {}
        # Один каст на три цели не должен давать три наложенных звука.
        self._spell_sound_times = {}
        self._load_spell_sounds(os.path.join(sound_dir, "spells"))
        self.cache_dir = asset_utils.get_cache_dir()
        # id ресурса -> локальный путь после приёма по сети
        self.local_assets = {}
        # Листы персонажей: id игрока -> словарь в формате LSS.
        # Старая модель (совместимость с выданными предметами).
        # Словари сами сообщают холсту, что лист поменялся: от него зависит
        # тёмное зрение, а лист приезжает уже после токенов.
        self.character_sheets = SheetStore(self._sheets_changed)
        # Листы по id ТОКЕНА — новая модель: лист принадлежит токену,
        # переезжает с ним между картами и лежит в saves/<сессия>/tokens/<id>/.
        self.token_sheets = SheetStore(self._sheets_changed)
        # Буфер обмена токенов для Ctrl+C / Ctrl+V. Храним id источника и
        # смещения группы, а не копию данных: сервер сам делает копию и
        # проверяет права, поэтому подделать чужие хиты нельзя.
        self._token_clipboard = None
        #: Заклинание с областью ждёт второго клика по карте: расход
        #: ячейки происходит только после подтверждения шаблона.
        self._pending_area_action = None
        #: Заклинание ждёт клика по цели на холсте (оно взведено, а не
        #: провалено): расход — только после выбора.
        self._pending_target_action = None
        # Правки листов со страниц вкладки «Лист персонажа»: копим и шлём
        # одним пакетом — так же, как правки листа игрока.
        self._page_sheet_dirty = {}
        # Что мы сами отправляли как свой лист: эхо этого же листа не должно
        # перерисовывать виджет и сбивать курсор в полях.
        self._sheet_sent = None
        # Слоты игроков: id слота -> {name, player_id, token_ids}.
        self.slots = {}
        self._slot_prompted = False
        self.map_rows = []      # [(map_id, name)]
        self.token_rows = []    # [(token_id, name)]
        self.token_eye_buttons = {}   # token_id -> кнопка-глазок в списке
        self.token_row_labels = {}    # token_id -> подпись рядом с глазком
        self.initiative_rows = []      # token_id построчно в списке очереди
        self.token_kind_filter = "all"
        self._turn_focus = None        # на ком остановилось автовыделение хода
        #: Карта, только что добавленная и ещё не показанная: для неё
        #: включаем заставку загрузки даже если файл уже лежит локально.
        self._pending_map_add_id = None
        #: Смена карты, ждущая описания: CHANGE_MAP пришёл раньше ADD_MAP.
        self._pending_change_map_id = None
        #: Карта, файл которой сейчас отправляется на сервер. Заставка
        #: стартует сразу при выборе файла, а не после ответа сервера.
        self._map_upload_loading_id = None
        self.audio_rows = []    # [(audio_id, name, path)]
        # Инструменты рисования создаёт вкладка правой панели
        # (build_draw_section). Игрок её не получает, поэтому поля заводим
        # сразу — тогда обращение к ним всегда безопасно и не нужно
        # проверять наличие атрибута.
        self.draw_actions = {}
        self._panel_sizes = None   # запомненная ширина панелей (для QSplitter)
        self.music_ids = []

        self.setWindowTitle(f"Berkut VTT dnd5e — {self.nickname} "
                            f"({'DM' if self.player_role == Role.DM else 'Игрок'})")
        self.setWindowIcon(app_icon())
        self.resize(1500, 900)

        self.build_ui()
        self.apply_theme('dark')

        # Горячие клавиши окна. install_shortcut ставит сразу два QShortcut:
        # латинский и кириллический, чтобы хоткеи работали и в русской
        # раскладке (Ctrl+Z и Ctrl+Я, Q и Й, N и Т).
        self.shortcut_fullscreen = install_shortcut(
            self, "F11", self.toggle_fullscreen, auto_repeat=False)
        # Сетка: Ctrl+G — диалог, Ctrl+Shift+G — калибровка по двум точкам
        self.shortcut_grid = install_shortcut(
            self, "Ctrl+G", self.open_grid_settings)
        self.shortcut_calibrate = install_shortcut(
            self, "Ctrl+Shift+G", self.start_grid_calibration)
        # Единая отмена: Ctrl+Z откатывает последнее действие — карту, токен
        # или штрих. Ctrl+Shift+Z и Ctrl+Y возвращают отменённое.
        self.shortcut_undo = install_shortcut(
            self, "Ctrl+Z", self.undo_last_action)
        self.shortcut_redo = install_shortcut(
            self, "Ctrl+Shift+Z", self.redo_last_action)
        self.shortcut_redo2 = install_shortcut(
            self, "Ctrl+Y", self.redo_last_action)
        self.shortcut_clear = install_shortcut(
            self, "Ctrl+Shift+Delete", self.clear_all_drawings)
        # Копирование токенов: Ctrl+C запоминает выделение, Ctrl+V создаёт
        # копии на открытой карте. Работает и у мастера, и у игрока —
        # игрок копирует только своего персонажа (это проверяет сервер).
        self.shortcut_copy_tokens = install_shortcut(
            self, "Ctrl+C", self.copy_selected_tokens, auto_repeat=False)
        self.shortcut_paste_tokens = install_shortcut(
            self, "Ctrl+V", self.paste_tokens, auto_repeat=False)

        # Бой: N — передать ход следующему участнику (только у ДМ).
        # Обработчик сам проверяет фокус в поле ввода, чтобы «n»,
        # набранное в чате, не уводило ход боя.
        if self.player_role == Role.DM:
            self.shortcut_next_turn = install_shortcut(
                self, "N", self.hotkey_next_turn, auto_repeat=False)

        # Быстрая атака: Q — вкл/выкл режим, Esc — выйти из него.
        # Оба хоткея доступны и игроку, и ДМ: игрок бьёт своим токеном.
        self.shortcut_quick_attack = install_shortcut(
            self, "Q", self.toggle_quick_attack_mode, auto_repeat=False)
        # Рывок: C/С — один раз за ход удвоить скорость текущего токена.
        # Раньше здесь была D, но она занята шагом WASD, а C освободилась
        # вместе с инструментом «Круг» (он переехал на O).
        self.shortcut_dash = install_shortcut(
            self, "C", self.hotkey_dash, auto_repeat=False)
        self.shortcut_escape = install_shortcut(
            self, "Esc", self._escape_pressed, auto_repeat=False)

        # История обратимых действий. 60 шагов: глубже смысла нет,
        # а память и путаница растут.
        self.history = UndoStack(limit=60)
        self.history.add_listener(self.refresh_undo_actions)
        self.canvas.edit_finished.connect(self.on_canvas_edit_finished)
        self.net_message.connect(self.handle_message_ui)
        self.network_client.set_message_callback(self.on_network_message)
        # Кнопки отмены создаёт build_toolbar, который вызывается выше.
        # Приводим их к пустой истории сразу, чтобы они не выглядели рабочими.
        self.refresh_undo_actions()
        self.send_connect()

        # Периодическая проверка обрыва связи
        self.link_timer = QTimer(self)
        self.link_timer.timeout.connect(self.check_link)
        self.link_timer.start(2000)

    # ------------------------------------------------------------------
    # Построение интерфейса
    # ------------------------------------------------------------------
    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # Полотно создаётся первым: панель DM ссылается на него
        self.canvas = GameCanvas()
        # Качество теней — до первой карты: иначе первый же пересчёт маски
        # пошёл бы с настройками по умолчанию, а потом пересчитался заново.
        self.canvas.set_graphics(self.graphics_settings)
        self.canvas.graphics_settings_requested.connect(
            self.open_graphics_dialog)
        # Карточки бросков живут на самом холсте: бросок должен быть виден
        # там, где смотрят в бой, а не только в чате справа.
        self.roll_overlay = RollOverlay(self.canvas)
        self.canvas.set_player_info(self.player_id, self.player_role)
        self.canvas.set_combat_effects_enabled(self.combat_effects_enabled)
        self.canvas.set_blood_transparency(self.blood_transparency)
        self.canvas.set_lighting_enabled(self.lighting_enabled)
        self.canvas.set_player_vision_mode(self.vision_mode)
        self.canvas.set_hide_dm_walls(self.hide_dm_walls)
        self.canvas.set_dm_fog_enabled(self.dm_fog)
        self.canvas.set_wasd_movement(self.wasd_movement)
        # Тёмное зрение записано в листе или статблоке: ночью холсту нужен
        # доступ к листу, чтобы знать, где персонаж видит в ч/б.
        self.canvas.set_sheet_lookup(self._sheet_for_token)
        # Скорость для подсветки клеток в бою: у персонажа — с листа,
        # у существа — из статблока, иначе стандартные 30 футов.
        self.canvas.speed_resolver = self._token_speed_feet
        self.canvas.token_moved.connect(self.on_token_moved)
        self.canvas.my_actor_dropped.connect(self.summon_my_actor)
        self.canvas.item_dropped.connect(self.on_item_dropped)
        self.canvas.tokens_moved.connect(self.on_tokens_moved)
        self.canvas.token_selected.connect(self.on_token_selected)
        self.canvas.token_selection_changed.connect(
            self._restore_token_list_selection)
        self.canvas.token_deselected.connect(self.on_token_deselected)
        self.canvas.map_selected.connect(self.on_map_selected)
        self.canvas.map_deselected.connect(self.on_map_deselected)
        self.canvas.add_token_requested.connect(self.on_add_token_requested)
        self.canvas.add_map_requested.connect(lambda x, y: self.add_map_dialog())
        # Объекты карты: геометрия после драга. Настройки и замок —
        # в левой панели, на холсте их кнопок нет.
        self.canvas.object_updated.connect(self.on_object_geometry_changed)
        self.canvas.map_updated.connect(self.on_map_geometry_changed)
        self.canvas.remove_token_requested.connect(self.on_remove_token)
        self.canvas.remove_tokens_requested.connect(self.on_remove_tokens)
        self.canvas.remove_map_requested.connect(self.on_remove_map_requested)
        self.canvas.token_updated_requested.connect(self.on_update_token)
        self.canvas.grid_settings_requested.connect(self.open_grid_settings)
        # Сетку мог изменить и сам холст (калибровка) — тогда рассылаем всем.
        self.canvas.grid_changed.connect(self.on_grid_changed)
        self.canvas.files_dropped.connect(self.on_file_dropped)
        # Растровый рисунок
        self.canvas.draw_op_committed.connect(self.on_draw_op_local)
        # Текстовые надписи: холст собирает правку, сервер её подтверждает.
        self.canvas.text_added.connect(self.on_map_text_added)
        self.canvas.text_updated.connect(self.on_map_text_updated)
        self.canvas.text_removed.connect(self.on_map_text_removed)
        self.canvas.text_selected.connect(self._sync_text_panel)
        self.canvas.clear_drawings_requested.connect(self.clear_all_drawings)
        self.canvas.undo_drawing_requested.connect(self.undo_drawing)
        self.canvas.tool_changed.connect(self.on_tool_changed)
        # Глазок на токене
        self.canvas.token_visibility_toggled.connect(self.on_token_visibility_requested)
        # Хиты/КД токена (ДМ)
        self.canvas.token_hp_requested.connect(self.on_token_hp_requested)
        # День/ночь с холста — на сервер: затемнение должно быть у всех.
        self.canvas.time_of_day_changed.connect(self.on_time_of_day_changed)
        self.canvas.night_tint_changed.connect(self.on_night_tint_changed)
        self.canvas.lights_changed.connect(self.on_lights_changed)
        # Заготовку света притащили из вкладки на карту: список и подсказка.
        self.canvas.light_preset_dropped.connect(
            self._on_light_preset_dropped)
        # Del/X на холсте при открытой вкладке освещения — удалить источник.
        self.canvas.light_delete_requested.connect(self.delete_map_lights)
        # Клик по метке на холсте выделяет ту же строку в списке.
        self.canvas.light_selected.connect(self.on_canvas_light_selected)
        self.canvas.walls_changed.connect(self.on_walls_changed)
        self.canvas.wall_snap_changed.connect(self.on_wall_snap_changed)
        self.canvas.door_toggled.connect(self.on_door_toggled)
        self.canvas.door_lock_requested.connect(self.on_door_lock_requested)
        # Привязка к клеткам — настройка стола: своё значение у каждого
        # клиента расходилось с мастером, и игроки ходили мимо клеток.
        self.canvas.snap_changed.connect(self.on_snap_requested)
        # Зрение игроков — тоже настройка стола: своё значение у каждого
        # клиента расходилось с мастером.
        self.canvas.vision_mode_changed.connect(self.on_vision_requested)
        # Скрип двери — по началу хода створки, а не по эху: иначе звук
        # опаздывал на сетевой оборот (а при задержке — на полкадра).
        self.canvas.door_swing_started.connect(
            lambda _opening_id, is_open: self._play_door_sound(is_open))
        # Цвет и плотность ночи меняются ползунком: шлём не каждый пиксель
        # движения, а последнее значение после короткой паузы.
        self._pending_night_tint = None
        self._night_send_timer = QTimer(self)
        self._night_send_timer.setSingleShot(True)
        self._night_send_timer.setInterval(140)
        self._night_send_timer.timeout.connect(self._send_night_tint)
        # Плотность тумана — та же история: ползунок шлёт только последнее
        # значение после паузы, а не сто сообщений на протаскивание.
        self._pending_fog = None
        self._fog_send_timer = QTimer(self)
        self._fog_send_timer.setSingleShot(True)
        self._fog_send_timer.setInterval(140)
        self._fog_send_timer.timeout.connect(self._send_fog)
        self.canvas.fog_alpha_changed.connect(self.on_fog_alpha_changed)
        self.canvas.fog_style_changed.connect(self.on_fog_style_committed)
        # Разведка комнат: холст отметил новую — отправить серверу, чтобы
        # она сохранилась вместе с миром. Пауза — как у тумана: за один
        # шаг в комнату прилетает несколько сигналов (полоса за полосой).
        self._pending_explored = {}
        self._explored_send_timer = QTimer(self)
        self._explored_send_timer.setSingleShot(True)
        self._explored_send_timer.setInterval(200)
        self._explored_send_timer.timeout.connect(self._send_explored_rooms)
        self.canvas.explored_rooms_changed.connect(
            self.on_explored_rooms_changed)
        # Хоткей особой атаки: ключ назначен в карточке существа, а нажатие
        # ловит холст (он же знает раскладку и модификаторы).
        self.canvas.special_attack_hotkey_requested.connect(
            lambda token_id, attack_id: self.on_special_attack_request(
                {"token_id": token_id, "attack_id": attack_id}))
        # Боевые действия по токену: единая карточка актёра, атака,
        # спасбросок и состояния. Отдельной карточки существа больше нет:
        # её функции перенесены в карточку актёра.
        # Редактируемая карточка актёра (Foundry-лист) по токену.
        self.canvas.token_actor_sheet_requested.connect(self.open_actor_sheet)
        self.canvas.token_attack_requested.connect(
            lambda token_id: self.open_attack_dialog(attacker_id=token_id))
        self.canvas.token_save_requested.connect(
            lambda token_id: self.open_save_dialog(token_id=token_id))
        self.canvas.token_conditions_requested.connect(self.open_conditions_dialog)
        # Быстрая атака: клик по токену при выбранном атакующем.
        self.canvas.token_quick_attack_requested.connect(self.on_quick_attack)
        self.canvas.quick_attack_toggle_requested.connect(
            self.toggle_quick_attack_mode)
        # Холст снимает взвод сам (удар, клик мимо, Esc) — окно должно
        # узнать об этом, иначе кнопка остаётся нажатой, а плашка висеть.
        self.canvas.quick_attack_mode_changed.connect(
            lambda _active: self._reflect_quick_attack_mode())
        # Воскрешение мёртвого токена — только мастер (пункт меню).
        self.canvas.token_revive_requested.connect(self.on_death_revive)
        # Токен со скоростью 0 не тащится: холст объясняет почему.
        self.canvas.movement_blocked.connect(self.append_system)
        # Сколько футов осталось на ход — коротко в статусбаре.
        self.canvas.movement_reported.connect(
            lambda text: self.statusBar().showMessage(text, 5000))
        # Рывок считает сервер: локальный эффект показываем сразу, а затем
        # сообщаем о нём, чтобы шаг дальше обычной скорости не отклонили.
        self.canvas.dash_used.connect(self.on_dash_used)
        self.canvas.template_done.connect(self.on_template_done)
        self.canvas.template_cancelled.connect(self.on_template_cancelled)
        # Взведённое заклинание: выбор цели кликом и отмена без расхода.
        self.canvas.target_picked.connect(self.on_target_picked)
        self.canvas.target_pick_cancelled.connect(self.on_target_pick_cancelled)
        # Панели разделены QSplitter: тянем за границу, чтобы менять ширину холста.
        # Раньше ручка была не видна — стиль красил её в цвет фона (см. QSplitter::handle).
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(10)
        splitter.setChildrenCollapsible(True)
        splitter.setOpaqueResize(True)
        if self.player_role == Role.DM:
            splitter.addWidget(self.build_dm_panel())
        else:
            # Игроку — своя левая панель с листом персонажа.
            splitter.addWidget(self.build_player_panel())
        splitter.addWidget(self.canvas)
        splitter.addWidget(self.build_right_panel())
        splitter.setStretchFactor(splitter.indexOf(self.canvas), 1)
        if self.player_role == Role.DM:
            # Лист персонажа — во вкладке левой панели. Шесть характеристик
            # в ряд требуют ~390 px содержимого, поэтому стартовая ширина
            # сразу оставляет место и рейке, и листу без горизонтальной ленты.
            sizes = [480, 700, 300]
        else:
            # Лист пошире, холст — основное, справа чат поуже. 440 px —
            # минимум, при котором шесть характеристик встают в один ряд.
            sizes = [440, 760, 280]
        splitter.setSizes(sizes)
        self.splitter = splitter
        self._right_panel_width = sizes[2]
        # Запоминаем ширину панелей, чтобы не терять её при переключении темы.
        splitter.splitterMoved.connect(self._remember_panel_sizes)
        # Свёрнутая рейка оставляет только полосу иконок (56 px), открытая
        # возвращает прежнюю ширину панели. Это касается обеих панелей:
        # иначе свёрнутая правая панель держала бы пустые 300 px.
        if self.player_role == Role.DM and hasattr(self, "section_tabs"):
            self.section_tabs.collapsedChanged.connect(self._on_section_collapsed)
        if hasattr(self, "right_tabs"):
            self.right_tabs.collapsedChanged.connect(
                self._on_right_section_collapsed)
            # Маркеры света тянутся мышью только при открытой вкладке.
            self.right_tabs.currentChanged.connect(
                self._on_right_section_changed)
        root.addWidget(splitter)
        self.build_toolbar()
        self.build_log_dock()

    # --- Панель DM ---
    #: Максимальная ширина левой колонки. Раньше было 420 px: лист
    #: персонажа не влезал по ширине даже на максимуме, и шесть
    #: характеристик уходили под горизонтальную прокрутку. 560 px даёт
    #: содержимому ~500 px — с запасом на рейку разделов и полосу прокрутки.
    LEFT_PANEL_MAX_WIDTH = 560
    #: Минимум холста: уже него карта превращается в щёлочку между панелями.
    CANVAS_MIN_WIDTH = 320

    def _set_panel_width(self, index: int, width: int) -> None:
        """Поставить ширину одной боковой панели, не трогая вторую.

        Место берётся (и отдаётся) только у холста. Раньше ширина менялась
        одним элементом списка `setSizes`, сумма расходилась с реальной
        шириной сплиттера, и Qt растягивал все полосы пропорционально:
        открытие левой вкладки или разворот карточки справочника на пару
        пикселей сужали правую панель — панели «спорили» друг с другом.
        Если холсту нечего отдать, панель просто не растёт до запрошенной
        ширины, а правый сосед остаётся при своих.
        """
        splitter = getattr(self, "splitter", None)
        if splitter is None or splitter.count() < 3:
            return
        sizes = splitter.sizes()
        if len(sizes) < 3:
            return
        canvas_index = 1 if index != 1 else 0
        panel = splitter.widget(index)
        ceiling = (panel.maximumWidth() if panel is not None else 0)
        if 0 < ceiling < 16777215:
            width = min(width, ceiling)
        target = max(0, int(width))
        delta = target - sizes[index]
        if delta == 0:
            return
        if delta > 0:
            # Место берём только у холста и только до его настоящего
            # минимума: у GameCanvas он 600 px (setMinimumSize в
            # game_canvas.py), а не CANVAS_MIN_WIDTH. Если попросить больше
            # этого запаса, Qt не в силах выполнить запрос и распределяет
            # недостачу по всем панелям — тогда сужается и правая, хотя её
            # просили не трогать.
            canvas = splitter.widget(canvas_index)
            canvas_min = self.CANVAS_MIN_WIDTH
            if canvas is not None:
                canvas_min = max(canvas_min, canvas.minimumSize().width())
            room = sizes[canvas_index] - canvas_min
            if room <= 0:
                return
            delta = min(delta, room)
        sizes[index] += delta
        sizes[canvas_index] -= delta
        splitter.setSizes(sizes)

    def build_dm_panel(self):
        panel = QWidget()
        panel.setMinimumWidth(180)
        # Потолок ширины: кнопки боя и списки рассчитаны на узкую колонку.
        # Без него колонку можно было растянуть до 900 px, и половина панели
        # оставалась пустой — интерфейс выглядел развалившимся.
        panel.setMaximumWidth(self.LEFT_PANEL_MAX_WIDTH)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        # Рейка разделов: квадратные кнопки с иконками, панель справа от рейки.
        tabs = SectionRail()
        tabs.setObjectName("dm_left_tabs")
        self.section_tabs = tabs
        self.current_theme = getattr(self, "current_theme", "dark")

        # Карты
        w = QWidget()
        page = QVBoxLayout(w)
        page.setContentsMargins(0, 0, 0, 0)
        # Верхняя половина — карты, нижняя — объекты карты. Разделитель
        # двигается: мастером управляет и список карт, и слой объектов.
        split = QSplitter(Qt.Vertical)
        split.setObjectName("maps_splitter")

        maps_top = QWidget()
        l = QVBoxLayout(maps_top)
        l.setContentsMargins(2, 2, 2, 2)
        self.maps_list = QuickDeleteList()
        self.maps_list.itemClicked.connect(self.on_map_list_clicked)
        self.maps_list.itemDoubleClicked.connect(self.change_map_from_item)
        self.maps_list.delete_requested.connect(self.remove_map_row)
        l.addWidget(self.maps_list)
        row = QHBoxLayout()
        b = QPushButton("Добавить карту")
        b.clicked.connect(self.add_map_dialog)
        row.addWidget(b)
        b = QPushButton("Показать")
        b.clicked.connect(self.change_map)
        row.addWidget(b)
        l.addLayout(row)
        row = QHBoxLayout()
        b = QPushButton("Удалить")
        b.clicked.connect(self.remove_map)
        row.addWidget(b)
        l.addLayout(row)
        split.addWidget(maps_top)

        objects_page = QWidget()
        objects_page.setObjectName("map_objects_page")
        l2 = QVBoxLayout(objects_page)
        l2.setContentsMargins(2, 2, 2, 2)
        l2.setSpacing(4)
        title = QLabel("Объекты")
        title.setObjectName("map_objects_title")
        l2.addWidget(title)
        self.objects_list = QuickDeleteList()
        self.objects_list.setObjectName("map_objects_list")
        # Та же причина, что и у списка токенов: строка — свой виджет
        # с кнопками, отступы пункта обрезали их по высоте.
        self.objects_list.setStyleSheet("QListWidget::item { padding: 0px; }")
        self.objects_list.itemClicked.connect(self.on_object_list_clicked)
        self.objects_list.itemDoubleClicked.connect(self.on_object_list_activated)
        self.objects_list.delete_requested.connect(self.remove_object_row)
        l2.addWidget(self.objects_list, 1)
        row = QHBoxLayout()
        b = QPushButton("Добавить объект")
        b.setObjectName("add_map_object_btn")
        b.setToolTip("PNG поверх карты: крыша, дерево, декор")
        b.clicked.connect(self.add_map_object_dialog)
        row.addWidget(b)
        b = QPushButton("Настроить")
        b.setObjectName("object_settings_btn")
        b.clicked.connect(self.open_selected_object_settings)
        row.addWidget(b)
        l2.addLayout(row)
        b = QPushButton("Удалить")
        b.setObjectName("remove_map_object_btn")
        b.clicked.connect(self.remove_selected_map_object)
        l2.addWidget(b)
        split.addWidget(objects_page)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        # Стартовое деление — примерно пополам, как в макете панели.
        split.setSizes([280, 280])
        page.addWidget(split)

        tab_index = tabs.addTab(w, section_icon("maps", SECTION_FALLBACK["maps"]), "")
        tabs.setTabToolTip(tab_index, "Карты")

        # Один список сцены для игроков, актёров и участников боя.
        token_page = QWidget()
        l = QVBoxLayout(token_page)
        l.setContentsMargins(2, 2, 2, 2)
        overlay_row = QHBoxLayout()
        self.token_names_checkbox = QCheckBox("Имя")
        self.token_names_checkbox.setChecked(True)
        self.token_names_checkbox.setToolTip("Показывать имя токена")
        overlay_row.addWidget(self.token_names_checkbox)
        self.token_hp_checkbox = QCheckBox("Хиты")
        self.token_hp_checkbox.setChecked(True)
        self.token_hp_checkbox.setToolTip("Показывать хиты тонкой полосой здоровья")
        overlay_row.addWidget(self.token_hp_checkbox)
        self.token_selected_only_checkbox = QCheckBox("Только выбранные")
        self.token_selected_only_checkbox.setChecked(True)
        self.token_selected_only_checkbox.setToolTip(
            "Имя и хиты видны только у выделенного токена")
        overlay_row.addWidget(self.token_selected_only_checkbox)
        overlay_row.addStretch(1)
        l.addLayout(overlay_row)
        self.token_kind_filter_combo = QComboBox()
        for label, value in (("Все участники", "all"),
                             ("Игроки", "player"),
                             ("Актёры / НПС", "actor")):
            self.token_kind_filter_combo.addItem(label, value)
        self.token_kind_filter_combo.currentIndexChanged.connect(
            lambda _index: self.set_token_kind_filter(
                self.token_kind_filter_combo.currentData()))
        l.addWidget(self.token_kind_filter_combo)

        self.tokens_list = QuickDeleteList()
        self.tokens_list.setObjectName("tokens_list")
        # У строк со своими виджетами нет отступов пункта: 6 px сверху и
        # снизу съедали высоту, и кнопка-глазок обрезалась. Своё правило
        # живёт на самом списке и переживает смену темы.
        self.tokens_list.setStyleSheet("QListWidget::item { padding: 0px; }")
        # Выбор нескольких токенов — Shift, Ctrl и синяя рамка по пустому
        # месту списка. Выделение сюда же уходит на холст: Ctrl+C, Delete и
        # групповое перетаскивание работают с тем, что выделено в списке.
        self.tokens_list.enable_multi_selection()
        self._syncing_token_selection = False
        self.tokens_list.itemClicked.connect(self.on_token_list_clicked)
        self.tokens_list.itemSelectionChanged.connect(
            self.on_token_list_selection_changed)
        self.tokens_list.delete_requested.connect(self.remove_token_row)
        self.tokens_list.delete_rows_requested.connect(self.remove_token_rows)
        l.addWidget(self.tokens_list, 1)
        # Боевая очередь и список объектов — один и тот же список сцены.
        self.initiative_list = self.tokens_list
        self.tokens_list.itemClicked.connect(self.on_initiative_row_clicked)
        row = QHBoxLayout()
        b = QPushButton("+ Игрок")
        b.setObjectName("add_player_token_btn")
        b.clicked.connect(lambda: self.add_token_dialog(0, 0, kind="player"))
        row.addWidget(b)
        b = QPushButton("+ Актёр")
        b.setObjectName("add_actor_token_btn")
        b.clicked.connect(lambda: self.add_token_dialog(0, 0, kind="actor"))
        row.addWidget(b)
        b = QPushButton("Удалить")
        b.clicked.connect(self.remove_selected_token)
        row.addWidget(b)
        l.addLayout(row)

        # Персонажи, которые сейчас на других картах. Строка сразу
        # предлагает перенос: у игрока один персонаж, и при смене сцены
        # фигурка переезжает целиком — со своим листом, хитами и КД.
        self.away_tokens_box = QWidget()
        self.away_tokens_box.setObjectName("away_tokens_box")
        away_l = QVBoxLayout(self.away_tokens_box)
        away_l.setContentsMargins(0, 2, 0, 0)
        away_l.setSpacing(2)
        self.away_tokens_title = QLabel("Персонажи на других картах")
        self.away_tokens_title.setObjectName("away_tokens_title")
        self.away_tokens_title.setToolTip(
            "Один персонаж — один токен: он переезжает на эту карту, "
            "а не копируется")
        away_l.addWidget(self.away_tokens_title)
        self.away_tokens_list = QuickDeleteList()
        self.away_tokens_list.setObjectName("away_tokens_list")
        self.away_tokens_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.away_tokens_list.setFocusPolicy(Qt.NoFocus)
        self.away_tokens_list.setMaximumHeight(96)
        away_l.addWidget(self.away_tokens_list)
        self.away_tokens_box.setVisible(False)
        l.addWidget(self.away_tokens_box)

        l.addWidget(QLabel("Владелец выбранного токена:"))
        self.owner_combo = QComboBox()
        self.owner_combo.addItem("— никому (DM) —", None)
        l.addWidget(self.owner_combo)
        b = QPushButton("Назначить владельца")
        b.clicked.connect(self.assign_owner)
        l.addWidget(b)

        # Размер токена — ползунком в процентах: две кнопки «Увеличить» и
        # «Уменьшить» меняли размер рывками и не показывали, где ты сейчас.
        # 0% — 1/10 клетки, 100% — ровно клетка, 500% — пять клеток.
        size_box = QWidget()
        sl = QVBoxLayout(size_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(2)
        head = QHBoxLayout()
        head.setSpacing(6)
        size_title = QLabel("Размер токена")
        head.addWidget(size_title)
        head.addStretch(1)
        self.token_size_label = QLabel("—")
        self.token_size_label.setObjectName("token_size_value")
        head.addWidget(self.token_size_label)
        sl.addLayout(head)
        self.token_size_slider = QSlider(Qt.Horizontal)
        self.token_size_slider.setRange(0, 500)
        self.token_size_slider.setValue(100)
        self.token_size_slider.setSingleStep(5)
        self.token_size_slider.setPageStep(25)
        self.token_size_slider.setToolTip(
            "Размер токена: 100% — одна клетка, 500% — пять")
        self.token_size_slider.valueChanged.connect(self.on_token_size_slider)
        self.token_size_slider.setEnabled(False)
        sl.addWidget(self.token_size_slider)
        l.addWidget(size_box)
        self.build_initiative_section(l)
        for checkbox in (self.token_names_checkbox, self.token_hp_checkbox,
                         self.token_selected_only_checkbox):
            checkbox.stateChanged.connect(self.apply_token_overlay_options)
        self.apply_token_overlay_options()
        tab_index = tabs.addTab(token_page, section_icon("tokens", SECTION_FALLBACK["tokens"]), "")
        tabs.setTabToolTip(tab_index, "Токены и бой")

        # Звук
        w = QWidget()
        l = QVBoxLayout(w)
        # Звуки — не плейлист: очереди и порядка у них нет, каждый звук
        # играет ровно один раз по нажатию.
        self.audio_list = AudioListWidget(play_label="▶ Играть")
        self.audio_list.itemDoubleClicked.connect(
            lambda item: self.play_sound_row(self.audio_list.row(item)))
        self.audio_list.delete_requested.connect(self.remove_sounds)
        self.audio_list.play_requested.connect(self.play_sound_row)
        l.addWidget(self.audio_list)
        row = QHBoxLayout()
        b = QPushButton("+ Звук (MP3/WAV)")
        b.setObjectName("sound_add_btn")
        b.clicked.connect(self.add_sound)
        row.addWidget(b)
        b = QPushButton("+ Папка")
        b.setObjectName("sound_folder_btn")
        b.clicked.connect(self.add_sound_folder)
        row.addWidget(b)
        l.addLayout(row)
        row = QHBoxLayout()
        b = QPushButton("Играть")
        b.setObjectName("sound_play_btn")
        b.clicked.connect(self.play_selected_sound)
        row.addWidget(b)
        # Кнопка удаления: у звуков её раньше не было вовсе.
        b = QPushButton("Удалить")
        b.setObjectName("sound_delete_btn")
        b.clicked.connect(self.remove_selected_sounds)
        row.addWidget(b)
        l.addLayout(row)

        l.addWidget(QLabel("Музыкальный плейлист:"))
        self.music_list = AudioListWidget()
        self.music_list.itemDoubleClicked.connect(lambda item: self.play_music())
        self.music_list.delete_requested.connect(self.remove_music_rows)
        l.addWidget(self.music_list)

        # Ползунок перемотки прямо под списком треков
        row = QHBoxLayout()
        self.music_pos_label = QLabel("0:00")
        self.music_pos_label.setFixedWidth(40)
        row.addWidget(self.music_pos_label)
        self.music_seek = QSlider(Qt.Horizontal)
        self.music_seek.setRange(0, 1000)   # промилле длительности
        self.music_seek.setValue(0)
        self.music_seek.setToolTip("Перемотка трека")
        self.music_seek.setEnabled(False)
        self.music_seek.sliderPressed.connect(self._on_seek_pressed)
        self.music_seek.sliderReleased.connect(self.on_music_seek)
        row.addWidget(self.music_seek)
        self.music_len_label = QLabel("0:00")
        self.music_len_label.setFixedWidth(40)
        row.addWidget(self.music_len_label)
        l.addLayout(row)

        # Обновляем позицию дважды в секунду, пока играет музыка
        self.music_seek_active = False
        self.music_timer = QTimer(self)
        self.music_timer.setInterval(500)
        self.music_timer.timeout.connect(self.update_music_position)
        self.music_timer.start()
        row = QHBoxLayout()
        b = QPushButton("+ Трек")
        b.clicked.connect(self.add_music)
        row.addWidget(b)
        b = QPushButton("+ Папка")
        b.clicked.connect(self.add_music_folder)
        row.addWidget(b)
        b = QPushButton("Удалить")
        b.clicked.connect(self.remove_music)
        row.addWidget(b)
        l.addLayout(row)
        row = QHBoxLayout()
        b = QPushButton("Вверх")
        b.clicked.connect(lambda: self.move_music(-1))
        row.addWidget(b)
        b = QPushButton("Вниз")
        b.clicked.connect(lambda: self.move_music(1))
        row.addWidget(b)
        l.addLayout(row)
        row = QHBoxLayout()
        b = QPushButton("▶ Играть")
        b.clicked.connect(self.play_music)
        row.addWidget(b)
        b = QPushButton("⏹ Стоп")
        b.clicked.connect(self.stop_music)
        row.addWidget(b)
        b = QPushButton("⏭ След.")
        b.clicked.connect(self.next_music)
        row.addWidget(b)
        l.addLayout(row)

        # Повтор и текущий трек
        row = QHBoxLayout()
        self.repeat_btn = QPushButton("🔁 Повтор: ВКЛ")
        self.repeat_btn.setCheckable(True)
        self.repeat_btn.setChecked(True)
        self.repeat_btn.clicked.connect(self.toggle_repeat)
        row.addWidget(self.repeat_btn)
        l.addLayout(row)
        self.now_playing_label = QLabel("♪ —")
        self.now_playing_label.setWordWrap(True)
        l.addWidget(self.now_playing_label)
        self.audio.music_track_changed.connect(
            lambda name: self.now_playing_label.setText(f"♪ {name}"))
        self.audio.music_stopped.connect(
            lambda: self.now_playing_label.setText("♫ —"))
        # Раньше сбой воспроизведения уходил в никуда: сигнал никто не слушал,
        # и наружу это выглядело как «нажимаю — ничего не происходит».
        self.audio.music_error.connect(self.on_music_error)
        self._last_music_error = None

        tab_index = tabs.addTab(w, section_icon("music", SECTION_FALLBACK["music"]), "")
        tabs.setTabToolTip(tab_index, "Музыка")


        # Справочник D&D 5e (SRD): монстры, предметы, заклинания
        self.build_compendium_tab(tabs)

        # «Мои актёры»: личная библиотека мастера между сессиями.
        self.my_actors_page = MyActorsPage()
        self.my_actors_page.summonRequested.connect(self.summon_my_actor)
        self.my_actors_page.libraryChanged.connect(self.on_my_actors_changed)
        tab_index = tabs.addTab(
            self.my_actors_page, section_icon("my_actors", "Мои актёры"), "")
        tabs.setTabToolTip(tab_index, "Мои актёры")

        # Лист персонажа — самая нижняя вкладка рейки: страница на каждого
        # токена игрока, лист грузится кнопкой прямо во вкладку, без окон.
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(0)
        self.sheet_pages = SheetPages(w, sheet_provider=self._page_sheet)
        # Хиты в листе правит мастер: у игрока это число, а не поле ввода.
        self.sheet_pages.set_hp_editable(self.player_role == Role.DM)
        # Размер токена — настройка стола, а не листа: только мастеру.
        self.sheet_pages.set_size_editable(self.player_role == Role.DM)
        self.sheet_pages.sheetLoaded.connect(self.on_page_sheet_loaded)
        self.sheet_pages.sheetEdited.connect(self.on_page_sheet_edited)
        self.sheet_pages.resourceSpent.connect(self.on_sheet_resource_spent)
        self.sheet_pages.sheetAudited.connect(self.on_sheet_audited)
        self.sheet_pages.rollRequested.connect(self.on_sheet_roll)
        self.sheet_pages.useActionRequested.connect(self.on_use_action)
        self.sheet_pages.effectRequested.connect(self.on_sheet_effect)
        # Страница конкретного токена: зелье лечит/ускоряет именно его, а не
        # «первый свой токен» (у мастера своих токенов нет вовсе).
        self.sheet_pages.effectRequestedForToken.connect(
            self.on_sheet_effect_for_token)
        self.sheet_pages.attackRequested.connect(self.on_sheet_attack)
        self.sheet_pages.deathSaveRollRequested.connect(self.on_death_save_roll)
        self.sheet_pages.deathSaveSetRequested.connect(self.on_death_save_set)
        self.sheet_pages.deathReviveRequested.connect(self.on_death_revive)
        self.sheet_pages.deathStabilizeRequested.connect(
            self.on_death_stabilize)
        self.sheet_pages.conditionsChanged.connect(self.on_page_conditions_changed)
        self.sheet_pages.stealthCheckRequested.connect(self.on_stealth_check)
        self.sheet_pages.tokenSizeChanged.connect(
            self.on_page_token_size_changed)
        self.sheet_pages.tokenVisionChanged.connect(
            self.on_page_vision_changed)
        l.addWidget(self.sheet_pages, 1)
        tab_index = tabs.addTab(
            w, section_icon("sheet", SECTION_FALLBACK["sheet"],
                            getattr(self, "current_theme", "dark")), "")
        tabs.setTabToolTip(tab_index, "Лист персонажа")
        self._sheet_tab_index = tab_index
        self.sync_token_pages()

        # Заголовок открытого раздела: подсказки кнопок заданы выше, можно

        # взять название первого раздела.

        if tabs.count():

            tabs.setTabToolTip(0, tabs.tabText(0))

            tabs.section_title.setText(tabs.tabText(0).upper())

        layout.addWidget(tabs)

        return panel

    # --- Левая панель игрока ---
    def build_player_panel(self):
        """Левая панель игрока: лист персонажа на всю высоту.

        Раньше лист лежал в правой колонке вместе с чатом и списком игроков.
        Там ему оставалось меньше трети высоты, и навыки уходили под
        прокрутку. Здесь лист занимает всю панель — видно всё сразу, а
        прокрутка нужна только на низких экранах.
        """
        panel = QWidget()
        panel.setObjectName("player_panel")
        panel.setMinimumWidth(280)
        panel.setMaximumWidth(560)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.character_sheet = CharacterSheet()
        self.character_sheet.rollRequested.connect(self.on_sheet_roll)
        self.character_sheet.useActionRequested.connect(self.on_use_action)
        self.character_sheet.effectRequested.connect(self.on_sheet_effect)
        self.character_sheet.attackRequested.connect(self.on_sheet_attack)
        self.character_sheet.deathSaveRollRequested.connect(
            self.on_death_save_roll)
        self.character_sheet.deathSaveSetRequested.connect(
            self.on_death_save_set)
        self.character_sheet.deathReviveRequested.connect(
            self.on_death_revive)
        self.character_sheet.deathStabilizeRequested.connect(
            self.on_death_stabilize)
        self.character_sheet.sheetChanged.connect(self.on_sheet_changed)
        self.character_sheet.resourceSpent.connect(
            self.on_sheet_resource_spent)
        self.character_sheet.auditAdded.connect(self.on_sheet_audited)
        self.character_sheet.conditionsChanged.connect(
            self.on_sheet_conditions_changed)
        self.character_sheet.stealthCheckRequested.connect(
            self.on_stealth_check)
        layout.addWidget(self.character_sheet, 1)

        # Совместимость: раньше лист был в раскрывающейся группе, и код
        # (и отладчик) дёргал setChecked. Теперь группа не нужна, но
        # вызовы не должны падать — держим заглушку с тем же поведением.
        self.sheet_group = _AlwaysOpenGroup(panel)
        return panel

    # --- Правая панель ---
    def build_right_panel(self):
        """Правая панель — рейка вкладок: чат, игроки, звук, рисование.

        Раньше это была одна колонка из четырёх групп: высоту делили список
        игроков и чат, а инструменты рисования ютились выпадающим меню в
        верхней рейке. Теперь у панели свои вкладки, как у любой Foundry-
        панели: чат открыт по умолчанию, ниже игроки, звук и тема, а
        последняя — «Рисование» с полной панелью инструментов.
        """
        panel = QWidget()
        panel.setObjectName("right_panel")
        panel.setMinimumWidth(190)
        panel.setMaximumWidth(900)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        theme = getattr(self, "current_theme", "dark")
        tabs = SectionRail(panel, rail_on_right=True)
        self.right_tabs = tabs
        # Индекс вкладки рисования: у игрока её нет, поэтому None.
        self._draw_tab_index = None
        # Индекс вкладки освещения: только мастер и только при включённом
        # галочкой модуле.
        self._lighting_tab_index = None

        # --- Чат ---
        w = QWidget()
        # Страница чата не должна сжиматься под одну кнопку «Кубики»:
        # иначе при переключении вкладок панель прыгает по ширине.
        w.setMinimumWidth(212)
        l = QVBoxLayout(w)
        l.setContentsMargins(8, 8, 8, 8)
        l.setSpacing(6)
        self.chat_view = QTextEdit()
        self.chat_view.setReadOnly(True)
        # Минимум задаётся содержимому: QWidget.setMinimumWidth не входит
        # в minimumSizeHint(), а именно его сравнивает тест вкладок.
        self.chat_view.setMinimumWidth(208)
        l.addWidget(self.chat_view, 1)
        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText(
            "Сообщение… (/roll 1d20+5 adv dc 15, /tell Имя текст)")
        self.chat_input.returnPressed.connect(self.send_chat)
        l.addWidget(self.chat_input)
        # Кубики: по умолчанию кнопки из 2.9, а при включённой галочке —
        # кнопка «Бросить кубики», открывающая лоток Dice Lab отдельным окном.
        # Виджет строится отдельно: галочка в настройках мастера меняет его
        # на лету, без пересборки всей правой панели.
        self._chat_layout = l
        l.addWidget(self._build_dice_widget())
        tab_index = tabs.addTab(w, tool_icon("tab_chat", theme), "")
        tabs.setTabToolTip(tab_index, "Чат")
        self._chat_tab_index = tab_index

        # --- Игроки ---
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(8, 8, 8, 8)
        self.players_list = QListWidget()
        self.players_list.setIconSize(QSize(36, 36))
        l.addWidget(self.players_list, 1)
        tab_index = tabs.addTab(w, tool_icon("tab_players", theme), "")
        tabs.setTabToolTip(tab_index, "Игроки")

        # --- Звук (громкость у каждого своя) и тема ---
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(8, 8, 8, 8)
        l.setSpacing(8)
        l.addWidget(QLabel("Громкость (только у вас)"))
        row = QHBoxLayout()
        row.addWidget(QLabel("Музыка:"))
        self.music_vol = QSlider(Qt.Horizontal)
        self.music_vol.setRange(0, 100)
        self.music_vol.setValue(70)
        self.music_vol.valueChanged.connect(lambda v: self.audio.set_music_volume(v / 100))
        row.addWidget(self.music_vol, 1)
        l.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Звуки:"))
        self.sound_vol = QSlider(Qt.Horizontal)
        self.sound_vol.setRange(0, 100)
        self.sound_vol.setValue(70)
        self.sound_vol.valueChanged.connect(lambda v: self.audio.set_sound_volume(v / 100))
        row.addWidget(self.sound_vol, 1)
        l.addLayout(row)
        l.addSpacing(6)
        l.addWidget(QLabel("Тема оформления"))
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Тёмная тема", "Светлая тема"])
        self.theme_combo.currentIndexChanged.connect(
            lambda i: self.apply_theme('dark' if i == 0 else 'light'))
        l.addWidget(self.theme_combo)
        l.addStretch(1)
        tab_index = tabs.addTab(w, tool_icon("tab_sound", theme), "")
        tabs.setTabToolTip(tab_index, "Звук и тема")

        # --- Рисование: последняя вкладка и только у мастера ---
        # Сервер принимает DRAW_OP только от DM (dm_only), поэтому игрок не
        # получает вкладку вовсе: кнопки, которые отвечают «доступ запрещён»,
        # хуже, чем их отсутствие.
        if self.player_role == Role.DM:
            self._draw_tab_index = self.build_draw_section(tabs, theme)

        # --- Освещение: самая нижняя вкладка, только у мастера и только
        # когда модуль включён галочкой в стартовом окне.
        if self.player_role == Role.DM and self.lighting_enabled:
            self.build_lighting_section(tabs, theme)

        layout.addWidget(tabs)
        return panel

    def build_draw_section(self, tabs: "SectionRail", theme: str = "dark") -> int:
        """Вкладка «Рисование»: инструменты сеткой, как в панели Photoshop.

        Раньше это было выпадающее меню в верхней рейке: на инструмент
        приходилось два клика (открыть меню, выбрать), а самолётный значок
        кисти ничего не говорил. Теперь каждый инструмент — кнопка с
        иконкой, а цвет, толщина и непрозрачность видны сразу.
        """
        page = QWidget()
        self.draw_page = page
        l = QVBoxLayout(page)
        l.setContentsMargins(8, 8, 8, 8)
        l.setSpacing(8)

        # Порядок как в панели Photoshop: кисть и ластик, затем рисование
        # линий, и ниже — фигуры.
        tools = (("brush", "Кисть", "Свободное рисование (B)"),
                 ("eraser", "Ластик", "Стереть часть рисунка (E)"),
                 ("line", "Линия", "Прямая линия (L)"),
                 ("polygon", "Фигура по точкам",
                  "Клик — вершина, клик по первой точке замыкает (F)"),
                 ("square", "Прямоугольник", "Рамка — растяните мышью (R)"),
                 ("circle", "Эллипс", "Овал — растяните мышью (C)"),
                 ("text", "Текст",
                  "Клик по карте — новая надпись (T), клик по надписи — правка"),
                 ("move", "Перемещение надписей",
                  "Таскать надписи по карте, не меняя текст (Н)"))

        # Инструменты — действия: у кнопки остаются иконка, подсказка и
        # галочка активного; галочку ставит _sync_tool_buttons.
        self.draw_actions = {}
        self.draw_buttons = {}
        grid = QGridLayout()
        grid.setSpacing(4)
        for i, (mode, label, hint) in enumerate(tools):
            act = QAction(tool_icon(DRAW_TOOL_ICONS[mode], theme), label, self)
            act.setToolTip(f"{label}: {hint}")
            act.setCheckable(True)
            act.triggered.connect(lambda _, m=mode: self.activate_tool(m))
            self.draw_actions[mode] = act
            btn = QToolButton(page)
            btn.setObjectName("draw_tool")
            btn.setDefaultAction(act)
            btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
            btn.setIconSize(QSize(22, 22))
            # Кнопки тянутся по ширине панели: три ровные колонки, как на
            # панели инструментов в Photoshop, а не кучка в левом углу.
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.setMinimumSize(38, 38)
            grid.addWidget(btn, i // 3, i % 3)
            self.draw_buttons[mode] = btn
        for col in range(3):
            grid.setColumnStretch(col, 1)
        l.addLayout(grid)

        # Настройки надписи: показываем всегда, а не только при активном
        # инструменте «Текст». Иначе выбрать надпись и поменять ей шрифт
        # можно было бы только угадав порядок кликов.
        l.addWidget(self._build_text_group(page, theme))

        # Параметры кисти — своим блоком: без заголовка они читались как
        # продолжение настроек текста, а два образца цвета рядом путали.
        brush_group = QGroupBox("Кисть", page)
        brush_group.setObjectName("panel_group")
        brush_layout = QVBoxLayout(brush_group)
        brush_layout.setContentsMargins(6, 12, 6, 6)
        brush_layout.setSpacing(6)

        # Цвет и заливка
        row = QHBoxLayout()
        row.setSpacing(6)
        self.color_button = QToolButton(brush_group)
        self.color_button.setObjectName("draw_color")
        self.color_button.setFixedSize(46, 30)
        self.color_button.setToolTip(
            "Цвет кисти. Двойной клик по образцу открывает палитру")
        self.color_button.clicked.connect(self.pick_draw_color)
        self._refresh_color_button()
        row.addWidget(self.color_button)
        self.fill_check = QCheckBox("Заливка")
        self.fill_check.setToolTip("Заливать фигуры полупрозрачным цветом")
        self.fill_check.setChecked(self.canvas.fill_shapes)
        self.fill_check.toggled.connect(self.canvas.set_fill_shapes)
        row.addWidget(self.fill_check)
        row.addStretch(1)
        brush_layout.addLayout(row)

        # Толщина и непрозрачность
        row = QHBoxLayout()
        row.addWidget(QLabel("Толщина"))
        self.brush_slider = QSlider(Qt.Horizontal)
        self.brush_slider.setRange(1, 60)
        self.brush_slider.setValue(int(self.canvas.brush_size))
        self.brush_slider.valueChanged.connect(self.on_brush_size_changed)
        row.addWidget(self.brush_slider, 1)
        self.brush_label = QLabel(f"{int(self.canvas.brush_size)} px")
        self.brush_label.setFixedWidth(40)
        row.addWidget(self.brush_label)
        brush_layout.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("Непрозр."))
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(5, 100)
        self.opacity_slider.setValue(int(self.canvas.brush_opacity * 100))
        self.opacity_slider.valueChanged.connect(self.on_opacity_changed)
        row.addWidget(self.opacity_slider, 1)
        self.opacity_label = QLabel(f"{int(self.canvas.brush_opacity * 100)}%")
        self.opacity_label.setFixedWidth(40)
        row.addWidget(self.opacity_label)
        brush_layout.addLayout(row)
        l.addWidget(brush_group)

        # Действия над рисунком
        b = QPushButton("↶ Отменить штрих")
        b.setObjectName("draw_undo_btn")
        b.setToolTip("Отменить последний штрих у всех (Ctrl+Z)")
        b.clicked.connect(self.undo_drawing)
        self.draw_undo_btn = b
        l.addWidget(b)
        b = QPushButton("Стереть всё")
        b.setObjectName("draw_clear_btn")
        b.setIcon(tool_icon("clear", theme))
        b.setToolTip("Стереть рисунок на карте у всех (Ctrl+Shift+Delete)")
        b.clicked.connect(self.clear_all_drawings)
        self.clear_draw_button = b
        l.addWidget(b)

        # Две ровные строки вместо одного длинного абзаца: в узкой панели
        # он рвался посередине слова («C / эллипс»).
        hint = QLabel("B кисть · E ластик · L линия\n"
                      "R рамка · C эллипс · F фигура\n"
                      "T текст · Н перемещение")
        hint.setObjectName("draw_hint")
        hint.setWordWrap(True)
        l.addWidget(hint)
        l.addStretch(1)

        tab_index = tabs.addTab(page, tool_icon("draw", theme), "")
        tabs.setTabToolTip(tab_index, "Рисование")
        return tab_index

    def build_lighting_section(self, tabs: "SectionRail",
                               theme: str = "dark") -> int:
        """Вкладка «Освещение» — самая нижняя в правой колонке (только DM).

        Здесь живёт всё, что видно на карте ночью: день/ночь, факелы и
        их радиус. Модуль включается галочкой в стартовом окне; без неё
        вкладки нет вовсе, чтобы игрок не искал несуществующий свет.
        """
        page = QWidget()
        self.lighting_page = page
        l = QVBoxLayout(page)
        l.setContentsMargins(8, 8, 8, 8)
        l.setSpacing(8)

        mode = QGroupBox("Свет на карте")
        mode.setObjectName("panel_group")
        ml = QVBoxLayout(mode)
        ml.setContentsMargins(6, 14, 6, 6)
        ml.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.light_day_btn = QPushButton("День")
        self.light_day_btn.setCheckable(True)
        self.light_day_btn.setToolTip("Дневной свет: карта видна целиком")
        self.light_day_btn.clicked.connect(
            lambda: self.canvas.toggle_time_of_day(False))
        self.light_night_btn = QPushButton("Ночь")
        self.light_night_btn.setCheckable(True)
        self.light_night_btn.setToolTip(
            "Ночь: видно только освещённое и дальность тёмного зрения")
        self.light_night_btn.clicked.connect(
            lambda: self.canvas.toggle_time_of_day(True))
        row.addWidget(self.light_day_btn)
        row.addWidget(self.light_night_btn)
        ml.addLayout(row)

        # Цвет и плотность ночи: весь стол можно залить красным, зелёным
        # или совсем убрать затемнение. Настройка принадлежит карте.
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("Цвет ночи:"))
        self.night_color_btn = QPushButton("")
        self.night_color_btn.setToolTip(
            "Цвет затемнения всей карты: синий по умолчанию, красный — "
            "для тревожной сцены, и любой другой")
        self.night_color_btn.clicked.connect(self.pick_night_color)
        row.addWidget(self.night_color_btn, 1)
        ml.addLayout(row)

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("Плотность:"))
        self.night_alpha_slider = QSlider(Qt.Horizontal)
        self.night_alpha_slider.setRange(0, 100)
        self.night_alpha_slider.setValue(lighting_rules.DEFAULT_NIGHT_ALPHA)
        self.night_alpha_slider.setToolTip(
            "0% — ночь не затемняет карту, 100% — полностью перекрывает")
        self.night_alpha_slider.valueChanged.connect(
            self.on_night_alpha_changed)
        row.addWidget(self.night_alpha_slider, 1)
        self.night_alpha_label = QLabel(
            f"{lighting_rules.DEFAULT_NIGHT_ALPHA}%")
        self.night_alpha_label.setMinimumWidth(34)
        row.addWidget(self.night_alpha_label)
        ml.addLayout(row)
        l.addWidget(mode)

        # Туман войны: своя плотность у комнат и у мира. Комнаты по
        # умолчанию чёрные — игрок не знает, что за стеной есть помещение,
        # а открытое пространство остаётся полупрозрачным.
        fog = QGroupBox("Туман войны")
        fog.setObjectName("panel_group")
        fl = QVBoxLayout(fog)
        fl.setContentsMargins(6, 14, 6, 6)
        fl.setSpacing(6)

        # Вид тумана: ч/б, сепия, цветной или свой цвет мастера. Плотность
        # задаёт, насколько скрыто, а вид — каким цветом залито неразведанное.
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("Вид:"))
        self.fog_style_combo = QComboBox()
        self.fog_style_combo.setToolTip(
            "Цвет неразведанного: ч/б — почти чёрный, сепия — старая карта, "
            "цветной — холодный синий, свой цвет — любой на выбор")
        # Пока список наполняется, первый же элемент включает сигнал: это не
        # выбор мастера, а сборка панели, поэтому сигналы на время глушим.
        self.fog_style_combo.blockSignals(True)
        for _key, _title in (("mono", "Ч/б"), ("sepia", "Сепия"),
                             ("color", "Цветной"), ("custom", "Свой цвет")):
            self.fog_style_combo.addItem(_title, _key)
        self.fog_style_combo.blockSignals(False)
        self.fog_style_combo.currentIndexChanged.connect(
            self.on_fog_style_changed)
        row.addWidget(self.fog_style_combo, 1)
        self.fog_color_btn = QPushButton()
        self.fog_color_btn.setToolTip(
            "Цвет своей заливки: нажмите, чтобы выбрать")
        self.fog_color_btn.setVisible(False)
        self.fog_color_btn.clicked.connect(self.pick_fog_color)
        row.addWidget(self.fog_color_btn)
        fl.addLayout(row)

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("Комнаты:"))
        self.fog_room_slider = QSlider(Qt.Horizontal)
        self.fog_room_slider.setRange(0, 100)
        self.fog_room_slider.setValue(lighting_rules.DEFAULT_FOG_ROOM_ALPHA)
        self.fog_room_slider.setToolTip(
            "Неизведанные комнаты за стенами: 100% — совсем темно (так "
            "игрок не знает, что там есть помещение), 0% — видно как на карте")
        self.fog_room_slider.valueChanged.connect(
            lambda value: self.on_fog_slider_changed("room", value))
        row.addWidget(self.fog_room_slider, 1)
        self.fog_room_label = QLabel(
            f"{lighting_rules.DEFAULT_FOG_ROOM_ALPHA}%")
        self.fog_room_label.setMinimumWidth(34)
        row.addWidget(self.fog_room_label)
        fl.addLayout(row)

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("Мир:"))
        self.fog_world_slider = QSlider(Qt.Horizontal)
        self.fog_world_slider.setRange(0, 100)
        self.fog_world_slider.setValue(lighting_rules.DEFAULT_FOG_WORLD_ALPHA)
        self.fog_world_slider.setToolTip(
            "Коридоры, улица и открытые места: 55% — планировка читается, "
            "но деталей не видно; 0% — тумана нет")
        self.fog_world_slider.valueChanged.connect(
            lambda value: self.on_fog_slider_changed("world", value))
        row.addWidget(self.fog_world_slider, 1)
        self.fog_world_label = QLabel(
            f"{lighting_rules.DEFAULT_FOG_WORLD_ALPHA}%")
        self.fog_world_label.setMinimumWidth(34)
        row.addWidget(self.fog_world_label)
        fl.addLayout(row)
        l.addWidget(fog)

        box = QGroupBox("Источники света")
        box.setObjectName("panel_group")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(6, 14, 6, 6)
        bl.setSpacing(6)

        self.light_list = LightList()
        self.light_list.setObjectName("light_list")
        # По умолчанию — пять строк (последняя раньше уезжала под подпись
        # «Поставить свет:»). Но это не потолок: ручка под списком тянется
        # мышью. В мире бывает по десятку факелов, и в пяти строках нужный
        # источник не найти.
        self.light_list.setFixedHeight(HeightGrip.DEFAULT_HEIGHT)
        # Горизонтальной полосе в узкой колонке неоткуда взяться: строки
        # короткие, а длинные имена аккуратно обрезаются многоточием.
        self.light_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.light_list.setTextElideMode(Qt.ElideRight)
        self.light_list.setToolTip(
            "Источники света этой карты. Рамкой, Shift или Ctrl выделяются "
            "несколько — глазок и замок применятся ко всей группе. "
            "Del или X удаляют выделенное. Маркер на холсте можно таскать "
            "мышью, пока открыта эта вкладка. Пустой клик снимает выбор. "
            "Ручку под списком можно потянуть — список станет выше")
        self.light_list.currentRowChanged.connect(self._on_light_row_changed)
        self.light_list.blank_clicked.connect(self.clear_light_selection)
        self.light_list.delete_requested.connect(self.delete_map_lights)
        bl.addWidget(self.light_list, 1)
        # Ручка высоты: тянется вниз — список растёт, вверх — сжимается.
        # Вкладка целиком в прокрутке, поэтому вытянутый список не выносит
        # «Стены» за край панели.
        self.light_list_grip = HeightGrip(self.light_list)
        bl.addWidget(self.light_list_grip)

        # Готовые заготовки: один клик — и на столе свеча, факел или костёр
        # с правильными радиусом, температурой и мягкостью кромки. Подпись
        # отдельной строкой: в узкой колонке она рядом с иконками обрезалась.
        preset_caption = QLabel("Поставить свет:")
        preset_caption.setToolTip(
            "Готовая заготовка света: радиус, температура и мягкость "
            "кромки уже заданы. Клик ставит источник в центр экрана, "
            "а перетаскивание иконки на карту — ровно туда, где отпустите")
        bl.addWidget(preset_caption)
        presets = QHBoxLayout()
        presets.setSpacing(4)
        self.light_preset_buttons = {}
        for preset in lighting_rules.LIGHT_PRESETS:
            # Своя кнопка: она же источник перетаскивания на холст.
            preset_btn = LightPresetButton(preset["key"])
            preset_btn.setObjectName("light_preset_btn")
            preset_btn.setAutoRaise(True)
            preset_btn.setFixedSize(34, 30)
            preset_btn.setIconSize(QSize(24, 24))
            preset_btn.setCursor(Qt.PointingHandCursor)
            preset_btn.setIcon(tool_icon(preset["icon"], theme))
            preset_btn.setToolTip(
                "%s: %d фт, %d K, край %d%%\n"
                "Клик — в центр экрана, перетащите на карту — в точку отпускания"
                % (preset["name"], int(preset["radius_ft"]),
                   int(preset["kelvin"]), int(preset["intensity"])))
            preset_btn.clicked.connect(
                lambda _=False, key=preset["key"]: self.add_preset_light(key))
            presets.addWidget(preset_btn)
            self.light_preset_buttons[preset["key"]] = preset_btn
        presets.addStretch(1)
        bl.addLayout(presets)

        self.light_del_btn = QPushButton("Убрать выбранный")
        self.light_del_btn.setToolTip("Удалить выбранный источник света")
        self.light_del_btn.clicked.connect(self.remove_map_light)
        bl.addWidget(self.light_del_btn)

        # Глазок и замок — иконками, как у слоёв и токенов: игрок иконок
        # света не видит вовсе, а мастер может спрятать её у себя и
        # закрепить источник, чтобы он не ехал от случайного клика.
        # Стоят в строке галочки, а не рядом с «Добавить/Убрать»: панель
        # узкая, и четыре кнопки в ряд уже не влезали бы.
        self._light_theme = theme
        self.light_eye_btn = QToolButton()
        self.light_eye_btn.setObjectName("light_eye_btn")
        self.light_eye_btn.setAutoRaise(True)
        self.light_eye_btn.setCheckable(True)
        self.light_eye_btn.setFixedSize(28, 24)
        self.light_eye_btn.setIconSize(QSize(18, 18))
        self.light_eye_btn.setCursor(Qt.PointingHandCursor)
        self.light_eye_btn.setIcon(tool_icon("eye", theme))
        self.light_eye_btn.setToolTip("Скрыть иконку источника на холсте")
        self.light_eye_btn.toggled.connect(self._on_light_eye_toggled)
        self.light_lock_btn = QToolButton()
        self.light_lock_btn.setObjectName("light_lock_btn")
        self.light_lock_btn.setAutoRaise(True)
        self.light_lock_btn.setCheckable(True)
        self.light_lock_btn.setFixedSize(28, 24)
        self.light_lock_btn.setIconSize(QSize(18, 18))
        self.light_lock_btn.setCursor(Qt.PointingHandCursor)
        self.light_lock_btn.setIcon(tool_icon("unlock", theme))
        self.light_lock_btn.setToolTip("Закрепить источник: мышь его не сдвинет")
        self.light_lock_btn.toggled.connect(self._on_light_lock_toggled)

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("Радиус:"))
        self.light_radius_spin = QSpinBox()
        self.light_radius_spin.setRange(
            int(lighting_rules.MIN_LIGHT_RADIUS_FT),
            int(lighting_rules.MAX_LIGHT_RADIUS_FT))
        self.light_radius_spin.setSingleStep(5)
        self.light_radius_spin.setSuffix(" фт")
        self.light_radius_spin.setValue(
            int(lighting_rules.DEFAULT_LIGHT_RADIUS_FT))
        self.light_radius_spin.setToolTip(
            "Насколько далеко факел пробивает темноту (5–120 фт)")
        self.light_radius_spin.valueChanged.connect(
            self._on_light_radius_changed)
        row.addWidget(self.light_radius_spin)
        row.addStretch(1)
        bl.addLayout(row)

        # Температура: цвет света считается из кельвинов, поэтому мастеру
        # достаточно одного числа — и свеча, и лампа дневного света.
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("Сила:"))
        self.light_kelvin_spin = QSpinBox()
        self.light_kelvin_spin.setRange(
            int(lighting_rules.MIN_LIGHT_KELVIN),
            int(lighting_rules.MAX_LIGHT_KELVIN))
        self.light_kelvin_spin.setSingleStep(100)
        self.light_kelvin_spin.setSuffix(" K")
        self.light_kelvin_spin.setValue(int(lighting_rules.DEFAULT_LIGHT_KELVIN))
        self.light_kelvin_spin.setToolTip(
            "Температура света в кельвинах: ~1800 — свеча, 2200 — факел, "
            "4000 — лампа, 6500 — дневной свет. С %d K свет «сверх силы»: "
            "виден и днём, а не только ночью"
            % lighting_rules.DAYLIGHT_GLOW_KELVIN)
        self.light_kelvin_spin.valueChanged.connect(
            self._on_light_kelvin_changed)
        row.addWidget(self.light_kelvin_spin)
        self.light_color_swatch = QLabel()
        self.light_color_swatch.setFixedSize(16, 16)
        self.light_color_swatch.setToolTip("Цвет, который даёт эта температура")
        row.addWidget(self.light_color_swatch)
        row.addStretch(1)
        bl.addLayout(row)

        # Интенсивность — плавность кромки: 0% рисует резкий круг, 100%
        # растягивает переход почти до центра, и граница света не видна.
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("Край:"))
        self.light_intensity_slider = QSlider(Qt.Horizontal)
        self.light_intensity_slider.setRange(
            int(lighting_rules.MIN_LIGHT_INTENSITY),
            int(lighting_rules.MAX_LIGHT_INTENSITY))
        self.light_intensity_slider.setValue(
            int(lighting_rules.DEFAULT_LIGHT_INTENSITY))
        self.light_intensity_slider.setToolTip(
            "Насколько плавно свет гаснет к краю: 0% — резкая кромка, "
            "100% — мягкий переход без видимого круга")
        self.light_intensity_slider.valueChanged.connect(
            self._on_light_intensity_changed)
        row.addWidget(self.light_intensity_slider, 1)
        self.light_intensity_label = QLabel(
            f"{lighting_rules.DEFAULT_LIGHT_INTENSITY}%")
        self.light_intensity_label.setMinimumWidth(34)
        row.addWidget(self.light_intensity_label)
        bl.addLayout(row)

        self.light_on_check = QCheckBox("Горит")
        self.light_on_check.setToolTip(
            "Источник горит: снимите галочку, чтобы погасить его, "
            "не удаляя с карты")
        self.light_on_check.toggled.connect(self._on_light_on_toggled)
        bl.addWidget(self.light_on_check)

        # Окна домов и уличные фонари горят только ночью: днём такой источник
        # остаётся на карте для правки, но не светит.
        row = QHBoxLayout()
        row.setSpacing(2)
        self.light_night_check = QCheckBox("Только ночью")
        self.light_night_check.setToolTip(
            "Зажигается только ночью: днём источник не светит, но остаётся "
            "на карте (так работают окна домов и фонари)")
        self.light_night_check.toggled.connect(
            self._on_light_night_only_toggled)
        row.addWidget(self.light_night_check, 1)
        row.addWidget(self.light_eye_btn)
        row.addWidget(self.light_lock_btn)
        bl.addLayout(row)
        l.addWidget(box)

        # --- Стены: рамкой и по точкам, окна и двери ---
        # Отдельная группа в той же вкладке: стены — часть освещения и
        # обзора (они режут свет и взгляд), а не декор карты.
        wall_box = QWidget()
        wall_box.setObjectName("panel_group")
        wl = QVBoxLayout(wall_box)
        wl.setContentsMargins(6, 14, 6, 6)
        wl.setSpacing(6)
        wall_caption = QLabel("Стены:")
        wall_caption.setToolTip(
            "Стены не пропускают свет, взгляд и токенов. Окно пропускает "
            "свет, но не фигурку; дверь пропускает всё, когда открыта")
        wl.addWidget(wall_caption)
        wall_tools = QHBoxLayout()
        wall_tools.setSpacing(4)
        self.wall_tool_buttons = {}
        for key, icon_name, tooltip in (
                ("rect", "wall_rect",
                 "Стена рамкой: растяните — получится комната"),
                ("points", "wall_pen",
                 "Стена по точкам: клик ставит вершину, ПКМ или Enter — готово"),
                ("edit", "wall_points",
                 "Точки стены: тащите вершины, двойной клик — новая, "
                 "ПКМ по точке — убрать"),
                ("window", "wall_window",
                 "Окно: свет и взгляд проходят, пройти нельзя"),
                ("door", "wall_door",
                 "Дверь: клик по стене ставит, клик по готовой — открыть")):
            wall_btn = QToolButton()
            wall_btn.setObjectName("wall_tool_btn")
            wall_btn.setAutoRaise(True)
            wall_btn.setCheckable(True)
            wall_btn.setFixedSize(34, 30)
            wall_btn.setIconSize(QSize(22, 22))
            wall_btn.setCursor(Qt.PointingHandCursor)
            wall_btn.setIcon(tool_icon(icon_name, theme))
            wall_btn.setToolTip(tooltip)
            wall_btn.clicked.connect(
                lambda _=False, tool=key: self._on_wall_tool(tool))
            wall_tools.addWidget(wall_btn)
            self.wall_tool_buttons[key] = wall_btn
        self.wall_delete_btn = QToolButton()
        self.wall_delete_btn.setObjectName("wall_delete_btn")
        self.wall_delete_btn.setAutoRaise(True)
        # Без checkable кнопка-корзина выглядела рабочей, но всегда читалась
        # выключенной (`isChecked` == False), и стены не удалялись вовсе.
        self.wall_delete_btn.setCheckable(True)
        self.wall_delete_btn.setFixedSize(30, 30)
        self.wall_delete_btn.setIconSize(QSize(20, 20))
        self.wall_delete_btn.setCursor(Qt.PointingHandCursor)
        self.wall_delete_btn.setIcon(tool_icon("clear", theme))
        self.wall_delete_btn.setToolTip(
            "Удалить стену: кликните по стене (вместе с её окнами и дверями)")
        self.wall_delete_btn.clicked.connect(
            lambda _=False: self._on_wall_tool("delete"))
        wall_tools.addWidget(self.wall_delete_btn)
        wall_tools.addStretch(1)
        wl.addLayout(wall_tools)

        wall_width_row = QHBoxLayout()
        wall_width_row.setSpacing(6)
        wall_width_row.addWidget(QLabel("Проём:"))
        self.wall_width_spin = QSpinBox()
        self.wall_width_spin.setRange(int(wall_rules.MIN_OPENING_FT),
                                      int(wall_rules.MAX_OPENING_FT))
        self.wall_width_spin.setSingleStep(5)
        self.wall_width_spin.setSuffix(" фт")
        self.wall_width_spin.setValue(int(wall_rules.DEFAULT_DOOR_FT))
        self.wall_width_spin.setToolTip(
            "Ширина новых окон и дверей: клетка в подземелье — 5 фт")
        self.wall_width_spin.valueChanged.connect(
            lambda value: self.canvas.set_wall_opening_ft(float(value)))
        wall_width_row.addWidget(self.wall_width_spin)
        wall_width_row.addStretch(1)
        wl.addLayout(wall_width_row)

        # Магнит и его шаг — отдельной строкой: колонка узкая, и рядом с
        # полем «Проём» подписи уже не помещаются.
        snap_row = QHBoxLayout()
        snap_row.setSpacing(4)
        self.wall_snap_box = QCheckBox("Snap")
        self.wall_snap_box.setToolTip(
            "Магнит вершин: точка встаёт на линии сетки при клике и перетаскивании")
        self.wall_snap_box.toggled.connect(self._on_wall_snap_toggled)
        snap_row.addWidget(self.wall_snap_box)
        self.wall_snap_step_box = QComboBox()
        self.wall_snap_step_box.addItem("Клетка", 1.0)
        self.wall_snap_step_box.addItem("½ клетки", 0.5)
        self.wall_snap_step_box.setToolTip(
            "Шаг магнита: целая клетка или её половина")
        self.wall_snap_step_box.setFixedWidth(92)
        self.wall_snap_step_box.setEnabled(False)
        self.wall_snap_step_box.currentIndexChanged.connect(
            self._on_wall_snap_step_changed)
        snap_row.addWidget(self.wall_snap_step_box)
        snap_row.addStretch(1)
        wl.addLayout(snap_row)
        l.addWidget(wall_box)
        l.addStretch(1)

        self._light_sync = False
        self._night_sync = False
        self._fog_sync = False
        self._wall_snap_sync = False
        # Вкладка целиком в прокрутке: растянутый ручкой список света делает
        # содержимое выше панели, и без прокрутки нижняя группа «Стены»
        # оказалась бы за краем экрана. Горизонтальной ленты нет — панель
        # узкая, а всё содержимое рассчитано на эту ширину.
        scroll = QScrollArea()
        scroll.setObjectName("lighting_scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        self.lighting_scroll = scroll
        tab_index = tabs.addTab(scroll, tool_icon("light", theme), "")
        tabs.setTabToolTip(tab_index, "Освещение")
        self._lighting_tab_index = tab_index
        return tab_index

    def _on_wall_tool(self, tool: str):
        """Кнопка инструмента стен: одна активна, повторный клик — выключить."""
        buttons = getattr(self, "wall_tool_buttons", {})
        button = buttons.get(tool) or getattr(self, "wall_delete_btn", None)
        active = bool(button is not None and button.isChecked())
        for key, other in buttons.items():
            other.setChecked(active and key == tool)
        if hasattr(self, "wall_delete_btn"):
            self.wall_delete_btn.setChecked(active and tool == "delete")
        if hasattr(self, "wall_width_spin"):
            self.canvas.set_wall_opening_ft(
                float(self.wall_width_spin.value()))
        self.canvas.set_wall_tool(tool if active else None)

    def _sync_night_tint_ui(self):
        """Показать в панели цвет и плотность ночи открытой карты."""
        if getattr(self, "_lighting_tab_index", None) is None:
            return
        color, alpha = self.canvas.night_tint_pair()
        self._night_sync = True
        try:
            # Образец цвета вместо кода: в узкой панели строка "#0A0E22"
            # обрезалась бы, а сам цвет видно и так.
            swatch = QPixmap(34, 16)
            swatch.fill(QColor(color))
            # Тонкая рамка: тёмно-синяя ночь на тёмной панели иначе сливается
            # с фоном и образец не виден.
            mark = QPainter(swatch)
            mark.setPen(QPen(QColor(120, 120, 128), 1))
            mark.drawRect(0, 0, swatch.width() - 1, swatch.height() - 1)
            mark.end()
            self.night_color_btn.setIcon(QIcon(swatch))
            self.night_color_btn.setIconSize(QSize(34, 16))
            self.night_color_btn.setText("")
            self.night_color_btn.setToolTip(
                f"Цвет ночного освещения: {color.upper()} — нажмите, "
                "чтобы выбрать другой")
            self.night_alpha_slider.setValue(alpha)
            self.night_alpha_label.setText(f"{alpha}%")
        finally:
            self._night_sync = False
        # Туман: ползунки показывают плотность открытой карты, список — вид.
        world_alpha, room_alpha = self.canvas.fog_alphas()
        self._fog_sync = True
        try:
            self.fog_room_slider.setValue(room_alpha)
            self.fog_world_slider.setValue(world_alpha)
            self.fog_room_label.setText(f"{room_alpha}%")
            self.fog_world_label.setText(f"{world_alpha}%")
            self._show_fog_style(self.canvas.fog_style(),
                                 self.canvas.fog_custom_color())
        finally:
            self._fog_sync = False

    def pick_night_color(self):
        """Выбрать цвет ночного затемнения через обычный диалог Qt."""
        color, _alpha = self.canvas.night_tint_pair()
        chosen = QColorDialog.getColor(QColor(color), self,
                                       "Цвет ночного освещения")
        if not chosen.isValid():
            return
        self.canvas.set_night_tint(color=chosen.name())
        self._sync_night_tint_ui()

    def _show_fog_style(self, style: str, color: str):
        """Показать в панели выбранный вид тумана и образец своего цвета."""
        combo = getattr(self, "fog_style_combo", None)
        if combo is None:
            return
        index = combo.findData(lighting_rules.normalize_fog_style(style))
        if index >= 0 and index != combo.currentIndex():
            combo.setCurrentIndex(index)
        mode = lighting_rules.normalize_fog_style(style)
        custom = mode == lighting_rules.FOG_STYLE_CUSTOM
        button = getattr(self, "fog_color_btn", None)
        if button is not None:
            # Кнопка нужна только для своего цвета: в готовых видах цвет
            # задан заранее и выбор его не меняет.
            button.setVisible(custom)
            if custom:
                swatch = QPixmap(34, 16)
                swatch.fill(QColor(
                    lighting_rules.normalize_fog_color(color)))
                mark = QPainter(swatch)
                mark.setPen(QPen(QColor(120, 120, 128), 1))
                mark.drawRect(0, 0, swatch.width() - 1,
                              swatch.height() - 1)
                mark.end()
                button.setIcon(QIcon(swatch))
                button.setIconSize(QSize(34, 16))
                button.setText("")

    def on_fog_style_changed(self, _index: int = 0):
        """Список видов тумана: применить на холст и разослать всем."""
        combo = getattr(self, "fog_style_combo", None)
        if combo is None or getattr(self, "_fog_sync", True):
            return
        style = combo.currentData()
        self.canvas.set_fog_style(style=style)
        self._show_fog_style(style, self.canvas.fog_custom_color())

    def pick_fog_color(self):
        """Выбрать свой цвет заливки неразведанного."""
        chosen = QColorDialog.getColor(
            QColor(self.canvas.fog_custom_color()), self,
            "Цвет тумана войны")
        if not chosen.isValid():
            return
        self.canvas.set_fog_style(
            style=lighting_rules.FOG_STYLE_CUSTOM, color=chosen.name())
        self._show_fog_style(lighting_rules.FOG_STYLE_CUSTOM,
                             chosen.name())

    def on_fog_slider_changed(self, kind: str, value: int):
        """Ползунок тумана: подпись, холст и (через сигнал) отправка всем."""
        label = self.fog_room_label if kind == "room" else self.fog_world_label
        label.setText(f"{value}%")
        if self._fog_sync:
            return
        self.canvas.set_fog_alpha(kind, value)

    def on_fog_alpha_changed(self, kind: str, value: int):
        """Холст принял новую плотность тумана — разослать всем, с паузой."""
        m = self.canvas.current_map
        if m is None:
            return
        self._pending_fog = m.id
        timer = getattr(self, "_fog_send_timer", None)
        if timer is None:
            self._send_fog()
        else:
            timer.start()

    def on_fog_style_committed(self, _style: str, _color: str):
        """Холст принял вид тумана — разослать всем, с паузой."""
        m = self.canvas.current_map
        if m is None:
            return
        self._pending_fog = m.id
        timer = getattr(self, "_fog_send_timer", None)
        if timer is None:
            self._send_fog()
        else:
            timer.start()

    def _send_fog(self):
        """Отправить последнюю выбранную плотность тумана (одно сообщение)."""
        map_id = getattr(self, "_pending_fog", None)
        self._pending_fog = None
        if not map_id:
            return
        m = self.game_state.maps.get(map_id) or self.canvas.current_map
        if m is None:
            return
        world, room = self.canvas.fog_alphas()
        self.network_client.send_message(MessageType.UPDATE_TIME, {
            'map_id': m.id,
            'time_of_day': normalize_time_of_day(m.time_of_day),
            'night_color': m.night_color,
            'night_alpha': m.night_alpha,
            'fog_room_alpha': room,
            'fog_world_alpha': world,
            'fog_style': self.canvas.fog_style(),
            'fog_color': self.canvas.fog_custom_color(),
        })

    def on_night_alpha_changed(self, value: int):
        """Ползунок плотности: сразу на холсте, отправка — после паузы."""
        self.night_alpha_label.setText(f"{value}%")
        if self._night_sync:
            return
        self.canvas.set_night_tint(alpha=value)

    # --- вкладка освещения --------------------------------------------------
    def _sync_lighting_ui(self):
        """Показать в панели то, что сейчас на карте (день, факелы, выбор)."""
        if getattr(self, "_lighting_tab_index", None) is None:
            return
        night = self.canvas.time_of_day == "night"
        self._light_sync = True
        try:
            self.light_day_btn.setChecked(not night)
            self.light_night_btn.setChecked(night)
            self.refresh_light_list()
        finally:
            self._light_sync = False
        self._sync_night_tint_ui()
        self._sync_wall_snap_ui()

    def _sync_wall_snap_ui(self):
        """Показать в панели магнит стен текущей карты."""
        box = getattr(self, "wall_snap_box", None)
        if box is None:
            return
        enabled = bool(self.canvas.wall_snap)
        step = self.canvas.wall_snap_step
        self._wall_snap_sync = True
        try:
            box.setChecked(enabled)
            index = self.wall_snap_step_box.findData(step)
            self.wall_snap_step_box.setCurrentIndex(index if index >= 0 else 0)
            self.wall_snap_step_box.setEnabled(enabled)
        finally:
            self._wall_snap_sync = False

    def _on_wall_snap_toggled(self, checked: bool):
        """Галочка Snap: включить магнит и разрешить выбор шага."""
        if getattr(self, "_wall_snap_sync", False):
            return
        self.wall_snap_step_box.setEnabled(bool(checked))
        self.canvas.set_wall_snap(bool(checked))

    def _on_wall_snap_step_changed(self, index: int):
        """Шаг магнита: клетка или половина клетки."""
        if getattr(self, "_wall_snap_sync", False):
            return
        step = self.wall_snap_step_box.itemData(index)
        self.canvas.set_wall_snap_step(float(step or 1.0))

    def on_wall_snap_changed(self, enabled: bool, step: float):
        """Магнит стен изменился: запомнить на карте и сообщить серверу.

        Настройка живёт в `MapData`, поэтому она едет вместе с миром: файл
        сохраняет сервер, и без этого сообщения галочка не пережила бы
        перезапуск (у остальных клиентов роль мастера одна — там лишь эхо).
        """
        m = self.canvas.current_map
        if m is None:
            return
        self.network_client.send_message(MessageType.UPDATE_MAP, {
            'map_id': m.id,
            'wall_snap': bool(enabled),
            'wall_snap_step': float(step),
        })
        self._sync_wall_snap_ui()

    def refresh_light_list(self):
        """Перестроить список факелов, сохранив выбранный и всё выделение.

        Список пересобирается после каждой правки, поэтому без переноса
        выделения рамкой выбранная группа распадалась бы после первого же
        клика по глазку — а им как раз и прячут всю группу сразу.
        """
        lights = self.canvas.lighting_lights()
        chosen = self.canvas.selected_light_id
        keep = set(self.light_list.selected_light_ids())
        self._light_sync = True
        try:
            self.light_list.clear()
            for light in lights:
                # Строка короткая: колонка узкая, а температура видна в полях
                # выбранного источника. Длинный текст обрезался бы многоточием.
                text = "%s · %d фт" % (light["name"],
                                       int(round(light["radius_ft"])))
                if not light["enabled"]:
                    text += " · выкл"
                elif light.get("night_only"):
                    text += " · ночью"
                item = QListWidgetItem(text)
                # Замок и спрятанный глазок — иконками в строке: словесные
                # пометки не влезали в узкую колонку и обрезались.
                if light.get("locked"):
                    item.setIcon(tool_icon("lock", self._light_theme))
                    item.setToolTip("Источник закреплён")
                elif light.get("marker_hidden"):
                    item.setIcon(tool_icon("eye_off", self._light_theme))
                    item.setToolTip("Иконка источника скрыта на холсте")
                item.setData(Qt.UserRole, light["id"])
                self.light_list.addItem(item)
            row = next((i for i, light in enumerate(lights)
                        if light["id"] == chosen), -1)
            # Сначала текущая строка, потом выделение группы: setCurrentRow
            # в режиме ExtendedSelection снимает остальные выделения.
            self.light_list.setCurrentRow(row)
            if row >= 0:
                # В списке бывает десятки строк: выбранный источник должен
                # быть виден, а не оставаться за прокруткой.
                self.light_list.scrollToItem(self.light_list.item(row))
            for index in range(self.light_list.count()):
                item = self.light_list.item(index)
                if item is not None and item.data(Qt.UserRole) in keep:
                    item.setSelected(True)
            has = row >= 0
            for widget in (self.light_radius_spin, self.light_kelvin_spin,
                           self.light_intensity_slider,
                           self.light_intensity_label, self.light_on_check,
                           self.light_night_check, self.light_del_btn,
                           self.light_eye_btn, self.light_lock_btn):
                widget.setEnabled(has)
            if has:
                light = lights[row]
                self.light_radius_spin.setValue(
                    int(round(light["radius_ft"])))
                self.light_on_check.setChecked(bool(light["enabled"]))
                self._sync_light_look(light)
            else:
                self.light_color_swatch.setStyleSheet(
                    "background:#00000000;border:1px solid #555;")
                self.light_intensity_label.setText("—")
        finally:
            self._light_sync = False

    def _sync_light_look(self, light: dict):
        """Температура, цвет, кромка, глазок и замок выбранного источника."""
        self.light_kelvin_spin.setValue(int(light.get("kelvin") or
                                           lighting_rules.DEFAULT_LIGHT_KELVIN))
        color = QColor(light.get("color") or
                       lighting_rules.DEFAULT_LIGHT_COLOR)
        self.light_color_swatch.setStyleSheet(
            "background:%s;border:1px solid #222;border-radius:3px;"
            % color.name())
        intensity = int(light.get("intensity")
                        if light.get("intensity") is not None
                        else lighting_rules.DEFAULT_LIGHT_INTENSITY)
        self.light_intensity_slider.setValue(intensity)
        self.light_intensity_label.setText(f"{intensity}%")
        hidden = bool(light.get("marker_hidden"))
        self.light_eye_btn.setChecked(hidden)
        self.light_eye_btn.setIcon(tool_icon("eye_off" if hidden else "eye",
                                             self._light_theme))
        self.light_eye_btn.setToolTip(
            "Показать иконку источника на холсте" if hidden else
            "Скрыть иконку источника на холсте")
        self.light_night_check.setChecked(bool(light.get("night_only")))
        locked = bool(light.get("locked"))
        self.light_lock_btn.setChecked(locked)
        self.light_lock_btn.setIcon(tool_icon("lock" if locked else "unlock",
                                              self._light_theme))
        self.light_lock_btn.setToolTip(
            "Отпустить источник: мышь снова его двигает" if locked else
            "Закрепить источник: мышь его не сдвинет")

    def _on_light_row_changed(self, row: int):
        if (self._light_sync
                or getattr(self, "_lighting_tab_index", None) is None):
            return
        lights = self.canvas.lighting_lights()
        if 0 <= row < len(lights):
            self.canvas.selected_light_id = lights[row]["id"]
        else:
            self.canvas.selected_light_id = None
        self.refresh_light_list()
        self.canvas.update()

    def _on_light_radius_changed(self, value: int):
        if self._light_sync:
            return
        light_id = self.canvas.selected_light_id
        if light_id is None:
            return
        self.canvas.update_light(light_id, radius_ft=value)
        self.refresh_light_list()

    def _on_light_on_toggled(self, checked: bool):
        if self._light_sync:
            return
        light_id = self.canvas.selected_light_id
        if light_id is None:
            return
        self.canvas.update_light(light_id, enabled=bool(checked))
        self.refresh_light_list()

    def _selected_light(self):
        """Выбранный источник света или None."""
        light_id = self.canvas.selected_light_id
        if light_id is None:
            return None
        return next((light for light in self.canvas.lighting_lights()
                     if light["id"] == light_id), None)

    def _on_light_kelvin_changed(self, value: int):
        """Ползунок температуры: цвет источника пересчитывается из кельвинов."""
        if self._light_sync:
            return
        light = self._selected_light()
        if light is None:
            return
        # Сила света — единственная настройка без Ctrl+Z: её подбирают
        # ползунком, и история забилась бы его шагами.
        self.canvas.update_light(light["id"], kelvin=int(value),
                                 remember=False)
        self.refresh_light_list()

    def _on_light_intensity_changed(self, value: int):
        """Интенсивность: насколько плавно гаснет свет к краю."""
        self.light_intensity_label.setText(f"{value}%")
        if self._light_sync:
            return
        light = self._selected_light()
        if light is None:
            return
        self.canvas.update_light(light["id"], intensity=int(value))

    def _on_light_night_only_toggled(self, night_only: bool):
        """Галочка «Только ночью»: днём источник не светит."""
        if self._light_sync:
            return
        light = self._selected_light()
        if light is None:
            return
        self.canvas.update_light(light["id"], night_only=bool(night_only))
        self.refresh_light_list()

    def _light_target_ids(self) -> list:
        """К чему применяются глазок, замок и удаление.

        Всё выделение в списке (рамкой, Shift или Ctrl), а если не
        выделено ничего — текущий выбранный источник.
        """
        ids = (self.light_list.selected_light_ids()
               if hasattr(self, "light_list") else [])
        if ids:
            return ids
        light_id = self.canvas.selected_light_id
        return [light_id] if light_id else []

    def on_canvas_light_selected(self, light_id: str):
        """На холсте взяли источник — подсветить его строку в списке.

        Раньше выбор шёл только в одну сторону: клик по строке выделял
        источник на карте, а клик по метке на карте в списке не был виден
        вовсе. Мастер не понимал, что именно он тащит.
        """
        if not light_id or getattr(self, "_light_sync", False):
            return
        if not hasattr(self, "light_list"):
            return
        self.refresh_light_list()
        self.canvas.update()

    def _on_light_eye_toggled(self, hidden: bool):
        """Глазок: спрятать или показать иконки — сразу у всей группы."""
        if self._light_sync:
            return
        ids = self._light_target_ids()
        if not ids:
            return
        self.canvas.update_lights(ids, marker_hidden=bool(hidden))
        self.refresh_light_list()

    def _on_light_lock_toggled(self, locked: bool):
        """Замок: закрепить всё выделенное — одной правкой."""
        if self._light_sync:
            return
        ids = self._light_target_ids()
        if not ids:
            return
        self.canvas.update_lights(ids, locked=bool(locked))
        self.refresh_light_list()

    def clear_light_selection(self):
        """Пустой клик во вкладке снимает выделение источника."""
        if self.canvas.selected_light_id is None:
            return
        self.canvas.selected_light_id = None
        self.refresh_light_list()
        self.canvas.update()

    def add_preset_light(self, key: str):
        """Один клик по заготовке: источник сразу на карте и у всех игроков."""
        light = self.canvas.add_light(preset=key)
        if light is None:
            return
        self.refresh_light_list()
        self.statusBar().showMessage(
            "%s: %d фт, %d K — перетащите по карте при открытой вкладке"
            % (light["name"], int(round(light["radius_ft"])),
               int(light["kelvin"])), 4000)

    def _on_light_preset_dropped(self, light: dict):
        """Свет перетащили на карту: перестроить список и сказать, что вышло."""
        self.refresh_light_list()
        self.statusBar().showMessage(
            "%s: %d фт, %d K — стоит там, где отпустили"
            % (light.get("name", "Свет"),
               int(round(light.get("radius_ft") or 0)),
               int(light.get("kelvin") or 0)), 4000)

    def remove_map_light(self):
        """«Убрать выбранный» и Del/X: удалить всё выделенное."""
        self.delete_map_lights(self._light_target_ids())

    def delete_map_lights(self, light_ids):
        """Удалить источники пачкой: одно сообщение и одна запись истории."""
        ids = [str(item) for item in (light_ids or []) if item]
        if not ids:
            return
        self.canvas.remove_lights(ids)
        self.refresh_light_list()
        self.canvas.update()

    def _on_right_section_changed(self, index: int):
        """Открыта ли вкладка освещения: только тогда маркеры тянутся мышью."""
        lighting = (getattr(self, "_lighting_tab_index", None) is not None
                    and index == self._lighting_tab_index)
        self.canvas.set_light_editing(lighting)
        if lighting:
            self._sync_lighting_ui()

    def _build_text_group(self, page, theme: str = "dark") -> QGroupBox:
        """Панель надписи: шрифт, размер, начертание и цвет.

        Порядок как в Word: строка шрифта с размером, под ней — начертание,
        цвет и удаление. Выделенную надпись панель меняет сразу, а без
        выделения — задаёт вид следующим. Две строки вместо сетки: у сетки
        широкая колонка размера растягивала бы и соседнюю кнопку, и панель
        вылезала бы за край.
        """
        group = QGroupBox("Текст", page)
        group.setObjectName("panel_group")
        outer = QVBoxLayout(group)
        outer.setContentsMargins(6, 12, 6, 6)
        outer.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(4)
        bottom = QHBoxLayout()
        bottom.setSpacing(4)

        # Шрифты: только те, что действительно есть в системе и умеют
        # кириллицу — иначе в списке были бы имена, которые молча дадут
        # подстановку и чужой вид надписи.
        self.text_font_combo = QComboBox(group)
        self.text_font_combo.setObjectName("text_font_combo")
        self.text_font_combo.addItems(cyrillic_font_families())
        self.text_font_combo.setToolTip("Шрифт надписи")
        # Поле должно тянуться вместе с панелью, а не раздувать её под самое
        # длинное название шрифта: панель и так не резиновая.
        self.text_font_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.text_font_combo.setMinimumContentsLength(10)
        # Жёсткий минимум: без него в ширину панели попадает размер поля под
        # самое длинное название шрифта, и вкладка раздувается вдвое. Строке
        # ниже поле отдано целиком, места хватит любому имени.
        self.text_font_combo.setMinimumWidth(96)
        self.text_font_combo.setSizePolicy(QSizePolicy.Preferred,
                                           QSizePolicy.Fixed)
        self.text_font_combo.currentTextChanged.connect(
            lambda _=None: self.on_text_style_changed())
        top.addWidget(self.text_font_combo, 1)

        self.text_size_spin = QSpinBox(group)
        self.text_size_spin.setObjectName("text_size_spin")
        self.text_size_spin.setRange(int(text_rules.SIZE_MIN),
                                     int(text_rules.SIZE_MAX))
        self.text_size_spin.setSingleStep(2)
        self.text_size_spin.setSuffix(" px")
        self.text_size_spin.setFixedWidth(72)
        self.text_size_spin.setToolTip(
            "Размер надписи: в пикселях карты, растёт вместе с зумом")
        self.text_size_spin.valueChanged.connect(
            lambda _=None: self.on_text_style_changed())
        # Размер — сразу за шрифтом, как в Word: «Calibri 28 px».
        top.addWidget(self.text_size_spin)
        outer.addLayout(top)

        # Начертание — четыре независимых переключателя, как в Word.
        self.text_style_buttons = {}
        hints = {"bold": "Жирный", "italic": "Курсив",
                 "underline": "Подчёркнутый", "strike": "Зачёркнутый"}
        for key in ("bold", "italic", "underline", "strike"):
            btn = QToolButton(group)
            btn.setObjectName("draw_tool")
            btn.setCheckable(True)
            btn.setIcon(tool_icon(TEXT_STYLE_ICONS[key], theme))
            btn.setIconSize(QSize(18, 18))
            btn.setFixedSize(30, 28)
            btn.setToolTip(hints[key])
            btn.toggled.connect(lambda _=False: self.on_text_style_changed())
            self.text_style_buttons[key] = btn
            bottom.addWidget(btn)

        # Цвет надписи: отдельный образец, а не цвет кисти. Белый текст на
        # светлой карте не виден, а заодно переключение жирного не должно
        # перекрашивать уже написанное.
        self.text_color_button = QToolButton(group)
        self.text_color_button.setObjectName("draw_color")
        self.text_color_button.setFixedSize(40, 28)
        self.text_color_button.setToolTip("Цвет надписи")
        self.text_color_button.clicked.connect(self.pick_text_color)

        # Удаление выбранной надписи — та же кнопка, что и Del на холсте.
        self.text_delete_btn = QToolButton(group)
        self.text_delete_btn.setObjectName("draw_tool")
        self.text_delete_btn.setFixedSize(30, 28)
        self.text_delete_btn.setIcon(tool_icon("text_delete", theme))
        self.text_delete_btn.setIconSize(QSize(18, 18))
        self.text_delete_btn.setToolTip("Удалить выбранную надпись (Delete)")
        self.text_delete_btn.clicked.connect(self.delete_selected_map_text)

        # Пружина держит начертание слева, а цвет и удаление — справа.
        bottom.addStretch(1)
        bottom.addWidget(self.text_color_button)
        bottom.addWidget(self.text_delete_btn)
        outer.addLayout(bottom)

        self._text_color = list(text_rules.DEFAULTS["color"])
        self._refresh_text_color_button()
        self._load_text_panel(self.canvas.text_style)
        return group

    def _build_dice_widget(self):
        """Собрать кубики для чата: кнопки D20…D2 или запуск 3D-лотка."""
        if self.enable_3d:
            self.dice_buttons = None
            self.dice_launch_button = QToolButton()
            self.dice_launch_button.setObjectName("dice_launch")
            self.dice_launch_button.setText("Бросить кубики")
            self.dice_launch_button.setIcon(dice_icon())
            self.dice_launch_button.setIconSize(QSize(22, 22))
            self.dice_launch_button.setToolButtonStyle(
                Qt.ToolButtonTextBesideIcon)
            self.dice_launch_button.setMinimumHeight(34)
            self.dice_launch_button.setToolTip(
                "Физические кубики в окне Dice Lab")
            self.dice_launch_button.clicked.connect(self.open_dice_window)
            return self.dice_launch_button
        self.dice_launch_button = None
        self.dice_buttons = DiceButtons(self)
        self.dice_buttons.rollRequested.connect(
            lambda cmd: self.quick_roll('/roll ' + cmd))
        return self.dice_buttons

    def _refresh_dice_widget(self):
        """Переключить кубики в чате на новый режим (3D ↔ кнопки)."""
        layout = getattr(self, "_chat_layout", None)
        if layout is None:
            return
        for attr in ("dice_launch_button", "dice_buttons"):
            old = getattr(self, attr, None)
            if old is not None:
                layout.removeWidget(old)
                old.deleteLater()
                setattr(self, attr, None)
        layout.insertWidget(2, self._build_dice_widget())

    def set_enable_3d(self, enabled: bool):
        """Галочка «3д дайсы»: тот же режим, что и в стартовом окне."""
        enabled = bool(enabled)
        if enabled == self.enable_3d:
            return
        self.enable_3d = enabled
        # Выключили — 3D-лоток закрывается, в чате снова кнопки D20…D2.
        if not enabled and self._dice_window is not None:
            self._dice_window.close()
        self._refresh_dice_widget()
        self.connection_info['enable_3d'] = enabled
        self._persist_setting('enable_3d', enabled)

    def set_combat_effects_enabled(self, enabled: bool):
        """Галочка «Эффекты в бою»: кровь и звуки, анимация остаётся."""
        self.combat_effects_enabled = bool(enabled)
        self.canvas.set_combat_effects_enabled(self.combat_effects_enabled)
        self.connection_info['combat_effects'] = self.combat_effects_enabled
        self._persist_setting('combat_effects', self.combat_effects_enabled)

    def set_blood_transparency(self, percent: int):
        """Прозрачность крови: ползунок в настройках мастера."""
        self.blood_transparency = max(0, min(100, int(percent)))
        label = getattr(self, 'blood_transparency_label', None)
        if label is not None:
            label.setText(f"{self.blood_transparency}%")
        self.canvas.set_blood_transparency(self.blood_transparency)
        self.connection_info['blood_transparency'] = self.blood_transparency
        self._persist_setting('blood_transparency', self.blood_transparency)

    def set_wasd_movement(self, enabled: bool):
        """Движение токена на WASD: необязательный режим клиента.

        Шаг — клетка раз в 200 мс (пять клеток в секунду). Рывок живёт на
        C, так что с WASD больше не спорит: D шагает вправо, C делает рывок.
        """
        enabled = bool(enabled)
        self.wasd_movement = enabled
        self.canvas.set_wasd_movement(enabled)
        self.connection_info['wasd_move'] = enabled
        self._persist_setting('wasd_move', enabled)
        checkbox = getattr(self, 'wasd_checkbox', None)
        if checkbox is not None and checkbox.isChecked() != enabled:
            checkbox.blockSignals(True)
            checkbox.setChecked(enabled)
            checkbox.blockSignals(False)

    def set_vision_mode(self, mode: str, notify: bool = True):
        """Зрение игроков: «поле зрения» или «вокруг токена» (настройка ДМ).

        ``notify=False`` — значение пришло с сервера, обратно не шлём.
        """
        mode = "cone" if str(mode).casefold() == "cone" else "around"
        if mode != self.vision_mode:
            self.vision_mode = mode
        self.canvas.set_player_vision_mode(mode, notify=notify)
        self.connection_info['vision_mode'] = mode
        self._persist_setting('vision_mode', mode)
        # Радиокнопки синхронизируются без обратного вызова: одна из них
        # не должна снимать выбранное значение второй.
        cone = getattr(self, 'vision_cone_radio', None)
        around = getattr(self, 'vision_around_radio', None)
        if cone is None or around is None:
            return
        for radio in (cone, around):
            radio.blockSignals(True)
        (cone if mode == "cone" else around).setChecked(True)
        for radio in (cone, around):
            radio.blockSignals(False)

    def set_hide_dm_walls(self, hidden: bool):
        """Мастер прячет у себя стены и окна: двери остаются видны."""
        self.hide_dm_walls = bool(hidden)
        self.canvas.set_hide_dm_walls(self.hide_dm_walls)
        if self.hide_dm_walls:
            # Стены скрыты — инструмент стен выключаем: иначе мастер рисовал
            # бы невидимые линии и не понимал, почему на столе ничего нет.
            for button in getattr(self, "wall_tool_buttons", {}).values():
                button.blockSignals(True)
                button.setChecked(False)
                button.blockSignals(False)
            delete_btn = getattr(self, "wall_delete_btn", None)
            if delete_btn is not None:
                delete_btn.blockSignals(True)
                delete_btn.setChecked(False)
                delete_btn.blockSignals(False)
            self.canvas.set_wall_tool(None)
            self.canvas.selected_wall_id = None
            self.canvas.selected_opening_id = None
        self.connection_info['hide_dm_walls'] = self.hide_dm_walls
        self._persist_setting('hide_dm_walls', self.hide_dm_walls)
        box = getattr(self, 'hide_dm_walls_checkbox', None)
        if box is not None and box.isChecked() != self.hide_dm_walls:
            box.blockSignals(True)
            box.setChecked(self.hide_dm_walls)
            box.blockSignals(False)

    def set_dm_fog(self, enabled: bool):
        """Галочка мастера «туман войны как у игроков».

        Вид становится как за столом игроков: туман считается по всем их
        токенам, а актёры мастера вне обзора не рисуются. Права не меняются:
        актёра можно выбрать в списке сцены и вести им. Сам флаг живёт в
        `settings.json` как `dm_fog`, потому что это настройка мастера, а
        не свойство мира.
        """
        self.dm_fog = bool(enabled)
        self.canvas.set_dm_fog_enabled(self.dm_fog)
        self.connection_info['dm_fog'] = self.dm_fog
        self._persist_setting('dm_fog', self.dm_fog)
        box = getattr(self, 'dm_fog_checkbox', None)
        if box is not None and box.isChecked() != self.dm_fog:
            box.blockSignals(True)
            box.setChecked(self.dm_fog)
            box.blockSignals(False)

    def set_preload_my_actors(self, enabled: bool):
        """Предзагрузка библиотеки игрокам: включить/выключить и разослать."""
        self.preload_my_actors = bool(enabled)
        self.connection_info['preload_my_actors'] = self.preload_my_actors
        self._persist_setting('preload_my_actors', self.preload_my_actors)
        if not self.preload_my_actors:
            self.preloaded_actors.clear()
            self.preloaded_actor_images.clear()
            self._preloaded_actor_ids.clear()
            self.network_client.send_message(MessageType.MY_ACTORS_CLEAR, {})
            return
        self.push_my_actors_to_server()

    def push_my_actors_to_server(self):
        """Отправить библиотеку игрокам (карточки + картинки), если включено."""
        page = getattr(self, 'my_actors_page', None)
        if page is None or not self.preload_my_actors:
            return
        current = set()
        for actor in page.library.get('actors', {}).values():
            actor_id = str(actor.get('id') or '')
            if not actor_id:
                continue
            current.add(actor_id)
            payload = {'id': 'myactor:' + actor_id,
                       'actor': dict(actor)}
            path = actor_image(actor)
            ref = asset_utils.local_asset_ref(path) if path else None
            if ref:
                payload['asset_upload'] = ref
            self.network_client.send_message(MessageType.MY_ACTORS_UPDATE,
                                             payload)
        # Удалённые из библиотеки актёры должны исчезнуть и у игроков.
        for stale in self._preloaded_actor_ids - current:
            self.network_client.send_message(
                MessageType.MY_ACTORS_REMOVE,
                {'id': 'myactor:' + stale})
        self._preloaded_actor_ids = current

    def on_my_actors_changed(self):
        """Библиотека изменилась: обновить предзагрузку у игроков."""
        if self.preload_my_actors:
            self.push_my_actors_to_server()

    def on_my_actors_update(self, data: dict):
        """Игрок получил актёра из библиотеки мастера заранее."""
        actor = data.get('actor')
        if not isinstance(actor, dict):
            return
        actor_id = str(actor.get('id') or '')
        if actor_id:
            self.preloaded_actors[actor_id] = copy.deepcopy(actor)

    def on_my_actors_remove(self, data: dict):
        key = str(data.get('id') or data.get('actor_id') or '')
        actor_id = key.split(':', 1)[1] if key.startswith('myactor:') else key
        self.preloaded_actors.pop(actor_id, None)
        self.preloaded_actor_images.pop(actor_id, None)

    def on_my_actors_clear(self, _data: dict = None):
        self.preloaded_actors.clear()
        self.preloaded_actor_images.clear()

    def apply_graphics(self, settings, persist: bool = True):
        """Применить настройки графики к холсту и запомнить в settings.json.

        Запоминаем в файле, а не только в памяти: движок отрисовки
        выбирается до создания окна (`main.choose_renderer`), и без записи
        выбор мастера терялся бы после перезапуска.
        """
        self.graphics_settings = graphics_rules.normalize(settings)
        self.canvas.set_graphics(self.graphics_settings)
        self.connection_info['graphics'] = self.graphics_settings
        if persist:
            self._persist_setting('graphics', self.graphics_settings)

    def open_graphics_dialog(self):
        """Окно «Графика» из меню «Стол»: те же настройки, что на старте.

        Рядом с настройками — отчёт о памяти и время кадра: мастер сразу
        видит, помогло ли снижение качества, а не гадает.
        """
        stats = self.canvas.graphics_report()
        dialog = GraphicsDialog(self.graphics_settings, self,
                                buffers=stats.get('buffers', 0),
                                frame_ms=stats.get('frame_ms'))
        dialog.setWindowIcon(app_icon())
        dialog.setAttribute(Qt.WA_DeleteOnClose, True)
        if dialog.exec_() != QDialog.Accepted:
            return
        self.apply_graphics(dialog.settings())
        # Показать результат сразу: время кадра после смены качества.
        stats = self.canvas.graphics_report()
        if stats.get('frame_ms'):
            self.canvas.show_hint(
                "Графика: кадр %.0f мс, светов %d, стен %d"
                % (stats['frame_ms'], stats.get('lights', 0),
                   stats.get('walls', 0)))

    def _persist_setting(self, key: str, value):
        """Запомнить настройку в settings.json, не теряя остальные ключи."""
        try:
            path = os.path.join(app_paths.home_dir(), "settings.json")
            try:
                with open(path, encoding="utf-8") as f:
                    cfg = json.load(f)
            except (OSError, ValueError):
                cfg = {}
            if not isinstance(cfg, dict):
                cfg = {}
            cfg[key] = value
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            # Настройка — не повод падать: в худшем случае она не запомнится.
            pass

    def build_settings_dialog(self) -> QDialog:
        """Окно настроек мастера: эффекты боя и 3D-кубики."""
        dlg = QDialog(self)
        dlg.setObjectName("settings_dialog")
        dlg.setWindowTitle("Настройки")
        dlg.setWindowIcon(app_icon())
        dlg.setMinimumWidth(380)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(10)

        title = QLabel("Эффекты стола")
        title.setObjectName("settings_title")
        lay.addWidget(title)

        self.combat_fx_checkbox = QCheckBox("Эффекты в бою")
        self.combat_fx_checkbox.setObjectName("combat_effects_checkbox")
        self.combat_fx_checkbox.setToolTip(
            "Кровь и звуки ударов. Анимация боя остаётся в любом случае")
        self.combat_fx_checkbox.setChecked(self.combat_effects_enabled)
        self.combat_fx_checkbox.toggled.connect(
            self.set_combat_effects_enabled)
        lay.addWidget(self.combat_fx_checkbox)

        hint = QLabel("Выключено — на карте нет крови и не звучат удары, "
                      "но сам удар анимирован как обычно.")
        hint.setObjectName("settings_hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        blood_row = QHBoxLayout()
        blood_row.addWidget(QLabel("Прозрачность крови"))
        self.blood_transparency_slider = QSlider(Qt.Horizontal)
        self.blood_transparency_slider.setObjectName("blood_transparency_slider")
        self.blood_transparency_slider.setRange(0, 100)
        self.blood_transparency_slider.setValue(self.blood_transparency)
        self.blood_transparency_slider.setToolTip(
            "0% — кровь обычная, 100% — её совсем не видно на карте")
        blood_row.addWidget(self.blood_transparency_slider, 1)
        self.blood_transparency_label = QLabel(f"{self.blood_transparency}%")
        self.blood_transparency_label.setFixedWidth(40)
        blood_row.addWidget(self.blood_transparency_label)
        lay.addLayout(blood_row)
        self.blood_transparency_slider.valueChanged.connect(
            self.set_blood_transparency)

        self.preload_actors_checkbox = QCheckBox(
            "Предзагружать игрокам моих актёров")
        self.preload_actors_checkbox.setObjectName("preload_my_actors_checkbox")
        self.preload_actors_checkbox.setToolTip(
            "Отправлять карточки актёров игрокам заранее. Выключено — токен "
            "приедет с карточкой при первом появлении")
        self.preload_actors_checkbox.setChecked(self.preload_my_actors)
        self.preload_actors_checkbox.toggled.connect(
            self.set_preload_my_actors)
        lay.addWidget(self.preload_actors_checkbox)

        self.dice_3d_checkbox = QCheckBox("3д дайсы")
        self.dice_3d_checkbox.setObjectName("dice_3d_checkbox")
        self.dice_3d_checkbox.setToolTip(
            "Кубики в окне Dice Lab вместо кнопок D20…D2")
        self.dice_3d_checkbox.setChecked(self.enable_3d)
        self.dice_3d_checkbox.toggled.connect(self.set_enable_3d)
        lay.addWidget(self.dice_3d_checkbox)

        # --- Зрение игроков ---
        vision_title = QLabel("Зрение игроков")
        vision_title.setObjectName("settings_title")
        lay.addWidget(vision_title)
        vision_row = QHBoxLayout()
        self.vision_cone_radio = QRadioButton("Поле зрения")
        self.vision_cone_radio.setObjectName("vision_cone_radio")
        self.vision_cone_radio.setToolTip(
            "Цветным видно только то, куда токен смотрит: угол и дальность "
            "из его карточки (R — поворот)")
        self.vision_around_radio = QRadioButton("Вокруг токена")
        self.vision_around_radio.setObjectName("vision_around_radio")
        self.vision_around_radio.setToolTip(
            "Цветным видно всё вокруг токена в радиусе зрения, как раньше")
        (self.vision_cone_radio if self.vision_mode == "cone"
         else self.vision_around_radio).setChecked(True)
        # Реагируем только на включение: радиокнопки в одной группе, и при
        # клике Qt гасит вторую — её toggled(False) шёл бы следом и возвращал
        # «вокруг токена», из-за чего режим не переключался вовсе.
        self.vision_cone_radio.toggled.connect(
            lambda on: on and self.set_vision_mode("cone"))
        self.vision_around_radio.toggled.connect(
            lambda on: on and self.set_vision_mode("around"))
        vision_row.addWidget(self.vision_cone_radio)
        vision_row.addWidget(self.vision_around_radio)
        vision_row.addStretch(1)
        lay.addLayout(vision_row)
        vision_hint = QLabel("«Поле зрения» — игрок видит цветным только "
                             "сектор перед токеном, остальное — ч/б.")
        vision_hint.setObjectName("settings_hint")
        vision_hint.setWordWrap(True)
        lay.addWidget(vision_hint)

        self.hide_dm_walls_checkbox = QCheckBox(
            "Скрыть у меня все стены")
        self.hide_dm_walls_checkbox.setObjectName("hide_dm_walls_checkbox")
        self.hide_dm_walls_checkbox.setToolTip(
            "Мастер не видит стены и окна — только двери. Для игры на "
            "телевизоре: заготовьте сцену заранее и скройте лишнее")
        self.hide_dm_walls_checkbox.setChecked(self.hide_dm_walls)
        self.hide_dm_walls_checkbox.toggled.connect(self.set_hide_dm_walls)
        lay.addWidget(self.hide_dm_walls_checkbox)

        self.dm_fog_checkbox = QCheckBox(
            "Включить для себя туман войны как у игроков")
        self.dm_fog_checkbox.setObjectName("dm_fog_checkbox")
        self.dm_fog_checkbox.setToolTip(
            "Мастер видит стол глазами всех токенов игроков: за стенами и "
            "в неразведанных местах — ч/б, своих актёров там не видно. "
            "Выделенный в списке актёр всё равно виден и им можно править")
        self.dm_fog_checkbox.setChecked(self.dm_fog)
        self.dm_fog_checkbox.toggled.connect(self.set_dm_fog)
        lay.addWidget(self.dm_fog_checkbox)

        # --- Управление ---
        wasd_title = QLabel("Управление")
        wasd_title.setObjectName("settings_title")
        lay.addWidget(wasd_title)
        self.wasd_checkbox = QCheckBox("Движение токена на WASD")
        self.wasd_checkbox.setObjectName("wasd_move_checkbox")
        self.wasd_checkbox.setToolTip(
            "W — вверх, A — влево, S — вниз, D — вправо. Шаг — клетка, "
            "пять клеток в секунду. Мастеру ведёт выделенный токен")
        self.wasd_checkbox.setChecked(self.wasd_movement)
        self.wasd_checkbox.toggled.connect(self.set_wasd_movement)
        lay.addWidget(self.wasd_checkbox)
        wasd_hint = QLabel("Рывок живёт на C и с WASD не спорит. Работает, "
                           "когда фокус на холсте.")
        wasd_hint.setObjectName("settings_hint")
        wasd_hint.setWordWrap(True)
        lay.addWidget(wasd_hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        lay.addWidget(buttons)

        self.settings_dialog = dlg
        return dlg

    def open_guide(self):
        """Открыть немодальный «Гайд» из верхней рейки.

        Держим один экземпляр: повторное нажатие поднимает уже открытое
        окно, а не плодит копии. Гайд доступен и игроку, и мастеру.
        """
        dialog = getattr(self, "_guide_dialog", None)
        if dialog is None:
            dialog = GuideDialog(
                self, theme=getattr(self, "current_theme", "dark"))
            # После закрытия C++-объект удаляется, а не копится скрытым
            # ребёнком главного окна при каждом открытии.
            dialog.setAttribute(Qt.WA_DeleteOnClose, True)
            dialog.finished.connect(
                lambda _=0: setattr(self, "_guide_dialog", None))
            self._guide_dialog = dialog
        else:
            dialog.set_theme(getattr(self, "current_theme", "dark"))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def open_support(self):
        """Окно «Поддержать»: одно на главное окно, без копий."""
        return open_support_window(
            self, getattr(self, "current_theme", "dark"))

    def open_settings_dialog(self):
        """Раздел «Настройки» в верхней рейке мастера."""
        dlg = self.build_settings_dialog()
        dlg.exec_()

    def open_rest_dialog(self):
        """Окно «Отдых» (костёр) — одно на главное окно, без копий."""
        dialog = getattr(self, "_rest_dialog", None)
        if dialog is None:
            dialog = RestDialog(self, short_left=self.short_rests_left)
            dialog.restRequested.connect(self.send_rest)
            dialog.timeOfDayRequested.connect(self.set_table_time_of_day)
            dialog.setAttribute(Qt.WA_DeleteOnClose, True)
            dialog.finished.connect(
                lambda _=0: setattr(self, "_rest_dialog", None))
            self._rest_dialog = dialog
        else:
            dialog.set_short_left(self.short_rests_left)
        dialog.set_time_of_day(
            self.canvas.time_of_day if self.canvas.current_map else None)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def send_rest(self, kind: str):
        """Попросить сервер отдохнуть на весь стол.

        Счётчик коротких передышек ведёт клиент мастера: в 5e их две между
        долгими отдыхами, а долгий отдых сбрасывает его обратно.
        """
        kind = "short" if str(kind or "").strip().lower() == "short" else "long"
        if self.player_role != Role.DM:
            self.statusBar().showMessage(
                "Отдых объявляет мастер", 4000)
            return
        if kind == "short":
            if self.short_rests_left <= 0:
                self.statusBar().showMessage(
                    "Короткие передышки кончились — нужен долгий отдых",
                    4000)
                self._sync_rest_dialog()
                return
            self.short_rests_left -= 1
        else:
            self.short_rests_left = self.short_rests_max
        self._sync_rest_dialog()
        self.network_client.send_message(MessageType.REST, {"kind": kind})

    def _sync_rest_dialog(self):
        dialog = getattr(self, "_rest_dialog", None)
        if dialog is not None:
            dialog.set_short_left(self.short_rests_left)
            dialog.set_time_of_day(
                self.canvas.time_of_day if self.canvas.current_map else None)

    def set_table_time_of_day(self, mode: str):
        """День/Ночь из окна отдыха — та же смена, что в меню «Стол».

        Длинный отдых почти всегда начинается с «наступает ночь», поэтому
        переключатель живёт и здесь, а не только на вкладке «Освещение».
        """
        if self.player_role != Role.DM:
            return
        night = str(mode or "").strip().lower() == "night"
        if self.canvas.current_map is None:
            self.statusBar().showMessage("Сначала откройте карту", 3000)
            self._sync_rest_dialog()
            return
        if (self.canvas.time_of_day == "night") == night:
            self._sync_rest_dialog()
            return
        # Холст сам разошлёт время суток всем (on_time_of_day_changed).
        self.canvas.toggle_time_of_day(night)

    def on_rest_result(self, data: dict):
        """Сервер разослал отдых: строка в чате и счётчик у мастера."""
        if not isinstance(data, dict):
            return
        kind = str(data.get("kind") or "long")
        long = bool(data.get("long", kind != "short"))
        if long:
            self.short_rests_left = self.short_rests_max
            self._sync_rest_dialog()
        rows = [row for row in (data.get("tokens") or [])
                if isinstance(row, dict)]
        if not rows:
            self.append_system("Отдых: персонажей за столом нет")
            return
        gained = 0
        for row in rows:
            try:
                gained += max(0, int(row.get("hp") or 0)
                              - int(row.get("hp_before") or 0))
            except (TypeError, ValueError):
                continue
        label = "Долгий отдых" if long else "Короткий отдых"
        names = ", ".join(str(row.get("name") or "?") for row in rows)
        self.append_system(
            f"{label}: {names}" + (f" (+{gained} хитов)" if gained else ""))
        # Одна карточка на весь отдых: у каждого персонажа своя цифра —
        # это стопка «лечений», которая заслонила бы бросок.
        count = len(rows)
        people = (f"{count} персонаж"
                  if count % 10 == 1 and count % 100 != 11
                  else f"{count} персонажа"
                  if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14
                  else f"{count} персонажей")
        self.show_roll_toast(
            label, f"+{gained}" if gained else str(count),
            f"{people} · хиты, ячейки и заряды", ROLL_GOOD)
        dialog = getattr(self, "_rest_dialog", None)
        if dialog is not None:
            dialog.set_status(
                f"{label}: восстановлено {len(rows)} персонажей")

    def _refresh_color_button(self):
        """Образец цвета: квадрат текущего цвета, а не стандартная иконка."""
        if getattr(self, "color_button", None) is None:
            return
        pix = QPixmap(30, 22)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(*self.canvas.drawing_color))
        painter.setPen(QPen(QColor("#8a8a92"), 1))
        painter.drawRoundedRect(1, 1, pix.width() - 2, pix.height() - 2, 3, 3)
        painter.end()
        self.color_button.setIcon(QIcon(pix))
        self.color_button.setIconSize(QSize(30, 22))

    def _make_menu_button(self, toolbar, text, tooltip, entries):
        """Одна кнопка в тулбаре, внутри — выпадающее меню.

        Верхняя панель была перегружена: сетка занимала три кнопки, растр —
        семь плюс слайдеры. Теперь это два меню, а освободившееся место
        отдано слайдерам кисти.
        """
        btn = QToolButton(toolbar)
        btn.setText(text)
        btn.setToolTip(tooltip)
        btn.setPopupMode(QToolButton.InstantPopup)
        btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        menu = QMenu(btn)
        menu.setToolTipsVisible(True)
        # Общий формат пунктов с ПКМ-меню холста: плоские пункты, подменю
        # и галочки — одна реализация на все меню стола.
        fill_menu_entries(menu, entries)
        btn.setMenu(menu)
        toolbar.addWidget(btn)
        return btn, menu

    def _refresh_board_menu(self):
        """Пересобрать меню «Стол» перед показом.

        Меню строится один раз вместе с панелью, а состав пунктов зависит от
        стола: «Удалить карту» появляется только при открытой карте, галочка
        привязки отражает текущее состояние.
        """
        self.board_menu.clear()
        fill_menu_entries(self.board_menu, self.canvas.board_menu_entries())

    def _add_support_button(self, toolbar):
        """Прижать «Поддержать» к правому краю рейки.

        QToolBar выстраивает действия слева направо, поэтому перед кнопкой
        кладём растяжку: без неё «Поддержать» стояла бы сразу после «Гайда».
        """
        spacer = QWidget(toolbar)
        spacer.setObjectName("toolbar_spacer")
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        toolbar.addWidget(spacer)
        toolbar.addSeparator()

        # В рейке — только монетка: с надписью кнопка занимала 120 px и на
        # ширине 1366 уезжала в меню «>>». У действия остаётся текст, чтобы
        # пункт в этом меню был подписан, а сама кнопка показывает только иконку.
        self.act_support = QAction(support_icon(), "Поддержать", self)
        self.act_support.setToolTip(
            "Поддержать: QR, Boosty, обратная связь")
        self.act_support.triggered.connect(self.open_support)
        toolbar.addAction(self.act_support)
        support_btn = toolbar.widgetForAction(self.act_support)
        if support_btn is not None:
            support_btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
            support_btn.setAccessibleName("Поддержать")

    def build_toolbar(self):
        tb = QToolBar("Инструменты")
        tb.setIconSize(QSize(20, 20))
        # У большинства действий только текст; у D20 есть иконка, и подпись
        # рядом с ней делает кнопку понятной без наведения.
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        tb.setMovable(False)

        # --- Кубики ---
        # Иконка d20 в верхней полосе: при включённых 3D-кубиках открывает
        # лоток Dice Lab, иначе показывает кнопки кубиков в чате.
        self.act_dice = QAction(dice_icon(), " D20", self)
        self.act_dice.setToolTip(
            "Бросить кубики: Dice Lab или D20…D2 в чате")
        self.act_dice.triggered.connect(self.open_dice)
        tb.addAction(self.act_dice)
        tb.addSeparator()

        # --- Общее ---
        act = QAction("🖱 Курсор (V)", self)
        act.triggered.connect(self.reset_tools)
        tb.addAction(act)

        # Сетка: три настройки в одном выпадающем меню
        self.grid_button, self.grid_menu = self._make_menu_button(
            tb, "\U0001F4D0 Сетка \u25BE",
            "Показать или скрыть сетку, настроить шаг и смещение, "
            "откалибровать по двум точкам",
            (("Сетка вкл/выкл", "Показать или скрыть линии сетки",
              self.toggle_grid),
             ("Настройки сетки (Ctrl+G)",
              "Шаг и смещение начала сетки", self.open_grid_settings),
             ("Калибровать по двум точкам (Ctrl+Shift+G)",
              "Указать две линии сетки на карте", self.start_grid_calibration)))

        act = QAction("Центрировать (Tab)", self)
        act.triggered.connect(self.canvas.fit_map_to_screen)
        tb.addAction(act)

        # Стол: те же действия, что по правому клику на пустом холсте, но
        # под рукой в верхней панели — не надо целиться в карту. У игрока
        # действий стола нет, поэтому кнопку ему не показываем.
        self.board_button, self.board_menu = self._make_menu_button(
            tb, "🗺 Стол ▾",
            "Действия стола: токены, карты, рисование, области, сетка",
            self.canvas.board_menu_entries())
        # Список зависит от состояния стола (открыта ли карта, включена ли
        # привязка), поэтому меню пересобирается перед каждым показом.
        self.board_menu.aboutToShow.connect(self._refresh_board_menu)
        self.board_button.setVisible(self.player_role == Role.DM)

        # Бой: всё, что мастер делает за столом, — в одном меню.
        self.combat_button, self.combat_menu = self._make_menu_button(
            tb, "⚔ Бой ▾",
            "Карточка актёра, атака, спасбросок и состояния выбранного токена",
            (("Карточка актёра… (редакт.)",
              "Характеристики, атаки, спасброски и защита токена",
              self.open_actor_sheet),
             ("Атаковать…", "Выбрать атакующего и цель, бросить атаку и урон",
              self.open_attack_dialog),
             ("Спасбросок…", "Бонус берётся с листа персонажа или из статблока",
              self.open_save_dialog),
             ("Состояния…", "Отравлен, Сбит с ног, Опутан и остальные",
              self.open_conditions_dialog),
             None,
             ("Бросить инициативу всем", "d20 + Ловкость каждому на карте",
              self.roll_all_initiative),
             ("Бросить инициативу своим НПС", "Только актёрам и существам",
              self.roll_initiative_for_npcs)))

        # Слоты: мастер создаёт места за столом, игрок занимает свободное.
        act = QAction("👥 Слоты", self)
        act.setToolTip("Слоты игроков: мастер выдаёт места, игрок занимает "
                       "свободное")
        act.triggered.connect(self.open_slots_dialog)
        tb.addAction(act)

        # Кнопки листа на полосе мастера нет: лист живёт вкладкой в левой
        # панели, туда и ведёт его иконка в рейке разделов.

        # Быстрая атака: выбран токен A, клик по B атакует его базовой
        # атакой из листа. Доступна и игроку, и ДМ.
        self.act_quick_attack = QAction("⚔ Быстрая атака (Q)", self)
        self.act_quick_attack.setToolTip(
            "Клик по цели — удар базовой атакой из листа. Esc отменяет")
        self.act_quick_attack.setCheckable(True)
        self.act_quick_attack.triggered.connect(self.toggle_quick_attack_mode)
        tb.addAction(self.act_quick_attack)

        # Рывок: доступен в свой ход, один раз за ход. Кнопка не checkable —
        # состояние показывает статусбар, а повторное нажатие просто ничего
        # не добавляет (иначе зелёные клетки росли бы без предела).
        self.act_dash = QAction("🏃 Рывок (C)", self)
        self.act_dash.setToolTip(
            "Рывок: ещё одна скорость на ход, не больше ×2")
        self.act_dash.triggered.connect(self.hotkey_dash)
        tb.addAction(self.act_dash)

        # Линейка: 1 клетка = 5 футов. Доступна и игроку — измерение локальное.
        self.act_ruler = QAction("📏 Линейка (M)", self)
        self.act_ruler.setToolTip(
            "Линейка в футах: клетка — 5 футов")
        self.act_ruler.setCheckable(True)
        self.act_ruler.triggered.connect(self.toggle_ruler)
        tb.addAction(self.act_ruler)

        # Гайд — общий для мастера и игрока, поэтому стоит ДО раннего
        # выхода для игрока ниже. Окно немодальное: можно читать и играть.
        tb.addSeparator()
        self.act_guide = QAction("📖 Гайд", self)
        self.act_guide.setToolTip(
            "Краткий мануал по столу и все горячие клавиши")
        self.act_guide.triggered.connect(self.open_guide)
        tb.addAction(self.act_guide)

        # Отмены и возврата на полосе нет намеренно: это действия на хоткеях
        # (Ctrl+Z / Ctrl+Shift+Z). Постоянные кнопки занимали место и путались
        # с «Стереть всё», которое необратимо.
        # QAction НЕ получают шорткат: в окне уже есть QShortcut на Ctrl+Z,
        # Ctrl+Shift+Z и Ctrl+Y. Два обработчика на одну клавишу дают
        # «ambiguous shortcut» — и не срабатывает ни один. QAction держим
        # только как состояние (setEnabled по истории), на полосу не ставим.
        self.act_undo = QAction("Отменить", self)
        self.act_undo.setEnabled(False)
        self.act_redo = QAction("Вернуть", self)
        self.act_redo.setEnabled(False)

        # --- Рисование ---
        # Инструменты рисования больше не живут в верхней рейке: они
        # переехали в правую панель — последняя вкладка «Рисование»
        # (build_draw_section). Здесь остался только разделитель.
        #
        # Права: в этой игре рисовать может только DM (сервер проверяет то же
        # самое в dm_only). Игрокам кнопки не показываем, иначе они получат
        # «доступ запрещён». Хотите разрешить всем — перенесите DRAW_OP,
        # CLEAR_DRAWINGS и UNDO_DRAWING из dm_only в everyone на сервере.
        if self.player_role != Role.DM:
            # Игроку доступна только общая часть рейки, но «Поддержать»
            # есть у всех — и именно справа.
            self._add_support_button(tb)
            return
        tb.addSeparator()

        # --- Миры: сохранить сессию в файл и загрузить обратно ---
        # Файл мира живёт на сервере (машина хоста):
        # <dataPath>/worlds/<ид>/world.json. Теми же файлами торгует
        # консоль `vtt package pack/unpack`.
        self.world_button = QToolButton(tb)
        self.world_button.setText("Мир")
        self.world_button.setToolTip("Сохранить, загрузить или удалить мир")
        self.world_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.world_button.clicked.connect(self.open_world_dialog)
        tb.addWidget(self.world_button)

        # Настройки стола: эффекты боя и 3D-кубики. Раздел только у мастера:
        # галочки меняют то, что видит и слышит весь стол.
        self.settings_button = QToolButton(tb)
        self.settings_button.setObjectName("settings_button")
        self.settings_button.setText("⚙ Настройки")
        self.settings_button.setToolTip(
            "Настройки мастера: эффекты, кровь, 3D-дайсы, актёры")
        self.settings_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.settings_button.clicked.connect(self.open_settings_dialog)
        tb.addWidget(self.settings_button)

        # Костёр — отдельное окно отдыха всего стола. Живёт справа от
        # настроек: это такая же команда мастера, а не действие карты.
        self.rest_button = QToolButton(tb)
        self.rest_button.setObjectName("rest_button")
        self.rest_button.setText("🔥 Отдых")
        self.rest_button.setToolTip(
            "Отдых партии: долгий или короткий — хиты, ячейки и заряды "
            "всем персонажам сразу")
        self.rest_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.rest_button.clicked.connect(self.open_rest_dialog)
        tb.addWidget(self.rest_button)

        # «Поддержать» — последней в рейке, у самого правого края и у
        # мастера, и у игрока.
        self._add_support_button(tb)

        # Кнопка-глазок живёт в строке токена на правой панели, отдельная
        # кнопка на панели инструментов была лишней.

    def build_log_dock(self):
        self.log_dock = QDockWidget("Журнал изменений", self)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_dock.setWidget(self.log_view)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.log_dock)
        self.log_dock.hide()

    # ------------------------------------------------------------------
    # Сеть
    # ------------------------------------------------------------------
    def send_connect(self):
        self.network_client.send_message(MessageType.CONNECT, {
            'player_id': self.player_id,
            'nickname': self.nickname,
            'role': self.player_role.value,
            'avatar_path': self.avatar_path,
        })

    def check_link(self):
        if not self.network_client.connected:
            self.statusBar().showMessage("Подключение потеряно. "
                                         "Перезапустите приложение для переподключения.")

    def on_network_message(self, msg_type: MessageType, data: dict):
        """Вызывается из сетевого потока — только пересылаем сигнал.

        Сетевой поток может успеть вызвать колбэк в момент закрытия окна.
        После удаления C++-объекта emit падает внутри sip, а не исключением
        Python, поэтому проверяем живость окна до обращения к сигналу.
        """
        if sip.isdeleted(self):
            return
        self.net_message.emit(msg_type, data)

    def handle_message_ui(self, msg_type: MessageType, data: dict):
        """Разобрать сообщение сервера — уже в потоке GUI."""
        if sip.isdeleted(self):
            return
        if msg_type == MessageType.ASSET_READY:
            self.on_asset_ready(data)
            return
        if msg_type == MessageType.ERROR:
            # Неудачная отправка карты не должна оставить крутиться d20
            # навсегда: ошибка приходит из очереди загрузки или от сервера.
            if self._map_upload_loading_id:
                self.canvas.cancel_map_loading(self._map_upload_loading_id)
                self._map_upload_loading_id = None
            self.append_system(data.get('message', 'Network error'))
            return
        handler = {
            MessageType.FULL_STATE: self.on_full_state,
            MessageType.PLAYER_JOINED: self.on_player_joined,
            MessageType.PLAYER_LEFT: self.on_player_left,
            MessageType.ADD_MAP: self.on_add_map,
            MessageType.CHANGE_MAP: self.on_change_map,
            MessageType.UPDATE_MAP: self.on_update_map,
            MessageType.REMOVE_MAP: self.on_remove_map,
            MessageType.UPDATE_GRID: self.on_update_grid,
            MessageType.UPDATE_TIME: self.on_update_time,
            MessageType.EXPLORED: self.on_explored_rooms,
            MessageType.SNAP: self.on_snap,
            MessageType.VISION: self.on_vision,
            MessageType.UPDATE_LIGHTS: self.on_update_lights,
            MessageType.UPDATE_WALLS: self.on_update_walls,
            MessageType.TOGGLE_DOOR: self.on_door_state,
            MessageType.DOOR_LOCK: self.on_door_lock,
            MessageType.ADD_MAP_OBJECT: self.on_add_map_object,
            MessageType.UPDATE_MAP_OBJECT: self.on_update_map_object,
            MessageType.REMOVE_MAP_OBJECT: self.on_remove_map_object,
            MessageType.ADD_TOKEN: self.on_add_token,
            MessageType.MOVE_TOKEN: self.on_move_token,
            MessageType.REMOVE_TOKEN: self.on_remove_token_net,
            MessageType.UPDATE_TOKEN: self.on_update_token_net,
            MessageType.TOGGLE_TOKEN_VISIBILITY: self.on_token_visibility_net,
            MessageType.DRAW_OP: self.on_draw_op_net,
            MessageType.MAP_TEXT_ADDED: self.on_map_text_added_net,
            MessageType.MAP_TEXT_UPDATED: self.on_map_text_updated_net,
            MessageType.MAP_TEXT_REMOVED: self.on_map_text_removed_net,
            MessageType.CLEAR_DRAWINGS: self.on_clear_drawings_net,
            MessageType.UNDO_DRAWING: self.on_undo_drawing_net,
            MessageType.ADD_AUDIO: self.on_add_audio_net,
            MessageType.PLAY_SOUND: self.on_play_sound_net,
            MessageType.PLAY_MUSIC: self.on_play_music_net,
            MessageType.CHAT_MESSAGE: self.on_chat_net,
            MessageType.ROLL_DICE: self.on_roll_net,
            MessageType.INITIATIVE_UPDATE: self.on_initiative_net,
            MessageType.MOVEMENT_STATE: self.on_movement_state,
            MessageType.AREA_RESULT: self.on_area_result,
            MessageType.SPELL_RESULT: self.on_spell_result,
            # Боевые результаты приходят от сервера: кубики бросает он, клиент
            # только показывает.
            MessageType.ATTACK_RESULT: self.on_attack_result,
            MessageType.SAVE_RESULT: self.on_save_result,
            MessageType.STEALTH_RESULT: self.on_stealth_result,
            MessageType.SPECIAL_ATTACK_RESULT: self.on_special_attack_result,
            MessageType.DEATH_SAVE_RESULT: self.on_death_save_result,
            MessageType.ROLL_INITIATIVE: self.on_initiative_rolled,
            MessageType.SHEET_STATE: self.on_sheet_state,
            # Отдых всего стола: сервер уже разослал хиты и листы, здесь —
            # только строка в чате и счётчик коротких передышек у мастера.
            MessageType.REST_RESULT: self.on_rest_result,
            MessageType.SLOTS_STATE: self.on_slots_state,
            MessageType.LOG_ENTRY: self.on_log_net,
            MessageType.PERMISSION_DENIED: self.on_denied,
            MessageType.MY_ACTORS_UPDATE: self.on_my_actors_update,
            MessageType.MY_ACTORS_REMOVE: self.on_my_actors_remove,
            MessageType.MY_ACTORS_CLEAR: self.on_my_actors_clear,
        }.get(msg_type)
        if handler:
            handler(data)

    # --- Полное состояние ---
    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.setWindowState(self._pre_fullscreen_state)
            if not self.isMaximized():
                self.restoreGeometry(self._pre_fullscreen_geometry)
        else:
            self._pre_fullscreen_state = self.windowState()
            self._pre_fullscreen_geometry = self.saveGeometry()
            self.showFullScreen()

    def on_asset_ready(self, data):
        asset_id, path = data['id'], data['path']
        if str(asset_id).startswith('myactor:'):
            # Предзагруженная картинка актёра: на карте её пока нет.
            self.preloaded_actor_images[
                str(asset_id).split(':', 1)[1]] = path
            return
        self.local_assets[asset_id] = path
        m = self.game_state.maps.get(asset_id)
        if m and self.game_state.current_map_id == asset_id:
            view = (self.canvas.scale, self.canvas.offset_x, self.canvas.offset_y)
            self.canvas.set_map(m, path)
            # Большая карта могла быть уменьшена под лимит видеокарты:
            # иначе она рисовалась бы чёрным прямоугольником.
            map_note = (self.canvas.map_error_note
                        or self.canvas.map_downscaled_note)
            if map_note:
                self.statusBar().showMessage(map_note, 8000)
            self.canvas.scale, self.canvas.offset_x, self.canvas.offset_y = view
            self.canvas.set_draw_ops(self.game_state.draw_ops.get(asset_id, []))
            self.canvas.set_map_texts(
                (self.game_state.map_texts or {}).get(asset_id, []))
        # Заставка завершается для любой карты, не только для открытой:
        # мастер мог добавлять карту, не переключаясь на неё.
        if m:
            self.canvas.finish_map_loading(asset_id)
        obj = self.game_state.map_objects.get(asset_id)
        if obj:
            self.canvas.add_object(obj, path)
        token = self.game_state.tokens.get(asset_id)
        if token:
            self.canvas.token_images.pop(asset_id, None)
            self.canvas.add_token(token, path)
        track = self.game_state.audio_tracks.get(asset_id)
        if track:
            self.on_add_audio_net({'id': asset_id, 'name': track.name,
                                   'type': track.type, 'path': path})
        self.canvas.update()

    def on_full_state(self, data: dict):
        state = data.get('game_state', {})
        self.game_state = GameState.from_dict(state)
        # Модуль освещения включает мастер; клиент следует за сервером,
        # иначе у игрока ночь была бы просто тёмной картинкой без света.
        if 'lighting_enabled' in state:
            self.canvas.set_lighting_enabled(
                bool(state.get('lighting_enabled')))
        # Привязка токенов к клеткам — такое же состояние стола, как свет:
        # новый клиент получает её сразу вместе с миром.
        if 'snap_tokens' in state:
            self.canvas.set_snap(bool(state.get('snap_tokens')), notify=False)
        # Зрение игроков: у стола один режим. При подключении мастер задаёт
        # его своей настройкой из settings.json, игрок — берёт с сервера.
        if 'vision_mode' in state:
            local = self.vision_mode
            self.set_vision_mode(state.get('vision_mode'), notify=False)
            if self.player_role == Role.DM:
                self.set_vision_mode(local, notify=False)
                self.on_vision_requested(local)
        # Мастер с включённой предзагрузкой отдаёт библиотеку игрокам,
        # как только соединение готово (при старте и переподключении).
        if (self.player_role == Role.DM and self.preload_my_actors
                and hasattr(self, 'my_actors_page')):
            self.push_my_actors_to_server()
        # Файлы едут отдельным каналом: сервер шлёт только описание (SHA-256,
        # размер, имя), а сами байты качает фоновый поток и присылает
        # ASSET_READY. Прежняя ветка с base64 в состоянии была уже мертва,
        # потому что сервер эти байты не отправляет.

        # Игроки
        self.refresh_players()

        # Карты (для DM — список)
        if self.player_role == Role.DM:
            self.maps_list.clear()
            self.map_rows = []
            for map_id, m in self.game_state.maps.items():
                self.maps_list.addItem(m.name)
                self.map_rows.append((map_id, m.name))

        # Восстанавливаем звуки и музыку при подключении к текущей сессии.
        self.audio.clear_playlist()
        self.music_ids.clear()
        if self.player_role == Role.DM:
            self.music_list.clear()
            self.audio_list.clear()
            self.audio_rows.clear()
        for audio_id, track in self.game_state.audio_tracks.items():
            self.on_add_audio_net({**track.to_dict(),
                                   'path': self.local_assets.get(audio_id, track.path)})

        # Текущая карта
        if self.game_state.current_map_id:
            self.on_change_map({'map_id': self.game_state.current_map_id})

        # Токены. Холст начинаем с нуля: иначе после загрузки мира на нём
        # остались бы токены прошлой сессии. Анимации прежней сессии тоже
        # сбрасываем — их координаты относятся к старым токенам.
        self.canvas.clear_animations()
        self.canvas.tokens.clear()
        self.canvas.token_images.clear()
        self.canvas.selected_tokens.clear()
        self.canvas.selected_token = None
        # Объекты — тоже с нуля: старые остались бы от прошлой сессии.
        self.canvas.objects.clear()
        self.canvas.object_images.clear()
        self.canvas.selected_object_id = None
        for obj in self.game_state.map_objects.values():
            self.canvas.add_object(
                obj, self.local_assets.get(obj.id, obj.image_path))
        for t in self.game_state.tokens.values():
            self.canvas.add_token(t, self._token_image_path(t))
        # Список слева пересобираем ПОСЛЕ добавления: on_change_map успевает
        # построить его пустым, и загруженные токены не появлялись в панели.
        if self.player_role == Role.DM:
            self.rebuild_token_rows()
            self.refresh_initiative()
            self.refresh_object_rows()

        # Листы персонажей. Раньше сюда не смотрели: после загрузки мира
        # лист лежал в состоянии, но в панели оставался пустым, и игрок
        # видел «Лист не загружен».
        self._adopt_sheets()
        # Страницы вкладки «Лист персонажа» — по токенам игрока.
        self.sync_token_pages()
        # Отметки смерти приехали вместе с состоянием: показываем их сразу,
        # иначе лист до первого броска показывал живого персонажа.
        for token in self.game_state.tokens.values():
            self._apply_death_state_to_views(token)

        # Бой: очередь ходов и подсветка текущего токена
        gs = self.game_state
        turn_id = (gs.combat_order[gs.combat_turn]
                   if gs.combat_active and gs.combat_order
                   and 0 <= gs.combat_turn < len(gs.combat_order) else None)
        self.canvas.set_combat_mode(gs.combat_active)
        self.canvas.set_turn_context(gs.combat_round, gs.combat_turn, turn_id)
        # В бою мастер сразу видит фигуру, чей ход: она выделена и готова
        # к ходу по клеткам.
        if self.player_role == Role.DM and turn_id:
            self._turn_focus = turn_id
            self._focus_turn_token(turn_id)
        self.refresh_initiative()

        # Растровый рисунок текущей карты (чтобы новый игрок увидел готовое)
        if self.canvas.current_map:
            ops = (self.game_state.draw_ops or {}).get(self.canvas.current_map.id, [])
            self.canvas.set_draw_ops(ops)
            # Надписи едут вместе с картой: без этого у поздно подключившегося
            # стола они бы пропали до первой правки.
            self.canvas.set_map_texts(
                (self.game_state.map_texts or {}).get(
                    self.canvas.current_map.id, []))

        # Журнал
        for entry in state.get('log_entries', []):
            self.append_log(entry)

        if state.get('game_started'):
            self.statusBar().showMessage("Игра уже идёт")

    # --- Карты ---
    def on_add_map(self, data: dict):
        """Добавление карты: получаем файл и показываем местоположение"""
        # Картинку привезёт ASSET_READY: в сообщении только описание файла.
        m = MapData.from_dict(data)
        self.game_state.maps[m.id] = m
        # Карта доехала после смены сцены — доводим переключение до конца.
        if getattr(self, "_pending_change_map_id", None) == m.id:
            self._pending_change_map_id = None
            self.on_change_map({'map_id': m.id})
        # Сервер делает добавленную карту текущей, только если до неё не было
        # ни одной. Только для такой карты и нужна заставка «она сейчас
        # появится»: добавление второй карты в список ничего не грузит.
        if self.game_state.current_map_id in (None, m.id):
            self._pending_map_add_id = m.id
        # Файл доехал до сервера; ждать осталось только ASSET_READY.
        if self._map_upload_loading_id == m.id:
            self._map_upload_loading_id = None

        if self.player_role == Role.DM:
            self.maps_list.addItem(m.name)
            self.map_rows.append((m.id, m.name))

    def on_change_map(self, data: dict):
        """Ключевой обработчик: карта реально встаёт на полотно"""
        map_id = data.get('map_id')
        m = self.game_state.maps.get(map_id)
        if not m:
            # Смена карты пришла раньше её описания (карта ещё едет).
            # Раньше такой приказ просто терялся: клиент оставался на
            # прошлой сцене, пока мастер не переключит карту ещё раз.
            self._pending_change_map_id = map_id
            self.game_state.current_map_id = map_id
            return
        self._pending_change_map_id = None
        self.game_state.current_map_id = map_id
        path = self.local_assets.get(map_id, m.image_path)
        # Заставка нужна, когда карта только что добавлена или её файл ещё
        # не доехал (у игрока CHANGE_MAP приходит раньше ASSET_READY).
        loading = self.canvas.map_loading_map_id()
        if loading and loading != map_id:
            self.canvas.cancel_map_loading()
        image_ready = bool(path and os.path.isfile(path))
        show_loading = (map_id == self._pending_map_add_id
                        or not image_ready)
        if show_loading:
            self.canvas.begin_map_loading(map_id)
        self.canvas.set_map(m, path)
        map_note = self.canvas.map_error_note or self.canvas.map_downscaled_note
        if map_note:
            self.statusBar().showMessage(map_note, 8000)
        if image_ready:
            self.canvas.finish_map_loading(map_id)
        if map_id == self._pending_map_add_id:
            self._pending_map_add_id = None
        # Новая карта — другой набор токенов: сбрасываем выделение и
        # пересобираем список, иначе на холсте остались бы выделенными
        # токены, которых на этой карте нет.
        self._cancel_target_pick()
        self.canvas.selected_tokens.clear()
        self.canvas.dragging_tokens = []
        self.canvas.group_drag_offsets.clear()
        self.canvas.selected_token = None
        if self.player_role == Role.DM:
            self.rebuild_token_rows()
            self.refresh_initiative()
            self.refresh_object_rows()
        # У каждой карты свой растровый рисунок — подгружаем именно её
        ops = (self.game_state.draw_ops or {}).get(map_id, [])
        self.canvas.set_draw_ops(ops)
        # Надписи тоже принадлежат карте, а не столу целиком.
        self.canvas.set_map_texts((self.game_state.map_texts or {}).get(map_id, []))
        # Строку списка приводим к показанной карте. Срабатывает только при
        # реальной смене карты, поэтому выбор пользователя не перебивает.
        if self.player_role == Role.DM:
            for row, (mid, _name) in enumerate(self.map_rows):
                if mid == map_id:
                    self.maps_list.blockSignals(True)
                    self.maps_list.setCurrentRow(row)
                    self.maps_list.blockSignals(False)
                    break
        self.statusBar().showMessage(f"Карта: {m.name}")
        # У каждой карты свой свет — список в открытой вкладке надо обновить.
        self._sync_lighting_ui()

    def on_map_geometry_changed(self, data: dict):
        """DM подвинул или растянул карту — рассылаем всем участникам.

        Раньше это оставалось локальным: экран обновлялся, но данные
        на сервер не уходили, поэтому у остальных карта не менялась.
        """
        self.network_client.send_message(MessageType.UPDATE_MAP, data)

    def on_update_map(self, data: dict):
        """Пришли новые границы карты — применяем у себя"""
        map_id = data.get('map_id')
        m = self.game_state.maps.get(map_id)
        if m:
            for f in ('pos_x', 'pos_y', 'width', 'height', 'rotation',
                      'img_width', 'img_height'):
                if f in data:
                    setattr(m, f, data[f])
        # Полотно держит собственную ссылку на карту — обновляем и её
        if self.canvas.current_map and self.canvas.current_map.id == map_id:
            cm = self.canvas.current_map
            for f in ('pos_x', 'pos_y', 'width', 'height', 'rotation',
                      'img_width', 'img_height'):
                if f in data:
                    setattr(cm, f, data[f])
            self.canvas.update()
        # Магнит стен — часть карты (и мира): обновляем модель и панель.
        if any(key in data for key in ('wall_snap', 'wall_snap_step')):
            if m is not None:
                if 'wall_snap' in data:
                    m.wall_snap = bool(data['wall_snap'])
                if 'wall_snap_step' in data:
                    m.wall_snap_step = wall_rules.normalize_snap_step(
                        data['wall_snap_step'])
            self._sync_wall_snap_ui()

    def on_remove_map(self, data: dict):
        map_id = data.get('map_id')
        self.canvas.cancel_map_loading(map_id)
        if self._map_upload_loading_id == map_id:
            self._map_upload_loading_id = None
        self.game_state.maps.pop(map_id, None)
        # Объекты удалённой карты не должны висеть в памяти и в списке.
        for object_id, obj in list(self.game_state.map_objects.items()):
            if obj.map_id == map_id:
                self.game_state.map_objects.pop(object_id, None)
                self.canvas.remove_object(object_id)
                self.local_assets.pop(object_id, None)
        self.map_rows = [r for r in self.map_rows if r[0] != map_id]
        if self.player_role == Role.DM:
            self.maps_list.clear()
            for _, name in self.map_rows:
                self.maps_list.addItem(name)
            self.refresh_object_rows()

    def on_update_grid(self, data: dict):
        """Сетку изменил мастер: обновляем модель и, если карта открыта, холст.

        canvas.current_map — тот же объект, что и в game_state.maps, поэтому
        пишем один раз; отдельно синхронизировать холст не нужно.
        """
        map_id = data.get('map_id')
        m = self.game_state.maps.get(map_id)
        if not m:
            return
        # Меняем только присланные поля: остальные остаются как были.
        grid = m.grid
        if 'grid_cols' in data:
            grid.cols = clamp_cells(data['grid_cols'])
        if 'grid_rows' in data:
            grid.rows = clamp_cells(data['grid_rows'])
        if 'grid_visible' in data:
            grid.visible = bool(data['grid_visible'])
        m.apply_grid(grid)
        self.canvas.update()

    def on_time_of_day_changed(self, mode: str):
        """Холст переключил день/ночь — разослать всем через сервер."""
        m = self.canvas.current_map
        if m is None:
            return
        color, alpha = self.canvas.night_tint_pair()
        self.network_client.send_message(MessageType.UPDATE_TIME, {
            'map_id': m.id,
            'time_of_day': normalize_time_of_day(mode),
            'night_color': color,
            'night_alpha': alpha,
        })
        self.statusBar().showMessage(
            "На карте ночь" if normalize_time_of_day(mode) == "night"
            else "На карте день", 2500)
        self._sync_lighting_ui()
        # Окно отдыха могло быть открыто: его кнопки повторяют состояние карты.
        self._sync_rest_dialog()

    def on_explored_rooms_changed(self, map_id: str, rooms: list):
        """Холст разведал комнаты — отправить серверу, чтобы не потерять.

        Разведка — состояние стола, а не сессии клиента: сервер складывает
        её на карту, и она уезжает с миром (автосохранение, файл мира).
        """
        if not map_id or not rooms:
            return
        pending = getattr(self, "_pending_explored", None)
        if pending is None:
            pending = {}
            self._pending_explored = pending
        pending.setdefault(map_id, []).extend(rooms)
        timer = getattr(self, "_explored_send_timer", None)
        if timer is None:
            self._send_explored_rooms()
        else:
            timer.start()

    def _send_explored_rooms(self):
        """Отправить накопленную разведку одним сообщением на карту."""
        pending = getattr(self, "_pending_explored", None) or {}
        self._pending_explored = {}
        for map_id, rooms in pending.items():
            if not map_id or not rooms:
                continue
            self.network_client.send_message(MessageType.MARK_EXPLORED, {
                'map_id': map_id,
                'rooms': rooms,
            })

    def on_explored_rooms(self, data: dict):
        """Разведка с сервера: слить с картой и перерисовать туман.

        Приходит полный список комнат карты, поэтому его можно смело
        объединять со своим: чужой шаг по комнате тоже открывает её у стола.
        """
        m = self.game_state.maps.get(data.get('map_id'))
        if m is None:
            return
        before = list(getattr(m, 'explored_rooms', None) or [])
        merged = wall_rules.normalize_explored_rooms(
            before + list(data.get('rooms') or []))
        if merged == before:
            return
        m.explored_rooms = merged
        current = self.canvas.current_map
        if current is not None and current.id == m.id:
            self.canvas.reload_explored_rooms()

    def on_night_tint_changed(self, color: str, alpha: int):
        """Мастер сменил цвет/плотность ночи — отложенная отправка всем."""
        m = self.canvas.current_map
        if m is None:
            return
        self._pending_night_tint = (m.id, normalize_time_of_day(m.time_of_day),
                                    str(color), int(alpha))
        timer = getattr(self, "_night_send_timer", None)
        if timer is None:
            self._send_night_tint()
        else:
            timer.start()

    def _send_night_tint(self):
        """Отправить последний выбранный цвет ночи (одно сообщение)."""
        pending = getattr(self, "_pending_night_tint", None)
        self._pending_night_tint = None
        if not pending:
            return
        map_id, mode, color, alpha = pending
        self.network_client.send_message(MessageType.UPDATE_TIME, {
            'map_id': map_id,
            'time_of_day': mode,
            'night_color': color,
            'night_alpha': alpha,
        })

    def on_lights_changed(self, lights: list):
        """Мастер поставил, передвинул или убрал свет — сообщить серверу.

        Здесь же правка попадает в историю: холст отдаёт снимок «до»
        вместе с сигналом (``take_lights_before``), поэтому Ctrl+Z
        возвращает источник целиком — с радиусом, флажками и именем.
        """
        m = self.canvas.current_map
        if m is None:
            return
        map_id = m.id
        before = self.canvas.take_lights_before()
        after = [dict(light) for light in lights]
        self.network_client.send_message(MessageType.UPDATE_LIGHTS, {
            'map_id': map_id,
            'lights': after,
        })
        # Пустой список — тоже снимок: первый источник на голой карте так
        # же отменяем, как и десятый, поэтому сравниваем с None, а не с bool.
        if before is None or before == after:
            return
        self._remember_lights_edit(map_id, before, after)

    def _remember_lights_edit(self, map_id: str, before: list, after: list):
        """Правка света в историю: откат и повтор — тот же полный список."""
        label, merge_key = self._lights_edit_label(before, after)

        def apply(lights):
            # Сначала у себя: мастер должен увидеть отмену сразу, а сервер
            # разошлёт тот же список остальным.
            current = self.canvas.current_map
            if current is not None and current.id == map_id:
                self.canvas.set_map_lights([dict(light) for light in lights],
                                           notify=False)
                # Список и поля вкладки живут отдельно от холста: без
                # обновления Ctrl+Z менял карту, а в списке оставались
                # удалённые источники — и отмена выглядела сломанной.
                self.refresh_light_list()
                self._sync_lighting_ui()
            self.network_client.send_message(MessageType.UPDATE_LIGHTS, {
                'map_id': map_id,
                'lights': [dict(light) for light in lights],
            })

        def undo():
            apply(before)

        def redo():
            apply(after)

        self._push_history(label, undo, redo, kind='lights',
                           merge_key=merge_key)

    @staticmethod
    def _lights_edit_label(before: list, after: list):
        """Что именно сделали со светом: подпись и ключ склейки.

        Ползунки радиуса и мягкости приходят десятком событий подряд, и
        ключ склейки оставляет на весь жест одну запись истории: иначе
        Ctrl+Z отменял бы по одному шагу ползунка.
        """
        old = {light['id']: light for light in before}
        new = {light['id']: light for light in after}
        added = [light for lid, light in new.items() if lid not in old]
        if added:
            return f"добавление света «{added[-1]['name']}»", None
        removed = [light for lid, light in old.items() if lid not in new]
        if removed:
            return f"удаление света «{removed[-1]['name']}»", None
        for lid, light in new.items():
            was = old.get(lid)
            if was is None:
                continue
            name = light['name']
            if (float(was['x']), float(was['y'])) != (float(light['x']),
                                                      float(light['y'])):
                return f"перемещение света «{name}»", None
            if was['radius_ft'] != light['radius_ft']:
                return (f"радиус света «{name}»",
                        f'light-radius:{lid}')
            if was['intensity'] != light['intensity']:
                return (f"мягкость света «{name}»",
                        f'light-intensity:{lid}')
            if bool(was['enabled']) != bool(light['enabled']):
                state = 'выключение' if not light['enabled'] else 'включение'
                return f"{state} света «{name}»", None
            if bool(was['night_only']) != bool(light['night_only']):
                state = ('только ночью' if light['night_only']
                         else 'и днём')
                return f"свет «{name}» {state}", None
            if bool(was['marker_hidden']) != bool(light['marker_hidden']):
                state = 'скрытие' if light['marker_hidden'] else 'показ'
                return f"{state} иконки света «{name}»", None
            if bool(was['locked']) != bool(light['locked']):
                state = 'закрепление' if light['locked'] else 'освобождение'
                return f"{state} света «{name}»", None
        return 'правка света', None

    def on_update_lights(self, data: dict):
        """Пришёл свет карты с сервера: запомнить в модели и перерисовать."""
        m = self.game_state.maps.get(data.get('map_id'))
        if m is None:
            return
        m.lights = lighting_rules.normalize_lights(data.get('lights'))
        self.canvas.update()
        self._sync_lighting_ui()

    def on_walls_changed(self, walls: list, openings: list):
        """Мастер расставил стены — сообщить серверу весь список.

        Здесь же правка попадает в историю: холст отдаёт снимок «до» вместе
        с сигналом (``take_walls_before``), поэтому Ctrl+Z возвращает стену
        целиком — со всеми точками, окнами и дверями, а не один отрезок.
        """
        m = self.canvas.current_map
        if m is None:
            return
        map_id = m.id
        before = self.canvas.take_walls_before()
        self.network_client.send_message(MessageType.UPDATE_WALLS, {
            'map_id': map_id,
            'walls': [dict(wall) for wall in walls],
            'openings': [dict(item) for item in openings],
        })
        if not before:
            return
        before_walls, before_openings = before
        # Нажал на точку и отпустил без движения — править нечего.
        if before_walls == walls and before_openings == openings:
            return
        self._remember_walls_edit(map_id, before_walls, before_openings,
                                  walls, openings)

    def _remember_walls_edit(self, map_id: str, before_walls, before_openings,
                             after_walls, after_openings):
        """Правка стен в историю: откат и повтор — тот же полный список."""
        label = self._walls_edit_label(before_walls, before_openings,
                                       after_walls, after_openings)

        def apply(walls, openings):
            # Сначала у себя: мастер должен увидеть отмену сразу, а сервер
            # разошлёт тот же список остальным.
            current = self.canvas.current_map
            if current is not None and current.id == map_id:
                self.canvas.set_map_walls(walls, openings, notify=False)
            self.network_client.send_message(MessageType.UPDATE_WALLS, {
                'map_id': map_id,
                'walls': [dict(wall) for wall in walls],
                'openings': [dict(item) for item in openings],
            })

        def undo():
            apply(before_walls, before_openings)

        def redo():
            apply(after_walls, after_openings)

        self._push_history(label, undo, redo, kind='walls')

    @staticmethod
    def _walls_edit_label(before_walls, before_openings, after_walls,
                          after_openings) -> str:
        """Что именно сделали со стенами — для подписи «Отменить: …»."""
        if len(after_walls) > len(before_walls):
            return 'постройка стены'
        if len(after_walls) < len(before_walls):
            return 'удаление стены'
        before_points = sum(len(wall.get('points') or []) for wall in before_walls)
        after_points = sum(len(wall.get('points') or []) for wall in after_walls)
        if after_points < before_points:
            return 'удаление точки'
        if after_points > before_points:
            return 'вставка точки'
        known = {item.get('id') for item in before_openings}
        added = [item for item in after_openings
                 if item.get('id') not in known]
        kinds = {item.get('kind') for item in added}
        if kinds == {wall_rules.WINDOW}:
            return 'окно в стене'
        if kinds == {wall_rules.DOOR}:
            return 'дверь в стене'
        if added:
            return 'проём в стене'
        if len(after_openings) < len(before_openings):
            return 'удаление проёма'
        return 'правка стены'

    def on_update_walls(self, data: dict):
        """Стены с сервера: запомнить в модели и перерисовать стол.

        Стены не правит никто, кроме мастера, а сервер присылает полный
        список; поэтому на клиенте это просто замена, без слияния.
        """
        m = self.game_state.maps.get(data.get('map_id'))
        if m is None:
            return
        m.walls = wall_rules.normalize_walls(data.get('walls'))
        m.openings = wall_rules.normalize_openings(data.get('openings'),
                                                   m.walls)
        # Стены приехали готовыми списками, мимо set_map_walls: кэш холста
        # (стены, куски, многоугольники света) без этого остался бы прежним.
        self.canvas.invalidate_walls_cache()
        self.canvas.update()

    def on_door_toggled(self, opening_id: str, is_open: bool):
        """Мастер толкнул дверь: одно сообщение, эхо прилетит всем."""
        m = self.canvas.current_map
        if m is None:
            return
        self.network_client.send_message(MessageType.TOGGLE_DOOR, {
            'map_id': m.id,
            'opening_id': opening_id,
            'open': bool(is_open),
        })

    def on_door_lock_requested(self, opening_id: str, locked: bool):
        """Мастер запер или отпер дверь — шлём на сервер, звук по эху."""
        m = self.canvas.current_map
        if m is None:
            return
        self.network_client.send_message(MessageType.SET_DOOR_LOCK, {
            'map_id': m.id,
            'opening_id': opening_id,
            'locked': bool(locked),
        })

    def on_door_lock(self, data: dict):
        """Замок двери (эхо): запомнить в модели, обновить стол и звук."""
        m = self.game_state.maps.get(data.get('map_id'))
        if m is None:
            return
        opening = wall_rules.set_door_lock(m, data.get('opening_id'),
                                           data.get('locked'))
        if opening is None:
            return
        # Замок захлопывает дверь — и она должна закрыться на глазах.
        self.canvas.animate_door(opening.get('id'),
                                 bool(opening.get('open')))
        self.canvas.update()
        # Запирание захлопывает дверь, а звук играет сам холст — в момент
        # начала хода створки, поэтому здесь второй раз не хлопаем.

    def on_vision_requested(self, mode: str):
        """Мастер выбрал режим зрения — это настройка стола, а не клиента."""
        self.network_client.send_message(MessageType.SET_VISION,
                                         {'vision_mode': str(mode)})

    def on_vision(self, data: dict):
        """Эхо зрения: играем по режиму мастера.

        Строку в чат пишем только при настоящей смене режима: мастер
        видит свой выбор в переключателе, а игроку нужно объяснить, почему
        стол вдруг стал ч/б — иначе настройка выглядит как «ничего не
        произошло».
        """
        mode = ("cone" if str(data.get('vision_mode')).casefold() == "cone"
                else "around")
        changed = mode != self.vision_mode
        self.set_vision_mode(mode, notify=False)
        if not changed:
            return
        if mode == "cone":
            self.append_system("Зрение игроков: «поле зрения» — цветным видно "
                               "только перед токеном")
        else:
            self.append_system("Зрение игроков: «вокруг токена» — цветным "
                               "видно всё в радиусе зрения")

    def on_snap_requested(self, enabled: bool):
        """Мастер включил привязку к клеткам — это настройка стола."""
        self.network_client.send_message(MessageType.SET_SNAP,
                                         {'snap_tokens': bool(enabled)})

    def on_snap(self, data: dict):
        """Эхо привязки: теперь у всех токены ходят по клеткам."""
        self.canvas.set_snap(bool(data.get('snap_tokens')), notify=False)

    def on_door_state(self, data: dict):
        """Дверь открыли/закрыли (эхо): створка идёт, скрип даёт холст."""
        m = self.game_state.maps.get(data.get('map_id'))
        if m is None:
            return
        opening = wall_rules.toggle_door(m, data.get('opening_id'),
                                         data.get('open'))
        if opening is not None:
            # Створка идёт к новому положению; у того, кто дверь толкнул,
            # ход уже запущен локально и второй раз не начнётся.
            self.canvas.animate_door(opening.get('id'),
                                     bool(opening.get('open')))
        self.canvas.update()
        # Звук уже сыграл холст, когда створка тронулась с места: на эхо его
        # дублировать нельзя — выходило два скрипа подряд.

    def _play_door_sound(self, is_open: bool):
        """Скрип двери: свой звук на открытие и на закрытие."""
        sound_id = self._door_open_sound_id if is_open \
            else self._door_close_sound_id
        if sound_id and sound_id in self.audio.sounds:
            self.audio.play_sound(sound_id)

    def on_update_time(self, data: dict):
        """Освещение карты пришло с сервера: обновить модель и холст.

        map_id чужой карты тоже запоминаем: когда мастер её откроет, ночь
        уже будет на месте.
        """
        m = self.game_state.maps.get(data.get('map_id'))
        if m is None:
            return
        m.time_of_day = normalize_time_of_day(data.get('time_of_day'))
        if 'night_color' in data:
            m.night_color = lighting_rules.normalize_night_color(
                data.get('night_color'))
        if 'night_alpha' in data:
            m.night_alpha = lighting_rules.normalize_night_alpha(
                data.get('night_alpha'))
        # Плотность тумана едет тем же сообщением: это свойство карты.
        if 'fog_room_alpha' in data:
            m.fog_room_alpha = lighting_rules.normalize_fog_room_alpha(
                data.get('fog_room_alpha'))
        if 'fog_world_alpha' in data:
            m.fog_world_alpha = lighting_rules.normalize_fog_world_alpha(
                data.get('fog_world_alpha'))
        # Вид тумана — тоже свойство карты: ч/б, сепия, цветной или свой.
        if 'fog_style' in data:
            m.fog_style = lighting_rules.normalize_fog_style(
                data.get('fog_style'))
        if 'fog_color' in data:
            m.fog_color = lighting_rules.normalize_fog_color(
                data.get('fog_color'))
        self.canvas.update()
        self._sync_lighting_ui()

    # --- Объекты карты ---
    def _current_map_objects(self):
        """Объекты открытой карты: верхний слой, внутри — от верхнего z."""
        map_id = self.canvas.current_map.id if self.canvas.current_map else None
        items = [o for o in self.game_state.map_objects.values()
                 if map_id is None or o.map_id in (None, map_id)]
        return sorted(items, key=lambda o: (not o.above_tokens,
                                            -int(getattr(o, 'z', 0) or 0),
                                            o.name.lower(), o.id))

    def refresh_object_rows(self):
        """Перестроить список «Объекты» с глазками и кнопками настроек."""
        if not hasattr(self, "objects_list"):
            return
        for i in range(self.objects_list.count()):
            self.objects_list.setItemWidget(self.objects_list.item(i), None)
        self.objects_list.clear()
        self.object_rows = []
        self.object_eye_buttons = {}
        self.object_lock_buttons = {}
        self.object_row_labels = {}
        for obj in self._current_map_objects():
            item = QListWidgetItem()
            self.objects_list.addItem(item)
            self.object_rows.append((obj.id, obj.name))
            self._attach_object_row(obj, item)

    def _eye_icon(self, visible: bool) -> QIcon:
        """Глазок строки: нарисованный, а не эмодзи — тот обрезался кнопкой."""
        return tool_icon("eye" if visible else "eye_off",
                         getattr(self, "current_theme", "dark"))

    def _lock_icon(self, locked: bool) -> QIcon:
        """Замок строки в двух состояниях: закрыт и открыт."""
        return tool_icon("lock" if locked else "unlock",
                         getattr(self, "current_theme", "dark"))

    def _attach_object_row(self, obj, item: QListWidgetItem):
        """Строка объекта: глазок, имя, замок и шестерёнка настроек."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(2, 0, 4, 0)
        layout.setSpacing(6)

        eye = QToolButton(row)
        eye.setObjectName("map_object_eye_btn")
        eye.setAutoRaise(True)
        eye.setFixedSize(28, 24)
        eye.setIconSize(QSize(20, 20))
        eye.setCursor(Qt.PointingHandCursor)
        eye.setText("👁" if obj.visible else "🙈")
        eye.setIcon(self._eye_icon(obj.visible))
        eye.setToolButtonStyle(Qt.ToolButtonIconOnly)
        eye.setToolTip("Скрыть объект" if obj.visible else "Показать объект")
        eye.clicked.connect(
            lambda _, oid=obj.id: self.set_map_object_visible(
                oid, not bool((self.game_state.map_objects.get(oid)
                               or obj).visible)))
        eye.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        label = QLabel(obj.name)
        label.setTextFormat(Qt.PlainText)
        layer = "выше токенов" if obj.above_tokens else "ниже токенов"
        extra = " · крыша" if obj.roof else ""
        label.setToolTip(f"{obj.name} — {layer}{extra}")
        label.setContentsMargins(0, 0, 0, 0)
        label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        lock = QToolButton(row)
        lock.setObjectName("map_object_lock_btn")
        lock.setAutoRaise(True)
        lock.setFixedSize(28, 24)
        lock.setIconSize(QSize(20, 20))
        lock.setCursor(Qt.PointingHandCursor)
        lock.setText("🔒" if obj.locked else "🔓")
        lock.setIcon(self._lock_icon(obj.locked))
        lock.setToolButtonStyle(Qt.ToolButtonIconOnly)
        lock.setToolTip("Разблокировать объект" if obj.locked
                        else "Заблокировать: объект нельзя двигать и растягивать")
        lock.clicked.connect(
            lambda _, oid=obj.id: self.apply_map_object_patch(
                oid, {"locked": not bool(
                    (self.game_state.map_objects.get(oid) or obj).locked)}))
        lock.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        gear = QToolButton(row)
        gear.setObjectName("map_object_gear_btn")
        gear.setAutoRaise(True)
        gear.setFixedSize(28, 24)
        gear.setCursor(Qt.PointingHandCursor)
        gear.setText("⚙")
        gear.setToolTip("Настройки объекта: слой, прозрачность, крыша, замок")
        gear.clicked.connect(
            lambda _, oid=obj.id: self.open_object_settings(oid))
        gear.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        layout.addWidget(eye, 0, Qt.AlignVCenter)
        layout.addWidget(label, 1, Qt.AlignVCenter)
        layout.addWidget(lock, 0, Qt.AlignVCenter)
        layout.addWidget(gear, 0, Qt.AlignVCenter)

        item.setText("")
        self.objects_list.set_row_widget(item, row)
        self.object_eye_buttons[obj.id] = eye
        self.object_lock_buttons[obj.id] = lock
        self.object_row_labels[obj.id] = label

    def on_object_list_clicked(self, item):
        row = self.objects_list.row(item)
        if 0 <= row < len(self.object_rows):
            self.canvas.select_object(self.object_rows[row][0])

    def on_object_list_activated(self, item):
        row = self.objects_list.row(item)
        if 0 <= row < len(self.object_rows):
            self.open_object_settings(self.object_rows[row][0])

    def add_map_object_dialog(self):
        """Загрузить PNG как объект карты: стрелка/крыша/декор."""
        if self.player_role != Role.DM:
            return
        if self.canvas.current_map is None:
            QMessageBox.information(self, "Объект",
                                    "Сначала добавьте и покажите карту")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Выбрать PNG-объект", "",
            "Изображения (*.png *.webp *.gif *.jpg *.jpeg *.bmp)")
        if not path:
            return
        pix = QPixmap(path)
        if pix.isNull():
            QMessageBox.warning(self, "Объект", "Не удалось прочитать изображение")
            return
        asset_ref = asset_utils.local_asset_ref(path)
        if not asset_ref:
            QMessageBox.warning(self, "Объект", "Не удалось прочитать файл объекта")
            return

        # По умолчанию — не больше трёх клеток по длинной стороне: крышу
        # или дерево потом тянут за углы, а гигантский PNG закрыл бы карту.
        cell = self.canvas.grid_cell_world or 50.0
        max_side = max(1.0, cell * 3.0)
        scale = min(1.0, max_side / max(pix.width(), pix.height()))
        width = max(20.0, pix.width() * scale)
        height = max(20.0, pix.height() * scale)
        cx, cy = self.canvas.view_center_world()
        layer = [o for o in self._current_map_objects() if o.above_tokens]
        top_z = max([int(getattr(o, 'z', 0) or 0) for o in layer] or [0])
        obj = MapObject(
            id=generate_id(), map_id=self.canvas.current_map.id,
            name=os.path.splitext(os.path.basename(path))[0][:120] or "Объект",
            image_path=path, x=cx - width / 2.0, y=cy - height / 2.0,
            width=width, height=height, z=top_z + 1,
        )
        self.local_assets[obj.id] = path
        payload = obj.to_dict()
        payload['asset_upload'] = asset_ref
        self.network_client.send_message(MessageType.ADD_MAP_OBJECT, payload)
        self.remember_map_object_added(obj, payload)

    def set_map_object_visible(self, object_id: str, visible: bool):
        self.apply_map_object_patch(object_id, {'visible': bool(visible)})

    def open_selected_object_settings(self):
        row = self.objects_list.currentRow()
        if 0 <= row < len(self.object_rows):
            self.open_object_settings(self.object_rows[row][0])

    def remove_object_row(self, row: int):
        """ПКМ → «Удалить» или Delete в списке объектов."""
        if 0 <= row < len(self.object_rows):
            self.objects_list.setCurrentRow(row)
            self.remove_selected_map_object()

    def remove_selected_map_object(self):
        row = self.objects_list.currentRow()
        if not (0 <= row < len(self.object_rows)):
            return
        object_id = self.object_rows[row][0]
        obj = self.game_state.map_objects.get(object_id)
        if obj is None:
            return
        answer = QMessageBox.question(
            self, "Удалить объект",
            f"Удалить объект «{obj.name}» с карты?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self.remember_map_object_removed(obj)
        self.canvas.remove_object(object_id)
        self.game_state.map_objects.pop(object_id, None)
        self.refresh_object_rows()
        self.network_client.send_message(MessageType.REMOVE_MAP_OBJECT,
                                         {'object_id': object_id})

    def _object_undo_payload(self, obj) -> dict:
        """Полный payload для возврата объекта: с картинкой, если она локальная."""
        payload = obj.to_dict()
        path = self.local_assets.get(obj.id) or obj.image_path
        ref = asset_utils.local_asset_ref(path) if path else None
        if ref:
            payload['asset_upload'] = ref
        return payload

    def remember_map_object_added(self, obj, payload: dict):
        """Объект добавлен: откат — удалить, повтор — добавить снова."""
        object_id = obj.id
        name = obj.name
        redo_payload = dict(payload)

        def undo():
            self.network_client.send_message(MessageType.REMOVE_MAP_OBJECT,
                                             {'object_id': object_id})

        def redo():
            self.network_client.send_message(MessageType.ADD_MAP_OBJECT,
                                             dict(redo_payload))

        self._push_history(f'добавление объекта «{name}»', undo, redo,
                           kind='object')

    def remember_map_object_removed(self, obj):
        """Объект удалён: возвращаем его целиком вместе с картинкой."""
        payload = self._object_undo_payload(obj)
        object_id = obj.id
        name = obj.name

        def undo():
            self.network_client.send_message(MessageType.ADD_MAP_OBJECT,
                                             dict(payload))

        def redo():
            self.network_client.send_message(MessageType.REMOVE_MAP_OBJECT,
                                             {'object_id': object_id})

        self._push_history(f'удаление объекта «{name}»', undo, redo,
                           kind='object')

    def remember_object_patch(self, obj, before: dict, patch: dict):
        """Изменение свойств объекта: слой, прозрачность, поворот, z, замок."""
        object_id = obj.id
        name = obj.name
        after = {k: patch[k] for k in patch if k in before}
        if not after:
            return
        undo_data = {'object_id': object_id, **before}
        redo_data = {'object_id': object_id, **after}

        def undo():
            self.network_client.send_message(MessageType.UPDATE_MAP_OBJECT,
                                             undo_data)

        def redo():
            self.network_client.send_message(MessageType.UPDATE_MAP_OBJECT,
                                             redo_data)

        self._push_history(f'изменение объекта «{name}»', undo, redo,
                           kind='object',
                           merge_key=f'object-patch:{object_id}:{",".join(sorted(after))}')

    def apply_map_object_patch(self, object_id: str, patch: dict,
                               remember: bool = True):
        """Изменить объект у себя сразу и разослать изменение всем."""
        obj = (self.game_state.map_objects.get(object_id)
               or self.canvas.objects.get(object_id))
        if obj is None:
            return
        before = {k: getattr(obj, k) for k in patch if hasattr(obj, k)}
        merged = obj.to_dict()
        merged.update(patch)
        updated = MapObject.from_dict(merged)
        self.game_state.map_objects[object_id] = updated
        self.canvas.add_object(updated,
                               self.local_assets.get(object_id,
                                                     updated.image_path))
        if updated.locked and self.canvas.selected_object_id == object_id:
            self.canvas.selected_object_id = None
        payload = {'object_id': object_id}
        payload.update(patch)
        self.network_client.send_message(MessageType.UPDATE_MAP_OBJECT, payload)
        if remember:
            self.remember_object_patch(obj, before, patch)
        self.refresh_object_rows()
        self.canvas.update()

    def on_object_geometry_changed(self, data: dict):
        """ДМ подвинул, растянул или повернул объект — шлём и пишем в историю."""
        if self.player_role != Role.DM:
            return
        before = data.pop('before', None)
        self.network_client.send_message(MessageType.UPDATE_MAP_OBJECT, data)
        if not before:
            return
        object_id = data.get('object_id')
        obj = (self.game_state.map_objects.get(object_id)
               or self.canvas.objects.get(object_id))
        name = obj.name if obj else 'объект'
        after = {k: data[k] for k in ('x', 'y', 'width', 'height',
                                      'rotation') if k in data}
        undo_data = {'object_id': object_id, **{k: before[k] for k in after
                                                if k in before}}
        redo_data = {'object_id': object_id, **after}
        if undo_data == redo_data:
            return

        def undo():
            self.network_client.send_message(MessageType.UPDATE_MAP_OBJECT,
                                             undo_data)

        def redo():
            self.network_client.send_message(MessageType.UPDATE_MAP_OBJECT,
                                             redo_data)

        self._push_history(f'изменение объекта «{name}»', undo, redo,
                           kind='object',
                           merge_key=f'object-geometry:{object_id}')

    def open_object_settings(self, object_id: str):
        """Настройки одного объекта: слой, прозрачность, глазок, крыша, замок."""
        obj = (self.game_state.map_objects.get(object_id)
               or self.canvas.objects.get(object_id))
        if obj is None:
            return
        dlg = QDialog(self)
        dlg.setObjectName("object_settings_dialog")
        dlg.setWindowTitle(f"Объект — {obj.name}")
        dlg.setWindowIcon(app_icon())
        dlg.setMinimumWidth(340)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        head = QLabel(obj.name)
        head.setObjectName("start_title")
        lay.addWidget(head)

        layer_title = QLabel("Слой относительно токенов")
        lay.addWidget(layer_title)
        layer_row = QHBoxLayout()
        self.object_above_radio = QRadioButton("Выше токенов")
        self.object_below_radio = QRadioButton("Ниже токенов")
        layer_row.addWidget(self.object_above_radio)
        layer_row.addWidget(self.object_below_radio)
        layer_row.addStretch(1)
        lay.addLayout(layer_row)
        self.object_above_radio.setChecked(obj.above_tokens)
        self.object_below_radio.setChecked(not obj.above_tokens)

        def set_above(checked):
            if checked:
                self.apply_map_object_patch(object_id, {'above_tokens': True})

        def set_below(checked):
            if checked:
                self.apply_map_object_patch(object_id, {'above_tokens': False})

        self.object_above_radio.toggled.connect(set_above)
        self.object_below_radio.toggled.connect(set_below)

        row = QHBoxLayout()
        row.addWidget(QLabel("Прозрачность"))
        self.object_opacity_slider = QSlider(Qt.Horizontal)
        self.object_opacity_slider.setRange(10, 100)
        self.object_opacity_slider.setValue(int(obj.opacity))
        row.addWidget(self.object_opacity_slider, 1)
        self.object_opacity_label = QLabel(f"{int(obj.opacity)}%")
        self.object_opacity_label.setFixedWidth(40)
        row.addWidget(self.object_opacity_label)
        lay.addLayout(row)

        def opacity_changed(value):
            self.object_opacity_label.setText(f"{value}%")
            self.apply_map_object_patch(object_id, {'opacity': int(value)})

        self.object_opacity_slider.valueChanged.connect(opacity_changed)

        rot_row = QHBoxLayout()
        rot_row.addWidget(QLabel("Поворот"))
        self.object_rotation_slider = QSlider(Qt.Horizontal)
        self.object_rotation_slider.setRange(0, 359)
        initial_rotation = int(round(obj.rotation or 0.0)) % 360
        self.object_rotation_slider.setValue(initial_rotation)
        self.object_rotation_slider.setToolTip(
            "Поворот вокруг центра. На холсте тяните за ручку, Shift — шаг 15°")
        rot_row.addWidget(self.object_rotation_slider, 1)
        # Угол можно не только тянуть, но и ввести числом с клавиатуры.
        self.object_rotation_spin = QSpinBox()
        self.object_rotation_spin.setObjectName("object_rotation_spin")
        self.object_rotation_spin.setRange(0, 359)
        self.object_rotation_spin.setWrapping(True)
        # Применяем число после Enter/ухода из поля, а не на каждой цифре:
        # иначе ввод «123» слал бы 1, 12 и 123.
        self.object_rotation_spin.setKeyboardTracking(False)
        self.object_rotation_spin.setSuffix("°")
        self.object_rotation_spin.setAlignment(Qt.AlignCenter)
        self.object_rotation_spin.setFixedWidth(64)
        self.object_rotation_spin.setValue(initial_rotation)
        self.object_rotation_spin.setToolTip(
            "Угол поворота: введите число и нажмите Enter")
        rot_row.addWidget(self.object_rotation_spin)
        lay.addLayout(rot_row)

        def rotation_changed(value):
            self.apply_map_object_patch(object_id, {'rotation': float(value)})

        # Ползунок и число синхронны; применяем только одно событие, иначе
        # каждое движение слало бы два одинаковых UPDATE_MAP_OBJECT.
        self.object_rotation_slider.valueChanged.connect(
            self.object_rotation_spin.setValue)
        self.object_rotation_spin.valueChanged.connect(
            self.object_rotation_slider.setValue)
        self.object_rotation_spin.valueChanged.connect(rotation_changed)

        z_title = QLabel("Порядок внутри слоя")
        lay.addWidget(z_title)
        z_row = QHBoxLayout()
        front = QPushButton("На передний план")
        front.setObjectName("object_front_btn")
        front.clicked.connect(
            lambda: self.raise_map_object(object_id, True))
        z_row.addWidget(front)
        back = QPushButton("На задний план")
        back.setObjectName("object_back_btn")
        back.clicked.connect(
            lambda: self.raise_map_object(object_id, False))
        z_row.addWidget(back)
        lay.addLayout(z_row)

        self.object_visible_checkbox = QCheckBox("Видим для всех (глазок)")
        self.object_visible_checkbox.setChecked(bool(obj.visible))
        self.object_visible_checkbox.toggled.connect(
            lambda checked: self.apply_map_object_patch(
                object_id, {'visible': bool(checked)}))
        lay.addWidget(self.object_visible_checkbox)

        self.object_roof_checkbox = QCheckBox("Крыша")
        self.object_roof_checkbox.setToolTip(
            "Крыша тает, пока под ней стоит игрок, и возвращается сама")
        self.object_roof_checkbox.setChecked(bool(obj.roof))
        self.object_roof_checkbox.toggled.connect(
            lambda checked: self.apply_map_object_patch(
                object_id, {'roof': bool(checked)}))
        lay.addWidget(self.object_roof_checkbox)

        self.object_lock_checkbox = QCheckBox("Заблокировать (замок)")
        self.object_lock_checkbox.setToolTip(
            "Объект не выбрать и не сдвинуть мышью. Настройки — в панели")
        self.object_lock_checkbox.setChecked(bool(obj.locked))
        self.object_lock_checkbox.toggled.connect(
            lambda checked: self.apply_map_object_patch(
                object_id, {'locked': bool(checked)}))
        lay.addWidget(self.object_lock_checkbox)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        lay.addWidget(buttons)
        self.object_settings_dialog = dlg
        dlg.exec_()

    def raise_map_object(self, object_id: str, to_front: bool = True):
        """Переместить объект на передний/задний план внутри своего слоя."""
        obj = (self.game_state.map_objects.get(object_id)
               or self.canvas.objects.get(object_id))
        if obj is None:
            return
        layer = [o for o in self._current_map_objects()
                 if bool(o.above_tokens) == bool(obj.above_tokens)]
        values = [int(getattr(o, 'z', 0) or 0) for o in layer] or [0]
        if to_front:
            new_z = max(values) + 1
        else:
            new_z = min(values) - 1
        self.apply_map_object_patch(object_id, {'z': int(new_z)})

    def on_add_map_object(self, data: dict):
        payload = {k: v for k, v in data.items()
                   if k not in ('asset_upload', 'asset_ref')}
        obj = MapObject.from_dict(payload)
        self.game_state.map_objects[obj.id] = obj
        self.canvas.add_object(obj, self.local_assets.get(obj.id, obj.image_path))
        if self.player_role == Role.DM:
            self.refresh_object_rows()

    def on_update_map_object(self, data: dict):
        payload = {k: v for k, v in data.items()
                   if k not in ('asset_upload', 'asset_ref')}
        obj = MapObject.from_dict(payload)
        self.game_state.map_objects[obj.id] = obj
        self.canvas.add_object(obj, self.local_assets.get(obj.id, obj.image_path))
        if self.player_role == Role.DM:
            self.refresh_object_rows()

    def on_remove_map_object(self, data: dict):
        object_id = data.get('object_id') or data.get('id')
        self.game_state.map_objects.pop(object_id, None)
        self.canvas.remove_object(object_id)
        self.local_assets.pop(object_id, None)
        if self.player_role == Role.DM:
            self.refresh_object_rows()

    def add_map_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Выбрать карту", "",
            "Карты (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.dd2vtt *.uvtt);;"
            "Изображения (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;"
            "Universal VTT (*.dd2vtt *.uvtt)")
        if not path:
            return
        self.send_map_file(path)

    def _import_dd2vtt(self, path: str):
        """Развернуть .dd2vtt: изображение достаём, сетку берём точную.

        Это главный плюс формата: шаг и смещение приходят из файла, и сетка
        совпадает с картой без подбора. Стены и двери сохраняем на будущее.
        """
        try:
            with open(path, 'rb') as f:
                raw = f.read()
            grid_new, image_bytes, walls, portals, _doc = grid_utils.parse_dd2vtt(raw)
        except Exception as exc:
            QMessageBox.warning(self, "Ошибка",
                                f"Не удалось прочитать .dd2vtt:\n{exc}")
            return None

        if not image_bytes:
            QMessageBox.warning(self, "Карта без картинки",
                                "В этом .dd2vtt нет изображения")
            return None

        stem = os.path.splitext(os.path.basename(path))[0]
        try:
            image_path = grid_utils.save_embedded_image(
                image_bytes, self.cache_dir, stem)
        except Exception as exc:
            QMessageBox.warning(self, "Карта не сохранилась",
                                f"Не получилось сохранить картинку:\n{exc}")
            return None
        # Имя карты берём из имени файла как есть: путь к кэшу мог быть очищен
        # от недопустимых символов, и по нему имя уже не восстановить.
        return image_path, grid_new, walls, portals, stem

    def send_map_file(self, path: str):
        """Отправить карту: файл передаётся целиком и показывается всем.

        Если это .dd2vtt — берём точную сетку из файла. Если обычная картинка —
        пробуем определить сетку автоматически (результат можно поправить).
        """
        grid_new = None
        extra = {}
        # Имя карты определяем ОДИН раз. Раньше для .dd2vtt имя бралось из
        # файла, а следующей строкой безусловно перезаписывалось именем
        # файла в кэше — там кириллица была заменена на подчёркивания,
        # поэтому карта называлась «______».
        name = os.path.splitext(os.path.basename(path))[0]
        if grid_utils.is_dd2vtt_path(path):
            imported = self._import_dd2vtt(path)
            if imported is None:
                return
            path, grid_new, walls, portals, map_name = imported
            extra = {'walls': walls, 'portals': portals}
            if map_name and map_name.strip():
                name = map_name
        # Экзотический формат (AVIF/HEIC/TIFF), переименованный в .jpg,
        # пересохраняем в PNG: иначе Qt не покажет карту, а сервер откажет
        # по сигнатуре. Обычные JPEG/PNG не трогаем.
        path = asset_utils.prepare_image_for_upload(path, self.cache_dir)
        asset_ref = asset_utils.local_asset_ref(path)
        if not asset_ref:
            QMessageBox.warning(self, "Ошибка",
                                "Не удалось прочитать файл карты (или он слишком большой).")
            return

        # Размеры считаем локально, чтобы карта встала по центру у всех
        pix = QPixmap(path)
        w = pix.width() or 2000
        h = pix.height() or 2000

        if grid_new is None:
            # .dd2vtt не было — пробуем оценить сетку по самой картинке.
            detected = grid_utils.detect_grid(path)
            if detected:
                grid_new = detected
                self.append_system(
                    f"Сетка определена автоматически: {detected.cols} × {detected.rows} клеток. "
                    "Если не совпала — задайте вручную или калибруйте (Ctrl+G).")

        map_data = MapData(id=generate_id(), name=name, image_path=path,
                           width=w, height=h,
                           img_width=float(pix.width() or w),
                           img_height=float(pix.height() or h))
        if grid_new is not None:
            map_data.grid_cols = int(grid_new.cols)
            map_data.grid_rows = int(grid_new.rows)
        # Стены и двери из файла — сразу в мировых координатах карты. В UVTT
        # они записаны в клетках, поэтому пересчёт идёт через размер клетки
        # стола: стена встаёт ровно на линию сетки, а не на пиксели картинки.
        imported_walls, imported_openings = [], []
        if extra.get('walls') or extra.get('portals'):
            cell_w, cell_h = map_data.grid_cell_world
            imported_walls, imported_openings = grid_utils.dd2vtt_geometry(
                extra.get('walls'), extra.get('portals'),
                cell_width=cell_w, cell_height=cell_h,
                pos_x=map_data.pos_x, pos_y=map_data.pos_y)
            if imported_walls:
                map_data.walls = imported_walls
                map_data.openings = imported_openings
        payload = map_data.to_dict()
        payload['asset_upload'] = asset_ref

        # Локально показываем сразу, не дожидаясь эха от сервера
        self.local_assets[map_data.id] = path
        self.game_state.maps[map_data.id] = map_data
        # Заставка стартует сразу при выборе файла, а не после того, как
        # сервер пришлёт CHANGE_MAP: иначе при большой карте всё время
        # передачи на холсте не было никакой обратной связи.
        if getattr(self.network_client, 'connected', False):
            self._map_upload_loading_id = map_data.id
            self.canvas.begin_map_loading(map_data.id)
        self.network_client.send_message(MessageType.ADD_MAP, payload)
        if imported_walls:
            self._reveal_imported_walls(
                len(imported_walls),
                sum(1 for item in imported_openings
                    if item.get("kind") == wall_rules.DOOR),
                sum(1 for item in imported_openings
                    if item.get("kind") == wall_rules.WINDOW))
        elif extra.get('walls'):
            self.append_system(
                "В .dd2vtt есть стены, но сетка карты неизвестна — "
                "стены не перенесены: задайте сетку и импортируйте снова")

    def _reveal_imported_walls(self, wall_count: int, doors: int = 0,
                               windows: int = 0):
        """Показать стены, приехавшие из .dd2vtt.

        Стены рисуются только при выбранном инструменте: без него мастер
        увидел бы одни двери и решил, что импорт не сработал. Поэтому после
        импорта включаем «Точки стены» — линии и вершины сразу на глазах, а
        повторный клик по кнопке или другой инструмент возвращают обычный
        курсор.
        """
        text = f"Из .dd2vtt: стен {wall_count}"
        # Двери и окна считаем отдельно: по файлу видно, как разобралась
        # настройка «Block light / Allow light», и ошибку импорта заметно сразу.
        if doors:
            text += f", дверей {doors}"
        if windows:
            text += f", окон {windows}"
        button = getattr(self, "wall_tool_buttons", {}).get("edit")
        if button is not None and not button.isChecked():
            button.setChecked(True)
            self._on_wall_tool("edit")
            text += " — включён инструмент «Точки стены», стены видны"
        elif button is None:
            text += " — стены появятся вместе с модулем освещения"
        self.append_system(text)

    def on_file_dropped(self, path: str):
        """Перетаскивание файлов в окно"""
        ext = os.path.splitext(path)[1].lower()
        if self.player_role != Role.DM:
            self.statusBar().showMessage("Только DM может менять карту и токены")
            return
        if ext in ('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp'):
            self.send_map_file(path)

    def change_map(self):
        """Показать карту, выделенную в списке (кнопка «Показать»).

        Сигнатура БЕЗ параметров — намеренно. QPushButton.clicked передаёт
        checked: bool, и если объявить здесь параметр, в него попадёт False.
        Дальше maps_list.row(False) падает с TypeError, а Qt глотает
        исключение внутри слота: кнопка молча ничего не делала.
        """
        # Списка карт у игрока нет вовсе — выходим, а не падаем.
        if self.player_role != Role.DM:
            return
        self._show_map_row(self.maps_list.currentRow())

    def change_map_from_item(self, item):
        """Показать карту двойным кликом по строке списка.

        Строку берём у самого элемента, а НЕ через currentRow(): одиночный
        клик до двойного уводит выделение на карту, лежащую на холсте, и по
        currentRow выбиралась бы нынешняя карта вместо новой. Именно из-за
        этого переключение срабатывало только со второго раза.
        """
        if item is None:
            return
        self._show_map_row(self.maps_list.row(item))

    def _show_map_row(self, row: int):
        """Отправить запрос на показ карты из строки списка."""
        if row < 0 or row >= len(self.map_rows):
            return
        map_id = self.map_rows[row][0]
        # Повторный выбор уже открытой карты ничего не меняет — не шлём лишнее.
        current = self.canvas.current_map
        if current is not None and map_id == current.id:
            return
        self.network_client.send_message(MessageType.CHANGE_MAP,
                                         {'map_id': map_id})

    # --- Синхронизация левой панели и холста ---
    def on_map_list_clicked(self, item):
        """Выбрали карту в левой панели -> включаем режим редактирования карты.

        Строку выставляем явно, не полагаясь на то, что список сделал это сам:
        так обработчик ведёт себя одинаково и при клике мышью, и при вызове
        из кода. Далее по этой строке работает кнопка «Показать».
        """
        row = self.maps_list.row(item)
        if row >= 0:
            self.maps_list.blockSignals(True)
            self.maps_list.setCurrentRow(row)
            self.maps_list.blockSignals(False)
        self.canvas.select_map()
        self.tokens_list.blockSignals(True)
        self.tokens_list.setCurrentRow(-1)
        self.tokens_list.blockSignals(False)

    def on_token_list_clicked(self, item):
        """Выбрали токен в левой панели -> подсвечиваем его на холсте."""
        row = self.tokens_list.row(item)
        if row < 0 or row >= len(self.token_rows):
            return
        token_id = self.token_rows[row][0]
        self.canvas.deselect_map()
        self.canvas.selected_token = token_id
        self.canvas.update()
        self._sync_token_size_control()

    def on_token_list_selection_changed(self):
        """В списке выделили строки (клик, Shift/Ctrl, рамка) -> холст.

        Холст — единственное место, где живёт выделение для Ctrl+C, Delete
        и группового перетаскивания. Раньше список умел выделять только
        одну строку, и копировать токены из левой вкладки было нельзя.
        """
        if self._syncing_token_selection:
            return
        rows = self.tokens_list.selected_rows()
        ids = [self.token_rows[row][0] for row in rows
               if 0 <= row < len(self.token_rows)]
        current = self.tokens_list.currentRow()
        current_id = (self.token_rows[current][0]
                      if 0 <= current < len(self.token_rows) else None)
        self._syncing_token_selection = True
        try:
            self.canvas.deselect_map()
            self.canvas.selected_tokens = set(ids)
            if current_id in ids:
                self.canvas.selected_token = current_id
            elif len(ids) == 1:
                self.canvas.selected_token = ids[0]
            else:
                self.canvas.selected_token = None
            self.canvas.update()
        finally:
            self._syncing_token_selection = False
        self._sync_token_size_control()

    def _restore_token_list_selection(self, ids=None):
        """Показать в списке выделение, которое сейчас на холсте.

        Пересборка строк и перестановка текущей строки очереди сбрасывают
        выделение списка; без этого рамка и Shift-выделение пропадали
        после каждого обновления состояния.
        """
        if not hasattr(self, "tokens_list"):
            return
        wanted = set(self.canvas.selected_token_ids() if ids is None
                     else ids)
        current_id = self.canvas.selected_token
        self._syncing_token_selection = True
        try:
            self.tokens_list.clearSelection()
            current_row = -1
            last_row = -1
            for row, (tid, _name) in enumerate(self.token_rows):
                if tid in wanted:
                    last_row = row
                if tid == current_id:
                    current_row = row
            anchor = current_row if current_row >= 0 else last_row
            if anchor >= 0:
                # setCurrentRow сбрасывает прежнее выделение — ставим его
                # первым, а потом довыделяем остальные строки.
                self.tokens_list.setCurrentRow(anchor)
            for row, (tid, _name) in enumerate(self.token_rows):
                if tid in wanted and row != anchor:
                    item = self.tokens_list.item(row)
                    if item is not None:
                        item.setSelected(True)
            if not wanted:
                self.tokens_list.setCurrentRow(-1)
        finally:
            self._syncing_token_selection = False

    def on_token_selected(self, token_id: str):
        """Токен выбран на холсте -> отражаем выбор в списке.

        Берём всё выделение холста, а не один id: рамка по нескольким
        токенам раньше подсвечивала в списке только последний, и мастеру
        было непонятно, кого он выделил на самом деле.
        """
        self._restore_token_list_selection()
        self._sync_token_size_control()
        # Подпись на плашке быстрой атаки зависит от выбранного токена:
        # его оружие и есть то, чем бьём.
        if self.canvas.quick_attack_mode:
            self._reflect_quick_attack_mode()

    def on_token_deselected(self):
        """С холста сняли выделение -> чистим список токенов."""
        self._syncing_token_selection = True
        try:
            self.tokens_list.clearSelection()
            self.tokens_list.setCurrentRow(-1)
        finally:
            self._syncing_token_selection = False
        self._sync_token_size_control()

    def on_map_selected(self):
        """Карта выделена на холсте — рамку с ручками рисует сам холст.

        Строку списка здесь НЕ трогаем. Раньше метод переставлял выделение на
        карту, лежащую на холсте, и выбор другой карты в списке тут же
        сбивался обратно на текущую — именно это мешало переключению.
        """

    def on_map_deselected(self):
        """С карты сняли выделение -> чистим список карт."""
        self.maps_list.blockSignals(True)
        self.maps_list.setCurrentRow(-1)
        self.maps_list.blockSignals(False)

    def remove_map(self):
        row = self.maps_list.currentRow()
        if row < 0 or row >= len(self.map_rows):
            return
        self._remove_map_with_history(self.map_rows[row][0])

    def remove_map_row(self, row: int):
        """ПКМ → «Удалить» или Delete в списке карт."""
        if 0 <= row < len(self.map_rows):
            self.maps_list.setCurrentRow(row)
            self._remove_map_with_history(self.map_rows[row][0])

    def _remove_map_with_history(self, map_id: str):
        """Удалить карту, запомнив её и токены для отмены.

        Сервер удаляет вместе с картой все её токены, поэтому их тоже
        сохраняем — иначе Ctrl+Z вернул бы пустую карту.
        """
        map_data = self.game_state.maps.get(map_id)
        orphan = ([t for t in self.game_state.tokens.values() if t.map_id == map_id]
                  if map_data else [])
        self.network_client.send_message(MessageType.REMOVE_MAP,
                                         {'map_id': map_id})
        if map_data is not None:
            self.remember_map_removed(map_data, orphan)

    def _on_section_collapsed(self, opened: bool):
        """Свернуть/развернуть левую панель, оставив рейку иконок.

        QSplitter не отдаёт место сам: при скрытии внутренней панели он
        сохраняет прежнюю ширину, и на карте остаётся пустая полоса. Поэтому
        ширину переставляем явно, а прежнюю запоминаем.
        """
        if not hasattr(self, "splitter"):
            return
        sizes = self.splitter.sizes()
        if not sizes:
            return
        if self.player_role != Role.DM:
            return          # у игрока рейки нет, панель не сворачивается
        panel = self.splitter.widget(0)
        if opened:
            width = getattr(self, "_left_panel_width", 300)
            # Потолок берём у самой панели: справочник с открытой карточкой
            # держит двойную ширину и не должен схлопываться при возврате.
            ceiling = (panel.maximumWidth() if panel is not None
                       else self.LEFT_PANEL_MAX_WIDTH)
            if panel is not None:
                panel.setMinimumWidth(180)
            self._set_panel_width(0, max(200, min(ceiling, width)))
        else:
            if sizes[0] > 80:
                self._left_panel_width = sizes[0]
            # Минимум панели (180) не давал сплиттеру отдать место: он
            # упирался в него и на карте оставалась пустая полоса. Снимаем
            # ограничение на время сворачивания — остаётся только рейка.
            if panel is not None:
                panel.setMinimumWidth(56)
            self._set_panel_width(0, 56)

    def _on_right_section_collapsed(self, opened: bool):
        """Свернуть/развернуть правую панель, оставив рейку иконок.

        Симметрично левой панели: при закрытии отдаём холсту всю ширину,
        оставляя только 56 px под иконки. Без этого QSplitter сохранял
        прежнюю ширину, и справа висела пустая полоса.
        """
        if not hasattr(self, "splitter"):
            return
        sizes = self.splitter.sizes()
        if len(sizes) < 3:
            return
        panel = self.splitter.widget(2)
        if opened:
            width = getattr(self, "_right_panel_width", 300)
            if panel is not None:
                panel.setMinimumWidth(190)
            self._set_panel_width(2, max(200, width))
        else:
            if sizes[2] > 80:
                self._right_panel_width = sizes[2]
            # Минимум панели (190) не даёт сплиттеру отдать место —
            # снимаем его на время сворачивания.
            if panel is not None:
                panel.setMinimumWidth(56)
            self._set_panel_width(2, 56)

    def _remember_panel_sizes(self, *_):
        """Запомнить ширину панелей после перетаскивания границы.

        Нужно, чтобы пользовательская ширина не сбрасывалась при смене темы
        или перестроении интерфейса.
        """
        self._panel_sizes = self.splitter.sizes()

    # ------------------------------------------------------------------
    # Перемотка музыки
    # ------------------------------------------------------------------
    @staticmethod
    def _format_time(seconds) -> str:
        """Секунды в вид 3:07"""
        try:
            seconds = max(0, int(seconds))
        except (TypeError, ValueError):
            return "0:00"
        return f"{seconds // 60}:{seconds % 60:02d}"

    def _on_seek_pressed(self):
        """Пока тянем ползунок, не перебиваем его позицией из плеера"""
        self.music_seek_active = True

    def on_music_seek(self):
        """Пользователь отпустил ползунок — перематываем трек"""
        self.music_seek_active = False
        duration = self.audio.get_duration()
        if duration <= 0:
            return
        target = duration * (self.music_seek.value() / 1000.0)
        if not self.audio.seek_music(target):
            # Формат не поддерживает перемотку — говорим об этом прямо
            self.append_system("Перемотка этого файла недоступна (нужен MP3 или OGG)")
        self.update_music_position()

    def update_music_position(self):
        """Обновить ползунок и подписи времени по текущей позиции"""
        duration = self.audio.get_duration()
        if duration > 0:
            self.music_seek.setEnabled(True)
            self.music_len_label.setText(self._format_time(duration))
        elif not self.audio.music_playing:
            self.music_seek.setEnabled(False)
        if self.music_seek_active:
            return
        pos = self.audio.get_position()
        self.music_pos_label.setText(self._format_time(pos))
        if duration > 0:
            self.music_seek.setValue(int(1000 * min(1.0, pos / duration)))

    def open_grid_settings(self):
        """Диалог сетки как в Dungeondraft: число клеток по ширине и высоте.

        Смещений нет: сетка идёт от края до края. Поле «размер клетки» — удобный
        способ задать сетку: число клеток пересчитывается из размера картинки.
        """
        if self.player_role != Role.DM:
            self.statusBar().showMessage("Сетку настраивает мастер")
            return
        m = self.canvas.current_map
        if not m:
            QMessageBox.information(self, "Сетка", "Сначала добавьте карту")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Настройки сетки")
        dlg.setMinimumWidth(640)
        outer = QVBoxLayout(dlg)
        form = QFormLayout()
        outer.addLayout(form)

        cols = QSpinBox(dlg)
        cols.setRange(1, 2000)
        cols.setValue(max(1, m.grid_cols or 10))
        cols.setToolTip("Клеток по ширине картинки")
        form.addRow("Клеток по ширине:", cols)

        rows = QSpinBox(dlg)
        rows.setRange(1, 2000)
        rows.setValue(max(1, m.grid_rows or 10))
        rows.setToolTip("Клеток по высоте картинки")
        form.addRow("Клеток по высоте:", rows)

        cell_px = QDoubleSpinBox(dlg)
        cell_px.setDecimals(2)
        cell_px.setRange(1.0, 20000.0)
        cell_px.setSingleStep(1.0)
        cell_px.setToolTip("Размер клетки в пикселях")
        if m.img_width and cols.value():
            cell_px.setValue(m.img_width / cols.value())
        form.addRow("Размер клетки (пикс.):", cell_px)

        hint = QLabel(
            f"Картинка: {int(m.img_width)} × {int(m.img_height)} пикс. "
            "Размер клетки и число клеток связаны: "
            "клетка = размер картинки / число клеток.", dlg)
        hint.setWordWrap(True)
        outer.addWidget(hint)

        # Синхронизация: меняем клетки — пересчитывается размер клетки, и наоборот.
        syncing = {"on": False}

        def from_count():
            if syncing["on"] or not m.img_width:
                return
            syncing["on"] = True
            cell_px.setValue(m.img_width / cols.value())
            syncing["on"] = False

        def from_size():
            if syncing["on"] or not m.img_width:
                return
            syncing["on"] = True
            cols.setValue(clamp_cells(m.img_width / cell_px.value()) or 1)
            rows.setValue(clamp_cells(m.img_height / cell_px.value()) or 1)
            syncing["on"] = False

        cols.valueChanged.connect(from_count)
        cell_px.valueChanged.connect(from_size)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        btn_ok = QPushButton("Применить", dlg)
        btn_cancel = QPushButton("Отмена", dlg)
        btn_dd = QPushButton("Импорт .dd2vtt…", dlg)
        btn_cal = QPushButton("Калибровать сетку", dlg)
        for b in (btn_ok, btn_cancel, btn_dd, btn_cal):
            b.setMinimumWidth(b.fontMetrics().horizontalAdvance(b.text()) + 32)
            b.setMinimumHeight(30)
        btn_row.addWidget(btn_cal)
        btn_row.addWidget(btn_dd)
        btn_row.addStretch(1)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_ok)
        outer.addLayout(btn_row)

        def on_dd():
            path, _ = QFileDialog.getOpenFileName(
                self, "Файл Universal VTT", "",
                "Universal VTT (*.dd2vtt *.uvtt *.df2vtt)")
            if not path:
                return
            try:
                with open(path, 'rb') as f:
                    raw = f.read()
                g_new, _img, _w, _p, _doc = grid_utils.parse_dd2vtt(raw)
            except Exception as exc:
                QMessageBox.warning(self, "Ошибка", f"Не удалось прочитать файл:\n{exc}")
                return
            cols.setValue(g_new.cols or cols.value())
            rows.setValue(g_new.rows or rows.value())
            self.append_system(f"Сетка из .dd2vtt: {g_new.cols} × {g_new.rows} клеток")

        def on_cal():
            dlg.accept()
            if self.canvas.begin_grid_calibration(10):
                self.statusBar().showMessage(
                    "Калибровка: щёлкните по двум линиям сетки на карте (10 клеток между ними)")

        btn_ok.clicked.connect(dlg.accept)
        btn_cancel.clicked.connect(dlg.reject)
        btn_dd.clicked.connect(on_dd)
        btn_cal.clicked.connect(on_cal)

        if dlg.exec_() != QDialog.Accepted:
            return
        self.canvas.set_grid(cols=cols.value(), rows=rows.value(), notify=False)
        self.send_grid_update()

    def toggle_grid(self):
        visible = not self.canvas.grid_visible
        # notify=False локально: отправку делает main_window, иначе две рассылки.
        self.canvas.set_grid(visible=visible, notify=False)
        if self.player_role == Role.DM and self.canvas.current_map:
            self.send_grid_update()

    def on_grid_changed(self, data: dict):
        """Холст изменил сетку (например, калибровка) — отправляем участникам."""
        if self.player_role == Role.DM and data.get('map_id'):
            self.network_client.send_message(MessageType.UPDATE_GRID, data)

    def start_grid_calibration(self):
        """Калибровка сетки по двум точкам на карте (Ctrl+G)."""
        if self.player_role != Role.DM:
            self.statusBar().showMessage("Сетку калибрует мастер")
            return
        if not self.canvas.current_map:
            QMessageBox.information(self, "Сетка", "Сначала добавьте карту")
            return
        if self.canvas.calibrating:
            self.canvas.cancel_grid_calibration()
            self.statusBar().showMessage("Калибровка отменена")
            return
        cells, ok = QInputDialog.getInt(
            self, "Калибровка сетки",
            "Сколько клеток между отметками?\nЧем больше, тем точнее",
            10, 1, 100, 1)
        if not ok:
            return
        if self.canvas.begin_grid_calibration(cells):
            self.statusBar().showMessage(
                f"Калибровка: кликните по двум линиям сетки ({cells} кл. между ними)")

    def send_grid_update(self):
        """Разослать сетку открытой карты: число клеток и видимость."""
        if self.player_role != Role.DM or not self.canvas.current_map:
            return
        g = self.canvas.grid
        self.network_client.send_message(MessageType.UPDATE_GRID, {
            'map_id': self.canvas.current_map.id,
            'grid_cols': g.cols,
            'grid_rows': g.rows,
            'grid_visible': g.visible,
        })

    def add_token_dialog(self, wx: float, wy: float, kind: str = "actor"):
        """Добавить токен: картинка, затем подгонка под рамку.

        Имя, рамка, увеличение и сдвиг портрета задаются в одном окне:
        мастер сразу видит токен таким, каким он ляжет на карту, а не
        выбирает картинку вслепую и не правит её потом.
        """
        path, _ = QFileDialog.getOpenFileName(
            self, "Выбрать изображение токена", "",
            "Изображения (*.png *.jpg *.jpeg *.bmp *.webp *.gif)")
        if not path:
            return

        asset_ref = asset_utils.local_asset_ref(path)
        if not asset_ref:
            QMessageBox.warning(self, "Токен не открылся",
                                "Не получилось прочитать файл токена")
            return

        default = os.path.splitext(os.path.basename(path))[0]
        dialog = TokenPortraitDialog(self, path, default)
        if dialog.exec_() != QDialog.Accepted:
            return
        name = dialog.token_name()
        if not name:
            return
        portrait_dx, portrait_dy = dialog.offsets()

        # map_id — открытая карта: токен появится только на ней и не полезет
        # на остальные карты.
        current_map_id = self.canvas.current_map.id if self.canvas.current_map else None
        # Кнопки «+ Игрок»/«+ Актёр» передают (0, 0): новый токен встаёт
        # посередине экрана — туда, куда сейчас смотрит мастер, — а не в
        # фиксированную точку карты. Клик по карте по-прежнему задаёт место.
        if wx or wy:
            spawn_x, spawn_y = wx, wy
        else:
            spawn_x, spawn_y = self.canvas.view_center_snapped()
        token = Token(id=generate_id(), name=name, image_path=path,
                      x=spawn_x, y=spawn_y, size=1.0, owner_id=None,
                      map_id=current_map_id, kind=kind,
                      frame=dialog.frame_key(),
                      portrait_zoom=dialog.zoom(),
                      portrait_dx=portrait_dx,
                      portrait_dy=portrait_dy)
        payload = token.to_dict()
        payload['asset_upload'] = asset_ref
        payload['x'] = spawn_x
        payload['y'] = spawn_y

        self.local_assets[token.id] = path
        self.network_client.send_message(MessageType.ADD_TOKEN, payload)
        self.remember_token_added(token)

    # --- Копирование токенов (Ctrl+C / Ctrl+V) -------------------------
    @staticmethod
    def _text_clipboard_widget():
        """Текстовое поле в фокусе — ему и отдаём Ctrl+C/Ctrl+V.

        Иначе окно перехватывало бы обычное копирование текста в чате
        и полях ввода и вставляло бы токены вместо слов.
        """
        widget = QApplication.focusWidget()
        if widget is None:
            return None
        if isinstance(widget, (QLineEdit, QTextEdit)):
            return widget
        if widget.inherits("QPlainTextEdit"):
            return widget
        return None

    def copy_selected_tokens(self):
        """Ctrl+C: запомнить выделенные токены текущей карты."""
        editor = self._text_clipboard_widget()
        if editor is not None:
            editor.copy()
            return
        ids = self.canvas.selected_token_ids()
        tokens = []
        characters = False
        for token_id in ids:
            token = (self.canvas.tokens.get(token_id)
                     or self.game_state.tokens.get(token_id))
            if token is None or not self.canvas.token_belongs_to_current_map(token):
                continue
            # Игрок копирует только актёров и своих существ. Ту же
            # проверку повторяет сервер: интерфейс — не защита.
            if (self.player_role != Role.DM
                    and token.owner_id != self.player_id):
                continue
            # Персонаж у игрока один: копия — это второй он же, со своей
            # копией листа. Хиты и КД потом расходились между фигурками.
            if getattr(token, 'kind', 'actor') == 'player':
                characters = True
                continue
            tokens.append(token)
        if not tokens:
            if characters:
                if self.player_role == Role.DM:
                    message = ("Персонаж не копируется: перенесите его "
                               "кнопкой «На эту карту»")
                else:
                    message = "Персонаж не копируется: он у игрока один"
                self.statusBar().showMessage(message, 4000)
            elif self.player_role != Role.DM:
                self.statusBar().showMessage(
                    "Копировать можно только своего персонажа", 3000)
            else:
                self.statusBar().showMessage(
                    "Выделите токены на карте для копирования", 3000)
            return
        # Смещения от левого верхнего угла группы: при вставке сохраняем
        # взаимное расположение, а не тащим все токены в одну точку.
        left = min(token.x for token in tokens)
        top = min(token.y for token in tokens)
        self._token_clipboard = {
            'source_map_id': (self.canvas.current_map.id
                              if self.canvas.current_map else None),
            'tokens': [{'source_token_id': token.id,
                        'dx': token.x - left,
                        'dy': token.y - top,
                        'name': token.name} for token in tokens],
        }
        count = len(tokens)
        suffix = "токен" if count == 1 else "токенов"
        self.statusBar().showMessage(f"Скопировано: {count} {suffix}", 3000)

    def _paste_world_point(self):
        """Точка вставки: под курсором, а если он вне холста — центр экрана."""
        cursor = self.canvas.mapFromGlobal(QCursor.pos())
        if self.canvas.rect().contains(cursor):
            wx, wy = self.canvas.screen_to_world(cursor.x(), cursor.y())
        else:
            wx, wy = self.canvas.view_center_world()
        return self.canvas.snap_to_grid(wx, wy)

    def paste_tokens(self):
        """Ctrl+V: попросить сервер скопировать токены на открытую карту."""
        editor = self._text_clipboard_widget()
        if editor is not None:
            editor.paste()
            return
        clipboard = getattr(self, "_token_clipboard", None)
        if not clipboard or not clipboard.get('tokens'):
            self.statusBar().showMessage(
                "Буфер пуст: сначала скопируйте токен (Ctrl+C)", 3000)
            return
        if self.canvas.current_map is None:
            self.statusBar().showMessage(
                "Нет открытой карты — вставлять некуда", 3000)
            return
        map_id = self.canvas.current_map.id
        base_x, base_y = self._paste_world_point()
        for item in clipboard['tokens']:
            payload = {
                'source_token_id': item.get('source_token_id'),
                'map_id': map_id,
                'x': base_x + float(item.get('dx') or 0.0),
                'y': base_y + float(item.get('dy') or 0.0),
            }
            if not payload['source_token_id']:
                continue
            self.network_client.send_message(MessageType.COPY_TOKEN, payload)
        count = len(clipboard['tokens'])
        suffix = "токен" if count == 1 else "токенов"
        self.statusBar().showMessage(
            f"Вставлено: {count} {suffix} — «{self.canvas.current_map.name}»",
            3000)

    def on_add_token_requested(self, x: float, y: float):
        self.add_token_dialog(x, y)

    def on_add_token(self, data: dict):
        token = Token.from_dict({k: v for k, v in data.items()
                                 if k not in ('asset_upload', 'asset_ref')})
        # Токен без map_id (старое сохранение) считаем токеном текущей карты,
        # чтобы он не «потерялся» навсегда.
        if token.map_id is None and self.canvas.current_map:
            token.map_id = self.canvas.current_map.id
        self.game_state.tokens[token.id] = token
        self.canvas.add_token(token, self._token_image_path(token))
        # Токен игрока — сразу страница во вкладке «Лист персонажа».
        self.sync_token_pages()
        if self.player_role == Role.DM:
            if self.canvas.token_belongs_to_current_map(token):
                self.refresh_initiative()
            else:
                # Токен лёг на соседнюю карту: он нужен в списке переноса.
                self.refresh_away_tokens()

    def rebuild_token_rows(self):
        """Пересобрать список токенов целиком (с глазками).

        Единственный правильный способ перестроить список: простой addItem
        теряет кнопки-глазки, поэтому всегда идём через add_token_row.
        """
        # setItemWidget поверх старых строк удаляет виджеты; если в этот
        # момент обходится список (например, из refresh_initiative), Qt
        # обращается к уже освобождённой памяти. Отписываем строки заранее.
        # Пересборка не должна сбрасывать выделение: пустой сигнал
        # itemSelectionChanged при clear() стирал выделение на холсте.
        self._syncing_token_selection = True
        try:
            for i in range(self.tokens_list.count()):
                self.tokens_list.setItemWidget(self.tokens_list.item(i), None)
            self.tokens_list.clear()
            self.token_eye_buttons = {}
            self.token_row_labels = {}
            self.token_rows = []
            # Только токены открытой карты: у каждой карты свой набор, и
            # список слева не должен показывать токены соседних карт.
            for tok in self._ordered_scene_tokens():
                if self._token_matches_filter(tok):
                    self.add_token_row(tok)
            self.initiative_rows = [token_id for token_id, _ in self.token_rows]
        finally:
            self._syncing_token_selection = False
        self._restore_token_list_selection()
        self.refresh_away_tokens()

    def _token_matches_filter(self, token):
        return self.token_kind_filter == "all" or token.kind == self.token_kind_filter

    @staticmethod
    def _token_tooltip(token):
        kind_label = token_kind.kind_label(token)
        combat_label = (f"Инициатива {token.initiative}"
                        if token.initiative is not None else "Без инициативы")
        text = f"{kind_label} — {combat_label}"
        effects = [effect for effect in (getattr(token, 'active_effects', None)
                                        or []) if isinstance(effect, dict)]
        if effects:
            names = []
            for effect in effects:
                label = str(effect.get('label') or 'Эффект')
                left = effect.get('rounds_left')
                names.append(f"{label} ({left} р.)" if left else label)
            text += "\nЭффекты: " + ", ".join(names)
        temp = int(getattr(token, 'hp_temp', 0) or 0)
        if temp > 0:
            text += f"\nВременные хиты: {temp}"
        return text

    def set_token_kind_filter(self, kind):
        self.token_kind_filter = (kind if kind == "all" or kind in token_kind.KINDS
                                  else "all")
        if self.player_role == Role.DM:
            self.rebuild_token_rows()

    def _ordered_scene_tokens(self):
        """Текущая карта: сперва очередь инициативы, затем прочие объекты.

        В бою очередь — только серверная: токен с инициативой, которого
        в ней нет (другая сцена, бросок еще не доехал), не встаёт в очередь
        соседней строкой и не выглядит участником.
        """
        tokens = self.canvas.current_map_tokens()
        ordered_ids = [tid for tid in self.game_state.combat_order if tid in tokens]
        queued = set(ordered_ids)
        initiated = []
        if not self.game_state.combat_active:
            initiated = sorted((t for t in tokens.values()
                                if t.id not in queued
                                and t.initiative is not None),
                               key=lambda t: (-t.initiative, t.name.lower(), t.id))
        ordered_ids.extend(t.id for t in initiated)
        queued.update(t.id for t in initiated)
        others = sorted((t for t in tokens.values() if t.id not in queued),
                        key=lambda t: (t.name.lower(), t.id))
        return [tokens[tid] for tid in ordered_ids] + others

    def add_token_row(self, token):
        """Строка токена с глазком: скрыть/показать для всех участников."""
        item = QListWidgetItem()
        item.setToolTip("👁 виден всем / 🙈 скрыт для всех")
        self.tokens_list.addItem(item)
        self.token_rows.append((token.id, token.name))
        item.setToolTip(self._token_tooltip(token))
        self._attach_token_eye(token.id, item)

    def _attach_token_eye(self, token_id: str, item: QListWidgetItem):
        """Строка токена: глазок слева, название справа подписью.

        Раньше кнопка-глазок накрывала название токена: она ставилась поверх
        всей строки, и длинные имена обрезались. Теперь это горизонтальная
        раскладка — кнопка фиксированной ширины, а текст занимает остаток и
        получает многоточие только если реально не влезает.
        """
        tok = self.game_state.tokens.get(token_id) or self.canvas.tokens.get(token_id)
        visible = bool(tok.visible) if tok else True

        row = QWidget()
        layout = QHBoxLayout(row)
        # Нулевые отступы по вертикали и небольшой зазор: строка ровно по высоте.
        layout.setContentsMargins(2, 0, 4, 0)
        layout.setSpacing(6)

        # Глазок строго первым и фиксированной ширины. Раньше эмодзи рисовался
        # поверх текста, потому что кнопка и подпись делили одну и ту же область.
        btn = QToolButton(row)
        btn.setObjectName("token_eye_btn")
        btn.setAutoRaise(True)
        btn.setFixedSize(28, 24)
        btn.setIconSize(QSize(20, 20))
        btn.setCursor(Qt.PointingHandCursor)
        btn.setText("👁" if visible else "🙈")
        btn.setIcon(self._eye_icon(visible))
        btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        btn.setToolTip("Скрыть токен" if visible else "Показать токен")
        btn.clicked.connect(
            lambda _, t=token_id: self.on_token_visibility_requested(t, None))
        # Кнопка не растягивается и не сжимается — текст получает остаток ширины.
        btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        label = QLabel(self._token_row_text(tok) if tok else "")
        label.setTextFormat(Qt.PlainText)
        label.setToolTip(label.text())
        label.setContentsMargins(0, 0, 0, 0)
        label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        layout.addWidget(btn, 0, Qt.AlignVCenter)
        layout.addWidget(label, 1, Qt.AlignVCenter)

        # Текст пункта пустой: иначе Qt подрисовал бы его ПОД виджетом строки,
        # и имя токена выглядело бы дважды.
        item.setText("")
        self.tokens_list.set_row_widget(item, row)
        self.token_eye_buttons[token_id] = btn
        self.token_row_labels[token_id] = label

    def remove_selected_token(self):
        row = self.tokens_list.currentRow()
        if 0 <= row < len(self.token_rows):
            self.on_remove_token(self.token_rows[row][0])

    def remove_token_row(self, row: int):
        """ПКМ → «Удалить» или Delete в списке сцены."""
        if 0 <= row < len(self.token_rows):
            self.tokens_list.setCurrentRow(row)
            self.on_remove_token(self.token_rows[row][0])

    def remove_token_rows(self, rows):
        """Delete по выделению списка: удаляется вся группа."""
        ids = [self.token_rows[row][0] for row in rows
               if 0 <= row < len(self.token_rows)]
        if not ids:
            return
        if len(ids) == 1:
            self.on_remove_token(ids[0])
            return
        self.on_remove_tokens(ids)

    def on_remove_token(self, token_id: str):
        # Снимок берём ДО удаления: после сервер его уже не помнит.
        token = self.game_state.tokens.get(token_id) or self.canvas.tokens.get(token_id)
        self.network_client.send_message(MessageType.REMOVE_TOKEN,
                                         {'token_id': token_id})
        if token is not None:
            self.remember_token_removed(token)

    def on_remove_tokens(self, token_ids: list):
        """Удалить пакет токенов, выделенных рамкой"""
        if not token_ids:
            return
        tokens = [self.game_state.tokens.get(t) or self.canvas.tokens.get(t)
                  for t in token_ids]
        tokens = [t for t in tokens if t is not None]
        for token_id in token_ids:
            self.network_client.send_message(MessageType.REMOVE_TOKEN,
                                             {'token_id': token_id})
        if tokens:
            self.remember_tokens_removed(tokens)
        self.canvas.selected_tokens.clear()
        self.canvas.selected_token = None
        self.append_system(f"Удалено токенов: {len(token_ids)}")

    def refresh_away_tokens(self):
        """Строки «Персонажи на других картах» с кнопкой переноса сюда.

        Список токенов слева показывает только открытую карту, поэтому
        персонажа из соседней сцены иначе не найти. Копировать его нельзя
        (это тот же игрок), поэтому у строки есть кнопка «На эту карту».
        """
        box = getattr(self, "away_tokens_box", None)
        if box is None:
            return
        current = self.canvas.current_map
        if self.player_role != Role.DM or current is None:
            box.setVisible(False)
            return
        maps = self.game_state.maps
        away = [token for token in self.game_state.tokens.values()
                if getattr(token, 'kind', 'actor') == 'player'
                and token.map_id and token.map_id != current.id
                and token.map_id in maps]
        away.sort(key=lambda tok: (maps[tok.map_id].name.lower(),
                                   (tok.name or '').lower(), tok.id))
        widget = self.away_tokens_list
        # Виджеты строк снимаем до clear(): иначе Qt обращается к уже
        # освобождённым виджетам и падает без трассировки.
        for i in range(widget.count()):
            widget.setItemWidget(widget.item(i), None)
        widget.clear()
        if not away:
            box.setVisible(False)
            return
        for token in away:
            map_name = maps[token.map_id].name
            item = QListWidgetItem()
            item.setToolTip(f"{token.name} — карта «{map_name}»")
            widget.addItem(item)
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(2, 0, 2, 0)
            layout.setSpacing(6)
            label = QLabel(f"{token.name} · {map_name}")
            label.setTextFormat(Qt.PlainText)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            button = QToolButton(row)
            button.setObjectName("away_token_move_btn")
            button.setText("→ На эту карту")
            button.setToolTip(
                f"Перенести «{token.name}» на «{current.name}» "
                "вместе с листом персонажа")
            button.setCursor(Qt.PointingHandCursor)
            button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            button.clicked.connect(
                lambda _, tid=token.id: self.move_token_to_map(tid))
            layout.addWidget(label, 1, Qt.AlignVCenter)
            layout.addWidget(button, 0, Qt.AlignVCenter)
            widget.set_row_widget(item, row)
        box.setVisible(True)

    def move_token_to_map(self, token_id: str):
        """Кнопка «На эту карту»: перенос токена, а не копия.

        Точка — центр экрана мастера: токен появляется там, куда он смотрит,
        а не за краем чужой карты.
        """
        token = self.game_state.tokens.get(token_id)
        current = self.canvas.current_map
        if token is None or current is None:
            return
        if token.map_id == current.id:
            return
        # Именно центр экрана мастера и без прижатия к полотну: токен должен
        # появиться там, куда он смотрит. Раньше точка обрезалась по краям
        # карты (и здесь, и в привязке к сетке), и фишка вылезала у края.
        x, y = self.canvas.view_center_placement()
        self.network_client.send_message(
            MessageType.MOVE_TOKEN_MAP,
            {'token_id': token_id, 'map_id': current.id, 'x': x, 'y': y})
        self.statusBar().showMessage(
            f"«{token.name}» переезжает на «{current.name}»", 3000)

    def on_remove_map_requested(self, map_id: str):
        """Удалить карту по правой кнопке на холсте"""
        if not map_id:
            return
        self._remove_map_with_history(map_id)

    def on_remove_token_net(self, data: dict):
        token_id = data.get('token_id')
        self.canvas.remove_token(token_id)
        self.game_state.tokens.pop(token_id, None)
        self.sync_token_pages()
        self.token_rows = [r for r in self.token_rows if r[0] != token_id]
        self.token_eye_buttons.pop(token_id, None)
        if self.player_role == Role.DM:
            # Обновить общий список сцены и порядок очереди.
            self.refresh_initiative()

    # --- Размер токена -------------------------------------------------
    #: Границы ползунка: 0% — 1/10 клетки, 100% — клетка, 500% — 5 клеток.
    TOKEN_SIZE_MIN_CELLS = 0.1
    TOKEN_SIZE_MAX_CELLS = 5.0

    def _selected_token_id(self):
        """id токена, выбранного в списке сцены (или None)."""
        row = self.tokens_list.currentRow() if hasattr(self, "tokens_list") else -1
        if 0 <= row < len(self.token_rows):
            return self.token_rows[row][0]
        return getattr(self.canvas, "selected_token", None)

    def _selected_token(self):
        token_id = self._selected_token_id()
        if not token_id:
            return None
        return (self.game_state.tokens.get(token_id)
                or self.canvas.tokens.get(token_id))

    @classmethod
    def _size_percent_to_cells(cls, percent) -> float:
        """Проценты ползунка -> размер в клетках (кусочно-линейно).

        0–100% растянуто от 1/10 клетки до целой, 100–500% — от клетки до
        пяти: так 100% всегда ровно клетка, как и просили.
        """
        try:
            percent = max(0, min(500, int(percent)))
        except (TypeError, ValueError):
            percent = 100
        if percent <= 100:
            cells = cls.TOKEN_SIZE_MIN_CELLS + (
                1.0 - cls.TOKEN_SIZE_MIN_CELLS) * percent / 100.0
        else:
            cells = 1.0 + (cls.TOKEN_SIZE_MAX_CELLS - 1.0) * (percent - 100) / 400.0
        return round(cells, 3)

    @classmethod
    def _cells_to_size_percent(cls, cells) -> int:
        """Размер в клетках -> проценты ползунка (обратное к предыдущему)."""
        try:
            cells = float(cells)
        except (TypeError, ValueError):
            cells = 1.0
        if cells <= 1.0:
            percent = (cells - cls.TOKEN_SIZE_MIN_CELLS) / (
                1.0 - cls.TOKEN_SIZE_MIN_CELLS) * 100.0
        else:
            percent = 100.0 + (cells - 1.0) / (
                cls.TOKEN_SIZE_MAX_CELLS - 1.0) * 400.0
        return int(round(max(0, min(500, percent))))

    def _sync_token_size_control(self):
        """Показать в ползунке размер выбранного токена (без отправки в сеть)."""
        slider = getattr(self, "token_size_slider", None)
        if slider is None:
            return
        token = self._selected_token()
        self._size_slider_syncing = True
        try:
            if token is None:
                slider.setEnabled(False)
                self.token_size_label.setText("—")
                self._size_before = None
                return
            percent = self._cells_to_size_percent(getattr(token, "size", 1.0))
            slider.setEnabled(True)
            slider.setValue(percent)
            self.token_size_label.setText(f"{percent}%")
            self._size_before = float(getattr(token, "size", 1.0) or 1.0)
        finally:
            self._size_slider_syncing = False

    def on_token_size_slider(self, percent: int):
        """Ползунок: размер меняется сразу на карте, в сеть — после паузы."""
        self.token_size_label.setText(f"{int(percent)}%")
        if getattr(self, "_size_slider_syncing", False):
            return
        token = self._selected_token()
        if token is None:
            return
        cells = self._size_percent_to_cells(percent)
        # Один и тот же объект лежит и в состоянии, и на холсте; обновляем оба
        # места, чтобы карта перерисовалась сразу, не ждав ответа сервера.
        for store in (self.game_state.tokens, self.canvas.tokens):
            entry = store.get(token.id)
            if entry is not None:
                entry.size = cells
        self.canvas.update()
        # Запоминаем, ЧЕЙ размер меняем: за 220 мс можно успеть выбрать другой
        # токен, и тогда отправлять пришлось бы уже его размер.
        self._size_pending_id = token.id
        self._size_pending_before = getattr(self, "_size_before", None)
        timer = getattr(self, "_size_commit_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._commit_token_size)
            self._size_commit_timer = timer
        timer.start(220)

    def _commit_token_size(self):
        """Отправить итоговый размер токена одним UPDATE_TOKEN."""
        if getattr(self, "_size_slider_syncing", False):
            return
        token_id = getattr(self, "_size_pending_id", None)
        if not token_id:
            return
        token = (self.game_state.tokens.get(token_id)
                 or self.canvas.tokens.get(token_id))
        if token is None:
            return
        before = getattr(self, "_size_pending_before", None)
        if before is None or abs(float(before) - float(token.size)) < 1e-9:
            return
        # Отправленное значение становится новым «было».
        if token_id == self._selected_token_id():
            self._size_before = float(token.size)
        self.on_update_token({'token_id': token.id, 'size': token.size,
                              '_before': {'size': before}})

    def assign_owner(self):
        row = self.tokens_list.currentRow()
        if row < 0 or row >= len(self.token_rows):
            return
        owner_id = self.owner_combo.currentData()
        self.on_update_token({'token_id': self.token_rows[row][0],
                              'owner_id': owner_id})

    def on_update_token(self, data: dict):
        """Отправить изменение токена и запомнить его для Ctrl+Z.

        Прежнее значение берём из ключа _before, если холст его положил: часть
        правок (размер, имя) меняет модель ДО отправки, поэтому вычитать старое
        значение из модели уже поздно. Если _before нет (например, назначение
        владельца), берём текущее значение модели — оно ещё прежнее.
        """
        payload = {k: v for k, v in data.items() if not k.startswith('_')}
        before = dict(data.get('_before') or {})

        token = (self.game_state.tokens.get(payload.get('token_id'))
                 or self.canvas.tokens.get(payload.get('token_id')))

        after = {}
        if token is not None:
            # facing и зрение тоже помним для Ctrl+Z: отмена после поворота
            # должна возвращать прежний угол, а не только имя или размер.
            for field in ('size', 'name', 'owner_id', 'kind', 'facing',
                          'vision_angle', 'vision_range'):
                if field in payload:
                    after[field] = payload[field]
                    if field not in before:
                        before[field] = getattr(token, field, None)

        self.network_client.send_message(MessageType.UPDATE_TOKEN, payload)
        if token is not None and before and after:
            # Не записываем действие, если значения не изменились.
            if any(before.get(f) != after.get(f) for f in after):
                self.remember_token_updated(token, before, after)

    # --- Глазок на токене: скрыть/показать для всей сессии ---
    def on_token_visibility_requested(self, token_id: str, visible):
        """Нажали глазок (на холсте или в панели) — просим сервер переключить"""
        msg = {'token_id': token_id}
        if visible is not None:
            msg['visible'] = bool(visible)
        self.network_client.send_message(MessageType.TOGGLE_TOKEN_VISIBILITY, msg)

    def on_token_visibility_net(self, data: dict):
        """Сервер сообщил новое состояние видимости — применяем у себя"""
        token_id = data.get('token_id')
        visible = bool(data.get('visible', True))
        if not token_id:
            return
        self.canvas.set_token_visible(token_id, visible)
        tok = self.game_state.tokens.get(token_id)
        if tok is not None:
            tok.visible = visible
            self.update_token_row(token_id)
        self.statusBar().showMessage(
            f"Токен {'показан' if visible else 'скрыт'} для всех участников", 2500)

    def update_token_row(self, token_id: str):
        """Обновить глазок и подпись строки токена в списке"""
        for row, (tid, _name) in enumerate(self.token_rows):
            if tid != token_id:
                continue
            tok = self.game_state.tokens.get(token_id) or self.canvas.tokens.get(token_id)
            if tok is None:
                return
            item = self.tokens_list.item(row)
            if item is not None:
                item.setText("")
                item.setToolTip(self._token_tooltip(tok))
            label = self.token_row_labels.get(token_id)
            if label is not None:
                label.setText(self._token_row_text(tok))
                label.setToolTip(label.text())
            btn = self.token_eye_buttons.get(token_id)
            if btn is not None:
                btn.setText("👁" if tok.visible else "🙈")
                btn.setIcon(self._eye_icon(tok.visible))
                # Подсказка описывает ДЕЙСТВИЕ кнопки, а не состояние токена.
                btn.setToolTip("Скрыть токен" if tok.visible else "Показать токен")
            return

    def _token_row_text(self, tok) -> str:
        """Подпись строки токена: имя, инициатива и за кем он закреплён.

        Без владельца в списке не было видно, какие токены уже отданы
        игрокам, а какие свободны: мастер выдавал одному и тому же
        персонажа второй раз или забывал, кому что отдал. За ДМ токен —
        значит, подписи нет.
        """
        text = tok.name
        if tok.initiative is not None:
            text = f"{text} — инициатива {tok.initiative}"
        owner_id = getattr(tok, "owner_id", None)
        if owner_id:
            owner = (self.game_state.players or {}).get(owner_id)
            nickname = getattr(owner, "nickname", None) if owner else None
            text = f"{text} · за {nickname or 'игроком без связи'}"
        return text if tok.visible else f"{text}  (скрыт)"

    def on_update_token_net(self, data: dict):
        self.canvas.update_token(data)
        if {'name', 'kind', 'size'} & set(data.keys()):
            # Имя и размер токена видны в шапке его страницы во вкладке листа.
            self.sync_token_pages()
        if 'hp' in data:
            # Полоска здоровья на холсте перерисовывается только по update(),
            # иначе цифра меняется, а полоса остаётся прежней.
            self.canvas.update()
        token = self.game_state.tokens.get(data.get('token_id'))
        if token:
            # Применяем ВСЕ поля, которые прислал сервер. Короткий список
            # раньше терял состояния и статблок: галочки в карточке при
            # повторном открытии были пустыми, а добавленная атака
            # «не сохранялась», хотя сервер её уже принял.
            for field in ('name', 'size', 'owner_id', 'visible', 'initiative',
                          'kind', 'hp', 'hp_max', 'hp_temp', 'ac', 'dex',
                          'map_id', 'facing', 'vision_angle', 'vision_range'):
                if field in data:
                    setattr(token, field, data[field])
            if 'map_id' in data:
                # Токен уехал на другую карту: с этой сцены он исчезает,
                # выделение и очередь пересобираются сразу, а не после
                # следующего действия.
                self.canvas.selected_tokens.discard(token.id)
                if self.canvas.selected_token == token.id:
                    self.canvas.selected_token = None
                self.canvas.dragging_tokens = [
                    t for t in self.canvas.dragging_tokens if t != token.id]
                self.canvas.group_drag_offsets.pop(token.id, None)
                if self.player_role == Role.DM:
                    self.refresh_initiative()
                else:
                    self.canvas.update()
                    self.refresh_away_tokens()
            if 'active_effects' in data:
                token.active_effects = [
                    dict(effect) for effect in (data.get('active_effects') or [])
                    if isinstance(effect, dict)]
            if 'death_saves' in data:
                token.death_saves = combat_rules.normalize_death_saves(
                    data.get('death_saves'))
            if 'conditions' in data:
                token.conditions = [str(v).strip()
                                    for v in (data.get('conditions') or [])
                                    if str(v).strip()]
            for field in ('resist', 'immune', 'vulnerable'):
                if field in data:
                    setattr(token, field, list(data.get(field) or []))
            if 'statblock' in data and isinstance(data['statblock'], dict):
                token.statblock = copy.deepcopy(data['statblock'])
            if token.hp is not None and token.hp > 0:
                # Живой токен больше не «озвучен» как умерший: после лечения
                # следующий смертельный провал снова даст звук.
                self._dead_sound_tokens.discard(token.id)
            # Сервер кладёт delta в UPDATE_TOKEN только для APPLY_DAMAGE.
            # Положительная delta — лечение: показываем его всем клиентам,
            # а не только тому, кто нажал «Применить».
            healed = data.get('delta')
            try:
                healed = int(healed) if healed is not None else 0
            except (TypeError, ValueError):
                healed = 0
            if healed > 0:
                heal_style = str(data.get('heal_style') or 'item')
                self.canvas.show_heal_effect(
                    token.id, healed, style=heal_style)
                # Карточку лечения показывает тот, кто его посчитал:
                # у заклинания это SPELL_RESULT, иначе было бы две.
                # У заклинания и области своя карточка результата: иначе
                # лечение показалось бы второй карточкой «предмет».
                source = str(data.get('source') or '')
                # Отдых лечит весь стол сразу: у него своя карточка
                # (REST_RESULT), иначе три-четыре «предмета» подряд
                # заслоняли бы бросок и путали источник лечения.
                if (heal_style != 'spell' and not data.get('rest')
                        and source not in ('spell', 'area')):
                    # Источник лечения в подписи: зелье, наложение рук или
                    # ручная правка мастера — это разные вещи.
                    source_ru = {'item': 'предмет',
                                 'lay_on_hands': 'наложение рук'}.get(
                                     heal_style) or 'лечение'
                    self.show_roll_toast(
                        "Лечение", healed, f"{token.name} · {source_ru}",
                        ROLL_GOOD)
                # У заклинания свой звук (Лечащее слово / Лечение ран),
                # у зелья — общий мягкий подъём. Если файла нет, остаётся
                # старый heal.wav, поэтому тишины не будет.
                if not self._play_spell_sound(str(data.get('fx') or '')):
                    self._play_heal_sound()
            elif healed < 0 and not str(data.get('source') or ''):
                # Урон без броска (мастер применил вручную, падение, ловушка).
                # У атаки, заклинания и области своя карточка с броском —
                # сервер помечает их source, чтобы число не удвоилось.
                self.show_roll_toast("Урон", abs(healed), token.name, ROLL_BAD)
            # Сервер подтвердил новый длительный эффект. Показываем подпись
            # всем участникам, а не только тому, кто выпил зелье: иначе
            # невидимость и сопротивление появлялись бы «молча».
            for label in data.get('effect_added') or []:
                self.canvas.show_buff_effect(token.id, str(label))
            for fx in data.get('effect_fx') or []:
                # Щит — полупрозрачный купол, Благословение — золотая аура.
                self.canvas.show_effect_fx(token.id, str(fx))
                self._play_spell_sound(str(fx))
            if data.get('hp_max_restored'):
                # Поражение (Harm) урезало максимум хитов, а лечение болезни
                # вернуло его: это видно строкой внизу, иначе игрок не
                # понял бы, отчего полоска здоровья стала длиннее.
                self.statusBar().showMessage(
                    "Максимум хитов восстановлен: "
                    f"+{int(data['hp_max_restored'])}", 4000)
            if data.get('effect_added') and not data.get('effect_fx'):
                self._play_heal_sound()
            if self.player_role == Role.DM:
                # Обновляем строку на месте: пересборка списка через addItem
                # потеряла бы кнопки-глазки.
                for i, (tid, _) in enumerate(self.token_rows):
                    if tid == token.id:
                        self.token_rows[i] = (tid, token.name)
                        break
                self.update_token_row(token.id)
        # Инициатива и имя участвуют в очереди ходов — обновляем её строки
        if {'initiative', 'name', 'kind'} & set(data.keys()):
            self.refresh_initiative()
        elif {'hp', 'hp_max'} & set(data.keys()) and token:
            self.update_token_row(token.id)
        # Хиты с карты сразу видны в листе: без этого лист показывал 101/101,
        # пока в окне «Хиты и КД» уже стояло 76.
        if token is not None and {'hp', 'hp_max'} & set(data.keys()):
            self._apply_token_hp_to_sheet(token)
        if token is not None and {'hp', 'hp_max', 'death_saves'} & set(data.keys()):
            # Кружки смерти живут на листе и на токене: обновляем оба.
            self._apply_death_state_to_views(token)
        if token is not None and {'conditions', 'statblock'} & set(data.keys()):
            self.canvas.update()
            if self.player_role == Role.DM:
                self.update_token_row(token.id)
            dialog = getattr(self, "_actor_sheet_dialog", None)
            if (dialog is not None
                    and getattr(dialog, "_token", None) is token
                    and hasattr(dialog, "refresh_from_token")):
                dialog.refresh_from_token()

    def _apply_token_hp_to_sheet(self, token):
        """Перенести хиты токена в его лист (если лист есть).

        Не шлёт ничего в сеть и не дёргает sheetChanged: это отображение
        уже принятого состояния, иначе получился бы цикл правок.
        """
        if token is None or token.hp is None:
            return False
        sheet = self._sheet_for_token(token)
        sheet = lss_format.unwrap_sheet(sheet)
        if not isinstance(sheet, dict):
            return False
        vitality = sheet.setdefault("vitality", {})
        if not isinstance(vitality, dict):
            return False
        changed = False
        for key, value in (("hp-current", token.hp), ("hp-max", token.hp_max)):
            if value is None:
                continue
            cell = vitality.get(key)
            if not isinstance(cell, dict):
                cell = {}
                vitality[key] = cell
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if cell.get("value") != value:
                cell["value"] = value
                changed = True
        if not changed:
            return False
        self.token_sheets[token.id] = sheet
        self._refresh_sheet_views(token.id, sheet)
        return True

    def _refresh_sheet_views(self, token_id, sheet):
        """Перерисовать открытый лист токена после правки в памяти.

        ``SheetPages.set_sheet`` пропускает тот же самый объект листа, поэтому
        для изменений «на месте» виджет надо обновить явно — иначе хиты и
        выбранное оружие оставались в интерфейсе старыми.
        """
        if not isinstance(sheet, dict):
            return
        pages = getattr(self, "sheet_pages", None)
        if pages is not None and token_id:
            widget = pages.sheet_widget(token_id)
            if widget is not None and widget.sheet is sheet:
                widget.refresh()
            else:
                pages.set_sheet(token_id, sheet)
        # Панель игрока может держать другую копию того же листа. Если её не
        # заменить, она продолжит показывать старые ячейки, а следующее
        # сохранение вернёт их на сервер — и расход снова «исчезнет».
        widget = getattr(self, "character_sheet", None)
        if widget is None:
            return
        current = getattr(widget, "sheet", None)
        bound = getattr(widget, "token_id", None)
        own = self._own_token()
        same_character = (bound == token_id if bound else
                          (token_id is None or (own is not None
                                                and own.id == token_id)))
        if not same_character:
            return
        if current is sheet:
            widget.refresh()
        else:
            widget.set_sheet(sheet)

    # ------------------------------------------------------------------
    # Боевые действия
    #
    # Клиент отправляет намерение, сервер бросает кубики и заменяет
    # хиты, а результат прилетает обратно сообщением ATTACK_RESULT. Поэтому здесь
    # нет ни одного вызова random: один и тот же бросок видят все участники.
    # ------------------------------------------------------------------

    def map_tokens_for_combat(self):
        """Токены текущей карты списком — для выпадающих списков целей."""
        tokens = self.canvas.current_map_tokens()
        return sorted(tokens.values(), key=lambda t: (t.name.lower(), t.id))

    def collect_token_attacks(self, token_id):
        """Атаки токена для диалога атаки: то же, что _raw_token_attacks,
        плюс отметка ``chosen`` — оружие, выбранное на странице листа.

        Так диалог открывается на оружии токена, а не всегда на первом.
        """
        attacks = self._raw_token_attacks(token_id)
        sheet = self._token_sheet(token_id)
        chosen = lss_format.attack_weapon(sheet) if sheet else None
        quick = lss_format.quick_weapon(sheet) if sheet else None
        for attack in attacks:
            if chosen and attack.get("id") == chosen.get("id"):
                attack["chosen"] = True
            if quick and attack.get("id") == quick.get("id"):
                attack["quick"] = True
        return attacks

    def chosen_attack_name(self, token_id):
        """id оружия, с которого открывается диалог атаки."""
        sheet = self._token_sheet(token_id)
        chosen = lss_format.attack_weapon(sheet) if sheet else None
        return chosen.get("id") if chosen else None

    def quick_attack_name(self, token_id):
        """id оружия/удара, которым бьёт быстрая атака (Q).

        У персонажа выбор лежит в листе LSS, у актёра — в статблоке
        (``quick_attack``). Без второй ветки у существ всегда бил первый удар.
        """
        sheet = self._token_sheet(token_id)
        weapon = lss_format.quick_weapon(sheet) if sheet else None
        if weapon:
            return weapon.get("id")
        token = (self.game_state.tokens.get(token_id)
                 or self.canvas.tokens.get(token_id))
        block = getattr(token, "statblock", None) or {}
        return block.get("quick_attack") or None

    @staticmethod
    def _statblock_attack_id(attack: dict) -> str:
        """Стабильный id удара статблока — тот же, что пишет карточка актёра."""
        attack_id = attack.get("id")
        if attack_id:
            return str(attack_id)
        name = str(attack.get("name") or "")
        damage = str(attack.get("damage") or "")
        return f"sb:{name}|{damage}".casefold()

    def _token_sheet(self, token_id):
        """Лист токена в виде самого листа LSS (или None).

        В памяти лист может лежать и обёрткой файла (``data`` — строка
        JSON): тогда оружие искалось в обёртке и список был пуст, хотя
        страница листа показывала оружие.
        """
        token = (self.game_state.tokens.get(token_id)
                 or self.canvas.tokens.get(token_id))
        if token is None:
            return None
        return lss_format.unwrap_sheet(self._sheet_for_token(token))

    def _raw_token_attacks(self, token_id):
        """Собрать атаки токена: оружие с листа и удары из статблока.

        Формат записи:
        {'id', 'name', 'attack_bonus', 'damage', 'damage_type'}.
        Так диалог атаки показывает список вместо ручного ввода формул.
        Порядок: оружие листа LSS (``weaponsList``) — оно у персонажа игрока
        главное, затем инвентарь стола, затем удары статблока.
        """
        out = []
        token = (self.game_state.tokens.get(token_id)
                 or self.canvas.tokens.get(token_id))
        if token is None:
            return out
        sheet = self._token_sheet(token_id)
        seen = set()

        def add(attack):
            """Не показывать ровно то же оружие дважды.

            Бонус входит в ключ: два одноимённых удара с разными
            надбавками (базовый и с инфузией) — это два разных выбора,
            и прятать один из них нельзя.
            """
            key = (attack.get("name"), attack.get("damage"),
                   attack.get("attack_bonus"))
            if key in seen:
                return
            seen.add(key)
            out.append(attack)

        # Оружие и инвентарь героя — только у персонажа игрока. У актёра
        # документ статблок: иначе чужой меч подменял удары монстра.
        if isinstance(sheet, dict) and not token_kind.is_actor(token):
            # 1) Оружие из самого листа LSS: имена, бонусы и формулы считает
            #    shared/lss_format, чтобы разбор не разъезжался по местам.
            # Одно кривое оружие не должно прятать остальные: разбор идёт
            # по строке, а не пачкой. Раньше исключение внутри списка
            # теряло все атаки листа — игрок видел пустой список и не
            # понимал, почему его меч «пропал».
            for weapon in lss_format.weapon_attacks(sheet):
                try:
                    if not weapon.get("damage"):
                        continue
                    # В листе тип написан по-русски («рубящий»), а бой ждёт
                    # ключ dnd5e — иначе сопротивления цели не сработают.
                    # Лист мог не указать тип — тогда оружие бьёт без типа.
                    attack = dict(weapon)
                    attack["damage_type"] = combat_rules.normalize_damage_type(
                        weapon.get("damage_type"))
                    # Оружие листа можно назначить быстрой атакой: выбор
                    # хранится в листе, у статблока такого места нет.
                    attack["quick_selectable"] = True
                    add(attack)
                except Exception:
                    logging.exception("Атака из листа не разобрана")
                    continue

            # 2) Инвентарь стола (предметы, выданные мастером с листа).
            for item in ((sheet.get("vtt") or {}).get("inventory") or []):
                try:
                    formulas = [str(x) for x in (item.get("formulas") or [])
                                if str(x).strip()]
                    if not formulas:
                        continue
                    # «лечение: 2d4+2» — не атака: раньше зелье лечения
                    # попадало в список атак токена и «било» им чужие хиты.
                    damage_formulas = [
                        seg for seg in ";".join(formulas).split(";")
                        if seg.strip() and not re.match(
                            r"\s*(лечение|heal)", seg, re.IGNORECASE)]
                    raw_damage = " ".join(damage_formulas)
                    if not raw_damage.strip():
                        continue
                    # «урон: 1d6 рубящий» -> «1d6» и ключ slashing. Иначе
                    # диалог показывал текст, а сопротивление цели не работало.
                    damage = (combat_rules.clean_damage_formula(raw_damage)
                              or damage_formulas[0].strip())
                    bonus = item.get("attack_bonus")
                    if bonus is None:
                        bonus = lss_format.generic_attack_bonus(sheet)
                    add({
                        "id": item.get("id") or item.get("name"),
                        "name": item.get("name") or "Оружие",
                        "attack_bonus": int(bonus),
                        "damage": damage,
                        "damage_type": combat_rules.normalize_damage_type(
                            item.get("damage_type") or raw_damage),
                        "quick_selectable": True,
                        "ranged": lss_format.weapon_is_ranged(
                            item, None, raw_damage),
                    })
                except Exception:
                    # Один непонятный предмет не должен съедать весь
                    # инвентарь в списке атак.
                    logging.exception("Предмет из инвентаря не разобран")
                    continue

        # 3) Удары существа из статблока.
        block = getattr(token, "statblock", None) or {}
        statblock_quick = block.get("quick_attack") or ""
        for attack in (block.get("attacks") or []):
            if not isinstance(attack, dict):
                continue
            attack_id = self._statblock_attack_id(attack)
            # В статблоках тип часто записан словами в самом уроне
            # («1d8+4 руб.»): отделяем его, иначе сопротивление цели
            # не сработает, а диалог покажет «— тип —».
            damage_text, parsed_type = combat_rules.split_damage_text(
                attack.get("damage"))
            add({
                "id": attack_id,
                "name": attack.get("name") or "Атака",
                "attack_bonus": attack.get("bonus"),
                "damage": damage_text or str(attack.get("damage") or ""),
                "damage_type": combat_rules.normalize_damage_type(
                    attack.get("damage_type") or parsed_type),
                # Удар существа теперь тоже можно назначить быстрой атакой
                # из карточки актёра — раньше такой выбор был только у оружия.
                "quick_selectable": True,
                "quick": bool(statblock_quick and statblock_quick == attack_id),
                "ranged": lss_format.weapon_is_ranged(
                    attack, str(attack.get("type") or "")),
            })
        return out

    def smite_options_for(self, attacker_id, target_id=None):
        """Круги кары для окна атаки: свободные ячейки и тип цели.

        Считает общий модуль правил (`shared.class_features`), поэтому окно
        показывает те же кубики, которые сервер потом бросит сам. У
        монстра листа нет — список пуст, и строка кары скрыта.
        """
        token = (self.game_state.tokens.get(attacker_id)
                 or self.canvas.tokens.get(attacker_id))
        if token is None:
            return {"levels": [], "reason": "Нет атакующего токена"}
        sheet = self._sheet_for_token(token)
        if not isinstance(sheet, dict):
            return {"levels": [],
                    "reason": "У атакующего нет листа персонажа"}
        if not class_features.has(sheet, class_features.SMITE):
            return {"levels": [],
                    "reason": "Божественная кара — особенность паладина "
                              "2 уровня: в листе её нет"}
        target = (self.game_state.tokens.get(target_id)
                  or self.canvas.tokens.get(target_id)) if target_id else None
        target_sheet = self._sheet_for_token(target) if target is not None else None
        extra = class_features.is_fiend_or_undead(target, target_sheet)
        levels = class_features.free_slots(sheet)
        reason = "" if levels else "Нет свободных ячеек заклинаний"
        return {"levels": levels, "extra": extra, "reason": reason}

    def chosen_attack_for(self, token_id):
        """Оружие токена для диалога атаки: выбор строки «Атака» или первое."""
        attacks = self._raw_token_attacks(token_id)
        if not attacks:
            return None
        chosen = self.chosen_attack_name(token_id)
        if chosen:
            for attack in attacks:
                if attack.get("id") == chosen:
                    return attack
        return attacks[0]

    def quick_attack_for(self, token_id):
        """Оружие быстрой атаки: строка «Для быстрой атаки» или выбор «Атаки»."""
        attacks = self._raw_token_attacks(token_id)
        if not attacks:
            return None
        quick = self.quick_attack_name(token_id)
        if quick:
            for attack in attacks:
                if attack.get("id") == quick:
                    return attack
        return self.chosen_attack_for(token_id)

    def open_attack_dialog(self, attacker_id=None, target_id=None,
                           preset=None):
        """Диалог атаки: оружие с листа атакующего, цель и кубы урона.

        Атакующий по умолчанию — выбранный токен: без этого окно открывалось
        с пустым списком атак, и оружие с листа приходилось искать вручную.
        """
        # Окно одно на клиент: повторный сигнал из таймера или двойного
        # клика иначе открывал второе окно поверх первого.
        if getattr(self, "_attack_dialog_open", False):
            return False
        tokens = self.map_tokens_for_combat()
        if len(tokens) < 2:
            self.append_system("Для атаки нужны два токена на карте")
            return
        attacker_id = attacker_id or self.canvas.selected_token
        # Без явно выбранного атакующего окно откроется на первом токене и
        # само покажет причину, если тот не может действовать. Явно
        # выбранного (ПКМ по токену) проверяем сразу — чтобы не открывать
        # окно, которое заведомо нельзя подтвердить.
        if attacker_id:
            allowed, why = self._token_can_act(attacker_id)
            if not allowed:
                self.append_system(f"Нельзя атаковать: {why}")
                return
        attacks = self.collect_token_attacks(attacker_id)
        dialog = AttackDialog(tokens, attacker_id=attacker_id,
                              target_id=target_id, parent=self, attacks=attacks,
                              attacks_provider=self.collect_token_attacks,
                              smite_provider=self.smite_options_for)
        dialog.quickAttackChanged.connect(self.set_quick_attack_weapon)
        if preset:
            # Оружие, которым уже собрались бить (нажали «Использовать» в
            # инвентаре), выбираем и в списке: иначе в поле «Атака» стояло
            # одно оружие, а в уроне — кубы другого.
            matched = self._select_attack_preset(dialog, preset)
            if not matched:
                dialog.preset_combo.setCurrentIndex(0)   # — вручную —
            if preset.get("attack_bonus") is not None:
                try:
                    dialog.bonus_spin.setValue(int(preset["attack_bonus"]))
                except (TypeError, ValueError):
                    pass
            preset_damage, preset_type = combat_rules.split_damage_text(
                preset.get("damage"))
            if preset_damage or preset.get("damage"):
                dialog.damage_edit.setEditText(
                    preset_damage if preset_type else str(preset["damage"]))
            dtype = combat_rules.normalize_damage_type(
                preset.get("damage_type") or preset_type)
            if dtype:
                index = dialog.type_combo.findData(dtype)
                if index >= 0:
                    dialog.type_combo.setCurrentIndex(index)
            if preset.get("smite"):
                # Кнопка «Божественная кара» на листе открывает то же окно с
                # уже выбранным кругом: остаётся выбрать цель и подтвердить.
                level = int(preset.get("smite_level") or 1)
                if not dialog.select_smite(level):
                    self.append_system(
                        "Божественная кара: нет свободной ячейки этого круга")
        self._attack_dialog_open = True
        try:
            accepted = dialog.exec_() == QDialog.Accepted
        finally:
            self._attack_dialog_open = False
        if accepted:
            payload = dialog.payload()
            # Ключ отклика нужен только заклинанию: по нему сервер вернёт
            # `fx` в ATTACK_RESULT, и холст нарисует луч, а не обычный удар.
            if preset and preset.get("fx"):
                payload["fx"] = str(preset["fx"])
            self._remember_weapon_type(attacker_id, preset, payload)
            self.network_client.send_message(MessageType.ROLL_ATTACK, payload)
            return True
        return False

    @staticmethod
    def _select_attack_preset(dialog, preset: dict) -> bool:
        """Выбрать в окне атаки оружие с тем же названием. True — нашли."""
        name = str((preset or {}).get("name") or "").strip().casefold()
        if not name:
            return False
        for index in range(1, dialog.preset_combo.count()):
            entry = dialog.preset_combo.itemData(index)
            if not isinstance(entry, dict):
                continue
            if str(entry.get("name") or "").strip().casefold() == name:
                dialog.preset_combo.setCurrentIndex(index)
                return True
        return False

    def _remember_weapon_type(self, attacker_id, preset, payload):
        """Тип урона, выбранный в окне атаки, остаётся в оружии листа.

        Поле «Тип урона» выглядело настройкой оружия, но жило ровно один
        удар: следующий удар тем же мечом снова был «без типа», и
        сопротивление цели не срабатывало. Теперь выбор возвращается в
        `weaponsList` и подтягивается дальше сам.
        """
        weapon = str((preset or {}).get("id") or "")
        label = combat_rules.damage_type_label((payload or {}).get("damage_type"))
        if not attacker_id or not weapon or not label:
            return False
        token = (self.game_state.tokens.get(attacker_id)
                 or self.canvas.tokens.get(attacker_id))
        if token is None:
            return False
        sheet = self._sheet_for_token(token)
        if not isinstance(sheet, dict):
            return False      # у актёра без листа тип помнит его карточка
        for entry in sheet.get("weaponsList") or []:
            if not isinstance(entry, dict):
                continue
            name = str(lss_format._cell_value(entry.get("name")) or "")
            if lss_format.weapon_id(entry, name or "Оружие") != weapon:
                continue
            # У старого оружия в `dmgType` лежит формула урона («1d8») —
            # перезапись стёрла бы кубики, такое оружие пропускаем.
            if not str(lss_format._cell_value(entry.get("dmg")) or "").strip():
                return False
            if lss_format.weapon_damage_type(entry).casefold() == label.casefold():
                return False
            entry["dmgType"] = {"value": label.lower()}
            self._store_token_sheet(token, sheet)
            self._refresh_sheet_views(token.id, sheet)
            return True
        return False

    # ------------------------------------------------------------------
    # Быстрая атака: выбран токен A, клик по токену B атакует его
    # базовой атакой из листа/статблока A — без диалогов.
    # ------------------------------------------------------------------
    def set_quick_attack_weapon(self, token_id, weapon_id):
        """Оружие, отмеченное в окне атаки, становится ударом быстрой атаки.

        Выбор лежит в самом листе (``vtt.quick_weapon``), поэтому работает и у
        игрока, и на странице токена у мастера, и переживает перезаход.
        """
        token = (self.game_state.tokens.get(token_id)
                 or self.canvas.tokens.get(token_id))
        if token is None:
            return False
        sheet = self._sheet_for_token(token)
        if isinstance(sheet, dict):
            if not lss_format.set_quick_weapon(sheet, weapon_id):
                return False
            self._store_token_sheet(token, sheet)
            self._refresh_sheet_views(token.id, sheet)
        else:
            # Актёр без листа: выбор быстрой атаки живёт в его статблоке.
            block = dict(getattr(token, "statblock", None) or {})
            if not any(self._statblock_attack_id(a) == str(weapon_id)
                       for a in block.get("attacks") or []
                       if isinstance(a, dict)):
                self.append_system(
                    "У этого токена нет листа — быстрая атака без выбора")
                return False
            block["quick_attack"] = str(weapon_id)
            token.statblock = block
            self.network_client.send_message(MessageType.UPDATE_TOKEN, {
                "token_id": token.id, "statblock": block})
        attack = self.quick_attack_for(token.id) or {}
        self.statusBar().showMessage(
            f"Быстрая атака (Q): {attack.get('name', 'оружие')} "
            f"({attack.get('damage', '—')})", 4000)
        return True

    def toggle_quick_attack_mode(self):
        """Вкл/выкл режим быстрой атаки по хоткею Q.

        «Q» — обычная буква: в чате и в заметках листа она должна
        печататься. QShortcut живёт на всё окно, поэтому без этой
        проверки «й» в поле заметок взводила бы удар вместо ввода.
        """
        if self._keyboard_in_text_field():
            return
        if not self.canvas.quick_attack_mode and not self.canvas.selected_token:
            self.statusBar().showMessage(
                "Сначала выберите атакующего токен, затем нажмите Q", 3000)
            # Кнопка уже нажалась сама — возвращаем её в честное состояние.
            self._reflect_quick_attack_mode()
            return
        # Мёртвый и недееспособный не атакует: не включаем режим, иначе
        # каждый клик по цели заканчивался бы отказом сервера.
        if not self.canvas.quick_attack_mode:
            allowed, why = self._token_can_act(self.canvas.selected_token)
            if not allowed:
                self.append_system(f"Нельзя атаковать: {why}")
                self._reflect_quick_attack_mode()
                return
        self.canvas.set_quick_attack_mode(not self.canvas.quick_attack_mode)
        self._reflect_quick_attack_mode()

    def _token_can_act(self, token_id):
        """Может ли токен действовать: (да/нет, причина для чата)."""
        token = (self.game_state.tokens.get(token_id)
                 or self.canvas.tokens.get(token_id)) if token_id else None
        if token is None:
            return False, "токен не выбран"
        conditions = token_stats.token_conditions(
            token, self._sheet_for_token(token))
        dead = combat_rules.death_save_state(
            getattr(token, "death_saves", None))["dead"]
        allowed, why = conditions_rules.can_act(
            conditions, dead=dead, hp=getattr(token, "hp", None))
        if allowed:
            return True, ""
        return False, f"«{getattr(token, 'name', 'токен')}» {why}"

    def _reflect_quick_attack_mode(self):
        """Показать состояние режима: кнопка, статус и плашка на холсте.

        Плашка перечисляет то, чем именно ударим: раньше о взведённой
        атаке говорила только кнопка, а на карте не было ни следа.
        """
        active = self.canvas.quick_attack_mode
        if getattr(self, "act_quick_attack", None) is not None:
            self.act_quick_attack.setChecked(active)
        if not active:
            self.canvas.set_quick_attack_hint("")
            self.statusBar().clearMessage()
            return
        attack = self.quick_attack_for(self.canvas.selected_token) or {}
        name = attack.get("name") or "безоружный удар"
        damage = attack.get("damage") or "—"
        bonus = attack.get("attack_bonus")
        bonus_text = f"{bonus:+d}" if isinstance(bonus, int) else "—"
        self.canvas.set_quick_attack_hint(
            f"Быстрая атака: {name}  ·  атака {bonus_text}  ·  урон {damage}"
            "  —  клик по цели, Esc — отмена")
        self.statusBar().showMessage(
            "Быстрая атака: клик по другому токену — удар. Esc — отмена.",
            6000)

    def _escape_pressed(self):
        """Esc: отменить текущий режим, иначе снять выделение.

        QShortcut окна срабатывает раньше холста, поэтому Esc до
        `GameCanvas.keyPressEvent` не доходит. Раньше здесь стояло
        «пока холст рисует — оставляем Esc ему»: клавиша просто терялась,
        и отмена поворота, недорисованной стены и калибровки не работала.
        Теперь обе стороны зовут одну цепочку `canvas.handle_escape()`.
        """
        # Взведённое заклинание, поворот, стена, калибровка, шаблон,
        # линейка, фигура, штрих, подсветка хода — всё живёт там.
        if self.canvas.handle_escape():
            return
        if self.canvas.quick_attack_mode:
            self.canvas.set_quick_attack_mode(False)
            self._reflect_quick_attack_mode()
            return
        self.canvas.selected_token = None
        self.canvas.selected_tokens.clear()
        self.canvas.token_deselected.emit()
        self.canvas.update()

    def basic_attack_for(self, token_id):
        """Базовая атака токена: выбранное оружие, иначе первая доступная.

        Порядок: оружие с листа персонажа (то, что игрок выбрал для быстрой
        атаки, иначе первое в листе), затем инвентарь и удары статблока,
        а если ничего нет — безоружный удар (1d4 + Сила). Возвращает словарь
        в формате {'id','name','attack_bonus','damage','damage_type'} — тот
        же, что собирает collect_token_attacks.
        """
        chosen = self.quick_attack_for(token_id)
        if chosen:
            return chosen
        # Голые руки: Сила для персонажа с листом, иначе просто +0.
        token = (self.game_state.tokens.get(token_id)
                 or self.canvas.tokens.get(token_id))
        bonus = 0
        if token is not None:
            sheet = self._sheet_for_token(token)
            if isinstance(sheet, dict) and sheet.get("stats"):
                score = (sheet["stats"].get("str") or {}).get("score", 10)
                bonus = lss_format.modifier(score)
            else:
                bonus = int(getattr(token, "dex", 0) or 0)
        return {"name": "Безоружный удар", "attack_bonus": bonus,
                "damage": "1d4", "damage_type": ""}

    def on_quick_attack(self, attacker_id: str, target_id: str):
        """Клик по цели в режиме быстрой атаки: бросить базовую атаку."""
        if not attacker_id or not target_id or attacker_id == target_id:
            return
        attacker = (self.game_state.tokens.get(attacker_id)
                    or self.canvas.tokens.get(attacker_id))
        target = (self.game_state.tokens.get(target_id)
                  or self.canvas.tokens.get(target_id))
        if attacker is None or target is None:
            return
        # Права: игрок атакует только своим токеном и только чужую цель.
        # Сервер всё равно проверяет, но ранний отказ экономит поезд по сети.
        if self.player_role != Role.DM and attacker.owner_id != self.player_id:
            self.append_system("Атаковать можно только своим токеном")
            return
        if target.owner_id == self.player_id and self.player_role != Role.DM:
            self.append_system("Нельзя атаковать собственный токен")
            return
        allowed, why = self._token_can_act(attacker_id)
        if not allowed:
            self.append_system(f"Нельзя атаковать: {why}")
            return
        if combat_rules.death_save_state(
                getattr(target, "death_saves", None))["dead"]:
            self.append_system(
                f"«{target.name}» мёртв — его нельзя атаковать, только воскресить")
            return
        attack = self.basic_attack_for(attacker_id)
        self.network_client.send_message(MessageType.ROLL_ATTACK, {
            "attacker_id": attacker_id,
            "target_id": target_id,
            "attack_bonus": attack.get("attack_bonus") or 0,
            "damage": attack.get("damage") or "",
            "damage_type": attack.get("damage_type") or "",
            # Без названия оружия сервер не отличит фехтовальный удар от
            # обычного, и скрытая атака плута не сработает.
            "weapon": attack.get("name") or "",
            "ranged": bool(attack.get("ranged")),
            "ability": str(attack.get("ability") or ""),
        })
        self.statusBar().showMessage(
            f"{attacker.name} атакует «{attack.get('name', 'удар')}» → {target.name}",
            3000)

    def open_save_dialog(self, token_id=None, ability="dex", dc=None):
        """Спасбросок токена: бонус подставляется с листа или статблока."""
        if getattr(self, "_save_dialog_open", False):
            return False
        tokens = self.map_tokens_for_combat()
        if not tokens:
            self.append_system("На карте нет токенов")
            return
        dialog = SaveDialog(
            tokens, token_id=token_id or self.canvas.selected_token,
            ability=ability, dc=dc, sheet_lookup=self._sheet_for_token,
            parent=self, can_revive=self.player_role == Role.DM)
        dialog.deathSaveRollRequested.connect(self.on_death_save_roll)
        dialog.deathSaveSetRequested.connect(self.on_death_save_set)
        dialog.deathReviveRequested.connect(self.on_death_revive)
        dialog.deathStabilizeRequested.connect(self.on_death_stabilize)
        self._save_dialog = dialog
        self._save_dialog_open = True
        try:
            accepted = dialog.exec_() == QDialog.Accepted
        finally:
            self._save_dialog = None
            self._save_dialog_open = False
        if accepted:
            payload = dialog.payload()
            # Если бонус снят с автоматики вручную — передаём его как есть,
            # иначе сервер считает сам по листу.
            if not dialog.auto_check.isChecked():
                payload["override_bonus"] = dialog.bonus_spin.value()
            self.network_client.send_message(MessageType.ROLL_SAVE, payload)
            return True
        return False

    def open_conditions_dialog(self, token_id=None):
        """Поставить состояния токену — видно всем и влияет на броски."""
        if getattr(self, "_conditions_dialog_open", False):
            return
        token_id = token_id or self.canvas.selected_token
        token = self.game_state.tokens.get(token_id) if token_id else None
        if token is None:
            self.statusBar().showMessage("Сначала выберите токен", 2500)
            return
        sheet = self._sheet_for_token(token)
        current = token_stats.token_conditions(token, sheet)
        dialog = ConditionsDialog(
            token.id, current, parent=self,
            immune=token_stats.token_condition_immunities(token, sheet))
        self._conditions_dialog_open = True
        try:
            accepted = dialog.exec_() == QDialog.Accepted
        finally:
            self._conditions_dialog_open = False
        if accepted:
            self.apply_token_conditions(dialog.payload())

    def apply_token_conditions(self, payload: dict):
        """Наложить состояния: локально сразу, затем подтверждение с сервера.

        Локальная правка нужна, чтобы иконки и галочки появлялись в тот же
        миг, не дожидаясь сети. Сервер отвечает UPDATE_TOKEN тем же списком,
        и он же остаётся источником истины для остальных участников.
        """
        if not isinstance(payload, dict):
            return
        token_id = payload.get('token_id')
        conditions = [str(v).strip() for v in (payload.get('conditions') or [])
                      if str(v).strip()]
        # Иммунитет проверяем и локально: иначе состояние на миг появилось
        # бы на токене, пока не пришёл ответ сервера с очищенным списком.
        probe = (self.game_state.tokens.get(token_id)
                 or self.canvas.tokens.get(token_id))
        if probe is not None:
            immune = conditions_rules.normalize_immunities(
                token_stats.token_condition_immunities(
                    probe, self._sheet_for_token(probe)))
            conditions = [name for name in conditions
                          if not conditions_rules.is_immune(name, immune)]
        for store in (self.game_state.tokens, self.canvas.tokens):
            token = store.get(token_id)
            if token is None:
                continue
            token.conditions = list(conditions)
        self.canvas.update_token({'token_id': token_id,
                                  'conditions': conditions})
        self.canvas.update()
        self.network_client.send_message(MessageType.SET_CONDITIONS, {
            'token_id': token_id, 'conditions': conditions})

    def open_actor_sheet(self, token_id=None):
        """Редактируемая карточка актёра (Foundry-лист) для токена.

        Правки применяются кнопкой «Сохранить» и уходят в сеть одним
        UPDATE_TOKEN: имя, размер, хиты, КД, характеристики, атаки,
        сопротивления и состояния. Мастер правит любой токен, игрок — свой.
        """
        if getattr(self, "_actor_sheet_dialog_open", False):
            return
        token_id = token_id or self.canvas.selected_token
        token = (self.game_state.tokens.get(token_id)
                 or self.canvas.tokens.get(token_id)) if token_id else None
        if token is None:
            self.statusBar().showMessage("Сначала выберите токен", 2500)
            return
        # Разделение ролей одно на всё окно (shared/token_kind.py): у
        # персонажа игрока документ — лист LSS, карточки актёра нет.
        if not token_kind.has_actor_sheet(token):
            self.append_system(
                f"«{token.name}» — персонаж игрока: карточки актёра нет, "
                "открываю лист LSS")
            self.open_lss_sheet(token.id)
            return
        # Права: игрок редактирует только свой токен (сервер тоже проверяет).
        if self.player_role != Role.DM and token.owner_id != self.player_id:
            self.append_system("Редактировать можно только свой токен")
            return
        before = self._token_edit_snapshot(token)
        dialog = ActorSheetDialog(
            self._enrich_statblock(token),
            sheet=self._sheet_for_token(token), parent=self,
            can_revive=self.player_role == Role.DM,
            other_tokens=self.map_tokens_for_combat(),
            can_edit=(self.player_role == Role.DM
                      or token.owner_id == self.player_id))
        dialog.saveRequested.connect(
            lambda payload: self.apply_actor_sheet_save(token, payload, before))
        # Атака и спасбросок из карточки уходят сразу: держать их до
        # «Сохранить» бессмысленно — это бросок, а не правка статблока.
        dialog.attackRollRequested.connect(
            lambda payload: self.network_client.send_message(
                MessageType.ROLL_ATTACK, payload))
        dialog.saveRollRequested.connect(
            lambda payload: self.network_client.send_message(
                MessageType.ROLL_SAVE, payload))
        # Галочка состояния в карточке — сразу эффект на токене (как в
        # Foundry), без ожидания «Сохранить»: иначе закрытая карточка
        # выглядела так, будто ничего не накладывает.
        dialog.conditionsChanged.connect(self.apply_token_conditions)
        # Спасброски от смерти бросаются из карточки и сразу уходят на сервер:
        # ждать «Сохранить» ради броска было бы странно.
        dialog.deathSaveRollRequested.connect(self.on_death_save_roll)
        dialog.deathSaveSetRequested.connect(self.on_death_save_set)
        dialog.deathReviveRequested.connect(self.on_death_revive)
        dialog.deathStabilizeRequested.connect(self.on_death_stabilize)
        # Особая атака (дыхание, проглатывание): кнопка в карточке — это
        # выбор цели на холсте, а расчёт делает сервер.
        dialog.specialAttackRequested.connect(self.on_special_attack_request)
        self._actor_sheet_dialog = dialog
        self._actor_sheet_dialog_open = True
        try:
            dialog.exec_()
        finally:
            self._actor_sheet_dialog = None
            self._actor_sheet_dialog_open = False

    def _token_edit_snapshot(self, token) -> dict:
        """Снимок полей карточки для отмены правки через Ctrl+Z."""
        if token is None:
            return {}
        return {
            'name': token.name,
            'size': token.size,
            'hp': token.hp,
            'hp_max': token.hp_max,
            'ac': token.ac,
            'dex': getattr(token, 'dex', None),
            'conditions': list(getattr(token, 'conditions', None) or []),
            'resist': list(getattr(token, 'resist', None) or []),
            'immune': list(getattr(token, 'immune', None) or []),
            'vulnerable': list(getattr(token, 'vulnerable', None) or []),
            'statblock': copy.deepcopy(getattr(token, 'statblock', None)),
        }

    def on_page_vision_changed(self, token_id: str, angle: float,
                               feet: float):
        """Изменили зрение в шапке листа: угол и дальность — в токен.

        Это настройка стола, а не поле листа: значение едет обычным
        UPDATE_TOKEN, поэтому работает и у игрока для своего токена,
        и у мастера — для любого.
        """
        token = (self.game_state.tokens.get(token_id)
                 or self.canvas.tokens.get(token_id))
        if token is None:
            return
        angle = vision_rules.normalize_angle(angle)
        feet = vision_rules.normalize_range(feet)
        before = {'vision_angle': getattr(token, "vision_angle", None),
                  'vision_range': getattr(token, "vision_range", None)}
        # Локально сразу: сектор на карте не ждёт ответа сервера.
        for store in (self.game_state.tokens, self.canvas.tokens):
            entry = store.get(token_id)
            if entry is not None:
                entry.vision_angle = angle
                entry.vision_range = feet
        self.canvas.update()
        self.on_update_token({'token_id': token_id,
                              'vision_angle': angle,
                              'vision_range': feet,
                              '_before': before})

    def on_page_token_size_changed(self, token_id: str, cells: float):
        """Мастер поменял размер токена в шапке вкладки листа.

        Это не поле листа: меняется сам токен на карте. Значение уходит
        обычным UPDATE_TOKEN, поэтому его видят все и работает Ctrl+Z.
        """
        token = (self.game_state.tokens.get(token_id)
                 or self.canvas.tokens.get(token_id))
        if token is None:
            return
        try:
            size = float(cells)
        except (TypeError, ValueError):
            return
        size = max(0.5, min(20.0, size))
        before = float(getattr(token, "size", 1.0) or 1.0)
        if abs(before - size) < 1e-9:
            return
        # Локально сразу — карта не ждёт ответа сервера.
        for store in (self.game_state.tokens, self.canvas.tokens):
            entry = store.get(token_id)
            if entry is not None:
                entry.size = size
        self.canvas.update()
        self.on_update_token({'token_id': token_id, 'size': size,
                              '_before': {'size': before}})

    def apply_actor_sheet_save(self, token, payload: dict, before: dict = None):
        """Сохранить карточку актёра и записать правку в общий Ctrl+Z."""
        if token is None or not isinstance(payload, dict):
            return
        if before is None:
            before = self._token_edit_snapshot(token)
        after = self._token_edit_snapshot(token)
        for field in before:
            if field in payload:
                after[field] = copy.deepcopy(payload[field])
        payload = dict(payload)
        payload['token_id'] = token.id
        self.network_client.send_message(MessageType.UPDATE_TOKEN, payload)
        if before != after:
            name = payload.get('name') or token.name
            undo_data = {'token_id': token.id, **before}
            redo_data = {'token_id': token.id, **after}

            def undo():
                self.network_client.send_message(MessageType.UPDATE_TOKEN,
                                                 undo_data)

            def redo():
                self.network_client.send_message(MessageType.UPDATE_TOKEN,
                                                 redo_data)

            self._push_history(f'правка карточки актёра «{name}»',
                               undo, redo, kind='token')
            self.append_system(f"Карточка актёра «{name}» сохранена")

    def _token_speed_feet(self, token):
        """Скорость токена в футах для подсветки клеток хода."""
        return token_stats.token_speed_ft(token, self._sheet_for_token(token))

    def _library_art_for_token(self, token) -> str:
        """Арт существа из локальной библиотеки по source_id или имени.

        image_path в мире может быть пустым: картинка призванного монстра
        едет отдельным каналом, а в старых сохранениях манифеста ресурсов
        ещё нет. Библиотека есть рядом с игрой, поэтому это последний
        надёжный источник картинки для уже сохранённых токенов.
        """
        records = getattr(self, "lib_records", {}) or {}
        monsters = records.get("monsters") or []
        if not monsters:
            return ""
        source_id = getattr(token, "source_id", None)
        record = next((rec for rec in monsters
                       if source_id and rec.get("id") == source_id), None)
        if record is None and getattr(token, "kind", "actor") != "player":
            # Игроку не подставляем арт монстра только из-за совпадения имени.
            name = str(getattr(token, "name", "") or "").strip()
            if name:
                record = next((rec for rec in monsters
                               if rec.get("name") == name
                               or rec.get("source_name") == name), None)
        if record is None:
            return ""
        return dnd5e_lib.art_path(world_store.get_data_root(), record) or ""

    def _token_image_path(self, token) -> str:
        """Локальная картинка токена: кэш, путь из мира или арт справочника.

        Возвращает исходный путь, даже если файла нет: add_token сам
        нарисует запасной круг, пока не придёт ресурс по сети.
        """
        path = (self.local_assets.get(token.id)
                or getattr(token, "image_path", "") or "")
        if path and os.path.isfile(path):
            return path
        art = self._library_art_for_token(token)
        return art or path

    def _enrich_statblock(self, token):
        """Дополнить статбокс токена недостающими полями из справочника.

        Токены, призванные до появления скоростей и защит в статбоксе,
        открывали карточку с пустыми полями. Дописываем только то, чего
        нет, чтобы не затереть правки мастера.
        """
        block = dict(getattr(token, "statblock", None) or {})
        derived_range = None
        source_id = getattr(token, "source_id", None)
        records = getattr(self, "lib_records", {}) or {}
        record = next((rec for rec in (records.get("monsters") or [])
                       if rec.get("id") == source_id), None)
        if record:
            reference = token_stats.statblock_from_record(record)
            # Только справочные защиты и скорость: атаки и спасброски
            # мастер мог намеренно очистить — их не возвращаем.
            for key in ("speed", "speeds", "resist", "immune",
                        "vulnerable", "condition_immunities"):
                if not block.get(key):
                    block[key] = reference.get(key)
            # Особые атаки появились позже обычных ударов: у существ,
            # призванных раньше, в статблоке их нет — иначе дыхание Бехира
            # не появилось бы в карточке без повторного призыва.
            if not block.get("special_attacks"):
                block["special_attacks"] = reference.get("special_attacks")
            # Чувства нужны токену для дальности обзора. Старым токенам
            # с дефолтными 60 фт подставляем дальность из бестиария, но
            # только если мастер сам не выставил другое число.
            if not block.get("senses"):
                block["senses"] = reference.get("senses")
            derived = vision_rules.settings_from_text(
                block.get("senses") or [])
            current = vision_rules.normalize_range(
                getattr(token, "vision_range", None))
            if (derived.get("vision_range")
                    and current == vision_rules.DEFAULT_VISION_RANGE_FT):
                derived_range = derived["vision_range"]
        if (block == (getattr(token, "statblock", None) or {})
                and derived_range is None):
            return token
        clone = copy.copy(token)
        clone.statblock = block
        if derived_range:
            clone.vision_range = derived_range
        return clone

    def _sheets_changed(self):
        """Лист обновился: холсту надо пересчитать тёмное зрение.

        Словари листов создаются до холста, поэтому окно ищет его в момент
        записи; до создания холста делать нечего.
        """
        canvas = getattr(self, "canvas", None)
        if canvas is not None:
            canvas.refresh_darkvision()

    def _sheet_for_token(self, token):
        """Лист персонажа для токена: свой берём из памяти, чужой — с сервера."""
        if token is None:
            return None
        # У актёра документ — статблок. Лист героя мог прилипнуть к монстру
        # из-за владельца-игрока: тогда удары, кара и ячейки считались по
        # чужому листу, а карточка показывала одни цифры, бросок — другие.
        if token_kind.is_actor(token) and getattr(token, "statblock", None):
            return None
        owner = getattr(token, "owner_id", None)
        # У мастера панели листа нет, поэтому self.character_sheet может не быть.
        # Без этой проверки карточка существа, спасбросок и состояния падали
        # с AttributeError прямо в момент открытия.
        # Сначала лист, открытый на странице этого токена: именно его видит
        # игрок и именно на нём вкладка «Заклинания» считала ячейки. Если
        # вернуть другую копию, окно спишет расход не из того листа и
        # заклинание пройдёт при нуле ячеек.
        pages = getattr(self, "sheet_pages", None)
        widget = pages.sheet_widget(token.id) if pages is not None else None
        page_sheet = getattr(widget, "sheet", None)
        if isinstance(page_sheet, dict):
            self.token_sheets[token.id] = page_sheet
            return page_sheet
        # Свой персонаж: панель игрока — то, что он видит и на чём нажимает
        # «Применить». Скрытая копия в token_sheets могла остаться с полными
        # ячейками, и расход уходил бы в неё: на панели ноль, а каст проходит.
        panel = getattr(self, "character_sheet", None)
        panel_token = getattr(panel, "token_id", None) if panel else None
        own = self._own_token() if owner == self.player_id else None
        panel_is_token = bool(
            panel is not None
            and (panel_token == token.id
                 or (not panel_token and (own is None or own.id == token.id))))
        if (owner == self.player_id and panel_is_token
                and isinstance(getattr(panel, "sheet", None), dict)):
            self.token_sheets[token.id] = panel.sheet
            return panel.sheet
        # Лист, привязанный к токену (новая модель). Он есть и у токена без
        # владельца: мастер создал персонажа и загрузил лист, но ещё не
        # выдал слот — раньше такой токен считался «без листа».
        by_token = getattr(self, "token_sheets", {}) or {}
        sheet = by_token.get(token.id)
        if isinstance(sheet, dict):
            return sheet
        if owner:
            sheet = self.character_sheets.get(owner)
            if isinstance(sheet, dict):
                return sheet
        return None

    def roll_all_initiative(self):
        """Бросить инициативу всем участникам текущей карты (d20 + Ловкость)."""
        tokens = self.map_tokens_for_combat()
        if not tokens:
            self.append_system("На карте нет токенов")
            return
        self.network_client.send_message(
            MessageType.ROLL_INITIATIVE,
            {'token_ids': [t.id for t in tokens]})
        self.append_system(
            f"Бросок инициативы для {len(tokens)} участников — d20 + Ловкость")

    # --- Спасброски от смерти -------------------------------------------
    #
    # Считает сервер: клиент только просит бросок или отмечает кружок
    # руками (когда кубик бросали в Dice Lab или настоящий). Результат
    # приходит всем одинаковый и сразу виден на токене и в листах.
    def on_death_save_roll(self, token_id: str):
        """Бросить спасбросок от смерти за токен."""
        if not token_id:
            return
        self.network_client.send_message(MessageType.DEATH_SAVE, {
            'token_id': token_id, 'action': 'roll'})

    def on_death_save_set(self, token_id: str, index: int, result: str):
        """Ручная отметка кружка: успех, провал или пусто."""
        if not token_id:
            return
        self.network_client.send_message(MessageType.DEATH_SAVE, {
            'token_id': token_id, 'action': 'set',
            'index': int(index), 'result': str(result or ''),
        })

    def on_death_revive(self, token_id: str):
        """Воскрешение мёртвого токена — команда уходит мастеру-серверу."""
        if not token_id:
            return
        self.network_client.send_message(MessageType.DEATH_SAVE, {
            'token_id': token_id, 'action': 'revive'})

    def on_death_stabilize(self, token_id: str):
        """Мастер стабилизирует умирающего: возврат в игру с 1 хитом.

        По 5e стабилизация оставляет на нуле хитов, но за столом это значит
        «выбыл и скучает»; в этом проекте стабилизация и воскрешение
        поднимают с 1 хита, чтобы персонаж мог снова действовать.
        """
        if not token_id:
            return
        self.network_client.send_message(MessageType.DEATH_SAVE, {
            'token_id': token_id, 'action': 'stabilize'})

    def _is_own_token(self, token) -> bool:
        return bool(token is not None
                    and (token.owner_id == self.player_id
                         or token.id == self.player_id))

    def _own_token(self):
        """Токен персонажа текущего игрока — для листа в левой панели."""
        for token in self.game_state.tokens.values():
            if self._is_own_token(token) and token_kind.is_player(token):
                return token
        return None

    def _apply_death_state_to_views(self, token):
        """Показать отметки смерти токена на его листах и страницах."""
        if token is None:
            return
        can_revive = self.player_role == Role.DM
        pages = getattr(self, "sheet_pages", None)
        if pages is not None and token_kind.is_player(token):
            pages.set_death_state(token.id, token.death_saves, token.hp,
                                  can_revive=can_revive)
        widget = getattr(self, "character_sheet", None)
        if (widget is not None and self._is_own_token(token)
                and widget.sheet is not None):
            widget.set_token_id(token.id)
            widget.set_death_state(token.death_saves, token.hp,
                                   can_revive=can_revive)

    def on_death_save_result(self, data: dict):
        """Сервер посчитал спасбросок от смерти — показать у всех."""
        token_id = data.get('token_id')
        token = self.game_state.tokens.get(token_id)
        if token is None:
            token = self.canvas.tokens.get(token_id)
        if token is not None:
            # Сначала холст, потом модель. Холст сравнивает «было/стало»,
            # чтобы запустить тряску смерти: если записать death_saves в
            # токен раньше, он уже считался мёртвым и анимация пропускалась
            # (а токен на клиенте — тот же объект, что и в game_state).
            state = {'token_id': token.id}
            if 'death_saves' in data:
                state['death_saves'] = combat_rules.normalize_death_saves(
                    data.get('death_saves'))
            for field in ('hp', 'hp_max'):
                if data.get(field) is not None:
                    state[field] = data[field]
            self.canvas.update_token(state)
            if 'death_saves' in state:
                token.death_saves = list(state['death_saves'])
            for field in ('hp', 'hp_max'):
                if field in state:
                    setattr(token, field, state[field])
            if data.get('revived') or (token.hp is not None and token.hp > 0):
                self._dead_sound_tokens.discard(token.id)
            self.canvas.update()
            if data.get('hp') is not None:
                self._apply_token_hp_to_sheet(token)
            self._apply_death_state_to_views(token)
            if self.player_role == Role.DM:
                self.update_token_row(token.id)
            dialog = getattr(self, "_actor_sheet_dialog", None)
            if dialog is not None and getattr(dialog, "_token", None) is token:
                dialog.set_death_state(token.death_saves, token.hp)
            save_dialog = getattr(self, "_save_dialog", None)
            if save_dialog is not None and hasattr(save_dialog, "set_death_state"):
                save_dialog.set_death_state(
                    token.id, token.death_saves, token.hp)

        name = data.get('name') or '?'
        if data.get('revived'):
            self.append_system(f"✨ {name} воскрешён — хиты {data.get('hp')}")
        elif data.get('action') == 'roll':
            verdict = "успех" if data.get('success') else "провал"
            self.append_system(
                f"💀 Спасбросок от смерти — {name}: d20 {data.get('d20')} — "
                f"{verdict}; успехов {data.get('successes', 0)}, "
                f"провалов {data.get('failures', 0)}")
            self.show_roll_toast(
                "Спасбросок от смерти", data.get('d20'),
                f"{name} · {verdict} · провалов {data.get('failures', 0)} из 3",
                ROLL_GOOD if data.get('success') else ROLL_BAD)
        else:
            self.append_system(
                f"💀 {name}: спасброски от смерти — "
                f"успехов {data.get('successes', 0)}, "
                f"провалов {data.get('failures', 0)}")
        if data.get('dead'):
            self.append_system(f"☠ {name} мёртв — его можно только воскресить")
            self._play_death_sound(token_id)

    def _play_heal_sound(self):
        """Озвучить лечение; выключенные боевые эффекты глушат и его."""
        if not self.combat_effects_enabled:
            return
        try:
            self.audio.play_sound(self._heal_sound_id, volume=0.7)
        except Exception:
            # Звук — украшение: его сбой не должен ломать бой.
            pass

    def _play_death_sound(self, token_id):
        """Один раз озвучить окончательную смерть токена.

        Выключенные «Эффекты в бою» убирают звук, но токен всё равно
        помечается озвученным: позже смерть не должна звучать задним числом.
        """
        if not token_id or token_id in self._dead_sound_tokens:
            return
        self._dead_sound_tokens.add(token_id)
        if not self.combat_effects_enabled:
            return
        try:
            self.audio.play_sound(self._death_sound_id, volume=0.85)
        except Exception:
            # Звук — украшение: его сбой не должен ломать бой.
            pass

    # --- Звуки ударов ---------------------------------------------------
    def _load_spell_sounds(self, folder: str):
        """Загрузить звуки заклинаний из ``assets/sounds/spells``.

        Ключ — имя файла без суффикса: ``fireball_cast``, ``fireball_boom``,
        ``magic_missile``, ``shield``, ``bless``, ``healing_word``,
        ``cure_wounds``. Поэтому звук можно заменить, не трогая код.
        """
        if not os.path.isdir(folder):
            return
        for name in sorted(os.listdir(folder)):
            stem, ext = os.path.splitext(name)
            if ext.lower() not in (".wav", ".ogg", ".flac", ".mp3"):
                continue
            sound_id = f"spell_{stem}"
            if self.audio.load_sound(os.path.join(folder, name), sound_id,
                                     stem):
                self._spell_sounds[stem.lower()] = sound_id

    def _play_spell_sound(self, fx) -> bool:
        """Озвучить отклик заклинания. False — файла нет, есть запасной звук."""
        if not self.combat_effects_enabled:
            return True
        key = str(fx or "").strip().lower()
        if not key:
            return False
        sound_id = self._spell_sounds.get(key)
        if not sound_id and key.startswith("spell_"):
            # У новых заклинаний своего файла может не быть: даём общий
            # звук каста, чтобы в бою не было тишины.
            sound_id = (self._spell_sounds.get("fireball_cast")
                        or self._spell_sounds.get("magic_missile"))
        if not sound_id:
            return False
        now = time.monotonic()
        if now - float(self._spell_sound_times.get(key, 0.0)) < 0.45:
            return True
        self._spell_sound_times[key] = now
        try:
            self.audio.play_sound(sound_id, volume=0.8)
        except Exception:
            pass
        return True

    def _load_attack_sounds(self, folder: str):
        """Загрузить наборы звуков удара из папки ``assets/sounds/attack``.

        Имя файла задаёт тип: ``miss_1.mp3`` — промах, ``hit_1.mp3`` —
        попадание, ``crit_1.mp3`` — критический удар. Новые файлы с теми же
        префиксами подхватываются сами, без правки кода.
        """
        if not os.path.isdir(folder):
            return
        for name in sorted(os.listdir(folder)):
            stem, ext = os.path.splitext(name)
            if ext.lower() not in (".mp3", ".ogg", ".wav", ".flac"):
                continue
            kind = stem.split("_", 1)[0].lower()
            if kind not in self._attack_sounds:
                continue
            sound_id = f"attack_{stem}"
            if self.audio.load_sound(os.path.join(folder, name), sound_id,
                                     stem):
                self._attack_sounds[kind].append(sound_id)

    @staticmethod
    def attack_sound_kind(data: dict) -> str:
        """Тип звука по результату атаки: промах, попадание или крит."""
        attack = data.get("attack") or {}
        if attack.get("critical"):
            return "crit"
        if attack.get("hit"):
            return "hit"
        return "miss"

    def _play_attack_sound(self, data: dict):
        """Озвучить удар: случайный файл из набора своего типа.

        Undo/повтор атаки звук не дублирует: его проигрывает только
        живой ATTACK_RESULT, а не обратная прокрутка эффекта.
        """
        if not self.combat_effects_enabled:
            return
        try:
            kind = self.attack_sound_kind(data)
            ids = self._attack_sounds.get(kind) or []
            if ids:
                self.audio.play_sound(random.choice(ids))
        except Exception:
            # Звук — украшение: его сбой не должен ломать бой.
            pass

    # --- Ответы сервера ---

    def on_attack_result(self, data: dict):
        """Сервер посчитал атаку: показываем её в чате разбором."""
        # Всё, что пришло из сети, экранируется: имена токенов и пометки
        # урона может задать игрок, а чат рисуется богатым текстом.
        from html import escape
        attack = data.get("attack") or {}
        damage = data.get("damage") or {}
        attacker = escape(str(data.get("attacker_name", "?")))
        target = escape(str(data.get("target_name", "?")))

        lines = [f"<b>⚔ {attacker} → {target}</b>"]
        text = escape(str(data.get("attack_text") or ""))
        if attack.get("critical"):
            colour = "#d9a441"
        elif attack.get("hit"):
            colour = "#a3be8c"
        else:
            colour = "#bf616a"
        # Метка текстом, а не только цветом: цвет не различим для части игроков
        # (правило accessibility из ui-ux-pro-max: не передавать смысл одним цветом).
        if attack.get("critical"):
            text = f"КРИТ! {text}"
        elif not attack.get("hit"):
            text = f"ПРОМАХ. {text}"
        lines.append(f'<span style="color:{colour}">{text}</span>')
        # Благословение и Порча бросают свой кубик на каждый бросок атаки:
        # без строки в чате итог расходился бы с обещанным «+1к4»/«−1к4»,
        # и мастер не понял бы, откуда взялась разница. Оба эффекта могут
        # висеть одновременно — тогда видно обе строки.
        bless = attack.get("bless") or {}
        if bless.get("total"):
            lines.append(
                '<span style="color:#e8d9a0">Благословение: +'
                f'<b>{escape(str(bless.get("total")))}</b> '
                f'({escape(combat_rules.dice_detail_text(bless)
                          or bless.get("die") or "1d4")})</span>')
        bane = attack.get("bane") or {}
        if bane.get("total"):
            lines.append(
                '<span style="color:#b58bd8">Порча: −'
                f'<b>{escape(str(bane.get("total")))}</b> '
                f'({escape(combat_rules.dice_detail_text(bane)
                          or bane.get("die") or "1d4")})</span>')
        # Почему бросок был с преимуществом/помехой и почему попадание стало
        # критическим: состояния объясняют цифры, иначе мастер не понимает
        # результат. Сервер кладёт готовые пометки в condition_notes.
        notes = [escape(str(n)) for n in (data.get("condition_notes") or []) if n]
        if notes:
            lines.append(f'<i style="color:#8e94a3">Состояния: '
                         f'{', '.join(notes)}</i>')
        if damage.get("amount"):
            shown_formula = str(damage.get("breakdown") or
                                damage.get("formula") or "")
            detail = (f'Урон: <b>{escape(str(damage["amount"]))}</b>'
                      f' ({escape(shown_formula)}')
            if damage.get("type"):
                detail += f', {escape(combat_rules.damage_type_label(damage["type"]))}'
            detail += ")"
            if damage.get("note"):
                detail += f' — <i>{escape(str(damage["note"]))}</i>'
            lines.append(detail)
            if data.get("target_hp") is not None:
                hp = escape(str(data["target_hp"]))
                if data.get("target_hp_max"):
                    hp += f'/{escape(str(data["target_hp_max"]))}'
                lines.append(f'Хиты цели: {hp}')
        elif damage.get("error"):
            lines.append(f'<span style="color:#bf616a">Урон: '
                         f'{escape(str(damage["error"]))}</span>')
        elif attack.get("hit"):
            lines.append("Попадание без урона")
        # Кара и метка — отдельные источники урона: в одной сумме их не
        # разобрать, а за столом важно видеть, что именно сработало.
        smite = data.get("smite") or {}
        if smite:
            lines.append(
                '<span style="color:#e8c56b">Божественная кара '
                f'({escape(str(smite.get("slot_level", "?")))} круг): '
                f'{escape(str(smite.get("dice", "")))} → '
                f'<b>{escape(str(smite.get("amount", 0)))}</b> излучением'
                + (' (нежить или исчадие)' if smite.get("extra") else "")
                + "</span>")
        mark = data.get("mark") or {}
        if mark.get("total"):
            # Подпись и тип урона приходят от сервера: у Сглаза это 1к6
            # некротического урона, а не тип оружия метки охотника.
            mark_name = escape(str(mark.get("label") or "Метка охотника"))
            mark_type = escape(str(mark.get("damage_type") or ""))
            lines.append(
                '<span style="color:#b8a6f0">'
                f'{mark_name}: +<b>{escape(str(mark.get("total")))}</b> '
                f'({escape(str(mark.get("formula") or "1d6"))}'
                + (f', {combat_rules.damage_type_label(mark_type)}'
                   if mark_type else "")
                + ")" + "</span>")
        sneak = data.get("sneak") or {}
        if sneak.get("amount"):
            # Скрытая атака — отдельная строка: видно, что кости дал именно
            # плут, а не оружие, и почему они сработали.
            lines.append(
                '<span style="color:#9cc7e8">Скрытая атака '
                f'({escape(str(sneak.get("label") or sneak.get("dice")))}): '
                f'+<b>{escape(str(sneak.get("amount")))}</b>'
                + (f' — {escape(str(sneak.get("reason")))}'
                   if sneak.get("reason") else "")
                + "</span>")
        if data.get("revealed"):
            lines.append('<i style="color:#8e94a3">Скрытность снята: атака '
                         "выдала позицию</i>")
        rage = data.get("rage") or {}
        if rage.get("bonus"):
            bonus_text = escape(str(rage.get("bonus")))
            lines.append(
                '<span style="color:#e2926b">Ярость: +'
                f'<b>{bonus_text}</b> к урону</span>')
        favor = data.get("favor") or {}
        if favor.get("total"):
            # Благоволение срабатывает на каждом попадании: в чате важно
            # видеть, что кость дал именно заклинание, а не оружие.
            lines.append(
                '<span style="color:#e6d79a">Божественное благоволение: +'
                f'<b>{escape(str(favor.get("total")))}</b> '
                f'({escape(str(favor.get("formula") or "1d4"))} '
                'излучением)</span>')
        zephyr = data.get("zephyr") or {}
        if zephyr.get("total"):
            # Удар Зефира — и урон, и прибавка скорости: за столом важно
            # видеть обе части, иначе «а куда делись 30 футов» осталось бы
            # без ответа.
            lines.append(
                '<span style="color:#9fd8c8">Удар Зефира: +'
                f'<b>{escape(str(zephyr.get("total")))}</b> '
                f'({escape(str(zephyr.get("formula") or "1d8"))} силового), '
                'скорость +30 футов до конца хода</span>')
        self.chat_view.append("<br>".join(lines))
        # Карточка броска: итог атаки и урон видны сразу, без захода в чат.
        if attack.get("total") is not None:
            tone = (ROLL_GOLD if attack.get("critical")
                    else ROLL_GOOD if attack.get("hit") else ROLL_BAD)
            card = (f"{data.get('attacker_name', '?')} → "
                    f"{data.get('target_name', '?')} · "
                    + combat_rules.attack_text(attack))
            if damage.get("amount"):
                card += (f" · урон {damage['amount']} "
                         f"({damage.get('formula', '')})").rstrip()
            self.show_roll_toast("Атака", attack.get("total"), card, tone)
        # Анимация поверх холста, как в Foundry: линия удара и число урона.
        label = ""
        if attack.get("critical"):
            label = "КРИТ!"
        elif not attack.get("hit"):
            label = "промах"
        elif damage.get("amount"):
            label = str(damage["amount"])
        self._replay_attack_effect(data, label=label, reverse=False)
        # Звук удара — по типу попадания: промах, удар или крит.
        self._play_attack_sound(data)
        if data.get("smite"):
            # Всполох света поверх цели: кара — не обычный удар.
            self.canvas.show_effect_fx(data.get("target_id"), "divine_smite")
            self._play_spell_sound("divine_smite")
        if data.get("zephyr"):
            # Зефир — отклик на самом следопыте: его ускорило, а не цель.
            self.canvas.show_effect_fx(data.get("attacker_id"),
                                       "spell_transmutation")
            self._play_spell_sound("spell_transmutation")
        banish = (data.get("spell_smite") or {}).get("banished") or {}
        if banish.get("banished"):
            # Изгнание — не урон, а выбывание по правилу «50 хитов или
            # меньше»: у цели свой отклик, чтобы это не спутали с добиванием.
            self.canvas.show_effect_fx(data.get("target_id"), "spell_force")
            self._play_spell_sound("spell_force")
        # Окончательная смерть от удара: тот же звук, что и от третьего
        # провала спасброска: смерть должна звучать одинаково, откуда бы
        # она ни пришла.
        if data.get("target_dead"):
            self._play_death_sound(data.get("target_id"))
        # Запись в общий Ctrl+Z: хиты и кружки до удара пришли с сервера.
        self._remember_attack_result(data)

    def _replay_attack_effect(self, data: dict, *, reverse: bool,
                              label: str = None):
        """Проиграть боевой эффект (отмена/повтор — в обратную сторону)."""
        attack = data.get("attack") or {}
        damage = data.get("damage") or {}
        if label is None:
            if attack.get("critical"):
                label = "КРИТ!"
            elif not attack.get("hit"):
                label = "промах"
            elif damage.get("amount"):
                label = str(damage["amount"])
            else:
                label = ""
        try:
            self.canvas.show_attack_effect(
                data.get("attacker_id"), data.get("target_id"),
                hit=bool(attack.get("hit")), critical=bool(attack.get("critical")),
                miss=not attack.get("hit"), amount=damage.get("amount"),
                label=label, reverse=reverse)
            spell_fx = str(data.get("fx") or "")
            if spell_fx and not reverse:
                self.canvas.show_spell_attack(data)
                self._play_spell_sound(spell_fx)
        except Exception:
            # Анимация — украшение: её сбой не должен ломать бой.
            pass

    def _remember_attack_result(self, data: dict):
        """Записать атаку в историю: Ctrl+Z возвращает хиты и кружки.

        Снимок «до» делает сервер и кладёт в ATTACK_RESULT вместе с «после».
        Откат идёт командой RESTORE_TOKEN: UPDATE_TOKEN доступен только
        мастеру, а отменить свой удар должен и игрок.
        """
        target_id = data.get("target_id")
        hp_before = data.get("target_hp_before")
        hp_after = data.get("target_hp")
        if not target_id or hp_before is None or hp_after is None:
            return
        death_before = list(data.get("death_saves_before") or [])
        death_after = list(data.get("death_saves") or [])
        try:
            hp_before, hp_after = int(hp_before), int(hp_after)
        except (TypeError, ValueError):
            return
        if hp_before == hp_after and death_before == death_after:
            return                      # промах без последствий — нечего менять
        attacker_id = data.get("attacker_id")
        target_name = data.get("target_name") or "?"
        attacker_name = data.get("attacker_name") or "?"
        hp_max = data.get("target_hp_max")

        def snapshot(hp, marks):
            payload = {"token_id": target_id, "hp": hp,
                       "death_saves": list(marks),
                       "attacker_id": attacker_id}
            if hp_max is not None:
                payload["hp_max"] = hp_max
            return payload

        before = snapshot(hp_before, death_before)
        after = snapshot(hp_after, death_after)

        def undo():
            self.network_client.send_message(MessageType.RESTORE_TOKEN, before)
            self._dead_sound_tokens.discard(target_id)
            self.append_system(
                f"↶ Отмена атаки {attacker_name} → {target_name}: "
                f"хиты восстановлены ({hp_before})")
            self._replay_attack_effect(data, reverse=True)

        def redo():
            self.network_client.send_message(MessageType.RESTORE_TOKEN, after)
            self.append_system(
                f"↷ Атака {attacker_name} → {target_name} повторена: "
                f"хиты {hp_after}")
            self._dead_sound_tokens.discard(target_id)
            self._replay_attack_effect(data, reverse=False)
            if combat_rules.death_save_state(death_after)["dead"]:
                self._play_death_sound(target_id)

        self._push_history(
            f"атака {attacker_name} → {target_name}", undo, redo,
            kind="attack")

    def on_save_result(self, data: dict):
        """Сервер бросил спасбросок: вердикт в чат."""
        from html import escape
        name = escape(str(data.get("name", "?")))
        label = escape(str(data.get("ability_label") or data.get("ability", "")))
        total = escape(str(data.get("total")))
        dc = escape(str(data.get("dc")))
        parts = [f"<b>🎲 Спасброск {label} — {name}</b>"]
        # При преимуществе/помехе видна и отброшенная кость: иначе нельзя
        # проверить, что сервер взял нужную из двух.
        parts.append(combat_rules.d20_text(data))
        # Кубики Благословения и Порчи уже вошли в бонус: показываем их
        # отдельно, иначе «+1к4» из карточки эффекта нигде не видно.
        for key, sign, colour, title in (("bless", "+", "#e8d9a0",
                                          "Благословение"),
                                         ("bane", "−", "#b58bd8", "Порча")):
            die = data.get(key) or {}
            if die.get("total"):
                parts.append(
                    f'<span style="color:{colour}">{title}: {sign}'
                    f'{die.get("total")} '
                    f'({combat_rules.dice_detail_text(die) or die.get("die") or "1d4"})'
                    '</span>')
        if dc is not None:
            if data.get("success"):
                parts.append('<span style="color:#a3be8c">Успех</span>')
            else:
                parts.append('<span style="color:#bf616a">Провал</span>')
            parts[-1] += f" против Сл {dc}"
        if data.get("auto_fail"):
            parts.append("<i>автопровал из-за состояния</i>")
        self.chat_view.append(" — ".join(parts[:1]) + ": " + " ".join(parts[1:]))
        # Тот же итог — карточкой: цифру видно всем, не открывая чат.
        if data.get("total") is None:
            return
        dc = data.get("dc")
        if dc is None:
            tone = "neutral"
        else:
            tone = ROLL_GOOD if data.get("success") else ROLL_BAD
        card = (f"{data.get('name', '?')} · "
                f"{data.get('ability_label') or data.get('ability') or ''}"
                f" · d20 {data.get('kept')} {int(data.get('bonus', 0)):+d}")
        if dc is not None:
            card += (" против Сл " + str(dc) + " — "
                     + ("успех" if data.get("success") else "провал"))
        self.show_roll_toast("Спасбросок", data.get("total"), card, tone)

    def on_stealth_check(self, token_id: str = ""):
        """Проверка Скрытности из листа или карточки.

        Бросает сервер: без этого у каждого был бы свой d20 и результат
        расходился бы с картой. Клиент только говорит, чей это токен.
        """
        token_id = str(token_id or "")
        token = None
        if token_id:
            token = (self.game_state.tokens.get(token_id)
                     or self.canvas.tokens.get(token_id))
        else:
            own = [t for t in self.map_tokens_for_combat()
                   if t.owner_id == self.player_id]
            token = (own[0] if own
                     else (self.canvas.tokens.get(self.canvas.selected_token)
                           or self.canvas.tokens.get(
                               self.canvas.turn_token_id or "")))
        if token is None:
            self.append_system(
                "Скрытность: сначала выберите токен на карте")
            return
        if (self.player_role != Role.DM
                and token.owner_id != self.player_id):
            self.append_system("Скрытность: это чужой персонаж")
            return
        self.network_client.send_message(MessageType.STEALTH_CHECK,
                                         {"token_id": token.id})

    def on_stealth_result(self, data: dict):
        """Сервер сравнил бросок с пассивным Восприятием."""
        from html import escape
        name = escape(str(data.get("token_name") or "?"))
        total = int(data.get("total") or 0)
        dc = int(data.get("dc") or 0)
        success = bool(data.get("success"))
        watcher = escape(str(data.get("watcher") or ""))
        if data.get("blocked"):
            # Правило 5e: нельзя прятаться, пока тебя видят. Это не провал
            # броска, а его отсутствие — поэтому и карточки с числом нет.
            self.chat_view.append(
                f"<b>👁 Прятаться нельзя — {name}</b><br>"
                f"Вас видит: {watcher or 'кто-то рядом'} (в его области "
                f"зрения)<br><span style=\"color:#bf616a\">Повернитесь "
                f"или прервите линию взгляда, чтобы уйти в скрытность"
                f"</span>"
                + ("<br><span style=\"color:#b48ead\">Это яркий свет: "
                   "в тусклом свете и в темноте спрятаться можно</span>"
                   if data.get("light_level") == lighting_rules.BRIGHT
                   else ""))
            self.append_system(
                f"Скрытность недоступна: {name} на виду у {watcher or 'цели'}")
            return
        lines = [f"<b>{'🐈‍⬛ Скрыт' if success else '👁 Замечен'} — {name}</b>"]
        detail = escape(str(data.get("breakdown") or f"d20 = {total}"))
        if dc:
            detail += f" против {dc}"
            if watcher:
                detail += f" (Восприятие: {watcher})"
        # Ночью и в тени у наблюдателя помеха на Восприятие (−5): без
        # подписи с видом освещения число в чате выглядело бы необъяснимо
        # маленьким.
        penalty = int(data.get("lighting_penalty") or 0)
        if penalty:
            level = data.get("light_level") or ""
            where = (lighting_rules.light_label(level) if level else "темнота")
            detail += f" · {where} {penalty} Восприятию"
        lines.append(detail)
        if success:
            lines.append('<span style="color:#a3be8c">Следующая атака — '
                         "с преимуществом; после неё скрытность снимается"
                         "</span>")
        else:
            lines.append('<span style="color:#bf616a">Спрятаться не '
                         "удалось: вас заметили</span>")
        self.chat_view.append("<br>".join(lines))
        self.show_roll_toast("Скрытность", total,
                             f"{data.get('token_name', '?')} · {detail}",
                             ROLL_GOOD if success else ROLL_BAD)

    def on_special_attack_request(self, payload: dict):
        """Особая атака существа: сначала цель на холсте, потом бросок.

        Кнопка в карточке и назначенная клавиша идут одним путём: мастер (или
        владелец существа) выбирает цель, а спасброски и урон считает сервер.
        """
        if not isinstance(payload, dict):
            return
        token_id = str(payload.get("token_id") or "")
        attack_id = str(payload.get("attack_id") or "")
        token = (self.game_state.tokens.get(token_id)
                 or self.canvas.tokens.get(token_id))
        if token is None:
            self.append_system("Особая атака: существо не найдено")
            return
        if self.player_role != Role.DM and token.owner_id != self.player_id:
            self.append_system("Особая атака: это чужое существо")
            return
        entry = special_attack_rules.find(token.statblock or {}, attack_id)
        if not entry:
            self.append_system(
                f"«{token.name}»: особая атака не найдена в статблоке")
            return
        # Прошлое ожидание цели и взведённое заклинание снимаем: иначе два
        # режима выбора цели спорили бы за первый клик.
        self._cancel_target_pick()
        self._pending_special_attack = {
            "token_id": token.id,
            "attack_id": entry.get("id") or attack_id,
        }
        name = str(entry.get("name") or "Особая атака")
        self.canvas.start_target_pick(name)
        self.statusBar().showMessage(
            f"{token.name}: {name} — кликните по цели · ПКМ или Esc — отмена",
            6000)

    def on_special_attack_result(self, data: dict):
        """Сервер посчитал спасброски и урон — показываем всем одинаково."""
        if not isinstance(data, dict):
            return
        fx = str(data.get("fx") or "special_attack")
        damage_type = str(data.get("damage_type") or "")
        caster_id = str(data.get("token_id") or "")
        half = bool(data.get("half_on_success"))
        # Дуги от существа к каждой цели: у дыхания видно, кого накрыло, а у
        # проглатывания остаётся только вспышка на цели.
        if fx.endswith("breath") or damage_type:
            for index, row in enumerate(data.get("rows") or []):
                self.canvas.show_spell_arc(
                    caster_id, row.get("token_id"),
                    damage_type=damage_type or "force",
                    delay=0.05 * index)
        for index, row in enumerate(data.get("rows") or []):
            amount = int(row.get("amount") or 0)
            if amount <= 0:
                continue
            self.canvas.show_damage_effect(
                row.get("token_id"), amount,
                damage_type=damage_type,
                saved=bool(half and row.get("save_success")),
                delay=0.12 + 0.05 * index)
        self._play_spell_sound(fx)
        # Разбор в чат: у каждого своя строка с броском спасброска — за
        # столом видно, что кубик действительно бросался за цель.
        from html import escape
        lines = [f"<b>☄ {escape(str(data.get('attacker_name') or '?'))}"
                 f" — {escape(str(data.get('attack_name') or '?'))}</b>"]
        shape = special_attack_rules.shape_label(
            {"shape": data.get("shape"),"size_ft": data.get("size_ft") or 0})
        if shape and shape != "одна цель":
            lines.append(escape(shape))
        damage = data.get("damage") or {}
        if damage.get("formula"):
            shown = str(damage.get("formula"))
            if damage.get("total") is not None:
                shown += f" = {damage['total']}"
            if damage.get("breakdown"):
                shown += f" [{damage['breakdown']}]"
            lines.append(f"Урон {escape(shown)}")
        for row in data.get("rows") or []:
            save = row.get("roll") or {}
            name = escape(str(row.get("name") or "?"))
            bits = [name]
            if save.get("kept") is not None:
                bits.append("спасбросок " + combat_rules.d20_text(save)
                            + (f" против {row['dc']}"
                               if row.get("dc") is not None else ""))
                bits.append("успех" if row.get("save_success") else "провал")
            amount = int(row.get("amount") or 0)
            if amount:
                bits.append(f"−{amount}")
            lines.append(" · ".join(bits))
        self.chat_view.append("<br>".join(lines))
        if damage.get("total"):
            self.show_roll_toast(
                str(data.get("attack_name") or "Особая атака"),
                int(damage.get("total") or 0),
                str(data.get("attacker_name") or ""), ROLL_BAD)

    def on_initiative_rolled(self, data: dict):
        """Показать броски инициативы: видно, кто сколько выбросил."""
        from html import escape
        entries = data.get("entries") or []
        if not entries:
            return
        text = ", ".join(
            f"{escape(str(e.get('name')))}"
            f" {escape(str(e.get('initiative')))}"
            f" (d20 {escape(str(e.get('roll')))}"
            f" {int(e.get('mod', 0)):+d})"
            for e in entries)
        self.chat_view.append(f'<span style="color:#b08d4f">Инициатива: {text}</span>')
        # Карточка на каждого: список читается сверху вниз по инициативе,
        # а старое (низшее в очереди) уступает место, если карточек много.
        # Сервер присылает броски в порядке токенов — сортируем сами.
        ordered = sorted(entries,
                         key=lambda e: int(e.get('initiative') or 0),
                         reverse=True)
        for entry in reversed(ordered):
            if entry.get("initiative") is None:
                continue
            detail = (f"{entry.get('name', '?')} · d20 "
                      f"{entry.get('roll', '?')} "
                      f"{int(entry.get('mod', 0)):+d}")
            self.show_roll_toast("Инициатива", entry.get("initiative"),
                                 detail, "neutral")

    def on_token_moved(self, token_id: str, x: float, y: float):
        """Токен перетащили: отправляем путь, а не только конец.

        Прямая от начала к концу может резать стену, хотя фишку обвели через
        дверь. Сервер проверяет каждый отрезок пути, поэтому такая перетаска
        раньше отклонялась и токен возвращался назад. Холст отдаёт сжатый
        путь (``_compress_drag_trail``), и сервер проходит его шаг за шагом —
        как это видел мастер.
        """
        path = self.canvas.take_token_move_path()
        if len(path) > 2:
            self.network_client.send_message(MessageType.MOVE_TOKEN, {
                'tokens': [{'token_id': token_id, 'x': px, 'y': py}
                           for px, py in path[1:]]})
            return
        self.network_client.send_message(MessageType.MOVE_TOKEN,
                                         {'token_id': token_id, 'x': x, 'y': y})

    def on_tokens_moved(self, moves: list):
        """Холст подвинул пачку токенов — отправляем одним сообщением.

        Одно перемещение вместо десятка: остальные участники применят сдвиг
        сразу целиком, а не увидят, как токены «разъезжаются» по одному.
        """
        if not moves:
            return
        self.network_client.send_message(MessageType.MOVE_TOKEN, {'tokens': moves})

    def on_move_token(self, data: dict):
        """Применить перемещение: как одиночное, так и групповое."""
        moves = data.get('tokens')
        if moves:
            for mv in moves:
                tid, x, y = mv.get('token_id'), mv.get('x'), mv.get('y')
                if tid is None or x is None or y is None:
                    continue
                # Чужое перемещение доезжает плавно; своё — уже стоит под
                # курсором. Так у всех участников токен не телепортируется.
                self.canvas.update_token_position(tid, x, y, animate=True)
                token = self.game_state.tokens.get(tid)
                if token:
                    token.x, token.y = x, y
            return
        self.canvas.update_token_position(data['token_id'], data['x'],
                                         data['y'], animate=True)
        token = self.game_state.tokens.get(data['token_id'])
        if token:
            token.x, token.y = data['x'], data['y']

    # --- Аудио ---
    def add_sound(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Выбрать звук", "", "Аудио (*.mp3 *.wav *.ogg *.flac)")
        if not path:
            return
        self.add_sound_files([path])

    def add_sound_folder(self):
        """Добавить все звуки из папки — как для музыки."""
        folder = QFileDialog.getExistingDirectory(self, "Выбрать папку со звуками")
        if not folder:
            return
        paths = [os.path.join(folder, name) for name in sorted(os.listdir(folder))
                 if name.lower().endswith(('.mp3', '.wav', '.ogg', '.flac'))
                 and os.path.isfile(os.path.join(folder, name))]
        if not paths:
            self.append_system("В папке нет звуковых файлов")
            return
        self.add_sound_files(paths)

    def add_sound_files(self, paths):
        """Загрузить звуки и разослать их остальным.

        Существующие пути пропускаем: иначе один и тот же файл попадал бы в
        список дважды, а его Sound перезагружался бы поверх себя же.
        """
        existing = {os.path.normcase(os.path.abspath(row[2]))
                    for row in self.audio_rows if row[2]}
        failed = 0
        for path in paths:
            key = os.path.normcase(os.path.abspath(path))
            if key in existing:
                continue
            asset_ref = asset_utils.local_asset_ref(path)
            if not asset_ref:
                failed += 1
                continue
            audio_id = generate_id()
            name = os.path.basename(path)
            # load_sound возвращает False, если файл не декодировался (например,
            # mp3 без ffmpeg). Тогда в список его не кладём, а говорим причину —
            # иначе кнопка «Играть» молча ничего не делала бы.
            if not self.audio.load_sound(path, audio_id, name):
                failed += 1
                self.append_system(f"Звук «{name}» не загружен: "
                                   f"{self.audio.last_error}")
                continue
            existing.add(key)
            self.local_assets[audio_id] = path
            self.audio_rows.append((audio_id, name, path))
            self.audio_list.addItem(name)
            self.network_client.send_message(MessageType.ADD_AUDIO, {
                'id': audio_id, 'name': name, 'path': path,
                'type': 'sound', 'asset_upload': asset_ref,
            })
        if failed:
            QMessageBox.warning(self, "Звуки",
                                f"Не удалось загрузить файлов: {failed}.\n"
                                f"Подробности — в системных сообщениях чата.")

    def on_add_audio_net(self, data: dict):
        audio_id = data.get('id')
        if not audio_id:
            return
        self.game_state.audio_tracks[audio_id] = AudioTrack.from_dict(data)
        path = self.local_assets.get(audio_id, data.get('path'))
        if data.get('type') == 'music':
            if audio_id not in self.music_ids and path and self.audio.add_music(path, data.get('name')):
                self.music_ids.append(audio_id)
                if self.player_role == Role.DM:
                    self.music_list.addItem(data.get('name', ''))
            return
        if path:
            self.audio.load_sound(path, audio_id)
        if self.player_role == Role.DM and all(row[0] != audio_id for row in self.audio_rows):
            self.audio_rows.append((audio_id, data.get('name', ''), path or ''))
            self.audio_list.addItem(data.get('name', ''))

    def play_selected_sound(self):
        row = self.audio_list.currentRow()
        if row < 0:
            self.append_system("Звук не выбран")
            return
        self.play_sound_row(row)

    def play_sound_row(self, row: int):
        """Проиграть звук из строки списка — один раз, без очереди."""
        if not (0 <= row < len(self.audio_rows)):
            return
        audio_id, name, _ = self.audio_rows[row]
        # Отправляем на сервер: он разошлёт всем, включая отправителя, поэтому
        # отдельно локально играть не нужно — иначе звук прозвучал бы дважды.
        self.network_client.send_message(MessageType.PLAY_SOUND,
                                         {'sound_id': audio_id, 'name': name})

    def remove_selected_sounds(self):
        """Кнопка «Удалить» — удаляет то, что выделено."""
        if not self.audio_list.selectedIndexes():
            self.append_system("Ничего не выделено")
            return
        self.remove_sounds(self.audio_list.selected_rows())

    def remove_sounds(self, rows: list):
        """Удалить звуки по строкам (кнопкой, ПКМ или Del/X).

        Проходим с конца: удаление сдвигает индексы ниже, и при движении
        вперёд часть строк осталась бы на месте.
        """
        for row in sorted(set(rows), reverse=True):
            if not (0 <= row < len(self.audio_rows)):
                continue
            audio_id, name, _ = self.audio_rows.pop(row)
            # Глушим и выгружаем из памяти: иначе удалённый звук продолжал бы
            # звучать уже после исчезновения из списка.
            self.audio.remove_sound(audio_id)
            self.local_assets.pop(audio_id, None)
            self.audio_list.takeItem(row)

    def on_play_sound_net(self, data: dict):
        self.audio.play_sound(data.get('sound_id'))

    def add_music(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Добавить музыку", "", "Аудио (*.mp3 *.wav *.ogg *.flac)")
        self.add_music_files(paths)

    def add_music_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Выбрать папку с музыкой")
        if folder:
            paths = [os.path.join(folder, name) for name in sorted(os.listdir(folder))
                     if name.lower().endswith(('.mp3', '.wav', '.ogg', '.flac'))
                     and os.path.isfile(os.path.join(folder, name))]
            self.add_music_files(paths)

    def add_music_files(self, paths):
        existing = {os.path.normcase(os.path.abspath(path)) for path in self.audio.playlist}
        for path in paths:
            key = os.path.normcase(os.path.abspath(path))
            if key in existing:
                continue
            asset_ref = asset_utils.local_asset_ref(path)
            if not asset_ref:
                continue
            audio_id = generate_id()
            name = os.path.basename(path)
            if not self.audio.add_music(path, name):
                continue
            existing.add(key)
            self.music_ids.append(audio_id)
            self.local_assets[audio_id] = path
            self.music_list.addItem(name)
            self.network_client.send_message(MessageType.ADD_AUDIO, {
                'id': audio_id, 'name': name, 'path': path,
                'type': 'music', 'asset_upload': asset_ref,
            })

    def remove_music(self):
        row = self.music_list.currentRow()
        if row >= 0:
            self.music_list.takeItem(row)
            self.music_ids.pop(row)
            self.audio.remove_music(row)

    def remove_music_rows(self, rows: list):
        """Удалить выделенные треки плейлиста (ПКМ, Del или X).

        Идём с конца — так индексы в начале списка не сдвигаются под руками,
        и каждый удаляемый ряд указывает на тот же трек, что и до удаления.
        """
        for row in sorted(set(rows), reverse=True):
            if not (0 <= row < len(self.music_ids)):
                continue
            self.music_list.takeItem(row)
            self.music_ids.pop(row)
            self.audio.remove_music(row)

    def move_music(self, delta: int):
        row = self.music_list.currentRow()
        new_row = row + delta
        if row < 0 or new_row < 0 or new_row >= self.music_list.count():
            return
        item = self.music_list.takeItem(row)
        self.music_list.insertItem(new_row, item)
        self.music_list.setCurrentRow(new_row)
        self.audio.move_music(row, new_row)
        self.music_ids.insert(new_row, self.music_ids.pop(row))

    def play_music(self):
        row = self.music_list.currentRow()
        if not self.audio.playlist:
            self.add_music_folder()
            if not self.audio.playlist:
                return
        if row < 0:
            row = self.audio.current_index
        # music_list, music_ids и playlist — три независимых списка, они могут
        # разойтись. Раньше music_ids[row] падал с IndexError внутри слота,
        # и Qt показывал это только в stderr.
        if (not 0 <= row < len(self.audio.playlist)
                or row >= len(self.music_ids)):
            QMessageBox.warning(self, "Музыка",
                                "Трека нет в плейлисте — добавьте заново.")
            return
        if self.audio.play_music(row):
            self.network_client.send_message(MessageType.PLAY_MUSIC, {
                'action': 'play', 'track_id': self.music_ids[row], 'sender_id': self.player_id})

    def on_music_error(self, message: str):
        """Показать причину, по которой музыка не заиграла."""
        self.append_system(f"Музыка: {message}")
        # Одинаковые подряд сообщения не показываем повторно, иначе при
        # восстановлении сессии они вылезали бы пачкой.
        if message == self._last_music_error:
            return
        self._last_music_error = message
        QMessageBox.warning(self, "Музыка", f"Не заиграло:\n{message}")

    def stop_music(self):
        self.audio.stop_music()
        self.now_playing_label.setText("♫ —")
        self.network_client.send_message(MessageType.PLAY_MUSIC, {
            'action': 'stop', 'sender_id': self.player_id})

    def next_music(self):
        self.audio.next_track()
        self.network_client.send_message(MessageType.PLAY_MUSIC, {
            'action': 'next', 'sender_id': self.player_id})

    def toggle_repeat(self):
        enabled = self.repeat_btn.isChecked()
        self.audio.set_repeat(enabled)
        self.repeat_btn.setText("🔁 Повтор: ВКЛ" if enabled else "🔁 Повтор: ВЫКЛ")

    def on_play_music_net(self, data: dict):
        if data.get('sender_id') == self.player_id:
            return
        action = data.get('action')
        if action == 'play':
            track_id = data.get('track_id')
            if track_id in self.music_ids:
                self.audio.play_music(self.music_ids.index(track_id))
            elif self.audio.playlist:
                self.audio.play_music()
        elif action == 'stop':
            self.audio.stop_music()
        elif action == 'next':
            self.audio.next_track()

    # --- Миры: сохранение и загрузка (команды уходят на сервер) ---
    def open_world_dialog(self):
        """Диалог мира: список сохранений с загрузкой, перезаписью и удалением.

        Раньше это были два пункта меню, где имя вводилось вслепую. Теперь
        видно, что сохранено и сколько в мире карт и токенов, а любое
        сохранение можно удалить. Автосохранения — отдельная кнопка: они
        не смешиваются с ручными мирами.
        """
        if self.player_role != Role.DM:
            self.statusBar().showMessage("Миром управляет мастер")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Мир")
        dlg.resize(500, 500)
        layout = QVBoxLayout(dlg)

        layout.addWidget(QLabel("Сохранённые миры:"))
        worlds_list = QListWidget()
        worlds_list.setToolTip("Двойной клик загружает мир всем")
        layout.addWidget(worlds_list, 1)

        hint = QLabel("")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid);")
        layout.addWidget(hint)

        row = QHBoxLayout()
        save_btn = QPushButton("Сохранить как…")
        save_btn.setToolTip("Сохранить сессию под новым именем")
        row.addWidget(save_btn)
        overwrite_btn = QPushButton("Перезаписать")
        overwrite_btn.setToolTip("Записать сессию поверх выбранного мира")
        row.addWidget(overwrite_btn)
        layout.addLayout(row)

        row = QHBoxLayout()
        load_btn = QPushButton("Загрузить")
        load_btn.setToolTip("Загрузить мир всем участникам")
        row.addWidget(load_btn)
        delete_btn = QPushButton("Удалить")
        delete_btn.setToolTip("Удалить сохранение с диска")
        row.addWidget(delete_btn)
        layout.addLayout(row)

        row = QHBoxLayout()
        autosave_btn = QPushButton("Автосохранения…")
        autosave_btn.setToolTip(
            "Отдельные автоматические снимки: восстановить состояние "
            "после падения или неудачной правки")
        autosave_btn.clicked.connect(self.open_autosave_dialog)
        row.addWidget(autosave_btn)
        row.addStretch(1)
        layout.addLayout(row)

        row = QHBoxLayout()
        row.addStretch(1)
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(dlg.accept)
        row.addWidget(close_btn)
        layout.addLayout(row)

        def selected_id():
            item = worlds_list.currentItem()
            return item.data(Qt.UserRole) if item else None

        def refresh():
            """Перечитать сохранения с диска."""
            worlds_list.clear()
            found = world_store.list_worlds(world_store.get_data_root())
            for world_id, info in sorted(found.items()):
                if info.get("broken"):
                    item = QListWidgetItem(f"{world_id}   ·   файл повреждён")
                    item.setToolTip(info.get("error", ""))
                else:
                    item = QListWidgetItem(
                        f"{world_id}   ·   карт {info.get('maps', 0)}, "
                        f"токенов {info.get('tokens', 0)}")
                    item.setToolTip(info.get("path", ""))
                item.setData(Qt.UserRole, world_id)
                item.setData(Qt.UserRole + 1, bool(info.get("broken")))
                worlds_list.addItem(item)
            hint.setText("Сохранений пока нет. Нажмите «Сохранить как…»."
                         if not found else
                         "Двойной щелчок загружает мир всем участникам.")
            # Порядок важен: сначала выделяем первую строку, потом считаем
            # состояние кнопок. Раньше было наоборот — при открытии диалога
            # с непустым списком «Загрузить» и «Перезаписать» оставались
            # серыми, хотя строка уже была выделена.
            if found and worlds_list.currentRow() < 0:
                worlds_list.setCurrentRow(0)
            update_buttons()

        def update_buttons():
            """Кнопки, которым нужен выбранный мир, включаются по выделению."""
            current = worlds_list.currentItem()
            has_sel = current is not None
            broken = bool(current.data(Qt.UserRole + 1)) if current else False
            # Повреждённый мир можно только удалить: загрузить его нельзя,
            # а перезапись стёрла бы остатки, которые ещё можно спасти руками.
            overwrite_btn.setEnabled(has_sel and not broken)
            load_btn.setEnabled(has_sel and not broken)
            delete_btn.setEnabled(has_sel)

        def do_save_as():
            name, ok = QInputDialog.getText(
                dlg, "Сохранить мир", "Название мира:",
                text=getattr(self, "_last_world_id", "my-world"))
            if not ok or not name.strip():
                return
            try:
                world_id = world_store.sanitize_world_id(name.strip())
            except ValueError as exc:
                QMessageBox.warning(dlg, "Такое имя не подойдёт", str(exc))
                return
            if world_id in world_store.list_worlds(world_store.get_data_root()):
                answer = QMessageBox.question(
                    dlg, "Мир существует",
                    f"Мир «{world_id}» уже сохранён. Заменить его?")
                if answer != QMessageBox.Yes:
                    return
            self._last_world_id = world_id
            self.network_client.send_message(MessageType.SAVE_WORLD,
                                             {'world_id': world_id})
            QTimer.singleShot(700, refresh)

        def do_overwrite():
            world_id = selected_id()
            if not world_id:
                return
            answer = QMessageBox.question(
                dlg, "Перезаписать мир",
                f"Записать текущую сессию поверх «{world_id}»?")
            if answer != QMessageBox.Yes:
                return
            self._last_world_id = world_id
            self.network_client.send_message(MessageType.SAVE_WORLD,
                                             {'world_id': world_id})
            QTimer.singleShot(700, refresh)

        def do_load():
            world_id = selected_id()
            if not world_id:
                return
            answer = QMessageBox.question(
                dlg, "Загрузить мир",
                f"Заменить текущее состояние сессией «{world_id}»?\n"
                "Изменения увидят все участники.")
            if answer != QMessageBox.Yes:
                return
            self._last_world_id = world_id
            self.network_client.send_message(MessageType.LOAD_WORLD,
                                             {'world_id': world_id})
            dlg.accept()

        def do_delete():
            world_id = selected_id()
            if not world_id:
                return
            answer = QMessageBox.question(
                dlg, "Удалить сохранение",
                f"Удалить мир «{world_id}» с диска?\n"
                "Текущая сессия останется как есть.")
            if answer != QMessageBox.Yes:
                return
            try:
                world_store.delete_world(world_store.get_data_root(), world_id)
            except (OSError, ValueError) as exc:
                QMessageBox.warning(dlg, "Не удалось удалить", str(exc))
                return
            # Удалённый мир не должен оставаться «последним открытым»:
            # иначе лист персонажа искал бы папку удалённого мира.
            if getattr(self, "_last_world_id", None) == world_id:
                self._last_world_id = None
            refresh()

        worlds_list.itemDoubleClicked.connect(lambda _item: do_load())
        # Смена выделения включает и гасит кнопки, иначе после удаления
        # выбранного мира кнопки оставались бы активными впустую.
        worlds_list.currentRowChanged.connect(lambda _row: update_buttons())
        save_btn.clicked.connect(do_save_as)
        overwrite_btn.clicked.connect(do_overwrite)
        load_btn.clicked.connect(do_load)
        delete_btn.clicked.connect(do_delete)

        refresh()
        dlg.exec_()

    def open_autosave_dialog(self):
        """Открыть отдельный список автосохранений (только мастер)."""
        if self.player_role != Role.DM:
            self.statusBar().showMessage("Автосохранениями управляет мастер")
            return
        dialog = AutosaveDialog(self, world_store.get_data_root())
        dialog.restore_requested.connect(self.restore_autosave)
        dialog.exec_()

    def restore_autosave(self, name: str):
        """Попросить сервер восстановить снимок; ответ придёт как FULL_STATE."""
        self.network_client.send_message(MessageType.RESTORE_AUTOSAVE,
                                         {'name': name})
        self.append_system(f"Восстанавливаю автосохранение «{name}»…")

    # --- Справочник D&D 5e (SRD) ---
    LIB_CATEGORIES = (("monsters", "Монстры"), ("items", "Предметы"),
                      ("spells", "Заклинания"))

    # --- Инициатива и очередь ходов (ДМ) ---
    def apply_token_overlay_options(self, *_):
        """Передать настройки подписей холсту."""
        self.canvas.set_token_overlay_options(
            names=self.token_names_checkbox.isChecked(),
            hp=self.token_hp_checkbox.isChecked(),
            selected_only=self.token_selected_only_checkbox.isChecked())

    #: Кнопки хода — одинакового квадратного размера, без подписей.
    NAV_BUTTON_WIDTH = 46
    NAV_BUTTON_HEIGHT = 32

    def _nav_button(self, icon_name: str, tip: str, slot) -> QPushButton:
        """Кнопка хода: только иконка, фиксированный размер, видимая рамка."""
        button = QPushButton()
        button.setObjectName("nav_button")
        button.setFixedSize(self.NAV_BUTTON_WIDTH, self.NAV_BUTTON_HEIGHT)
        button.setIconSize(QSize(20, 20))
        button.setIcon(tool_icon(icon_name,
                                 getattr(self, "current_theme", "dark")))
        button.setToolTip(tip)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(slot)
        return button

    def build_initiative_section(self, parent_layout):
        """Добавить очередь боя к панели токенов мастера.

        ДМ вносит броски, начинает бой и двигает ход кнопками или клавишей N;
        текущий участник подсвечен жёлтым кольцом у всех игроков.
        """
        group = QGroupBox("Инициатива и очередь ходов")
        l = QVBoxLayout(group)
        l.setContentsMargins(4, 6, 4, 4)

        self.initiative_status = QLabel("Бой не начат")
        self.initiative_status.setWordWrap(True)
        l.addWidget(self.initiative_status)

        # Действия по два в ряд: шесть кнопок в одну строку растягивали
        # панель и вылезали за её край даже на широком экране.
        actions = QGridLayout()
        actions.setHorizontalSpacing(6)
        actions.setVerticalSpacing(6)
        for index, (label, tip, slot) in enumerate((
                ("Инициатива…",
                 "Внести бросок инициативы выбранному токену (d20 + ловкость)",
                 self.set_selected_initiative),
                ("Урон…", "Нанести урон выбранной цели",
                 lambda: self.apply_damage_dialog(heal=False)),
                ("Лечить…", "Восстановить хиты выбранной цели",
                 lambda: self.apply_damage_dialog(heal=True)),
                ("Бросить всем НПС",
                 "Бросить d20 всем добавленным актёрам и существам из "
                 "справочника (кроме токенов игроков)",
                 self.roll_initiative_for_npcs),
                ("Бросить всем",
                 "d20 + Ловкость всем токенам на карте: NPC и игроки "
                 "(у игроков Ловкость берётся с их листа)",
                 self.roll_all_initiative),
                ("Вне боя", "Убрать выбранный токен из очереди ходов",
                 self.clear_selected_initiative))):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.setMinimumHeight(28)
            b.clicked.connect(slot)
            actions.addWidget(b, index // 2, index % 2)
        l.addLayout(actions)

        # Ходы: только иконки, один размер на все три кнопки. Подписи
        # «Назад»/«Следующий» занимали всю ширину колонки и выглядели
        # тяжёлыми — текстовые подсказки остались в тултипах.
        row = QHBoxLayout()
        row.setSpacing(6)
        self.combat_start_btn = QPushButton("⚔ Начать бой")
        self.combat_start_btn.setToolTip(
            "Очередь по инициативе, при равенстве — по имени")
        self.combat_start_btn.setMinimumHeight(30)
        self.combat_start_btn.setMaximumWidth(150)
        self.combat_start_btn.clicked.connect(self.start_combat)
        row.addWidget(self.combat_start_btn)
        row.addStretch(1)
        l.addLayout(row)

        nav = QHBoxLayout()
        nav.setSpacing(6)
        self.turn_prev_btn = self._nav_button(
            "nav_prev", "Предыдущий ход (возврат раунда)",
            lambda: self.request_next_turn(-1))
        nav.addWidget(self.turn_prev_btn)
        self.turn_next_btn = self._nav_button(
            "nav_next", "Передать ход следующему (клавиша N)",
            lambda: self.request_next_turn(1))
        nav.addWidget(self.turn_next_btn)
        nav.addStretch(1)
        self.combat_end_btn = self._nav_button(
            "nav_finish", "Закончить бой: очередь и подсветка хода снимаются",
            self.end_combat)
        self.combat_end_btn.setObjectName("nav_button_end")
        nav.addWidget(self.combat_end_btn)
        l.addLayout(nav)
        parent_layout.addWidget(group, 1)

        self.refresh_initiative()

    def refresh_initiative(self):
        """Обновить общие строки сцены и состояние боевых кнопок."""
        if not hasattr(self, 'initiative_list'):
            return
        gs = self.game_state
        current_id = (gs.combat_order[gs.combat_turn]
                      if gs.combat_active and gs.combat_order
                      and 0 <= gs.combat_turn < len(gs.combat_order) else None)
        old_rows = list(self.initiative_rows)
        prev_row = self.initiative_list.currentRow()
        prev_id = old_rows[prev_row] if 0 <= prev_row < len(old_rows) else None

        # Список отображает очередь сверху, затем остальных участников карты.
        # В бою источник очереди — только сервер: локально дописывать токены
        # с инициативой нельзя, иначе они выглядят ходящими, хотя сервер
        # их в бой не включил (другая карта, броска ещё нет).
        ordered_ids = [tid for tid in gs.combat_order
                       if tid in self.canvas.current_map_tokens()]
        initiated = []
        if not gs.combat_active:
            initiated = sorted(
                (t for t in self.canvas.current_map_tokens().values()
                 if t.initiative is not None and t.id not in ordered_ids),
                key=lambda t: (-t.initiative, t.name.lower(), t.id))
        ordered_ids.extend(t.id for t in initiated)
        ordered_set = set(ordered_ids)
        others = sorted((t for t in self.canvas.current_map_tokens().values()
                         if t.id not in ordered_set),
                        key=lambda t: (t.name.lower(), t.id))
        self.initiative_rows = ordered_ids + [t.id for t in others]
        # rebuild_token_rows строит строки заново и сам заполняет
        # initiative_rows. Повторный обход сразу после него уничтожал
        # виджеты-глазки на лету, и Qt падал без трассировки.
        if getattr(self, "_in_rows_rebuild", False):
            return
        self._in_rows_rebuild = True
        try:
            self.rebuild_token_rows()
        finally:
            self._in_rows_rebuild = False
        for row, token_id in enumerate(self.initiative_rows):
            tok = gs.tokens.get(token_id)
            item = self.initiative_list.item(row)
            if tok is None or item is None:
                continue
            label = self.token_row_labels.get(token_id)
            if label:
                prefix = "▶ " if token_id == current_id else ""
                label.setText(prefix + self._token_row_text(tok))
            font = item.font()
            font.setBold(token_id == current_id)
            item.setFont(font)

        ordered = [gs.tokens[tid] for tid in ordered_ids if tid in gs.tokens]
        if not ordered:
            ordered = initiated
        if gs.combat_active and current_id in gs.tokens:
            cur = gs.tokens[current_id]
            self.initiative_status.setText(
                f"Раунд {gs.combat_round} — ход: {cur.name}")
        elif gs.combat_active:
            # Бой идёт, но на этой карте нет его участников (мастер ушёл в
            # соседнюю комнату). Это не «бой не начат» — иначе казалось бы,
            # что бой потерялся, а очередь невидимых токенов — баг.
            self.initiative_status.setText(
                "Бой идёт на другой карте: здесь участников очереди нет")
        elif ordered:
            self.initiative_status.setText(
                f"Готовы к бою: {len(ordered)}. Нажмите «Начать бой»")
        else:
            self.initiative_status.setText(
                "Бой не начат: внесите инициативу участникам")
        in_combat = gs.combat_active and bool(ordered_ids)
        self.combat_start_btn.setEnabled(not gs.combat_active and bool(ordered))
        # Закончить бой можно всегда, пока он идёт: на пустой карте иначе
        # не оставалось бы ни одной активной кнопки боя.
        self.combat_end_btn.setEnabled(gs.combat_active)
        self.turn_prev_btn.setEnabled(in_combat)
        self.turn_next_btn.setEnabled(in_combat)

        if prev_id is not None and prev_id in self.initiative_rows:
            self.initiative_list.blockSignals(True)
            self.initiative_list.setCurrentRow(self.initiative_rows.index(prev_id))
            self.initiative_list.blockSignals(False)
        # setCurrentRow сбрасывает прежнее выделение строк — возвращаем
        # группу, иначе рамка и Shift-выделение пропадали на каждом ходу.
        self._restore_token_list_selection()
        # Ползунок размера показывает токен, который сейчас выбран.
        self._sync_token_size_control()

    def _selected_initiative_token(self):
        """Токен выбранной строки очереди или None (со статусом вместо исключения)."""
        row = self.initiative_list.currentRow()
        if row < 0 or row >= len(self.initiative_rows):
            self.statusBar().showMessage("Сначала выберите токен в списке", 2500)
            return None
        return self.initiative_rows[row]

    def on_initiative_row_clicked(self, item):
        """Строка очереди -> подсветить токен на холсте (как в списке токенов)."""
        row = self.initiative_list.row(item)
        if row < 0 or row >= len(self.initiative_rows):
            return
        self.canvas.deselect_map()
        self.canvas.selected_token = self.initiative_rows[row]
        self.canvas.update()
        self._sync_token_size_control()

    def set_selected_initiative(self):
        """Внести бросок инициативы выбранному токену (диалог с числом)."""
        token_id = self._selected_initiative_token()
        if token_id is None:
            return
        tok = (self.game_state.tokens.get(token_id)
               or self.canvas.tokens.get(token_id))
        current = tok.initiative if tok and tok.initiative is not None else 10
        label = f"Инициатива «{tok.name if tok else token_id}» (−10…99):"
        value, ok = QInputDialog.getInt(self, "Инициатива", label,
                                        current, -10, 99)
        if not ok:
            return
        self.network_client.send_message(
            MessageType.SET_INITIATIVE, {'token_id': token_id,
                                         'initiative': value})

    def apply_damage_dialog(self, heal: bool = False):
        """Нанести урон (или лечение) выбранному токену.

        В Foundry это кнопка на карточке атаки: посчитали урон — применили к
        цели. Здесь то же самое через диалог с числом.
        """
        token_id = self._selected_initiative_token() or self.canvas.selected_token
        if not token_id:
            self.statusBar().showMessage(
                "Сначала выберите цель на карте или в списке", 2500)
            return
        tok = (self.game_state.tokens.get(token_id)
               or self.canvas.tokens.get(token_id))
        if not tok:
            return
        current = tok.hp if tok.hp is not None else (tok.hp_max or 0)
        limit = tok.hp_max or 999
        title = "Лечение" if heal else "Урон"
        label = (f"{title} для «{tok.name}» (сейчас "
                 f"{current}" + (f"/{limit}" if tok.hp_max else "") + "):")
        value, ok = QInputDialog.getInt(
            self, title, label, 1, 0, 999)
        if not ok or value <= 0:
            return
        self.network_client.send_message(
            MessageType.APPLY_DAMAGE,
            {'token_id': token_id, 'amount': value, 'heal': heal})

    def roll_initiative_for_npcs(self):
        """Бросить инициативу всем добавленным НПС и актёрам.

        Игроки не входят: у них свой персонаж и свой бросок. Модификатор
        Ловкости берёт сервер из статблока токена или листа — поэтому
        инициатива это d20 + Лов, и правки в карточке актёра тоже влияют.
        """
        if self.player_role != Role.DM:
            self.append_system("Бросать инициативу за всех может только ДМ")
            return

        # Только открытая карта: бой идёт на одной сцене. Раньше кнопка
        # кидала за токенов всех карт сразу, и они потом «ходили» в бою,
        # где стоять не должны.
        targets = [t for t in self.map_tokens_for_combat()
                   if t.kind == "actor" or (t.kind != "player"
                                            and t.owner_id is None)]
        if not targets:
            self.append_system("Нет НПС и актёров для броска инициативы")
            return

        # Бросок делает сервер: раньше клиент сам кидал d20 и искал
        # модификатор в справочнике по имени, из-за чего правки в карточке
        # актёра не учитывались, а существо не из справочника бросало с +0.
        self.network_client.send_message(
            MessageType.ROLL_INITIATIVE,
            {'token_ids': [t.id for t in targets]})

        self.append_system(
            f"Инициатива брошена за {len(targets)} существ "
            f"(d20 + модификатор Ловкости)")

    def clear_selected_initiative(self):
        """Вычеркнуть выбранный токен из очереди ходов."""
        token_id = self._selected_initiative_token()
        if token_id is None:
            return
        self.network_client.send_message(
            MessageType.SET_INITIATIVE, {'token_id': token_id,
                                         'initiative': None})

    def start_combat(self):
        self.network_client.send_message(MessageType.START_COMBAT, {})

    def end_combat(self):
        self.network_client.send_message(MessageType.END_COMBAT, {})

    def request_next_turn(self, step: int = 1):
        """Сдвинуть ход на шаг: +1 вперёд (по умолчанию), −1 назад."""
        if not self.game_state.combat_active:
            self.statusBar().showMessage("Сначала начните бой", 2500)
            return
        self.network_client.send_message(MessageType.NEXT_TURN, {'step': step})

    def hotkey_next_turn(self):
        """Клавиша N: передать ход следующему, но не пока печатаешь в чате."""
        if self._keyboard_in_text_field():
            return
        self.request_next_turn(1)

    def on_dash_used(self, token_id: str):
        """Холст совершил рывок: сервер должен увеличить бюджет."""
        if not token_id:
            return
        self.network_client.send_message(MessageType.DASH,
                                         {'token_id': token_id})

    def on_movement_state(self, data: dict):
        """Сервер прислал потраченные футы/рывок текущего хода."""
        self.canvas.apply_movement_state(data.get('tokens') or [])

    def on_template_cancelled(self):
        """Шаблон закрыт по Esc: заклинание не применяется и не тратит ячейку."""
        pending = getattr(self, "_pending_area_action", None)
        self._pending_area_action = None
        if pending:
            action = use_action.UseAction.from_dict(
                (pending or {}).get("action") or {})
            self.append_system(
                f"{action.note or action.name}: область отменена")

    def on_template_done(self, result: dict):
        """Шаблон на карте подтверждён: отправить область на сервер.

        Сюда приходят и шаблоны мастера «просто посмотреть», у которых нет
        ожидающего заклинания. Такие шаблоны ничего не тратят и не шлют.
        """
        pending = getattr(self, "_pending_area_action", None)
        if not pending:
            return
        self._pending_area_action = None
        if not isinstance(result, dict):
            return
        origin = result.get("origin_cell")
        if not origin:
            self.append_system("Область не поставлена: кликните по клетке карты")
            return
        action = use_action.UseAction.from_dict(pending.get("action") or {})
        sheet = pending.get("sheet") or {}
        actor_id = str(pending.get("actor_id") or action.token_id or "")
        if not actor_id:
            self.append_system(f"{action.note or action.name}: нет действующего токена")
            return
        ok, reason = use_action.can_use(sheet, action)
        if not ok:
            self.append_system(f"{action.note or action.name}: {reason}")
            return
        ok, reason = use_action.consume(sheet, action)
        if not ok:
            self.append_system(f"{action.note or action.name}: {reason}")
            return
        self._persist_action_sheet(action, sheet)
        dc = action.dc
        if dc is None:
            dc = use_action.spell_dc(sheet)
        area = action.area or {}
        payload = {
            "caster_id": actor_id,
            "map_id": result.get("map_id") or self.game_state.current_map_id,
            "shape": str(area.get("shape") or result.get("kind") or "circle"),
            "size_ft": float(area.get("size_ft") or result.get("size_ft") or 20),
            "origin_cell": list(origin),
            "aim_cell": list(result.get("aim_cell") or origin),
            "dc": int(dc),
            "ability": action.ability,
            "range_ft": int(action.range_ft or 0),
            "formula": action.formula,
            "damage_type": action.damage_type,
            "half_on_success": bool(action.half_on_success),
            "spell_name": action.name,
            "spell_id": action.spell_id,
            "source_id": action.source_id,
            # Круг потраченной ячейки: от него растёт пул Сна и Цветного
            # шара и лечение Полного исцеления.
            "slot_level": int((action.consumption or {}).get("level") or 0),
        }
        self.network_client.send_message(MessageType.AREA_DAMAGE, payload)
        self.statusBar().showMessage(
            f"{action.name}: область {payload['shape']} {payload['size_ft']:.0f} фут",
            3000)

    def on_area_result(self, data: dict):
        """Сервер посчитал область: след на карте, цифры и звук."""
        if not isinstance(data, dict):
            return
        fx = str(data.get("fx") or data.get("spell_key") or "")
        if fx == "fireball":
            # Свист снаряда — сразу, взрыв — когда шар долетит. Клиент уже
            # анимирует полёт с той же задержкой, поэтому звук совпадает.
            self._play_spell_sound("fireball_cast")
            QTimer.singleShot(560, lambda: self._play_spell_sound("fireball_boom"))
        elif fx:
            self._play_spell_sound(fx)
        self.canvas.show_area_result(data)
        targets = [row for row in (data.get("targets") or [])
                   if isinstance(row, dict)]
        pool = bool(data.get("pool"))
        conditions_only = bool(data.get("conditions_only"))
        if targets and any(int(row.get("delta") or 0) < 0 for row in targets):
            self._play_attack_sound({'attack': {'hit': True}})
        # Пул: выпавшая сумма и потраченная — разные числа. Игрок должен
        # видеть, сколько выпало, иначе непонятно, почему заснули не все.
        rolled = int(data.get("pool_rolled") or data.get("total") or 0)
        spent = int(data.get("total") or 0)
        if not targets:
            if pool:
                self.statusBar().showMessage(
                    f"{data.get('spell_name', 'Заклинание')}: пул "
                    f"{rolled} хитов — в области никого",
                    3500)
            else:
                self.statusBar().showMessage(
                    f"{data.get('spell_name', 'Область')}: в области никого нет",
                    3000)
            return
        if pool or conditions_only:
            # Сон, Цветной шар, Паутина: цифр урона нет, зато важно, кто
            # именно попал под эффект. В чате уже есть полный разбор, а
            # карточка коротко называет первых.
            name = str(data.get("spell_name") or "Заклинание")
            parts = []
            for row in targets:
                labels = [str(value) for value in (row.get("applied") or [])]
                if labels:
                    parts.append(f"{row.get('name', '?')}: {labels[0]}")
                else:
                    parts.append(f"{row.get('name', '?')}: спасбросок пройден")
            shown = parts[:3]
            detail = ", ".join(shown)
            if len(parts) > len(shown):
                detail += f" и ещё {len(parts) - len(shown)}"
            if pool:
                detail = (f"пул {rolled}"
                          + (f", потрачено {spent}" if spent != rolled else "")
                          + " · " + detail)
            self.show_roll_toast(name, "—", detail, ROLL_BAD)
            self.statusBar().showMessage(f"{name}: {detail}", 4500)
            return
        # Каждому пострадавшему — карточка с числом: область бьёт сразу
        # нескольких, и в чате эти цифры легко потерять.
        for row in reversed(targets):
            try:
                delta = int(row.get("delta") or 0)
            except (TypeError, ValueError):
                continue
            if not delta:
                continue
            if delta > 0:
                detail = f"{row.get('name', '?')} · лечение"
                tone = ROLL_GOOD
            else:
                saved = bool(row.get("save_success"))
                detail = (f"{row.get('name', '?')} · "
                          + ("спасбросок пройден" if saved
                             else "спасбросок провалён"))
                # Урон остаётся красным и при пройденном спасброске:
                # половина урона — всё равно урон, а не успех.
                tone = ROLL_BAD
            self.show_roll_toast(
                str(data.get("spell_name") or "Область"), abs(delta),
                detail, tone)

    def hotkey_dash(self):
        """Клавиша C/С: рывок текущего токена, если фокус не в поле ввода.

        Для действия это важнее, чем для отмены: буква «с» в пустом поле
        чата не должна превращаться в рывок. Поэтому проверяем сам тип
        виджета, а не только доступность отмены ввода.
        """
        if self._keyboard_in_text_field():
            return
        self.canvas.dash_action()

    def on_initiative_net(self, data: dict):
        """Сервер прислал состояние боя: очередь, раунд, чей ход."""
        gs = self.game_state
        gs.combat_active = bool(data.get('combat_active'))
        order = [str(t) for t in data.get('order', []) or []]
        gs.combat_order = order
        try:
            turn = int(data.get('turn', 0))
        except (TypeError, ValueError):
            turn = 0
        gs.combat_turn = max(0, min(turn, len(order) - 1)) if order else 0
        try:
            gs.combat_round = max(1, int(data.get('round', 1)))
        except (TypeError, ValueError):
            gs.combat_round = 1

        current_id = (order[gs.combat_turn]
                      if gs.combat_active and order else None)
        self.canvas.set_combat_mode(gs.combat_active)
        self.canvas.set_turn_context(gs.combat_round, gs.combat_turn, current_id)
        self.refresh_initiative()

        # Автосмена текущего токена: выделить фигуру, чей ход наступил.
        # Один раз на ход — повторные обновления состояния не дёргают выбор.
        if current_id != self._turn_focus:
            self._turn_focus = current_id
            if self.player_role == Role.DM and current_id:
                self._focus_turn_token(current_id)

    def _focus_turn_token(self, token_id: str):
        """Выделить на холсте и в списках токен, чей ход идёт сейчас."""
        if not (self.game_state.tokens.get(token_id)
                or self.canvas.tokens.get(token_id)):
            return
        self.canvas.deselect_map()
        self.canvas.selected_token = token_id
        self.canvas.update()
        self.on_token_selected(token_id)
        if hasattr(self, 'initiative_rows') and token_id in self.initiative_rows:
            row = self.initiative_rows.index(token_id)
            self.initiative_list.blockSignals(True)
            self.initiative_list.setCurrentRow(row)
            self.initiative_list.blockSignals(False)

    def build_compendium_tab(self, tabs):
        """Вкладка «Справочник»: библиотека SRD, карточки, призыв, броски."""
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(4, 4, 4, 4)

        self.lib_records = dnd5e_lib.load_library(world_store.get_data_root())
        # Служебные записи Foundry (Эффект, Категория, Зачарование) — не
        # предметы инвентаря. Скрываем их только в интерфейсе:
        # из items.json они нужны повторному импорту SRD.
        if "items" in self.lib_records:
            self.lib_records["items"] = content_audit.visible_items(
                self.lib_records["items"])
        total = sum(len(self.lib_records[k]) for k, _ in self.LIB_CATEGORIES)

        self.lib_category = QComboBox()
        for key, label in self.LIB_CATEGORIES:
            self.lib_category.addItem(
                f"{label} ({len(self.lib_records[key])})", key)
        self.lib_category.currentIndexChanged.connect(self.lib_refresh)

        self.lib_search = QLineEdit()
        self.lib_search.setPlaceholderText(
            "Поиск по названию (рус. или англ.)…")
        self.lib_search.textChanged.connect(self.lib_refresh)

        # Две колонки: список слева, карточка и её действия справа. Раньше
        # описание лежало под списком и съедало высоту — список ужимался до
        # пяти строк, а карточка всё равно требовала прокрутки.
        body = QHBoxLayout()
        body.setSpacing(8)
        left_col = QVBoxLayout()
        left_col.setSpacing(4)
        left_col.addWidget(self.lib_category)
        left_col.addWidget(self.lib_search)

        # Предметы из справочника можно перетащить прямо на токен: так
        # мастер применяет зелье, не выдавая его в инвентарь. Монстры и
        # заклинания остаются обычными строками без переноса.
        self.lib_list = ItemDragList()
        self.lib_list.setDragDropMode(QAbstractItemView.DragOnly)
        self.lib_list.setAcceptDrops(False)
        self.lib_list.setIconSize(QSize(30, 30))
        # Длинные имена обрезаются многоточием: горизонтальная полоса
        # прокрутки в узкой колонке с двумя панелями только мешала.
        self.lib_list.setWordWrap(False)
        self.lib_list.setTextElideMode(Qt.ElideRight)
        self.lib_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.lib_list.set_payload_builder(self._lib_drag_payload)
        self.lib_list.currentItemChanged.connect(self.lib_on_selection)
        left_col.addWidget(self.lib_list, 1)
        # Список уже — карточка шире: в ней сетка карточек действий.
        body.addLayout(left_col, 3)

        self.lib_detail = QWidget()
        self.lib_detail.setObjectName("lib_detail")
        detail = QVBoxLayout(self.lib_detail)
        detail.setContentsMargins(0, 0, 0, 0)
        detail.setSpacing(6)
        self.lib_view = QTextBrowser()
        self.lib_view.setObjectName("lib_card_view")
        self.lib_view.setOpenExternalLinks(False)
        self.lib_view.setMinimumWidth(260)
        detail.addWidget(self.lib_view, 1)

        # Действия записи — компактные карточки: название и эффект, а не
        # строка-кнопка на каждое число. Сетка переносится по ширине.
        self.lib_actions_scroll = QScrollArea()
        self.lib_actions_scroll.setObjectName("lib_actions_scroll")
        self.lib_actions_scroll.setWidgetResizable(True)
        self.lib_actions_scroll.setFrameShape(QFrame.NoFrame)
        self.lib_actions_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff)
        self.lib_actions_scroll.setMaximumHeight(240)
        self.lib_actions_host = CardBoard()
        self.lib_actions_host.resized.connect(self._lib_fit_actions)
        self.lib_actions_scroll.setWidget(self.lib_actions_host)
        self.lib_actions_scroll.setVisible(False)
        detail.addWidget(self.lib_actions_scroll)

        # Правая колонка появляется только при выборе записи, и вместе с ней
        # панель становится вдвое шире: иначе двум колонкам тесно.
        self.lib_detail.setVisible(False)
        self._lib_show_hint()
        body.addWidget(self.lib_detail, 4)
        l.addLayout(body, 1)

        # Действия — сеткой в две колонки, а не строкой из трёх кнопок.
        # В ряд подписи требуют 835 px, и страница справочника не могла
        # сузиться: сплиттер отбирал место у правой панели, когда карточка
        # разворачивалась. Две колонки укладываются почти вдвое уже, а
        # в развёрнутом справочнике кнопки просто растягиваются.
        actions = QGridLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setHorizontalSpacing(6)
        actions.setVerticalSpacing(6)
        self.lib_summon_btn = QPushButton("Призвать как токена")
        self.lib_summon_btn.setToolTip(
            "Поставить монстра на текущую карту: имя, хиты и КД — из статблока")
        self.lib_summon_btn.clicked.connect(self.lib_summon)
        actions.addWidget(self.lib_summon_btn, 0, 0)
        self.lib_give_btn = QPushButton("Выдать токену…")
        self.lib_give_btn.setToolTip(
            "Отдать предмет или заклинание персонажу — они попадут в лист")
        self.lib_give_btn.clicked.connect(self.lib_give_to_player)
        actions.addWidget(self.lib_give_btn, 0, 1)
        self.lib_share_btn = QPushButton("Карточка в чат")
        self.lib_share_btn.setToolTip("Краткая карточка в чат всем")
        self.lib_share_btn.clicked.connect(self.lib_share_card)
        actions.addWidget(self.lib_share_btn, 1, 0, 1, 2)
        l.addLayout(actions)

        if not total:
            hint = QLabel("Библиотека пуста. Импорт SRD:")
            hint.setWordWrap(True)
            l.addWidget(hint)
            code = QLabel("vtt library import-dnd5e <dnd5e.zip>")
            code.setTextInteractionFlags(Qt.TextSelectableByMouse)
            l.addWidget(code)
            self.lib_summon_btn.setEnabled(False)
            self.lib_share_btn.setEnabled(False)

        self.lib_refresh()
        tab_index = tabs.addTab(w, section_icon("reference", SECTION_FALLBACK["reference"]), "")
        tabs.setTabToolTip(tab_index, "Справочник")
        # Двойная ширина принадлежит только справочнику: при уходе в другой
        # раздел колонка снова занимает обычные 560 px, а место отдаётся холсту.
        self._lib_tab_index = tab_index
        if hasattr(tabs, "stack"):
            tabs.stack.currentChanged.connect(self._on_lib_section_changed)

    def lib_current_records(self):
        key = self.lib_category.currentData()
        return self.lib_records.get(key, [])

    def lib_refresh(self, *_):
        needle = self.lib_search.text().strip().casefold()
        self.lib_list.clear()
        for rec in self.lib_current_records():
            # Ищем и по русскому имени, и по английскому source_name:
            # исходное имя сохраняется в записи как ключ поиска.
            if needle and needle not in rec.get("name", "").casefold() \
                    and needle not in str(rec.get("source_name") or "").casefold():
                continue
            icon = item_icon(rec, 30)
            item = QListWidgetItem(icon, rec.get("name", "?"))
            item.setData(Qt.UserRole, rec)
            if rec.get("kind") == "item":
                if use_action.can_apply_to_token(rec):
                    item.setToolTip(
                        "Перетащите на токен, чтобы применить предмет")
            self.lib_list.addItem(item)

    @staticmethod
    def _lib_drag_payload(row_item):
        """Только настоящие предметы: монстров и заклинания не тащим."""
        rec = row_item.data(Qt.UserRole) if row_item is not None else None
        if not isinstance(rec, dict) or rec.get("kind") != "item":
            return None
        payload = drag_payload(rec, source="library", list_id="library")
        # Из справочника тащим только расходники с готовым действием:
        # зелья, масла, яды и алхимический огонь. Оружие остаётся списком.
        return payload if use_action.can_apply_to_token(rec) else None

    def lib_on_selection(self, current, _previous=None):
        rec = current.data(Qt.UserRole) if current else None
        # Чистим карточки прошлого действия
        self.lib_actions_host.clear()
        if not rec:
            self._lib_show_hint()
            self.lib_actions_scroll.setVisible(False)
            # Ширину при пустом выборе не трогаем: карточка ещё может
            # понадобиться, а дёргающаяся панель мешает работать.
            return
        self.lib_view.setHtml(self._lib_card_html(rec))
        self._lib_fill_action_cards(rec)
        self._lib_show_detail(True)

    # --- две колонки справочника ---------------------------------------
    def _lib_show_hint(self):
        """Пустая правая колонка объясняет, как её заполнить."""
        if not hasattr(self, "lib_view"):
            return
        self.lib_view.setHtml(
            '<div style="color:#77736b;">Выберите запись слева — описание '
            'и действия откроются здесь.</div>')

    def _lib_show_detail(self, visible: bool):
        """Показать карточку справа и расширить панель под две колонки."""
        if not hasattr(self, "lib_detail"):
            return
        visible = bool(visible)
        self.lib_detail.setVisible(visible)
        if visible == getattr(self, "_lib_detail_shown", None):
            return
        self._lib_detail_shown = visible
        self._lib_apply_panel_width(visible)

    def _on_lib_section_changed(self, index: int):
        """Ушли из справочника — вернуть колонке обычную ширину."""
        if not hasattr(self, "lib_detail"):
            return
        if index == getattr(self, "_lib_tab_index", -1):
            if getattr(self, "_lib_detail_shown", False):
                self._lib_apply_panel_width(True)
            return
        if getattr(self, "_lib_detail_shown", False):
            self._lib_apply_panel_width(False)

    def _lib_apply_panel_width(self, expanded: bool):
        """Двойная ширина левой колонки, пока открыта карточка справа.

        Список и карточка в одной колонке не помещаются: 480 px хватает
        только на список. Панель растёт вдвое, холст забирает остаток;
        переход в другой раздел возвращает исходные 560 px.
        """
        splitter = getattr(self, "splitter", None)
        if splitter is None or splitter.count() < 3:
            return
        panel = splitter.widget(0)
        if panel is None:
            return
        sizes = splitter.sizes()
        if len(sizes) < 3:
            return
        if expanded:
            # Двойная ширина, но холст не сжимаем ниже его настоящего
            # минимума (600 px), а правую панель не трогаем вовсе: раньше
            # в расчёте стояло 320 px, запрос был невыполним, и Qt добирал
            # разницу из чата — правый столбец сужался при развороте карточки.
            right = sizes[2] if len(sizes) > 2 else 300
            canvas = splitter.widget(1)
            canvas_min = max(self.CANVAS_MIN_WIDTH,
                             canvas.minimumSize().width()
                             if canvas is not None else 0)
            room = (self.width() or 1280) - right - canvas_min
            limit = max(self.LEFT_PANEL_MAX_WIDTH,
                        min(self.LEFT_PANEL_MAX_WIDTH * 2, room))
            panel.setMaximumWidth(limit)
            # Свёрнутая до рейки панель (56 px) не разворачивается сама:
            # ширину держит SectionRail, а не карточка.
            if sizes[0] < 200:
                return
            target = min(limit, max(sizes[0], sizes[0] * 2))
            # Место даёт только холст: правую панель разворот карточки не
            # трогает. Если холст уже у минимума, карточка вырастет на то,
            # что есть, — и ни одна полоса не сожмётся сама.
            self._set_panel_width(0, int(target))
            self._left_panel_width = self.splitter.sizes()[0]
            return
        panel.setMaximumWidth(self.LEFT_PANEL_MAX_WIDTH)
        # При закрытии карточки лишнюю ширину забирает обратно холст.
        if sizes[0] > self.LEFT_PANEL_MAX_WIDTH:
            self._set_panel_width(0, self.LEFT_PANEL_MAX_WIDTH)
        self._left_panel_width = min(getattr(self, "_left_panel_width",
                                             self.LEFT_PANEL_MAX_WIDTH),
                                     self.LEFT_PANEL_MAX_WIDTH)

    @staticmethod
    def _lib_card_html(rec: dict) -> str:
        def text(value):
            """Convert imported Foundry values into displayable plain text."""
            if value is None:
                return ""
            if isinstance(value, dict):
                value = (value.get("value") or value.get("subtype")
                         or value.get("label") or "")
            if isinstance(value, (list, tuple, set)):
                return ", ".join(filter(None, (text(part) for part in value)))
            return str(value)

        def esc(value):
            return (text(value).replace("&", "&amp;")
                    .replace("<", "&lt;").replace(">", "&gt;")
                    .replace("\n", "<br>"))

        rows = []
        kind = rec.get("kind")
        if kind == "monster":
            # Записи из старого foundry_index.json приходили без abilities/mods,
            # и карточка падала с KeyError прямо при открытии. Считаем модификаторы
            # сами, если их нет: карточка должна открыться хоть как-то.
            raw_abilities = rec.get("abilities") or {}
            mods = dict(rec.get("mods") or {})

            def _score(abbr):
                value = raw_abilities.get(abbr)
                if isinstance(value, dict):
                    value = value.get("value")
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return 10

            for abbr in ("str", "dex", "con", "int", "wis", "cha"):
                score = _score(abbr)
                mods.setdefault(abbr, (score - 10) // 2)
            abilities = ", ".join(
                f"{abbr.upper()} {_score(abbr)} ({int(mods[abbr]):+d})"
                for abbr in ("str", "dex", "con", "int", "wis", "cha"))
            skills = ", ".join(f"{s.get('name', '?')} {int(s.get('mod') or 0):+d}"
                               for s in rec.get("skills", []))
            rows = [
                f"<b>{esc(rec.get('name') or 'Без названия')}</b> — {esc(rec.get('size'))} "
                f"{esc(rec.get('ctype'))}, CR {rec.get('cr')} "
                f"({rec.get('xp', 0)} опыта)",
                f"КД {rec.get('ac')}, ХП {rec.get('hp')} "
                f"({esc(rec.get('hp_formula'))}), инициатива "
                f"{int(mods['dex']):+d}",
                "Скорость: " + (", ".join(rec.get("speeds", [])) or "—"),
                esc(abilities),
                ("Спасброски: " + esc(", ".join(
                    f"{s['name']} {s['mod']:+d}" for s in rec.get("saves", [])))
                 if rec.get("saves") else ""),
                ("Навыки: " + esc(skills) if skills else ""),
                "Пассивное восприятие " + str(rec.get("passive")),
                ("Уязвимость к урону: " + esc(rec["damage_vulnerabilities"])
                 if rec.get("damage_vulnerabilities") else ""),
                ("Сопротивление урону: " + esc(rec["damage_resistances"])
                 if rec.get("damage_resistances") else ""),
                ("Иммунитет к урону: " + esc(rec["damage_immunities"])
                 if rec.get("damage_immunities") else ""),
                ("Иммунитет к состояниям: " + esc(rec["condition_immunities"])
                 if rec.get("condition_immunities") else ""),
                ("Языки: " + esc(", ".join(rec.get("languages", [])))
                 if rec.get("languages") else ""),
                ("Чувства: " + esc(", ".join(rec.get("senses", [])))
                 if rec.get("senses") else ""),
                ("Окружение: " + esc(rec.get("environment"))
                 if rec.get("environment") else ""),
                ("Легендарные действия: " + str(rec["legendary_actions"])
                 if rec.get("legendary_actions") else ""),
                ("Легендарное сопротивление: " + str(rec["legendary_resistances"])
                 if rec.get("legendary_resistances") else ""),
                ("Действия в логове: инициатива " +
                 str(rec.get("lair_initiative") or 20)
                 if rec.get("has_lair_actions") else ""),
            ]
            for a in rec.get("attacks", []):
                bonus = f"{a['bonus']:+d}" if a.get("bonus") is not None else ""
                rng = f", дистанция {a['range']}" if a.get("range") else ""
                rows.append(
                    f"• <b>{esc(a['name'])}</b>: атака {bonus}{rng}; "
                    f"урон {esc(a.get('damage')) or '—'}")
            for f in rec.get("features", []):
                # Текст особенности показываем целиком: раньше он резался на
                # 200 символах, и в справочнике почти каждое описание
                # обрывалось на «…» посередине фразы. Карточка и так в
                # прокрутке, так что длина ей не мешает.
                feature_text = esc(f.get("text", "")).replace("\n", "<br>")
                activation = (f" ({esc(f['activation'])})"
                              if f.get("activation") else "")
                mech = f.get("mechanics") or {}
                mech_bits = [
                    f"спасбросок {s['ability']}"
                    + (f" DC {s['dc']}" if s.get("dc") else "")
                    for s in mech.get("saves", [])]
                if mech.get("damage"):
                    mech_bits.append(mech["damage"])
                if mech.get("healing"):
                    mech_bits.append(mech["healing"])
                prefix = (esc(", ".join(mech_bits)) + ". ") if mech_bits else ""
                rows.append(
                    f"• <b>{esc(f['name'])}</b>{activation}: "
                    f"{prefix}{feature_text}")
        elif kind == "item":
            rows = [f"<b>{esc(rec.get('name') or 'Без названия')}</b> — {esc(rec.get('itype'))}"]
            meta = [text(m) for m in (rec.get("subtype"), rec.get("rarity"),
                                      rec.get("weight"), rec.get("price"))]
            meta = [m for m in meta if m]
            if meta:
                rows.append(esc(", ".join(meta)))
            if rec.get("formulas"):
                rows.append(esc("; ".join(rec["formulas"])))
            if rec.get("activity_damage"):
                rows.append(esc(rec["activity_damage"]))
            if rec.get("heal"):
                rows.append(esc(rec["heal"]))
            if rec.get("range"):
                rows.append(esc(rec["range"]))
            if rec.get("uses"):
                uses_text = rec["uses"]
                if uses_text.startswith("заряды: "):
                    uses_text = "Заряды: " + uses_text[len("заряды: "):]
                else:
                    uses_text = uses_text[:1].upper() + uses_text[1:]
                rows.append(esc(uses_text))
            if rec.get("save_dc") is not None:
                rows.append(f"Спасбросок: Сл {rec['save_dc']}")
            if rec.get("attack_bonus") is not None:
                rows.append(f"Атака: {rec['attack_bonus']:+d}")
            rows.append(esc(rec.get("description", "")))
        else:  # spell
            head = (f"<b>{esc(rec.get('name') or 'Без названия')}</b> — {esc(rec.get('level_text'))}, "
                    f"{esc(rec.get('school'))}")
            if rec.get("concentration"):
                head += " (концентрация)"
            if rec.get("ritual"):
                head += " (ритуал)"
            rows = [head]
            meta = [text(m) for m in (rec.get("activation"), rec.get("range"),
                                      rec.get("duration"), rec.get("components"))]
            rows.append(esc(", ".join(m for m in meta if m)))
            if rec.get("materials"):
                rows.append("Материалы: " + esc(rec["materials"]))
            if rec.get("attack"):
                rows.append("Атака: " + esc(rec["attack"]))
            if rec.get("save"):
                rows.append("Спасбросок: " + esc(rec["save"]))
            if rec.get("damage"):
                rows.append(esc(rec["damage"]))
            if rec.get("scaling"):
                rows.append(esc(rec["scaling"]))
            rows.append(esc(rec.get("description", "")))
        return "<br>".join(r for r in rows if r)

    _DAMAGE_FORMULA_RE = re.compile(r"(\d+d\d+(?:[+-]\d+)?)")

    @classmethod
    def _lib_formulas(cls, text) -> list:
        """Формулы из строки вида «2d6+3 огонь; 1d8 излучением»."""
        parts = []
        for segment in str(text or "").split(";"):
            match = cls._DAMAGE_FORMULA_RE.search(segment)
            if match:
                parts.append(match.group(1))
        return parts

    def _lib_action_entries(self, rec: dict) -> list:
        """Действия записи для карточек: имя, эффект и готовые броски.

        Одно действие — одна карточка. У дракона было девять кнопок
        во всю ширину панели («Атака: Укус +10», «Урон: 2d10+6», …);
        теперь три карточки с эффектом в подписи.
        """
        kind = rec.get("kind")
        entries = []

        def add(title, bits, tooltip, rolls):
            rolls = [roll for roll in rolls if roll[1]]
            # Спасбросок — главный бросок такого действия: цель бросает его
            # до урона, поэтому он и становится кликом по карточке.
            rolls.sort(key=lambda roll: 0 if roll[0] == "Спасбросок" else 1)
            title = title or "Действие"
            # Формулы и числа в подписи, а не «слово: слово». Действие без
            # чисел всё равно получает подпись: пустая строка выглядит браком.
            subtitle = " · ".join(part for part in bits if part) or "эффект"
            # Старые записи библиотеки не хранят текст действия: без
            # подсказки карточка всё равно должна объяснять свои числа.
            if not tooltip:
                tooltip = f"{title}: {subtitle}" if subtitle else title
            entries.append({"title": title, "subtitle": subtitle,
                            "tooltip": tooltip, "rolls": rolls})

        if kind == "monster":
            for attack in rec.get("attacks", []) or []:
                name = attack.get("name") or "Атака"
                bonus = attack.get("bonus")
                damages = self._lib_formulas(attack.get("damage"))
                bits = []
                if bonus is not None:
                    bits.append(f"атака {bonus:+d}")
                if damages:
                    bits.append(" + ".join(damages))
                if attack.get("range"):
                    bits.append(str(attack["range"]))
                rolls = []
                if bonus is not None:
                    rolls.append(("Атака", f"1d20{bonus:+d}", None))
                rolls += [("Урон", formula, None) for formula in damages]
                add(name, bits, attack.get("text"), rolls)
            # Механика activities особенностей: спасбросок с DC
            # (цель бросает d20 против DC) и урон из damage.parts.
            for feature in rec.get("features", []) or []:
                mech = feature.get("mechanics") or {}
                bits, rolls = [], []
                for save in mech.get("saves", []) or []:
                    ability = save.get("ability") or ""
                    if save.get("dc") is not None:
                        bits.append(f"спасбросок {ability}, Сл {save['dc']}")
                        rolls.append(("Спасбросок", "1d20", save["dc"]))
                    elif ability:
                        bits.append(f"спасбросок {ability}")
                for formula in self._lib_formulas(mech.get("damage")):
                    bits.append(formula)
                    rolls.append(("Урон", formula, None))
                for formula in self._lib_formulas(mech.get("healing")):
                    bits.append("лечение " + formula)
                    rolls.append(("Лечение", formula, None))
                if feature.get("activation"):
                    bits.append(str(feature["activation"]))
                add(feature.get("name") or "Особенность", bits,
                    feature.get("text"), rolls)
        elif kind == "spell":
            damages = self._lib_formulas(rec.get("damage"))
            bits, rolls = [], []
            if damages:
                bits.append(" + ".join(damages))
                rolls += [("Урон", formula, None) for formula in damages]
            # Спасбросок бывает двух видов: с числовым Сл (свиток, предмет)
            # и «Сл заклинателя» — тогда числа в карточке нет, но бросок всё
            # равно нужен: игрок подставит свой Сл.
            if rec.get("save_dc") is not None:
                bits.append(f"Спасбросок Сл {rec['save_dc']}")
                rolls.append(("Спасбросок", "1d20", rec["save_dc"]))
            elif rec.get("save"):
                bits.append("Спасбросок: " + str(rec["save"]))
                rolls.append(("Спасбросок", "1d20", None))
            add(rec.get("name"), bits, rec.get("description"), rolls)
        else:  # item
            bits, rolls = [], []
            for formula in rec.get("formulas", []) or []:
                # Лечебная формула — это не урон, а восстановление хитов.
                label = ("Лечение" if re.match(r"\s*(лечение|heal)",
                                                str(formula), re.IGNORECASE)
                         else "Урон")
                found = self._lib_formulas(formula)
                if found:
                    bits.append(f"{label.lower()} {found[0]}")
                    rolls.append((label, found[0], None))
            for field, label in (("activity_damage", "Урон"),
                                 ("heal", "Лечение")):
                for formula in self._lib_formulas(rec.get(field)):
                    bits.append(f"{label.lower()} {formula}")
                    rolls.append((label, formula, None))
            if rec.get("attack_bonus") is not None:
                bonus = rec["attack_bonus"]
                bits.append(f"атака {bonus:+d}")
                rolls.append(("Атака", f"1d20{bonus:+d}", None))
            if rec.get("save_dc") is not None:
                bits.append(f"Спасбросок Сл {rec['save_dc']}")
                rolls.append(("Спасбросок", "1d20", rec["save_dc"]))
            add(rec.get("name"), bits, rec.get("description"), rolls)
        return entries

    @staticmethod
    def _lib_roll_label(roll) -> str:
        """Подпись броска в меню карточки: «Атака 1d20+10», «Урон 2d6»."""
        label, expr, dc = roll
        if dc is not None:
            return f"{label} {expr} против Сл {dc}"
        return f"{label} {expr}"

    def _lib_fill_action_cards(self, rec: dict):
        """Компактные карточки действий вместо кнопок во всю ширину.

        Клик — главный бросок действия (атака или спасбросок), остальные
        броски лежат в контекстном меню. Действие без броска остаётся
        справкой: подсветки при наведении у неё нет.
        """
        entries = self._lib_action_entries(rec)[:10]
        for entry in entries:
            card = ActionCard(entry["title"], entry["subtitle"],
                              tooltip_html(entry["tooltip"]), self.lib_actions_host)
            rolls = entry["rolls"]
            if rolls:
                def run(roll, title=entry["title"]):
                    label, expr, dc = roll
                    note = (title if label in ("Атака", "Спасбросок")
                            else f"{title} · {label.lower()}")
                    self.send_roll(expr, dc=dc, note=note)

                card.set_actionable(True)
                # run передаём значением: замыкание на переменную цикла
                # привязало бы все карточки к последнему действию списка.
                card.clicked.connect(
                    lambda _=False, r=rolls[0], act=run: act(r))
                if len(rolls) > 1:
                    card.set_alternatives(
                        [(self._lib_roll_label(roll),
                          lambda r=roll, act=run: act(r))
                         for roll in rolls])
                hint = "Клик — " + self._lib_roll_label(rolls[0]).lower()
                if len(rolls) > 1:
                    hint += ", правый клик — другие броски"
                base = card.toolTip()
                card.setToolTip((base + "<br><br>" if base else "")
                                + f"<i>{hint}</i>")
            else:
                card.set_actionable(False)
                if not card.toolTip():
                    card.setToolTip(entry["title"])
            self.lib_actions_host.add_card(card)
        self.lib_actions_scroll.setVisible(bool(entries))
        self._lib_fit_actions()

    def _lib_fit_actions(self):
        """Высота области карточек — по числу строк сетки, не больше 240 px.

        Фиксированная высота не давала бы карточкам дышать: при двух
        колонках их три ряда, при трёх — два. Считаем по сетке и ставим
        ровно столько, сколько нужно, чтобы не было лишней прокрутки.
        """
        host = getattr(self, "lib_actions_host", None)
        scroll = getattr(self, "lib_actions_scroll", None)
        if host is None or scroll is None or not host.cards():
            return
        need = host.grid.sizeHint().height()
        if need <= 0:
            return
        height = max(96, min(240, need))
        if abs(scroll.height() - height) > 2:
            scroll.setFixedHeight(height)

    def lib_give_to_player(self):
        """Мастер отдаёт запись справочника игроку.

        В Foundry это перетаскивание предмета на актёра. У нас — выбор
        игрока из списка: сервер кладёт предмет в лист и рассылает
        обновление, поэтому предмет виден и мастеру, и игроку.
        """
        if self.player_role != Role.DM:
            self.append_system("Выдавать предметы может только мастер")
            return
        current = self.lib_list.currentItem()
        if not current:
            self.append_system("Выберите предмет или заклинание в списке")
            return
        rec = current.data(Qt.UserRole) or {}
        kind = rec.get("kind")

        # Кому выдаём: токены игроков, у которых есть лист. Раньше выбор был
        # только по игроку, и при нескольких персонажах предмет уходил не тому.
        candidates = []
        for token in self.game_state.tokens.values():
            if getattr(token, "kind", "actor") != "player":
                continue
            sheet = self.token_sheets.get(token.id)
            if not isinstance(sheet, dict):
                sheet = self.character_sheets.get(token.owner_id)
            if not isinstance(sheet, dict):
                continue
            owner = self.game_state.players.get(token.owner_id)
            label = token.name or "Персонаж"
            if owner is not None:
                label += f" — {owner.nickname}"
            elif token.owner_id:
                label += " — лист без игрока"
            candidates.append((token.id, token.owner_id or '', label))
        if not candidates:
            self.append_system(
                "Нет токенов игроков с листом: сначала загрузите лист "
                "в токен персонажа")
            return
        names = [label for _tid, _owner, label in candidates]
        choice, ok = QInputDialog.getItem(
            self, "Выдать токену", "Кому:", names, 0, False)
        if not ok:
            return
        token_id, owner_id, label = candidates[names.index(choice)]

        if kind == "spell":
            payload = {'player_id': owner_id, 'token_id': token_id,
                       'spell': rec}
            self.network_client.send_message(MessageType.GRANT_SPELL, payload)
            self.append_system(
                f"Заклинание «{rec.get('name')}» отправлено: {label}")
        else:
            qty, ok = QInputDialog.getInt(
                self, "Сколько выдать",
                f"«{rec.get('name')}» — сколько штук?", 1, 1, 99)
            if not ok:
                return
            payload = {'player_id': owner_id, 'token_id': token_id,
                       'item': rec, 'qty': qty}
            self.network_client.send_message(MessageType.GRANT_ITEM, payload)
            self.append_system(
                f"«{rec.get('name')}» ×{qty} отправлено: {label}")

    def lib_share_card(self):
        """Краткая карточка выбранной записи — в чат всем участникам."""
        current = self.lib_list.currentItem()
        if not current:
            return
        rec = current.data(Qt.UserRole)
        text = self._lib_card_html(rec)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        self.network_client.send_message(MessageType.CHAT_MESSAGE, {
            'message': f"[Справочник] {text}", 'timestamp': self.now(),
            'msg_type': 'normal',
        })

    def lib_summon(self):
        """Поставить монстра из справочника на текущую карту (ДМ)."""
        current = self.lib_list.currentItem()
        if not current:
            return
        rec = current.data(Qt.UserRole)
        if rec.get("kind") != "monster":
            self.append_system("Призывать можно только монстров")
            return
        m = self.canvas.current_map
        if not m:
            self.append_system("Сначала откройте карту")
            return
        scale = dnd5e_lib.SIZE_TOKEN_SCALE.get(rec.get("size_code"), 1.0)
        # Имя прогоняем через перевод ещё раз: запись могла быть импортирована
        # до того, как словарь пополнился, и тогда в ней лежит английское имя.
        # Призываем туда, куда сейчас смотрит мастер, а не в геометрический
        # центр карты: на больших картах фиксированная точка оказывалась
        # далеко за пределами экрана.
        spawn_x, spawn_y = self.canvas.view_center_snapped()
        # Дальность обзора из чувств существа: «тёмное зрение 90 фт» значит
        # 90, а не молчаливые 60. Угол читается только если в описании
        # сказано про круговой обзор.
        vision_kwargs = vision_rules.settings_from_text(rec.get("senses"))
        token = Token(
            id=generate_id(),
            name=dnd5e_lib.translate_name(rec.get("source_name") or rec["name"]),
            image_path="",
            x=spawn_x, y=spawn_y,
            size=scale, owner_id=self.player_id, visible=True, map_id=m.id,
            hp=rec.get("hp"), hp_max=rec.get("hp_max"), ac=rec.get("ac"),
            kind="actor",
            # Статблок и защита едут вместе с токеном: без них карточка мастера
            # открывалась пустой, а сопротивления не работали в бою.
            statblock=token_stats.statblock_from_record(rec),
            resist=list(rec.get("damage_resistances") or []),
            immune=list(rec.get("damage_immunities") or []),
            vulnerable=list(rec.get("damage_vulnerabilities") or []),
            # Существо из справочника — без оправы: у него есть арт, и рамка
            # только спорила с ним. Рамки остаются у токенов с картинками.
            frame=token_frames.NO_FRAME,
            source_id=rec.get("id"),
            **vision_kwargs)
        payload = token.to_dict()
        art = dnd5e_lib.art_path(world_store.get_data_root(), rec)
        ref = asset_utils.local_asset_ref(art) if art else ""
        payload['asset_upload'] = ref or None
        self.network_client.send_message(MessageType.ADD_TOKEN, payload)
        self.append_system(
            f"Призван {token.name}: КД {rec.get('ac')}, ХП {rec.get('hp')}")

    def summon_my_actor(self, actor_id: str, wx: float = None,
                        wy: float = None):
        """Поставить актёра из «Моих актёров» на карту.

        Статблок, хиты и КД берутся из сохранённой карточки: токен ведёт
        себя как призванный из справочника монстр.
        """
        page = getattr(self, 'my_actors_page', None)
        actor = page.actor(actor_id) if page is not None else None
        if not isinstance(actor, dict):
            return
        m = self.canvas.current_map
        if m is None:
            self.append_system("Сначала откройте карту")
            return
        token = token_from_actor(actor)
        token.id = generate_id()
        token.map_id = m.id
        token.owner_id = self.player_id
        token.visible = True
        path = actor_image(actor) or self.preloaded_actor_images.get(actor_id, "")
        token.image_path = path or ""
        if wx is None or wy is None:
            # Без перетаскивания — центр экрана мастера.
            wx, wy = self.canvas.view_center_snapped()
        token.x, token.y = wx, wy
        payload = token.to_dict()
        ref = asset_utils.local_asset_ref(path) if path else ""
        payload['asset_upload'] = ref or None
        self.network_client.send_message(MessageType.ADD_TOKEN, payload)
        self.append_system(f"Призван актёр «{token.name}»")

    # --- Хиты и КД токена (ДМ, диалог) ---
    def on_token_hp_requested(self, token_id: str):
        tok = (self.game_state.tokens.get(token_id)
               or self.canvas.tokens.get(token_id))
        if tok is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Хиты и КД — {tok.name}")
        form = QFormLayout(dialog)
        hp = QSpinBox()
        hp.setRange(0, 9999)
        hp.setValue(tok.hp if tok.hp is not None else 0)
        hp_max = QSpinBox()
        hp_max.setRange(0, 9999)
        hp_max.setValue(tok.hp_max if tok.hp_max is not None else 0)
        hp_max.setSpecialValueText("нет")
        ac = QSpinBox()
        ac.setRange(0, 99)
        ac.setValue(tok.ac if tok.ac is not None else 10)
        form.addRow("Хиты:", hp)
        form.addRow("Максимум хитов:", hp_max)
        form.addRow("КД:", ac)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec_() != QDialog.Accepted:
            return
        self.network_client.send_message(MessageType.UPDATE_TOKEN, {
            'token_id': token_id,
            'hp': hp.value(),
            'hp_max': hp_max.value() or None,
            'ac': ac.value(),
        })

    # --- Чат и кубики ---
    def send_chat(self):
        text = self.chat_input.text().strip()
        if not text:
            return
        self.chat_input.clear()

        if text.startswith('/tell'):
            parts = text.split(maxsplit=2)
            if len(parts) < 3:
                self.append_system("Использование: /tell Имя сообщение")
                return
            self.network_client.send_message(MessageType.CHAT_MESSAGE, {
                'message': parts[2], 'timestamp': self.now(),
                'msg_type': 'whisper', 'recipient': parts[1],
            })
        elif text.startswith('/roll'):
            self.roll_dice(text)
        else:
            self.network_client.send_message(MessageType.CHAT_MESSAGE, {
                'message': text, 'timestamp': self.now(), 'msg_type': 'normal',
            })

    def quick_roll(self, cmd: str):
        self.roll_dice(cmd)

    # --- кубики -------------------------------------------------------
    def open_dice(self):
        """Иконка D20 в верхней полосе.

        С включёнными 3D-кубиками открывает отдельный лоток, иначе —
        раскрывает чат с кнопками и ставит фокус на D20.
        """
        if self.enable_3d:
            self.open_dice_window()
            return
        tabs = getattr(self, "right_tabs", None)
        if tabs is not None and tabs.currentIndex() != self._chat_tab_index:
            tabs.toggle_section(self._chat_tab_index)
        panel = getattr(self, "dice_buttons", None)
        if panel is not None:
            panel.focus_first()

    def open_dice_window(self):
        """Открыть физические кубики Dice Lab отдельным окном."""
        if not self.enable_3d:
            self.open_dice()
            return
        if self._dice_window is None:
            try:
                from dice_lab.app import DiceLabWindow
            except Exception as exc:      # нет panda3d или ресурсов
                QMessageBox.warning(
                    self, "3D-кубики недоступны",
                    "Не удалось загрузить Dice Lab.\n\n"
                    "Установите зависимости:\n"
                    "python -m pip install -r dice_lab/requirements.txt\n\n"
                    f"Ошибка: {exc}")
                return
            window = DiceLabWindow()
            window.setWindowIcon(dice_icon())
            # Закрыли крестиком — окно удаляется: в Dice Lab закрытие
            # выключает звук, и повторное show() оставило бы его немым.
            window.setAttribute(Qt.WA_DeleteOnClose, True)
            window.rolled.connect(self.on_dice_lab_roll)
            window.destroyed.connect(self._forget_dice_window)
            self._dice_window = window
        self._dice_window.show()
        self._dice_window.raise_()
        self._dice_window.activateWindow()

    def _forget_dice_window(self, *_args):
        """Окно Dice Lab закрыто и удалено — в следующий раз создаём заново."""
        self._dice_window = None

    def on_dice_lab_roll(self, result: dict):
        """Бросок из отдельного 3D-окна уходит в общий чат всем участникам."""
        self.send_roll_result(result)

    def roll_dice(self, text: str):
        """Разобрать /roll и бросить.

        Синтаксис (как в D&D 5e):
          /roll d20            — как раньше;
          /roll 2d6+3          — формула с модификатором;
          /roll 1d20+5 adv     — преимущество (берётся больший d20);
          /roll 1d20+5 dis     — недостаток;
          /roll 1d20+7 dc 15   — сравнить с DC (в чате видно успех/провал).
        """
        tokens = text.split()[1:] or ['d20']
        mode, dc, expr_tokens = None, None, []
        i = 0
        while i < len(tokens):
            tok = tokens[i].lower()
            if tok in ('adv', 'advantage', 'преимущество'):
                mode = 'adv'
            elif tok in ('dis', 'disadvantage', 'недостаток'):
                mode = 'dis'
            elif tok == 'dc' and i + 1 < len(tokens):
                try:
                    dc = int(tokens[i + 1])
                    i += 1
                except ValueError:
                    self.append_system("После dc нужно число: например dc 15")
                    return
            else:
                expr_tokens.append(tokens[i])
            i += 1
        expr = ''.join(expr_tokens) if len(expr_tokens) == 1 else '+'.join(
            expr_tokens)
        self.send_roll(expr, mode=mode, dc=dc)

    def on_sheet_roll(self, formula: str, note: str):
        """Кнопка на листе персонажа просит бросок — уходит в общий чат."""
        self.send_roll(formula, note=note or "Проверка")

    def on_sheet_attack(self, weapon: dict):
        """Оружие с листа: открыть диалог атаки с предзаполненным уроном.

        Атакующий — токен владельца на текущей карте. Цель выбирает игрок в
        диалоге. Бросок и урон считает сервер через ROLL_ATTACK, поэтому
        учитываются броня цели, крит и сопротивления.
        """
        # Лист страницы знает свой токен. Берём именно его: у мастера
        # открыто несколько листов, а у игрока бывает несколько персонажей.
        preset = dict(weapon)
        token_id = preset.pop("_token_id", None)
        attacker = self.game_state.tokens.get(token_id) if token_id else None
        on_map = [t for t in self.map_tokens_for_combat()
                  if t.id == (attacker.id if attacker else None)]
        allowed = (attacker is not None and on_map
                   and (self.player_role == Role.DM
                        or attacker.owner_id == self.player_id))
        if not allowed:
            own = [t for t in self.map_tokens_for_combat()
                   if t.owner_id == self.player_id]
            if not own:
                self.append_system(
                    f"«{weapon.get('name', 'Оружие')}»: на этой карте у вас "
                    "нет токена")
                return
            attacker = own[0]
        self.open_attack_dialog(attacker_id=attacker.id, preset=preset)

    def on_sheet_effect_for_token(self, token_id: str, kind: str,
                                  formula: str, note: str):
        """Эффект со страницы конкретного токена (лист игрока у мастера)."""
        self.on_sheet_effect(kind, formula, note, token_id=token_id)

    # ------------------------------------------------------------------
    # Единый пайплайн использования (UseAction)
    #
    # Заклинание, зелье, свиток и оружие идут одним путём:
    # проверка → выбор цели → расход → бросок → применение → чат.
    # Вкладки листа только собирают UseAction; правила и сеть — здесь.
    # ------------------------------------------------------------------
    def _use_heal_pool(self, action, actor, sheet) -> bool:
        """Наложение рук: выбрать цель, сколько хитов передать и яд.

        Запас живёт в листе и списывается сервером: клиент только просит
        передать хиты касанием. Цели — только в пределах 5 футов: правила
        разрешают лечить касанием, а не через полкарты.
        """
        if actor is None:
            self.append_system("Наложение рук: на карте нет вашего токена")
            return False
        left = class_features.lay_on_hands_left(sheet)
        maximum = class_features.lay_on_hands_max(sheet)
        if not class_features.has(sheet, class_features.LAY_ON_HANDS):
            self.append_system(
                "Наложение рук: этой особенности нет в листе")
            return False
        if left <= 0:
            self.append_system(
                "Наложение рук: запас исчерпан до долгого отдыха")
            return False
        candidates = []
        preset = action.target_id or ""
        for token in self.map_tokens_for_combat():
            if not token.visible:
                continue
            if token.id == actor.id:
                candidates.append(token)
                continue
            if self._token_distance_ft(actor, token) <= 5.0 + 0.1:
                candidates.append(token)
        if not candidates:
            self.append_system("Наложение рук: рядом никого нет")
            return False
        if preset not in {token.id for token in candidates}:
            preset = actor.id

        dialog = QDialog(self)
        dialog.setWindowTitle("Наложение рук")
        dialog.setMinimumWidth(340)
        layout = QVBoxLayout(dialog)
        hint = QLabel(f"Запас: {left} из {maximum} хитов. "
                      "Передать можно только касанием (5 футов).")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        target_combo = QComboBox()
        for token in candidates:
            mark = f"{token.name} — {token.hp}/{token.hp_max}"
            target_combo.addItem(mark, token.id)
        index = target_combo.findData(preset)
        if index >= 0:
            target_combo.setCurrentIndex(index)
        form.addRow("Кому:", target_combo)
        amount_spin = QSpinBox()
        amount_spin.setRange(0, left)
        amount_spin.setValue(min(left, 10))
        amount_spin.setToolTip("Сколько хитов из запаса передать")
        form.addRow("Хитов:", amount_spin)
        cure_check = QCheckBox("Снять отравление (5 хитов из запаса)")
        cure_check.setToolTip(
            "Наложение рук снимает один яд или одну болезнь за 5 хитов; "
            "в листе пока есть состояние «Отравлен»")
        form.addRow("", cure_check)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok
                                   | QDialogButtonBox.Cancel, dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec_() != QDialog.Accepted:
            return False
        amount = max(0, int(amount_spin.value()))
        cure = bool(cure_check.isChecked())
        cost = class_features.lay_on_hands_cost(amount, cure)
        if cost <= 0:
            self.append_system(
                "Наложение рук: укажите хиты или отметьте отравление")
            return False
        if cost > left:
            self.append_system(
                f"Наложение рук: в запасе только {left} хитов")
            return False
        target_id = str(target_combo.currentData() or actor.id)
        self.network_client.send_message(MessageType.APPLY_DAMAGE, {
            "token_id": target_id,
            "heal": True,
            "amount": amount,
            "heal_style": "lay_on_hands",
            "feature": "lay_on_hands",
            "source_token_id": actor.id,
            "cure": cure,
        })
        names = {token.id: token.name for token in candidates}
        self.statusBar().showMessage(
            f"Наложение рук → {names.get(target_id, target_id)}", 3000)
        self.append_system(
            f"Наложение рук: {names.get(target_id, target_id)} +{amount} хит"
            + (" (снять отравление)" if cure else ""))
        return True

    def _action_sheet(self, action):
        """Лист, из которого взяли предмет/заклинание."""
        token_id = action.token_id
        if token_id:
            token = (self.game_state.tokens.get(token_id)
                     or self.canvas.tokens.get(token_id))
            sheet = self._sheet_for_token(token) if token is not None else None
            if isinstance(sheet, dict):
                return sheet
        # Панель игрока без id токена: его лист уже открыт.
        widget = getattr(self, "character_sheet", None)
        if widget is not None and isinstance(widget.sheet, dict):
            return widget.sheet
        return self.character_sheets.get(self.player_id)

    def _action_actor(self, action):
        """Кто действует: страница токена, свой токен или выбранный."""
        token_id = action.token_id
        if token_id:
            token = (self.game_state.tokens.get(token_id)
                     or self.canvas.tokens.get(token_id))
            if token is None:
                return None
            if (self.player_role != Role.DM
                    and token.owner_id != self.player_id):
                return None
            return token
        own = [t for t in self.map_tokens_for_combat()
               if t.owner_id == self.player_id]
        if own:
            return own[0]
        for candidate in (self.canvas.turn_token_id,
                          self.canvas.selected_token):
            token = self.canvas.tokens.get(candidate or "")
            if token is not None:
                return token
        return None

    def _selected_canvas_tokens(self):
        """Выделенные на холсте токены открытой карты.

        Рамка выделения хранит их в `selected_tokens`, одиночный клик — в
        `selected_token`; для лечения нужны оба, иначе цель, поймавшая
        рамка, терялась.
        """
        canvas = getattr(self, "canvas", None)
        if canvas is None:
            return []
        try:
            ids = list(canvas.selected_token_ids())
        except (TypeError, AttributeError):
            ids = []
        tokens = []
        for token_id in ids:
            token = (canvas.tokens.get(token_id)
                     or self.game_state.tokens.get(token_id))
            if token is None:
                continue
            if not canvas.token_belongs_to_current_map(token):
                continue
            tokens.append(token)
        return tokens

    @staticmethod
    def _ally_from_selection(tokens, actor):
        """Первый выделенный токен, который не сам лекарь."""
        actor_id = getattr(actor, "id", None)
        for token in tokens or []:
            if token.id != actor_id:
                return token
        return None

    def _is_mass_heal(self, action) -> bool:
        """Массовое лечение: заклинание на нескольких существ сразу."""
        return (getattr(action, "kind", "") == "heal"
                and getattr(action, "source", "") == "spell"
                and int(getattr(action, "targets_max", 0) or 0) > 1)

    def _mass_heal_targets(self, action, actor):
        """Все выделенные цели массового лечения в радиусе заклинания.

        Правила 5e: «Массовое лечащее слово» — до 6 существ в пределах
        60 футов, «Массовое лечение ран» — до 6 существ в сфере 30 футов,
        центр которой в пределах дистанции. Поэтому сначала режем по
        дистанции, потом по сфере, и только потом по количеству.
        """
        limit = max(1, int(action.targets_max or 1))
        radius = float(action.radius_ft or 0)
        chosen = self._selected_canvas_tokens()
        # Заклинание, брошенное на токен, всегда входит в цели: иначе бросок
        # на конкретного союзника молча терялся бы при выделении соседей.
        dropped_id = str(getattr(action, "target_id", "") or "")
        if dropped_id and dropped_id not in {token.id for token in chosen}:
            dropped = (self.canvas.tokens.get(dropped_id)
                       or self.game_state.tokens.get(dropped_id))
            if dropped is not None:
                chosen.insert(0, dropped)
        if not chosen:
            # Ничего не выделено — показываем список с галочками: без него
            # массовое лечение молча ушло бы в одного колдуна.
            chosen = self._choose_spell_targets(actor, action)
            if not chosen:
                return []
        tokens = []
        actor_id = getattr(actor, "id", None)
        for token in chosen:
            if token.id != actor_id and action.range_ft:
                if self._token_distance_ft(actor, token) > action.range_ft + 0.1:
                    self.append_system(
                        f"{action.name}: «{token.name}» дальше "
                        f"{int(action.range_ft)} фут — пропущен")
                    continue
            tokens.append(token)
        if radius > 1 and len(tokens) > 1:
            # Центр сферы — выбранная цель ближе всех к колдуну: так в
            # область попадает максимум из того, что игрок выделил.
            center = min(tokens, key=lambda token: self._token_distance_ft(
                actor, token))
            inside = [center]
            for token in tokens:
                if token.id == center.id:
                    continue
                if self._token_distance_ft(center, token) <= radius + 0.1:
                    inside.append(token)
                else:
                    self.append_system(
                        f"{action.name}: «{token.name}» вне сферы "
                        f"{int(radius)} фут — пропущен")
            tokens = inside
        if len(tokens) > limit:
            skipped = ", ".join(token.name for token in tokens[limit:])
            tokens = tokens[:limit]
            self.append_system(
                f"{action.name}: до {limit} целей — пропущены {skipped}")
        alive = []
        for token in tokens:
            if combat_rules.death_save_state(
                    getattr(token, "death_saves", None))["dead"]:
                self.append_system(
                    f"{action.name}: «{token.name}» мёртв — лечение не "
                    "поднимает мёртвых")
                continue
            alive.append(token)
        if not alive:
            self.append_system(f"{action.name}: нет целей для лечения")
        return alive

    def _choose_heal_target(self, actor):
        """Кого лечить: явная страница токена, выделение или выбор из списка.

        Отдельное окно нужно затем, чтобы игрок мог поднять зельем другого
        персонажа: выделить чужой токен на холсте он не может, а лечить
        союзника — обычное действие за столом.
        """
        # Не открываем второй выбор цели поверх первого: при двойном клике
        # или повторном сигнале окна копились стопкой.
        if getattr(self, "_heal_target_prompt", False):
            return None
        candidates = [t for t in self.map_tokens_for_combat() if t.visible]
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        labels = []
        default = 0
        for index, token in enumerate(candidates):
            hp = (f"{token.hp}/{token.hp_max}"
                  if token.hp is not None and token.hp_max is not None else "?")
            labels.append(f"{token.name} — HP {hp}")
            if actor is not None and token.id == actor.id:
                default = index
        self._heal_target_prompt = True
        try:
            choice, ok = QInputDialog.getItem(
                self, "Кого лечить", "Выберите цель:", labels, default, False)
        finally:
            self._heal_target_prompt = False
        if not ok:
            return None
        return candidates[labels.index(choice)] if choice in labels else None

    def _action_target(self, action, actor):
        """Цель действия. None — пусть диалог атаки/спасброска выберет сам."""
        # «На себя» — это сам действующий, даже если на холсте выделен
        # другой токен: Второе дыхание лечит владельца листа.
        if getattr(action, "target_mode", "") == "self" and actor is not None:
            return actor
        # Что бы ни бросили на токен — зелье, метку, благословение, — целью
        # становится именно он, а не случайно выделенный сосед на холсте.
        if action.target_id:
            dropped = (self.game_state.tokens.get(action.target_id)
                       or self.canvas.tokens.get(action.target_id))
            if (dropped is not None
                    and self.canvas.token_belongs_to_current_map(dropped)):
                return dropped
        if action.kind == "utility":
            return actor
        selected_id = self.canvas.selected_token
        selected = self.canvas.tokens.get(selected_id or "")
        if selected is not None and not self.canvas.token_belongs_to_current_map(selected):
            selected = None
        if action.kind in ("heal", "revive", "dispel"):
            # Лечение без явной цели идёт в того, кого выделили на холсте.
            # Смотрим весь набор выделения, а не только последний клик:
            # рамка кладёт цели в selected_tokens, а selected_token при двух
            # и более токенах остаётся пустым — и лечение молча уходило в себя.
            chosen = self._selected_canvas_tokens()
            if action.source == "spell" and chosen:
                ally = self._ally_from_selection(chosen, actor)
                return ally or chosen[0]
            # Страница конкретного токена важнее случайно выделенного соседа,
            # но если это страница самого лекаря, а на карте выбран союзник —
            # лечим союзника. Иначе «Лечащее слово» со своего листа всегда
            # било в себя, даже когда целью был выбран другой персонаж.
            if action.token_id:
                explicit = (self.game_state.tokens.get(action.token_id)
                            or self.canvas.tokens.get(action.token_id))
                if explicit is not None:
                    if (explicit.id == actor.id and chosen):
                        return chosen[0]
                    return explicit
            if chosen:
                return chosen[0]
            return self._choose_heal_target(actor)
        if selected is not None:
            return selected
        if action.kind in ("attack", "save"):
            # Диалоги умеют выбрать цель сами; актёр уже известен.
            return None
        return None

    @staticmethod
    def _sheet_with_hp(sheet, hp):
        """Копия листа с заданными текущими хитами — для отправки на сервер.

        Нужна лечению: на клиенте хиты уже подняты, чтобы полоска и лист
        показали результат сразу, но серверу нельзя присылать вылеченные
        хиты вместе с APPLY_DAMAGE — он прибавил бы ту же лечение второй
        раз. Копию делаем глубокой: правка не должна лечь в живой лист.
        """
        try:
            value = int(hp)
        except (TypeError, ValueError):
            return None
        wire = copy.deepcopy(sheet)
        if not isinstance(wire, dict):
            return None
        vitality = wire.setdefault("vitality", {})
        if not isinstance(vitality, dict):
            return None
        cell = vitality.get("hp-current")
        if not isinstance(cell, dict):
            cell = {}
            vitality["hp-current"] = cell
        cell["value"] = value
        return wire

    def _apply_heal_locally(self, token, amount, sheet):
        """Сразу поднять хиты на клиенте, пока сервер считает своё.

        Полоска на холсте и лист показывают результат без задержки. На
        сервер при этом уходит лист с хитами ДО лечения (см.
        ``_sheet_with_hp``): он поднимает те же хиты сам в APPLY_DAMAGE,
        и одно лечение не должно прибавиться дважды.
        """
        if token is None or not isinstance(sheet, dict):
            return
        try:
            amount = abs(int(amount))
        except (TypeError, ValueError):
            return
        try:
            current = int(token.hp or 0)
        except (TypeError, ValueError):
            current = 0
        try:
            limit = int(token.hp_max) if token.hp_max else 9999
        except (TypeError, ValueError):
            limit = 9999
        new_hp = min(limit, current + amount)
        for store in (self.game_state.tokens, self.canvas.tokens):
            entry = store.get(token.id)
            if entry is not None:
                entry.hp = new_hp
                if new_hp > 0:
                    entry.death_saves = []
        token.hp = new_hp
        if new_hp > 0:
            token.death_saves = []
        vitality = sheet.setdefault("vitality", {})
        if isinstance(vitality, dict):
            cell = vitality.setdefault("hp-current", {})
            if isinstance(cell, dict):
                cell["value"] = new_hp
        self.token_sheets[token.id] = sheet
        owner = getattr(token, "owner_id", None)
        if owner:
            self.character_sheets[owner] = sheet
        # У игрока панель может держать другой словарь того же листа.
        widget = getattr(self, "character_sheet", None)
        if (widget is not None and widget.sheet is not None
                and widget.sheet is not sheet and self._is_own_token(token)):
            other = widget.sheet.setdefault("vitality", {})
            if isinstance(other, dict):
                cell = other.setdefault("hp-current", {})
                if isinstance(cell, dict):
                    cell["value"] = new_hp
        self._refresh_sheet_views(token.id, sheet)
        if (widget is not None and widget.sheet is not None
                and self._is_own_token(token)):
            widget.refresh()
        self._apply_death_state_to_views(token)

    def _refresh_action_sheet(self, action, sheet):
        """Перерисовать лист после списания ячейки/зелья."""
        if not sheet:
            return
        token_id = action.token_id
        if token_id:
            self._refresh_sheet_views(token_id, sheet)
        widget = getattr(self, "character_sheet", None)
        if token_id is None and widget is not None:
            widget.refresh()

    def _persist_action_sheet(self, action, sheet, wire_sheet=None):
        """Сохранить расход: на диск, на сервер и в открытые страницы.

        ``wire_sheet`` — версия листа для сети, если она отличается от
        локальной. Так лечение присылает расход и хиты ДО лечения, а на
        экране остаются уже вылеченные: иначе сервер считал бы лечение
        дважды.
        """
        if not sheet:
            return
        wire = wire_sheet if isinstance(wire_sheet, dict) else sheet
        # Метка локальной правки: эхо сервера, построенное до этого расхода,
        # не должно вернуть потраченное (зелье, заряд, ячейку).
        self._sheet_local_edit_at = time.monotonic()
        token_id = action.token_id
        edits = getattr(self, "_sheet_local_edits", None)
        if not isinstance(edits, dict):
            edits = {}
            self._sheet_local_edits = edits
        edits[token_id or self.player_id] = self._sheet_local_edit_at
        if token_id:
            token = (self.game_state.tokens.get(token_id)
                     or self.canvas.tokens.get(token_id))
            if token is not None:
                self._store_token_sheet(token, sheet, wire)
        else:
            self.on_sheet_changed(sheet)
            # Расход — дискретное действие, ждать 400 мс нельзя: за это
            # время ответ сервера на лечение/эффект успевал вернуть старый
            # лист, и потраченное зелье (или заряд способности) оживало —
            # отсюда «на одно использование больше».
            timer = getattr(self, "_sheet_timer", None)
            if timer is not None:
                timer.stop()
            self._flush_sheet(wire)
        self._refresh_action_sheet(action, sheet)

    def _token_distance_ft(self, first, second) -> float:
        """Расстояние токенов в футах по клеткам текущей карты."""
        map_data = (self.game_state.maps.get(getattr(first, "map_id", None))
                    or self.canvas.current_map)
        if map_data is None:
            return 0.0
        try:
            cell_w, cell_h = map_data.grid_cell_world
            if not cell_w or not cell_h:
                return 0.0
            dx = (float(second.x) - float(first.x)) / float(cell_w)
            dy = (float(second.y) - float(first.y)) / float(cell_h)
            return math.hypot(dx, dy) * 5.0
        except (TypeError, ValueError, AttributeError):
            return 0.0

    def _start_area_template(self, action, actor, sheet) -> bool:
        """Поставить шаблон области для заклинания из листа.

        Один путь и для урона по площади, и для состояний без урона
        (Паутина, Опутывание, Жир), и для пула хитов (Сон, Цветной шар):
        кто попал — решает сервер по геометрии шаблона. Ячейка тратится
        только после подтверждения: Esc не сжигает заклинание.
        """
        if actor is None:
            return False
        area = dict(getattr(action, "area", None) or {})
        shape = str(area.get("shape") or "circle")
        if shape not in self.canvas.TEMPLATE_KINDS:
            self.append_system(
                f"{action.note or action.name}: неизвестная форма области")
            return False
        if not self.canvas.grid or not self.canvas.grid.cols:
            self.append_system(
                f"{action.note or action.name}: нужна сетка карты (Ctrl+G)")
            return False
        size = float(area.get("size_ft") or 20)
        self._pending_area_action = {
            "action": action.to_dict(),
            "sheet": sheet,
            "actor_id": actor.id,
        }
        preset = None
        if shape in ("cone", "line"):
            # Конус и линия растут от колдуна: центр ставим сами,
            # игрок выбирает только направление одним кликом.
            preset = self.canvas._template_cell_of(actor.x, actor.y)
        self.canvas.start_template(shape, size, origin_cell=preset)
        hint = ("наведите направление и кликните" if preset is not None
                else "клик — центр области, второй клик — применить")
        self.statusBar().showMessage(f"{action.name}: {hint}")
        return True

    def _send_cast_spell(self, action, actor, target, sheet,
                         targets=None) -> bool:
        """Волшебная стрела / Лечащее слово: просим сервер посчитать.

        Клиент не бросает кубы и не присылает урон: сервер сам находит
        заклинание в листе, проверяет дистанцию и меняет хиты. Здесь только
        проверка целей, расход ячейки и запрос. ``targets`` — список для
        массового лечения: ячейка тратится одна, а бросает сервер каждому
        отдельно, как и требуют правила.
        """
        rows = [token for token in (targets or []) if token is not None]
        if not rows and target is not None:
            rows = [target]
        if not rows:
            self.append_system(f"{action.note or action.name}: не выбрана цель")
            return False
        for row in rows:
            if not self.canvas.token_belongs_to_current_map(row):
                self.append_system(
                    f"{action.note or action.name}: «{row.name}» на другой карте")
                return False
            distance = self._token_distance_ft(actor, row)
            if (action.range_ft and distance
                    and distance > action.range_ft + 0.1):
                self.append_system(
                    f"{action.note or action.name}: «{row.name}» дальше "
                    f"дистанции {action.range_ft} фут")
                return False
        ok, reason = use_action.consume(sheet, action)
        if not ok:
            self.append_system(f"{action.note or action.name}: {reason}")
            return False
        self._persist_action_sheet(action, sheet)
        payload = {
            "caster_id": actor.id,
            "target_id": rows[0].id,
            "spell_id": action.spell_id,
            "source_id": action.source_id,
            "spell_name": action.name,
            # Круг потраченной ячейки нужен развеиванию магии: эффект не
            # выше этого круга снимается сразу, от сильного — проверка.
            "slot_level": int((action.consumption or {}).get("level") or 0),
        }
        if len(rows) > 1:
            payload["target_ids"] = [row.id for row in rows]
        self.network_client.send_message(MessageType.CAST_SPELL, payload)
        self.statusBar().showMessage(
            f"{action.name} → " + ", ".join(row.name for row in rows), 3000)
        return True

    def _send_chain_spell(self, action, actor, primary, secondaries, sheet) -> bool:
        """Цепная молния: одна ячейка, первая цель и разряды от неё (SRD).

        Вторые цели выбираются в 30 футах от ПЕРВОЙ цели, а не от колдуна,
        поэтому дистанцию заклинания (150 футов) проверяем только для
        первой цели: остальных сверяет сервер по своему радиусу и числу
        разрядов. Расход ячейки — один на всю цепочку.
        """
        if primary is None:
            self.append_system(f"{action.note or action.name}: не выбрана цель")
            return False
        if not self.canvas.token_belongs_to_current_map(primary):
            self.append_system(
                f"{action.note or action.name}: «{primary.name}» на другой карте")
            return False
        distance = self._token_distance_ft(actor, primary)
        if action.range_ft and distance and distance > action.range_ft + 0.1:
            self.append_system(
                f"{action.note or action.name}: «{primary.name}» дальше "
                f"дистанции {action.range_ft} фут")
            return False
        ok, reason = use_action.consume(sheet, action)
        if not ok:
            self.append_system(f"{action.note or action.name}: {reason}")
            return False
        self._persist_action_sheet(action, sheet)
        rows = [primary] + [token for token in (secondaries or [])
                            if token is not None and token.id != primary.id]
        payload = {
            "caster_id": actor.id,
            "target_id": primary.id,
            "spell_id": action.spell_id,
            "source_id": action.source_id,
            "spell_name": action.name,
            "slot_level": int((action.consumption or {}).get("level") or 0),
            "target_ids": [row.id for row in rows],
        }
        self.network_client.send_message(MessageType.CAST_SPELL, payload)
        self.statusBar().showMessage(
            f"{action.name} → " + ", ".join(row.name for row in rows), 3000)
        return True

    def _choose_spell_targets(self, actor, action, center=None, reach_ft=None,
                              limit=None):
        """Выбрать до N существ в радиусе — для Благословения и разрядов.

        Раньше такие заклинания молча применялись к колдуну. Правила SRD
        разрешают выбрать до трёх существ в пределах 30 футов, поэтому
        показываем список с галочками: за столом видно, кто попадёт под
        эффект, и лишних выбрать нельзя.

        ``center`` — от кого отмеряется радиус. У Благословения это колдун,
        а у Цепной молнии — первая цель: её разряды бьют в 30 футах ОТ НЕЁ.
        ``limit`` и ``reach_ft`` нужны цепочке: число разрядов зависит от
        круга ячейки, а радиус берётся из правила заклинания, а не из его
        дистанции (150 футов — это дистанция до первой цели).
        Возвращает список (может быть пустым) или None, если диалог закрыт
        без подтверждения: цепочка без разрядов — нормальный каст, а
        закрытый диалог — отмена.
        """
        center = center if center is not None else actor
        limit = max(1, int(limit or action.targets_max or 1))
        radius = float(action.radius_ft or 0)
        # Сфера (радиус области) и дистанция заклинания — разные вещи:
        # «Массовое лечащее слово» бьёт на 60 футов без области, и раньше
        # список предлагал существ с другого конца карты.
        reach = float(reach_ft) if reach_ft else (
            radius or float(action.range_ft or 0))
        preselect = {token.id for token in self._selected_canvas_tokens()}
        candidates = []
        for token in self.map_tokens_for_combat():
            if not token.visible:
                continue
            if token.id != center.id and reach:
                if self._token_distance_ft(center, token) > reach + 0.1:
                    continue
            candidates.append(token)
        if not candidates:
            self.append_system(f"{action.name}: рядом нет целей")
            return []
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{action.name}: цели")
        dialog.setMinimumWidth(340)
        layout = QVBoxLayout(dialog)
        hint = QLabel(f"Выберите до {limit} существ"
                      + (f" в пределах {int(reach)} футов."
                         if reach else "."))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        widget = QListWidget(dialog)
        for token in candidates:
            hp = (f"{token.hp}/{token.hp_max}"
                  if token.hp is not None and token.hp_max is not None else "?")
            item = QListWidgetItem(f"{token.name} — HP {hp}")
            item.setData(Qt.UserRole, token.id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            # Уже выделенные на холсте существа отмечены сразу: диалог
            # подтверждает выбор, а не заставляет выбирать заново.
            # Уже выделенные на холсте существа отмечены сразу. Самого
            # колдуна отмечаем только там, где радиус идёт от него
            # (Благословение на себя); в разрядах Цепной молнии он
            # не цель по умолчанию, хотя целью быть может.
            default_on = (token.id in preselect
                          or (token.id == actor.id and center is actor))
            item.setCheckState(Qt.Checked if default_on else Qt.Unchecked)
            widget.addItem(item)
        layout.addWidget(widget)
        checked = sum(1 for row in range(widget.count())
                      if widget.item(row).checkState() == Qt.Checked)
        counter = QLabel(f"Выбрано: {checked}")
        layout.addWidget(counter)

        def _on_change(_item=None):
            chosen = [row for row in range(widget.count())
                      if widget.item(row).checkState() == Qt.Checked]
            if len(chosen) > limit:
                # Лишнюю галочку снимаем сразу: заклинание не должно
                # «съесть» ячейку и применить эффект к четвёртому существу.
                widget.item(chosen[-1]).setCheckState(Qt.Unchecked)
                chosen = chosen[:-1]
            counter.setText(f"Выбрано: {len(chosen)}")

        widget.itemChanged.connect(_on_change)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
                                   dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec_() != QDialog.Accepted:
            return None
        chosen_ids = [widget.item(row).data(Qt.UserRole)
                      for row in range(widget.count())
                      if widget.item(row).checkState() == Qt.Checked]
        tokens = [token for token in candidates if token.id in chosen_ids]
        if not tokens:
            self.append_system(f"{action.name}: цели не выбраны")
        return tokens[:limit]

    def _apply_spell_buff(self, action, actor, target, sheet) -> bool:
        """Щит и Благословение: эффект на сервер, отклик — всем клиентам."""
        if int(action.targets_max or 0) > 1:
            targets = self._choose_spell_targets(actor, action)
            if not targets:
                return False
        elif action.target_mode == "self":
            targets = [actor]
        else:
            targets = [target or actor]
        # Метка охотника и Сглаз — только на чужое существо: себя метить
        # нельзя, и без цели заклинание взводится на выбор кликом.
        if action.spell_key in ("hunters_mark", "hex"):
            title = "Сглаз" if action.spell_key == "hex" else "Метка охотника"
            if target is None:
                # Без цели заклинание взводится: игрок кликает по врагу
                # на карте, и только тогда тратится ячейка.
                if self._arm_target_pick(action, sheet):
                    return False
                self.append_system(
                    f"{title}: выберите чужую цель — перетащите заклинание "
                    "на токен или выделите его на карте")
                return False
            if target.id == actor.id:
                self.append_system(
                    f"{title}: себя пометить нельзя — выберите чужое "
                    "существо на карте")
                return False
            targets = [target]
        # Дистанцию проверяем до списания ячейки: сервер тоже откажет, но
        # тогда третий круг Ускорения сгорел бы впустую.
        for token in targets:
            if not action.range_ft or token.id == actor.id:
                continue
            distance = self._token_distance_ft(actor, token)
            if distance and distance > action.range_ft + 0.1:
                self.append_system(
                    f"{action.note or action.name}: «{token.name}» дальше "
                    f"дистанции {action.range_ft} фут")
                return False
        # Правило метки: если помеченная цель умерла, на следующем своём
        # ходу её можно бонусным действием перенести на другое существо,
        # не тратя новую ячейку. Живую цель так не обойти.
        move_mark = (action.spell_key == "hunters_mark"
                     and self._mark_can_move(actor.id))
        if not move_mark:
            ok, reason = use_action.consume(sheet, action)
            if not ok:
                self.append_system(f"{action.note or action.name}: {reason}")
                return False
        self._persist_action_sheet(action, sheet)
        cast_id = generate_id()
        # Временные хиты бросаются до сервера: он проверяет сумму, а не
        # формулу, иначе новое заклинание молча не дало бы щита.
        effects = []
        for effect in action.effects:
            effect = dict(effect)
            if effect.get("type") == "temp_hp" and effect.get("dice"):
                try:
                    result = dice_ext.roll(str(effect["dice"]))
                    effect["amount"] = int(result["total"])
                except Exception:
                    continue
            effects.append(effect)
        if not effects:
            self.append_system(f"{action.note or action.name}: нет эффекта")
            return False
        for token in targets:
            self.network_client.send_message(MessageType.APPLY_EFFECT, {
                "token_id": token.id,
                "effects": effects,
                "from_token_id": actor.id,
                "source": action.source_id or action.name,
                "spell_name": action.name,
                "spell_id": action.spell_id,
                "source_id": action.source_id,
                "cast_id": cast_id,
                # У Благословения проверяется радиус выбора целей, у
                # Ускорения и Доспехов мага — дистанция самого заклинания
                # (30 футов и касание): без неё бафф накладывался через
                # всю карту.
                "max_distance_ft": action.radius_ft or action.range_ft,
            })
        names = ", ".join(token.name for token in targets)
        if action.spell_key == "hunters_mark" and move_mark:
            self.append_system(
                f"{action.name} → {names}: перенесена с павшей цели, "
                "ячейка не потрачена")
        else:
            self.append_system(f"{action.name} → {names}: {action.note}")
        self.statusBar().showMessage(f"{action.name}: {names}", 3000)
        return True

    def _mark_can_move(self, caster_id: str) -> bool:
        """Есть ли у колдуна метка, чья цель уже мертва.

        Это условие бонусного переноса метки по правилам. Живая цель
        блокирует перенос: иначе метку можно было бы бесплатно вешать на
        каждого врага подряд.
        """
        found = False
        for token in list(self.game_state.tokens.values()):
            if not isinstance(getattr(token, "active_effects", None), list):
                continue
            for effect in token.active_effects:
                if (isinstance(effect, dict)
                        and effect.get("type") == "mark"
                        and str(effect.get("caster_id") or "") == caster_id):
                    dead = combat_rules.death_save_state(
                        getattr(token, "death_saves", None))["dead"]
                    if not dead:
                        return False
                    found = True
        return found

    def on_spell_result(self, data: dict):
        """Сервер разрешил прямое заклинание: отклик и звук.

        Хиты уже пришли в UPDATE_TOKEN; здесь только анимация снарядов и
        лечение, чтобы не рисовать одно и то же дважды.
        """
        if not isinstance(data, dict):
            return
        # Массовое лечение: анимация и звук идут по каждой цели, а карточка
        # броска — одна на каст. Иначе шесть окон подряд закрывали бы стол;
        # разбор по целям остаётся в журнале.
        if data.get("mass"):
            self.canvas.show_spell_result(data)
            return
        if data.get("mass_summary"):
            count = int(data.get("targets_count") or 0)
            self.show_roll_toast(
                str(data.get("spell_name") or "Заклинание"),
                int(data.get("amount") or 0),
                f"{count} цел. · лечение", ROLL_GOOD)
            return
        self.canvas.show_spell_result(data)
        # Сервер уже бросил кубы: итог показываем карточкой, чтобы его
        # увидели все, а не только тот, кто читает чат.
        target_row = data.get("target") or {}
        cured = [str(value) for value in (data.get("cured") or [])]
        revived = bool(data.get("revived") or target_row.get("revived"))
        if revived:
            # Воскрешение — не «лечение на 1 хит»: в карточке так и пишем,
            # иначе выглядело бы, будто мёртвого просто подлатали.
            self.show_roll_toast(
                str(data.get("spell_name") or "Заклинание"),
                target_row.get("hp") or 1,
                f"{target_row.get('name') or data.get('target_name', '?')} · "
                "возвращён к жизни", ROLL_GOOD)
            if cured:
                self.statusBar().showMessage(
                    f"Снято: {', '.join(cured)}", 4000)
            return
        if str(data.get("kind") or "") == "dispel":
            spell = str(data.get("spell_name") or "Развеивание магии")
            self.statusBar().showMessage(
                (f"{spell}: снято {', '.join(cured)}" if cured else
                 f"{spell}: снято {', '.join(data.get('dispelled') or [])}"
                 if data.get("dispelled") else
                 f"{spell}: нечего развеивать"), 4000)
            return
        amount = target_row.get("amount")
        # Одиночный спасбросок: без строки со спасброском непонятно, почему
        # урона нет вовсе (успех) или почему он вдвое меньше (половина).
        save_row = data.get("save") if isinstance(data.get("save"), dict) else {}
        save_detail = ""
        # Последствия из правил конкретных заклинаний: пол хитов (Урон),
        # урезанный максимум хитов (тоже Урон) и пометка о зомби (Перст
        # смерти). Без них в карточке урон выглядел бы меньше броска, а
        # максимум хитов менялся бы молча.
        extra_detail = ""
        if data.get("hp_floor"):
            extra_detail += " · урон не опускает хиты ниже 1"
        reduced = int(data.get("hp_max_reduced") or 0)
        if reduced:
            extra_detail += f" · максимум хитов −{reduced}"
            if target_row.get("hp_max"):
                extra_detail += f" (теперь {target_row.get('hp_max')})"
        zombie = data.get("zombie")
        if isinstance(zombie, dict) and zombie.get("label"):
            extra_detail += f" · {zombie['label']}"
        chain = data.get("chain")
        if isinstance(chain, dict):
            # Цепная молния: у каждой цели своя карточка, и без подписи
            # непонятно, кто первая цель, а кто поймал разряд.
            if chain.get("primary"):
                extra_detail += (" · первая цель, разрядов до "
                                 f"{int(chain.get('bolts') or 0)}")
            else:
                extra_detail += (f" · разряд {int(chain.get('index') or 0)}"
                                 f" из {int(chain.get('count') or 0)}")
        if save_row:
            save_detail = (
                f" · спасбросок {save_row.get('ability_label')} "
                + combat_rules.d20_text(save_row)
                + f" против Сл {save_row.get('dc')} — "
                + ("успех" if save_row.get("success") else "провал"))
        if data.get("breakdown"):
            # Кости урона/лечения заклинания: без разбора в карточке
            # виден только итог, и проверить бросок нельзя.
            save_detail += f" · {data['breakdown']}"
        if amount and not data.get("blocked"):
            fx_name = str(data.get("fx") or "")
            heal = (str(data.get("kind") or "") == "heal"
                    or fx_name in ("healing_word", "cure_wounds"))
            detail = (f"{target_row.get('name') or data.get('target_name', '?')} · "
                      + ("лечение" if heal else "урон") + save_detail
                      + extra_detail)
            if cured:
                detail += f" · снято: {', '.join(cured)}"
            self.show_roll_toast(
                str(data.get("spell_name") or "Заклинание"), amount,
                detail, ROLL_GOOD if heal else ROLL_BAD)
        elif save_row and not data.get("blocked"):
            # Спасбросок пройден и половины у правила нет: показываем 0,
            # а не молчим — иначе игрок решит, что каст не сработал.
            self.show_roll_toast(
                str(data.get("spell_name") or "Заклинание"), 0,
                (f"{target_row.get('name') or data.get('target_name', '?')}"
                 + save_detail + extra_detail), ROLL_GOOD)
        kind = str(data.get("kind") or "")
        fx = str(data.get("fx") or "")
        if data.get("blocked"):
            self._play_spell_sound("shield")
            self.statusBar().showMessage(
                f"Щит отразил «{data.get('spell_name', 'заклинание')}»", 4000)
        elif kind == "darts":
            self._play_spell_sound(fx or "magic_missile")

    def _play_page_sound(self) -> None:
        """Шелест страницы: нажатие «Применить» в книге должно быть слышно.

        Раньше кнопка работала молча, и до первого видимого шага (диалог
        цели или поставленный шаблон) было непонятно, что клик засчитан.
        """
        if not self.combat_effects_enabled:
            return
        now = time.monotonic()
        # Быстрые повторные клики не должны накладывать шелест друг на друга.
        if now - float(getattr(self, "_page_sound_time", 0.0)) < 0.25:
            return
        self._page_sound_time = now
        try:
            self.audio.play_sound(self._page_sound_id, volume=0.55)
        except Exception:
            # Звук — украшение: его сбой не должен ломать каст.
            pass

    def _arm_target_pick(self, action, sheet) -> bool:
        """Взвести заклинание на выбор цели кликом по токену на холсте.

        Так приложение само показывает, что ждёт игрока: прицел вместо
        стрелки, золотое кольцо на токене под курсором и плашка с отменой.
        Расход (ячейка, заряд) идёт только после выбора цели.
        """
        canvas = getattr(self, "canvas", None)
        if action is None or canvas is None:
            return False
        if not self.map_tokens_for_combat():
            return False
        if self._pending_area_action is not None:
            self._pending_area_action = None
            canvas.cancel_template()
        self._pending_target_action = {"action": action.to_dict(),
                                       "sheet": sheet}
        label = str(action.name or "Заклинание")
        reach = int(getattr(action, "range_ft", 0) or 0)
        if reach:
            label += f" · до {reach} фут"
        canvas.start_target_pick(label)
        self.statusBar().showMessage(
            f"{action.name}: кликните по цели · ПКМ или Esc — отмена", 6000)
        return True

    def on_target_picked(self, token_id: str) -> None:
        """Игрок выбрал цель кликом: повторяем действие уже с целью."""
        # Особая атака существа идёт своим путём: это не действие листа, а
        # запрос на сервер, где спасбросок бросает цель, а не существо.
        special = getattr(self, "_pending_special_attack", None)
        if special:
            self._pending_special_attack = None
            if self.canvas.target_pick_mode:
                self.canvas.cancel_target_pick(silent=True)
            self.network_client.send_message(MessageType.SPECIAL_ATTACK, {
                **special, "target_id": str(token_id)})
            return
        pending = self._pending_target_action
        self._pending_target_action = None
        if not pending:
            return
        # Цель могла прийти не кликом (внешняя панель, тест): прицел всё
        # равно снимаем, иначе холст продолжал бы ждать выбора.
        if self.canvas.target_pick_mode:
            self.canvas.cancel_target_pick(silent=True)
        target = (self.game_state.tokens.get(str(token_id))
                  or self.canvas.tokens.get(str(token_id)))
        if target is None or not self.canvas.token_belongs_to_current_map(target):
            self.append_system("Цель не найдена — заклинание не применено")
            return
        data = dict(pending.get("action") or {})
        data["target_id"] = target.id
        self.on_use_action(data)

    def on_target_pick_cancelled(self) -> None:
        """Esc или ПКМ: снять взведённое заклинание, ничего не потратив."""
        special = getattr(self, "_pending_special_attack", None)
        if special:
            self._pending_special_attack = None
            self.append_system("Особая атака отменена — цель не выбрана")
            return
        pending = self._pending_target_action
        self._pending_target_action = None
        if not pending:
            return
        if self.canvas.target_pick_mode:
            self.canvas.cancel_target_pick(silent=True)
        name = str((pending.get("action") or {}).get("name") or "Заклинание")
        self.append_system(
            f"{name}: выбор цели отменён — ячейка не потрачена")
        self.statusBar().showMessage(f"{name}: отменено", 3000)

    def _cancel_target_pick(self) -> None:
        """Снять взведённую цель без сообщений — новый каст её заменяет."""
        # Особая атака и заклинание делят один выбор цели: снимать нужно оба
        # ожидания, иначе позже сработало бы то, что мастер уже отменил.
        self._pending_special_attack = None
        if self._pending_target_action is None:
            return
        self._pending_target_action = None
        canvas = getattr(self, "canvas", None)
        if canvas is not None:
            canvas.cancel_target_pick(silent=True)

    def on_use_action(self, data):
        """Один пайплайн для заклинаний и предметов."""
        if not isinstance(data, dict):
            return
        # Новый каст снимает прошлое «ждём цель»: иначе оно висело бы и
        # перехватывало клики по карте. Звук книги — только для листа.
        if data.get("from_sheet") and str(data.get("source") or "") == "spell":
            self._play_page_sound()
        self._cancel_target_pick()
        action = use_action.UseAction.from_dict(data)
        sheet = self._action_sheet(action)
        if not isinstance(sheet, dict):
            # Старый вызов эффекта скорости мог прийти без листа (например,
            # из внешней панели). Сам эффект листа не требует, а расход у
            # такого действия пустой; всё остальное без листа невозможно.
            if (action.kind == "utility"
                    and (action.consumption or {}).get("type") == "none"
                    and action.effects):
                sheet = {}
            else:
                self.append_system(
                    f"{action.note or action.name}: лист не загружен")
                return
        actor = self._action_actor(action)
        if actor is None:
            self.append_system(f"{action.note or action.name}: нет действующего токена")
            return
        allowed, why = self._token_can_act(actor.id)
        if not allowed:
            self.append_system(f"{action.note or action.name}: нельзя применить — {why}")
            return
        if action.kind == "heal_pool":
            # Наложение рук: цель и количество выбирает окно, а не выделение
            # на холсте — иначе легко вылечить не того.
            self._use_heal_pool(action, actor, sheet)
            return
        # Массовое лечение целится не в одного токена: цель — весь набор
        # выделенных существ в радиусе правила. Список собирается до
        # одиночного выбора цели, иначе открывался бы лишний запрос
        # «кого лечить» поверх выбора группы.
        heal_targets = []
        if self._is_mass_heal(action):
            heal_targets = self._mass_heal_targets(action, actor)
            if not heal_targets:
                return
            target = heal_targets[0]
        else:
            target = self._action_target(action, actor)
        if (action.kind in ("heal", "damage", "darts", "revive", "dispel",
                            "save_target")
                and target is None):
            # Цели нет и выбирать её не из чего: вместо строчки в чате
            # взводим заклинание — холст покажет прицел и плашку отмены.
            if not self._arm_target_pick(action, sheet):
                self.append_system(
                    f"{action.note or action.name}: не выбрана цель")
            return
        target_dead = bool(target is not None and combat_rules.death_save_state(
            getattr(target, "death_saves", None))["dead"])
        if action.kind == "heal" and target_dead:
            self.append_system(
                f"{action.note or action.name}: «{target.name}» мёртв — "
                "лечение не поднимает мёртвых")
            return
        if action.kind == "revive" and target is not None and not target_dead:
            # Оживление и Воскрешение — для мёртвых: иначе ячейка ушла бы
            # впустую на живого союзника.
            self.append_system(
                f"{action.note or action.name}: «{target.name}» жив — "
                "воскрешать некого")
            return
        if (action.kind in ("heal", "damage", "darts") and not action.formula
                and not int(getattr(action, "pool", 0) or 0)):
            self.append_system(
                f"{action.note or action.name}: формула не распознана")
            return
        ok, reason = use_action.can_use(sheet, action)
        if not ok:
            self.append_system(f"{action.note or action.name}: {reason}")
            return

        # --- Заклинания с серверным броском --------------------------------
        # Волшебная стрела, Лечащее слово и Лечение ран считаются на сервере:
        # он берёт формулу из листа, проверяет дистанцию и состояние цели.
        # Одиночный спасбросок (Священное пламя, Злая насмешка, Увядание)
        # тоже считает сервер: он бросает спасбросок цели и кубик урона.
        if action.kind == "save_target" and int(getattr(action, "chain", 0) or 0):
            # Цепная молния: первая цель уже выбрана (кликом или выделением),
            # а разряды бьют в существ в 30 футах ОТ НЕЁ — поэтому список
            # второстепенных целей строится вторым шагом. Пустой список
            # не отменяет каст: без соседей молния бьёт только первую цель.
            bolts = self._choose_spell_targets(
                actor, action, center=target,
                reach_ft=int(getattr(action, "chain_radius_ft", 0) or 30),
                limit=int(action.chain or 3))
            if bolts is None:
                return
            if not self._send_chain_spell(action, actor, target, bolts, sheet):
                return
            return
        if action.kind in ("darts", "heal", "revive", "dispel",
                           "save_target") and action.spell_key:
            if not self._send_cast_spell(action, actor, target, sheet,
                                         targets=heal_targets):
                return
            return
        if action.kind == "buff" and action.effects:
            if not self._apply_spell_buff(action, actor, target, sheet):
                return
            return

        # Атака и спасбросок открывают окно выбора; расход — только после
        # подтверждения, иначе отмена съедала бы зелье или ячейку.
        if action.kind == "attack":
            preset = {
                "name": action.name,
                "attack_bonus": action.attack_bonus,
                "damage": action.formula,
                "damage_type": action.damage_type,
                "fx": action.fx,
                # Атака заклинанием — не оружие: ярость к ней не прибавляется.
                "ability": "spell" if action.spell_key else "",
            }
            if not self.open_attack_dialog(
                    attacker_id=actor.id,
                    target_id=target.id if target is not None else None,
                    preset=preset):
                return
            ok, reason = use_action.consume(sheet, action)
            if not ok:
                self.append_system(f"{action.note or action.name}: {reason}")
                return
            self._persist_action_sheet(action, sheet)
            return
        if action.kind == "pool":
            # Сон и Цветной шар: цель — не существо, а область. Пул хитов
            # тратит сервер по самым раненым в шаблоне.
            if self._start_area_template(action, actor, sheet):
                return
            return
        if action.kind == "save":
            # Заклинание с областью: игрок ставит шаблон на карте, а сервер
            # сам решает, кто попал, и бросает спасброски — как у урона,
            # так и у состояний без урона (Паутина, Опутывание).
            if action.area and (action.formula or action.effects):
                self._start_area_template(action, actor, sheet)
                return
            if not self.open_save_dialog(
                    token_id=target.id if target is not None else actor.id,
                    ability=action.ability, dc=action.dc):
                return
            ok, reason = use_action.consume(sheet, action)
            if not ok:
                self.append_system(f"{action.note or action.name}: {reason}")
                return
            self._persist_action_sheet(action, sheet)
            return

        # Утилита без механики: нечего тратить и применять.
        if action.kind == "utility" and not action.effects:
            self.append_system(f"{action.note or action.name}: нет эффекта")
            return

        # Для лечения/урона бросаем до списания: неверная формула не должна
        # съедать зелье или ячейку. Само применение — после расхода.
        result = None
        if action.formula:
            try:
                result = dice_ext.roll(action.formula)
            except dice_ext.DiceError as exc:
                self.append_system(
                    f"{action.note or action.name}: формула не распознана ({exc})")
                return
        ok, reason = use_action.consume(sheet, action)
        if not ok:
            self.append_system(f"{action.note or action.name}: {reason}")
            return

        if action.kind == "utility":
            # Цель — не всегда тот, кто выпил: зелье можно перетащить на
            # союзника. Сервер применит эффект к указанному токену и сам
            # проверит права и длительность.
            target = target or actor
            labels = item_effects.summary(action.effects)
            for effect in action.effects:
                if effect.get("type") == "speed":
                    # Локальное ускорение нужно сразу, чтобы клетки
                    # подсветились до ответа сервера; таймер всё равно
                    # подтвердит серверное состояние.
                    rounds = int(effect.get("rounds") or 10)
                    self.canvas.apply_speed_potion(target.id, rounds=rounds)
            # Расход уходит на сервер ДО эффекта: сервер собирает свой
            # ответ с копии листа, и если SHEET_UPDATE придёт позже, в
            # ответе окажется полное количество — потраченное вернётся и
            # следующее использование станет бесплатным.
            self._persist_action_sheet(action, sheet)
            self.network_client.send_message(
                MessageType.APPLY_EFFECT,
                {'token_id': target.id, 'effects': action.effects,
                 'from_token_id': actor.id,
                 'source': action.source_id or action.name})
            self.append_system(
                f"{action.name} → {target.name}: "
                + (labels or "эффект") + ".")
            return

        amount = int(result["total"]) if result is not None else 0
        # Лечение себя: видно сразу, но на сервер уходит прежний лист.
        wire_sheet = None
        if action.kind == "heal" and target.id == actor.id:
            # На сервер лист уходит с хитами ДО лечения: сами хиты поднимет
            # APPLY_DAMAGE ниже. Пока в SHEET_UPDATE ехали уже вылеченные
            # хиты, сервер сперва переносил их на токен, а потом прибавлял
            # лечение ещё раз: пять зелий на 32 хита поднимали 64.
            wire_sheet = self._sheet_with_hp(sheet, target.hp)
            if wire_sheet is not None:
                # Если хиты токена неизвестны, честную копию не собрать:
                # тогда лечит только сервер, а лист придёт его ответом.
                self._apply_heal_locally(target, amount, sheet)
        # Порядок важен: сначала расход в листе, потом лечение. Сервер
        # собирает SHEET_STATE с копии листа в момент APPLY_DAMAGE, и
        # обратный порядок возвращал потраченное зелье или заряд: первое
        # использование выглядело бесплатным, а лист снова показывал 1/1.
        self._persist_action_sheet(action, sheet, wire_sheet)
        if result is not None:
            self.network_client.send_message(MessageType.ROLL_DICE, {
                'dice': result['formula'],
                'result': amount,
                'breakdown': dice_ext.format_roll(result),
                'timestamp': self.now(),
                'note': action.note or action.name,
            })
        self.network_client.send_message(
            MessageType.APPLY_DAMAGE,
            {'token_id': target.id, 'amount': amount,
             'heal': action.kind == "heal",
             'heal_style': 'spell' if action.source == "spell" else 'item'})

    def on_item_dropped(self, payload, target_id=None):
        """Предмет бросили на токен: применить лечение к этой цели.

        Из инвентаря берём именно ту запись, что лежит в листе: цена,
        количество и id могут отличаться от записи справочника. Из
        справочника игрок может применить только то зелье, которое у него
        действительно есть; мастер — любое (он и так распоряжается столом).
        """
        if not isinstance(payload, dict):
            return
        target_id = str(target_id or payload.get("target_id") or "")
        target = (self.game_state.tokens.get(target_id)
                  or self.canvas.tokens.get(target_id))
        if target is None or not self.canvas.token_belongs_to_current_map(target):
            self.append_system("Предмет брошен мимо токена")
            return
        rec = payload.get("item") or {}
        if not isinstance(rec, dict) or not rec.get("name"):
            self.append_system("Не удалось прочитать предмет")
            return
        if payload.get("source") == "library":
            self._use_library_item_on_token(rec, target, payload)
            return
        if payload.get("source") == "inventory":
            self._use_inventory_item_on_token(rec, target, payload)
            return
        if payload.get("source") == "spell":
            self._cast_spell_on_token(rec, target, payload)
            return
        self.append_system("Этот предмет нельзя применить перетаскиванием")

    def _cast_spell_on_token(self, rec, target, payload):
        """Заклинание из книги бросили на токен: цель уже выбрана.

        Запись заклинания перечитываем из листа по id: формула, круг и
        подготовка берутся из листа, а не из перетаскиваемой копии.
        """
        spell = rec.get("spell") if isinstance(rec.get("spell"), dict) else rec
        if not isinstance(spell, dict) or not spell.get("name"):
            self.append_system("Не удалось прочитать заклинание")
            return
        stub = use_action.UseAction(
            source="spell", kind="utility",
            name=str(spell.get("name") or "Заклинание"),
            token_id=str(payload.get("token_id") or ""))
        actor = self._action_actor(stub)
        if actor is None:
            self.append_system(
                "Нет токена, чья книга заклинаний — некому кастовать")
            return
        sheet = self._sheet_for_token(actor)
        if not isinstance(sheet, dict):
            self.append_system("У токена нет листа персонажа")
            return
        actual = None
        spell_id = str(spell.get("id") or "")
        if spell_id:
            actual = sheet_items.find_spell(sheet, spell_id)
        if actual is None:
            name = str(spell.get("name") or "")
            actual = next((entry for entry in
                           ((sheet.get("vtt") or {}).get("spells") or [])
                           if str(entry.get("name") or "") == name), None)
        if actual is None:
            self.append_system(
                f"«{spell.get('name')}» нет в книге этого персонажа")
            return
        # Круг ячейки выбирается в книге: перетаскивание несёт его с
        # собой, чтобы Огненный шар третьим кругом летел 9к6, а не 8к6.
        try:
            slot_level = int(payload.get("slot_level")
                             or rec.get("slot_level") or 0) or None
        except (TypeError, ValueError):
            slot_level = None
        action = use_action.from_spell(actual, sheet, token_id=actor.id,
                                       slot_level=slot_level)
        if action.kind == "utility" and not action.effects:
            self.append_system(
                f"«{action.name}»: у заклинания нет механического эффекта")
            return
        action.target_id = target.id
        self.on_use_action(action.to_dict())

    @staticmethod
    def _inventory_record(sheet, rec):
        """Найти строку инвентаря по записи из справочника или по id."""
        if not isinstance(sheet, dict):
            return None
        source_id = str(rec.get("id") or rec.get("source_id") or "")
        name = str(rec.get("name") or "")
        for item in sheet_items.ensure(sheet)["inventory"]:
            if source_id and str(item.get("id") or "") == source_id:
                return item
            if (source_id and str(item.get("source_id") or "") == source_id):
                return item
            if name and str(item.get("name") or "") == name:
                return item
        return None

    def _use_inventory_item_on_token(self, rec, target, payload):
        action_stub = use_action.UseAction(
            source="item", kind="heal", name=str(rec.get("name") or ""),
            token_id=str(payload.get("token_id") or ""))
        actor = self._action_actor(action_stub)
        if actor is None:
            self.append_system("Нет токена, из инвентаря которого взят предмет")
            return
        sheet = self._sheet_for_token(actor)
        item = self._inventory_record(sheet, rec)
        if item is None:
            self.append_system(f"«{rec.get('name')}» нет в инвентаре")
            return
        action = use_action.from_item(item, sheet, token_id=actor.id)
        if action.kind == "utility" and not action.effects:
            self.append_system(f"«{item.get('name')}»: у предмета нет эффекта")
            return
        if action.kind not in ("heal", "attack", "utility"):
            self.append_system(
                "Этот предмет нельзя применить перетаскиванием на токен")
            return
        action.target_id = target.id
        self.on_use_action(action.to_dict())

    def _use_library_item_on_token(self, rec, target, payload):
        """Справочник: игрок тратит своё зелье, мастер применяет напрямую."""
        probe = use_action.from_item(rec)
        if probe.kind == "utility" and not probe.effects:
            self.append_system(f"«{rec.get('name')}»: у предмета нет эффекта")
            return
        if probe.kind not in ("heal", "attack", "utility"):
            self.append_system(
                "Этот предмет нельзя применить перетаскиванием на токен")
            return
        stub = use_action.UseAction(
            source="item", kind="heal", name=str(rec.get("name") or ""),
            token_id=str(payload.get("token_id") or ""))
        actor = self._action_actor(stub)
        sheet = self._sheet_for_token(actor) if actor is not None else None
        item = self._inventory_record(sheet, rec)
        if item is not None:
            # У игрока это же зелье лежит в инвентаре — списываем именно
            # его, чтобы перетаскивание из справочника не было бесконечным.
            action = use_action.from_item(item, sheet, token_id=actor.id)
            action.target_id = target.id
            self.on_use_action(action.to_dict())
            return
        if self.player_role != Role.DM:
            self.append_system(
                f"«{rec.get('name')}» нет в инвентаре — возьмите зелье у мастера")
            return
        if probe.kind == "utility":
            # Мастер применяет зелье из справочника без списания из чужого
            # инвентаря: эффект всё равно накладывает сервер.
            self.network_client.send_message(
                MessageType.APPLY_EFFECT,
                {'token_id': target.id, 'effects': probe.effects,
                 'source': probe.source_id or probe.name})
            self.append_system(
                f"Мастер применил «{rec.get('name')}» на {target.name}: "
                + (item_effects.summary(probe.effects) or "эффект"))
            return
        if probe.kind == "attack":
            if actor is None:
                self.append_system(
                    "Для броска атаки нужен токен, бросающий предмет")
                return
            probe.token_id = actor.id
            probe.target_id = target.id
            probe.consumption = {"type": "none"}
            probe.item_id = ""
            self.on_use_action(probe.to_dict())
            return
        if combat_rules.death_save_state(
                getattr(target, "death_saves", None))["dead"]:
            self.append_system("Лечение не поднимает мёртвых")
            return
        if not probe.formula:
            self.append_system(f"«{rec.get('name')}»: формула лечения не распознана")
            return
        try:
            result = dice_ext.roll(probe.formula)
        except dice_ext.DiceError as exc:
            self.append_system(f"«{rec.get('name')}»: {exc}")
            return
        amount = int(result["total"])
        self.network_client.send_message(MessageType.ROLL_DICE, {
            'dice': result['formula'], 'result': amount,
            'breakdown': dice_ext.format_roll(result),
            'timestamp': self.now(), 'note': f"{probe.note}: {target.name}",
        })
        self.network_client.send_message(MessageType.APPLY_DAMAGE, {
            'token_id': target.id, 'amount': amount,
            'heal': True, 'heal_style': 'item',
        })
        self.append_system(
            f"Мастер применил «{rec.get('name')}» на {target.name}: +{amount}")

    def on_sheet_effect(self, kind: str, formula: str, note: str,
                        token_id: str = None):
        """Старый точечный вызов: превращаем в UseAction и идём общим путём.

        Сигнатура сохранена: её используют внешние панели и тесты. Новый
        код должен звать on_use_action с полным действием.
        """
        action_kind = "utility" if kind == "speed" else kind
        action = use_action.UseAction(
            source="item" if kind in ("heal", "damage", "speed") else "legacy",
            kind=action_kind,
            name=note or "Эффект",
            formula=formula or "",
            target_mode="ally" if kind == "heal" else "self",
            consumption={"type": "none"},
            effects=([{"type": "speed", "rounds": 10}]
                     if kind == "speed" else
                     [{"type": kind, "formula": formula}]
                     if kind in ("heal", "damage") else []),
            token_id=token_id or "",
            note=note or "",
        )
        self.on_use_action(action.to_dict())



    def _adopt_sheets(self):
        """Подхватить листы из состояния мира.

        Свой лист берём только если панель пуста: иначе загрузка мира
        затирала бы несохранённые правки игрока. Чужие листы просто
        запоминаем — они нужны мастеру.
        """
        # Листы, привязанные к токенам (новая модель), подхватываем всегда:
        # без этого после загрузки мира карточки токенов оставались пустыми.
        by_token = getattr(self.game_state, "token_sheets", None) or {}
        if by_token:
            self.token_sheets.update(
                {k: v for k, v in by_token.items() if isinstance(v, dict)})
        sheets = getattr(self.game_state, "sheets", None) or {}
        for player_id, sheet in sheets.items():
            if not isinstance(sheet, dict):
                continue
            self.character_sheets[player_id] = sheet
            if player_id != self.player_id:
                continue
            # У мастера своей панели листа нет — атрибут не создаётся.
            # Без проверки загрузка мира с листом мастера падала.
            if not hasattr(self, "character_sheet"):
                continue
            if self.character_sheet.sheet is None:
                self.character_sheet.set_sheet(sheet)
                player = self.game_state.players.get(player_id)
                name = player.nickname if player else player_id
                self.append_system(f"Лист персонажа загружен из мира: {name}")
        # В новом мире лист лежит в папке токена, а не по игроку. Если слот
        # не выдавался (вход без слотов) или старый мир не успел продублировать
        # лист по игроку — панель всё равно должна показать лист персонажа.
        if not hasattr(self, "character_sheet"):
            return
        if self.character_sheet.sheet is not None:
            return
        for token in self.game_state.tokens.values():
            if not (self._is_own_token(token) and token_kind.is_player(token)):
                continue
            sheet = self.token_sheets.get(token.id)
            if not isinstance(sheet, dict):
                continue
            self.character_sheets[self.player_id] = sheet
            self.character_sheet.set_sheet(sheet)
            self.append_system(
                f"Лист персонажа загружен из мира: {token.name}")
            break

    def on_sheet_changed(self, sheet: dict):
        """Лист изменился: запоминаем и отправляем на сервер.

        Сервер держит копии листов, чтобы мастер видел инвентарь игроков и
        мог выдать предмет. Без отправки выдача некуда было бы положить.
        """
        self.character_sheets[self.player_id] = sheet
        self._sheet_dirty = True
        # В листе сменили оружие быстрой атаки — плашка на холсте должна
        # сказать об этом сразу, а не после следующего выбора токена.
        if getattr(self.canvas, "quick_attack_mode", False):
            self._reflect_quick_attack_mode()
        # Загрузили лист уже «на нуле» — сразу показываем спасброски от смерти.
        own = self._own_token()
        if own is not None:
            self._apply_death_state_to_views(own)
        # Частые правки (надеть/снять) не должны заваливать сеть: шлём
        # не чаще раза в 400 мс, последнее состояние побеждает.
        timer = getattr(self, "_sheet_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._flush_sheet)
            self._sheet_timer = timer
        timer.start(400)

    def _flush_sheet(self, wire_sheet=None):
        sheet = self.character_sheets.get(self.player_id)
        if not sheet:
            # Нечего отправлять: снимаем флаг, иначе он навсегда запретил
            # бы принимать серверные обновления своего листа.
            self._sheet_dirty = False
            return
        self._sheet_dirty = False
        wire = wire_sheet if isinstance(wire_sheet, dict) else sheet
        # Что отправили — запоминаем: эхо этого же листа не должно
        # перерисовывать виджет и сбивать курсор в полях.
        self._sheet_sent = copy.deepcopy(wire)
        self.network_client.send_message(
            MessageType.SHEET_UPDATE,
            {'player_id': self.player_id, 'sheet': wire})

    def on_sheet_state(self, data: dict):
        """Сервер прислал лист: свой — только если правил мастер.

        Эхо своей правки не перерисовываем: иначе курсор сбивался бы в
        полях, пока игрок печатает. Мастер мог загрузить лист в наш токен —
        тогда показываем его: изменение пришло извне.
        """
        player_id = data.get('player_id') or ''
        sheet = data.get('sheet')
        token_id = data.get('token_id')
        # У токена без владельца player_id пустой — лист всё равно надо
        # показать на его странице.
        if not isinstance(sheet, dict) or not (player_id or token_id):
            return
        previous = (self.token_sheets.get(token_id) if token_id
                    else self.character_sheets.get(player_id))
        changed = previous != sheet
        widget = getattr(self, "character_sheet", None)
        own_token = bool(widget is not None and token_id and self._own_token()
                         is not None and self._own_token().id == token_id)
        # Свой несохранённый расход важнее эха сервера. Пока лист ждёт
        # отправки (например, лечение уже вернулось, а SHEET_UPDATE ещё
        # нет), старое эхо вернуло бы потраченное зелье или заряд —
        # использование как будто не считалось бы.
        if (getattr(self, "_sheet_dirty", False)
                and (player_id == self.player_id or own_token)):
            return
        # Только что потратили предмет или заряд: серверное эхо могло быть
        # собрано до SHEET_UPDATE, и старое количество вернулось бы.
        # Смотрим на конкретный лист (токен или игрок), а не только на свой:
        # у мастера открыта страница чужого персонажа, и его нажатие тоже
        # не должно откатываться ответом сервера.
        edits = getattr(self, "_sheet_local_edits", None) or {}
        stamps = [float(edits.get(token_id) or 0.0),
                  float(edits.get(player_id) or 0.0)]
        if player_id == self.player_id or own_token:
            stamps.append(float(getattr(self, "_sheet_local_edit_at", 0.0)
                                or 0.0))
        last_edit = max(stamps)
        if last_edit and 0.0 <= time.monotonic() - last_edit < 1.0:
            return
        if player_id:
            self.character_sheets[player_id] = sheet
        if token_id:
            self.token_sheets[token_id] = sheet
        # Старый мир мог не знать owner_id: токен узнаётся по id игрока.
        # Мастер грузил туда лист с player_id мастера — игрок всё равно
        # должен увидеть свой лист в панели, а не только на странице.
        if own_token:
            self.character_sheets[self.player_id] = sheet
        if player_id == self.player_id or own_token:
            sent = getattr(self, "_sheet_sent", None)
            # changed=False бывает, когда лист уже приехал в полном состоянии,
            # но панель тогда ещё не знала, что токен принадлежит игроку.
            # Пустую панель заполняем в любом случае.
            if (widget is not None and (changed or widget.sheet is None)
                    and sheet != sent):
                widget.set_sheet(sheet)
                self.statusBar().showMessage(
                    "Лист персонажа обновлён", 5000)
            return
        # Чужой лист: если это страница во вкладке мастера — обновляем её.
        # Хиты приходят из боя с token_id: без перерисовки страница
        # показывала старые 101/101, хотя токен уже терял хиты.
        pages = getattr(self, "sheet_pages", None)
        if pages is not None and token_id:
            pages.set_sheet(token_id, sheet)
        if not changed:
            return                      # своё же эхо — логировать нечего
        player = self.game_state.players.get(player_id)
        name = player.nickname if player else player_id
        self.append_system(f"Лист персонажа обновлён: {name}")

    # --- Вкладка «Лист персонажа» ---------------------------------------
    def open_lss_sheet(self, token_id=None):
        """Открыть вкладку «Лист персонажа» в левой панели.

        Отдельного окна у листа больше нет: страница персонажа живёт во
        вкладке, лист грузится кнопкой «LSS лист» прямо на ней. Если
        выбран токен игрока — открываем сразу его страницу.
        """
        pages = getattr(self, "sheet_pages", None)
        if pages is None:
            # У игрока своей вкладки нет — его лист и так в левой панели.
            self.statusBar().showMessage(
                "Ваш лист персонажа — в левой панели", 4000)
            return
        tabs = getattr(self, "section_tabs", None)
        index = getattr(self, "_sheet_tab_index", -1)
        if tabs is not None and 0 <= index < tabs.count():
            if tabs.currentIndex() != index:
                tabs.toggle_section(index)
        if token_id is None:
            token_id = getattr(self.canvas, "selected_token", None)
        token = self.game_state.tokens.get(token_id) if token_id else None
        if token is not None and token_kind.is_player(token):
            pages.select(token.id)

    def _page_sheet(self, token_id):
        """Лист токена: из памяти, а если его ещё не было — с диска."""
        sheet = self.token_sheets.get(token_id)
        if isinstance(sheet, dict):
            return sheet
        try:
            sheet = world_store.load_token_sheet(
                world_store.get_data_root(), self._session_folder(), token_id)
        except OSError:
            sheet = None
        if isinstance(sheet, dict):
            self.token_sheets[token_id] = sheet
        return sheet

    def sync_token_pages(self):
        """Страницы вкладки «Лист персонажа» — по одной на токен игрока."""
        pages = getattr(self, "sheet_pages", None)
        if pages is not None:
            pages.sync_tokens(self.game_state.tokens)
            # У новой страницы ещё нет отметок смерти — отдаём их сразу.
            for token in self.game_state.tokens.values():
                self._apply_death_state_to_views(token)
        self._refresh_special_attack_hotkeys()

    def _refresh_special_attack_hotkeys(self):
        """Собрать хоткеи особых атак со статблоков токенов.

        Своё существо игрок и мастер видят одним и тем же способом: клавиша
        хранится в статблоке и едет вместе с остальным состоянием. Чужие
        токены игроку не нужны — иначе одна буква запускала бы чужую атаку.
        """
        canvas = getattr(self, "canvas", None)
        if canvas is None:
            return
        mapping = {}
        for token in self.game_state.tokens.values():
            if not special_attack_rules.has_attacks(
                    getattr(token, "statblock", None) or {}):
                continue
            if (self.player_role != Role.DM
                    and token.owner_id != self.player_id):
                continue
            if token.hp is not None and int(token.hp) <= 0:
                continue
            for entry in (token.statblock or {}).get("special_attacks") or []:
                if not isinstance(entry, dict):
                    continue
                key = str(entry.get("hotkey") or "").strip().upper()
                if len(key) != 1 or key in mapping:
                    continue
                if special_attack_rules.hotkey_conflict(key):
                    # Клавиша стола (C — рывок, Q — быстрая атака…): она
                    # уже занята, и атака не имеет права её отнимать.
                    # В карточке такая клавиша подсвечена красным.
                    self._report_hotkey_conflict(token, entry, key)
                    continue
                mapping[key] = (token.id, str(entry.get("id") or ""))
        canvas.set_special_attack_hotkeys(mapping)

    def _report_hotkey_conflict(self, token, entry, key):
        """Сказать один раз за сессию, почему у существа не работает клавиша.

        Без сообщения мастер видит только «нажал C — произошёл рывок»
        и ищет ошибку не там. Повторы глушим: пересборка хоткеев идёт
        после каждого хода и любого листа.
        """
        marker = (getattr(token, "id", ""), str(entry.get("id") or ""), key)
        reported = getattr(self, "_reported_hotkey_conflicts", None)
        if reported is None:
            reported = self._reported_hotkey_conflicts = set()
        if marker in reported:
            return
        reported.add(marker)
        reason = special_attack_rules.hotkey_conflict(key)
        self.append_system(
            f"Особая атака «{entry.get('name') or key}» у "
            f"{getattr(token, 'name', 'существа')}: клавиша {reason}. "
            f"Выберите свободную в карточке существа.")

    def _store_token_sheet(self, token, sheet, wire_sheet=None):
        """Лист лёг в токен: память, диск и сервер одним шагом.

        ``wire_sheet`` уходит на сервер вместо локального листа — так
        лечение сохраняет расход, но не присылает уже вылеченные хиты.
        """
        self.token_sheets[token.id] = sheet
        owner = getattr(token, "owner_id", None)
        if owner:
            self.character_sheets[owner] = sheet
        world_store.save_token_sheet(
            world_store.get_data_root(), self._session_folder(), token.id, sheet)
        wire = wire_sheet if isinstance(wire_sheet, dict) else sheet
        self.network_client.send_message(
            MessageType.SHEET_UPDATE,
            {'token_id': token.id, 'player_id': owner or self.player_id,
             'sheet': wire})

    def on_page_sheet_loaded(self, token_id: str, sheet: dict):
        """Лист загрузили кнопкой «LSS лист» на странице персонажа."""
        token = self.game_state.tokens.get(token_id)
        if token is None:
            return
        self._store_token_sheet(token, sheet)
        self._apply_death_state_to_views(token)
        self.append_system(f"Лист персонажа загружен: {token.name}")

    def on_page_conditions_changed(self, token_id: str, conditions: list):
        """Галочка состояния на странице листа — сразу на токен.

        Полный список (а не «добавить/снять одно») нужен затем, чтобы снятая
        галочка действительно снимала состояние: сервер заменяет список
        токена целиком и переносит его в лист.
        """
        token = (self.game_state.tokens.get(token_id)
                 or self.canvas.tokens.get(token_id))
        if token is None:
            return
        sheet = self.token_sheets.get(token_id)
        if sheet is not None:
            self._store_token_sheet(token, sheet)
        self.apply_token_conditions({'token_id': token_id,
                                     'conditions': list(conditions)})

    def on_sheet_conditions_changed(self, conditions: list):
        """Галочка состояния на листе игрока — на его собственный токен."""
        token = self._own_token()
        token_id = getattr(self.character_sheet, "token_id", None)
        if token is None and token_id:
            token = self.game_state.tokens.get(token_id)
        if token is None:
            return
        self.apply_token_conditions({'token_id': token.id,
                                     'conditions': list(conditions)})

    def on_sheet_audited(self, summary: dict):
        """Разбор листа что-то дописал — говорим об этом за столом.

        Без сообщения оружие и кнопки появлялись бы «сами»: игрок не понял
        бы, откуда взялось «Второе дыхание» в его листе.
        """
        if not isinstance(summary, dict):
            return
        line = sheet_audit.summary_line(summary)
        if line:
            self.append_system(line)

    def on_page_sheet_edited(self, token_id: str, sheet: dict):
        """Лист правят прямо на странице — копим и шлём одним пакетом."""
        if token_id not in self.game_state.tokens:
            return
        self.token_sheets[token_id] = sheet
        self._page_sheet_dirty[token_id] = sheet
        timer = getattr(self, "_page_sheet_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._flush_page_sheets)
            self._page_sheet_timer = timer
        timer.start(400)

    def _flush_page_sheets(self):
        dirty = getattr(self, "_page_sheet_dirty", {})
        self._page_sheet_dirty = {}
        for token_id, sheet in dirty.items():
            token = self.game_state.tokens.get(token_id)
            if token is not None:
                self._store_token_sheet(token, sheet)

    def on_sheet_resource_spent(self, token_id: str, sheet: dict):
        """Заряд особенности списан: сохранить лист сразу.

        Дебаунс для набора текста хорош, а для расхода — вреден: пока он
        ждёт 400 мс, эхо сервера успевает прийти со старой копией листа и
        вернуть заряд. Тогда Второе дыхание и Действие всплеск проходили
        дважды, хотя на кнопке было «1/1».
        """
        if not isinstance(sheet, dict):
            return
        now = time.monotonic()
        self._sheet_local_edit_at = now
        edits = getattr(self, "_sheet_local_edits", None)
        if not isinstance(edits, dict):
            edits = {}
            self._sheet_local_edits = edits
        edits[str(token_id or self.player_id)] = now
        token = (self.game_state.tokens.get(token_id) if token_id else None)
        if token is None and not token_id:
            token = self._own_token()
        if token is not None:
            self._store_token_sheet(token, sheet)
        else:
            self.on_sheet_changed(sheet)
            timer = getattr(self, "_sheet_timer", None)
            if timer is not None:
                timer.stop()
            self._flush_sheet()

    def on_lss_roll(self, roll: dict):
        """Бросок из листа LSS — тем же ROLL_DICE, что и свои броски."""
        roll.setdefault("timestamp", self.now())
        self.network_client.send_message(MessageType.ROLL_DICE, roll)

    def on_lss_health(self, payload: dict):
        """Хиты из листа LSS: правим свой лист — сервер синхронизирует токен."""
        from client import lss_embed
        sheet = self.character_sheets.get(self.player_id)
        if not sheet:
            self.statusBar().showMessage(
                "LSS: своего листа нет — хиты не применены")
            return
        if not lss_embed.apply_health(sheet, payload):
            name = payload.get("characterName") or "?"
            self.statusBar().showMessage(
                f"LSS: хиты «{name}» не совпали с листом — не применены")
            return
        self.on_sheet_changed(sheet)
        if getattr(self, "character_sheet", None) is not None:
            self.character_sheet.refresh()
        self.statusBar().showMessage(
            f"LSS: хиты {payload.get('current')}/{payload.get('max')}")

    def on_lss_document(self, document: dict):
        """Документ персонажа из листа LSS — «теневой лист» приложения.

        Характеристики, КД и хиты берём из самого листа, поэтому броски и
        карточка токена работают даже без скачанного файла персонажа.
        Живые правки хитов приходят отдельно events-ом dnd:health.
        """
        if not isinstance(document, dict) or not document.get("name"):
            return
        self.lss_documents = getattr(self, "lss_documents", {})
        self.lss_documents[str(document["name"]).casefold()] = document

        token = self._lss_target_token()
        sheet = document.get("sheet")
        if token is None:
            self.statusBar().showMessage(
                "LSS: нет токена игрока — лист не привязан", 6000)
            return
        if not isinstance(sheet, dict):
            return

        # Лист живёт в токене: память, диск и сервер — по id токена.
        self.token_sheets[token.id] = sheet
        path = world_store.save_token_sheet(
            world_store.get_data_root(), self._session_folder(), token.id, sheet)
        self.network_client.send_message(
            MessageType.SHEET_UPDATE,
            {'token_id': token.id,
             'player_id': token.owner_id or self.player_id,
             'sheet': sheet})

        parts = [token.name]
        if document.get("hp") is not None:
            parts.append(f"HP {document.get('hp')}/{document.get('hp_max')}")
        if document.get("ac") is not None:
            parts.append(f"КД {document.get('ac')}")
        stats = document.get("stats") or {}
        if stats:
            parts.append(" ".join(f"{k.upper()} {v}" for k, v in stats.items()))
        self.append_system("LSS: " + " · ".join(parts) +
                           f" → токен сохранён ({Path(path).name})")

    # --- Слоты игроков ---------------------------------------------------
    def on_slots_state(self, data: dict):
        """Сервер прислал список слотов: сохраняем и обновляем окно."""
        slots = data.get('slots')
        if isinstance(slots, dict):
            self.slots = slots
        if getattr(self, "_slots_dialog", None) is not None:
            self._refresh_slots_dialog()
        if self.player_role == Role.DM:
            return
        free = [s for s in self.slots.values() if not s.get('player_id')]
        if self._my_slot() is None and free and not self._slot_prompted:
            self._slot_prompted = True
            self.open_slots_dialog()

    def _my_slot(self):
        for slot_id, slot in self.slots.items():
            if slot.get('player_id') == self.player_id:
                return slot_id
        return None

    def _refresh_slots_dialog(self):
        dialog = getattr(self, "_slots_dialog", None)
        if dialog is None:
            return
        dialog.player_id = self.player_id
        dialog.is_dm = self.player_role == Role.DM
        dialog.set_state(self.slots,
                         list(self.game_state.tokens.values()),
                         self._my_slot())

    def open_slots_dialog(self):
        """Окно слотов: создание и выдача токенов мастером, занятие игроком."""
        dialog = getattr(self, "_slots_dialog", None)
        if dialog is None:
            dialog = SlotsDialog(
                self, is_dm=self.player_role == Role.DM,
                player_id=self.player_id, my_slot=self._my_slot())
            dialog.addRequested.connect(
                lambda: self.network_client.send_message(
                    MessageType.ADD_SLOT, {}))
            dialog.deleteRequested.connect(
                lambda slot_id: self.network_client.send_message(
                    MessageType.DELETE_SLOT, {'slot_id': slot_id}))
            dialog.claimRequested.connect(
                lambda slot_id: self.network_client.send_message(
                    MessageType.CLAIM_SLOT, {'slot_id': slot_id}))
            dialog.assignRequested.connect(
                lambda slot_id, token_id, assigned: self.network_client.send_message(
                    MessageType.ASSIGN_SLOT_TOKEN,
                    {'slot_id': slot_id, 'token_id': token_id,
                     'assigned': assigned}))
            dialog.finished.connect(
                lambda _=0: setattr(self, "_slots_dialog", None))
            self._slots_dialog = dialog
        self._refresh_slots_dialog()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _session_folder(self) -> str:
        """Имя папки сессии в saves/: сохранённый мир или id сессии."""
        name = getattr(self, "_last_world_id", None)
        if not name:
            name = getattr(self.game_state, "session_id", None)
        return world_store.sanitize_session_name(name or "autosave")

    def _lss_target_token(self):
        """Токен, которому принадлежит открытый лист LSS.

        У игрока это его собственный токен-персонаж, у мастера — выбранный
        токен игрока (или единственный такой на карте).
        """
        selected = None
        token_id = getattr(self.canvas, "selected_token", None)
        if token_id:
            selected = self.game_state.tokens.get(token_id)
        if selected is not None and token_kind.is_player(selected):
            if self.player_role == Role.DM or selected.owner_id == self.player_id:
                return selected
        own = [t for t in self.game_state.tokens.values()
               if token_kind.is_player(t) and t.owner_id == self.player_id]
        if self.player_role != Role.DM:
            return own[0] if own else None
        # Мастер без выбора: если на карте ровно один персонаж — он и есть.
        candidates = [t for t in self.game_state.tokens.values()
                      if token_kind.is_player(t)]
        return candidates[0] if len(candidates) == 1 else None

    def open_character_sheet(self, path: str = None):
        """Открыть файл листа (из меню или перетаскиванием)."""
        if path:
            if self.character_sheet.load_file(path):
                self._flush_sheet()
                self.sheet_group.setChecked(True)
                self.append_system(
                    f"Лист персонажа загружен: "
                    f"{Path(path).name}")
        else:
            self.character_sheet.load_dialog()

    def send_roll(self, expr: str, mode=None, dc=None, note: str = ""):
        """Бросить формулу и отправить результат всем (с разбором в чате)."""
        try:
            result = dice_ext.roll(expr, mode=mode)
        except dice_ext.DiceError as exc:
            self.append_system(str(exc) + ". Пример: /roll 1d20+5 adv dc 15")
            return
        self.send_roll_result(result, dc=dc, note=note)

    def send_roll_result(self, result: dict, dc=None, note: str = ""):
        """Отправить уже посчитанный бросок всем участникам.

        Окно кубиков считает результат само (чтобы показать те же числа на
        гранях), поэтому отправка вынесена отдельно: клиент не бросает
        второй раз и не показывает одно, а в чат не уходит другое.
        """
        payload = {
            'dice': result['formula'],
            'result': result['total'],
            # Окно Dice Lab присылает готовый разбор: чат не должен
            # бросать кубики второй раз и показывать другие числа.
            'breakdown': result.get('breakdown')
            or dice_ext.format_roll(result),
            'timestamp': self.now(),
        }
        mode = result.get('mode')
        if mode:
            payload['mode'] = mode
        if dc is not None:
            payload['dc'] = dc
            payload['success'] = dice_ext.check(result['total'], dc)
        if note:
            payload['note'] = note
        self.network_client.send_message(MessageType.ROLL_DICE, payload)

    def on_chat_net(self, data: dict):
        # Чат рисуется богатым текстом, поэтому всё, что пришло из сети,
        # экранируется: иначе игрок мог бы вставить в чужой чат свою разметку
        # (или картинку с внешнего адреса) и замаскировать системные строки.
        from html import escape
        sender = escape(str(data.get('sender', '?')))
        msg = escape(str(data.get('message', '')))
        ts = escape(str(data.get('timestamp', self.now())))
        if data.get('msg_type') == 'whisper':
            recipient = escape(str(data.get('recipient') or ''))
            self.chat_view.append(
                f'<span style="color:#b48ead">[{ts}] {sender} (шёпот) → '
                f'{recipient}: {msg}</span>')
        else:
            self.chat_view.append(f"[{ts}] <b>{sender}</b>: {msg}")

    def on_roll_net(self, data: dict):
        # Бросок приходит от клиента: разбор и заметку тоже экранируем,
        # иначе игрок вставит разметку в общий чат через «свой» бросок.
        from html import escape
        mode = data.get('mode')
        dice = escape(str(data.get('dice', '?')))
        total = escape(str(data.get('result', '?')))
        mode_ru = {'adv': ' (преимущество)', 'dis': ' (недостаток)'}.get(mode, '')
        line = (f'<span style="color:#a3be8c">'
                f'[{escape(str(data.get("timestamp", self.now())))}] '
                f'🎲 {escape(str(data.get("player", "?")))} бросает {dice}{mode_ru}: '
                f'<b>{total}</b>')
        if data.get('breakdown'):
            line += f' = {escape(str(data["breakdown"]))}'
        if data.get('note'):
            line += f' — {escape(str(data["note"]))}'
        if data.get('dc') is not None:
            ok = data.get('success')
            verdict = ('<b style="color:#a3be8c">Успех!</b>' if ok
                       else '<b style="color:#bf616a">Провал!</b>')
            line += f' — DC {data["dc"]}: {verdict}'
        self.chat_view.append(line + '</span>')
        # Отклик поверх холста: бросок видят все, а чат остаётся для
        # сверки формулы и заметки.
        dc = data.get('dc')
        tone = "neutral"
        if dc is not None:
            tone = ROLL_GOOD if data.get('success') else ROLL_BAD
        detail = " · ".join(part for part in (
            str(data.get('player') or ''),
            str(data.get('dice') or '') + mode_ru,
            (f"Сл {dc}: " + ("успех" if data.get('success') else "провал"))
            if dc is not None else '',
            str(data.get('note') or '')) if part)
        self.show_roll_toast("Бросок", data.get('result', '?'), detail, tone)

    def on_denied(self, data: dict):
        self.append_system(f"Сервер отклонил действие: {data.get('action')} "
                           f"(недостаточно прав)")

    def on_log_net(self, data: dict):
        self.append_log(data.get('text', ''))

    def append_log(self, text: str):
        # Журнал — богатый текст: без экранирования имя токена или ник
        # игрока могли бы вставить в него разметку (в том числе ссылку или
        # картинку с чужого адреса).
        from html import escape
        self.log_view.append(f"[{self.now()}] {escape(str(text))}")

    def show_roll_toast(self, title: str, value, detail: str = "",
                        tone: str = "neutral"):
        """Карточка броска поверх холста — одинаковая у всех участников.

        Чат карточка не заменяет: она гаснет через несколько секунд, а
        разбор броска остаётся в чате и в логе для сверки.
        """
        overlay = getattr(self, "roll_overlay", None)
        if overlay is None:
            return None
        text = "—" if value is None else str(value)
        return overlay.push(str(title or ""), text, str(detail or ""), tone)

    def append_system(self, text: str):
        # Системные строки тоже содержат имена токенов из сети.
        from html import escape
        self.chat_view.append(
            f'<span style="color:#d08770">*** {escape(str(text))}</span>')

    # --- Игроки ---
    def refresh_players(self):
        self.players_list.clear()

        # Список владельцев создаётся только в панели мастера.
        is_dm = self.player_role == Role.DM
        if is_dm:
            self.owner_combo.clear()
            self.owner_combo.addItem("— никому (DM) —", None)

        for p in self.game_state.players.values():
            item = QListWidgetItem(f"{p.nickname}  ({p.role.value.upper()})")
            icon_pix = None
            if p.avatar_path and os.path.exists(p.avatar_path):
                icon_pix = QPixmap(p.avatar_path)
            # Игрок не выбрал аватар — показываем общий силуэт, а не пустое
            # место: список без иконок читается хуже.
            if icon_pix is None or icon_pix.isNull():
                theme = getattr(self, "current_theme", "dark")
                fallback = os.path.join(
                    ICON_DIR, "avatar_light.png" if theme == "dark" else "avatar.png")
                if os.path.isfile(fallback):
                    icon_pix = QPixmap(fallback)
            if icon_pix is not None and not icon_pix.isNull():
                item.setIcon(QIcon(icon_pix.scaled(
                    36, 36, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
            self.players_list.addItem(item)
            if is_dm:
                self.owner_combo.addItem(p.nickname, p.id)

    def on_player_joined(self, data: dict):
        from shared.models import Player
        p = Player.from_dict(data['player'])
        self.game_state.players[p.id] = p
        self.refresh_players()
        self.append_log(f"{p.nickname} присоединился")

    def on_player_left(self, data: dict):
        pid = data.get('player_id')
        p = self.game_state.players.pop(pid, None)
        self.refresh_players()
        if p:
            self.append_log(f"{p.nickname} покинул игру")

    # --- Прочее ---
    def reset_tools(self):
        """Вернуться в режим курсора"""
        self.canvas.stop_drawing()
        self._sync_tool_buttons('')

    def toggle_ruler(self):
        """Включить линейку или выключить, если она уже активна."""
        if self.canvas.drawing_mode == 'ruler':
            self.canvas.stop_drawing()
            self._sync_tool_buttons('')
        else:
            self.canvas.start_ruler()
            self._sync_tool_buttons('ruler')

    def activate_tool(self, mode: str):
        """Включить инструмент рисования"""
        self.canvas.start_drawing(mode)
        self._sync_tool_buttons(mode)

    def _sync_tool_buttons(self, mode: str):
        """Отметить активный инструмент на вкладке рисования.

        Кнопки вкладки — обычные QToolButton поверх QAction, поэтому хватает
        снять/поставить галочку на действиях: подсветка кнопки придёт сама.
        """
        for m, act in self.draw_actions.items():
            act.setCheckable(True)
            act.setChecked(m == mode)
        # Линейка живёт отдельной кнопкой в тулбаре: снимаем с неё галочку,
        # когда выбран другой инструмент. Иначе две кнопки выглядели бы
        # включёнными сразу.
        if getattr(self, 'act_ruler', None) is not None:
            self.act_ruler.setChecked(mode == 'ruler')

    # --- Панель надписи (вкладка «Рисование») ---------------------------

    def _load_text_panel(self, style: dict):
        """Показать в панели оформление: выделенной надписи или новой.

        Сигналы на время загрузки глушим: иначе установка значений тут же
        ушла бы обратно в холст и переписала надпись тем, что в ней уже есть.
        """
        style = dict(style or {})
        font = str(style.get('font') or text_rules.DEFAULT_FONT)
        size = int(round(float(style.get('size') or text_rules.DEFAULTS['size'])))
        widgets = [self.text_font_combo, self.text_size_spin]
        widgets += list(self.text_style_buttons.values())
        for widget in widgets:
            widget.blockSignals(True)
        # Шрифт мог прийти из чужого мира и быть не установленным в системе:
        # добавляем его в список, иначе выбор молча съедет на первый.
        if self.text_font_combo.findText(font) < 0:
            self.text_font_combo.addItem(font)
        self.text_font_combo.setCurrentText(font)
        self.text_size_spin.setValue(max(self.text_size_spin.minimum(),
                                        min(self.text_size_spin.maximum(), size)))
        for key, button in self.text_style_buttons.items():
            button.setChecked(bool(style.get(key)))
        for widget in widgets:
            widget.blockSignals(False)
        self._text_color = list(text_rules.rgb(style.get('color')))
        self._refresh_text_color_button()
        self.text_delete_btn.setEnabled(bool(self.canvas.selected_text_id))

    def _sync_text_panel(self, text_id):
        """Холст выбрал надпись (или снял выбор) — панель показывает её вид."""
        if getattr(self, 'text_font_combo', None) is None:
            # Сигнал пришёл, пока панель ещё собирается.
            return
        item = self.canvas.text_by_id(text_id) if text_id else None
        self._load_text_panel(item if item is not None else self.canvas.text_style)

    def on_text_style_changed(self):
        """Панель поменяла оформление: новым надписям сразу, выделенной — тоже."""
        fields = {
            'font': self.text_font_combo.currentText(),
            'size': float(self.text_size_spin.value()),
            'color': list(self._text_color),
        }
        for key, button in self.text_style_buttons.items():
            fields[key] = button.isChecked()
        self.canvas.set_text_style(fields)
        # Оформление могло измениться не ровно так, как просили (шрифт не
        # нашёлся, размер упёрся в предел) — показываем то, что вышло.
        item = self.canvas.text_by_id(self.canvas.selected_text_id)
        if item is not None:
            self._load_text_panel(text_rules.style_of(item))

    def pick_text_color(self):
        """Цвет надписи: белый текст на светлой карте не виден."""
        colour = QColorDialog.getColor(QColor(*self._text_color), self,
                                       "Цвет надписи")
        if not colour.isValid():
            return
        self._text_color = [colour.red(), colour.green(), colour.blue()]
        self._refresh_text_color_button()
        self.on_text_style_changed()

    def _refresh_text_color_button(self):
        """Образец цвета надписи: тот же вид, что у цвета кисти."""
        button = getattr(self, 'text_color_button', None)
        if button is None:
            return
        pix = QPixmap(30, 22)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(*self._text_color))
        painter.setPen(QPen(QColor('#8a8a92'), 1))
        painter.drawRoundedRect(1, 1, pix.width() - 2, pix.height() - 2, 3, 3)
        painter.end()
        button.setIcon(QIcon(pix))
        button.setIconSize(QSize(30, 22))

    def delete_selected_map_text(self):
        """Удалить выбранную надпись — то же, что Del на холсте."""
        self.canvas.delete_selected_text()

    def on_brush_size_changed(self, value: int):
        self.canvas.set_brush_size(float(value))
        self.brush_label.setText(f"{value} px")

    def on_opacity_changed(self, value: int):
        self.canvas.set_brush_opacity(value / 100.0)
        self.opacity_label.setText(f"{value}%")

    def pick_draw_color(self):
        """Выбрать цвет кисти"""
        color = QColorDialog.getColor(QColor(*self.canvas.drawing_color), self,
                                      "Цвет рисования")
        if color.isValid():
            self.canvas.drawing_color = (color.red(), color.green(), color.blue())
            self.canvas.update()
            # Образец на вкладке должен показать новый цвет сразу.
            self._refresh_color_button()

    def undo_drawing(self):
        """Отменить последний штрих у всех"""
        if not self.canvas.draw_ops:
            self.statusBar().showMessage("Рисунок пуст — нечего отменять", 2500)
            return
        map_id = self._current_map_id()
        self.canvas.undo_drawing(emit=False)
        # Свой список тоже укорачиваем: сервер отправителя исключает.
        ops = self.game_state.draw_ops.get(map_id)
        if ops:
            ops.pop()
        self.network_client.send_message(MessageType.UNDO_DRAWING,
                                         {'map_id': map_id})

    def clear_all_drawings(self):
        """Стереть весь рисунок на карте у всех"""
        if not self.canvas.draw_ops:
            self.statusBar().showMessage("Рисунок пуст", 2500)
            return
        answer = QMessageBox.question(self, "Стереть рисунок",
                                      "Удалить весь рисунок на этой карте?",
                                      QMessageBox.Yes | QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        map_id = self._current_map_id()
        self.canvas.clear_drawings(emit=False)
        # И у себя вычищаем: сервер отправителя исключает из рассылки.
        if map_id:
            self.game_state.draw_ops[map_id] = []
        self.network_client.send_message(MessageType.CLEAR_DRAWINGS,
                                         {'map_id': map_id})

    # ------------------------------------------------------------------
    # История действий (Ctrl+Z / Ctrl+Shift+Z)
    # ------------------------------------------------------------------
    def _asset_ref_for(self, asset_id: str):
        """Дескриптор файла для повторной отправки ресурса на сервер.

        Хранится только путь; сами байты читает фоновый поток загрузки.
        Возвращаем None, если файл неизвестен — тогда восстанавливать нечего.
        """
        path = self.local_assets.get(asset_id)
        if path and os.path.exists(path):
            return {'local_path': path}
        return None

    def _token_payload(self, token) -> dict:
        """Полный payload токена для ADD_TOKEN, включая дескриптор картинки."""
        payload = token.to_dict()
        ref = self._asset_ref_for(token.id)
        if ref:
            payload['asset_upload'] = ref
        return payload

    def _map_payload(self, map_data) -> dict:
        payload = map_data.to_dict()
        ref = self._asset_ref_for(map_data.id)
        if ref:
            payload['asset_upload'] = ref
        return payload

    def _push_history(self, label: str, undo, redo, kind: str = 'other',
                      merge_key: str = None):
        """Записать действие. Во время отката записи не происходит."""
        self.history.push(Action(label=label, undo=undo, redo=redo,
                                 kind=kind, merge_key=merge_key))

    def refresh_undo_actions(self):
        """Обновить подписи и доступность кнопок отмены и повтора.

        Вызывается и при создании окна, и подписчиком стека при каждом
        изменении истории.
        """
        can_undo = self.history.can_undo
        self.act_undo.setEnabled(can_undo)
        self.act_undo.setToolTip(
            f"Отменить: {self.history.undo_label} (Ctrl+Z)" if can_undo
            else "Отменять нечего (Ctrl+Z)")

        can_redo = self.history.can_redo
        self.act_redo.setEnabled(can_redo)
        self.act_redo.setToolTip(
            f"Вернуть: {self.history.redo_label} (Ctrl+Shift+Z)" if can_redo
            else "Возвращать нечего (Ctrl+Shift+Z)")

    @staticmethod
    def _keyboard_in_text_field() -> bool:
        """Фокус в поле, куда печатают (чат, поиск, заметки листа).

        Хоткеи-буквы (Q, D, N) живут на всё окно, и без этой проверки они
        съедали бы буквы в заметках листа: «й» взводила бы удар, «в» —
        рывок, «т» — передачу хода. Отличается от ``_text_input_has_focus``
        тем, что проверяет сам виджет, а не наличие правок в нём.
        """
        widget = QApplication.focusWidget()
        if widget is None:
            return False
        if isinstance(widget, (QLineEdit, QTextEdit)):
            return True
        return bool(widget.inherits("QPlainTextEdit"))

    def _text_input_has_focus(self) -> bool:
        """Фокус в поле ввода текста.

        Тогда Ctrl+Z должен отменять ввод в этом поле, а не действие с картой:
        иначе игрок, правящий сообщение в чате, случайно снесёт токен.
        """
        widget = QApplication.focusWidget()
        if widget is None:
            return False
        # Отменять ввод имеет смысл только там, где он есть: пустое поле или
        # счётчик без правок не должен съедать Ctrl+Z у отмены атаки.
        # Раньше любой QLineEdit/QSpinBox с фокусом блокировал общую отмену,
        # и после диалога атаки Ctrl+Z «не работал».
        if isinstance(widget, QTextEdit):
            return widget.document().isUndoAvailable()
        if isinstance(widget, QLineEdit):
            return widget.isUndoAvailable()
        return False

    def undo_last_action(self):
        """Откатить последнее действие: карту, токен или штрих."""
        if self._text_input_has_focus():
            return
        # Пока стена расставляется по точкам, Ctrl+Z снимает последнюю
        # поставленную вершину. Иначе незаконченная ломаная оставалась на
        # экране, а вместо промахнувшейся точки исчезала чужая готовая стена
        # из истории. Когда вершин не осталось — обычная отмена.
        left = len(getattr(self.canvas, "_wall_draft", None) or [])
        if self.canvas.undo_wall_point():
            self.statusBar().showMessage(
                f"Вершина убрана · осталось точек: {left - 1}" if left > 1
                else "Расстановка стены сброшена", 2500)
            return
        if not self.history.can_undo:
            self.statusBar().showMessage("Отменять нечего", 2500)
            return
        try:
            label = self.history.undo()
        except Exception as exc:
            self.append_system(f"Не удалось отменить: {exc}")
            return
        self.statusBar().showMessage(f"Отменено: {label}", 3000)

    def redo_last_action(self):
        """Вернуть отменённое действие."""
        if self._text_input_has_focus():
            return
        if not self.history.can_redo:
            self.statusBar().showMessage("Возвращать нечего", 2500)
            return
        try:
            label = self.history.redo()
        except Exception as exc:
            self.append_system(f"Не удалось вернуть: {exc}")
            return
        self.statusBar().showMessage(f"Возвращено: {label}", 3000)

    # --- запись операций с токенами ---
    def remember_token_added(self, token):
        """Токен добавлен: откат — удалить, повтор — добавить снова."""
        payload = self._token_payload(token)
        name = token.name

        def undo():
            self.network_client.send_message(MessageType.REMOVE_TOKEN,
                                             {'token_id': token.id})

        def redo():
            self.network_client.send_message(MessageType.ADD_TOKEN, payload)

        self._push_history(f'добавление токена «{name}»', undo, redo, kind='token')

    def remember_token_removed(self, token):
        """Токен удалён: сохраняем полный payload, чтобы вернуть его целиком."""
        payload = self._token_payload(token)
        name = token.name

        def undo():
            self.network_client.send_message(MessageType.ADD_TOKEN, payload)

        def redo():
            self.network_client.send_message(MessageType.REMOVE_TOKEN,
                                             {'token_id': token.id})

        self._push_history(f'удаление токена «{name}»', undo, redo, kind='token')

    def remember_tokens_removed(self, tokens: list):
        payloads = [self._token_payload(t) for t in tokens]
        ids = [t.id for t in tokens]
        count = len(ids)

        def undo():
            for pay in payloads:
                self.network_client.send_message(MessageType.ADD_TOKEN, pay)

        def redo():
            for tid in ids:
                self.network_client.send_message(MessageType.REMOVE_TOKEN,
                                                 {'token_id': tid})

        self._push_history(f'удаление токенов: {count} шт.', undo, redo,
                           kind='token')

    def remember_token_updated(self, token, before: dict, after: dict):
        """Изменение свойств токена: размер, имя, владелец."""
        if not before and not after:
            return
        name = token.name
        undo_data = {'token_id': token.id, **before}
        redo_data = {'token_id': token.id, **after}
        what = 'размер' if 'size' in after else ('имя' if 'name' in after
                                                else 'свойства')

        def undo():
            self.network_client.send_message(MessageType.UPDATE_TOKEN, undo_data)

        def redo():
            self.network_client.send_message(MessageType.UPDATE_TOKEN, redo_data)

        self._push_history(f'изменение ({what}) токена «{name}»', undo, redo,
                           kind='token',
                           merge_key=f'token-update:{token.id}:{what}')

    def on_canvas_edit_finished(self, data: dict):
        """Холст закончил перетаскивание или ресайз — пишем действие в историю."""
        kind = data.get('kind')
        before = data.get('before') or {}
        after = data.get('after') or {}
        if not before or not after:
            return

        if kind == 'tokens':
            points = []
            for tid in after:
                old = before.get(tid)
                new = after.get(tid)
                if old is None or new is None:
                    continue
                if (abs(old[0] - new[0]) > 0.01) or (abs(old[1] - new[1]) > 0.01):
                    points.append((tid, old, new))
            if not points:
                return
            # Пройденный путь: по нему и назад, и снова вперёд. Иначе отмена
            # шла бы по прямой и упиралась в ту же самую стену.
            trails = data.get('path') or {}

            def route(tid, old, new, forward: bool):
                """Шаги перемещения для одного токена."""
                waypoints = trails.get(tid)
                if waypoints and len(waypoints) > 2:
                    steps = [tuple(point) for point in waypoints][1:]
                    if not forward:
                        steps = list(reversed(
                            [tuple(point) for point in waypoints]))[1:]
                    return [{'token_id': tid, 'x': point[0], 'y': point[1]}
                            for point in steps]
                target = new if forward else old
                return [{'token_id': tid, 'x': target[0], 'y': target[1]}]

            moves_back = []
            moves_fwd = []
            for tid, old, new in points:
                moves_back.extend(route(tid, old, new, False))
                moves_fwd.extend(route(tid, old, new, True))
            count = len(points)
            label = ('перемещение токена' if count == 1
                     else f'перемещение токенов: {count} шт.')

            def undo():
                self.network_client.send_message(MessageType.MOVE_TOKEN,
                                                 {'tokens': moves_back})

            def redo():
                self.network_client.send_message(MessageType.MOVE_TOKEN,
                                                 {'tokens': moves_fwd})

            self._push_history(label, undo, redo, kind='token')
            return

        if kind == 'map_geometry':
            mid = next(iter(after))
            old = before.get(mid)
            new = after.get(mid)
            if not old or not new:
                return
            m = self.game_state.maps.get(mid)
            name = m.name if m else 'карта'
            size_changed = (abs(old.get('width', 0) - new.get('width', 0)) > 0.01
                            or abs(old.get('height', 0) - new.get('height', 0)) > 0.01)
            label = (f'изменение размера карты «{name}»' if size_changed
                     else f'перемещение карты «{name}»')

            def undo():
                self.network_client.send_message(MessageType.UPDATE_MAP, dict(old))

            def redo():
                self.network_client.send_message(MessageType.UPDATE_MAP, dict(new))

            self._push_history(label, undo, redo, kind='map',
                               merge_key=f'map-geometry:{mid}')

    # --- запись операций с картами ---
    def remember_map_removed(self, map_data, orphan_tokens: list = None):
        """Карта удалена. Возвращаем её и её токены — сервер сносит токены вместе с картой."""
        payload = self._map_payload(map_data)
        token_payloads = [self._token_payload(t) for t in (orphan_tokens or [])]
        name = map_data.name
        mid = map_data.id

        def undo():
            self.network_client.send_message(MessageType.ADD_MAP, payload)
            for pay in token_payloads:
                self.network_client.send_message(MessageType.ADD_TOKEN, pay)

        def redo():
            self.network_client.send_message(MessageType.REMOVE_MAP,
                                             {'map_id': mid})

        self._push_history(f'удаление карты «{name}»', undo, redo, kind='map')

    def _current_map_id(self):
        return self.canvas.current_map.id if self.canvas.current_map else None

    # --- Сеть: растровый рисунок ---

    def on_draw_op_local(self, op: dict):
        """Свой штрих: запоминаем у себя и рассылаем остальным.

        Запомнить обязательно. Раньше операция только уходила в сеть, а
        сервер рассылает её всем, КРОМЕ отправителя. В результате в
        собственном game_state.draw_ops этой карты не оставалось ничего,
        и при возвращении на карту рисунок восстанавливать было не из чего —
        он пропадал.
        """
        map_id = self._current_map_id()
        if map_id:
            self.game_state.draw_ops.setdefault(map_id, []).append(op)
        self.network_client.send_message(MessageType.DRAW_OP, {
            'map_id': map_id,
            'op': op,
        })
        # Штрих тоже становится обратимым действием: иначе Ctrl+Z после
        # перемещения токена откатывал бы не то, что видит пользователь.
        if map_id:
            def undo(_mid=map_id):
                self.canvas.undo_drawing(emit=False)
                ops = self.game_state.draw_ops.get(_mid)
                if ops:
                    ops.pop()
                self.network_client.send_message(MessageType.UNDO_DRAWING,
                                                 {'map_id': _mid})

            def redo(_mid=map_id, _op=op):
                # Локально применяем сами: сервер рассылает штрих всем, КРОМЕ
                # отправителя, поэтому без этого шага его не будет видно.
                self.game_state.draw_ops.setdefault(_mid, []).append(_op)
                self.canvas.apply_remote_draw_op(_op)
                self.network_client.send_message(MessageType.DRAW_OP,
                                                 {'map_id': _mid, 'op': _op})

            self._push_history('штрих на карте', undo, redo, kind='draw')

    def on_draw_op_net(self, data: dict):
        """Пришёл штрих от другого игрока"""
        map_id = data.get('map_id')
        if map_id and self.canvas.current_map and map_id != self.canvas.current_map.id:
            # Рисунок для другой карты — запомним, но на холст не кладём
            ops = self.game_state.draw_ops.setdefault(map_id, [])
            ops.append(data.get('op'))
            return
        op = data.get('op')
        if op:
            self.canvas.apply_remote_draw_op(op)

    def on_clear_drawings_net(self, data: dict):
        """Кто-то стёр весь рисунок"""
        map_id = data.get('map_id')
        if map_id and self.canvas.current_map and map_id != self.canvas.current_map.id:
            self.game_state.draw_ops[map_id] = []
            return
        self.canvas.clear_drawings(emit=False)

    def on_undo_drawing_net(self, data: dict):
        """Кто-то отменил последний штрих"""
        map_id = data.get('map_id')
        if map_id and self.canvas.current_map and map_id != self.canvas.current_map.id:
            ops = self.game_state.draw_ops.get(map_id, [])
            if ops:
                ops.pop()
            return
        self.canvas.undo_drawing(emit=False)

    # --- Сеть: текстовые надписи ---------------------------------------

    def _store_map_text(self, map_id: str, item: dict, append: bool = True):
        """Записать надпись в состояние стола по её id.

        ``append=False`` — только замена уже известной надписи: правка для
        незнакомого id означает, что добавление до нас не дошло, и плодить
        надпись, которой нет у остальных, нельзя.
        """
        if not map_id or not isinstance(item, dict) or not item.get('id'):
            return
        items = self.game_state.map_texts.setdefault(map_id, [])
        for index, old in enumerate(items):
            if str(old.get('id') or '') == str(item.get('id')):
                items[index] = item
                return
        if append:
            items.append(item)

    def on_map_text_added(self, item: dict):
        """Своя новая надпись: запоминаем у себя и сообщаем остальным.

        id предлагает клиент, а сервер его сохраняет: эхо своей надписи
        придёт с тем же id, и она не задвоится ни на холсте, ни в мире.
        """
        map_id = self._current_map_id()
        if not map_id:
            return
        self._store_map_text(map_id, item)
        self.network_client.send_message(MessageType.ADD_MAP_TEXT,
                                         {'map_id': map_id, 'item': item})

    def on_map_text_updated(self, text_id: str, patch: dict):
        """Своя правка или перенос надписи: id плюс изменённые поля."""
        map_id = self._current_map_id()
        item = self.canvas.text_by_id(text_id)
        if not map_id or item is None:
            return
        self._store_map_text(map_id, item)
        self.network_client.send_message(MessageType.UPDATE_MAP_TEXT, {
            'map_id': map_id, 'text_id': str(text_id),
            'patch': dict(patch or {}),
        })

    def on_map_text_removed(self, text_id: str):
        """Своя надпись удалена"""
        map_id = self._current_map_id()
        if not map_id:
            return
        self.game_state.map_texts[map_id] = [
            row for row in self.game_state.map_texts.get(map_id, [])
            if str(row.get('id') or '') != str(text_id)]
        self.network_client.send_message(MessageType.REMOVE_MAP_TEXT,
                                         {'map_id': map_id,
                                          'text_id': str(text_id)})

    def on_map_text_added_net(self, data: dict):
        """Надпись появилась у кого-то (или сервер подтвердил нашу)"""
        map_id = str(data.get('map_id') or '')
        item = data.get('item')
        if not map_id or not isinstance(item, dict):
            return
        self._store_map_text(map_id, item)
        if self._current_map_id() == map_id:
            self.canvas.apply_text_added(item)

    def on_map_text_updated_net(self, data: dict):
        """Надпись поправили или перенесли"""
        map_id = str(data.get('map_id') or '')
        item = data.get('item')
        if not map_id or not isinstance(item, dict) or not item.get('id'):
            return
        self._store_map_text(map_id, item, append=False)
        if self._current_map_id() == map_id:
            self.canvas.apply_text_updated(item)

    def on_map_text_removed_net(self, data: dict):
        """Надпись стёрли (в том числе в чужой карте — просто забываем)"""
        map_id = str(data.get('map_id') or '')
        text_id = str(data.get('text_id') or '')
        if not map_id or not text_id:
            return
        self.game_state.map_texts[map_id] = [
            row for row in self.game_state.map_texts.get(map_id, [])
            if str(row.get('id') or '') != text_id]
        if self._current_map_id() == map_id:
            self.canvas.apply_text_removed(text_id)

    def on_tool_changed(self, mode: str):
        """Холст сменил инструмент — обновляем кнопки"""
        self._sync_tool_buttons(mode)

    def apply_theme(self, name: str):
        self.current_theme = 'dark' if name == 'dark' else 'light'
        if name == 'dark':
            self.setStyleSheet(DARK_QSS)
        else:
            self.setStyleSheet(LIGHT_QSS)
        # Заголовок окна рисует Windows: без этого он оставался светлым
        # (или тёмным) независимо от темы приложения.
        apply_window_frame(self, self.current_theme == 'dark')
        guide = getattr(self, "_guide_dialog", None)
        if guide is not None:
            guide.set_theme(self.current_theme)
        support = getattr(self, "_support_dialog", None)
        if support is not None:
            support.set_theme(self.current_theme)
        self.canvas.update()
        self.refresh_section_icons()

    def refresh_section_icons(self):
        """Переставить иконки разделов под текущую тему.

        Тёмный рисунок на тёмном фоне не виден, поэтому у каждой иконки два
        файла и мы выбираем подходящий.
        """
        theme = getattr(self, "current_theme", "dark")
        # Порядок строго как у разделов в панели. Раньше здесь стояло пять
        # имён на четыре раздела: «world» сдвигало справочник, и он получал
        # чужую иконку — поэтому казалось, что раздел пропал.
        names = ("maps", "tokens", "music", "reference", "my_actors", "sheet")
        tabs = getattr(self, "section_tabs", None)
        for index, name in enumerate(names):
            if tabs is not None and index < tabs.count():
                tabs.setTabIcon(
                    index, section_icon(name, SECTION_FALLBACK.get(name, ""),
                                        theme))

        # Правая рейка: чат, игроки, звук и последняя — рисование.
        right_names = ("tab_chat", "tab_players", "tab_sound")
        tabs = getattr(self, "right_tabs", None)
        if tabs is not None:
            for index, name in enumerate(right_names):
                if index < tabs.count():
                    tabs.setTabIcon(index, tool_icon(name, theme))
            draw_index = getattr(self, "_draw_tab_index", None)
            if draw_index is not None and draw_index < tabs.count():
                tabs.setTabIcon(draw_index, tool_icon("draw", theme))

        # Иконки инструментов и корзины тоже перекрашиваются под тему.
        for mode, act in getattr(self, "draw_actions", {}).items():
            name = DRAW_TOOL_ICONS.get(mode)
            if name:
                act.setIcon(tool_icon(name, theme))
        # Иконки панели текста: начертание и удаление тоже в двух темах.
        for key, button in getattr(self, "text_style_buttons", {}).items():
            name = TEXT_STYLE_ICONS.get(key)
            if name:
                button.setIcon(tool_icon(name, theme))
        text_delete = getattr(self, "text_delete_btn", None)
        if text_delete is not None:
            text_delete.setIcon(tool_icon("text_delete", theme))
        clear_btn = getattr(self, "clear_draw_button", None)
        if clear_btn is not None:
            clear_btn.setIcon(tool_icon("clear", theme))
        # Глазки и замки в списках: у них тоже по два файла на тему.
        for oid, button in getattr(self, "object_eye_buttons", {}).items():
            obj = self.game_state.map_objects.get(oid)
            if obj is not None:
                button.setIcon(self._eye_icon(obj.visible))
        for oid, button in getattr(self, "object_lock_buttons", {}).items():
            obj = self.game_state.map_objects.get(oid)
            if obj is not None:
                button.setIcon(self._lock_icon(obj.locked))
        for tid, button in getattr(self, "token_eye_buttons", {}).items():
            tok = self.game_state.tokens.get(tid) or self.canvas.tokens.get(tid)
            if tok is not None:
                button.setIcon(self._eye_icon(tok.visible))
        # Кнопки ходов боя: иконки тоже есть в двух темах.
        for attr, icon_name in (("turn_prev_btn", "nav_prev"),
                                ("turn_next_btn", "nav_next"),
                                ("combat_end_btn", "nav_finish")):
            button = getattr(self, attr, None)
            if button is not None:
                button.setIcon(tool_icon(icon_name, theme))
        self._refresh_color_button()

    def now(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def keyPressEvent(self, event):
        # Tab обрабатывает сам холст: там он однозначен и не мешает полям
        # ввода. Здесь Tab не перехватываем — иначе он срабатывал бы и из чата,
        # потому что фокус успевает перейти на холст до этого обработчика.
        super().keyPressEvent(event)

    def closeEvent(self, event):
        # Сначала останавливаем всё, что может позвать окно после закрытия.
        # Раньше сокет и фоновые потоки оставались жить, и поздний
        # ASSET_READY/ERROR приходил в уже удалённый MainWindow — процесс
        # падал в sip.cp313-win_amd64.pyd с 0xc0000005.
        self.link_timer.stop()
        for attr in ("_sheet_timer", "_page_sheet_timer"):
            timer = getattr(self, attr, None)
            if timer is not None:
                timer.stop()
        dice_window = getattr(self, "_dice_window", None)
        if dice_window is not None:
            dice_window.close()
        try:
            self.network_client.send_message(MessageType.DISCONNECT, {})
        except Exception:
            pass
        shutdown = getattr(self.network_client, "shutdown", None)
        try:
            if callable(shutdown):
                shutdown()
            else:
                disconnect = getattr(self.network_client, "disconnect", None)
                if callable(disconnect):
                    disconnect()
        except Exception:
            logging.exception("Не удалось остановить сетевой клиент")
        if self.server:
            self.server.stop()
        event.accept()



# --- Иконки разделов левой панели ---------------------------------------



DARK_QSS = """
/* Нейтральный серый с бронзовым акцентом: без цветных заливок, тонкие
   границы, всё внимание на карте и тексте. Контраст ≥4.5:1, фокус виден. */
* {
    font-family: "Segoe UI Variable Text", "Segoe UI", "Inter",
                 "DejaVu Sans", sans-serif;
    font-size: 10pt;
}
QLabel[heading="true"], QGroupBox::title, #section_title {
    font-family: "Georgia", "Cambria", "DejaVu Serif", serif;
}

QMainWindow, QWidget { background-color: #19191c; color: #e5e1d8; }
QScrollArea { background: transparent; border: none; }
QAbstractItemView { outline: 0; }

#section_host { background: #202024; }
#section_title {
    background: #1c1c1f;
    color: #c9a35f;
    font-size: 10.5pt;
    font-weight: 600;
    letter-spacing: 1.4px;
    padding: 11px 13px 10px 13px;
    border-bottom: 1px solid #2a2a2e;
}

#section_rail_host, #section_rail { background: #17171a; }
#rail_button {
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
}
#rail_button:hover { background-color: #26262a; border-color: #3a3a40; }
#rail_button:checked {
    background-color: #2a2822;
    border-color: #b08d4f;
}

#draw_tool {
    background-color: #26262a;
    border: 1px solid #34343a;
    border-radius: 5px;
}
#draw_tool:hover { background-color: #303036; border-color: #4a4a52; }
#draw_tool:checked {
    background-color: #2a2822;
    border-color: #b08d4f;
}
/* Инструменты стен: выбранный должен быть виден сразу, как кисти
   в рисовании — иначе непонятно, чем работаешь, и что уже нажато. */
QToolButton#wall_tool_btn, QToolButton#wall_delete_btn {
    background-color: #26262a;
    border: 1px solid #34343a;
    border-radius: 5px;
    padding: 0;
}
QToolButton#wall_tool_btn:hover, QToolButton#wall_delete_btn:hover {
    background-color: #303036;
    border-color: #4a4a52;
}
QToolButton#wall_tool_btn:checked, QToolButton#wall_delete_btn:checked {
    background-color: #3a2d18;
    border: 1px solid #e0b262;
}
#draw_color {
    background-color: #202023;
    border: 1px solid #34343a;
    border-radius: 5px;
    padding: 2px;
}
#draw_color:hover { border-color: #b08d4f; }
#draw_hint { color: #8c8c94; font-size: 11px; }
/* Размер надписи: рядом с кнопками нужна видимая рамка, иначе поле
   теряется — общее правило оставляет у полей только линию снизу. */
#text_size_spin {
    background-color: #202023;
    border: 1px solid #34343a;
    border-radius: 5px;
    padding: 2px 4px;
}
#text_size_spin:hover { border-color: #4a4a52; }
#text_size_spin:focus { border-color: #b08d4f; }
/* Группа текста — отдельный блок, а не строка в общем списке кисти:
   иначе её поля читаются как настройки толщины и непрозрачности. */
#panel_group {
    background-color: #1e1e22;
    border: 1px solid #2c2c31;
    border-radius: 8px;
    margin-top: 10px;
    padding: 0;
}
#panel_group::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 8px;
    padding: 0 5px;
    color: #c9a35f;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 1px;
}

/* Хиты в листе: у мастера это поле правки, у игрока — подпись.
   Рамку показываем только при вводе, чтобы цифра выглядела как число. */
#sheet_hp_spin {
    background: transparent; border: 1px solid transparent;
    padding: 0px; font-weight: 700;
}
#sheet_hp_spin:focus { border-color: #b08d4f; background-color: #141416; }

QGroupBox {
    border: none;
    margin-top: 16px; padding: 2px 0 0 0;
    background: transparent;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 0; padding: 0 2px;
    color: #c9a35f; font-weight: 600; font-size: 9pt;
    letter-spacing: 0.8px;
}

QLabel { color: #e5e1d8; background: transparent; }
QLabel:disabled { color: #6d6a64; }

/* Типографика листа: текст важнее рамок, поэтому у ячеек нет фона. */
#sheet_name { font-family: "Georgia", "Cambria", "DejaVu Serif", serif;
              color: #f3efe6; letter-spacing: 0.2px; }
#sheet_sub { color: #99958c; font-size: 9pt; }
#sheet_section { color: #c9a35f; font-size: 8.5pt; font-weight: 700;
                 letter-spacing: 1.5px; padding-top: 4px; }
#sheet_cell { background: transparent; border: none; }
#sheet_cell_title { color: #908c84; font-size: 7.8pt; font-weight: 700;
                    letter-spacing: 0.8px; }
#sheet_vital { color: #f3efe6; font-size: 15pt; font-weight: 700; }
#sheet_ability { color: #f3efe6; font-size: 13pt; font-weight: 700; }
#sheet_skill_prof { color: #e8d5aa; }
/* Особенности класса и расы: кнопка — действие с формулой, чип —
   пассивное правило. Оба живут в одной строке под характеристиками. */
QPushButton#sheet_feature_btn {
    text-align: left; padding: 4px 9px; border-radius: 4px;
    border: 1px solid #3f3b33; background: #232327; color: #e8d5aa;
}
QPushButton#sheet_feature_btn:hover {
    background: #2b2a2e; border-color: #b08d4f;
}
QLabel#sheet_feature_chip {
    color: #a9a49b; padding: 4px 2px; font-size: 9pt;
}

/* Атаки: строки-карточки как в интерактивном листе LSS. В строке только
   чтение и действия, правка живёт в окне атаки. */
QFrame#atk_row_card {
    background: #232327; border: 1px solid #34343a; border-radius: 6px;
}
QFrame#atk_row_card:hover { background: #26262b; border-color: #6f5a33; }
QToolButton#atk_row_name {
    border: none; background: transparent; color: #e5e1d8;
    font-size: 10.2pt; padding: 1px 2px; text-align: left;
}
QToolButton#atk_row_name:hover { color: #f0d9a8; }
QToolButton#atk_row_bonus {
    background: #2b2a33; border: 1px solid #4a4550; border-radius: 6px;
    color: #f2cc76; font-size: 11.5pt; font-weight: 700; padding: 2px 0;
}
QToolButton#atk_row_bonus:hover {
    background: #332f2a; border-color: #b08d4f; color: #ffe3a8;
}
QLabel#atk_row_damage {
    background: #26262c; border: 1px solid #3a3a44; border-radius: 5px;
    color: #cfc8bb; font-size: 9.4pt; padding: 3px 7px;
}
QToolButton#atk_row_info {
    background: transparent; border: 1px solid #3f3b45;
    border-radius: 11px; color: #8f8a80; font-size: 8.4pt;
    font-weight: 700; padding: 0;
}
QToolButton#atk_row_info:hover {
    color: #f0d9a8; border-color: #b08d4f;
}
QToolButton#sheet_add_attack {
    background-color: #232327; border: 1px solid #3f3b33;
    border-radius: 11px; color: #d6b877; font-size: 11pt;
    font-weight: 700; padding: 0;
}
QToolButton#sheet_add_attack:hover {
    background-color: #2d2d32; border-color: #b08d4f; color: #f0d9a8;
}
QToolButton#sheet_add_attack:pressed { background-color: #1c1c20; }
QToolButton#sheet_add_attack:checked {
    background: #3a2b24; border-color: #b06a4a; color: #f0b3a0;
}
QToolButton#sheet_quick_chip {
    background: transparent; border: 1px solid #34343a;
    border-radius: 4px; color: #85838b; font-size: 7.6pt;
    font-weight: 700; padding: 0;
}
QToolButton#sheet_quick_chip:hover { border-color: #6f5a33; color: #d6b877; }
QToolButton#sheet_quick_chip:checked {
    background: #2a2822; border-color: #b08d4f; color: #f2cc76;
}
QToolButton#sheet_attack_remove { padding: 0; color: #8f8a80; }
QToolButton#sheet_attack_remove:hover { color: #d08770; }

/* Окно атаки: разделы-карточки, как на LSS. */
#atk_dialog, #atk_body { background: #1a1a1f; }
QLineEdit#atk_name {
    background: transparent; border: none;
    border-bottom: 1px solid #3a3a42; color: #f3efe6; padding: 3px 2px;
}
QLineEdit#atk_name:focus { border-bottom-color: #b08d4f; }
#atk_summary { color: #c9c4b8; font-size: 9.5pt; }
#atk_span { color: #7f7b73; font-size: 8.5pt; }
#atk_badge {
    color: #f2cc76; font-size: 16pt; font-weight: 700;
    background: #24222a; border: 1px solid #4a4048; border-radius: 29px;
}
#atk_badge_caption {
    color: #8b857a; font-size: 7.4pt; font-weight: 700;
    letter-spacing: 1px;
}
QFrame#atk_section {
    background: #222228; border: 1px solid #33333b; border-radius: 8px;
}
#atk_section_title {
    color: #c9a35f; font-size: 8.4pt; font-weight: 700;
    letter-spacing: 1.2px;
}
QToolButton#atk_add {
    background: #2a2830; border: 1px solid #413d4a; border-radius: 5px;
    color: #d6b877; font-size: 8.4pt; padding: 2px 8px;
}
QToolButton#atk_add:hover { border-color: #b08d4f; color: #f0d9a8; }
QToolButton#atk_toggle {
    background: #24242b; border: 1px solid #3a3a44; border-radius: 6px;
    color: #a9a49b; font-size: 9pt; padding: 3px 8px; min-height: 24px;
}
QToolButton#atk_toggle:hover { border-color: #6f5a33; }
QToolButton#atk_toggle:checked {
    background: #2f2a22; border-color: #b08d4f; color: #f2cc76;
    font-weight: 600;
}
QComboBox#atk_combo, QLineEdit#atk_formula {
    background: #24242b; border: 1px solid #3a3a44; border-radius: 5px;
    color: #e5e1d8; padding: 4px 6px;
}
QComboBox#atk_combo:hover, QLineEdit#atk_formula:hover {
    border-color: #6f5a33;
}
QLineEdit#atk_formula:focus, QComboBox#atk_combo:focus {
    border-color: #b08d4f;
}
#atk_field_caption {
    color: #908c84; font-size: 7.8pt; font-weight: 700;
    letter-spacing: 0.8px;
}
#atk_chip {
    background: #2b2a33; border: 1px solid #403c47; border-radius: 5px;
    color: #cfc8bb; font-size: 8.6pt; padding: 3px 4px;
}
QFrame#atk_row {
    background: #26262c; border: 1px solid #34343c; border-radius: 5px;
}
QLineEdit#atk_row_label, QLineEdit#atk_row_expr {
    background: transparent; border: 1px solid transparent;
    border-radius: 4px; color: #e5e1d8; padding: 2px 4px;
}
QLineEdit#atk_row_label:hover, QLineEdit#atk_row_expr:hover {
    background: #2c2c33;
}
QLineEdit#atk_row_label:focus, QLineEdit#atk_row_expr:focus {
    background: #1e1e23; border-color: #b08d4f;
}
QToolButton#atk_roll {
    background: transparent; border: 1px solid #3f3b45; border-radius: 4px;
    color: #a9a49b; font-size: 8pt; font-weight: 700;
}
QToolButton#atk_roll:hover { color: #f0d9a8; border-color: #b08d4f; }
QToolButton#atk_roll:disabled { color: #55525c; border-color: #2f2f36; }
QToolButton#atk_row_remove {
    background: transparent; border: none; color: #8f8a80; font-size: 11pt;
}
QToolButton#atk_row_remove:hover { color: #d08770; }
QPlainTextEdit#atk_notes {
    background: #202026; border: 1px solid #33333b; border-radius: 5px;
    color: #e5e1d8; padding: 6px; selection-background-color: #6f5325;
}
QPlainTextEdit#atk_notes:focus { border-color: #b08d4f; }
#atk_hint { color: #77736b; font-size: 8.5pt; padding: 2px 4px; }
QPushButton#atk_delete {
    background: transparent; border: 1px solid #6b3a3a; color: #d08770;
}
QPushButton#atk_delete:hover {
    background: #31232a; border-color: #a35a4a; color: #f0b3a0;
}
QPushButton#atk_done {
    background: #2f2a22; border: 1px solid #b08d4f; color: #f2cc76;
    font-weight: 600;
}
QPushButton#atk_done:hover { background: #3a3328; }

/* Карточка броска поверх холста: видна всем, гаснет сама. */
#roll_toast_title {
    color: #9a94a8; font-size: 8.2pt; font-weight: 700; letter-spacing: 1.3px;
}
#roll_toast_value {
    color: #ffffff; font-size: 22pt; font-weight: 700;
}
#roll_toast_detail { color: #b9b3aa; font-size: 8.6pt; }
#sheet_note_hint { color: #7c7a73; font-size: 8pt; }
#sheet_note_title { color: #908c84; font-size: 8pt; font-weight: 700;
                    letter-spacing: 0.6px; }
QPlainTextEdit#sheet_notes {
    background: #202024; border: 1px solid #2c2c31; border-radius: 5px;
    padding: 6px 8px; color: #e5e1d8; selection-background-color: #6f5325;
}
QPlainTextEdit#sheet_notes:focus {
    background: #1d1d21; border-color: #b08d4f;
}

QPushButton {
    background-color: #26262a; border: 1px solid #36363c;
    border-radius: 3px; padding: 6px 11px; color: #d8d6d2;
    min-height: 17px;
}
QPushButton:hover { background-color: #2e2e33; border-color: #55524a; }
QPushButton:focus { outline: none; border-color: #6f5a33; }
QPushButton:pressed { background-color: #212125; }
QPushButton:disabled { background-color: #1e1e21; color: #5c5c62;
                       border-color: #2a2a2e; }
QPushButton:checked { background-color: #2a2822; border-color: #b08d4f;
                      color: #d6b877; }
/* Кнопки ходов боя: только иконка, одинаковый размер, не тянутся на всю
   колонку. Светлый акцент при наведении — как у кнопок Windows. */
QPushButton#nav_button {
    background-color: #222226; border: 1px solid #36363c;
    border-radius: 4px; padding: 0;
}
QPushButton#nav_button:hover { background-color: #2e2e33; border-color: #b08d4f; }
QPushButton#nav_button:pressed { background-color: #1c1c20; }
QPushButton#nav_button:disabled { background-color: #1e1e21;
                                  border-color: #2a2a2e; }
QPushButton#nav_button_end:hover { border-color: #d08770; }
QLabel#token_size_value { color: #c8a45e; font-weight: 600; }

QToolButton {
    background-color: transparent; border: 1px solid transparent;
    border-radius: 3px; padding: 5px 9px; color: #d8d6d2;
}
QToolButton:hover { background-color: #26262a; border-color: #36363c; }
QToolButton:focus { outline: none; border-color: #6f5a33; }
QToolButton:pressed { background-color: #212125; }
QToolButton::menu-indicator { image: none; }
/* Строки «Объекты» и сцены: у обычной QToolButton отступы 5x9 px, из-за
   чего в кнопке 24 px от иконки оставалась полоска. Здесь рисуем без них. */
QToolButton#map_object_eye_btn,
QToolButton#map_object_lock_btn,
QToolButton#map_object_gear_btn,
QToolButton#light_eye_btn,
QToolButton#light_lock_btn,
QToolButton#light_preset_btn,
QToolButton#token_eye_btn { padding: 0; }
/* Персонажи на других картах: тихий заголовок и компактная кнопка
   переноса — она должна находиться сразу, но не спорить с боевой панелью. */
QLabel#away_tokens_title { color: #8d8a83; font-size: 11px; }
QToolButton#away_token_move_btn {
    padding: 1px 7px; border: 1px solid #3c3a33; border-radius: 4px;
    color: #d8c79a; font-size: 11px;
}
QToolButton#away_token_move_btn:hover {
    background-color: #2a2822; border-color: #b08d4f;
}

/* Поля — не коробки, а строки в тексте: тонкая линия снизу, полная
   рамка только там, где без неё не понятно (фокус). */
QLineEdit, QSpinBox, QDoubleSpinBox {
    background: transparent; border: none;
    border-bottom: 1px solid #33333a; border-radius: 0;
    padding: 5px 3px; color: #e9e5dc;
    selection-background-color: #7c6335; selection-color: #f8f4ea;
}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover {
    border-bottom-color: #4a4740;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border-bottom-color: #c9a35f;
}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
    color: #6d6a64; border-bottom-color: #2a2a2e;
}
QSpinBox QLineEdit, QDoubleSpinBox QLineEdit, QComboBox QLineEdit {
    background: transparent; border: none; padding: 0;
}

QComboBox {
    background: #202024; border: none; border-radius: 6px;
    padding: 6px 9px; color: #e9e5dc;
    selection-background-color: #7c6335; selection-color: #f8f4ea;
}
QComboBox:hover { background: #26262a; }
QComboBox:focus { background: #26262a; }
/* У выпадающих списков намеренно не описан подконтрол со стрелкой
   (drop-down): как только он попадает в таблицу стилей, Qt перестаёт
   рисовать свою стрелку, и список выглядел обычным полем ввода без выбора. */
QComboBox QAbstractItemView {
    background-color: #202024; border: none; border-radius: 6px;
    padding: 4px; selection-background-color: #2e2a22;
    selection-color: #e8c98a;
}

/* Многострочный текст: мягкая панель без рамки, без «клеток». */
QTextEdit, QPlainTextEdit {
    background: #1e1e22; border: none; border-radius: 8px;
    padding: 10px 12px; color: #e3dfd7;
    selection-background-color: #7c6335; selection-color: #f8f4ea;
}

/* Списки растворяются в фоне: строки выделяются оттенком, а не рамкой
   и не серой заливкой. */
QListWidget, QTreeView, QTableView {
    background: transparent; border: none; padding: 2px;
    color: #e3dfd7; outline: 0;
    selection-background-color: #2e2a22; selection-color: #e8c98a;
}
QListWidget::item { padding: 6px 8px; border-radius: 6px; border: none; }
QListWidget::item:hover { background-color: #242427; }
QListWidget::item:selected { background-color: #2e2a22; color: #e8c98a; }
/* Список сцены: выбранная строка заметно ярче остальных — мастер должен
   с первого взгляда видеть, кто выделен на холсте. У строки свой виджет,
   поэтому подсветка нужна и в неактивном окне. */
#tokens_list::item:selected { background-color: #4a3d20; }
#tokens_list::item:selected:!active { background-color: #3d3320; }
/* Карточки действий справочника: название сверху, эффект снизу. Сетка
   из карточек читается быстрее, чем столбик кнопок во всю ширину. */
#action_card {
    background: #202024; border: 1px solid #2f2f35; border-radius: 6px;
}
#action_card[actionable="true"]:hover {
    background: #26262a; border-color: #7c6335;
}
#action_card_title { color: #efe9dc; font-size: 9pt; font-weight: 600; }
#action_card_sub { color: #a29b8e; font-size: 8pt; }
#lib_card_view {
    background: #1e1e22; border: none; border-radius: 8px;
    padding: 10px 12px;
}
#action_cards_board, #lib_actions_scroll { background: transparent; }
QListWidget::item:focus { border: none; outline: none; }
/* Карточки актёров и предметов рисуют выделение сами (CardSelectionDelegate):
   фон и рамка — у всей ячейки, без синей заливки PNG и пунктира текста. */
#my_actors_grid, #inventory_list { outline: 0; }
#my_actors_grid::item:selected, #inventory_list::item:selected {
    background: transparent; color: palette(text);
}
#my_actors_grid::item:focus, #inventory_list::item:focus { border: none; }

QTabWidget::pane {
    border: none; background: transparent; top: 0;
}
QTabBar::tab {
    background: transparent; color: #8d8981;
    padding: 9px 12px 8px 12px; margin-right: 7px;
    border: none; border-bottom: 2px solid transparent;
}
QTabBar::tab:hover { color: #e5e1d8; }
QTabBar::tab:selected {
    background: transparent; color: #d9b877;
    border-bottom-color: #b08d4f;
}

QToolBar {
    background: #1e1e21; border: none;
    border-bottom: 1px solid #2c2c30;
    spacing: 3px; padding: 5px 8px;
}
QToolBar QToolButton { padding: 5px 10px; }
QToolBar::separator { background: #2c2c30; width: 1px; margin: 3px 7px; }

QMenu {
    background-color: #202023; border: 1px solid #36363c;
    border-radius: 4px; padding: 4px;
}
QMenu::item { padding: 6px 22px 6px 13px; border-radius: 3px; }
QMenu::item:selected { background-color: #2a2822; color: #d6b877; }
QMenu::separator { height: 1px; background: #2c2c30; margin: 4px 7px; }

QSlider::groove:horizontal {
    height: 3px; background: #2c2c30; border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #b08d4f; width: 12px; height: 12px;
    margin: -5px 0; border-radius: 6px;
}
QSlider::handle:horizontal:hover { background: #d6b877; }
QSlider::sub-page:horizontal { background: #6b5730; border-radius: 2px; }

QCheckBox { spacing: 6px; color: #d8d6d2; }
QCheckBox::indicator {
    width: 14px; height: 14px; border-radius: 3px;
    border: 1px solid #3c3c42; background: #141416;
}
QCheckBox::indicator:checked { background: #b08d4f; border-color: #b08d4f; }
QCheckBox::indicator:hover { border-color: #b08d4f; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical {
    background: #36363c; border-radius: 5px; min-height: 26px;
}
QScrollBar::handle:vertical:hover { background: #55524a; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle:horizontal {
    background: #36363c; border-radius: 5px; min-width: 26px;
}
QScrollBar::handle:horizontal:hover { background: #55524a; }

QStatusBar {
    background: #1e1e21; color: #86868c;
    border-top: 1px solid #2c2c30;
}
QStatusBar::item { border: none; }

QHeaderView::section {
    background-color: #26262a; color: #b08d4f;
    border: none; border-right: 1px solid #2c2c30;
    padding: 6px 8px; font-weight: 600;
}

QToolTip {
    background-color: #202023; color: #d8d6d2;
    border: 1px solid #b08d4f; border-radius: 3px;
    padding: 6px 9px;
}

QSplitter::handle { background-color: #141416; }
QSplitter::handle:horizontal { width: 8px; }
QSplitter::handle:hover { background-color: #55524a; }
QSplitter::handle:pressed { background-color: #b08d4f; }

/* Кубики: кнопки из 2.9 и запуск 3D-лотка. */
QPushButton#dice_button {
    background: #26262a; border: 1px solid #3a3a40; border-radius: 4px;
    color: #e4e2dc; font-size: 9pt; font-weight: 600; padding: 4px 6px;
}
QPushButton#dice_button:hover { background: #33343c; }
QPushButton#dice_button:pressed { background: #b08d4f; color: #141416; }
QToolButton#dice_launch {
    background: #d97a3f; border: none; border-radius: 6px;
    color: white; font-weight: 600; padding: 6px 10px;
}
QToolButton#dice_launch:hover { background: #e58a4e; }
QToolButton#dice_launch:disabled { background: #4d3a2c; }

/* Окно отдыха: главная кнопка — единственный акцент, день/ночь — тише. */
#rest_title {
    font-family: "Georgia", "Cambria", "DejaVu Serif", serif;
    color: #c9a35f; font-size: 13pt; font-weight: 600;
}
#rest_hint { color: #8c8c94; font-size: 9pt; }
#rest_long_btn {
    background-color: #2a2822; border: 1px solid #b08d4f;
    color: #e8d5a8; font-size: 11pt; font-weight: 600;
}
#rest_long_btn:hover { background-color: #35301f; border-color: #d0aa62; }
#rest_short_btn { font-size: 10.5pt; }
#rest_day_btn, #rest_night_btn { font-size: 9.5pt; }
#rest_status { color: #a3be8c; font-size: 9pt; }
"""

LIGHT_QSS = """
/* Светлая тема: та же сдержанность, серо-бумажный фон и бронзовый акцент. */
* {
    font-family: "Segoe UI Variable Text", "Segoe UI", "Inter",
                 "DejaVu Sans", sans-serif;
    font-size: 10pt;
}
QLabel[heading="true"], QGroupBox::title, #section_title {
    font-family: "Georgia", "Cambria", "DejaVu Serif", serif;
}

QMainWindow, QWidget { background-color: #f2f0ea; color: #2b2a26; }
QScrollArea { background: transparent; border: none; }
QAbstractItemView { outline: 0; }

#section_host { background: #faf9f5; }
#section_title {
    background: #efece5;
    color: #8a6a2f;
    font-size: 10.5pt;
    font-weight: 600;
    letter-spacing: 1.4px;
    padding: 11px 13px 10px 13px;
    border-bottom: 1px solid #ddd9cf;
}

#section_rail_host, #section_rail { background: #e4e2dc; }
#rail_button { background-color: transparent; border: 1px solid transparent;
               border-radius: 6px; }
#rail_button:hover { background-color: #dad8d1; border-color: #c4c2ba; }
#rail_button:checked {
    background-color: #efe7d5;
    border-color: #a98a4a;
}

#draw_tool {
    background-color: #ece9e2;
    border: 1px solid #cdcac2;
    border-radius: 5px;
}
#draw_tool:hover { background-color: #e2ded4; border-color: #b3b0a6; }
#draw_tool:checked {
    background-color: #efe7d5;
    border-color: #a98a4a;
}
QToolButton#wall_tool_btn, QToolButton#wall_delete_btn {
    background-color: #ece9e2;
    border: 1px solid #cdcac2;
    border-radius: 5px;
    padding: 0;
}
QToolButton#wall_tool_btn:hover, QToolButton#wall_delete_btn:hover {
    background-color: #e2ded4;
    border-color: #b3b0a6;
}
QToolButton#wall_tool_btn:checked, QToolButton#wall_delete_btn:checked {
    background-color: #f3e6cb;
    border: 1px solid #a98a4a;
}
#draw_color {
    background-color: #f4f3ef;
    border: 1px solid #cdcac2;
    border-radius: 5px;
    padding: 2px;
}
#draw_color:hover { border-color: #a98a4a; }
#draw_hint { color: #6f6d66; font-size: 11px; }
#text_size_spin {
    background-color: #f4f3ef;
    border: 1px solid #cdcac2;
    border-radius: 5px;
    padding: 2px 4px;
}
#text_size_spin:hover { border-color: #b3b0a6; }
#text_size_spin:focus { border-color: #a98a4a; }
#panel_group {
    background-color: #f1efe9;
    border: 1px solid #dcd8cf;
    border-radius: 8px;
    margin-top: 10px;
    padding: 0;
}
#panel_group::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 8px;
    padding: 0 5px;
    color: #8a6a2f;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 1px;
}

#sheet_hp_spin {
    background: transparent; border: 1px solid transparent;
    padding: 0px; font-weight: 700;
}
#sheet_hp_spin:focus { border-color: #a98a4a; background-color: #f7f6f2; }

QGroupBox {
    border: none;
    margin-top: 16px; padding: 2px 0 0 0;
    background: transparent;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 0; padding: 0 2px;
    color: #8a6a2f; font-weight: 600; font-size: 9pt;
    letter-spacing: 0.8px;
}

QLabel { color: #2b2a26; background: transparent; }
QLabel:disabled { color: #8e8b83; }

/* Типографика листа: мягкие подписи вместо серых плашек. */
#sheet_name { font-family: "Georgia", "Cambria", "DejaVu Serif", serif;
              color: #26241f; letter-spacing: 0.2px; }
#sheet_sub { color: #77736b; font-size: 9pt; }
#sheet_section { color: #8a6a2f; font-size: 8.5pt; font-weight: 700;
                 letter-spacing: 1.5px; padding-top: 4px; }
#sheet_cell { background: transparent; border: none; }
#sheet_cell_title { color: #858177; font-size: 7.8pt; font-weight: 700;
                    letter-spacing: 0.8px; }
#sheet_vital { color: #26241f; font-size: 15pt; font-weight: 700; }
#sheet_ability { color: #26241f; font-size: 13pt; font-weight: 700; }
#sheet_skill_prof { color: #6d531e; }
QPushButton#sheet_feature_btn {
    text-align: left; padding: 4px 9px; border-radius: 4px;
    border: 1px solid #d5d1c6; background: #f6f4ee; color: #6d531e;
}
QPushButton#sheet_feature_btn:hover {
    background: #fffdf7; border-color: #a98a4a;
}
QLabel#sheet_feature_chip {
    color: #6f6b62; padding: 4px 2px; font-size: 9pt;
}

/* Таблица атак и заметки: рамка только на вводе, как в тёмной теме. */
QLineEdit#sheet_attack_name, QLineEdit#sheet_attack_damage,
QLineEdit#sheet_attack_notes, QSpinBox#sheet_attack_bonus {
    background: transparent; border: 1px solid transparent;
    border-radius: 4px; padding: 1px 4px; color: #2b2a26;
    selection-background-color: #d8c69a;
}
QLineEdit#sheet_attack_name:hover, QLineEdit#sheet_attack_damage:hover,
QLineEdit#sheet_attack_notes:hover, QSpinBox#sheet_attack_bonus:hover {
    background: #faf9f5;
}
QLineEdit#sheet_attack_name:focus, QLineEdit#sheet_attack_damage:focus,
QLineEdit#sheet_attack_notes:focus, QSpinBox#sheet_attack_bonus:focus {
    background: #fffdf7; border-color: #a98a4a;
}
QToolButton#sheet_add_attack {
    background-color: #f7f6f2; border: 1px solid #d3cfc3;
    border-radius: 11px; color: #8a6a2f; font-size: 11pt;
    font-weight: 700; padding: 0;
}
QToolButton#sheet_add_attack:hover {
    background-color: #efe7d5; border-color: #a98a4a; color: #6d531e;
}
QToolButton#sheet_add_attack:pressed { background-color: #e4e2dc; }
QToolButton#sheet_quick_chip {
    background: transparent; border: 1px solid #d5d2c9;
    border-radius: 4px; color: #8b8880; font-size: 7.6pt;
    font-weight: 700; padding: 0;
}
QToolButton#sheet_quick_chip:hover { border-color: #bda06a; color: #8a6a2f; }
QToolButton#sheet_quick_chip:checked {
    background: #efe7d5; border-color: #a98a4a; color: #6d531e;
}
QToolButton#sheet_attack_roll, QToolButton#sheet_attack_remove { padding: 0; }
QToolButton#sheet_attack_remove:hover { color: #b4543a; }
#sheet_note_hint { color: #8b8880; font-size: 8pt; }
#sheet_note_title { color: #858177; font-size: 8pt; font-weight: 700;
                    letter-spacing: 0.6px; }
QPlainTextEdit#sheet_notes {
    background: #f7f6f2; border: 1px solid #ddd9cf; border-radius: 5px;
    padding: 6px 8px; color: #2b2a26; selection-background-color: #d8c69a;
}
QPlainTextEdit#sheet_notes:focus {
    background: #fffdf7; border-color: #a98a4a;
}

QPushButton {
    background-color: #f7f6f2; border: 1px solid #cfcdc6;
    border-radius: 3px; padding: 6px 11px; color: #2a2a2c;
    min-height: 17px;
}
QPushButton:hover { background-color: #eeede8; border-color: #b3ab97; }
QPushButton:focus { outline: none; border-color: #bda06a; }
QPushButton:pressed { background-color: #e4e2dc; }
QPushButton:disabled { background-color: #e8e6e1; color: #a3a19b;
                       border-color: #dcdad3; }
QPushButton:checked { background-color: #efe7d5; border-color: #a98a4a;
                      color: #6d531e; }
QPushButton#nav_button {
    background-color: #f3f2ee; border: 1px solid #cfcdc6;
    border-radius: 4px; padding: 0;
}
QPushButton#nav_button:hover { background-color: #ffffff; border-color: #a98a4a; }
QPushButton#nav_button:pressed { background-color: #e4e2dc; }
QPushButton#nav_button:disabled { background-color: #e8e6e1;
                                  border-color: #dcdad3; }
QPushButton#nav_button_end:hover { border-color: #b45309; }
QLabel#token_size_value { color: #85662a; font-weight: 600; }

QToolButton {
    background-color: transparent; border: 1px solid transparent;
    border-radius: 3px; padding: 5px 9px; color: #2a2a2c;
}
QToolButton:hover { background-color: #eeede8; border-color: #cfcdc6; }
QToolButton:focus { outline: none; border-color: #bda06a; }
QToolButton:pressed { background-color: #e4e2dc; }
/* Те же строки в светлой теме: без внутренних отступов, чтобы значки
   глазка и замка не превращались в тонкую полоску. */
QToolButton#map_object_eye_btn,
QToolButton#map_object_lock_btn,
QToolButton#map_object_gear_btn,
QToolButton#light_eye_btn,
QToolButton#light_lock_btn,
QToolButton#light_preset_btn,
QToolButton#token_eye_btn { padding: 0; }
QLabel#away_tokens_title { color: #8a877f; font-size: 11px; }
QToolButton#away_token_move_btn {
    padding: 1px 7px; border: 1px solid #d5d1c6; border-radius: 4px;
    color: #6d531e; font-size: 11px;
}
QToolButton#away_token_move_btn:hover {
    background-color: #f2efe6; border-color: #a98a4a;
}

/* Поля — строки в тексте, а не белые прямоугольники в серой сетке. */
QLineEdit, QSpinBox, QDoubleSpinBox {
    background: transparent; border: none;
    border-bottom: 1px solid #d6d2c7; border-radius: 0;
    padding: 5px 3px; color: #26241f;
    selection-background-color: #e7dcc4; selection-color: #2b2415;
}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover {
    border-bottom-color: #bdb7aa;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border-bottom-color: #a98a4a;
}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
    color: #8e8b83; border-bottom-color: #e2ded4;
}
QSpinBox QLineEdit, QDoubleSpinBox QLineEdit, QComboBox QLineEdit {
    background: transparent; border: none; padding: 0;
}

QComboBox {
    background: #faf9f5; border: none; border-radius: 6px;
    padding: 6px 9px; color: #26241f;
    selection-background-color: #e7dcc4; selection-color: #2b2415;
}
QComboBox:hover { background: #f0ede5; }
QComboBox:focus { background: #f0ede5; }
QComboBox QAbstractItemView {
    background-color: #faf9f5; border: none; border-radius: 6px;
    padding: 4px; selection-background-color: #efe3cb;
    selection-color: #6d531e;
}

QTextEdit, QPlainTextEdit {
    background: #fbf9f4; border: none; border-radius: 8px;
    padding: 10px 12px; color: #2b2a26;
    selection-background-color: #e7dcc4; selection-color: #2b2415;
}

QListWidget, QTreeView, QTableView {
    background: transparent; border: none; padding: 2px;
    color: #2b2a26; outline: 0;
    selection-background-color: #efe3cb; selection-color: #6d531e;
}
QListWidget::item { padding: 6px 8px; border-radius: 6px; border: none; }
QListWidget::item:hover { background-color: #f0ede5; }
QListWidget::item:selected { background-color: #efe3cb; color: #6d531e; }
#tokens_list::item:selected { background-color: #e6d2a5; }
#tokens_list::item:selected:!active { background-color: #eee0c4; }
/* См. DARK_QSS: карточки действий справочника. */
#action_card {
    background: #fbf9f4; border: 1px solid #dedbd3; border-radius: 6px;
}
#action_card[actionable="true"]:hover {
    background: #f4f1e9; border-color: #c9b283;
}
#action_card_title { color: #2b2a26; font-size: 9pt; font-weight: 600; }
#action_card_sub { color: #77736b; font-size: 8pt; }
#lib_card_view {
    background: #fbf9f4; border: none; border-radius: 8px;
    padding: 10px 12px;
}
#action_cards_board, #lib_actions_scroll { background: transparent; }
QListWidget::item:focus { border: none; outline: none; }
/* См. CardSelectionDelegate: выделяется ячейка, а не PNG и не текст. */
#my_actors_grid, #inventory_list { outline: 0; }
#my_actors_grid::item:selected, #inventory_list::item:selected {
    background: transparent; color: palette(text);
}
#my_actors_grid::item:focus, #inventory_list::item:focus { border: none; }

QTabWidget::pane {
    border: none; background: transparent; top: 0;
}
QTabBar::tab {
    background: transparent; color: #77736b;
    padding: 9px 12px 8px 12px; margin-right: 7px;
    border: none; border-bottom: 2px solid transparent;
}
QTabBar::tab:hover { color: #2b2a26; }
QTabBar::tab:selected {
    background: transparent; color: #8a6a2f;
    border-bottom-color: #a98a4a;
}

QToolBar {
    background: #eae8e3; border: none;
    border-bottom: 1px solid #d0cec7;
    spacing: 3px; padding: 5px 8px;
}
QToolBar::separator { background: #d0cec7; width: 1px; margin: 3px 7px; }

QMenu {
    background-color: #f7f6f2; border: 1px solid #cfcdc6;
    border-radius: 4px; padding: 4px;
}
QMenu::item { padding: 6px 22px 6px 13px; border-radius: 3px; }
QMenu::item:selected { background-color: #efe7d5; color: #6d531e; }
QMenu::separator { height: 1px; background: #d0cec7; margin: 4px 7px; }

QSlider::groove:horizontal {
    height: 3px; background: #d5d3cc; border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #a98a4a; width: 12px; height: 12px;
    margin: -5px 0; border-radius: 6px;
}
QSlider::handle:horizontal:hover { background: #85662a; }
QSlider::sub-page:horizontal { background: #c9b283; border-radius: 2px; }

QCheckBox { spacing: 6px; color: #2a2a2c; }
QCheckBox::indicator {
    width: 14px; height: 14px; border-radius: 3px;
    border: 1px solid #bfbdb6; background: #ffffff;
}
QCheckBox::indicator:checked { background: #a98a4a; border-color: #a98a4a; }
QCheckBox::indicator:hover { border-color: #a98a4a; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical {
    background: #cbc9c2; border-radius: 5px; min-height: 26px;
}
QScrollBar::handle:vertical:hover { background: #a9a79f; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle:horizontal {
    background: #cbc9c2; border-radius: 5px; min-width: 26px;
}
QScrollBar::handle:horizontal:hover { background: #a9a79f; }

QStatusBar {
    background: #eae8e3; color: #74726d;
    border-top: 1px solid #d0cec7;
}
QStatusBar::item { border: none; }

QHeaderView::section {
    background-color: #eae8e3; color: #85662a;
    border: none; border-right: 1px solid #d0cec7;
    padding: 6px 8px; font-weight: 600;
}

QToolTip {
    background-color: #f7f6f2; color: #2a2a2c;
    border: 1px solid #a98a4a; border-radius: 3px;
    padding: 6px 9px;
}

QSplitter::handle { background-color: #d5d3cc; }
QSplitter::handle:horizontal { width: 8px; }
QSplitter::handle:hover { background-color: #a9a79f; }
QSplitter::handle:pressed { background-color: #a98a4a; }

/* Кубики: кнопки из 2.9 и запуск 3D-лотка. */
QPushButton#dice_button {
    background: #ffffff; border: 1px solid #cdcac2; border-radius: 4px;
    color: #2a2a2c; font-size: 9pt; font-weight: 600; padding: 4px 6px;
}
QPushButton#dice_button:hover { background: #e8e6e0; }
QPushButton#dice_button:pressed { background: #a98a4a; color: white; }
QToolButton#dice_launch {
    background: #d97a3f; border: none; border-radius: 6px;
    color: white; font-weight: 600; padding: 6px 10px;
}
QToolButton#dice_launch:hover { background: #e58a4e; }
QToolButton#dice_launch:disabled { background: #e0c6b2; }

#rest_title {
    font-family: "Georgia", "Cambria", "DejaVu Serif", serif;
    color: #8a6a2f; font-size: 13pt; font-weight: 600;
}
#rest_hint { color: #77736a; font-size: 9pt; }
#rest_long_btn {
    background-color: #f7f3e8; border: 1px solid #a98a4a;
    color: #4a3a1c; font-size: 11pt; font-weight: 600;
}
#rest_long_btn:hover { background-color: #efe7d2; border-color: #8f6f31; }
#rest_short_btn { font-size: 10.5pt; }
#rest_day_btn, #rest_night_btn { font-size: 9.5pt; }
#rest_status { color: #4c7a3a; font-size: 9pt; }
"""

ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icons")


def section_icon(name: str, fallback: str, theme: str = "dark",
                 subdir: str = "") -> QIcon:
    """Иконка раздела из client/assets/icons/[<subdir>/]<name>.png.

    Иконки нарисованы для светлой подложки: тёмный контур, кремовая заливка.
    Поэтому кнопка рейки в обеих темах даёт им светлый квадрат (см. QSS
    #rail_button) — на нём арт читается, а не сливается с фоном.

    <name>_light.png поддерживается как запасной вариант: если он есть, для
    тёмной темы берётся он. Обычно его нет — хватает одного файла.

    Если файлов нет, возвращаем пустой QIcon: раздел покажет текст.
    """
    candidates = ([f"{name}_light.png", f"{name}.png"] if theme == "dark"
                  else [f"{name}.png", f"{name}_light.png"])
    base = os.path.join(ICON_DIR, subdir) if subdir else ICON_DIR
    for fname in candidates:
        path = os.path.join(base, fname)
        if os.path.isfile(path):
            return QIcon(path)
    return QIcon()


def tool_icon(name: str, theme: str = "dark") -> QIcon:
    """Иконка инструмента рисования и вкладок правой панели.

    Набор лежит в client/assets/icons/tools: та же монохромная графика, что
    и у инструментов Photoshop, — рейка и панель инструментов должны
    выглядеть одним интерфейсом.
    """
    return section_icon(name, "", theme, subdir="tools")


def cyrillic_font_families():
    """Список шрифтов для надписей: только кириллические и установленные.    В наборе есть обычные Word-шрифты (Calibri, Cambria, Times New Roman и
    прочие), но показываем только те, что действительно установлены:
    установленного шрифта молча дало бы подстановку, и надпись выглядела бы
    чужой. Если таких шрифтов нет вовсе, берём все доступные.
    """
    try:
        database = QFontDatabase()
        families = set(database.families(QFontDatabase.Cyrillic))
        if not families:
            # Система ничего не сказала про кириллицу (бывает у Qt без
            # настроенных шрифтов) — берём всё, что вообще установлено.
            families = set(database.families())
    except Exception:
        families = set()
    ordered = [name for name in text_rules.FONT_CHOICES if name in families]
    ordered += sorted(families - set(ordered))
    # О системе ничего не известно — показываем весь список Word-шрифтов:
    # недоступное имя Qt подменит сам, а пустой список был бы хуже.
    return ordered or list(text_rules.FONT_CHOICES)


SECTION_FALLBACK = {"maps": "Карты", "tokens": "Бой", "music": "Музыка",
                    "reference": "Справочник", "my_actors": "Мои актёры",
                    "sheet": "Лист персонажа"}

#: Режим кисти на холсте -> имя файла иконки в client/assets/icons/tools.
DRAW_TOOL_ICONS = {"brush": "brush", "eraser": "eraser", "line": "line",
                   "square": "rect", "circle": "ellipse", "polygon": "pen",
                   "text": "text", "move": "move"}

#: Начертание надписи -> иконка кнопки-переключателя в панели текста.
TEXT_STYLE_ICONS = {"bold": "text_bold", "italic": "text_italic",
                    "underline": "text_underline", "strike": "text_strike"}


class _AlwaysOpenGroup:
    """Заглушка вместо раскрывающейся группы листа.

    Лист переехал в отдельную панель и больше не сворачивается. Старый код
    (и скрипт отладки) зовёт setChecked(True) — эти вызовы должны проходить
    молча, иначе панель не соберётся.
    """

    def __init__(self, parent=None):
        self._checked = True
        self._parent = parent

    def setChecked(self, value):
        self._checked = bool(value)

    def isChecked(self):
        return self._checked

    def setVisible(self, value):
        if self._parent is not None:
            self._parent.setVisible(bool(value))

    def isVisible(self):
        return bool(self._parent.isVisible()) if self._parent else self._checked


class LightPresetButton(QToolButton):
    """Иконка заготовки света: клик ставит в центр экрана, перетаскивание — на карту.

    Кнопка сама начинает `QDrag` с ключом заготовки
    (`LIGHT_PRESET_MIME`), а холст ловит его в `dropEvent` и создаёт
    источник ровно в точке отпускания. Клик остаётся прежним: пока
    курсор не сдвинулся дальше порога Qt, обычный `clicked` ставит свет в
    центр экрана.
    """

    def __init__(self, preset_key: str, parent=None):
        super().__init__(parent)
        self.preset_key = str(preset_key)
        self._press_pos = None
        self._dragged = False

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_pos = event.pos()
            self._dragged = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._press_pos is not None and not self._dragged
                and event.buttons() & Qt.LeftButton
                and (event.pos() - self._press_pos).manhattanLength()
                >= QApplication.startDragDistance()):
            self._dragged = True
            self._start_drag()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        dragged, self._dragged = self._dragged, False
        self._press_pos = None
        if dragged:
            # После перетаскивания кнопка не должна ещё и «нажаться»:
            # иначе свет встал бы и в точку отпускания, и в центр экрана.
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _start_drag(self):
        mime = QMimeData()
        mime.setData(LIGHT_PRESET_MIME, self.preset_key.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        icon = self.icon()
        pix = icon.pixmap(32, 32) if not icon.isNull() else QPixmap()
        if not pix.isNull():
            drag.setPixmap(pix)
            drag.setHotSpot(QPoint(pix.width() // 2, pix.height() // 2))
        drag.exec_(Qt.CopyAction)
        self._press_pos = None


class SectionRail(QWidget):
    """Вертикальная рейка разделов и стопка их панелей.

    Вёл себя как QTabWidget (addTab / setTabToolTip / count / tabIcon), чтобы
    не переписывать вызовы, но добавлено главное отличие: повторное нажатие
    по активной иконке СКРЫВАЕТ панель, а нажатие по другой — переключает.

    Кнопки без подписей: текст растягивал рейку и занимал место у карты.
    Название видно в подсказке.
    """

    # Сигнал: панель открыта (True) или свёрнута (False). Ширину держит
    # QSplitter, поэтому MainWindow по нему переставляет sizes.
    collapsedChanged = pyqtSignal(bool)
    #: Активный раздел сменился (-1 — панель свёрнута). Нужен холсту:
    #: маркеры света тянутся мышью только при открытой вкладке освещения.
    currentChanged = pyqtSignal(int)

    def __init__(self, parent=None, rail_on_right: bool = False):
        super().__init__(parent)
        self.setObjectName("section_rail_host")
        self.rail_on_right = bool(rail_on_right)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        self.rail = QWidget()
        self.rail.setObjectName("section_rail")
        self.rail.setFixedWidth(56)
        self.rail_layout = QVBoxLayout(self.rail)
        self.rail_layout.setContentsMargins(8, 10, 8, 10)
        self.rail_layout.setSpacing(8)

        self.stack = QStackedWidget()
        self.stack.setObjectName("section_stack")

        # Заголовок активного раздела: в Foundry у каждой боковой панели есть
        # название сверху, иначе непонятно, куда попал.
        self.stack_host = QWidget()
        self.stack_host.setObjectName("section_host")
        host_layout = QVBoxLayout(self.stack_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(0)
        self.section_title = QLabel("")
        self.section_title.setObjectName("section_title")
        host_layout.addWidget(self.section_title)
        host_layout.addWidget(self.stack, 1)

        # Левая панель: рейка у края окна, содержимое правее. Правая панель —
        # зеркально: содержимое слева, рейка прижата к правому краю окна,
        # иначе свёрнутая панель оставляла полосу пустоты между иконками
        # и краем экрана.
        if self.rail_on_right:
            row.addWidget(self.stack_host, 1)
            row.addWidget(self.rail)
        else:
            row.addWidget(self.rail)
            row.addWidget(self.stack_host, 1)

        self.rail_layout.addStretch(1)
        self.buttons = []
        self.icons = []
        self._active = -1

    # --- совместимость с QTabWidget ---
    def addTab(self, widget, icon, label=""):
        index = self.stack.addWidget(widget)
        btn = QToolButton(self.rail)
        btn.setObjectName("rail_button")
        btn.setCheckable(True)
        btn.setAutoRaise(True)
        btn.setFixedSize(40, 40)
        btn.setIconSize(QSize(24, 24))
        btn.setIcon(icon)
        btn.setToolTip(label)
        btn.clicked.connect(lambda _=False, i=index: self.toggle_section(i))
        self.rail_layout.insertWidget(len(self.buttons), btn)
        self.buttons.append(btn)
        self.icons.append(icon)
        # Первый добавленный раздел сразу активен: иначе первое нажатие по
        # нему считалось бы «открытием уже открытого» и панель пряталась.
        if len(self.buttons) == 1:
            btn.setChecked(True)
            self._active = 0
            self.stack.setCurrentIndex(0)
            self.section_title.setText(label.upper())
        return index

    def setTabToolTip(self, index, text):
        """Подсказка раздела; она же — заголовок панели.

        Подпись приходит после addTab, поэтому заголовок открытого раздела
        обновляем здесь: иначе сверху остаётся пустая полоса.
        """
        if 0 <= index < len(self.buttons):
            self.buttons[index].setToolTip(text)
            if index == self._active:
                self.section_title.setText(text.upper())

    def count(self):
        return len(self.buttons)

    def tabText(self, index):
        return self.buttons[index].toolTip() if 0 <= index < len(self.buttons) else ""

    def tabToolTip(self, index):
        """Подсказка раздела — она же его название: на кнопке текста нет."""
        return self.tabText(index)

    def tabIcon(self, index):
        return self.icons[index] if 0 <= index < len(self.icons) else QIcon()

    def setTabIcon(self, index, icon):
        if 0 <= index < len(self.buttons):
            self.buttons[index].setIcon(icon)
            self.icons[index] = icon

    def currentIndex(self):
        return self._active

    # --- собственное поведение ---
    def toggle_section(self, index: int):
        """Открыть раздел; повторное нажатие по активному — скрыть панель.

        QStackedWidget игнорирует setVisible(False), если внутри есть текущая
        страница: он сам управляет видимостью детей. Поэтому ширину стека
        обнуляем — панель исчезает, рейка остаётся на месте.
        """
        if index == self._active:
            self.buttons[index].setChecked(False)
            self.stack_host.setMaximumWidth(0)
            self._active = -1
            self.collapsedChanged.emit(False)
            self.currentChanged.emit(-1)
            return
        self.stack_host.setMaximumWidth(16777215)
        if self._active < 0:
            self.collapsedChanged.emit(True)
        for i, btn in enumerate(self.buttons):
            btn.setChecked(i == index)
        self.stack.setCurrentIndex(index)
        self.section_title.setText(self.buttons[index].toolTip().upper())
        self._active = index
        self.currentChanged.emit(index)
