"""Формат персонажа Long Story Short (LSS) для D&D 5e.

Файл, который LSS принимает кнопкой «Загрузить .json»: внешняя обёртка с
полем data, где лежит JSON-строка с самим листом. Структуру взяли из
исходников модуля elfrey-lss-converter (convert.js), поэтому имена полей
совпадают с тем, что ждёт сайт.

Здесь только чистые преобразования словарей — без Qt и без сети, чтобы
их можно было проверять отдельно.

Оружие листа (``weaponsList``) переводится в те же записи атак, что
собирает окно для диалога атаки и быстрого боя::

    {"id", "name", "attack_bonus", "damage", "damage_type", "notes"}

Формулы LSS пишутся через кириллическую «к» и переменные характеристик
(``1к6+[STR]``), а приложение считает урон в записи ``1d6+3``. Поэтому
``formula_from_text`` подставляет модификаторы и переводит букву кубика.
"""
import copy
import json
import re
import uuid

from shared import combat as combat_rules

ABILITIES = ("str", "dex", "con", "int", "wis", "cha")

ABILITY_RU = {
    "str": "Сила", "dex": "Ловкость", "con": "Телосложение",
    "int": "Интеллект", "wis": "Мудрость", "cha": "Харизма",
}

# Навыки: ключ LSS -> (характеристика, название по-русски)
SKILLS = {
    "acrobatics": ("dex", "Акробатика"),
    "animal handling": ("wis", "Дрессировка"),
    "arcana": ("int", "Магия"),
    "athletics": ("str", "Атлетика"),
    "deception": ("cha", "Обман"),
    "history": ("int", "История"),
    "insight": ("wis", "Проницательность"),
    "intimidation": ("cha", "Запугивание"),
    "investigation": ("int", "Расследование"),
    "medicine": ("wis", "Медицина"),
    "nature": ("int", "Природа"),
    "perception": ("wis", "Внимание"),
    "performance": ("cha", "Исполнение"),
    "persuasion": ("cha", "Убеждение"),
    "religion": ("int", "Религия"),
    "sleight of hand": ("dex", "Ловкость рук"),
    "stealth": ("dex", "Скрытность"),
    "survival": ("wis", "Выживание"),
}


def modifier(score) -> int:
    """Модификатор характеристики по правилам D&D 5e."""
    try:
        return (int(score) - 10) // 2
    except (TypeError, ValueError):
        return 0


def character_level(sheet: dict) -> int:
    """Уровень персонажа: ``info.level.value`` или плоское ``level``."""
    if not isinstance(sheet, dict):
        return 0
    info = sheet.get("info") or {}
    for raw in (info.get("level"), sheet.get("level"),
                sheet.get("levelTotal")):
        value = _as_int(_cell_value(raw), 0) or 0
        if value:
            return max(1, min(20, value))
    return 0


def proficiency_bonus(sheet: dict) -> int:
    """Бонус мастерства листа.

    LSS хранит его в двух полях: ``proficiency`` (рассчитанный по уровню) и
    ``proficiencyCustom`` — ручная замена. Сайт LSS читает так:
    ``proficiencyCustom || proficiency``, причём ноль считается отсутствием
    замены.

    У многих экспортов оба поля пустые (``proficiency: null``), хотя уровень
    в листе есть. Без третьего шага такой персонаж терял мастерство во
    всём: навыки, спасброски, попадание оружием и Сл заклинаний. Поэтому
    если оба поля пусты, считаем мастерство по уровню.
    """
    if not isinstance(sheet, dict):
        return 0
    custom = _as_int(sheet.get("proficiencyCustom"), 0) or 0
    if custom:
        return custom
    stored = _as_int(sheet.get("proficiency"), 0) or 0
    if stored:
        return stored
    level = character_level(sheet)
    if level:
        return 2 + (level - 1) // 4
    return 0


def _proficiency_multiplier(value) -> int:
    """Сколько раз бонус мастерства входит: 0 — нет, 1 — владение, 2 — экспертиза.

    В LSS ``isProf`` бывает не только true/false, но и 0/1/2, где двойка —
    экспертиза (skill_bonus и save_bonus в схеме листа).
    """
    if value is True:
        return 1
    if value is False or value is None:
        return 0
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in ("true", "да", "yes"):
            return 1
        if text in ("false", "нет", "no", ""):
            return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return 2 if number >= 2 else (1 if number >= 1 else 0)


def skill_bonus(sheet: dict, skill: str) -> int:
    """Итоговый бонус навыка: модификатор + мастерство (владение/экспертиза)."""
    base, _ = SKILLS.get(skill, ("int", ""))
    stats = sheet.get("stats", {})
    entry = (sheet.get("skills") or {}).get(skill) or {}
    # customModifier в LSS — полная замена посчитанного итога.
    custom = _as_int(entry.get("customModifier"))
    if custom is not None:
        return custom
    score = (stats.get(base) or {}).get("score", 10)
    bonus = modifier(score)
    bonus += proficiency_bonus(sheet) * _proficiency_multiplier(entry.get("isProf"))
    return bonus


def save_bonus(sheet: dict, ability: str) -> int:
    """Бонус спасброска: модификатор + мастерство при владении."""
    stats = sheet.get("stats", {})
    entry = (sheet.get("saves") or {}).get(ability) or {}
    custom = _as_int(entry.get("customModifier"))
    if custom is not None:
        return custom
    score = (stats.get(ability) or {}).get("score", 10)
    bonus = modifier(score)
    bonus += proficiency_bonus(sheet) * _proficiency_multiplier(entry.get("isProf"))
    return bonus


# ----------------------------------------------------------------------
# Оружие листа -> атаки токена
# ----------------------------------------------------------------------

def _cell_value(raw, default=None):
    """Значение поля LSS: ``{"value": X}`` -> ``X``, иначе само значение."""
    if isinstance(raw, dict):
        if "value" in raw:
            return raw.get("value")
        return default
    return default if raw is None else raw


def _stat_score(sheet: dict, ability: str, default: int = 10) -> int:
    """Значение характеристики из листа: ``stats.str.score``."""
    entry = (sheet.get("stats") or {}).get(ability)
    if isinstance(entry, dict):
        entry = entry.get("score", default)
    try:
        return int(entry)
    except (TypeError, ValueError):
        return default


