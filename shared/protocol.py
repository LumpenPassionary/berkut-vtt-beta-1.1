"""
Сетевой протокол виртуального стола D&D.

Формат сообщения: 8 байт длины (big-endian) + JSON в UTF-8.
По этому каналу идут только команды и состояние: карты, токены, чат, броски.
Файлы ресурсов передаются отдельным соединением (shared.asset_transport),
поэтому размер сообщения здесь невелик.
"""
import json
from enum import Enum


class MessageType(Enum):
    # Соединение и лобби
    ASSET_TRANSFER = "asset_transfer"
    ASSET_READY = "asset_ready"
    CONNECT = "connect"
    DISCONNECT = "disconnect"
    PLAYER_JOINED = "player_joined"
    PLAYER_LEFT = "player_left"
    START_GAME = "start_game"
    # Слоты игроков: мастер создаёт их до и во время игры, игрок занимает
    # свободный. Слоту можно заранее выдать токены персонажей.
    SLOTS_STATE = "slots_state"    # сервер -> все: полный список слотов
    ADD_SLOT = "add_slot"          # мастер -> сервер
    DELETE_SLOT = "delete_slot"    # мастер -> сервер
    CLAIM_SLOT = "claim_slot"      # игрок -> сервер: занять свободный
    ASSIGN_SLOT_TOKEN = "assign_slot_token"   # мастер: токен -> слот
    RELEASE_SLOT = "release_slot"  # игрок: освободить свой слот

    # Состояние игры
    FULL_STATE = "full_state"
    # Файлы миров: ДМ сохраняет сессию в файл и загружает обратно
    SAVE_WORLD = "save_world"
    LOAD_WORLD = "load_world"
    # Отдельные автосохранения: ДМ выбирает снимок и восстанавливает его.
    # Список читается с диска хоста, сюда идёт только команда восстановления.
    RESTORE_AUTOSAVE = "restore_autosave"

    # Карты
    ADD_MAP = "add_map"
    REMOVE_MAP = "remove_map"
    CHANGE_MAP = "change_map"
    UPDATE_MAP = "update_map"
    UPDATE_GRID = "update_grid"
    # Освещение карты: день или ночь. Ночь видна всем участникам стола.
    UPDATE_TIME = "update_time"
    # Источники света на карте: мастер добавляет, двигает и удаляет их.
    # Приходит полный список — так не бывает «потерянного» источника.
    UPDATE_LIGHTS = "update_lights"
    # Объекты карты: PNG-слои (крыши, деревья, декор) над или под токенами
    ADD_MAP_OBJECT = "add_map_object"
    UPDATE_MAP_OBJECT = "update_map_object"
    REMOVE_MAP_OBJECT = "remove_map_object"
    # Стены карты: мастер расставляет, сервер хранит и рассылает всем.
    # Приходит полный список — как и у света, чтобы не было «потерянной»
    # стены из-за пропущенного сообщения.
    UPDATE_WALLS = "update_walls"
    # Дверь открыли или закрыли: самим ходом токена или кликом мастера.
    # Отдельное сообщение, потому что двери хлопают посреди пачки движений
    # и должны звучать у всех, кто у стола.
    TOGGLE_DOOR = "toggle_door"
    # Запереть или отпереть дверь: только мастер. Запертая дверь не
    # открывается ни токеном, ни кликом, пока её не отопрут.
    SET_DOOR_LOCK = "set_door_lock"
    # Эхо замка: сервер -> все, чтобы и игроки знали, что дверь заперта.
    DOOR_LOCK = "door_lock"
    # Привязка токенов к клеткам: настройка стола, а не клиента. Мастер
    # включает её у себя — токены у всех ходят по клеткам.
    SET_SNAP = "set_snap"
    # Эхо привязки: сервер -> все, чтобы игроки ходили так же, как мастер.
    SNAP = "snap"
    # Зрение игроков: «поле зрения» или «вокруг токена» — настройка мастера
    # для всего стола, поэтому она тоже живёт на сервере.
    SET_VISION = "set_vision"
    VISION = "vision"
    # Разведанные комнаты: клиент сообщает, куда вошёл, а сервер объединяет
    # это с картой и рассылает всем. Разведка живёт на карте, поэтому едет
    # с миром и не закрывается заново после перезапуска.
    MARK_EXPLORED = "mark_explored"
    # Эхо разведки: сервер -> все, полный список комнат карты.
    EXPLORED = "explored"

    # Токены
    ADD_TOKEN = "add_token"
    # Копия токена (Ctrl+C / Ctrl+V). Мастер копирует любой токен,
    # игрок — только своего персонажа. Сервер сам создаёт новый id
    # и переносит лист, поэтому подделать чужие хиты нельзя.
    COPY_TOKEN = "copy_token"
    REMOVE_TOKEN = "remove_token"
    MOVE_TOKEN = "move_token"
    # Перенос токена НА ДРУГУЮ карту (мастер). Не копия: у персонажа
    # остаются тот же лист и те же хиты, а фигурка просто меняет сцену.
    MOVE_TOKEN_MAP = "move_token_map"
    # Бюджет движения в бою — на сервере: рывок и зелье скорости
    # увеличивают его, а MOVE_TOKEN сверх остатка отклоняется.
    DASH = "dash"
    SPEED_POTION = "speed_potion"
    MOVEMENT_STATE = "movement_state"   # сервер -> все: потрачено/всего
    UPDATE_TOKEN = "update_token"
    # Глазок на токене: скрыть/показать для всей сессии
    TOGGLE_TOKEN_VISIBILITY = "toggle_token_visibility"
    # Урон и лечение токена: мастер любому, игрок — своему
    APPLY_DAMAGE = "apply_damage"
    # Длительный эффект зелья/масла: сопротивление, состояние, временные
    # хиты, ускорение. Сервер сам проверяет тип, длительность и права,
    # снимает истёкшие эффекты на смене раунда.
    APPLY_EFFECT = "apply_effect"
    # Прямое заклинание по одной цели (Волшебная стрела, Лечащее слово,
    # Лечение ран): клиент называет заклинание и цель, сервер находит его
    # в листе, сам бросает кубы и меняет хиты. Так снаряды Волшебной стрелы
    # нельзя подменить, а Щит цели действительно их отражает.
    CAST_SPELL = "cast_spell"          # клиент -> сервер
    SPELL_RESULT = "spell_result"      # сервер -> все: снаряды, лечение, хиты

    # Бой: инициатива и очередь ходов (ДМ управляет, сервер рассылает)
    SET_INITIATIVE = "set_initiative"     # выставить/сбросить инициативу
    START_COMBAT = "start_combat"         # начать бой, построить очередь
    END_COMBAT = "end_combat"             # закончить бой
    NEXT_TURN = "next_turn"               # следующий/предыдущий ход
    INITIATIVE_UPDATE = "initiative_update"  # сервер -> все: состояние боя


    # Растровый рисунок (кисть/круг/квадрат/ластик)
    DRAW_OP = "draw_op"
    CLEAR_DRAWINGS = "clear_drawings"
    UNDO_DRAWING = "undo_drawing"

    # Текстовые надписи на карте: отдельные объекты, а не штрихи — их
    # можно выбрать, перетащить, поправить и удалить по одной.
    ADD_MAP_TEXT = "add_map_text"          # клиент -> сервер
    UPDATE_MAP_TEXT = "update_map_text"    # клиент -> сервер: правка/перенос
    REMOVE_MAP_TEXT = "remove_map_text"    # клиент -> сервер
    MAP_TEXT_ADDED = "map_text_added"      # сервер -> все
    MAP_TEXT_UPDATED = "map_text_updated"  # сервер -> все
    MAP_TEXT_REMOVED = "map_text_removed"  # сервер -> все

    # Аудио
    PLAY_SOUND = "play_sound"
    PLAY_MUSIC = "play_music"
    ADD_AUDIO = "add_audio"

    # Чат и кубики
    CHAT_MESSAGE = "chat_message"
    ROLL_DICE = "roll_dice"

    # Листы персонажей: игрок держит лист у себя, мастер видит и правит
    SHEET_UPDATE = "sheet_update"     # клиент -> сервер: мой лист изменился
    SHEET_STATE = "sheet_state"       # сервер -> все: полные листы
    GRANT_ITEM = "grant_item"         # ДМ -> игроку: выдать предмет
    GRANT_SPELL = "grant_spell"       # ДМ -> игроку: выдать заклинание
    # Отдых всего стола — только мастер: хиты, ячейки, заряды особенностей
    # и предметов. Игроки не отдыхают поодиночке: это событие стола.
    REST = "rest"                     # ДМ -> сервер: {"kind": "long"|"short"}
    REST_RESULT = "rest_result"       # сервер -> все: кто и сколько восстановил

    # Атака токена по токену: клиент присылает "чем и по кому", сервер
    # бросает d20 против КД цели, считает урон с сопротивлением и меняет
    # хиты. Бросок на сервере — чтобы результат нельзя было подменить клиентом.
    ROLL_ATTACK = "roll_attack"       # клиент -> сервер
    ATTACK_RESULT = "attack_result"   # сервер -> все: попадание, урон, новые хиты
    # Область заклинания: клиент подтвердил шаблон, сервер сам решает, кто
    # попал, бросает спасброски и урон. Клиент не может подменить список целей.
    AREA_DAMAGE = "area_damage"       # клиент -> сервер
    AREA_RESULT = "area_result"       # сервер -> все: клетки, цели, урон
    # Спасбросок токена: бонус берётся с листа персонажа или
    # из статблока существа, а не из полей диалога.
    ROLL_SAVE = "roll_save"           # клиент -> сервер: бросить спасбросок по цели
    SAVE_RESULT = "save_result"       # сервер -> все
    # Бросок инициативы: d20 + Ловкость токена (лист/статблок)
    ROLL_INITIATIVE = "roll_initiative"
    # Спасброски от смерти: бросок d20 на сервере, ручная отметка кружка,
    # воскрешение мастером. Результат одинаков у всех участников.
    DEATH_SAVE = "death_save"        # клиент -> сервер
    DEATH_SAVE_RESULT = "death_save_result"   # сервер -> все
    # Состояния на токене (Отравлен, Сбит с ног…)
    SET_CONDITIONS = "set_conditions"
    # Скрытность: клиент просит проверить Скрытность, сервер бросает d20,
    # сравнивает с пассивным Восприятием наблюдателей и ставит/снимает
    # состояние «Скрыт». Бросок на сервере — как и у атаки.
    STEALTH_CHECK = "stealth_check"     # клиент -> сервер
    STEALTH_RESULT = "stealth_result"   # сервер -> все
    # Особая атака существа (дыхание Бехира, проглатывание, ужас): клиент
    # выбирает цель, а спасброски бросает сервер — за цели, а не за монстра.
    SPECIAL_ATTACK = "special_attack"        # клиент -> сервер
    SPECIAL_ATTACK_RESULT = "special_attack_result"   # сервер -> все
    # Вернуть хиты и отметки смерти к снимку: отмена (и повтор) атаки.
    # Снимок присылает клиент, сервер проверяет права и рассылает результат,
    # поэтому игрок тоже может отменить свой удар, хотя UPDATE_TOKEN — ДМ.
    RESTORE_TOKEN = "restore_token"
    # Шаблон области (AoE) — только для себя: другим участникам не шлём

    # Библиотека мастера «Мои актёры»: предзагрузка игрокам
    MY_ACTORS_UPDATE = "my_actors_update"   # ДМ -> сервер -> остальные
    MY_ACTORS_REMOVE = "my_actors_remove"
    MY_ACTORS_CLEAR = "my_actors_clear"

    # Служебные
    ERROR = "error"
    PERMISSION_DENIED = "permission_denied"
    LOG_ENTRY = "log_entry"


