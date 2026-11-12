"""Журнал приложения и защита от «тихого» падения в слоте Qt.

PyQt5 при необработанном исключении из слота завершает процесс молча:
окно игрока просто закрывается, трассировки в консоли нет (проверено:
процесс умирает ещё до вывода). Пользователь видит только «вылетел
отладчик». Свой ``sys.excepthook`` превращает такую ошибку в запись
в журнал и в stderr, а приложение продолжает работать.
"""
import logging
import os
import sys
import threading
import traceback

from shared import app_paths

LOG_DIR = app_paths.home_dir()


def app_log_path() -> str:
    """Файл журнала приложения (рядом с данными сессии)."""
    return os.path.join(LOG_DIR, "app.log")


def install_excepthook() -> None:
    """Писать трассировку необработанных ошибок вместо убийства процесса.

    Своя ошибка в этом хуке недопустима: PyQt5 при исключении из слота
    завершает процесс. Поэтому stderr может отсутствовать (оконная сборка
    pythonw/PyInstaller) — тогда остаётся только журнал.
    """

    def _report(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        # Файл: только если журнал настроен — иначе дубли в stderr.
        logger = logging.getLogger("app")
        root = logging.getLogger()
        if logger.handlers or root.handlers:
            try:
                logger.error("Необработанная ошибка:\n%s", text)
            except Exception:
                pass
        stream = sys.stderr
        if stream is not None:
            try:
                stream.write(text)
                stream.flush()
            except Exception:
                pass

    def _hook(exc_type, exc, tb):
        _report(exc_type, exc, tb)

    def _thread_hook(args):
        # Закрытие потока через SystemExit — нормальный выход, не ошибка.
        if issubclass(args.exc_type, SystemExit):
            return
        _report(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = _hook
    # Ошибка в сетевом потоке раньше исчезала в stderr оконной сборки:
    # сервер продолжал жить, но причина сбоя нигде не оставалась.
    threading.excepthook = _thread_hook
