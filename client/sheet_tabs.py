"""Вкладки листа персонажа: инвентарь и книга заклинаний.

Как в Foundry: у актёра есть отдельные вкладки Inventory и Features/Spells.
Здесь то же самое — список предметов с кнопками «надеть», «применить»,
«выбросить» и книга заклинаний с ячейками по уровням.

Виджеты ничего не знают о сети: они меняют словарь листа и зовут колбэки
`on_change(sheet)` и `on_roll(формула, подпись)`. Так их можно проверять
без сервера.
"""
import os
import re

from PyQt5.QtCore import QEvent, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QGridLayout,
                             QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                             QMenu, QMessageBox, QPushButton, QSizePolicy,
                             QSpinBox, QVBoxLayout, QWidget)

from shared import conditions as conditions_rules
from shared import item_rules
from shared import sheet_items as items
from shared import use_action
from client.card_selection import CardSelectionDelegate
from client.inventory_delegate import INVENTORY_GRID, InventorySlotDelegate
from client.item_drag import ITEM_ROLE, ItemDragList, drag_payload
from client.item_icons import item_icon


_LSS_CATALOG = None


def _lss_catalog():
    """Записи заклинаний справочника — один раз на процесс.

    Нужны, чтобы превратить ссылки из книги LSS (id/слаги) в строки с
    названием, уроном и описанием. Если библиотека ещё не импортирована,
    вкладка просто останется со ссылками без текста.
    """
    global _LSS_CATALOG
    if _LSS_CATALOG is None:
        try:
            from shared import dnd5e_lib, world_store
            library = dnd5e_lib.load_library(world_store.get_data_root())
            _LSS_CATALOG = list(library.get("spells") or [])
        except (OSError, ValueError):
            _LSS_CATALOG = []
    return _LSS_CATALOG


def _clean(value) -> str:
    """Убрать хвост вроде «рубящий», оставив только формулу кубика."""
    m = re.search(r"(\d+d\d+(?:\s*[+-]\s*\d+)?)", str(value or ""))
    if not m:
        return ""
    return m.group(1).replace(" ", "")


