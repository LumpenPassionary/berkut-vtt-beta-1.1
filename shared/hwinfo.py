"""Сколько памяти у стола: видеокарта и память процесса.

Зачем это нужно. «Программа лагает» почти всегда значит одно из двух: кадр
не успевает собраться на процессоре, или видеокарте не хватает своей памяти
и драйвер начинает подгружать текстуры через системную шину. Понять это
на глаз нельзя, поэтому отчёт показывает три вещи:

* какая видеокарта у машины и сколько у неё памяти (по данным драйвера);
* сколько памяти занято самим приложением;
* сколько памяти занимают буферы холста (маски света, тумана, картинки карт).

Модуль чистый: без Qt и без сети. Всё системное чтение обёрнуто в try —
отчёт о памяти не должен мешать игре, даже если Windows что-то не отдала.
"""
import ctypes
import os
import sys

#: Класс «видеоадаптеры» в реестре Windows. Под ним драйвер пишет и имя
#: карты, и точный размер видеопамяти в байтах.
_GPU_CLASS_KEY = (r"SYSTEM\CurrentControlSet\Control\Class"
                  r"\{4d36e968-e325-11ce-bfc1-08002be10318}")


def format_bytes(size) -> str:
    """Байты в человеческий вид: «12.0 ГБ», «480 МБ», «512 КБ»."""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "—"
    if value != value or value < 0:  # NaN или мусор из реестра
        return "—"
    for unit, step in (("ГБ", 1024.0 ** 3), ("МБ", 1024.0 ** 2),
                       ("КБ", 1024.0)):
        if value >= step:
            return "%.1f %s" % (value / step, unit)
    return "%d Б" % int(value)


# ---------------------------------------------------------------------------
# Память самого процесса
# ---------------------------------------------------------------------------
class _ProcessMemoryCountersEx(ctypes.Structure):
    """Структура PROCESS_MEMORY_COUNTERS_EX из psapi.h."""

    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def _windows_memory() -> dict:
    """Рабочий набор и приватная память процесса через psapi.

    Прототипы функций задаются явно: без них ctypes округляет дескриптор
    процесса до 32 бит, вызов возвращает «отказ», и отчёт молчит — именно
    поэтому сначала и было «система не сообщила объём».
    """
    counters = _ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_ProcessMemoryCountersEx),
        ctypes.c_ulong]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    handle = kernel32.GetCurrentProcess()
    ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters),
                                    ctypes.sizeof(counters))
    if not ok:
        return {}
    return {
        # Рабочий набор — сколько физической памяти реально держит процесс.
        "rss": int(counters.WorkingSetSize),
        # Приватная память — сколько занято только этим процессом, включая
        # то, что система уже выгрузила в файл подкачки.
        "private": int(counters.PrivateUsage),
        "peak": int(counters.PeakWorkingSetSize),
        "peak_private": int(counters.PeakPagefileUsage),
    }


def process_memory() -> dict:
    """Память текущего процесса в байтах (пустой словарь — не смогли)."""
    if sys.platform.startswith("win"):
        try:
            return _windows_memory()
        except Exception:
            return {}
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # На Linux ru_maxrss — килобайты, на macOS — байты.
        factor = 1 if sys.platform == "darwin" else 1024
        peak = int(usage.ru_maxrss) * factor
        return {"rss": peak, "private": peak, "peak": peak,
                "peak_private": peak}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Видеокарты
# ---------------------------------------------------------------------------
def _registry_adapters() -> list:
    """Адаптеры из реестра: имя драйвера и объём видеопамяти.

    Ветка ``HardwareInformation.qwMemorySize`` — единственное место в Windows,
    где объём видеопамяти лежит числом (64 бита). У виртуальных адаптеров и
    «Базового видеоадаптера Microsoft» её нет — такие пропускаем.
    """
    try:
        import winreg
    except Exception:
        return []
    result = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _GPU_CLASS_KEY)
    except OSError:
        return []
    index = 0
    while True:
        try:
            name = winreg.EnumKey(root, index)
        except OSError:
            break
        index += 1
        if not name[:4].isdigit():
            continue
        try:
            key = winreg.OpenKey(root, name)
        except OSError:
            continue
        with key:
            try:
                desc = str(winreg.QueryValueEx(key, "DriverDesc")[0]).strip()
            except OSError:
                continue
            size = 0
            for value_name in ("HardwareInformation.qwMemorySize",
                               "HardwareInformation.MemorySize"):
                try:
                    raw = winreg.QueryValueEx(key, value_name)[0]
                except OSError:
                    continue
                if isinstance(raw, bytes):
                    # Старый формат: строка из четырёх байт — REG_BINARY.
                    size = int.from_bytes(raw[:8].ljust(8, b"\x00"),
                                          "little")
                else:
                    size = int(raw)
                if size > 0:
                    break
            if size <= 0:
                continue
            if "basic display" in desc.casefold():
                continue
            result.append({"name": desc, "vram": size})
    return result


