"""Локальные ресурсы: папка кэша и подготовка ссылки на файл.

Байты по сети передаёт shared.asset_transport (потоково, с SHA-256).
Здесь остались только локальные вещи: где лежит кэш, как описать файл
для очереди загрузки и как почистить кэш, не тронув сохранения.
"""
import os
import shutil
import uuid

from shared import app_paths

# Предел размера одного ресурса. Настраивается через VTT_MAX_ASSET_MB.
from shared.asset_transport import (IMAGE_EXTENSIONS, MAX_ASSET_BYTES,
                                    detect_extension)  # noqa: F401

CACHE_DIRNAME = "assets_cache"


def get_cache_dir(base_dir: str = None) -> str:
    """Папка локального кэша ресурсов."""
    base = base_dir or app_paths.home_dir()
    cache = os.path.join(base, CACHE_DIRNAME)
    os.makedirs(cache, exist_ok=True)
    return cache


def cache_stats(base_dir: str = None):
    """Сколько лежит в кэше: (файлов, байт).

    Считаем только содержимое папки кэша: миры, листы и настройки живут
    рядом, но к кэшу не относятся и в размер не попадают.
    """
    cache = get_cache_dir(base_dir)
    files = 0
    size = 0
    for root, _dirs, names in os.walk(cache):
        for name in names:
            path = os.path.join(root, name)
            try:
                size += os.path.getsize(path)
            except OSError:
                continue
            files += 1
    return files, size


def clear_cache(base_dir: str = None):
    """Удалить кэш ресурсов и вернуть (файлов, освобождено байт).

    Чистится только ``assets_cache``: сохранения миров (``worlds``), листы
    (``saves``), настройки и журнал остаются на месте. Папка кэша
    создаётся заново пустой — приложение сразу готово к работе.
    """
    cache = get_cache_dir(base_dir)
    removed = 0
    freed = 0
    for name in os.listdir(cache):
        path = os.path.join(cache, name)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                for root, _dirs, names in os.walk(path):
                    for inner in names:
                        try:
                            freed += os.path.getsize(os.path.join(root, inner))
                            removed += 1
                        except OSError:
                            pass
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    freed += os.path.getsize(path)
                except OSError:
                    pass
                os.remove(path)
                removed += 1
        except OSError:
            # Один занятый файл не должен мешать почистить остальное.
            continue
    return removed, freed


def human_size(size: int) -> str:
    """Байты в подпись «12,4 МБ» для интерфейса."""
    value = float(max(0, int(size or 0)))
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            text = f"{value:.0f}" if unit == "Б" else f"{value:.1f}"
            return f"{text.replace('.', ',')} {unit}"
        value /= 1024.0
    return f"{value:.1f} ГБ"


def prepare_image_for_upload(path: str, cache_dir: str = None) -> str:
    """Сделать картинку пригодной для передачи, если формат экзотический.

    Карта может называться ``.jpg``, а внутри быть AVIF, HEIC, TIFF или
    JPEG 2000: Qt такие файлы часто не открывает, а сервер не пропускает по
    сигнатуре. Если содержимое не входит в поддерживаемые PNG/JPEG/GIF/BMP/
    WebP, пробуем прочитать его через Pillow и пересохранить в PNG рядом
    с кэшем. Обычные картинки возвращаются тем же путём без перекодирования.

    Возвращает исходный путь, если формат уже поддержан или Pillow не смог
    прочитать файл: тогда решение остаётся за проверкой передачи, которая
    не пропустит исполняемый файл под видом картинки.
    """
    detected = detect_extension(path)
    if detected in IMAGE_EXTENSIONS:
        return path
    try:
        from PIL import Image
        root = cache_dir or get_cache_dir()
        os.makedirs(root, exist_ok=True)
        out = os.path.join(root, 'converted-' + uuid.uuid4().hex + '.png')
        with Image.open(path) as image:
            image.load()
            has_alpha = (image.mode in ('RGBA', 'LA')
                         or (image.mode == 'P' and 'transparency' in image.info))
            converted = image.convert('RGBA' if has_alpha else 'RGB')
            converted.save(out, 'PNG')
        return out
    except Exception:
        return path


def local_asset_ref(path: str):
    """Описание локального файла для очереди загрузки.

    Возвращает {'local_path': ...} — сами байты читает фоновый поток в
    network_client. Возвращает "" если файл недоступен или слишком большой.
    """
    try:
        if os.path.getsize(path) > MAX_ASSET_BYTES:
            print(f"Файл слишком большой для передачи: {path}")
            return ""
    except OSError as exc:
        print(f"Не удалось прочитать файл {path}: {exc}")
        return ""
    return {"local_path": os.path.abspath(path)}
