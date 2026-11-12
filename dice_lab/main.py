"""Точка входа Dice Lab для запуска двойным щелчком.

Корневой ``main.py`` запускает виртуальный стол, поэтому у отдельного
лотка кубиков есть свой ``main.py``. Его можно открыть двойным щелчком
или запустить из любого каталога:

    python dice_lab/main.py
"""
import os
import sys
import traceback

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _report_error():
    """Показать ошибку при запуске двойным щелчком.

    С консолью достаточно traceback и Enter. Без консоли (pythonw) надо
    показать окно, иначе приложение просто молча не откроется.
    """
    if sys.stderr is not None:
        try:
            traceback.print_exc()
        except Exception:
            pass
    if sys.stdin is not None and sys.stdin.isatty():
        try:
            input('Нажмите Enter, чтобы закрыть...')
        except (EOFError, KeyboardInterrupt):
            pass
        return
    if os.name == 'nt':
        try:
            import ctypes

            details = traceback.format_exc()[-1600:]
            ctypes.windll.user32.MessageBoxW(
                None, details, 'Dice Lab — ошибка запуска', 0x10)
        except Exception:
            pass


def main():
    try:
        from dice_lab.app import main as run_app

        return run_app()
    except Exception:
        _report_error()
        return 1


if __name__ == '__main__':
    sys.exit(main())
