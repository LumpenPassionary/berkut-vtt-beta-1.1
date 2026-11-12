"""Рамки токенов: картинки из assets/frames и геометрия окна портрета.

Рамка — PNG с прозрачным отверстием посередине. Отверстие НЕ прописано
числами в коде: оно измеряется по альфа-каналу самой картинки. Поэтому
новая рамка просто кладётся файлом в ``client/assets/frames`` и сразу
появляется в окне подгонки — без правок в коде.

Портрет под рамкой:

  * рамка есть — картинка заполняет отверстие и обрезается не кругом, а
    маской его прозрачной части: когти, зубцы и прочие украшения, которые
    заходят внутрь оправы, остаются видны, а портрет доходит ровно до
    внутренней границы; мастер двигает портрет и меняет увеличение;
  * рамки нет — прежний вид: картинка вписывается в квадрат токена целиком,
    как в библиотеке существ (Тараск 2200×5000 не обрезался).

Одна и та же функция ``draw_portrait`` рисует токен и на карте, и в окне
подгонки, поэтому предпросмотр не может разойтись с картой.
"""
import json
import os
import time
from collections import OrderedDict

from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QBrush, QColor, QImage, QPainter, QPixmap

from shared.models import PORTRAIT_OFFSET_LIMIT

FRAMES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "assets", "frames")
#: «Рамки нет». Пустая строка у токена означает рамку по умолчанию.
NO_FRAME = "none"
#: Рамка по умолчанию: её получают все токены, у которых поле пустое
#: (в том числе токены старых миров, где поля рамки не было вовсе).
DEFAULT_FRAME = "bronze"
#: Если отверстие измерить не удалось (сплошная плашка), портрет всё равно
#: должен быть виден: берём разумную долю — как у поставляемой рамки.
FALLBACK_HOLE = 0.68
#: С какого разрешения отверстие размечается по альфе. 512 хватает, чтобы
#: маска не мылилась при сильном приближении, а разметка одной рамки
#: занимает около 0.13 с — и только один раз за запуск.
HOLE_MASK_SIZE = 512
#: Альфа рамки не выше порога — прозрачный пиксель (отверстие или фон
#: вокруг оправы). Такой же порог у `measure_hole`.
HOLE_ALPHA = 40
#: Порядок рамки, если он не задан в frames.json: такие рамки идут после
#: расставленных вручную, по алфавиту подписей.
DEFAULT_ORDER = 1000.0
#: Подложка под портрет внутри отверстия: карта не просвечивает сквозь
#: прозрачные края картинки, и портрет читается как медальон.
BACKING_COLOR = QColor(26, 24, 22)

#: Размеры, под которые заранее уменьшается рамка. Токены на экране почти
#: всегда мельче 256 px; крупные размеры нужны при сильном приближении.
_SIZES = tuple(range(64, 257, 32)) + (512, 1024)

_library = {}
_library_mtime = None
#: Когда папку проверяли в прошлый раз. Рендер спрашивает рамку у каждого
#: токена каждый кадр, а getmtime на диск — это системный вызов: проверяем
#: папку не чаще раза в две секунды, новая рамка появится в меню и так.
_library_checked = 0.0
LIBRARY_RECHECK_S = 2.0
_images = {}
_holes = {}
_hole_masks = {}
_pixmaps = OrderedDict()
_PIXMAPS_MAX = 48
#: Уменьшенные маски отверстия: (рамка, размер) -> картинка. Собираются
#: один раз на пару и ложатся на портрет готовыми.
_scaled_masks = OrderedDict()
_SCALED_MASKS_MAX = 24
#: Буферы слоя «портрет/заливка под маской отверстия» по размерам.
#: Новую картинку на каждый вызов рисовать нельзя: на видеокарте
#: (QOpenGLWidget) только что созданная картинка ложится сплошным чёрным
#: прямоугольником по своему размеру — тот же случай, что описан у буферов
#: холста (`GameCanvas.scratch_image`). Поэтому картинка живёт между
#: кадрами и только перезаписывается по месту.
_layer_images = OrderedDict()
_LAYER_IMAGES_MAX = 6


