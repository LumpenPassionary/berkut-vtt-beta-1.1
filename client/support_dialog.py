"""Окно «Поддержать»: QR для перевода и контакты для обратной связи.

Проект бесплатный, поэтому поддержка — только по желанию. Здесь нет ни
сети, ни состояния игры: окно читает статическую картинку QR и текст.
Ссылки открываются системным браузером, а весь текст можно выделить и
скопировать — для этого используется QTextBrowser, а не QLabel.
"""
import os

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon, QPixmap
from PyQt5.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel,
                             QPushButton, QTextBrowser, QVBoxLayout)

from client import app_icon

#: Реквизиты и ссылки. Почту и Telegram показываем текстом, чтобы их можно
#: было не только открыть, но и скопировать вручную.
BOOSTY_URL = "https://boosty.to/berkut_vtt_dnd5e"
EMAIL = "basilenkov1va@gmail.com"
TELEGRAM_URL = "https://web.telegram.org/k/#@DonesChronicles"

ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
QR_PATH = os.path.join(ASSET_DIR, "support_qr.png")
COIN_PATH = os.path.join(ASSET_DIR, "icons", "support_coin.png")


def support_icon() -> QIcon:
    """Монетка для кнопки «Поддержать» (в рейке и стартовом окне)."""
    if os.path.isfile(COIN_PATH):
        return QIcon(COIN_PATH)
    return QIcon()


def body_html() -> str:
    """Основной текст окна. Ссылки настоящие, поэтому кликабельны."""
    return f"""
    <p class="lead">Проект распространяется бесплатно. Если вы хотите
    поддержать его, пожалуйста, наведите камеру телефона на QR-код либо
    отсканируйте его в приложении банка. Можно и оформить подписку на
    Boosty:</p>
    <p class="link-line"><a href="{BOOSTY_URL}">{BOOSTY_URL}</a></p>
    <p>Это поможет мне и дальше обновлять программу, добавить сетевую игру
    через интернет и расширять возможности виртуального стола…</p>
    <p class="thanks">Спасибо, что пользуетесь Berkut VTT!</p>
    """


def contacts_html() -> str:
    """Отдельная строка с обратной связью и новостями."""
    return f"""
    <p>Сообщить о баге, пожеланиях и идеях можно здесь, обратная связь:
    <a href="mailto:{EMAIL}">{EMAIL}</a></p>
    <p>Новости об обновлениях:
    <a href="{TELEGRAM_URL}">{TELEGRAM_URL}</a></p>
    """


_DARK_QSS = """
QDialog { background-color: #1a1a1c; color: #d8d6d2; }
QLabel { background: transparent; color: #d8d6d2; }
QLabel#support_title {
    color: #f0ece4; font-family: 'DejaVu Serif', Georgia, serif;
    font-size: 19pt; font-weight: 700;
}
QLabel#support_sub { color: #8d8a84; font-size: 10pt; }
QFrame#support_card {
    background: #202023; border: 1px solid #2c2c30; border-radius: 10px;
}
QFrame#support_qr_card {
    background: #ffffff; border: 1px solid #cbb47a; border-radius: 10px;
}
QLabel#support_qr_caption { color: #5e5347; font-size: 9pt; }
QFrame#support_contacts {
    background: #202023; border: 1px solid #2c2c30; border-radius: 8px;
}
QLabel#support_contacts_title {
    color: #b08d4f; font-size: 9pt; font-weight: 600; letter-spacing: 1px;
}
QTextBrowser {
    background: transparent; border: none; color: #d8d6d2;
    selection-background-color: #b08d4f; selection-color: #17130b;
}
QPushButton {
    background: #26262a; border: 1px solid #3a3a40; border-radius: 5px;
    padding: 7px 14px; color: #e4e2dc;
}
QPushButton:hover { background: #33343a; border-color: #4b4b52; }
QPushButton#support_close {
    background: #b08d4f; border: 1px solid #c8a45e; color: #17130b;
    font-weight: 600; padding: 8px 18px;
}
QPushButton#support_close:hover { background: #c8a45e; }
"""