def _as_int(raw, default=None):
    """Аккуратно привести к int: None, пустая строка и мусор — default."""
    if isinstance(raw, bool) or raw is None:
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def numeric_formula(text, sheet: dict):
    """Числовая формула листа: «[DEX] + 14» -> 16, «18» -> 18.

    В листе LSS КД и инициатива часто записаны формулой. Показывать её в
    карточке нельзя: игрок ждёт число. Считаем сами, подставляя только
    модификаторы характеристик, мастерство и уровень, — без eval над
    чужим текстом и без права на что-либо ещё.
    """
    if not isinstance(text, str) or not text.strip():
        return None

    def replace(match):
        name = match.group(1).upper()
        if name in ("STR", "DEX", "CON", "INT", "WIS", "CHA"):
            return str(modifier(_stat_score(sheet, name.lower())))
        if name == "PROF":
            return str(proficiency_bonus(sheet))
        if name in ("LVL", "LEVEL"):
            return str(character_level(sheet))
        return "?"

    expr = re.sub(r"\[([A-Za-zА-Яа-я0-9_]+)\]", replace, text)
    if "?" in expr:
        return None
    expr = (expr.replace("−", "-").replace("–", "-")
            .replace(",", ".").replace("х", "*").replace("Х", "*"))
    expr = re.sub(r"\s+", "", expr)
    if not expr or not re.fullmatch(r"[0-9+\-*/().]+", expr):
        return None
    try:
        return int(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307 — только цифры
    except (SyntaxError, TypeError, ZeroDivisionError):
        return None


def ac_value(sheet: dict):
    """КД листа числом: формула и флаг щита учтены.

    Вернуть строку-формулу враждебно игроку: в карточке «[DEX] + 14» вместо
    18 выглядит как поломка. Если посчитать нельзя — None, а карточка
    покажет прочерк с подсказкой формулы.
    """
    if not isinstance(sheet, dict):
        return None
    vitality = sheet.get("vitality") or {}
    raw = (vitality.get("ac") or {}).get("value")
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        # Готовое число уже включает щит: игрок вписал итоговый КД.
        return int(raw)
    value = numeric_formula(str(raw), sheet)
    if value is None:
        return None
    # Щит в листе — отдельный флаг: к формуле он прибавляет +2.
    shield = (vitality.get("shield") or {}).get("value")
    if shield is True:
        value += 2
    return value


def initiative_value(sheet: dict):
    """Бонус инициативы листа: число, формула или модификатор Ловкости."""
    if not isinstance(sheet, dict):
        return None
    vitality = sheet.get("vitality") or {}
    raw = (vitality.get("initiative") or {}).get("value")
    if isinstance(raw, bool):
        raw = None
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str) and raw.strip():
        value = numeric_formula(raw, sheet)
        if value is not None:
            return value
    stats = sheet.get("stats") or {}
    if "dex" in stats:
        return modifier(_stat_score(sheet, "dex"))
    return None


