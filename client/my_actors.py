"""Библиотека «Мои актёры»: собственные актёры мастера между сессиями.

Актёр — это папка ``<данные>/my assets/my actors/<имя>/`` с ``actor.json``
и одним PNG. Тот же PNG служит и токеном на карте, и аватаркой в сетке
библиотеки. Отдельная вкладка внутри панели — папка-категория; актёры
хранятся квадратными иконками, а не строками.

Библиотека не чистится вместе с кэшем ресурсов: «Очистить кэш» трогает
только ``assets_cache``. Удаление актёра уносит его папку целиком.
"""
import copy
import json
import os
import shutil
import uuid

from PyQt5.QtCore import QMimeData, QPoint, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QDrag, QFont, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import (QAbstractItemView, QDialog, QFileDialog,
                             QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QMenu, QMessageBox,
                             QPushButton, QTabBar, QToolButton, QVBoxLayout,
                             QWidget)

from shared import app_paths, token_stats, vision as vision_rules
from shared.models import Token
from client.card_selection import CardSelectionDelegate

#: Свой формат переноса: холст принимает актёра только из библиотеки.
ACTOR_MIME = "application/x-vtt-my-actor"
DEFAULT_TAB = "Все актёры"


def library_dir() -> str:
    path = os.path.join(app_paths.home_dir(), "my assets", "my actors")
    os.makedirs(path, exist_ok=True)
    return path


def _index_path() -> str:
    return os.path.join(library_dir(), "library.json")


def _safe_folder(name: str) -> str:
    """Имя папки из имени актёра: без запрещённых символов и точек в конце."""
    cleaned = "".join(ch for ch in str(name or "")
                      if ch.isprintable() and ch not in '<>:"/\\|?*')
    cleaned = " ".join(cleaned.split()).strip(" .")
    return (cleaned or "Актёр")[:80]


def _unique_folder(name: str, used: set, fallback: str = "Актёр") -> str:
    base = _safe_folder(name) or fallback
    candidate = base
    index = 2
    taken = {str(u or "").casefold() for u in used}
    while candidate.casefold() in taken:
        candidate = f"{base} ({index})"
        index += 1
    return candidate


def default_library() -> dict:
    return {"tabs": [{"name": DEFAULT_TAB, "actors": []}], "actors": {}}


def load_library() -> dict:
    """Прочитать library.json; битый файл не должен отнимать библиотеку."""
    data = None
    try:
        with open(_index_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return default_library()
    actors = data.get("actors")
    if not isinstance(actors, dict):
        actors = {}
    tabs = []
    assigned = set()
    for tab in data.get("tabs") or []:
        if not isinstance(tab, dict):
            continue
        name = str(tab.get("name") or "").strip()[:60]
        ids = []
        for raw_id in (tab.get("actors") or []):
            actor_id = str(raw_id)
            if actor_id in actors and actor_id not in assigned:
                ids.append(actor_id)
                assigned.add(actor_id)
        if name:
            tabs.append({"name": name, "actors": ids})
    if not tabs:
        tabs = [{"name": DEFAULT_TAB, "actors": list(actors)}]
    # Каждый актёр обязан быть ровно в одной вкладке: потерянный id
    # возвращаем в первую, иначе он пропадал бы из интерфейса.
    seen = {aid for tab in tabs for aid in tab["actors"]}
    for actor_id in actors:
        if actor_id not in seen:
            tabs[0]["actors"].append(actor_id)
    return {"tabs": tabs, "actors": actors}


def save_library(library: dict):
    os.makedirs(library_dir(), exist_ok=True)
    tmp = _index_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(library, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _index_path())


def actor_dir(actor: dict) -> str:
    # Имя папки — один сегмент: путь из испорченного library.json не должен
    # выводить за пределы библиотеки.
    name = os.path.basename(str(actor.get("folder") or "").replace("\\", "/"))
    return os.path.join(library_dir(), name)


def actor_image(actor: dict) -> str:
    name = str(actor.get("image") or "")
    if not name:
        return ""
    path = os.path.join(actor_dir(actor), name)
    return path if os.path.isfile(path) else ""


def save_actor_file(actor: dict):
    """Записать карточку актёра в его папку (отдельный actor.json)."""
    folder = actor_dir(actor)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "actor.json"), "w", encoding="utf-8") as f:
        json.dump(actor, f, ensure_ascii=False, indent=2)


