"""Данные листа Long Story Short (LSS): разбор событий и документа.

Отдельного окна у листа больше нет: лист живёт во вкладке «Лист персонажа»
левой панели (client/sheet_pages.py). Кнопка «LSS лист» на странице токена
загружает файл листа, и сам лист показывается прямо во вкладке — без
окна-пульта и без подсказки, которые мешали игрокам и мастеру.

Почему не встроить сайт целиком: официальное встраивание (iframe +
postMessage, см. longstoryshort.app/doc/developers/embedding/) ограничено
белым списком доменов в CSP ``frame-ancestors``, localhost туда не входит;
QtWebEngine 5.15 — это Chromium 83, на нём бандл листа падает с SyntaxError.
Поэтому разбор данных отделён от показа: модуль чистый, без Qt и без сети.

Что умеют события листа (протокол SDK v2, ловушка — client/lss_host.py):

  * ``lss:roll``   → бросок уходит в чат всего стола (MessageType.ROLL_DICE);
  * ``dnd:health`` → хиты переносятся в лист персонажа и на токен игрока;
  * документ персонажа (ответы fetch/XHR листа) → «теневой лист» приложения:
    характеристики, КД, навыки и описание для бросков и карточки.

Функции разбора сохранены: ими пользуются логика токен-листов
(main_window.on_lss_document) и тесты. ``panel_available()`` сообщает,
установлен ли необязательный pywebview — он понадобится, если живой лист
сайта вернём внутрь страницы.
"""
import copy
import json
import os
import re

#: Точка входа листа (список персонажей — пользователь выбирает сам).
SHEET_URL = "https://longstoryshort.app/iframe/characters/list/"

#: Ключ и версия конверта SDK — чужой postMessage молча игнорируется.
MARKER = "__lssSheetSdk"
VERSION = 2

#: Корень проекта: из него запускается `python -m client.lss_host`.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def panel_available() -> bool:
    """Есть ли необязательный pywebview (WebView2) для панели LSS.

    Проверяем наличие пакета, не импортируя его: импорт тянет GUI-стек
    и нужен только в момент реального открытия панели.
    """
    from importlib.util import find_spec
    try:
        return find_spec("webview") is not None
    except (ImportError, ValueError):
        # Сломанный метаданные/пустая строка в sys.path — пакета нет.
        return False


#: Имя из старой панели на QtWebEngine: main_window проверяет его.
webengine_available = panel_available


def parse_roll(payload) -> dict:
    """DiceRollPayload из LSS → словарь для MessageType.ROLL_DICE.

    Возвращает None, если в total нет числа (сервер принимает только int).
    total у LSS — строка: «18 | 7» для бросков с преимуществом, берём первое.
    """
    if not isinstance(payload, dict):
        return None
    raw_total = str(payload.get("total") or "")
    match = re.search(r"-?\d+", raw_total)
    if not match:
        return None
    title = str(payload.get("title") or payload.get("formula") or "бросок")
    formula = str(payload.get("formula") or "")
    breakdown = str(payload.get("breakdown") or "")
    joined = formula
    if breakdown:
        joined = f"{joined} = {breakdown}" if joined else breakdown
    note = str(payload.get("characterName") or "").strip()
    if payload.get("isCrit"):
        mark = "КРИТ!" if payload.get("critKind") == "success" else "крит"
        note = f"{note} · {mark}" if note else mark
    return {
        "dice": title[:64],
        "result": int(match.group()),
        "breakdown": joined[:200],
        "note": note[:200],
    }


def _value(node, default=None):
    """Развернуть поле LSS вида {"value": ...} (вложенность бывает двойная)."""
    for _ in range(2):
        if isinstance(node, dict) and "value" in node:
            node = node["value"]
        else:
            break
    return default if node is None else node


def ability_modifier(score) -> int:
    """Модификатор D&D 5e из значения характеристики."""
    try:
        return (int(score) - 10) // 2
    except (TypeError, ValueError):
        return 0


