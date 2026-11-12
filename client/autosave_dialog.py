"""Окно списка автосохранений: восстановить, удалить или открыть папку."""

from __future__ import annotations

import os
import subprocess
import sys
import time

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel,
                             QListWidget, QListWidgetItem, QMessageBox,
                             QPushButton, QVBoxLayout)

from shared import world_store


class AutosaveDialog(QDialog):
    """Показывает снимки из data/autosaves и отдаёт выбранное имя."""

    restore_requested = pyqtSignal(str)

    def __init__(self, parent=None, data_root=None):
        super().__init__(parent)
        self.data_root = data_root
        self.setWindowTitle("Автосохранения")
        self.setMinimumSize(560, 420)
        self.setAttribute(Qt.WA_DeleteOnClose, False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        title = QLabel("Автосохранения")
        title.setStyleSheet("font-size: 15pt; font-weight: 600;")
        layout.addWidget(title)
        hint = QLabel(
            "Снимки делаются отдельно от основного мира, хранятся "
            "в папке autosaves и не перезаписывают ручные сохранения. "
            "Выберите снимок и нажмите «Восстановить».")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #c8c2b4;")
        layout.addWidget(hint)

        self.list = QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.currentRowChanged.connect(self._update_actions)
        self.list.itemDoubleClicked.connect(self._restore)
        layout.addWidget(self.list, 1)

        self.folder_label = QLabel()
        self.folder_label.setWordWrap(True)
        self.folder_label.setStyleSheet("color: #8f8a7d; font-size: 8.5pt;")
        layout.addWidget(self.folder_label)

        buttons = QHBoxLayout()
        self.restore_btn = QPushButton("Восстановить")
        self.restore_btn.setDefault(True)
        self.restore_btn.clicked.connect(self.restore_selected)
        self.delete_btn = QPushButton("Удалить")
        self.delete_btn.clicked.connect(self.delete_selected)
        self.folder_btn = QPushButton("Открыть папку")
        self.folder_btn.clicked.connect(self.open_folder)
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.reject)
        buttons.addWidget(self.restore_btn)
        buttons.addWidget(self.delete_btn)
        buttons.addWidget(self.folder_btn)
        buttons.addStretch(1)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        self._entries = []
        self.reload()

    # ------------------------------------------------------------------
    def reload(self):
        self.list.clear()
        self._entries = world_store.list_autosaves(self.data_root)
        for entry in self._entries:
            when = (time.strftime("%Y-%m-%d %H:%M",
                                  time.localtime(entry["saved_at"]))
                    if entry.get("saved_at") else "?")
            if entry.get("broken"):
                text = f"{when}   повреждённый снимок"
            else:
                text = (f"{when}   {entry['world_id'] or 'без названия'}   "
                        f"карт: {entry['maps']}, токенов: {entry['tokens']}")
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, entry["name"])
            if entry.get("broken"):
                item.setForeground(Qt.gray)
            self.list.addItem(item)
        folder = world_store.autosaves_dir(self.data_root)
        self.folder_label.setText(f"Папка: {os.path.abspath(folder)}")
        if self.list.count():
            self.list.setCurrentRow(0)
        self._update_actions()

    def _update_actions(self, *_args):
        has = self.list.currentItem() is not None
        self.restore_btn.setEnabled(has)
        self.delete_btn.setEnabled(has)

    def _current_entry(self):
        item = self.list.currentItem()
        if item is None:
            return None
        name = item.data(Qt.UserRole)
        return next((e for e in self._entries if e["name"] == name), None)

    def _choose_from_disk(self):
        """Запасной вариант, если снимков нет в списке."""
        folder = world_store.autosaves_dir(self.data_root)
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите автосохранение", os.path.abspath(folder),
            "Автосохранения (*.json)")
        if path:
            return os.path.basename(path)
        return None

    # ------------------------------------------------------------------
    def restore_selected(self):
        entry = self._current_entry()
        name = entry["name"] if entry else self._choose_from_disk()
        if not name:
            return
        if entry and entry.get("broken"):
            QMessageBox.warning(self, "Автосохранения",
                                "Этот снимок повреждён и не может быть "
                                "восстановлен.")
            return
        if QMessageBox.question(
                self, "Восстановить автосохранение",
                "Текущее состояние карты будет заменено выбранным снимком. "
                "Продолжить?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        self.restore_requested.emit(name)
        self.accept()

    def delete_selected(self):
        entry = self._current_entry()
        if not entry:
            return
        if QMessageBox.question(
                self, "Удалить автосохранение",
                "Удалить снимок от "
                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(entry['saved_at']))}?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            world_store.delete_autosave(self.data_root, entry["name"])
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Автосохранения",
                                f"Не удалось удалить снимок:\n{exc}")
            return
        self.reload()

    def open_folder(self):
        folder = os.path.abspath(world_store.autosaves_dir(self.data_root))
        try:
            os.makedirs(folder, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(folder)                      # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])         # noqa: S603
            else:
                subprocess.Popen(["xdg-open", folder])     # noqa: S603
        except Exception as exc:
            QMessageBox.warning(self, "Автосохранения",
                                f"Не удалось открыть папку:\n{exc}")

    def _restore(self, _item):
        # двойной клик по элементу обрабатывается так же, как кнопка
        self.restore_selected()
