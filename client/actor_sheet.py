"""Редактируемая карточка актёра (монстра/НПС) в стиле Foundry.

Одна карточка заменяет прежние «Карточку актёра» и «Карточку существа»:
характеристики, спасброски, атаки, защита, состояния и спасброски от смерти
живут здесь. Отсюда же можно бросить атаку или спасбросок — отдельная
карточка существа больше не нужна.

Диалог ничего не отправляет сам: он копит правки и отдаёт их наружу через
``payload()``/``saveRequested``, а решение слать ли UPDATE_TOKEN принимает
главное окно. Внутри карточки Ctrl+Z отменяет последнюю правку черновика,
даже пока окно открыто и модальный диалог перехватывает горячие клавиши
главного окна.
"""
import os

from PyQt5.QtCore import QEvent, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (QColor, QFont, QIcon, QKeySequence, QPainter,
                         QPainterPath, QPen, QPixmap)
from PyQt5.QtWidgets import (QAbstractSpinBox, QCheckBox, QComboBox, QDialog,
                             QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout,
                             QKeySequenceEdit, QLabel, QLineEdit, QProgressBar,
                             QPushButton, QScrollArea, QSpinBox, QStyle,
                             QTextEdit, QToolButton, QToolTip, QVBoxLayout,
                             QWidget)

from shared import combat as combat_rules
from shared import conditions as conditions_rules
from shared import lss_format
from shared import sheet_items
from shared import token_stats
from shared import special_attacks as special_attack_rules
from shared import vision as vision_rules
from client.death_saves import DeathSavesBar
from client.hotkeys import event_key


ACTOR_SHEET_QSS = """
#actor_sheet {
    background: #101116;
    color: #e8e6e0;
}
#actor_header {
    background: #171922;
    border-bottom: 1px solid #2c3040;
}
#actor_name {
    background: transparent;
    border: none;
    border-bottom: 1px solid transparent;
    padding: 2px 0 4px 0;
    color: #f3efe6;
    font-size: 19pt;
    font-weight: 600;
}
#actor_name:hover { border-bottom-color: #3d4252; }
#actor_name:focus { border-bottom-color: #c8a45e; }
#actor_sub { color: #9aa0ae; font-size: 9pt; }
#actor_chip {
    background: #242838;
    border: 1px solid #3a4054;
    border-radius: 10px;
    padding: 4px 9px;
    color: #b9c0d0;
    font-size: 9pt;
}
#actor_chip[dead="true"] {
    background: #3a2022;
    border-color: #7d3f42;
    color: #ffb4ae;
}
#actor_body { background: #101116; }
#sheet_section {
    color: #c8a45e;
    font-size: 9pt;
    font-weight: 700;
    letter-spacing: 1.4px;
    padding: 2px 0 0 0;
}
#section_hint { color: #8e94a3; font-size: 8.5pt; }
#vital_card, #ability_card, #save_card, #attack_row {
    background: #191c26;
    border: 1px solid #2a2f3d;
    border-radius: 8px;
}
#vital_card:hover, #ability_card:hover, #save_card:hover,
#attack_row:hover { border-color: #3b4152; }
#vital_title, #ability_title {
    color: #8f96a6;
    font-size: 7.8pt;
    font-weight: 700;
    letter-spacing: 1px;
}
#vital_value, #ability_mod {
    color: #f3efe6;
    font-size: 15pt;
    font-weight: 700;
}
#ability_mod { color: #c8a45e; font-size: 11pt; }
#attack_header {
    color: #7f8696;
    font-size: 7.5pt;
    font-weight: 700;
    letter-spacing: 0.8px;
}
/* Поля карточки актёра: текст без коробок. Линия подчёркивает поле,
   а полная рамка появляется только в фокусе. */
QLineEdit, QSpinBox, QDoubleSpinBox {
    background: transparent;
    border: none;
    border-bottom: 1px solid #2e3342;
    border-radius: 0;
    padding: 5px 2px;
    color: #ebe8e1;
    selection-background-color: #7f6836;
    selection-color: #f8f4ea;
}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover {
    border-bottom-color: #464c5e;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border-bottom-color: #c8a45e;
}
QSpinBox QLineEdit, QDoubleSpinBox QLineEdit, QComboBox QLineEdit {
    background: transparent; border: none; padding: 0;
}
QTextEdit {
    background: #141720;
    border: none;
    border-radius: 8px;
    padding: 9px 11px;
    color: #e8e6e0;
    selection-background-color: #7f6836;
    selection-color: #f8f4ea;
}
QComboBox {
    background: #191c26;
    border: none;
    border-radius: 6px;
    padding: 5px 8px;
    color: #e8e6e0;
    selection-background-color: #7f6836;
    selection-color: #f8f4ea;
}
QComboBox:hover { background: #1e2230; }
QLineEdit#attack_name, QLineEdit#attack_damage, QSpinBox#attack_bonus {
    background: transparent;
    border: none;
    border-bottom: 1px solid transparent;
    padding: 3px 4px;
}
QLineEdit#attack_name:hover, QLineEdit#attack_damage:hover,
QSpinBox#attack_bonus:hover { border-bottom-color: #333949; }
QLineEdit#attack_name:focus, QLineEdit#attack_damage:focus,
QSpinBox#attack_bonus:focus { border-bottom-color: #c8a45e; }
QComboBox#attack_type { padding: 3px 5px; }
QCheckBox {
    spacing: 7px;
    color: #d8d6d2;
    padding: 2px 0;
}
QCheckBox::indicator {
    width: 15px;
    height: 15px;
    border-radius: 4px;
    border: 1px solid #3b4152;
    background: #11131a;
}
QCheckBox::indicator:checked {
    background: #c8a45e;
    border-color: #c8a45e;
}
QCheckBox::indicator:hover { border-color: #c8a45e; }
#save_bonus { color: #f3efe6; font-weight: 700; min-width: 28px; }
/* Спасбросок: 0 — пусто, +2 — жёлтое владение, +4 — фиолетовое мастерство. */
#save_check::indicator {
    width: 15px; height: 15px;
    border-radius: 3px;
    border: 1px solid #3a4054;
    background: #11131a;
}
#save_check[rank="1"]::indicator {
    background: #c8a45e;
    border-color: #e6cd8f;
}
#save_check[rank="2"]::indicator {
    background: #9a6ad4;
    border-color: #c3a0f0;
}
#save_roll {
    background: #222736;
    border: 1px solid #3a4256;
    border-radius: 5px;
    color: #cfd6e6;
    font-size: 8pt;
    font-weight: 700;
    padding: 2px 6px;
}
#save_roll:hover { background: #2b3244; border-color: #5b657e; color: #fff; }
#save_roll:pressed { background: #1a1f2b; }
QToolButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 5px;
    padding: 3px;
}
QToolButton:hover { background: #252a38; border-color: #3b4152; }
QToolButton:pressed { background: #1b1f2b; }
QPushButton {
    background: #232838;
    border: 1px solid #363d50;
    border-radius: 7px;
    padding: 7px 14px;
    color: #e8e6e0;
    font-weight: 600;
}
QPushButton:hover { background: #2c3244; border-color: #4b5368; }
QPushButton:pressed { background: #1d2230; }
QPushButton#primary_button {
    background: #c8a45e;
    border-color: #c8a45e;
    color: #17130b;
    padding: 8px 20px;
}
QPushButton#primary_button:hover { background: #d8b56e; border-color: #d8b56e; }
QPushButton#ghost_button { background: transparent; border-color: #363d50; }
QPushButton#ghost_button:hover { background: #1d2230; }
QPushButton#add_attack {
    background: transparent;
    border: 1px dashed #4b5368;
    color: #c8a45e;
}
QPushButton#add_attack:hover { background: #1d2230; border-color: #c8a45e; }
QProgressBar#hp_bar {
    background: #11131a;
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
    color: transparent;
}
QProgressBar#hp_bar::chunk {
    background: #8d3c3c;
    border-radius: 3px;
}
#actor_footer {
    background: #171922;
    border-top: 1px solid #2c3040;
}
#footer_hint { color: #7f8696; font-size: 8.5pt; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; }
QScrollBar::handle:vertical {
    background: #363d50; border-radius: 5px; min-height: 28px;
}
QScrollBar::handle:vertical:hover { background: #4b5368; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QToolTip {
    background: #1b1f2b;
    color: #e8e6e0;
    border: 1px solid #c8a45e;
    padding: 5px 8px;
}
"""

