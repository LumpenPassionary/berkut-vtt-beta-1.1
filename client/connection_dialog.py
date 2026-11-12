"""
Стартовое окно: создать игру (DM) или подключиться к игре.

Здесь же показывается IPv4-адрес хоста, который нужно передать игрокам,
с подсказкой про Radmin VPN, и кнопка «Очистить кэш»: она убирает только
скачанные картинки и временные файлы — миры и листы не трогает.
"""
import os
import re
import socket
import json
import sys

from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QSpinBox, QFileDialog,
                             QMessageBox, QTabWidget, QWidget, QGroupBox,
                             QCheckBox, QGridLayout, QFrame)
from PyQt5.QtCore import Qt, QSize

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared import app_paths
from shared import assets as asset_utils
from shared import graphics as graphics_rules
from shared.models import generate_id
from client import app_icon
from client.graphics_settings import GraphicsSettingsWidget
from client.support_dialog import SupportDialog, support_icon

CONFIG_PATH = os.path.join(app_paths.home_dir(), "settings.json")

#: Версия приложения: показывается в правом нижнем углу стартового окна.
APP_VERSION = "beta 2.1"

#: Оформление стартового окна: та же палитра, что у стола (тёмный серый с
#: бронзовым акцентом), поэтому переход из меню в игру не выглядит скачком.
START_QSS = """
/* Фон задаём только окну и страницам вкладок: правило вида
   «QDialog, QWidget» красило бы каждую подпись отдельным тёмным
   прямоугольником поверх карточек. */
QDialog { background-color: #1a1a1c; color: #d8d6d2; }
QWidget#start_page { background: transparent; }
QLabel { background: transparent; color: #d8d6d2; }
QCheckBox { background: transparent; }
QLabel#start_title { font-size: 20px; font-weight: 600; color: #f0ece4; }
QLabel#start_sub { color: #8d8a84; font-size: 10pt; }
QLabel#start_hint { color: #8d8a84; font-size: 9pt; }
QLabel#start_version { color: #6d6a64; font-size: 9pt; letter-spacing: 1px; }
QLabel#start_card_title {
    color: #b08d4f; font-size: 9pt; font-weight: 600; letter-spacing: 1px;
}
QLabel#start_ip { color: #e4e2dc; font-family: "Consolas", "DejaVu Sans Mono"; }
QFrame#start_card {
    background: #202023; border: 1px solid #2c2c30; border-radius: 8px;
}
QLineEdit, QSpinBox {
    background: transparent; border: none;
    border-bottom: 1px solid #38383f; border-radius: 0;
    padding: 6px 3px; color: #e4e2dc;
    selection-background-color: #7c6335; selection-color: #f8f4ea;
}
QLineEdit:hover, QSpinBox:hover { border-bottom-color: #4b4b52; }
QLineEdit:focus, QSpinBox:focus { border-bottom-color: #b08d4f; }
QLineEdit:disabled, QSpinBox:disabled { color: #6d6a64; }
/* Поле ввода внутри счётчика не рисует свою рамку: иначе у него
   получался второй, меньший прямоугольник поверх счётчика. */
QSpinBox QLineEdit { background: transparent; border: none; padding: 0; }
QPushButton {
    background: #26262a; border: 1px solid #3a3a40; border-radius: 5px;
    padding: 7px 12px; color: #e4e2dc;
}
QPushButton:hover { background: #33343a; border-color: #4b4b52; }
QPushButton:pressed { background: #202024; }
QPushButton#primary_button {
    background: #b08d4f; border: 1px solid #c8a45e; color: #17130b;
    font-weight: 600; padding: 9px 16px;
}
QPushButton#primary_button:hover { background: #c8a45e; }
QPushButton#ghost_button { background: transparent; border: 1px solid #3a3a40; }
QPushButton#ghost_button:hover { border-color: #b08d4f; color: #f0ece4; }
QPushButton#support_button {
    background: transparent; border: 1px solid #4a3c24; color: #e7cf9a;
    padding: 7px 12px;
}
QPushButton#support_button:hover {
    background: #2a2418; border-color: #b08d4f; color: #f3dfb2;
}
QTabWidget::pane {
    border: 1px solid #2c2c30; border-radius: 8px; top: -1px;
    background: #1e1e21;
}
QTabBar::tab {
    background: #202023; color: #9a968f; padding: 8px 18px;
    border: 1px solid #2c2c30; border-bottom: none;
    border-top-left-radius: 6px; border-top-right-radius: 6px;
    margin-right: 2px;
}
QTabBar::tab:hover { color: #e4e2dc; background: #26262a; }
QTabBar::tab:selected {
    background: #1e1e21; color: #c8a45e; border-color: #3a3a40;
}
QCheckBox { spacing: 7px; color: #d8d6d2; }
QCheckBox::indicator {
    width: 14px; height: 14px; border-radius: 3px;
    border: 1px solid #3c3c42; background: #141416;
}
QCheckBox::indicator:checked { background: #b08d4f; border-color: #b08d4f; }
QGroupBox {
    border: 1px solid #2c2c30; border-radius: 8px; margin-top: 10px;
    padding: 10px 8px 8px 8px; background: #202023;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 10px; padding: 0 5px;
    color: #b08d4f;
}
"""