class InventoryTab(QWidget):
    """Вкладка «Инвентарь»: предметы, надетое, вес."""

    rollRequested = pyqtSignal(str, str)
    #: единое действие использования предмета (UseAction в виде dict)
    useActionRequested = pyqtSignal(dict)
    #: старые сигналы оставлены для совместимости с внешними слушателями;
    #: настоящее окно слушает useActionRequested и обрабатывает всё одним
    #: пайплайном, поэтому двойного лечения нет.
    effectRequested = pyqtSignal(str, str, str)
    attackRequested = pyqtSignal(dict)
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sheet = None
        self.token_id = ""
        l = QVBoxLayout(self)
        l.setContentsMargins(6, 6, 6, 6)
        l.setSpacing(6)

        self.summary = QLabel("")
        self.summary.setObjectName("sheet_sub")
        self.summary.setWordWrap(True)
        l.addWidget(self.summary)

        self.list = ItemDragList()
        self.list.setObjectName("inventory_list")
        # Выделение — вся ячейка предмета, а не пунктирная рамка вокруг
        # текста. Это же правило работает и для сетки «Мои актёры».
        # Слот рисует сам: иконка, подпись в две строки и количество не
        # разъезжаются по соседним ячейкам, как у стандартного QListWidget.
        self.list.setItemDelegate(InventorySlotDelegate(self.list))
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.itemDoubleClicked.connect(lambda _item: self.use_selected())
        # Сетка ячеек: один предмет — одна ячейка. Внутреннее перемещение
        # меняет порядок списка, а он же и есть порядок ячеек в листе.
        self.list.setViewMode(QListWidget.IconMode)
        self.list.setResizeMode(QListWidget.Adjust)
        self.list.setMovement(QListWidget.Snap)
        self.list.setWordWrap(False)
        self.list.setUniformItemSizes(True)
        self.list.setIconSize(QSize(48, 48))
        self.list.setGridSize(INVENTORY_GRID)
        self.list.setSpacing(4)
        self.list.set_payload_builder(self._drag_item_payload)
        self.list.set_reorder_callback(self._reorder_from_drag)
        # Пустые слоты до вместимости инвентаря: без них сетка обрывалась
        # после последнего предмета и не читалась как сетка.
        self.list.set_empty_grid(items.INVENTORY_SLOTS,
                                 InventorySlotDelegate.paint_empty_slot)
        self.list.set_drop_hint_painter(
            InventorySlotDelegate.paint_drop_target)
        self.list.set_activate_callback(self.use_selected)
        self.list.currentItemChanged.connect(self._update_actions)
        self.list.setToolTip(
            "Enter или двойной клик — применить. Перетащите предмет на токен "
            "или в другую ячейку.")
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._item_menu)
        self._refreshing = False
        l.addWidget(self.list, 1)

        # Пустой инвентарь раньше выглядел просто сеткой без подсказки: не
        # понять, это баг или правда нечего показать. Строка объясняет, что
        # делать дальше, и занимает место только когда инвентарь пуст.
        self.empty_hint = QLabel("Инвентарь пуст · добавьте предметы из справочника")
        self.empty_hint.setObjectName("sheet_sub")
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setVisible(False)
        l.addWidget(self.empty_hint)

        row = QHBoxLayout()
        row.setSpacing(4)
        self.use_btn = QPushButton("Использовать")
        self.use_btn.setToolTip("Применить предмет: лечение, спасбросок или атака")
        self.use_btn.clicked.connect(self.use_selected)
        row.addWidget(self.use_btn)
        self.equip_btn = QPushButton("Надеть")
        self.equip_btn.setToolTip("Отметить как надетое/взведённое")
        self.equip_btn.clicked.connect(self.toggle_equipped)
        row.addWidget(self.equip_btn)
        self.drop_btn = QPushButton("Выбросить")
        self.drop_btn.setToolTip("Убрать из инвентаря (со всем количеством)")
        self.drop_btn.clicked.connect(self.drop_selected)
        row.addWidget(self.drop_btn)
        l.addLayout(row)

        row = QHBoxLayout()
        # Отдых живёт в меню мастера (иконка костра в верхней рейке): это
        # событие всего стола, а не действие одного листа. Здесь остаётся
        # только выброс части стака.
        self.qty_label = QLabel("Выбросить:")
        self.qty_label.setObjectName("sheet_sub")
        row.addWidget(self.qty_label)
        self.qty_spin = QSpinBox()
        self.qty_spin.setRange(1, 9999)
        self.qty_spin.setPrefix("× ")
        self.qty_spin.setToolTip(
            "Сколько штук из стака выбросить (1 — одну)")
        self.qty_spin.setEnabled(False)
        self.qty_spin.valueChanged.connect(self._update_actions)
        row.addWidget(self.qty_spin)
        l.addLayout(row)

        # Настройка (attunement): в 5e не больше трёх предметов одновременно.
        self.attune_btn = QPushButton("Настроиться")
        self.attune_btn.setToolTip(
            f"Настройка на магический предмет: не больше "
            f"{item_rules.ATTUNEMENT_LIMIT} сразу")
        self.attune_btn.clicked.connect(self.toggle_attuned)
        l.addWidget(self.attune_btn)

        # Монеты: лежат в листе LSS как coins {cp,sp,ep,gp,pp}. Раньше их
        # нельзя было ни увидеть, ни потратить, а электрум (ep) вообще
        # оставался невидимым: поле ЭМ просто отсутствовало, и монеты листа
        # в этой деноминации не показывались и не правились.
        self.coins_title = QLabel("МОНЕТЫ")
        self.coins_title.setObjectName("sheet_section")
        l.addWidget(self.coins_title)
        coins_row = QHBoxLayout()
        coins_row.setSpacing(3)
        self.coin_spins = {}
        # Сигнал монет отложен: на каждую цифру не нужно перестраивать сетку
        # инвентаря и гнать лист на сервер.
        self._coin_timer = QTimer(self)
        self._coin_timer.setSingleShot(True)
        self._coin_timer.timeout.connect(self.changed.emit)
        for key, label, full in (("pp", "ПМ", "Платина"),
                                 ("gp", "ЗМ", "Золото"),
                                 ("ep", "ЭМ", "Электрум"),
                                 ("sp", "СМ", "Серебро"),
                                 ("cp", "ММ", "Медь")):
            cell = QVBoxLayout()
            cell.setSpacing(0)
            title = QLabel(label)
            title.setObjectName("sheet_cell_title")
            title.setAlignment(Qt.AlignCenter)
            title.setToolTip(f"{full} ({label})")
            cell.addWidget(title)
            spin = QSpinBox()
            spin.setObjectName(f"sheet_coin_{key}")
            spin.setRange(0, 999999)
            # Пять полей должны влезать в узкую панель: у обычного
            # QSpinBox минимальная ширина больше, чем нужно для пяти цифр.
            spin.setMinimumWidth(48)
            spin.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            spin.setToolTip(f"Монеты: {full.lower()} ({label})")
            spin.valueChanged.connect(self.save_coins)
            cell.addWidget(spin)
            self.coin_spins[key] = spin
            coins_row.addLayout(cell)
        l.addLayout(coins_row)
        # Пока ничего не выбрано, действия недоступны: серые кнопки честнее
        # нажатий, которые молча ничего не делают.
        self._update_actions()

    def set_sheet(self, sheet: dict):
        self.sheet = sheet
        if sheet is not None:
            items.ensure(sheet)
        self.refresh()

    def set_token_id(self, token_id: str):
        """Привязать инвентарь к токену: без этого окно не знает, чей
        предмет перетащили на карту."""
        self.token_id = str(token_id or "")
        if hasattr(self, "list"):
            self.list.drag_list_id = self._drag_list_id()

    def _drag_list_id(self) -> str:
        return f"inventory:{getattr(self, 'token_id', '') or 'own'}"

    def refresh(self):
        # После «Надеть», «Использовать» и перестановки список строится
        # заново. Без этого выбранная ячейка терялась, и следующая кнопка
        # молча ничего не делала — выделение должно переживать обновление.
        current = self.list.currentItem()
        previous_id = current.data(Qt.UserRole) if current is not None else None
        previous_row = self.list.currentRow()
        # Полная перестройка сетки сбрасывала прокрутку в начало: после
        # «Надеть» на нижнем предмете список прыгал к первому. Запоминаем
        # позицию и возвращаем её после восстановления выделения.
        scroll = self.list.verticalScrollBar().value()
        self._refreshing = True
        try:
            # Свой id нужен уже на этапе drag: при перетаскивании внутри
            # списка предмет должен переставляться, а не применяться.
            self.list.drag_list_id = self._drag_list_id()
            self._refresh_locked()
        finally:
            self._refreshing = False
        self._restore_selection(previous_id, previous_row)
        self.list.verticalScrollBar().setValue(scroll)
        self._update_actions()

    def _restore_selection(self, item_id, row: int):
        """Вернуть выделение на тот же предмет после перестройки сетки."""
        if item_id is None or not self.list.count():
            return
        for index in range(self.list.count()):
            if self.list.item(index).data(Qt.UserRole) == item_id:
                self.list.setCurrentRow(index)
                return
        # Предмет израсходован целиком: выбираем соседнюю ячейку, чтобы
        # кнопки продолжали работать, а не гасли без объяснения.
        self.list.setCurrentRow(min(max(row, 0), self.list.count() - 1))

    def _refresh_locked(self):
        self.list.clear()
        self.empty_hint.setVisible(False)
        if not self.sheet:
            self.summary.setText("Лист не загружен")
            return
        block = items.ensure(self.sheet)
        inv = block["inventory"]
        # Подсказка нужна только на пустом инвентаре: иначе она отнимала бы
        # высоту у сетки с предметами.
        self.empty_hint.setVisible(not inv)
        equipped = sum(1 for i in inv if i.get("equipped"))
        weight = items.total_weight(self.sheet)
        # Грузоподъёмность и настройка: правила уже в shared.item_rules,
        # здесь только показываем, чтобы мастер и игрок видели перегруз.
        carry = item_rules.carry_status(self.sheet)
        attuned = len(item_rules.attuned_items(self.sheet))
        line = (f"{len(inv)} видов  ·  слотов "
                f"{items.inventory_slots_used(self.sheet)}/"
                f"{items.INVENTORY_SLOTS}  ·  надето {equipped}  ·  "
                f"вес {weight:g}/{carry['capacity']:g} фнт  ·  "
                f"настроено {attuned}/{item_rules.ATTUNEMENT_LIMIT}")
        if carry["state"] == "immobile":
            line += "  ·  ПЕРЕГРУЗ: не может двигаться"
        elif carry["state"] == "slow":
            line += "  ·  перегруз: скорость −10 фт"
        self.summary.setText(line)
        for item in inv:
            row = QListWidgetItem(item_icon(item, 48),
                                  str(item.get("name") or "?"))
            row.setData(Qt.UserRole, item.get("id"))
            # Делегат берёт из роли полное имя, количество и метки. Текст
            # элемента остаётся только для доступности и поиска.
            row.setData(ITEM_ROLE, item)
            tip = [item.get("name", ""), item.get("itype", ""),
                   item.get("rarity", ""), item.get("price", ""),
                   item.get("weight", "")]
            try:
                qty = int(item.get("qty", 1) or 1)
            except (TypeError, ValueError):
                qty = 1
            if qty > 1:
                tip.insert(1, f"×{qty}")
            if item.get("equipped"):
                tip.append("надето")
            if item.get("attuned"):
                tip.append("настроено")
            tip = [t for t in tip if t]
            extra = item.get("damage") or item.get("heal") or ""
            if not extra:
                extra = next((str(f) for f in (item.get("formulas") or [])
                              if str(f).strip()), "")
            if extra:
                tip.append(str(extra))
            if item.get("charges_max"):
                tip.append(f"заряды {item.get('charges', 0)}/"
                           f"{item.get('charges_max')}")
            if use_action.can_apply_to_token(item):
                tip.append("Перетащите на токен, чтобы применить")
            row.setToolTip("  ·  ".join(str(t) for t in tip))
            self.list.addItem(row)
        self._load_coins()

    def _drag_item_payload(self, row_item):
        """Запись предмета для QDrag. Только предметы, а не подписи."""
        if row_item is None or not self.sheet:
            return None
        record = items.find_item(self.sheet, row_item.data(Qt.UserRole))
        if not record:
            return None
        payload = drag_payload(record, source="inventory",
                               token_id=getattr(self, "token_id", ""),
                               list_id=self._drag_list_id())
        # Внутри инвентаря можно переставлять любой предмет, но на карту
        # бросаем только расходники с готовым действием: зелья лечения,
        # сопротивления, невидимости, алхимический огонь и яды.
        payload["usable"] = use_action.can_apply_to_token(record)
        return payload

    def _reorder_from_drag(self, payload: dict, row: int):
        """Перетаскивание внутри инвентаря: поменять порядок ячеек."""
        if not self.sheet or not isinstance(payload, dict):
            return
        record = payload.get("item") or {}
        item_id = record.get("id")
        inventory = items.ensure(self.sheet)["inventory"]
        old = next((index for index, entry in enumerate(inventory)
                    if entry.get("id") == item_id), None)
        if not item_id or old is None:
            return
        # Подсветка цели стоит слева от ячейки, то есть обещает «вставить
        # перед ней». При переносе сверху вниз цель после извлечения предмета
        # сдвигается на шаг влево — без поправки предмет вставал бы после
        # ячейки, на которую его бросили.
        append = row >= len(inventory)
        target = (len(inventory) - 1) if append else int(row)
        if not append and old < target:
            target -= 1
        if target == old:
            return
        if not items.move_item(self.sheet, item_id, target):
            return
        self.refresh()
        self.changed.emit()

    def _on_rows_moved(self, *_args):
        """Предмет перетащили: порядок сетки становится порядком листа."""
        if self._refreshing or not self.sheet:
            return
        block = items.ensure(self.sheet)
        by_id = {item.get("id"): item for item in block["inventory"]}
        order = []
        for row in range(self.list.count()):
            entry = by_id.get(self.list.item(row).data(Qt.UserRole))
            if entry is not None and entry not in order:
                order.append(entry)
        # Предметы, не попавшие в виджет (например, список обновился
        # в середине перетаскивания), не теряем.
        order.extend(item for item in block["inventory"] if item not in order)
        block["inventory"][:] = order
        for index, entry in enumerate(order):
            entry["slot"] = index
        self.refresh()
        self.changed.emit()

    def _selected(self):
        row = self.list.currentItem()
        if row is None or not self.sheet:
            return None
        return items.find_item(self.sheet, row.data(Qt.UserRole))

    def _notice(self, text: str):
        """Короткая подсказка без модального окна.

        Рутинные отказы («нет эффекта», лимит настройки) раньше открывали
        QMessageBox и прерывали игру. Теперь сообщение живёт в строке
        состояния, а если её нет — в строке сводки инвентаря.
        """
        window = self.window()
        bar = window.statusBar() if hasattr(window, "statusBar") else None
        if bar is not None:
            bar.showMessage(str(text), 4000)
            return
        self.summary.setText(str(text))

    def _update_actions(self, *_):
        """Подписи и доступность кнопок по выбранному предмету.

        Иначе на зелье смотрела кнопка «Надеть», на надетом мече — «Надеть»,
        а «Настроиться» на верёвке открывала диалог с ошибкой. Кнопка
        должна говорить, что произойдёт, и не предлагать невозможное.
        """
        item = self._selected()
        has_item = item is not None
        self.equip_btn.setEnabled(has_item)
        self.drop_btn.setEnabled(has_item)
        if not has_item:
            self.use_btn.setEnabled(False)
            self.use_btn.setText("Использовать")
            self.use_btn.setToolTip("Сначала выберите предмет в сетке")
            self.equip_btn.setText("Надеть")
            self.equip_btn.setToolTip("Сначала выберите предмет в сетке")
            self.attune_btn.setEnabled(False)
            self.attune_btn.setText("Настроиться")
            self.attune_btn.setToolTip("Сначала выберите предмет в сетке")
            self.drop_btn.setToolTip("Сначала выберите предмет в сетке")
            self._qty_item_id = None
            self.qty_spin.blockSignals(True)
            self.qty_spin.setMaximum(1)
            self.qty_spin.setValue(1)
            self.qty_spin.blockSignals(False)
            self.qty_spin.setEnabled(False)
            self.qty_label.setEnabled(False)
            return

        name = str(item.get("name") or "предмет")
        equipped = bool(item.get("equipped"))
        self.equip_btn.setText("Снять" if equipped else "Надеть")
        self.equip_btn.setToolTip(
            f"Снять пометку «надето» с «{name}»" if equipped
            else f"Отметить «{name}» как надетый/взведённый")

        # Поле количества живёт по размеру стака: нельзя выбросить больше,
        # чем есть, и незачем показывать поле у одиночного предмета.
        try:
            stack = max(1, int(item.get("qty", 1) or 1))
        except (TypeError, ValueError):
            stack = 1
        # Новый предмет — счётчик снова с 1: иначе выбранное для одного
        # стака число переезжало бы на другой и грозило удалить его целиком.
        self.qty_spin.blockSignals(True)
        self.qty_spin.setMaximum(stack)
        if getattr(self, "_qty_item_id", None) != item.get("id"):
            self._qty_item_id = item.get("id")
            self.qty_spin.setValue(1)
        elif self.qty_spin.value() > stack:
            self.qty_spin.setValue(stack)
        self.qty_spin.blockSignals(False)
        self.qty_spin.setEnabled(stack > 1)
        self.qty_label.setEnabled(stack > 1)
        count = min(max(1, self.qty_spin.value()), stack)
        self.drop_btn.setToolTip(
            f"Убрать «{name}» из инвентаря" if stack <= 1 else
            f"Выбросить {count} из {stack} шт. «{name}»"
            + (" — предмет исчезнет из инвентаря" if count >= stack else ""))

        reason = ""
        action_kind = ""
        try:
            action = use_action.from_item(
                item, self.sheet,
                token_id=getattr(self, "token_id", ""))
            action_kind = action.kind
            applicable = (action.kind in ("heal", "attack", "save")
                          or (action.kind == "utility"
                              and bool(action.effects)))
            if applicable:
                applicable, reason = use_action.can_use(self.sheet, action)
        except (TypeError, ValueError, KeyError, AttributeError):
            # Битый экспорт LSS не должен ронять окно в слоте выделения.
            applicable = False
            reason = "Не удалось прочитать эффект предмета"
        attack = action_kind == "attack"
        self.use_btn.setText("Атаковать" if attack else "Использовать")
        self.use_btn.setEnabled(applicable)
        if applicable:
            tip = (f"Атаковать оружием «{name}»" if attack
                   else f"Применить «{name}»")
        else:
            tip = reason or f"У «{name}» нет эффекта для применения"
        self.use_btn.setToolTip(tip)

        attuned = bool(item.get("attuned"))
        needs = bool(item.get("attunement"))
        attuned_count = len(item_rules.attuned_items(self.sheet))
        can_attune = (attuned or needs) and (
            attuned or attuned_count < item_rules.ATTUNEMENT_LIMIT)
        self.attune_btn.setEnabled(can_attune)
        self.attune_btn.setText("Снять настройку" if attuned else "Настроиться")
        if attuned:
            tip = f"Снять магическую настройку с «{name}»"
        elif needs and can_attune:
            tip = (f"Настроиться на «{name}» "
                   f"({attuned_count}/{item_rules.ATTUNEMENT_LIMIT} занято)")
        elif needs:
            tip = ("Лимит настройки исчерпан: "
                   f"{item_rules.ATTUNEMENT_LIMIT} предмета одновременно")
        else:
            tip = f"«{name}» не требует магической настройки"
        self.attune_btn.setToolTip(tip)

    def _item_menu(self, pos):
        """ПКМ по предмету: те же действия, что на панели под сеткой.

        Меню сначала выделяет предмет под курсором, иначе действие ушло бы
        на прошлый выбор, который пользователь уже не видит.
        """
        row = self.list.itemAt(pos)
        if row is None:
            return
        self.list.setCurrentItem(row)
        menu = QMenu(self)
        action = menu.addAction(self.use_btn.text())
        action.setEnabled(self.use_btn.isEnabled())
        action.triggered.connect(lambda _checked=False: self.use_selected())
        action = menu.addAction(self.equip_btn.text())
        action.setEnabled(self.equip_btn.isEnabled())
        action.triggered.connect(lambda _checked=False: self.toggle_equipped())
        action = menu.addAction(self.attune_btn.text())
        action.setEnabled(self.attune_btn.isEnabled())
        action.triggered.connect(lambda _checked=False: self.toggle_attuned())
        menu.addSeparator()
        action = menu.addAction("Выбросить")
        action.setEnabled(self.drop_btn.isEnabled())
        action.triggered.connect(lambda _checked=False: self.drop_selected())
        menu.exec_(self.list.viewport().mapToGlobal(pos))

    def toggle_attuned(self):
        """Включить или выключить магическую настройку на выбранный предмет."""
        item = self._selected()
        if not item or not self.sheet:
            return
        try:
            item_rules.set_attuned(self.sheet, item["id"],
                                   not item.get("attuned"))
        except item_rules.ItemRuleError as exc:
            # Не модальное окно: причина видна в строке состояния, игра
            # не прерывается ради подсказки о лимите настройки.
            self._notice(str(exc))
            return
        self.refresh()
        self.changed.emit()

    @staticmethod
    def _coin_value(cell) -> int:
        """Монеты листа: и ячейка LSS ``{'value': N}``, и просто число.

        Обычная запись сайта — словарь, но в ручных и импортированных
        листах встречается голое число; раньше оно показывалось нулём,
        то есть монеты «исчезали» из поля без предупреждения.
        """
        if isinstance(cell, dict):
            cell = cell.get("value")
        if isinstance(cell, str):
            cell = cell.strip()
        try:
            return max(0, int(cell or 0))
        except (TypeError, ValueError):
            return 0

    def save_coins(self, *_):
        """Записать монеты из полей в лист LSS (coins: {key: {'value': N}}).

        Слабый сигнал отложен: пока игрок набирает сумму, лист уже верный,
        а перестраивать сетку инвентаря и слать SHEET_UPDATE на каждую
        цифру незачем.
        """
        if not self.sheet:
            return
        coins = self.sheet.get("coins")
        if not isinstance(coins, dict):
            coins = {}
            self.sheet["coins"] = coins
        for key, spin in getattr(self, "coin_spins", {}).items():
            cell = coins.get(key)
            value = int(spin.value())
            if isinstance(cell, dict):
                cell["value"] = value
            else:
                coins[key] = {"value": value}
        timer = getattr(self, "_coin_timer", None)
        if timer is not None:
            timer.start(350)
        else:
            self.changed.emit()

    def _load_coins(self):
        """Показать монеты листа в полях, не вызывая лишних сохранений.

        Поле, в котором сейчас набирают, не трогаем: серверное эхо иначе
        подменяло цифру под курсором (набрал 50 — поле прыгнуло на 5).
        """
        if not self.sheet or not getattr(self, "coin_spins", None):
            return
        coins = self.sheet.get("coins") or {}
        if not isinstance(coins, dict):
            return
        for key, spin in self.coin_spins.items():
            if spin.hasFocus():
                continue
            spin.blockSignals(True)
            spin.setValue(self._coin_value(coins.get(key)))
            spin.blockSignals(False)

    def toggle_equipped(self):
        item = self._selected()
        if not item:
            return
        item["equipped"] = not item.get("equipped")
        self.refresh()
        self.changed.emit()

    def use_selected(self):
        """Применить предмет: собрать UseAction и отдать главному окну.

        Расход здесь не делается: сначала окно проверяет, есть ли цель и
        может ли действовать персонаж. Иначе зелье сгорало бы, даже если
        лечить некого — как было раньше.
        """
        item = self._selected()
        if not item:
            return
        action = use_action.from_item(item, self.sheet,
                                      token_id=getattr(self, "token_id", ""))
        self.useActionRequested.emit(action.to_dict())
        # Совместимость: старые подписчики (тесты, внешние панели) видят
        # те же события, что и до появления UseAction. Настоящее окно их
        # не слушает, поэтому двойного применения нет.
        if action.kind == "heal":
            self.effectRequested.emit("heal", action.formula, action.note)
        elif action.kind == "utility" and any(
                e.get("type") == "speed" for e in action.effects):
            self.effectRequested.emit("speed", "", action.note)
        elif action.kind == "attack":
            bonus = action.attack_bonus
            if bonus is None:
                bonus = _ability_attack_bonus(self.sheet)
            self.attackRequested.emit({
                "name": action.name,
                "attack_bonus": int(bonus),
                "damage": action.formula,
                "damage_type": action.damage_type,
            })
        elif action.kind == "damage":
            self.effectRequested.emit("damage", action.formula, action.note)
        elif action.kind == "utility" and not action.effects:
            self._notice(
                f"У предмета «{action.name}» нет урона, лечения "
                "или спасброска")
        self.refresh()
        self.changed.emit()

    def drop_selected(self):
        """Выбросить выбранное: поле «×» задаёт, сколько штук из стака.

        Раньше кнопка всегда убирала весь стак, а поле количества рядом
        ничего не делало. Теперь можно выбросить часть, и диалог говорит
        точное число, которое исчезнет.
        """
        item = self._selected()
        if not item:
            return
        try:
            stack = max(1, int(item.get("qty", 1) or 1))
        except (TypeError, ValueError):
            stack = 1
        count = min(max(1, self.qty_spin.value()), stack)
        name = str(item.get("name") or "предмет")
        whole = count >= stack
        question = (f"Убрать «{name}» из инвентаря?" if whole
                    else f"Убрать {count} шт. из стака «{name}» "
                         f"({stack} → {stack - count})?")
        if QMessageBox.question(self, "Выбросить", question) != QMessageBox.Yes:
            return
        items.remove_item(self.sheet, item["id"], None if whole else count)
        self.refresh()
        self.changed.emit()

