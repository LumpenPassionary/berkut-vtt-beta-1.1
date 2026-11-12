"""
Мир как файл: сохранение сессии и «упаковка/распаковка» в духе fvtt package.

Файл мира — world.json внутри <корень данных>/worlds/<ид_мира>/world.json.
Корень данных берётся из настройки dataPath конфига (cli/config.py).
Домашняя папка — DND_VTT_HOME, либо путь из core.json рядом с игрой,
либо ~/.dnd_vtt (см. shared/app_paths.py).

Что хранится в мире:
  * карты, токены, рисунок по картам, аудиотреки, плейлист, текущая карта;
  * НЕ хранится: игроки и session_id — это данные живой сессии,
    они генерируются заново при загрузке (как в Foundry: мир не помнит,
    кто в нём сидит).

pack/unpack превращают world.json в дерево файлов «по одному документу
на файл» — удобно править руками и держать в git:
    world.json                  — метаданные мира (текущая карта, плейлист)
    maps/<id>.json              — одна карта
    tokens/<id>.json            — один токен
    objects/<id>.json           — один объект карты
    draw_ops/<id_карты>.json    — рисунок одной карты (список операций)
    audio/<id>.json             — один аудиотрек
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
import time

from shared import app_paths
from shared.models import GameState, generate_id

WORLD_FILENAME = "world.json"
FORMAT_VERSION = 1

#: Автосохранения — отдельные файлы, а не перезапись миров. Так ручное
#: сохранение остаётся точкой возврата, а автоматические снимки можно
#: листать и восстанавливать после падения или неудачной правки.
AUTOSAVES_DIRNAME = "autosaves"
AUTOSAVE_VERSION = 1
AUTOSAVE_PREFIX = "autosave-"
AUTOSAVE_KEEP = 10
_AUTOSAVE_NAME_RE = re.compile(
    r"^autosave-\d{8}-\d{6}-\d{6}\.json$")

# Идентификатор мира попадает в путь к файлу, поэтому — только безопасные
# символы: иначе «../../etc» превратился бы в запись куда попало.
_WORLD_ID_RE = re.compile(r"^[A-Za-z0-9._\-А-Яа-я]+$")


def home_dir() -> str:
    """Домашняя папка данных приложения.

    Порядок: ``DND_VTT_HOME`` → ``core.json`` рядом с игрой → ``~/.dnd_vtt``.
    Подробности — в ``shared/app_paths.py``.
    """
    return app_paths.home_dir()


def get_config_path() -> str:
    """Путь к файлу конфигурации CLI (config.json)."""
    return os.path.join(home_dir(), "config.json")


def get_data_root() -> str:
    """Корень данных: dataPath из конфига, иначе домашняя папка."""
    try:
        with open(get_config_path(), encoding="utf-8") as f:
            configured = json.load(f).get("dataPath")
        if configured:
            return os.path.abspath(os.path.expanduser(configured))
    except (OSError, ValueError):
        pass
    return home_dir()


def sanitize_world_id(world_id) -> str:
    """Проверить идентификатор мира: только буквы/цифры/токи и «-»/«_»."""
    wid = str(world_id or "").strip()
    if not wid or not _WORLD_ID_RE.match(wid) or wid in (".", ".."):
        raise ValueError(f"Недопустимый идентификатор мира: {world_id!r}")
    return wid


def world_path(data_root: str, world_id: str) -> str:
    """Путь к файлу мира."""
    return os.path.join(data_root, "worlds",
                        sanitize_world_id(world_id), WORLD_FILENAME)


def list_worlds(data_root: str) -> dict:
    """Все миры в корне данных: {id: {maps, tokens, current_map_id}}."""
    root = os.path.join(data_root, "worlds")
    found = {}
    if not os.path.isdir(root):
        return found
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name, WORLD_FILENAME)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            # Битый мир не прячем: иначе он молча исчезает из списка, и
            # человек не понимает, куда делось сохранение. Показываем
            # строку с пометкой — удалить её всё ещё можно.
            found[name] = {
                "maps": 0, "tokens": 0, "current_map_id": None,
                "path": path, "broken": True, "error": str(exc)[:120],
            }
            continue
        found[name] = {
            "maps": len(data.get("maps", {})),
            "tokens": len(data.get("tokens", {})),
            "current_map_id": data.get("current_map_id"),
            "path": path,
        }
    return found


def delete_world(data_root: str, world_id: str) -> None:
    """Удалить папку сохранения целиком.

    Идентификатор проверяем тем же правилом, что и при записи: иначе через
    него можно было бы выйти за пределы папки миров.
    """
    wid = sanitize_world_id(world_id)
    folder = os.path.join(data_root, "worlds", wid)
    if not os.path.isdir(folder):
        raise ValueError(f"Мир не найден: {wid}")
    shutil.rmtree(folder)


# ------------------------------------------------------------------
# Сохранение и загрузка мира
# ------------------------------------------------------------------

def state_to_world_dict(state: GameState, assets: dict = None) -> dict:
    """Состояние сессии -> содержимое файла мира (без игроков и session_id).

    ``assets`` — описания файлов (id -> {sha256, size, name}). Их нужно
    сохранять вместе с миром: иначе после загрузки сервер не знает, какие
    картинки токенов скачивать, и существа из справочника остаются пустыми
    кружками. Сами байты лежат в кэше ресурсов, здесь только манифест.
    """
    data = state.to_dict()
    data.pop("players", None)
    data.pop("session_id", None)
    if assets:
        data["asset_manifest"] = {
            str(asset_id): dict(meta)
            for asset_id, meta in assets.items()
            if isinstance(meta, dict)
        }
    data["format_version"] = FORMAT_VERSION
    return data


def _atomic_write_json(path: str, payload: dict):
    """Записать JSON через временный файл: читателю не попадёт обрезок."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def save_world(state: GameState, path: str, assets: dict = None) -> str:
    """Записать состояние сессии в файл мира (атомарно)."""
    _atomic_write_json(path, state_to_world_dict(state, assets))
    return path