def _display_adapters() -> list:
    """Имена адаптеров через EnumDisplayDevices (запасной путь)."""
    names = []
    try:
        user32 = ctypes.windll.user32

        class _DisplayDevice(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong),
                        ("DeviceName", ctypes.c_wchar * 32),
                        ("DeviceString", ctypes.c_wchar * 128),
                        ("StateFlags", ctypes.c_ulong),
                        ("DeviceID", ctypes.c_wchar * 128),
                        ("DeviceKey", ctypes.c_wchar * 128)]

        index = 0
        while index < 16:
            device = _DisplayDevice()
            device.cb = ctypes.sizeof(device)
            if not user32.EnumDisplayDevicesW(None, index,
                                              ctypes.byref(device), 0):
                break
            index += 1
            name = str(device.DeviceString or "").strip()
            if name and name not in names:
                names.append(name)
    except Exception:
        return []
    return names


def gpu_adapters() -> list:
    """Список видеокарт: [{'name': ..., 'vram': байты или 0}].

    Объём берётся из реестра; если его там нет (например, старый драйвер),
    остаётся только имя — отчёт честно покажет «объём неизвестен».
    """
    adapters = []
    try:
        adapters = _registry_adapters()
    except Exception:
        adapters = []
    if adapters:
        return adapters
    return [{"name": name, "vram": 0} for name in _display_adapters()]


# ---------------------------------------------------------------------------
# Отчёт для интерфейса
# ---------------------------------------------------------------------------
def gpu_line(adapter) -> str:
    """Строка про одну видеокарту: имя и объём памяти."""
    if not isinstance(adapter, dict):
        return "Видеокарта: не определена"
    name = str(adapter.get("name") or "не определена").strip()
    size = adapter.get("vram") or 0
    if size:
        return "Видеокарта: %s — %s видеопамяти" % (name, format_bytes(size))
    return "Видеокарта: %s — объём памяти драйвер не сообщил" % name


def memory_lines(memory=None, adapters=None, buffers=0,
                 frame_ms=None) -> list:
    """Готовые строки отчёта — их же проверяют тесты.

    ``buffers`` — сколько занимают буферы холста (маски, картинки карт),
    ``frame_ms`` — среднее время кадра, если его уже измерили.
    """
    lines = []
    if adapters is None:
        adapters = gpu_adapters()
    if not adapters:
        lines.append("Видеокарта: не определена")
    else:
        for adapter in adapters:
            lines.append(gpu_line(adapter))
    if memory is None:
        memory = process_memory()
    rss = memory.get("rss") or 0
    private = memory.get("private") or 0
    if rss:
        lines.append("Память приложения: %s (рабочий набор), "
                     "пик %s" % (format_bytes(rss),
                                 format_bytes(memory.get("peak") or rss)))
    else:
        lines.append("Память приложения: система не сообщила объём")
    if private and private > rss * 1.25:
        lines.append("Своя память процесса: %s — системе уже пришлось "
                     "выгружать часть в файл подкачки" % format_bytes(private))
    if buffers:
        lines.append("Буферы холста: %s (маски света и тумана, картинки карт)"
                     % format_bytes(buffers))
    if frame_ms is not None:
        lines.append("Кадр сейчас: %.1f мс%s" % (
            float(frame_ms),
            " — это уже тормозит, снизьте качество теней" if frame_ms > 40
            else ""))
    return lines


def report_text(**kwargs) -> str:
    """Отчёт одной строкой с переносами — для подписи в окне настроек."""
    return "\n".join(memory_lines(**kwargs))


def is_low_memory(adapters=None, threshold=2 * 1024 ** 3) -> bool:
    """Похоже ли, что видеопамяти мало (меньше 2 ГБ).

    Нужно только для подсказки в интерфейсе: на встроенной графике
    «высокое качество теней» — верный способ получить 20 кадров в секунду.
    """
    if adapters is None:
        adapters = gpu_adapters()
    sizes = [adapter.get("vram") or 0 for adapter in adapters or []]
    sizes = [size for size in sizes if size > 0]
    return bool(sizes) and max(sizes) < threshold


def current_pid_memory_mb() -> float:
    """Память процесса в мегабайтах — короткая цифра для логов."""
    memory = process_memory()
    return round((memory.get("rss") or 0) / (1024.0 ** 2), 1)


def system_description() -> str:
    """Одна строка в лог при старте: система и видеокарта."""
    adapters = gpu_adapters()
    gpu = adapters[0]["name"] if adapters else "видеокарта не определена"
    return "%s, Python %s, %s" % (os.name, sys.version.split()[0], gpu)