def formula_from_text(text, sheet: dict, allow_constant: bool = False):
    """Формула LSS -> запись приложения: «1к6+[STR]» -> «1d6+3».

    Подставляем только характеристики, бонус мастерства и уровень, после
    чего в строке остаются цифры, знаки и буква кубика. Если в формуле есть
    что-то ещё (например ``[LEVEL]`` из чужой версии листа) — возвращаем
    None: показать чужую формулу как свою нельзя, урон посчитается неверно.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    info = sheet.get("info") or {}
    proficiency = proficiency_bonus(sheet)
    level = _as_int(_cell_value(info.get("level")), 0) or 0

    def replace(match):
        name = match.group(1).upper()
        if name in ("STR", "DEX", "CON", "INT", "WIS", "CHA"):
            return str(modifier(_stat_score(sheet, name.lower())))
        if name == "PROF":
            return str(proficiency)
        if name in ("LVL", "LEVEL"):
            return str(level)
        return "?"   # неизвестная переменная — ниже отвергнем

    expr = re.sub(r"\[([A-Za-zА-Яа-я0-9_]+)\]", replace, text)
    if "?" in expr:
        return None
    # Кириллическая запись кубика: «1к6» -> «1d6», «2К8» -> «2d8».
    expr = re.sub(r"([0-9])\s*[кК]", r"\1d", expr)
    expr = (expr.replace("−", "-").replace("–", "-").replace(",", ".")
            .replace("х", "*").replace("Х", "*").replace("×", "*"))
    expr = re.sub(r"\s+", "", expr)
    expr = re.sub(r"\+-|\+\-", "-", expr)
    # «1d4+0» и «1d4-0» — тот же кубик: нулевой модификатор не пишем.
    expr = re.sub(r"[+-]0(?![0-9d])", "", expr)
    if not expr:
        return None
    if not re.fullmatch(r"[0-9dD+\-*/().]+", expr):
        return None
    expr = expr.lower()
    if not allow_constant and "d" not in expr:
        return None
    return expr


#: Как характеристика может быть записана в оружии листа.
_ABILITY_ALIASES = {
    "str": "str", "сила": "str", "сил": "str", "сил.": "str",
    "dex": "dex", "ловкость": "dex", "лов": "dex", "лов.": "dex",
    "con": "con", "телосложение": "con", "тел": "con", "тел.": "con",
    "int": "int", "интеллект": "int", "инт": "int", "инт.": "int",
    "wis": "wis", "мудрость": "wis", "муд": "wis", "муд.": "wis",
    "cha": "cha", "харизма": "cha", "хар": "cha", "хар.": "cha",
}


def ability_key(value) -> str:
    """Характеристика оружия в виде ключа (``dex``) или пустая строка."""
    text = str(value or "").strip().casefold().replace("ё", "е")
    return _ABILITY_ALIASES.get(text, "")


def weapon_ability(weapon: dict) -> str:
    """Характеристика оружия в виде ключа.

    Пустое или отсутствующее ``ability`` у оружия с уроном LSS считает
    Силой (так лист показывает «Длинный меч» без явной характеристики),
    а ``none`` — оружием, которое бьёт без модификатора характеристики.
    """
    raw = weapon.get("ability") if isinstance(weapon, dict) else None
    if raw is None or str(raw).strip() == "":
        return "str"
    return ability_key(raw)


def weapon_proficiency_rank(weapon: dict) -> int:
    """Владение оружием ступенью: 0 — нет, 1 — бонус мастерства, 2 — экспертиза."""
    return _proficiency_multiplier((weapon or {}).get("isProf"))


def weapon_base_attack_bonus(sheet: dict, weapon: dict) -> int:
    """Бонус атаки без ручных надбавок: характеристика + мастерство.

    Это «база» строки оружия на сайте LSS: модификатор характеристики
    (``ability``, по умолчанию Сила) плюс мастерство, если в оружии
    отмечено владение (``isProf``). Редактируемое в листе приложения поле
    «Бонус» хранится как надбавка к этой базе, поэтому базу нельзя терять.
    """
    ability = weapon_ability(weapon)
    bonus = (modifier(_stat_score(sheet, ability))
             if ability in ABILITIES else 0)
    bonus += proficiency_bonus(sheet) * _proficiency_multiplier(
        (weapon or {}).get("isProf"))
    return bonus


def _weapon_bonus_records(sheet: dict, weapon: dict, kind: str) -> list:
    """Полные записи ``bonuses`` оружия, отсортированные как в LSS.

    Возвращает пары (запись, режим): режим ``set`` заменяет посчитанное
    значение, ``add`` прибавляется. Цель записи — ``weapon.<id>.attack``
    или ``weapon.<id>.damage``; у старых листов вместо id встречается имя.
    """
    if not isinstance(sheet, dict) or not isinstance(weapon, dict):
        return []
    ident = str(weapon_id(weapon) or "")
    name = str(_cell_value(weapon.get("name")) or "").strip()
    keys = {key.casefold() for key in (ident, name) if key}
    if not keys:
        return []
    suffix = "." + str(kind or "")
    entries = []
    for index, entry in enumerate(sheet.get("bonuses") or []):
        if not isinstance(entry, dict):
            continue
        target = str(entry.get("target") or "").strip().casefold()
        if not target.startswith("weapon.") or not target.endswith(suffix):
            continue
        if target[len("weapon."):-len(suffix)] not in keys:
            continue
        if not str(entry.get("expr") or "").strip():
            continue
        mode = ("set" if str(entry.get("mode") or "").strip().casefold()
                == "set" else "add")
        priority = _as_int(entry.get("priority"), 0) or 0
        entries.append((priority, index, entry, mode))
    entries.sort(key=lambda item: (item[0], item[1]))
    return [(entry, mode) for _priority, _index, entry, mode in entries]


def weapon_bonus_entries(sheet: dict, weapon: dict, kind: str) -> list:
    """Надбавки оружия из списка ``bonuses`` листа LSS, по порядку.

    LSS хранит бонусы атаки и урона отдельными записями: ``target`` вида
    ``weapon.<id>.attack`` / ``weapon.<id>.damage``, ``expr`` (число,
    формула вида ``[DEX]+4+2+1-5`` или кубики ``1d4``) и ``mode``.

    Именно здесь лежат +2 «Боевого стиля: стрельба», +1 инфузии арбалета
    Броттора и штраф -5 «Меткого стрелка». Без чтения этого списка все
    атаки листа показывали одинаковый бонус.
    """
    return [{"mode": mode, "expr": str(entry.get("expr") or "").strip()}
            for entry, mode in _weapon_bonus_records(sheet, weapon, kind)]


def weapon_bonus_items(sheet: dict, weapon: dict, kind: str) -> list:
    """Надбавки для окна атаки: id, подпись, выражение и посчитанное значение.

    Окно LSS показывает каждую надбавку строкой с названием, формулой и
    готовым числом («Инфузия — 1», «Световой урон — 1–4 (~3)»). Здесь
    собираем все три части, чтобы интерфейс не повторял разбор листа.
    """
    items = []
    for entry, mode in _weapon_bonus_records(sheet, weapon, kind):
        expr = str(entry.get("expr") or "").strip()
        items.append({
            "id": str(entry.get("id") or ""),
            "label": str(entry.get("label") or "").strip(),
            "expr": expr,
            "mode": mode,
            "value_text": expression_value_text(sheet, expr),
            "entry": entry,
        })
    return items


def expression_value_text(sheet: dict, expr) -> str:
    """Готовое значение выражения LSS: «2», «1–4 (~3)».

    В листе выражение может быть как числом, так и кубиками или формулой
    с характеристикой. Окно атаки показывает результат, иначе игрок
    видит «1d4» и сам считает средний урон.
    """
    formula = formula_from_text(str(expr or ""), sheet, allow_constant=True)
    if not formula:
        return ""
    span = dice_span(formula)
    if span is not None:
        low, high, average = span
        return f"{low}–{high} (~{average})"
    value = numeric_formula(formula, sheet)
    if value is None:
        return str(formula)
    return f"{value:+d}" if value < 0 else str(value)


def dice_span(formula) -> tuple:
    """Диапазон и среднее формулы кубиков: «1d10+6» -> (7, 16, 12).

    Понимает только готовую формулу с числами (после подстановки
    характеристик). Возвращает None, если это не кубики.
    """
    text = str(formula or "").replace(" ", "").replace("−", "-")
    text = text.replace("–", "-").replace("к", "d").replace("К", "d")
    if not text or not re.fullmatch(r"[0-9d+\-]+", text):
        return None
    low = high = 0.0
    has_dice = False
    for sign, term in re.findall(r"([+-]?)([^+-]+)", text):
        if not term:
            continue
        multiplier = -1 if sign == "-" else 1
        match = re.fullmatch(r"(\d*)d(\d+)", term)
        if match:
            count = int(match.group(1) or 1)
            sides = int(match.group(2))
            has_dice = True
            low += multiplier * count
            high += multiplier * count * sides
            continue
        if term.isdigit():
            low += multiplier * int(term)
            high += multiplier * int(term)
            continue
        return None
    if not has_dice:
        return None
    low_i, high_i = int(low), int(high)
    # Среднее округляем вверх от половины: 1d4 (1–4) — это «~3», как в LSS,
    # а не «~2» от банковского округления Python.
    return low_i, high_i, int((low_i + high_i) / 2 + 0.5)


def weapon_damage_span(sheet: dict, weapon: dict):
    """Диапазон урона оружия с модификатором и надбавками — для заголовка."""
    return dice_span(weapon_damage_formula(sheet, weapon))


def ensure_weapon_id(weapon: dict) -> str:
    """id оружия для целей ``bonuses``; без id — выдать новый."""
    if not isinstance(weapon, dict):
        return ""
    ident = str(weapon.get("id") or "").strip()
    if ident:
        return ident
    ident = f"weapon-{uuid.uuid4().hex[:10]}"
    weapon["id"] = ident
    return ident


def _migrate_weapon_choices(sheet: dict, weapon: dict, ident: str) -> None:
    """Выбор атаки/быстрой атаки по имени переписать на новый id.

    У импортированного оружия без id выбор хранился именем. Как только у
    оружия появляется id (нужен для надбавок), имя перестало бы совпадать
    с записью в ``vtt``, и Q молча перескочила бы на другое оружие.
    """
    block = _vtt(sheet)
    name = str(_cell_value((weapon or {}).get("name")) or "").strip()
    if not isinstance(block, dict) or not name or not ident:
        return
    for key in ("attack_weapon", "quick_weapon", "chosen_weapon"):
        if str(block.get(key) or "").strip() == name:
            block[key] = ident


def add_weapon_bonus(sheet: dict, weapon: dict, kind: str,
                     label: str = "", expr: str = "1") -> dict:
    """Добавить надбавку оружия в ``sheet['bonuses']`` и вернуть запись."""
    if not isinstance(sheet, dict) or not isinstance(weapon, dict):
        return {}
    kind = "damage" if str(kind) == "damage" else "attack"
    had_id = bool(str(weapon.get("id") or "").strip())
    ident = ensure_weapon_id(weapon)
    if not ident:
        return {}
    if not had_id:
        _migrate_weapon_choices(sheet, weapon, ident)
    bonuses = sheet.get("bonuses")
    if not isinstance(bonuses, list):
        bonuses = []
        sheet["bonuses"] = bonuses
    entry = {
        "id": f"bonus-{uuid.uuid4().hex[:12]}",
        "label": str(label or ""),
        "target": f"weapon.{ident}.{kind}",
        "expr": str(expr or ""),
        "source": {"kind": "user"},
    }
    bonuses.append(entry)
    return entry


def remove_weapon_bonus(sheet: dict, bonus_id) -> bool:
    """Убрать одну надбавку по её id."""
    bonuses = sheet.get("bonuses") if isinstance(sheet, dict) else None
    if not isinstance(bonuses, list) or not bonus_id:
        return False
    keep = [entry for entry in bonuses
            if not (isinstance(entry, dict)
                    and str(entry.get("id") or "") == str(bonus_id))]
    if len(keep) == len(bonuses):
        return False
    sheet["bonuses"] = keep
    return True


def remove_weapon_bonuses(sheet: dict, weapon: dict) -> int:
    """Убрать все надбавки оружия (при удалении строки из листа)."""
    bonuses = sheet.get("bonuses") if isinstance(sheet, dict) else None
    if not isinstance(bonuses, list) or not isinstance(weapon, dict):
        return 0
    ident = str(weapon.get("id") or "").strip()
    name = str(_cell_value(weapon.get("name")) or "").strip()
    prefixes = {f"weapon.{key}." for key in (ident, name) if key}
    if not prefixes:
        return 0
    keep = [entry for entry in bonuses
            if not (isinstance(entry, dict)
                    and any(str(entry.get("target") or "").startswith(p)
                            for p in prefixes))]
    removed = len(bonuses) - len(keep)
    if removed:
        sheet["bonuses"] = keep
    return removed


def set_weapon_proficiency(weapon: dict, value) -> bool:
    """Владение оружием: False — нет, True — бонус мастерства."""
    if not isinstance(weapon, dict):
        return False
    weapon["isProf"] = (2 if _proficiency_multiplier(value) >= 2
                        else bool(_proficiency_multiplier(value)))
    return True


def set_weapon_ability(weapon: dict, ability: str) -> bool:
    """Характеристика атаки и урона (``str``…``cha``)."""
    key = ability_key(ability) or str(ability or "").strip().casefold()
    if not isinstance(weapon, dict) or key not in ABILITIES:
        return False
    weapon["ability"] = key
    return True


def set_weapon_damage_type(weapon: dict, text) -> bool:
    """Тип урона оружия (``dmgType``), как его пишет LSS."""
    return _set_cell_value(weapon, "dmgType", text)


def set_weapon_notes_visibility(weapon: dict, visible) -> bool:
    """Показывать ли заметки оружия в бою (``notesVisibility``)."""
    if not isinstance(weapon, dict):
        return False
    weapon["notesVisibility"] = bool(visible)
    return True


def weapon_notes_visibility(weapon: dict) -> bool:
    """Видимость заметок: у старых листов поля нет — считаем видимыми."""
    if not isinstance(weapon, dict):
        return True
    return bool(weapon.get("notesVisibility", True))


def weapon_attack_bonus_without_manual(sheet: dict, weapon: dict) -> int:
    """Бонус атаки из данных листа: база и надбавки ``bonuses``.

    Ручную надбавку самой строки (``modBonus``) не учитываем: именно от
    этой величины считается разница, когда бонус правят в листе.
    """
    bonus = weapon_base_attack_bonus(sheet, weapon)
    for entry in weapon_bonus_entries(sheet, weapon, "attack"):
        value = numeric_formula(entry.get("expr"), sheet)
        if value is None:
            continue
        bonus = value if entry.get("mode") == "set" else bonus + value
    return bonus


def weapon_manual_attack_bonus(weapon: dict) -> int:
    """Ручная надбавка строки оружия: ``modBonus``/``modCustom``/``attack_bonus``."""
    for key in ("modBonus", "modCustom", "attack_bonus"):
        extra = _as_int(_cell_value((weapon or {}).get(key)))
        if extra:
            return extra
    return 0


def weapon_attack_bonus(sheet: dict, weapon: dict) -> int:
    """Бонус атаки оружия: характеристика + мастерство + надбавки листа.

    LSS показывает в атаке модификатор характеристики (``ability``, по
    умолчанию Сила) и прибавляет мастерство только если в самом оружии
    отмечено владение (``isProf``). Владение персонажа оружием в целом
    (``prof.weapon-martial``) на бонус конкретной строки не влияет — иначе
    кинжал без галочки получал бы мастерство, которого на сайте нет.

    Дальше идут надбавки ``bonuses`` листа: стиль боя, инфузия, штраф
    «Меткого стрелка» (``mode: set``). Поле ``mod`` конвертера LSS — это уже
    готовый toHit, поэтому его не складываем: иначе бонус посчитался бы
    дважды. Ручные надбавки (``modBonus`` и родственные) — разница,
    которую игрок ввёл в приложении поверх базы и надбавок листа.
    """
    return (weapon_attack_bonus_without_manual(sheet, weapon)
            + weapon_manual_attack_bonus(weapon))


def set_weapon_attack_bonus(sheet: dict, weapon: dict, total) -> bool:
    """Записать желаемый итоговый бонус атаки в лист.

    В LSS нет поля «итоговый бонус»: есть база (характеристика +
    мастерство), надбавки ``bonuses`` (стиль боя, инфузия, штраф черты)
    и ручная надбавка ``modBonus``. Считаем надбавку как разницу с базой и
    ``bonuses`` — иначе правка прибавилась бы к стилю боя второй раз.
    При смене характеристики или уровня бонус пересчитается вместе с
    листом, а не останется числом, оторванным от данных.
    """
    if not isinstance(weapon, dict):
        return False
    try:
        total = int(total)
    except (TypeError, ValueError):
        return False
    base = weapon_attack_bonus_without_manual(sheet, weapon)
    weapon["modBonus"] = {"value": total - base}
    # Другие способы записать надбавку перебивали бы новое значение.
    weapon.pop("modCustom", None)
    weapon.pop("attack_bonus", None)
    return True


def generic_attack_bonus(sheet: dict) -> int:
    """Бонус атаки предмета без своего поля: лучшая из Силы/Ловкости + мастерство.

    Так оружие, выданное в инвентарь, бьёт с тем же бонусом, что и оружие
    из ``weaponsList``, а не с нулём.
    """
    stats = sheet.get("stats", {}) if isinstance(sheet, dict) else {}
    best = max(modifier((stats.get(ability) or {}).get("score", 10))
               for ability in ("str", "dex"))
    return best + proficiency_bonus(sheet)


#: Переменная характеристики внутри формулы урона: ``1к8+[STR]``.
_FORMULA_ABILITY_RE = re.compile(
    r"\[\s*(STR|DEX|CON|INT|WIS|CHA)\s*\]", re.IGNORECASE)


def _looks_like_dice(text) -> bool:
    """Похоже ли значение на формулу кубиков: «1d8», «2к6+3»."""
    return bool(re.search(r"\d*\s*[dк]\s*\d+", str(text or ""), re.IGNORECASE))


def _add_flat_modifier(formula: str, amount: int) -> str:
    """Прибавить модификатор к формуле, сложив его с последней константой.

    «1d8» + 4 -> «1d8+4», «1d8+1» + 4 -> «1d8+5». Дробить на два слагаемых
    нельзя: ``combat._clean_formula`` читает только первую константу и
    «1d8+1+4» превратилось бы в «1d8+1».
    """
    if not amount:
        return formula
    match = re.search(r"([+-])(\d+)$", formula or "")
    if not match:
        return f"{formula}+{amount}" if amount > 0 else f"{formula}{amount}"
    total = int(match.group(2)) * (-1 if match.group(1) == "-" else 1) + amount
    base = formula[:match.start()]
    if total > 0:
        return f"{base}+{total}"
    if total < 0:
        return f"{base}{total}"
    return base


def _weapon_damage_text(weapon: dict, sheet: dict):
    """Формула урона оружия из листа.

    В LSS кубики лежат в ``dmg``. Домашние и перенесённые листы иногда
    кладут формулу в ``dmgType`` (так у «меча Зариэль» в листе игрока:
    ``dmg: null``, ``dmgType: 1d8``) — если оттуда читать только тип урона,
    оружие вообще пропадает из атак.
    """
    primary = _cell_value(weapon.get("dmg"))
    if formula_from_text(primary, sheet):
        return primary
    # Домашние листы пишут тип прямо в формуле: «1d8 колющий». Отделяем
    # слово, иначе формула не разбирается и оружие пропадает из атак.
    formula, _parsed_type = combat_rules.split_damage_text(primary)
    if formula_from_text(formula, sheet):
        return primary
    for key in ("damage", "dmgType", "damageType", "damage_type"):
        candidate = _cell_value(weapon.get(key))
        formula, _parsed_type = combat_rules.split_damage_text(candidate)
        if _looks_like_dice(formula) and formula_from_text(formula, sheet):
            return candidate
    return None


def weapon_damage_bonuses(sheet: dict, weapon: dict) -> dict:
    """Надбавки к урону из ``bonuses``: замена, плоские числа и кубики.

    LSS пишет их отдельными строками: ``1`` и ``10`` — к константе,
    ``1d4`` — дополнительный кубик («Световой урон»), ``mode: set`` —
    замена всей формулы. Возвращаем всё три вида, чтобы сборка формулы
    урона была в одном месте.
    """
    result = {"set": "", "flat": 0, "dice": []}
    for entry in weapon_bonus_entries(sheet, weapon, "damage"):
        formula = formula_from_text(entry.get("expr"), sheet,
                                    allow_constant=True)
        if not formula:
            continue
        if entry.get("mode") == "set":
            result["set"] = formula
        elif "d" in formula:
            if formula not in result["dice"]:
                result["dice"].append(formula)
        else:
            value = numeric_formula(formula, sheet)
            if value:
                result["flat"] += value
    return result


def weapon_damage_formula(sheet: dict, weapon: dict) -> str:
    """Итоговая формула урона: формула листа, характеристика и ``bonuses``.

    Это то, что видит игрок в окне атаки и в быстрой атаке: «1d10+[DEX]»
    плюс +1 инфузии и дополнительный 1d4 света превращаются в
    «1d10+6+1d4». В таблице листа поле по-прежнему правит базовую
    формулу — в этом его смысл.
    """
    raw_damage = _weapon_damage_text(weapon, sheet)
    formula_text, _parsed_type = combat_rules.split_damage_text(raw_damage)
    damage = formula_from_text(formula_text, sheet)
    ability = weapon_ability(weapon)
    if (damage and ability in ABILITIES
            and not _FORMULA_ABILITY_RE.search(str(formula_text or ""))):
        damage = _add_flat_modifier(
            damage, modifier(_stat_score(sheet, ability)))
    if not damage:
        return ""
    bonuses = weapon_damage_bonuses(sheet, weapon)
    if bonuses["set"]:
        damage = bonuses["set"]
    if bonuses["flat"]:
        damage = _add_flat_modifier(damage, bonuses["flat"])
    for term in bonuses["dice"]:
        if term not in damage:
            damage = f"{damage}+{term}"
    return damage


def weapon_damage_type(weapon: dict, raw_damage=None) -> str:
    """Тип урона оружия в любом написании: ``dmgType``/``damageType``.

    Кубики, ошибочно лежащие в поле типа, — не тип урона: у «меча Зариэль»
    в листе игрока ``dmg: null``, а ``dmgType: 1d8``, и подставлять это в
    бой нельзя — сопротивление цели считалось бы по «1d8».
    """
    if not isinstance(weapon, dict):
        return ""
    raw_type = ""
    for key in ("dmgType", "damageType", "damage_type"):
        raw_type = str(_cell_value(weapon.get(key)) or "").strip()
        if raw_type:
            break
    if not raw_type:
        # Тип мог быть записан словами прямо в формуле урона.
        return combat_rules.damage_type_label(raw_damage)
    plain = str(_cell_value(weapon.get("dmg")) or "").strip()
    if raw_type == str(raw_damage or "").strip() and raw_type != plain:
        return combat_rules.damage_type_label(raw_damage)
    return raw_type


#: Слова, по которым видно дальнобойное оружие. Полной таблицы свойств в
#: листе нет: у самодельного оружия («РУЖЬЁ», «арбалет Броттора») в заметках
#: написано только название. Догадка влияет лишь на то, предложить ли кару —
#: урон и атака от неё не меняются.
RANGED_WORDS = (
    "лук", "арбалет", "ружь", "пистолет", "праща", "дротик", "метательн",
    "дальнобойн", "bow", "crossbow", "rifle", "gun", "sling", "dart",
)


def weapon_is_ranged(weapon: dict = None, name: str = None,
                     notes: str = None) -> bool:
    """Дальнобойное ли оружие: по названию и свойствам.

    Нужно для правил, которые различают рукопашную и дальнюю атаку —
    например, Божественная кара работает только в рукопашной.
    """
    parts = [str(name or ""), str(notes or "")]
    if isinstance(weapon, dict):
        parts.append(str(_cell_value(weapon.get("name")) or ""))
        parts.append(str(_cell_value(weapon.get("notes")) or ""))
        parts.append(str(_cell_value(weapon.get("properties")) or ""))
    text = " ".join(parts).casefold().replace("ё", "е")
    return any(word in text for word in RANGED_WORDS)


def weapon_attacks(sheet: dict) -> list:
    """Оружие из листа (``weaponsList``) в записях атак приложения.

    Порядок сохраняем как в листе: первым идёт то, что игрок поставил
    первым, — оно же используется, если выбор ещё не сделан.

    Урон LSS хранит без модификатора характеристики («1к8»), а сам
    прибавляет его при броске (поле ``damageBonus``). Поэтому модификатор
    добавляем здесь — иначе меч бил бы «1d8» без Силы. Если модификатор уже
    записан в формулу (``1к8+[STR]``), второй раз не прибавляем.
    """
    if not isinstance(sheet, dict):
        return []
    weapons = sheet.get("weaponsList")
    if not isinstance(weapons, list):
        return []
    out = []
    for weapon in weapons:
        if not isinstance(weapon, dict):
            continue
        name = str(_cell_value(weapon.get("name")) or "").strip()
        if not name:
            name = "Оружие"
        raw_damage = _weapon_damage_text(weapon, sheet)
        damage = weapon_damage_formula(sheet, weapon)
        raw_type = weapon_damage_type(weapon, raw_damage)
        out.append({
            "id": weapon_id(weapon, name),
            "name": name,
            "attack_bonus": weapon_attack_bonus(sheet, weapon),
            "damage": damage or "",
            "damage_type": raw_type,
            "notes": weapon_notes(weapon),
            # Божественная кара не работает по дальней атаке: диалог
            # скрывает её для лука и ружья.
            "ranged": weapon_is_ranged(weapon, name),
        })
    return out


def weapon_id(weapon: dict, name: str = None) -> str:
    """Стабильный id оружия: поле ``id`` или имя (так делают атаки токена)."""
    if not isinstance(weapon, dict):
        return ""
    raw = weapon.get("id")
    if raw:
        return str(raw)
    text = (name if name is not None
            else str(_cell_value(weapon.get("name")) or "")).strip()
    return text or "Оружие"


def weapon_notes(weapon: dict) -> str:
    """Описание/свойства оружия: поле ``notes`` листа."""
    return str(_cell_value((weapon or {}).get("notes")) or "").strip()


def weapon_rows(sheet: dict) -> list:
    """Оружие для таблицы в листе: строки в порядке листа и как есть.

    В отличие от ``weapon_attacks`` здесь урон — **формула из листа** без
    прибавленного модификатора: поле должно правиться тем же текстом, что
    человек видит на сайте LSS. Итог с модификатором считает
    ``weapon_attacks`` и диалог атаки.
    """
    if not isinstance(sheet, dict):
        return []
    weapons = sheet.get("weaponsList")
    if not isinstance(weapons, list):
        return []
    rows = []
    for weapon in weapons:
        if not isinstance(weapon, dict):
            continue
        name = str(_cell_value(weapon.get("name")) or "").strip()
        rows.append({
            "weapon": weapon,
            "id": weapon_id(weapon, name or "Оружие"),
            "name": name or "Оружие",
            "bonus": weapon_attack_bonus(sheet, weapon),
            "damage": str(_weapon_damage_text(weapon, sheet) or "").strip(),
            # Итог в бою с характеристикой и надбавками листа: поле
            # правит базу, а числа на столе берутся отсюда.
            "total_damage": weapon_damage_formula(sheet, weapon),
            "damage_type": weapon_damage_type(
                weapon, _weapon_damage_text(weapon, sheet)),
            "notes": weapon_notes(weapon),
        })
    return rows


def _set_cell_value(weapon: dict, key: str, text) -> bool:
    """Записать текстовое поле оружия в формате LSS (``{"value": ...}``)."""
    if not isinstance(weapon, dict):
        return False
    text = str(text or "").strip()
    if text:
        weapon[key] = {"value": text}
    else:
        weapon.pop(key, None)
    return True


def set_weapon_name(weapon: dict, text) -> bool:
    return _set_cell_value(weapon, "name", text)


def set_weapon_notes(weapon: dict, text) -> bool:
    return _set_cell_value(weapon, "notes", text)


def set_weapon_damage(weapon: dict, text) -> bool:
    """Записать формулу урона оружия в ``dmg``.

    Если кубики лежали в поле типа (так бывает у импортированных листов),
    старый текст убираем: иначе после правки «1d8» остался бы типом урона.
    """
    if not isinstance(weapon, dict):
        return False
    raw_type = _cell_value(weapon.get("dmgType"))
    if _looks_like_dice(raw_type) and not str(_cell_value(
            weapon.get("dmg")) or "").strip():
        weapon.pop("dmgType", None)
    return _set_cell_value(weapon, "dmg", text)


def add_weapon(sheet: dict, name: str = "Новое оружие",
               damage: str = "1d6") -> dict:
    """Добавить оружие в ``weaponsList`` и вернуть его запись."""
    weapons = sheet.get("weaponsList") if isinstance(sheet, dict) else None
    if not isinstance(weapons, list):
        weapons = []
        if isinstance(sheet, dict):
            sheet["weaponsList"] = weapons
    weapon = {
        "id": f"weapon-{uuid.uuid4().hex[:10]}",
        "name": {"value": str(name or "Новое оружие")},
        "dmg": {"value": str(damage or "1d6")},
        "ability": "str",
        "isProf": True,
        "notes": {"value": ""},
    }
    weapons.append(weapon)
    return weapon


def remove_weapon(sheet: dict, weapon: dict) -> bool:
    """Убрать оружие из листа и снять с него выбор атаки/быстрой атаки."""
    weapons = sheet.get("weaponsList") if isinstance(sheet, dict) else None
    if not isinstance(weapons, list) or not isinstance(weapon, dict):
        return False
    removed = [w for w in weapons if w is weapon]
    if not removed:
        return False
    weapons.remove(weapon)
    # Надбавки адресуются id оружия: без строки они остались бы мусором и
    # могли случайно прилипнуть к новому оружию с тем же id.
    remove_weapon_bonuses(sheet, weapon)
    wid = weapon_id(weapon)
    block = _vtt(sheet)
    if isinstance(block, dict):
        for key in ("attack_weapon", "quick_weapon", "chosen_weapon"):
            if block.get(key) in (wid, str(_cell_value(weapon.get("name"))
                                           or "").strip()):
                block.pop(key, None)
    return True


def _vtt(sheet: dict):
    """Блок настроек приложения внутри листа (или None, если структура чужая)."""
    if not isinstance(sheet, dict):
        return None
    block = sheet.get("vtt")
    return block if isinstance(block, dict) else None


def _weapon_id(block: dict, key: str):
    if not isinstance(block, dict):
        return None
    chosen = block.get(key)
    return str(chosen) if chosen else None


def _set_weapon_id(sheet: dict, key: str, weapon_id) -> bool:
    block = _vtt(sheet)
    if block is None:
        if not isinstance(sheet, dict):
            return False
        block = sheet.setdefault("vtt", {})
        if not isinstance(block, dict):   # чужая структура — не ломаем лист
            return False
    if weapon_id:
        block[key] = str(weapon_id)
    else:
        block.pop(key, None)
    return True


def attack_weapon_id(sheet: dict):
    """Оружие, выбранное строкой «Атака» (с него открывается диалог)."""
    return _weapon_id(_vtt(sheet), "attack_weapon")


def set_attack_weapon(sheet: dict, weapon_id) -> bool:
    """Запомнить оружие для диалога атаки; None — снова первое из листа."""
    return _set_weapon_id(sheet, "attack_weapon", weapon_id)


def quick_weapon_id(sheet: dict):
    """Оружие, выбранное строкой «Для быстрой атаки» (удар по Q)."""
    return _weapon_id(_vtt(sheet), "quick_weapon")


def set_quick_weapon(sheet: dict, weapon_id) -> bool:
    """Запомнить оружие быстрой атаки; None — вернуть выбор к «Атаке»."""
    return _set_weapon_id(sheet, "quick_weapon", weapon_id)


# Старые имена: до появления отдельной строки быстрой атаки выбора было
# два — «атака» и «быстрая атака» жили в одном поле. Оставляем алиасы,
# чтобы не переписывать вызовы по всему проекту.
def chosen_weapon_id(sheet: dict):
    """Оружие быстрой атаки (старое имя)."""
    return quick_weapon_id(sheet)


def set_chosen_weapon(sheet: dict, weapon_id) -> bool:
    """Выбрать оружие быстрой атаки (старое имя)."""
    return set_quick_weapon(sheet, weapon_id)


def _find_weapon(attacks: list, weapon_id):
    if not weapon_id:
        return None
    for attack in attacks:
        if attack["id"] == weapon_id or attack["name"] == weapon_id:
            return attack
    return None


def attack_weapon(sheet: dict):
    """Оружие для диалога атаки: выбор строки «Атака» или первое.

    Если строка «Атака» не менялась, берём оружие быстрой атаки: до
    появления двух строк выбор был один, и старые листы не должны
    открывать диалог на другом оружии.
    """
    attacks = weapon_attacks(sheet)
    if not attacks:
        return None
    return (_find_weapon(attacks, attack_weapon_id(sheet))
            or _find_weapon(attacks, quick_weapon_id(sheet))
            or attacks[0])


def quick_weapon(sheet: dict):
    """Оружие быстрой атаки: строка «Для быстрой атаки» или выбор «Атаки»."""
    attacks = weapon_attacks(sheet)
    if not attacks:
        return None
    return (_find_weapon(attacks, quick_weapon_id(sheet))
            or _find_weapon(attacks, attack_weapon_id(sheet))
            or attacks[0])


def chosen_weapon(sheet: dict):
    """Оружие быстрой атаки (старое имя)."""
    return quick_weapon(sheet)


# ----------------------------------------------------------------------
# Текстовые блоки листа: заметки, предыстория, свойства
# ----------------------------------------------------------------------
#
# LSS хранит свободный текст деревом документа (как ProseMirror):
#
#   {"value": {"data": {"type": "doc", "content": [
#       {"type": "paragraph", "content": [{"type": "text", "text": "…"}]}
#   ]}, "size": 9}}
#
# В интерфейсе нужен обычный текст с переносами, поэтому дерево разбирается
# в строки, а правка собирается обратно в такие же абзацы. Так лист можно
# отредактировать в приложении и сохранить для сайта без потери других блоков.

def _inline_plain(node) -> str:
    """Текст внутри абзаца: сам текст, переносы и упоминания."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return str(node.get("text") or "")
    if kind == "hardBreak":
        return "\n"
    if kind in ("mention", "hashtag"):
        attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
        return "@" + str(attrs.get("label") or attrs.get("id") or "")
    return "".join(_inline_plain(child)
                   for child in (node.get("content") or []))


