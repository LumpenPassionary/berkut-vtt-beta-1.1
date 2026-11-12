"""Окно подгонки портрета под рамку токена.

Открывается после выбора картинки: мастер видит токен крупно, точно таким,
каким он будет на карте (рисует его та же функция, что и холст —
``client.token_frames.draw_portrait``), двигает портрет мышью, меняет
увеличение колесом или ползунком и выбирает рамку справа.

Что здесь важно:

  * всё, что выходит за отверстие рамки, обрезается — поэтому в окне видно
    ровно то, что останется на карте;
  * рамки не прописаны в коде: список строится по папке
    ``client/assets/frames``, а «Без рамки» — отдельный вариант;
  * у окна нет системной белой полосы: свой заголовок с бронзовым названием,
    скруглённые углы и тон в цвет панелей приложения.
"""
import os

from PyQt5.QtCore import QPoint, QRectF, QSize, Qt, pyqtSignal
from PyQt5.QtGui import (QColor, QIcon, QImage, QPainter, QPen, QPixmap,
                         QRadialGradient)
from PyQt5.QtWidgets import (QDialog, QFrame, QGraphicsDropShadowEffect,
                             QHBoxLayout, QLabel, QLineEdit, QListView,
                             QListWidget, QListWidgetItem, QPushButton,
                             QSizePolicy, QSlider, QVBoxLayout, QWidget)

from shared.models import (PORTRAIT_ZOOM_MAX, PORTRAIT_ZOOM_MIN,
                           normalize_portrait_zoom)
from client import app_icon, token_frames
from client.win_frame import is_dark_theme

#: Ключ варианта «рамки нет» в списке выбора.
NO_FRAME = token_frames.NO_FRAME

#: Внутренний отступ вокруг токена в окне предпросмотра.
PREVIEW_PAD = 20
#: С какого размера картинка уменьшается для предпросмотра: рамка и портрет
#: рисуются из одного файла, но масштабировать 4000 px на каждый кадр незачем.
WORK_IMAGE_MAX = 1024
#: Размер значка рамки в списке.
THUMB_PX = 74


