"""
Shared game models for D&D Virtual Tabletop
"""
import math
import uuid
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

from shared.grid import GridConfig, clamp_cells, cell_size_from_cells, cells_from_cell_size
from shared import combat as combat_rules
from shared import vision as vision_rules
from shared import lighting as lighting_rules
from shared import walls as wall_rules


class Role(Enum):
    DM = "dm"
    PLAYER = "player"


#: Освещение карты. Ночь — затемнение поверхности; при включённом модуле
#: свет источников вырезает из него круги (shared/lighting.py). Значения два,
#: без плавных стадий.
TIME_OF_DAY_VALUES = ("day", "night")

#: Пределы подгонки портрета под рамку: увеличение и сдвиг. Границы живут в
#: модели, а не в окне, поэтому мусор из сети или старого мира сводится к
#: тому же диапазону, что видит мастер в окне подгонки.
PORTRAIT_ZOOM_MIN = 0.5
PORTRAIT_ZOOM_MAX = 4.0
PORTRAIT_OFFSET_LIMIT = 1.5
#: Самое длинное имя рамки: ключ — имя файла, а не путь и не абзац текста.
FRAME_KEY_LIMIT = 48


def normalize_frame_key(value) -> str:
    """Ключ рамки токена из сети или старого мира.

    Пустая строка — рамка по умолчанию, ``none`` — без рамки, остальное —
    имя файла из папки рамок. Пути и служебные символы отбрасываются: ключ
    не должен уводить клиент к произвольному файлу.
    """
    text = str(value or "").strip()
    text = text.replace("\\", "/").split("/")[-1]
    text = "".join(ch for ch in text if ch.isprintable()).strip()
    return text[:FRAME_KEY_LIMIT]


def normalize_portrait_zoom(value) -> float:
    """Увеличение портрета: 1.0 — заполняет отверстие рамки."""
    try:
        zoom = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(zoom):
        return 1.0
    return max(PORTRAIT_ZOOM_MIN, min(PORTRAIT_ZOOM_MAX, zoom))


def normalize_portrait_offset(value) -> float:
    """Сдвиг портрета в долях размера токена."""
    try:
        offset = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(offset):
        return 0.0
    return max(-PORTRAIT_OFFSET_LIMIT, min(PORTRAIT_OFFSET_LIMIT, offset))


def normalize_time_of_day(value) -> str:
    """Освещение из сети или старого сейва: незнакомое значение — день.

    Старые миры поля не знают, и молчаливая ночь на каждой карте была бы
    неприятным сюрпризом. Понимаем и русские подписи — на случай, если
    значение когда-нибудь придёт из интерфейса.
    """
    text = str(value or "").strip().lower()
    if text in ("night", "ночь", "dark"):
        return "night"
    return "day"


@dataclass
class Player:
    id: str
    nickname: str
    role: Role
    avatar_path: Optional[str] = None
    connected: bool = True
    
    def to_dict(self):
        return {
            'id': self.id,
            'nickname': self.nickname,
            'role': self.role.value,
            'avatar_path': self.avatar_path,
            'connected': self.connected
        }
    
    @staticmethod
    def from_dict(data):
        return Player(
            id=data['id'],
            nickname=data['nickname'],
            role=Role(data['role']),
            avatar_path=data.get('avatar_path'),
            connected=data.get('connected', True)
        )