_LIGHT_QSS = """
QDialog { background-color: #f4f1ea; color: #2c2a28; }
QLabel { background: transparent; color: #2c2a28; }
QLabel#support_title {
    color: #262421; font-family: 'DejaVu Serif', Georgia, serif;
    font-size: 19pt; font-weight: 700;
}
QLabel#support_sub { color: #6f6d66; font-size: 10pt; }
QFrame#support_card {
    background: #fffdf8; border: 1px solid #ddd6c8; border-radius: 10px;
}
QFrame#support_qr_card {
    background: #ffffff; border: 1px solid #bfa670; border-radius: 10px;
}
QLabel#support_qr_caption { color: #5e5347; font-size: 9pt; }
QFrame#support_contacts {
    background: #fffdf8; border: 1px solid #ddd6c8; border-radius: 8px;
}
QLabel#support_contacts_title {
    color: #85662a; font-size: 9pt; font-weight: 600; letter-spacing: 1px;
}
QTextBrowser {
    background: transparent; border: none; color: #2c2a28;
    selection-background-color: #c8a45e; selection-color: #17130b;
}
QPushButton {
    background: #fffaf0; border: 1px solid #d4c9b4; border-radius: 5px;
    padding: 7px 14px; color: #2c2a28;
}
QPushButton:hover { background: #f2ead9; border-color: #b08d4f; }
QPushButton#support_close {
    background: #b08d4f; border: 1px solid #9b7838; color: #17130b;
    font-weight: 600; padding: 8px 18px;
}
QPushButton#support_close:hover { background: #c8a45e; }
"""