class PortraitPreview(QWidget):
    """Крупный токен в рамке — тот же рендер, что на карте.

    Мышь двигает портрет, колесо меняет увеличение. Ничего не считается
    заново «для предпросмотра»: окно передаёт те же увеличение и сдвиг,
    которые потом уедут на токен и на карту.
    """

    zoomChanged = pyqtSignal(float)
    offsetsChanged = pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("portrait_preview")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.OpenHandCursor)
        self._image = QPixmap()
        self._frame = token_frames.resolve("")
        self._zoom = 1.0
        self._dx = 0.0
        self._dy = 0.0
        self._drag_from = None
        self._drag_offsets = (0.0, 0.0)

    # --- состояние ------------------------------------------------------
    def set_image(self, image):
        if isinstance(image, QImage):
            image = QPixmap.fromImage(image)
        self._image = image if image is not None else QPixmap()
        self._dx = self._dy = 0.0
        self._zoom = 1.0
        self.update()

    def image(self):
        return self._image

    def frame(self):
        return self._frame

    def set_frame(self, key):
        self._frame = token_frames.resolve(key)
        self.update()

    def zoom(self):
        return self._zoom

    def set_zoom(self, value, notify=False):
        zoom = normalize_portrait_zoom(value)
        if abs(zoom - self._zoom) < 1e-6:
            return
        self._zoom = zoom
        self._clamp()
        self.update()
        if notify:
            self.zoomChanged.emit(self._zoom)

    def offsets(self):
        return self._dx, self._dy

    def set_offsets(self, dx, dy, notify=False):
        dx = float(dx or 0.0)
        dy = float(dy or 0.0)
        if abs(dx - self._dx) < 1e-6 and abs(dy - self._dy) < 1e-6:
            return
        self._dx, self._dy = dx, dy
        self._clamp()
        self.update()
        if notify:
            self.offsetsChanged.emit(self._dx, self._dy)

    def reset(self):
        """Сброс подгонки: портрет заполняет отверстие по центру."""
        self._zoom = 1.0
        self._dx = self._dy = 0.0
        self.update()
        self.zoomChanged.emit(self._zoom)
        self.offsetsChanged.emit(self._dx, self._dy)

    # --- геометрия ------------------------------------------------------
    def token_rect(self) -> QRectF:
        side = max(8.0, float(min(self.width(), self.height())
                              - 2 * PREVIEW_PAD))
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        return QRectF(cx - side / 2.0, cy - side / 2.0, side, side)

    def _clamp(self):
        if self._image.isNull():
            return
        rect = self.token_rect()
        hole = token_frames.hole_ratio(self._frame)
        self._dx, self._dy = token_frames.clamp_offsets(
            rect, self._image.width(), self._image.height(), hole=hole,
            zoom=self._zoom, dx=self._dx, dy=self._dy)

    # --- рисование ------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        rect = self.token_rect()
        if not self._image.isNull():
            # Мягкий ореол вокруг токена: тёмная картинка не сливается с
            # фоном окна, а край рамки читается.
            glow = QRadialGradient(rect.center(), rect.width() * 0.85)
            glow.setColorAt(0.0, QColor(214, 184, 119, 26))
            glow.setColorAt(0.72, QColor(0, 0, 0, 0))
            painter.setPen(Qt.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(rect.adjusted(-6, -6, 6, 6))
            token_frames.draw_portrait(
                painter, self._image, rect, frame=self._frame,
                zoom=self._zoom, dx=self._dx, dy=self._dy)
        painter.end()

    def render_token(self, size: int) -> QImage:
        """Только токен без фона — для проверок и значков."""
        image = QImage(max(8, int(size)), max(8, int(size)),
                       QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        token_frames.draw_portrait(
            painter, self._image, QRectF(0, 0, image.width(), image.height()),
            frame=self._frame, zoom=self._zoom, dx=self._dx, dy=self._dy)
        painter.end()
        return image

    # --- мышь -----------------------------------------------------------
    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120.0
        if not steps:
            return
        self.set_zoom(self._zoom * (1.12 ** steps), notify=True)
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self._image.isNull():
            self._drag_from = event.pos()
            self._drag_offsets = (self._dx, self._dy)
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_from is None:
            return
        delta = event.pos() - self._drag_from
        side = max(1.0, self.token_rect().width())
        self.set_offsets(self._drag_offsets[0] + delta.x() / side,
                         self._drag_offsets[1] + delta.y() / side,
                         notify=True)
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag_from is not None:
            self._drag_from = None
            self.setCursor(Qt.OpenHandCursor)
            event.accept()


class _TitleBar(QWidget):
    """Свой заголовок: без белой системной полосы, тащится мышью."""

    def __init__(self, title: str, subtitle: str, parent=None):
        super().__init__(parent)
        self.setObjectName("portrait_header")
        self.setFixedHeight(46)
        self._drag_from = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 8, 10, 8)
        lay.setSpacing(10)
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(0)
        name = QLabel(title)
        name.setObjectName("portrait_title")
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("portrait_subtitle")
        text.addWidget(name)
        text.addWidget(self.subtitle)
        lay.addLayout(text, 1)
        self.close_button = QPushButton("✕")
        self.close_button.setObjectName("portrait_close")
        self.close_button.setFixedSize(28, 28)
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.setToolTip("Закрыть")
        lay.addWidget(self.close_button)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_from = event.globalPos() - self.window().frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_from is not None and event.buttons() & Qt.LeftButton:
            self.window().move(event.globalPos() - self._drag_from)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_from = None


def _theme_colors(dark: bool) -> dict:
    """Цвета окна в тон текущей темы приложения."""
    if dark:
        return {
            "root": "#19191c", "edge": "#34343a", "header_line": "#2a2a2e",
            "title": "#c9a35f", "subtitle": "#7d7a72", "text": "#e5e1d8",
            "muted": "#8c8c94", "field": "#202023", "field_edge": "#34343a",
            "pool": "#131315", "hover": "#202024", "sel": "#2a2822",
            "accent": "#b08d4f", "preview": "#121214",
        }
    return {
        "root": "#f6f4ef", "edge": "#cfcdc6", "header_line": "#e0ded7",
        "title": "#8a6a2e", "subtitle": "#7d7a72", "text": "#2a2a2c",
        "muted": "#75726b", "field": "#ffffff", "field_edge": "#cfcdc6",
        "pool": "#efece5", "hover": "#e8e5dd", "sel": "#efe7d5",
        "accent": "#a98a4a", "preview": "#e8e6e0",
    }


def _dialog_qss(dark: bool) -> str:
    c = _theme_colors(dark)
    return f"""
#portrait_root {{
    background-color: {c['root']};
    border: 1px solid {c['edge']};
    border-radius: 10px;
}}
#portrait_header {{ background: transparent; border-bottom: 1px solid {c['header_line']}; }}
#portrait_title {{
    color: {c['title']};
    font-family: "Georgia", "Cambria", "DejaVu Serif", serif;
    font-size: 13pt;
    letter-spacing: 1.2px;
}}
#portrait_subtitle {{ color: {c['subtitle']}; font-size: 9pt; }}
#portrait_close {{
    background: transparent; border: none; color: {c['muted']};
    font-size: 12pt; font-weight: 600; padding: 0; border-radius: 6px;
}}
#portrait_close:hover {{ color: {c['text']}; background: {c['hover']}; }}
#portrait_preview {{
    background-color: {c['preview']};
    border: 1px solid {c['header_line']};
    border-radius: 8px;
}}
#portrait_label {{ color: {c['muted']}; font-size: 9pt; letter-spacing: 0.6px; }}
#portrait_hint {{ color: {c['muted']}; font-size: 9pt; }}
#portrait_zoom_value {{ color: {c['muted']}; font-size: 9pt; }}
QLineEdit#portrait_name {{
    background-color: {c['field']};
    border: 1px solid {c['field_edge']};
    border-radius: 6px;
    padding: 7px 9px;
    color: {c['text']};
}}
QLineEdit#portrait_name:focus {{ border-color: {c['accent']}; }}
QListWidget#frame_pool {{
    background-color: {c['pool']};
    border: 1px solid {c['header_line']};
    border-radius: 8px;
}}
QListWidget#frame_pool::item {{
    color: {c['text']};
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 2px;
}}
QListWidget#frame_pool::item:hover {{ background-color: {c['hover']}; }}
QListWidget#frame_pool::item:selected {{
    background-color: {c['sel']};
    border-color: {c['accent']};
    color: {c['text']};
}}
"""


def _work_image(path: str) -> QPixmap:
    """Картинка для предпросмотра: та же, но не гигантская.

    Геометрия от уменьшения не меняется (пропорции те же), а рамка и
    значки списка рисуются за миллисекунды, а не за десятые доли секунды.
    """
    image = QImage(path)
    if image.isNull():
        return QPixmap()
    if max(image.width(), image.height()) > WORK_IMAGE_MAX:
        image = image.scaled(WORK_IMAGE_MAX, WORK_IMAGE_MAX, Qt.KeepAspectRatio,
                             Qt.SmoothTransformation)
    return QPixmap.fromImage(image)


class TokenPortraitDialog(QDialog):
    """Подгонка портрета и выбор рамки перед добавлением токена."""

    def __init__(self, parent, image_path: str, default_name: str = "",
                 frame=None):
        super().__init__(parent)
        self.setWindowTitle("Портрет токена")
        self.setWindowIcon(app_icon())
        # Свой заголовок вместо системного: белая полоса Windows над тёмным
        # интерфейсом выглядела чужой.
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet(_dialog_qss(is_dark_theme(parent)))
        self._image = _work_image(image_path)
        self._center_once = True
        # Рамка, выбранная в прошлый раз (актёр уже носит свою): окно
        # открывается на ней, а не сбрасывает выбор на стандартную.
        self._initial_frame = token_frames.resolve(frame or "")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        self.root = QFrame(self)
        self.root.setObjectName("portrait_root")
        outer.addWidget(self.root)

        shadow = QGraphicsDropShadowEffect(self.root)
        shadow.setBlurRadius(34)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 0, 0, 190))
        self.root.setGraphicsEffect(shadow)

        root_lay = QVBoxLayout(self.root)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        title = "Портрет токена"
        subtitle = os.path.basename(image_path)
        self.header = _TitleBar(title, subtitle, self.root)
        self.header.close_button.clicked.connect(self.reject)
        root_lay.addWidget(self.header)

        body = QHBoxLayout()
        body.setContentsMargins(16, 14, 16, 8)
        body.setSpacing(16)
        root_lay.addLayout(body, 1)

        # --- левая колонка: крупный токен и подгонка ---------------------
        left = QVBoxLayout()
        left.setSpacing(10)
        self.preview = PortraitPreview(self.root)
        self.preview.set_image(self._image)
        self.preview.zoomChanged.connect(self._on_preview_zoom)
        self.preview.offsetsChanged.connect(self._on_preview_offsets)
        left.addWidget(self.preview, 1)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.zoom_out = QPushButton("−")
        self.zoom_out.setFixedWidth(30)
        self.zoom_out.setToolTip("Уменьшить картинку")
        self.zoom_out.clicked.connect(lambda: self._step_zoom(-0.1))
        self.zoom_in = QPushButton("+")
        self.zoom_in.setFixedWidth(30)
        self.zoom_in.setToolTip("Увеличить картинку")
        self.zoom_in.clicked.connect(lambda: self._step_zoom(0.1))
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setRange(int(round(PORTRAIT_ZOOM_MIN * 100)),
                                  int(round(PORTRAIT_ZOOM_MAX * 100)))
        self.zoom_slider.setValue(100)
        self.zoom_slider.setToolTip("Увеличение портрета")
        self.zoom_slider.valueChanged.connect(self._on_slider)
        self.zoom_value = QLabel("100 %")
        self.zoom_value.setObjectName("portrait_zoom_value")
        self.zoom_value.setFixedWidth(48)
        self.reset_button = QPushButton("Сбросить")
        self.reset_button.clicked.connect(self._reset)
        controls.addWidget(self.zoom_out)
        controls.addWidget(self.zoom_slider, 1)
        controls.addWidget(self.zoom_in)
        controls.addWidget(self.zoom_value)
        controls.addWidget(self.reset_button)
        left.addLayout(controls)

        hint = QLabel("Двигайте картинку мышью, колесо или ползунок — "
                      "увеличение. Всё, что выходит за отверстие, обрежется.")
        hint.setObjectName("portrait_hint")
        hint.setWordWrap(True)
        left.addWidget(hint)
        body.addLayout(left, 1)

        # --- правая колонка: имя и рамки ---------------------------------
        right = QVBoxLayout()
        right.setSpacing(6)
        right.setContentsMargins(0, 0, 0, 0)

        name_label = QLabel("ИМЯ ТОКЕНА")
        name_label.setObjectName("portrait_label")
        right.addWidget(name_label)
        self.name_edit = QLineEdit(str(default_name or ""))
        self.name_edit.setObjectName("portrait_name")
        self.name_edit.setPlaceholderText("Как назвать токен?")
        self.name_edit.textChanged.connect(lambda _: self._refresh_accept())
        self.name_edit.returnPressed.connect(self._accept_if_possible)
        right.addWidget(self.name_edit)

        frame_label = QLabel("РАМКА")
        frame_label.setObjectName("portrait_label")
        right.addSpacing(8)
        right.addWidget(frame_label)
        self.frame_pool = QListWidget()
        self.frame_pool.setObjectName("frame_pool")
        self.frame_pool.setViewMode(QListView.IconMode)
        self.frame_pool.setFlow(QListView.LeftToRight)
        self.frame_pool.setWrapping(True)
        self.frame_pool.setResizeMode(QListView.Adjust)
        self.frame_pool.setMovement(QListView.Static)
        self.frame_pool.setIconSize(QSize(THUMB_PX, THUMB_PX))
        self.frame_pool.setGridSize(QSize(THUMB_PX + 22, THUMB_PX + 34))
        self.frame_pool.setSpacing(1)
        self.frame_pool.setUniformItemSizes(True)
        self.frame_pool.setWordWrap(True)
        self.frame_pool.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.frame_pool.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.frame_pool.setMinimumWidth(240)
        self.frame_pool.setMaximumWidth(260)
        self.frame_pool.currentItemChanged.connect(self._on_frame_changed)
        right.addWidget(self.frame_pool, 1)
        self._build_frame_pool()
        body.addLayout(right)

        # --- нижняя строка -------------------------------------------------
        footer = QHBoxLayout()
        footer.setContentsMargins(16, 4, 16, 14)
        footer.setSpacing(10)
        self.name_hint = QLabel("")
        self.name_hint.setObjectName("portrait_hint")
        footer.addWidget(self.name_hint, 1)
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.clicked.connect(self.reject)
        self.add_button = QPushButton("Добавить токен")
        self.add_button.setDefault(True)
        self.add_button.clicked.connect(self._accept_if_possible)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.add_button)
        root_lay.addLayout(footer)

        self.name_edit.selectAll()
        self._refresh_accept()
        self.resize(900, 640)

    # --- список рамок ---------------------------------------------------
    def _thumb(self, key: str) -> QPixmap:
        """Значок варианта: выбранная рамка поверх этого же портрета.

        Так в списке видно не «бронзовое кольцо вообще», а как будет
        выглядеть именно этот токен.
        """
        size = THUMB_PX
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        rect = QRectF(1, 1, size - 2, size - 2)
        if key == NO_FRAME:
            if not self._image.isNull():
                painter.setOpacity(0.92)
                token_frames.draw_portrait(painter, self._image, rect,
                                           frame=NO_FRAME)
                painter.setOpacity(1.0)
            painter.setPen(QPen(QColor(120, 116, 108), 1.0, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(rect.adjusted(1, 1, -1, -1))
        else:
            token_frames.draw_portrait(painter, self._image, rect, frame=key)
        painter.end()
        return pixmap

    def _build_frame_pool(self):
        self.frame_pool.clear()
        entries = [(NO_FRAME, "Без рамки")]
        entries += [(spec.key, spec.title) for spec in token_frames.available_frames()]
        selected = None
        for key, title in entries:
            item = QListWidgetItem(QIcon(self._thumb(key)), title)
            item.setData(Qt.UserRole, key)
            item.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
            item.setToolTip(title)
            self.frame_pool.addItem(item)
            if key == self._initial_frame:
                selected = item
        if selected is not None:
            self.frame_pool.setCurrentItem(selected)
        elif self.frame_pool.count():
            self.frame_pool.setCurrentRow(0)

    def _on_frame_changed(self, current, _previous=None):
        if current is None:
            return
        self.preview.set_frame(current.data(Qt.UserRole))

    # --- управление -----------------------------------------------------
    def _on_slider(self, value):
        self.preview.set_zoom(value / 100.0)

    def _on_preview_zoom(self, zoom):
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(int(round(zoom * 100)))
        self.zoom_slider.blockSignals(False)
        self.zoom_value.setText(f"{int(round(zoom * 100))} %")

    def _on_preview_offsets(self, _dx, _dy):
        self._refresh_accept()

    def _step_zoom(self, delta):
        self.preview.set_zoom(self.preview.zoom() + delta)

    def _reset(self):
        self.preview.reset()

    def _refresh_accept(self):
        ready = bool(self.name_edit.text().strip())
        self.add_button.setEnabled(ready)
        self.name_hint.setText("" if ready or not self._image.isNull()
                               else "Картинка не открылась")

    def _accept_if_possible(self):
        if not self.name_edit.text().strip():
            self.name_hint.setText("Дайте токену имя")
            self.name_edit.setFocus()
            return
        self.accept()

    # --- результат ------------------------------------------------------
    def token_name(self) -> str:
        return self.name_edit.text().strip()

    def frame_key(self) -> str:
        return self.preview.frame()

    def zoom(self) -> float:
        return self.preview.zoom()

    def offsets(self):
        return self.preview.offsets()

    # --- окно -----------------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        if self._center_once:
            self._center_once = False
            parent = self.parentWidget()
            if parent is not None:
                geo = parent.frameGeometry()
                self.move(geo.center() - QPoint(self.width() // 2,
                                                self.height() // 2))