def _default_port(saved: int = 25565) -> int:
    """Порт: DND_VTT_PORT (vtt launch --port) важнее сохранённого значения."""
    try:
        return int(os.environ.get("DND_VTT_PORT", "")) or saved
    except ValueError:
        return saved


def local_ipv4_list():
    """Список локальных IPv4-адресов (и Radmin VPN среди них)"""
    addresses = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in addresses:
                addresses.append(ip)
    except Exception:
        pass
    # Основной адрес через фиктивное подключение (не отправляет данные)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        primary = s.getsockname()[0]
        s.close()
        if primary not in addresses:
            addresses.insert(0, primary)
    except Exception:
        pass
    if not addresses:
        addresses = ["127.0.0.1"]
    return addresses


class ConnectionDialog(QDialog):
    """Окно создания/подключения игры"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Berkut VTT dnd5e — подключение")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(600, 560)
        self.setStyleSheet(START_QSS)

        self.connection_info = None
        self.avatar_path = None
        # Галочка «Эффекты в бою» живёт в общих настройках: в стартовом окне
        # её нет, но запомненное значение должно дойти до стола.
        self.saved_combat_effects = True
        # Прозрачность крови: 0 — обычная, 100 — не видно.
        self.saved_blood_transparency = 0
        # Предзагрузка «Моих актёров» игрокам.
        self.saved_preload_my_actors = False
        # Зрение игроков (настройка мастера) и скрытые у мастера стены:
        # в стартовом окне их нет, но запомненные значения идут на стол.
        self.saved_vision_mode = "around"
        self.saved_hide_dm_walls = False
        # Туман войны у мастера: вид как у игроков, но права те же.
        self.saved_dm_fog = False
        # WASD: необязательное движение токена с клавиатуры. Своё у каждого
        # клиента, поэтому живёт в settings.json рядом с остальными
        # настройками и просто доезжает до стола.
        self.saved_wasd_move = False
        #: id игрока по нику: сервер узнаёт владельца токена именно по нему,
        #: поэтому после перезахода он должен остаться прежним.
        self.saved_player_ids = {}
        # Графика: качество теней, сглаживание и движок отрисовки. Живёт в
        # settings.json рядом с остальными настройками и уходит на стол.
        self.graphics_settings = dict(graphics_rules.DEFAULTS)

        self.build_ui()
        self.load_settings()
        # Вкладка «Графика» строит поля до чтения файла: без этой строки
        # окно показывало бы настройки по умолчанию, а на стол уходили бы
        # запомненные.
        self.graphics_widget.set_settings(self.graphics_settings)
        self.refresh_cache_label()

    # ------------------------------------------------------------------
    def _card(self):
        """Карточка-панель: рамка, отступы и вертикальный слой."""
        card = QFrame()
        card.setObjectName("start_card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        return card, layout

    @staticmethod
    def _field(grid, row, text, widget):
        """Подпись над полем: читается с любой ширины окна."""
        label = QLabel(text)
        label.setObjectName("start_hint")
        grid.addWidget(label, row * 2, 0)
        grid.addWidget(widget, row * 2 + 1, 0, 1, 2)

    def build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(12)

        # --- шапка: значок, название, назначение ---
        head = QHBoxLayout()
        head.setSpacing(12)
        badge = QLabel()
        badge.setFixedSize(52, 52)
        badge.setAlignment(Qt.AlignCenter)
        badge.setPixmap(app_icon().pixmap(52, 52))
        badge.setStyleSheet("background: transparent;")
        head.addWidget(badge)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("Berkut VTT dnd5e")
        title.setObjectName("start_title")
        titles.addWidget(title)
        sub = QLabel("Виртуальный стол в локальной сети — карты, токены, "
                     "кубики и бой")
        sub.setObjectName("start_sub")
        sub.setWordWrap(True)
        titles.addWidget(sub)
        head.addLayout(titles, 1)

        # Поддержка — с первого экрана, но не мешает создать игру:
        # маленькая кнопка в правом верхнем углу шапки.
        self.support_btn = QPushButton("Поддержать")
        self.support_btn.setObjectName("support_button")
        self.support_btn.setIcon(support_icon())
        self.support_btn.setIconSize(QSize(20, 20))
        self.support_btn.setToolTip(
            "Проект бесплатный — поддержать можно переводом по QR "
            "или подпиской на Boosty")
        self.support_btn.clicked.connect(self.open_support)
        head.addWidget(self.support_btn, 0, Qt.AlignTop)
        layout.addLayout(head)

        tabs = QTabWidget()
        tabs.addTab(self.host_tab(), "Создать игру (DM)")
        tabs.addTab(self.join_tab(), "Подключиться")
        tabs.addTab(self.graphics_tab(), "Графика")
        self.tabs = tabs
        layout.addWidget(tabs, 1)

        # --- общая настройка и подвал ---
        self.enable_3d = QCheckBox("Включить 3д дайсы")
        self.enable_3d.setToolTip(
            "Бросать физические кубики Dice Lab в отдельном окне "
            "вместо кнопок D20…D2 в чате")
        layout.addWidget(self.enable_3d)

        # Освещение — отдельный модуль стола: без галочки ночь только
        # затемняет карту, а свет, тёмное зрение и проверки Восприятия
        # работают по-старому.
        self.enable_lighting = QCheckBox("Включить освещение и темноту")
        self.enable_lighting.setToolTip(
            "Источники света на карте, ночь, тёмное зрение и помеха "
            "Восприятию в темноте. Включает вкладку «Освещение» у мастера")
        layout.addWidget(self.enable_lighting)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: #2c2c30;")
        layout.addWidget(line)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.cache_btn = QPushButton("Очистить кэш")
        self.cache_btn.setObjectName("ghost_button")
        self.cache_btn.setToolTip(
            "Удалить скачанные картинки и временные файлы. Миры, сохранения и "
            "листы персонажей останутся.")
        self.cache_btn.clicked.connect(self.clear_cache)
        footer.addWidget(self.cache_btn)
        self.cache_label = QLabel("Кэш: —")
        self.cache_label.setObjectName("start_hint")
        footer.addWidget(self.cache_label)
        footer.addStretch(1)
        # Версия — сразу видно, какая сборка запущена, и не мешает кнопкам
        # слева: подпись приглушённая и стоит в правом нижнем углу.
        self.version_label = QLabel(APP_VERSION)
        self.version_label.setObjectName("start_version")
        self.version_label.setToolTip("Версия приложения")
        footer.addWidget(self.version_label, 0, Qt.AlignRight | Qt.AlignVCenter)
        layout.addLayout(footer)

    # ------------------------------------------------------------------
    def open_support(self):
        """Показать окно поддержки поверх модального стартового окна."""
        dialog = SupportDialog(self, theme="dark")
        # Без этого закрытые окна копились бы скрытыми детьми стартового
        # окна: exec_() заканчивается, а C++-объект с родителем живёт.
        dialog.setAttribute(Qt.WA_DeleteOnClose, True)
        dialog.exec_()

    # ------------------------------------------------------------------
    def host_tab(self):
        w = QWidget()
        w.setObjectName("start_page")
        l = QVBoxLayout(w)
        l.setContentsMargins(12, 12, 12, 12)
        l.setSpacing(10)

        card, cl = self._card()
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        self.host_nick = QLineEdit()
        self.host_nick.setPlaceholderText("Например: Мастер")
        self._field(grid, 0, "Ваш ник (DM)", self.host_nick)

        avatar_row = QHBoxLayout()
        avatar_row.setSpacing(8)
        self.host_avatar_btn = QPushButton("Выбрать аватар")
        self.host_avatar_btn.clicked.connect(
            lambda: self.pick_avatar(self.host_avatar_lbl))
        avatar_row.addWidget(self.host_avatar_btn)
        self.host_avatar_lbl = QLabel("не выбран")
        self.host_avatar_lbl.setObjectName("start_hint")
        avatar_row.addWidget(self.host_avatar_lbl, 1)
        label = QLabel("Аватар мастера")
        label.setObjectName("start_hint")
        grid.addWidget(label, 2, 0)
        grid.addLayout(avatar_row, 3, 0, 1, 2)

        self.host_port = QSpinBox()
        self.host_port.setRange(1024, 65535)
        self.host_port.setValue(25565)
        port_row = QHBoxLayout()
        port_row.setSpacing(8)
        self.host_port.setFixedWidth(120)
        port_row.addWidget(self.host_port)
        port_row.addStretch(1)
        label = QLabel("Порт сервера")
        label.setObjectName("start_hint")
        grid.addWidget(label, 4, 0)
        grid.addLayout(port_row, 5, 0, 1, 2)
        cl.addLayout(grid)

        b = QPushButton("Создать игру и стать DM")
        b.setObjectName("primary_button")
        b.clicked.connect(self.do_host)
        cl.addWidget(b)
        l.addWidget(card)

        g = QGroupBox("Адрес для подключения игроков")
        gl = QVBoxLayout(g)
        gl.setSpacing(6)
        self.addresses_label = QLabel("")
        self.addresses_label.setObjectName("start_ip")
        self.addresses_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.addresses_label.setWordWrap(True)
        gl.addWidget(self.addresses_label)
        hint = QLabel("Игрок указывает адрес и тот же порт. Для игры через "
                      "интернет подключите всех к одной сети Radmin VPN и "
                      "используйте её адрес.")
        hint.setObjectName("start_hint")
        hint.setWordWrap(True)
        gl.addWidget(hint)
        l.addWidget(g)
        self.fill_addresses()
        l.addStretch(1)
        return w

    # ------------------------------------------------------------------
    def join_tab(self):
        w = QWidget()
        w.setObjectName("start_page")
        l = QVBoxLayout(w)
        l.setContentsMargins(12, 12, 12, 12)
        l.setSpacing(10)

        card, cl = self._card()
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)

        self.join_host = QLineEdit()
        self.join_host.setPlaceholderText("192.168.1.100 или 26.x.x.x (Radmin)")
        self._field(grid, 0, "Адрес хоста", self.join_host)

        self.join_port = QSpinBox()
        self.join_port.setRange(1024, 65535)
        self.join_port.setValue(25565)
        self.join_port.setFixedWidth(120)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.join_port)
        row.addStretch(1)
        label = QLabel("Порт")
        label.setObjectName("start_hint")
        grid.addWidget(label, 2, 0)
        grid.addLayout(row, 3, 0, 1, 2)

        self.join_nick = QLineEdit()
        self.join_nick.setPlaceholderText("Ваше имя в игре")
        # Ник идёт после порта: так порядок полей совпадает с подключением.
        label_nick = QLabel("Ваш ник")
        label_nick.setObjectName("start_hint")
        grid.addWidget(label_nick, 4, 0)
        grid.addWidget(self.join_nick, 5, 0, 1, 2)

        avatar_row = QHBoxLayout()
        avatar_row.setSpacing(8)
        self.join_avatar_btn = QPushButton("Выбрать аватар")
        self.join_avatar_btn.clicked.connect(
            lambda: self.pick_avatar(self.join_avatar_lbl))
        avatar_row.addWidget(self.join_avatar_btn)
        self.join_avatar_lbl = QLabel("не выбран")
        self.join_avatar_lbl.setObjectName("start_hint")
        avatar_row.addWidget(self.join_avatar_lbl, 1)
        label = QLabel("Аватар")
        label.setObjectName("start_hint")
        grid.addWidget(label, 6, 0)
        grid.addLayout(avatar_row, 7, 0, 1, 2)

        cl.addLayout(grid)
        self.join_as_dm = QCheckBox("Подключиться с правами DM")
        cl.addWidget(self.join_as_dm)

        b = QPushButton("Подключиться")
        b.setObjectName("primary_button")
        b.clicked.connect(self.do_join)
        cl.addWidget(b)
        l.addWidget(card)
        l.addStretch(1)
        return w

    # ------------------------------------------------------------------
    def fill_addresses(self):
        """Показать адреса хоста одним списком, Radmin — с пометкой."""
        ips = local_ipv4_list()
        lines = []
        for ip in ips:
            mark = "  ·  Radmin VPN" if ip.startswith("26.") else ""
            lines.append(f"{ip}{mark}")
        if hasattr(self, "addresses_label"):
            self.addresses_label.setText("\n".join(lines))

    # ------------------------------------------------------------------
    def graphics_tab(self):
        """Вкладка «Графика»: качество теней и отчёт о памяти.

        Настройки общие на всю машину, а не на игру: они лежат в
        settings.json, поэтому действуют и на мастерский стол, и на игрока.
        """
        w = QWidget()
        w.setObjectName("start_page")
        l = QVBoxLayout(w)
        l.setContentsMargins(12, 12, 12, 12)
        l.setSpacing(10)

        hint = QLabel(
            "Если стол лагает при десятках источников света и сотнях стен — "
            "снизьте качество теней. Это единственная настройка, которая "
            "влияет на плавность по-настоящему: картинка карты и токены "
            "рисуются почти бесплатно.")
        hint.setObjectName("start_hint")
        hint.setWordWrap(True)
        l.addWidget(hint)

        self.graphics_widget = GraphicsSettingsWidget(self.graphics_settings)
        l.addWidget(self.graphics_widget, 1)
        self.graphics_widget.changed.connect(self._remember_graphics)
        return w

    def _remember_graphics(self):
        """Запомнить выбор сразу: окно могут закрыть крестиком."""
        self.graphics_settings = self.graphics_widget.settings()
        return self.graphics_settings

    def pick_avatar(self, label: QLabel):
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите аватар", "",
            "Изображения (*.png *.jpg *.jpeg *.bmp *.gif *.webp)")
        if path:
            self.avatar_path = path
            label.setText(os.path.basename(path))

    def player_id_for(self, nickname: str) -> str:
        """Идентификатор игрока, постоянный для этого ника.

        Сервер узнаёт владельца токена по id. Пока id рождался заново на
        каждое подключение, после перезахода персонаж оставался за
        «старым» игроком, и сервер отклонял поворот токена и правку
        зрения («недостаточно прав»). Теперь id запоминается в настройках
        рядом с ником: перезашёл — тот же персонаж твой.
        """
        nick = str(nickname or "").strip().casefold()
        saved = str(self.saved_player_ids.get(nick) or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", saved):
            saved = generate_id()
        self.saved_player_ids[nick] = saved
        return saved

    def do_host(self):
        nick = self.host_nick.text().strip()
        if not nick:
            QMessageBox.warning(self, "Ошибка", "Введите ник")
            return
        self.connection_info = {
            'mode': 'host',
            'nickname': nick,
            'port': self.host_port.value(),
            'avatar_path': self.avatar_path,
            'player_id': self.player_id_for(nick),
            'enable_3d': self.enable_3d.isChecked(),
            'lighting_enabled': self.enable_lighting.isChecked(),
            'combat_effects': self.saved_combat_effects,
            'blood_transparency': self.saved_blood_transparency,
            'preload_my_actors': self.saved_preload_my_actors,
            'vision_mode': self.saved_vision_mode,
            'hide_dm_walls': self.saved_hide_dm_walls,
            'dm_fog': self.saved_dm_fog,
            'wasd_move': self.saved_wasd_move,
            'graphics': self.graphics_settings,
        }
        self.save_settings()
        self.accept()

    def do_join(self):
        nick = self.join_nick.text().strip()
        host = self.join_host.text().strip()
        if not nick:
            QMessageBox.warning(self, "Ошибка", "Введите ник")
            return
        if not host:
            QMessageBox.warning(self, "Ошибка", "Введите адрес хоста")
            return
        self.connection_info = {
            'mode': 'join',
            'nickname': nick,
            'host': host,
            'port': self.join_port.value(),
            'avatar_path': self.avatar_path,
            'player_id': self.player_id_for(nick),
            'role': 'dm' if self.join_as_dm.isChecked() else 'player',
            'enable_3d': self.enable_3d.isChecked(),
            'lighting_enabled': self.enable_lighting.isChecked(),
            'combat_effects': self.saved_combat_effects,
            'blood_transparency': self.saved_blood_transparency,
            'preload_my_actors': self.saved_preload_my_actors,
            'vision_mode': self.saved_vision_mode,
            'hide_dm_walls': self.saved_hide_dm_walls,
            'dm_fog': self.saved_dm_fog,
            'wasd_move': self.saved_wasd_move,
            'graphics': self.graphics_settings,
        }
        self.save_settings()
        self.accept()

    # ------------------------------------------------------------------
    def refresh_cache_label(self):
        """Подпись под кнопкой: сколько сейчас занимает кэш."""
        try:
            _files, size = asset_utils.cache_stats()
        except OSError:
            size = 0
        self.cache_label.setText(f"Кэш: {asset_utils.human_size(size)}")
        self.cache_btn.setEnabled(True)

    def clear_cache(self):
        """Удалить кэш картинок. Миры, листы и настройки не трогаем."""
        try:
            files, size = asset_utils.cache_stats()
        except OSError as exc:
            QMessageBox.warning(self, "Кэш", f"Не удалось прочитать кэш:\n{exc}")
            return
        if not files:
            QMessageBox.information(self, "Кэш пуст",
                                    "Удалять нечего: кэш уже пуст.")
            self.refresh_cache_label()
            return
        answer = QMessageBox.question(
            self, "Очистить кэш",
            f"Удалить {asset_utils.human_size(size)} кэша "
            f"({files} файл(ов))?\n\n"
            "Сохранения миров, листы персонажей и настройки останутся "
            "на месте. Картинки скачаются заново при следующем открытии.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        removed, freed = asset_utils.clear_cache()
        self.refresh_cache_label()
        QMessageBox.information(
            self, "Готово",
            f"Кэш очищен: удалено {removed} файл(ов), "
            f"освобождено {asset_utils.human_size(freed)}.")

    # ------------------------------------------------------------------
    def load_settings(self):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
            self.host_nick.setText(cfg.get("nick", ""))
            self.join_nick.setText(cfg.get("nick", ""))
            self.join_host.setText(cfg.get("last_host", ""))
            self.host_port.setValue(_default_port(cfg.get("port", 25565)))
            self.join_port.setValue(_default_port(cfg.get("port", 25565)))
            self.enable_3d.setChecked(bool(cfg.get("enable_3d", False)))
            self.enable_lighting.setChecked(
                bool(cfg.get("lighting_enabled", False)))
            self.saved_combat_effects = bool(cfg.get("combat_effects", True))
            self.saved_blood_transparency = int(
                cfg.get("blood_transparency", 0) or 0)
            self.saved_preload_my_actors = bool(
                cfg.get("preload_my_actors", False))
            self.saved_vision_mode = (
                "cone" if str(cfg.get("vision_mode", "around")).casefold()
                == "cone" else "around")
            self.saved_hide_dm_walls = bool(cfg.get("hide_dm_walls", False))
            self.saved_dm_fog = bool(cfg.get("dm_fog", False))
            self.saved_wasd_move = bool(cfg.get("wasd_move", False))
            self.graphics_settings = graphics_rules.normalize(
                cfg.get("graphics"))
            # Запомненные id: ник -> id. Мусор в файле не должен ломать вход.
            ids = cfg.get("player_ids")
            if isinstance(ids, dict):
                self.saved_player_ids = {
                    str(key).strip().casefold(): str(value).strip()
                    for key, value in ids.items() if str(value).strip()}
        except Exception:
            pass

    def save_settings(self):
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            # Читаем старый файл: в нём могут быть ключи настроек стола
            # («эффекты в бою»), которые стартовое окно не знает.
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    cfg = json.load(f)
            except (OSError, ValueError):
                cfg = {}
            if not isinstance(cfg, dict):
                cfg = {}
            cfg.update({
                "nick": (self.connection_info or {}).get("nickname", ""),
                "last_host": self.join_host.text().strip(),
                "port": (self.connection_info or {}).get("port", 25565),
                "enable_3d": self.enable_3d.isChecked(),
                "lighting_enabled": self.enable_lighting.isChecked(),
                "combat_effects": self.saved_combat_effects,
                "blood_transparency": self.saved_blood_transparency,
                "preload_my_actors": self.saved_preload_my_actors,
                "vision_mode": self.saved_vision_mode,
                "hide_dm_walls": self.saved_hide_dm_walls,
                "dm_fog": self.saved_dm_fog,
                "wasd_move": self.saved_wasd_move,
                "graphics": (self._remember_graphics()
                             if getattr(self, "graphics_widget", None) else
                             self.graphics_settings),
                "player_ids": dict(self.saved_player_ids),
            })
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