def _block_lines(node) -> list:
    """Дерево документа LSS -> строки обычного текста."""
    if isinstance(node, list):
        lines = []
        for child in node:
            lines.extend(_block_lines(child))
        return lines
    if not isinstance(node, dict):
        return []
    kind = node.get("type")
    content = node.get("content") or []
    if kind in (None, "doc"):
        return _block_lines(content)
    if kind in ("paragraph", "heading"):
        return _inline_plain(node).split("\n")
    if kind in ("bulletList", "orderedList"):
        out = []
        for index, item in enumerate(content, 1):
            text = " ".join(line.strip() for line in _block_lines(item)
                            if line.strip())
            mark = "• " if kind == "bulletList" else f"{index}. "
            out.append(mark + text)
        return out
    if kind == "blockquote":
        return ["> " + line for line in _block_lines(content)]
    if kind == "codeBlock":
        return _inline_plain(node).split("\n")
    if kind == "horizontalRule":
        return ["———"]
    text = _inline_plain(node)
    return text.split("\n") if text else []


def _doc_of(cell):
    """Достать документ из ячейки ``text`` в любом из известных видов."""
    value = cell
    if isinstance(value, dict) and "value" in value:
        value = value.get("value")
    if isinstance(value, dict) and "data" in value:
        value = value.get("data")
    return value


