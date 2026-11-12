"""Заголовок окна в стиле Windows 10/11 — тёмный, под цвет приложения.

Qt отвечает только за содержимое окна: полосу заголовка и рамку рисует
Windows, и по умолчанию они светлые. Над тёмным интерфейсом из-за этого
оставалась белая полоска — окно выглядело сырым. Здесь заголовок красится
средствами DWM: тёмный режим, цвет шапки и рамки в тон панелям приложения,
скруглённые углы Windows 11.

Всё сделано на ctypes и мягко деградирует: на не-Windows, в сборках без
DWM или на старых версиях Windows функция просто ничего не делает, а
приложение продолжает работать как раньше.
"""
import ctypes
import sys

# Значения из dwmapi.h. Атрибуты с 20-го появились в Windows 10 1903,
# цвет шапки и скруглённые углы — в Windows 11; на старых системах вызов
# просто вернёт ошибку, и это не должно ломать запуск.
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36

DWMWCP_ROUND = 2

#: Цвета под тему приложения: шапка и рамка в тон панелям, текст — как в QSS.
DARK_TITLE = {
    "caption": "#1a1a1c",   # фон окна и панелей
    "border": "#2c2c30",
    "text": "#d8d6d2",
}
LIGHT_TITLE = {
    "caption": "#ecebe7",
    "border": "#d0cec7",
    "text": "#2a2a2c",
}


def _colorref(value: str) -> int:
    """'#rrggbb' -> COLORREF (0x00BBGGRR), как его ждёт DWM."""
    text = str(value).lstrip("#")
    try:
        red = int(text[0:2], 16)
        green = int(text[2:4], 16)
        blue = int(text[4:6], 16)
    except (ValueError, IndexError):
        return 0
    return red | (green << 8) | (blue << 16)


def _dwm():
    """Dwmapi или None, если система не Windows."""
    if sys.platform != "win32":
        return None
    try:
        return ctypes.windll.dwmapi
    except (AttributeError, OSError):
        return None


def _set_attr(dwm, hwnd: int, attribute: int, value: int) -> bool:
    """Записать целочисленный атрибут окна; False — система его не знает."""
    data = ctypes.c_int(value)
    try:
        result = dwm.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), ctypes.c_uint(attribute),
            ctypes.byref(data), ctypes.sizeof(data))
    except (AttributeError, OSError):
        return False
    return result == 0