def _ability_attack_bonus(sheet: dict) -> int:
    """Бонус атаки по лучшей из Силы и Ловкости плюс мастерство."""
    from shared import lss_format as lss
    stats = sheet.get("stats", {})
    best = max(lss.modifier((stats.get(a) or {}).get("score", 10))
               for a in ("str", "dex"))
    return best + lss.proficiency_bonus(sheet)


class SpellsTab(QWidget):
    """Вкладка «Заклинания»: книга, подготовка, ячейки, применение."""

    rollRequested = pyqtSignal(str, str)
    #: единое действие: лечение, урон, спасбросок или атака
    useActionRequested = pyqtSignal(dict)
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sheet = None
        #: Токен, которому принадлежит книга: по нему перетаскивание
        #: находит лист колдуна у мастера с несколькими страницами.
        self.token_id = ""
        l = QVBoxLayout(self)
        l.setContentsMargins(6, 6, 6, 6)
        l.setSpacing(6)

        self.slots_label = QLabel("")
        self.slots_label.setObjectName("sheet_sub")
        self.slots_label.setWordWrap(True)
        l.addWidget(self.slots_label)

        # Книга заклинаний тоже перетаскивается на токен: метку охотника,
        # лечение и снаряды можно навести прямо на цель на карте.
        self.list = ItemDragList()
        self.list.set_payload_builder(self._drag_payload)
        # Та же бронзовая подсветка строки, что у карточек и слотов: синяя
        # заливка QListWidget в тёмном листе выглядела чужой.
        self.list.setItemDelegate(CardSelectionDelegate(self.list))
        self.list.itemDoubleClicked.connect(lambda _item: self.cast_selected())
        self.list.currentItemChanged.connect(self._update_buttons)
        # Enter на выбранной строке — то же, что двойной клик. Подсказка
        # списка обещает это, а штатный QListWidget Enter не обрабатывает.
        self.list.installEventFilter(self)
        self.list.setToolTip(
            "Книга заклинаний: Enter или двойной клик — применить; "
            "✓ — подготовлено, ○ — только в книге")
        l.addWidget(self.list, 1)

        # Пустая книга раньше выглядела просто пустым списком. Подсказка
        # объясняет, откуда взять заклинания, и исчезает, когда они есть.
        self.empty_hint = QLabel(
            "В книге нет заклинаний · добавьте их из справочника "
            "или загрузите лист LSS")
        self.empty_hint.setObjectName("sheet_sub")
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setVisible(False)
        l.addWidget(self.empty_hint)

        # Круг ячейки: заклинание можно усилить, потратив ячейку выше.
        # Список собирается по свободным ячейкам листа, поэтому «3 круг»
        # не появится, когда третих ячеек уже нет.
        slot_row = QHBoxLayout()
        slot_row.setSpacing(4)
        slot_row.addWidget(QLabel("Ячейка:"))
        self.slot_combo = QComboBox()
        self.slot_combo.setToolTip(
            "Круг ячейки: выше круга заклинания — сильнее урон, больше "
            "целей или снарядов")
        self.slot_combo.currentIndexChanged.connect(self._revalidate)
        slot_row.addWidget(self.slot_combo, 1)
        l.addLayout(slot_row)
        self._slot_spell_id = None

        row = QHBoxLayout()
        row.setSpacing(4)
        self.cast_btn = QPushButton("Применить")
        self.cast_btn.setToolTip(
            "Потратить ячейку и бросить урон или спасбросок")
        self.cast_btn.clicked.connect(self.cast_selected)
        row.addWidget(self.cast_btn)
        self.prepare_btn = QPushButton("Подготовить")
        self.prepare_btn.setToolTip("Отметить заклинание как подготовленное")
        self.prepare_btn.clicked.connect(self.toggle_prepared)
        row.addWidget(self.prepare_btn)
        self.remove_btn = QPushButton("Убрать")
        self.remove_btn.setToolTip("Убрать заклинание из книги")
        self.remove_btn.clicked.connect(self.remove_selected)
        row.addWidget(self.remove_btn)
        l.addLayout(row)

        # Отдых — в меню мастера (костёр в верхней рейке), а не в книге:
        # он возвращает ресурсы всему столу сразу.
        self._update_buttons()

    def set_token_id(self, token_id: str):
        """Привязать книгу к токену: чей лист тратит ячейку."""
        self.token_id = str(token_id or "")

    def _drag_payload(self, item):
        """Запись книги → переносимый payload для холста."""
        if item is None or not self.sheet:
            return None
        spell = items.find_spell(self.sheet, item.data(Qt.UserRole))
        if spell is None:
            return None
        return drag_payload({"kind": "spell", "spell": spell,
                             "name": spell.get("name") or "Заклинание",
                             "slot_level": self._slot_level(),
                             "usable": True},
                            source="spell",
                            token_id=getattr(self, "token_id", "") or "")

    def set_sheet(self, sheet: dict):
        self.sheet = sheet
        if sheet is not None:
            items.ensure(sheet)
            # Книга LSS приходит ссылками в обёртке файла. Подтягиваем её
            # один раз (по подписи), иначе вкладка оставалась пустой.
            if isinstance((sheet.get("vtt") or {}).get("lss_spells"), dict):
                items.import_lss_spells(sheet, _lss_catalog())
        self.refresh()

    def refresh(self):
        # Список пересобирается — круг ячейки тоже надо пересчитать.
        self._slot_spell_id = None
        current = self.list.currentItem()
        previous_id = current.data(Qt.UserRole) if current is not None else None
        previous_row = self.list.currentRow()
        scroll = self.list.verticalScrollBar().value()
        self.list.clear()
        self.empty_hint.setVisible(False)
        if not self.sheet:
            self.slots_label.setText("Лист не загружен")
            return
        block = items.ensure(self.sheet)
        cells = []
        for level in range(1, 10):
            state = items.slots_for(self.sheet, level)
            mx = int(state.get("max", 0))
            if mx:
                left = mx - int(state.get("used", 0))
                cells.append(f"{level}: {left}/{mx}")
        self.slots_label.setText(
            "Ячейки — " + "   ".join(cells) if cells else "Ячеек нет")

        spells = sorted(block["spells"],
                        key=lambda s: (int(s.get("level") or 0),
                                       s.get("name", "").lower()))
        self.empty_hint.setVisible(not spells)
        for spell in spells:
            level = int(spell.get("level") or 0)
            mark = "✓ " if spell.get("prepared") else "○ "
            lvl = "заговор" if level == 0 else f"{level} ур."
            text = f"{mark}{spell.get('name', '?')}   ({lvl})"
            dmg = _clean(spell.get("damage"))
            if dmg:
                text += f"  {dmg}"
            row = QListWidgetItem(text)
            row.setData(Qt.UserRole, spell.get("id"))
            # В подсказке сразу видно круг и уровень: по одной подписи
            # строки не понять, это «Огненный шар» 3 круга или заговор.
            school = _clean(spell.get("school"))
            header = f"{spell.get('name', '?')} · {lvl}"
            if school:
                header += f" · {school}"
            description = str(spell.get("description", "") or "").strip()
            row.setToolTip(header + (f"\n\n{description[:400]}"
                                     if description else ""))
            self.list.addItem(row)
        # «Подготовить» и «Применить» перестраивают книгу; без возврата
        # выделения следующее нажатие молча уходило бы в никуда.
        if previous_id is not None and self.list.count():
            for index in range(self.list.count()):
                if self.list.item(index).data(Qt.UserRole) == previous_id:
                    self.list.setCurrentRow(index)
                    break
            else:
                self.list.setCurrentRow(min(max(previous_row, 0),
                                            self.list.count() - 1))
        # Как и в инвентаре: выделение переживает перестройку, а вид
        # книги не прыгает к первой строке после каждого применения.
        self.list.verticalScrollBar().setValue(scroll)
        self._update_buttons()

    def eventFilter(self, watched, event):
        """Enter в книге применяет заклинание, как и двойной клик."""
        if (watched is self.list and event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and self.list.currentItem() is not None):
            self.cast_selected()
            return True
        return super().eventFilter(watched, event)

    def _slot_level(self):
        """Выбранный круг ячейки (None — свой круг заклинания или заговор)."""
        try:
            return int(self.slot_combo.currentData() or 0) or None
        except (TypeError, ValueError):
            return None

    def _refresh_slot_combo(self, spell):
        """Пересобрать список кругов под выбранное заклинание."""
        combo = self.slot_combo
        level = int((spell or {}).get("level") or 0) if spell else 0
        spell_id = (spell or {}).get("id")
        if spell_id == self._slot_spell_id and combo.count():
            return
        self._slot_spell_id = spell_id
        combo.blockSignals(True)
        combo.clear()
        if not spell or not self.sheet:
            combo.addItem("—", 0)
            combo.setEnabled(False)
        elif level <= 0:
            combo.addItem("Заговор", 0)
            combo.setEnabled(False)
        else:
            for ring in range(level, 10):
                cells = items.slots_for(self.sheet, ring)
                left = int(cells.get("max", 0)) - int(cells.get("used", 0))
                if ring > level and left <= 0:
                    continue
                suffix = (" (свой круг)" if ring == level
                          else f" — свободно {left}")
                combo.addItem(f"{ring} круг{suffix}", ring)
            combo.setEnabled(True)
        combo.blockSignals(False)

    def _revalidate(self, *_args):
        """Проверить, можно ли применить с выбранной ячейкой."""
        spell = self._selected()
        if spell is None or not self.sheet:
            return
        try:
            action = use_action.from_spell(
                spell, self.sheet,
                token_id=getattr(self, "token_id", ""),
                slot_level=self._slot_level())
            usable, reason = use_action.can_use(self.sheet, action)
        except (TypeError, ValueError, KeyError, AttributeError):
            usable, reason = True, ""
        self.cast_btn.setEnabled(usable)
        if not usable:
            self.cast_btn.setToolTip(f"«{spell.get('name')}»: {reason}")

    def _update_buttons(self, *_args):
        """Без выбора кнопки не притворяются рабочими."""
        spell = self._selected()
        has_spell = spell is not None
        self._refresh_slot_combo(spell)
        self.cast_btn.setEnabled(has_spell)
        self.remove_btn.setEnabled(has_spell)
        self.cast_btn.setToolTip(
            f"Применить «{spell.get('name')}»" if has_spell
            else "Сначала выберите заклинание в книге")
        self.remove_btn.setToolTip(
            f"Убрать «{spell.get('name')}» из книги" if has_spell
            else "Сначала выберите заклинание в книге")
        if not has_spell:
            self.prepare_btn.setEnabled(False)
            self.prepare_btn.setToolTip("Сначала выберите заклинание в книге")
            return

        # Ячейки и подготовка проверяются при выборе, а не только по клику:
        # серая кнопка с причиной честнее живой кнопки, которая ругается
        # после нажатия. Броском это не считается — цель выбирается позже.
        try:
            action = use_action.from_spell(
                spell, self.sheet,
                token_id=getattr(self, "token_id", ""),
                slot_level=self._slot_level())
            usable, reason = use_action.can_use(self.sheet, action)
        except (TypeError, ValueError, KeyError, AttributeError):
            usable, reason = True, ""
        if not usable:
            self.cast_btn.setEnabled(False)
            self.cast_btn.setToolTip(f"«{spell.get('name')}»: {reason}")

        level = int(spell.get("level") or 0)
        self.prepare_btn.setEnabled(level > 0)
        if level <= 0:
            self.prepare_btn.setToolTip("Заговоры всегда подготовлены")
        elif spell.get("prepared"):
            self.prepare_btn.setToolTip(
                f"Снять «{spell.get('name')}» с подготовки")
        else:
            self.prepare_btn.setToolTip(
                f"Подготовить «{spell.get('name')}»")

    def _selected(self):
        row = self.list.currentItem()
        if row is None or not self.sheet:
            return None
        return items.find_spell(self.sheet, row.data(Qt.UserRole))

    def toggle_prepared(self):
        spell = self._selected()
        if not spell:
            return
        if int(spell.get("level") or 0) == 0:
            return      # заговоры готовы всегда
        spell["prepared"] = not spell.get("prepared")
        self.refresh()
        self.changed.emit()

    def cast_selected(self):
        """Применить заклинание одним UseAction.

        Ячейка здесь не тратится: пайплайн сначала проверяет цель и
        подготовку, потом списывает ячейку, бросает и применяет эффект.
        """
        spell = self._selected()
        if not spell:
            return
        action = use_action.from_spell(spell, self.sheet,
                                       token_id=getattr(self, "token_id", ""),
                                       slot_level=self._slot_level())
        # Проверяем расход здесь, на том листе, который игрок видит. Окно
        # проверяет ещё раз, но если оно возьмёт другую копию листа, кнопка
        # «Применить» оставалась бы живой при нуле ячеек.
        ok, reason = use_action.can_use(self.sheet, action)
        if not ok:
            self.slots_label.setText(f"⚠ {reason}")
            return
        self.useActionRequested.emit(action.to_dict())
        self.refresh()
        self.changed.emit()

    def remove_selected(self):
        spell = self._selected()
        if not spell:
            return
        items.remove_spell(self.sheet, spell["id"])
        self.refresh()
        self.changed.emit()