def text_block(sheet: dict, key: str) -> str:
    """Текст блока листа (``text.notes-1``) обычными строками."""
    if not isinstance(sheet, dict):
        return ""
    texts = sheet.get("text")
    if not isinstance(texts, dict):
        return ""
    raw = _doc_of(texts.get(key))
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return "\n".join(_block_lines(raw)).strip("\n")
    return ""


def set_text_block(sheet: dict, key: str, text) -> bool:
    """Записать блок листа обратно в формате LSS (дерево абзацев)."""
    if not isinstance(sheet, dict):
        return False
    texts = sheet.get("text")
    if not isinstance(texts, dict):
        texts = {}
        sheet["text"] = texts
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    content = []
    for line in value.split("\n"):
        if line.strip():
            content.append({"type": "paragraph",
                            "content": [{"type": "text", "text": line}]})
        else:
            content.append({"type": "paragraph"})
    doc = {"type": "doc", "content": content}
    texts[key] = {"value": {"data": doc, "size": len(value)}}
    return True


def _notes_key_order(key: str):
    """Порядок заметок: notes-1, notes-2…, затем notes-left/right."""
    match = re.search(r"(\d+)$", key)
    if match:
        return (0, int(match.group(1)), key)
    return (1, 0, key)


def notes_blocks(sheet: dict) -> list:
    """Заметки листа для блока внизу листа персонажа.

    На сайте LSS заметки — это текстовые блоки Notes: они лежат в ``text``
    под ключами ``notes-1``, ``notes-2``… (у части листов — ``notes-left``
    и ``notes-right``). Показываем их все по порядку, каждую под своим
    заголовком. Если заметок в листе нет, отдаём одну пустую ``notes-1``,
    чтобы игрок мог начать писать прямо в приложении.
    """
    texts = sheet.get("text") if isinstance(sheet, dict) else None
    keys = []
    if isinstance(texts, dict):
        keys = [str(key) for key in texts
                if str(key).casefold().startswith("notes")]
    keys.sort(key=_notes_key_order)
    if not keys:
        keys = ["notes-1"]
    return [{"key": key,
             "title": "Заметки" if index == 0 else f"Заметки {index + 1}",
             "text": text_block(sheet, key)}
            for index, key in enumerate(keys)]


