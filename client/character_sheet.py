"""Лист персонажа для игроков — в формате Long Story Short.

Сделан по образцу листа LSS: слева характеристики и спасброски, справа
навыки, сверху — имя, класс, уровень; ниже хиты, КД и скорость. Каждое
число кликабельно и бросает кубик в общий чат: это главное, зачем лист
нужен за столом.

Данные лежат в листе как есть (формат LSS), поэтому файл можно скачать,
отредактировать на longstoryshort.app и загрузить обратно.
"""
import os

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (QAbstractSpinBox, QCheckBox, QFileDialog, QFrame,
                             QGridLayout, QHBoxLayout, QLabel,
                             QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                             QScrollArea, QSizePolicy, QSpinBox,
                             QTabWidget, QToolButton, QVBoxLayout, QWidget)

from shared import class_features
from shared import lighting as lighting_rules
from shared import lss_format as lss
from shared import sheet_audit
from shared import sheet_items as items
from shared import use_action
from client.attack_editor import AttackEditorDialog, AttackRow
from client.death_saves import DeathSavesBar
from client.sheet_tabs import ConditionsPanel, InventoryTab, SpellsTab


class RollButton(QPushButton):
    """Кнопка-число: показывает бонус, по нажатию бросает d20 + бонус."""

    def __init__(self, bonus: int, parent=None):
        super().__init__(parent)
        sign = f"+{bonus}" if bonus >= 0 else str(bonus)
        self.setText(sign)
        self.bonus = bonus
        self.setFixedWidth(46)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(f"Бросить d20 {sign}")