@dataclass
class Token:
    id: str
    name: str
    image_path: str
    x: float
    y: float
    size: float = 1.0
    owner_id: Optional[str] = None
    #: Ник владельца на момент назначения. Нужен, чтобы после перезахода
    #: с другим id сервер отдал персонажа тому же человеку, а не первому
    #: подключившемуся. Старые миры поля не знают — это нормально.
    owner_nickname: Optional[str] = None
    visible: bool = True          # глазок на токене: скрыт/показан для всей сессии
    # Карта, которой принадлежит токен. У каждой карты свой набор токенов,
    # поэтому без этого поля на любой карте рисовались бы все токены сразу.
    map_id: Optional[str] = None
    # Бой: хиты и КД (как в D&D 5e). Необязательные поля — старые клиенты
    # и старые миры их не знают, и всё работает.
    hp: Optional[int] = None
    hp_max: Optional[int] = None
    #: Временные хиты (зелье героизма и подобное). Урон сначала съедает их,
    #: а не основные хиты. Необязательное поле — старые миры его не знают.
    hp_temp: int = 0
    ac: Optional[int] = None
    # Инициатива в бою (как в D&D 5e/Foundry): None = в бое не участвует.
    # Необязательное поле — старые клиенты и старые миры его не знают.
    initiative: Optional[int] = None
    # Тип участника сцены: актёр (НПС/существо) или игрок.
    kind: str = "actor"
    # --- Бой на токене (по образцу Active Effects в Foundry) ---
    #: Состояния именами из shared.sheet_items.CONDITIONS_RU — лист и токен
    #: говорят на одном языке, поэтому их можно переносить между ними.
    conditions: List[str] = field(default_factory=list)
    #: Временные эффекты (зелья, масла, заклинания): сопротивление, состояние,
    #: временные хиты. Каждый эффект — словарь с типом, подписью и остатком
    #: раундов; сервер сам снимает истёкшие и возвращает снятые флаги.
    active_effects: List[dict] = field(default_factory=list)
    #: Спасброски от смерти: список отметок "success"/"failure""/"" по
    #: порядку кружков (слева направо). Заполняется, когда хиты упали до нуля.
    #: Смерть и стабилизация — производные от этого списка (shared.combat).
    death_saves: List[str] = field(default_factory=list)
    #: Защита от типов урона. Принимаем и ключи dnd5e ("fire"),
    #: и русские названия ("огонь") — shared.combat сводит их к одному виду.
    resist: List[str] = field(default_factory=list)
    immune: List[str] = field(default_factory=list)
    vulnerable: List[str] = field(default_factory=list)
    #: Модификатор Ловкости для инициативы (d20 + dex).
    #: У монстра приходит из статблока, у персонажа — с листа.
    dex: Optional[int] = None
    # --- Взгляд и зрение (5e: нельзя прятаться от того, кто тебя видит) ---
    #: Направление взгляда в градусах atan2: 0 — вправо (восток), 90 — вниз
    #: (юг), 180 — влево, 270 — вверх. Новый токен всегда смотрит вниз.
    facing: float = vision_rules.DEFAULT_FACING
    #: Угол обзора конусом (по умолчанию 70°) и дальность обзора в футах
    #: (по умолчанию 60). Мастер правит их в карточке актёра, игрок — в листе.
    vision_angle: float = vision_rules.DEFAULT_VISION_ANGLE
    vision_range: float = vision_rules.DEFAULT_VISION_RANGE_FT
    #: Статблок существа: карточка мастера открывается без библиотеки,
    #: а спасброски и сопротивление урону считаются после перезапуска или входа игрока.
    statblock: Optional[dict] = None
    #: Запись справочника, из которой призван токен.
    source_id: Optional[str] = None
    # --- Портрет под рамкой ---
    #: Рамка токена: ключ файла из client/assets/frames. Пусто — рамка по
    #: умолчанию (так приезжают токены старых миров, где поля не было),
    #: ``none`` — без рамки.
    frame: str = ""
    #: Подгонка портрета в отверстии рамки: увеличение и сдвиг. Хранится на
    #: токене, поэтому картинка на карте у всех выглядит одинаково и
    #: переживает перезапуск и переподключение.
    portrait_zoom: float = 1.0
    portrait_dx: float = 0.0
    portrait_dy: float = 0.0

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'image_path': self.image_path,
            'x': self.x,
            'y': self.y,
            'size': self.size,
            'owner_id': self.owner_id,
            'owner_nickname': self.owner_nickname,
            'visible': self.visible,
            'map_id': self.map_id,
            'hp': self.hp,
            'hp_max': self.hp_max,
            'hp_temp': self.hp_temp,
            'ac': self.ac,
            'initiative': self.initiative,
            'kind': self.kind,
            'conditions': list(self.conditions),
            'active_effects': list(self.active_effects),
            'death_saves': list(self.death_saves),
            'resist': list(self.resist),
            'immune': list(self.immune),
            'vulnerable': list(self.vulnerable),
            'dex': self.dex,
            'facing': vision_rules.normalize_facing(self.facing),
            'vision_angle': vision_rules.normalize_angle(self.vision_angle),
            'vision_range': vision_rules.normalize_range(self.vision_range),
            'statblock': self.statblock,
            'source_id': self.source_id,
            'frame': normalize_frame_key(self.frame),
            'portrait_zoom': normalize_portrait_zoom(self.portrait_zoom),
            'portrait_dx': normalize_portrait_offset(self.portrait_dx),
            'portrait_dy': normalize_portrait_offset(self.portrait_dy)
        }
    
    @staticmethod
    def from_dict(data):
        def _opt_int(key):
            value = data.get(key)
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        def _str_list(key):
            """Список строк из чего угодно: старые миры могли прислать
            строку, None или словарь — всё сводим к списку имён."""
            value = data.get(key)
            if value is None:
                return []
            if isinstance(value, str):
                return [value] if value.strip() else []
            if isinstance(value, dict):
                value = list(value.keys())
            if isinstance(value, (list, tuple, set)):
                return [str(v) for v in value if str(v).strip()]
            return []

        def _effects(key):
            """Только словари с типом: мусор из старого мира не роняет бой."""
            value = data.get(key)
            if not isinstance(value, (list, tuple)):
                return []
            result = []
            for entry in value:
                if not isinstance(entry, dict):
                    continue
                kind = str(entry.get("type") or "").strip()
                if not kind:
                    continue
                clean = dict(entry)
                clean["type"] = kind
                result.append(clean)
            return result

        # Хиты ниже нуля остались в старых мирах: на нуле персонаж умирает,
        # а не уходит в минус, поэтому отрицательное значение поднимаем к 0.
        hp_value = _opt_int('hp')
        if hp_value is not None and hp_value < 0:
            hp_value = 0

        return Token(
            id=data['id'],
            name=data['name'],
            image_path=data['image_path'],
            x=data['x'],
            y=data['y'],
            size=data.get('size', 1.0),
            owner_id=data.get('owner_id'),
            owner_nickname=data.get('owner_nickname'),
            visible=data.get('visible', True),
            map_id=data.get('map_id'),
            hp=hp_value,
            hp_max=_opt_int('hp_max'),
            hp_temp=max(0, _opt_int('hp_temp') or 0),
            ac=_opt_int('ac'),
            initiative=_opt_int('initiative'),
            kind=data.get('kind', 'actor') if data.get('kind') in ('actor', 'player') else 'actor',
            conditions=_str_list('conditions'),
            active_effects=_effects('active_effects'),
            death_saves=combat_rules.normalize_death_saves(
                data.get('death_saves')),
            resist=_str_list('resist'),
            immune=_str_list('immune'),
            vulnerable=_str_list('vulnerable'),
            dex=_opt_int('dex'),
            # Старый мир полей взгляда не знает — тогда токен смотрит вниз
            # с обычным углом 70° и дальностью 60 футов.
            facing=vision_rules.normalize_facing(data.get('facing')),
            vision_angle=vision_rules.normalize_angle(
                data.get('vision_angle')),
            vision_range=vision_rules.normalize_range(
                data.get('vision_range')),
            statblock=data.get('statblock') if isinstance(data.get('statblock'), dict) else None,
            source_id=data.get('source_id'),
            # Старый мир полей портрета не знает: пустая рамка означает
            # рамку по умолчанию, а увеличение 1.0 — прежний вид картинки.
            frame=normalize_frame_key(data.get('frame')),
            portrait_zoom=normalize_portrait_zoom(data.get('portrait_zoom')),
            portrait_dx=normalize_portrait_offset(data.get('portrait_dx')),
            portrait_dy=normalize_portrait_offset(data.get('portrait_dy'))
        )