def unwrap_sheet(value):
    """Достать сам лист из значения: файл, строка JSON или обёртка LSS.

    Нужно, чтобы оружие находилось одинаково и у файла (.json с полем
    ``data``), и у листа, уже лежащего в памяти. Чужое значение — None.
    """
    if isinstance(value, dict):
        if "stats" in value:
            return value
        inner = value.get("data")
        return unwrap_sheet(inner) if inner is not None else None
    if isinstance(value, str):
        try:
            return unwrap_sheet(json.loads(value))
        except json.JSONDecodeError:
            return None
    return None


def parse(text: str) -> dict:
    """Разобрать файл LSS. Бросает ValueError, если это не лист персонажа."""
    try:
        outer = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Файл не похож на JSON: {exc}") from exc
    raw = outer.get("data", outer)
    sheet = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(sheet, dict) or "stats" not in sheet:
        raise ValueError("В файле нет характеристик — это не лист персонажа LSS")
    # Книга заклинаний LSS живёт в обёртке, а не в data: только ссылки на
    # заклинания (id/слаги), без текста. Раньше parse её выбрасывал, поэтому
    # вкладка «Заклинания» у загруженного листа всегда была пустой.
    spellbook = outer.get("spells")
    if isinstance(spellbook, dict):
        block = sheet.setdefault("vtt", {})
        block["lss_spells"] = copy.deepcopy(spellbook)
        block["lss_edition"] = str(outer.get("edition") or
                                   sheet.get("edition") or "")
    return sheet