def evaluate_formula(text, data: dict):
    """Вычислить формулу листа LSS: «[DEX] + 14», «10+[PROF]». None — если нельзя.

    Подставляем только модификаторы характеристик, бонус мастерства и уровень;
    после подстановки в выражении остаются лишь цифры и знаки — его можно
    считать без eval над чужим текстом.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    stats = data.get("stats") or {}
    info = data.get("info") or {}
    proficiency = (_as_int(data.get("proficiencyCustom"), 0)
                   or _as_int(data.get("proficiency"), 0) or 0)
    level = _as_int(info.get("level"), 0) or 0

    def replace(match):
        name = match.group(1).upper()
        if name in ("STR", "DEX", "CON", "INT", "WIS", "CHA"):
            return str(ability_modifier(_as_int(stats.get(name.lower()), 10)))
        if name == "PROF":
            return str(proficiency)
        if name == "LVL":
            return str(level)
        return "0"

    expr = re.sub(r"\[([A-Za-zА-Яа-я]+)\]", replace, text)
    expr = expr.replace("−", "-").replace(",", ".").strip()
    if not re.fullmatch(r"[0-9+\-*/(). ]+", expr):
        return None
    try:
        return int(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307 — только цифры
    except (SyntaxError, TypeError, ZeroDivisionError):
        return None


def _ac(data: dict, vitality: dict):
    """КД: число, а если формула — вычисленный результат плюс щит."""
    raw = _value(vitality.get("ac"))
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    value = evaluate_formula(raw, data)
    if value is None:
        return None
    # Щит в листе — отдельный флаг: он прибавляет +2 к формуле КД.
    if _value(vitality.get("shield")) is True:
        value += 2
    return value


def _initiative(data: dict, vitality: dict):
    """Инициатива: число, а если пусто или формула — считаем от Ловкости."""
    value = _as_int(vitality.get("initiative"))
    if value is not None:
        return value
    raw = _value(vitality.get("initiative"))
    if isinstance(raw, str) and raw.strip():
        return evaluate_formula(raw, data)
    if "dex" in (data.get("stats") or {}):
        return ability_modifier(_as_int(data["stats"]["dex"], 10))
    return None


def _as_int(node, default=None):
    # Характеристики LSS лежат как {"score": 18}, остальные числа — как
    # {"value": 56}; принимаем оба вида.
    if isinstance(node, dict) and "score" in node:
        node = node["score"]
    value = _value(node)
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(-?\d+)\s*", value)
        if match:
            return int(match.group(1))
    return default


def _unwrap(body) -> dict:
    """Снять обёртки с документом LSS.

    Слоёв бывает три: ответ API (поля _id/edition/updatedAt), внутри него
    data строкой, а в самом data — {jsonType, name, vitality, …}.
    """
    node = body
    for _ in range(3):
        if not isinstance(node, dict) or "data" not in node:
            break
        nxt = node["data"]
        if isinstance(nxt, str):
            try:
                nxt = json.loads(nxt)
            except ValueError:
                return {}
        if not isinstance(nxt, dict):
            return {}
        node = nxt
    return node if isinstance(node, dict) else {}


def parse_document(body) -> dict:
    """Документ персонажа LSS → словарь для «теневого листа».

    Принимает ответ API листа, документ целиком
    ({"jsonType": "character", "data": "<json>"}) и уже развёрнутое
    содержимое data. Возвращает {} для чужих данных.
    """
    if not isinstance(body, dict):
        return {}
    data = _unwrap(body)
    if not data:
        return {}
    json_type = data.get("jsonType")
    if json_type is not None and str(json_type) != "character":
        return {}
    name = str(_value(data.get("name")) or "").strip()
    vitality = data.get("vitality") if isinstance(data.get("vitality"), dict) else {}
    stats = {}
    for key in ("str", "dex", "con", "int", "wis", "cha"):
        score = _as_int((data.get("stats") or {}).get(key))
        if score is not None:
            stats[key] = score
    if not name and not stats and not vitality:
        return {}
    info = data.get("info") if isinstance(data.get("info"), dict) else {}

    # Теневой лист для сервера: тот же документ, но числа хитов и КД
    # приведены к int — сервер синхронизирует по ним токен. КД в листе бывает
    # формулой («[DEX] + 14») — подставляем посчитанное значение.
    sheet = copy.deepcopy(data)
    sheet_vitality = sheet.setdefault("vitality", {})
    if not isinstance(sheet_vitality, dict):
        sheet_vitality = {}
        sheet["vitality"] = sheet_vitality
    for key, value in (("hp-current", _as_int(vitality.get("hp-current"))),
                       ("hp-max", _as_int(vitality.get("hp-max"))),
                       ("hp-temp", _as_int(vitality.get("hp-temp"))),
                       ("ac", _ac(data, vitality))):
        if value is not None:
            entry = sheet_vitality.get(key)
            if not isinstance(entry, dict):
                entry = {}
                sheet_vitality[key] = entry
            entry["value"] = int(value)

    out = {
        "name": name,
        "level": _as_int(info.get("level")),
        "char_class": str(_value(info.get("charClass")) or ""),
        "race": str(_value(info.get("race")) or ""),
        "hp": _as_int(vitality.get("hp-current")),
        "hp_max": _as_int(vitality.get("hp-max")),
        "hp_temp": _as_int(vitality.get("hp-temp")),
        "ac": _ac(data, vitality),
        "speed": _as_int(vitality.get("speed")),
        "initiative": _initiative(data, vitality),
        "stats": stats,
        "edition": str(body.get("edition") or data.get("edition") or ""),
        "document": body,
        "sheet": sheet,
    }
    return out


def apply_health(sheet: dict, payload) -> bool:
    """Перенести dnd:health в лист персонажа. True — если записали.

    Имя из события должно совпадать с именем листа (без учёта регистра):
    панель может показывать любого персонажа, а чужие хиты в свой лист
    переносить нельзя. Пустое имя листа тоже не принимаем.
    """
    if not isinstance(sheet, dict) or not isinstance(payload, dict):
        return False
    sheet_name = str((sheet.get("name") or {}).get("value") or "").strip()
    event_name = str(payload.get("characterName") or "").strip()
    if not sheet_name or sheet_name.casefold() != event_name.casefold():
        return False
    vitality = sheet.setdefault("vitality", {})
    for key, field in (("hp-current", "current"),
                       ("hp-max", "max"),
                       ("hp-temp", "temp")):
        value = payload.get(field)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            vitality.setdefault(key, {})["value"] = int(value)
        elif isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
            vitality.setdefault(key, {})["value"] = int(value.strip())
    return True
