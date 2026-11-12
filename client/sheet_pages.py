"""Вкладка «Лист персонажа»: страница на каждого токена игрока.

Страницы персонажей стоят вверху вкладки и переключаются. Пока у токена
нет листа, на его странице посередине одна кнопка «LSS лист»: она загружает
файл листа Long Story Short (.json) для конкретного персонажа и убирается —
дальше лист живёт прямо во вкладке, без отдельного окна.

Сам лист — тот же виджет, что у игрока в левой панели
(client/character_sheet.py): броски из него уходят в чат стола, инвентарь
и заклинания правятся на месте. Связь с главным окном — сигналы:

  * ``sheetLoaded(token_id, sheet)`` — лист загрузили в страницу;
  * ``sheetEdited(token_id, sheet)`` — лист изменили прямо на странице;
  * ``rollRequested`` / ``effectRequested`` / ``attackRequested`` — те же
    действия, что и у листа персонажа игрока; эффект дополнительно
    приходит как ``effectRequestedForToken(token_id, kind, formula, note)``,
    чтобы зелье применилось к тому токену, чья страница открыта.
"""
import os

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QAbstractSpinBox, QButtonGroup, QDoubleSpinBox,
                             QFileDialog, QFrame, QHBoxLayout, QLabel,
                             QPushButton, QScrollArea, QSpinBox, QStackedWidget,
                             QVBoxLayout, QWidget)

from shared import lss_format as lss
from shared import vision as vision_rules
from client.character_sheet import CharacterSheet