def dump(sheet: dict) -> str:
    """Собрать файл, который примет LSS (внешняя обёртка + JSON-строка)."""
    # Не теряем книгу заклинаний, которая пришла из LSS: без этого
    # сохранение файла затирало её пустым объектом.
    preserved = ((sheet.get("vtt") or {}).get("lss_spells")
                 if isinstance(sheet, dict) else None)
    spellbook = (copy.deepcopy(preserved)
                 if isinstance(preserved, dict) else
                 {"mode": "text", "prepared": [], "book": []})
    outer = {
        "tags": [],
        "disabledBlocks": {"info-left": [], "info-right": [],
                           "notes-left": [], "notes-right": []},
        "spells": spellbook,
        "data": json.dumps(sheet, ensure_ascii=False),
        "jsonType": "character",
        "version": "2",
    }
    return json.dumps(outer, ensure_ascii=False, indent=2)


def summary(sheet: dict) -> dict:
    """Короткая сводка для карточки в интерфейсе."""
    info = sheet.get("info", {})
    vit = sheet.get("vitality", {})
    return {
        "name": (sheet.get("name") or {}).get("value", "Без имени"),
        "class": (info.get("charClass") or {}).get("value", ""),
        "level": (info.get("level") or {}).get("value", ""),
        "race": (info.get("race") or {}).get("value", ""),
        "hp": (vit.get("hp-current") or {}).get("value"),
        "hp_max": (vit.get("hp-max") or {}).get("value"),
        "ac": (vit.get("ac") or {}).get("value"),
        "speed": (vit.get("speed") or {}).get("value"),
        "proficiency": sheet.get("proficiency", 2),
    }