class SupportDialog(QDialog):
    """Окно поддержки: текст, QR и контакты.

    Окно одинаково работает из стартового экрана и из стола. Тему ему
    передаёт родитель: стартовый экран всегда тёмный, стол — по настройке.
    Из стола оно немодальное, на стартовом экране открывается через exec_().
    """

    def __init__(self, parent=None, theme="dark"):
        super().__init__(parent)
        self.setWindowTitle("Поддержать — Berkut VTT")
        self.setWindowIcon(app_icon())
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setMinimumSize(820, 600)
        self.resize(960, 660)
        self._theme = "light" if theme == "light" else "dark"
        self.build_ui()
        self.set_theme(self._theme)

    # ------------------------------------------------------------------
    def build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)

        # Шапка: монетка, название и короткое пояснение.
        head = QHBoxLayout()
        head.setSpacing(12)
        coin = QLabel()
        coin.setFixedSize(54, 54)
        coin.setPixmap(support_icon().pixmap(54, 54))
        coin.setStyleSheet("background: transparent;")
        head.addWidget(coin, 0, Qt.AlignTop)

        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("Поддержать")
        title.setObjectName("support_title")
        titles.addWidget(title)
        sub = QLabel("Berkut VTT dnd5e — виртуальный стол, который "
                     "остаётся бесплатным")
        sub.setObjectName("support_sub")
        sub.setWordWrap(True)
        titles.addWidget(sub)
        head.addLayout(titles, 1)
        root.addLayout(head)

        body_row = QHBoxLayout()
        body_row.setSpacing(12)
        root.addLayout(body_row, 1)

        card = QFrame()
        card.setObjectName("support_card")
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(18, 16, 18, 14)
        card_lay.setSpacing(8)

        self.body = QTextBrowser()
        self.body.setObjectName("support_body")
        self.body.setFrameShape(QFrame.NoFrame)
        self.body.setOpenExternalLinks(True)
        # Ссылки открываются в браузере, но текст остаётся выделяемым:
        # иначе адрес нельзя было бы скопировать вручную.
        self.body.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
            | Qt.LinksAccessibleByMouse | Qt.LinksAccessibleByKeyboard)
        self.body.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body.setHtml(body_html())
        card_lay.addWidget(self.body, 1)
        body_row.addWidget(card, 1)

        # QR — на белой подложке: на тёмной карточке код не считается.
        qr_card = QFrame()
        qr_card.setObjectName("support_qr_card")
        qr_card.setFixedWidth(326)
        qr_lay = QVBoxLayout(qr_card)
        qr_lay.setContentsMargins(18, 16, 18, 16)
        qr_lay.setSpacing(10)

        self.qr_label = QLabel()
        self.qr_label.setObjectName("support_qr")
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setFixedSize(288, 288)
        self._qr_source = QPixmap(QR_PATH)
        if not self._qr_source.isNull():
            # Рисуем в 2× и уменьшаем логически до 288: на экране с
            # масштабом 125–200 % код остаётся резким, а не растянутым.
            qr_pix = self._qr_source.scaled(
                576, 576, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            qr_pix.setDevicePixelRatio(2.0)
            self.qr_label.setPixmap(qr_pix)
        else:
            self.qr_label.setText("QR-код не найден")
        qr_lay.addWidget(self.qr_label, 0, Qt.AlignHCenter)

        caption = QLabel("Наведите камеру телефона или отсканируйте "
                         "код в приложении банка")
        caption.setObjectName("support_qr_caption")
        caption.setWordWrap(True)
        caption.setAlignment(Qt.AlignCenter)
        qr_lay.addWidget(caption)
        body_row.addWidget(qr_card, 0, Qt.AlignTop)

        # Обратная связь — отдельной строкой, чтобы не терялась в абзаце.
        contacts = QFrame()
        contacts.setObjectName("support_contacts")
        contacts_lay = QVBoxLayout(contacts)
        contacts_lay.setContentsMargins(14, 10, 14, 10)
        contacts_lay.setSpacing(3)
        contacts_title = QLabel("ОБРАТНАЯ СВЯЗЬ И НОВОСТИ")
        contacts_title.setObjectName("support_contacts_title")
        contacts_lay.addWidget(contacts_title)

        self.contacts = QTextBrowser()
        self.contacts.setObjectName("support_contacts_text")
        self.contacts.setFrameShape(QFrame.NoFrame)
        self.contacts.setOpenExternalLinks(True)
        self.contacts.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
            | Qt.LinksAccessibleByMouse | Qt.LinksAccessibleByKeyboard)
        self.contacts.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.contacts.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.contacts.setFixedHeight(74)
        self.contacts.setHtml(contacts_html())
        contacts_lay.addWidget(self.contacts)
        root.addWidget(contacts)

        footer = QHBoxLayout()
        footer.addStretch(1)
        close_btn = QPushButton("Закрыть")
        close_btn.setObjectName("support_close")
        close_btn.clicked.connect(self.close)
        footer.addWidget(close_btn)
        root.addLayout(footer)

    # ------------------------------------------------------------------
    def set_theme(self, theme):
        """Перекрасить окно под тему стола (или тёмный стартовый экран)."""
        self._theme = "light" if theme == "light" else "dark"
        if self._theme == "light":
            colors = {"accent": "#85662a", "text": "#2c2a28",
                      "muted": "#6f6d66"}
            self.setStyleSheet(_LIGHT_QSS)
        else:
            colors = {"accent": "#d6b877", "text": "#ddd9d1",
                      "muted": "#9a968e"}
            self.setStyleSheet(_DARK_QSS)
        style = f"""
            body {{ color: {colors['text']}; font-size: 11pt; }}
            p {{ margin: 0 0 10px 0; }}
            p.lead {{ font-size: 11.5pt; }}
            p.link-line {{ margin-top: -4px; }}
            p.thanks {{ color: {colors['accent']}; font-size: 12pt;
                        font-weight: 600; margin-top: 14px; }}
            a {{ color: {colors['accent']}; text-decoration: underline; }}
            p.muted {{ color: {colors['muted']}; }}
        """
        self.body.document().setDefaultStyleSheet(style)
        self.contacts.document().setDefaultStyleSheet(style)
        # setHtml повторно применяет таблицу стилей к уже загруженному HTML.
        self.body.setHtml(body_html())
        self.contacts.setHtml(contacts_html())


def open_support(parent, theme="dark"):
    """Открыть одно окно поддержки на родителя (без копий при повторе).

    Возвращает диалог. Для стартового окна вызывающий может использовать
    ``exec_()``, чтобы окно было модальным поверх уже модального старта.
    """
    dialog = getattr(parent, "_support_dialog", None)
    if dialog is None:
        dialog = SupportDialog(parent, theme=theme)
        dialog.setAttribute(Qt.WA_DeleteOnClose, True)
        dialog.finished.connect(
            lambda _=0: setattr(parent, "_support_dialog", None))
        parent._support_dialog = dialog
    else:
        dialog.set_theme(theme)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
