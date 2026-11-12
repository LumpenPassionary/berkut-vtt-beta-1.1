"""Отладчик стороны игрока: поднимает сервер и сажает за стол игрока.

Зачем: проверять интерфейс глазами игрока, не открывая второе окно руками
и не заводя второй компьютер. Скрипт поднимает сервер на свободном порту,
подключает к нему клиента с ролью «игрок», кладёт на стол тестовый мир и
показывает обычное окно игры — с панелью игрока, без панели мастера.

Запуск:

    python tools/debug_player.py

Полезные ключи:

    --theme light           светлая тема (по умолчанию тёмная)
    --no-demo               пустой мир, без карты и токенов
    --sheet путь.json       загрузить лист персонажа Long Story Short
    --screenshot файл.png   сохранить снимок окна и выйти (без показа окна)
    --keep                  не закрывать сервер после закрытия окна

Скриншот работает и без экрана (QT_QPA_PLATFORM=offscreen), поэтому его
можно снимать на сервере без графики.
"""
import argparse
import os
import socket
import sys
import tempfile
import threading
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen"
                      if "--screenshot" in sys.argv else "")

from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QApplication

from client.main_window import MainWindow
from client.network_client import NetworkClient
from server.game_server import GameServer
from shared.models import GameState, MapData, Player, Role, Token
from shared.protocol import MessageType

MAP_CANDIDATES = (
    os.path.join(BASE_DIR, "sample_map.png"),
    os.path.join(BASE_DIR, "design", "sample_map.png"),
)
SHEET_CANDIDATE = os.path.join(BASE_DIR, "sample_sheet.json")


def free_port() -> int:
    """Свободный порт, чтобы отладчик не мешал запущенной игре."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def demo_state(map_path: str) -> GameState:
    """Тестовый мир: подземелье, группа игроков и два гоблина."""
    state = GameState(session_id="debug")
    width, height = 1600, 1066
    state.maps["m1"] = MapData(id="m1", name="Курган", image_path=map_path,
                               width=width, height=height,
                               grid_cols=30, grid_rows=20, grid_visible=True)
    state.current_map_id = "m1"
    state.players["dm"] = Player(id="dm", nickname="Мастер", role=Role.DM)
    state.players["p1"] = Player(id="p1", nickname="Аня", role=Role.PLAYER)
    state.players["p2"] = Player(id="p2", nickname="Игорь", role=Role.PLAYER)

    state.tokens["me"] = Token(
        id="me", name="Аня — друид", image_path="", x=520, y=560, map_id="m1",
        kind="player", owner_id="p1", hp=34, hp_max=44, ac=15)
    for i, (name, x, y, hp, hp_max) in enumerate(
            (("Торин — воин", 300, 620, 22, 44),
             ("Лира — бард", 640, 400, 18, 27))):
        state.tokens[f"c{i}"] = Token(
            id=f"c{i}", name=name, image_path="", x=x, y=y, map_id="m1",
            kind="player", owner_id="p2", hp=hp, hp_max=hp_max, ac=16)
    for i, (name, x, y, hp, ac) in enumerate(
            (("Гоблин-стражник", 900, 380, 12, 14),
             ("Гоблин-лучник", 1080, 520, 9, 13))):
        state.tokens[f"g{i}"] = Token(
            id=f"g{i}", name=name, image_path="", x=x, y=y, map_id="m1",
            kind="actor", hp=hp, hp_max=hp, ac=ac)

    state.combat_active = True
    state.combat_order = ["me", "g0", "c0", "g1", "c1"]
    state.combat_turn = 0
    state.combat_round = 3
    for tid, initiative in (("me", 19), ("g0", 17), ("c0", 14),
                            ("g1", 11), ("c1", 8)):
        state.tokens[tid].initiative = initiative
    return state


def find_map() -> str:
    for path in MAP_CANDIDATES:
        if os.path.isfile(path):
            return path
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Отладчик стороны игрока")
    parser.add_argument("--theme", choices=("dark", "light"), default="dark")
    parser.add_argument("--no-demo", action="store_true",
                        help="не подкладывать тестовую карту и токены")
    parser.add_argument("--sheet", default=None,
                        help="лист персонажа LSS (.json)")
    parser.add_argument("--screenshot", default=None,
                        help="сохранить снимок окна и выйти")
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=950)
    parser.add_argument("--keep", action="store_true",
                        help="не останавливать сервер при закрытии")
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    app.setApplicationName("VTT — отладка игрока")

    port = free_port()
    data_root = tempfile.mkdtemp(prefix="vtt_debug_")
    server = GameServer("127.0.0.1", port, data_root=data_root)
    threading.Thread(target=server.start, daemon=True).start()
    time.sleep(0.4)   # серверу нужно мгновение, чтобы открыть порт

    client = NetworkClient()

    # Игрок подключается как обычный участник: слева у него не будет
    # панели мастера — это и проверяем.
    info = {"player_id": "p1", "nickname": "Аня",
            "mode": "client", "role": "player", "avatar_path": None}
    window = MainWindow(info, client, server=server)
    client.set_message_callback(window.on_network_message)

    window.resize(args.width, args.height)
    window.apply_theme(args.theme)
    window.show()

    if not client.connect("127.0.0.1", port):
        print("Не удалось подключиться к тестовому серверу", file=sys.stderr)
        server.stop()
        return 1
    client.send_message(MessageType.CONNECT, info)

    # Даём серверу отдать FULL_STATE, затем подкладываем демо-мир.
    def seed():
        if not args.no_demo:
            state = demo_state(find_map())
            # Состояние кладём прямо в сервер: у отладчика нет мастера,
            # который бы его создал обычными командами.
            with server.lock:
                players = server.game_state.players
                server.game_state = state
                server.game_state.players = players
            server.broadcast(MessageType.FULL_STATE, server.build_full_state())
        sheet = args.sheet or (SHEET_CANDIDATE
                               if os.path.isfile(SHEET_CANDIDATE) else None)
        if sheet:
            window.character_sheet.load_file(sheet)
            window.sheet_group.setChecked(True)
        window.canvas.fit_map_to_screen()

    QTimer.singleShot(700, seed)

    if args.screenshot:
        def shoot():
            app.processEvents()
            pix = QPixmap(window.size())
            window.render(pix)
            pix.save(args.screenshot)
            print(f"Снимок сохранён: {args.screenshot} "
                  f"({pix.width()}x{pix.height()})")
            window.close()
            app.quit()

        QTimer.singleShot(2200, shoot)
        code = app.exec_()
    else:
        print(f"Сервер: 127.0.0.1:{port}  |  данные: {data_root}")
        print("Окно игрока открыто. Закройте его, чтобы выйти.")
        code = app.exec_()

    if not args.keep:
        try:
            server.stop()
        except Exception:
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