class FrameSpec:
    """Рамка из папки: ключ (имя файла), подпись для меню, путь, порядок."""

    __slots__ = ("key", "title", "path", "order")

    def __init__(self, key: str, title: str, path: str,
                 order: float = DEFAULT_ORDER):
        self.key = key
        self.title = title
        self.path = path
        self.order = order

    def __eq__(self, other):
        return isinstance(other, FrameSpec) and other.key == self.key

    def __hash__(self):
        return hash(self.key)

    def __repr__(self):
        return f"FrameSpec({self.key!r}, {self.title!r})"


def _titles() -> dict:
    """Подписи и порядок рамок из frames.json (необязательный файл).

    Ключ — имя файла без расширения. Значение — либо подпись строкой
    (``"bronze": "Бронзовая"``), либо объект с подписью и местом в списке
    (``"boss": {"title": "Босс", "order": 7}``). Порядок нужен потому,
    что рамки в меню идут не по алфавиту: стандартная, деревянная, золотая,
    а дальше остальные. Без файла подпись делается из имени:
    ``bronze`` -> ``Bronze``.
    """
    path = os.path.join(FRAMES_DIR, "frames.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for key, value in data.items():
        title, order = value, None
        if isinstance(value, dict):
            title = value.get("title")
            order = value.get("order")
        try:
            order = float(order) if order is not None else None
        except (TypeError, ValueError):
            order = None
        result[str(key)] = (str(title) if title else "", order)
    return result


def _prettify(key: str) -> str:
    text = str(key).replace("_", " ").replace("-", " ").strip()
    return text[:1].upper() + text[1:] if text else str(key)


def _load_library() -> dict:
    """Просканировать папку рамок; кэш сбрасывается при её изменении."""
    global _library, _library_mtime, _library_checked
    now = time.monotonic()
    if _library and now - _library_checked < LIBRARY_RECHECK_S:
        return _library
    _library_checked = now
    try:
        mtime = os.path.getmtime(FRAMES_DIR)
    except OSError:
        _library = {}
        _library_mtime = None
        return _library
    if _library_mtime == mtime and _library:
        return _library

    titles = _titles()
    found = {}
    try:
        names = sorted(os.listdir(FRAMES_DIR))
    except OSError:
        names = []
    for name in names:
        if not name.casefold().endswith(".png"):
            continue
        key = os.path.splitext(name)[0]
        title, order = titles.get(key, (None, None))
        found[key] = FrameSpec(key, str(title or _prettify(key)),
                               os.path.join(FRAMES_DIR, name),
                               DEFAULT_ORDER if order is None else order)
    _library = found
    _library_mtime = mtime
    return _library


def available_frames() -> list:
    """Все рамки папки по порядку из frames.json. «Без рамки» сюда не
    входит — это отдельный вариант в меню, а не файл."""
    return sorted(_load_library().values(),
                  key=lambda spec: (spec.order, spec.title.casefold()))


def frame_path(key: str):
    spec = _load_library().get(str(key or "").strip())
    return spec.path if spec else None


def resolve(key) -> str:
    """Ключ рамки, приведённый к тому, что есть на диске.

    Пустое поле означает рамку по умолчанию; неизвестный ключ (рамку убрали
    с диска) — «без рамки»: токен не должен потеряться или стать пустым.
    """
    text = str(key or "").strip()
    if text == NO_FRAME:
        return NO_FRAME
    if not text:
        text = DEFAULT_FRAME
    return text if text in _load_library() else NO_FRAME


def token_frame(token) -> str:
    """Ключ рамки конкретного токена.

    Существо из справочника (``source_id``) рамки не носит: у него уже есть
    арт, и бронзовая оправа вокруг него спорила с рисунком. Явно заданная
    рамка важнее правила: токен, которому рамку назначили, остаётся в ней.
    """
    key = str(getattr(token, "frame", "") or "").strip()
    if not key and getattr(token, "source_id", None):
        return NO_FRAME
    return resolve(key)


def frame_image(key: str) -> QImage:
    """Картинка рамки (кэш). None — файла нет или он не читается."""
    key = resolve(key)
    if key == NO_FRAME:
        return None
    if key in _images:
        return _images[key]
    path = frame_path(key)
    image = QImage(path) if path else QImage()
    if image.isNull():
        _images[key] = None
        return None
    _images[key] = image
    return image


def _alpha_grid(image: QImage, size: int, threshold: int = HOLE_ALPHA) \
        -> bytearray:
    """Прозрачность картинки в сетке size×size: 1 — прозрачный пиксель."""
    sample = image.scaled(size, size, Qt.IgnoreAspectRatio,
                          Qt.SmoothTransformation).convertToFormat(
        QImage.Format_ARGB32)
    raw = sample.constBits().asstring(sample.byteCount())
    stride = sample.bytesPerLine()
    grid = bytearray(b"\x01" * (size * size))
    for y in range(size):
        base = y * stride
        row = raw[base:base + size * 4]
        for x in range(size):
            if row[x * 4 + 3] > threshold:
                grid[y * size + x] = 0
    return grid


def _flood_hole(clear: bytearray, size: int) -> bytearray:
    """Залить прозрачную область, в которой лежит центр картинки.

    Прозрачность у рамки не одна: кроме отверстия прозрачны и углы вокруг
    оправы. Без заливки портрет полез бы в них квадратом. Заливка идёт
    построчно (спанами): так 512² размечаются за десятые доли секунды даже
    на чистом Python.
    """
    filled = bytearray(size * size)
    center = size // 2
    stack = [(center, center)]
    while stack:
        x, y = stack.pop()
        if y < 0 or y >= size:
            continue
        row = y * size
        if filled[row + x] or not clear[row + x]:
            continue
        left = x
        while left > 0 and clear[row + left - 1] \
                and not filled[row + left - 1]:
            left -= 1
        right = x
        while right + 1 < size and clear[row + right + 1] \
                and not filled[row + right + 1]:
            right += 1
        for index in range(left, right + 1):
            filled[row + index] = 1
        for next_row in (row - size, row + size):
            if next_row < 0 or next_row >= size * size:
                continue
            index = left
            while index <= right:
                if clear[next_row + index] and not filled[next_row + index]:
                    step = index
                    while step <= right and clear[next_row + step] \
                            and not filled[next_row + step]:
                        step += 1
                    stack.append((index, next_row // size))
                    index = step
                else:
                    index += 1
    return filled


def _hole_from_grid(clear: bytearray, size: int):
    """Маска отверстия и его рамка: (QImage, cx, cy, диаметр) в долях.

    Заливка переводится в байты картинки сразу по строкам: пиксель за
    пикселем (`setPixel`) — это сотни тысяч вызовов и десятые доли секунды
    на каждую рамку.
    """
    filled = _flood_hole(clear, size)
    xs_min, xs_max, ys_min, ys_max = size, -1, size, -1
    data = bytearray(size * size * 4)
    for y in range(size):
        row = y * size
        start = None
        for x in range(size):
            if filled[row + x]:
                if start is None:
                    start = x
            elif start is not None:
                base = (row + start) * 4
                data[base:(row + x) * 4] = b"\xff" * ((x - start) * 4)
                xs_min = min(xs_min, start)
                xs_max = max(xs_max, x - 1)
                ys_min = min(ys_min, y)
                ys_max = max(ys_max, y)
                start = None
        if start is not None:
            base = (row + start) * 4
            data[base:(row + size) * 4] = b"\xff" * ((size - start) * 4)
            xs_min = min(xs_min, start)
            xs_max = max(xs_max, size - 1)
            ys_min = min(ys_min, y)
            ys_max = max(ys_max, y)
    if xs_max < 0:
        return None
    mask = QImage(bytes(data), size, size,
                  QImage.Format_ARGB32_Premultiplied).copy()
    width = (xs_max - xs_min + 1) / float(size)
    height = (ys_max - ys_min + 1) / float(size)
    center_x = (xs_min + xs_max + 1) / 2.0 / size
    center_y = (ys_min + ys_max + 1) / 2.0 / size
    return mask, center_x, center_y, min(width, height)


def measure_hole(image: QImage, threshold: int = None):
    """Прозрачное отверстие картинки: (cx, cy, диаметр) в долях ширины.

    Отверстие — это прозрачная область, в которой лежит центр картинки;
    диаметр берётся по её габаритам, а не по вписанному кругу: у рамок с
    когтями и зубцами круг заметно меньше настоящей дыры, и портрет в нём
    не доходил до внутренней границы.
    """
    if image is None or image.isNull():
        return None
    limit = HOLE_ALPHA if threshold is None else int(threshold)
    clear = _alpha_grid(image, HOLE_MASK_SIZE, limit)
    measured = _hole_from_grid(clear, HOLE_MASK_SIZE)
    if measured is None:
        return None
    _mask, center_x, center_y, diameter = measured
    return (center_x, center_y, diameter)


def hole_data(key) -> tuple:
    """Маска прозрачного отверстия рамки и её геометрия (кэш).

    Возвращает ``(маска, диаметр, cx, cy)``: маска в HOLE_MASK_SIZE², всё
    остальное — в долях ширины картинки. Считается один раз на рамку:
    без кэша каждый токен в каждом кадре размечал бы картинку заново.
    """
    key = resolve(key)
    if key == NO_FRAME:
        return (None, 0.0, 0.5, 0.5)
    if key in _holes:
        return _holes[key]
    image = frame_image(key)
    measured = None
    if image is not None and not image.isNull():
        clear = _alpha_grid(image, HOLE_MASK_SIZE)
        measured = _hole_from_grid(clear, HOLE_MASK_SIZE)
    if measured is None:
        result = (None, FALLBACK_HOLE, 0.5, 0.5)
    else:
        mask, center_x, center_y, diameter = measured
        result = (mask, max(0.2, min(0.98, diameter)), center_x, center_y)
    _holes[key] = result
    return result


def _measured(key) -> tuple:
    """Геометрия отверстия с кэшем: (диаметр, cx, cy) в долях ширины."""
    _mask, ratio, center_x, center_y = hole_data(key)
    return (ratio, center_x, center_y)


def hole_mask(key) -> QImage:
    """Маска отверстия: непрозрачно там, где рамка прозрачна.

    Этой маской обрезается портрет, поэтому он повторяет внутреннюю границу
    оправы — вместе с когтями и зубцами, которые в неё заходят.
    """
    return hole_data(key)[0]


def scaled_hole_mask(key, pixels: float) -> QImage:
    """Маска отверстия под размер на экране (кэш по размерам)."""
    key = resolve(key)
    size = _bucket(pixels)
    cached = _scaled_masks.get((key, size))
    if cached is not None:
        _scaled_masks.move_to_end((key, size))
        return cached
    source = hole_mask(key)
    if source is None:
        return None
    mask = source.scaled(size, size, Qt.IgnoreAspectRatio,
                         Qt.SmoothTransformation)
    mask = mask.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    _scaled_masks[(key, size)] = mask
    while len(_scaled_masks) > _SCALED_MASKS_MAX:
        _scaled_masks.popitem(last=False)
    return mask


def hole_ratio(key) -> float:
    """Диаметр отверстия в долях ширины токена; 0.0 — рамки нет.

    Если отверстие не размечено (маски нет), здесь тоже 0.0: обрезать
    портрет нечем, и «рамка» обернулась бы тёмной подложкой на весь квадрат
    токена — вместо тонкой оправы чёрный квадрат.
    """
    key = resolve(key)
    if key == NO_FRAME:
        return 0.0
    mask, ratio, _cx, _cy = hole_data(key)
    return ratio if mask is not None else 0.0


def hole_center(key) -> tuple:
    """Центр отверстия в долях ширины картинки (обычно ровно 0.5, 0.5)."""
    key = resolve(key)
    if key == NO_FRAME:
        return (0.5, 0.5)
    measured = _measured(key)
    return (measured[1], measured[2])


def clear_cache():
    """Сбросить кэши (нужно тестам и после правки папки рамок)."""
    global _library, _library_mtime, _library_checked
    _library = {}
    _library_mtime = None
    _library_checked = 0.0
    _images.clear()
    _holes.clear()
    _pixmaps.clear()
    _scaled_masks.clear()
    _layer_images.clear()


def _bucket(pixels: float) -> int:
    """Размер, под который уменьшается рамка: всегда не меньше нужного,
    поэтому на экране рамка никогда не растягивается и не мылится."""
    size = max(8, int(round(float(pixels))))
    for step in _SIZES:
        if size <= step:
            return step
    return _SIZES[-1]


def scaled_frame(key, pixels: float) -> QPixmap:
    """Рамка, уменьшенная под размер на экране (кэш по размерам)."""
    key = resolve(key)
    pixels = _bucket(pixels)
    cached = _pixmaps.get((key, pixels))
    if cached is not None:
        _pixmaps.move_to_end((key, pixels))
        return cached
    image = frame_image(key)
    if image is None:
        return None
    pixmap = QPixmap.fromImage(image.scaled(
        pixels, pixels, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    _pixmaps[(key, pixels)] = pixmap
    while len(_pixmaps) > _PIXMAPS_MAX:
        _pixmaps.popitem(last=False)
    return pixmap


def screen_size(painter: QPainter, rect: QRectF) -> float:
    """Сколько пикселей экрана занимает rect под текущим преобразованием.

    Холст рисует токены в мире под масштабом камеры, окно подгонки — уже в
    экранных пикселях. По этому числу рамка уменьшается один раз и дальше не
    мылится: при сильном приближении берётся крупная копия, при отдалении —
    мелкая, а не одна и та же на все случаи.
    """
    try:
        scale = abs(float(painter.worldTransform().m11())) or 1.0
    except (AttributeError, TypeError, ValueError):
        scale = 1.0
    device = painter.device()
    ratio = 1.0
    try:
        ratio = float(device.devicePixelRatioF()) if device is not None else 1.0
    except (AttributeError, TypeError, ValueError):
        ratio = 1.0
    return max(rect.width(), rect.height()) * scale * max(0.05, ratio)


def draw_frame(painter: QPainter, key, rect: QRectF) -> bool:
    """Нарисовать рамку в квадрат токена. False — рамки нет."""
    key = resolve(key)
    if key == NO_FRAME or rect.width() <= 0 or rect.height() <= 0:
        return False
    pixmap = scaled_frame(key, screen_size(painter, rect))
    if pixmap is None:
        return False
    center = hole_center(key)
    side = max(rect.width(), rect.height())
    # Отверстие, измеренное со смещением, не должно съезжать: рамка
    # сдвигается так, чтобы центр отверстия лёг в центр токена.
    offset_x = (0.5 - center[0]) * side
    offset_y = (0.5 - center[1]) * side
    target = QRectF(rect)
    if offset_x or offset_y:
        target = rect.translated(offset_x, offset_y)
    painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
    return True


def hole_rect(rect: QRectF, hole: float) -> QRectF:
    """Круг портрета внутри квадрата токена."""
    if hole <= 0.0:
        return QRectF(rect)
    diameter = rect.width() * hole
    center = rect.center()
    return QRectF(center.x() - diameter / 2.0, center.y() - diameter / 2.0,
                  diameter, diameter)


def portrait_rect(rect: QRectF, image_width, image_height, *, hole: float = 0.0,
                  zoom: float = 1.0, dx: float = 0.0, dy: float = 0.0) -> QRectF:
    """Куда кладётся портрет внутри токена.

    С рамкой — картинка заполняет отверстие (cover): меньшая сторона равна
    диаметру, лишнее уходит под рамку и обрезается. Без рамки — прежнее
    вписывание целиком (contain), чтобы портреты библиотеки не обрезались.

    ``zoom`` и ``dx``/``dy`` считаются от размера токена: увеличение вокруг
    центра и сдвиг в долях ширины токена — одинаково в окне подгонки и на
    карте при любом масштабе.
    """
    try:
        width = float(image_width)
        height = float(image_height)
        rect_w = float(rect.width())
    except (TypeError, ValueError):
        return QRectF(rect)
    if width <= 0 or height <= 0 or rect_w <= 0:
        return QRectF(rect)
    zoom = max(0.05, float(zoom or 1.0))
    if hole > 0.0:
        factor = rect_w * float(hole) / min(width, height)
    else:
        factor = min(rect.width() / width, rect.height() / height)
    factor *= zoom
    draw_w = width * factor
    draw_h = height * factor
    center = rect.center()
    return QRectF(center.x() + float(dx or 0.0) * rect_w - draw_w / 2.0,
                  center.y() + float(dy or 0.0) * rect_w - draw_h / 2.0,
                  draw_w, draw_h)


def clamp_offsets(rect: QRectF, image_width, image_height, *, hole: float,
                  zoom: float, dx: float, dy: float) -> tuple:
    """Сдвиг портрета, при котором отверстие рамки остаётся закрытым.

    Портрет заполняет отверстие, поэтому запас хода есть только по той
    оси, где картинка шире/выше круга. Если мастер уменьшил увеличение и
    картинка уже не закрывает круг, сдвиг ограничивается только разумным
    пределом — двигать можно свободно, пустота внутри оправы допустима.
    """
    art = portrait_rect(rect, image_width, image_height, hole=hole, zoom=zoom)
    side = max(1.0, float(rect.width()))
    window = side * float(hole) if hole > 0.0 else side

    def axis(offset, size):
        offset = float(offset or 0.0)
        if size >= window:
            slack = (size - window) / 2.0 / side
            return max(-slack, min(slack, offset))
        limit = float(PORTRAIT_OFFSET_LIMIT)
        return max(-limit, min(limit, offset))

    return (axis(dx, art.width()), axis(dy, art.height()))


def _layer_image(pixels: int) -> QImage:
    """Прозрачный буфер слоя под размер токена — один и тот же между кадрами.

    В кадре он должен выводиться ровно ОДИН раз: портрет и все заливки
    (ранение, тон смерти, вспышка попадания) собираются в него за один
    проход, а на холст он идёт одним `drawImage`. Раньше заливки выводили
    его ещё раз поверх готового портрета — и на видеокарте повторный вывод
    ложился чёрным прямоугольником во весь токен.
    """
    size = _bucket(pixels)
    image = _layer_images.get(size)
    if image is None:
        image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
        if len(_layer_images) >= _LAYER_IMAGES_MAX:
            _layer_images.popitem(last=False)
        _layer_images[size] = image
    image.fill(Qt.transparent)
    return image


def paint_in_hole(painter: QPainter, key, rect: QRectF, paint) -> bool:
    """Нарисовать что-то внутри отверстия рамки — ровно по её прозрачной части.

    ``paint(painter, rect)`` рисует в тех же координатах, что и холст
    (мировых). Готовое ложится в буфер размером с токен на экране и
    обрезается маской отверстия, поэтому портрет доходит до внутренней
    границы оправы — вместе с когтями и зубцами, — но не вылезает ни в фон
    вокруг оправы, ни на саму рамку.

    Возвращает True, если маска была и обрезка произошла.
    """
    key = resolve(key)
    if hole_ratio(key) <= 0.0 or rect.width() <= 0 or rect.height() <= 0:
        paint(painter, rect)
        return False
    pixels = _bucket(screen_size(painter, rect))
    mask = scaled_hole_mask(key, pixels)
    if mask is None:
        # Маски нет — обрезать нечем. Рисовать всё равно нельзя: без обрезки
        # подложка и заливка ложатся квадратом на весь токен, а это и есть
        # тот чёрный квадрат, который видно при ударе и на нуле хитов —
        # заливка ранения и смерти идёт именно в этих состояниях.
        # Механизм тот же, что у картинок, созданных в кадре (см. выше про
        # `_layer_images`): новый прямоугольник, выведенный в кадре, на
        # видеокарте виден чёрным. Здесь его площадь — весь токен с клетку.
        # Сторожит `tests/test_paint_no_new_images.py`.
        return False
    layer = _layer_image(pixels)
    layer.fill(Qt.transparent)
    inner = QPainter(layer)
    inner.setRenderHint(QPainter.Antialiasing, True)
    inner.setRenderHint(QPainter.SmoothPixmapTransform, True)
    scale = pixels / float(rect.width())
    inner.scale(scale, scale)
    inner.translate(-rect.x(), -rect.y())
    try:
        paint(inner, rect)
    finally:
        inner.end()
    cut = QPainter(layer)
    cut.setCompositionMode(QPainter.CompositionMode_DestinationIn)
    cut.drawImage(0, 0, mask)
    cut.end()
    painter.drawImage(rect, layer)
    return True


def fill_hole(painter: QPainter, key, rect: QRectF, color: QColor,
              overlays=()) -> bool:
    """Залить отверстие цветом (ранение, вспышка) — только внутри оправы.

    Без обрезки круг попадания ложился бы поверх когтей рамки и красил их.
    ``overlays`` — тон смерти и вспышка: у токена без картинки они ложатся
    сюда же, в тот же проход, чтобы кадр обошёлся одним выводом слоя.
    В кадр даёт один вывод буфера слоя: на видео­карте повторный вывод того
    же буфера ложится чёрным квадратом во весь токен.
    """
    def paint(inner, area):
        inner.setPen(Qt.NoPen)
        inner.setBrush(QColor(color))
        inner.drawRect(area)
        for extra in overlays:
            inner.setBrush(QBrush(QColor(extra)))
            inner.drawRect(area)

    return paint_in_hole(painter, key, rect, paint)


def draw_portrait(painter: QPainter, image, rect: QRectF, *, frame="",
                  zoom: float = 1.0, dx: float = 0.0, dy: float = 0.0,
                  turn: float = 0.0, overlays=()) -> bool:
    """Портрет в рамке — единый рендер карты и окна подгонки.

    ``turn`` — поворот портрета вокруг центра (взгляд токена); рамка не
    поворачивается: это оправа, а не часть рисунка.

    Обрезка идёт по прозрачной части самой рамки (`paint_in_hole`), поэтому
    у оправы с когтями портрет заполняет всю дыру, а не вписанный круг.
    """
    key = resolve(frame)
    hole = hole_ratio(key)
    if image is None or (hasattr(image, "isNull") and image.isNull()):
        return False
    if isinstance(image, QImage):
        image = QPixmap.fromImage(image)
    # Сдвиг ограничивается так же, как в окне подгонки: пустота внутри
    # оправы — это не «мастер так хотел», а промах в данных токена.
    dx, dy = clamp_offsets(rect, image.width(), image.height(), hole=hole,
                           zoom=zoom, dx=dx, dy=dy)
    art = portrait_rect(rect, image.width(), image.height(), hole=hole,
                        zoom=zoom, dx=dx, dy=dy)
    if hasattr(image, "rect"):
        source = QRectF(image.rect())
    else:
        source = QRectF(0, 0, image.width(), image.height())

    def paint(inner, area):
        # Подложка под портрет — только внутри оправы: карта не просвечивает
        # сквозь прозрачные края картинки, и портрет читается как медальон.
        # Без рамки подложки нет: токен там — это сама картинка, а не тёмный
        # квадрат вокруг неё (у существ из справочника арт не обрезан).
        inner.save()
        if hole > 0.0:
            inner.setPen(Qt.NoPen)
            inner.setBrush(BACKING_COLOR)
            inner.drawRect(area)
        if turn:
            inner.translate(area.center())
            inner.rotate(float(turn))
            inner.translate(-area.center())
        inner.drawPixmap(art, image, source)
        inner.restore()
        # Тон смерти и вспышка попадания — в тот же слой и в тот же проход:
        # второй вывод буфера поверх готового портрета на видеокарте ложится
        # чёрным квадратом во весь токен.
        for color in overlays:
            inner.setPen(Qt.NoPen)
            inner.setBrush(QBrush(QColor(color)))
            inner.drawRect(area)

    painter.save()
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    if hole > 0.0:
        painter.setRenderHint(QPainter.Antialiasing, True)
        paint_in_hole(painter, key, rect, paint)
    else:
        paint(painter, rect)
    painter.restore()
    draw_frame(painter, key, rect)
    return True


def render_portrait(image, size: int, *, frame="", zoom: float = 1.0,
                    dx: float = 0.0, dy: float = 0.0) -> QImage:
    """Портрет в картинку — для значков выбора рамки и для тестов."""
    size = max(8, int(size))
    target = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    target.fill(Qt.transparent)
    painter = QPainter(target)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    draw_portrait(painter, image, QRectF(0, 0, size, size), frame=frame,
                  zoom=zoom, dx=dx, dy=dy)
    painter.end()
    return target