def apply_window_frame(window, dark: bool = True) -> bool:
    """Покрасить заголовок окна под приложение. True — получилось.

    ``dark`` берётся из темы: у главного окна это ``current_theme``, у
    диалогов по умолчанию тёмная — интерфейс приложения тёмный.
    """
    dwm = _dwm()
    if dwm is None or window is None:
        return False
    try:
        hwnd = int(window.winId())
    except (AttributeError, TypeError, ValueError):
        return False
    if not hwnd:
        return False

    colors = DARK_TITLE if dark else LIGHT_TITLE
    applied = False
    # Тёмный режим: сначала новый атрибут, потом старый (до 1903).
    for attribute in (DWMWA_USE_IMMERSIVE_DARK_MODE,
                      DWMWA_USE_IMMERSIVE_DARK_MODE_OLD):
        if _set_attr(dwm, hwnd, attribute, 1 if dark else 0):
            applied = True
            break
    # Цвет шапки, рамки и текста — только Windows 11. На десятке эти
    # вызовы тихо не срабатывают, но тёмный режим уже применён.
    if _set_attr(dwm, hwnd, DWMWA_CAPTION_COLOR,
                 _colorref(colors["caption"])):
        applied = True
    _set_attr(dwm, hwnd, DWMWA_BORDER_COLOR, _colorref(colors["border"]))
    _set_attr(dwm, hwnd, DWMWA_TEXT_COLOR, _colorref(colors["text"]))
    # Скруглённые углы Windows 11: приложение выглядит как системное.
    _set_attr(dwm, hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
    return applied


def is_dark_theme(widget) -> bool:
    """Тёмная ли тема у окна: у главного окна она хранится в атрибуте."""
    return getattr(widget, "current_theme", "dark") != "light"


#: Виджеты, которые окном быть не могут: подпись, рамка, поле. Если такой
#: виджет показывают, ещё не дав ему родителя, Qt делает его отдельным
#: окном — на холсте мигает пустая рамка, а потом остаётся сама по себе.
#: Именно так появлялись «микроокна» при бросках, отдыхе и лечении.
_LEAF_WIDGETS = (
    "QLabel", "QFrame", "QPushButton", "QToolButton", "QCheckBox",
    "QRadioButton", "QLineEdit", "QComboBox", "QSpinBox", "QScrollArea",
    "QPlainTextEdit", "QTextEdit", "QListWidget", "QProgressBar",
)

#: Классы, которые окном быть могут: их страховка не трогает.
_WINDOW_CLASSES = (
    "QDialog", "QMainWindow", "QMenu", "QMenuBar", "QTipLabel",
    "QSplashScreen", "QMessageBox",
)

#: Сколько «микроокон» погашено за сеанс. Счётчик нужен тестам и разбору
#: жалоб: 0 — значит таких виджетов больше никто не показывает вслепую.
STRAY_WINDOWS = []

#: Типы всплывающих окон: подсказки, меню и выпадающие списки. У них своя
#: рамка, и трогать её нельзя. Сравнивать надо КОД ТИПА (младший байт
#: флагов), а не биты: код диалога тоже содержит бит `Window`, поэтому
#: проверка через `&` считала диалог всплывашкой и пропускала оформление.
_POPUP_TYPES = ("Popup", "ToolTip", "Menu", "Tool", "DropDownMenu")


def _is_popup_window(window) -> bool:
    """Окно — подсказка, меню или выпадающий список, а не диалог."""
    try:
        from PyQt5.QtCore import Qt
        window_type = int(window.windowFlags()) & 0xFF
        codes = {int(getattr(Qt, name)) for name in _POPUP_TYPES
                 if hasattr(Qt, name)}
        return window_type in codes
    except Exception:
        return False


def hide_stray_window(obj) -> bool:
    """Погасить случайное окно-виджет. True — это и был такой случай.

    Настоящие окна не трогаем: диалоги и главное окно имеют собственную
    роль, а подсказки и меню — флаги ``ToolTip``/``Popup``.
    """
    try:
        from PyQt5.QtCore import Qt
        if not obj.isWindow() or obj.parentWidget() is not None:
            return False
        # Тип окна лежит в младшем байте флагов (Qt.WindowType_Mask) и это
        # код, а не битовая маска: 1 — обычное окно, 9 — всплывающий список,
        # 11 — палитра, 13 — подсказка. Всё, кроме обычного окна, живёт свои
        # окном по делу и трогать его нельзя.
        if int(obj.windowFlags()) & 0xFF != Qt.Window:
            return False
        names = {cls.__name__ for cls in type(obj).__mro__}
        # Настоящие окна приложения — диалоги, главное окно, меню и подсказки
        # Qt (`QTipLabel`): их трогать нельзя, у них своё назначение.
        if names.intersection(_WINDOW_CLASSES):
            return False
        if not names.intersection(_LEAF_WIDGETS):
            return False
        obj.hide()
        STRAY_WINDOWS.append(type(obj).__name__)
        return True
    except Exception:
        # Защита от «микроокон» не должна мешать приложению работать.
        return False


#: Окна, которые рисуют себя сами: пустых детей у них может не быть.
_SELF_DRAWN = ("QSplashScreen",)

#: Пустые окна, погашенные за сеанс: имя класса -> сколько раз.
BLANK_WINDOWS = {}


def sweep_blank_window(window) -> bool:
    """Погасить окно, в котором нет ни одного видимого виджета.

    Пустая рамка с крестиком — не подсказка и не диалог: её показывают по
    ошибке, до того как в окно положили содержимое. Такие окна всплывали
    при нажатии на особенность в листе и при отдыхе: в них ничего нет, но
    они висят поверх холста, пока их не закроешь руками.

    Проверка отложена на один оборот цикла событий — к этому времени
    настоящее окно уже собрано, а мусорное так и остаётся пустым.
    """
    try:
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QWidget
        if window is None:
            return False
        try:
            if not window.isWindow() or not window.isVisible():
                return False
            # Именно бит `Window`, а не точный код типа: у диалога тип
            # `Dialog` (Window|Dialog), и проверка «ровно Window» пропускала
            # бы как раз те пустые окна, ради которых всё затевалось.
            if not int(window.windowFlags()) & int(Qt.Window):
                return False
        except RuntimeError:
            # Окно уже удалено — трогать нечего.
            return False
        names = {cls.__name__ for cls in type(window).__mro__}
        if names.intersection(_SELF_DRAWN):
            return False
        for child in window.findChildren(QWidget):
            try:
                if child.isVisible() and child.window() is window:
                    return False
            except RuntimeError:
                continue
        window.hide()
        BLANK_WINDOWS[type(window).__name__] = (
            BLANK_WINDOWS.get(type(window).__name__, 0) + 1)
        return True
    except Exception:
        # Защита от мусорных окон не должна мешать приложению работать.
        return False


def _queue_blank_check(window) -> None:
    """Проверить показанное окно на пустоту следующим оборотом цикла.

    Ссылка слабая: диалог могли уже закрыть и удалить, а обращение к
    мёртвому C++-объекту роняет процесс без трассировки.
    """
    try:
        import weakref
        from PyQt5.QtCore import QTimer
    except Exception:
        return
    reference = weakref.ref(window)

    def check():
        target = reference()
        if target is None:
            return
        name = type(target).__name__
        if sweep_blank_window(target):
            try:
                import logging
                logging.info("погашено пустое окно: %s", name)
            except Exception:
                pass

    try:
        QTimer.singleShot(0, check)
    except Exception:
        pass


def theme_is_dark(widget, palette=None) -> bool:
    """Тема ближайшего окна, а не QPalette: QSS не меняет палитру.

    Виджеты листа живут внутри главного окна и не хранят тему сами,
    поэтому идём по родителям до первого `current_theme`. Если темы нет
    нигде (тесты, отдельные виджеты), остаётся палитра, а по умолчанию
    интерфейс тёмный.
    """
    node = widget
    while node is not None:
        theme = getattr(node, "current_theme", None)
        if theme:
            return str(theme) != "light"
        node = node.parentWidget()
    if palette is not None:
        return palette.window().color().lightness() < 128
    return True


def install_window_frame(app) -> None:
    """Поставить фильтр на приложение: все окна получают тёмный заголовок.

    Фильтр ставится один раз на QApplication, поэтому оформление получают
    и главное окно, и стартовый диалог, и карточка актёра, и мелкие окна
    ошибок — без того, чтобы каждое окно помнило о DWM.
    """
    try:
        from PyQt5.QtCore import QEvent, QObject, Qt
        from PyQt5.QtWidgets import QWidget
    except Exception:
        return

    class _FrameFilter(QObject):
        def eventFilter(self, obj, event):
            try:
                # На выходе из приложения окна разрушаются: трогать их
                # нельзя — обращение к `winId` умирающего окна роняет
                # процесс. Фильтр снимает себя сам, но события ещё идут.
                from PyQt5.QtCore import QCoreApplication
                if QCoreApplication.closingDown():
                    return False
                if (event.type() == QEvent.Show
                        and isinstance(obj, QWidget) and obj.isWindow()):
                    # Всплывающие подсказки и меню — тоже «окна», но рамку
                    # им менять незачем: скругление ломает их вид.
                    flags = obj.windowFlags()
                    if hide_stray_window(obj):
                        # Подпись или поле, которое показали до раскладки:
                        # отдельным окном оно быть не должно. Спрятали —
                        # внутри раскладки оно покажется как надо.
                        return False
                    if not _is_popup_window(obj):
                        # Пустоту проверяем до оформления: покраска
                        # заголовка трогает DWM и на чужой платформе
                        # может не отработать — гасить мусорное окно
                        # надо в любом случае.
                        _queue_blank_check(obj)
                        try:
                            apply_window_frame(obj, is_dark_theme(obj))
                        except Exception:
                            pass
            except Exception:
                # Оформление окна не должно мешать работе приложения.
                pass
            return False

    filter_object = _FrameFilter(app)
    app._vtt_frame_filter = filter_object
    app.installEventFilter(filter_object)
    # На выходе фильтр снимается сам. Иначе последние события приходятся на
    # разрушающиеся окна, и обращение к `winId` умирающего окна валило процесс
    # уже после отчёта — «выход с ошибкой» там, где всё сделано.
    try:
        app.aboutToQuit.connect(lambda: _detach_frame_filter(app))
    except (AttributeError, TypeError):
        pass


def _detach_frame_filter(app) -> None:
    """Снять фильтр оформления (безопасно вызывать дважды)."""
    try:
        filter_object = getattr(app, "_vtt_frame_filter", None)
        if filter_object is not None:
            app.removeEventFilter(filter_object)
            app._vtt_frame_filter = None
    except Exception:
        pass