def new_sheet(name: str) -> dict:
    """Пустой лист с нормальными значениями по умолчанию."""
    return {
        "isDefault": True,
        "jsonType": "character",
        "template": "default",
        "name": {"value": name},
        "info": {
            "charClass": {"name": "charClass", "label": "класс", "value": ""},
            "level": {"name": "level", "label": "уровень", "value": 1},
            "race": {"name": "race", "label": "раса", "value": ""},
            "background": {"name": "background", "label": "предыстория", "value": ""},
            "playerName": {"name": "playerName", "label": "игрок", "value": ""},
            "alignment": {"name": "alignment", "label": "мировоззрение", "value": ""},
            "experience": {"name": "experience", "label": "опыт", "value": 0},
        },
        "proficiency": 2,
        "stats": {a: {"name": a, "label": ABILITY_RU[a], "score": 10,
                      "modifier": 0, "check": 0} for a in ABILITIES},
        "saves": {a: {"name": a, "isProf": False} for a in ABILITIES},
        "skills": {k: {"baseStat": v[0], "name": k, "label": v[1], "isProf": False}
                   for k, v in SKILLS.items()},
        "vitality": {
            "hp-current": {"value": 10}, "hp-max": {"value": 10},
            "hp-temp": {"value": 0}, "ac": {"value": 10},
            "speed": {"value": 30}, "initiative": {"value": 0},
            "isDying": False, "deathFails": 0, "deathSuccesses": 0,
        },
        "text": {},
        "coins": {k: {"value": 0} for k in ("cp", "sp", "ep", "gp", "pp")},
        "weaponsList": [],
        "resources": {},
    }