@dataclass
class MapData:
    id: str
    name: str
    image_path: str
    width: int = 1000
    height: int = 1000
    # Сетка задана ЧИСЛОМ клеток по каждой стороне картинки, как в Dungeondraft.
    # Размер клетки не хранится: он вычисляется как размер / число клеток.
    grid_cols: int = 0
    grid_rows: int = 0
    grid_visible: bool = True
    # Размер картинки в пикселях. Нужен, чтобы перевести клетки в мировые единицы.
    img_width: float = 0.0
    img_height: float = 0.0
    # Положение и размер карты в мировых координатах (ручное масштабирование за углы)
    pos_x: float = 0.0
    pos_y: float = 0.0
    rotation: float = 0.0
    #: Освещение карты: "day" или "night". Ночь затемняет карту, сетку,
    #: рисунок и надписи; токены и объекты поверх тел остаются читаемыми.
    time_of_day: str = "day"
    #: Цвет и плотность ночного затемнения: мастер может сделать ночь
    #: красной, зелёной или совсем прозрачной. Плотность — проценты.
    night_color: str = lighting_rules.DEFAULT_NIGHT_COLOR
    night_alpha: int = lighting_rules.DEFAULT_NIGHT_ALPHA
    #: Туман войны: плотность отдельно для неизведанных комнат и для мира.
    #: Проценты непрозрачности: 100 — совсем темно ("комнаты" по умолчанию),
    #: 0 — видно как есть. Живёт на карте, как и ночь: едет вместе с миром.
    fog_room_alpha: int = lighting_rules.DEFAULT_FOG_ROOM_ALPHA
    fog_world_alpha: int = lighting_rules.DEFAULT_FOG_WORLD_ALPHA
    #: Вид тумана: ч/б, сепия, цветной или свой цвет мастера. Цвет — такая
    #: же настройка карты, как плотность, и едет вместе с миром.
    fog_style: str = lighting_rules.DEFAULT_FOG_STYLE
    fog_color: str = lighting_rules.DEFAULT_FOG_COLOR
    #: Разведанные комнаты: подписи (рамка и площадь) комнат, где стол уже
    #: побывал. Туман комнат их не заливает чёрным. Живёт на карте, а не в
    #: настройках клиента, поэтому едет вместе с миром и после перезапуска
    #: не закрывается заново (shared/walls.normalize_explored_rooms).
    explored_rooms: List[list] = field(default_factory=list)
    #: Источники света на карте: точка, радиус в футах, цвет, включённость.
    #: Ночью они вырезают из темноты освещённые круги (shared/lighting.py).
    lights: List[dict] = field(default_factory=list)
    #: Стены карты: ломаные в мировых координатах. Они заслоняют свет и
    #: взгляд, а токен сквозь них не проходит (shared/walls.py).
    walls: List[dict] = field(default_factory=list)
    #: Проёмы стен: окно пропускает свет и взгляд, дверь — ещё и ход.
    openings: List[dict] = field(default_factory=list)
    #: Магнит вершин стен и шаг привязки в долях клетки (1.0 или 0.5).
    #: Живёт на карте, а не в настройках клиента, чтобы ехать вместе с миром.
    wall_snap: bool = False
    wall_snap_step: float = wall_rules.DEFAULT_SNAP_STEP

    @property
    def grid(self) -> GridConfig:
        """Сетка карты: число клеток по сторонам."""
        return GridConfig(cols=int(self.grid_cols), rows=int(self.grid_rows),
                          visible=bool(self.grid_visible))

    def apply_grid(self, grid: GridConfig):
        """Записать сетку из объекта GridConfig."""
        self.grid_cols = clamp_cells(grid.cols)
        self.grid_rows = clamp_cells(grid.rows)
        self.grid_visible = bool(grid.visible)

    @property
    def grid_cell_world(self) -> Tuple[float, float]:
        """Размер клетки в мировых единицах: растянутая карта даёт растянутую клетку."""
        return (cell_size_from_cells(self.width, self.grid_cols),
                cell_size_from_cells(self.height, self.grid_rows))

    @property
    def right(self) -> float:
        return self.pos_x + self.width

    @property
    def bottom(self) -> float:
        return self.pos_y + self.height

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'image_path': self.image_path,
            'width': self.width,
            'height': self.height,
            'grid_cols': self.grid_cols,
            'grid_rows': self.grid_rows,
            'grid_visible': self.grid_visible,
            'img_width': self.img_width,
            'img_height': self.img_height,
            'pos_x': self.pos_x,
            'pos_y': self.pos_y,
            'rotation': self.rotation,
            'time_of_day': normalize_time_of_day(self.time_of_day),
            'night_color': lighting_rules.normalize_night_color(
                self.night_color),
            'night_alpha': lighting_rules.normalize_night_alpha(
                self.night_alpha),
            'fog_room_alpha': lighting_rules.normalize_fog_room_alpha(
                self.fog_room_alpha),
            'fog_world_alpha': lighting_rules.normalize_fog_world_alpha(
                self.fog_world_alpha),
            'fog_style': lighting_rules.normalize_fog_style(self.fog_style),
            'fog_color': lighting_rules.normalize_fog_color(self.fog_color),
            'explored_rooms': wall_rules.normalize_explored_rooms(
                self.explored_rooms),
            'lights': lighting_rules.normalize_lights(self.lights),
            'walls': wall_rules.normalize_walls(self.walls),
            'openings': wall_rules.normalize_openings(self.openings,
                                                      self.walls),
            'wall_snap': bool(self.wall_snap),
            'wall_snap_step': wall_rules.normalize_snap_step(
                self.wall_snap_step),
        }

    @staticmethod
    def from_dict(data):
        # Старые сохранения хранили шаг в пикселях (grid_size). Переводим его
        # в число клеток, если известен размер картинки.
        cols = clamp_cells(data.get('grid_cols', 0))
        rows = clamp_cells(data.get('grid_rows', 0))
        iw = float(data.get('img_width', 0.0) or 0.0)
        ih = float(data.get('img_height', 0.0) or 0.0)
        legacy = data.get('grid_size')
        if (not cols or not rows) and legacy and iw > 0 and ih > 0:
            try:
                step = float(legacy)
            except (TypeError, ValueError):
                step = 0.0
            if step > 0:
                cols = cols or cells_from_cell_size(iw, step)
                rows = rows or cells_from_cell_size(ih, step)
        return MapData(
            id=data['id'],
            name=data['name'],
            image_path=data['image_path'],
            width=data.get('width', 1000),
            height=data.get('height', 1000),
            grid_cols=cols,
            grid_rows=rows,
            grid_visible=data.get('grid_visible', True),
            img_width=data.get('img_width', 0.0),
            img_height=data.get('img_height', 0.0),
            pos_x=data.get('pos_x', 0.0),
            pos_y=data.get('pos_y', 0.0),
            rotation=data.get('rotation', 0.0),
            time_of_day=normalize_time_of_day(data.get('time_of_day')),
            night_color=lighting_rules.normalize_night_color(
                data.get('night_color')),
            night_alpha=lighting_rules.normalize_night_alpha(
                data.get('night_alpha')),
            fog_room_alpha=lighting_rules.normalize_fog_room_alpha(
                data.get('fog_room_alpha')),
            fog_world_alpha=lighting_rules.normalize_fog_world_alpha(
                data.get('fog_world_alpha')),
            fog_style=lighting_rules.normalize_fog_style(
                data.get('fog_style')),
            fog_color=lighting_rules.normalize_fog_color(
                data.get('fog_color')),
            explored_rooms=wall_rules.normalize_explored_rooms(
                data.get('explored_rooms')),
            lights=lighting_rules.normalize_lights(data.get('lights')),
            walls=wall_rules.normalize_walls(data.get('walls')),
            openings=wall_rules.normalize_openings(
                data.get('openings'), data.get('walls')),
            wall_snap=bool(data.get('wall_snap', False)),
            wall_snap_step=wall_rules.normalize_snap_step(
                data.get('wall_snap_step'))
        )


