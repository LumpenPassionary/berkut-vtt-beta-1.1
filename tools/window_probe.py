"""Какие окна сейчас открыты — разовый разбор «всплывающих окошек».

Запуск при открытом приложении, когда лишние окна видны на экране:

    python tools/window_probe.py

Слежение: печатать только те окна, которые ПОЯВИЛИСЬ за указанное время.
Так ловится окно, которое мелькает на одно действие:

    python tools/window_probe.py --watch 60

Класс окна сразу показывает, что это за окно:

* ``...QWindowToolTip`` — подсказка Qt;
* ``...QWindowPopup`` — меню или выпадающий список Qt;
* ``...QWindowIcon`` — обычное окно Qt (диалог, панель, оверлей);
* ``ConsoleWindowClass`` — консоль (запустился консольный процесс);
* ``#32770`` — системный диалог Windows;
* ``Chrome_WidgetWin_0`` — окно браузера/WebView2;
* ``SDL_app``, ``PandaWindow`` — окна pygame/panda3d.

По названию процесса видно, чужое это окно или наше (``python``,
``Berkut VTT``).
"""
import ctypes
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def process_name(pid: int) -> str:
    """Имя процесса по pid — без внешних утилит, через Win32."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return "?"
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(handle)
    return "?"


def snapshot() -> dict:
    """Видимые окна верхнего уровня: hwnd -> сведения об окне."""
    windows = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width < 12 or height < 12:
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        windows[int(hwnd)] = {
            "process": process_name(pid.value),
            "pid": pid.value,
            "class": cls.value,
            "title": title.value,
            "width": width,
            "height": height,
            "left": rect.left,
            "top": rect.top,
        }
        return True

    user32.EnumWindows(collect, 0)
    return windows


def format_row(info: dict) -> str:
    name = f"{info['process']} ({info['pid']})"
    size = f"{info['width']}x{info['height']}"
    position = f"{info['left']},{info['top']}"
    return (f"{name:<22} {info['class']:<26} {size:<11} {position:<13} "
            f"{info['title']!r}")


def main(argv) -> int:
    if sys.platform != "win32":
        print("Скрипт рассчитан на Windows")
        return 1
    # Консоль Windows часто в cp1251: кириллица в заголовках иначе ломается.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    watch = 0
    if "--watch" in argv:
        index = argv.index("--watch")
        try:
            watch = int(argv[index + 1])
        except (IndexError, ValueError):
            watch = 60

    if watch > 0:
        print(f"Слежу за новыми окнами {watch} с. Сделайте действие в игре.")
        known = snapshot()
        deadline = time.monotonic() + watch
        seen = 0
        while time.monotonic() < deadline:
            time.sleep(0.15)
            current = snapshot()
            for hwnd, info in current.items():
                if hwnd in known:
                    continue
                seen += 1
                print(f"[{time.strftime('%H:%M:%S')}] " + format_row(info),
                      flush=True)
            known = current
        print(f"\nновых окон за {watch} с: {seen}")
        return 0

    rows = sorted(snapshot().values(),
                  key=lambda row: (row["process"].casefold(),
                                   row["title"].casefold()))
    print(f"{'процесс':<22} {'класс':<26} {'размер':<11} "
          f"{'позиция':<13} заголовок")
    for info in rows:
        print(format_row(info))
    print(f"\nвсего окон: {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
