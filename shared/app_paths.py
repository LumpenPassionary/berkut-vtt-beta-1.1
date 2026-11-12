"""Где приложение хранит данные: миры, кэш, лист LSS, настройки и журнал.

Два режима:

* **исходники** — по умолчанию ``~/.dnd_vtt``;
* **собранная программа** — папка ``data`` рядом с ``.exe``, чтобы все данные
  лежали в одной папке программы.

Порядок поиска:

1. переменная окружения ``DND_VTT_HOME`` (тесты и разовые запуски);
2. ``core.json`` рядом с приложением — например ``{"home": "data"}``;
3. для собранного ``.exe`` — ``data`` рядом с ним;
4. ``%LOCALAPPDATA%\\DnD VTT``, если папка из пунктов 2–3 недоступна на
   запись (например, программа установлена в ``Program Files``);
5. ``~/.dnd_vtt`` — запасной вариант для исходников и не-Windows систем.

Относительный путь в ``core.json`` считается от папки приложения.
Проверка записи обязательна: ``app.log`` открывается до появления окна, и на
защищённой папке приложение иначе завершалось бы молча. Существование самой
папки не требуется — её создаст приложение, — но родительская папка должна
быть доступна на запись.

Никаких абсолютных путей в коде: перенос папки — это правка одного файла.
"""
import json
import os
import sys
import tempfile

POINTER_NAME = "core.json"
DEFAULT_DIRNAME = ".dnd_vtt"
#: Папка данных рядом с .exe: DnD_VirtualTable.exe + data + library.
PORTABLE_DIRNAME = "data"
#: Куда отступать, если папка программы только для чтения.
FALLBACK_DIRNAME = "DnD VTT"

#: Результаты проверки записи: (путь, существует ли папка) -> bool.
#: Проверка создаёт временный файл, поэтому её нельзя делать на каждый вызов
#: home_dir() — он вызывается при каждой записи листа и каждом логе.
_WRITABLE_CACHE = {}


def app_dir() -> str:
    """Папка приложения: исходники или каталог собранного .exe."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def pointer_path() -> str:
    return os.path.join(app_dir(), POINTER_NAME)


def dir_writable(path: str) -> bool:
    """Можно ли писать в папку (или создать её внутри доступной).

    Права проверяются реальной записью временного файла, а не ``os.access``:
    на Windows ``os.access`` врёт при работе от администратора и на сетевых
    дисках. Для ещё не существующей папки проверяется ближайший существующий
    родитель — приложение создаст остальное само.
    """
    key = (path, os.path.isdir(path))
    cached = _WRITABLE_CACHE.get(key)
    if cached is not None:
        return cached

    probe = path
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent

    writable = False
    if os.path.isdir(probe):
        try:
            fd, name = tempfile.mkstemp(prefix=".vtt-write-", dir=probe)
        except OSError:
            writable = False
        else:
            os.close(fd)
            try:
                os.unlink(name)
            except OSError:
                pass
            writable = True

    _WRITABLE_CACHE[key] = writable
    return writable


def _pointer_home() -> str:
    """Путь из core.json: файл прочитан и путь задан.

    Существование самой папки не проверяется: её создаёт приложение.
    Проверяется родительская папка — так отключённый диск не превращается
    в молчаливый возврат данных в ``~/.dnd_vtt``, а перенос .exe на машину
    без этого диска не ломает запуск.
    """
    try:
        with open(pointer_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return ""
    value = data.get("home") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value.strip():
        return ""
    path = os.path.expanduser(value.strip())
    if not os.path.isabs(path):
        path = os.path.join(app_dir(), path)
    path = os.path.abspath(path)
    if os.path.isdir(path) or os.path.isdir(os.path.dirname(path)):
        return path
    return ""


def _portable_home() -> str:
    """Папка данных собранной программы: ``data`` рядом с .exe."""
    return os.path.join(app_dir(), PORTABLE_DIRNAME)


def _fallback_home() -> str:
    """Запасной путь, если папка программы недоступна на запись."""
    base = os.environ.get("LOCALAPPDATA")
    if os.name == "nt" and base:
        return os.path.join(base, FALLBACK_DIRNAME)
    return os.path.join(os.path.expanduser("~"), DEFAULT_DIRNAME)


def home_dir() -> str:
    """Корень данных: env → core.json → data рядом с .exe → запасной путь.

    Папка может ещё не существовать — её создаст тот, кто пишет данные;
    возвращается именно она, а не запасная, если она доступна на запись.
    """
    env = os.environ.get("DND_VTT_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))

    pointer = _pointer_home()
    if pointer and dir_writable(pointer):
        return pointer

    if getattr(sys, "frozen", False):
        portable = _portable_home()
        if dir_writable(portable):
            return portable
        fallback = _fallback_home()
        if dir_writable(fallback):
            return fallback
        # Последний шанс: профиль пользователя. Лучше писать туда, чем
        # упасть на журнале при старте.
        return os.path.join(os.path.expanduser("~"), DEFAULT_DIRNAME)

    return os.path.join(os.path.expanduser("~"), DEFAULT_DIRNAME)
