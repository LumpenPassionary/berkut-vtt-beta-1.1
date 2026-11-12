"""Окно атаки в духе интерактивного листа Long Story Short.

На сайте LSS атака правится не в таблице, а в отдельном окне: название,
владение, характеристика, надбавки к попаданию и урону, формула, тип урона,
заметки. Здесь то же окно для нашего листа — с теми же данными
(``weaponsList`` и ``bonuses``), поэтому правки переживают сохранение и
открываются на сайте без потерь.

Почему отдельный модуль: ``character_sheet.py`` отвечает за ленту листа, а
это самостоятельный документ со своими строками-бонусами и пересчётом
чисел. Общие правила (бонус атаки, формула урона, диапазон) остаются в
``shared/lss_format.py`` — окно только показывает и правит их.
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QToolButton,
    QVBoxLayout, QWidget,
)

from shared import combat as combat_rules
from shared import lss_format as lss


class AttackRow(QFrame):
    """Строка атаки в листе: название, бонус, урон с видом и кнопки.

    Как на LSS: вся строка — одна карточка. Клик по названию открывает
    окно атаки, клик по бонусу — бросок по цели, «i» — то же окно атаки.
    """

    editRequested = pyqtSignal()
    attackRequested = pyqtSignal()
    deleteRequested = pyqtSignal()
    quickToggled = pyqtSignal(bool)

    def __init__(self, row: dict, quick: bool = False, delete_mode: bool = False,
                 parent=None):
        super().__init__(parent)
        self.setObjectName("atk_row_card")
        self.row_data = row
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(6)

        self.quick = QToolButton()
        self.quick.setObjectName("sheet_quick_chip")
        self.quick.setText("Q")
        self.quick.setCheckable(True)
        self.quick.setChecked(bool(quick))
        self.quick.setFixedSize(20, 22)
        self.quick.setCursor(Qt.PointingHandCursor)
        self.quick.setToolTip("Быстрая атака этим оружием: Q → клик по цели")
        self.quick.clicked.connect(
            lambda checked=False: self.quickToggled.emit(bool(checked)))
        layout.addWidget(self.quick)

        self.name = QToolButton()
        self.name.setObjectName("atk_row_name")
        self.name.setCursor(Qt.PointingHandCursor)
        self.name.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.name.setToolTip("Открыть окно атаки: бонусы, урон, заметки")
        self.name.clicked.connect(lambda: self.editRequested.emit())
        layout.addWidget(self.name, 3)

        self.bonus = QToolButton()
        self.bonus.setObjectName("atk_row_bonus")
        self.bonus.setCursor(Qt.PointingHandCursor)
        self.bonus.setFixedWidth(48)
        self.bonus.setToolTip("Бросить атаку: d20 + бонус")
        self.bonus.clicked.connect(lambda: self.attackRequested.emit())
        layout.addWidget(self.bonus)

        self.damage = QLabel("")
        self.damage.setObjectName("atk_row_damage")
        self.damage.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.damage.setToolTip("Формула урона и вид урона")
        layout.addWidget(self.damage, 4)

        self.info = QToolButton()
        self.info.setObjectName("atk_row_info")
        self.info.setText("i")
        self.info.setFixedSize(22, 22)
        self.info.setCursor(Qt.PointingHandCursor)
        self.info.setToolTip("Подробности атаки")
        self.info.clicked.connect(lambda: self.editRequested.emit())
        layout.addWidget(self.info)

        self.remove = QToolButton()
        self.remove.setObjectName("sheet_attack_remove")
        self.remove.setText("×")
        self.remove.setFixedSize(22, 22)
        self.remove.setCursor(Qt.PointingHandCursor)
        self.remove.setToolTip("Убрать оружие из листа")
        self.remove.setVisible(bool(delete_mode))
        self.remove.clicked.connect(lambda: self.deleteRequested.emit())
        layout.addWidget(self.remove)

        self.update_data(row)

    def set_delete_mode(self, enabled: bool):
        self.remove.setVisible(bool(enabled))

    def update_data(self, row: dict):
        """Обновить подписи, не пересоздавая строку (фокус не теряется)."""
        self.row_data = row or {}
        self._full_name = str(self.row_data.get("name") or "Оружие")
        self._full_damage = self._damage_text()
        self.bonus.setText(f"{int(self.row_data.get('bonus') or 0):+d}")
        self.damage.setToolTip(self._full_damage)
        self._fit_text()

    def _damage_text(self) -> str:
        damage = str(self.row_data.get("total_damage")
                     or self.row_data.get("damage") or "").strip()
        dtype = str(self.row_data.get("damage_type") or "").strip()
        if damage and dtype:
            return f"{damage} · {dtype}"
        return damage or dtype or "—"

    def _fit_text(self):
        """Обрезать длинное название и урон по ширине строки."""
        width = max(40, self.name.width() - 10)
        self.name.setText(self.name.fontMetrics().elidedText(
            self._full_name, Qt.ElideRight, width))
        # У чипа урона есть отступы и рамка — иначе текст липнет к краю.
        width = max(40, self.damage.width() - 20)
        self.damage.setText(self.damage.fontMetrics().elidedText(
            self._full_damage, Qt.ElideRight, width))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_full_name"):
            self._fit_text()


class BonusRow(QFrame):
    """Строка надбавки: подпись, выражение, готовое значение, бросок и ×."""

    changed = pyqtSignal()
    removed = pyqtSignal(str)
    rollRequested = pyqtSignal(str, str)

    def __init__(self, sheet: dict, item: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("atk_row")
        self.sheet = sheet
        self.entry = item.get("entry") or {}
        self.bonus_id = str(item.get("id") or "")
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 4, 4, 4)
        row.setSpacing(5)

        self.label_edit = QLineEdit(str(item.get("label") or ""))
        self.label_edit.setObjectName("atk_row_label")
        self.label_edit.setPlaceholderText("Название надбавки")
        self.label_edit.setToolTip(
            "Например «Инфузия» или «Стиль боя: стрельба»")
        row.addWidget(self.label_edit, 1)

        self.expr_edit = QLineEdit(str(item.get("expr") or ""))
        self.expr_edit.setObjectName("atk_row_expr")
        self.expr_edit.setPlaceholderText("1 или 1d4")
        self.expr_edit.setFixedWidth(86)
        self.expr_edit.setToolTip(
            "Число, кубики или формула: 1, 1d4, [DEX]+2")
        row.addWidget(self.expr_edit)

        self.value_chip = QLabel(str(item.get("value_text") or "—"))
        self.value_chip.setObjectName("atk_chip")
        self.value_chip.setAlignment(Qt.AlignCenter)
        self.value_chip.setMinimumWidth(72)
        row.addWidget(self.value_chip)

        self.roll_btn = QToolButton()
        self.roll_btn.setObjectName("atk_roll")
        self.roll_btn.setText("d")
        self.roll_btn.setFixedSize(22, 22)
        self.roll_btn.setCursor(Qt.PointingHandCursor)
        self.roll_btn.setToolTip("Бросить надбавку в чат")
        row.addWidget(self.roll_btn)

        self.remove_btn = QToolButton()
        self.remove_btn.setObjectName("atk_row_remove")
        self.remove_btn.setText("×")
        self.remove_btn.setFixedSize(22, 22)
        self.remove_btn.setCursor(Qt.PointingHandCursor)
        self.remove_btn.setToolTip("Убрать надбавку")
        row.addWidget(self.remove_btn)

        self.label_edit.textEdited.connect(self._on_label)
        self.expr_edit.textEdited.connect(self._on_expr)
        self.roll_btn.clicked.connect(self._on_roll)
        self.remove_btn.clicked.connect(
            lambda _checked=False: self.removed.emit(self.bonus_id))
        self.refresh()

    def _on_label(self, text: str):
        self.entry["label"] = str(text or "")
        self.changed.emit()

    def _on_expr(self, text: str):
        self.entry["expr"] = str(text or "")
        self.refresh()
        self.changed.emit()

    def _on_roll(self, _checked=False):
        expr = str(self.expr_edit.text() or "").strip()
        if not expr:
            return
        label = str(self.label_edit.text() or "").strip()
        self.rollRequested.emit(expr, label)

    def refresh(self):
        """Пересчитать готовое значение и доступность броска."""
        expr = str(self.expr_edit.text() or "").strip()
        value = lss.expression_value_text(self.sheet, expr)
        self.value_chip.setText(value or "—")
        formula = lss.formula_from_text(expr, self.sheet,
                                        allow_constant=True) or ""
        self.roll_btn.setEnabled(lss.dice_span(formula) is not None)


class AttackEditorDialog(QDialog):
    """Окно одной атаки: как на LSS, с живым сохранением в лист.

    Изменения сразу пишутся в словарь листа и сигналом ``changed`` уходят
    наверх: отдельной кнопки «Сохранить» нет, как и на сайте. Закрытие
    окна ничего не откатывает — надбавка уже в листе.
    """

    changed = pyqtSignal()
    rollRequested = pyqtSignal(str, str)
    deleted = pyqtSignal()

    def __init__(self, sheet: dict, weapon: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("atk_dialog")
        self.setWindowTitle("Атака")
        self.setMinimumWidth(500)
        self.resize(540, 720)
        self.sheet = sheet if isinstance(sheet, dict) else {}
        self.weapon = weapon if isinstance(weapon, dict) else {}
        self._building = True
        self._build()
        self._building = False
        self.refresh()

    # ------------------------------------------------------------------
    # Построение
    # ------------------------------------------------------------------
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(9)

        outer.addWidget(self._build_header())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("atk_body")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)
        layout.addWidget(self._build_attack_section())
        layout.addWidget(self._build_damage_section())
        layout.addWidget(self._build_notes_section())
        layout.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        footer = QHBoxLayout()
        footer.setSpacing(6)
        self.delete_btn = QPushButton("Удалить")
        self.delete_btn.setObjectName("atk_delete")
        self.delete_btn.setCursor(Qt.PointingHandCursor)
        self.delete_btn.setToolTip("Убрать это оружие из листа")
        self.delete_btn.clicked.connect(self._on_delete)
        footer.addWidget(self.delete_btn)
        footer.addStretch(1)
        self.done_btn = QPushButton("Готово")
        self.done_btn.setObjectName("atk_done")
        self.done_btn.setDefault(True)
        self.done_btn.clicked.connect(self.accept)
        footer.addWidget(self.done_btn)
        outer.addLayout(footer)

    def _build_header(self) -> QWidget:
        head = QFrame()
        head.setObjectName("atk_header")
        row = QHBoxLayout(head)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        column = QVBoxLayout()
        column.setSpacing(1)
        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("atk_name")
        self.name_edit.setPlaceholderText("Название атаки")
        font = QFont()
        font.setPointSize(13)
        font.setBold(True)
        self.name_edit.setFont(font)
        self.name_edit.setText(str(lss._cell_value(
            self.weapon.get("name")) or "").strip())
        self.name_edit.textEdited.connect(self._on_name)
        column.addWidget(self.name_edit)

        self.summary = QLabel("")
        self.summary.setObjectName("atk_summary")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        column.addWidget(self.summary)

        self.span_label = QLabel("")
        self.span_label.setObjectName("atk_span")
        column.addWidget(self.span_label)
        row.addLayout(column, 1)

        badge_box = QVBoxLayout()
        badge_box.setSpacing(0)
        self.badge = QLabel("0")
        self.badge.setObjectName("atk_badge")
        self.badge.setAlignment(Qt.AlignCenter)
        self.badge.setMinimumSize(58, 58)
        self.badge.setToolTip("Итоговый бонус атаки: d20 + это число")
        badge_box.addWidget(self.badge)
        caption = QLabel("АТАКА")
        caption.setObjectName("atk_badge_caption")
        caption.setAlignment(Qt.AlignCenter)
        badge_box.addWidget(caption)
        row.addLayout(badge_box)
        return head

    @staticmethod
    def _section(title: str, add_tooltip: str = ""):
        """Рамка раздела с заголовком и кнопкой «+ Бонус»."""
        frame = QFrame()
        frame.setObjectName("atk_section")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(9, 8, 9, 9)
        layout.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(6)
        label = QLabel(title)
        label.setObjectName("atk_section_title")
        head.addWidget(label)
        head.addStretch(1)
        add = QToolButton()
        add.setObjectName("atk_add")
        add.setText("+ Бонус")
        add.setCursor(Qt.PointingHandCursor)
        add.setToolTip(add_tooltip or "Добавить надбавку")
        head.addWidget(add)
        layout.addLayout(head)
        return frame, layout, add

    def _build_attack_section(self) -> QWidget:
        frame, layout, self.add_attack_bonus = self._section(
            "АТАКА", "Надбавка к попаданию: инфузия, стиль боя, черта")
        self.add_attack_bonus.clicked.connect(
            lambda: self._on_add_bonus("attack"))

        prof_caption = QLabel("ВЛАДЕНИЕ")
        prof_caption.setObjectName("atk_field_caption")
        layout.addWidget(prof_caption)

        toggle_row = QHBoxLayout()
        toggle_row.setSpacing(4)
        self.prof_none = QToolButton()
        self.prof_none.setText("Нет")
        self.prof_prof = QToolButton()
        pb = lss.proficiency_bonus(self.sheet)
        self.prof_prof.setText(f"Владение {pb:+d}")
        self.prof_group = []
        for button in (self.prof_none, self.prof_prof):
            button.setObjectName("atk_toggle")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            toggle_row.addWidget(button)
            self.prof_group.append(button)
        self.prof_none.clicked.connect(
            lambda: self._on_proficiency(False))
        self.prof_prof.clicked.connect(
            lambda: self._on_proficiency(True))
        layout.addLayout(toggle_row)

        ability_label = QLabel("ХАРАКТЕРИСТИКА")
        ability_label.setObjectName("atk_field_caption")
        layout.addWidget(ability_label)
        self.ability_combo = QComboBox()
        self.ability_combo.setObjectName("atk_combo")
        for key in lss.ABILITIES:
            self.ability_combo.addItem(lss.ABILITY_RU.get(key, key), key)
        current = self.ability_combo.findData(lss.weapon_ability(self.weapon))
        self.ability_combo.setCurrentIndex(max(0, current))
        self.ability_combo.currentIndexChanged.connect(self._on_ability)
        layout.addWidget(self.ability_combo)

        self.attack_bonus_box = QVBoxLayout()
        self.attack_bonus_box.setSpacing(4)
        layout.addLayout(self.attack_bonus_box)
        self._fill_bonus_rows("attack", self.attack_bonus_box)
        return frame

    def _build_damage_section(self) -> QWidget:
        frame, layout, self.add_damage_bonus = self._section(
            "УРОН", "Надбавка к урону: кубик, число или замена формулы")
        self.add_damage_bonus.clicked.connect(
            lambda: self._on_add_bonus("damage"))

        caption = QLabel("УРОН")
        caption.setObjectName("atk_field_caption")
        layout.addWidget(caption)
        formula_row = QHBoxLayout()
        formula_row.setSpacing(5)
        self.damage_edit = QLineEdit()
        self.damage_edit.setObjectName("atk_formula")
        self.damage_edit.setPlaceholderText("1d10+[DEX]")
        self.damage_edit.setText(str(lss._cell_value(
            self.weapon.get("dmg")) or "").strip())
        self.damage_edit.setToolTip(
            "Формула как в LSS: [STR], [DEX], кубики. Модификатор "
            "характеристики подставится сам")
        self.damage_edit.textEdited.connect(self._on_damage)
        formula_row.addWidget(self.damage_edit, 1)
        self.damage_chip = QLabel("")
        self.damage_chip.setObjectName("atk_chip")
        self.damage_chip.setAlignment(Qt.AlignCenter)
        self.damage_chip.setMinimumWidth(90)
        formula_row.addWidget(self.damage_chip)
        layout.addLayout(formula_row)

        type_caption = QLabel("ВИД УРОНА")
        type_caption.setObjectName("atk_field_caption")
        layout.addWidget(type_caption)

        type_row = QHBoxLayout()
        type_row.setSpacing(5)
        self.type_combo = QComboBox()
        self.type_combo.setObjectName("atk_combo")
        self.type_combo.setEditable(True)
        self.type_combo.setInsertPolicy(QComboBox.NoInsert)
        labels = []
        for label in combat_rules.DAMAGE_TYPES.values():
            if label not in labels:
                labels.append(label)
        for label in labels:
            self.type_combo.addItem(label)
        raw_type = lss.weapon_damage_type(
            self.weapon, lss._weapon_damage_text(self.weapon, self.sheet))
        self.type_combo.setCurrentText(str(raw_type or ""))
        if self.type_combo.lineEdit() is not None:
            self.type_combo.lineEdit().setPlaceholderText(
                "Вид урона: колющий и светом")
        self.type_combo.currentTextChanged.connect(self._on_type)
        type_row.addWidget(self.type_combo, 1)
        layout.addLayout(type_row)

        self.damage_bonus_box = QVBoxLayout()
        self.damage_bonus_box.setSpacing(4)
        layout.addLayout(self.damage_bonus_box)
        self._fill_bonus_rows("damage", self.damage_bonus_box)
        return frame

    def _build_notes_section(self) -> QWidget:
        frame, layout, add = self._section("ЗАМЕТКИ")
        add.setVisible(False)
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setObjectName("atk_notes")
        self.notes_edit.setPlaceholderText(
            "Как это оружие работает в бою, что складывать, когда применять")
        self.notes_edit.setMinimumHeight(96)
        self.notes_edit.setPlainText(lss.weapon_notes(self.weapon))
        self.notes_edit.textChanged.connect(self._on_notes)
        layout.addWidget(self.notes_edit)

        self.notes_visible = QCheckBox("Отобразить заметки")
        self.notes_visible.setObjectName("atk_notes_toggle")
        self.notes_visible.setChecked(lss.weapon_notes_visibility(self.weapon))
        self.notes_visible.toggled.connect(self._on_notes_visibility)
        layout.addWidget(self.notes_visible)
        return frame

    # ------------------------------------------------------------------
    # Надбавки
    # ------------------------------------------------------------------
    def _fill_bonus_rows(self, kind: str, layout: QVBoxLayout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        items = lss.weapon_bonus_items(self.sheet, self.weapon, kind)
        for item in items:
            row = BonusRow(self.sheet, item, self)
            row.changed.connect(self._on_bonus_changed)
            row.removed.connect(self._on_bonus_removed)
            row.rollRequested.connect(self.rollRequested.emit)
            layout.addWidget(row)
        if not items:
            hint = QLabel("Надбавок нет — добавьте «+ Бонус»")
            hint.setObjectName("atk_hint")
            layout.addWidget(hint)

    def _on_add_bonus(self, kind: str):
        label = "Бонус" if kind == "attack" else "Бонус к урону"
        lss.add_weapon_bonus(self.sheet, self.weapon, kind,
                             label=label, expr="1")
        box = (self.attack_bonus_box if kind == "attack"
               else self.damage_bonus_box)
        self._fill_bonus_rows(kind, box)
        self._touch()

    def _on_bonus_removed(self, bonus_id: str):
        if lss.remove_weapon_bonus(self.sheet, bonus_id):
            self._fill_bonus_rows("attack", self.attack_bonus_box)
            self._fill_bonus_rows("damage", self.damage_bonus_box)
            self._touch()

    def _on_bonus_changed(self):
        self.refresh()
        self._touch()

    # ------------------------------------------------------------------
    # Поля оружия
    # ------------------------------------------------------------------
    def _on_name(self, text: str):
        old_name = str(lss._cell_value(self.weapon.get("name")) or "").strip()
        lss.set_weapon_name(self.weapon, text)
        if not self.weapon.get("id"):
            # У оружия без id выбор Q хранился по имени — переписываем.
            self._rename_choice(old_name, str(text or "").strip())
        self._touch()

    def _rename_choice(self, old: str, new: str):
        block = self.sheet.get("vtt")
        if not isinstance(block, dict) or not old or not new:
            return
        for key in ("quick_weapon", "attack_weapon", "chosen_weapon"):
            if str(block.get(key) or "").strip() == old:
                block[key] = new

    def _on_proficiency(self, owned: bool):
        lss.set_weapon_proficiency(self.weapon, owned)
        self._sync_prof_buttons()
        self._touch()

    def _sync_prof_buttons(self):
        owned = lss.weapon_proficiency_rank(self.weapon) > 0
        self.prof_none.setChecked(not owned)
        self.prof_prof.setChecked(owned)

    def _on_ability(self, *_):
        key = self.ability_combo.currentData()
        if lss.set_weapon_ability(self.weapon, key):
            self._touch()

    def _on_damage(self, text: str):
        lss.set_weapon_damage(self.weapon, text)
        self._touch()

    def _on_type(self, text: str):
        if self._building:
            return
        lss.set_weapon_damage_type(self.weapon, text)
        self._touch()

    def _on_notes(self):
        if self._building:
            return
        lss.set_weapon_notes(self.weapon, self.notes_edit.toPlainText())
        self._touch()

    def _on_notes_visibility(self, checked: bool):
        if self._building:
            return
        lss.set_weapon_notes_visibility(self.weapon, checked)
        self._touch()

    def _on_delete(self):
        self.deleted.emit()
        self.accept()

    def _touch(self):
        if self._building:
            return
        self.refresh()
        self.changed.emit()

    # ------------------------------------------------------------------
    # Пересчёт
    # ------------------------------------------------------------------
    def refresh(self):
        """Обновить бонус, сводку урона и чипы — без пересборки полей."""
        bonus = lss.weapon_attack_bonus(self.sheet, self.weapon)
        self.badge.setText(f"{bonus:+d}" if bonus else "0")
        damage = lss.weapon_damage_formula(self.sheet, self.weapon)
        raw_type = lss.weapon_damage_type(
            self.weapon, lss._weapon_damage_text(self.weapon, self.sheet))
        kind = combat_rules.damage_type_label(raw_type) if raw_type else ""
        self.summary.setText(
            " ".join(part for part in (damage, kind) if part) or "Без урона")
        span = lss.weapon_damage_span(self.sheet, self.weapon)
        self.span_label.setText(
            f"Урон: {span[0]}–{span[1]} (~{span[2]})" if span else "")
        base = str(lss._cell_value(self.weapon.get("dmg")) or "").strip()
        self.damage_chip.setText(
            lss.expression_value_text(self.sheet, base) or "—")
        self._sync_prof_buttons()
        for box in (self.attack_bonus_box, self.damage_bonus_box):
            for index in range(box.count()):
                widget = box.itemAt(index).widget()
                if isinstance(widget, BonusRow):
                    widget.refresh()
