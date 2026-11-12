"""
Точка входа виртуального стола D&D.

Один и тот же исполняемый файл позволяет и создать игру (DM),
и подключиться к чужой игре как игрок — выбор делается в стартовом
окне. Внешние сервисы не используются, всё работает в локальной сети.
"""
import sys
import os
import logging

# Путь к модулям проекта (важно для сборки в один исполняемый файл)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QMessageBox

from client import app_icon
from client.connection_dialog import ConnectionDialog
from client.network_client import NetworkClient
from client.win_frame import install_window_frame
from server.game_server import GameServer
from shared.app_paths import home_dir as data_home
from shared.exclog import install_excepthook


def setup_logging():
    """Журнал приложения в файл (локально, рядом с данными)"""
    log_dir = data_home()
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(os.path.join(log_dir, "app.log"), encoding="utf-8"),
        ],
    )
    # Ошибка в слоте Qt не должна убивать приложение: PyQt5 завершает
    # процесс молча, и окно просто закрывается. Пишем трассировку и живём.
    install_excepthook()


def saved_renderer():
    """Движок из настроек графики (settings.json): auto, gpu или cpu.

    Читаем файл здесь, а не через окно настроек: движок нужен до того, как
    окно вообще создано. Битый файл — это «auto», а не ошибка запуска.
    """
    import json
    from shared import graphics as graphics_rules
    try:
        with open(os.path.join(data_home(), "settings.json"),
                  encoding="utf-8") as handle:
            cfg = json.load(handle)
    except (OSError, ValueError):
        return "auto"
    if not isinstance(cfg, dict):
        return "auto"
    return graphics_rules.normalize(cfg.get("graphics"))["renderer"]


def choose_renderer():
    """Решить до создания холста: стол рисует видеокарта или процессор.

    QOpenGLWidget выбирается один раз, при импорте `client.game_canvas`,
    поэтому проверку OpenGL надо сделать раньше. Если драйвер не дал
    контекст, честно откатываемся на растр — стол не должен остаться пустым.

    «Видеокарта» в настройках — это ещё и честный отказ от проверки: мастер
    мог сам выбрать OpenGL, и молча подменять его нельзя. Но если контекст
    не создался, всё равно падаем на процессор: пустой стол хуже.
    """
    if os.environ.get("VTT_RENDERER"):
        return
    wanted = saved_renderer()
    if wanted == "cpu":
        os.environ["VTT_RENDERER"] = "cpu"
        logging.info("Движок отрисовки: процессор (выбран в настройках)")
        return
    ok = False
    try:
        from PyQt5.QtGui import QOpenGLContext
        probe = QOpenGLContext()
        ok = bool(probe.create() and probe.isValid())
        probe.doneCurrent()
    except Exception:
        ok = False
    os.environ["VTT_RENDERER"] = "gpu" if ok else "cpu"
    if not ok:
        logging.warning("OpenGL недоступен — стол рисует процессор")


def main():
    setup_logging()

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    # Движок стола выбирается после QApplication и до импорта окна, которое
    # создаёт холст: потом базовый класс уже не поменять.
    choose_renderer()
    from client import game_canvas
    from client.main_window import MainWindow
    logging.info("Отрисовка стола: %s", game_canvas.renderer_name())
    app.setApplicationName("Berkut VTT dnd5e")
    app.setApplicationDisplayName("Berkut VTT dnd5e")
    app.setWindowIcon(app_icon())
    # Заголовки окон — в тон приложению: иначе над тёмным интерфейсом
    # оставалась белая полоса заголовка, которую рисует сама Windows.
    install_window_frame(app)

    dialog = ConnectionDialog()
    if dialog.exec_() != dialog.Accepted:
        return 0

    info = dialog.connection_info
    if not info:
        return 0

    client = NetworkClient()
    server = None

    if info["mode"] == "host":
        try:
            port = int(info["port"])
            server = GameServer(
                "0.0.0.0", port,
                # Модуль освещения — галочка стартового окна у мастера:
                # он же решает, влияет ли темнота на скрытность и Восприятие.
                lighting_enabled=bool(info.get('lighting_enabled', False)))
            server.start()
        except Exception as e:
            QMessageBox.critical(None, "Ошибка",
                                f"Не удалось запустить сервер на порту {info['port']}:\n{e}\n\n"
                                "Возможно, порт занят — выберите другой.")
            return 1

        # Хост подключается к собственному серверу как обычный клиент
        if not client.connect("127.0.0.1", port):
            QMessageBox.critical(None, "Ошибка", "Не удалось подключиться к своему серверу")
            server.stop()
            return 1
    else:
        if not client.connect(info["host"], int(info["port"])):
            QMessageBox.critical(
                None, "Ошибка подключения",
                f"Не удалось подключиться к {info['host']}:{info['port']}.\n\n"
                "Проверьте:\n"
                "• хост создал игру и адрес указан верно;\n"
                "• порт совпадает у хоста и игрока;\n"
                "• брандмауэр разрешает подключение;\n"
                "• для Radmin VPN используется его адрес и он подключён.")
            return 1

    window = MainWindow(info, client, server)
    # Строка о машине в журнал: по ней видно, на чём именно лагало, если
    # мастер пришлёт app.log. Отчёт о памяти тоже пишем — но только в лог.
    from shared import hwinfo
    logging.info("Машина: %s", hwinfo.system_description())
    logging.info("Память при запуске: %.0f МБ, буферы холста %s",
                 hwinfo.current_pid_memory_mb(),
                 hwinfo.format_bytes(window.canvas.buffer_bytes()))
    window.show()
    rc = app.exec_()
    # Страховка для выхода не через closeEvent (например, QApplication.quit):
    # сетевые потоки не должны пережить цикл событий и позвать удалённое окно.
    try:
        client.shutdown()
    except Exception:
        pass
    if server is not None and getattr(server, "running", False):
        server.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
