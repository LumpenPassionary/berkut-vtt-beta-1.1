"""Отладочный клиент игрока: подключиться к своей игре как участник.

Зачем: проверять сторону игрока, не заводя второй компьютер. Вы запускаете
`main.py` и создаёте игру (DM), а затем этим скриптом открываете второе окно —
уже как игрок, подключённый к тому же серверу. Так видно ровно то, что видит
игрок: свою панель с листом персонажа, чат, ход боя и анимации.

В отличие от `tools/debug_player.py` (который поднимает СВОЙ изолированный
сервер с демо-миром), этот скрипт идёт в уже запущенную игру — обычным
подключением по сети.

Запуск (после того как main.py создал игру на порту 25565):

    python debug_player.py

Полезные ключи:

    --host 127.0.0.1      адрес хоста (по умолчанию localhost)
    --port 25565          порт сервера (по умолчанию 25565)
    --nick Имя            имя игрока (по умолчанию «Отладчик»)
    --dm                  подключиться с правами DM (по умолчанию игрок)
    --3d / --no-3d        включить или выключить 3D-кубики Dice Lab;
                          без ключа берётся галочка из settings.json
"""
import argparse
import json
import logging
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from PyQt5.QtWidgets import QApplication, QMessageBox

from client.main_window import MainWindow
from client.network_client import NetworkClient
from shared.app_paths import home_dir as data_home
from shared.exclog import install_excepthook
from shared.models import generate_id


def saved_enable_3d():
    """Галочка «Включить 3д дайсы» из settings.json (если файла нет — False)."""
    try:
        path = os.path.join(data_home(), "settings.json")
        with open(path, encoding="utf-8") as handle:
            return bool(json.load(handle).get("enable_3d", False))
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Подключиться к своей игре как игрок (отладка)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="адрес хоста (по умолчанию 127.0.0.1)")
    parser.add_argument("--port", type=int, default=25565,
                        help="порт сервера (по умолчанию 25565)")
    parser.add_argument("--nick", default="Отладчик",
                        help="имя игрока")
    parser.add_argument("--dm", action="store_true",
                        help="подключиться с правами DM")
    # Галочка из стартового окна живёт в settings.json этого пользователя.
    # Отладчик диалог не показывает, поэтому по умолчанию повторяет её;
    # ключи --3d/--no-3d нужны, чтобы проверить оба режима без правки файла.
    parser.add_argument("--3d", dest="enable_3d", action="store_true",
                        default=None, help="включить 3D-кубики Dice Lab")
    parser.add_argument("--no-3d", dest="enable_3d", action="store_false",
                        help="оставить кнопки кубиков")
    args = parser.parse_args()
    if args.enable_3d is None:
        args.enable_3d = saved_enable_3d()

    app = QApplication(sys.argv)
    app.setApplicationName("D&D VTT — отладка игрока")
    # Журнал и защита от «тихого» падения в слоте Qt — как в main.py:
    # без хука ошибка в обработчике просто закрывает окно отладчика.
    log_dir = data_home()
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(os.path.join(log_dir, "app.log"),
                                      encoding="utf-8")],
    )
    install_excepthook()

    info = {
        "mode": "join",
        "nickname": args.nick,
        "host": args.host,
        "port": args.port,
        "avatar_path": None,
        "player_id": generate_id(),
        "role": "dm" if args.dm else "player",
        "enable_3d": bool(args.enable_3d),
    }

    client = NetworkClient()
    if not client.connect(args.host, args.port):
        QMessageBox.critical(
            None, "Нет подключения",
            f"Не удалось подключиться к {args.host}:{args.port}.\n\n"
            "Сначала запустите main.py и создайте игру (DM), затем повторите\n"
            "запуск debug_player.py с тем же портом.")
        return 1

    window = MainWindow(info, client)
    window.resize(1500, 900)
    window.show()

    code = app.exec_()
    try:
        client.disconnect()
    except Exception:
        pass
    return code


if __name__ == "__main__":
    sys.exit(main())