class Protocol:
    """Кодирование и декодирование сообщений"""

    HEADER_SIZE = 8
    # Потолок на одно сообщение. Файлы едут другим каналом, так что предел
    # защищает от мусора в заголовке, а не обслуживает передачу карт.
    MAX_MESSAGE_SIZE = 128 * 1024 * 1024
    # Отдельный, куда более низкий потолок для команд КЛИЕНТ → СЕРВЕР:
    # клиент присылает только состояние стола (лист, токены, чат, рисунок),
    # и 4 МБ хватает с запасом. Всё большее — ошибка сборки или попытка
    # занять память сервера одним заголовком. Сервер отвечает большими
    # кадрами (FULL_STATE), поэтому для исходящих остаётся MAX_MESSAGE_SIZE.
    MAX_CLIENT_MESSAGE_SIZE = 4 * 1024 * 1024

    @staticmethod
    def encode_message(msg_type: MessageType, data: dict) -> bytes:
        message = {'type': msg_type.value, 'data': data}
        payload = json.dumps(message, ensure_ascii=False).encode('utf-8')
        if len(payload) > Protocol.MAX_MESSAGE_SIZE:
            raise ValueError('Сообщение слишком большое для протокола')
        return len(payload).to_bytes(Protocol.HEADER_SIZE, 'big') + payload

    @staticmethod
    def decode_message(payload: bytes):
        message = json.loads(payload.decode('utf-8'))
        return MessageType(message['type']), message.get('data', {})