class ConditionsPanel(QWidget):
    """Состояния внизу листа — галочками с иконками, как в карточке актёра.

    Раньше это была строка «+»/«−» с общим списком: состояние снималось
    только из листа, токен продолжал его показывать (правила объединяют
    список листа и токена), поэтому «−» выглядел нерабочим. Теперь каждая
    галочка сразу задаёт полный список состояний токена.
    """

    #: лист изменился — сохранить его в мире
    changed = pyqtSignal()
    #: полный список состояний для токена (наложить/снять сразу)
    conditionsToggled = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sheet = None
        self._building = False
        self.checks = {}

        l = QVBoxLayout(self)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(4)

        title = QLabel("СОСТОЯНИЯ")
        title.setObjectName("sheet_section")
        l.addWidget(title)

        self.summary = QLabel("Состояний нет")
        self.summary.setObjectName("sheet_sub")
        self.summary.setWordWrap(True)
        l.addWidget(self.summary)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(3)
        for index, name in enumerate(items.CONDITIONS_RU):
            check = QCheckBox(name)
            check.setObjectName("sheet_condition")
            icon = self._condition_icon(name)
            if not icon.isNull():
                check.setIcon(icon)
                check.setIconSize(QSize(18, 18))
            rules = conditions_rules.rules_text(name)
            check.setToolTip(
                f"<b>{name}</b><br>{rules.replace(chr(10), '<br>')}"
                if rules else f"Состояние «{name}» — включается сразу")
            check.toggled.connect(
                lambda checked, n=name: self._on_toggled(n, checked))
            self.checks[name] = check
            grid.addWidget(check, index // 2, index % 2)
        # Истощение — единственное состояние с уровнями 1–6: от него зависят
        # помехи, скорость и смерть на шестом. Уровень хранится в названии
        # («Истощён 3»), а без поля в листе галочка умела только первую
        # степень — и та без выбора.
        self.exhaustion_spin = QSpinBox()
        self.exhaustion_spin.setObjectName("sheet_exhaustion")
        self.exhaustion_spin.setRange(1, 6)
        # Один разряд плюс стрелки: префикс «ур.» — отдельной подписью,
        # иначе текст не влезал и в поле оставалось «ур. …».
        self.exhaustion_spin.setFixedWidth(46)
        self.exhaustion_spin.setEnabled(False)
        self.exhaustion_spin.setToolTip(
            "Уровень истощения 1–6: 1 — помеха на проверки, 3 — помеха на "
            "проверки и спасброски, 5 — скорость 0, 6 — смерть")
        self.exhaustion_spin.valueChanged.connect(self._on_exhaustion_level)
        level_row = QHBoxLayout()
        level_row.setContentsMargins(0, 0, 0, 0)
        level_row.setSpacing(3)
        level_label = QLabel("ур.")
        level_label.setObjectName("sheet_cell_title")
        level_label.setToolTip(self.exhaustion_spin.toolTip())
        level_row.addWidget(level_label)
        level_row.addWidget(self.exhaustion_spin)
        level_row.addStretch(1)
        grid.addLayout(level_row,
                       (len(items.CONDITIONS_RU) - 1) // 2,
                       (len(items.CONDITIONS_RU) - 1) % 2 + 1)
        l.addLayout(grid)

    @staticmethod
    def _condition_icon(name: str):
        """Иконка состояния из набора проекта (ленивый импорт: без цикла)."""
        from PyQt5.QtGui import QIcon
        try:
            from client.game_canvas import CONDITION_ICON_DIR, condition_icon_key
            key = condition_icon_key(name)
            if key:
                path = os.path.join(CONDITION_ICON_DIR, key + ".png")
                if os.path.isfile(path):
                    return QIcon(path)
        except Exception:
            pass
        return QIcon()

    # ------------------------------------------------------------------
    def set_sheet(self, sheet: dict):
        """Показать состояния листа, не тревожа сигналы галочек."""
        self.sheet = sheet
        if sheet is not None:
            items.ensure(sheet)
        self.refresh()

    def active_conditions(self) -> list:
        """Состояния из листа в том виде, в каком их понимает бой."""
        if not self.sheet:
            return []
        names = []
        for entry in items.ensure(self.sheet)["conditions"]:
            name = entry.get("name") if isinstance(entry, dict) else entry
            text = str(name or "").strip()
            if text and text not in names:
                names.append(text)
        return names

    def refresh(self):
        if not self.sheet:
            return
        names = self.active_conditions()
        active = {conditions_rules.canonical(n) for n in names}
        active.discard(None)
        exhausted = "Истощён" in active
        self._building = True
        try:
            for name, check in self.checks.items():
                check.blockSignals(True)
                check.setChecked(name in active)
                check.blockSignals(False)
            # Галочка и уровень — одно состояние: без истощения поле
            # уровня недоступно и показывает первую степень.
            level = max(1, conditions_rules.exhaustion_level(names))
            self.exhaustion_spin.blockSignals(True)
            self.exhaustion_spin.setValue(level if exhausted else 1)
            self.exhaustion_spin.blockSignals(False)
            self.exhaustion_spin.setEnabled(exhausted)
            self.exhaustion_spin.setToolTip(
                f"Уровень истощения: {level} из 6 — меняется здесь же"
                if exhausted else
                "Сначала отметьте «Истощён»: уровень 1–6 включается после")
        finally:
            self._building = False
        self._update_summary()

    def _update_summary(self):
        """Короткая строка: что из состояний сейчас действует."""
        names = self.active_conditions()
        if not names:
            self.summary.setText("Состояний нет")
            return
        text = ", ".join(names)
        can_act, why = conditions_rules.can_act(names)
        if not can_act:
            text += f"  ·  не может действовать ({why})"
        elif not conditions_rules.can_move(names)[0]:
            text += "  ·  скорость 0"
        self.summary.setText(text)

    def _exhaustion_name(self) -> str:
        """Имя для листа: уровень 1 — просто «Истощён», дальше — с цифрой."""
        level = max(1, int(self.exhaustion_spin.value()))
        return "Истощён" if level <= 1 else f"Истощён {level}"

    def _on_exhaustion_level(self, _value: int):
        """Уровень истощения поменяли: имя состояния в листе — с цифрой."""
        if self._building or not self.sheet:
            return
        if not self.checks["Истощён"].isChecked():
            return
        block = items.ensure(self.sheet)
        conds = block["conditions"]
        stored = self._exhaustion_name()
        for index, entry in enumerate(conds):
            raw = entry.get("name") if isinstance(entry, dict) else entry
            if conditions_rules.canonical(raw) != "Истощён":
                continue
            conds[index] = {"name": stored,
                            "rounds": (entry.get("rounds")
                                       if isinstance(entry, dict) else None)}
            break
        else:
            conds.append({"name": stored, "rounds": None})
        self._update_summary()
        self.changed.emit()
        self.conditionsToggled.emit(self.active_conditions())

    def _on_toggled(self, name: str, checked: bool):
        """Галочка: полный список состояний листа и сразу — на токен."""
        if self._building or not self.sheet:
            return
        block = items.ensure(self.sheet)
        conds = block["conditions"]
        stored = (self._exhaustion_name()
                  if name == "Истощён" and checked else name)
        if checked:
            if not any(conditions_rules.canonical(c.get("name"))
                       == name for c in conds if isinstance(c, dict)):
                conds.append({"name": stored, "rounds": None})
        else:
            block["conditions"] = [
                c for c in conds
                if conditions_rules.canonical(
                    c.get("name") if isinstance(c, dict) else c) != name]
        items.ensure(self.sheet)
        self.refresh()
        self.changed.emit()
        self.conditionsToggled.emit(self.active_conditions())