@dataclass
class MapObject:
    """Объект на карте: PNG поверх карты, но под или над токенами.

    Это не токен и не растр: картинка остаётся отдельным слоем, её можно
    таскать, масштабировать за углы, делать прозрачной и скрывать. Крыша
    (``roof``) плавно исчезает, когда под неё заходит токен ИГРОКА.
    """
    id: str
    map_id: Optional[str]
    name: str
    image_path: str
    x: float
    y: float
    width: float
    height: float
    #: Непрозрачность в процентах (10..100).
    opacity: int = 100
    visible: bool = True
    #: True — объект рисуется поверх токенов, False — под ними.
    above_tokens: bool = True
    #: Крыша: скрывается, пока под ней стоит токен игрока (не актёра).
    roof: bool = False
    #: Замок: объект нельзя выбрать и изменить мышью.
    locked: bool = False
    #: Порядок внутри своего слоя: больше — выше. Новый объект — сверху.
    z: int = 0
    #: Поворот вокруг центра в градусах (0…360).
    rotation: float = 0.0

    def to_dict(self):
        return {
            'id': self.id,
            'map_id': self.map_id,
            'name': self.name,
            'image_path': self.image_path,
            'x': self.x,
            'y': self.y,
            'width': self.width,
            'height': self.height,
            'opacity': self.opacity,
            'visible': self.visible,
            'above_tokens': self.above_tokens,
            'roof': self.roof,
            'locked': self.locked,
            'z': self.z,
            'rotation': self.rotation,
        }

    @staticmethod
    def from_dict(data):
        def _number(key, default, low=None, high=None):
            try:
                value = float(data.get(key, default))
            except (TypeError, ValueError):
                value = float(default)
            if value != value or value in (float('inf'), float('-inf')):
                value = float(default)
            if low is not None:
                value = max(low, value)
            if high is not None:
                value = min(high, value)
            return value

        return MapObject(
            id=str(data['id']),
            map_id=(str(data['map_id']) if data.get('map_id') is not None
                    else None),
            name=str(data.get('name') or 'Объект')[:120],
            image_path=str(data.get('image_path') or ''),
            x=_number('x', 0.0, -1_000_000.0, 1_000_000.0),
            y=_number('y', 0.0, -1_000_000.0, 1_000_000.0),
            width=_number('width', 100.0, 10.0, 200_000.0),
            height=_number('height', 100.0, 10.0, 200_000.0),
            opacity=int(_number('opacity', 100, 10, 100)),
            visible=bool(data.get('visible', True)),
            above_tokens=bool(data.get('above_tokens', True)),
            roof=bool(data.get('roof', False)),
            locked=bool(data.get('locked', False)),
            z=int(_number('z', 0, -1_000_000, 1_000_000)),
            rotation=_number('rotation', 0.0) % 360.0,
        )