#: Подсказка к полю клавиши особой атаки — до проверки конфликта.
SPECIAL_KEY_HINT = (
    "Клавиша быстрого вызова: одна латинская буква или цифра. "
    "Клавиши стола (C — рывок, Q — быстрая атака, R — поворот, "
    "M — линейка, N — ход, WASD и кисти) заняты — их взять нельзя")

#: Красная рамка поля, если клавиша уже занята столом.
_CONFLICT_STYLE = "QKeySequenceEdit { border: 1px solid #c0392b; }"


def _mod(score) -> int:
    return lss_format.modifier(score)


class ActorSheetDialog(QDialog):
    """Единая карточка актёра: правка, атаки, спасброски и состояния."""

    saveRequested = pyqtSignal(dict)
    #: готовая атака из строки статблока (payload для ROLL_ATTACK)
    attackRollRequested = pyqtSignal(dict)
    #: обычный спасбросок из карточки (payload для ROLL_SAVE)
    saveRollRequested = pyqtSignal(dict)
    #: состояния переключены прямо в карточке (token_id, список состояний)
    conditionsChanged = pyqtSignal(dict)
    #: спасбросок от смерти (token_id) — бросок делает сервер
    deathSaveRollRequested = pyqtSignal(str)
    #: ручная отметка кружка (token_id, индекс, значение)
    deathSaveSetRequested = pyqtSignal(str, int, str)
    #: воскрешение мёртвого (token_id) — только мастер
    deathReviveRequested = pyqtSignal(str)
    #: стабилизация умирающего (token_id) — только мастер
    deathStabilizeRequested = pyqtSignal(str)
    #: особая атака существа ({token_id, attack_id}) — нужна цель на холсте
    specialAttackRequested = pyqtSignal(dict)

    def __init__(self, token, sheet=None, parent=None, can_revive=False,
                 other_tokens=None, can_edit: bool = True):
        super().__init__(parent)
        self.setObjectName("actor_sheet")
        self.setWindowTitle(f"Карточка актёра — {token.name}")
        # Ширина рассчитана на шесть карточек характеристик, ряд атаки и
        # четыре поля защиты без горизонтальной прокрутки.
        self.setMinimumSize(780, 700)
        self.resize(920, 860)
        self.setStyleSheet(ACTOR_SHEET_QSS)
        self._token = token
        self._sheet = sheet
        self._can_revive = bool(can_revive)
        self._can_edit = bool(can_edit)
        self._others = list(other_tokens or [])
        self._death_results = list(getattr(token, "death_saves", None) or [])
        self._block = dict(getattr(token, "statblock", None) or {})
        self._restoring = False
        self._edit_history = []
        self._redo_history = []
        self._undo_timer = QTimer(self)
        self._undo_timer.setSingleShot(True)
        self._undo_timer.setInterval(350)
        self._undo_timer.timeout.connect(self._commit_snapshot)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._build_header(root)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setObjectName("actor_body")
        body = QWidget()
        body.setObjectName("actor_body")
        self._body = QVBoxLayout(body)
        self._body.setContentsMargins(18, 16, 18, 18)
        self._body.setSpacing(13)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        self._build_vitals()
        self._build_death_bar()
        self._build_abilities()
        self._build_saves()
        self._build_attacks()
        self._build_protection()
        self._build_conditions()
        self._build_notes()
        self._build_footer(root)

        self._recalc_abilities()
        self._recalc_saves()
        self._sync_death_bar()
        self._update_hp_chip()
        self._connect_dirty_tracking()
        self._edit_history = [self._snapshot()]
        self._install_shortcuts()

    # ------------------------------------------------------------------
    # Внешний вид
    # ------------------------------------------------------------------
    def _build_header(self, root: QVBoxLayout):
        header = QFrame()
        header.setObjectName("actor_header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(18, 14, 18, 14)
        hl.setSpacing(13)

        portrait = QLabel()
        portrait.setFixedSize(58, 58)
        portrait.setPixmap(self._portrait_pixmap(self._token, 58))
        portrait.setToolTip("Портрет токена")
        hl.addWidget(portrait)

        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.name_edit = QLineEdit(self._token.name)
        self.name_edit.setObjectName("actor_name")
        self.name_edit.setPlaceholderText("Имя актёра")
        self.name_edit.setToolTip("Имя токена на карте")
        titles.addWidget(self.name_edit)

        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(0.5, 20.0)
        self.size_spin.setSingleStep(0.5)
        self.size_spin.setDecimals(1)
        self.size_spin.setValue(float(self._token.size or 1.0))
        self.size_spin.setToolTip("Размер в клетках: 0.5 — мелкий, 1 — средний, 2 — крупный")

        self.type_edit = QLineEdit(str(self._block.get("type") or ""))
        self.type_edit.setPlaceholderText("Тип: гуманоид (орк)")
        self.type_edit.setObjectName("actor_sub")
        self.type_edit.setToolTip("Тип существа")
        self.cr_edit = QLineEdit(str(self._block.get("cr") or ""))
        self.cr_edit.setPlaceholderText("ОП")
        self.cr_edit.setMaximumWidth(52)
        self.cr_edit.setObjectName("actor_sub")
        self.cr_edit.setToolTip("Уровень угрозы (CR)")

        sub_row = QHBoxLayout()
        sub_row.setSpacing(6)
        sub_row.addWidget(self.type_edit, 1)
        cr_label = QLabel("ОП")
        cr_label.setObjectName("actor_sub")
        sub_row.addWidget(cr_label)
        sub_row.addWidget(self.cr_edit)
        titles.addLayout(sub_row)

        # Зрение токена: угол сектора и дальность. Это настройка стола, а не
        # статблока, поэтому хранится в токене. По этим числам сервер решает,
        # можно ли существу спрятаться (правило 5e «нельзя прятаться от
        # того, кто тебя видит»). Поворот взгляда — клавиша R на холсте.
        vision_row = QHBoxLayout()
        vision_row.setSpacing(5)
        vision_label = QLabel("ОБЗОР")
        vision_label.setObjectName("actor_sub")
        vision_row.addWidget(vision_label)
        self.vision_angle_spin = QSpinBox()
        self.vision_angle_spin.setRange(int(vision_rules.MIN_ANGLE),
                                        int(vision_rules.MAX_ANGLE))
        self.vision_angle_spin.setSingleStep(5)
        self.vision_angle_spin.setSuffix("°")
        self.vision_angle_spin.setValue(int(vision_rules.normalize_angle(
            getattr(self._token, "vision_angle", None))))
        self.vision_angle_spin.setToolTip(
            "Угол обзора конусом: 70° по умолчанию, 360° — круговой обзор")
        self.vision_angle_spin.setMaximumWidth(78)
        vision_row.addWidget(self.vision_angle_spin)
        self.vision_range_spin = QSpinBox()
        self.vision_range_spin.setRange(int(vision_rules.MIN_RANGE_FT),
                                        int(vision_rules.MAX_RANGE_FT))
        self.vision_range_spin.setSingleStep(5)
        self.vision_range_spin.setSuffix(" фт")
        self.vision_range_spin.setValue(int(vision_rules.normalize_range(
            getattr(self._token, "vision_range", None))))
        self.vision_range_spin.setToolTip(
            "Дальность зрения в футах: 60 футов по умолчанию")
        self.vision_range_spin.setMaximumWidth(96)
        vision_row.addWidget(self.vision_range_spin)
        vision_row.addStretch(1)
        titles.addLayout(vision_row)
        hl.addLayout(titles, 1)

        self.hp_chip = QLabel()
        self.hp_chip.setObjectName("actor_chip")
        self.hp_chip.setAlignment(Qt.AlignCenter)
        self.hp_chip.setMinimumWidth(104)
        hl.addWidget(self.hp_chip, 0, Qt.AlignTop)

        root.addWidget(header)

    def _portrait_pixmap(self, token, size: int) -> QPixmap:
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, size, size), 10, 10)
        painter.setClipPath(path)
        src = QPixmap()
        image_path = getattr(token, "image_path", "")
        if image_path and os.path.isfile(image_path):
            src.load(image_path)
        if not src.isNull():
            scaled = src.scaled(size, size, Qt.KeepAspectRatioByExpanding,
                                Qt.SmoothTransformation)
            painter.drawPixmap((size - scaled.width()) // 2,
                               (size - scaled.height()) // 2, scaled)
        else:
            painter.fillRect(0, 0, size, size, QColor("#2b2433"))
            font = QFont(painter.font())
            font.setPointSize(max(10, int(size * 0.34)))
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor("#c8a45e"))
            letter = (getattr(token, "name", "") or "?")[:1].upper()
            painter.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, letter)
        painter.setClipping(False)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor("#c8a45e"), 1))
        painter.drawRoundedRect(QRectF(0.5, 0.5, size - 1, size - 1), 9, 9)
        painter.end()
        return pix

    def _section(self, title: str, hint: str = "") -> QWidget:
        """Заголовок раздела: подпись, тонкая линия и необязательная подсказка."""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(8)
        label = QLabel(title)
        label.setObjectName("sheet_section")
        row.addWidget(label)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: #2a2f3d; background: #2a2f3d; max-height: 1px;")
        line.setSizePolicy(line.sizePolicy().horizontalPolicy(),
                           line.sizePolicy().verticalPolicy())
        row.addWidget(line, 1)
        if hint:
            hint_label = QLabel(hint)
            hint_label.setObjectName("section_hint")
            hint_label.setWordWrap(True)
            row.addWidget(hint_label)
        return box

    def _build_vitals(self):
        row = QHBoxLayout()
        row.setSpacing(8)
        self.hp_spin = QSpinBox()
        self.hp_spin.setRange(0, 9999)
        self.hp_spin.setValue(int(self._token.hp) if self._token.hp is not None else 0)
        self.hp_spin.setAlignment(Qt.AlignCenter)
        self.hp_spin.setToolTip("Текущие хиты. Ноль начинает спасброски от смерти")
        self.hp_max_spin = QSpinBox()
        self.hp_max_spin.setRange(0, 9999)
        self.hp_max_spin.setValue(int(self._token.hp_max) if self._token.hp_max else 0)
        self.hp_max_spin.setSpecialValueText("—")
        self.hp_max_spin.setAlignment(Qt.AlignCenter)
        self.hp_max_spin.setToolTip("Максимум хитов")
        self.ac_spin = QSpinBox()
        self.ac_spin.setRange(0, 99)
        self.ac_spin.setValue(
            int(self._token.ac) if self._token.ac is not None
            else int(self._block.get("ac") or 10))
        self.ac_spin.setAlignment(Qt.AlignCenter)
        self.ac_spin.setToolTip("Класс доспеха")
        # Бонус к инициативе — производное от Ловкости. Отдельного поля
        # «Ловкость» здесь больше нет: характеристика редактируется ниже,
        # а тут видно ровно то число, которое сервер прибавит к d20.
        self.initiative_spin = QSpinBox()
        self.initiative_spin.setRange(-10, 20)
        self.initiative_spin.setValue(
            token_stats.token_dex_mod(self._token, self._sheet))
        self.initiative_spin.setAlignment(Qt.AlignCenter)
        self.initiative_spin.setReadOnly(True)
        self.initiative_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.initiative_spin.setFocusPolicy(Qt.NoFocus)
        self.initiative_spin.setToolTip(
            "Бонус к инициативе — модификатор Ловкости. Считается сам.")
        self.speed_spin = QSpinBox()
        self.speed_spin.setRange(0, 999)
        self.speed_spin.setValue(
            token_stats.token_speed_ft(self._token, self._sheet))
        self.speed_spin.setAlignment(Qt.AlignCenter)
        self.speed_spin.setSuffix(" фт")
        self.speed_spin.setToolTip(
            "Скорость в футах. В бою вокруг токена подсвечиваются клетки "
            "этого радиуса, 5 футов на клетку.")

        hp_card = QFrame()
        hp_card.setObjectName("vital_card")
        hp_layout = QVBoxLayout(hp_card)
        hp_layout.setContentsMargins(9, 7, 9, 8)
        hp_layout.setSpacing(3)
        hp_title = QLabel("ХИТЫ")
        hp_title.setObjectName("vital_title")
        hp_title.setAlignment(Qt.AlignCenter)
        hp_layout.addWidget(hp_title)
        hp_line = QHBoxLayout()
        hp_line.setSpacing(3)
        hp_line.addWidget(self.hp_spin, 1)
        slash = QLabel("/")
        slash.setObjectName("vital_title")
        hp_line.addWidget(slash)
        hp_line.addWidget(self.hp_max_spin, 1)
        hp_layout.addLayout(hp_line)
        self.hp_bar = QProgressBar()
        self.hp_bar.setObjectName("hp_bar")
        self.hp_bar.setRange(0, 1)
        self.hp_bar.setValue(1)
        self.hp_bar.setTextVisible(False)
        self.hp_bar.setFixedHeight(5)
        hp_layout.addWidget(self.hp_bar)
        row.addWidget(hp_card, 2)

        row.addWidget(self._vital_card("КД", self.ac_spin), 1)
        row.addWidget(self._vital_card("ИНИЦИАТИВА", self.initiative_spin), 1)
        row.addWidget(self._vital_card("СКОРОСТЬ", self.speed_spin), 1)
        row.addWidget(self._vital_card("РАЗМЕР", self.size_spin, suffix="кл."), 1)
        self._body.addLayout(row)

        self.hp_spin.valueChanged.connect(self._update_hp_chip)
        self.hp_max_spin.valueChanged.connect(self._update_hp_chip)
        self.hp_spin.valueChanged.connect(self._sync_death_bar)

    def _vital_card(self, title: str, widget: QWidget, suffix: str = "") -> QFrame:
        card = QFrame()
        card.setObjectName("vital_card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(9, 7, 9, 8)
        layout.setSpacing(3)
        label = QLabel(title)
        label.setObjectName("vital_title")
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)
        widget.setMinimumHeight(28)
        layout.addWidget(widget)
        if suffix:
            small = QLabel(suffix)
            small.setObjectName("section_hint")
            small.setAlignment(Qt.AlignCenter)
            layout.addWidget(small)
        return card

    def _build_death_bar(self):
        self.death_bar = DeathSavesBar()
        self.death_bar.set_token_id(self._token.id)
        self.death_bar.rollRequested.connect(self.deathSaveRollRequested.emit)
        self.death_bar.setRequested.connect(self.deathSaveSetRequested.emit)
        self.death_bar.reviveRequested.connect(self.deathReviveRequested.emit)
        self.death_bar.stabilizeRequested.connect(
            self.deathStabilizeRequested.emit)
        self._body.addWidget(self.death_bar)

    def _build_abilities(self):
        self._body.addWidget(self._section("ХАРАКТЕРИСТИКИ"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        self.ability_spins = {}
        self._ability_mod_labels = {}
        abilities = self._block.get("abilities") or {}
        for col, key in enumerate(token_stats.ABILITIES):
            card = QFrame()
            card.setObjectName("ability_card")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(6, 5, 6, 5)
            layout.setSpacing(1)
            title = QLabel(token_stats.ABILITY_LABELS[key].upper())
            title.setObjectName("ability_title")
            title.setAlignment(Qt.AlignCenter)
            layout.addWidget(title)
            spin = QSpinBox()
            spin.setRange(1, 30)
            score = abilities.get(key, 10)
            spin.setValue(int(score) if score is not None else 10)
            spin.setAlignment(Qt.AlignCenter)
            spin.setToolTip(f"{token_stats.ABILITY_LABELS[key]}: значение характеристики")
            spin.valueChanged.connect(self._recalc_abilities)
            # Спасбросок — модификатор характеристики + владение, поэтому
            # правка характеристики должна сразу пересчитать ряд спасбросков.
            spin.valueChanged.connect(self._recalc_saves)
            if key == "dex":
                # Инициатива — это d20 + модификатор Ловкости, поэтому поле
                # бонуса обновляется сразу, как только правят характеристику.
                spin.valueChanged.connect(self._sync_initiative_bonus)
            layout.addWidget(spin)
            mod = QLabel()
            mod.setObjectName("ability_mod")
            mod.setAlignment(Qt.AlignCenter)
            layout.addWidget(mod)
            self.ability_spins[key] = spin
            self._ability_mod_labels[key] = mod
            grid.addWidget(card, 0, col)
        self._body.addLayout(grid)

    def _build_saves(self):
        self._body.addWidget(self._section(
            "СПАСБРОСКИ",
            "клик по клетке: нет / +2 / +4 \u00b7 кнопка d20 бросает спасбросок"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(7)
        self.save_checks = {}
        self._save_labels = {}
        known = self._block.get("saves") or []
        # Ступень владения: 0 — нет, 1 — владение (+2), 2 — мастерство (+4).
        # Это упрощённая модель стола: бонус мастерства не зависит от ОП
        # (в 5e у CR 10 он +4, у CR 5 — +3). Старые статблоки хранят только
        # итоговый mod, поэтому ранг восстанавливаем по разнице с
        # модификатором характеристики.
        stored = {}
        if isinstance(known, dict):
            for key, value in known.items():
                ability = token_stats.normalize_ability(key) or key
                stored[ability] = self._guess_save_rank(ability, value)
        else:
            for entry in known:
                if isinstance(entry, dict):
                    key = token_stats.normalize_ability(
                        entry.get("key") or entry.get("name")
                        or entry.get("ability"))
                elif isinstance(entry, str):
                    key = token_stats.normalize_ability(entry)
                else:
                    key = ""
                if key:
                    stored[key] = self._guess_save_rank(key, entry)
        for index, key in enumerate(token_stats.ABILITIES):
            card = QFrame()
            card.setObjectName("save_card")
            row = QHBoxLayout(card)
            row.setContentsMargins(7, 5, 5, 5)
            row.setSpacing(4)
            check = QCheckBox(token_stats.ABILITY_LABELS[key])
            check.setObjectName("save_check")
            check.setTristate(True)
            check.setCheckState(Qt.CheckState(stored.get(key, 0)))
            check.setCursor(Qt.PointingHandCursor)
            check.setToolTip(
                "Владение спасброском «"
                + token_stats.ABILITY_LABELS[key]
                + "»: клик — нет / +2 / +4")
            check.stateChanged.connect(self._recalc_saves)
            row.addWidget(check, 1)
            bonus = QLabel()
            bonus.setObjectName("save_bonus")
            bonus.setAlignment(Qt.AlignCenter)
            row.addWidget(bonus)
            # Кнопка броска стоит отдельно от галочки: клик по клетке
            # владения её не задевает.
            roll = QToolButton()
            roll.setObjectName("save_roll")
            roll.setText("d20")
            roll.setCursor(Qt.PointingHandCursor)
            roll.setToolTip(
                "Бросить спасбросок " + token_stats.ABILITY_LABELS[key]
                + " — d20 + модификатор и владение")
            roll.clicked.connect(lambda _=False, k=key: self._roll_save(k))
            row.addWidget(roll)
            self.save_checks[key] = check
            self._save_labels[key] = bonus
            grid.addWidget(card, index // 3, index % 3)
        self._body.addLayout(grid)

    def _guess_save_rank(self, key: str, entry) -> int:
        """Ступень владения из статблока (1 — +2, 2 — +4).

        Если в записи есть rank, он и главный. Иначе смотрим на разницу
        итогового бонуса и модификатора характеристики: +2 — владение,
        +4 и больше — мастерство (у аболета Телосложение +6 при моде +2
        даёт фиолетовую ступень).
        """
        rank = entry.get("rank") if isinstance(entry, dict) else None
        if rank is not None:
            try:
                return max(0, min(2, int(rank)))
            except (TypeError, ValueError):
                return 1
        mod = entry.get("mod") if isinstance(entry, dict) else entry
        if mod is None:
            return 1
        try:
            diff = int(mod) - _mod(self.ability_spins[key].value())
        except (TypeError, ValueError, KeyError):
            return 1
        return 2 if diff >= 4 else 1

    def _build_attacks(self):
        self._body.addWidget(self._section(
            "АТАКИ", "правка сохраняется вместе с карточкой"))
        header = QHBoxLayout()
        header.setSpacing(5)
        for text, stretch in (("НАЗВАНИЕ", 2), ("БОНУС", 0),
                              ("УРОН", 2), ("ТИП", 1), ("", 0)):
            label = QLabel(text)
            label.setObjectName("attack_header")
            if stretch:
                header.addWidget(label, stretch)
            else:
                header.addWidget(label)
        header.addSpacing(30)
        self._body.addLayout(header)

        self.attack_rows = []
        self.attacks_container = QWidget()
        self._attacks_layout = QVBoxLayout(self.attacks_container)
        self._attacks_layout.setContentsMargins(0, 0, 0, 0)
        self._attacks_layout.setSpacing(5)
        for attack in (self._block.get("attacks") or []):
            if isinstance(attack, dict):
                self._add_attack_row(attack)
        self._body.addWidget(self.attacks_container)

        add_bar = QHBoxLayout()
        add = QPushButton("+ Добавить атаку")
        add.setObjectName("add_attack")
        add.setToolTip("Добавить удар в статблок существа")
        add.clicked.connect(lambda: self._add_attack_row({}))
        add_bar.addWidget(add)
        add_bar.addStretch(1)
        self._body.addLayout(add_bar)

        quick_row = QHBoxLayout()
        quick_label = QLabel("Для быстрой атаки (Q):")
        quick_label.setObjectName("section_hint")
        quick_row.addWidget(quick_label)
        self.quick_combo = QComboBox()
        self.quick_combo.setToolTip(
            "Удар, который уйдёт при быстрой атаке (Q → клик по цели)")
        quick_row.addWidget(self.quick_combo, 1)
        self._body.addLayout(quick_row)
        self._refresh_quick_combo(self._block.get("quick_attack"))
        self._build_special_attacks()

    def _build_special_attacks(self):
        """Особые атаки существа: дыхание, проглатывание, ужас.

        Это действия со спасброском ЦЕЛИ, а не свойство существа, поэтому
        они стоят отдельными кнопками в разделе «АТАКИ». У каждой можно
        назначить клавишу прямо во время сессии — она попадает в статблок
        и работает на холсте у владельца токена и у мастера.
        """
        raw = [dict(entry) for entry in (self._block.get("special_attacks")
                                         or [])
               if isinstance(entry, dict)]
        self.special_rows = []
        if not raw:
            return
        self._body.addWidget(self._section(
            "ОСОБЫЕ АТАКИ", "спасбросок бросает цель · клавиша — хоткей"))
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        for index, entry in enumerate(raw):
            normal = special_attack_rules.normalize(entry, index)
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(6)
            button = QPushButton(normal["name"])
            button.setObjectName("special_attack_btn")
            summary = special_attack_rules.summary(normal)
            description = str(normal.get("text") or "").strip()
            button.setToolTip(
                summary + ("\n\n" + description if description else "")
                + "\n\nНажмите и выберите цель на холсте")
            button.clicked.connect(
                lambda _=False, aid=normal["id"]:
                self.specialAttackRequested.emit(
                    {"token_id": self._token.id, "attack_id": aid}))
            line.addWidget(button, 1)
            hint = QLabel(summary)
            hint.setObjectName("section_hint")
            hint.setWordWrap(True)
            line.addWidget(hint, 2)
            key_edit = QKeySequenceEdit(
                QKeySequence(str(normal.get("hotkey") or "")))
            key_edit.setObjectName("special_attack_key")
            key_edit.setMaximumWidth(96)
            key_edit.setToolTip(SPECIAL_KEY_HINT)
            key_edit.keySequenceChanged.connect(
                lambda sequence, edit=key_edit:
                self._on_special_hotkey_changed(edit, sequence))
            # Клавиша могла быть сохранена раньше, когда проверки не было:
            # показываем красным, что она не сработает.
            old_problem = special_attack_rules.hotkey_conflict(
                str(normal.get("hotkey") or ""))
            if old_problem:
                key_edit.setStyleSheet(_CONFLICT_STYLE)
                key_edit.setToolTip(old_problem)
            line.addWidget(key_edit)
            layout.addWidget(row)
            self.special_rows.append(
                {"entry": normal, "widget": row, "key": key_edit})
        self._body.addWidget(container)

    def _build_protection(self):
        self._body.addWidget(self._section("ЗАЩИТА"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(7)
        self.resist_edit = QLineEdit(", ".join(
            self._block.get("resist") or getattr(self._token, "resist", None) or []))
        self.immune_edit = QLineEdit(", ".join(
            self._block.get("immune") or getattr(self._token, "immune", None) or []))
        self.vuln_edit = QLineEdit(", ".join(
            self._block.get("vulnerable")
            or getattr(self._token, "vulnerable", None) or []))
        self.cond_imm_edit = QLineEdit(", ".join(
            self._block.get("condition_immunities") or []))
        for index, (title, edit) in enumerate((
                ("Сопротивление", self.resist_edit),
                ("Иммунитет", self.immune_edit),
                ("Уязвимость", self.vuln_edit),
                ("Иммунитет к состояниям", self.cond_imm_edit))):
            label = QLabel(title)
            label.setObjectName("section_hint")
            edit.setPlaceholderText("через запятую: огонь, холод")
            grid.addWidget(label, (index // 2) * 2, index % 2)
            grid.addWidget(edit, (index // 2) * 2 + 1, index % 2)
        self._body.addLayout(grid)

    def _build_conditions(self):
        self._body.addWidget(self._section(
            "СОСТОЯНИЯ", "влияют на атаки, спасброски и вид токена"))
        active = {str(n).casefold()
                  for n in token_stats.token_conditions(self._token, self._sheet)}
        # Иммунитет из статблока: такое состояние наложить нельзя, иначе
        # карточка разрешала бы, например, отравить нежить.
        immune = set(conditions_rules.normalize_immunities(
            self._block.get("condition_immunities")
            or token_stats.token_condition_immunities(self._token)))
        self.cond_checks = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)
        for index, name in enumerate(sheet_items.CONDITIONS_RU):
            check = QCheckBox(name)
            icon = self._condition_icon(name)
            if not icon.isNull():
                check.setIcon(icon)
                check.setIconSize(QSize(20, 20))
            if name in immune:
                check.setChecked(False)
                check.setEnabled(False)
                check.setToolTip(
                    f"«{name}»: у существа иммунитет к этому состоянию")
            else:
                check.setChecked(name.casefold() in active)
                check.setToolTip(
                    f"Состояние «{name}» — накладывается сразу, как в Foundry")
            # Галочка применяется сразу, а не после «Сохранить»: состояния —
            # эффект на токене, а не правка статблока. Раньше они ждали
            # сохранения, и закрытая карточка «ничего не накладывала».
            check.toggled.connect(self._on_condition_toggled)
            self.cond_checks[name] = check
            grid.addWidget(check, index // 3, index % 3)
        self._body.addLayout(grid)
        # Длительные эффекты — отдельно от состояний: «Сопротивление огню»
        # живёт на токене и снимается сервером по раундам, а не галочкой.
        self.effects_label = QLabel("")
        self.effects_label.setObjectName("section_hint")
        self.effects_label.setWordWrap(True)
        self._body.addWidget(self.effects_label)
        self._sync_effects_from_token()

    def _on_condition_toggled(self, *_):
        """Галочка состояния: сразу разослать список, плюс запись в Ctrl+Z."""
        self._schedule_snapshot()
        if self._restoring:
            return
        self.conditionsChanged.emit({
            'token_id': self._token.id,
            'conditions': self._selected_conditions(),
        })

    def _condition_icon(self, name: str) -> QIcon:
        try:
            from client.game_canvas import CONDITION_ICON_DIR, condition_icon_key
            key = condition_icon_key(name)
            if not key:
                return QIcon()
            path = os.path.join(CONDITION_ICON_DIR, key + ".png")
            return QIcon(path) if os.path.isfile(path) else QIcon()
        except Exception:
            return QIcon()

    def _build_notes(self):
        self._body.addWidget(self._section("ОПИСАНИЕ"))
        self.notes_edit = QTextEdit()
        self.notes_edit.setPlainText(str(self._block.get("description") or ""))
        self.notes_edit.setPlaceholderText(
            "Тактика, особенности, что важно помнить за столом…")
        self.notes_edit.setMinimumHeight(70)
        self._body.addWidget(self.notes_edit)

    def _build_footer(self, root: QVBoxLayout):
        footer = QFrame()
        footer.setObjectName("actor_footer")
        row = QHBoxLayout(footer)
        row.setContentsMargins(18, 10, 18, 10)
        row.setSpacing(8)
        hint = QLabel("Ctrl+Z — отменить правку, Ctrl+Shift+Z — вернуть")
        hint.setObjectName("footer_hint")
        row.addWidget(hint)
        row.addStretch(1)
        cancel = QPushButton("Отмена")
        cancel.setObjectName("ghost_button")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        self.save_button = QPushButton("Сохранить")
        self.save_button.setObjectName("primary_button")
        self.save_button.setDefault(True)
        self.save_button.setToolTip("Отправить правки карточки всем участникам")
        self.save_button.clicked.connect(self._on_save)
        row.addWidget(self.save_button)
        root.addWidget(footer)
        self._footer_hint = hint

    # ------------------------------------------------------------------
    # Пересчёт и синхронизация
    # ------------------------------------------------------------------
    def _recalc_abilities(self, *_):
        for key, spin in self.ability_spins.items():
            self._ability_mod_labels[key].setText(f"{_mod(spin.value()):+d}")

    def _sync_initiative_bonus(self, *_):
        """Показать бонус к инициативе от текущего значения Ловкости."""
        if hasattr(self, "initiative_spin"):
            self.initiative_spin.setValue(
                _mod(self.ability_spins["dex"].value()))

    def _recalc_saves(self, *_):
        for key, check in self.save_checks.items():
            rank = int(check.checkState())
            bonus = _mod(self.ability_spins[key].value()) + 2 * rank
            self._save_labels[key].setText(f"{bonus:+d}")
            self._apply_save_rank_style(check, rank)

    def _apply_save_rank_style(self, check, rank: int):
        """Подсветить галочку: жёлтая — владение (+2), фиолетовая — +4."""
        check.setProperty("rank", str(rank))
        check.style().unpolish(check)
        check.style().polish(check)

    def _update_hp_chip(self, *_):
        hp = self.hp_spin.value()
        hp_max = self.hp_max_spin.value()
        text = str(hp) + (f" / {hp_max}" if hp_max else "")
        temp = int(getattr(self._token, "hp_temp", 0) or 0)
        if temp > 0:
            text += f" (+{temp})"
        self.hp_chip.setText(f"ХИТЫ {text}" if hp > 0 else f"МЁРТВ {text}")
        self.hp_chip.setToolTip(
            f"Временные хиты: {temp}" if temp > 0 else "")
        self.hp_chip.setProperty("dead", hp <= 0)
        self.hp_chip.style().unpolish(self.hp_chip)
        self.hp_chip.style().polish(self.hp_chip)
        self.hp_bar.setRange(0, max(1, hp_max))
        self.hp_bar.setValue(max(0, min(hp, hp_max or 1)))
        self.hp_bar.setVisible(bool(hp_max))

    def _sync_death_bar(self, *_):
        if not hasattr(self, "death_bar"):
            return
        self.death_bar.set_token_id(self._token.id)
        self.death_bar.set_death_state(
            self._death_results, self.hp_spin.value(), can_edit=True,
            can_revive=self._can_revive)

    def set_death_state(self, results, hp=None):
        self._death_results = list(results or [])
        if hp is not None:
            try:
                hp = int(hp)
            except (TypeError, ValueError):
                hp = None
        if hp is not None and self.hp_spin.value() != hp:
            self.hp_spin.blockSignals(True)
            self.hp_spin.setValue(hp)
            self.hp_spin.blockSignals(False)
        self._sync_death_bar()
        self._update_hp_chip()

    def refresh_from_token(self):
        """Обновить только безопасные поля после сетевого апдейта.

        Поля, которые пользователь сейчас правит, не трогаем: иначе чужой
        UPDATE_TOKEN перебивал бы ввод. Смерть, состояния и эффекты
        обновляем всегда — это важно для кружков под черепом и чипов зелий.
        """
        self._token = self._token
        self._sync_effects_from_token()
        if not self._edit_history:
            return
        self.set_death_state(getattr(self._token, "death_saves", None),
                             getattr(self._token, "hp", None))
        self._sync_conditions_from_token()

    def _sync_effects_from_token(self):
        """Показать длительные эффекты и временные хиты в карточке."""
        label = getattr(self, "effects_label", None)
        if label is None:
            return
        parts = []
        for effect in getattr(self._token, "active_effects", None) or []:
            if not isinstance(effect, dict):
                continue
            text = str(effect.get("label") or "Эффект")
            left = effect.get("rounds_left")
            if left:
                text += f" — {left} р."
            parts.append(text)
        temp = int(getattr(self._token, "hp_temp", 0) or 0)
        if temp > 0:
            parts.append(f"временные хиты: {temp}")
        label.setText("Эффекты: " + ", ".join(parts) if parts else "")
        label.setVisible(bool(parts))

    def _sync_conditions_from_token(self):
        active = {str(n).casefold()
                  for n in token_stats.token_conditions(self._token, self._sheet)}
        self._restoring = True
        for name, check in self.cond_checks.items():
            check.blockSignals(True)
            check.setChecked(name.casefold() in active)
            check.blockSignals(False)
        self._restoring = False

    # ------------------------------------------------------------------
    # Атаки
    # ------------------------------------------------------------------
    def _row_attack_id(self, row) -> str:
        attack_id = str(row.get("id") or "")
        if attack_id:
            return attack_id
        name = row["name"].text().strip()
        damage = row["damage"].text().strip()
        if not name and not damage:
            return ""
        attack_id = f"sb:{name}|{damage}".casefold()
        row["id"] = attack_id
        return attack_id

    def _refresh_quick_combo(self, preferred=None):
        if getattr(self, "quick_combo", None) is None:
            return
        if preferred is None:
            preferred = self.quick_combo.currentData()
        if preferred is None:
            preferred = (getattr(self._token, "statblock", None) or {}).get(
                "quick_attack")
        self.quick_combo.blockSignals(True)
        self.quick_combo.clear()
        for row in self.attack_rows:
            attack_id = self._row_attack_id(row)
            if not attack_id:
                continue
            label = row["name"].text().strip() or "Атака"
            damage = row["damage"].text().strip()
            if damage:
                label += f" ({damage})"
            self.quick_combo.addItem(label, attack_id)
        index = self.quick_combo.findData(preferred) if preferred else -1
        if index < 0 and preferred:
            # Старые сохранения помнят id с типом внутри урона
            # («sb:укус|2d4+2 колющий»), а строка теперь хранит чистую
            # формулу. Без сравнения по нормализованному id быстрая атака
            # молча перескакивала на первый удар.
            wanted = self._normalized_attack_id(preferred)
            for position in range(self.quick_combo.count()):
                if self._normalized_attack_id(
                        self.quick_combo.itemData(position)) == wanted:
                    index = position
                    break
        if self.quick_combo.count():
            self.quick_combo.setCurrentIndex(index if index >= 0 else 0)
        self.quick_combo.blockSignals(False)

    @staticmethod
    def _normalized_attack_id(attack_id) -> str:
        """id удара без типа урона: «sb:укус|2d4+2 колющий» -> «sb:укус|2d4+2»."""
        text = str(attack_id or "")
        name, sep, damage = text.partition("|")
        if not sep:
            return text.casefold()
        formula, _kind = combat_rules.split_damage_text(damage)
        return f"{name}|{formula}".casefold()

    def _add_attack_row(self, attack: dict):
        row = QFrame()
        row.setObjectName("attack_row")
        hl = QHBoxLayout(row)
        hl.setContentsMargins(6, 4, 4, 4)
        hl.setSpacing(5)
        name = QLineEdit(str(attack.get("name") or ""))
        name.setObjectName("attack_name")
        name.setPlaceholderText("Название удара")
        name.setMinimumWidth(110)
        name.setToolTip("Название атаки")
        bonus = QSpinBox()
        bonus.setObjectName("attack_bonus")
        bonus.setRange(-20, 30)
        try:
            bonus.setValue(int(attack.get("bonus") or 0))
        except (TypeError, ValueError):
            bonus.setValue(0)
        bonus.setToolTip("Бонус атаки")
        bonus.setMinimumWidth(48)
        # Тип мог быть записан словами в самой формуле («1d4+2 колющий»):
        # отделяем его и кладём в соседнее поле, как на карточке Foundry.
        # Поле может прийти и ячейкой LSS ({"value": ...}) — разворачиваем.
        raw_damage = attack.get("damage")
        if isinstance(raw_damage, dict):
            raw_damage = raw_damage.get("value")
        damage_text, parsed_type = combat_rules.split_damage_text(raw_damage)
        if parsed_type:
            # Тип найден — в «УРОН» остаётся только формула (даже если она
            # пустая: запись «колющий» без кубиков — не формула).
            damage = QLineEdit(damage_text)
        else:
            damage = QLineEdit(str(raw_damage or ""))
        damage.setObjectName("attack_damage")
        damage.setPlaceholderText("2d6+3")
        damage.setMinimumWidth(80)
        damage.setToolTip("Формула урона (тип — отдельным полем)")
        dtype = QComboBox()
        dtype.setObjectName("attack_type")
        dtype.addItem("— тип —", "")
        for key, label in combat_rules.DAMAGE_TYPES.items():
            dtype.addItem(label, key)
        raw_type = attack.get("damage_type")
        if isinstance(raw_type, dict):
            raw_type = raw_type.get("value")
        type_value = (combat_rules.normalize_damage_type(raw_type or "")
                      or parsed_type)
        index = dtype.findData(type_value)
        if index >= 0:
            dtype.setCurrentIndex(index)
        dtype.setMinimumWidth(94)
        dtype.setToolTip("Тип урона")
        roll = QToolButton()
        roll.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        roll.setIconSize(QSize(14, 14))
        roll.setToolTip("Бросить эту атаку по выбранной цели")
        roll.clicked.connect(lambda _=False, r=row: self._roll_attack_row(r))
        remove = QToolButton()
        remove.setIcon(self.style().standardIcon(QStyle.SP_TrashIcon))
        remove.setIconSize(QSize(14, 14))
        remove.setToolTip("Удалить атаку")
        remove.clicked.connect(lambda _=False, r=row: self._remove_attack_row(r))
        hl.addWidget(name, 2)
        hl.addWidget(bonus)
        hl.addWidget(damage, 2)
        hl.addWidget(dtype, 1)
        hl.addWidget(roll)
        hl.addWidget(remove)
        self._attacks_layout.addWidget(row)
        entry = {"widget": row, "id": str(attack.get("id") or ""),
                 "name": name, "bonus": bonus, "damage": damage, "type": dtype}
        self.attack_rows.append(entry)
        for widget in [row] + row.findChildren(QWidget):
            widget.installEventFilter(self)
        name.textChanged.connect(self._refresh_quick_combo)
        name.textChanged.connect(self._schedule_snapshot)
        damage.textChanged.connect(self._schedule_snapshot)
        # Слово типа в поле урона сразу переносим в список «ТИП»: иначе
        # бросок урона спотыкался о «колющий», а сопротивление цели
        # вообще не срабатывало.
        damage.textChanged.connect(
            lambda _text, e=entry: self._sync_damage_type(e))
        damage.editingFinished.connect(
            lambda e=entry: self._commit_damage_text(e))
        bonus.valueChanged.connect(self._schedule_snapshot)
        dtype.currentIndexChanged.connect(self._schedule_snapshot)
        self._refresh_quick_combo()
        if not self._restoring:
            self._schedule_snapshot()

    def _sync_damage_type(self, row):
        """Тип, напечатанный в поле урона, сразу выбирается в списке «ТИП»."""
        if self._restoring:
            return
        _formula, key = combat_rules.split_damage_text(row["damage"].text())
        if not key:
            return
        index = row["type"].findData(key)
        if index < 0 or row["type"].currentIndex() == index:
            return
        row["type"].blockSignals(True)
        row["type"].setCurrentIndex(index)
        row["type"].blockSignals(False)
        self._schedule_snapshot()

    def _commit_damage_text(self, row):
        """Убрать слово типа из поля урона: его место — в списке «ТИП»."""
        text = row["damage"].text()
        formula, key = combat_rules.split_damage_text(text)
        if key:
            index = row["type"].findData(key)
            if index >= 0:
                row["type"].blockSignals(True)
                row["type"].setCurrentIndex(index)
                row["type"].blockSignals(False)
        if formula and formula != text.strip():
            row["damage"].blockSignals(True)
            row["damage"].setText(formula)
            row["damage"].blockSignals(False)
            self._refresh_quick_combo()
            self._schedule_snapshot()

    def _remove_attack_row(self, row):
        self.attack_rows = [r for r in self.attack_rows if r["widget"] is not row]
        row.setParent(None)
        row.deleteLater()
        self._refresh_quick_combo()
        self._schedule_snapshot()

    def _on_special_hotkey_changed(self, edit, sequence):
        """Не дать особой атаке занять горячую клавишу стола.

        QKeySequenceEdit запоминает клавишу сразу по нажатию, поэтому
        проверять надо здесь же: занятую букву (C — рывок, Q — быстрая
        атака) стираем и говорим об этом, а не молча роняем при отправке.
        """
        text = sequence.toString().strip().upper()
        problem = special_attack_rules.hotkey_conflict(text)
        if problem:
            edit.blockSignals(True)
            edit.setKeySequence(QKeySequence())
            edit.blockSignals(False)
            edit.setStyleSheet(_CONFLICT_STYLE)
            edit.setToolTip(problem)
            QToolTip.showText(edit.mapToGlobal(edit.rect().bottomLeft()),
                              problem, edit)
        else:
            edit.setStyleSheet("")
            edit.setToolTip(SPECIAL_KEY_HINT)
        self._schedule_snapshot()

    def _collect_special_attacks(self) -> list:
        """Особые атаки вместе с назначенными клавишами.

        Храню только однобуквенные хоткеи: клавиши вроде F5 или Ctrl+Z на
        холсте перехватывают другие инструменты, и обещание «любая клавиша»
        превращалось бы в тишину. При пустом поле хоткея нет.
        """
        rows = getattr(self, "special_rows", None)
        if not rows:
            return [dict(entry) for entry in (self._block.get("special_attacks")
                                              or [])
                    if isinstance(entry, dict)]
        out = []
        for row in rows:
            entry = dict(row["entry"])
            sequence = row["key"].keySequence().toString().strip()
            entry["hotkey"] = sequence.upper() if len(sequence) == 1 else ""
            out.append(entry)
        return out

    def _collect_attacks(self) -> list:
        out = []
        for row in self.attack_rows:
            name = row["name"].text().strip()
            damage, parsed = combat_rules.split_damage_text(
                row["damage"].text())
            if not name and not damage:
                continue
            out.append({
                "id": self._row_attack_id(row),
                "name": name or "Атака",
                "bonus": row["bonus"].value(),
                "damage": damage,
                "damage_type": (row["type"].currentData()
                                or parsed or ""),
            })
        return out

    def _attack_from_row(self, row) -> dict:
        damage, parsed = combat_rules.split_damage_text(
            row["damage"].text())
        return {
            "name": row["name"].text().strip() or "Атака",
            "bonus": row["bonus"].value(),
            "damage": damage,
            "damage_type": row["type"].currentData() or parsed or "",
        }

    def _roll_attack_row(self, row):
        """Открыть бросок атаки с уже заполненным ударом этой строки."""
        try:
            from client.combat_panel import AttackDialog
        except Exception:
            return
        attack = self._attack_from_row(row)
        tokens = self._others or [self._token]
        dialog = AttackDialog(tokens, attacker_id=self._token.id, parent=self)
        dialog.bonus_spin.setValue(int(attack.get("bonus") or 0))
        if attack.get("damage"):
            dialog.damage_edit.setEditText(attack["damage"])
        index = dialog.type_combo.findData(attack.get("damage_type") or "")
        if index >= 0:
            dialog.type_combo.setCurrentIndex(index)
        dialog.attackRequested.connect(self.attackRollRequested.emit)
        dialog.exec_()

    def _roll_save(self, ability="dex"):
        """Кнопка d20 в строке спасброска: сразу бросок, без диалога.

        Бонус считается по текущей ступени владения карточки: сервер
        принимает ручной бонус от мастера, поэтому непрогруженное
        сохранение не мешает броску.
        """
        rank = int(self.save_checks[ability].checkState())
        bonus = _mod(self.ability_spins[ability].value()) + 2 * rank
        self.saveRollRequested.emit({
            'token_id': self._token.id,
            'ability': ability,
            'override_bonus': bonus,
        })

    # ------------------------------------------------------------------
    # Сбор правок
    # ------------------------------------------------------------------
    def _split_list(self, edit: QLineEdit) -> list:
        return [part.strip() for part in edit.text().split(",")
                if part.strip()]

    def _selected_conditions(self) -> list:
        return [name for name, check in self.cond_checks.items()
                if check.isChecked()]

    def payload(self) -> dict:
        abilities = {key: spin.value()
                     for key, spin in self.ability_spins.items()}
        mods = {key: _mod(value) for key, value in abilities.items()}
        saves = []
        for key, check in self.save_checks.items():
            rank = int(check.checkState())
            if rank:
                saves.append({"key": key, "rank": rank,
                              "mod": _mod(self.ability_spins[key].value())
                              + 2 * rank})
        token = self._token
        block = dict(getattr(token, "statblock", None) or {})
        block.update({
            "name": self.name_edit.text().strip() or token.name,
            "type": self.type_edit.text().strip(),
            "cr": self.cr_edit.text().strip(),
            "abilities": abilities,
            "mods": mods,
            "saves": saves,
            "attacks": self._collect_attacks(),
            "special_attacks": self._collect_special_attacks(),
            "quick_attack": self.quick_combo.currentData() or "",
            "resist": self._split_list(self.resist_edit),
            "immune": self._split_list(self.immune_edit),
            "vulnerable": self._split_list(self.vuln_edit),
            "condition_immunities": self._split_list(self.cond_imm_edit),
            "description": self.notes_edit.toPlainText(),
        })
        block["ac"] = self.ac_spin.value()
        block["hp"] = self.hp_spin.value()
        block["hp_max"] = self.hp_max_spin.value()
        # Число важнее списка строк: по нему холст считает клетки хода.
        block["speed"] = self.speed_spin.value()
        return {
            "token_id": token.id,
            "name": block["name"],
            "size": self.size_spin.value(),
            "hp": self.hp_spin.value(),
            "hp_max": self.hp_max_spin.value() or None,
            "ac": self.ac_spin.value(),
            "dex": self.initiative_spin.value(),
            "resist": block["resist"],
            "immune": block["immune"],
            "vulnerable": block["vulnerable"],
            "conditions": self._selected_conditions(),
            "statblock": block,
            # Зрение живёт на токене: оставляем его в ответе карточки,
            # иначе сохранение стирало бы угол и дальность.
            "vision_angle": float(self.vision_angle_spin.value()),
            "vision_range": float(self.vision_range_spin.value()),
        }

    def _on_save(self):
        self._commit_snapshot()
        self.saveRequested.emit(self.payload())
        self.accept()

    # ------------------------------------------------------------------
    # Ctrl+Z внутри карточки
    # ------------------------------------------------------------------
    def _install_shortcuts(self):
        """Ctrl+Z в карточке: фильтр событий, а не QShortcut.

        QLineEdit и QSpinBox перехватывают Ctrl+Z сами (отмена ввода в поле),
        поэтому ярлык диалога до них не доходил, и «отменить правку» не
        работало. Фильтр стоит на каждом поле и забирает сочетание раньше
        самого виджета — отменяется правка карточки, а не один символ.
        """
        for widget in [self] + self.findChildren(QWidget):
            widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        """Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y — история правок карточки."""
        if event.type() == QEvent.KeyPress:
            # event_key переводит «я» русской раскладки в Z, поэтому
            # Ctrl+Z/Ctrl+Я отменяют правку одинаково.
            key = event_key(event)
            mods = event.modifiers()
            if key == Qt.Key_Z and (mods & Qt.ControlModifier):
                if mods & Qt.ShiftModifier:
                    self._redo_edit()
                else:
                    self._undo_edit()
                return True
            if key == Qt.Key_Y and (mods & Qt.ControlModifier):
                self._redo_edit()
                return True
        return super().eventFilter(obj, event)

    def _connect_dirty_tracking(self):
        for spin in list(self.ability_spins.values()) + [
                self.hp_spin, self.hp_max_spin, self.ac_spin,
                self.initiative_spin, self.speed_spin,
                self.size_spin, self.vision_angle_spin,
                self.vision_range_spin]:
            spin.valueChanged.connect(self._schedule_snapshot)
        for edit in (self.name_edit, self.type_edit, self.cr_edit,
                     self.resist_edit, self.immune_edit, self.vuln_edit,
                     self.cond_imm_edit):
            edit.textChanged.connect(self._schedule_snapshot)
        self.notes_edit.textChanged.connect(self._schedule_snapshot)
        self.quick_combo.currentIndexChanged.connect(self._schedule_snapshot)

    def _schedule_snapshot(self, *_):
        if self._restoring:
            return
        self._undo_timer.start()

    def _snapshot(self) -> dict:
        return {
            "name": self.name_edit.text(),
            "size": self.size_spin.value(),
            "type": self.type_edit.text(),
            "cr": self.cr_edit.text(),
            "hp": self.hp_spin.value(),
            "hp_max": self.hp_max_spin.value(),
            "ac": self.ac_spin.value(),
            "dex": self.initiative_spin.value(),
            "speed": self.speed_spin.value(),
            "vision_angle": self.vision_angle_spin.value(),
            "vision_range": self.vision_range_spin.value(),
            "abilities": {k: s.value() for k, s in self.ability_spins.items()},
            "saves": {k: int(c.checkState())
                      for k, c in self.save_checks.items()},
            "attacks": self._collect_attacks(),
            "quick": self.quick_combo.currentData() or "",
            "resist": self.resist_edit.text(),
            "immune": self.immune_edit.text(),
            "vuln": self.vuln_edit.text(),
            "cond_imm": self.cond_imm_edit.text(),
            "conditions": self._selected_conditions(),
            "notes": self.notes_edit.toPlainText(),
        }

    def _apply_snapshot(self, state: dict):
        self._restoring = True
        try:
            self.name_edit.setText(state.get("name", ""))
            self.size_spin.setValue(float(state.get("size", 1.0)))
            self.type_edit.setText(state.get("type", ""))
            self.cr_edit.setText(state.get("cr", ""))
            self.hp_spin.setValue(int(state.get("hp", 0)))
            self.hp_max_spin.setValue(int(state.get("hp_max", 0)))
            self.ac_spin.setValue(int(state.get("ac", 10)))
            self.initiative_spin.setValue(int(state.get("dex", 0)))
            self.speed_spin.setValue(int(state.get("speed", 0)))
            if "vision_angle" in state:
                self.vision_angle_spin.setValue(int(state["vision_angle"]))
            if "vision_range" in state:
                self.vision_range_spin.setValue(int(state["vision_range"]))
            for key, value in (state.get("abilities") or {}).items():
                if key in self.ability_spins:
                    self.ability_spins[key].setValue(int(value))
            for key, value in (state.get("saves") or {}).items():
                if key in self.save_checks:
                    rank = max(0, min(2, int(value)))
                    self.save_checks[key].setCheckState(Qt.CheckState(rank))
            for row in list(self.attack_rows):
                row["widget"].setParent(None)
                row["widget"].deleteLater()
            self.attack_rows = []
            for attack in (state.get("attacks") or []):
                self._add_attack_row(dict(attack))
            self._refresh_quick_combo(state.get("quick"))
            self.resist_edit.setText(state.get("resist", ""))
            self.immune_edit.setText(state.get("immune", ""))
            self.vuln_edit.setText(state.get("vuln", ""))
            self.cond_imm_edit.setText(state.get("cond_imm", ""))
            selected = {str(n).casefold()
                        for n in (state.get("conditions") or [])}
            for name, check in self.cond_checks.items():
                check.setChecked(name.casefold() in selected)
            self.notes_edit.setPlainText(state.get("notes", ""))
        finally:
            self._restoring = False

    def _commit_snapshot(self):
        if self._restoring:
            return
        snapshot = self._snapshot()
        if not self._edit_history or snapshot != self._edit_history[-1]:
            self._edit_history.append(snapshot)
            self._redo_history.clear()
            if len(self._edit_history) > 60:
                self._edit_history.pop(0)

    def _undo_edit(self):
        if self._undo_timer.isActive():
            self._undo_timer.stop()
            self._commit_snapshot()
        if len(self._edit_history) <= 1:
            self._footer_hint.setText("Отменять нечего")
            return
        self._redo_history.append(self._edit_history.pop())
        self._apply_snapshot(self._edit_history[-1])
        self._footer_hint.setText("Правка отменена — Ctrl+Shift+Z вернёт")

    def _redo_edit(self):
        if not self._redo_history:
            self._footer_hint.setText("Возвращать нечего")
            return
        state = self._redo_history.pop()
        self._edit_history.append(state)
        self._apply_snapshot(state)
        self._footer_hint.setText("Правка возвращена")
