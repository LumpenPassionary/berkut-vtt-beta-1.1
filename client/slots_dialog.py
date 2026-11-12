"""Слоты игроков: места за столом, которые создаёт мастер.

Слот — это не токен и не игрок, а место между ними: мастер заранее создаёт
«Слот 1…N», выдаёт слоту токены персонажей и может подгрузить туда лист LSS.
Игрок при входе занимает свободный слот; если свободных нет, сервер
отклоняет вход сообщением «Свободных слотов не осталось».

Диалог ничего не отправляет сам: он показывает состояние и просит окно стола
сделать действие сигналами — так его можно проверять без сети.
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel,
                             QListWidget, QListWidgetItem, QPushButton,
                             QVBoxLayout)


class SlotsDialog(QDialog):
    """Список слотов с кнопками по роли."""

    addRequested = pyqtSignal()
    deleteRequested = pyqtSignal(str)          # slot_id
    claimRequested = pyqtSignal(str)           # slot_id или "" — любой свободный
    assignRequested = pyqtSignal(str, str, bool)   # slot_id, token_id, выдать

    def __init__(self, parent=None, is_dm=False, player_id="", my_slot=None):
        super().__init__(parent)
        self.setWindowTitle("Слоты игроков")
        self.setMinimumSize(560, 420)
        self.is_dm = bool(is_dm)
        self.player_id = player_id
        self.my_slot = my_slot
        self._slots = {}
        self._tokens = []

        self.summary = QLabel("Слотов нет")
        self.summary.setWordWrap(True)
        self.list = QListWidget()
        self.list.setAlternatingRowColors(True)

        row = QHBoxLayout()
        if self.is_dm:
            add = QPushButton("+ Слот")
            add.setToolTip("Создать свободный слот — можно до начала игры")
            add.clicked.connect(self.addRequested.emit)
            row.addWidget(add)

            remove = QPushButton("Удалить слот")
            remove.clicked.connect(self._delete)
            row.addWidget(remove)

            self.token_box = QComboBox()
            self.token_box.setToolTip("Токен персонажа, который займёт слот")
            row.addWidget(self.token_box, 1)

            assign = QPushButton("Выдать токен слоту")
            assign.clicked.connect(self._assign)
            row.addWidget(assign)

            take = QPushButton("Забрать")
            take.clicked.connect(lambda: self._assign(assigned=False))
            row.addWidget(take)
        else:
            claim = QPushButton("Занять свободный слот")
            claim.setToolTip("Сесть за стол на свободное место")
            claim.clicked.connect(self._claim)
            row.addWidget(claim)
        row.addStretch(1)

        close = QPushButton("Закрыть")
        close.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self.summary)
        layout.addWidget(self.list, 1)
        layout.addLayout(row)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(close)
        layout.addLayout(bottom)

    # ------------------------------------------------------------------
    def set_state(self, slots: dict, tokens=(), my_slot=None):
        """Показать слоты: {id: {name, player_id, token_ids}} и токены."""
        self._slots = dict(slots or {})
        self._tokens = list(tokens or ())
        self.my_slot = my_slot if my_slot is not None else self.my_slot
        names = {t.id: t.name for t in self._tokens}
        free = 0
        self.list.clear()
        for slot_id, slot in sorted(self._slots.items(),
                                    key=lambda kv: str(kv[1].get("name") or "")):
            occupant = slot.get("player_id")
            if not occupant:
                free += 1
            tokens_text = ", ".join(
                names.get(tid, tid[:8]) for tid in (slot.get("token_ids") or []))
            if occupant == self.player_id:
                status = "вы"
            elif occupant:
                status = "занят"
            else:
                status = "свободен"
            text = f"{slot.get('name') or slot_id[:8]} — {status}"
            if tokens_text:
                text += f" · токены: {tokens_text}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, slot_id)
            if not occupant:
                item.setForeground(Qt.darkGreen)
            elif occupant == self.player_id:
                item.setForeground(Qt.darkBlue)
            self.list.addItem(item)

        total = len(self._slots)
        if not total:
            self.summary.setText(
                "Слотов нет — вход свободный. Мастер может добавить места.")
        else:
            self.summary.setText(
                f"Мест: {total}, свободно: {free}. "
                "Мастер выдаёт слоту токены персонажей заранее.")
        if self.is_dm:
            current = self.token_box.currentData()
            self.token_box.clear()
            for token in self._tokens:
                if getattr(token, "kind", "actor") != "player":
                    continue
                self.token_box.addItem(f"{token.name}", token.id)
            index = self.token_box.findData(current)
            if index >= 0:
                self.token_box.setCurrentIndex(index)

    def selected_slot(self) -> str:
        item = self.list.currentItem()
        return str(item.data(Qt.UserRole)) if item is not None else ""

    # ------------------------------------------------------------------
    def _delete(self):
        slot_id = self.selected_slot()
        if slot_id:
            self.deleteRequested.emit(slot_id)

    def _claim(self):
        self.claimRequested.emit(self.selected_slot())

    def _assign(self, assigned=True):
        slot_id = self.selected_slot()
        token_id = self.token_box.currentData() if self.is_dm else None
        if slot_id and token_id:
            self.assignRequested.emit(slot_id, token_id, bool(assigned))