def new_actor(name: str, library: dict) -> dict:
    used = {a.get("folder") for a in library["actors"].values()}
    folder = _unique_folder(name, used)
    statblock = {
        "name": name, "type": "", "cr": "", "abilities": {
            key: 10 for key in token_stats.ABILITIES},
        "mods": {}, "saves": [], "attacks": [], "quick_attack": "",
        "resist": [], "immune": [], "vulnerable": [],
        "condition_immunities": [], "description": "",
        "ac": 10, "hp": 10, "hp_max": 10, "speed": 30,
    }
    return {
        "id": uuid.uuid4().hex,
        "name": name,
        "folder": folder,
        "image": "",
        "hp": 10, "hp_max": 10, "ac": 10, "size": 1.0,
        "statblock": statblock,
    }


def token_from_actor(actor: dict) -> Token:
    """Токен-черновик для карточки: у него нет карты, он живёт только в окне."""
    block = copy.deepcopy(actor.get("statblock") or {})
    # Дальность обзора — из чувств («тёмное зрение 90 фт»), а не всегда 60.
    vision_kwargs = vision_rules.settings_from_text(
        block.get("senses") or actor.get("senses"))
    # Сохранённая в карточке настройка важнее текста о чувствах: мастер мог
    # поправить угол или дальность руками.
    if actor.get("vision_angle") is not None:
        vision_kwargs["vision_angle"] = actor["vision_angle"]
    if actor.get("vision_range") is not None:
        vision_kwargs["vision_range"] = actor["vision_range"]
    return Token(
        id="myactor:" + str(actor.get("id")),
        name=str(actor.get("name") or "Актёр"),
        image_path=actor_image(actor),
        x=0.0, y=0.0,
        size=float(actor.get("size") or 1.0),
        hp=actor.get("hp"), hp_max=actor.get("hp_max"),
        ac=actor.get("ac"), kind="actor", statblock=block,
        # Рамка и подгонка портрета — часть карточки: призванный на карту
        # токен выглядит так же, как мастер настроил его в библиотеке.
        frame=actor.get("frame") or "",
        portrait_zoom=actor.get("portrait_zoom") or 1.0,
        portrait_dx=actor.get("portrait_dx") or 0.0,
        portrait_dy=actor.get("portrait_dy") or 0.0,
        resist=list(block.get("resist") or []),
        immune=list(block.get("immune") or []),
        vulnerable=list(block.get("vulnerable") or []),
        **vision_kwargs,
    )