class SheetPages(QWidget):
    """Страницы листов персонажей для вкладки «Лист персонажа»."""

    #: лист загрузили в страницу токена (token_id, sheet)
    sheetLoaded = pyqtSignal(str, dict)
    #: лист изменили на странице (token_id, sheet)
    sheetEdited = pyqtSignal(str, dict)
    #: бросок с листа — в чат стола (formula, note)
    rollRequested = pyqtSignal(str, str)
    #: единое действие предмета/заклинания с id токена-владельца страницы
    useActionRequested = pyqtSignal(dict)
    #: эффект предмета (kind, formula, note)
    effectRequested = pyqtSignal(str, str, str)
    #: то же, но сразу с id токена: у мастера открыто несколько страниц,
    #: и «первый свой токен» выбрать нельзя.
    effectRequestedForToken = pyqtSignal(str, str, str, str)
    #: оружие с листа — открыть диалог атаки (dict)
    attackRequested = pyqtSignal(dict)
    #: спасбросок от смерти с листа страницы (token_id)
    deathSaveRollRequested = pyqtSignal(str)
    #: заряд особенности списан сразу (token_id, sheet) — без задержки ввода
    resourceSpent = pyqtSignal(str, dict)
    #: ручная отметка кружка (token_id, индекс, значение)
    deathSaveSetRequested = pyqtSignal(str, int, str)
    #: воскрешение мёртвого (token_id) — только мастер
    deathReviveRequested = pyqtSignal(str)
    #: стабилизация умирающего (token_id) — только мастер
    deathStabilizeRequested = pyqtSignal(str)
    #: проверка Скрытности страницы (token_id) — бросок делает сервер
    stealthCheckRequested = pyqtSignal(str)
    #: состояния на листе страницы (token_id, полный список состояний)
    conditionsChanged = pyqtSignal(str, list)
    #: разбор листа добавил оружие/особенности — сообщить в чат стола
    sheetAudited = pyqtSignal(dict)
    #: мастер задал размер токена открытой страницы (token_id, клетки)
    tokenSizeChanged = pyqtSignal(str, float)
    #: задан угол зрения или дальность (token_id, угол, футы)
    tokenVisionChanged = pyqtSignal(str, float, float)

    def __init__(self, parent=None, sheet_provider=None, file_picker=None):
        super().__init__(parent)
        self.setObjectName("sheet_pages")
        #: token_id -> {"bar", "page", "placeholder", "button", "error", "sheet"}
        self._pages = {}
        self._order = []
        self._sheet_provider = sheet_provider
        self._file_picker = file_picker
        #: хиты в листе правит только мастер (см. set_hp_editable)
        self._hp_editable = False
        #: размер токена — настройка стола, а не листа; тоже только мастеру
        self._size_editable = False
        self._token_sizes = {}
        #: token_id -> (угол зрения, дальность в футах)
        self._token_vision = {}
        self._build()

    # ------------------------------------------------------------------
    # Построение
    # ------------------------------------------------------------------
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # Строка страниц вверху: одна кнопка на токен игрока.
        self.bar = QWidget()
        self.bar.setObjectName("sheet_page_bar")
        self._bar_layout = QHBoxLayout(self.bar)
        self._bar_layout.setContentsMargins(0, 0, 0, 0)
        self._bar_layout.setSpacing(4)
        self._bar_layout.addStretch(1)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        scroll = QScrollArea()
        scroll.setObjectName("sheet_page_scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedHeight(42)
        scroll.setWidget(self.bar)

        # Размер токена — настройка стола, а не листа. Поле стоит в шапке
        # листа на уровне имени (CharacterSheet.head_extra): в строке
        # переключателя персонажей оно наезжало на вкладки, когда их
        # становилось больше одной.
        self.size_box = QWidget()
        self.size_box.setObjectName("sheet_size_box")
        sl = QHBoxLayout(self.size_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(4)
        sl.addStretch(1)
        self.size_title = QLabel("РАЗМЕР")
        self.size_title.setObjectName("sheet_cell_title")
        self.size_title.setToolTip("Размер токена на карте: не входит в лист")
        # Общая ширина подписи выравнивает все три поля по одной линии.
        self.size_title.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.size_title.setFixedWidth(84)
        sl.addWidget(self.size_title)
        self.size_spin = QDoubleSpinBox()
        self.size_spin.setObjectName("sheet_size_spin")
        self.size_spin.setRange(0.5, 20.0)
        self.size_spin.setSingleStep(0.5)
        self.size_spin.setDecimals(1)
        self.size_spin.setValue(1.0)
        self.size_spin.setButtonSymbols(QAbstractSpinBox.UpDownArrows)
        self.size_spin.setAlignment(Qt.AlignCenter)
        self.size_spin.setFixedWidth(68)
        self.size_spin.setToolTip(
            "Размер токена в клетках: 0.5 — мелкий, 1 — средний, 2 — крупный. "
            "На лист персонажа не записывается")
        self.size_spin.valueChanged.connect(self._on_size_edited)
        sl.addWidget(self.size_spin)
        self.size_box.setVisible(False)

        # Зрение токена: угол сектора и дальность. Живёт рядом с размером
        # в шапке листа, но видно и игроку: по этим числам сервер решает,
        # можно ли персонажу спрятаться (нельзя спрятаться от того, кто
        # смотрит в твою сторону). Поворот взгляда — клавиша R на холсте.
        self.vision_box = QWidget()
        self.vision_box.setObjectName("sheet_vision_box")
        vl = QVBoxLayout(self.vision_box)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(1)

        def _field_row(layout, title_text, tip, spin):
            """Строка «подпись — число», выровненная по правому краю."""
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            row.addStretch(1)
            label = QLabel(title_text)
            label.setObjectName("sheet_cell_title")
            label.setToolTip(tip)
            label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            label.setFixedWidth(84)
            row.addWidget(label)
            row.addWidget(spin)
            layout.addLayout(row)

        vision_title = "ЗРЕНИЕ"
        self.vision_angle_spin = QSpinBox()
        self.vision_angle_spin.setObjectName("sheet_vision_angle")
        self.vision_angle_spin.setRange(int(vision_rules.MIN_ANGLE),
                                        int(vision_rules.MAX_ANGLE))
        self.vision_angle_spin.setSingleStep(5)
        self.vision_angle_spin.setSuffix("°")
        self.vision_angle_spin.setValue(int(vision_rules.DEFAULT_VISION_ANGLE))
        self.vision_angle_spin.setAlignment(Qt.AlignCenter)
        self.vision_angle_spin.setFixedWidth(62)
        self.vision_angle_spin.setToolTip(
            "Угол обзора: 70° по умолчанию, 360° — круговой обзор")
        self.vision_angle_spin.valueChanged.connect(self._on_vision_edited)
        self.vision_range_spin = QSpinBox()
        self.vision_range_spin.setObjectName("sheet_vision_range")
        self.vision_range_spin.setRange(int(vision_rules.MIN_RANGE_FT),
                                        int(vision_rules.MAX_RANGE_FT))
        self.vision_range_spin.setSingleStep(5)
        self.vision_range_spin.setSuffix(" фт")
        self.vision_range_spin.setValue(
            int(vision_rules.DEFAULT_VISION_RANGE_FT))
        self.vision_range_spin.setAlignment(Qt.AlignCenter)
        self.vision_range_spin.setFixedWidth(78)
        self.vision_range_spin.setToolTip(
            "Дальность зрения: 60 фт по умолчанию. Бестиарий и лист "
            "подставляют своё число (тёмное зрение 90 фт → 90)")
        self.vision_range_spin.valueChanged.connect(self._on_vision_edited)
        _field_row(vl, vision_title, "Куда и насколько широко видит токен",
                   self.vision_angle_spin)
        _field_row(vl, "ДАЛЬНОСТЬ", "Дальность зрения в футах",
                   self.vision_range_spin)
        self.vision_box.setVisible(False)

        # Колонка настроек стола справа от имени. Раньше размер и зрение
        # стояли одной строкой и отнимали ширину у имени: «Аня — друид»
        # ломалось на две строки. Теперь три поля идут друг под другом
        # узкой колонкой, а имя получает всю оставшуюся ширину.
        self.token_box = QWidget()
        self.token_box.setObjectName("sheet_token_box")
        column = QVBoxLayout(self.token_box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(1)
        column.addWidget(self.size_box)
        column.addWidget(self.vision_box)
        self.token_box.setVisible(False)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)
        head.addWidget(scroll, 1)
        outer.addLayout(head)

        self.empty_label = QLabel(
            "Токенов персонажей нет: создайте токен с типом «Игрок» — "
            "страница появится сама.")
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignCenter)
        outer.addWidget(self.empty_label, 1)

        self.stack = QStackedWidget()
        self.stack.setObjectName("sheet_page_stack")
        outer.addWidget(self.stack, 1)

        self._refresh_empty()

    def _add_page(self, token):
        """Страница токена: заглушка с кнопкой и место под лист."""
        page = QStackedWidget()

        placeholder = QWidget()
        pl = QVBoxLayout(placeholder)
        pl.setContentsMargins(8, 8, 8, 8)
        pl.setSpacing(6)
        # Верхняя строка заглушки принимает поле размера токена, пока лист
        # не загружен: иначе мастер терял доступ к размеру до загрузки.
        placeholder_extra = QHBoxLayout()
        placeholder_extra.setContentsMargins(0, 0, 0, 0)
        placeholder_extra.setSpacing(6)
        placeholder_extra.addStretch(1)
        pl.addLayout(placeholder_extra)
        pl.addStretch(1)
        hint = QLabel("Лист ещё не загружен.\n"
                      "Нажмите «LSS лист» и выберите файл.")
        hint.setObjectName("sheet_page_hint")
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignCenter)
        pl.addWidget(hint)
        button = QPushButton("LSS лист")
        button.setObjectName("lss_load_btn")
        button.setToolTip("Загрузить лист персонажа Long Story Short (.json)")
        button.setFixedWidth(180)
        button.clicked.connect(
            lambda _=False, tid=token.id: self._on_load_clicked(tid))
        pl.addWidget(button, 0, Qt.AlignCenter)
        error = QLabel("")
        error.setWordWrap(True)
        error.setAlignment(Qt.AlignCenter)
        error.setVisible(False)
        pl.addWidget(error, 0, Qt.AlignCenter)
        pl.addStretch(1)

        sheet_widget = CharacterSheet()
        sheet_widget.set_token_id(token.id)
        sheet_widget.set_can_edit_hp(self._hp_editable)
        sheet_widget.rollRequested.connect(self.rollRequested.emit)
        # Действие всегда знает, чья страница: у мастера их несколько.
        sheet_widget.useActionRequested.connect(
            lambda payload, tid=token.id:
            self.useActionRequested.emit({**payload, "token_id": tid}))
        sheet_widget.effectRequested.connect(self.effectRequested.emit)
        sheet_widget.effectRequestedForToken.connect(
            self.effectRequestedForToken.emit)
        # Атака с листа страницы должна знать, ЧЕЙ это токен: у мастера
        # открыто несколько листов, и брать «первый свой» нельзя.
        sheet_widget.attackRequested.connect(
            lambda payload, tid=token.id:
            self.attackRequested.emit({**payload, "_token_id": tid}))
        sheet_widget.deathSaveRollRequested.connect(
            self.deathSaveRollRequested.emit)
        sheet_widget.deathSaveSetRequested.connect(
            self.deathSaveSetRequested.emit)
        sheet_widget.deathReviveRequested.connect(
            self.deathReviveRequested.emit)
        sheet_widget.deathStabilizeRequested.connect(
            self.deathStabilizeRequested.emit)
        # Скрытность считается по листу этой страницы: у мастера открыто
        # несколько персонажей, и «первый свой токен» тут не подходит.
        sheet_widget.stealthCheckRequested.connect(
            lambda _tid, tid=token.id: self.stealthCheckRequested.emit(tid))
        sheet_widget.sheetChanged.connect(
            lambda s, tid=token.id: self.sheetEdited.emit(tid, s))
        # Заряд тратится сразу: дебаунс ввода здесь только мешает — за
        # 400 мс эхо сервера успевало вернуть потраченное.
        sheet_widget.resourceSpent.connect(
            lambda _tid, s, tid=token.id:
            self.resourceSpent.emit(tid, s))
        sheet_widget.auditAdded.connect(self.sheetAudited.emit)
        sheet_widget.conditionsChanged.connect(
            lambda conds, tid=token.id:
            self.conditionsChanged.emit(tid, list(conds)))

        page.addWidget(placeholder)
        page.addWidget(sheet_widget)
        #: Куда переезжает поле размера, пока лист не загружен.
        placeholder_extra_host = placeholder_extra

        name = token.name or "Персонаж"
        bar_button = QPushButton(name)
        bar_button.setCheckable(True)
        bar_button.setToolTip(f"Страница персонажа: {name}")
        bar_button.clicked.connect(lambda _=False, tid=token.id: self.select(tid))
        self._group.addButton(bar_button)
        # Растяжка стоит последней — новая кнопка встаёт перед ней.
        self._bar_layout.insertWidget(self._bar_layout.count() - 1, bar_button)

        self.stack.addWidget(page)
        self._pages[token.id] = {
            "name": name,
            "bar": bar_button,
            "page": page,
            "size_layout": placeholder_extra_host,
            "placeholder": placeholder,
            "button": button,
            "error": error,
            "sheet": sheet_widget,
        }
        self._order.append(token.id)

    def _remove_page(self, token_id: str):
        page = self._pages.pop(token_id, None)
        if page is None:
            return
        if token_id in self._order:
            self._order.remove(token_id)
        self._group.removeButton(page["bar"])
        self._bar_layout.removeWidget(page["bar"])
        page["bar"].deleteLater()
        self.stack.removeWidget(page["page"])
        page["page"].deleteLater()

    def _refresh_empty(self):
        has_pages = bool(self._order)
        self.empty_label.setVisible(not has_pages)
        self.stack.setVisible(has_pages)
        self.size_spin.setEnabled(has_pages and self._size_editable)

    # ------------------------------------------------------------------
    # Данные
    # ------------------------------------------------------------------
    def sync_tokens(self, tokens):
        """Страницы для всех токенов игрока: создать новые, убрать старые.

        Принимает {id: Token} или список токенов. Порядок страниц — порядок
        появления: переключатель вверху не перескакивает при каждой правке.
        """
        items = list(tokens.values()) if isinstance(tokens, dict) else list(tokens)
        wanted = {t.id: t for t in items
                  if getattr(t, "kind", "actor") == "player"}
        for token_id in [t for t in self._order if t not in wanted]:
            self._remove_page(token_id)
            self._token_sizes.pop(token_id, None)
            self._token_vision.pop(token_id, None)
        for token_id, token in wanted.items():
            try:
                self._token_sizes[token_id] = float(token.size or 1.0)
            except (TypeError, ValueError):
                self._token_sizes[token_id] = 1.0
            self._token_vision[token_id] = (
                vision_rules.normalize_angle(
                    getattr(token, "vision_angle", None)),
                vision_rules.normalize_range(
                    getattr(token, "vision_range", None)))
            page = self._pages.get(token_id)
            if page is None:
                self._add_page(token)
                sheet = (self._sheet_provider(token_id)
                         if self._sheet_provider else None)
                if isinstance(sheet, dict):
                    # Сохранённый лист показываем сразу — кнопка не нужна.
                    self.set_sheet(token_id, sheet, emit=False)
                continue
            name = token.name or "Персонаж"
            if name != page["name"]:
                page["name"] = name
                page["bar"].setText(name)
                page["bar"].setToolTip(f"Страница персонажа: {name}")
        self._refresh_empty()
        if self._order and self.stack.currentWidget() is None:
            self.select(self._order[0])
        else:
            self._attach_size_box()
            self._sync_size_spin()
            self._attach_vision_box()

    def set_size_editable(self, editable: bool):
        """Показать мастеру поле размера токена в шапке листа."""
        self._size_editable = bool(editable)
        self.size_box.setVisible(self._size_editable)
        self._attach_size_box()
        self._sync_size_spin()

    def _attach_size_box(self, token_id: str = None):
        """Перенести поле размера в шапку открытого листа.

        Поле одно на вкладку: при переключении персонажа оно переезжает в
        шапку его страницы (или прячется, пока лист не загружен), поэтому
        не размножается по страницам и всегда показывает свой токен.
        """
        if not self._size_editable:
            self.size_box.setVisible(False)
            self._attach_token_box(token_id)
            return
        if token_id is None:
            token_id = self.current_token_id()
        page = self._pages.get(token_id)
        if page is None:
            self.size_box.setVisible(False)
            self._attach_token_box(token_id)
            return
        self.size_box.setVisible(True)
        self._attach_token_box(token_id)

    def _attach_token_box(self, token_id: str = None):
        """Поставить колонку настроек стола (размер и зрение) в шапку.

        Колонка одна на вкладку: при смене персонажа она переезжает в шапку
        его страницы (или в заглушку, пока лист не загружен), поэтому поля
        не размножаются по страницам и всегда показывают свой токен.
        """
        if token_id is None:
            token_id = self.current_token_id()
        page = self._pages.get(token_id)
        if page is None:
            self.token_box.setVisible(False)
            return
        sheet_widget = page["sheet"]
        # Лист загружен — колонка стоит рядом с именем персонажа; листа нет —
        # в строке над заглушкой, чтобы настройки всё равно были доступны.
        if page["page"].currentIndex() == 1:
            target = sheet_widget.head_extra
            host = sheet_widget
        else:
            target = page["size_layout"]
            host = target.parentWidget()
        if host is not None and self.token_box.parentWidget() is not host:
            target.addWidget(self.token_box, 0, Qt.AlignTop)
        # isHidden(), а не isVisible(): у скрытой колонки дети формально
        # невидимы, и по isVisible() колонка не могла бы снова появиться.
        self.token_box.setVisible(not self.size_box.isHidden()
                                  or not self.vision_box.isHidden())


    def _sync_size_spin(self, token_id=None):
        """Показать в поле размер открытого токена (без сигналов)."""
        if token_id is None:
            token_id = self.current_token_id()
        size = self._token_sizes.get(token_id)
        self.size_spin.blockSignals(True)
        try:
            if size is not None:
                self.size_spin.setValue(float(size))
            self.size_spin.setEnabled(bool(token_id) and self._size_editable)
        finally:
            self.size_spin.blockSignals(False)

    def _on_size_edited(self, value):
        """Мастер изменил размер: отдаём id страницы наружу."""
        if not self._size_editable:
            return
        token_id = self.current_token_id()
        if not token_id:
            return
        self._token_sizes[token_id] = float(value)
        self.tokenSizeChanged.emit(token_id, float(value))

    def _attach_vision_box(self, token_id: str = None):
        """Поставить поля зрения в шапку открытого листа.

        Поля одни на вкладку и переезжают при переключении персонажа —
        так они не размножаются по страницам и всегда показывают свой токен.
        """
        if token_id is None:
            token_id = self.current_token_id()
        page = self._pages.get(token_id)
        if page is None:
            self.vision_box.setVisible(False)
            self._attach_token_box(token_id)
            return
        self.vision_box.setVisible(True)
        self._attach_token_box(token_id)
        self._sync_vision_spins(token_id)

    def _sync_vision_spins(self, token_id: str = None):
        """Показать в полях зрение открытого токена (без сигналов)."""
        if token_id is None:
            token_id = self.current_token_id()
        if token_id is None or token_id not in self._token_vision:
            self.vision_angle_spin.setEnabled(False)
            self.vision_range_spin.setEnabled(False)
            return
        angle, feet = self._token_vision[token_id]
        for spin, value in ((self.vision_angle_spin, angle),
                            (self.vision_range_spin, feet)):
            spin.blockSignals(True)
            try:
                spin.setValue(int(round(value)))
            finally:
                spin.blockSignals(False)
            spin.setEnabled(True)

    def _on_vision_edited(self, _value=None):
        """Правка зрения: запомнить и отдать наружу (token_id, угол, футы)."""
        token_id = self.current_token_id()
        if not token_id:
            return
        angle = vision_rules.normalize_angle(self.vision_angle_spin.value())
        feet = vision_rules.normalize_range(self.vision_range_spin.value())
        if token_id in self._token_vision and \
                self._token_vision[token_id] == (angle, feet):
            return
        self._token_vision[token_id] = (angle, feet)
        self.tokenVisionChanged.emit(token_id, float(angle), float(feet))

    def _apply_sheet_vision(self, token_id: str):
        """Взять дальность зрения из описания листа, если она там есть.

        В листе LSS чувства лежат текстом («тёмное зрение 60 фт»), а не
        полем. Подставляем только поверх умолчания: ручную настройку
        мастера лист не перетирает.
        """
        page = self._pages.get(token_id)
        if page is None:
            return
        sheet = page["sheet"].sheet
        if not isinstance(sheet, dict):
            return
        found = vision_rules.settings_from_sheet(sheet)
        if not found:
            return
        angle, feet = self._token_vision.get(
            token_id, (vision_rules.DEFAULT_VISION_ANGLE,
                       vision_rules.DEFAULT_VISION_RANGE_FT))
        new_angle = angle
        new_feet = feet
        if (found.get("vision_angle")
                and angle == vision_rules.DEFAULT_VISION_ANGLE):
            new_angle = found["vision_angle"]
        if (found.get("vision_range")
                and feet == vision_rules.DEFAULT_VISION_RANGE_FT):
            new_feet = found["vision_range"]
        if (new_angle, new_feet) == (angle, feet):
            return
        self._token_vision[token_id] = (new_angle, new_feet)
        if token_id == self.current_token_id():
            self._sync_vision_spins(token_id)
        self.tokenVisionChanged.emit(token_id, float(new_angle),
                                     float(new_feet))

    def set_hp_editable(self, editable: bool):
        """Разрешить правку хитов в самих листах (вкладка мастера)."""
        self._hp_editable = bool(editable)
        for page in self._pages.values():
            page["sheet"].set_can_edit_hp(self._hp_editable)

    def hp_editable(self) -> bool:
        return bool(self._hp_editable)

    def set_death_state(self, token_id: str, results, hp,
                        can_revive: bool = False) -> bool:
        """Показать на странице токена его отметки смерти.

        Полоса видна только на нуле хитов — это решает сам виджет.
        """
        page = self._pages.get(token_id)
        if page is None:
            return False
        page["sheet"].set_death_state(results, hp, can_revive=can_revive)
        return True

    def set_sheet(self, token_id: str, sheet: dict, emit: bool = False) -> bool:
        """Показать лист на странице токена.

        Один и тот же лист второй раз не перерисовывается: иначе эхо с
        сервера сбивало бы курсор в полях и слало бы сообщения в сеть.
        """
        page = self._pages.get(token_id)
        if page is None or not isinstance(sheet, dict):
            return False
        if page["sheet"].sheet == sheet and page["page"].currentIndex() == 1:
            return True
        page["error"].setVisible(False)
        page["sheet"].set_sheet(sheet)
        page["page"].setCurrentIndex(1)
        # Лист появился — поле размера переезжает из заглушки в шапку.
        if token_id == self.current_token_id():
            self._attach_size_box(token_id)
            self._attach_vision_box(token_id)
        self._apply_sheet_vision(token_id)
        if emit:
            self.sheetLoaded.emit(token_id, sheet)
        return True

    def select(self, token_id: str) -> bool:
        """Переключить вкладку на страницу персонажа."""
        page = self._pages.get(token_id)
        if page is None:
            return False
        self.stack.setCurrentWidget(page["page"])
        if not page["bar"].isChecked():
            page["bar"].setChecked(True)
        self._attach_size_box(token_id)
        self._sync_size_spin(token_id)
        self._attach_vision_box(token_id)
        return True

    def current_token_id(self):
        widget = self.stack.currentWidget()
        for token_id, page in self._pages.items():
            if page["page"] is widget:
                return token_id
        return None

    # ------------------------------------------------------------------
    # Кнопка «LSS лист»
    # ------------------------------------------------------------------
    def _pick_file(self):
        if self._file_picker is not None:
            return self._file_picker()
        path, _ = QFileDialog.getOpenFileName(
            self, "Загрузить лист персонажа", "",
            "Лист Long Story Short (*.json);;Все файлы (*)")
        return path or None

    def _on_load_clicked(self, token_id: str):
        page = self._pages.get(token_id)
        if page is None:
            return
        path = self._pick_file()
        if not path:
            return                     # просто закрыли выбор файла
        try:
            with open(path, encoding="utf-8") as handle:
                sheet = lss.parse(handle.read())
        except (OSError, ValueError) as exc:
            page["error"].setText(
                f"Не загружено: {os.path.basename(path)} — {exc}")
            page["error"].setVisible(True)
            return
        page["error"].setVisible(False)
        self.set_sheet(token_id, sheet, emit=True)

    # ------------------------------------------------------------------
    # Доступ к страницам (нужно главному окну и тестам)
    # ------------------------------------------------------------------
    def token_ids(self) -> list:
        return list(self._order)

    def page_index(self, token_id: str) -> int:
        page = self._pages.get(token_id)
        return self.stack.indexOf(page["page"]) if page else -1

    def load_button(self, token_id: str):
        page = self._pages.get(token_id)
        return page["button"] if page else None

    def bar_button(self, token_id: str):
        page = self._pages.get(token_id)
        return page["bar"] if page else None

    def sheet_widget(self, token_id: str):
        page = self._pages.get(token_id)
        return page["sheet"] if page else None

    def shows_sheet(self, token_id: str) -> bool:
        """True, если на странице виден лист, а не заглушка с кнопкой."""
        page = self._pages.get(token_id)
        return bool(page) and page["page"].currentIndex() == 1

    def error_text(self, token_id: str) -> str:
        page = self._pages.get(token_id)
        return page["error"].text() if page else ""
