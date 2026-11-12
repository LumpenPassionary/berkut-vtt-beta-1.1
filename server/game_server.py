"""
Игровой сервер виртуального стола D&D.

Сервер — единственный источник истины: клиенты присылают намерения,
сервер проверяет права и рассылает результат всем участникам.
Скрытые токены (глазок «скрыть») не рисуются у участников сессии.
"""
import collections
import copy
import math
import queue
import re
import socket
import threading
import logging
import sys
import os
import time
from typing import Dict, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.models import (GameState, Player, Role, MapData, Token, MapObject,
                           AudioTrack, generate_id, normalize_time_of_day)
from shared import lighting as lighting_rules
from shared import walls as wall_rules
from shared.protocol import Protocol, MessageType
from shared.grid import clamp_cells
from shared import class_features
from shared import map_text as map_text_rules
from shared import combat as combat_rules
from shared import conditions as conditions_rules
from shared import dice
from shared import lss_format
from shared import use_action
from shared import spell_effects
from shared import spells as spell_rules
from shared import sheet_items
from shared import asset_transport as at
from shared import token_stats as token_stat
from shared import special_attacks as special_attack_rules
from shared import token_kind
from shared import vision as vision_rules
from shared import templates as aoe_templates
from shared import world_store
from shared.assets import get_cache_dir
import secrets


# ----------------------------------------------------------------------
# Пределы нагрузки и таймауты.
#
# Игра идёт в локальной сети, но сервер не должен падать от одного
# неаккуратного клиента: каждое соединение — поток, а каждое сообщение —
# память. Значения можно переопределить переменными окружения.
# ----------------------------------------------------------------------
def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


#: Сколько игроков (включая мастера) сервер принимает одновременно.
MAX_CLIENTS = _env_int('VTT_MAX_CLIENTS', 16, 2, 64)
#: Сколько соединений может висеть до CONNECT (рукопожатия).
MAX_HANDSHAKES = _env_int('VTT_MAX_HANDSHAKES', 8, 1, 64)
#: Сколько секунд ждать CONNECT, прежде чем закрыть пустое соединение.
HANDSHAKE_TIMEOUT = float(_env_int('VTT_HANDSHAKE_TIMEOUT', 15, 3, 120))
#: Очередь исходящих кадров на клиента. Медленный клиент не тормозит стол:
#: переполнение очереди означает, что он не успевает читать, и его отключают.
MAX_OUTBOX = _env_int('VTT_MAX_OUTBOX', 2048, 64, 65536)
#: Сколько байт может ждать в очереди одного клиента. Счётчик кадров сам по
#: себе не спасает: 2048 кадров по 4 МБ — это гигабайты памяти сервера.
MAX_OUTBOX_BYTES = _env_int('VTT_MAX_OUTBOX_MB', 64, 4, 1024) * 1024 * 1024
#: Лимит команд от одного клиента: устойчиво и короткой вспышкой.
RATE_PER_SECOND = float(_env_int('VTT_RATE_PER_SEC', 400, 20, 5000))
RATE_BURST = float(_env_int('VTT_RATE_BURST', 800, 20, 10000))
#: Журнал стола в памяти: полный не растёт без конца.
MAX_LOG_ENTRIES = _env_int('VTT_MAX_LOG', 1000, 50, 5000)
#: Сколько текстовых надписей можно поставить на одну карту. Подпись —
#: не бесконечный список: иначе карта превращается в мешанину, а состояние
#: мира растёт на каждой правке.
MAX_MAP_TEXTS = _env_int('VTT_MAX_MAP_TEXTS', 200, 10, 2000)
#: Отдельные автосохранения: интервал и сколько снимков хранить.
#: Снимки не заменяют миры и не требуют нажатия «Сохранить».
AUTOSAVE_INTERVAL = _env_int('VTT_AUTOSAVE_SEC', 60, 10, 3600)
AUTOSAVE_KEEP = _env_int('VTT_AUTOSAVE_KEEP', 10, 2, 100)


class ClientHandler:
    """Обслуживание одного подключённого клиента"""

    def __init__(self, conn: socket.socket, addr: tuple, server: 'GameServer'):
        self.conn = conn
        self.addr = addr
        self.server = server
        self.player_id: Optional[str] = None
        self.running = True
        self.send_lock = threading.Lock()
        self.transfer_token = secrets.token_urlsafe(32)
        # Отправка идёт отдельным потоком через ограниченную очередь: рассылка
        # не ждёт медленный сокет и не блокирует остальной стол.
        self.outbox: queue.Queue = queue.Queue(maxsize=MAX_OUTBOX)
        self._queued_bytes = 0
        self._queue_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False
        self.writer = threading.Thread(target=self._writer_loop, daemon=True,
                                       name=f'writer-{addr[0]}:{addr[1]}')
        self.writer.start()
        # Простой лимит частоты: токены пополняются временем.
        self._tokens = RATE_BURST
        self._tokens_at = time.monotonic()

    # ------------------------------------------------------------------
    # Транспорт
    # ------------------------------------------------------------------
    def send_message(self, msg_type: MessageType, data: dict):
        try:
            payload = Protocol.encode_message(msg_type, data)
        except Exception as e:
            logging.error(f"Не удалось собрать сообщение для {self.addr}: {e}")
            return
        self.send_raw(payload)

    def send_raw(self, payload: bytes):
        """Поставить готовый кадр в очередь своего потока-отправителя.

        Так рассылка одному зависшему клиенту не держит остальных: очередь
        ограничена, а переполнение значит, что клиент не читает сеть.
        """
        if not self.running or self._closed:
            return
        with self._queue_lock:
            overflow = self._queued_bytes + len(payload) > MAX_OUTBOX_BYTES
            if not overflow:
                self._queued_bytes += len(payload)
        if overflow:
            # Кадров в очереди немного, но они тяжёлые: клиент не читает сеть.
            logging.warning("Очередь %s переполнена по байтам — отключён",
                            self.addr)
            self._drop_slow()
            return
        try:
            self.outbox.put_nowait(payload)
        except queue.Full:
            with self._queue_lock:
                self._queued_bytes -= len(payload)
            logging.warning("Клиент %s не успевает читать — отключён", self.addr)
            self._drop_slow()

    def _writer_loop(self):
        while True:
            payload = self.outbox.get()
            if payload is None:
                break
            with self._queue_lock:
                self._queued_bytes = max(0, self._queued_bytes - len(payload))
            try:
                self.conn.sendall(payload)
            except OSError as exc:
                logging.info("Отправка клиенту %s прервана: %s", self.addr, exc)
                self.running = False
                break

    def _drop_slow(self):
        """Разорвать сокет, не дожидаясь очереди."""
        self.running = False
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self, flush: bool = True):
        """Закрыть соединение, по возможности дослав уже поставленные кадры."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self.running = False
        if flush:
            try:
                self.outbox.put_nowait(None)
            except queue.Full:
                pass
            if self.writer.is_alive():
                self.writer.join(timeout=1.0)
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass

    def _recv_exact(self, count: int) -> Optional[bytes]:
        buf = bytearray()
        while len(buf) < count:
            chunk = self.conn.recv(min(at.CHUNK, count - len(buf)))
            if not chunk:
                return None
            buf += chunk
        return buf

    def receive_full_message(self) -> Optional[bytes]:
        try:
            header = self._recv_exact(Protocol.HEADER_SIZE)
            if header is None:
                return None
            length = int.from_bytes(header, 'big')
            if length <= 0 or length > Protocol.MAX_CLIENT_MESSAGE_SIZE:
                logging.warning("Некорректный размер сообщения: %s", length)
                return None
            return self._recv_exact(length)
        except socket.timeout:
            logging.info("Клиент %s не ответил вовремя", self.addr)
            return None
        except OSError as e:
            # Обычное закрытие сокета (10054/10053) — не ошибка сервера:
            # игрок просто закрыл игру.
            logging.info("Соединение %s закрыто: %s", self.addr, e)
            return None
        except Exception as e:
            logging.error(f"Ошибка приёма: {e}")
            return None

    def _allow_message(self) -> bool:
        """Лимит частоты: корзина токенов на соединение."""
        now = time.monotonic()
        self._tokens = min(
            RATE_BURST,
            self._tokens + (now - self._tokens_at) * RATE_PER_SECOND)
        self._tokens_at = now
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True

    def handle(self):
        try:
            while self.running:
                raw = self.receive_full_message()
                if raw is None:
                    break
                if not self._allow_message():
                    logging.warning("Слишком частые сообщения от %s — отключён",
                                    self.addr)
                    self.send_message(MessageType.ERROR, {
                        'message': 'Слишком много сообщений — соединение закрыто'})
                    break
                try:
                    msg_type, data = Protocol.decode_message(raw)
                    self.process_message(msg_type, data)
                except Exception as e:
                    # Одно битое сообщение не должно рвать сессию: отвечаем
                    # ошибкой и ждём следующее.
                    logging.error(f"Ошибка обработки сообщения: {e}")
                    self.send_message(MessageType.ERROR,
                                      {'message': 'Некорректное сообщение'})
        finally:
            self.cleanup()

    # ------------------------------------------------------------------
    # Маршрутизация сообщений
    # ------------------------------------------------------------------
    def process_message(self, msg_type: MessageType, data: dict):
        if msg_type == MessageType.ASSET_TRANSFER:
            self.serve_asset(data)
            self.running = False
            return
        routes = {
            MessageType.CONNECT: self.handle_connect,
        }
        if msg_type in routes:
            routes[msg_type](data)
            return

        if self.player_id is None:
            return
        if data.get('asset_ref'):
            if not at.cached(self.server.asset_root, data['asset_ref']):
                self.send_message(MessageType.ERROR, {'message': 'Resource not uploaded'})
                return

        # Команды, требующие роли ДМ (проверка на сервере, а не в интерфейсе)
        dm_only = {
            MessageType.START_GAME: self.server.start_game,
            MessageType.ADD_MAP: self.server.add_map,
            MessageType.CHANGE_MAP: self.server.change_map,
            MessageType.UPDATE_MAP: self.server.update_map,
            MessageType.REMOVE_MAP: self.server.remove_map,
            MessageType.ADD_MAP_OBJECT: self.server.add_map_object,
            MessageType.UPDATE_MAP_OBJECT: self.server.update_map_object,
            MessageType.REMOVE_MAP_OBJECT: self.server.remove_map_object,
            MessageType.ADD_TOKEN: self.server.add_token,
            MessageType.REMOVE_TOKEN: self.server.remove_token,
            # UPDATE_TOKEN намеренно НЕ здесь: игроку нужно поворачивать
            # своего персонажа (R) и менять свои угол/дальность зрения.
            # Обработчик сам пускает мастера ко всему, а игрока — только
            # к facing/vision своего токена (см. update_token).
            # Перенос токена на другую карту — только мастер: игрок не
            # переставляет сцены и не уводит чужих персонажей.
            MessageType.MOVE_TOKEN_MAP: self.server.move_token_map,
            MessageType.SET_INITIATIVE: self.server.set_initiative,
            MessageType.START_COMBAT: self.server.start_combat,
            MessageType.END_COMBAT: self.server.end_combat,
            MessageType.NEXT_TURN: self.server.next_turn,
            MessageType.UPDATE_GRID: self.server.update_grid,
            MessageType.UPDATE_TIME: self.server.update_time,
            MessageType.SET_SNAP: self.server.set_snap,
            MessageType.SET_VISION: self.server.set_vision,
            MessageType.UPDATE_LIGHTS: self.server.update_lights,
            # Стены и проёмы — такая же настройка стола, как свет: игрок
            # их только видит (и не проходит сквозь них).
            MessageType.UPDATE_WALLS: self.server.update_walls,
            # Замок двери — событие мастера, как и сами стены.
            MessageType.SET_DOOR_LOCK: self.server.set_door_lock,
            MessageType.PLAY_SOUND: self.server.play_sound,
            MessageType.PLAY_MUSIC: self.server.play_music,
            MessageType.ADD_AUDIO: self.server.add_audio,
            MessageType.ADD_SLOT: self.server.add_slot,
            MessageType.DELETE_SLOT: self.server.delete_slot,
            MessageType.ASSIGN_SLOT_TOKEN: self.server.assign_slot_token,
            MessageType.MY_ACTORS_UPDATE: self.server.update_my_actor,
            MessageType.MY_ACTORS_REMOVE: self.server.remove_my_actor,
            MessageType.MY_ACTORS_CLEAR: self.server.clear_my_actors,
            MessageType.DRAW_OP: self.server.draw_op,
            MessageType.CLEAR_DRAWINGS: self.server.clear_drawings,
            MessageType.UNDO_DRAWING: self.server.undo_drawing,
            # Надписи на карте — такой же инструмент мастера, как кисть:
            # вкладки «Рисование» у игрока нет вовсе.
            MessageType.ADD_MAP_TEXT: self.server.add_map_text,
            MessageType.UPDATE_MAP_TEXT: self.server.update_map_text,
            MessageType.REMOVE_MAP_TEXT: self.server.remove_map_text,
            MessageType.SAVE_WORLD: self.server.save_world,
            MessageType.LOAD_WORLD: self.server.load_world,
            MessageType.RESTORE_AUTOSAVE: self.server.restore_autosave,
            # Отдых всего стола — событие мастера, не игрока.
            MessageType.REST: self.server.rest,
        }
        # Команды, доступные всем
        everyone = {
            MessageType.SHEET_UPDATE: self.server.sheet_update,
            # Поворот токена и правка его зрения своим игроком.
            MessageType.UPDATE_TOKEN: self.server.update_token,
            MessageType.COPY_TOKEN: self.server.copy_token,
            MessageType.GRANT_ITEM: self.server.grant_item,
            MessageType.GRANT_SPELL: self.server.grant_spell,
            MessageType.MOVE_TOKEN: self.server.move_token,
            MessageType.DASH: self.server.dash,
            MessageType.SPEED_POTION: self.server.speed_potion,
            MessageType.TOGGLE_TOKEN_VISIBILITY: self.server.toggle_token_visibility,
            # Дверь может толкнуть и игрок: своим токеном, стоя рядом.
            MessageType.TOGGLE_DOOR: self.server.toggle_door,
            # Разведал комнату — игрок, а не только мастер: у стола эта
            # разведка общая и хранится на карте, поэтому доступна всем.
            MessageType.MARK_EXPLORED: self.server.mark_explored,
            MessageType.APPLY_DAMAGE: self.server.apply_damage,
            MessageType.APPLY_EFFECT: self.server.apply_effect,
            MessageType.CAST_SPELL: self.server.cast_spell,
            # Боевые действия: права проверяют сами обработчики —
            # игроку нужно атаковать своим токеном и кидать свои спасброски.
            MessageType.ROLL_ATTACK: self.server.roll_attack,
            MessageType.AREA_DAMAGE: self.server.area_damage,
            MessageType.ROLL_SAVE: self.server.roll_save,
            MessageType.ROLL_INITIATIVE: self.server.roll_initiative,
            MessageType.DEATH_SAVE: self.server.death_save,
            MessageType.SET_CONDITIONS: self.server.set_conditions,
            MessageType.STEALTH_CHECK: self.server.stealth_check,
            MessageType.SPECIAL_ATTACK: self.server.special_attack,
            # Отмена атаки: вернуть снимок хитов и кружков смерти. Права
            # проверяет обработчик — игроку тоже нужно отменять свой удар.
            MessageType.RESTORE_TOKEN: self.server.restore_token,
            MessageType.CHAT_MESSAGE: self.server.handle_chat,
            MessageType.ROLL_DICE: self.server.handle_dice_roll,
            MessageType.DISCONNECT: self.handle_disconnect,
            MessageType.CLAIM_SLOT: self.server.claim_slot,
            MessageType.RELEASE_SLOT: self.server.release_slot,
        }

        if msg_type in dm_only:
            if self.server.check_permission(self.player_id, require_dm=True):
                dm_only[msg_type](self.player_id, data)
            else:
                self.send_message(MessageType.PERMISSION_DENIED,
                                  {'action': msg_type.value})
                logging.warning(f"Отказ в доступе: {self.addr} → {msg_type.value}")
        elif msg_type in everyone:
            everyone[msg_type](self.player_id, data)

    def _reject(self, text: str):
        """Отказать во входе и закрыть соединение после отправки ошибки."""
        self.send_message(MessageType.ERROR, {'message': text})
        self.running = False
        logging.info("Вход отклонён: %s — %s", self.addr, text)

    def handle_connect(self, data: dict):
        """Проверить данные входа и зарегистрировать участника.

        Всё, что приходит из сети, проверяется до использования: раньше
        отсутствие поля падало исключением, а чужой идентификатор позволял
        выдать себя за уже подключённого мастера.
        """
        if not isinstance(data, dict):
            self._reject('Некорректные данные подключения')
            return
        if self.player_id is not None:
            self._reject('Повторный CONNECT на том же соединении')
            return
        player_id = str(data.get('player_id') or '').strip()
        nickname = str(data.get('nickname') or '').strip()
        role_raw = str(data.get('role') or '').strip()
        nickname = ''.join(ch for ch in nickname if ch.isprintable())[:32]
        try:
            role = Role(role_raw)
        except ValueError:
            self._reject('Неизвестная роль участника')
            return
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', player_id):
            self._reject('Некорректный идентификатор игрока')
            return
        if not nickname:
            nickname = 'Игрок' if role != Role.DM else 'Мастер'
        avatar_path = data.get('avatar_path')
        if avatar_path is not None:
            avatar_path = str(avatar_path)[:260]

        player = Player(
            id=player_id, nickname=nickname, role=role,
            avatar_path=avatar_path, connected=True,
        )
        # Слоты: если мастер их создал, игрок садится только на свободный.
        # Слотов нет — вход свободный, как раньше. Сначала слот, потом
        # регистрация: иначе отказ по слотам оставлял бы «игрока» в списке.
        slot_id = None
        if role != Role.DM:
            slot_id, error = self.server.reserve_slot(
                player_id, data.get('slot_id'))
            if error:
                self._reject(error)
                return

        ok, error = self.server.register_player(player, self)
        if not ok:
            self._reject(error)
            return
        self.player_id = player_id
        # Дальше соединение живёт без таймаута рукопожатия: игрок может
        # молчать хоть весь вечер, пока никто не ходит.
        try:
            self.conn.settimeout(None)
        except OSError:
            pass

        # Мир без слотов: если мастер подготовил ровно одного персонажа
        # с листом, отдаём его единственному игроку. Иначе лист лежит в
        # токене без владельца, и игрок видит пустую панель.
        auto_changed, auto_sheets = ([], [])
        if role != Role.DM and slot_id is None:
            auto_changed, auto_sheets = self.server.auto_assign_character(
                player_id)

        # Отдаём новому участнику полное актуальное состояние вместе
        # с самими файлами ресурсов (карты, токены, аватары)
        full = self.server.build_full_state()
        full["transfer_token"] = self.transfer_token
        self.send_message(MessageType.FULL_STATE, full)
        # Поздно подключившийся видит уже потраченные футы текущего хода.
        self.server._send_current_movement_state_to(self.player_id)
        with self.server.lock:
            slots = self.server._public_slots_locked()
        self.send_message(MessageType.SLOTS_STATE, {'slots': slots})
        # Игроку, подключившемуся позже, отдаём уже предзагруженную
        # библиотеку: призыв актёров не должен ждать отправки файлов.
        with self.server.lock:
            preloaded = [dict(item) for item in self.server.my_actors.values()]
        for item in preloaded:
            payload = {'id': item.get('id'), 'actor': item.get('actor')}
            if item.get('asset_ref'):
                payload['asset_ref'] = item['asset_ref']
            self.send_message(MessageType.MY_ACTORS_UPDATE, payload)

        for payload in auto_changed:
            self.server.broadcast(MessageType.UPDATE_TOKEN, payload)
        for payload in auto_sheets:
            self.server.broadcast(MessageType.SHEET_STATE, payload)
        self.server.broadcast(MessageType.PLAYER_JOINED,
                              {'player': player.to_dict()},
                              exclude=self.player_id)
        self.server.push_log(f"{player.nickname} подключился к игре")

    def serve_asset(self, data):
        self.conn.settimeout(60)
        try:
            with self.server.lock:
                owner = next((c for c in self.server.clients.values()
                              if secrets.compare_digest(c.transfer_token, str(data.get('token', '')))), None)
            if owner is None:
                raise PermissionError('Not authenticated')
            meta = data['meta']
            at.validate(meta)
            root = self.server.asset_root
            if data.get('op') == 'put':
                if not self.server.is_dm(owner.player_id):
                    raise PermissionError('DM only')
                hit = at.cached(root, meta)
                at.send_json(self.conn, {'ok': True, 'cached': bool(hit)})
                if not hit:
                    at.receive_file(self.conn, root, meta)
                    at.send_json(self.conn, {'ok': True})
            elif data.get('op') == 'get':
                with self.server.lock:
                    known = any(m == meta for m in self.server.assets.values())
                if not known:
                    raise PermissionError('Unknown resource')
                path = at.cached(root, meta)
                if not path:
                    raise FileNotFoundError('Resource missing')
                at.send_json(self.conn, {'ok': True})
                at.send_file(self.conn, path, meta['size'])
            else:
                raise ValueError('Invalid transfer operation')
        except Exception as exc:
            logging.warning('Resource transfer: %s', exc)
            try:
                at.send_json(self.conn, {'ok': False, 'error': str(exc)})
            except OSError:
                pass

    def handle_disconnect(self, player_id=None, data=None):
        """Клиент закрыл игру.

        Диспетчер зовёт обработчики как (player_id, data), поэтому сигнатуру
        надо держать такой же: раньше здесь был только self, и отключение
        падало с TypeError в журнале сервера.
        """
        self.running = False

    def cleanup(self):
        try:
            if self.player_id:
                self.server.remove_player(self.player_id)
        finally:
            self.close()
            self.server.release_connection()


class GameServer:
    """Основной игровой сервер"""

    def __init__(self, host: str = '0.0.0.0', port: int = 25565,
                 data_root: str = None, lighting_enabled: bool = False):
        self.host = host
        self.port = port
        #: Модуль освещения включён галочкой в стартовом окне у мастера.
        #: Выключен — ночь остаётся просто затемнением, а темнота не трогает
        #: скрытность и Восприятие: чужие правила не должны включаться сами.
        self.lighting_enabled = bool(lighting_enabled)
        # Привязка токенов к клеткам — настройка стола: мастер включает её
        # у себя, и токены у всех ходят по клеткам. Хранится на сервере, а
        # не в файле каждого клиента: иначе у игрока привязка жила бы своей
        # жизнью и он ходил бы мимо клеток.
        self.snap_tokens = False
        # Зрение игроков: «around» — всё вокруг токена в радиусе зрения,
        # «cone» — только сектор обзора. Настройка мастера для всего стола:
        # у каждого клиента своя копия расходилась с мастером, и игрок
        # продолжал видеть по-старому.
        self.vision_mode = "around"
        # Куда писать миры: общий с CLI корень данных (dataPath/config.json)
        self.data_root = data_root or world_store.get_data_root()
        self.server_socket: Optional[socket.socket] = None
        self.clients: Dict[str, ClientHandler] = {}
        self.game_state = GameState(session_id=generate_id())
        self.running = False
        self.game_started = False
        self.lock = threading.RLock()
        # Имя мира, из которого/в который сохраняли: попадает в метаданные
        # автоснимка, чтобы в списке восстановления было понятно, что это.
        self.current_world_id = ""
        self._autosave_stop = threading.Event()
        self._autosave_thread = None
        self._autosave_lock = threading.Lock()
        self._autosave_interval = AUTOSAVE_INTERVAL
        self._autosave_keep = AUTOSAVE_KEEP

        # Бюджет движения в бою. Это состояние текущего хода, а не мира:
        # хранить его в GameState и писать в сохранение не нужно. Ключ —
        # id токена, поэтому несколько своих токенов не путают счётчики.
        self._move_spent: Dict[str, float] = {}
        self._move_dash = set()
        self._move_haste_until: Dict[str, int] = {}
        self._move_haste_pending: Dict[str, int] = {}
        #: Плоская прибавка скорости в футах (Удар Зефира: +30 футов),
        #: в отличие от удвоения от Ускорения и зелья.
        self._move_bonus_until: Dict[str, int] = {}
        self._move_turn_signature = None

        # Скрытая атака плута — раз в ХОД, а не раз в атаку. Счётчик, как и
        # бюджет движения, живёт в памяти текущего боя: в мир он не пишется.
        self._sneak_used = set()
        self._sneak_signature = None
        # Особые атаки с перезарядкой 5–6: (token_id, id атаки) -> раунд
        # использования. Начало хода существа бросает d6 и снимает отметку.
        self._special_used: Dict[tuple, int] = {}

        # Хранилище бинарных ресурсов: id ресурса -> SHA-256 metadata (not file contents).
        # Нужно, чтобы игрок, подключившийся позже, тоже получил картинки.
        self.assets: Dict[str, dict] = {}
        # Предзагруженные «Мои актёры» мастера: id 'myactor:<id>' -> данные.
        self.my_actors: Dict[str, dict] = {}

        self.asset_root = os.path.join(get_cache_dir(), "server")
        # Журнал ограничен: сервер может работать часами, и список без
        # предела — утечка памяти. Полное состояние отдаёт последние 200.
        self.log_entries = collections.deque(maxlen=MAX_LOG_ENTRIES)
        # Сколько игроков принимаем и сколько соединений вообще висит.
        self.max_clients = MAX_CLIENTS
        self.max_connections = MAX_CLIENTS + MAX_HANDSHAKES
        self._open_connections = 0
        self._connections_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------
    def start(self):
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(16)
        self.running = True
        logging.info(f"Сервер запущен на {self.host}:{self.port}")
        threading.Thread(target=self.accept_connections, daemon=True).start()
        self._autosave_stop.clear()
        self._autosave_thread = threading.Thread(
            target=self._autosave_loop, daemon=True, name='vtt-autosave')
        self._autosave_thread.start()

    def _register_connection(self) -> bool:
        """Учесть новое TCP-соединение. False — сервер перегружен."""
        with self._connections_lock:
            if self._open_connections >= self.max_connections:
                return False
            self._open_connections += 1
            return True

    def release_connection(self):
        with self._connections_lock:
            self._open_connections = max(0, self._open_connections - 1)

    def accept_connections(self):
        while self.running:
            try:
                conn, addr = self.server_socket.accept()
            except OSError:
                break
            if not self._register_connection():
                # Соединение сверх лимита даже не становится потоком:
                # говорим причину и закрываем, иначе сокет-флуд съест память.
                logging.warning("Слишком много соединений: %s", addr)
                try:
                    conn.settimeout(2.0)
                    conn.sendall(Protocol.encode_message(
                        MessageType.ERROR,
                        {'message': 'Сервер перегружен, попробуйте позже'}))
                except OSError:
                    pass
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass
                continue
            try:
                # Низкая задержка важнее экономии пакетов: команды мелкие,
                # а стол ждёт отклик сразу.
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                conn.settimeout(HANDSHAKE_TIMEOUT)
                logging.info(f"Новое подключение: {addr}")
                handler = ClientHandler(conn, addr, self)
                threading.Thread(target=handler.handle, daemon=True).start()
            except Exception as e:
                self.release_connection()
                logging.error(f"Ошибка приёма соединения: {e}")
                try:
                    conn.close()
                except OSError:
                    pass

    def stop(self):
        self.running = False
        # Сначала останавливаем таймер, потом делаем последний снимок:
        # иначе он мог бы одновременно писать тот же файл.
        self._autosave_stop.set()
        thread = self._autosave_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        self._autosave_thread = None
        try:
            if self.server_socket:
                self.server_socket.close()
        except Exception:
            pass
        with self.lock:
            handlers = list(self.clients.values())
        for handler in handlers:
            try:
                handler.conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        # Последний снимок при штатном закрытии: даже если таймер не успел
        # сработать после последней правки, работа не пропадёт.
        self.autosave_now('shutdown')
        logging.info("Сервер остановлен")

    # ------------------------------------------------------------------
    # Отдельные автосохранения
    # ------------------------------------------------------------------
    def _autosave_loop(self):
        """Фоновый таймер: снимок раз в AUTOSAVE_INTERVAL секунд."""
        while not self._autosave_stop.wait(self._autosave_interval):
            self.autosave_now('timer')

    def autosave_now(self, reason: str = 'manual'):
        """Сделать автоснимок текущего состояния.

        Пустой стол не сохраняем: иначе первый же запуск вытеснил бы из
        ротации полезные снимки пустышкой. Одинаковое состояние тоже не
        пишем — save_autosave сам сравнивает хэш.
        """
        try:
            with self._autosave_lock:
                # Снимок состояния снимаем под lock (чтобы не поймать
                # наполовину применённую правку), а хэш и запись файла
                # делаем уже без него: диск не должен тормозить стол.
                with self.lock:
                    state = self.game_state
                    has_content = bool(state.maps or state.tokens
                                       or state.map_objects or state.draw_ops
                                       or state.audio_tracks or state.slots)
                    if not has_content:
                        return None
                    state_dict = world_store.state_to_world_dict(
                        state, assets=self.assets)
                    world_id = self.current_world_id
                return world_store.save_autosave_dict(
                    state_dict, self.data_root,
                    world_id=world_id, keep=self._autosave_keep)
        except (OSError, ValueError, TypeError) as exc:
            logging.warning("Автосохранение не создано (%s): %s", reason, exc)
            return None

    # ------------------------------------------------------------------
    # Участники
    # ------------------------------------------------------------------
    def register_player(self, player: Player,
                        handler: ClientHandler):
        """Занять место за столом. Возвращает (получилось, причина)."""
        with self.lock:
            if player.id in self.clients:
                return False, ('Этот игрок уже подключён. Закройте старую '
                               'игру или подождите отключения')
            if player.role == Role.DM and any(
                    p.role == Role.DM
                    for p in self.game_state.players.values()):
                return False, 'Мастер уже за столом — второго сервер не пускает'
            if len(self.clients) >= self.max_clients:
                return False, (f'Сервер заполнен: {self.max_clients} '
                               'участников')
            self.game_state.players[player.id] = player
            self.clients[player.id] = handler
        logging.info(f"Игрок подключился: {player.nickname} ({player.role.value})")
        return True, ''

    def add_player(self, player: Player, handler: ClientHandler):
        """Совместимость: регистрация без разбора причины."""
        return self.register_player(player, handler)

    def remove_player(self, player_id: str):
        with self.lock:
            player = self.game_state.players.get(player_id)
            if player:
                player.connected = False
                logging.info(f"Игрок отключился: {player.nickname}")
            self.clients.pop(player_id, None)
            self.game_state.players.pop(player_id, None)
        self.broadcast(MessageType.PLAYER_LEFT, {'player_id': player_id})
        if player:
            self.push_log(f"{player.nickname} покинул игру")

    def broadcast(self, msg_type: MessageType, data: dict, exclude: str = None):
        """Разослать всем. Кадр кодируется ОДИН раз на всех.

        Раньше каждый клиент заново сериализовал одно и то же состояние:
        при шести игроках и большой карте это лишняя работа в потоке, который
        держит остальных.
        """
        with self.lock:
            targets = list(self.clients.items())
        if not targets:
            return
        payload = None
        for player_id, client in targets:
            if player_id == exclude:
                continue
            if hasattr(client, 'send_raw'):
                if payload is None:
                    try:
                        payload = Protocol.encode_message(msg_type, data)
                    except (TypeError, ValueError) as exc:
                        logging.error('Не удалось разослать %s: %s',
                                      msg_type.value, exc)
                        return
                client.send_raw(payload)
            else:
                # Заглушки в тестах живут с прежним интерфейсом.
                client.send_message(msg_type, data)

    def send_error(self, player_id: str, text: str):
        """Отправить ошибку одному клиенту (если он ещё подключён)"""
        client = self.clients.get(player_id)
        if client:
            client.send_message(MessageType.ERROR, {'message': text})

    def deny_action(self, player_id: str, action):
        """Ответить отказом по правам — так же, как это делает диспетчер.

        Кадр только ставится в очередь (send_raw не блокирует), поэтому
        отказ можно отправлять и под замком состояния.
        """
        client = self.clients.get(player_id)
        if client is None:
            return
        client.send_message(MessageType.PERMISSION_DENIED, {
            'action': action.value if isinstance(action, MessageType)
            else str(action)})

    def push_log(self, text: str):
        """Разослать запись журнала всем участникам (журнал ограничен)."""
        self.log_entries.append(text)
        self.broadcast(MessageType.LOG_ENTRY, {'text': text})

    # ------------------------------------------------------------------
    # Права
    # ------------------------------------------------------------------
    def check_permission(self, player_id: str, require_dm: bool = False) -> bool:
        player = self.game_state.players.get(player_id)
        if not player:
            return False
        return not (require_dm and player.role != Role.DM)

    def is_dm(self, player_id: str) -> bool:
        player = self.game_state.players.get(player_id)
        return bool(player and player.role == Role.DM)

    # ------------------------------------------------------------------
    # Слоты игроков
    #
    # Слот — это место за столом: мастер создаёт его заранее (даже когда
    # никто не подключён), выдаёт ему токены персонажей и может подгрузить
    # лист. Игрок при входе занимает свободный слот; если свободных нет —
    # вход отклоняется с понятным сообщением.
    # ------------------------------------------------------------------
    def _public_slots_locked(self) -> dict:
        return {slot_id: dict(slot)
                for slot_id, slot in self.game_state.slots.items()}

    def _broadcast_slots(self):
        with self.lock:
            payload = {'slots': self._public_slots_locked()}
        self.broadcast(MessageType.SLOTS_STATE, payload)

    def _apply_slot_tokens_locked(self, slot: dict, player_id):
        """Отдать игроку токены, выданные слоту, и рассказать об этом всем."""
        changed = []
        for token_id in slot.get('token_ids') or []:
            token = self.game_state.tokens.get(token_id)
            if token is None:
                continue
            token.owner_id = player_id
            self._note_owner_nickname_locked(token)
            if getattr(token, 'kind', 'actor') != 'player':
                token.kind = 'player'
            changed.append({'token_id': token.id, 'owner_id': player_id,
                            'kind': token.kind})
            # Лист, загруженный мастером до входа игрока, лежит в папке
            # токена. Кладём его и на нового владельца, иначе игрок при
            # входе не получит лист и увидит пустую вкладку.
            self._hand_sheet_to_owner_locked(token, player_id)
        return changed

    def _hand_sheet_to_owner_locked(self, token, player_id):
        """Лист токена — новому владельцу. Возвращает пакет SHEET_STATE.

        Берём только лист этого токена. Лист игрока по старой модели может
        относиться к другому его персонажу — его сюда подставлять нельзя.
        """
        if not player_id:
            return None
        sheets = getattr(self.game_state, 'token_sheets', None) or {}
        sheet = sheets.get(token.id)
        if not isinstance(sheet, dict):
            return None
        sheets[token.id] = sheet
        self.game_state.sheets[player_id] = sheet
        return {'player_id': player_id, 'sheet': sheet, 'token_id': token.id}

    def _note_owner_nickname_locked(self, token):
        """Запомнить ник владельца токена (вызывать под замком).

        По нику сервер узнаёт «своего» после перезахода со старым id:
        токен прежнего подключения возвращается тому же человеку, а не
        первому, кто зашёл за стол.
        """
        owner_id = getattr(token, 'owner_id', None)
        if not owner_id:
            token.owner_nickname = None
            return
        owner = self.game_state.players.get(owner_id)
        if owner is not None and getattr(owner, 'nickname', None):
            token.owner_nickname = owner.nickname

    def _token_owner_absent_locked(self, token) -> bool:
        """Токен игрока без владельца за столом (вызывать под замком).

        id игрока рождается клиентом, и раньше он был новым на каждое
        подключение: после перезахода владелец токена оставался «чужим»,
        и сервер отклонял поворот своего же персонажа. Такой токен считаем
        ничейным, пока его не перевыдали слотом.
        """
        owner = getattr(token, "owner_id", None)
        if not owner:
            return True
        return owner not in self.game_state.players

    def _token_free_for_locked(self, token, nickname: str) -> bool:
        """Можно ли отдать этот токен игроку с таким ником (под замком).

        Свободен — если хозяина нет вовсе. Токен вышедшего игрока отдаётся
        только тому же человеку: его узнаём по нику, записанному при
        назначении. Без этой проверки второй игрок забирал чужого
        персонажа вместе с листом.
        """
        owner_id = getattr(token, 'owner_id', None)
        if not owner_id:
            return True
        if owner_id in self.game_state.players:
            return False
        was = str(getattr(token, 'owner_nickname', None) or '').strip().casefold()
        return bool(was) and bool(nickname) and was == nickname

    def auto_assign_character(self, player_id: str):
        """Отдать единственному игроку единственного готового персонажа.

        Слоты — основной способ выдать персонажа. Но если мастер слотов не
        создавал, а в мире лежит один токен игрока с загруженным листом,
        без этого он остаётся ничьим: игрок подключается и видит пустую
        панель листа. Возвращает (изменения токена, пакеты листа).
        """
        changed = []
        sheets = []
        with self.lock:
            # Слоты — явный выбор мастера: не трогаем его.
            if self.game_state.slots:
                return changed, sheets
            players = [p for p in self.game_state.players.values()
                       if p.role != Role.DM]
            if len(players) != 1 or players[0].id != player_id:
                return changed, sheets
            for token in self.game_state.tokens.values():
                if (token.owner_id == player_id
                        and getattr(token, 'kind', 'actor') == 'player'):
                    return changed, sheets
            # Ничейный токен — либо совсем без хозяина, либо тот, чей
            # прежний владелец вышел и вернулся под другим id. Раньше
            # «ничейным» считался любой такой токен, и мира без слотов
            # хватало, чтобы первый зашедший игрок получил чужого
            # персонажа вместе с чужим листом, хотя мастер ему ничего не
            # выдавал. Теперь «свой» доказывается ником: сервер помнит,
            # кому токен принадлежал (``owner_nickname``).
            current = self.game_state.players.get(player_id)
            nickname = str(getattr(current, 'nickname', '') or '').strip().casefold()
            candidates = [
                token for token in self.game_state.tokens.values()
                if (self._token_free_for_locked(token, nickname)
                    and getattr(token, 'kind', 'actor') == 'player'
                    and isinstance(self.game_state.token_sheets.get(token.id),
                                   dict))]
            if len(candidates) != 1:
                return changed, sheets
            token = candidates[0]
            token.owner_id = player_id
            self._note_owner_nickname_locked(token)
            changed.append({'token_id': token.id, 'owner_id': player_id,
                            'kind': token.kind})
            payload = self._hand_sheet_to_owner_locked(token, player_id)
            if payload:
                sheets.append(payload)
        return changed, sheets

    def _slot_sheets_locked(self, slot: dict) -> list:
        """Пакеты SHEET_STATE для листов токенов слота (вызывать под замком)."""
        payloads = []
        for token_id in slot.get('token_ids') or []:
            token = self.game_state.tokens.get(token_id)
            if token is None or not token.owner_id:
                continue
            # Только лист этого токена: лист игрока от другого персонажа
            # сюда попадать не должен.
            sheet = (getattr(self.game_state, 'token_sheets', None)
                     or {}).get(token.id)
            if not isinstance(sheet, dict):
                continue
            self.game_state.sheets[token.owner_id] = sheet
            payloads.append({'player_id': token.owner_id, 'sheet': sheet,
                             'token_id': token.id})
        return payloads

    def reserve_slot(self, player_id: str, requested=None):
        """Занять слот при входе. Возвращает (slot_id, ошибка).

        Слотов нет — вход свободный: (None, None).
        """
        changed = []
        with self.lock:
            slots = self.game_state.slots
            if not slots:
                return None, None
            target = None
            # Слот с player_id прошлой сессии свободен: при загрузке мира
            # в нём остаётся старый игрок, которого за столом уже нет.
            # Иначе вернувшийся получал «Свободных слотов не осталось».
            if requested and requested in slots:
                slot = slots[requested]
                if (not slot.get('player_id')
                        or slot.get('player_id') == player_id
                        or slot.get('player_id') not in self.game_state.players):
                    target = requested
            if target is None:
                for slot_id, slot in slots.items():
                    if (not slot.get('player_id')
                            or slot.get('player_id')
                            not in self.game_state.players):
                        target = slot_id
                        break
            if target is None:
                return None, 'Свободных слотов не осталось'
            slot = slots[target]
            slot['player_id'] = player_id
            changed = self._apply_slot_tokens_locked(slot, player_id)
            sheets = self._slot_sheets_locked(slot)
        for payload in changed:
            self.broadcast(MessageType.UPDATE_TOKEN, payload)
        for payload in sheets:
            self.broadcast(MessageType.SHEET_STATE, payload)
        self._broadcast_slots()
        return target, None

    def add_slot(self, player_id: str, data: dict):
        """Мастер добавил слот — до или во время игры."""
        if not self.is_dm(player_id):
            return
        from shared.models import generate_id
        name = str(data.get('name') or '').strip()
        with self.lock:
            slot_id = generate_id()
            if not name:
                name = f'Слот {len(self.game_state.slots) + 1}'
            self.game_state.slots[slot_id] = {
                'id': slot_id, 'name': name, 'player_id': None,
                'token_ids': []}
        self._broadcast_slots()

    def delete_slot(self, player_id: str, data: dict):
        if not self.is_dm(player_id):
            return
        slot_id = data.get('slot_id')
        with self.lock:
            self.game_state.slots.pop(slot_id, None)
        self._broadcast_slots()

    def assign_slot_token(self, player_id: str, data: dict):
        """Мастер выдал слоту токен персонажа (или забрал обратно)."""
        if not self.is_dm(player_id):
            return
        slot_id = data.get('slot_id')
        token_id = data.get('token_id')
        assigned = bool(data.get('assigned', True))
        changed = []
        with self.lock:
            slot = self.game_state.slots.get(slot_id)
            token = self.game_state.tokens.get(token_id)
            if slot is None or token is None:
                return
            ids = list(slot.get('token_ids') or [])
            if assigned and token_id not in ids:
                ids.append(token_id)
            if not assigned and token_id in ids:
                ids.remove(token_id)
            slot['token_ids'] = ids
            if assigned and getattr(token, 'kind', 'actor') != 'player':
                token.kind = 'player'
                changed.append({'token_id': token.id, 'kind': 'player'})
            if slot.get('player_id'):
                token.owner_id = slot['player_id'] if assigned else None
                self._note_owner_nickname_locked(token)
                changed.append({'token_id': token.id,
                                'owner_id': token.owner_id,
                                'kind': token.kind})
                if assigned:
                    self._hand_sheet_to_owner_locked(token, token.owner_id)
            sheets = self._slot_sheets_locked(slot) if assigned else []
        for payload in changed:
            self.broadcast(MessageType.UPDATE_TOKEN, payload)
        for payload in sheets:
            self.broadcast(MessageType.SHEET_STATE, payload)
        self._broadcast_slots()

    def claim_slot(self, player_id: str, data: dict):
        """Игрок занял свободный слот (в том числе уже в игре)."""
        slot_id = data.get('slot_id')
        changed = []
        with self.lock:
            slots = self.game_state.slots

            def free(slot):
                """Свободен или занят игроком прошлой сессии."""
                owner = slot.get('player_id')
                return not owner or owner not in self.game_state.players

            target = None
            if slot_id and slot_id in slots and free(slots[slot_id]):
                target = slot_id
            else:
                for sid, slot in slots.items():
                    if free(slot):
                        target = sid
                        break
            if target is None:
                client = self.clients.get(player_id)
                if client:
                    client.send_message(MessageType.ERROR,
                                        {'message': 'Свободных слотов не осталось'})
                return
            slot = slots[target]
            # Один игрок — один слот: прежний освобождаем.
            for sid, other in slots.items():
                if sid != target and other.get('player_id') == player_id:
                    other['player_id'] = None
            slot['player_id'] = player_id
            changed = self._apply_slot_tokens_locked(slot, player_id)
            sheets = self._slot_sheets_locked(slot)
        for payload in changed:
            self.broadcast(MessageType.UPDATE_TOKEN, payload)
        for payload in sheets:
            self.broadcast(MessageType.SHEET_STATE, payload)
        self._broadcast_slots()

    def release_slot(self, player_id: str, data: dict):
        """Игрок вышел из-за стола — слот снова свободен."""
        with self.lock:
            for slot in self.game_state.slots.values():
                if slot.get('player_id') == player_id:
                    slot['player_id'] = None
        self._broadcast_slots()

    # ------------------------------------------------------------------
    # Полное состояние (для новых подключений)
    # ------------------------------------------------------------------
    def build_full_state(self) -> dict:
        with self.lock:
            state = self.game_state.to_dict()
            assets = dict(self.assets)
        state['asset_manifest'] = assets
        state['game_started'] = self.game_started
        # Модуль освещения: клиенты по этому флагу рисуют свет и показывают
        # вкладку. Правила всё равно считает сервер.
        state['lighting_enabled'] = self.lighting_enabled
        # Привязка токенов к клеткам: стол живёт по ней у всех одинаково.
        state['snap_tokens'] = self.snap_tokens
        # Зрение игроков: у всех один режим, его выбрал мастер.
        state['vision_mode'] = self.vision_mode
        # deque не режется срезом — нужен список.
        state['log_entries'] = list(self.log_entries)[-200:]
        return {'game_state': state}

    def start_game(self, player_id: str, data: dict = None):
        self.game_started = True
        self.broadcast(MessageType.START_GAME, {})
        self.push_log("Игра началась")

    # ------------------------------------------------------------------
    # Миры: сохранение и загрузка (по команде ДМ, файл на сервере)
    # ------------------------------------------------------------------
    def save_world(self, player_id: str, data: dict = None):
        """Записать текущую сессию в <dataPath>/worlds/<ид>/world.json."""
        world_id = (data or {}).get('world_id') or 'autosave'
        try:
            with self.lock:
                path = world_store.save_world_to(
                    self.game_state, self.data_root, world_id,
                    assets=self.assets)
                self.current_world_id = world_id
        except (OSError, ValueError) as exc:
            self.send_error(player_id, f"Мир не сохранён: {exc}")
            return
        self.push_log(f"Мир «{world_id}» сохранён: {path}")

    def load_world(self, player_id: str, data: dict = None):
        """Заменить состояние сессии файлом мира и разослать всем FULL_STATE."""
        world_id = (data or {}).get('world_id') or 'autosave'
        try:
            new_state, asset_manifest = world_store.load_world_with_assets_from(
                self.data_root, world_id)
        except (OSError, ValueError, KeyError) as exc:
            self.send_error(player_id, f"Мир не загружен: {exc}")
            return
        with self.lock:
            # Игроки остаются: мир — контент, сессия — кто за столом
            players = self.game_state.players
            self.game_state = new_state
            self.game_state.players = players
            # Манифест картинок — часть мира. Без него после загрузки не
            # скачиваются портреты призванных существ и карты.
            self.assets = dict(asset_manifest)
            self.current_world_id = world_id
            self._reset_movement_state_locked()
            # Сохранение могло быть сделано со старой очередью, куда входили
            # токены других карт или без инициативы: пересобираем сразу, иначе
            # после загрузки ход уходил бы невидимым существам.
            self._prune_order_locked()
            players = [p.id for p in self.game_state.players.values()
                       if p.role != Role.DM]
        # Мир мог быть сохранён с персонажем без владельца. Если за столом
        # один игрок — отдаём ему единственного готового персонажа до
        # рассылки состояния, чтобы он сразу увидел лист.
        for pid in players:
            self.auto_assign_character(pid)
        self._broadcast_full_state()
        self.push_log(f"Мир «{world_id}» загружен — состояние обновлено")

    def _broadcast_full_state(self):
        """Разослать состояние каждому клиенту с его transfer_token.

        Общий build_full_state() без токена затирал у клиента право на
        загрузку файлов — после любой загрузки мира ломался следующий ассет.
        """
        with self.lock:
            targets = list(self.clients.items())
        for _player_id, client in targets:
            payload = self.build_full_state()
            payload["transfer_token"] = client.transfer_token
            client.send_message(MessageType.FULL_STATE, payload)
        # Новый участник должен увидеть уже потраченные футы текущего хода.
        self._broadcast_current_movement_state()

    def restore_autosave(self, player_id: str, data: dict = None):
        """Восстановить состояние из отдельного автосохранения."""
        name = str((data or {}).get('name') or '').strip()
        try:
            info = world_store.read_autosave(self.data_root, name)
        except (OSError, ValueError, KeyError) as exc:
            self.send_error(player_id, f"Автосохранение не восстановлено: {exc}")
            return
        with self.lock:
            players = self.game_state.players
            self.game_state = info['state']
            self.game_state.players = players
            self.assets = dict(info.get('asset_manifest') or {})
            self.current_world_id = info['world_id']
            self._reset_movement_state_locked()
            # Очередь — производное состояние: пересобираем её по токенам
            # открытой карты, чтобы восстановление не вернуло в бой
            # участников соседних сцен.
            self._prune_order_locked()
            players = [p.id for p in self.game_state.players.values()
                       if p.role != Role.DM]
        for pid in players:
            self.auto_assign_character(pid)
        self._broadcast_full_state()
        self.push_log(f"Автосохранение «{name}» восстановлено")

    # ------------------------------------------------------------------
    # Карты
    # ------------------------------------------------------------------
    def add_map(self, player_id: str, data: dict):
        # Файл карты приходит от ДМ в base64: сервер хранит его у себя
        # (чтобы выдать позже подключившимся) и рассылает всем сразу
        manifest = data.get('asset_ref')
        map_data = MapData.from_dict(data)

        with self.lock:
            if manifest:
                self.assets[map_data.id] = manifest
            self.game_state.maps[map_data.id] = map_data
            if not self.game_state.current_map_id:
                self.game_state.current_map_id = map_data.id
            is_current = self.game_state.current_map_id == map_data.id

        self.broadcast(MessageType.ADD_MAP, data)

        # Ключевое исправление: раньше карта добавлялась, но команда
        # «показать её» не рассылалась, поэтому полотно оставалось пустым
        if is_current:
            self.broadcast(MessageType.CHANGE_MAP, {'map_id': map_data.id})
            self._broadcast_map_tokens(map_data.id)

        self.push_log(f"DM добавил карту «{map_data.name}»")

    def _broadcast_map_tokens(self, map_id: str):
        """Разослать расстановку токенов сцены — сверка после смены карты.

        Пока стол был на другой карте, клиент мог разойтись с сервером:
        пропущенная пачка перемещений оставляла у него старые координаты,
        и по возвращении сцена выглядела замороженной. Одна команда
        MOVE_TOKEN со всеми токенами карты возвращает всех к одному
        состоянию, не трогая правила хода.
        """
        with self.lock:
            moves = [{'token_id': token.id, 'x': token.x, 'y': token.y}
                     for token in self.game_state.tokens.values()
                     if getattr(token, 'map_id', None) == map_id]
        if moves:
            self.broadcast(MessageType.MOVE_TOKEN, {'tokens': moves})

    def change_map(self, player_id: str, data: dict):
        map_id = data.get('map_id')
        with self.lock:
            if map_id not in self.game_state.maps:
                return
            self.game_state.current_map_id = map_id
            name = self.game_state.maps[map_id].name
        self.broadcast(MessageType.CHANGE_MAP, {'map_id': map_id})
        self._broadcast_map_tokens(map_id)
        # Бой привязан к сцене: на новой карте очередь собирается из её
        # участников и не может остаться за невидимым токеном. Сам бой не
        # заканчиваем — мастер мог просто заглянуть в соседнюю комнату,
        # и ход вернётся вместе с возвратом на карту боя.
        with self.lock:
            self._prune_order_locked(end_if_empty=False, reset_turn_if_gone=True)
            payload = self._combat_payload_locked(self.game_state)
            live = payload['combat_active'] or bool(payload['order'])
            turning = payload['combat_active']
            turn_name = self._current_turn_name()
        if live:
            self.broadcast(MessageType.INITIATIVE_UPDATE, payload)
            self._broadcast_current_movement_state()
            if turning and turn_name:
                self.push_log(f"Карта «{name}» — ход: {turn_name}")
        self.push_log(f"DM переключился на карту «{name}»")

    def update_map(self, player_id: str, data: dict):
        map_id = data.get('map_id')
        with self.lock:
            map_data = self.game_state.maps.get(map_id)
            if not map_data:
                return
            for field in ('pos_x', 'pos_y', 'width', 'height', 'rotation'):
                if field in data:
                    setattr(map_data, field, data[field])
            # Магнит стен живёт на карте: без него настройка не доехала бы
            # до сохранения мира (файл пишет сервер, а не клиент).
            if 'wall_snap' in data:
                map_data.wall_snap = bool(data['wall_snap'])
                data['wall_snap'] = map_data.wall_snap
            if 'wall_snap_step' in data:
                map_data.wall_snap_step = wall_rules.normalize_snap_step(
                    data['wall_snap_step'])
                data['wall_snap_step'] = map_data.wall_snap_step
        self.broadcast(MessageType.UPDATE_MAP, data)

    def remove_map(self, player_id: str, data: dict):
        map_id = data.get('map_id')
        with self.lock:
            self.game_state.maps.pop(map_id, None)
            self.assets.pop(map_id, None)
            # Токены принадлежат карте: удаляем карту — её токены больше не
            # нужны, иначе они всплыли бы на другой карте как «бесхозные».
            orphaned = [tid for tid, tok in self.game_state.tokens.items()
                        if tok.map_id == map_id]
            for tid in orphaned:
                self.game_state.tokens.pop(tid, None)
                self.assets.pop(tid, None)
            # Объекты тоже принадлежат карте: без этого они всплыли бы на
            # другой карте как бесхозные PNG.
            orphaned_objects = [oid for oid, obj in self.game_state.map_objects.items()
                                if obj.map_id == map_id]
            for oid in orphaned_objects:
                self.game_state.map_objects.pop(oid, None)
                self.assets.pop(oid, None)
            if self.game_state.current_map_id == map_id:
                self.game_state.current_map_id = next(iter(self.game_state.maps), None)
            new_current = self.game_state.current_map_id
        # Порядок важен: сначала карта, потом её токены — иначе на клиенте
        # токены исчезнут со старой карты, которая ещё на экране.
        self.broadcast(MessageType.REMOVE_MAP, {'map_id': map_id})
        for tid in orphaned:
            self.broadcast(MessageType.REMOVE_TOKEN, {'token_id': tid})
        for oid in orphaned_objects:
            self.broadcast(MessageType.REMOVE_MAP_OBJECT, {'object_id': oid})
        # Токены карты ушли — очередь ходов могла поредеть.
        self._broadcast_combat_if_active()
        if new_current:
            self.broadcast(MessageType.CHANGE_MAP, {'map_id': new_current})
        self.push_log("DM удалил карту")

    # ------------------------------------------------------------------
    # Объекты карты: PNG-слои над или под токенами
    # ------------------------------------------------------------------
    def add_map_object(self, player_id: str, data: dict):
        """Добавить объект к карте. Объект без существующей карты не нужен."""
        manifest = data.get('asset_ref')
        obj = MapObject.from_dict(data)
        with self.lock:
            if obj.map_id not in self.game_state.maps:
                return
            if manifest:
                self.assets[obj.id] = manifest
            self.game_state.map_objects[obj.id] = obj
        payload = obj.to_dict()
        if manifest:
            payload['asset_ref'] = manifest
        if data.get('asset_upload'):
            payload['asset_upload'] = data['asset_upload']
        self.broadcast(MessageType.ADD_MAP_OBJECT, payload)
        self.push_log(f"DM добавил объект «{obj.name}»")

    def update_map_object(self, player_id: str, data: dict):
        """Изменить слой, прозрачность, крышу, замок или геометрию объекта."""
        object_id = data.get('object_id') or data.get('id')
        with self.lock:
            current = self.game_state.map_objects.get(object_id)
            if not current:
                return
            merged = current.to_dict()
            merged.update({k: data[k] for k in data
                           if k in merged and k not in ('id', 'map_id')})
            merged['id'] = current.id
            merged['map_id'] = current.map_id
            updated = MapObject.from_dict(merged)
            self.game_state.map_objects[updated.id] = updated
        self.broadcast(MessageType.UPDATE_MAP_OBJECT, updated.to_dict())

    # ------------------------------------------------------------------
    # «Мои актёры»: предзагрузка библиотеки мастера игрокам
    # ------------------------------------------------------------------
    def update_my_actor(self, player_id: str, data: dict):
        actor = data.get('actor')
        if not isinstance(actor, dict):
            return
        actor_id = str(actor.get('id') or '')
        if not actor_id:
            return
        key = 'myactor:' + actor_id
        manifest = data.get('asset_ref')
        with self.lock:
            self.my_actors[key] = {'id': key, 'actor': actor,
                                   'asset_ref': manifest}
        payload = {'id': key, 'actor': actor}
        if manifest:
            payload['asset_ref'] = manifest
        self.broadcast(MessageType.MY_ACTORS_UPDATE, payload,
                       exclude=player_id)

    def remove_my_actor(self, player_id: str, data: dict):
        key = str(data.get('id') or data.get('actor_id') or '')
        if key and not key.startswith('myactor:'):
            key = 'myactor:' + key
        with self.lock:
            self.my_actors.pop(key, None)
        self.broadcast(MessageType.MY_ACTORS_REMOVE, {'id': key},
                       exclude=player_id)

    def clear_my_actors(self, player_id: str, data: dict):
        with self.lock:
            self.my_actors.clear()
        self.broadcast(MessageType.MY_ACTORS_CLEAR, {}, exclude=player_id)

    def remove_map_object(self, player_id: str, data: dict):
        object_id = data.get('object_id') or data.get('id')
        with self.lock:
            obj = self.game_state.map_objects.pop(object_id, None)
            if obj is None:
                return
            self.assets.pop(object_id, None)
        self.broadcast(MessageType.REMOVE_MAP_OBJECT, {'object_id': object_id})
        self.push_log(f"DM удалил объект «{obj.name}»")

    def update_grid(self, player_id: str, data: dict):
        """Сетка карты: число клеток по сторонам. Смещений нет — сетка от края."""
        map_id = data.get('map_id')
        with self.lock:
            map_data = self.game_state.maps.get(map_id)
            if not map_data:
                return
            # Некорректное значение не трогаем: остаётся прежнее число клеток.
            cols = clamp_cells(data.get('grid_cols', map_data.grid_cols))
            rows = clamp_cells(data.get('grid_rows', map_data.grid_rows))
            if cols:
                map_data.grid_cols = cols
            if rows:
                map_data.grid_rows = rows
            if 'grid_visible' in data:
                map_data.grid_visible = bool(data['grid_visible'])
        self.broadcast(MessageType.UPDATE_GRID, {
            'map_id': map_id,
            'grid_cols': map_data.grid_cols,
            'grid_rows': map_data.grid_rows,
            'grid_visible': map_data.grid_visible,
        })

    def set_vision(self, player_id: str, data: dict):
        """Зрение игроков — настройка мастера для всего стола.

        «Поле зрения» показывает цветным только сектор обзора токена, «вокруг
        токена» — всё в радиусе зрения. Раньше это было в settings.json
        каждого клиента: мастер переключал у себя, а игроки продолжали
        видеть по-старому.
        """
        if not self.is_dm(player_id):
            client = self.clients.get(player_id)
            if client:
                client.send_message(MessageType.PERMISSION_DENIED,
                                    {'action': 'set_vision'})
            return
        mode = str(data.get('vision_mode') or '').strip().lower()
        with self.lock:
            self.vision_mode = "cone" if mode == "cone" else "around"
            value = self.vision_mode
        self.broadcast(MessageType.VISION, {'vision_mode': value})

    def set_snap(self, player_id: str, data: dict):
        """Привязка токенов к клеткам — настройка стола, а не клиента.

        Мастер включает её у себя в меню «Стол», сервер запоминает и
        рассылает всем: иначе у игрока токен скользил бы по пикселям, а у
        мастера стоял бы по клеткам — это расхождение и было в отчёте.
        """
        if not self.is_dm(player_id):
            client = self.clients.get(player_id)
            if client:
                client.send_message(MessageType.PERMISSION_DENIED,
                                    {'action': 'set_snap'})
            return
        with self.lock:
            self.snap_tokens = bool(data.get('snap_tokens'))
            value = self.snap_tokens
        self.broadcast(MessageType.SNAP, {'snap_tokens': value})

    def _snap_player_move_locked(self, player_id: str, token_map,
                                 x: float, y: float) -> tuple:
        """Игрок ходит по клеткам, а не по пикселям — всегда.

        Привязка мастера — про удобство стола, а у игрока это правило:
        фишка встаёт в центр клетки. Делается на сервере, а не только в
        клиенте: клиент можно и обмануть, а ход должен быть один для всех.
        """
        if self.is_dm(player_id) or token_map is None:
            return x, y
        grid = getattr(token_map, 'grid', None)
        if grid is None or not getattr(grid, 'valid', False):
            return x, y
        try:
            return grid.snap(x, y, token_map.pos_x, token_map.pos_y,
                             token_map.width, token_map.height)
        except (AttributeError, TypeError, ValueError):
            return x, y

    def update_time(self, player_id: str, data: dict):
        """Освещение карты: день/ночь, цвет и плотность ночи — всем.

        Ночь — это затемнение поверхности карты выбранным цветом (красным,
        синим, каким угодно); плотность — проценты альфы. Незнакомое
        значение времени считается днём: так старый клиент не оставит стол
        в вечной ночи. Если клиент прислал только часть настроек, остальные
        сохраняются — старые клиенты и сами по себе не ломают новое поле.
        """
        map_id = data.get('map_id')
        with self.lock:
            map_data = self.game_state.maps.get(map_id)
            if not map_data:
                return
            if 'time_of_day' in data:
                map_data.time_of_day = normalize_time_of_day(
                    data.get('time_of_day'))
            if 'night_color' in data:
                map_data.night_color = lighting_rules.normalize_night_color(
                    data.get('night_color'))
            if 'night_alpha' in data:
                map_data.night_alpha = lighting_rules.normalize_night_alpha(
                    data.get('night_alpha'))
            # Туман войны: плотность отдельно для комнат и для мира. Едет
            # вместе с ночью — это такая же настройка карты.
            if 'fog_room_alpha' in data:
                map_data.fog_room_alpha = \
                    lighting_rules.normalize_fog_room_alpha(
                        data.get('fog_room_alpha'))
            if 'fog_world_alpha' in data:
                map_data.fog_world_alpha = \
                    lighting_rules.normalize_fog_world_alpha(
                        data.get('fog_world_alpha'))
            # Вид тумана: ч/б, сепия, цветной или свой цвет мастера.
            if 'fog_style' in data:
                map_data.fog_style = lighting_rules.normalize_fog_style(
                    data.get('fog_style'))
            if 'fog_color' in data:
                map_data.fog_color = lighting_rules.normalize_fog_color(
                    data.get('fog_color'))
            payload = {
                'map_id': map_id,
                'time_of_day': normalize_time_of_day(map_data.time_of_day),
                'night_color': lighting_rules.normalize_night_color(
                    map_data.night_color),
                'night_alpha': lighting_rules.normalize_night_alpha(
                    map_data.night_alpha),
                'fog_room_alpha': lighting_rules.normalize_fog_room_alpha(
                    map_data.fog_room_alpha),
                'fog_world_alpha': lighting_rules.normalize_fog_world_alpha(
                    map_data.fog_world_alpha),
                'fog_style': lighting_rules.normalize_fog_style(
                    map_data.fog_style),
                'fog_color': lighting_rules.normalize_fog_color(
                    map_data.fog_color),
            }
        self.broadcast(MessageType.UPDATE_TIME, payload)

    def mark_explored(self, player_id: str, data: dict):
        """Клиент разведал комнаты: запомнить на карте и объявить всем.

        Разведка — состояние стола, а не отдельного игрока: партия ходит
        вместе, и комната, куда зашёл любой персонаж, считается разведанной
        у всех. Живёт на самой карте (`MapData.explored_rooms`), поэтому
        попадает и в автосохранение, и в файл мира — после перезапуска
        туман не закрывает уже виденное.

        Список объединяется, а не заменяется: клиенты присылают только
        новые комнаты, и пропущенное сообщение не стирает чужую разведку.
        """
        map_id = (data or {}).get('map_id')
        rooms = wall_rules.normalize_explored_rooms((data or {}).get('rooms'))
        if not rooms:
            return
        with self.lock:
            map_data = self.game_state.maps.get(map_id)
            if map_data is None:
                return
            before = list(getattr(map_data, 'explored_rooms', None) or [])
            merged = wall_rules.normalize_explored_rooms(before + rooms)
            map_data.explored_rooms = merged
            payload = {'map_id': map_id, 'rooms': merged}
            changed = len(merged) != len(before)
        if changed:
            self.broadcast(MessageType.EXPLORED, payload)

    def update_lights(self, player_id: str, data: dict):
        """Источники света карты: полный список от мастера — всем.

        Приходит весь список, а не отдельные правки: так нельзя потерять
        источник из-за пропущенного сообщения, а порядок и id остаются
        одними у всех. Игрок менять свет не может — это настройка стола.
        """
        if not self.is_dm(player_id):
            client = self.clients.get(player_id)
            if client:
                client.send_message(MessageType.PERMISSION_DENIED,
                                    {'action': 'update_lights'})
            return
        map_id = data.get('map_id')
        lights = lighting_rules.normalize_lights(data.get('lights'))
        with self.lock:
            map_data = self.game_state.maps.get(map_id)
            if not map_data:
                return
            map_data.lights = lights
        self.broadcast(MessageType.UPDATE_LIGHTS, {
            'map_id': map_id,
            'lights': lights,
        })

    def update_walls(self, player_id: str, data: dict):
        """Стены и проёмы карты: полный список от мастера — всем.

        Как и со светом, приходит весь список: пропущенное сообщение не
        оставляет на столе «полустену», а порядок и id одинаковы у всех.
        Проёмы без стены отбрасываются здесь же — иначе окно висело бы в
        пустоте у тех, кто получил правку позже остальных.
        """
        if not self.is_dm(player_id):
            client = self.clients.get(player_id)
            if client:
                client.send_message(MessageType.PERMISSION_DENIED,
                                    {'action': 'update_walls'})
            return
        map_id = data.get('map_id')
        walls = wall_rules.normalize_walls(data.get('walls'))
        openings = wall_rules.normalize_openings(data.get('openings'), walls)
        with self.lock:
            map_data = self.game_state.maps.get(map_id)
            if not map_data:
                return
            map_data.walls = walls
            map_data.openings = openings
        self.broadcast(MessageType.UPDATE_WALLS, {
            'map_id': map_id,
            'walls': walls,
            'openings': openings,
        })

    def toggle_door(self, player_id: str, data: dict):
        """Открыть или закрыть дверь: мастер — любую, игрок — рядом с токеном.

        Дверь — единственное препятствие, которое можно убрать руками:
        поэтому игроку разрешено толкнуть её своим токеном, если тот стоит
        у двери. Дальнюю дверь через полкарты он не откроет.
        """
        map_id = data.get('map_id')
        opening_id = str(data.get('opening_id') or '').strip()
        with self.lock:
            map_data = self.game_state.maps.get(map_id)
            if map_data is None or not opening_id:
                return
            opening = next((item for item in map_data.openings
                            if item.get('id') == opening_id), None)
            if opening is None or opening.get('kind') != wall_rules.DOOR:
                return
            # Запертую дверь не открывает даже мастер кликом: сначала
            # «Отпереть» в меню двери. Иначе замок обходится в один клик.
            if opening.get('locked'):
                self.send_error(
                    player_id,
                    "Дверь заперта — сначала отоприте её")
                return
            if not self.is_dm(player_id) and not self._token_near_door_locked(
                    player_id, map_data, opening):
                self.send_error(
                    player_id, "Дверь слишком далеко: подойдите токеном")
                return
            state = data.get('open')
            wall_rules.toggle_door(map_data, opening_id,
                                   None if state is None else bool(state))
            payload = {'map_id': map_id, 'opening_id': opening_id,
                       'open': bool(opening.get('open'))}
        self.broadcast(MessageType.TOGGLE_DOOR, payload)

    def set_door_lock(self, player_id: str, data: dict):
        """Запереть или отпереть дверь — только мастер.

        Замок закрывает дверь сразу же: запертая открытая дверь — это
        дверь, через которую можно пройти. Игрокам замок не доступен:
        иначе они открывали бы себе двери в чужие комнаты.
        """
        if not self.is_dm(player_id):
            client = self.clients.get(player_id)
            if client:
                client.send_message(MessageType.PERMISSION_DENIED,
                                    {'action': 'set_door_lock'})
            return
        map_id = data.get('map_id')
        opening_id = str(data.get('opening_id') or '').strip()
        locked = bool(data.get('locked'))
        with self.lock:
            map_data = self.game_state.maps.get(map_id)
            if map_data is None or not opening_id:
                return
            opening = wall_rules.set_door_lock(map_data, opening_id, locked)
            if opening is None:
                return
            payload = {'map_id': map_id, 'opening_id': opening_id,
                       'locked': bool(opening.get('locked')),
                       'open': bool(opening.get('open'))}
        self.broadcast(MessageType.DOOR_LOCK, payload)

    def _token_near_door_locked(self, player_id: str, map_data, opening) -> bool:
        """Стоит ли хоть один токен игрока у этого проёма.

        Меряем в мировых единицах от середины двери: полторы клетки — это
        «рукой дотянуться» для стола, где клетка — один шаг.
        """
        walls = {wall['id']: wall for wall in wall_rules.map_walls(map_data)}
        wall = walls.get(opening.get('wall_id'))
        segment = wall_rules.opening_segment(wall, opening) if wall else None
        if segment is None:
            return False
        try:
            cell_w, cell_h = map_data.grid_cell_world
        except (AttributeError, TypeError, ValueError):
            cell_w = cell_h = 0.0
        reach = max(float(cell_w or 0.0), float(cell_h or 0.0), 40.0) * 1.5
        for token in self.game_state.tokens.values():
            if token.map_id != map_data.id:
                continue
            if token.owner_id != player_id:
                continue
            _, distance = wall_rules.point_on_segment(segment, token.x,
                                                      token.y)
            if distance <= reach:
                return True
        return False

    @staticmethod
    def _coordinate(value):
        """Координата из сети: конечное число в разумном диапазоне.

        NaN и бесконечность ломают отрисовку и сравнения на клиенте, а
        миллиардные координаты уводят токен за пределы любого мира.
        """
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        return max(-1_000_000.0, min(1_000_000.0, number))

    def add_token(self, player_id: str, data: dict):
        manifest = data.get('asset_ref')
        token = Token.from_dict(data)
        token.x = self._coordinate(token.x) or 0.0
        token.y = self._coordinate(token.y) or 0.0

        with self.lock:
            if manifest:
                self.assets[token.id] = manifest
            self.game_state.tokens[token.id] = token
        # Пересылаем нормализованный токен, а не исходный пакет: так статблок
        # и состояния приезжают ко всем в одинаковом виде, даже если клиент
        # прислал лишние поля.
        payload = token.to_dict()
        # Дескриптор картинки обязателен в рассылке, как у карт и аудио:
        # клиент по нему скачивает файл и присылает ASSET_READY. Раньше он
        # терялся, и у всех, кроме автора, токен оставался пустым кружком
        # до переподключения (FULL_STATE присылал манифест заново).
        if manifest:
            payload['asset_ref'] = manifest
        if data.get('asset_upload'):
            payload['asset_upload'] = data['asset_upload']
        self.broadcast(MessageType.ADD_TOKEN, payload)
        self.push_log(f"DM добавил токен «{token.name}»")

    def copy_token(self, player_id: str, data: dict):
        """Копия токена на выбранную карту (Ctrl+C / Ctrl+V).

        Мастер копирует любой токен, игрок — только своего персонажа.
        Копию делает сервер по id источника, а не клиент по присланным
        полям: иначе игрок мог бы прислать себе чужие хиты и статблок.
        Лист персонажа копируется вместе с токеном, иначе на новой карте
        открывалась бы пустая страница. Лист копии независим от исходного:
        правка одного персонажа не меняет второго.
        """
        source_id = data.get('source_token_id')
        target_map_id = data.get('map_id')
        x = self._coordinate(data.get('x'))
        y = self._coordinate(data.get('y'))
        with self.lock:
            source = self.game_state.tokens.get(source_id)
            if source is None:
                self.send_error(player_id, "Токен для копирования не найден")
                return
            is_dm = self.is_dm(player_id)
            if not is_dm and source.owner_id != player_id:
                client = self.clients.get(player_id)
                if client:
                    client.send_message(
                        MessageType.PERMISSION_DENIED,
                        {'action': 'copy_token'})
                return
            if target_map_id not in self.game_state.maps:
                self.send_error(player_id, "Карта для вставки не найдена")
                return
            if x is None or y is None:
                return
            # Персонаж у игрока один. Копия — это второй такой же персонаж
            # с отдельной копией листа: хиты и КД с листа доставались
            # только первой фигурке, а игрок видел «дубль с чужими
            # числами». Смену сцены делаем переносом, а не копией.
            if getattr(source, 'kind', 'actor') == 'player':
                self.send_error(
                    player_id,
                    "Персонажа нельзя копировать — перенесите его "
                    "кнопкой «На эту карту»")
                return

            token = Token.from_dict(source.to_dict())
            token.id = generate_id()
            token.map_id = target_map_id
            token.x = x
            token.y = y
            # Копия — новый участник сцены, а не тот же боец: инициатива
            # прошлого боя не переезжает, иначе двойник молча вставал бы
            # в очередь и получал ходы, которых мастер не давал.
            token.initiative = None
            # Картинка уже лежит на сервере: копия получает тот же
            # дескриптор и не требует повторной загрузки с машины клиента.
            manifest = self.assets.get(source.id)
            if manifest:
                self.assets[token.id] = manifest
            self.game_state.tokens[token.id] = token

            sheet = self.game_state.token_sheets.get(source.id)
            if (not isinstance(sheet, dict) and source.owner_id
                    and getattr(source, 'kind', 'actor') == 'player'):
                # Старые миры хранят лист только по владельцу. Для копии
                # переносим его в token_sheets: иначе страница на новой
                # карте открылась бы пустой.
                sheet = self.game_state.sheets.get(source.owner_id)
            if isinstance(sheet, dict):
                sheet = copy.deepcopy(sheet)
                self.game_state.token_sheets[token.id] = sheet
            else:
                sheet = None

        payload = token.to_dict()
        if manifest:
            payload['asset_ref'] = manifest
        self.broadcast(MessageType.ADD_TOKEN, payload)
        if sheet is not None:
            # player_id пустой: лист принадлежит токену, а не игроку.
            # Иначе копия перетёрла бы лист исходного персонажа в панели.
            self.broadcast(MessageType.SHEET_STATE,
                           {'player_id': '', 'token_id': token.id,
                            'sheet': sheet})
        self.push_log(f"Токен «{source.name}» скопирован")

    @staticmethod
    def _map_center(map_data) -> tuple:
        """Центр полотна карты — куда ставить токен без верных координат.

        Раньше токен без координат оставался в точке прошлой сцены, и на
        большой карте фишка оказывалась за десять тысяч пикселей от
        холста. Центр карты всегда виден, а мастер потом подвинет.
        """
        try:
            width = float(getattr(map_data, 'width', 0) or 0)
            height = float(getattr(map_data, 'height', 0) or 0)
            pos_x = float(getattr(map_data, 'pos_x', 0) or 0)
            pos_y = float(getattr(map_data, 'pos_y', 0) or 0)
        except (TypeError, ValueError):
            return 0.0, 0.0
        return pos_x + width / 2.0, pos_y + height / 2.0

    def move_token_map(self, player_id: str, data: dict):
        """Мастер переносит токен на другую карту (не копию, а сам токен).

        Смену сцены делаем переносом: у персонажа остаётся тот же лист,
        те же хиты и та же фигурка. Копия создавала второй токен с
        отдельным листом, и числа на одной из карт переставали
        обновляться.
        """
        token_id = data.get('token_id')
        map_id = data.get('map_id')
        with self.lock:
            token = self.game_state.tokens.get(token_id)
            if token is None:
                self.send_error(player_id, "Токен для переноса не найден")
                return
            map_data = self.game_state.maps.get(map_id)
            if map_data is None:
                self.send_error(player_id, "Карты для переноса нет")
                return
            if token.map_id == map_id:
                return
            token.map_id = map_id
            # Точку задаёт клиент мастера — центр его экрана. Сервер её не
            # пересчитывает и не прижимает к полотну: раньше проверка «на
            # карте ли точка» подменяла её центром карты или краем, хотя
            # мастер смотрел совсем в другое место. Центр карты остался
            # запасным вариантом, если координат нет вовсе.
            x = self._coordinate(data.get('x'))
            y = self._coordinate(data.get('y'))
            if x is None or y is None:
                x, y = self._map_center(map_data)
            token.x = x
            token.y = y
            map_name = map_data.name
            payload = {'token_id': token.id, 'map_id': token.map_id,
                       'x': token.x, 'y': token.y, 'name': token.name}
            token_name = token.name
        self.broadcast(MessageType.UPDATE_TOKEN, payload)
        self.push_log(f"«{token_name}» перемещён на карту «{map_name}»")

    def _turn_move_error_locked(self, player_id: str, token) -> str:
        """Почему игроку нельзя двигать этот токен в текущий ход (или "").

        Очередь ходов — авторитетное состояние сервера: проверки на клиенте
        можно обойти старым клиентом или правкой сообщения. В бою игрок
        двигает только тот токен, чей сейчас ход; мастер сохраняет право
        двигать любой токен вне очереди.
        """
        if token is None or self.is_dm(player_id):
            return ""
        gs = self.game_state
        if not gs.combat_active or not gs.combat_order:
            return ""
        if not 0 <= gs.combat_turn < len(gs.combat_order):
            return ""
        current_id = gs.combat_order[gs.combat_turn]
        if token.id == current_id:
            return ""
        current = gs.tokens.get(current_id)
        name = current.name if current is not None else "другого участника"
        return (f"Сейчас ход «{name}» — «{token.name}» "
                "двигать нельзя")

    # --- Бюджет движения текущего хода ---------------------------------
    def _current_turn_token_id_locked(self):
        gs = self.game_state
        if not gs.combat_active or not gs.combat_order:
            return None
        if not 0 <= gs.combat_turn < len(gs.combat_order):
            return None
        return gs.combat_order[gs.combat_turn]

    def _sync_movement_turn_locked(self):
        """Новый ход или раунд — обнулить потраченные футы и рывок."""
        gs = self.game_state
        signature = ((gs.combat_round, self._current_turn_token_id_locked())
                     if gs.combat_active else None)
        if signature != self._move_turn_signature:
            self._move_spent.clear()
            self._move_dash.clear()
            self._move_turn_signature = signature

    def _reset_movement_state_locked(self):
        """Полный сброс: загрузка мира, конец боя, новый бой."""
        self._move_spent.clear()
        self._move_dash.clear()
        self._move_haste_until.clear()
        self._move_haste_pending.clear()
        self._move_bonus_until.clear()
        self._move_turn_signature = None

    def _activate_pending_haste_locked(self):
        """Зелье, выпитое до боя, включается с первого раунда."""
        if not self._move_haste_pending:
            return
        gs = self.game_state
        for token_id, rounds in list(self._move_haste_pending.items()):
            if int(rounds) <= 0:
                continue
            until = gs.combat_round + int(rounds) - 1
            self._move_haste_until[token_id] = max(
                until, self._move_haste_until.get(token_id, 0))
        self._move_haste_pending.clear()

    def _hasted_locked(self, token) -> bool:
        if token is None or not self.game_state.combat_active:
            return False
        return (self._move_haste_until.get(token.id, 0)
                >= self.game_state.combat_round)

    def _move_bonus_locked(self, token) -> int:
        """Прибавка скорости в футах (Удар Зефира) — 0, если не действует."""
        if token is None or not self.game_state.combat_active:
            return 0
        if (self._move_bonus_until.get(token.id, 0)
                < self.game_state.combat_round):
            return 0
        for effect in getattr(token, 'active_effects', None) or []:
            if effect.get('type') == 'speed' and effect.get('bonus_ft'):
                return int(effect.get('bonus_ft') or 0)
        return 0

    def _movement_total_locked(self, token) -> int:
        """Сколько футов доступно за этот ход: скорость × рывок/зелье + прибавка."""
        if token is None:
            return 0
        speed = token_stat.token_speed_ft(
            token, self._sheet_for_token_locked(token))
        if speed <= 0:
            return 0
        doubled = token.id in self._move_dash or self._hasted_locked(token)
        return int(speed) * (2 if doubled else 1) + self._move_bonus_locked(token)

    def _movement_cost_locked(self, token, x, y):
        """Стоимость шага в футах по клеткам карты; None — сетку не измерить."""
        gs = self.game_state
        map_data = (gs.maps.get(getattr(token, 'map_id', None))
                    or gs.maps.get(gs.current_map_id))
        if map_data is None or not map_data.grid.valid:
            return None
        cell_w, cell_h = map_data.grid_cell_world
        if cell_w <= 0 or cell_h <= 0:
            return None
        try:
            return math.hypot((float(x) - float(token.x)) / cell_w,
                              (float(y) - float(token.y)) / cell_h) * 5.0
        except (TypeError, ValueError):
            return None

    def _movement_budget_error_locked(self, player_id, token, cost) -> str:
        """Почему игроку не хватает скорости на шаг (или пустая строка).

        Мастер не ограничен бюджетом: он поправляет сцену вручную. Если у
        карты нет сетки, расстояние в футах не определено (cost is None) —
        проверка пропускается, иначе бой на карте без клеток стал бы
        недвижимым.
        """
        if (token is None or cost is None or self.is_dm(player_id)
                or not self.game_state.combat_active):
            return ""
        self._sync_movement_turn_locked()
        total = self._movement_total_locked(token)
        spent = float(self._move_spent.get(token.id, 0.0))
        left = max(0.0, total - spent)
        if cost <= left + 1e-6:
            return ""
        return (f"«{token.name}»: не хватает скорости — нужно "
                f"{int(round(cost))} фт, осталось {int(round(left))} фт")

    def _record_move_locked(self, token, cost):
        """Учесть принятый шаг в бюджете текущего хода."""
        if not self.game_state.combat_active or token is None:
            return
        if cost is None or cost <= 0:
            return
        self._sync_movement_turn_locked()
        self._move_spent[token.id] = (float(self._move_spent.get(token.id, 0.0))
                                      + cost)

    def _movement_payload_locked(self, token) -> dict:
        self._sync_movement_turn_locked()
        total = self._movement_total_locked(token)
        spent = min(total, float(self._move_spent.get(token.id, 0.0)))
        rounds = 0
        if self._hasted_locked(token):
            rounds = max(0, self._move_haste_until.get(token.id, 0)
                         - self.game_state.combat_round + 1)
        return {
            'token_id': token.id,
            'spent': round(spent, 3),
            'total': total,
            'left': round(max(0.0, total - spent), 3),
            'dash': token.id in self._move_dash,
            'haste_rounds': rounds,
            'speed_bonus': self._move_bonus_locked(token),
        }

    def _movement_payloads_locked(self, tokens: list) -> list:
        return [self._movement_payload_locked(token)
                for token in tokens if token is not None]

    def _broadcast_movement_state(self, payloads: list):
        if payloads:
            self.broadcast(MessageType.MOVEMENT_STATE, {'tokens': payloads})

    def _send_movement_state_to(self, player_id: str, payloads: list):
        client = self.clients.get(player_id)
        if client is not None and payloads:
            client.send_message(MessageType.MOVEMENT_STATE,
                                {'tokens': payloads})

    def _current_movement_payloads(self):
        with self.lock:
            gs = self.game_state
            token_id = self._current_turn_token_id_locked()
            token = gs.tokens.get(token_id) if token_id else None
            return ([self._movement_payload_locked(token)]
                    if token is not None else [])

    def _broadcast_current_movement_state(self):
        self._broadcast_movement_state(self._current_movement_payloads())

    def _send_current_movement_state_to(self, player_id: str):
        self._send_movement_state_to(player_id,
                                     self._current_movement_payloads())

    def _send_move_correction(self, player_id: str, tokens: list):
        """Вернуть клиенту позиции, которые сервер не принял."""
        client = self.clients.get(player_id)
        if client is None or not tokens:
            return
        payload = [{'token_id': token.id, 'x': token.x, 'y': token.y,
                    'name': token.name} for token in tokens]
        client.send_message(MessageType.MOVE_TOKEN, {'tokens': payload})

    def move_token(self, player_id: str, data: dict):
        """Переместить токен или пачку токенов (выделение рамкой).

        Два формата входа:
          * {'token_id', 'x', 'y'} — один токен;
          * {'tokens': [{'token_id', 'x', 'y'}, ...]} — группа.
        Права проверяются на сервере по каждому токену: игрок двигает только
        свои, чужие молча выпадают из пачки. В бою игрок двигает только тот
        токен, чей сейчас ход: очередь проверяется здесь, а не на клиенте.
        Токен со скоростью 0 (состояние или 0 хитов) не двигается никем —
        мастер тоже не перетаскивает его.
        """
        moves = data.get('tokens')
        if moves:
            applied = []
            applied_tokens = []
            denied_tokens = []
            movement_payloads = []
            denied_state = []
            denied = False
            dead_denied = False
            wall_denied = False
            opened_doors = []
            immobile_denied = None
            turn_denied = None
            budget_denied = None
            with self.lock:
                for mv in moves:
                    token = self.game_state.tokens.get(mv.get('token_id'))
                    if not token:
                        continue
                    # Мёртвого не двигают даже мастером: только воскрешение.
                    if combat_rules.death_save_state(token.death_saves)['dead']:
                        dead_denied = True
                        continue
                    if not self.is_dm(player_id) and token.owner_id != player_id:
                        denied = True
                        continue
                    turn_error = self._turn_move_error_locked(player_id, token)
                    if turn_error:
                        turn_denied = turn_error
                        continue
                    # Скорость 0 — правило для всех: схваченного,
                    # опутанного, парализованного, ошеломлённого, без
                    # сознания, окаменевшего и истощённого (5) не тащит
                    # даже мастер. Иначе правило работало бы только у игроков.
                    conds = token_stat.token_conditions(
                        token, self._sheet_for_token_locked(token))
                    can_move, _why = conditions_rules.can_move(
                        conds, hp=token.hp)
                    if not can_move:
                        immobile_denied = _why
                        continue
                    x = self._coordinate(mv.get('x'))
                    y = self._coordinate(mv.get('y'))
                    if x is None or y is None:
                        continue
                    # Стены: сквозь стену токен не проходит ни у кого, а
                    # закрытую дверь на пути он толкает — сервер открывает
                    # её сам, и звук слышат все. Проверка — до бюджета:
                    # отказ не должен стоить движения.
                    token_map = self.game_state.maps.get(token.map_id)
                    if not self.is_dm(player_id):
                        x, y = self._snap_player_move_locked(
                            player_id, token_map, x, y)
                    if (token_map is not None
                            and wall_rules.token_path_blocked(
                                token_map, token.x, token.y, x, y)):
                        door = wall_rules.door_crossing(token_map, token.x,
                                                        token.y, x, y)
                        if door is not None:
                            wall_rules.toggle_door(token_map, door['id'], True)
                            opened_doors.append((token.map_id, dict(door)))
                        else:
                            wall_denied = True
                            denied_tokens.append(token)
                            continue
                    # Бюджет считаем до перестановки: стоимость — путь от
                    # текущего центра до нового.
                    cost = self._movement_cost_locked(token, x, y)
                    budget_error = self._movement_budget_error_locked(
                        player_id, token, cost)
                    if budget_error:
                        budget_denied = budget_denied or budget_error
                        denied_tokens.append(token)
                        continue
                    token.x = x
                    token.y = y
                    self._record_move_locked(token, cost)
                    applied.append({'token_id': token.id, 'x': token.x,
                                    'y': token.y, 'name': token.name})
                    applied_tokens.append(token)
                if self.game_state.combat_active:
                    movement_payloads = self._movement_payloads_locked(
                        applied_tokens)
                    denied_state = self._movement_payloads_locked(
                        denied_tokens[:1])
            if applied:
                self.broadcast(MessageType.MOVE_TOKEN, {'tokens': applied})
                self._broadcast_movement_state(movement_payloads)
            for map_id, door in opened_doors:
                self.broadcast(MessageType.TOGGLE_DOOR, {
                    'map_id': map_id, 'opening_id': door['id'],
                    'open': bool(door.get('open')),
                })
            if dead_denied:
                self.send_error(
                    player_id,
                    "Мёртвого токена нельзя двигать — сначала воскресите его")
            if immobile_denied:
                self.send_error(
                    player_id,
                    f"Токен не может двигаться: {immobile_denied}")
            if turn_denied:
                self.send_error(player_id, turn_denied)
            if budget_denied:
                self.send_error(player_id, budget_denied)
                self._send_move_correction(player_id, denied_tokens)
                self._send_movement_state_to(player_id, denied_state)
            if wall_denied:
                self.send_error(
                    player_id,
                    "Стена: токен сквозь неё не проходит — обойдите или "
                    "откройте дверь")
                self._send_move_correction(player_id, denied_tokens)
                self._send_movement_state_to(player_id, denied_state)
            if denied:
                client = self.clients.get(player_id)
                if client:
                    client.send_message(MessageType.PERMISSION_DENIED,
                                        {'action': 'move_token'})
            return

        token_id = data.get('token_id')
        with self.lock:
            token = self.game_state.tokens.get(token_id)
            if not token:
                return
            # Мёртвого не двигает никто: сначала воскрешение.
            if combat_rules.death_save_state(token.death_saves)['dead']:
                self.send_error(
                    player_id,
                    f"«{token.name}» мёртв: сначала воскресите его")
                return
            # Проверка прав на сервере: игрок двигает только свои токены
            if not self.is_dm(player_id) and token.owner_id != player_id:
                client = self.clients.get(player_id)
                if client:
                    client.send_message(MessageType.PERMISSION_DENIED,
                                        {'action': 'move_token'})
                return
            turn_error = self._turn_move_error_locked(player_id, token)
            if turn_error:
                self.send_error(player_id, turn_error)
                return
            # Скорость 0 не даёт двигать токен никому, включая мастера.
            # Состояния без скорости 0 (ползком, половина скорости) движение
            # разрешают: это мастер учитывает сам за столом.
            conds = token_stat.token_conditions(
                token, self._sheet_for_token_locked(token))
            can_move, why = conditions_rules.can_move(conds, hp=token.hp)
            if not can_move:
                self.send_error(
                    player_id,
                    f"«{token.name}» не может двигаться: {why}")
                return
            x = self._coordinate(data.get('x'))
            y = self._coordinate(data.get('y'))
            if x is None or y is None:
                return
            # Игрок ставит фишку в центр клетки — до проверки стен: иначе
            # проверялась бы прямая мимо клетки, куда он на самом деле идёт.
            token_map = self.game_state.maps.get(token.map_id)
            if not self.is_dm(player_id):
                x, y = self._snap_player_move_locked(player_id, token_map,
                                                    x, y)
            # Стена не пропускает токен. Закрытую дверь он толкает: сервер
            # открывает её и объявляет всем (слышно всем у стола).
            if (token_map is not None
                    and wall_rules.token_path_blocked(token_map, token.x,
                                                      token.y, x, y)):
                door = wall_rules.door_crossing(token_map, token.x,
                                                token.y, x, y)
                if door is None:
                    self.send_error(
                        player_id,
                        "Стена: токен сквозь неё не проходит — обойдите "
                        "или откройте дверь")
                    self._send_move_correction(player_id, [token])
                    return
                wall_rules.toggle_door(token_map, door['id'], True)
                door_payload = {'map_id': token.map_id,
                                'opening_id': door['id'],
                                'open': True}
            else:
                door_payload = None
            # Бюджет: шаг сверх остатка отклоняем целиком, а не подрезаем —
            # иначе персонаж окажется не там, куда игрок кликнул.
            cost = self._movement_cost_locked(token, x, y)
            budget_error = self._movement_budget_error_locked(
                player_id, token, cost)
            if budget_error:
                self.send_error(player_id, budget_error)
                self._send_move_correction(player_id, [token])
                self._send_movement_state_to(
                    player_id, [self._movement_payload_locked(token)])
                return
            token.x = x
            token.y = y
            self._record_move_locked(token, cost)
            result = {'token_id': token_id, 'x': token.x, 'y': token.y,
                      'name': token.name}
            movement = (self._movement_payload_locked(token)
                        if self.game_state.combat_active else None)
        self.broadcast(MessageType.MOVE_TOKEN, result)
        if door_payload is not None:
            self.broadcast(MessageType.TOGGLE_DOOR, door_payload)
        if movement is not None:
            self._broadcast_movement_state([movement])

    def dash(self, player_id: str, data: dict):
        """Рывок: один раз за ход удвоить доступную скорость.

        Клиент показывает рывок сразу, но бюджет всё равно считает сервер:
        без этой команды шаг дальше обычной скорости был бы отклонён.
        """
        token_id = data.get('token_id')
        with self.lock:
            token = self.game_state.tokens.get(token_id)
            if not token:
                return
            if not self.is_dm(player_id) and token.owner_id != player_id:
                client = self.clients.get(player_id)
                if client:
                    client.send_message(MessageType.PERMISSION_DENIED,
                                        {'action': 'dash'})
                return
            turn_error = self._turn_move_error_locked(player_id, token)
            if turn_error:
                self.send_error(player_id, turn_error)
                self._send_movement_state_to(
                    player_id, [self._movement_payload_locked(token)])
                return
            if not self.game_state.combat_active:
                self.send_error(player_id, "Рывок доступен только в бою")
                return
            if combat_rules.death_save_state(token.death_saves)['dead']:
                self.send_error(player_id, f"«{token.name}» мёртв")
                return
            conds = token_stat.token_conditions(
                token, self._sheet_for_token_locked(token))
            can_move, why = conditions_rules.can_move(conds, hp=token.hp)
            if not can_move:
                self.send_error(
                    player_id, f"«{token.name}» не может двигаться: {why}")
                return
            self._sync_movement_turn_locked()
            if self._movement_total_locked(token) <= 0:
                self.send_error(player_id, f"«{token.name}»: скорость 0")
                return
            if self._hasted_locked(token):
                self.send_error(player_id,
                                f"«{token.name}»: скорость уже удвоена зельем")
                return
            if token.id in self._move_dash:
                self.send_error(player_id,
                                f"«{token.name}»: рывок уже использован")
                return
            self._move_dash.add(token.id)
            payload = self._movement_payload_locked(token)
        self._broadcast_movement_state([payload])

    def speed_potion(self, player_id: str, data: dict):
        """Зелье скорости: удвоение на N раундов, учтённое сервером.

        Расход предмета остаётся в листе клиента; серверу важно только
        время действия, чтобы проверка бюджета не резала законные 60 футов.
        """
        token_id = data.get('token_id')
        try:
            rounds = max(1, min(100, int(data.get('rounds') or 10)))
        except (TypeError, ValueError):
            rounds = 10
        with self.lock:
            token = self.game_state.tokens.get(token_id)
            if not token:
                return
            if not self.is_dm(player_id) and token.owner_id != player_id:
                client = self.clients.get(player_id)
                if client:
                    client.send_message(MessageType.PERMISSION_DENIED,
                                        {'action': 'speed_potion'})
                return
            self._sync_movement_turn_locked()
            if self.game_state.combat_active:
                until = self.game_state.combat_round + rounds - 1
                self._move_haste_until[token.id] = max(
                    until, self._move_haste_until.get(token.id, 0))
                payload = self._movement_payload_locked(token)
            else:
                # До боя зелье ждёт первого раунда, как и на клиенте.
                self._move_haste_pending[token.id] = max(
                    rounds, self._move_haste_pending.get(token.id, 0))
                payload = None
        if payload is not None:
            self._broadcast_movement_state([payload])

    def toggle_token_visibility(self, player_id: str, data: dict):
        """Глазок на токене: скрыть/показать его для ВСЕХ участников сессии.

        Права как у перемещения токена: DM может любой, игрок — только свой.
        Видимость меняется на сервере и рассылается всем, поэтому состояние
        одинаково у всех участников.
        """
        token_id = data.get('token_id')
        with self.lock:
            token = self.game_state.tokens.get(token_id)
        if not token:
            return
        if not self.is_dm(player_id) and token.owner_id != player_id:
            client = self.clients.get(player_id)
            if client:
                client.send_message(MessageType.PERMISSION_DENIED,
                                    {'action': 'toggle_token_visibility'})
            logging.warning(f"Отказ в доступе: {player_id} → переключение видимости токена")
            return

        # visible можно передать явно, иначе просто инвертируем
        if 'visible' in data:
            token.visible = bool(data['visible'])
        else:
            token.visible = not token.visible

        whom = 'показал' if token.visible else 'скрыл'
        self.broadcast(MessageType.TOGGLE_TOKEN_VISIBILITY,
                       {'token_id': token.id, 'visible': token.visible})
        self.push_log(f"Токен «{token.name}» {whom}")

    def remove_token(self, player_id: str, data: dict):
        token_id = data.get('token_id')
        with self.lock:
            token = self.game_state.tokens.pop(token_id, None)
            self.assets.pop(token_id, None)
        self.broadcast(MessageType.REMOVE_TOKEN, {'token_id': token_id})
        if token:
            self.push_log(f"DM удалил токен «{token.name}»")
        # Удалённый участник выпадает из очереди ходов — сообщаем всем.
        self._broadcast_combat_if_active()

    def update_token(self, player_id: str, data: dict):
        token_id = data.get('token_id')
        with self.lock:
            token = self.game_state.tokens.get(token_id)
            if not token:
                return
            # UPDATE_TOKEN — команда мастера, но игроку нужно самому
            # поворачивать своего персонажа (R) и настраивать его зрение
            # в шапке листа. Пускаем только эти поля и только для своего
            # токена: имя, хиты, состояния и статблок остаются у мастера.
            if not self.is_dm(player_id):
                own_vision = {'token_id', 'facing', 'vision_angle',
                              'vision_range', '_before'}
                # Отказ — с тем же ответом, что и у диспетчера: игрок
                # должен видеть, что правка не прошла, а не гадать, почему
                # в листе осталось старое значение.
                if not set(data) <= own_vision:
                    logging.info("update_token от %s: посторонние поля %s",
                                 player_id, sorted(set(data) - own_vision))
                    self.deny_action(player_id, MessageType.UPDATE_TOKEN)
                    return
                if not {'facing', 'vision_angle', 'vision_range'} & set(data):
                    self.deny_action(player_id, MessageType.UPDATE_TOKEN)
                    return
                # Свой токен — или токен игрока, чей владелец за столом
                # не сидит: после перезахода id игрока мог смениться (старые
                # миры), и поворот «своего же» персонажа отклонялся.
                if (token.owner_id != player_id
                        and not (getattr(token, 'kind', 'actor') == 'player'
                                 and self._token_owner_absent_locked(token))):
                    logging.info("update_token от %s: токен «%s» чужой",
                                 player_id, token.name)
                    self.deny_action(player_id, MessageType.UPDATE_TOKEN)
                    return
            if 'name' in data:
                token.name = data['name']
            if 'size' in data:
                token.size = float(data['size'])
            if 'owner_id' in data:
                token.owner_id = data['owner_id']
                self._note_owner_nickname_locked(token)
            if 'visible' in data:
                token.visible = bool(data['visible'])
            # Бой: хиты и КД. Мусор отбрасываем, диапазоны зажимаем —
            # в рассылку уже уходит нормализованное значение.
            hp_before = token.hp
            for field, lo, hi in (('hp', 0, 9999),
                                  ('hp_max', 0, 9999),
                                  ('ac', 0, 99)):
                if field in data:
                    if data[field] is None:
                        # None = «сбросить поле» (для КД молча игнорируем)
                        if field == 'ac':
                            data.pop(field)
                        else:
                            setattr(token, field, None)
                        continue
                    try:
                        value = int(data[field])
                    except (TypeError, ValueError):
                        data.pop(field)
                        continue
                    value = max(lo, min(hi, value))
                    data[field] = value
                    setattr(token, field, value)
            # Лечение выше нуля или новый ноль после ранения очищают
            # отметки смерти: раненый не должен остаться «мёртвым».
            if 'hp' in data and token.death_saves:
                hp_now = token.hp
                healed = hp_now is not None and hp_now > 0
                dropped = (hp_now == 0 and hp_before is not None
                           and int(hp_before) > 0)
                if healed or dropped:
                    token.death_saves = []
                    data['death_saves'] = []
            # Отметки смерти можно вернуть явно: это нужно отмене атаки,
            # которая восстановила хиты и должна вернуть и кружки. Ставим их
            # ПОСЛЕ сброса при лечении/новом нуле, иначе откат потерял бы
            # провалы, которые были до удара.
            if 'death_saves' in data:
                token.death_saves = combat_rules.normalize_death_saves(
                    data['death_saves'])
                data['death_saves'] = list(token.death_saves)
            # Состояния и статблок приходят списками и словарём: обновляем
            # через модель, чтобы мусор не попал в состояние мира.
            if 'conditions' in data and isinstance(data['conditions'], list):
                clean = conditions_rules.clean(data['conditions'])
                # Иммунитет проверяем и здесь: карточка присылает состояния
                # ещё и вместе со статблоком при сохранении.
                immune = conditions_rules.normalize_immunities(
                    (data.get('statblock') or token.statblock or {}).get(
                        'condition_immunities') or [])
                clean = [n for n in clean
                         if not conditions_rules.is_immune(n, immune)]
                token.conditions = clean
                data['conditions'] = list(clean)
            for field in ('resist', 'immune', 'vulnerable'):
                if field in data:
                    value = data[field]
                    if isinstance(value, dict):
                        value = list(value.keys())
                    if isinstance(value, str):
                        value = [value]
                    if isinstance(value, list):
                        clean = [str(v) for v in value if str(v).strip()]
                        setattr(token, field, clean)
                        data[field] = list(clean)
            if 'statblock' in data and isinstance(data['statblock'], dict):
                token.statblock = data['statblock']
            if 'dex' in data and data['dex'] is not None:
                try:
                    token.dex = max(-10, min(20, int(data['dex'])))
                    data['dex'] = token.dex
                except (TypeError, ValueError):
                    data.pop('dex', None)
            # Взгляд и зрение: поворот клавишей R и правка угла/дальности в
            # карточке едут тем же пакетом. Нормализуем здесь, чтобы клиент
            # не мог прислать угол 5000° или дальность -10.
            if 'facing' in data and data['facing'] is not None:
                token.facing = vision_rules.normalize_facing(data['facing'])
                data['facing'] = token.facing
            if 'vision_angle' in data and data['vision_angle'] is not None:
                token.vision_angle = vision_rules.normalize_angle(
                    data['vision_angle'])
                data['vision_angle'] = token.vision_angle
            if 'vision_range' in data and data['vision_range'] is not None:
                token.vision_range = vision_rules.normalize_range(
                    data['vision_range'])
                data['vision_range'] = token.vision_range
            # Правка хитов в окне «Хиты и КД» должна доехать до листа:
            # иначе в листе оставались старые хиты.
            sheet_payload = (self._sheet_payload_for_token_hp_locked(token)
                             if {'hp', 'hp_max'} & set(data.keys()) else None)
            # Смена владельца: лист токена должен сразу уйти новому игроку.
            # Берём лист именно этого токена, а не лист другого персонажа
            # этого же игрока.
            owner_sheet_payload = None
            if 'owner_id' in data and token.owner_id:
                owner_sheet_payload = self._hand_sheet_to_owner_locked(
                    token, token.owner_id)
        self.broadcast(MessageType.UPDATE_TOKEN, data)
        if sheet_payload:
            self.broadcast(MessageType.SHEET_STATE, sheet_payload)
        if owner_sheet_payload:
            self.broadcast(MessageType.SHEET_STATE, owner_sheet_payload)
        if 'size' in data:
            self.push_log("DM изменил размер токена")
        if 'hp' in data:
            self.push_log(
                f"{token.name}: хиты {token.hp}"
                + (f"/{token.hp_max}" if token.hp_max else ""))

    # ------------------------------------------------------------------
    # Листы персонажей
    #
    # Игрок держит лист у себя и присылает его целиком при каждом изменении.
    # Мастеру он нужен, чтобы видеть инвентарь и заклинания игроков и выдавать
    # предметы из справочника. Хранить лист только у клиента нельзя: тогда
    # мастер ничего не видит, а выдача предмета некуда деться.
    # ------------------------------------------------------------------
    def _sync_token_from_sheet_locked(self, player_id: str, sheet: dict,
                                      token_id: str = None):
        """Перенести хиты, КД и состояния с листа на токен игрока.

        Лист персонажа — это сам персонаж, а не одна его фигурка: если у
        игрока осталось два токена (например, от старой копии), хиты и КД
        обновляются у обоих. Иначе на второй карте висели старые числа, и
        казалось, что лист «не подтягивается».

        token_id задан — обновляем этот токен и остальные токены персонажа
        того же владельца. Вызывать только под замком. Возвращает список
        пакетов UPDATE_TOKEN (пустой, если менять нечего).
        """
        if not isinstance(sheet, dict):
            return []
        vitality = sheet.get('vitality') or {}
        hp = ((vitality.get('hp-current') or {}).get('value'))
        hp_max = ((vitality.get('hp-max') or {}).get('value'))
        # КД в LSS часто записан формулой («[DEX] + 14»): сервер раньше
        # пробовал int() и молча терял такое значение — на карте оставался
        # старый КД. Считаем формулу по тем же правилам, что и карточка.
        raw_ac = ((vitality.get('ac') or {}).get('value'))
        if raw_ac is None or raw_ac == '':
            ac = None
        else:
            ac = lss_format.ac_value(sheet)
            if ac is None:
                try:
                    from shared import sheet_audit
                    ac = sheet_audit.armor_profile(sheet).total
                except Exception:
                    ac = None
        names = []
        for entry in ((sheet.get('vtt') or {}).get('conditions') or []):
            name = entry.get('name') if isinstance(entry, dict) else entry
            text = str(name or '').strip()
            if text and text not in names:
                names.append(text)

        # Владельцы, чьи персонажи живут по этому листу: сам игрок и
        # владелец конкретного токена (мастер мог выдать лист ничейному
        # токену — тогда персонаж принадлежит ему по листу).
        owners = set()
        if player_id:
            owners.add(player_id)
        if token_id is not None:
            given = self.game_state.tokens.get(token_id)
            if given is not None and given.owner_id:
                owners.add(given.owner_id)

        payloads = []
        for token in self.game_state.tokens.values():
            if token_id is not None and token.id == token_id:
                pass
            elif not (token.owner_id in owners
                      and getattr(token, 'kind', 'actor') == 'player'):
                continue
            payload = {'token_id': token.id}
            if hp is not None:
                try:
                    hp_before = (int(token.hp)
                                 if token.hp is not None else None)
                    token.hp = max(0, min(9999, int(hp)))
                    payload['hp'] = token.hp
                    if token.death_saves and (
                            token.hp > 0
                            or (token.hp == 0 and (hp_before or 0) > 0)):
                        token.death_saves = []
                        payload['death_saves'] = []
                except (TypeError, ValueError):
                    pass
            if hp_max is not None:
                try:
                    token.hp_max = max(0, min(9999, int(hp_max)))
                    payload['hp_max'] = token.hp_max
                except (TypeError, ValueError):
                    pass
            if ac is not None:
                try:
                    token.ac = max(0, min(99, int(ac)))
                    payload['ac'] = token.ac
                except (TypeError, ValueError):
                    pass
            # Состояния с листа добавляем к тем, что уже стоят на токене:
            # иначе мастерское «Отравлен» стиралось бы при каждом
            # сохранении листа.
            merged = list(token.conditions)
            added = False
            for name in names:
                if name not in merged:
                    merged.append(name)
                    added = True
            if added:
                token.conditions = merged
                payload['conditions'] = list(merged)
            if len(payload) > 1:
                payloads.append(payload)
        return payloads

    def sheet_update(self, player_id: str, data: dict):
        """Игрок прислал свой лист. Игроки правят только свой лист."""
        sheet = data.get('sheet')
        target = data.get('player_id') or player_id
        if not isinstance(sheet, dict):
            return
        if target != player_id and not self.is_dm(player_id):
            client = self.clients.get(player_id)
            if client:
                client.send_message(MessageType.PERMISSION_DENIED,
                                    {'action': 'sheet_update'})
            return
        # Лист может принадлежать конкретному токену: тогда он живёт в
        # token_sheets и синхронизирует именно свой токен.
        token_id = data.get('token_id')
        token = self.game_state.tokens.get(token_id) if token_id else None
        if token is None and target:
            # Игрок присылает лист без id токена (панель игрока знает только
            # свой слот). Привязываем к его токену персонажа — тогда мастер
            # видит правки на странице листа, а хиты синхронизируются.
            token = next(
                (tok for tok in self.game_state.tokens.values()
                 if tok.owner_id == target
                 and getattr(tok, 'kind', 'actor') == 'player'), None)
        if token is not None:
            # Лист персонажа бывает только у токена игрока.
            if getattr(token, 'kind', 'actor') != 'player':
                token = None
            # Игрок не может писать лист в чужой токен: иначе он сменил бы
            # хиты и КД персонажа другого игрока.
            elif not self.is_dm(player_id) and token.owner_id != player_id:
                client = self.clients.get(player_id)
                if client:
                    client.send_message(MessageType.PERMISSION_DENIED,
                                        {'action': 'sheet_update'})
                return
        # Мастер загрузил лист в ещё ничей токен, а за столом один игрок:
        # это его персонаж, иначе игрок так и не увидит лист. Слоты — явный
        # выбор мастера, их не обходим.
        auto_owner = None
        if (token is not None and not token.owner_id
                and self.is_dm(player_id)):
            with self.lock:
                no_slots = not self.game_state.slots
                players = [p for p in self.game_state.players.values()
                           if p.role != Role.DM]
            if no_slots and len(players) == 1:
                auto_owner = players[0].id
        with self.lock:
            if auto_owner:
                token.owner_id = auto_owner
                self._note_owner_nickname_locked(token)
                target = auto_owner
            self.game_state.sheets[target] = sheet
            if token is not None:
                self.game_state.token_sheets[token.id] = sheet
                if token.owner_id:
                    self.game_state.sheets[token.owner_id] = sheet
            # Токен игрока живёт по тем же хитам, что и лист: без этого правка
            # листа не видна на карте, а урон с карты не виден в листе.
            token_payloads = self._sync_token_from_sheet_locked(
                target, sheet, token_id=token.id if token is not None else None)
        # Сначала владелец: клиент должен узнать токен своим до того, как
        # получит SHEET_STATE, иначе не покажет лист в панели.
        if auto_owner and token is not None:
            self.broadcast(MessageType.UPDATE_TOKEN,
                           {'token_id': token.id, 'owner_id': auto_owner,
                            'kind': token.kind})
        self.broadcast(MessageType.SHEET_STATE,
                       {'player_id': target, 'sheet': sheet,
                        'token_id': token.id if token is not None else None})
        for token_payload in token_payloads:
            self.broadcast(MessageType.UPDATE_TOKEN, token_payload)

    def _grant_target_locked(self, data: dict):
        """Куда положить предмет: лист токена или старый лист игрока.

        Мастер выдаёт предмет токену (у него может быть свой лист), а не
        просто игроку: у одного игрока бывает несколько персонажей.
        Возвращает (sheet, token, owner_id, error).
        """
        token_id = data.get('token_id')
        if token_id:
            token = self.game_state.tokens.get(token_id)
            if token is None:
                return None, None, '', 'Токен не найден'
            if getattr(token, 'kind', 'actor') != 'player':
                return None, None, '', 'Лист бывает только у токена игрока'
            sheet = self._sheet_for_token_locked(token)
            if not isinstance(sheet, dict):
                return (None, token, token.owner_id or '',
                        f"У токена «{token.name}» нет листа персонажа")
            return sheet, token, token.owner_id or '', ''

        target = data.get('player_id')
        sheet = self.game_state.sheets.get(target)
        if not isinstance(sheet, dict):
            return None, None, target or '', 'У игрока ещё нет листа персонажа'
        return sheet, None, target, ''

    def grant_item(self, player_id: str, data: dict):
        """Мастер выдал предмет токену игрока (или старому адресату-игроку)."""
        if not self.is_dm(player_id):
            client = self.clients.get(player_id)
            if client:
                client.send_message(MessageType.PERMISSION_DENIED,
                                    {'action': 'grant_item'})
            return
        item = data.get('item')
        if not isinstance(item, dict):
            return
        with self.lock:
            sheet, token, target, error = self._grant_target_locked(data)
            if error:
                self.send_error(player_id, error)
                return
            entry = sheet_items.add_item(sheet, item,
                                         int(data.get('qty', 1) or 1))
            name = entry.get('name', '?')
            if token is not None:
                # Лист живёт на токене и переезжает вместе с ним.
                self.game_state.token_sheets[token.id] = sheet
                if token.owner_id:
                    self.game_state.sheets[token.owner_id] = sheet
        self.broadcast(MessageType.SHEET_STATE,
                       {'player_id': target or '', 'sheet': sheet,
                        'token_id': token.id if token is not None else None})
        recipient = (token.name if token is not None else
                     self.game_state.players.get(target).nickname
                     if target in self.game_state.players else '')
        self.push_log(f"Мастер выдал «{name}»"
                      + (f" — {recipient}" if recipient else ""))

    def grant_spell(self, player_id: str, data: dict):
        """Мастер добавил заклинание в книгу токена игрока (или игрока)."""
        if not self.is_dm(player_id):
            client = self.clients.get(player_id)
            if client:
                client.send_message(MessageType.PERMISSION_DENIED,
                                    {'action': 'grant_spell'})
            return
        spell = data.get('spell')
        if not isinstance(spell, dict):
            return
        with self.lock:
            sheet, token, target, error = self._grant_target_locked(data)
            if error:
                self.send_error(player_id, error)
                return
            entry = sheet_items.add_spell(sheet, spell)
            name = entry.get('name', '?')
            if token is not None:
                self.game_state.token_sheets[token.id] = sheet
                if token.owner_id:
                    self.game_state.sheets[token.owner_id] = sheet
        self.broadcast(MessageType.SHEET_STATE,
                       {'player_id': target or '', 'sheet': sheet,
                        'token_id': token.id if token is not None else None})
        self.push_log(f"Мастер добавил заклинание «{name}»")

    # ------------------------------------------------------------------
    # Существа
    #
    # Токен несёт статблок (характеристики, спасброски, защиту),
    # поэтому сервер может бросать за существо, даже если готового
    # статблока нет в памяти клиента. Значения, пришедшие от клиента
    # (бонус атаки, урон), проверяем на разумные границы: это не
    # античит, но одна опечатка не должна наносить 99999 урона.
    # ------------------------------------------------------------------

    def _conditions_with_hp(self, token, sheet=None) -> list:
        """Состояния токена плюс неявное «без сознания» на нуле хитов.

        По правилам 5e существо с 0 хитов беспомощно: по нему бьют с
        преимуществом и в упор — критически, а Силу и Ловкость оно
        проваливает автоматически. Иконку состояния за это не ставим:
        ноль хитов и так виден по красной заливке и черепу.
        """
        names = token_stat.token_conditions(token, sheet)
        try:
            if token.hp is not None and int(token.hp) <= 0:
                if "Без сознания" not in names:
                    names.append("Без сознания")
        except (TypeError, ValueError):
            pass
        return names

    def _tokens_within_5ft(self, first, second) -> bool:
        """Стоят ли токены в пределах 5 футов друг от друга.

        Один шаг сетки — 5 футов. Если карта или сетка неизвестна, считаем,
        что бой идёт в упоре: за столом атакуют в основном соседние
        токены, а «нет расстояния» не должно отменять правила состояний.
        """
        try:
            first_map = self.game_state.maps.get(getattr(first, 'map_id', None))
            second_map = self.game_state.maps.get(getattr(second, 'map_id', None))
            map_data = first_map or second_map
            if map_data is None:
                return True
            cell_w, cell_h = map_data.grid_cell_world
            if not cell_w or not cell_h:
                return True
            dx = (float(second.x) - float(first.x)) / cell_w
            dy = (float(second.y) - float(first.y)) / cell_h
            return (dx * dx + dy * dy) ** 0.5 <= 1.1
        except (TypeError, ValueError, AttributeError):
            return True

    # --- Скрытность и скрытая атака ------------------------------------
    def _store_conditions_locked(self, token, names) -> Optional[dict]:
        """Записать состояния токена И в лист. -> пакет SHEET_STATE или None.

        Лист и токен хранят один и тот же список: иначе снятая в бою
        скрытность возвращалась из старого листа при следующем чтении.
        """
        clean = conditions_rules.clean(names)
        token.conditions = clean
        sheet = self._sheet_for_token_locked(token)
        if not isinstance(sheet, dict):
            return None
        block = sheet.setdefault('vtt', {})
        if not isinstance(block, dict):
            return None
        block['conditions'] = [{'name': name, 'rounds': None} for name in clean]
        by_token = getattr(self.game_state, 'token_sheets', None)
        if isinstance(by_token, dict):
            by_token[token.id] = sheet
        if token.owner_id:
            self.game_state.sheets[token.owner_id] = sheet
        return {'player_id': token.owner_id or '', 'sheet': sheet,
                'token_id': token.id}

    def _set_hidden_locked(self, token, hidden: bool):
        """Поставить или снять «Скрыт». -> (пакет токена, пакет листа)."""
        names = [str(name) for name in (token.conditions or [])]
        key = class_features.HIDDEN.casefold()
        present = any(name.casefold() == key for name in names)
        if hidden and not present:
            names.append(class_features.HIDDEN)
        elif not hidden and present:
            names = [name for name in names if name.casefold() != key]
        else:
            return None, None
        sheet_payload = self._store_conditions_locked(token, names)
        return ({'token_id': token.id,
                 'conditions': list(token.conditions or [])}, sheet_payload)

    def _observers_for_stealth_locked(self, token) -> list:
        """Кто может заметить токен: существа на той же карте.

        Свои персонажи того же игрока и трупы наблюдателями не считаются:
        своя же группа не мешает спрятаться.
        """
        observers = []
        for other in self.game_state.tokens.values():
            if other is None or other.id == token.id:
                continue
            if getattr(other, 'map_id', None) != getattr(token, 'map_id', None):
                continue
            if getattr(other, 'owner_id', None) and other.owner_id == getattr(
                    token, 'owner_id', None):
                continue
            try:
                if other.hp is not None and int(other.hp) <= 0:
                    continue
            except (TypeError, ValueError):
                pass
            observers.append(other)
        return observers

    def _darkvision_for_token_locked(self, token) -> float:
        """Тёмное зрение токена в футах: из статблока или листа персонажа."""
        block = getattr(token, "statblock", None)
        texts = [block.get("senses")] if isinstance(block, dict) else []
        sheet = self._sheet_for_token_locked(token)
        return max(lighting_rules.darkvision_ft(texts),
                   lighting_rules.darkvision_from_sheet(sheet))

    def _observers_seeing_locked(self, token) -> list:
        """Кто реально видит токен: конус обзора плюс свет и темнота.

        Модуль освещения выключен — работает прежнее правило «видит тот,
        в чей конус попал». Включён — ночью видно только освещённое или
        в дальности тёмного зрения наблюдателя.
        """
        observers = self._observers_for_stealth_locked(token)
        if not self.lighting_enabled:
            seen = vision_rules.observed_by(token, observers,
                                            self.game_state.maps)
            return seen
        map_data = self.game_state.maps.get(getattr(token, "map_id", None))
        result = []
        for other in observers:
            if lighting_rules.sees(other, token, map_data,
                                   self._darkvision_for_token_locked(other)):
                result.append(other)
        return result

    def _stealth_dc_locked(self, token) -> tuple:
        """Сложность скрытности: лучшее пассивное Восприятие.

        -> ``(Сл, кто, кто с помехой)``. С включённым освещением считаются
        только те, кто смотрит в сторону токена: остальные его не ищут, а
        раньше их пассивное Восприятие задирало Сл всей карты. В темноте у
        наблюдателя без тёмного зрения (или если токен дальше его тёмного
        зрения) пассивное Восприятие получает —5 — помеху по 5e.
        """
        best_dc, watcher, penalized = 0, None, []
        map_data = self.game_state.maps.get(getattr(token, "map_id", None))
        observers = self._observers_for_stealth_locked(token)
        if self.lighting_enabled and map_data is not None:
            observers = [other for other in observers
                         if vision_rules.in_cone(other, token, map_data)]
        for other in observers:
            sheet = self._sheet_for_token_locked(other)
            dc = class_features.passive_perception(sheet, other)
            if self.lighting_enabled:
                penalty = lighting_rules.perception_penalty(
                    other, map_data,
                    self._darkvision_for_token_locked(other), target=token)
                if penalty:
                    name = str(getattr(other, "name", "") or "").strip()
                    if name and name not in penalized:
                        penalized.append(name)
                dc = max(1, int(dc + penalty))
            if dc > best_dc:
                best_dc, watcher = dc, other.name
        return best_dc, watcher, penalized

    def _special_recharge_tick_locked(self) -> list:
        """Начало хода существа: бросок перезарядки его особых атак.

        В 5e дыхание с пометкой «перезарядка 5–6» восстанавливается в
        начале каждого хода существа броском d6. Отметка снимается только
        при 5–6 — иначе способность остаётся недоступной ещё ход.
        """
        notes = []
        current = self._current_turn_token_id_locked()
        if not current:
            return notes
        for key in list(self._special_used):
            if key[0] != current:
                continue
            roll = dice.roll('1d6')['total']
            label = str(key[1] or "").split('|')[0]
            if label.startswith('sa:'):
                label = label[3:]
            if roll >= 5:
                self._special_used.pop(key, None)
                notes.append(f"⚡ «{label}» перезарядилось (d6 {roll})")
            else:
                notes.append(
                    f"… «{label}»: перезарядка не восстановилась (d6 {roll})")
        return notes

    def _sneak_sync_locked(self):
        """Новый ход — скрытая атака снова доступна."""
        gs = self.game_state
        signature = ((gs.combat_round, self._current_turn_token_id_locked())
                     if gs.combat_active else None)
        if signature != self._sneak_signature:
            self._sneak_used.clear()
            self._sneak_signature = signature

    def _ally_within_5ft_locked(self, attacker, target) -> bool:
        """Есть ли у цели ещё один противник в 5 футах (условие скрытой атаки)."""
        for other in self.game_state.tokens.values():
            if other is None or other.id in (target.id, attacker.id):
                continue
            if getattr(other, 'map_id', None) != getattr(target, 'map_id', None):
                continue
            if getattr(other, 'owner_id', None) and other.owner_id == getattr(
                    target, 'owner_id', None):
                continue
            try:
                if other.hp is not None and int(other.hp) <= 0:
                    continue
            except (TypeError, ValueError):
                pass
            if self._tokens_within_5ft(other, target):
                return True
        return False

    def stealth_check(self, player_id: str, data: dict):
        """Проверка Скрытности по правилам 5e: d20 + бонус против пассивного Восприятия.

        Одного броска хватает, чтобы за столом появилось понятное состояние
        «Скрыт»: сервер сам сравнивает результат с пассивным Восприятием
        всех, кто может видеть токен, ставит или снимает состояние, а лист
        остаётся тем же источником правды.

        Свет решает, можно ли вообще прятаться: в ярком свете на виду —
        нельзя, в тусклом свете и в темноте (слабо/сильно заслонённая
        местность) — можно, но наблюдатель ищет с помехой −5.
        """
        token_id = str((data or {}).get('token_id') or '')
        with self.lock:
            token = self.game_state.tokens.get(token_id)
            if token is None:
                self.send_error(player_id, "Скрытность: токен не найден")
                return
            if not self.is_dm(player_id) and token.owner_id != player_id:
                client = self.clients.get(player_id)
                if client:
                    client.send_message(MessageType.PERMISSION_DENIED,
                                        {'action': 'stealth_check'})
                return
            # Правило 5e: нельзя прятаться от того, кто тебя видит. Пока у
            # токенов не было направления взгляда, этот случай проверить
            # было нечем. Теперь сервер сам смотрит, попадает ли токен в
            # чужой конус обзора, и отказывает без броска.
            watchers = self._observers_seeing_locked(token)
            names = []
            for watcher in watchers:
                name = str(getattr(watcher, "name", "") or "").strip()
                if name and name not in names:
                    names.append(name)
            watched_by = ", ".join(names)
            # Освещённость точки, где стоит прячущийся: яркий свет, тусклый
            # свет (слабо заслонённая местность) или тьма (сильно заслонённая).
            map_data = self.game_state.maps.get(getattr(token, "map_id", None))
            level = lighting_rules.BRIGHT
            if self.lighting_enabled and map_data is not None:
                level = lighting_rules.light_level(map_data, token.x, token.y)
            in_dark = level == lighting_rules.DARK
            # 5e: «нельзя прятаться от существа, которое вас видит», но
            # заслонённая местность — другое дело: в тусклом свете и в темноте
            # попытка разрешена даже на виду, просто наблюдатель ищет с
            # помехой −5. В ярком свете на виду спрятаться по-прежнему нельзя.
            can_hide = (level != lighting_rules.BRIGHT) or not watchers
            sheet_payload = None
            token_payload = None
            conditions = list(token.conditions or [])
            if not can_hide:
                payload = {
                    'token_id': token.id,
                    'token_name': token.name,
                    'owner_id': token.owner_id or '',
                    'total': 0,
                    'bonus': 0,
                    'dc': 0,
                    'watcher': watched_by,
                    'success': False,
                    'blocked': True,
                    'light_level': level,
                    'lighting_penalty': 0,
                    'conditions': conditions,
                    'note': (f"Скрытность: {token.name} на виду у "
                             f"{watched_by} — прятаться нельзя"),
                    'breakdown': 'без броска: вас видят',
                }
            else:
                sheet = self._sheet_for_token_locked(token)
                if isinstance(sheet, dict) and sheet.get('stats'):
                    bonus = class_features.stealth_bonus(sheet)
                else:
                    bonus = token_stat.token_ability_mod(token, sheet, 'dex')
                result = combat_rules.roll_save(bonus, None)
                total = int(result.get('total') or 0)
                dc, watcher, penalized = self._stealth_dc_locked(token)
                success = total >= dc if dc else True
                # Темнота скрывает только тогда, когда её никто не пробивает
                # тёмным зрением.
                hidden_in_dark = bool(in_dark and not watchers)
                penalty = -5 if penalized else 0
                token_payload, sheet_payload = self._set_hidden_locked(
                    token, success)
                conditions = list(token.conditions or [])
                payload = {
                    'token_id': token.id,
                    'token_name': token.name,
                    'owner_id': token.owner_id or '',
                    'total': total,
                    'bonus': int(bonus),
                    'dc': int(dc),
                    'watcher': watcher or '',
                    'success': bool(success),
                    'blocked': False,
                    'light_level': level,
                    'lighting_penalty': penalty,
                    'conditions': conditions,
                    'note': ((f"Скрытность: {total} против пассивного "
                              f"Восприятия {dc}"
                              + (f" ({watcher})" if watcher else ""))
                             if dc else
                             f"Скрытность: {total} — никто не смотрит")
                             + (f" · {lighting_rules.light_label(level)}: "
                                f"−5 Восприятию"
                                + (f" ({', '.join(penalized)})"
                                   if penalized else "")
                                if penalized else "")
                             + (" · тьма: без тёмного зрения не видно"
                                if hidden_in_dark else ""),
                    'breakdown': (result.get('breakdown')
                                  or f"d20{int(bonus):+d}={total}"),
                }
                if token_payload:
                    token_payload['stealth'] = bool(success)
        self.broadcast(MessageType.UPDATE_TOKEN, token_payload or {
            'token_id': token.id, 'conditions': conditions})
        if sheet_payload:
            self.broadcast(MessageType.SHEET_STATE, sheet_payload)
        # Одного сообщения достаточно: STEALTH_RESULT уже содержит бросок,
        # Сл и наблюдателя, поэтому отдельный ROLL_DICE давал бы вторую
        # строку и вторую карточку в чате.
        self.broadcast(MessageType.STEALTH_RESULT, payload)
        if payload.get('blocked'):
            self.push_log("👁 " + payload['note'])
        else:
            self.push_log(("🙈 " if payload['success'] else "👁 ")
                          + payload['note']
                          + (f" — {token.name} скрыт"
                             if payload['success']
                             else f" — {token.name} на виду"))

    def _sheet_for_token_locked(self, token) -> dict:
        """Лист персонажа, которому принадлежит токен (или None).

        Сначала ищем лист по id токена (новая модель: лист живёт в токене и
        переезжает с ним), затем — по владельцу (старые миры и совместимость).
        У актёра со статблоком листа нет вовсе: иначе монстр, выданный игроку,
        жил бы хитами, ячейками и оружием чужого героя.
        """
        # Актёр (монстр/НПС) живёт статблоком. Лист игрока-владельца к нему
        # не относится: иначе выданный игроку монстр лечился, кастовал и
        # правил хиты в листе своего героя, а атаки брались из чужого
        # оружия — карточка показывала +19, бросок уходил с +7. Проверка
        # стоит до поиска по токену: старый мир мог уже сохранить чужой
        # лист под id монстра.
        if token_kind.is_actor(token) and getattr(token, 'statblock', None):
            return None
        by_token = getattr(self.game_state, 'token_sheets', None) or {}
        sheet = by_token.get(getattr(token, 'id', None))
        if isinstance(sheet, dict):
            return sheet
        owner = getattr(token, 'owner_id', None)
        if owner:
            sheet = self.game_state.sheets.get(owner)
            if isinstance(sheet, dict):
                return sheet
        return None

    def _sheet_payload_for_token_hp_locked(self, token) -> dict:
        """Хиты токена -> в его лист; вернуть пакет SHEET_STATE или None.

        Лист живёт в двух местах: ``token_sheets`` (по токену) и ``sheets``
        (по владельцу, старые миры). Пишем в оба: вкладка листа читает
        лист токена, а старые проверки — по игроку. Вызывать под замком.
        """
        sheet = self._sheet_for_token_locked(token)
        if not isinstance(sheet, dict):
            return None
        vitality = sheet.setdefault('vitality', {})
        if not isinstance(vitality, dict):
            return None
        try:
            if token.hp is not None:
                vitality.setdefault('hp-current', {})['value'] = int(token.hp)
            if token.hp_max is not None:
                vitality.setdefault('hp-max', {})['value'] = int(token.hp_max)
        except (TypeError, ValueError):
            return None
        by_token = getattr(self.game_state, 'token_sheets', None)
        if isinstance(by_token, dict):
            by_token[token.id] = sheet
        if token.owner_id:
            self.game_state.sheets[token.owner_id] = sheet
        return {'player_id': token.owner_id or '', 'sheet': sheet,
                'token_id': token.id}

    def rest(self, player_id: str, data: dict):
        """Отдых всего стола — только мастер (права проверяет диспетчер).

        Правила 5e: короткий отдых возвращает то, что «до короткого отдыха»
        (Второе дыхание, Действие всплеск, Божественный канал), а хиты
        поднимает на половину максимума — за столом это заменяет подсчёт
        костей хитов. Долгий отдых возвращает всё: хиты, ячейки, заряды
        особенностей, заряды предметов, запас Наложения рук, снимает
        состояния и оставляет минус одну ступень истощения.

        Актёры (монстры и НПС) не отдыхают: это ресурс игроков, и раздача
        полных хитов всей сцене сломала бы бой.
        """
        kind = str((data or {}).get('kind') or 'long').strip().lower()
        long = kind != 'short'
        entries = []
        with self.lock:
            for token in list(self.game_state.tokens.values()):
                if not token_kind.is_player(token):
                    continue
                sheet = self._sheet_for_token_locked(token)
                before = token.hp
                if isinstance(sheet, dict):
                    sheet_items.rest(sheet, long=long,
                                     heal="" if long else "half")
                    vitality = sheet.get('vitality') or {}
                    try:
                        hp_max = int((vitality.get('hp-max') or {})
                                     .get('value') or 0)
                        hp = int((vitality.get('hp-current') or {})
                                 .get('value') or 0)
                    except (TypeError, ValueError):
                        hp_max = hp = 0
                    if hp_max > 0:
                        token.hp_max = hp_max
                        token.hp = max(0, min(hp_max, hp))
                    block = sheet.get('vtt') or {}
                    names = [str(entry.get('name') if isinstance(entry, dict)
                                 else entry) for entry in
                             (block.get('conditions') or [])]
                    token.conditions = [name for name in names if name]
                    by_token = getattr(self.game_state, 'token_sheets', None)
                    if isinstance(by_token, dict):
                        by_token[token.id] = sheet
                    if token.owner_id:
                        self.game_state.sheets[token.owner_id] = sheet
                else:
                    # Листа нет (персонаж без листа): хотя бы хиты.
                    try:
                        hp_max = int(token.hp_max or 0)
                    except (TypeError, ValueError):
                        hp_max = 0
                    if hp_max > 0:
                        if long:
                            token.hp = hp_max
                        else:
                            start = int(token.hp or 0)
                            token.hp = min(hp_max, start + (hp_max + 1) // 2)
                if long:
                    token.death_saves = []
                    token.active_effects = []
                entries.append({
                    'token_id': token.id,
                    'name': token.name,
                    'owner_id': token.owner_id or '',
                    'hp': token.hp,
                    'hp_max': token.hp_max,
                    'hp_before': before,
                    'conditions': list(token.conditions or []),
                    'death_saves': list(token.death_saves or []),
                    'sheet': sheet if isinstance(sheet, dict) else None,
                })
        kind_label = 'Долгий отдых' if long else 'Короткий отдых'
        for entry in entries:
            self.broadcast(MessageType.UPDATE_TOKEN, {
                'token_id': entry['token_id'],
                'hp': entry['hp'],
                'hp_max': entry['hp_max'],
                'delta': (int(entry['hp'] or 0)
                          - int(entry['hp_before'] or 0)),
                'conditions': entry['conditions'],
                'death_saves': entry['death_saves'],
                'rest': kind,
            })
            if entry['sheet'] is not None:
                self.broadcast(MessageType.SHEET_STATE, {
                    'player_id': entry['owner_id'],
                    'token_id': entry['token_id'],
                    'sheet': entry['sheet'],
                })
        self.broadcast(MessageType.REST_RESULT, {
            'kind': kind,
            'long': long,
            'tokens': [{k: v for k, v in entry.items() if k != 'sheet'}
                       for entry in entries],
        })
        # Журнал отдыха — построчно по персонажам: видно, кому сколько хитов
        # вернулось и что ещё восстановилось. Раньше была только сумма,
        # и игроки не понимали, достались ли им ячейки и заряды.
        details = []
        for entry in entries:
            before = int(entry['hp_before'] or 0)
            after = int(entry['hp'] or 0)
            bit = f"{entry['name']} {before}→{after}"
            if long:
                bit += " (ячейки, заряды и состояния)"
            details.append(bit)
        self.push_log(f"{kind_label}: " + "; ".join(details))

    def set_conditions(self, player_id: str, data: dict):
        """Поставить состояния на токен: ДМ — любому, игрок — своему."""
        token_id = data.get('token_id')
        raw = data.get('conditions')
        if not isinstance(raw, list):
            return
        # Оставляем только знакомые состояния: иначе в подписи токена
        # появится случайный текст, а правила боя его не узнают. Уровень
        # истощения («Истощён 3») — часть названия, его сохраняем.
        clean = conditions_rules.clean(raw)
        blocked = []
        with self.lock:
            token = self.game_state.tokens.get(token_id)
            if not token:
                return
            if not self.is_dm(player_id) and token.owner_id != player_id:
                client = self.clients.get(player_id)
                if client:
                    client.send_message(MessageType.PERMISSION_DENIED,
                                        {'action': 'set_conditions'})
                return
            # Иммунитет из статблока: «иммунен к очарованию» значит, что
            # галочка просто так не сработает ни у игрока, ни у мастера.
            immune = conditions_rules.normalize_immunities(
                (token.statblock or {}).get("condition_immunities") or [])
            blocked = [n for n in clean
                       if conditions_rules.is_immune(n, immune)]
            if blocked:
                clean = [n for n in clean if n not in blocked]
            token.conditions = clean
            payload = {'token_id': token.id, 'conditions': list(clean)}
            name = token.name
            # Тот же список — в лист персонажа. Иначе снятая галочка
            # возвращалась: лист хранил старое состояние, а правила
            # объединяют лист с токеном (token_conditions).
            sheet_payload = None
            sheet = self._sheet_for_token_locked(token)
            if isinstance(sheet, dict):
                block = sheet.setdefault('vtt', {})
                if isinstance(block, dict):
                    block['conditions'] = [{'name': n, 'rounds': None}
                                           for n in clean]
                    by_token = getattr(self.game_state, 'token_sheets', None)
                    if isinstance(by_token, dict):
                        by_token[token.id] = sheet
                    if token.owner_id:
                        self.game_state.sheets[token.owner_id] = sheet
                    sheet_payload = {'player_id': token.owner_id or '',
                                     'sheet': sheet, 'token_id': token.id}
        if blocked:
            self.send_error(
                player_id,
                f"«{name}» не может получить состояние: "
                + ", ".join(blocked))
        self.broadcast(MessageType.UPDATE_TOKEN, payload)
        if sheet_payload:
            self.broadcast(MessageType.SHEET_STATE, sheet_payload)
        if clean:
            self.push_log(f"{name}: " + ", ".join(clean))
        else:
            self.push_log(f"{name}: состояния сняты")

    def restore_token(self, player_id: str, data: dict):
        """Вернуть хиты и отметки смерти к снимку — отмена (или повтор) атаки.

        Снимок делает сервер в момент удара (``target_hp_before``,
        ``death_saves_before`` в ATTACK_RESULT), клиент хранит его вместе с
        записью истории Ctrl+Z и присылает обратно.

        Права: мастер — любому токену, игрок — своему персонажу или токену,
        которым он только что бил (``attacker_id``). Без этой команды отмена
        у игрока не работала бы: UPDATE_TOKEN доступен только мастеру.
        """
        token_id = data.get('token_id')
        if not token_id or 'hp' not in data:
            return
        with self.lock:
            token = self.game_state.tokens.get(token_id)
            if token is None:
                return
            is_dm = self.is_dm(player_id)
            if not is_dm:
                attacker = self.game_state.tokens.get(data.get('attacker_id'))
                owns = (token.owner_id == player_id or token.id == player_id
                        or (attacker is not None
                            and attacker.owner_id == player_id))
                if not owns:
                    client = self.clients.get(player_id)
                    if client:
                        client.send_message(MessageType.PERMISSION_DENIED,
                                            {'action': 'restore_token'})
                    return
            try:
                hp = max(0, min(9999, int(data['hp'])))
            except (TypeError, ValueError):
                return
            # Максимум хитов — только мастер: через «отмену атаки» игрок
            # иначе мог бы поднять себе потолок и вылечиться до 9999.
            if not is_dm:
                cap = token.hp_max if token.hp_max is not None else 9999
                hp = min(hp, max(0, cap))
            token.hp = hp
            if data.get('hp_max') is not None and is_dm:
                try:
                    token.hp_max = max(0, min(9999, int(data['hp_max'])))
                except (TypeError, ValueError):
                    pass
            if 'death_saves' in data:
                token.death_saves = combat_rules.normalize_death_saves(
                    data.get('death_saves'))
            payload = {
                'token_id': token.id,
                'hp': token.hp,
                'hp_max': token.hp_max,
                'death_saves': list(token.death_saves),
            }
            sheet_payload = self._sheet_payload_for_token_hp_locked(token)
            name = token.name
        self.broadcast(MessageType.UPDATE_TOKEN, payload)
        if sheet_payload:
            self.broadcast(MessageType.SHEET_STATE, sheet_payload)
        self.push_log(f"{name}: атака отменена, хиты {payload['hp']}")

    def roll_initiative(self, player_id: str, data: dict):
        """Бросить инициативу: d20 + Ловкость.

        Бросок делает сервер, а не клиент: иначе игрок мог бы прислать себе
        любое число. Без token_ids — бросаем всем, у кого есть хиты или лист.
        """
        token_ids = data.get('token_ids')
        if not token_ids:
            # Без списка бросаем только за тех, кто на открытой карте:
            # бой идёт на одной сцене, и токен из соседней комнаты не
            # должен проснуться в очереди вместе с партией.
            with self.lock:
                current_map = self.game_state.current_map_id
                token_ids = [tid for tid, tok in self.game_state.tokens.items()
                             if (tok.kind != 'player' or tok.owner_id)
                             and (current_map is None
                                  or tok.map_id in (None, current_map))]
        results = []
        with self.lock:
            for token_id in token_ids:
                token = self.game_state.tokens.get(token_id)
                if not token:
                    continue
                sheet = self._sheet_for_token_locked(token)
                dex = token_stat.token_dex_mod(token, sheet)
                roll = combat_rules.roll_initiative(dex)
                token.initiative = max(-10, min(99, roll['total']))
                results.append({'token_id': token.id, 'name': token.name,
                                'roll': roll['roll'], 'mod': roll['mod'],
                                'initiative': token.initiative})
            if results:
                self._prune_order_locked()
                payload = self._combat_payload_locked(self.game_state)
        if not results:
            return
        for entry in results:
            self.broadcast(MessageType.UPDATE_TOKEN,
                           {'token_id': entry['token_id'],
                            'initiative': entry['initiative']})
        self.broadcast(MessageType.INITIATIVE_UPDATE, payload)
        self.broadcast(MessageType.ROLL_INITIATIVE, {'entries': results})
        # В журнале — не только итог, но и сам бросок: за столом видно,
        # что кубик бросили честно: «Рингер d20 12 +3 = 15».
        self.push_log("Инициатива: " + ", ".join(
            f"{e['name']} к20 {e['roll']} {int(e['mod']):+d} = "
            f"{e['initiative']}" for e in results))

    def roll_save(self, player_id: str, data: dict):
        """Спасбросок токена против Сл.

        Бонус берётся с листа персонажа или из статблока существа —
        именно это делает спасброски «подвязанными к листу», а не отдельной
        кнопкой с ручным числом.
        """
        token_id = data.get('token_id')
        ability = token_stat.normalize_ability(data.get('ability') or 'dex')
        if not ability:
            return
        try:
            dc = int(data.get('dc')) if data.get('dc') is not None else None
        except (TypeError, ValueError):
            dc = None
        with self.lock:
            token = self.game_state.tokens.get(token_id)
            if not token:
                return
            if not self.is_dm(player_id) and token.owner_id != player_id:
                client = self.clients.get(player_id)
                if client:
                    client.send_message(MessageType.PERMISSION_DENIED,
                                        {'action': 'roll_save'})
                return
            sheet = self._sheet_for_token_locked(token)
            bonus = token_stat.token_save_bonus(token, sheet, ability)
            # Благословение: +1d4 к спасброскам, отдельный бросок на каждый.
            bless_bonus, bless_detail = self._bless_bonus_locked(token)
            if bless_bonus:
                bonus += bless_bonus
            # Порча: −1d4 к спасброскам, тоже свой бросок на каждый.
            bane_penalty, bane_detail = self._bane_penalty_locked(token)
            if bane_penalty:
                bonus -= bane_penalty
            # Ручной бонус из диалога принимаем ТОЛЬКО от мастера: иначе игрок
            # пришлёт себе +20 и спасбросок ничего не будет значить. Поле
            # 'bonus' клиент отправляет всегда — его игнорируем намеренно.
            if self.is_dm(player_id) and data.get('override_bonus') is not None:
                try:
                    bonus = int(data['override_bonus'])
                except (TypeError, ValueError):
                    pass
            conditions = self._conditions_with_hp(token, sheet)
            # Состояния — по правилам dnd.su: паралич/окаменение/без
            # сознания дают автопровал Силы и Ловкости, опутанность —
            # помеху на Ловкость, истощение 3+ — помеху на всё. Яд на
            # спасброски не влияет (только на атаки и проверки).
            save_mods = conditions_rules.save_modifiers(conditions, ability)
            # Ускорение даёт преимущество на спасброски Ловкости. Мастер
            # может снять его вручную галочкой «помеха»: преимущество и
            # помеха взаимно гасятся, как в 5e.
            adv_labels = self._save_advantage_locked(token, ability)
            adv = bool(data.get('advantage')) or bool(adv_labels)
            dis = bool(data.get('disadvantage')) or save_mods['disadvantage']
            auto_fail = save_mods['auto_fail']
            if auto_fail:
                result = {'rolls': [1], 'kept': 1, 'bonus': bonus,
                          'total': 1 + bonus, 'dc': dc, 'success': False,
                          'mode': None, 'natural': False, 'auto_fail': True}
            else:
                result = combat_rules.roll_save(bonus, dc, advantage=adv,
                                                disadvantage=dis)
                result['auto_fail'] = False
            payload = {'token_id': token.id, 'name': token.name,
                       'ability': ability,
                       'ability_label': token_stat.ABILITY_LABELS.get(ability, ability),
                       'dc': dc, 'conditions': conditions, **result}
            if bless_detail:
                payload['bless'] = bless_detail
            if bane_detail:
                payload['bane'] = bane_detail
            if adv_labels:
                payload['advantage_from'] = adv_labels
        self.broadcast(MessageType.SAVE_RESULT, payload)
        verdict = "успех" if payload.get('success') else "провал"
        text = (f"🎲 Спасброск {payload['ability_label']} — "
                f"{payload['name']}: " + combat_rules.d20_text(payload))
        if dc is not None:
            text += f" против Сл {dc} — {verdict}"
        if payload.get('auto_fail'):
            text += " (автопровал)"
        if payload.get('advantage_from'):
            text += f" (преимущество: {', '.join(payload['advantage_from'])})"
        for key, label, sign in (("bless", "благословение", "+"),
                                 ("bane", "порча", "−")):
            detail = payload.get(key)
            if not isinstance(detail, dict) or not detail.get('total'):
                continue
            text += f"; {label} {sign}{detail['total']}"
            shown = combat_rules.dice_detail_text(detail)
            if shown:
                text += f" ({shown})"
        self.push_log(text)

    def death_save(self, player_id: str, data: dict):
        """Спасбросок от смерти: бросок, ручная отметка или воскрешение.

        Доступно только на нуле хитов. Игрок — своему токену, мастер — любому:
        бросок делает сервер, а ручная отметка нужна, когда кубик бросали в
        Dice Lab или настоящими кубиками и результат вводят руками.
        """
        token_id = data.get('token_id')
        action = str(data.get('action') or 'roll')
        with self.lock:
            token = self.game_state.tokens.get(token_id)
            if token is None:
                return
            if not self.is_dm(player_id) and token.owner_id != player_id:
                client = self.clients.get(player_id)
                if client:
                    client.send_message(MessageType.PERMISSION_DENIED,
                                        {'action': 'death_save'})
                return
            state = combat_rules.death_save_state(token.death_saves)
            roll = None
            revived = False
            stabilized = False
            if action == 'stabilize':
                # Кнопка мастера «Стабилизировать» в окне спасбросков.
                # По 5e медицина оставляет персонажа на 0 хитов и стабильным,
                # но за столом это значит «выбыл и скучает»: в этом проекте
                # стабилизация возвращает в игру с 1 хитом (так же, как
                # воскрешение поднимает с 1, а не с полного запаса).
                if not self.is_dm(player_id):
                    self.send_error(player_id,
                                    "Стабилизировать может только мастер")
                    return
                if token.hp is None or int(token.hp) > 0:
                    self.send_error(
                        player_id,
                        "Стабилизировать можно только при 0 хитов")
                    return
                if state['dead']:
                    self.send_error(
                        player_id,
                        f"«{token.name}» мёртв — его можно только воскресить")
                    return
                token.death_saves = []
                token.hp = 1
                state = combat_rules.death_save_state(token.death_saves)
                stabilized = True
            elif action == 'revive':
                if not self.is_dm(player_id):
                    self.send_error(player_id,
                                    "Воскресить может только мастер")
                    return
                if not state['dead']:
                    self.send_error(player_id,
                                    "Воскресить можно только мёртвого")
                    return
                token.death_saves = []
                # Воскрешение поднимает с 1 хитом, а не с полным запасом:
                # персонаж вернулся в бой, но остаётся раненым и уязвимым.
                token.hp = 1
                state = combat_rules.death_save_state(token.death_saves)
                revived = True
            else:
                if token.hp is None or int(token.hp) > 0:
                    self.send_error(
                        player_id,
                        "Спасброски от смерти доступны только при 0 хитов")
                    return
                if state['dead']:
                    self.send_error(
                        player_id,
                        f"«{token.name}» мёртв — его можно только воскресить")
                    return
                if action == 'set':
                    value = str(data.get('result') or '')
                    if value == combat_rules.DEATH_SUCCESS:
                        passed = True
                    elif value == combat_rules.DEATH_FAILURE:
                        passed = False
                    else:
                        passed = None
                    token.death_saves, state = combat_rules.set_death_save(
                        token.death_saves, data.get('index'), passed)
                else:
                    if state['stable']:
                        self.send_error(
                            player_id,
                            "Персонаж стабилизирован — спасброски не нужны")
                        return
                    roll = combat_rules.roll_death_save()
                    token.death_saves, state = combat_rules.add_death_save(
                        token.death_saves, roll['success'])
            payload = {
                'token_id': token.id,
                'name': token.name,
                'action': action,
                'revived': revived,
                'stabilized': stabilized,
                'hp': token.hp,
                'hp_max': token.hp_max,
                'death_saves': list(token.death_saves),
                'successes': state['successes'],
                'failures': state['failures'],
                'dead': state['dead'],
                'stable': state['stable'],
                'dying': state['dying'],
                'slots': state['slots'],
                'dc': state['dc'],
            }
            if roll is not None:
                payload['d20'] = roll['d20']
                payload['success'] = roll['success']
            sheet_payload = (self._sheet_payload_for_token_hp_locked(token)
                             if (revived or stabilized) else None)
        self.broadcast(MessageType.UPDATE_TOKEN, {
            'token_id': payload['token_id'],
            'hp': payload['hp'],
            'hp_max': payload['hp_max'],
            'death_saves': payload['death_saves'],
        })
        if sheet_payload:
            self.broadcast(MessageType.SHEET_STATE, sheet_payload)
        self.broadcast(MessageType.DEATH_SAVE_RESULT, payload)

        if revived:
            self.push_log(f"✨ {payload['name']} воскрешён "
                          f"(хиты {payload['hp']})")
        elif stabilized:
            self.push_log(f"🩹 {payload['name']} стабилизирован "
                          f"(хиты {payload['hp']})")
        elif action == 'roll':
            verdict = "успех" if payload.get('success') else "провал"
            self.push_log(
                f"💀 Спасбросок от смерти — {payload['name']}: "
                f"d20 {payload['d20']} — {verdict} "
                f"({combat_rules.death_save_text(state)})")
        else:
            self.push_log(f"💀 {payload['name']}: спасброски от смерти — "
                          f"{combat_rules.death_save_text(state)}")
        if state['dead']:
            self.push_log(f"☠ {payload['name']} мёртв")

    def special_attack(self, player_id: str, data: dict):
        """Особая атака существа: дыхание Бехира, проглатывание, ужас.

        Ключевое правило 5e: спасбросок бросает ЦЕЛЬ, а не существо.
        Поэтому запись из справочника («спасбросок Ловкости Сл 16») — это
        действие существа по цели, а не бросок за само существо.

        Область (линия, конус, куб, сфера) собирается той же геометрией,
        что у заклинаний, поэтому 20-футовая линия накрывает всех, кто в
        неё попал, а один бросок урона общий — как в правилах.
        """
        data = data if isinstance(data, dict) else {}
        attacker_id = str(data.get('token_id') or '')
        target_id = str(data.get('target_id') or '')
        attack_id = str(data.get('attack_id') or '')
        with self.lock:
            attacker = self.game_state.tokens.get(attacker_id)
            if attacker is None:
                self.send_error(player_id, "Особая атака: существо не найдено")
                return
            if not self.is_dm(player_id) and attacker.owner_id != player_id:
                client = self.clients.get(player_id)
                if client:
                    client.send_message(MessageType.PERMISSION_DENIED,
                                        {'action': 'special_attack'})
                return
            entry = special_attack_rules.find(
                attacker.statblock or {}, attack_id)
            if not entry:
                self.send_error(player_id, "Особая атака: запись не найдена")
                return
            gs = self.game_state
            # Перезарядка 5–6 (дыхание драконов и Бехира): пока не
            # восстановилась — второй раз в том же бою нельзя.
            recharge_key = (attacker.id,
                            str(entry.get('id') or entry.get('name')))
            if entry.get('recharge') and gs.combat_active:
                if recharge_key in self._special_used:
                    self.send_error(
                        player_id,
                        f"«{entry['name']}»: перезарядка {entry['recharge']} "
                        "ещё не восстановилась")
                    return
                self._special_used[recharge_key] = gs.combat_round

            map_data = gs.maps.get(getattr(attacker, 'map_id', None))
            cell_w, cell_h = ((0.0, 0.0) if map_data is None
                              else map_data.grid_cell_world)
            shape = str(entry.get('shape') or '')
            target = gs.tokens.get(target_id) if target_id else None
            targets = []
            if shape and map_data is not None and cell_w > 0 and cell_h > 0 \
                    and target is not None:
                cols = max(1, int(map_data.grid_cols or 0))
                rows_count = max(1, int(map_data.grid_rows or 0))

                def cell_of(token):
                    return (int((float(token.x) - map_data.pos_x) // cell_w),
                            int((float(token.y) - map_data.pos_y) // cell_h))

                cells = set(special_attack_rules.shape_cells(
                    entry, cell_of(attacker), cell_of(target), cols, rows_count))
                for token in list(gs.tokens.values()):
                    if token.id == attacker.id:
                        continue
                    if getattr(token, 'map_id', None) != map_data.id:
                        continue
                    if not getattr(token, 'visible', True):
                        continue
                    if token.hp is None:
                        continue
                    if combat_rules.death_save_state(
                            token.death_saves)['dead']:
                        continue
                    if cell_of(token) in cells:
                        targets.append(token)
            elif target is not None:
                targets.append(target)
            if not targets:
                self.send_error(
                    player_id,
                    f"«{entry['name']}»: в области нет целей")
                return

            damage = {}
            total = 0
            if entry.get('damage'):
                damage = combat_rules.roll_damage(
                    entry['damage'],
                    damage_type=str(entry.get('damage_type') or ''))
                if damage.get('error'):
                    self.send_error(player_id,
                                    f"Урон не посчитан: {damage['error']}")
                    return
                total = int(damage.get('total') or 0)
            dc = entry.get('dc')
            ability = str(entry.get('ability') or 'dex')
            damage_type = (str(entry.get('damage_type') or '')
                           or combat_rules.damage_type_from_text(
                               str(entry.get('damage') or '')))
            half = bool(entry.get('half_on_success'))

            rows_out = []
            token_payloads = []
            for token in targets:
                sheet = self._sheet_for_token_locked(token)
                saved = None
                save_info = None
                bonus = 0
                if dc is not None:
                    bonus = token_stat.token_save_bonus(token, sheet, ability)
                    save_info = combat_rules.roll_save(bonus, int(dc))
                    saved = bool(save_info.get('success'))
                base = 0 if (saved and not half) else total
                if saved and half:
                    base = total // 2
                protection = token_stat.token_protection(token, sheet)
                dealt = combat_rules.apply_resistance(
                    base, damage_type, **protection)
                amount = max(0, int(dealt.get('amount') or 0))
                before = max(0, int(token.hp))
                if amount:
                    token.hp = max(0, before - amount)
                rows_out.append({
                    'token_id': token.id,
                    'name': token.name,
                    'save_success': saved,
                    'bonus': int(bonus),
                    'dc': dc,
                    'roll': save_info,
                    'amount': amount,
                    'hp': token.hp,
                    'hp_max': token.hp_max,
                    'note': str(dealt.get('note') or ''),
                })
                token_payloads.append({
                    'token_id': token.id,
                    'hp': token.hp,
                    'death_saves': list(token.death_saves or []),
                })
            payload = {
                'token_id': attacker.id,
                'attacker_name': attacker.name,
                'attack_id': entry.get('id'),
                'attack_name': entry.get('name'),
                'shape': shape,
                'size_ft': entry.get('size_ft'),
                'width_ft': entry.get('width_ft'),
                'damage_type': damage_type,
                'damage': damage,
                'half_on_success': half,
                'ability': ability,
                'dc': dc,
                'targets': [token.id for token in targets],
                'rows': rows_out,
                'fx': special_attack_rules.fx_key(entry),
            }
        for token_payload in token_payloads:
            self.broadcast(MessageType.UPDATE_TOKEN, token_payload)
        self.broadcast(MessageType.SPECIAL_ATTACK_RESULT, payload)
        self.push_log(special_attack_rules.roll_text(
            payload['attacker_name'], entry, rows_out, damage))
        for row in rows_out:
            if row['hp'] is not None and row['hp'] <= 0:
                self.push_log(f"☠ {row['name']} повержен (0 хитов)")

    def roll_attack(self, player_id: str, data: dict):
        """Атака токена по токену: попадание, крит, урон, сопротивление.

        Права: ДМ — любой атакующий, игрок — только свой токен или свой
        персонаж. Целью может быть любой токен, кроме своего — бить себя
        можно только через урон вручную (так сложнее случайно убить своего
        персонажа одним кликом).
        """
        attacker_id = data.get('attacker_id')
        target_id = data.get('target_id')
        if not target_id:
            return
        if attacker_id is None:
            attacker_id = player_id
        with self.lock:
            attacker = self.game_state.tokens.get(attacker_id)
            target = self.game_state.tokens.get(target_id)
            if target is None:
                return
            target_death = combat_rules.death_save_state(target.death_saves)
            # Снимок «до» нужен клиенту для отмены атаки: иначе Ctrl+Z
            # вернул бы только хиты, но не кружки спасбросков.
            death_before = list(target.death_saves)
            hp_before_attack = (max(0, int(target.hp))
                                if target.hp is not None
                                else None)
            # Мёртвый токен больше не цель для атаки: его можно только
            # воскресить (кнопка мастера), иначе добивание было бы вечным.
            if target_death['dead']:
                self.send_error(
                    player_id,
                    f"«{target.name}» мёртв — его нельзя выбрать целью атаки")
                return
            if not self.is_dm(player_id):
                # Игрок атакует своим токеном или своим персонажем,
                # и только по чужой цели.
                owns_attacker = (attacker is not None
                                 and (attacker.owner_id == player_id
                                      or attacker.id == player_id))
                if not owns_attacker:
                    client = self.clients.get(player_id)
                    if client:
                        client.send_message(MessageType.PERMISSION_DENIED,
                                            {'action': 'roll_attack'})
                    return
                if target.owner_id == player_id:
                    client = self.clients.get(player_id)
                    if client:
                        client.send_message(MessageType.PERMISSION_DENIED,
                                            {'action': 'roll_attack'})
                    return

            attacker_sheet = self._sheet_for_token_locked(attacker) if attacker else None
            target_sheet = self._sheet_for_token_locked(target)
            target_ac = token_stat.token_ac(target, target_sheet)

            if attacker is None:
                self.send_error(player_id,
                                "Атакующий токен не найден на карте")
                return

            # Состояния: парализованный, ошеломлённый, без сознания и
            # окаменевший не действуют вовсе. Раньше сервер проверял только
            # права, и мёртвый токен продолжал бить.
            attacker_conditions = self._conditions_with_hp(attacker,
                                                           attacker_sheet)
            attacker_dead = combat_rules.death_save_state(
                attacker.death_saves)['dead']
            can_act, why = conditions_rules.can_act(
                attacker_conditions, dead=attacker_dead, hp=attacker.hp)
            if not can_act:
                self.send_error(
                    player_id,
                    f"«{attacker.name}» не может атаковать: {why}")
                return
            target_conditions = self._conditions_with_hp(target, target_sheet)

            try:
                bonus = int(data.get('attack_bonus') or 0)
            except (TypeError, ValueError):
                bonus = 0
            bonus = max(-20, min(30, bonus))
            # Благословение: +1d4 к броску атаки, кубик бросается здесь.
            bless_bonus, bless_detail = self._bless_bonus_locked(attacker)
            if bless_bonus:
                bonus = max(-20, min(30, bonus + bless_bonus))
            # Порча: −1d4 к броску атаки того, кто провалил спасбросок.
            bane_penalty, bane_detail = self._bane_penalty_locked(attacker)
            if bane_penalty:
                bonus = max(-20, min(30, bonus - bane_penalty))

            # Дистанция нужна двум правилам: сбитый с ног даёт преимущество
            # только в упоре, а парализованного/бессознательного в упоре бьют
            # критически. Карта известна — считаем в футах по её клеткам.
            within_5ft = self._tokens_within_5ft(attacker, target)
            modes = conditions_rules.attack_modifiers(
                attacker_conditions, target_conditions, within_5ft)
            advantage = bool(data.get('advantage')) or modes['advantage']
            disadvantage = (bool(data.get('disadvantage'))
                            or modes['disadvantage'])

            # Удар Зефира (Zephyr Strike): пока держится концентрация, один раз
            # можно дать себе преимущество на бросок атаки оружием. Атака
            # заклинанием (Горящий снаряд и т.п.) — не оружие, поэтому её
            # заклинание не ускоряет и не тратит.
            # Помеха от чужого эффекта (Потрясающий удар): состояние это не
            # SRD, поэтому оно лежит эффектом и читается здесь.
            disadv_once = False
            disadv_effect = self._attack_disadvantage_effect_locked(attacker)
            if disadv_effect is not None:
                disadvantage = True
                if 'помеха от эффекта' not in (modes.get('notes') or []):
                    modes.setdefault('notes', []).append('помеха от эффекта')
                # Злая насмешка: помеха только на СЛЕДУЮЩИЙ бросок атаки,
                # поэтому этот бросок её и тратит — чип снимается сразу,
                # как только кость брошена (Потрясающий удар без `once`
                # держится до конца следующего хода цели, как велит правило).
                disadv_once = bool(disadv_effect.get('once'))
            favor_effect = self._pending_favor_locked(attacker)
            zephyr_effect = None
            if str(data.get('ability') or '').strip().casefold() != 'spell':
                zephyr_effect = self._pending_zephyr_locked(attacker)
            if zephyr_effect is not None:
                advantage = True

            attack = combat_rules.roll_attack(
                bonus, target_ac, advantage=advantage,
                disadvantage=disadvantage)
            if bless_detail:
                attack['bless'] = bless_detail
            if bane_detail:
                attack['bane'] = bane_detail
            # Паралич/бессознательность: попадание в упор — критическое,
            # даже если на d20 выпало не 20.
            if attack['hit'] and modes.get('auto_crit'):
                attack['crit'] = True
                attack['critical'] = True
                attack['auto_crit'] = True

            # Скрытая атака: клиент о ней не просит и костей не присылает.
            # Право на неё, оружие, преимущество, союзник у цели и «раз в ход»
            # проверяются здесь по листу атакующего.
            # Без инициативы понятия «ход» нет: в свободной игре скрытая
            # атака не должна блокироваться навсегда после первого удара.
            track_turn = bool(self.game_state.combat_active)
            sneak_plan = None
            if attack['hit'] and isinstance(attacker_sheet, dict):
                self._sneak_sync_locked()
                sneak_plan = class_features.sneak_attack_plan(
                    attacker_sheet,
                    name=str(data.get('weapon') or ''),
                    ranged=bool(data.get('ranged')),
                    ability=str(data.get('ability') or ''),
                    advantage=advantage,
                    disadvantage=disadvantage,
                    ally_within_5ft=self._ally_within_5ft_locked(attacker,
                                                               target),
                    used_this_turn=(track_turn
                                    and attacker.id in self._sneak_used))

            damage_info = None
            death_info = None
            extra_payloads = []
            concentration_notes = []
            if disadv_once:
                # Одноразовая помеха отработала: снимаем чип и сообщаем
                # всем новое состояние атакующего (без этого он висел бы до
                # конца боя, гася все его атаки).
                attacker.active_effects = [
                    item for item in (attacker.active_effects or [])
                    if item is not disadv_effect]
                extra_payloads.append({
                    'token_id': attacker.id,
                    'active_effects': list(attacker.active_effects or []),
                })
            smite_payload = None
            spell_smite_payload = None
            mark_payload = None
            rage_payload = None
            zephyr_payload = None
            favor_payload = None
            sneak_payload = None
            payload_sheet_attacker = None
            zephyr_movement = None
            attacker_effects_changed = False
            banish_applied = False
            attacker_effects_after = None
            target_effects_after = None
            smite_burst_updates = []
            smite_note = ''
            hp_tracked = target.hp is not None
            before = target.hp if target.hp is not None else (target.hp_max or 0)
            before = max(0, int(before))
            payload_token = None
            payload_sheet = None

            # Божественная кара: клиент только просит круг. Разрешена ли она,
            # хватает ли ячейки и сколько кубиков бросать — решает сервер по
            # листу атакующего и типу цели. Ячейка тратится только при
            # попадании: промах не должен съедать ресурс.
            try:
                smite_slot = int(data.get('smite_slot') or 0)
            except (TypeError, ValueError):
                smite_slot = 0
            smite_plan = None
            if smite_slot:
                if not attacker_sheet:
                    self.send_error(
                        player_id,
                        'Божественная кара: у атакующего нет листа персонажа')
                    return
                if bool(data.get('ranged')):
                    self.send_error(
                        player_id,
                        'Божественная кара работает только в рукопашной атаке')
                    return
                smite_plan = class_features.smite_plan(
                    attacker_sheet, smite_slot,
                    extra=class_features.is_fiend_or_undead(target, target_sheet))
                if smite_plan is None:
                    self.send_error(
                        player_id,
                        f'Божественная кара: нет свободной ячейки {smite_slot} '
                        'круга или она недоступна')
                    return

            # Кара-заклинание (Громовая, Злобная, Пылающая): все три требуют
            # рукопашной атаки оружием, поэтому дальний выстрел её не тратит.
            spell_smite_effect = self._pending_smite_locked(
                attacker, ranged=bool(data.get('ranged')))

            if attack['hit'] and (data.get('damage') or smite_plan
                                  or spell_smite_effect):
                protection = token_stat.token_protection(target, target_sheet)
                stone_factor = conditions_rules.damage_factor(target_conditions)

                def _stone(part):
                    """Окаменение даёт сопротивление ВСЕМУ урону.

                    Сопротивления в 5e не складываются: если у цели уже было
                    сопротивление к типу, второе не делает урон вчетверо меньше.
                    """
                    if not part:
                        return part
                    base_factor = part.get('factor', 1.0)
                    if stone_factor < 1.0 and base_factor >= 1.0:
                        combined = base_factor * stone_factor
                        part['amount'] = int(part['total'] * combined)
                        part['factor'] = combined
                        note = part.get('note') or ''
                        part['note'] = ((note + ', ' if note else '')
                                        + 'окаменение')
                    return part

                weapon_info = None
                mark_info = None
                #: Подпись и тип урона добавки: у Метки охотника — тип
                #: оружия, у Сглаза — некротический (он бьёт сам по себе).
                mark_label = ''
                mark_type = ''
                formula = str(data.get('damage') or '')
                # Тип урона мог остаться словом внутри формулы (старые
                # карточки и сохранения): сервер сам отделяет его, иначе
                # сопротивление цели не сработает у удара и у метки.
                formula, parsed_type = combat_rules.split_damage_text(formula)
                attack_damage_type = combat_rules.normalize_damage_type(
                    data.get('damage_type') or parsed_type)
                # Ярость: +2/+3/+4 к урону рукопашной атаки Силой. Это
                # плоская прибавка, поэтому она входит в формулу до броска —
                # крит удваивает только кости, а сопротивление цели
                # уменьшает весь урон вместе с прибавкой.
                rage_amount = class_features.rage_damage_bonus(
                    attacker, ranged=bool(data.get('ranged')),
                    ability=str(data.get('ability') or ''))
                if formula and rage_amount:
                    formula = class_features.add_damage_bonus(
                        formula, rage_amount)
                    rage_payload = {
                        'bonus': rage_amount,
                        'label': 'Ярость',
                    }
                if formula:
                    weapon_info = _stone(combat_rules.roll_damage(
                        formula, critical=attack['critical'],
                        damage_type=attack_damage_type,
                        **protection))
                    # Метка охотника: пока цель помечена этим атакующим,
                    # каждое попадание добавляет 1к6 урона того же типа.
                    # Кубик удваивается на крите вместе с остальными костями
                    # атаки, поэтому mark_die() уже возвращает 2к6.
                    mark = class_features.mark_on(target, attacker.id)
                    if mark:
                        # Сглаз наносит некротический урон независимо от
                        # оружия: у Метки охотника типа в эффекте нет, и
                        # тогда добавка идёт тем же типом, что и удар.
                        hex_mark = (str(mark.get('spell_key') or '')
                                    == 'hex')
                        mark_label = 'Сглаз' if hex_mark else 'Метка охотника'
                        mark_type = (combat_rules.normalize_damage_type(
                            mark.get('damage_type')) or attack_damage_type)
                        mark_info = _stone(combat_rules.roll_damage(
                            class_features.mark_die(attack['critical']),
                            critical=False,
                            damage_type=mark_type,
                            **protection))
                smite_info = None
                if smite_plan:
                    smite_info = _stone(combat_rules.roll_damage(
                        smite_plan['dice'], critical=attack['critical'],
                        damage_type=smite_plan['damage_type'],
                        **protection))
                    if class_features.spend_slot(attacker_sheet,
                                                 smite_plan['slot_level']):
                        smite_payload = {
                            'slot_level': int(smite_plan['slot_level']),
                            'dice': smite_plan['dice'],
                            'formula': smite_info.get('formula'),
                            'total': int(smite_info.get('total') or 0),
                            'amount': int(smite_info.get('amount') or 0),
                            'damage_type': smite_plan['damage_type'],
                            'extra': bool(smite_plan.get('extra')),
                            'note': smite_info.get('note') or '',
                        }
                        by_token = getattr(self.game_state, 'token_sheets', None)
                        if isinstance(by_token, dict):
                            by_token[attacker.id] = attacker_sheet
                        if attacker.owner_id:
                            self.game_state.sheets[attacker.owner_id] = \
                                attacker_sheet
                        payload_sheet_attacker = {
                            'player_id': attacker.owner_id or '',
                            'sheet': attacker_sheet,
                            'token_id': attacker.id,
                        }
                    else:
                        # Ячейку забрали между проверкой и попаданием — кара
                        # не срабатывает, но атака остаётся обычной.
                        smite_info = None
                        smite_plan = None
                        smite_note = ('✨ Божественная кара: свободных ячеек '
                                      'не осталось')
                if mark_info:
                    mark_payload = {
                        'die': '1d6',
                        'label': mark_label or 'Метка охотника',
                        'damage_type': mark_type,
                        'formula': mark_info.get('formula'),
                        'total': int(mark_info.get('total') or 0),
                        'amount': int(mark_info.get('amount') or 0),
                    }
                # Кара-заклинание: кубики и последствие по правилу, которое
                # ждало этого удара. Спасбросок цели бросает сервер; у
                # Пылающей кары на попадании спасброска нет — только
                # напоминание об 1к6 огня в начале её ходов.
                spell_smite_info = None
                spell_smite_payload = None
                smite_rider_applied = []
                rider_throw = None
                smite_burst_rows = []
                smite_burst_updates = []
                if spell_smite_effect:
                    rider = str(spell_smite_effect.get('rider') or '')
                    rider_save = str(
                        spell_smite_effect.get('rider_save') or '')
                    rider_success = None
                    smite_dice = str(spell_smite_effect.get('dice') or '')
                    if smite_dice:
                        spell_smite_info = _stone(combat_rules.roll_damage(
                            smite_dice, critical=attack['critical'],
                            damage_type=str(
                                spell_smite_effect.get('damage_type') or ''),
                            **protection))
                    if rider == 'burst':
                        # Град шипов: спасбросок проходят цель и соседи в
                        # 5 футах. Урон цели входит в общий урон удара,
                        # у соседей он приходит отдельным обновлением токена.
                        smite_burst_rows, smite_burst_updates = \
                            self._smite_burst_locked(
                                attacker, target, spell_smite_effect,
                                attacker_sheet)
                        center_row = next(
                            (row for row in smite_burst_rows
                             if row.get('token_id') == target.id), None)
                        if center_row:
                            rider_success = bool(center_row.get('save_success'))
                            if center_row.get('part'):
                                spell_smite_info = center_row['part']
                    elif rider_save and rider != 'burn':
                        try:
                            dc = spell_rules.spell_save_dc(attacker_sheet or {})
                        except (TypeError, ValueError):
                            dc = 10
                        # Большой и крупнее проходит спасбросок Силы от
                        # лозы с преимуществом (PHB).
                        advantage_save = bool(
                            rider == 'restrained' and rider_save == 'str'
                            and self._target_is_large_locked(target))
                        rider_throw = self._save_with_mods_locked(
                            target, target_sheet, rider_save, dc,
                            advantage=advantage_save)
                        rider_success = bool(rider_throw.get('success'))
                    if rider != 'burst' and rider_success is not True:
                        smite_rider_applied = self._apply_smite_rider_locked(
                            target, spell_smite_effect, caster_id=attacker.id,
                            source=str(spell_smite_effect.get('label') or ''))
                    spell_smite_payload = {
                        'dice': str(spell_smite_effect.get('dice') or ''),
                        'label': str(
                            spell_smite_effect.get('label') or 'Кара'),
                        'spell_key': str(
                            spell_smite_effect.get('spell_key') or ''),
                        'damage_type': str(
                            spell_smite_effect.get('damage_type') or ''),
                        'formula': (spell_smite_info or {}).get('formula'),
                        'total': int(
                            (spell_smite_info or {}).get('total') or 0),
                        'amount': int(
                            (spell_smite_info or {}).get('amount') or 0),
                        'rider': rider,
                        'rider_save': rider_save,
                        'rider_success': rider_success,
                        'rider_total': (int(rider_throw.get('total') or 0)
                                        if rider_throw else None),
                        'applied': smite_rider_applied,
                        'burst': smite_burst_rows,
                        'note': str(
                            spell_smite_effect.get('rider_note') or ''),
                    }
                    if self._consume_smite_locked(attacker,
                                                 spell_smite_effect):
                        attacker_effects_changed = True
                # Скрытая атака: только плут, раз в ход, фехтовальное или
                # дальнобойное оружие. Кости удваиваются на крите, как любая
                # часть урона атаки.
                # +1к8 силового урона от Удара Зефира — часть урона атаки,
                # поэтому на крите кость удваивается вместе с остальными.
                # Божественное благоволение: тот же кубик на каждое
                # попадание оружием (не атаку заклинанием) — и не сгорает.
                favor_info = None
                if (favor_effect is not None
                        and str(data.get('ability') or '').strip().casefold()
                        != 'spell'):
                    favor_info = _stone(combat_rules.roll_damage(
                        '1d4', critical=attack['critical'],
                        damage_type='radiant', **protection))
                    favor_payload = {
                        'dice': '1d4',
                        'formula': (favor_info or {}).get('formula'),
                        'total': int((favor_info or {}).get('total') or 0),
                        'amount': int((favor_info or {}).get('amount') or 0),
                        'label': 'Божественное благоволение',
                    }
                zephyr_info = None
                if zephyr_effect is not None:
                    zephyr_info = _stone(combat_rules.roll_damage(
                        '1d8', critical=attack['critical'],
                        damage_type='force', **protection))
                    zephyr_payload = {
                        'dice': '1d8',
                        'formula': (zephyr_info or {}).get('formula'),
                        'total': int((zephyr_info or {}).get('total') or 0),
                        'amount': int((zephyr_info or {}).get('amount') or 0),
                        'label': 'Удар Зефира',
                    }
                sneak_info = None
                if sneak_plan:
                    sneak_info = _stone(combat_rules.roll_damage(
                        sneak_plan['formula'], critical=attack['critical'],
                        damage_type=attack_damage_type, **protection))
                    if sneak_info is not None:
                        if track_turn:
                            self._sneak_used.add(attacker.id)
                        sneak_payload = {
                            'dice': sneak_plan['dice'],
                            'formula': sneak_info.get('formula'),
                            'total': int(sneak_info.get('total') or 0),
                            'amount': int(sneak_info.get('amount') or 0),
                            'reason': sneak_plan['reason'],
                            'label': sneak_plan['label'],
                        }
                # Метка — тоже урон атаки: её кубик обязан уйти в те же
                # хиты, иначе строка чата обещает 1к6, а хит не убывает.
                parts = [part for part in (weapon_info, smite_info, mark_info,
                                          sneak_info, spell_smite_info,
                                          zephyr_info, favor_info)
                         if part]
                if parts:
                    damage_info = dict(parts[0])
                    for extra in parts[1:]:
                        damage_info['amount'] = (int(damage_info.get('amount') or 0)
                                                + int(extra.get('amount') or 0))
                        damage_info['total'] = (int(damage_info.get('total') or 0)
                                               + int(extra.get('total') or 0))
                        damage_info['formula'] = (
                            str(damage_info.get('formula') or '')
                            + ('' if not extra.get('formula') else
                               '+' + str(extra.get('formula'))))
                        damage_info['breakdown'] = (
                            str(damage_info.get('breakdown') or '')
                            + ('' if not extra.get('breakdown') else
                               ' + ' + str(extra.get('breakdown'))))
                    if weapon_info and smite_info:
                        damage_info['smite'] = smite_info
                        damage_info['note'] = ', '.join(
                            filter(None, (weapon_info.get('note'),
                                          smite_info.get('note'))))
                    if mark_info:
                        damage_info['mark'] = mark_info
                    if sneak_info:
                        damage_info['sneak'] = sneak_info
                    if spell_smite_info:
                        damage_info['spell_smite'] = spell_smite_info
                    if zephyr_info:
                        damage_info['zephyr'] = zephyr_info
                    if favor_info:
                        damage_info['favor'] = favor_info
                dealt = int(damage_info['amount']) if damage_info else 0
                # Хиты не уходят в минус: на нуле включаются спасброски.
                target.hp = max(0, before - dealt)
                # Изгоняющая кара решает по хитам ПОСЛЕ удара: если их не
                # больше 50 — цель изгнана. Проверить это можно только здесь.
                if (spell_smite_payload
                        and str(spell_smite_effect.get('rider') or '')
                        == 'banished'):
                    banish = self._banish_after_hit_locked(
                        target, spell_smite_effect, target.hp,
                        source=str(spell_smite_effect.get('label') or ''))
                    spell_smite_payload['banished'] = banish
                    if banish.get('banished'):
                        banish_applied = True
                        self.push_log(
                            f"✧ «{target.name}» изгнан(а): хитов "
                            f"{banish['hp']} (не больше {banish['limit']}) — "
                            "недееспособен(на), пока держится кара")
                payload_token = {'token_id': target.id, 'hp': target.hp,
                                 'hp_max': target.hp_max, 'ac': target.ac,
                                 'delta': target.hp - before,
                                 'source': 'attack'}
                # Концентрация: раненый колдун бросает Телосложение.
                dropped, note = self._concentration_check_locked(
                    target, dealt, target_sheet)
                if note:
                    concentration_notes.append(note)
                for other in dropped:
                    if other.id == target.id:
                        payload_token['active_effects'] = list(
                            other.active_effects or [])
                    else:
                        extra_payloads.append({
                            'token_id': other.id,
                            'active_effects': list(other.active_effects or []),
                        })
                # Если цель — персонаж игрока, те же хиты пишем и в лист.
                payload_sheet = self._sheet_payload_for_token_hp_locked(target)
            # Добивание: по цели с нулём хитов любое попадание — автоматический
            # провал одного спасброска от смерти. Числовой урон не важен:
            # считается сама атака, даже без формулы урона. Токен без хитов
            # (hp=None) не «умирает»: у него просто не ведётся счёт HP.
            if (attack['hit'] and before <= 0 and hp_tracked
                    and not target_death['dead']):
                target.hp = 0
                target.death_saves, death_after = combat_rules.add_death_save(
                    target.death_saves, False)
                death_info = {
                    'auto_failure': True,
                    'successes': death_after['successes'],
                    'failures': death_after['failures'],
                    'dead': death_after['dead'],
                    'stable': death_after['stable'],
                    'results': list(target.death_saves),
                }
                if payload_token is None:
                    payload_token = {'token_id': target.id, 'hp': target.hp,
                                     'hp_max': target.hp_max, 'ac': target.ac,
                                     'delta': 0}
                if payload_sheet is None:
                    payload_sheet = self._sheet_payload_for_token_hp_locked(
                        target)
            if payload_token is not None:
                payload_token['death_saves'] = list(target.death_saves)

            # Удар Зефира кончается на этом ударе (и даже на промахе):
            # преимущество потрачено, а скорость растёт на 30 футов до конца
            # хода — это отдельный эффект на одну эту смену раунда.
            if zephyr_effect is not None:
                zephyr_speed = self._build_effect_locked({
                    'type': 'speed', 'bonus_ft': 30, 'rounds': 1,
                    'label': 'Удар Зефира (+30 футов)',
                    'duration': 'до конца хода',
                    'spell_key': 'zephyr_strike',
                })
                if zephyr_speed:
                    zephyr_effects = list(
                        getattr(attacker, 'active_effects', None) or [])
                    zephyr_effects.append(zephyr_speed)
                    attacker.active_effects = zephyr_effects
                    self._apply_effect_side_locked(attacker, zephyr_speed)
                    attacker_effects_changed = True
                if self._consume_smite_locked(attacker, zephyr_effect):
                    attacker_effects_changed = True
                # Бюджет движения меняется сразу: клиент должен разрешить
                # пройти лишние 30 футов в этом же ходу.
                zephyr_movement = self._movement_payload_locked(attacker)
                self.push_log(
                    f"↗ {attacker.name}: Удар Зефира — скорость +30 футов "
                    "до конца хода")

            # Скрытность после атаки снимается всегда — и при промахе тоже:
            # в 5e, чтобы спрятаться снова, нужен новый бросок Скрытности.
            reveal_payload, reveal_sheet = self._set_hidden_locked(attacker, False)
            if reveal_payload is not None:
                attacker_conditions = self._conditions_with_hp(attacker,
                                                              attacker_sheet)

            payload = {
                'attacker_id': attacker.id if attacker else attacker_id,
                'attacker_name': attacker.name if attacker else '—',
                'target_id': target.id,
                'target_name': target.name,
                # Только известный ключ отклика: по нему клиент рисует луч
                # заклинания, а не обычный удар оружием.
                'fx': str(data.get('fx') or '')[:32],
                'attack': attack,
                'attack_text': combat_rules.attack_text(attack),
                'damage': damage_info,
                'smite': smite_payload,
                'spell_smite': spell_smite_payload,
                'mark': mark_payload,
                'zephyr': zephyr_payload,
                'favor': favor_payload,
                'sneak': sneak_payload,
                'rage': rage_payload,
                'revealed': bool(reveal_payload),
                'target_hp': target.hp,
                'target_hp_before': hp_before_attack,
                'target_hp_max': target.hp_max,
                'death_saves_before': death_before,
                'target_conditions': target_conditions,
                'attacker_conditions': attacker_conditions,
                'condition_notes': modes.get('notes') or [],
                'death_saves': list(target.death_saves),
                'death': death_info,
                'target_dead': combat_rules.death_save_state(
                    target.death_saves)['dead'],
            }
            # Снимки карточек снимаем под замком: после его снятия другое
            # действие могло бы изменить эффекты и токен ушёл бы в сеть
            # с чужим состоянием.
            attacker_effects_after = None
            if attacker_effects_changed:
                attacker_effects_after = {
                    'token_id': payload['attacker_id'],
                    'active_effects': list(attacker.active_effects or []),
                }
            target_effects_after = None
            if ((spell_smite_payload and spell_smite_payload.get('applied'))
                    or banish_applied):
                target_effects_after = {
                    'token_id': target.id,
                    'active_effects': list(target.active_effects or []),
                    'conditions': list(target.conditions or []),
                }
        if payload_token:
            self.broadcast(MessageType.UPDATE_TOKEN, payload_token)
        for extra in extra_payloads:
            self.broadcast(MessageType.UPDATE_TOKEN, extra)
        if attacker_effects_after:
            # Кара сгорела: чип в карточке колдуна должен пропасть у всех.
            self.broadcast(MessageType.UPDATE_TOKEN, attacker_effects_after)
        if zephyr_movement:
            # Удар Зефира: новый бюджет движения с прибавкой +30 футов.
            self._broadcast_movement_state([zephyr_movement])
        if target_effects_after:
            self.broadcast(MessageType.UPDATE_TOKEN, target_effects_after)
        for burst_update in smite_burst_updates:
            self.broadcast(MessageType.UPDATE_TOKEN, burst_update)
        for note in concentration_notes:
            self.push_log(note)
        if smite_note:
            self.push_log(smite_note)
        if payload_sheet:
            self.broadcast(MessageType.SHEET_STATE, payload_sheet)
        if payload_sheet_attacker:
            self.broadcast(MessageType.SHEET_STATE, payload_sheet_attacker)
        if reveal_payload:
            self.broadcast(MessageType.UPDATE_TOKEN, reveal_payload)
        if reveal_sheet:
            self.broadcast(MessageType.SHEET_STATE, reveal_sheet)
        self.broadcast(MessageType.ATTACK_RESULT, payload)

        # В лог пишем одну читаемую строку: кто, по кому, сколько.
        line = (f"⚔ {payload['attacker_name']} → {payload['target_name']}: "
                f"{payload['attack_text']}")
        # Благословение и Порча бросают свой кубик на каждый удар. В чате
        # они видны отдельными строками, а в журнале — здесь, иначе итог
        # атаки не сходится с бонусом и мастер ищет ошибку в правилах.
        for key, label, sign in (("bless", "благословение", "+"),
                                 ("bane", "порча", "−")):
            detail = (payload.get("attack") or {}).get(key)
            if not isinstance(detail, dict) or not detail.get("total"):
                continue
            line += f"; {label} {sign}{detail['total']}"
            shown = combat_rules.dice_detail_text(detail)
            if shown:
                line += f" ({shown})"
        if smite_payload:
            line += (f"; кара {smite_payload['dice']}"
                     + (' (нежить или исчадие)' if smite_payload.get('extra')
                        else ''))
        if spell_smite_payload:
            line += (f"; {spell_smite_payload['label']}"
                     f" = {spell_smite_payload['total']}")
            if spell_smite_payload.get('applied'):
                line += f"; {', '.join(spell_smite_payload['applied'])}"
            elif spell_smite_payload.get('rider_save'):
                line += ('; спасбросок от последствия пройден'
                         if spell_smite_payload.get('rider_success')
                         else '; спасбросок не бросается')
            others = [row for row in (spell_smite_payload.get('burst') or [])
                      if row.get('token_id') != payload.get('target_id')]
            if others:
                line += ('; шипы задели ' + ', '.join(
                    f"{row['name']} ({row['amount']})" for row in others))
        if mark_payload and mark_payload.get('total'):
            line += f"; метка +{mark_payload['total']} (в составе урона)"
        if sneak_payload:
            line += (f"; скрытая атака {sneak_payload['formula']}"
                     f" = {sneak_payload['total']} ({sneak_payload['reason']})")
        if rage_payload:
            line += f"; ярость +{rage_payload['bonus']} (в составе урона)"
        if zephyr_payload:
            line += (f"; удар зефира {zephyr_payload['formula']}"
                     f" = {zephyr_payload['total']} (в составе урона)")
        if favor_payload:
            line += (f"; благоволение {favor_payload['formula']}"
                     f" = {favor_payload['total']} (в составе урона)")
        banish = (spell_smite_payload or {}).get('banished') or {}
        if banish.get('banished'):
            line += (f"; изгнана (хитов {banish.get('hp')} ≤ "
                     f"{banish.get('limit')})")
        if damage_info and damage_info.get('amount'):
            line += f"; урон {damage_info['amount']}"
            # Разбор костей урона — рядом с итогом: «2к6(3, 4)+3».
            extras = [str(damage_info.get('breakdown') or '').strip(),
                      str(damage_info.get('note') or '').strip()]
            extras = [item for item in extras if item]
            if extras:
                line += f" ({'; '.join(extras)})"
            if payload['target_hp'] is not None:
                line += f", хиты {payload['target_hp']}"
                if payload['target_hp_max']:
                    line += f"/{payload['target_hp_max']}"
        elif damage_info and damage_info.get('error'):
            line += f"; урон не посчитан: {damage_info['error']}"
        if death_info:
            line += (f"; спасбросок от смерти провален автоматически "
                     f"({death_info['failures']} из 3)")
        self.push_log(line)
        if payload['target_hp'] is not None and payload['target_hp'] <= 0:
            if payload.get('target_dead'):
                self.push_log(f"☠ {payload['target_name']} мёртв")
            elif data.get('damage') or death_info:
                self.push_log(
                    f"☠ {payload['target_name']} повержен (0 хитов)")

    def _find_area_spell_locked(self, sheet, data: dict, name: str):
        """Найти запись заклинания в листе колдуна по id или названию.

        Нужна, чтобы формула, Сл и тип урона брались с листа, а не из
        сетевого запроса: иначе клиент мог прислать «100d100».
        """
        if not isinstance(sheet, dict):
            return None
        spells = (sheet.get("vtt") or {}).get("spells") or []
        spell_id = str(data.get("spell_id") or "")
        source_id = str(data.get("source_id") or "")
        for record in spells:
            if not isinstance(record, dict):
                continue
            if spell_id and spell_id in (
                    str(record.get("id") or ""),
                    str(record.get("lss_ref") or "")):
                return record
            if source_id and source_id == str(record.get("source_id") or ""):
                return record
            if name and str(record.get("name") or "") == name:
                return record
        return None

    def area_damage(self, player_id: str, data: dict):
        """Заклинание по области: спасброски, урон и новые хиты.

        Клиент присылает только геометрию и формулу. Список целей, броски
        спасброска и урон считает сервер: иначе игрок мог бы прислать
        «все провалили спасбросок» и убить любого на карте.
        """
        caster_id = str(data.get("caster_id") or "")
        map_id = str(data.get("map_id") or self.game_state.current_map_id or "")
        shape = str(data.get("shape") or "circle")
        if shape not in aoe_templates.SHAPES:
            return
        try:
            size_ft = float(data.get("size_ft") or 0)
        except (TypeError, ValueError):
            return
        size_ft = max(5.0, min(120.0, size_ft))
        origin = data.get("origin_cell") or data.get("origin")
        if not isinstance(origin, (list, tuple)) or len(origin) != 2:
            return
        try:
            origin_col, origin_row = int(origin[0]), int(origin[1])
        except (TypeError, ValueError):
            return
        aim = data.get("aim_cell") or [origin_col + 1, origin_row]
        try:
            aim_col, aim_row = int(aim[0]), int(aim[1])
        except (TypeError, ValueError):
            aim_col, aim_row = origin_col + 1, origin_row
        ability = token_stat.normalize_ability(data.get("ability") or "dex") or "dex"
        try:
            dc = int(data.get("dc") or 15)
        except (TypeError, ValueError):
            dc = 15
        dc = max(1, min(40, dc))
        half = bool(data.get("half_on_success"))
        try:
            range_ft = float(data.get("range_ft") or 0)
        except (TypeError, ValueError):
            range_ft = 0.0
        range_ft = max(0.0, min(5280.0, range_ft))
        formula = str(data.get("formula") or "")
        damage_type = combat_rules.normalize_damage_type(data.get("damage_type") or "")
        spell_name = str(data.get("spell_name") or "Заклинание")[:80]

        with self.lock:
            caster = self.game_state.tokens.get(caster_id)
            map_data = self.game_state.maps.get(map_id)
            if caster is None or map_data is None:
                return
            if not self.is_dm(player_id) and caster.owner_id != player_id:
                client = self.clients.get(player_id)
                if client:
                    client.send_message(MessageType.PERMISSION_DENIED,
                                        {'action': 'area_damage'})
                return
            if caster.map_id and caster.map_id != map_data.id:
                self.send_error(player_id, "Заклинание не на текущей карте")
                return
            cols, rows = int(map_data.grid_cols), int(map_data.grid_rows)
            if cols <= 0 or rows <= 0:
                self.send_error(player_id,
                                "Для области нужна сетка карты (Ctrl+G)")
                return
            if not (0 <= origin_col < cols and 0 <= origin_row < rows):
                return
            cw = map_data.width / cols
            ch = map_data.height / rows
            try:
                caster_col = int((caster.x - map_data.pos_x) // cw)
                caster_row = int((caster.y - map_data.pos_y) // ch)
            except (TypeError, ValueError):
                caster_col = caster_row = 0
            caster_sheet = self._sheet_for_token_locked(caster)
            caster_conditions = self._conditions_with_hp(caster, caster_sheet)
            caster_dead = combat_rules.death_save_state(
                caster.death_saves)["dead"]
            can_act, why = conditions_rules.can_act(
                caster_conditions, dead=caster_dead, hp=caster.hp)
            if not can_act:
                self.send_error(
                    player_id, f"«{caster.name}» не может действовать: {why}")
                return
            record = self._find_area_spell_locked(
                caster_sheet, data, spell_name)
            spell_key = spell_rules.canonical(record) if record else ''
            # План заклинания и его формула известны только тогда, когда
            # запись нашлась в листе; для ручной области мастера они пустые.
            spec = {}
            plan_kind = ''
            plan_effects = []
            pool_dice = ''
            trusted_formula = ''
            if record is None and not self.is_dm(player_id):
                self.send_error(
                    player_id,
                    f"«{spell_name}» нет в листе — применение отклонено")
                return
            if record is not None:
                # Всё, что определяет силу заклинания, берём с листа.
                trusted_formula = use_action.formula_from(record.get("damage"))
                # Круг ячейки приходит с клиента (он её и списывает у себя),
                # но в разумных пределах: от него растёт пул Сна и лечение
                # Полного исцеления, и «ячейка 20 круга» ломала бы числа.
                try:
                    cast_slot = int(data.get("slot_level") or 0)
                except (TypeError, ValueError):
                    cast_slot = 0
                base_level = int(record.get("level") or 0)
                if cast_slot < base_level or cast_slot > 9:
                    cast_slot = 0
                spec = spell_rules.plan(record, caster_sheet,
                                        slot_level=cast_slot or None)
                plan_kind = str(spec.get("kind") or "")
                plan_effects = [dict(effect) for effect in
                                (spec.get("effects") or [])
                                if isinstance(effect, dict)]
                # У Сна и Цветного шара формула — не урон, а пул хитов:
                # её берём из правила, а не из поля «урон» листа.
                pool_dice = str(spec.get("formula") or "") \
                    if plan_kind == "pool" else ""
                if spec.get("no_damage"):
                    # Правило заклинания прямо говорит, что урона нет:
                    # поле «урон» в листе (у Паутины там «2к4 огнём» из
                    # слова «горючая») ничего не решает.
                    trusted_formula = ""
                if not trusted_formula and not pool_dice and not plan_effects:
                    self.send_error(
                        player_id, f"«{spell_name}»: формула урона не найдена")
                    return
                # Форму области берём из правила (у Паутины и Сна она есть в
                # плане), а из описания — только если правило её не задало.
                plan_shape = str(spec.get("shape") or "")
                if plan_shape:
                    shape = plan_shape
                    size_ft = max(5.0, min(120.0,
                                           float(spec.get("size_ft") or 20)))
                    half = bool(spec.get("half_on_success"))
                else:
                    area_spec = spell_effects.area_for_spell(record)
                    if not area_spec:
                        self.send_error(
                            player_id,
                            f"«{spell_name}»: область не распознана")
                        return
                    shape = str(area_spec.get("shape") or shape)
                    size_ft = float(area_spec.get("size_ft") or size_ft)
                    size_ft = max(5.0, min(120.0, size_ft))
                    half = bool(area_spec.get("half_on_success"))
                # Пул берётся из правила: от него зависит, кто уснёт, а не
                # только «сколько хитов выпало». У урона приоритет у плана:
                # только в нём есть рост от круга ячейки (Огненный шар
                # 5 кругом — 10к6, а не 8к6 из поля «урон» листа).
                formula = (pool_dice or str(spec.get("formula") or "")
                           or trusted_formula)
                damage_type = combat_rules.damage_type_from_text(
                    record.get("damage"))
                ability = use_action.save_ability(record.get("save")) or "dex"
                trusted_dc = record.get("save_dc")
                if trusted_dc is None:
                    trusted_dc = use_action.spell_dc(caster_sheet)
                try:
                    dc = int(trusted_dc)
                except (TypeError, ValueError):
                    dc = 15
                dc = max(1, min(40, dc))
                range_ft = float(use_action.range_feet(record.get("range")) or 0)
                spell_name = str(record.get("name") or spell_name)[:80]
                if shape not in aoe_templates.SHAPES:
                    return
            if not formula and not plan_effects:
                # Заклинание без урона и без эффекта (только подпись) по
                # площади применить нечем: честнее сказать это прямо.
                self.send_error(
                    player_id,
                    f"«{spell_name}»: у заклинания нет ни урона, ни эффекта")
                return
            # Проверки ниже применяются и к заклинанию с листа, и к
            # ручной области мастера: клиентские поля уже заменены
            # доверенными там, где запись нашлась.
            if shape in ("cone", "line"):
                # Конус и линия по правилам начинаются в клетке колдуна.
                if (origin_col, origin_row) != (caster_col, caster_row):
                    self.send_error(
                        player_id,
                        "Конус и линия начинаются в клетке заклинателя")
                    return
            if range_ft > 0:
                distance = math.hypot(origin_col - caster_col,
                                      origin_row - caster_row) * 5.0
                if distance > range_ft + 0.1:
                    self.send_error(
                        player_id,
                        f"«{caster.name}»: область дальше дистанции "
                        f"заклинания ({range_ft:.0f} фут)")
                    return
            direction_col = aim_col - origin_col
            direction_row = aim_row - origin_row
            if direction_col == 0 and direction_row == 0:
                direction_col = 1
            cells = aoe_templates.shape_cells(
                shape, origin_col, origin_row, size_ft,
                direction_col, direction_row, cols, rows)
            if not cells:
                return
            cell_set = set(cells)

            target_rows = []
            token_updates = []
            sheet_updates = []
            extra_payloads = []
            effect_payloads = []
            concentration_notes = []
            total = 0
            # Сколько хитов выпало в пуле — до того, как он потратился на
            # цели: без этого игрок не понимает, почему заснули не все.
            pool_rolled = 0
            raw = {"formula": formula}
            if formula:
                raw = combat_rules.roll_damage(formula, damage_type="")
                if raw.get("error"):
                    self.send_error(player_id,
                                    f"Урон не посчитан: {raw['error']}")
                    return
                total = int(raw.get("total") or 0)
            # Кто попал в шаблон: этим списком пользуются все три режима —
            # урон, состояния без урона и пул хитов.
            inside = []
            for token in list(self.game_state.tokens.values()):
                if token.map_id != map_data.id or not token.visible:
                    continue
                if token.hp is None:
                    continue
                try:
                    col = int((token.x - map_data.pos_x) // cw)
                    row = int((token.y - map_data.pos_y) // ch)
                except (TypeError, ValueError):
                    continue
                if (col, row) not in cell_set:
                    continue
                if combat_rules.death_save_state(token.death_saves)["dead"]:
                    continue
                inside.append(token)

            # Заклинание с концентрацией (Паутина, Опутывание, Сон) снимает
            # прошлую концентрацию колдуна — как и любой другой каст.
            if any(effect.get("concentration") for effect in plan_effects):
                for other in self._drop_concentration_locked(caster.id):
                    if other.id == caster.id:
                        continue
                    extra_payloads.append({
                        'token_id': other.id,
                        'active_effects': list(other.active_effects or []),
                    })

            if plan_kind == "pool":
                # Пул хитов (Сон, Цветной шар): кубики тратятся на существ
                # с наименьшими текущими хитами, пока хватает суммы. Так
                # заклинание и работает по правилам — «начиная с самых
                # раненных».
                pool_left = total
                pool_rolled = total
                order = sorted(inside, key=lambda item: (
                    int(item.hp or 0), str(item.name or "")))
                for token in order:
                    if pool_left <= 0:
                        break
                    if self._pool_skip_locked(token, spec):
                        continue
                    hp = max(0, int(token.hp or 0))
                    if hp > pool_left:
                        continue
                    pool_left -= hp
                    added = self._attach_effects_locked(
                        token, plan_effects, caster_id=caster.id,
                        source=str(spec.get("name") or spell_name))
                    target_rows.append({
                        "token_id": token.id, "name": token.name,
                        "hp": token.hp, "hp_max": token.hp_max,
                        "delta": 0, "amount": 0,
                        "save": None, "save_success": False,
                        "applied": added,
                        "death_saves": list(token.death_saves),
                        "death": None,
                    })
                    if added:
                        effect_payloads.append(
                            self._active_effects_payload_locked(token, added))
                total = total - pool_left
            else:
                for token in inside:
                    sheet = self._sheet_for_token_locked(token)
                    save = self._save_with_mods_locked(token, sheet, ability, dc)
                    saved = bool(save.get("success"))
                    if plan_effects and not formula:
                        # Заклинание без урона (Паутина, Опутывание, Жир):
                        # проваливший спасбросок получает состояние,
                        # прошедший — ничего.
                        row = {
                            "token_id": token.id, "name": token.name,
                            "hp": token.hp, "hp_max": token.hp_max,
                            "delta": 0, "amount": 0,
                            "save": save, "save_success": saved,
                            "applied": [],
                            "death_saves": list(token.death_saves),
                            "death": None,
                        }
                        if not saved:
                            added = self._attach_effects_locked(
                                token, plan_effects, caster_id=caster.id,
                                source=str(spec.get("name") or spell_name))
                            row["applied"] = added
                            if added:
                                effect_payloads.append(
                                    self._active_effects_payload_locked(
                                        token, added))
                        target_rows.append(row)
                        continue
                    base_amount = total // 2 if saved and half else (
                        0 if saved else total)
                    protection = token_stat.token_protection(token, sheet)
                    dealt = combat_rules.apply_resistance(
                        base_amount, damage_type, **protection)
                    amount = int(dealt.get("amount") or 0)
                    stone = conditions_rules.damage_factor(
                        self._conditions_with_hp(token, sheet))
                    if stone < 1.0 and int(dealt.get("factor", 1.0) or 1.0) >= 1:
                        amount = int(amount * stone)
                    before = max(0, int(token.hp))
                    token.hp = max(0, before - amount)
                    delta = token.hp - before
                    dropped, note = self._concentration_check_locked(
                        token, amount, sheet)
                    if note:
                        concentration_notes.append(note)
                    for other in dropped:
                        if other.id != token.id:
                            extra_payloads.append({
                                'token_id': other.id,
                                'active_effects': list(
                                    other.active_effects or []),
                            })
                    death_info = None
                    if before <= 0 and amount > 0 and not combat_rules.death_save_state(
                            token.death_saves)["dead"]:
                        token.death_saves, death_after = combat_rules.add_death_save(
                            token.death_saves, False)
                        death_info = {
                            "auto_failure": True,
                            "successes": death_after["successes"],
                            "failures": death_after["failures"],
                            "dead": death_after["dead"],
                            "stable": death_after["stable"],
                        }
                    token_row = {
                        "token_id": token.id, "name": token.name,
                        "hp": token.hp, "hp_max": token.hp_max,
                        "delta": delta, "amount": amount,
                        "save": save, "save_success": saved,
                        "death_saves": list(token.death_saves),
                        "death": death_info,
                    }
                    if dropped and any(other.id == token.id for other in dropped):
                        token_row['active_effects'] = list(
                            token.active_effects or [])
                    target_rows.append(token_row)
                    # source говорит клиенту, что цифру уже показала карточка
                    # области: иначе она появилась бы второй карточкой «Урон».
                    token_updates.append({
                        "token_id": token.id, "hp": token.hp,
                        "hp_max": token.hp_max, "ac": token.ac,
                        "delta": delta, "death_saves": list(token.death_saves),
                        "source": "area",
                    })
                    sheet_payload = self._sheet_payload_for_token_hp_locked(token)
                    if sheet_payload:
                        sheet_updates.append(sheet_payload)
            center = (map_data.pos_x + (origin_col + 0.5) * cw,
                      map_data.pos_y + (origin_row + 0.5) * ch)
            result = {
                "caster_id": caster.id, "caster_name": caster.name,
                "spell_name": spell_name, "shape": shape,
                "size_ft": size_ft, "cells": [list(c) for c in cells],
                "origin_cell": [origin_col, origin_row],
                "aim_cell": [aim_col, aim_row],
                "center": [center[0], center[1]],
                "damage_type": damage_type, "formula": raw.get("formula"),
                "total": total, "dc": dc, "ability": ability,
                "half_on_success": half, "targets": target_rows,
                "spell_key": spell_key, "kind": plan_kind,
                "conditions_only": bool(plan_effects and not formula),
                "pool": bool(plan_kind == "pool"),
                "pool_rolled": pool_rolled,
                "fx": str(spec.get("fx") or ""),
            }

        for update in token_updates:
            self.broadcast(MessageType.UPDATE_TOKEN, update)
        for payload in effect_payloads:
            self.broadcast(MessageType.UPDATE_TOKEN, payload)
        for extra in extra_payloads:
            self.broadcast(MessageType.UPDATE_TOKEN, extra)
        for note in concentration_notes:
            self.push_log(note)
        for sheet_payload in sheet_updates:
            self.broadcast(MessageType.SHEET_STATE, sheet_payload)
        self.broadcast(MessageType.AREA_RESULT, result)
        if plan_kind == "pool":
            if target_rows:
                names = ", ".join(
                    f"{row['name']} ({int(row['hp'])} хит)"
                    for row in target_rows)
                line = (f"✨ {result['caster_name']} — {spell_name}: "
                        f"пул {pool_rolled} хитов (потрачено "
                        f"{int(result['total'])}); попало: {names}")
            else:
                line = (f"✨ {result['caster_name']} — {spell_name}: "
                        f"пул {pool_rolled} хитов — никто не попал")
        elif plan_effects and not formula:
            details = []
            for row in target_rows:
                applied = ", ".join(row.get("applied") or [])
                if applied:
                    details.append(f"{row['name']} — {applied}")
                else:
                    details.append(f"{row['name']} — спасбросок пройден")
            line = (f"✨ {result['caster_name']} — {spell_name}: "
                    f"{len(target_rows)} цел.")
            if details:
                line += "; " + ", ".join(details)
        else:
            line = (f"✨ {result['caster_name']} — {spell_name}: "
                    f"{len(target_rows)} цел.")
            # Числа для сверки: какой бросок урона выпал и что выбросила
            # каждая цель на спасброске. Без этого в журнале был только
            # итог, и за столом нельзя было проверить, откуда он взялся.
            if result.get("formula"):
                line += f"; урон {result['formula']} = {result.get('total')}"
            if target_rows:
                details = []
                for row in target_rows:
                    verdict = "успех" if row.get("save_success") else "провал"
                    save = row.get("save") if isinstance(row.get("save"), dict) else {}
                    if save.get("total") is not None:
                        verdict = (f"спасбросок {save['total']}"
                                   + (f" против {result['dc']}"
                                      if result.get('dc') is not None else "")
                                   + f" — {verdict}")
                    details.append(f"{row['name']} {row['amount']} ({verdict})")
                line += "; " + ", ".join(details)
        self.push_log(line)

    def cast_spell(self, player_id: str, data: dict):
        """Прямое заклинание: снаряды, лечение одной цели или массовое лечение.

        Клиент называет заклинание и цель (для массового — список), но ни
        формулу, ни результат не присылает. Сервер находит запись в листе
        колдуна, проверяет дистанцию и состояния и сам бросает кубы. Поэтому
        Волшебную стрелу нельзя «подкрутить», а Щит цели действительно
        отражает снаряды.
        """
        caster_id = str(data.get('caster_id') or '')
        # Массовое лечение приходит списком; одиночное — одним id. Дубликаты
        # и пустые строки отбрасываем, иначе одна цель получила бы два броска.
        target_ids = []
        for value in (data.get('target_ids') or []):
            value = str(value or '')
            if value and value not in target_ids:
                target_ids.append(value)
        target_id = str(data.get('target_id') or '')
        if target_id and target_id not in target_ids:
            target_ids.insert(0, target_id)
        target_id = target_ids[0] if target_ids else ''
        spell_name = str(data.get('spell_name') or 'Заклинание')[:80]
        with self.lock:
            caster = self.game_state.tokens.get(caster_id)
            target = self.game_state.tokens.get(target_id)
            if caster is None or target is None:
                return
            if caster.map_id != target.map_id:
                self.send_error(player_id, "Цель на другой карте")
                return
            if not self.is_dm(player_id) and caster.owner_id != player_id:
                client = self.clients.get(player_id)
                if client:
                    client.send_message(MessageType.PERMISSION_DENIED,
                                        {'action': 'cast_spell'})
                return
            if not target.visible and not self.is_dm(player_id):
                self.send_error(player_id, "Цель скрыта от игроков")
                return
            caster_sheet = self._sheet_for_token_locked(caster)
            record = self._find_area_spell_locked(caster_sheet, data, spell_name)
            if record is None:
                if not self.is_dm(player_id):
                    self.send_error(
                        player_id,
                        f"«{spell_name}» нет в листе — применение отклонено")
                    return
                self.send_error(player_id, f"«{spell_name}» не найдено")
                return
            spec = spell_rules.plan(record, caster_sheet)
            key = str(spec.get('key') or '')
            # Лечение считает сервер, потому что его формула лежит в листе и
            # включает модификатор характеристики. Раньше здесь был список
            # из трёх ключей, и любое другое лечебное заклинание (например
            # «Массовое лечащее слово» или запись из книги LSS) сервер
            # отклонял: в чате появлялось «применяется другим способом», а
            # хит‑поинты не менялись. Теперь вид действия берём из плана.
            kind = str(spec.get('kind') or '')
            healing = kind == 'heal'
            # Возврат к жизни и развеивание — такая же серверная арифметика:
            # клиент не может сам поднять мёртвого или снять чужой эффект
            # без проверки записи в листе.
            revive = kind == 'revive'
            dispel = kind == 'dispel'
            # Одиночный спасбросок (Священное пламя, Ядовитые брызги,
            # Увядание): бросок спасброска и урон считает сервер — иначе
            # игрок прислал бы себе «цель провалила» и снял любые хиты.
            save_target = kind == 'save_target'
            if key != 'magic_missile' and not (healing or revive or dispel
                                               or save_target):
                self.send_error(
                    player_id, f"«{spell_name}» применяется другим способом")
                return
            if save_target:
                # Круг потраченной ячейки нужен росту урона (Увядание —
                # +1к8 за круг выше четвёртого). Берём его из запроса, но
                # только в разумных границах: запас ячеек ведёт лист.
                try:
                    cast_slot = int(data.get('slot_level') or 0)
                except (TypeError, ValueError):
                    cast_slot = 0
                base_level = int(record.get('level') or 0)
                if cast_slot < base_level or cast_slot > 9:
                    cast_slot = 0
                if cast_slot and cast_slot != base_level:
                    spec = spell_rules.plan(record, caster_sheet,
                                            slot_level=cast_slot)
            conditions = self._conditions_with_hp(caster, caster_sheet)
            dead_caster = combat_rules.death_save_state(caster.death_saves)['dead']
            can_act, why = conditions_rules.can_act(
                conditions, dead=dead_caster, hp=caster.hp)
            if not can_act:
                self.send_error(
                    player_id, f"«{caster.name}» не может действовать: {why}")
                return
            if healing and int(spec.get('targets_max') or 0) > 1:
                # Массовое лечение: своих целей у заклинания несколько, и
                # сервер проверяет каждую сам. Одна ячейка — один каст, а
                # кубы бросаются каждому отдельно, как в правилах.
                targets = self._mass_heal_targets_locked(
                    player_id, caster, target_ids, spec)
                if targets:
                    self._cast_mass_heal_locked(
                        player_id, caster, targets, spec, spell_name)
                return
            range_ft = float(spec.get('range_ft') or 0)
            distance = self._token_distance_ft_locked(caster, target)
            if range_ft and distance > range_ft + 0.1:
                self.send_error(
                    player_id,
                    f"«{target.name}» дальше дистанции «{spell_name}» "
                    f"({range_ft:.0f} фут)")
                return
            target_sheet = self._sheet_for_token_locked(target)
            target_dead = combat_rules.death_save_state(
                target.death_saves)['dead']
            if target.hp is None:
                self.send_error(player_id, "У цели не ведутся хиты")
                return
            if revive:
                # Оживление и Воскрешение — заклинания, у которых цель
                # обязана быть мёртвой: иначе ячейка ушла бы впустую.
                if not target_dead:
                    self.send_error(
                        player_id,
                        f"«{target.name}» жив — воскрешать некого")
                    return
            elif target_dead:
                self.send_error(
                    player_id, f"«{target.name}» мёртв — выберите другую цель")
                return
            if save_target and int(spec.get('chain') or 0):
                # Цепная молния: первая цель уже проверена выше (жива, в
                # дистанции), а разряды от неё считает _cast_chain_spell_locked:
                # список целей из запроса может быть подделан, поэтому
                # количество и радиус (30 футов ОТ ПЕРВОЙ ЦЕЛИ) сверяет сервер.
                chain_ids = [target.id]
                for value in (data.get('target_ids') or ()):
                    text = str(value or '')
                    if text and text not in chain_ids:
                        chain_ids.append(text)
                self._cast_chain_spell_locked(player_id, caster, spec,
                                              spell_name, chain_ids)
                return
            before = max(0, int(target.hp))
            blocked = False
            self_effects_dropped = False
            darts = []
            extra_payloads = []
            concentration_notes = []
            cured = []
            dispelled = []
            revived = False
            # Одиночный спасбросок: бросок спасброска, урон и последствия.
            save_row = None
            # Разбор брошенных костей («12к10: 3, 5, ...»): в чат и журнал
            # уходит рядом с итогом, чтобы стол мог проверить бросок.
            breakdown = ''
            applied = []
            target_effects_changed = False
            # Последствия конкретных правил: пол хитов (Урон), урезанный
            # максимум хитов и пометка о зомби (Перст смерти).
            hp_floor_used = False
            hp_max_reduced = 0
            hp_max_restored = 0
            zombie_note = None
            if healing:
                try:
                    # Формула может быть плоским числом: Полное исцеление
                    # лечит ровно на 70 хитов без кубика.
                    rolled = dice.roll_or_flat(
                        str(spec.get('formula') or '1d4'))
                except dice.DiceError as exc:
                    self.send_error(player_id, f"Формула не посчитана: {exc}")
                    return
                amount = int(rolled.get('total') or 0)
                limit = target.hp_max if target.hp_max else 9999
                target.hp = min(int(limit), before + max(0, amount))
                if target.hp > 0 and target.death_saves:
                    target.death_saves = []
                delta = int(target.hp) - before
                formula = str(rolled.get('formula') or spec.get('formula'))
                breakdown = str(rolled.get('breakdown') or '')
                # Полное исцеление снимает ещё и слепоту, глухоту и болезни.
                cured = self._cure_conditions_locked(target, spec.get('cures'))
                # Вместе с болезнью возвращается максимум хитов, урезанный
                # Поражением (Harm, SRD): «любой эффект, снимающий болезнь».
                if spec.get('disease'):
                    hp_max_restored = self._restore_hp_max_locked(target)
            elif revive:
                # Правило SRD: возвращение к жизни с 1 хитом, отметки
                # смерти снимаются, а с ними — то, что лечится вместе
                # с возвращением (Воскрешение лечит болезни и отравление).
                target.death_saves = []
                try:
                    revive_hp = max(1, int(spec.get('revive_hp') or 1))
                except (TypeError, ValueError):
                    revive_hp = 1
                target.hp = revive_hp
                cured = self._cure_conditions_locked(target, spec.get('cures'))
                delta = int(target.hp) - before
                formula = ''
                revived = True
            elif dispel:
                try:
                    slot_level = int(data.get('slot_level') or 0)
                except (TypeError, ValueError):
                    slot_level = 0
                slot_level = max(int(spec.get('level') or 3),
                                 min(9, slot_level))
                removed_effects = self._dispel_effects_locked(
                    target, slot_level, caster_sheet)
                dispelled = [str(effect.get('label') or 'Эффект')
                             for effect in removed_effects]
                delta = 0
                formula = ''
            elif save_target:
                # Спасбросок бросает сервер — тем же кодом, что и в области:
                # паралич даёт автопровал, Благословение и Порча — свой кубик,
                # преимущества берутся из эффектов, бонус — из листа или
                # статблока цели. Сл тоже серверная: из правила по листу
                # колдуна, а не из запроса.
                ability = token_stat.normalize_ability(
                    spec.get('save') or 'dex') or 'dex'
                try:
                    dc = int(spec.get('dc') or 0)
                except (TypeError, ValueError):
                    dc = 0
                if not 1 <= dc <= 40:
                    dc = int(spell_rules.spell_save_dc(caster_sheet))
                dc = max(1, min(40, dc))
                save = self._save_with_mods_locked(
                    target, target_sheet, ability, dc)
                saved = bool(save.get('success'))
                try:
                    rolled = dice.roll(str(spec.get('formula') or '1d4'))
                except dice.DiceError as exc:
                    self.send_error(player_id, f"Формула не посчитана: {exc}")
                    return
                total = int(rolled.get('total') or 0)
                half = bool(spec.get('half_on_success'))
                if saved:
                    amount = total // 2 if half else 0
                else:
                    amount = total
                protection = token_stat.token_protection(target, target_sheet)
                dealt = combat_rules.apply_resistance(
                    amount, str(spec.get('damage_type') or ''), **protection)
                amount = int(dealt.get('amount') or 0)
                stone = conditions_rules.damage_factor(
                    self._conditions_with_hp(target, target_sheet))
                if stone < 1.0 and int(dealt.get('factor', 1.0) or 1.0) >= 1:
                    amount = int(amount * stone)
                # Поражение (Harm, SRD): «урон не может опустить хиты цели ниже
                # 1». Именно поэтому пол стоит здесь, а не в боекомплекте:
                # максимум хитов падает ровно на полученный (уже урезанный)
                # урон, и по правилам это меньше полного броска.
                floor = max(0, int(spec.get('hp_floor') or 0))
                if floor:
                    limit = max(0, before - floor)
                    if amount > limit:
                        amount = limit
                        hp_floor_used = True
                target.hp = max(0, before - amount)
                delta = int(target.hp) - before
                formula = str(rolled.get('formula')
                              or spec.get('formula') or '')
                breakdown = str(rolled.get('breakdown') or '')
                save_row = {
                    'ability': ability,
                    'ability_label': token_stat.ABILITY_LABELS.get(ability,
                                                                   ability),
                    'success': saved,
                    'd20': int(save.get('d20') or 0),
                    'bonus': int(save.get('bonus') or 0),
                    'total': int(save.get('total') or 0),
                    'dc': dc,
                    'half': bool(saved and half),
                    'target': target.name,
                }
                if amount:
                    dropped, conc_note = self._concentration_check_locked(
                        target, amount, target_sheet)
                    if conc_note:
                        concentration_notes.append(conc_note)
                    for other in dropped:
                        if other.id != target.id:
                            extra_payloads.append({
                                'token_id': other.id,
                                'active_effects': list(
                                    other.active_effects or []),
                            })
                if not saved and spec.get('reduce_hp_max') and amount > 0:
                    # Поражение (Harm, SRD): при провале спасброска максимум
                    # хитов цели уменьшается на полученный урон на 1 час.
                    # Прибавка возвращается, когда чип истёк или его сняло
                    # лечение болезни — за это отвечает тип эффекта hp_max.
                    hp_effect = self._attach_hp_max_effect_locked(
                        target, amount,
                        source=str(spec.get('name') or spell_name),
                        caster_id=caster.id)
                    if hp_effect is not None:
                        hp_max_reduced = int(
                            (hp_effect.get('applied') or {}).get('hp_max') or 0)
                        target_effects_changed = True
                if (spec.get('zombie') and target.hp is not None
                        and int(target.hp) <= 0):
                    # Перст смерти (SRD): убитый этим заклинанием гуманоид
                    # встаёт зомби в начале следующего хода колдуна. Само
                    # появление остаётся ручным действием мастера, а чип
                    # и строка в журнале напоминают, что зомби ждёт.
                    zombie_note = self._zombie_note_locked(target, spec, caster)
                    if zombie_note is not None:
                        target_effects_changed = True
                if not saved and spec.get('effects'):
                    # Последствие провала: у Злой насмешки — помеха на
                    # следующий бросок атаки, который её же и потратит.
                    applied = self._attach_effects_locked(
                        target, spec.get('effects'), caster_id=caster.id,
                        source=str(spec.get('name') or spell_name))
                    target_effects_changed = bool(applied)
            else:
                # Волшебную стрелу отражает именно «Щит» (реакция на 1 раунд),
                # а не любое заклинание со словом «Щит» в подписи: «Щит веры»
                # даёт +2 КД на 10 минут и снаряды не отражает. Поэтому
                # подпись проверяем точно — «Щит (+5 КД)» или «Щит».
                blocked = any(
                    str(effect.get('spell_key') or '') == 'shield'
                    or str(effect.get('label') or '').strip().startswith('Щит (')
                    or str(effect.get('label') or '').strip() == 'Щит'
                    for effect in (target.active_effects or []))
                if blocked:
                    amount = 0
                    delta = 0
                    formula = str(spec.get('formula') or '')
                else:
                    count = max(1, min(8, int(spec.get('darts') or 1)))
                    dart_formula = str(spec.get('dart_formula') or '1d4+1')
                    raw_total = 0
                    for _index in range(count):
                        try:
                            dart = dice.roll(dart_formula)
                        except dice.DiceError as exc:
                            self.send_error(player_id,
                                            f"Снаряд не посчитан: {exc}")
                            return
                        value = int(dart.get('total') or 0)
                        raw_total += value
                        darts.append({'total': value,
                                      'formula': dart.get('formula')})
                    protection = token_stat.token_protection(
                        target, target_sheet)
                    dealt = combat_rules.apply_resistance(
                        raw_total, 'force', **protection)
                    amount = int(dealt.get('amount') or 0)
                    target.hp = max(0, before - amount)
                    delta = int(target.hp) - before
                    formula = str(spec.get('formula') or dart_formula)
                    self_effects_dropped = False
                    if amount:
                        dropped, conc_note = self._concentration_check_locked(
                            target, amount, target_sheet)
                        self_effects_dropped = any(
                            other.id == target.id for other in dropped)
                        if conc_note:
                            concentration_notes.append(conc_note)
                        for other in dropped:
                            if other.id != target.id:
                                extra_payloads.append({
                                    'token_id': other.id,
                                    'active_effects': list(
                                        other.active_effects or []),
                                })
            death_info = None
            if (before <= 0 and delta < 0
                    and not combat_rules.death_save_state(
                        target.death_saves)['dead']):
                target.death_saves, death_after = combat_rules.add_death_save(
                    target.death_saves, False)
                death_info = {
                    'auto_failure': True,
                    'successes': death_after['successes'],
                    'failures': death_after['failures'],
                    'dead': death_after['dead'],
                }
            payload = {
                'caster_id': caster.id, 'caster_name': caster.name,
                'target_id': target.id, 'target_name': target.name,
                'spell_name': str(spec.get('name') or spell_name),
                'spell_key': key, 'fx': str(spec.get('fx') or ''),
                'kind': str(spec.get('kind') or ''),
                'formula': formula, 'breakdown': breakdown,
                'darts': darts, 'blocked': blocked,
                'revived': revived, 'cured': cured, 'dispelled': dispelled,
                'save': save_row, 'applied': applied,
                'damage_type': str(spec.get('damage_type') or ''),
                # Последствия из правил конкретных заклинаний: пол хитов
                # (Урон), урезанный максимум хитов и пометка о зомби
                # (Перст смерти). Карточка и журнал читают их по имени.
                'hp_floor': bool(hp_floor_used),
                'hp_max_reduced': int(hp_max_reduced or 0),
                'hp_max_restored': int(hp_max_restored or 0),
                'zombie': zombie_note,
                'target': {
                    'token_id': target.id, 'name': target.name,
                    'hp': target.hp, 'hp_max': target.hp_max,
                    'delta': delta, 'amount': abs(delta),
                    'hp_floor': bool(hp_floor_used),
                    'hp_max_reduced': int(hp_max_reduced or 0),
                    'hp_max_restored': int(hp_max_restored or 0),
                    'revived': revived, 'cured': cured,
                    'death_saves': list(target.death_saves),
                    'death': death_info,
                },
            }
            token_update = {'token_id': target.id, 'hp': target.hp,
                            'hp_max': target.hp_max, 'ac': target.ac,
                            'delta': delta,
                            'heal': key != 'magic_missile',
                            'heal_style': 'spell', 'source': 'spell',
                            'revived': revived,
                            'fx': str(spec.get('fx') or ''),
                            'spell_name': str(spec.get('name') or ''),
                            'death_saves': list(target.death_saves)}
            if key == 'magic_missile' and self_effects_dropped:
                token_update['active_effects'] = list(
                    target.active_effects or [])
            if target_effects_changed:
                # Чип последствия (помеха от Злой насмешки) должен дойти до
                # всех сразу, а не после следующего обновления токена.
                token_update['active_effects'] = list(
                    target.active_effects or [])
            if hp_max_restored:
                # Строка состояния у всех: лечение болезни вернуло максимум
                # хитов, и это не должно выглядеть случайной правкой.
                token_update['hp_max_restored'] = int(hp_max_restored)
            if extra_payloads or concentration_notes:
                payload['concentration_broken'] = True
            sheet_payload = self._sheet_payload_for_token_hp_locked(target)
            target_name = target.name
        self.broadcast(MessageType.UPDATE_TOKEN, token_update)
        for extra in extra_payloads:
            self.broadcast(MessageType.UPDATE_TOKEN, extra)
        for conc_note in concentration_notes:
            self.push_log(conc_note)
        if sheet_payload:
            self.broadcast(MessageType.SHEET_STATE, sheet_payload)
        self.broadcast(MessageType.SPELL_RESULT, payload)
        if blocked:
            self.push_log(
                f"🛡 {target_name}: Щит отразил «{payload['spell_name']}»")
        elif key == 'magic_missile':
            self.push_log(
                f"✨ {payload['caster_name']} → {target_name}: "
                f"{payload['spell_name']}, {len(darts)} снаряда, "
                f"урон {abs(int(payload['target']['delta']))}")
        elif payload.get('revived'):
            line = (f"✨ {payload['caster_name']} — «{target_name}» "
                    f"воскрешён заклинанием {payload['spell_name']} "
                    f"(хиты {payload['target']['hp']})")
            if payload.get('cured'):
                line += f"; снято: {', '.join(payload['cured'])}"
            self.push_log(line)
        elif str(payload.get('kind') or '') == 'dispel':
            if payload.get('dispelled'):
                self.push_log(
                    f"✨ {payload['caster_name']} — {payload['spell_name']}: "
                    f"с «{target_name}» снято {', '.join(payload['dispelled'])}")
            else:
                self.push_log(
                    f"✨ {payload['caster_name']} — {payload['spell_name']}: "
                    f"на «{target_name}» нечего развеивать")
        elif str(payload.get('kind') or '') == 'save_target':
            # Строка со спасброском видна всем: видно и к20, и бонус, и Сл.
            # Без этого игроки видели бы только число урона и не понимали,
            # почему при успехе урона не было вовсе.
            row = payload.get('save') or {}
            amount = abs(int(payload['target']['delta'] or 0))
            word = 'успех' if row.get('success') else 'провал'
            line = (f"🎲 {payload['caster_name']} → {target_name}: "
                    f"{payload['spell_name']}, спасбросок "
                    f"{row.get('ability_label')} {row.get('total')} "
                    f"(к20 {row.get('d20')} "
                    f"{int(row.get('bonus') or 0):+d}) против "
                    f"Сл {row.get('dc')} — {word}")
            line += f", урон {amount}" if amount else ", урона нет"
            if amount and payload.get('breakdown'):
                line += f" ({payload['breakdown']})"
            if payload.get('hp_floor'):
                # Поражение (Harm): правило прямо ограничивает урон — без этой
                # строки казалось бы, что кубик бросился меньше, чем надо.
                line += "; урон не опускает хиты ниже 1"
            if payload.get('hp_max_reduced'):
                line += (f"; максимум хитов −{int(payload['hp_max_reduced'])} "
                         "на 1 час")
            zombie = payload.get('zombie')
            if isinstance(zombie, dict) and zombie.get('note'):
                line += f"; {zombie['note']}"
            if payload.get('applied'):
                line += f"; {', '.join(payload['applied'])}"
            self.push_log(line)
        else:
            line = (f"✨ {payload['caster_name']} → {target_name}: "
                    f"{payload['spell_name']}, "
                    f"+{int(payload['target']['delta'])} хит")
            if payload.get('breakdown'):
                line += f" ({payload['breakdown']})"
            if payload.get('cured'):
                line += f"; снято: {', '.join(payload['cured'])}"
            self.push_log(line)

    def _mass_heal_targets_locked(self, player_id: str, caster, target_ids,
                                  spec: dict) -> list:
        """Кого лечит массовое заклинание: цели в дистанции и в сфере.

        Клиент присылает только id: количество, дистанцию и радиус сферы
        проверяет сервер. Больше `targets_max` существ поднять нельзя даже
        поддельным запросом, мёртвые и скрытые цели выпадают из списка.
        """
        name = str(spec.get('name') or 'Заклинание')
        limit = max(1, int(spec.get('targets_max') or 1))
        range_ft = float(spec.get('range_ft') or 0)
        radius = float(spec.get('radius_ft') or 0)
        targets = []
        for target_id in target_ids[:limit]:
            token = self.game_state.tokens.get(target_id)
            if token is None or token.map_id != caster.map_id:
                continue
            if not token.visible and not self.is_dm(player_id):
                continue
            if token.hp is None:
                self.send_error(
                    player_id, f"«{token.name}»: у цели не ведутся хиты")
                continue
            if combat_rules.death_save_state(token.death_saves)['dead']:
                self.send_error(
                    player_id,
                    f"«{token.name}» мёртв — лечение не поднимает мёртвых")
                continue
            distance = self._token_distance_ft_locked(caster, token)
            if range_ft and distance > range_ft + 0.1:
                self.send_error(
                    player_id,
                    f"«{token.name}» дальше дистанции «{name}» "
                    f"({range_ft:.0f} фут)")
                continue
            targets.append(token)
        if radius > 1 and len(targets) > 1:
            # Сфера: центром становится цель ближе всех к колдуну — так
            # «Массовое лечение ран» не растягивается на всю карту.
            center = min(targets, key=lambda token: self._token_distance_ft_locked(
                caster, token))
            inside = [center]
            for token in targets:
                if token.id == center.id:
                    continue
                if self._token_distance_ft_locked(center, token) <= radius + 0.1:
                    inside.append(token)
                else:
                    self.send_error(
                        player_id,
                        f"«{token.name}» вне сферы «{name}» "
                        f"({int(radius)} фут)")
            targets = inside
        if not targets:
            self.send_error(player_id, f"«{name}»: нет целей в радиусе")
        return targets

    def _cast_mass_heal_locked(self, player_id: str, caster, targets: list,
                               spec: dict, spell_name: str):
        """Массовое лечение: каждому свой бросок, одна строка в журнале.

        В 5e каждая цель восстанавливает свои хиты отдельным броском, так
        что один общий бросок здесь был бы ошибкой. Отклик (карточка и
        анимация) уходит на каждую цель, а в чат — одна итоговая строка:
        шесть строк на один каст читать было бы невозможно.
        """
        formula = str(spec.get('formula') or '1d4')
        name = str(spec.get('name') or spell_name)
        key = str(spec.get('key') or '')
        fx = str(spec.get('fx') or '')
        cures = spec.get('cures')
        try:
            # Массовое исцеление — не кубик на каждого, а один пул хитов
            # (700), который передаётся существам: каждый лечится до
            # полного, остаток идёт следующим. Так это и работает в SRD.
            pool_left = int(spec.get('pool') or 0)
        except (TypeError, ValueError):
            pool_left = 0
        rows = []
        for target in targets:
            before = max(0, int(target.hp or 0))
            limit = target.hp_max if target.hp_max else 9999
            if pool_left:
                need = max(0, int(limit) - before)
                amount = min(need, pool_left)
                pool_left -= amount
                rolled_formula = (f"{amount} из {int(spec.get('pool') or 0)}"
                                  if amount else '')
            else:
                try:
                    rolled = dice.roll(formula)
                except dice.DiceError as exc:
                    self.send_error(player_id, f"Формула не посчитана: {exc}")
                    return
                amount = max(0, int(rolled.get('total') or 0))
                rolled_formula = str(rolled.get('formula') or formula)
            target.hp = min(int(limit), before + amount)
            if target.hp > 0 and target.death_saves:
                target.death_saves = []
            # Массовое исцеление снимает и болезни со слепотой — об этом
            # напоминало бы молчание: правило есть, значит работает.
            cured = self._cure_conditions_locked(target, cures)
            # Вместе с болезнью возвращается и максимум хитов, урезанный
            # Поражением (Harm, SRD): «любой эффект, снимающий болезнь».
            restored = (self._restore_hp_max_locked(target)
                        if spec.get('disease') else 0)
            delta = int(target.hp) - before
            rows.append({
                'target': target,
                'delta': delta,
                'formula': rolled_formula,
                'cured': cured,
                'hp_max_restored': restored,
            })
        for row in rows:
            target = row['target']
            payload = {
                'caster_id': caster.id, 'caster_name': caster.name,
                'target_id': target.id, 'target_name': target.name,
                'spell_name': name, 'spell_key': key, 'fx': fx,
                'kind': 'heal', 'formula': row['formula'], 'darts': [],
                'blocked': False, 'cured': row.get('cured') or [],
                'hp_max_restored': int(row.get('hp_max_restored') or 0),
                'damage_type': str(spec.get('damage_type') or ''),
                'mass': True,
                'target': {
                    'token_id': target.id, 'name': target.name,
                    'hp': target.hp, 'hp_max': target.hp_max,
                    'delta': row['delta'], 'amount': abs(row['delta']),
                    'cured': row.get('cured') or [],
                    'death_saves': list(target.death_saves),
                    'death': None,
                },
            }
            update = {
                'token_id': target.id, 'hp': target.hp,
                'hp_max': target.hp_max, 'ac': target.ac,
                'delta': row['delta'], 'heal': True,
                'heal_style': 'spell', 'source': 'spell',
                'fx': fx, 'spell_name': name,
                'death_saves': list(target.death_saves),
            }
            if row.get('hp_max_restored'):
                update['hp_max_restored'] = int(row['hp_max_restored'])
            self.broadcast(MessageType.UPDATE_TOKEN, update)
            sheet_payload = self._sheet_payload_for_token_hp_locked(target)
            if sheet_payload:
                self.broadcast(MessageType.SHEET_STATE, sheet_payload)
            self.broadcast(MessageType.SPELL_RESULT, payload)
        details = ', '.join(
            (f"{row['target'].name} +{row['delta']}"
             + (f" (снято: {', '.join(row['cured'])})" if row.get('cured')
                else "")) if row['delta']
            else (f"{row['target'].name} полон"
                  + (f" (снято: {', '.join(row['cured'])})" if row.get('cured')
                     else "")) for row in rows)
        # Одна карточка броска на весь каст: шесть всплывающих окон подряд
        # заслоняли бы стол, а суммы по целям остаются в журнале.
        self.broadcast(MessageType.SPELL_RESULT, {
            'caster_id': caster.id, 'caster_name': caster.name,
            'spell_name': name, 'spell_key': key, 'fx': fx,
            'kind': 'heal', 'mass_summary': True,
            'amount': sum(max(0, row['delta']) for row in rows),
            'targets_count': len(rows),
            'names': [row['target'].name for row in rows],
        })
        self.push_log(
            f"✨ {caster.name} — {name}: {len(rows)} цел.; {details}")

    def _cast_chain_spell_locked(self, player_id: str, caster, spec: dict,
                                 spell_name: str, target_ids: list):
        """Цепная молния: первая цель и разряды от неё (SRD 5.1).

        Правило: «три разряда отходят от цели к трём другим существам, и
        каждое должно быть в 30 футах от ПЕРВОЙ цели; существо может быть
        целью только одного разряда». Поэтому радиус отмеряется от первой
        цели, а не от колдуна, и в одного и того же разряд не идёт дважды.

        Урон брошен один раз на всю цепочку — это то же число, что в
        правиле («the target takes 10d8»), иначе у каждой цели был бы свой
        урон от одного разряда молнии. Спасбросок Ловкости бросает
        КАЖДАЯ цель своя: успех — половина, провал — полный урон (половины
        у заклинания нет только на крите дополнительных костей). Количество
        разрядов (3 + круг ячейки выше шестого) и радиус сервер проверяет
        сам: подделанный список целей его не обманет.
        """
        name = str(spec.get('name') or spell_name)
        key = str(spec.get('key') or '')
        fx = str(spec.get('fx') or '')
        bolts_max = max(1, int(spec.get('chain') or 3))
        radius = float(spec.get('chain_radius_ft') or 30)
        half = bool(spec.get('half_on_success'))
        damage_type = str(spec.get('damage_type') or '')
        ability = token_stat.normalize_ability(spec.get('save') or 'dex') \
            or 'dex'
        try:
            dc = int(spec.get('dc') or 0)
        except (TypeError, ValueError):
            dc = 0
        if not 1 <= dc <= 40:
            dc = int(spell_rules.spell_save_dc(
                self._sheet_for_token_locked(caster) or {}))
        dc = max(1, min(40, dc))
        primary = self.game_state.tokens.get(
            str(target_ids[0] if target_ids else ''))
        if primary is None:
            self.send_error(player_id, 'Цель заклинания не найдена')
            return
        targets = [primary]
        for value in list(target_ids[1:]):
            if len(targets) - 1 >= bolts_max:
                break
            token = self.game_state.tokens.get(str(value or ''))
            if token is None or token.id == primary.id:
                continue
            if any(row.id == token.id for row in targets):
                continue
            if token.map_id != primary.map_id or token.hp is None:
                continue
            if not token.visible and not self.is_dm(player_id):
                continue
            if combat_rules.death_save_state(token.death_saves)['dead']:
                continue
            distance = self._token_distance_ft_locked(primary, token)
            if distance > radius + 0.1:
                # Не молчание: мастер должен видеть, что разряд не пошёл.
                self.send_error(
                    player_id,
                    f"«{token.name}» дальше {radius:.0f} футов от первой "
                    f"цели — разряд не пошёл")
                continue
            targets.append(token)
        try:
            rolled = dice.roll(str(spec.get('formula') or '10d8'))
        except dice.DiceError as exc:
            self.send_error(player_id, f"Формула не посчитана: {exc}")
            return
        total = int(rolled.get('total') or 0)
        formula = str(rolled.get('formula') or spec.get('formula') or '')
        rows = []
        extra_updates = []
        concentration_notes = []
        for index, target in enumerate(targets):
            before = max(0, int(target.hp or 0))
            target_sheet = self._sheet_for_token_locked(target)
            save = self._save_with_mods_locked(target, target_sheet, ability, dc)
            saved = bool(save.get('success'))
            amount = (total // 2 if half else 0) if saved else total
            protection = token_stat.token_protection(target, target_sheet)
            dealt = combat_rules.apply_resistance(amount, damage_type,
                                                  **protection)
            amount = int(dealt.get('amount') or 0)
            stone = conditions_rules.damage_factor(
                self._conditions_with_hp(target, target_sheet))
            if stone < 1.0 and int(dealt.get('factor', 1.0) or 1.0) >= 1:
                amount = int(amount * stone)
            target.hp = max(0, before - amount)
            delta = int(target.hp) - before
            death = None
            if amount:
                dropped, conc_note = self._concentration_check_locked(
                    target, amount, target_sheet)
                if conc_note:
                    concentration_notes.append(conc_note)
                for other in dropped:
                    if other.id != target.id:
                        extra_updates.append({
                            'token_id': other.id,
                            'active_effects': list(other.active_effects or []),
                        })
            if (before <= 0 and delta < 0
                    and not combat_rules.death_save_state(
                        target.death_saves)['dead']):
                target.death_saves, death_after = combat_rules.add_death_save(
                    target.death_saves, False)
                death = {
                    'auto_failure': True,
                    'successes': death_after['successes'],
                    'failures': death_after['failures'],
                    'dead': death_after['dead'],
                }
            rows.append({
                'target': target,
                'save': save,
                'saved': saved,
                'delta': delta,
                'amount': amount,
                'death': death,
                'index': index,
            })
        # Отклик — на каждую цель отдельно: у каждой свой спасбросок и своё
        # число урона, и карточка без цели читалась бы неверно. В журнал
        # идёт столько же строк: их видно и мастеру, и игрокам, и по ним
        # потом сверяют броски.
        for row in rows:
            target = row['target']
            payload = {
                'caster_id': caster.id, 'caster_name': caster.name,
                'target_id': target.id, 'target_name': target.name,
                'spell_name': name, 'spell_key': key, 'fx': fx,
                'kind': 'save_target', 'formula': formula, 'darts': [],
                'blocked': False, 'cured': [], 'dispelled': [],
                'revived': False, 'applied': [],
                'damage_type': damage_type,
                'hp_floor': False, 'hp_max_reduced': 0,
                'hp_max_restored': 0, 'zombie': None,
                # Метаданные цепочки: по ним карточка подписывает разряд,
                # а холст рисует дуги от первой цели. `primary_id` нужен
                # именно для дуг: по правилу разряды отходят от ПЕРВОЙ
                # цели, а не от предыдущей в списке.
                'chain': {
                    'index': int(row['index']) + 1,
                    'count': len(rows),
                    'primary': row['index'] == 0,
                    'primary_id': primary.id,
                    'bolts': bolts_max,
                    'radius_ft': int(radius),
                    'from_id': (rows[row['index'] - 1]['target'].id
                                if row['index'] else ''),
                },
                'save': {
                    'ability': ability,
                    'ability_label': token_stat.ABILITY_LABELS.get(ability,
                                                                   ability),
                    'success': bool(row['saved']),
                    'd20': int(row['save'].get('d20') or 0),
                    'bonus': int(row['save'].get('bonus') or 0),
                    'total': int(row['save'].get('total') or 0),
                    'dc': dc,
                    'half': bool(row['saved'] and half),
                    'target': target.name,
                },
                'target': {
                    'token_id': target.id, 'name': target.name,
                    'hp': target.hp, 'hp_max': target.hp_max,
                    'delta': row['delta'], 'amount': row['amount'],
                    'hp_floor': False, 'hp_max_reduced': 0,
                    'revived': False, 'cured': [],
                    'death_saves': list(target.death_saves),
                    'death': row['death'],
                },
            }
            self.broadcast(MessageType.UPDATE_TOKEN, {
                'token_id': target.id, 'hp': target.hp,
                'hp_max': target.hp_max, 'ac': target.ac,
                'delta': row['delta'], 'heal': False,
                'fx': fx, 'spell_name': name,
                'death_saves': list(target.death_saves),
            })
            sheet_payload = self._sheet_payload_for_token_hp_locked(target)
            if sheet_payload:
                self.broadcast(MessageType.SHEET_STATE, sheet_payload)
            self.broadcast(MessageType.SPELL_RESULT, payload)
            word = 'успех' if row['saved'] else 'провал'
            line = (f"⚡ {caster.name} → {target.name}: {name}"
                    + ("" if row['index'] == 0
                       else f" (разряд {row['index'] + 1} из {len(rows)})")
                    + f", спасбросок "
                    f"{token_stat.ABILITY_LABELS.get(ability, ability)} "
                    f"{row['save'].get('total')} "
                    f"(к20 {row['save'].get('d20')} "
                    f"{int(row['save'].get('bonus') or 0):+d}) против "
                    f"Сл {dc} — {word}")
            line += (f", урон {row['amount']}" if row['amount']
                     else ", урона нет")
            self.push_log(line)
        for extra in extra_updates:
            self.broadcast(MessageType.UPDATE_TOKEN, extra)
        for conc_note in concentration_notes:
            self.push_log(conc_note)

    def apply_damage(self, player_id: str, data: dict):
        """Нанести урон или вылечить токен.

        Права: мастер — любому; игрок — урон только своему токену, но
        лечить может и союзника (иначе «Лечащее слово» и зелье, переданное
        другу, отклонялись бы сервером). Хиты зажимаем в диапазон и не
        даём уйти ниже −999, чтобы одно опечаткой не сломало бой.
        """
        token_id = data.get('token_id')
        amount = data.get('amount', 0)
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return
        healing = bool(data.get('heal'))
        heal_style = 'spell' if data.get('heal_style') == 'spell' else 'item'
        feature = str(data.get('feature') or '').strip()
        with self.lock:
            token = self.game_state.tokens.get(token_id)
            if not token:
                return
            if (not self.is_dm(player_id)
                    and token.owner_id != player_id and not healing):
                client = self.clients.get(player_id)
                if client:
                    client.send_message(MessageType.PERMISSION_DENIED,
                                        {'action': 'apply_damage'})
                return
            pool_payload = None
            cured = []
            if healing and feature == 'lay_on_hands':
                # Наложение рук: запас 5 × уровень паладина живёт в листе,
                # а не в клиенте. Иначе кнопка лечила бы бесконечно, а
                # после перезапуска запас забывался бы.
                source_id = str(data.get('source_token_id') or token_id)
                source = self.game_state.tokens.get(source_id)
                source_sheet = (self._sheet_for_token_locked(source)
                                if source is not None else None)
                if not isinstance(source_sheet, dict):
                    self.send_error(player_id,
                                    'Наложение рук: у паладина нет листа')
                    return
                if not class_features.has(source_sheet, 'lay_on_hands'):
                    self.send_error(
                        player_id,
                        'Наложение рук: этой особенности нет в листе')
                    return
                if (source is not None and source.id != token.id
                        and self._token_distance_ft_locked(source, token)
                        > class_features.LAY_ON_HANDS_REACH_FT + 0.1):
                    self.send_error(
                        player_id,
                        'Наложение рук: цель дальше 5 футов (нужно касание)')
                    return
                left = class_features.lay_on_hands_left(source_sheet)
                if left <= 0:
                    self.send_error(
                        player_id,
                        'Наложение рук: запас лечения исчерпан до долгого отдыха')
                    return
                cure = bool(data.get('cure'))
                amount = max(0, min(abs(amount), left))
                if cure:
                    amount = min(amount, max(0, left -
                                              class_features.LAY_ON_HANDS_CURE_COST))
                cost = class_features.lay_on_hands_cost(amount, cure)
                if cost <= 0:
                    self.send_error(
                        player_id,
                        'Наложение рук: укажите, сколько хитов передать')
                    return
                if not class_features.lay_on_hands_spend(source_sheet, cost):
                    self.send_error(player_id,
                                    'Наложение рук: в запасе не хватает хитов')
                    return
                if cure:
                    # Наложение рук снимает один яд ИЛИ одну болезнь.
                    for state in ('Отравлен', 'Болезнь'):
                        canonical = conditions_rules.canonical(state)
                        if canonical and canonical in (token.conditions or []):
                            token.conditions = [name for name
                                                in token.conditions
                                                if name != canonical]
                            cured = [canonical]
                            break
                by_token = getattr(self.game_state, 'token_sheets', None)
                if isinstance(by_token, dict) and source is not None:
                    by_token[source.id] = source_sheet
                if source is not None and source.owner_id:
                    self.game_state.sheets[source.owner_id] = source_sheet
                pool_payload = {
                    'player_id': source.owner_id if source else '',
                    'sheet': source_sheet,
                    'token_id': source.id if source else '',
                }
                heal_style = 'lay_on_hands'
            if token.hp is None:
                token.hp = token.hp_max or 0
            before = int(token.hp)
            temp_absorbed = 0
            if healing:
                limit = token.hp_max if token.hp_max else 9999
                token.hp = min(int(limit), before + abs(amount))
                # Лечение выше нуля снимает спасброски от смерти.
                if token.hp > 0 and token.death_saves:
                    token.death_saves = []
            else:
                # Временные хиты (зелье героизма) — отдельный буфер: урон
                # сначала съедает их и только остаток уходит в основные хиты.
                remaining = abs(amount)
                try:
                    temp = max(0, int(token.hp_temp or 0))
                except (TypeError, ValueError):
                    temp = 0
                if temp:
                    temp_absorbed = min(temp, remaining)
                    token.hp_temp = temp - temp_absorbed
                    remaining -= temp_absorbed
                # Хиты не уходят в минус: на нуле начинаются спасброски.
                token.hp = max(0, before - remaining)
            payload = {'token_id': token.id, 'hp': token.hp,
                       'hp_max': token.hp_max, 'hp_temp': token.hp_temp,
                       'ac': token.ac,
                       'delta': int(token.hp) - before,
                       'temp_absorbed': temp_absorbed,
                       'heal': healing, 'heal_style': heal_style,
                       'death_saves': list(token.death_saves)}
            if cured:
                payload['conditions'] = list(token.conditions or [])
                payload['conditions_removed'] = list(cured)
            if feature:
                payload['feature'] = feature
            name, hp = token.name, token.hp
            hp_lost = max(0, before - int(token.hp))
            dropped, concentration_note = self._concentration_check_locked(
                token, hp_lost)
            extra_payloads = []
            if dropped:
                payload['active_effects'] = list(token.active_effects or [])
                extra_payloads = [
                    {'token_id': other.id,
                     'active_effects': list(other.active_effects or [])}
                    for other in dropped if other.id != token.id]
            # Снятое отравление должно исчезнуть и из листа: правила
            # объединяют состояния токена и листа, и старая запись вернула
            # бы яд на следующем обновлении карточки.
            if cured:
                cured_sheet = self._sheet_for_token_locked(token)
                if isinstance(cured_sheet, dict):
                    block = cured_sheet.get('vtt')
                    if isinstance(block, dict):
                        block['conditions'] = [
                            {'name': n, 'rounds': None}
                            for n in (token.conditions or [])]
            # Урон с карты должен быть виден в листе игрока: без этого приходилось
            # держать два разных счёта хитов в голове. Синхронизируем только если
            # у токена есть лист: у монстров его нет и не нужно.
            sheet_payload = self._sheet_payload_for_token_hp_locked(token)
        self.broadcast(MessageType.UPDATE_TOKEN, payload)
        for extra in extra_payloads:
            self.broadcast(MessageType.UPDATE_TOKEN, extra)
        if concentration_note:
            self.push_log(concentration_note)
        if sheet_payload:
            self.broadcast(MessageType.SHEET_STATE, sheet_payload)
        if pool_payload:
            # Лист паладина изменился: запас лечения и его кнопка на листе
            # должны показать остаток сразу.
            self.broadcast(MessageType.SHEET_STATE, pool_payload)
        if feature == 'lay_on_hands':
            self.push_log(
                f"✨ {name}: наложение рук +{abs(amount or 0)} хит"
                + (" (снято состояние)" if cured else ""))
        if not healing and hp is not None and hp <= 0:
            self.push_log(f"☠ {name} повержен (хиты {hp})")
        elif not healing and payload.get('temp_absorbed'):
            self.push_log(
                f"{name}: временные хиты поглотили "
                f"{payload['temp_absorbed']} урона (осталось "
                f"{payload.get('hp_temp', 0)})"
                + (f"; хиты {hp}" if hp_lost else ""))
        elif feature != 'lay_on_hands':
            # У наложения рук своя строка выше: с запасом и снятым ядом.
            shown = abs(amount) if healing else hp_lost
            self.push_log(
                f"{name}: {'+' if healing else '−'}{shown} хит "
                f"({hp}" + ("/" + str(payload['hp_max']) if payload['hp_max'] else "") + ")")

    # ------------------------------------------------------------------
    # Длительные эффекты предметов
    #
    # Зелья сопротивления, невидимости и героизма живут на токене, а не в
    # клиенте: иначе после перезапуска стол забывал бы, что персонаж
    # невидим или устойчив к огню. Сервер сам снимает истёкшие эффекты на
    # смене раунда и возвращает снятые флаги (сопротивление, состояние).
    # ------------------------------------------------------------------
    EFFECT_TYPES = ('resist', 'condition', 'temp_hp', 'speed', 'note',
                    'ac', 'bless', 'bane', 'armor', 'save_adv', 'mark',
                    'rage', 'smite', 'zephyr', 'favor', 'attack_disadv',
                    'hp_max')
    EFFECT_MAX_ROUNDS = 6000          # 10 часов боя — дальше уже ошибка

    @staticmethod
    def _effect_label(value, fallback='Эффект') -> str:
        text = str(value or '').strip() or fallback
        return text[:80]

    def _build_effect_locked(self, spec: dict, source: str = '',
                             caster_id: str = '',
                             cast_id: str = '') -> Optional[dict]:
        """Проверить присланный клиентом эффект и привести его к словарю.

        Клиент не может наложить произвольное поле: разрешены только
        известные типы, длительность зажата, состояние обязано быть из
        справочника, а тип урона — из dnd5e. Словарь `note` механически
        ничего не меняет, но показывается в карточке как обещанный эффект.
        """
        if not isinstance(spec, dict):
            return None
        kind = str(spec.get('type') or '').strip().casefold()
        if kind not in self.EFFECT_TYPES:
            return None
        try:
            rounds = int(spec.get('rounds') or 1)
        except (TypeError, ValueError):
            rounds = 1
        rounds = max(1, min(self.EFFECT_MAX_ROUNDS, rounds))
        label = self._effect_label(spec.get('label'), 'Эффект')
        duration = str(spec.get('duration') or '').strip()[:40]
        effect = {
            'id': generate_id(),
            'type': kind,
            'label': label,
            'rounds_left': rounds,
            'source': str(source or spec.get('source') or '').strip()[:80],
            'applied': {},
        }
        if duration:
            effect['duration'] = duration
        # Ключ заклинания и отклик нужны клиенту для анимации и правила
        # «Щит отражает Волшебную стрелу»; произвольную строку сюда не
        # пускаем — только короткий служебный ключ.
        spell_key = str(spec.get('spell_key') or '').strip()[:40]
        if spell_key:
            effect['spell_key'] = spell_key
        # Круг заклинания-источника нужен развеиванию магии: эффект круга
        # выше потраченной ячейки снимается только проверкой.
        try:
            spell_level = int(spec.get('spell_level') or spec.get('level') or 0)
        except (TypeError, ValueError):
            spell_level = 0
        if spell_level > 0:
            effect['spell_level'] = max(1, min(9, spell_level))
        fx = str(spec.get('fx') or '').strip().casefold()
        # Свои отклики у доведённых заклинаний, а у остальных — школьная
        # аура `spell_<школа/стихия>`: без этого новое заклинание молчало бы.
        if (fx in ('shield', 'bless', 'healing_word', 'cure_wounds',
                   'magic_missile', 'haste', 'mage_armor',
                   'hunters_mark', 'divine_smite', 'rage')
                or re.fullmatch(r'spell_[a-z]{3,20}', fx)):
            effect['fx'] = fx
        if spec.get('concentration'):
            effect['concentration'] = True
            if caster_id:
                effect['caster_id'] = caster_id
            if cast_id:
                effect['cast_id'] = cast_id
        if kind == 'resist':
            damage_type = combat_rules.normalize_damage_type(
                spec.get('damage_type') or label)
            if not damage_type:
                return None
            effect['damage_type'] = damage_type
            if not label or label == 'Эффект':
                effect['label'] = ('Сопротивление: '
                                   + combat_rules.damage_type_label(damage_type))
        elif kind == 'condition':
            condition = conditions_rules.canonical(spec.get('condition')
                                                    or label)
            if not condition:
                return None
            effect['condition'] = condition
        elif kind == 'temp_hp':
            try:
                amount = int(spec.get('amount') or 0)
            except (TypeError, ValueError):
                amount = 0
            if amount <= 0:
                return None
            effect['amount'] = max(1, min(999, amount))
        elif kind == 'speed':
            effect['rounds_movement'] = rounds
            # Плоская прибавка скорости (Удар Зефира) — в футах.
            try:
                bonus = int(spec.get('bonus_ft') or 0)
            except (TypeError, ValueError):
                bonus = 0
            if bonus:
                effect['bonus_ft'] = max(5, min(120, bonus))
        elif kind == 'ac':
            try:
                amount = int(spec.get('amount') or 0)
            except (TypeError, ValueError):
                amount = 0
            if amount <= 0 or amount > 10:
                return None
            effect['amount'] = amount
            if not label or label == 'Эффект':
                effect['label'] = f'КД +{amount}'
            # Не 20 раундов: Щит веры и Ускорение длятся минутами, и
            # обрезать их до двух минут нельзя.
            effect['rounds_left'] = max(
                1, min(self.EFFECT_MAX_ROUNDS, rounds))
        elif kind == 'bless':
            # Благословение в SRD всегда 1d4; другой кубик сюда не пускаем,
            # чтобы клиент не мог выдать себе +1d12 к атакам.
            if str(spec.get('dice') or '1d4').strip().casefold() != '1d4':
                return None
            effect['dice'] = '1d4'
            effect['rounds_left'] = max(1, min(self.EFFECT_MAX_ROUNDS, rounds))
        elif kind == 'bane':
            # Порча вычитает именно 1к4 из атак и спасбросков (SRD).
            # Другой кубик сюда не пускаем: иначе клиент прислал бы −1к12.
            if str(spec.get('dice') or '1d4').strip().casefold() != '1d4':
                return None
            effect['dice'] = '1d4'
            effect['rounds_left'] = max(1, min(self.EFFECT_MAX_ROUNDS, rounds))
        elif kind == 'armor':
            # «Доспехи мага»: база КД 13 + модификатор Ловкости. Базу
            # зажимаем 10…20 — клиент не должен прислать КД 30.
            try:
                base = int(spec.get('base') or 13)
            except (TypeError, ValueError):
                base = 13
            if base < 10 or base > 20:
                return None
            ability = token_stat.normalize_ability(spec.get('ability') or 'dex')
            effect['base'] = base
            effect['ability'] = ability or 'dex'
            if not label or label == 'Эффект':
                effect['label'] = f'Доспехи мага ({base} + Ловкость)'
        elif kind == 'save_adv':
            # Преимущество на спасбросок от эффекта (Ускорение — Ловкость).
            ability = token_stat.normalize_ability(spec.get('ability') or '')
            if not ability:
                return None
            effect['ability'] = ability
            if not label or label == 'Эффект':
                effect['label'] = ('Преимущество на спасбросок '
                                   + token_stat.ABILITY_LABELS.get(ability,
                                                                   ability))
        elif kind == 'mark':
            # Метка охотника и Сглаз: кубик урона и автор метки. Метка без
            # колдуна бессмысленна (некому добавлять 1к6), а кубик кроме
            # 1к6 сервер не принимает: иначе клиент выдал бы себе 1к12.
            if str(spec.get('die') or '1d6').strip().casefold() != '1d6':
                return None
            if not caster_id:
                return None
            effect['die'] = '1d6'
            effect['caster_id'] = caster_id
            effect['concentration'] = True
            # Ключ метки остаётся тем, что пришёл от правила: по нему
            # клиент решает, можно ли переносить метку бонусным действием
            # (это правило Метки охотника, у Сглаза такого нет).
            mark_key = str(spec.get('spell_key') or '').strip().casefold()
            effect['spell_key'] = (mark_key
                                   if mark_key in ('hunters_mark', 'hex')
                                   else 'hunters_mark')
            # Тип урона: у Сглаза он некротический, у Метки охотника —
            # тип самой атаки, поэтому пустое поле остаётся пустым.
            mark_type = combat_rules.normalize_damage_type(
                spec.get('damage_type'))
            if mark_type:
                effect['damage_type'] = mark_type
            if not label or label == 'Эффект':
                effect['label'] = 'Метка охотника'
        elif kind == 'rage':
            # Ярость: бонус к урону и сопротивление трём физическим типам.
            # Бонус приходит уже пересчитанным по листу (2/3/4); клиентское
            # число проверяем диапазоном, а длительность не даём растянуть
            # дольше минуты.
            try:
                bonus = int(spec.get('bonus') or 0)
            except (TypeError, ValueError):
                bonus = 0
            if bonus < 2 or bonus > 4:
                return None
            effect['bonus'] = bonus
            effect['rounds_left'] = max(1, min(10, rounds))
            effect['fx'] = 'rage'
            if not label or label == 'Эффект':
                effect['label'] = f'Ярость (+{bonus} к урону)'
        elif kind == 'attack_disadv':
            # Помеха на броски атаки (Потрясающий удар, Злая насмешка).
            # Кубика нет — это флаг, который сервер читает при броске
            # атаки. `once` — помеха только на следующий бросок: его она и
            # тратит (так работает Злая насмешка), а без флага держится
            # весь срок (так держится Потрясающий удар).
            if not caster_id:
                return None
            effect['caster_id'] = caster_id
            effect['spell_key'] = (str(spec.get('spell_key') or '').strip()[:40]
                                   or 'staggering_smite')
            effect['rounds_left'] = max(1, min(3, rounds))
            if spec.get('once'):
                effect['once'] = True
            if not label or label == 'Эффект':
                effect['label'] = 'Помеха на атаки и проверки'
        elif kind == 'hp_max':
            # Поражение (Harm, SRD): максимум хитов цели уменьшен полученным
            # некротическим уроном на час. Число приходит из правила, но
            # проверяем диапазон: ноль бессмыслен, а больше 999 хитов в
            # столе не бывает. Автора помним, чтобы подпись чипа не врала.
            try:
                amount = int(spec.get('amount') or 0)
            except (TypeError, ValueError):
                amount = 0
            if amount <= 0 or amount > 999 or not caster_id:
                return None
            effect['amount'] = amount
            effect['caster_id'] = caster_id
            effect['rounds_left'] = max(1, min(self.EFFECT_MAX_ROUNDS, rounds))
            if not duration:
                effect['duration'] = '1 час'
            if not label or label == 'Эффект':
                effect['label'] = f'Поражение: максимум хитов −{amount}'
        elif kind == 'favor':
            # Божественное благоволение: кубик фиксирован правилом (1к4
            # излучением), клиентское поле его не заменяет.
            if not caster_id:
                return None
            effect['dice'] = '1d4'
            effect['damage_type'] = 'radiant'
            effect['spell_key'] = 'divine_favor'
            effect['caster_id'] = caster_id
            effect['concentration'] = True
            effect['fx'] = 'spell_radiant'
            if not label or label == 'Эффект':
                effect['label'] = 'Божественное благоволение: +1к4 излучением'
        elif kind == 'zephyr':
            # Удар Зефира: кубик и прибавка скорости — правило заклинания,
            # а не поле из сети. Колдун обязателен: эффект держится на нём.
            if not caster_id:
                return None
            effect['dice'] = '1d8'
            effect['damage_type'] = 'force'
            effect['bonus_ft'] = 30
            effect['spell_key'] = 'zephyr_strike'
            effect['caster_id'] = caster_id
            effect['concentration'] = True
            effect['fx'] = 'spell_transmutation'
            if not label or label == 'Эффект':
                effect['label'] = ('Удар Зефира: +1к8 силового и +30 футов '
                                   'скорости')
        elif kind == 'smite':
            # Кара-заклинание (Громовая, Злобная, Пылающая): читается на
            # себя и ждёт первого попадания рукопашным оружием. Кубики,
            # тип урона и последствие берём из таблицы `spells.SMITE_SPELLS`
            # по ключу, а не из присланного поля: иначе клиент выдал бы
            # себе 1к12 вместо 2к6. Спасбросок цели на попадании — это
            # правило удара, поэтому в эффекте он лежит как `rider_save`,
            # а не как `save`: иначе сервер потребовал бы спасбросок при
            # самом чтении кары.
            spell_key = str(spec.get('spell_key') or '').strip().casefold()
            rules = spell_rules.SMITE_SPELLS.get(spell_key)
            if not rules or not caster_id:
                return None
            slot_level = int(effect.get('spell_level') or 1)
            dice = spell_rules.smite_dice(spell_key, slot_level)
            rider_dice = spell_rules.smite_rider_dice(spell_key, slot_level)
            damage_type = str(rules.get('damage_type') or '')
            if not dice and not rider_dice:
                # У заклинания нет ни урона на попадании, ни последствия:
                # накладывать нечего.
                return None
            damage_label = (combat_rules.damage_type_label(damage_type)
                            .casefold() if damage_type else '')
            name = label.split(':')[0].strip() if ':' in label else label
            effect['dice'] = dice
            effect['damage_type'] = damage_type
            effect['spell_key'] = spell_key
            effect['mode'] = str(rules.get('mode') or 'melee')
            effect['rider'] = str(rules.get('rider') or '')
            effect['rider_save'] = str(rules.get('save') or '')
            # В подписи последствия подставляем те же кубики, что бросим:
            # «{extra}» в правиле — это 1к6, 2к6… по кругу ячейки.
            note = str(rules.get('note') or '')
            if '{extra}' in note:
                note = note.replace('{extra}', rider_dice or dice or '1к6')
            effect['rider_note'] = note[:200]
            effect['rider_dice'] = rider_dice
            effect['rider_damage_type'] = str(
                rules.get('rider_damage_type') or damage_type)
            effect['radius_ft'] = max(0, min(30, int(rules.get('radius_ft') or 0)))
            effect['caster_id'] = caster_id
            effect['concentration'] = True
            if dice:
                effect['label'] = (f"{name or 'Кара'}: +{dice} урона "
                                   f"({damage_label}) следующим ударом")[:80]
            else:
                # У Опутывающего удара и Града шипов урона на попадании нет —
                # обещать кубики нельзя, иначе подпись расходилась бы с броском.
                effect['label'] = (f"{name or 'Удар'}: "
                                   f"{str(rules.get('short') or 'ждёт попадания')}"
                                   )[:80]
        else:
            note = str(spec.get('note') or '').strip()
            if note:
                effect['note'] = note[:200]
        return effect

    def _pending_favor_locked(self, attacker):
        """Действует ли на атакующего Божественное благоволение (SRD).

        В отличие от кар, эффект не сгорает: кубик добавляется к каждому
        попаданию оружием до конца концентрации. Атака заклинанием — не
        удар оружием, её благоволение не усиливает.
        """
        if attacker is None:
            return None
        for effect in getattr(attacker, 'active_effects', None) or []:
            if isinstance(effect, dict) and effect.get('type') == 'favor':
                return effect
        return None

    def _pending_zephyr_locked(self, attacker):
        """Висит ли на атакующем неиспользованный Удар Зефира (XGE).

        Заклинание даёт преимущество на одну атаку оружием и +1к8 силового
        урона, а после удара — +30 футов скорости до конца хода. Кубик и
        прибавка зашиты в эффект, поэтому из сети их подменить нельзя.
        """
        if attacker is None:
            return None
        for effect in getattr(attacker, 'active_effects', None) or []:
            if isinstance(effect, dict) and effect.get('type') == 'zephyr':
                return effect
        return None

    def _pending_smite_locked(self, attacker, ranged: bool = False):
        """Заклинание «кубики ждут попадания», подходящее этой атаке.

        Режим берётся из правила: кары паладина требуют рукопашной атаки,
        Град шипов — дальнобойной, Опутывающему удару подходит любая
        атака оружием. Поэтому дальний выстрел не тратит кару, а удар
        в упор не тратит Град шипов.
        """
        for effect in getattr(attacker, 'active_effects', None) or []:
            if not isinstance(effect, dict):
                continue
            if effect.get('type') != 'smite':
                continue
            mode = str(effect.get('mode') or 'melee').casefold()
            if mode == 'ranged' and not ranged:
                continue
            if mode == 'melee' and ranged:
                continue
            return effect
        return None

    def _banish_after_hit_locked(self, target, effect, hp_after,
                                 source: str = '') -> dict:
        """Изгоняющая кара: изгнана ли цель после удара (PHB).

        Спасброска у заклинания нет: если после удара у цели осталось не
        больше 50 хитов, её изгоняют на чужой план — она недееспособна и не
        может вернуться, пока держится кара. Само перемещение — ручное
        действие мастера, поэтому стол вешает чип и пишет об этом в журнал.
        """
        rules = spell_rules.SMITE_SPELLS.get(
            str(effect.get('spell_key') or '')) or {}
        limit = int(rules.get('banish_hp') or 50)
        try:
            hp = int(hp_after)
        except (TypeError, ValueError):
            return {'banished': False, 'limit': limit, 'hp': None}
        if hp > limit:
            return {'banished': False, 'limit': limit, 'hp': hp}
        name = str(effect.get('label') or 'Изгоняющая кара').split(':')[0].strip()
        self._attach_effects_locked(target, [{
            'type': 'note',
            'note': (f'изгнана с плана: хитов {hp} (не больше {limit}) — '
                     'недееспособна и не может вернуться, пока держится кара'),
            'label': f'{name}: изгнана (≤{limit} хитов)',
            'rounds': int(effect.get('rounds_left') or 10),
            'duration': str(effect.get('duration') or '1 минута'),
            'concentration': True,
            'spell_key': str(effect.get('spell_key') or ''),
            'fx': str(effect.get('fx') or ''),
        }], caster_id=str(effect.get('caster_id') or ''), source=source)
        return {'banished': True, 'limit': limit, 'hp': hp}

    def _attack_disadvantage_effect_locked(self, token):
        """Эффект «помеха на атаки» на токене или None.

        Это не состояние SRD, у него нет своего названия в справочнике,
        поэтому помеха живёт отдельным эффектом `attack_disadv`. Возвращаем
        сам эффект, а не флаг: у Злой насмешки он одноразовый (`once`) —
        бросок атаки его тратит, и сервер должен знать, что снимать.
        """
        if token is None:
            return None
        for effect in getattr(token, 'active_effects', None) or []:
            if (isinstance(effect, dict)
                    and effect.get('type') == 'attack_disadv'):
                return effect
        return None

    def _attack_disadvantage_locked(self, token) -> bool:
        """Есть ли на токене эффект «помеха на атаки»."""
        return self._attack_disadvantage_effect_locked(token) is not None

    def _target_is_large_locked(self, token) -> bool:
        """Большой ли цели токен: у Опутывающего удара такая цель получает
        преимущество на спасбросок Силы (5e).

        Токен размером 2×2 клетки — это Большой (10 на 10 футов), больший —
        ещё крупнее. Если размер записан словом в статблоке, тоже считаем.
        """
        try:
            if float(getattr(token, 'size', 1.0) or 1.0) >= 2.0:
                return True
        except (TypeError, ValueError):
            pass
        block = getattr(token, 'statblock', None) or {}
        text = ' '.join(str(block.get(key) or '') for key in
                        ('size', 'size_label', 'type')).casefold()
        return any(word in text for word in
                   ('больш', 'огромн', 'исполинск', 'large', 'huge',
                    'gargantuan'))

    def _consume_smite_locked(self, attacker, effect) -> bool:
        """Снять отработавшую кару: второй удар кубиков не получает."""
        effects = list(getattr(attacker, 'active_effects', None) or [])
        if effect not in effects:
            return False
        effects.remove(effect)
        attacker.active_effects = effects
        self._remove_effect_side_locked(attacker, effect)
        return True

    def _apply_smite_rider_locked(self, target, effect, caster_id: str,
                                  source: str = '') -> list:
        """Последствие кары: сбит с ног, испуган или горит.

        Спасбросок бросает сервер по листу или статблоку цели — клиент не
        может про него «забыть». У Пылающей кары на самом попадании
        спасброска нет: она вешает напоминание о 1к6 огня в начале ходов
        цели, а это уже ручное правило мастера.
        """
        rider = str(effect.get('rider') or '')
        if not rider:
            return []
        key = str(effect.get('spell_key') or '')
        note = str(effect.get('rider_note') or '')
        fx = str(effect.get('fx') or '')
        rounds = int(effect.get('rounds_left') or 10)
        duration = str(effect.get('duration') or '1 минута')
        name = str(effect.get('label') or 'Кара').split(':')[0].strip()
        if rider == 'reveal':
            # Клеймящая кара: цель перестаёт быть невидимой и светится пятном
            # в 5 футах, пока держится кара. Невидимость снимается по-настоящему
            # (и состояние на токене, и чип), а про свет пишет заметка.
            hidden_removed = self._cure_conditions_locked(target,
                                                           ('invisible',))
            specs = [{
                'type': 'note',
                'note': note or 'цель светится, пока держится кара',
                'label': f"{name}: цель светится",
                'rounds': rounds, 'duration': duration,
                'concentration': True, 'spell_key': key, 'fx': fx,
            }]
            applied = self._attach_effects_locked(target, specs,
                                                 caster_id=caster_id,
                                                 source=source)
            if hidden_removed:
                applied.append('снята невидимость')
            return applied
        if rider == 'staggered':
            # Потрясающий удар: проваливший спасбросок бьёт и проверяет с
            # помехой и не может совершать реакции — до конца своего
            # следующего хода, поэтому эффект короткий (2 раунда), а не
            # на всю минуту кары.
            specs = [{
                'type': 'attack_disadv',
                'label': f"{name}: помеха на атаки и проверки",
                'rounds': 2, 'duration': 'до конца его следующего хода',
                'spell_key': key, 'fx': fx,
            }]
            if note:
                specs.append({
                    'type': 'note', 'note': note,
                    'label': f"{name}: без реакций",
                    'rounds': 2, 'duration': 'до конца его следующего хода',
                    'spell_key': key, 'fx': fx,
                })
            return self._attach_effects_locked(target, specs,
                                               caster_id=caster_id,
                                               source=source)
        if rider == 'banished':
            # Изгоняющая кара решает по хитам цели ПОСЛЕ удара: их считает
            # roll_attack (`_banish_after_hit_locked`), а не этот разбор.
            return []
        if rider == 'burn':
            spec = {
                'type': 'note', 'note': note or '1к6 огня в начале хода',
                'label': f"{name}: {note or 'горит'}",
                'rounds': rounds, 'duration': duration,
                'concentration': True, 'spell_key': key, 'fx': fx,
            }
        elif rider == 'restrained':
            # Лоза держит цель до конца заклинания: состояние с
            # концентрацией плюс чип с уроном в начале ходов и правилом
            # освобождения (проверка Силы против Сл колдуна — это ручное
            # действие мастера).
            condition = spell_rules.condition_ru('restrained')
            specs = [{
                'type': 'condition', 'condition': condition,
                'label': f"{name}: {str(condition).casefold()}",
                'rounds': rounds, 'duration': duration,
                'concentration': True, 'spell_key': key, 'fx': fx,
            }]
            if note:
                specs.append({
                    'type': 'note', 'note': note,
                    'label': f"{name}: лоза",
                    'rounds': rounds, 'duration': duration,
                    'concentration': True, 'spell_key': key, 'fx': fx,
                })
            return self._attach_effects_locked(target, specs,
                                               caster_id=caster_id,
                                               source=source)
        else:
            condition = spell_rules.condition_ru(rider)
            spec = {
                'type': 'condition', 'condition': condition,
                'label': f"{name}: {str(condition).casefold()}",
                'rounds': rounds, 'duration': duration,
                # Испуг держится как часть кары (концентрация), сбитого с ног
                # поднимают движением — состояние остаётся само по себе.
                'concentration': rider == 'frightened',
                'spell_key': key, 'fx': fx,
            }
        return self._attach_effects_locked(target, [spec],
                                           caster_id=caster_id,
                                           source=source)

    def _smite_burst_locked(self, caster, center, effect, caster_sheet):
        """Град шипов: спасброски Ловкости цели и всех в 5 футах от неё.

        Возвращает (строки для карточки, обновления токенов). Размер кольца
        и кубики берутся из правила, а не из запроса; спасбросок бросает
        сервер по листу или статблоку каждого попавшего — успех даёт
        половину урона.
        """
        key = str(effect.get('spell_key') or '')
        rules = spell_rules.SMITE_SPELLS.get(key) or {}
        radius = float(rules.get('radius_ft') or 5)
        formula = str(effect.get('rider_dice') or '1d10')
        dtype = str(effect.get('rider_damage_type')
                    or rules.get('rider_damage_type') or 'piercing')
        try:
            dc = spell_rules.spell_save_dc(caster_sheet or {})
        except (TypeError, ValueError):
            dc = 10
        victims = [center]
        for token in list(self.game_state.tokens.values()):
            if token is center or token.map_id != center.map_id:
                continue
            if not token.visible or token.hp is None:
                continue
            if combat_rules.death_save_state(token.death_saves)['dead']:
                continue
            try:
                near = self._token_distance_ft_locked(center, token)
            except Exception:
                continue
            if near <= radius + 0.1:
                victims.append(token)
        rows = []
        updates = []
        for token in victims:
            sheet = self._sheet_for_token_locked(token)
            save = self._save_with_mods_locked(token, sheet, 'dex', dc)
            saved = bool(save.get('success'))
            try:
                rolled = dice.roll(formula)
            except dice.DiceError:
                continue
            part = {'formula': rolled.get('formula'),
                    'total': int(rolled.get('total') or 0)}
            base = part['total'] // 2 if saved else part['total']
            protection = token_stat.token_protection(token, sheet)
            dealt = combat_rules.apply_resistance(base, dtype, **protection)
            amount = int(dealt.get('amount') or 0)
            # Окаменение даёт сопротивление всему урону — как и в ударе.
            stone = conditions_rules.damage_factor(
                self._conditions_with_hp(token, sheet))
            if stone < 1.0 and int(dealt.get('factor', 1.0) or 1.0) >= 1:
                amount = int(amount * stone)
            part['amount'] = amount
            part['damage_type'] = dtype
            before = max(0, int(token.hp or 0))
            token.hp = max(0, before - amount)
            delta = token.hp - before
            rows.append({
                'token_id': token.id, 'name': token.name,
                'hp': token.hp, 'hp_max': token.hp_max,
                'delta': delta, 'amount': amount,
                'total': part['total'], 'formula': part['formula'],
                'damage_type': dtype,
                'save': save, 'save_success': saved,
                'part': part,
            })
            if amount:
                dropped, note = self._concentration_check_locked(
                    token, amount, sheet)
                extra = []
                for other in dropped:
                    if other.id != token.id:
                        extra.append({'token_id': other.id,
                                      'active_effects': list(
                                          other.active_effects or [])})
                if token is center and dropped and any(
                        other.id == token.id for other in dropped):
                    updates.append({'token_id': token.id,
                                    'hp': token.hp,
                                    'hp_max': token.hp_max,
                                    'active_effects': list(
                                        token.active_effects or []),
                                    'delta': 0})
                updates.extend(extra)
            if token is not center:
                # Соседей ранит сам град шипов: их хиты меняются напрямую,
                # а карточку броска показывает общий результат атаки —
                # поэтому это отдельное обновление токена, а не новый урон
                # без источника (иначе получилась бы вторая карточка).
                updates.append({'token_id': token.id, 'hp': token.hp,
                                'hp_max': token.hp_max, 'ac': token.ac,
                                'delta': delta, 'source': 'attack',
                                'fx': str(effect.get('fx') or '')})
                if delta < 0:
                    self.push_log(
                        f"🌿 Град шипов: «{token.name}» "
                        f"{abs(delta)} урона"
                        + (' (спасбросок пройден)' if saved else ''))
        return rows, updates

    def _apply_effect_side_locked(self, token, effect: dict) -> None:
        """Применить побочные флаги эффекта к токену."""
        kind = effect.get('type')
        if kind == 'resist':
            damage_type = combat_rules.normalize_damage_type(
                effect.get('damage_type'))
            present = any(
                combat_rules.normalize_damage_type(value) == damage_type
                for value in (token.resist or []))
            if not present:
                token.resist.append(damage_type)
            # При обновлении уже наложенного эффекта не теряем признак
            # «это сопротивление добавили мы»: иначе повторное зелье
            # оставляло огонь навсегда после истечения.
            if not effect['applied'].get('resist'):
                effect['applied']['resist'] = not present
        elif kind == 'condition':
            condition = effect.get('condition')
            present = condition in (token.conditions or [])
            if not present:
                token.conditions.append(condition)
            if not effect['applied'].get('condition'):
                effect['applied']['condition'] = not present
        elif kind == 'temp_hp':
            amount = int(effect.get('amount') or 0)
            try:
                current = max(0, int(token.hp_temp or 0))
            except (TypeError, ValueError):
                current = 0
            effect['applied']['temp_hp'] = amount if amount > current else 0
            token.hp_temp = max(current, amount)
        elif kind == 'speed':
            rounds = int(effect.get('rounds_movement')
                         or effect.get('rounds_left') or 1)
            bonus = int(effect.get('bonus_ft') or 0)
            if bonus:
                # Удар Зефира даёт +30 футов, а не удвоение: бюджет
                # движения считает их отдельно от зелья скорости.
                if self.game_state.combat_active:
                    until = self.game_state.combat_round + rounds - 1
                    self._move_bonus_until[token.id] = max(
                        until, self._move_bonus_until.get(token.id, 0))
                return
            if self.game_state.combat_active:
                until = self.game_state.combat_round + rounds - 1
                self._move_haste_until[token.id] = max(
                    until, self._move_haste_until.get(token.id, 0))
            else:
                self._move_haste_pending[token.id] = max(
                    rounds, self._move_haste_pending.get(token.id, 0))
        elif kind == 'hp_max':
            amount = int(effect.get('amount') or 0)
            try:
                before = int(token.hp_max or 0) if token.hp_max else 0
            except (TypeError, ValueError):
                before = 0
            # Один и тот же эффект не может урезать максимум дважды:
            # повторное обновление чипа (тот же объект) уже применено.
            if (amount > 0 and before > 1
                    and 'hp_max' not in (effect.get('applied') or {})):
                after = max(1, before - amount)
                token.hp_max = after
                effect['applied']['hp_max'] = before - after
                # Текущие хиты не могут быть больше максимума: «лишние»
                # хиты уходят сразу, как при уменьшении максимума в 5e.
                if token.hp is not None and int(token.hp) > after:
                    token.hp = after
        elif kind == 'rage':
            # Сопротивление дробящему, колющему и рубящему на время ярости.
            # Помним только те типы, которые добавили мы: чужое сопротивление
            # снимать нельзя.
            granted = list(effect['applied'].get('rage_resist') or [])
            for key in class_features.RAGE_RESIST:
                damage_type = combat_rules.normalize_damage_type(key)
                if damage_type and damage_type not in (token.resist or []):
                    token.resist.append(damage_type)
                    if damage_type not in granted:
                        granted.append(damage_type)
            # Повторная ярость не теряет список того, что добавили именно
            # мы: иначе после истечения сопротивления остались бы навсегда.
            effect['applied']['rage_resist'] = granted

    def _remove_effect_side_locked(self, token, effect: dict) -> None:
        """Снять флаги истёкшего эффекта, не задев чужие.

        Если сопротивление или состояние уже были у токена до зелья, мы их
        не добавляли и не должны снимать. Если такой же эффект наложен
        вторым зельем, флаг остаётся, пока жив хотя бы один эффект.
        """
        kind = effect.get('type')
        applied = effect.get('applied') or {}
        others = [item for item in (token.active_effects or [])
                  if item is not effect]
        if kind == 'resist' and applied.get('resist'):
            damage_type = combat_rules.normalize_damage_type(
                effect.get('damage_type'))
            still = any(
                combat_rules.normalize_damage_type(item.get('damage_type'))
                == damage_type for item in others
                if item.get('type') == 'resist')
            if not still:
                token.resist = [
                    value for value in (token.resist or [])
                    if combat_rules.normalize_damage_type(value) != damage_type]
        elif kind == 'condition' and applied.get('condition'):
            condition = effect.get('condition')
            still = any(item.get('condition') == condition for item in others
                        if item.get('type') == 'condition')
            if not still:
                token.conditions = [value for value in (token.conditions or [])
                                    if conditions_rules.canonical(value)
                                    != conditions_rules.canonical(condition)]
        elif kind == 'temp_hp':
            still = any(item.get('type') == 'temp_hp' for item in others)
            if not still:
                token.hp_temp = 0
        elif kind == 'speed':
            # У скорости два вида: удвоение (Ускорение, зелье) и плоская
            # прибавка (Удар Зефира). Снимаем только своё.
            bonus = int(effect.get('bonus_ft') or 0)
            speed_others = [item for item in others
                            if item.get('type') == 'speed']
            if bonus:
                still = any(int(item.get('bonus_ft') or 0)
                            for item in speed_others)
                if not still:
                    self._move_bonus_until.pop(token.id, None)
                return
            still = any(not int(item.get('bonus_ft') or 0)
                        for item in speed_others)
            if not still:
                self._move_haste_until.pop(token.id, None)
                self._move_haste_pending.pop(token.id, None)
        elif kind == 'hp_max':
            # Возвращаем только свою прибавку: чип истёк (час прошёл) или
            # его сняло лечение болезни (Малое/Большое восстановление,
            # Полное исцеление — по правилам Урона это возвращает максимум).
            applied_hp = int(applied.get('hp_max') or 0)
            if applied_hp and token.hp_max:
                token.hp_max = max(1, int(token.hp_max or 0) + applied_hp)
                if token.hp is not None and int(token.hp) > token.hp_max:
                    token.hp = token.hp_max
                # Прибавку возвращаем ровно один раз: повторный вызов
                # (истечение и лечение болезни в одном кадре) не должен
                # начислить её ещё раз.
                applied['hp_max'] = 0
        elif kind == 'rage':
            # Снимаем только те сопротивления, что добавила ярость, и только
            # если ни одна другая ярость их больше не держит.
            granted = set(applied.get('rage_resist') or [])
            still = set()
            for item in others:
                if item.get('type') == 'rage':
                    still.update((item.get('applied') or {})
                                 .get('rage_resist') or [])
            keep = granted - still
            if keep:
                token.resist = [
                    value for value in (token.resist or [])
                    if combat_rules.normalize_damage_type(value) not in keep]

    def _active_effects_payload_locked(self, token, added=None) -> dict:
        payload = {
            'token_id': token.id,
            'active_effects': list(token.active_effects or []),
            'conditions': list(token.conditions or []),
            'resist': list(token.resist or []),
            'hp_temp': int(token.hp_temp or 0),
            # Хиты и максимум идут вместе с чипами: истечение чипа Урона
            # (Harm) возвращает максимум, и клиент должен увидеть новое
            # значение сразу, а не после следующего обновления токена.
            'hp': token.hp,
            'hp_max': token.hp_max,
        }
        if added:
            payload['effect_added'] = list(added)
            # Ключи отклика новых эффектов: Щит показывает купол, а
            # Благословение — золотую ауру, у каждого свой звук.
            effects_fx = [effect.get('fx')
                          for effect in (token.active_effects or [])
                          if effect.get('label') in added and effect.get('fx')]
            if effects_fx:
                payload['effect_fx'] = effects_fx
        return payload

    def _save_with_mods_locked(self, token, sheet, ability, dc,
                              advantage: bool = False) -> dict:
        """Спасбросок токена со всеми поправками стола.

        Один и тот же код нужен урону по области и состояниям по области:
        парализованный проваливает автоматически, Благословение даёт +1к4,
        эффекты вроде Ускорения — преимущество, а бонус берётся из листа
        или статблока. Раньше это было только внутри урона по площади, и
        второе место пришлось бы писать заново.

        `advantage` — преимущество от самого правила (у Опутывающего удара
        Большой и крупнее проходит спасбросок Силы с преимуществом);
        с преимуществом от эффектов оно не складывается, а просто есть.
        """
        conditions = self._conditions_with_hp(token, sheet)
        save_mods = conditions_rules.save_modifiers(conditions, ability)
        bonus = token_stat.token_save_bonus(token, sheet, ability)
        bless_bonus, bless_detail = self._bless_bonus_locked(token)
        if bless_bonus:
            bonus += bless_bonus
        bane_penalty, bane_detail = self._bane_penalty_locked(token)
        if bane_penalty:
            bonus -= bane_penalty
        adv_labels = list(self._save_advantage_locked(token, ability))
        if advantage:
            adv_labels.append('Большой или крупнее: преимущество')
        if save_mods.get('auto_fail'):
            save = {'success': False, 'total': 1 + bonus, 'dc': dc,
                    'kept': 1, 'rolls': [1], 'auto_fail': True,
                    'bonus': bonus}
        else:
            save = combat_rules.roll_save(
                bonus, dc, advantage=bool(adv_labels),
                disadvantage=bool(save_mods.get('disadvantage')))
            save['auto_fail'] = False
            if adv_labels:
                save['advantage_from'] = adv_labels
        if bless_detail:
            save['bless'] = bless_detail
        if bane_detail:
            save['bane'] = bane_detail
        return save

    def _attach_effects_locked(self, token, specs, caster_id: str = '',
                               source: str = '') -> list:
        """Наложить эффекты плана на токен. Возвращает подписи наложенных.

        Повторное применение обновляет время уже висящего эффекта, а не
        плодит второй чип с тем же названием: Опутывание на второго врага —
        это два разных существа, но бросок того же заклинания на того же
        не должен давать две «Паутины» в карточке.
        """
        added = []
        for spec in specs or ():
            if not isinstance(spec, dict):
                continue
            effect = self._build_effect_locked(spec, source,
                                               caster_id=caster_id)
            if effect is None:
                continue
            existing = next(
                (item for item in (token.active_effects or [])
                 if item.get('type') == effect.get('type')
                 and item.get('label') == effect.get('label')), None)
            if existing is not None:
                existing['rounds_left'] = max(
                    int(existing.get('rounds_left') or 0),
                    int(effect.get('rounds_left') or 0))
                self._apply_effect_side_locked(token, existing)
                continue
            token.active_effects.append(effect)
            self._apply_effect_side_locked(token, effect)
            added.append(effect.get('label') or 'Эффект')
        return added

    def _attach_hp_max_effect_locked(self, token, amount: int, source: str = '',
                                     caster_id: str = '') -> Optional[dict]:
        """Урезать максимум хитов цели и повесить об этом чип (Урон, SRD).

        Возвращает сам эффект или None. Каждый каст — отдельный чип: два
        провала подряд складываются, а не заменяют друг друга, поэтому
        общий `_attach_effects_locked` (он обновляет чип с той же
        подписью) здесь не годится.
        """
        spec = {
            'type': 'hp_max',
            'amount': int(amount),
            'rounds': 600,                # час: 600 раундов по 6 секунд
            'duration': '1 час',
            'label': f'Поражение: максимум хитов −{int(amount)}',
            'source': source,
            'note': ('максимум хитов уменьшен полученным уроном; снимается '
                     'лечением болезни или через час'),
        }
        effect = self._build_effect_locked(spec, source, caster_id=caster_id)
        if effect is None:
            return None
        token.active_effects = list(token.active_effects or []) + [effect]
        self._apply_effect_side_locked(token, effect)
        return effect

    def _restore_hp_max_locked(self, token) -> int:
        """Вернуть максимум хитов, урезанный Поражением (Harm, SRD).

        Правило говорит: максимум падает на час, но любой эффект, снимающий
        болезнь, возвращает его к норме раньше. Снимаем все чипы `hp_max`
        сразу: лечение болезни не выбирает, какой из них убрать.
        """
        restored = 0
        kept = []
        for effect in list(token.active_effects or []):
            if effect.get('type') == 'hp_max':
                restored += int((effect.get('applied') or {})
                                .get('hp_max') or 0)
                self._remove_effect_side_locked(token, effect)
                continue
            kept.append(effect)
        token.active_effects = kept
        return restored

    def _zombie_note_locked(self, target, spec: dict, caster) -> Optional[dict]:
        """Встанет ли убитый Перстом смерти зомби (SRD).

        Зомби встаёт только из гуманоида, поэтому тип берём у статблока и
        листа тем же разбором, что и правила пула. Нежити, конструктам и
        исчадиям стол вешает честную пометку и пишет об этом в журнал:
        зомби из них не получится.
        """
        skip = tuple(str(word).casefold().replace('ё', 'е')
                     for word in (spec.get('zombie_skip') or ()))
        humanoid = True
        if skip:
            humanoid = not self._pool_skip_locked(target, {'pool_skip': skip})
        name = str(getattr(caster, 'name', '') or 'колдуна')
        if humanoid:
            note = (f'убит Перстом смерти: в начале следующего хода '
                    f'{name} встанет отсюда зомби (пока под его командой)')
            label = 'Перст смерти: встанет зомби'
        else:
            note = ('тип существа не гуманоид — зомби из него не встанет')
            label = 'Перст смерти: зомби не встанет'
        effect = self._build_effect_locked({
            'type': 'note',
            'note': note,
            'label': label,
            'rounds': 10,
            'duration': '1 минута',
            'spell_key': 'finger_of_death',
            'fx': str(spec.get('fx') or ''),
        }, str(spec.get('name') or ''), caster_id=str(getattr(caster, 'id', '') or ''))
        if effect is None:
            return None
        target.active_effects = list(target.active_effects or []) + [effect]
        return {'humanoid': humanoid, 'label': label, 'note': note}

    def _pool_skip_locked(self, token, spec: dict) -> bool:
        """Кого пул хитов не берёт (Сон, Цветной шар).

        Сон не действует на нежить и на тех, кого нельзя очаровать,
        Цветной шар — на слепых и безглазых. Тип и расу берём у статблока
        и листа, а список слов — из правила заклинания.
        """
        words = tuple(str(word).casefold().replace('ё', 'е')
                      for word in (spec.get('pool_skip') or ()))
        if not words:
            return False
        statblock = getattr(token, 'statblock', None) or {}
        sheet = self._sheet_for_token_locked(token)
        parts = [str(getattr(token, 'name', '') or '')]
        if isinstance(statblock, dict):
            for key in ('type', 'race', 'name'):
                parts.append(str(statblock.get(key) or ''))
        if isinstance(sheet, dict):
            info = sheet.get('info') or {}
            for key in ('race', 'charClass'):
                value = info.get(key)
                if isinstance(value, dict):
                    value = value.get('value')
                parts.append(str(value or ''))
        text = ' '.join(parts).casefold().replace('ё', 'е')
        return any(word in text for word in words)

    def _cure_conditions_locked(self, token, names, one: bool = False) -> list:
        """Снять перечисленные состояния с токена и его чипов эффектов.

        Убираем и запись в самом токене, и чип эффекта, который её держит:
        иначе состояние вернулось бы при следующем истечении эффекта или
        осталось бы висеть в карточке после лечения.
        """
        wanted = []
        for name in (names or ()):
            canonical = (conditions_rules.canonical(name)
                         or spell_rules.condition_ru(name))
            if canonical and canonical not in wanted:
                wanted.append(canonical)
        if not wanted:
            return []
        removed = []
        for condition in list(token.conditions or []):
            canonical = conditions_rules.canonical(condition)
            if canonical in wanted and canonical not in removed:
                removed.append(canonical)
                if one:
                    break
        if not removed:
            return []
        token.conditions = [value for value in (token.conditions or [])
                            if conditions_rules.canonical(value)
                            not in removed]
        kept = []
        for effect in (token.active_effects or []):
            condition = conditions_rules.canonical(effect.get('condition')) \
                if effect.get('type') == 'condition' else ''
            if condition and condition in removed:
                self._remove_effect_side_locked(token, effect)
                continue
            kept.append(effect)
        token.active_effects = kept
        # Мёртвое имя чипа не должно оставаться в карточке: токен вернёт
        # список активных эффектов клиентам сам.
        return removed

    def _dispel_effects_locked(self, target, slot_level: int, sheet) -> list:
        """Развеивание магии: снять эффекты заклинаний с цели (SRD 5.1).

        Эффект круга не выше потраченной ячейки снимается сразу; от более
        сильного требуется проверка 20 + модификатор характеристики против
        Сл 10 + круг эффекта. Концентрация колдуна при снятом эффекте
        прекращается.
        """
        modifier = 0
        try:
            modifier = spell_rules.casting_modifier(sheet)
        except Exception:
            modifier = 0
        removed = []
        casters = []
        kept = []
        for effect in list(target.active_effects or []):
            # Состояния, сопротивление и ярость — не заклинания: развеивание
            # их не трогает, это урон, яд или способность класса.
            if str(effect.get('type') or '') in ('condition', 'rage'):
                kept.append(effect)
                continue
            if not (effect.get('spell_key') or effect.get('spell_level')):
                kept.append(effect)
                continue
            level = int(effect.get('spell_level') or 0)
            if level <= int(slot_level or 0):
                pass
            else:
                # Проверка базовой характеристики против Сл 10 + круг
                # эффекта: провал — эффект остаётся на цели.
                rolled = dice.roll('1d20')
                if int(rolled.get('total') or 0) + int(modifier) < 10 + level:
                    kept.append(effect)
                    continue
            removed.append(effect)
            caster_id = str(effect.get('caster_id') or '')
            if caster_id and caster_id not in casters:
                casters.append(caster_id)
            self._remove_effect_side_locked(target, effect)
        target.active_effects = kept
        for caster_id in casters:
            self._drop_concentration_locked(caster_id)
        return removed

    def _token_distance_ft_locked(self, first, second) -> float:
        """Расстояние между токенами в футах по клеткам карты."""
        map_data = (self.game_state.maps.get(getattr(first, 'map_id', None))
                    or self.game_state.maps.get(getattr(second, 'map_id', None)))
        if map_data is None:
            return 0.0
        cell_w, cell_h = map_data.grid_cell_world
        if not cell_w or not cell_h:
            return 0.0
        try:
            dx = (float(second.x) - float(first.x)) / float(cell_w)
            dy = (float(second.y) - float(first.y)) / float(cell_h)
            return math.hypot(dx, dy) * 5.0
        except (TypeError, ValueError, AttributeError):
            return 0.0

    def _drop_concentration_locked(self, caster_id: str,
                                   keep_cast_id: Optional[str] = None):
        """Снять концентрацию колдуна со всех токенов стола.

        Благословение лежит копией на каждом из трёх существ, но это одна
        концентрация: провал спасброска Телосложения должен снять все три
        ауры сразу, а не только с колдуна.
        """
        changed = []
        for token in self.game_state.tokens.values():
            effects = list(token.active_effects or [])
            if not effects:
                continue
            kept = []
            removed = []
            for effect in effects:
                if (effect.get('concentration')
                        and str(effect.get('caster_id') or '') == caster_id
                        and not (keep_cast_id
                                 and effect.get('cast_id') == keep_cast_id)):
                    removed.append(effect)
                else:
                    kept.append(effect)
            if not removed:
                continue
            token.active_effects = kept
            for effect in removed:
                self._remove_effect_side_locked(token, effect)
            changed.append(token)
        return changed

    def _bless_bonus_locked(self, token):
        """Бросок +1d4 от Благословения: (бонус, разбор).

        Кубик бросается на каждую атаку и каждый спасбросок отдельно, как
        в SRD, а не один раз при наложении эффекта.
        """
        for effect in getattr(token, 'active_effects', None) or []:
            if effect.get('type') != 'bless':
                continue
            rolled = dice.roll('1d4')
            return int(rolled.get('total') or 0), {
                'die': '1d4', 'rolls': rolled.get('rolls'),
                'total': int(rolled.get('total') or 0),
            }
        return 0, None

    def _bane_penalty_locked(self, token):
        """Вычет 1d4 от Порчи: (штраф, разбор).

        SRD: проваливший спасбросок Харизмы вычитает 1к4 из бросков атаки
        и спасбросков, пока действует заклинание. Кубик бросается на каждый
        бросок цели — как и у Благословения, а не один раз при наложении.
        """
        for effect in getattr(token, 'active_effects', None) or []:
            if effect.get('type') != 'bane':
                continue
            rolled = dice.roll('1d4')
            return int(rolled.get('total') or 0), {
                'die': '1d4', 'rolls': rolled.get('rolls'),
                'total': int(rolled.get('total') or 0),
            }
        return 0, None

    def _save_advantage_locked(self, token, ability):
        """Преимущество на спасбросок от эффектов (Ускорение — Ловкость).

        Возвращает подписи эффектов, давших преимущество: их видно в чате,
        иначе бросок с двумя d20 выглядел бы случайным.
        """
        ability = token_stat.normalize_ability(ability)
        if not ability:
            return []
        labels = []
        for effect in (getattr(token, 'active_effects', None) or []):
            if not isinstance(effect, dict) or effect.get('type') != 'save_adv':
                continue
            if token_stat.normalize_ability(effect.get('ability')) != ability:
                continue
            labels.append(str(effect.get('label') or 'Эффект'))
        return labels

    def _concentration_check_locked(self, token, damage: int, sheet=None):
        """Спасбросок Телосложения на концентрацию после урона.

        Возвращает (затронутые токены, строка для журнала). Правило SRD:
        Сл 10 или половина полученного урона, что больше.
        """
        if int(damage or 0) <= 0:
            return [], ''
        # Концентрацию держит только колдун. Благословение на союзнике —
        # копия эффекта с чужим caster_id; урон по союзнику не должен
        # заставлять его бросать Телосложение за чужое заклинание.
        # Ищем эффекты на всём столе: метка охотника живёт на цели, но
        # концентрацию держит следопыт, и бросает он, когда бьют его, а не
        # помеченного.
        holds = any(
            effect.get('concentration')
            and str(effect.get('caster_id') or '') == token.id
            for other in self.game_state.tokens.values()
            for effect in (other.active_effects or []))
        if not holds:
            return [], ''
        sheet = sheet if sheet is not None else self._sheet_for_token_locked(token)
        bonus = token_stat.token_save_bonus(token, sheet, 'con')
        dc = max(10, int(damage) // 2)
        save = combat_rules.roll_save(bonus, dc)
        if save.get('success'):
            return [], (f"🔮 {token.name}: концентрация сохранена "
                        f"(Сл {dc}, {save.get('total')})")
        changed = self._drop_concentration_locked(token.id)
        return changed, (f"💫 {token.name}: концентрация нарушена "
                         f"(Сл {dc}, {save.get('total')})")

    def apply_effect(self, player_id: str, data: dict):
        """Наложить на токен длительный эффект зелья или заклинания.

        Права: мастер — любому, игрок — только своему токену. Эффект
        приходит списком, но сервер сам проверяет типы, длительность и
        состояние. Результат рассылается всем, поэтому чужой клиент видит
        невидимость и сопротивление так же, как свой.

        Для заклинаний (Щит, Благословение) сервер дополнительно сверяет
        запись в листе колдуна: прислать «+5 КД» из воздуха нельзя.
        """
        token_id = str(data.get('token_id') or '')
        specs = data.get('effects')
        if not token_id or not isinstance(specs, (list, tuple)):
            return
        source_label = str(data.get('source') or '').strip()
        spell_name = str(data.get('spell_name') or '').strip()
        caster = self.game_state.tokens.get(
            str(data.get('from_token_id') or ''))
        with self.lock:
            token = self.game_state.tokens.get(token_id)
            if token is None:
                return
            if not self.is_dm(player_id) and token.owner_id != player_id:
                # Зелье можно передать союзнику: игрок накладывает эффект
                # своим токеном. Чужой токен без своего источника — отказ.
                if caster is None or caster.owner_id != player_id:
                    client = self.clients.get(player_id)
                    if client:
                        client.send_message(MessageType.PERMISSION_DENIED,
                                            {'action': 'apply_effect'})
                    return
            # Заклинание: эффект собирает сервер из правила, а не клиент.
            # Иначе к «Щиту» можно было бы приложить чужой `bless` или
            # прислать +10 КД вместо +5.
            spell_effects_trusted = None
            trusted_plan = {}
            if spell_name:
                caster_sheet = (self._sheet_for_token_locked(caster)
                                if caster else None)
                record = self._find_area_spell_locked(
                    caster_sheet, data, spell_name)
                if record is None and not self.is_dm(player_id):
                    self.send_error(
                        player_id,
                        f"«{spell_name}» нет в листе — эффект отклонён")
                    return
                if record is not None:
                    trusted = spell_rules.plan(record, caster_sheet)
                    trusted_plan = trusted
                    spell_effects_trusted = [dict(effect) for effect
                                             in (trusted.get('effects') or [])
                                             if isinstance(effect, dict)]
                    if not spell_effects_trusted:
                        self.send_error(
                            player_id,
                            f"«{spell_name}» не накладывает эффект")
                        return
            try:
                max_distance = float(data.get('max_distance_ft') or 0)
            except (TypeError, ValueError):
                max_distance = 0.0
            if (max_distance > 0 and caster is not None
                    and caster.id != token.id
                    and self._token_distance_ft_locked(caster, token)
                    > max_distance + 0.1):
                self.send_error(
                    player_id,
                    f"«{token.name}» дальше дистанции заклинания "
                    f"({max_distance:.0f} фут)")
                return
            cast_id = str(data.get('cast_id') or '')
            # Концентрацию определяем по доверенным эффектам, а не по
            # присланным полям: клиент не может скрыть флаг и держать две
            # концентрации одновременно.
            source_specs = (spell_effects_trusted
                            if spell_effects_trusted is not None
                            else list(specs)[:8])
            replaced = []
            if any(spec.get('concentration') for spec in source_specs
                   if isinstance(spec, dict)):
                caster_id = caster.id if caster is not None else token.id
                replaced = self._drop_concentration_locked(
                    caster_id, keep_cast_id=cast_id or None)
            added = []
            save_results = []
            sheet_payloads = []
            caster_id = caster.id if caster is not None else ''
            target_sheet = self._sheet_for_token_locked(token)
            for spec in source_specs:
                # Состояние со спасброском (Удержание личности, Сон от
                # заклинания): бросает сервер, а не клиент. Сл берётся из
                # плана по листу колдуна, бонус — из листа или статблока
                # цели. Успех — эффект не накладывается вовсе.
                save = str(spec.get('save') or '').strip().casefold()
                if save and spell_effects_trusted is not None:
                    try:
                        dc = int(spec.get('dc') or 0)
                    except (TypeError, ValueError):
                        dc = 0
                    if 1 <= dc <= 40:
                        bonus = token_stat.token_save_bonus(
                            token, target_sheet, save)
                        throw = combat_rules.saving_throw(bonus, dc)
                        label = str(spec.get('label') or 'Эффект')
                        ability = token_stat.ABILITY_LABELS.get(save, save)
                        save_results.append({
                            'label': label,
                            'ability': save,
                            'ability_label': ability,
                            'success': bool(throw['success']),
                            'd20': int(throw['d20']),
                            'bonus': int(throw['bonus']),
                            'total': int(throw['total']),
                            'dc': int(throw['dc']),
                            'target': token.name,
                        })
                        if throw['success']:
                            continue
                if str(spec.get('type') or '').strip().casefold() == 'rage':
                    # Ярость — способность варвара, а не зелье: сервер сам
                    # проверяет уровень, списывает заряд и считает бонус
                    # (+2 / +3 / +4). Клиентское число не используется.
                    if caster is None or caster.id != token.id:
                        self.send_error(player_id,
                                        'Ярость: только на своего персонажа')
                        continue
                    if class_features.rage_on(token) is not None:
                        # 5e: нельзя войти в ярость повторно, пока она идёт.
                        self.send_error(
                            player_id,
                            'Ярость уже действует — дождитесь конца минуты')
                        continue
                    caster_sheet = self._sheet_for_token_locked(caster)
                    bonus = class_features.rage_bonus(caster_sheet or {})
                    if not bonus:
                        self.send_error(
                            player_id,
                            'Ярость: у персонажа нет уровня варвара')
                        continue
                    if not class_features.feature_spend(
                            caster_sheet, class_features.RAGE):
                        self.send_error(
                            player_id,
                            'Ярость: заряды кончились — нужен долгий отдых')
                        continue
                    spec = dict(spec, bonus=bonus, rounds=10)
                    by_token = getattr(self.game_state, 'token_sheets', None)
                    if isinstance(by_token, dict):
                        by_token[caster.id] = caster_sheet
                    if caster.owner_id:
                        self.game_state.sheets[caster.owner_id] = caster_sheet
                    sheet_payloads.append({
                        'player_id': caster.owner_id or '',
                        'sheet': caster_sheet,
                        'token_id': caster.id,
                    })
                effect = self._build_effect_locked(
                    spec, source_label, caster_id=caster_id,
                    cast_id=cast_id)
                if effect is None:
                    continue
                # Повторное зелье того же вида обновляет время, а не
                # плодит второй чип с тем же названием.
                existing = next(
                    (item for item in (token.active_effects or [])
                     if item.get('type') == effect.get('type')
                     and item.get('label') == effect.get('label')), None)
                if existing is not None:
                    existing['rounds_left'] = max(
                        int(existing.get('rounds_left') or 0),
                        int(effect.get('rounds_left') or 0))
                    if effect.get('amount'):
                        existing['amount'] = max(
                            int(existing.get('amount') or 0),
                            int(effect.get('amount') or 0))
                    if effect.get('fx'):
                        existing['fx'] = effect['fx']
                    self._apply_effect_side_locked(token, existing)
                    continue
                token.active_effects.append(effect)
                self._apply_effect_side_locked(token, effect)
                added.append(effect.get('label') or 'Эффект')
            # Заклинание может не только наложить эффект, но и снять
            # состояние: Малое восстановление, Защита от яда, Снятие
            # проклятия. Список состояний берём из правила листа, а не из
            # запроса, — иначе игрок снял бы себе что угодно.
            cured = self._cure_conditions_locked(
                token, trusted_plan.get('cures'),
                one=bool(trusted_plan.get('cures_one')))
            # Поражение (Harm, SRD): «любой эффект, снимающий болезнь, возвращает
            # максимум хитов к норме». Отдельной кнопки это не требует —
            # смотрим на тот же флаг `disease`, по которому снимаются болезни.
            restored_hp = (self._restore_hp_max_locked(token)
                           if trusted_plan.get('disease') else 0)
            payload = self._active_effects_payload_locked(token, added)
            if restored_hp:
                payload['hp_max_restored'] = restored_hp
            if save_results:
                payload['save_results'] = save_results
            if cured:
                payload['cured'] = cured
            name = token.name
            speed_changed = any(effect.get('type') == 'speed'
                                for effect in (token.active_effects or []))
            movement = (self._movement_payload_locked(token)
                        if speed_changed else None)
            replaced_payloads = [
                {'token_id': other.id,
                 'active_effects': list(other.active_effects or []),
                 'condition_removed': True}
                for other in replaced if other.id != token.id]
        self.broadcast(MessageType.UPDATE_TOKEN, payload)
        for extra in replaced_payloads:
            self.broadcast(MessageType.UPDATE_TOKEN, extra)
        for sheet_payload in sheet_payloads:
            # Израсходованный заряд ярости должен сразу вернуться на лист.
            self.broadcast(MessageType.SHEET_STATE, sheet_payload)
        if movement is not None:
            self._broadcast_movement_state([movement])
        if replaced_payloads:
            self.push_log(f"💫 {name}: прошлая концентрация снята")
        if added:
            self.push_log(f"✨ {name}: {', '.join(added)}")
        if payload.get('cured'):
            self.push_log(f"✨ {name}: снято — {', '.join(payload['cured'])}")
        if payload.get('hp_max_restored'):
            self.push_log(
                f"✨ {name}: максимум хитов восстановлен на "
                f"{int(payload['hp_max_restored'])}")
        for row in save_results:
            # «Эффект», а не «состояние»: тем же путём идёт не только
            # Удержание личности (состояние), но и Порча (штраф).
            word = "успех — эффект не наложен" if row['success'] \
                else "провал"
            self.push_log(
                f"🎲 {row['target']}: спасбросок "
                f"{row['ability_label']} {row['total']} "
                f"(к20 {row['d20']} {row['bonus']:+d}) против Сл {row['dc']} — "
                f"{word}")

    def _tick_active_effects_locked(self) -> list:
        """Уменьшить длительность эффектов на смене раунда.

        Возвращает токены, у которых что-то истекло: рассылать их нужно вне
        блокировки, как и остальные сообщения сервера.
        """
        changed = []
        for token in self.game_state.tokens.values():
            effects = list(token.active_effects or [])
            if not effects:
                continue
            kept = []
            expired = []
            for effect in effects:
                if 'rounds_left' not in effect:
                    kept.append(effect)
                    continue
                try:
                    left = int(effect.get('rounds_left') or 0) - 1
                except (TypeError, ValueError):
                    left = 0
                if left <= 0:
                    expired.append(effect)
                else:
                    effect['rounds_left'] = left
                    kept.append(effect)
            if not expired:
                continue
            token.active_effects = kept
            for effect in expired:
                self._remove_effect_side_locked(token, effect)
            changed.append(token)
        return changed

    # ------------------------------------------------------------------
    # Бой: инициатива и очередь ходов
    #
    # Как в Foundry Combat Tracker: ДМ выставляет инициативу токенам,
    # начинает бой — сервер строит очередь по убыванию инициативы — и
    # двигает ход дальше. Очередь и раунд живут в GameState, поэтому
    # попадают и в полное состояние нового клиента, и в сохранение мира.
    # ------------------------------------------------------------------
    def _combat_participants_locked(self) -> list:
        """Кто вообще может стоять в очереди ходов.

        Бой идёт на одной сцене. Токены соседних карт сохраняют инициативу
        (мастер выставил её для следующей встречи), но в очередь текущего боя
        не входят: иначе ход доставался бы существам, которых за столом нет,
        стрелка «следующий» перескакивала бы через невидимых, а раунды шли
        бы быстрее реальных ходов. Токен без map_id — из старого сохранения:
        он принадлежит открытой карте.
        """
        gs = self.game_state
        current_map = gs.current_map_id
        return [token for token in gs.tokens.values()
                if token.initiative is not None
                and (current_map is None
                     or token.map_id in (None, current_map))]

    def _prune_order_locked(self, end_if_empty: bool = True,
                            reset_turn_if_gone: bool = False):
        """Пересобрать очередь из инициатив токенов, сохранив текущий ход.

        Очередь — производное состояние: её источник — initiative самих
        токенов. Поэтому токен без инициативы, токен соседней карты или
        удалённый токен исчезают из очереди автоматически, а индекс хода
        пересчитывается так, чтобы текущим оставался тот же токен, а не тот
        же номер строки.

        ``end_if_empty=False`` — для переключения карты: там пустая очередь
        на этой сцене не означает, что бой закончился (мастер мог просто
        заглянуть в соседнюю комнату). ``reset_turn_if_gone`` — для той же
        смены сцены: если текущего бойца на новой карте нет, раунд там
        начинается с первого хода, а не с номера строки прошлой сцены.
        """
        gs = self.game_state
        current = (gs.combat_order[gs.combat_turn]
                   if 0 <= gs.combat_turn < len(gs.combat_order) else None)
        # Равные инициативы: по имени, затем по id — порядок детерминирован
        # у всех участников (в D&D 5e ничью решает ловкость, у нас её нет).
        participants = self._combat_participants_locked()
        participants.sort(key=lambda t: (-t.initiative, t.name.lower(), t.id))
        order = [t.id for t in participants]
        gs.combat_order = order
        if current in order:
            gs.combat_turn = order.index(current)
        elif reset_turn_if_gone:
            gs.combat_turn = 0
        else:
            gs.combat_turn = max(0, min(gs.combat_turn, len(order) - 1))
        if gs.combat_active and not order and end_if_empty:
            gs.combat_active = False

    @staticmethod
    def _combat_payload_locked(gs: GameState) -> dict:
        return {
            'combat_active': gs.combat_active,
            'order': list(gs.combat_order),
            'turn': gs.combat_turn,
            'round': gs.combat_round,
        }

    def _broadcast_combat_if_active(self):
        """Сообщить очередь всем, если бой идёт или только что опустел.

        Без боя сообщение бесполезно и шумит в клиентах, поэтому его
        отправляем лишь когда очередь реально существовала.
        """
        with self.lock:
            had = (self.game_state.combat_active
                   or bool(self.game_state.combat_order))
            self._prune_order_locked()
            payload = self._combat_payload_locked(self.game_state)
            live = payload['combat_active'] or bool(payload['order'])
        if had or live:
            self.broadcast(MessageType.INITIATIVE_UPDATE, payload)
            self._broadcast_current_movement_state()

    def _current_turn_name(self):
        gs = self.game_state
        if 0 <= gs.combat_turn < len(gs.combat_order):
            token = gs.tokens.get(gs.combat_order[gs.combat_turn])
            if token:
                return token.name
        return None

    def set_initiative(self, player_id: str, data: dict):
        """Выставить или сбросить инициативу токена(ов).

        Форматы входа:
          * {'token_id', 'initiative'} — один токен (initiative=None сброс);
          * {'entries': [{'token_id', 'initiative'}, ...]} — пачкой
            (удобно, когда ДМ вносит значения сразу после броска).
        """
        entries = data.get('entries')
        if not entries and data.get('token_id') is not None:
            entries = [{'token_id': data['token_id'],
                        'initiative': data.get('initiative')}]
        if not entries:
            return

        changed = []   # [(token_id, initiative)]
        names = []
        with self.lock:
            for entry in entries:
                token = self.game_state.tokens.get(entry.get('token_id'))
                if not token:
                    continue
                value = entry.get('initiative')
                if value is None:
                    token.initiative = None
                else:
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        continue
                    # Диапазон как в D&D: от −10 до 99, мимо — зажимаем.
                    token.initiative = max(-10, min(99, value))
                changed.append((token.id, token.initiative))
                names.append(f"{token.name}: "
                             + (str(token.initiative)
                                if token.initiative is not None else "вне боя"))
            # Инициатива в бою меняет порядок — пересобираем, сохраняя ход
            if changed:
                self._prune_order_locked()
                payload = self._combat_payload_locked(self.game_state)
        if not changed:
            return
        # Сначала значения инициативы, затем состояние очереди:
        # клиент обновляет трекер и от значений, и от порядка.
        for token_id, initiative in changed:
            self.broadcast(MessageType.UPDATE_TOKEN,
                           {'token_id': token_id, 'initiative': initiative})
        self.broadcast(MessageType.INITIATIVE_UPDATE, payload)
        self._broadcast_current_movement_state()
        self.push_log("Инициатива: " + ", ".join(names))

    def start_combat(self, player_id: str, data: dict = None):
        """Начать бой: очередь по убыванию инициативы, первый ход — первой."""
        with self.lock:
            self._prune_order_locked()
            if not self.game_state.combat_order:
                self.send_error(
                    player_id,
                    "Бой не начат: укажите инициативу хотя бы одному "
                    "токену на этой карте")
                return
            gs = self.game_state
            gs.combat_active = True
            gs.combat_turn = 0
            gs.combat_round = 1
            # Зелье, выпитое до боя, — единственное, что переживает сброс:
            # оно ждало именно этого момента.
            pending_haste = dict(self._move_haste_pending)
            self._reset_movement_state_locked()
            self._move_haste_pending.update(pending_haste)
            self._activate_pending_haste_locked()
            payload = self._combat_payload_locked(gs)
            first = self._current_turn_name()
        self.broadcast(MessageType.INITIATIVE_UPDATE, payload)
        self._broadcast_current_movement_state()
        self.push_log(f"⚔ Бой начат (раунд 1) — ход: {first or '?'}")

    def end_combat(self, player_id: str, data: dict = None):
        """Закончить бой: очередь и инициативы остаются, ход больше не идёт."""
        with self.lock:
            self.game_state.combat_active = False
            self._reset_movement_state_locked()
            payload = self._combat_payload_locked(self.game_state)
        self.broadcast(MessageType.INITIATIVE_UPDATE, payload)
        self.push_log("Бой окончен")

    def next_turn(self, player_id: str, data: dict = None):
        """Следующий (step=1, по умолчанию) или предыдущий (step=-1) ход.

        Проход за последний участника закрывает раунд и открывает новый,
        поэтому очередь крутится бесконечно — кончает бой только ДМ.
        """
        try:
            step = -1 if int((data or {}).get('step', 1)) < 0 else 1
        except (TypeError, ValueError):
            step = 1
        expired_tokens = []
        with self.lock:
            gs = self.game_state
            # Пустая очередь (вся сцена пуста) — не повод молча закончить
            # бой: мастер мог переключиться на соседнюю карту.
            self._prune_order_locked(end_if_empty=False)
            if not gs.combat_active or not gs.combat_order:
                return
            turn = gs.combat_turn + step
            if turn >= len(gs.combat_order):
                turn = 0
                gs.combat_round += 1
                # Длительности эффектов считаются раундами боя. Тикаем
                # только при движении вперёд: возврат хода назад не должен
                # продлевать или отнимать время у зелья.
                expired_tokens = self._tick_active_effects_locked()
            elif turn < 0:
                turn = len(gs.combat_order) - 1
                gs.combat_round = max(1, gs.combat_round - 1)
            gs.combat_turn = turn
            # Перезарядка особых атак: 5–6 на d6 в начале хода существа.
            recharge_notes = self._special_recharge_tick_locked()
            payload = self._combat_payload_locked(gs)
            name = self._current_turn_name()
            rnd = gs.combat_round
            expired_payloads = [
                self._active_effects_payload_locked(token)
                for token in expired_tokens
            ]
        self.broadcast(MessageType.INITIATIVE_UPDATE, payload)
        for expired in expired_payloads:
            self.broadcast(MessageType.UPDATE_TOKEN, expired)
        self._broadcast_current_movement_state()
        for note in recharge_notes:
            self.push_log(note)
        self.push_log(f"Ход {name or '?'} (раунд {rnd})")
        if expired_payloads:
            self.push_log(
                "Истекли эффекты: " + ", ".join(
                    str(token.name) for token in expired_tokens))

    # ------------------------------------------------------------------
    # Растровый рисунок
    # ------------------------------------------------------------------
    def draw_op(self, player_id: str, data: dict):
        """Новый штрих — копим на сервере и раздаём остальным.

        Рисунок хранится как список операций: так новый участник получает
        его целиком в полном состоянии, а отмена — это просто pop().
        """
        map_id = data.get('map_id')
        op = data.get('op')
        if not map_id or not op:
            return
        with self.lock:
            self.game_state.draw_ops.setdefault(map_id, []).append(op)
        self.broadcast(MessageType.DRAW_OP, {'map_id': map_id, 'op': op},
                       exclude=player_id)

    # ------------------------------------------------------------------
    # Текстовые надписи на карте
    # ------------------------------------------------------------------
    def add_map_text(self, player_id: str, data: dict):
        """Новая надпись: проверяем поля и раздаём всем.

        Надпись — объект, а не штрих кисти: клиент присылает только текст,
        место и оформление, а id и пределы значений назначает сервер.
        """
        map_id = str((data or {}).get('map_id') or '')
        raw = (data or {}).get('item')
        if not map_id or not isinstance(raw, dict):
            return
        # Пределы значений назначает сервер: клиент присылает текст, место
        # и оформление.
        # Координаты передаются отдельными аргументами: в fields им места
        # нет, иначе один и тот же аргумент пришёл бы дважды.
        fields = {key: value for key, value in raw.items()
                  if key in map_text_rules.EDITABLE and key not in ('x', 'y')}
        item = map_text_rules.new_text(raw.get('x', 0.0), raw.get('y', 0.0),
                                       **fields)
        # id, предложенный клиентом, сохраняем: эхо своей надписи он узнает
        # по id и не нарисует её дважды. Ограничение длины — как в правилах.
        client_id = str(raw.get('id') or '').strip()[:64]
        if client_id:
            renamed = map_text_rules.normalize(item, text_id=client_id)
            if renamed is not None:
                item = renamed
        with self.lock:
            if map_id not in self.game_state.maps:
                return
            items = self.game_state.map_texts.setdefault(map_id, [])
            # Предел на карту: подписи — не бесконечный список, иначе
            # карта превращается в мешанину, а состояние растёт в мире.
            if len(items) >= MAX_MAP_TEXTS:
                self.send_error(player_id, 'Слишком много надписей на карте')
                return
            if any(str(row.get('id') or '') == item['id'] for row in items):
                # Чужий id не должен затирать уже существующую надпись:
                # в этом случае назначаем новый собственный.
                item = map_text_rules.new_text(raw.get('x', 0.0),
                                               raw.get('y', 0.0), **fields)
            items.append(item)
        # Автору эхо не нужно: он уже применил надпись у себя. Раньше оно
        # приходило всем, и запоздавшее эхо старой правки откатывало более
        # новую, а эхо добавления могло воскресить только что удалённую
        # надпись. У штрихов так и сделано — теперь так же и у текста.
        self.broadcast(MessageType.MAP_TEXT_ADDED,
                       {'map_id': map_id, 'item': item}, exclude=player_id)

    def update_map_text(self, player_id: str, data: dict):
        """Правка или перенос надписи: id + изменённые поля."""
        map_id = str((data or {}).get('map_id') or '')
        text_id = str((data or {}).get('text_id') or '')
        patch = (data or {}).get('patch')
        if not map_id or not text_id or not isinstance(patch, dict):
            return
        with self.lock:
            items = self.game_state.map_texts.get(map_id) or []
            for index, item in enumerate(items):
                if str(item.get('id') or '') != text_id:
                    continue
                updated = map_text_rules.apply_patch(item, patch)
                if not str(updated.get('text') or '').strip():
                    # Стёрли весь текст — надпись удаляется, как в
                    # Photoshop: пустой слой остаётся невидимым.
                    items.pop(index)
                    removed = True
                else:
                    items[index] = updated
                    removed = False
                break
            else:
                return
        if removed:
            self.broadcast(MessageType.MAP_TEXT_REMOVED,
                           {'map_id': map_id, 'text_id': text_id},
                           exclude=player_id)
        else:
            self.broadcast(MessageType.MAP_TEXT_UPDATED,
                           {'map_id': map_id, 'item': updated},
                           exclude=player_id)

    def remove_map_text(self, player_id: str, data: dict):
        """Удалить одну надпись."""
        map_id = str((data or {}).get('map_id') or '')
        text_id = str((data or {}).get('text_id') or '')
        if not map_id or not text_id:
            return
        with self.lock:
            items = self.game_state.map_texts.get(map_id) or []
            kept = [item for item in items
                    if str(item.get('id') or '') != text_id]
            if len(kept) == len(items):
                return
            self.game_state.map_texts[map_id] = kept
        self.broadcast(MessageType.MAP_TEXT_REMOVED,
                       {'map_id': map_id, 'text_id': text_id},
                       exclude=player_id)

    def clear_drawings(self, player_id: str, data: dict):
        """Стереть весь рисунок на карте для всех"""
        map_id = data.get('map_id')
        if not map_id:
            return
        with self.lock:
            self.game_state.draw_ops[map_id] = []
        self.broadcast(MessageType.CLEAR_DRAWINGS, {'map_id': map_id},
                       exclude=player_id)
        self.push_log("Рисунок на карте стёрт")

    def undo_drawing(self, player_id: str, data: dict):
        """Убрать последний штрих у всех"""
        map_id = data.get('map_id')
        if not map_id:
            return
        with self.lock:
            ops = self.game_state.draw_ops.get(map_id, [])
            if ops:
                ops.pop()
        self.broadcast(MessageType.UNDO_DRAWING, {'map_id': map_id},
                       exclude=player_id)

    # ------------------------------------------------------------------
    # Аудио
    # ------------------------------------------------------------------
    def add_audio(self, player_id: str, data: dict):
        """Новый звук: сохраняем файл на сервере и раздаём всем"""
        audio_id = data.get('id')
        with self.lock:
            if data.get('asset_ref'):
                self.assets[audio_id] = data['asset_ref']
            self.game_state.audio_tracks[audio_id] = AudioTrack.from_dict(data)
        self.broadcast(MessageType.ADD_AUDIO, data)
        self.push_log(f"DM добавил звук «{data.get('name')}»")

    def play_sound(self, player_id: str, data: dict):
        self.broadcast(MessageType.PLAY_SOUND, data)
        self.push_log(f"DM воспроизвёл звук «{data.get('name', '')}»")

    def play_music(self, player_id: str, data: dict):
        self.broadcast(MessageType.PLAY_MUSIC, data)
        self.push_log("DM управляет музыкой")

    # ------------------------------------------------------------------
    # Чат и кубики
    # ------------------------------------------------------------------
    def handle_chat(self, player_id: str, data: dict):
        player = self.game_state.players.get(player_id)
        if not player:
            return
        # Длина ограничена, а лишние поля отбрасываются: одно сообщение
        # рассылается ВСЕМ, и кадр в 4 МБ от флудера занял бы очередь у
        # каждого клиента. Заодно в чужие чаты не попадают служебные поля.
        message = str(data.get('message') or '')[:4000]
        clean = {
            'message': message,
            'sender': player.nickname,
            'sender_id': player_id,
            'timestamp': str(data.get('timestamp') or '')[:32],
        }

        if data.get('msg_type') == 'whisper':
            recipient = str(data.get('recipient') or '')[:32]
            clean['msg_type'] = 'whisper'
            clean['recipient'] = recipient
            target = next((p for p in self.game_state.players.values()
                           if p.nickname.lower() == recipient.lower()), None)
            if target and target.id in self.clients:
                self.clients[target.id].send_message(
                    MessageType.CHAT_MESSAGE, clean)
            if player_id in self.clients:
                self.clients[player_id].send_message(
                    MessageType.CHAT_MESSAGE, clean)
        else:
            self.broadcast(MessageType.CHAT_MESSAGE, clean)

    def handle_dice_roll(self, player_id: str, data: dict):
        player = self.game_state.players.get(player_id)
        if not player:
            return
        # Клиент считает бросок сам (как и раньше), но форма сообщения
        # проверяется: в чат попадают только знакомые поля в разумных пределах.
        clean = {
            'dice': str(data.get('dice', ''))[:64],
            'result': self._safe_int(data.get('result'), -9999, 99999),
            'timestamp': str(data.get('timestamp', ''))[:32],
            # Без имени клиент рисовал в чате «None бросает»: сервер
            # отправлял бросок без автора. Имя берём из сессии, а не из
            # присланных данных — подменить его нельзя.
            'player': player.nickname,
        }
        if data.get('mode') in ('adv', 'dis'):
            clean['mode'] = data['mode']
        dc = data.get('dc')
        if dc is not None:
            clean['dc'] = self._safe_int(dc, 0, 999)
            # Успех считаем по итогу и Сл, а не по флагу клиента: иначе
            # игрок мог бы прислать «успех» при броске ниже Сл, и весь
            # стол увидел бы неправду (и зелёную карточку).
            clean['success'] = clean['result'] >= clean['dc']
        for key in ('breakdown', 'note'):
            if data.get(key):
                clean[key] = str(data[key])[:200]
        self.broadcast(MessageType.ROLL_DICE, clean)
        # В журнале — не только итог, но и разбор: кубик может выпасть
        # неверно, и по одной цифре нельзя понять, откуда она.
        line = f"{player.nickname} бросил {clean['dice']}: {clean['result']}"
        if clean.get('breakdown'):
            line += f" ({clean['breakdown']})"
        if clean.get('note'):
            line += f" — {clean['note']}"
        if clean.get('dc') is not None:
            line += (f" против Сл {clean['dc']}: "
                     + ("успех" if clean.get('success') else "провал"))
        self.push_log(line)

    @staticmethod
    def _safe_int(value, lo, hi):
        try:
            return max(lo, min(hi, int(value)))
        except (TypeError, ValueError):
            return 0