@dataclass
class AudioTrack:
    id: str
    name: str
    path: str
    type: str  # music or sound
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'path': self.path,
            'type': self.type
        }
    
    @staticmethod
    def from_dict(data):
        return AudioTrack(
            id=data['id'],
            name=data['name'],
            path=data['path'],
            type=data['type']
        )


@dataclass
class ChatMessage:
    sender: str
    message: str
    timestamp: str
    type: str = "normal"  # normal, whisper, system, roll
    recipient: Optional[str] = None
    
    def to_dict(self):
        return {
            'sender': self.sender,
            'message': self.message,
            'timestamp': self.timestamp,
            'type': self.type,
            'recipient': self.recipient
        }
    
    @staticmethod
    def from_dict(data):
        return ChatMessage(
            sender=data['sender'],
            message=data['message'],
            timestamp=data['timestamp'],
            type=data.get('type', 'normal'),
            recipient=data.get('recipient')
        )


@dataclass
class GameState:
    session_id: str
    current_map_id: Optional[str] = None
    maps: Dict[str, MapData] = field(default_factory=dict)
    tokens: Dict[str, Token] = field(default_factory=dict)
    # Объекты карт: PNG-слои (крыши, деревья, декор), отдельно от токенов.
    map_objects: Dict[str, MapObject] = field(default_factory=dict)
    players: Dict[str, Player] = field(default_factory=dict)
    # Растровый слой рисунка: карта -> список операций (иначе новый игрок
    # не увидит уже нарисованное). Каждая операция — простой словарь.
    draw_ops: Dict[str, List[dict]] = field(default_factory=dict)
    #: Текстовые надписи на картах: карта -> список объектов. В отличие от
    #: штрихов это не растр: надпись можно выбрать, перетащить, поправить и
    #: удалить, не стирая рисунок вокруг.
    map_texts: Dict[str, List[dict]] = field(default_factory=dict)
    audio_tracks: Dict[str, AudioTrack] = field(default_factory=dict)
    music_playlist: List[str] = field(default_factory=list)
    current_music_index: int = 0
    # Бой: очередь ходов по инициативе. combat_order — id токенов по
    # убыванию инициативы, combat_turn — индекс текущего хода,
    # combat_round — номер раунда. combat_active=False — бой не идёт.
    combat_active: bool = False
    combat_order: List[str] = field(default_factory=list)
    combat_turn: int = 0
    combat_round: int = 1
    # Листы персонажей: id игрока -> лист в формате Long Story Short.
    # Сервер хранит их, чтобы мастер видел инвентарь и заклинания игроков и
    # мог выдать предмет. Без этого лист жил бы только у одного клиента.
    sheets: Dict[str, dict] = field(default_factory=dict)
    # Листы по идентификатору ТОКЕНА: основа новой модели, где лист
    # принадлежит токену и переезжает с ним между картами. sheets
    # (по player_id) остаётся для совместимости и выдачи предметов.
    token_sheets: Dict[str, dict] = field(default_factory=dict)
    # Слоты игроков: id слота -> {name, player_id, token_ids}. Мастер создаёт
    # слоты заранее, игрок занимает свободный при входе. Слот живёт в сессии,
    # а не в мире: это место за столом, а не содержимое карты.
    slots: Dict[str, dict] = field(default_factory=dict)

    def to_dict(self):
        return {
            'session_id': self.session_id,
            'current_map_id': self.current_map_id,
            'maps': {k: v.to_dict() for k, v in self.maps.items()},
            'tokens': {k: v.to_dict() for k, v in self.tokens.items()},
            'map_objects': {k: v.to_dict() for k, v in self.map_objects.items()},
            'players': {k: v.to_dict() for k, v in self.players.items()},
            'draw_ops': self.draw_ops,
            'map_texts': self.map_texts,
            'audio_tracks': {k: v.to_dict() for k, v in self.audio_tracks.items()},
            'music_playlist': self.music_playlist,
            'current_music_index': self.current_music_index,
            'combat_active': self.combat_active,
            'combat_order': list(self.combat_order),
            'combat_turn': self.combat_turn,
            'combat_round': self.combat_round,
            'sheets': self.sheets,
            'token_sheets': self.token_sheets,
            'slots': self.slots,
        }
    
    @staticmethod
    def from_dict(data):
        return GameState(
            session_id=data['session_id'],
            current_map_id=data.get('current_map_id'),
            maps={k: MapData.from_dict(v) for k, v in data.get('maps', {}).items()},
            tokens={k: Token.from_dict(v) for k, v in data.get('tokens', {}).items()},
            map_objects={k: MapObject.from_dict(v)
                         for k, v in (data.get('map_objects') or {}).items()},
            players={k: Player.from_dict(v) for k, v in data.get('players', {}).items()},
            draw_ops=data.get('draw_ops', {}) or {},
            map_texts=data.get('map_texts', {}) or {},
            audio_tracks={k: AudioTrack.from_dict(v) for k, v in data.get('audio_tracks', {}).items()},
            music_playlist=data.get('music_playlist', []),
            current_music_index=data.get('current_music_index', 0),
            combat_active=bool(data.get('combat_active', False)),
            combat_order=[str(t) for t in data.get('combat_order', []) or []],
            combat_turn=_safe_int(data.get('combat_turn'), 0),
            combat_round=_safe_int(data.get('combat_round'), 1),
            sheets=data.get('sheets', {}) or {},
            token_sheets=data.get('token_sheets', {}) or {},
            slots=data.get('slots', {}) or {}
        )


def generate_id():
    """Generate unique ID"""
    return str(uuid.uuid4())


def _safe_int(value, default: int) -> int:
    """Целое из ненадёжных данных (старые миры лежат в JSON)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