class CharacterSheet(QWidget):
    """Лист персонажа игрока."""

    #: просим бросить формулу; подпись уходит в чат рядом с результатом
    rollRequested = pyqtSignal(str, str)
    #: единое действие предмета/заклинания — единый пайплайн в main_window
    useActionRequested = pyqtSignal(dict)
    #: старые сигналы оставлены для совместимости с внешним кодом
    effectRequested = pyqtSignal(str, str, str)
    #: тот же эффект вместе с id токена: страницы мастеру нужны адресно
    effectRequestedForToken = pyqtSignal(str, str, str, str)
    #: оружие с листа — открыть диалог атаки на выбранную цель
    attackRequested = pyqtSignal(dict)
    #: лист изменился — нужно сохранить в мире
    sheetChanged = pyqtSignal(dict)
    #: состояния отмечены/сняты — сразу применить их к токену
    conditionsChanged = pyqtSignal(list)
    #: заряд особенности списан (token_id, sheet) — сохранить без задержки
    resourceSpent = pyqtSignal(str, dict)
    #: разбор листа добавил оружие/особенности — сказать об этом в чате
    auditAdded = pyqtSignal(dict)
    #: спасбросок от смерти (token_id); бросок делает сервер
    deathSaveRollRequested = pyqtSignal(str)
    #: ручная отметка кружка (token_id, индекс, значение)
    deathSaveSetRequested = pyqtSignal(str, int, str)
    #: воскрешение мёртвого (token_id) — только мастер
    deathReviveRequested = pyqtSignal(str)
    #: стабилизация умирающего (token_id) — только мастер
    deathStabilizeRequested = pyqtSignal(str)
    #: проверка Скрытности (token_id): бросок и сравнение с пассивным
    #: Восприятием делает сервер, чтобы за столом был один результат
    stealthCheckRequested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sheet = None
        self._building = False
        #: Отпечатки того, что уже нарисовано. Пока они совпадают с листом,
        #: блок не пересобирается: эхо сервера (расход заряда, отдых)
        #: не должно перестраивать всю ленту листа на глазах у игрока.
        self._abilities_key = None
        self._skills_key = None
        self._features_key = None
        self._feature_widgets = []
        #: какому токену принадлежит этот лист (для спасбросков от смерти)
        self.token_id = None
        #: хиты правит только мастер — игрок видит их числом
        self._can_edit_hp = False
        self._build()

    # ------------------------------------------------------------------
    # Построение
    # ------------------------------------------------------------------
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # --- шапка: имя, класс, уровень и файл листа ---
        # Кнопки файла живут сверху, а не под лентой: внизу лист должен
        # заканчиваться содержимым (инвентарь, состояния), а не служебными
        # действиями, до которых приходится прокручивать всю страницу.
        head = QWidget()
        head.setObjectName("sheet_head")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(10, 9, 10, 8)
        hl.setSpacing(6)

        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.name_label = QLabel("Лист не загружен")
        self.name_label.setObjectName("sheet_name")
        f = QFont()
        f.setPointSize(12)
        f.setBold(True)
        self.name_label.setFont(f)
        self.name_label.setWordWrap(True)
        titles.addWidget(self.name_label)

        self.sub_label = QLabel("")
        self.sub_label.setObjectName("sheet_sub")
        self.sub_label.setWordWrap(True)
        titles.addWidget(self.sub_label)
        hl.addLayout(titles, 1)

        # Место для настроек стола на уровне имени: у мастера здесь живёт
        # размер токена (вкладка «Лист персонажа»), у игрока место пустое.
        # Раньше поле висело в строке переключателя персонажей и наезжало
        # на вкладки, когда их было две и больше.
        self.head_extra = QHBoxLayout()
        self.head_extra.setContentsMargins(0, 0, 0, 0)
        self.head_extra.setSpacing(6)
        hl.addLayout(self.head_extra, 0)

        # Одна кнопка вместо двух: панель узкая, а два длинных заголовка
        # рядом с именем сжимали его до многоточия. В меню — те же два
        # действия с пояснениями и горячими клавишами окна.
        self.file_btn = QPushButton("Лист ▾")
        self.file_btn.setObjectName("sheet_file_btn")
        self.file_btn.setToolTip(
            "Открыть лист Long Story Short или сохранить файл")
        self.file_btn.setCursor(Qt.PointingHandCursor)
        file_menu = QMenu(self.file_btn)
        # Без этого Qt не показывает подсказки пунктов меню, и объяснения
        # «что именно загрузится» остаются невидимыми.
        file_menu.setToolTipsVisible(True)
        action = file_menu.addAction("Загрузить .json…")
        action.setToolTip("Выбрать лист Long Story Short или экспорт Foundry")
        action.triggered.connect(lambda _checked=False: self.load_dialog())
        action = file_menu.addAction("Сохранить файлом…")
        action.setToolTip("Сохранить лист .json — его принимает "
                          "longstoryshort.app")
        action.triggered.connect(lambda _checked=False: self.save_dialog())
        self.file_btn.setMenu(file_menu)
        hl.addWidget(self.file_btn, 0, Qt.AlignTop)
        outer.addWidget(head)

        # --- прокручиваемая часть ---
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(8, 8, 8, 8)
        self.body_layout.setSpacing(9)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # --- боевые показатели ---
        self.vital_box = QWidget()
        vl = QHBoxLayout(self.vital_box)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(6)
        self.hp_value = QLabel("—")
        self.hp_value.setObjectName("sheet_vital")
        # Правка хитов прямо в листе — только мастеру. У игрока число
        # остаётся подписью: хиты меняет бой, а не рука игрока.
        self.hp_spin = QSpinBox()
        self.hp_spin.setObjectName("sheet_hp_spin")
        # Ниже нуля хитов не бывает: на нуле персонаж кидает спасброски.
        self.hp_spin.setRange(0, 9999)
        self.hp_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.hp_spin.setAlignment(Qt.AlignCenter)
        self.hp_spin.setToolTip(
            "Хиты персонажа: правка сразу уходит на токен")
        self.hp_spin.valueChanged.connect(self._on_hp_edited)
        self.hp_spin.setVisible(False)
        self.hp_spin.setFixedWidth(58)
        # Максимум рядом с полем: мастеру тоже нужно видеть «76 / 101».
        self.hp_max_label = QLabel("")
        self.hp_max_label.setObjectName("sheet_cell_title")
        self.hp_max_label.setVisible(False)
        self.ac_value = QLabel("—")
        self.ac_value.setObjectName("sheet_vital")
        self.speed_value = QLabel("—")
        self.speed_value.setObjectName("sheet_vital")
        for title, widget, tip in (
                ("ХИТЫ", self.hp_value, "Текущие и максимальные хиты"),
                ("КД", self.ac_value, "Класс Доспеха"),
                ("СКОРОСТЬ", self.speed_value, "Футов за ход")):
            cell = QFrame()
            cell.setObjectName("sheet_cell")
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(6, 5, 6, 5)
            cl.setSpacing(1)
            t = QLabel(title)
            t.setObjectName("sheet_cell_title")
            t.setAlignment(Qt.AlignCenter)
            cl.addWidget(t)
            widget.setAlignment(Qt.AlignCenter)
            widget.setToolTip(tip)
            if widget is self.hp_value:
                hp_row = QHBoxLayout()
                hp_row.setSpacing(2)
                hp_row.addStretch(1)
                hp_row.addWidget(self.hp_spin)
                hp_row.addWidget(self.hp_max_label)
                hp_row.addStretch(1)
                hp_row.addWidget(widget)
                cl.addLayout(hp_row)
            else:
                cl.addWidget(widget)
            vl.addWidget(cell)
        self.body_layout.addWidget(self.vital_box)

        # --- чувства: тёмное зрение галочкой ---
        # Это поле листа, а не токена: по нему считает и холст (что видно в
        # темноте), и сервер (от кого нельзя спрятаться). Галочка важна тем,
        # что раньше тёмное зрение угадывалось по тексту всего листа и могло
        # достаться персонажу из чужого описания.
        self.senses_title = self._section_title("ЧУВСТВА")
        self.body_layout.addWidget(self.senses_title)
        self.senses_box = QWidget()
        sb = QHBoxLayout(self.senses_box)
        sb.setContentsMargins(0, 0, 0, 0)
        sb.setSpacing(6)
        self.darkvision_check = QCheckBox("Тёмное зрение")
        self.darkvision_check.setObjectName("sheet_darkvision_check")
        self.darkvision_check.setCursor(Qt.PointingHandCursor)
        self.darkvision_check.setToolTip(
            "Ночью токен видит в чёрно-белом на эту дальность, и темнота "
            "его не касается")
        self.darkvision_check.stateChanged.connect(self._on_darkvision_edited)
        self.darkvision_spin = QSpinBox()
        self.darkvision_spin.setObjectName("sheet_darkvision_ft")
        self.darkvision_spin.setRange(0, int(lighting_rules.MAX_DARKVISION_FT))
        self.darkvision_spin.setSingleStep(5)
        self.darkvision_spin.setSuffix(" фт")
        self.darkvision_spin.setAlignment(Qt.AlignCenter)
        self.darkvision_spin.setFixedWidth(80)
        self.darkvision_spin.setToolTip(
            "Дальность тёмного зрения в футах: 60 — обычная для 5e")
        self.darkvision_spin.valueChanged.connect(self._on_darkvision_edited)
        sb.addWidget(self.darkvision_check)
        sb.addWidget(self.darkvision_spin)
        sb.addStretch(1)
        self.body_layout.addWidget(self.senses_box)

        # --- спасброски от смерти ---
        # Место — сразу под хитами, как в листе 5e. Блок сам скрывается,
        # пока хиты выше нуля: у здорового персонажа он только мешает.
        self.death_bar = DeathSavesBar()
        self.death_bar.rollRequested.connect(self.deathSaveRollRequested.emit)
        self.death_bar.setRequested.connect(self.deathSaveSetRequested.emit)
        self.death_bar.reviveRequested.connect(self.deathReviveRequested.emit)
        self.death_bar.stabilizeRequested.connect(
            self.deathStabilizeRequested.emit)
        self.body_layout.addWidget(self.death_bar)

        # --- атаки: таблица оружия из листа, как в карточке актёра ---
        # Строка = оружие листа: имя, бонус, урон и описание правятся здесь
        # же и остаются в weaponsList, поэтому диалог атаки и Q берут ровно
        # то, что видит игрок.
        self._build_attacks()

        # --- характеристики и спасброски ---
        self.abilities_title = self._section_title("ХАРАКТЕРИСТИКИ")
        self.body_layout.addWidget(self.abilities_title)
        self.abilities_grid = QGridLayout()
        self.abilities_grid.setHorizontalSpacing(6)
        self.abilities_grid.setVerticalSpacing(4)
        self.abilities_box = QWidget()
        self.abilities_box.setLayout(self.abilities_grid)
        self.body_layout.addWidget(self.abilities_box)

        # --- особенности класса и расы: кнопки из разбора листа ---
        # Здесь живут действия, у которых нет строки оружия: Второе дыхание,
        # Скрытая атака, черты расы. Кнопка сама знает свою формулу.
        self.features_title = self._section_title("ОСОБЕННОСТИ")
        self.body_layout.addWidget(self.features_title)
        self.features_grid = QGridLayout()
        self.features_grid.setHorizontalSpacing(6)
        self.features_grid.setVerticalSpacing(4)
        self.features_box = QWidget()
        self.features_box.setLayout(self.features_grid)
        self.body_layout.addWidget(self.features_box)

        # --- навыки ---
        # Заголовок и кнопка «Скрыться» стоят в одной строке: проверка
        # Скрытности — навык, но результат виден на карте, поэтому кнопка
        # рядом с навыками, а не в глубине состояний.
        self.skills_head = QWidget()
        sh = QHBoxLayout(self.skills_head)
        sh.setContentsMargins(0, 0, 0, 0)
        sh.setSpacing(6)
        self.skills_title = self._section_title("НАВЫКИ")
        sh.addWidget(self.skills_title)
        sh.addStretch(1)
        self.stealth_btn = QPushButton("Скрыться")
        self.stealth_btn.setObjectName("sheet_stealth_btn")
        self.stealth_btn.setCursor(Qt.PointingHandCursor)
        self.stealth_btn.setToolTip(
            "Проверка Скрытности: d20 + бонус против пассивного Восприятия. "
            "Успех ставит состояние «Скрыт», и следующая атака идёт с "
            "преимуществом")
        self.stealth_btn.clicked.connect(self._roll_stealth)
        sh.addWidget(self.stealth_btn)
        self.body_layout.addWidget(self.skills_head)
        self.skills_grid = QGridLayout()
        self.skills_grid.setHorizontalSpacing(6)
        self.skills_grid.setVerticalSpacing(2)
        self.skills_box = QWidget()
        self.skills_box.setLayout(self.skills_grid)
        self.body_layout.addWidget(self.skills_box)

        # --- инвентарь и заклинания: отдельными вкладками, как в Foundry ---
        self.sheet_tabs = QTabWidget()
        self.sheet_tabs.setObjectName("sheet_tabs")
        self.inventory_tab = InventoryTab()
        self.spells_tab = SpellsTab()
        self.sheet_tabs.addTab(self.inventory_tab, "Инвентарь")
        self.sheet_tabs.addTab(self.spells_tab, "Заклинания")
        self.sheet_tabs.setMinimumHeight(260)

        for tab in (self.inventory_tab, self.spells_tab):
            tab.rollRequested.connect(
                lambda formula, note: self.rollRequested.emit(formula, note))
            # Вкладки меняют не только предметы: «Отдых» чистит состояния и
            # лечит хиты, поэтому после их сигнала токен тоже должен узнать
            # новые состояния, а не только лист.
            tab.changed.connect(self._on_item_tabs_changed)
            tab.useActionRequested.connect(self._on_use_action)
        # Старые точечные сигналы больше не подключены: применение идёт
        # одним UseAction, иначе лечение сработало бы дважды.
        self.body_layout.addWidget(self.sheet_tabs, 1)

        # --- состояния ---
        # В самом низу ленты листа: галочки с иконками, как в карточке
        # актёра. Отметка сразу уходит на токен, поэтому снять состояние
        # так же просто, как поставить: раньше «−» чистил только лист.
        self.conditions_panel = ConditionsPanel()
        # Старое имя оставлено для кода, который ещё ищет «строку состояний».
        self.conditions_bar = self.conditions_panel
        self.conditions_panel.changed.connect(self._on_tabs_changed)
        self.conditions_panel.conditionsToggled.connect(
            self.conditionsChanged.emit)
        self.body_layout.addWidget(self.conditions_panel)

        # --- заметки из листа LSS ---
        # Блок стоит в самом низу ленты: места после переноса файла наверх
        # хватает, и игрок пишет свои заметки там же, где их видит сайт.
        self._build_notes()


    @staticmethod
    def _section_title(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sheet_section")
        return label

    # ------------------------------------------------------------------
    # Атаки: таблица оружия из листа
    # ------------------------------------------------------------------
    #
    # Раньше оружие выбиралось двумя выпадающими списками: «АТАКА» и «ДЛЯ
    # БЫСТРОЙ АТАКИ». Теперь вкладка повторяет лист LSS: строка — карточка
    # с названием, бонусом и уроном, а всё остальное (владение,
    # характеристика, надбавки, заметки) живёт в отдельном окне атаки.
    # Данные по-прежнему лежат в weaponsList и bonuses, поэтому лист
    # открывается на longstoryshort.app без потерь.

    def _build_attacks(self):
        self.attacks_box = QWidget()
        self.attacks_box.setObjectName("sheet_attacks")
        al = QVBoxLayout(self.attacks_box)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(4)

        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        title_row.addWidget(self._section_title("АТАКИ"))
        hint = QLabel("Q — быстрая атака · i — подробности")
        hint.setObjectName("sheet_note_hint")
        title_row.addStretch(1)
        title_row.addWidget(hint)
        al.addLayout(title_row)

        # Шапка колонок — как в LSS: название, бонус, урон и вид. Кнопки
        # «+» и «−» стоят справа: «+» добавляет оружие, «−» показывает
        # удаление у каждой строки.
        header = QHBoxLayout()
        header.setContentsMargins(6, 0, 6, 0)
        header.setSpacing(6)
        header.addSpacing(26)                      # чип Q
        for text, stretch in (("НАЗВАНИЕ", 1), ("БОНУС", 0),
                              ("УРОН / ВИД", 1)):
            label = QLabel(text)
            label.setObjectName("sheet_cell_title")
            if stretch:
                header.addWidget(label, stretch)
            else:
                label.setFixedWidth(48)
                label.setAlignment(Qt.AlignCenter)
                header.addWidget(label)
        header.addSpacing(22)                      # «i»

        self.add_attack_btn = QToolButton()
        self.add_attack_btn.setObjectName("sheet_add_attack")
        self.add_attack_btn.setText("+")
        self.add_attack_btn.setFixedSize(22, 22)
        self.add_attack_btn.setCursor(Qt.PointingHandCursor)
        self.add_attack_btn.setToolTip("Добавить оружие: откроется окно атаки")
        self.add_attack_btn.clicked.connect(self._on_add_attack)
        header.addWidget(self.add_attack_btn)

        self.remove_attack_btn = QToolButton()
        self.remove_attack_btn.setObjectName("sheet_add_attack")
        self.remove_attack_btn.setText("−")
        self.remove_attack_btn.setCheckable(True)
        self.remove_attack_btn.setFixedSize(22, 22)
        self.remove_attack_btn.setCursor(Qt.PointingHandCursor)
        self.remove_attack_btn.setToolTip(
            "Режим удаления: у каждой строки появится «×»")
        self.remove_attack_btn.toggled.connect(self._on_delete_mode)
        header.addWidget(self.remove_attack_btn)
        al.addLayout(header)

        self.attacks_rows_layout = QVBoxLayout()
        self.attacks_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.attacks_rows_layout.setSpacing(4)
        al.addLayout(self.attacks_rows_layout)

        #: строки таблицы: {"weapon", "row", "quick", "name", "bonus", …}
        self.attack_rows = []
        self._attack_delete_mode = False
        #: открытое окно атаки (не модальное); None — окна нет
        self._attack_editor = None
        self.body_layout.addWidget(self.attacks_box)

    def _clear_attack_rows(self):
        while self.attacks_rows_layout.count():
            item = self.attacks_rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.attack_rows = []

    def _rebuild_attacks(self):
        """Собрать строки оружия заново (загрузка, +, удаление)."""
        self._clear_attack_rows()
        rows = lss.weapon_rows(self.sheet) if self.sheet else []
        quick = lss.quick_weapon(self.sheet) if self.sheet else None
        quick_id = quick.get("id") if quick else None
        for row in rows:
            self._add_attack_row(row, row["id"] == quick_id)
        self.attacks_box.setVisible(bool(rows) or self.sheet is not None)
        self._sync_quick_chips(rows)

    def _add_attack_row(self, row: dict, is_quick: bool = False):
        weapon = row["weapon"]
        widget = AttackRow(row, quick=is_quick,
                           delete_mode=self._attack_delete_mode)
        # Строка запоминает ЗАПИСЬ, а не словарь оружия: после ответа
        # сервера лист приходит новым словарём, и ссылка на старый
        # потеряла бы правки из окна атаки.
        entry = {
            "id": row.get("id"),
            "weapon": weapon,
            "row": widget,
            "quick": widget.quick,
            "name": widget.name,
            "bonus": widget.bonus,
            "damage": widget.damage,
            "notes": widget.damage,
        }
        widget.quickToggled.connect(
            lambda _checked=False, e=entry: self._on_quick_chip(e["weapon"]))
        widget.editRequested.connect(
            lambda e=entry: self._open_attack_editor(e["weapon"]))
        widget.attackRequested.connect(
            lambda e=entry: self._emit_weapon_attack(e["weapon"]))
        widget.deleteRequested.connect(
            lambda e=entry: self._on_remove_attack(e["weapon"]))
        widget.bonus.setToolTip(self._bonus_tooltip(weapon))
        widget.name.setToolTip(
            f"{row.get('name') or 'Атака'}: открыть окно атаки")
        self.attacks_rows_layout.addWidget(widget)
        self.attack_rows.append(entry)

    def _on_delete_mode(self, enabled: bool):
        """«−» в шапке: показать или спрятать «×» у строк."""
        self._attack_delete_mode = bool(enabled)
        for entry in self.attack_rows:
            entry["row"].set_delete_mode(self._attack_delete_mode)

    def _open_attack_editor(self, weapon):
        """Окно атаки в духе LSS: правки сразу ложатся в лист."""
        if not self.sheet:
            return
        if self._attack_editor is not None:
            # Открыто окно другой атаки — закрываем его: клик по строке
            # должен открыть ту, по которой кликнули, а не поднять старую.
            self._attack_editor.close()
        dialog = AttackEditorDialog(self.sheet, weapon, self)
        dialog.changed.connect(
            lambda w=weapon: self._on_attack_edited(w))
        dialog.rollRequested.connect(
            lambda formula, note: self.rollRequested.emit(
                formula, note or "Атака"))
        dialog.deleted.connect(
            lambda w=weapon: self._on_remove_attack(w))
        # Окно не модальное: правки видны в строках сразу, и можно
        # сверяться с остальным листом, как на сайте LSS.
        dialog.finished.connect(self._on_attack_editor_finished)
        self._attack_editor = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_attack_editor_finished(self, _result=0):
        """Окно закрыли: строки листа догоняют последние правки."""
        self._attack_editor = None
        self._refresh_attacks()

    def _on_attack_edited(self, weapon):
        """Правка в окне: обновить строку и сохранить лист."""
        rows = lss.weapon_rows(self.sheet) if self.sheet else []
        row = next((r for r in rows if r["weapon"] is weapon), None)
        for entry in self.attack_rows:
            if entry["weapon"] is weapon and row is not None:
                entry["row"].update_data(row)
                entry["bonus"].setToolTip(self._bonus_tooltip(weapon))
                break
        self._sync_quick_chips(rows)
        self._schedule_sheet_change()

    def _bonus_tooltip(self, weapon) -> str:
        """Из чего сложился бонус атаки: база, надбавки листа, ручная правка."""
        base = lss.weapon_base_attack_bonus(self.sheet, weapon)
        total = lss.weapon_attack_bonus(self.sheet, weapon)
        sheet_part = (lss.weapon_attack_bonus_without_manual(
            self.sheet, weapon) - base)
        manual = lss.weapon_manual_attack_bonus(weapon)
        parts = [f"база {base:+d} — характеристика и мастерство"]
        if sheet_part:
            labels = ", ".join(
                str(entry.get("expr") or "")
                for entry in lss.weapon_bonus_entries(
                    self.sheet, weapon, "attack"))
            parts.append(f"надбавки листа {sheet_part:+d}" +
                         (f" ({labels})" if labels else ""))
        if manual:
            parts.append(f"ручная надбавка {manual:+d}")
        return f"Бонус атаки: d20{total:+d}\n" + "\n".join(parts)

    def _on_quick_chip(self, weapon):
        """Q-кнопка строки: этим оружием бьёт быстрая атака."""
        if self._building or not self.sheet:
            return
        if not lss.set_quick_weapon(self.sheet, lss.weapon_id(
                weapon, str(lss._cell_value(weapon.get("name")) or ""))):
            return
        self._sync_quick_chips()
        self._schedule_sheet_change()

    def _sync_quick_chips(self, rows=None):
        """Подсветить одну Q-кнопку — оружие быстрой атаки."""
        if not self.sheet:
            return
        rows = rows if rows is not None else lss.weapon_rows(self.sheet)
        chosen = lss.quick_weapon(self.sheet)
        chosen_id = chosen.get("id") if chosen else None
        for entry, row in zip(self.attack_rows, rows):
            entry["quick"].setChecked(bool(chosen_id)
                                     and row["id"] == chosen_id)

    def _emit_weapon_attack(self, weapon):
        """Кнопка удара: отдать наружу оружие в форме записи атаки."""
        if not self.sheet:
            return
        wid = lss.weapon_id(weapon)
        lss.set_attack_weapon(self.sheet, wid)
        attack = next((a for a in lss.weapon_attacks(self.sheet)
                       if a["id"] == wid), None)
        if attack is None:
            attack = {"id": wid,
                      "name": str(lss._cell_value(weapon.get("name")) or "Оружие"),
                      "attack_bonus": lss.weapon_attack_bonus(self.sheet, weapon),
                      "damage": "", "damage_type": "", "notes": ""}
        self.attackRequested.emit(dict(attack))
        self._schedule_sheet_change()

    def _on_add_attack(self):
        """«+» в шапке: новый слот оружия и сразу окно атаки, как в LSS."""
        if self.sheet is None:
            return
        weapon = lss.add_weapon(self.sheet)
        self._rebuild_attacks()
        self._schedule_sheet_change(immediate=True)
        self._open_attack_editor(weapon)

    def _on_remove_attack(self, weapon):
        """«×» в строке: убрать оружие из weaponsList."""
        if self.sheet is None:
            return
        if not lss.remove_weapon(self.sheet, weapon):
            return
        self._rebuild_attacks()
        self._schedule_sheet_change(immediate=True)

    def _refresh_attacks(self):
        """Показать оружие листа без пересоздания строк.

        Строки сравниваются по id оружия, а не по объектам: эхо сервера
        приносит новые словари с тем же списком, и раньше это выливалось в
        пересборку всей таблицы атак после каждого нажатия на листе.
        """
        if not hasattr(self, "attacks_rows_layout") or self.sheet is None:
            return
        rows = lss.weapon_rows(self.sheet)
        ids = [row["id"] for row in rows]
        if ids != [e.get("id") for e in self.attack_rows]:
            self._rebuild_attacks()
            return
        for entry, row in zip(self.attack_rows, rows):
            entry["id"] = row["id"]
            entry["weapon"] = row["weapon"]
            entry["row"].update_data(row)
            entry["bonus"].setToolTip(self._bonus_tooltip(row["weapon"]))
        self._sync_quick_chips(rows)

    def _on_use_action(self, action: dict):
        """Действие с вкладки: добавить токен страницы и отдать наверх.

        У мастера открыто несколько листов, поэтому без id токена нельзя
        понять, чей это инвентарь. У панели игрока id пустой — окно возьмёт
        его собственного персонажа.
        """
        payload = dict(action or {})
        # Пометка «пришло с листа»: по ней окно включает звук книги и
        # показывает прицел, если цели ещё нет. Перетаскивание на токен
        # этой пометки не ставит — там цель уже известна.
        payload["from_sheet"] = True
        if self.token_id:
            payload["token_id"] = self.token_id
        # Действие «на себя» лечит именно владельца листа, а не того, кого
        # случайно выделили на холсте.
        if payload.get("target_mode") == "self" and self.token_id:
            payload["target_id"] = self.token_id
        self.useActionRequested.emit(payload)

    def _on_inventory_effect(self, kind: str, formula: str, note: str):
        """Старый точечный путь: оставлен для внешних вызовов."""
        if self.token_id:
            self.effectRequestedForToken.emit(
                self.token_id, kind, formula, note)
        else:
            self.effectRequested.emit(kind, formula, note)

    def set_token_id(self, token_id):
        """Привязать лист к токену: спасброски смерти уходят по его id."""
        self.token_id = token_id
        # Инвентарь перетаскивает предметы на карту и должен знать, чей
        # это лист: иначе окно не найдёт источник расхода у мастера.
        for tab in (getattr(self, "inventory_tab", None),
                    getattr(self, "spells_tab", None)):
            if tab is not None and hasattr(tab, "set_token_id"):
                tab.set_token_id(token_id)
        if hasattr(self, "death_bar"):
            self.death_bar.set_token_id(token_id)

    def set_death_state(self, results, hp, can_revive: bool = False):
        """Показать на листе отметки смерти токена (только при 0 хитов)."""
        if not hasattr(self, "death_bar"):
            return
        self.death_bar.set_token_id(self.token_id)
        self.death_bar.set_death_state(results, hp, can_edit=True,
                                       can_revive=can_revive)

    def set_can_edit_hp(self, editable: bool):
        """Разрешить правку хитов в самом листе (мастер)."""
        editable = bool(editable)
        if editable == self._can_edit_hp:
            self._apply_hp_mode()
            return
        self._can_edit_hp = editable
        self._apply_hp_mode()

    def can_edit_hp(self) -> bool:
        return bool(self._can_edit_hp)

    def _apply_hp_mode(self):
        """Показать либо число, либо поле правки — в зависимости от роли."""
        self.hp_value.setVisible(not self._can_edit_hp)
        self.hp_spin.setVisible(bool(self._can_edit_hp))
        self.hp_max_label.setVisible(bool(self._can_edit_hp)
                                     and bool(self.hp_max_label.text()))
        self.hp_value.setToolTip(
            "Текущие и максимальные хиты" if not self._can_edit_hp
            else "Текущие хиты: правятся в поле справа")

    def _on_hp_edited(self, value: int):
        """Мастер изменил хиты в листе: записать и отдать наружу."""
        if self._building or not self.sheet or not self._can_edit_hp:
            return
        vitality = self.sheet.setdefault("vitality", {})
        if not isinstance(vitality, dict):
            return
        cell = vitality.get("hp-current")
        if not isinstance(cell, dict):
            cell = {}
            vitality["hp-current"] = cell
        try:
            current = int(value)
        except (TypeError, ValueError):
            return
        if cell.get("value") == current:
            return
        cell["value"] = current
        self.hp_value.setText(self._hp_text())
        self.sheetChanged.emit(self.sheet)

    def _on_darkvision_edited(self, *_args):
        """Галочка «тёмное зрение»: записать в лист и сразу отдать наружу.

    Пишется в то же поле, что и у LSS (`vitality.darkvision`), плюс явный
    флаг `enabled`: по нему холст и сервер отличают «галочка снята» от
    «в листе просто нет числа».
        """
        if self._building or not self.sheet:
            return
        vitality = self.sheet.setdefault("vitality", {})
        if not isinstance(vitality, dict):
            return
        enabled = bool(self.darkvision_check.isChecked())
        feet = int(self.darkvision_spin.value())
        if enabled and feet <= 0:
            # Включили без числа: 60 фт — обычная дальность, и её видно в поле.
            feet = int(lighting_rules.DEFAULT_DARKVISION_FT)
            self.darkvision_spin.blockSignals(True)
            self.darkvision_spin.setValue(feet)
            self.darkvision_spin.blockSignals(False)
        feet = min(feet, int(lighting_rules.MAX_DARKVISION_FT))
        cell = vitality.get("darkvision")
        if not isinstance(cell, dict):
            cell = {}
            vitality["darkvision"] = cell
        self.darkvision_spin.setEnabled(enabled)
        if cell.get("enabled") == enabled and cell.get("value") == feet:
            return
        cell["enabled"] = enabled
        cell["value"] = feet
        self._schedule_sheet_change(immediate=True)

    # ------------------------------------------------------------------
    # Данные
    # ------------------------------------------------------------------
    def set_sheet(self, sheet: dict):
        """Показать лист и перерисовать все числа."""
        timer = getattr(self, "_save_timer", None)
        if timer is not None:
            # Правки уже лежат в старом словаре листа; новому листу они не
            # нужны, а отложенный сигнал мог бы отправить его повторно.
            timer.stop()
        self.sheet = sheet or lss.new_sheet("Без имени")
        items.ensure(self.sheet)
        # Разбор по правилам: то, что записано в тексте листа, но не в полях
        # (оружие, классовые и расовые особенности), дописывается сюда.
        # Повторный показ листа ничего не дублирует: разбор идемпотентен.
        try:
            summary = sheet_audit.audit_and_apply(self.sheet)
        except Exception:  # чужой/битый лист не должен ломать карточку
            summary = {}
        self.refresh()
        if summary.get("changed"):
            self.sheetChanged.emit(self.sheet)
            self.auditAdded.emit(summary)
        # Состояния уже пришли с листом: отправлять их на токен заново не
        # нужно, это сделал бы каждый показ страницы.
        self._conditions_sent = self.conditions_panel.active_conditions()

    # ------------------------------------------------------------------
    # Заметки: свободный текст из листа LSS
    # ------------------------------------------------------------------

    def _build_notes(self):
        self.notes_box = QWidget()
        self.notes_box.setObjectName("sheet_notes_box")
        nl = QVBoxLayout(self.notes_box)
        nl.setContentsMargins(0, 0, 0, 0)
        nl.setSpacing(3)

        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        title_row.addWidget(self._section_title("ЗАМЕТКИ"))
        hint = QLabel("из листа LSS")
        hint.setObjectName("sheet_note_hint")
        title_row.addStretch(1)
        title_row.addWidget(hint)
        nl.addLayout(title_row)

        self._notes_layout = QVBoxLayout()
        self._notes_layout.setContentsMargins(0, 0, 0, 0)
        self._notes_layout.setSpacing(4)
        nl.addLayout(self._notes_layout)
        #: [{"key": "notes-1", "edit": QPlainTextEdit}]
        self.notes_editors = []
        self.body_layout.addWidget(self.notes_box)

    def _rebuild_notes(self, blocks):
        while self._notes_layout.count():
            item = self._notes_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.notes_editors = []
        many = len(blocks) > 1
        for block in blocks:
            if many:
                label = QLabel(block["title"])
                label.setObjectName("sheet_note_title")
                self._notes_layout.addWidget(label)
            edit = QPlainTextEdit()
            edit.setObjectName("sheet_notes")
            edit.setPlainText(block["text"])
            edit.setPlaceholderText(
                "Цели, долги, планы — то, что нужно помнить за столом…")
            edit.setMinimumHeight(96)
            edit.setTabChangesFocus(True)
            edit.setToolTip("Заметки сохраняются в лист и уходят мастеру")
            edit.textChanged.connect(self._on_notes_typed)
            self._notes_layout.addWidget(edit)
            self.notes_editors.append({"key": block["key"], "edit": edit})

    def _refresh_notes(self):
        """Подтянуть заметки из листа, не сбивая ввод в открытом блоке."""
        if not hasattr(self, "_notes_layout") or self.sheet is None:
            return
        blocks = lss.notes_blocks(self.sheet)
        if [b["key"] for b in blocks] != [e["key"]
                                          for e in self.notes_editors]:
            self._rebuild_notes(blocks)
            return
        for entry, block in zip(self.notes_editors, blocks):
            edit = entry["edit"]
            if edit.hasFocus() or edit.toPlainText() == block["text"]:
                continue
            edit.blockSignals(True)
            edit.setPlainText(block["text"])
            edit.blockSignals(False)

    def _on_notes_typed(self):
        """Заметки правят: сразу в лист, сигнал — после паузы в вводе."""
        if self._building or not self.sheet:
            return
        for entry in self.notes_editors:
            text = entry["edit"].toPlainText().rstrip("\n")
            if lss.text_block(self.sheet, entry["key"]) != text:
                lss.set_text_block(self.sheet, entry["key"], text)
        self._schedule_sheet_change()

    # ------------------------------------------------------------------
    # Сохранение правок листа
    # ------------------------------------------------------------------

    def _schedule_sheet_change(self, immediate: bool = False):
        """Отдать лист наружу: сразу или после паузы в наборе текста."""
        if self._building or self.sheet is None:
            return
        timer = getattr(self, "_save_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._flush_sheet_change)
            self._save_timer = timer
        if immediate:
            timer.stop()
            self.sheetChanged.emit(self.sheet)
            return
        timer.start(350)

    def _flush_sheet_change(self):
        if self.sheet is not None:
            self.sheetChanged.emit(self.sheet)

    def _on_tabs_changed(self):
        """Состояния поменяли галочкой: перерисовать лист и отдать наружу."""
        self.refresh_counts()
        # Панель уже сама отправила условия на токен — не дублируем.
        self._conditions_sent = self.conditions_panel.active_conditions()
        self.sheetChanged.emit(self.sheet)

    def _on_item_tabs_changed(self):
        """Изменился инвентарь/заклинания: лист и, возможно, состояния.

        Состояния в лист приходят не только галочкой: их меняет разбор
        листа и отдых всего стола (кнопка у мастера). Тогда галочки в листе
        сменились, а значки на токене остались бы — поэтому сравниваем
        список и отправляем его на токен.
        """
        self.refresh_counts()
        self._sync_conditions_to_token()
        self.sheetChanged.emit(self.sheet)

    def _sync_conditions_to_token(self):
        """Состояния листа изменились не галочкой — сообщить на токен."""
        if self.sheet is None or not hasattr(self, "conditions_panel"):
            return
        names = self.conditions_panel.active_conditions()
        if names == getattr(self, "_conditions_sent", None):
            return
        self._conditions_sent = list(names)
        self.conditionsChanged.emit(names)

    def refresh_counts(self):
        """Обновить сводки на вкладках, не сбрасывая выбор пользователя."""
        if not self.sheet:
            return
        if hasattr(self, "inventory_tab"):
            self.inventory_tab.set_sheet(self.sheet)
        if hasattr(self, "spells_tab"):
            self.spells_tab.set_sheet(self.sheet)
        if hasattr(self, "conditions_panel"):
            self.conditions_panel.set_sheet(self.sheet)

    def refresh(self):
        if not self.sheet:
            return
        self._building = True
        try:
            s = lss.summary(self.sheet)
            self.name_label.setText(s["name"])
            bits = [b for b in (s["class"], f"{s['level']} ур." if s["level"] else "",
                                s["race"]) if b]
            self.sub_label.setText("  ·  ".join(bits) or "—")

            hp, hp_max = s["hp"], s["hp_max"]
            self.hp_value.setText(self._hp_text())
            self.hp_max_label.setText(f"/{hp_max}" if hp_max is not None else "")
            self.hp_spin.blockSignals(True)
            if hp is not None:
                try:
                    self.hp_spin.setValue(int(hp))
                except (TypeError, ValueError):
                    self.hp_spin.setValue(0)
            self.hp_spin.blockSignals(False)
            self._apply_hp_mode()
            # КД в листе часто записан формулой («[DEX] + 14»). В карточке
            # должно быть число, а сама формула — в подсказке.
            ac = lss.ac_value(self.sheet)
            # Откуда взялось число: доспех, Ловкость и щит по правилам 5e.
            armor = ((self.sheet.get("vtt") or {}).get("armor")
                     if isinstance(self.sheet.get("vtt"), dict) else None)
            if ac is None and isinstance(armor, dict):
                # Формула с неизвестным полем не считается — но доспех
                # известен, и по правилам КД можно собрать.
                ac = armor.get("total")
            self.ac_value.setText(str(ac) if ac is not None else "—")
            raw_ac = (self.sheet.get("vitality") or {}).get("ac")
            raw_ac = (raw_ac or {}).get("value") if isinstance(raw_ac, dict) else raw_ac
            if isinstance(raw_ac, str) and raw_ac.strip() and ac is not None:
                tip = f"Класс Доспеха. В листе записано «{raw_ac}»"
            else:
                tip = "Класс Доспеха"
            if isinstance(armor, dict) and armor.get("breakdown"):
                tip += (f"\nКД по правилам: {armor.get('total')} "
                        f"({armor['breakdown']})")
            self.ac_value.setToolTip(tip)
            self.speed_value.setText(str(s["speed"]) if s["speed"] is not None else "—")
            self._refresh_darkvision()

            self._refresh_attacks()
            self._refresh_notes()
            self._fill_abilities()
            self._fill_features()
            self._fill_skills()
            if hasattr(self, "inventory_tab"):
                self.inventory_tab.set_sheet(self.sheet)
            if hasattr(self, "spells_tab"):
                self.spells_tab.set_sheet(self.sheet)
            if hasattr(self, "conditions_panel"):
                self.conditions_panel.set_sheet(self.sheet)
        finally:
            self._building = False

    def _refresh_darkvision(self):
        """Показать в галочке то, что считает холст и сервер.

        У старых листов флага нет: тогда галочка отмечается, если в листе
        есть дальность (числом или фразой в чувствах), — ровно по тому же
        правилу, по которому считается тёмное зрение сейчас. Первое же
        переключение записывает флаг насовсем.
        """
        if not hasattr(self, "darkvision_check") or not self.sheet:
            return
        feet = lighting_rules.darkvision_from_sheet(self.sheet)
        toggle = lighting_rules.darkvision_toggle(self.sheet)
        enabled = (feet > 0) if toggle is None else bool(toggle)
        self.darkvision_check.blockSignals(True)
        self.darkvision_check.setChecked(bool(enabled))
        self.darkvision_check.blockSignals(False)
        shown = feet if feet > 0 else \
            lighting_rules.darkvision_feet(self.sheet)
        if shown <= 0:
            shown = lighting_rules.DEFAULT_DARKVISION_FT
        self.darkvision_spin.blockSignals(True)
        self.darkvision_spin.setValue(int(shown))
        self.darkvision_spin.blockSignals(False)
        self.darkvision_spin.setEnabled(bool(enabled))

    def _hp_text(self) -> str:
        """Подпись хитов: «текущие/максимум» или прочерк."""
        if not self.sheet:
            return "—"
        s = lss.summary(self.sheet)
        hp, hp_max = s["hp"], s["hp_max"]
        return f"{hp}/{hp_max}" if hp is not None else "—"

    def _clear(self, grid):
        while grid.count():
            item = grid.takeAt(0)
            w = item.widget()
            if w is not None:
                # Сначала отцепляем от контейнера, потом удаляем: 
                # deleteLater выполнится только в цикле событий, а до этого
                # старая кнопка оставалась бы дочерней и находилась в поиске
                # по objectName — с ней же и старым числом зарядов.
                w.setParent(None)
                w.deleteLater()

    def _fill_abilities(self):
        # Отпечаток: характеристика, спасбросок и владение. Пока в листе они
        # те же, сетку не пересобираем — иначе каждое эхо сервера мигало
        # двенадцатью ячейками под рукой у игрока.
        stats = self.sheet.get("stats", {})
        saves = self.sheet.get("saves", {})
        key = tuple(
            ((stats.get(ability) or {}).get("score", 10),
             lss.save_bonus(self.sheet, ability),
             bool((saves.get(ability) or {}).get("isProf")))
            for ability in lss.ABILITIES)
        if key == self._abilities_key and self.abilities_grid.count():
            return
        self._abilities_key = key
        self._clear(self.abilities_grid)
        for col, ability in enumerate(lss.ABILITIES):
            score = (stats.get(ability) or {}).get("score", 10)
            mod = lss.modifier(score)

            cell = QFrame()
            cell.setObjectName("sheet_cell")
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(4, 4, 4, 4)
            cl.setSpacing(2)

            label = QLabel(lss.ABILITY_RU[ability][:3].upper())
            label.setObjectName("sheet_cell_title")
            label.setAlignment(Qt.AlignCenter)
            cl.addWidget(label)

            value = QLabel(str(score))
            value.setObjectName("sheet_ability")
            value.setAlignment(Qt.AlignCenter)
            cl.addWidget(value)

            btn = RollButton(mod, cell)
            btn.setToolTip(f"Проверка {lss.ABILITY_RU[ability]}: d20 {mod:+d}")
            btn.clicked.connect(
                lambda _=False, a=ability, m=mod: self._roll_check(
                    f"1d20{m:+d}", f"Проверка {lss.ABILITY_RU[a]}"))
            wrap = QHBoxLayout()
            wrap.addStretch(1)
            wrap.addWidget(btn)
            wrap.addStretch(1)
            cl.addLayout(wrap)

            self.abilities_grid.addWidget(cell, 0, col)

            save_mod = lss.save_bonus(self.sheet, ability)
            prof = (self.sheet.get("saves", {}).get(ability) or {}).get("isProf")
            sb = RollButton(save_mod, self.abilities_box)
            sb.setObjectName("sheet_save_btn")
            sb.setToolTip(
                f"Спасбросок {lss.ABILITY_RU[ability]}: d20 {save_mod:+d}"
                + (" (владение)" if prof else ""))
            sb.clicked.connect(
                lambda _=False, a=ability, m=save_mod: self._roll_check(
                    f"1d20{m:+d}", f"Спасбросок {lss.ABILITY_RU[a]}"))
            sb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.abilities_grid.addWidget(sb, 1, col)

    @staticmethod
    def _feature_tip(feature: dict, sheet: dict = None) -> str:
        """Подсказка кнопки особенности: правило, расход и формула."""
        parts = []
        text = str(feature.get("text") or "").strip()
        if text:
            parts.append(text)
        key = str(feature.get("key") or "")
        if key == class_features.LAY_ON_HANDS and isinstance(sheet, dict):
            left = class_features.lay_on_hands_left(sheet)
            maximum = class_features.lay_on_hands_max(sheet)
            parts.append(f"Запас: {left} из {maximum} хитов "
                         "(возвращается на долгом отдыхе)")
            parts.append("Действие, касание 5 футов. Снять отравление — "
                         "5 хитов из запаса")
        resource = (class_features.feature_resource(
                        sheet, key, str(feature.get("recharge") or ""))
                    if isinstance(sheet, dict) else None)
        if resource:
            parts.append(f"Заряды: {resource['left']} из {resource['max']}"
                         + (" — кончились, нужен отдых"
                            if resource["left"] <= 0 else ""))
        if key == class_features.SMITE and isinstance(sheet, dict):
            slots = class_features.free_slots(sheet)
            if slots:
                parts.append("Свободные ячейки: " + ", ".join(
                    f"{entry['level']} круг ({entry['dice']})"
                    for entry in slots))
            else:
                parts.append("Нет свободных ячеек заклинаний")
            parts.append("Кнопка открывает окно атаки с выбранной карой: "
                         "ячейка тратится только при попадании")
        formula = str(feature.get("formula") or "")
        if formula:
            parts.append(f"Формула: {formula}")
        recharge = str(feature.get("recharge") or "")
        if resource and not recharge:
            recharge = resource.get("recharge", "")
        if recharge == "short":
            parts.append("Восстанавливается после короткого или долгого отдыха")
        elif recharge == "long":
            parts.append("Восстанавливается после долгого отдыха")
        action = str(feature.get("action") or "")
        if action == "bonus":
            parts.append("Бонусное действие")
        return "\n".join(parts)

    def _feature_actionable(self, feature: dict, resource) -> bool:
        """По особенности можно ударить: бросок, лечение, кара или заряд."""
        kind = str(feature.get("kind") or "info")
        key = str(feature.get("key") or "")
        charged_info = bool(resource) and kind in ("info", "utility")
        return bool(
            (kind in ("heal", "roll") and str(feature.get("formula") or ""))
            or key in (class_features.LAY_ON_HANDS, class_features.SMITE)
            or kind in ("heal_pool", "smite") or charged_info)

    def _feature_label(self, feature: dict, resource) -> str:
        """Подпись кнопки: имя и остаток зарядов, если они есть."""
        name = str(feature.get("name") or "Особенность")
        key = str(feature.get("key") or "")
        if key == class_features.LAY_ON_HANDS and self.sheet:
            # Остаток запаса виден на самой кнопке: за столом его ищут
            # глазами, а не в подсказке.
            return (f"{name} {class_features.lay_on_hands_left(self.sheet)}"
                    f"/{class_features.lay_on_hands_max(self.sheet)}")
        if resource:
            return f"{name} · {resource['left']}/{resource['max']}"
        return name

    def _feature_style(self, widget, feature: dict, resource):
        """Подсказка и доступность: у кнопки — те же, что при создании."""
        kind = str(feature.get("kind") or "info")
        charged_info = bool(resource) and kind in ("info", "utility")
        tip = self._feature_tip(feature, self.sheet)
        if charged_info:
            # У такой особенности нет броска, который можно посчитать:
            # кнопка — счётчик расхода. Говорим это прямо, чтобы нажатие не
            # выглядело «ничем не закончившимся».
            tip += ("\nНажмите, чтобы отметить расход: заряд спишется, "
                    "остаток виден на кнопке")
        enabled = not (resource and resource["left"] <= 0)
        if not enabled:
            when = ("долгого" if resource["recharge"] == "long"
                    else "короткого или долгого")
            name = str(feature.get("name") or "Особенность")
            tip = f"{name}: заряды кончились — нужен отдых до {when}"
        widget.setToolTip(tip)
        if isinstance(widget, QPushButton):
            widget.setEnabled(enabled)

    def _feature_state(self, features) -> tuple:
        """Состав кнопок без зарядов: по нему решается, пересобирать ли сетку."""
        return tuple(
            (str(f.get("key") or ""), str(f.get("name") or ""),
             str(f.get("kind") or ""), str(f.get("formula") or ""),
             str(f.get("text") or ""), str(f.get("recharge") or ""))
            for f in features)

    def _fill_features(self):
        """Кнопки особенностей под характеристиками."""
        if not hasattr(self, "features_grid"):
            return
        block = (self.sheet or {}).get("vtt") if self.sheet else None
        features = (block or {}).get("features") if isinstance(block, dict) else None
        features = [f for f in (features or []) if isinstance(f, dict)]
        has_any = bool(features)
        self.features_title.setVisible(has_any)
        self.features_box.setVisible(has_any)
        if not has_any:
            self._features_key = None
            self._feature_widgets = []
            return
        # Мультикласс: показываем, какие уровни какому классу засчитаны.
        # Тогда видно, почему у следопыта 5 / плута 3 скрытая атака 2к6,
        # а «Второе дыхание» лечит 1к10+3, а не от всего 11 уровня.
        levels = class_features.class_levels(self.sheet or {})
        if len(levels) > 1:
            note = class_features.multiclass_note(self.sheet or {})
            self.features_title.setText("ОСОБЕННОСТИ · " + note.upper())
        else:
            self.features_title.setText("ОСОБЕННОСТИ")
        structure = self._feature_state(features)
        if structure == self._features_key and self._feature_widgets:
            # Тот же набор особенностей: меняются только заряды. Обновляем
            # подписи на месте — кнопки не пересоздаются и не мигают.
            for entry, feature in zip(self._feature_widgets, features):
                entry["feature"] = feature
                resource = (class_features.feature_resource(
                                self.sheet, str(feature.get("key") or ""),
                                str(feature.get("recharge") or ""))
                            if self.sheet else None)
                entry["widget"].setText(self._feature_label(feature, resource))
                self._feature_style(entry["widget"], feature, resource)
            return
        self._features_key = structure
        self._clear(self.features_grid)
        self._feature_widgets = []
        for index, feature in enumerate(features):
            key = str(feature.get("key") or "")
            resource = (class_features.feature_resource(
                            self.sheet, key, str(feature.get("recharge") or ""))
                        if self.sheet else None)
            # Особенность с зарядами нажимаема, даже если её эффект не бросок:
            # так Ярость, Действие всплеск и Ци списываются со счёта, а не
            # живут бесконечно. Без зарядов кнопка серая и объясняет причину.
            entry = {"feature": feature, "widget": None, "resource": resource}
            row, column = index // 2, index % 2
            if self._feature_actionable(feature, resource):
                button = QPushButton(self._feature_label(feature, resource))
                button.setObjectName("sheet_feature_btn")
                button.setCursor(Qt.PointingHandCursor)
                button.clicked.connect(
                    lambda _=False, e=entry: self._use_feature(e["feature"]))
                self._feature_style(button, feature, resource)
                self.features_grid.addWidget(button, row, column)
                entry["widget"] = button
            else:
                # Пассивная особенность — не кнопка, а чип: нажимать нечего.
                chip = QLabel(self._feature_label(feature, resource))
                chip.setObjectName("sheet_feature_chip")
                chip.setWordWrap(False)
                self._feature_style(chip, feature, resource)
                self.features_grid.addWidget(chip, row, column)
                entry["widget"] = chip
            self._feature_widgets.append(entry)

    def _use_feature(self, feature: dict):
        """Нажали особенность: лечение — через общий пайплайн, прочее — бросок."""
        if not isinstance(feature, dict):
            return
        name = str(feature.get("name") or "Особенность")
        formula = str(feature.get("formula") or "")
        kind = str(feature.get("kind") or "")
        key = str(feature.get("key") or "")
        if key == class_features.SMITE or kind == "smite":
            # Кара — не отдельный бросок, а добавка к атаке: открываем то же
            # окно атаки, где уже выбран круг и видно кубики.
            self.attackRequested.emit({
                "name": "", "attack_bonus": None, "damage": "",
                "damage_type": "", "smite": True, "smite_level": 1,
            })
            return
        if key == class_features.LAY_ON_HANDS or kind == "heal_pool":
            action = use_action.from_feature(
                feature, self.sheet, token_id=self.token_id or "")
            self._on_use_action(action.to_dict())
            return
        if key == class_features.RAGE and self.sheet:
            # Ярость — не просто счётчик: это эффект на минуту (+урон и
            # сопротивление физическому). Заряд списывает сервер, потому
            # что бонус к урону считает тоже он: клиентское число
            # подменить нельзя.
            effect = class_features.rage_effect(self.sheet)
            if not effect:
                return
            self._on_use_action({
                "source": "feature", "kind": "utility",
                "name": "Ярость",
                "target_mode": "self",
                "effects": [effect],
                "consumption": {"type": "none"},
                "feature_key": key,
                "note": (f"Ярость: +{effect['bonus']} к урону рукопашной "
                         "атаки Силой, сопротивление физическому урону, "
                         "1 минута"),
            })
            return
        # Действие всплеск, Ци, Вдохновение барда: расход виден на кнопке,
        # поэтому списываем заряд здесь же и сохраняем лист.
        resource = (class_features.feature_resource(
                        self.sheet, key, str(feature.get("recharge") or ""))
                    if self.sheet else None)
        if resource:
            if not class_features.feature_spend(
                    self.sheet, key, resource.get("recharge") or ""):
                return
            self._fill_features()
            # Расход — дискретное действие: отдаём его сразу, не дожидаясь
            # отложенного сохранения. Иначе старое эхо сервера успевало
            # вернуть заряд, и особенность проходила дважды.
            self.resourceSpent.emit(str(self.token_id or ""), self.sheet)
            self.sheetChanged.emit(self.sheet)
            if not formula:
                return
        if not formula:
            return
        if kind == "heal":
            self._on_use_action({
                "source": "feature",
                "kind": "heal",
                "name": name,
                "formula": formula,
                "target_mode": "self",
                "target_id": self.token_id or "",
                "consumption": {"type": "none"},
                "note": name,
            })
            return
        self.rollRequested.emit(formula, name)

    def _fill_skills(self):
        skills = self.sheet.get("skills") or {}
        key = tuple(
            (bool((skills.get(name) or {}).get("isProf")),
             lss.skill_bonus(self.sheet, name))
            for name in lss.SKILLS)
        # Бонус виден на кнопке: за столом важно понимать, с каким числом
        # игрок собирается прятаться, не открывая подсказку.
        if hasattr(self, "stealth_btn"):
            bonus = lss.skill_bonus(self.sheet, "stealth")
            self.stealth_btn.setText(
                "Скрыться" if not bonus else f"Скрыться {bonus:+d}")
        if key == self._skills_key and self.skills_grid.count():
            return
        self._skills_key = key
        self._clear(self.skills_grid)
        row = 0
        for key, (_base, label) in lss.SKILLS.items():
            entry = skills.get(key) or {}
            bonus = lss.skill_bonus(self.sheet, key)

            name = QLabel(("● " if entry.get("isProf") else "○ ") + label)
            name.setToolTip("Владение навыком" if entry.get("isProf")
                            else "Без владения")
            if entry.get("isProf"):
                name.setObjectName("sheet_skill_prof")
            self.skills_grid.addWidget(name, row, 0)

            btn = RollButton(bonus, self.skills_box)
            btn.clicked.connect(
                lambda _=False, k=key, m=bonus, lbl=label: self._roll_check(
                    f"1d20{m:+d}", f"{lbl}"))
            self.skills_grid.addWidget(btn, row, 1)
            row += 1

    def _roll_check(self, formula: str, note: str):
        self.rollRequested.emit(formula, note)

    def _roll_stealth(self):
        """Проверка Скрытности: сервер сам бросит и сравнит с Восприятием."""
        self.stealthCheckRequested.emit(str(self.token_id or ""))

    # ------------------------------------------------------------------
    # Файлы
    # ------------------------------------------------------------------
    def load_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Загрузить лист персонажа", "",
            "Лист Long Story Short (*.json);;Все файлы (*)")
        if path:
            self.load_file(path)

    def load_file(self, path: str) -> bool:
        """Прочитать лист из файла. Возвращает True при успехе."""
        try:
            with open(path, encoding="utf-8") as f:
                sheet = lss.parse(f.read())
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Лист не загружен",
                                f"{os.path.basename(path)}\n{exc}")
            return False
        self.set_sheet(sheet)
        self.sheetChanged.emit(sheet)
        return True

    def save_dialog(self):
        if not self.sheet:
            QMessageBox.information(self, "Нет листа",
                                    "Сначала загрузите лист персонажа")
            return
        name = lss.summary(self.sheet)["name"] or "character"
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить лист", f"{name}.json",
            "Лист Long Story Short (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(lss.dump(self.sheet))
        except OSError as exc:
            QMessageBox.warning(self, "Не сохранено", str(exc))
            return
        # Лист может жить и без QMainWindow (тесты, отдельное окно):
        # статус-бара тогда нет, но файл уже сохранён — молчать не страшно.
        window = self.window()
        bar = window.statusBar() if hasattr(window, "statusBar") else None
        if bar is not None:
            bar.showMessage(
                f"Лист сохранён: {os.path.basename(path)}", 3000)

    # ------------------------------------------------------------------
    # Правки с листа
    # ------------------------------------------------------------------