def save_world_to(state: GameState, data_root: str, world_id: str,
                  assets: dict = None) -> str:
    """Сохранить мир по его идентификатору в корне данных."""
    return save_world(state, world_path(data_root, world_id), assets=assets)


def _read_world_data(path: str) -> dict:
    """Прочитать JSON мира и проверить, что корень — объект."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Корень world.json — не объект")
    return data


def _state_from_world_data(data: dict) -> GameState:
    """Собрать состояние из словаря мира со свежей сессией."""
    data = dict(data)
    data["session_id"] = generate_id()
    data["players"] = {}
    return GameState.from_dict(data)


def _world_asset_manifest(data: dict) -> dict:
    """Манифест картинок из файла мира: только корректные записи."""
    manifest = data.get("asset_manifest") if isinstance(data, dict) else None
    if not isinstance(manifest, dict):
        return {}
    return {str(asset_id): dict(meta) for asset_id, meta in manifest.items()
            if isinstance(meta, dict)}


def load_world(path: str) -> GameState:
    """Прочитать мир из файла.

    Игроки и session_id берутся свежие: мир — это контент, а не сессия.
    """
    return _state_from_world_data(_read_world_data(path))


def load_world_with_assets(path: str):
    """Прочитать мир вместе с манифестом его картинок: (GameState, dict)."""
    data = _read_world_data(path)
    return _state_from_world_data(data), _world_asset_manifest(data)


def load_world_from(data_root: str, world_id: str) -> GameState:
    return load_world(world_path(data_root, world_id))


def load_world_with_assets_from(data_root: str, world_id: str):
    """Загрузить мир и его манифест картинок по идентификатору."""
    return load_world_with_assets(world_path(data_root, world_id))


# ------------------------------------------------------------------
# Автосохранения: отдельная папка, ротация и восстановление
# ------------------------------------------------------------------

def autosaves_dir(data_root: str = None) -> str:
    """Папка автосохранений: <данные>/autosaves."""
    return os.path.join(data_root or get_data_root(), AUTOSAVES_DIRNAME)


def _autosave_path(data_root: str, name: str) -> str:
    """Путь к автосохранению по имени файла.

    Имя принимается только в формате, который создаёт save_autosave:
    иначе через него можно было бы выйти из папки autosaves.
    """
    name = str(name or "").strip()
    if not _AUTOSAVE_NAME_RE.match(name):
        raise ValueError(f"Недопустимое имя автосохранения: {name!r}")
    return os.path.join(autosaves_dir(data_root), name)


def _autosave_name(now: float) -> str:
    """Имя снимка: время с точностью до микросекунды, сортируется строкой."""
    stamp = time.strftime(AUTOSAVE_PREFIX + "%Y%m%d-%H%M%S-",
                          time.localtime(now))
    micro = int((now % 1) * 1_000_000)
    return f"{stamp}{micro:06d}.json"


def _state_hash(state_dict: dict) -> str:
    """Хэш содержимого без времени и мира — чтобы не плодить копии."""
    canonical = json.dumps(state_dict, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _autosave_files(data_root: str = None) -> list:
    """Имена снимков, новые сверху — без чтения JSON.

    Имя содержит время с микросекундами, поэтому строковая сортировка
    совпадает с хронологией. Так таймер сервера не перечитывает все
    снимки целиком ради проверки и ротации.
    """
    root = autosaves_dir(data_root)
    if not os.path.isdir(root):
        return []
    names = [n for n in os.listdir(root) if _AUTOSAVE_NAME_RE.match(n)]
    return sorted(names, reverse=True)


def list_autosaves(data_root: str = None) -> list:
    """Список автосохранений, новые сверху.

    Битый файл не исчезает из списка: его видно и можно удалить, иначе
    человек не понимает, куда делся последний снимок.
    """
    root = autosaves_dir(data_root)
    if not os.path.isdir(root):
        return []
    entries = []
    for name in _autosave_files(data_root):
        path = os.path.join(root, name)
        try:
            mtime = os.path.getmtime(path)
            size = os.path.getsize(path)
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            if not isinstance(doc, dict):
                raise ValueError("корень JSON — не объект")
            state = doc.get("state")
            # Совместимость с ручными снимками, сделанными до появления
            # обёртки: обычный world.json тоже можно положить сюда.
            if not isinstance(state, dict):
                state = doc
            saved_at = doc.get("saved_at")
            try:
                saved_at = float(saved_at)
            except (TypeError, ValueError):
                saved_at = mtime
            entries.append({
                "name": name,
                "path": path,
                "saved_at": saved_at,
                "size": size,
                "world_id": str(doc.get("world_id") or ""),
                "maps": len(state.get("maps") or {}),
                "tokens": len(state.get("tokens") or {}),
                "current_map_id": state.get("current_map_id"),
                "content_hash": str(doc.get("content_hash") or ""),
                "broken": False,
            })
        except (OSError, ValueError, TypeError) as exc:
            entries.append({
                "name": name,
                "path": path,
                "saved_at": 0.0,
                "size": 0,
                "world_id": "",
                "maps": 0,
                "tokens": 0,
                "current_map_id": None,
                "content_hash": "",
                "broken": True,
                "error": str(exc)[:120],
            })
    return sorted(entries, key=lambda item: (item["saved_at"], item["name"]),
                  reverse=True)


def save_autosave(state: GameState, data_root: str = None,
                  world_id: str = None,
                  keep: int = AUTOSAVE_KEEP, assets: dict = None) -> str:
    """Записать отдельное автосохранение по объекту состояния."""
    return save_autosave_dict(state_to_world_dict(state, assets), data_root,
                              world_id, keep)


def save_autosave_dict(state_dict: dict, data_root: str = None,
                       world_id: str = None,
                       keep: int = AUTOSAVE_KEEP) -> str:
    """Записать отдельное автосохранение и удалить старые.

    Принимает уже снятый словарь состояния: сервер делает снимок под своим
    lock, а хэширование и запись на диск — уже без блокировки стола.
    Если содержимое не изменилось с прошлого снимка, файл не создаётся:
    иначе таймер каждую минуту вытеснял бы из ротации действительно разные
    состояния одинаковыми копиями.
    """
    digest = _state_hash(state_dict)
    root = autosaves_dir(data_root)
    names = _autosave_files(data_root)
    if names:
        newest_path = os.path.join(root, names[0])
        try:
            with open(newest_path, encoding="utf-8") as f:
                newest = json.load(f)
            if (isinstance(newest, dict)
                    and newest.get("content_hash") == digest):
                return newest_path
        except (OSError, ValueError, TypeError):
            # Битый последний снимок не должен запрещать новые сохранения:
            # просто пишем следующий файл, а старый останется в списке.
            pass

    now = time.time()
    name = _autosave_name(now)
    path = os.path.join(root, name)
    # Две записи в одну микросекунду теоретически возможны на быстром диске.
    while os.path.exists(path):
        now += 0.000001
        name = _autosave_name(now)
        path = os.path.join(root, name)
    payload = {
        "autosave_version": AUTOSAVE_VERSION,
        "saved_at": now,
        "world_id": str(world_id or ""),
        "content_hash": digest,
        "state": state_dict,
    }
    _atomic_write_json(path, payload)
    prune_autosaves(data_root, keep)
    return path


def read_autosave(data_root: str, name: str) -> dict:
    """Прочитать снимок: состояние, мир и время сохранения."""
    path = _autosave_path(data_root, name)
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, dict):
        raise ValueError("Автосохранение повреждено")
    state_data = doc.get("state")
    if not isinstance(state_data, dict):
        state_data = doc
    state_data = dict(state_data)
    state_data["session_id"] = generate_id()
    state_data["players"] = {}
    asset_manifest = _world_asset_manifest(state_data)
    if not asset_manifest:
        # Совместимость с ручными снимками, где манифест лежал снаружи.
        asset_manifest = _world_asset_manifest(doc)
    try:
        saved_at = float(doc.get("saved_at") or os.path.getmtime(path))
    except (TypeError, ValueError, OSError):
        saved_at = 0.0
    return {
        "state": GameState.from_dict(state_data),
        "world_id": str(doc.get("world_id") or ""),
        "saved_at": saved_at,
        "name": name,
        "path": path,
        "asset_manifest": asset_manifest,
    }


def load_autosave(data_root: str, name: str) -> GameState:
    """Восстановить состояние из автосохранения."""
    return read_autosave(data_root, name)["state"]


def prune_autosaves(data_root: str = None, keep: int = AUTOSAVE_KEEP) -> int:
    """Оставить только `keep` самых новых снимков. Возвращает число удалённых."""
    keep = max(1, int(keep))
    root = autosaves_dir(data_root)
    removed = 0
    for name in _autosave_files(data_root)[keep:]:
        try:
            os.remove(os.path.join(root, name))
            removed += 1
        except OSError:
            pass
    return removed


def delete_autosave(data_root: str, name: str) -> None:
    """Удалить один снимок."""
    path = _autosave_path(data_root, name)
    if not os.path.isfile(path):
        raise ValueError(f"Автосохранение не найдено: {name}")
    os.remove(path)


def latest_autosave(data_root: str = None):
    """Последний снимок или None."""
    entries = list_autosaves(data_root)
    return entries[0] if entries else None


# ------------------------------------------------------------------
# pack / unpack: world.json <-> дерево файлов
# ------------------------------------------------------------------

def unpack_world(world_file: str, dest: str, clean: bool = False,
                 log=None) -> list:
    """Разложить мир на файлы «по одному документу на файл».

    Возвращает список записанных путей.
    """
    raw_world = _read_world_data(world_file)
    state = _state_from_world_data(raw_world)
    asset_manifest = _world_asset_manifest(raw_world)
    if clean and os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest, exist_ok=True)

    written = []

    def emit(rel_path: str, payload):
        path = os.path.join(dest, rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        written.append(path)
        if log:
            log(f"Записан {rel_path}")

    data = state_to_world_dict(state)
    meta = {
        "format_version": data["format_version"],
        "current_map_id": data.get("current_map_id"),
        "music_playlist": data.get("music_playlist", []),
        "current_music_index": data.get("current_music_index", 0),
    }
    if asset_manifest:
        meta["asset_manifest"] = asset_manifest
    emit(WORLD_FILENAME, meta)
    for map_id, m in data.get("maps", {}).items():
        emit(os.path.join("maps", f"{map_id}.json"), m)
    for token_id, t in data.get("tokens", {}).items():
        emit(os.path.join("tokens", f"{token_id}.json"), t)
    for object_id, obj in (data.get("map_objects") or {}).items():
        emit(os.path.join("objects", f"{object_id}.json"), obj)
    for map_id, ops in (data.get("draw_ops") or {}).items():
        if ops:
            emit(os.path.join("draw_ops", f"{map_id}.json"), ops)
    for audio_id, a in data.get("audio_tracks", {}).items():
        emit(os.path.join("audio", f"{audio_id}.json"), a)
    return written


def _read_dir_jsons(folder: str) -> dict:
    """Прочитать все *.json из папки: {имя файла без расширения: объект}."""
    out = {}
    if not os.path.isdir(folder):
        return out
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            out[name[:-len(".json")]] = json.load(f)
    return out


def pack_world(src: str, world_file: str, log=None) -> str:
    """Собрать мир из дерева файлов и записать world.json (атомарно).

    Каждый документ проходит через from_dict — битый файл даёт понятную
    ошибку с именем файла, а не общий traceback.
    """
    meta_path = os.path.join(src, WORLD_FILENAME)
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)

    maps = _read_dir_jsons(os.path.join(src, "maps"))
    tokens = _read_dir_jsons(os.path.join(src, "tokens"))
    objects = _read_dir_jsons(os.path.join(src, "objects"))
    audio = _read_dir_jsons(os.path.join(src, "audio"))
    draw_ops = _read_dir_jsons(os.path.join(src, "draw_ops"))
    asset_manifest = meta.get("asset_manifest")
    if not isinstance(asset_manifest, dict):
        asset_manifest = {}

    # Валидация: каждый документ должен собираться в модель.
    for kind, group in (("карты", maps), ("токены", tokens),
                        ("объекты", objects), ("аудио", audio)):
        for doc_id, doc in group.items():
            try:
                _dict_to_model(kind, doc)
            except Exception as exc:
                raise ValueError(
                    f"Битый файл {kind}/{doc_id}.json: {exc}") from exc

    # Токен без своей карты доедет до клиента, но висеть не на чем.
    for token_id, t in tokens.items():
        map_id = t.get("map_id")
        if map_id and map_id not in maps:
            if log:
                log(f"Предупреждение: токен {token_id} указывает на "
                    f"несуществующую карту {map_id}")

    payload = {
        "format_version": FORMAT_VERSION,
        "session_id": generate_id(),
        "current_map_id": meta.get("current_map_id"),
        "maps": maps,
        "tokens": tokens,
        "map_objects": objects,
        "players": {},
        "draw_ops": {k: v for k, v in draw_ops.items() if v},
        "audio_tracks": audio,
        "music_playlist": meta.get("music_playlist", []),
        "current_music_index": meta.get("current_music_index", 0),
    }
    # Сериализация через модель: на выходе каноничный словарь.
    state = GameState.from_dict(payload)
    _atomic_write_json(world_file,
                       state_to_world_dict(state, assets=asset_manifest))
    if log:
        log(f"Мир записан в {world_file}")
    return world_file


# ------------------------------------------------------------------
# Листы персонажей и карточки актёров: по идентификатору ТОКЕНА
#
# Раньше лист принадлежал игроку (player_id), и при переносе токена на
# другую карту лист к нему не переезжал. Теперь документ лежит в папке
# самого токена: saves/<сессия>/tokens/<token_id>/sheet.json.
# ------------------------------------------------------------------

SAVES_DIRNAME = "saves"


def sanitize_session_name(name) -> str:
    """Имя папки сессии: без запрещённых символов, не пустое."""
    text = str(name or "").strip()
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", text)
    text = text.strip(" .")
    return text[:80] or "autosave"


def saves_dir(data_root: str = None) -> str:
    """Корень сохранений: <данные>/saves."""
    return os.path.join(data_root or get_data_root(), SAVES_DIRNAME)


def session_dir(data_root: str, session: str) -> str:
    """Папка одной сессии: saves/<название игры>/."""
    return os.path.join(saves_dir(data_root), sanitize_session_name(session))


def token_dir(data_root: str, session: str, token_id: str) -> str:
    """Папка токена: saves/<сессия>/tokens/<token_id>/."""
    safe_id = sanitize_world_id(token_id)
    return os.path.join(session_dir(data_root, session), "tokens", safe_id)


def token_sheet_path(data_root: str, session: str, token_id: str) -> str:
    """Файл листа персонажа токена (только для kind=player)."""
    return os.path.join(token_dir(data_root, session, token_id), "sheet.json")


def token_actor_path(data_root: str, session: str, token_id: str) -> str:
    """Файл карточки актёра токена (только для kind=actor)."""
    return os.path.join(token_dir(data_root, session, token_id), "card.json")


def save_token_sheet(data_root: str, session: str, token_id: str,
                     sheet: dict) -> str:
    """Записать лист токена. Возвращает путь к файлу."""
    path = token_sheet_path(data_root, session, token_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic_write_json(path, sheet)
    return path


def load_token_sheet(data_root: str, session: str, token_id: str):
    """Прочитать лист токена; None — если файла нет или он битый."""
    try:
        with open(token_sheet_path(data_root, session, token_id),
                  encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_token_actor(data_root: str, session: str, token_id: str,
                     card: dict) -> str:
    """Записать карточку актёра токена. Возвращает путь к файлу."""
    path = token_actor_path(data_root, session, token_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic_write_json(path, card)
    return path


def load_token_actor(data_root: str, session: str, token_id: str):
    """Прочитать карточку актёра; None — если файла нет или он битый."""
    try:
        with open(token_actor_path(data_root, session, token_id),
                  encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _dict_to_model(kind: str, doc: dict):
    """Проверить документ по его типу (для сообщений об ошибках)."""
    from shared.models import MapData, Token, MapObject, AudioTrack
    model = {"карты": MapData, "токены": Token, "объекты": MapObject,
             "аудио": AudioTrack}[kind]
    return model.from_dict(doc)