class ActorGrid(QListWidget):
    """Сетка квадратных иконок: 3 столбца в узкой панели, больше — шире."""
    deleteRequested = pyqtSignal(str)
    contextRequested = pyqtSignal(str, QPoint)
    activated = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("my_actors_grid")
        # Выделяем ячейку целиком: штатная синяя подложка и пунктир вокруг
        # подписи выглядят как ошибка интерфейса, а не как выбор актёра.
        self.setItemDelegate(CardSelectionDelegate(self))
        self.setViewMode(QListWidget.IconMode)
        # В узкой панели помещаются три столбца; при растяжении окна
        # QListWidget сам добавляет четвёртый и следующие.
        self.setIconSize(QSize(64, 64))
        self.setGridSize(QSize(72, 92))
        self.setSpacing(4)
        self.setMovement(QListWidget.Static)
        self.setResizeMode(QListWidget.Adjust)
        self.setWrapping(True)
        self.setWordWrap(True)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        # Подсветка ячейки под курсором — без этого State_MouseOver не
        # приходит в делегат и карточки кажутся «мёртвыми» до клика.
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        # Карточка открывается только двойным кликом: одиночный клик
        # просто выделяет актёра для кнопок «PNG» и «Призвать».
        self.itemDoubleClicked.connect(self._double_clicked)

    def _actor_id(self, item) -> str:
        return str(item.data(Qt.UserRole) or "") if item is not None else ""

    def _double_clicked(self, item):
        actor_id = self._actor_id(item)
        if actor_id:
            self.activated.emit(actor_id)

    def _menu(self, pos):
        item = self.itemAt(pos)
        actor_id = self._actor_id(item)
        if actor_id:
            self.contextRequested.emit(actor_id,
                                       self.viewport().mapToGlobal(pos))

    def keyPressEvent(self, event):
        actor_id = self._actor_id(self.currentItem())
        if event.key() == Qt.Key_Delete:
            if actor_id:
                self.deleteRequested.emit(actor_id)
                return
        # Enter открывает карточку выбранного актёра — как двойной клик.
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if actor_id:
                self.activated.emit(actor_id)
                return
        super().keyPressEvent(event)

    def startDrag(self, supported_actions):
        item = self.currentItem()
        actor_id = self._actor_id(item)
        if not actor_id:
            return
        mime = QMimeData()
        mime.setData(ACTOR_MIME, actor_id.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        pix = item.icon().pixmap(64, 64)
        if not pix.isNull():
            drag.setPixmap(pix)
            drag.setHotSpot(QPoint(pix.width() // 2, pix.height() // 2))
        drag.exec_(Qt.CopyAction)


class MyActorsPage(QWidget):
    """Панель «Мои актёры»: вкладки-папки, сетка и карточка актёра."""

    summonRequested = pyqtSignal(str)
    libraryChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("my_actors_page")
        self.library = load_library()
        self._tabs_guard = False

        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(5)

        head = QHBoxLayout()
        head.setSpacing(4)
        self.tab_bar = QTabBar()
        self.tab_bar.setObjectName("my_actors_tabs")
        self.tab_bar.setExpanding(False)
        self.tab_bar.setMovable(False)
        self.tab_bar.setElideMode(Qt.ElideRight)
        self.tab_bar.setUsesScrollButtons(True)
        self.tab_bar.currentChanged.connect(self._tab_changed)
        self.tab_bar.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tab_bar.customContextMenuRequested.connect(self._tab_menu)
        head.addWidget(self.tab_bar, 1)
        add_tab = QToolButton()
        add_tab.setObjectName("my_actors_add_tab")
        add_tab.setText("+")
        add_tab.setToolTip("Новая вкладка библиотеки")
        add_tab.clicked.connect(self.add_tab)
        head.addWidget(add_tab)
        root.addLayout(head)

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("my_actors_search")
        self.search_edit.setPlaceholderText("Поиск по имени…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setToolTip(
            "Искать по названиям карточек во всех вкладках библиотеки")
        self.search_edit.textChanged.connect(self._refresh_grid)
        root.addWidget(self.search_edit)

        self.grid = ActorGrid()
        self.grid.activated.connect(self.open_actor)
        self.grid.deleteRequested.connect(self.delete_actor)
        self.grid.contextRequested.connect(self._actor_menu)
        self.grid.currentItemChanged.connect(self._update_buttons)
        root.addWidget(self.grid, 1)

        self.hint = QLabel(
            "Двойной клик или Enter — карточка актёра. Перетащите иконку "
            "на карту, чтобы поставить токен.")
        self.hint.setObjectName("settings_hint")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

        # Кнопки в две узкие строки: панель мастера неширокая, и три
        # подписи в один ряд растягивали её шире карты.
        row = QHBoxLayout()
        add = QPushButton("Добавить")
        add.setObjectName("my_actors_add_btn")
        add.setToolTip("Создать нового актёра; карточка откроется "
                       "двойным кликом")
        add.clicked.connect(self.add_actor)
        row.addWidget(add)
        self.image_btn = QPushButton("PNG")
        self.image_btn.setObjectName("my_actors_image_btn")
        self.image_btn.setToolTip(
            "Выбрать картинку токена и аватарки, не призывая его")
        self.image_btn.clicked.connect(self.set_selected_image)
        row.addWidget(self.image_btn)
        root.addLayout(row)
        row = QHBoxLayout()
        self.summon_btn = QPushButton("Призвать")
        self.summon_btn.setObjectName("my_actors_summon_btn")
        self.summon_btn.setToolTip("Поставить выбранного актёра на открытую карту")
        self.summon_btn.clicked.connect(self.summon_selected)
        row.addWidget(self.summon_btn)
        self.delete_btn = QPushButton("Удалить")
        self.delete_btn.setObjectName("my_actors_delete_btn")
        self.delete_btn.setToolTip("Удалить актёра вместе с его папкой")
        self.delete_btn.clicked.connect(self.delete_selected)
        row.addWidget(self.delete_btn)
        root.addLayout(row)

        self.reload()

    def _update_buttons(self, *_args):
        """Без выбранного актёра действия с ним недоступны, а не молчат."""
        has_actor = bool(self.selected_actor_id())
        tips = (
            (self.image_btn,
             "Выбрать картинку токена и аватарки, не призывая его"),
            (self.summon_btn,
             "Поставить выбранного актёра на открытую карту"),
            (self.delete_btn, "Удалить актёра вместе с его папкой"),
        )
        for button, tip in tips:
            button.setEnabled(has_actor)
            button.setToolTip(
                tip if has_actor else "Сначала выберите актёра в сетке")

    # ------------------------------------------------------------------
    # Вкладки и сетка
    # ------------------------------------------------------------------
    def reload(self):
        self.library = load_library()
        self._rebuild_tabs()
        self._refresh_grid()

    def _rebuild_tabs(self):
        previous = self.tab_bar.currentIndex()
        self._tabs_guard = True
        self.tab_bar.blockSignals(True)
        while self.tab_bar.count():
            self.tab_bar.removeTab(0)
        for tab in self.library["tabs"]:
            self.tab_bar.addTab(tab["name"])
        self.tab_bar.blockSignals(False)
        self._tabs_guard = False
        if self.tab_bar.count():
            self.tab_bar.setCurrentIndex(min(max(previous, 0),
                                            self.tab_bar.count() - 1))

    def current_tab(self):
        index = self.tab_bar.currentIndex()
        if 0 <= index < len(self.library["tabs"]):
            return self.library["tabs"][index]
        return None

    def _tab_changed(self, *_):
        if not self._tabs_guard:
            self._refresh_grid()

    def _search_query(self) -> str:
        edit = getattr(self, "search_edit", None)
        return edit.text().strip().casefold() if edit is not None else ""

    def _refresh_grid(self, *_):
        # Переименование, загрузка PNG и поиск перестраивают сетку. Раньше
        # выделение при этом пропадало, и следующая кнопка работала уже
        # ни с чем; возвращаем его на того же актёра.
        current = self.grid.currentItem()
        keep_id = current.data(Qt.UserRole) if current is not None else None
        self.grid.clear()
        query = self._search_query()
        if query:
            # Поиск идёт по всей библиотеке, а не только по открытой вкладке:
            # иначе актёра легко «потерять» в соседней папке.
            actors = sorted(self.library["actors"].items(),
                            key=lambda pair: str(pair[1].get("name", ""))
                            .casefold())
        else:
            tab = self.current_tab()
            if tab is None:
                self._update_hint(0, query)
                return
            actors = [(aid, self.library["actors"].get(aid))
                      for aid in tab["actors"]]
        shown = 0
        for actor_id, actor in actors:
            if not isinstance(actor, dict):
                continue
            name = str(actor.get("name", ""))
            if query and query not in name.casefold():
                continue
            item = QListWidgetItem(self._icon(actor), name)
            item.setData(Qt.UserRole, actor_id)
            item.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
            item.setToolTip(f"{name}\nПеретащите на карту, "
                            "чтобы поставить токен")
            self.grid.addItem(item)
            shown += 1
        if keep_id is not None:
            kept = self._item_for(keep_id)
            if kept is not None:
                self.grid.setCurrentItem(kept)
        self._update_buttons()
        self._update_hint(shown, query)

    def _update_hint(self, shown: int, query: str):
        if query and not shown:
            self.hint.setText(
                f"Ничего не найдено по запросу «{query}».")
        elif query:
            self.hint.setText(
                f"Найдено: {shown}. Двойной клик или Enter — карточка.")
        else:
            self.hint.setText(
                "Двойной клик или Enter — карточка актёра. Перетащите "
                "иконку на карту, чтобы поставить токен.")

    def _icon(self, actor: dict) -> QIcon:
        size = 64
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        path = actor_image(actor)
        src = QPixmap(path) if path else QPixmap()
        if not src.isNull():
            scaled = src.scaled(size, size, Qt.KeepAspectRatioByExpanding,
                                Qt.SmoothTransformation)
            painter.drawPixmap((size - scaled.width()) // 2,
                               (size - scaled.height()) // 2, scaled)
        else:
            painter.fillRect(0, 0, size, size, QColor("#242838"))
            font = QFont(painter.font())
            font.setPointSize(22)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor("#c8a45e"))
            painter.drawText(pix.rect(), Qt.AlignCenter,
                             str(actor.get("name") or "?")[:1].upper())
        painter.setPen(QColor("#c8a45e"))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(0, 0, size - 1, size - 1)
        painter.end()
        return QIcon(pix)

    def actor(self, actor_id: str):
        return self.library["actors"].get(actor_id)

    def selected_actor_id(self) -> str:
        item = self.grid.currentItem()
        return str(item.data(Qt.UserRole) or "") if item is not None else ""

    # ------------------------------------------------------------------
    # Вкладки
    # ------------------------------------------------------------------
    def add_tab(self):
        name, ok = QInputDialog.getText(self, "Новая вкладка",
                                        "Как назвать вкладку?")
        name = (name or "").strip()[:60]
        if not ok or not name:
            return
        self.library["tabs"].append({"name": name, "actors": []})
        save_library(self.library)
        self._rebuild_tabs()
        self.tab_bar.setCurrentIndex(len(self.library["tabs"]) - 1)
        self._refresh_grid()

    def _tab_menu(self, pos):
        index = self.tab_bar.tabAt(pos)
        if index < 0 or index >= len(self.library["tabs"]):
            return
        menu = QMenu(self)
        menu.addAction("Переименовать вкладку",
                       lambda: self.rename_tab(index))
        menu.addAction("Удалить вкладку",
                       lambda: self.delete_tab(index))
        menu.exec_(self.tab_bar.mapToGlobal(pos))

    def rename_tab(self, index: int):
        if not (0 <= index < len(self.library["tabs"])):
            return
        tab = self.library["tabs"][index]
        name, ok = QInputDialog.getText(self, "Переименовать вкладку",
                                        "Новое название:", text=tab["name"])
        name = (name or "").strip()[:60]
        if not ok or not name:
            return
        tab["name"] = name
        save_library(self.library)
        self._rebuild_tabs()
        self.tab_bar.setCurrentIndex(index)

    def delete_tab(self, index: int):
        if len(self.library["tabs"]) <= 1:
            QMessageBox.information(self, "Мои актёры",
                                    "Должна остаться хотя бы одна вкладка")
            return
        if not (0 <= index < len(self.library["tabs"])):
            return
        tab = self.library["tabs"][index]
        answer = QMessageBox.question(
            self, "Удалить вкладку",
            f"Удалить «{tab['name']}»? Актёры перейдут в первую вкладку, "
            "файлы останутся.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self.library["tabs"].pop(index)
        self.library["tabs"][0]["actors"].extend(tab["actors"])
        save_library(self.library)
        self._rebuild_tabs()
        self._refresh_grid()

    # ------------------------------------------------------------------
    # Актёры
    # ------------------------------------------------------------------
    def add_actor(self):
        names = {a.get("name") for a in self.library["actors"].values()}
        name = "Новый актёр"
        index = 2
        while name in names:
            name = f"Новый актёр {index}"
            index += 1
        actor = new_actor(name, self.library)
        save_actor_file(actor)
        self.library["actors"][actor["id"]] = actor
        tabs = self.library["tabs"]
        target = self.current_tab() or tabs[0]
        target["actors"].append(actor["id"])
        save_library(self.library)
        self._refresh_grid()
        # Новый актёр только выделяется в сетке: карточка открывается
        # двойным кликом, как и у остальных.
        item = self._item_for(actor["id"])
        if item is not None:
            self.grid.setCurrentItem(item)
            self.grid.scrollToItem(item)
        self._notify_changed()

    def open_actor(self, actor_id: str):
        actor = self.actor(actor_id)
        if not isinstance(actor, dict):
            return
        from client.actor_sheet import ActorSheetDialog
        dialog = ActorSheetDialog(token_from_actor(actor), parent=self,
                                   can_revive=True, can_edit=True)
        dialog.setWindowTitle(f"Мой актёр — {actor.get('name')}")
        dialog.saveRequested.connect(
            lambda payload, aid=actor_id: self._save_actor_card(aid, payload))
        dialog.exec_()

    def _save_actor_card(self, actor_id: str, payload: dict):
        actor = self.actor(actor_id)
        if not isinstance(actor, dict) or not isinstance(payload, dict):
            return
        new_name = str(payload.get("name") or actor.get("name") or "Актёр")[:120]
        self._rename_folder_if_needed(actor, new_name)
        actor["name"] = new_name
        actor["hp"] = payload.get("hp")
        actor["hp_max"] = payload.get("hp_max")
        actor["ac"] = payload.get("ac")
        actor["size"] = payload.get("size") or 1.0
        # Зрение — настройка стола, но она часть карточки актёра: иначе
        # после пересохранения логова с тёмным зрением токен снова видел 60.
        if payload.get("vision_angle") is not None:
            actor["vision_angle"] = float(payload["vision_angle"])
        if payload.get("vision_range") is not None:
            actor["vision_range"] = float(payload["vision_range"])
        block = copy.deepcopy(payload.get("statblock") or {})
        block["name"] = new_name
        actor["statblock"] = block
        save_actor_file(actor)
        save_library(self.library)
        self._refresh_grid()
        self._notify_changed()

    def _rename_folder_if_needed(self, actor: dict, new_name: str):
        """Имя актёра — имя папки: переименование переносит файлы."""
        old_folder = str(actor.get("folder") or "").strip()
        used = {a.get("folder") for aid, a in self.library["actors"].items()
                if aid != actor.get("id")}
        wanted = _safe_folder(new_name)
        if wanted == old_folder:
            return
        if wanted.casefold() in {str(u).casefold() for u in used}:
            folder = _unique_folder(new_name, used)
        else:
            folder = wanted
        # У старой записи папки могло не быть. Корень библиотеки нельзя
        # переносить или удалять: в нём лежат папки всех остальных актёров.
        if not old_folder:
            actor["folder"] = folder
            return
        old_dir = os.path.join(library_dir(), old_folder)
        new_dir = os.path.join(library_dir(), folder)
        try:
            os.makedirs(new_dir, exist_ok=True)
            if os.path.isdir(old_dir):
                for name in os.listdir(old_dir):
                    shutil.move(os.path.join(old_dir, name),
                                os.path.join(new_dir, name))
                os.rmdir(old_dir)
        except OSError as exc:
            QMessageBox.warning(self, "Мои актёры",
                                f"Не удалось переименовать папку:\n{exc}")
            return
        actor["folder"] = folder

    def rename_actor(self, actor_id: str):
        actor = self.actor(actor_id)
        if not isinstance(actor, dict):
            return
        name, ok = QInputDialog.getText(self, "Переименовать актёра",
                                        "Имя:", text=str(actor.get("name")))
        name = (name or "").strip()[:120]
        if not ok or not name:
            return
        self._rename_folder_if_needed(actor, name)
        actor["name"] = name
        block = dict(actor.get("statblock") or {})
        block["name"] = name
        actor["statblock"] = block
        save_actor_file(actor)
        save_library(self.library)
        self._refresh_grid()
        self._notify_changed()

    def set_selected_image(self):
        actor_id = self.selected_actor_id()
        if actor_id:
            self.set_actor_image(actor_id)
        else:
            QMessageBox.information(self, "Мои актёры",
                                    "Выберите актёра в списке")

    def set_actor_image(self, actor_id: str):
        """Выбрать PNG токена: подогнать под рамку и только сохранить.

        Окно то же, что у токена игрока: мастер сразу видит, как актёр будет
        выглядеть на карте, и выбирает рамку здесь, а не всплепую. На карту
        актёр при этом не призывается.
        """
        actor = self.actor(actor_id)
        if not isinstance(actor, dict):
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Картинка актёра (токен и аватарка)", "",
            "Изображения (*.png *.webp *.jpg *.jpeg *.bmp *.gif)")
        if not path:
            return
        pix = QPixmap(path)
        if pix.isNull():
            QMessageBox.warning(self, "Мои актёры",
                                "Картинка не открылась")
            return
        from client.token_portrait_dialog import TokenPortraitDialog
        dialog = TokenPortraitDialog(
            self, path, str(actor.get("name") or ""),
            frame=actor.get("frame") or "")
        dialog.setWindowTitle(f"Портрет актёра — {actor.get('name')}")
        if dialog.exec_() != QDialog.Accepted:
            return
        target_dir = actor_dir(actor)
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, "token.png")
        if not pix.save(target, "PNG"):
            QMessageBox.warning(self, "Мои актёры",
                                "Картинка не сохранилась")
            return
        actor["image"] = "token.png"
        actor["frame"] = dialog.frame_key()
        actor["portrait_zoom"] = dialog.zoom()
        actor["portrait_dx"], actor["portrait_dy"] = dialog.offsets()
        save_actor_file(actor)
        save_library(self.library)
        self._refresh_grid()
        self._notify_changed()

    def summon_selected(self):
        actor_id = self.selected_actor_id()
        if actor_id:
            self.summonRequested.emit(actor_id)
        else:
            QMessageBox.information(self, "Мои актёры",
                                    "Выберите актёра в списке")

    def delete_selected(self):
        actor_id = self.selected_actor_id()
        if actor_id:
            self.delete_actor(actor_id)

    def delete_actor(self, actor_id: str):
        actor = self.actor(actor_id)
        if not isinstance(actor, dict):
            return
        answer = QMessageBox.question(
            self, "Удалить актёра",
            f"Удалить актёра «{actor.get('name')}» и всю его папку "
            "(карточку и картинку)?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        target = os.path.abspath(actor_dir(actor))
        root = os.path.abspath(library_dir())
        # Удаляем только собственную папку актёра, никогда корень библиотеки.
        if target != root and os.path.dirname(target) == root:
            shutil.rmtree(target, ignore_errors=True)
        self.library["actors"].pop(actor_id, None)
        for tab in self.library["tabs"]:
            tab["actors"] = [aid for aid in tab["actors"] if aid != actor_id]
        save_library(self.library)
        self._refresh_grid()
        self.libraryChanged.emit()

    def _actor_menu(self, actor_id: str, global_pos: QPoint):
        actor = self.actor(actor_id)
        if not isinstance(actor, dict):
            return
        self.grid.setCurrentItem(self._item_for(actor_id))
        menu = QMenu(self)
        menu.addAction("Открыть карточку", lambda: self.open_actor(actor_id))
        menu.addAction("Переименовать", lambda: self.rename_actor(actor_id))
        menu.addAction("Загрузить PNG…",
                       lambda: self.set_actor_image(actor_id))
        menu.addSeparator()
        menu.addAction("Призвать на карту",
                       lambda: self.summonRequested.emit(actor_id))
        menu.addSeparator()
        menu.addAction("Удалить актёра",
                       lambda: self.delete_actor(actor_id))
        menu.exec_(global_pos)

    def _item_for(self, actor_id: str):
        for row in range(self.grid.count()):
            item = self.grid.item(row)
            if str(item.data(Qt.UserRole) or "") == actor_id:
                return item
        return None

    def _notify_changed(self):
        self.libraryChanged.emit()
