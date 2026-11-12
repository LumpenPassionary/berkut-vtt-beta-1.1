"""Классовые особенности, которые за столом действительно применяют.

Справочник листа хранит особенность строкой: «Божественная кара — потратив
ячейку, добавить 2к8 излучением». Такой текст нельзя ни посчитать, ни
спросить у игрока, сколько ячеек он тратит. Этот модуль превращает три
особенности в данные, по которым работают и клиент, и сервер:

  * **Божественная кара** (паладин 2): при попадании рукопашным оружием
    потратить ячейку — 2к8 излучением, +1к8 за каждый круг выше первого,
    но не больше 5к8; по нежити и исчадиям ещё +1к8 (предел 6к8).
  * **Наложение рук** (паладин 1): запас лечения 5 × уровень паладина;
    действием передать любое количество хитов касанием и за 5 хитов из
    запаса снять отравление или болезнь. Запас возвращается на долгом отдыхе.
  * **Метка охотника** (следопыт 1): концентрация до часа, 90 футов;
    попав по помеченной цели, следопыт добавляет 1к6 к урону атаки.

Почему отдельный модуль: это правила, а не разметка. Здесь нет Qt и сети,
поэтому таблицы кубиков и запасы проверяются тестами, а сервер и клиент
считают одинаково. Модуль ничего не выдумывает: если у листа нет уровня
паладина, кара и наложение рук недоступны, а не «примерно работают».
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from shared import lss_format as lss
from shared import sheet_items

SMITE = "divine_smite"
LAY_ON_HANDS = "lay_on_hands"
HUNTERS_MARK = "hunters_mark"

#: Кара — урон излучением, а не урон оружия: сопротивление цели к огню её
#: не уменьшает, зато сопротивление излучению уменьшает.
SMITE_DAMAGE_TYPE = "radiant"
SMITE_BASE_DICE = 2          # 2к8 за ячейку 1 круга
SMITE_MAX_DICE = 5           # больше 5к8 кара не даёт
SMITE_EXTRA_DICE = 1         # +1к8 по нежити и исчадиям
SMITE_MAX_EXTRA = 6          # предел с бонусом
SMITE_MIN_PALADIN_LEVEL = 2

LAY_ON_HANDS_PER_LEVEL = 5
LAY_ON_HANDS_CURE_COST = 5
LAY_ON_HANDS_REACH_FT = 5

MARK_DIE = "1d6"
MARK_RANGE_FT = 90

RAGE = "rage"
RAGE_ROUNDS = 10                     # 1 минута — столько длится ярость

#: Скрытность и скрытая атака (плут).
HIDDEN = "Скрыт"
SNEAK_ATTACK = "sneak_attack"
#: Фехтовальное оружие 5e (SRD): им наносят скрытую атаку в рукопашной.
#: Список нужен там, где у оружия нет поля «фехтовальное»: лист хранит
#: только название, характеристику и дальнобойность.
FINESSE_WEAPONS = (
    "кинжал", "рапира", "короткий меч", "серп", "кнут", "дротик", "дубинка",
    "dagger", "rapier", "shortsword", "short sword", "sickle", "whip",
    "dart", "club",
)
RAGE_RESIST = ("bludgeoning", "piercing", "slashing")

#: Все классы SRD: ключ → как он может быть назван в листе.
#: `klass` в CLASS_FEATURES (`shared.sheet_audit`) ссылается на этот ключ,
#: чтобы уровень брался именно у этого класса, а не у суммы уровней.
CLASS_KEYS = {
    "barbarian": ("варвар", "barbarian"),
    "bard": ("бард", "bard"),
    "cleric": ("жрец", "cleric"),
    "druid": ("друид", "druid"),
    "fighter": ("воин", "боец", "fighter"),
    "monk": ("монах", "monk"),
    "paladin": ("паладин", "paladin"),
    "ranger": ("следопыт", "ranger"),
    "rogue": ("плут", "rogue"),
    "sorcerer": ("чародей", "sorcerer"),
    "warlock": ("колдун", "warlock"),
    "wizard": ("волшебник", "wizard"),
}

#: Короткие имена для подписей и логов.
CLASS_RU = {
    "barbarian": "Варвар", "bard": "Бард", "cleric": "Жрец",
    "druid": "Друид", "fighter": "Воин", "monk": "Монах",
    "paladin": "Паладин", "ranger": "Следопыт", "rogue": "Плут",
    "sorcerer": "Чародей", "warlock": "Колдун", "wizard": "Волшебник",
}

#: Типы существ, по которым кара бьёт сильнее (SRD: Undead, Fiend).
_EXTRA_TYPE_WORDS = (
    "нежить", "нежит", "undead", "исчадие", "fiend", "демон", "demon",
    "дьявол", "devil", "дьявол", "юголот", "yugoloth", "демоническ",
)


def _cell(value):
    """Значение ячейки LSS: ``{"value": …}`` или как есть."""
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def _class_text(sheet: dict) -> str:
    info = (sheet or {}).get("info") or {}
    return str(_cell(info.get("charClass")) or "").strip().casefold()


def _level(sheet: dict) -> int:
    try:
        return max(0, int(lss.character_level(sheet)))
    except (TypeError, ValueError):
        return 0


#: Заметки листа: там игроки пишут «5 следопыта, 3 плута, 3 воина».
_NOTES_KEYS = ("notes-1", "notes-2", "notes-3", "notes-4", "notes-5",
               "notes-6", "notes-7", "notes-8", "notes-left", "notes-right")
_CLASS_NUMBER = r"(\d{1,2})"
_NUMBER_WORDS = ("уровень", "уровня", "уровней", "ур", "level", "lvl")

# ----------------------------------------------------------------------
# Кэш разбора заметок и уровней
# ----------------------------------------------------------------------
# `class_levels()` — чистая функция от строки класса, общего уровня и текста
# заметок, но раньше каждый вызов заново разбирал блоки LSS (`json.loads`
# по каждому блоку). За одну перерисовку листа функция вызывается десятки
# раз (на каждую особенность, на каждую запись аудита), и лист мультикласса
# считался секундами: открытие листа, «Второе дыхание» и отдых заметно
# подвисали. Кэш — по самим входам функции, поэтому он не зависит от того,
# какой именно словарь листа пришёл, и не может вернуть чужое значение.
_CACHE_LIMIT = 24
_NOTES_CACHE: Dict[object, str] = {}
_LEVELS_CACHE: Dict[object, Dict[str, int]] = {}


def _remember(cache: Dict[object, object], signature, value):
    """Запомнить разбор, не давая кэшу расти без границ."""
    if len(cache) >= _CACHE_LIMIT:
        cache.pop(next(iter(cache)), None)
    cache[signature] = value
    return value


def _notes_signature(sheet: dict):
    """Дешёвый отпечаток блоков заметок: без разбора дерева LSS.

    ``repr`` значения — работа уровня C: на заметки листа уходит меньше
    микросекунды, тогда как `text_block` разбирает абзацы каждого блока.
    """
    texts = (sheet or {}).get("text") if isinstance(sheet, dict) else None
    if not isinstance(texts, dict):
        return ()
    signature = []
    for key in _NOTES_KEYS:
        if key not in texts:
            continue
        raw = repr(texts.get(key))
        signature.append((key, len(raw), hash(raw)))
    return tuple(signature)


def _notes_text(sheet: dict) -> str:
    """Текст заметок листа: там обычно и написан состав мультикласса.

    В LSS заметки — это блоки `text.notes-1…`, а не одно поле. Ошибка
    читать только `info.charClass`: при мультиклассе там бывает «Следопыт»
    без чисел, а реальные уровни игрок пишет в заметках.
    """
    texts = (sheet or {}).get("text") if isinstance(sheet, dict) else None
    if not isinstance(texts, dict):
        return ""
    signature = _notes_signature(sheet)
    cached = _NOTES_CACHE.get(signature)
    if cached is not None:
        return cached
    parts = []
    for key in _NOTES_KEYS:
        if key in texts:
            try:
                block = lss.text_block(sheet, key)
            except Exception:
                block = ""
            if block:
                parts.append(block)
    return _remember(_NOTES_CACHE, signature, "\n".join(parts))


def _numbers_near(text: str, alias: str) -> List[int]:
    """Числа рядом с названием класса: «Плут 3», «3 уровня плута».

    Берём только числа в пределах 1–20 и только в пределах 24 символов,
    иначе «Следопыт 5 / Воин 3» дал бы следопыту и тройку воина.
    """
    if not text:
        return []
    found: List[int] = []
    escaped = re.escape(alias)
    after = re.compile(escaped + r"[^\d]{0,16}" + _CLASS_NUMBER, re.IGNORECASE)
    before = re.compile(_CLASS_NUMBER + r"[^\d]{0,16}" + escaped, re.IGNORECASE)
    for pattern in (after, before):
        for value in pattern.findall(text):
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= number <= 20:
                found.append(number)
    return found


def _composition_levels(text: str) -> Dict[str, int]:
    """Уровни из строки состава мультикласса: «Следопыт 5/плут 2/воин 3».

    Рядом с составом в заметках полно других чисел: «Плут 1 уровень: 10»
    (хиты по уровням), «ИТОГО ХП: 101», «+4 мастерство». Если брать первое
    попавшееся число, у Рингера выходило 1 уровень плута и воина — и Второе
    дыхание лечило 1к10+1 вместо 1к10+3. Поэтому состав ищем по строке, где
    названы и пронумерованы сразу два и более классов.
    """
    fallback: Dict[str, int] = {}
    for line in str(text or "").splitlines():
        keys = _mentioned(line)
        if len(keys) < 2:
            continue
        found: Dict[str, int] = {}
        for key in keys:
            for alias in CLASS_KEYS.get(key, ()):
                numbers = _numbers_near(line, alias)
                if numbers:
                    found[key] = numbers[0]
                    break
        if len(found) < 2:
            continue
        folded = line.casefold()
        if "мультикласс" in folded or "мульти класс" in folded:
            return found
        if not fallback:
            fallback = found
    return fallback


def _mentioned(text: str) -> List[str]:
    """Классы, которые вообще названы в тексте, в порядке упоминания.

    Только целыми словами: «воинственный» — не воин, иначе заметка о
    характере добавляла бы персонажу второе дыхание.
    """
    folded = str(text or "").casefold().replace("ё", "е")
    positions = []
    for key, aliases in CLASS_KEYS.items():
        best = None
        for alias in aliases:
            match = re.search(r"(?<![а-яa-z])" + re.escape(alias)
                              + r"(?![а-яa-z])", folded)
            if match and (best is None or match.start() < best):
                best = match.start()
        if best is not None:
            positions.append((best, key))
    return [key for _index, key in sorted(positions)]


def class_levels(sheet: dict) -> Dict[str, int]:
    """Уровни по каждому классу, а не сумма всех уровней листа.

    Источник чисел — сначала строка класса («Паладин 6 / Воин 5»), затем
    заметки («5 следопыта, 3 плута»). Если чисел нет вовсе, все названные
    классы получают общий уровень листа — прежнее поведение, чтобы лист
    без подробностей продолжал работать.

    Мультикласс по правилам 5e складывает уровни классов, но особенности
    каждого класса считаются только от его уровня: плут 3 не даёт 6к6
    скрытой атаки только потому, что у персонажа 11 уровней всего.
    """
    sheet = sheet if isinstance(sheet, dict) else {}
    total = _level(sheet)
    class_text = _class_text(sheet)
    notes = _notes_text(sheet)
    signature = (class_text, total, notes)
    cached = _LEVELS_CACHE.get(signature)
    if cached is not None:
        # Копия: вызывающий код (карточка, аудит) иногда правит результат.
        return dict(cached)
    levels: Dict[str, int] = {}
    numbered: Dict[str, int] = {}
    mentioned: List[str] = []
    line_mentioned = _mentioned(class_text)
    # Явный состав мультикласса важнее разрозненных чисел из заметок:
    # у Рингера есть и «Плут 1 уровень: 10», и «МУЛЬТИКЛАСС Следопыт 5/
    # плут 2/воин 3». Без этого особенности считались от чужих уровней.
    composition = _composition_levels(notes)
    if composition:
        levels.update(composition)
        numbered.update(composition)
        mentioned.extend(key for key in composition if key not in mentioned)
    for source in (class_text, notes):
        for key in _mentioned(source):
            if key not in mentioned:
                mentioned.append(key)
            if key in levels:
                continue
            aliases = CLASS_KEYS[key]
            numbers = []
            for alias in aliases:
                numbers = _numbers_near(source, alias)
                if numbers:
                    break
            if numbers:
                levels[key] = numbers[0]
                numbered[key] = numbers[0]
    if not mentioned:
        return _remember(_LEVELS_CACHE, signature, {})
    if not numbered:
        # Чисел нет: считаем, что все уровни листа принадлежат классу из
        # шапки. Так было и раньше, и это лучше нулевого запаса лечения.
        return _remember(_LEVELS_CACHE, signature,
                         {key: total for key in mentioned if total})
    # Класс без числа забирает остаток уровней — но только если он назван
    # в строке класса. Упоминание в свободных заметках без числа не делает
    # персонажа мультиклассом: «когда-то служил воином» — не уровень.
    assigned = sum(numbered.values())
    remainder = max(0, total - assigned)
    for key in line_mentioned:
        if key in levels:
            continue
        levels[key] = min(20, remainder) if remainder else 0
        remainder = max(0, remainder - levels[key])
    return _remember(_LEVELS_CACHE, signature,
                     {key: value for key, value in levels.items() if value > 0})


def class_level(sheet: dict, klass: str) -> int:
    """Уровень одного класса в листе (0 — такого класса нет)."""
    key = str(klass or "").casefold()
    if key not in CLASS_KEYS:
        # Старые вызовы передавали русское название.
        for name, aliases in CLASS_KEYS.items():
            if key in aliases:
                key = name
                break
        else:
            return 0
    return int(class_levels(sheet).get(key, 0) or 0)


def class_level_for(sheet: dict, klass_key: str) -> int:
    """Уровень по ключу из CLASS_FEATURES (fighter, rogue…)."""
    return int(class_levels(sheet).get(str(klass_key or ""), 0) or 0)


def keys_for_aliases(aliases) -> List[str]:
    """Все классы, которым принадлежит особенность.

    «Компетентность» есть у плута и барда, «Увёртливость» — у плута и
    монаха: выбрать первый по алфавиту нельзя, иначе у плута пропадала
    бы кнопка, а у барда появлялась чужая.
    """
    text = " ".join(str(alias) for alias in (aliases or ())).casefold()
    out = []
    for key, names in CLASS_KEYS.items():
        for alias in names:
            if re.search(r"(?<![а-яёa-z])" + re.escape(alias)
                         + r"(?![а-яёa-z])", text):
                out.append(key)
                break
    return out


def key_for_aliases(aliases) -> str:
    """Первый класс по названиям (для совместимости)."""
    found = keys_for_aliases(aliases)
    return found[0] if found else ""


def best_class(sheet: dict, aliases) -> tuple:
    """Класс с наибольшим уровнем среди названных: (ключ, уровень).

    У мультикласса плут 3 / бард 1 «Компетентность» должна считаться от
    плута: иначе кубик и уровень были бы от чужого класса.
    """
    levels = class_levels(sheet)
    best_key, best_level = "", 0
    for key in keys_for_aliases(aliases):
        value = int(levels.get(key, 0) or 0)
        if value > best_level:
            best_key, best_level = key, value
    return best_key, best_level


def level_for_aliases(sheet: dict, aliases) -> int:
    """Уровень класса, названного в CLASS_FEATURES, а не сумма всех уровней."""
    return best_class(sheet, aliases)[1]


def multiclass_note(sheet: dict) -> str:
    """Строка для чата: «Следопыт 5, Плут 3, Воин 3»."""
    levels = class_levels(sheet)
    if not levels:
        return ""
    parts = [f"{CLASS_RU.get(key, key)} {value}" for key, value in
             sorted(levels.items(), key=lambda item: -item[1])]
    return ", ".join(parts)


# ----------------------------------------------------------------------
# Особенности листа
# ----------------------------------------------------------------------
def features(sheet: dict) -> List[dict]:
    block = (sheet or {}).get("vtt") if isinstance(sheet, dict) else None
    raw = (block or {}).get("features") if isinstance(block, dict) else None
    return [feature for feature in (raw or []) if isinstance(feature, dict)]


def feature(sheet: dict, key: str) -> Optional[dict]:
    for entry in features(sheet):
        if str(entry.get("key") or "") == str(key or ""):
            return entry
    return None


def has(sheet: dict, key: str) -> bool:
    """Есть ли особенность в листе (или её даёт класс по правилам)."""
    if feature(sheet, key) is not None:
        return True
    if key == SMITE:
        return class_level(sheet, "паладин") >= SMITE_MIN_PALADIN_LEVEL
    if key == LAY_ON_HANDS:
        return class_level(sheet, "паладин") >= 1
    return False


# ----------------------------------------------------------------------
# Божественная кара
# ----------------------------------------------------------------------
def smite_dice(slot_level: int, extra: bool = False) -> str:
    """Кубик кары для ячейки указанного круга.

    Правило: 2к8 за первый круг, +1к8 за каждый круг выше, предел 5к8;
    по нежити и исчадиям +1к8 сверх этого, но не больше 6к8.
    """
    try:
        slot_level = int(slot_level)
    except (TypeError, ValueError):
        return ""
    if slot_level < 1:
        return ""
    count = SMITE_BASE_DICE + max(0, slot_level - 1)
    count = min(SMITE_MAX_DICE, count)
    if extra:
        count = min(SMITE_MAX_EXTRA, count + SMITE_EXTRA_DICE)
    return f"{count}d8"


def free_slots(sheet: dict) -> List[dict]:
    """Свободные ячейки по кругам: [{'level', 'left', 'dice'}]."""
    out = []
    for level in range(1, 10):
        cells = sheet_items.slots_for(sheet, level)
        maximum = int(cells.get("max", 0) or 0)
        used = int(cells.get("used", 0) or 0)
        left = maximum - used
        if left <= 0:
            continue
        out.append({"level": level, "left": left,
                    "dice": smite_dice(level)})
    return out


def smite_plan(sheet: dict, slot_level: int, extra: bool = False) -> Optional[dict]:
    """План кары: доступна ли она и что именно бросать.

    Возвращает {} (None), если кара недоступна или ячейки этого круга нет:
    сервер обязан отказать, а не «округлить» до ближайшей свободной. Иначе
    игрок выбрал бы «1 круг» и получил кубик третьего.
    """
    if not has(sheet, SMITE):
        return None
    try:
        slot_level = int(slot_level)
    except (TypeError, ValueError):
        return None
    if slot_level < 1 or slot_level > 9:
        return None
    cells = sheet_items.slots_for(sheet, slot_level)
    if int(cells.get("used", 0) or 0) >= int(cells.get("max", 0) or 0):
        return None
    dice = smite_dice(slot_level, extra=extra)
    if not dice:
        return None
    return {
        "slot_level": slot_level,
        "dice": dice,
        "extra": bool(extra),
        "damage_type": SMITE_DAMAGE_TYPE,
        "label": ("Божественная кара" + (" (нежить или исчадие)" if extra else "")),
    }


def spend_slot(sheet: dict, slot_level: int) -> bool:
    """Потратить ячейку ровно выбранного круга.

    ``sheet_items.spend_slot`` умеет «повышать» круг, когда своих ячеек нет.
    Для кары это ошибка: кубик уже посчитан по выбранному кругу.
    """
    try:
        slot_level = int(slot_level)
    except (TypeError, ValueError):
        return False
    if slot_level < 1:
        return False
    cells = sheet_items.slots_for(sheet, slot_level)
    if int(cells.get("used", 0) or 0) >= int(cells.get("max", 0) or 0):
        return False
    cells["used"] = int(cells.get("used", 0) or 0) + 1
    return True


def is_fiend_or_undead(target, sheet: dict = None) -> bool:
    """Нежить или исчадие? Тип берём у статблока токена.

    У персонажей тип «гуманоид», поэтому по листу кара бьёт как обычно.
    Если тип не записан, бонуса нет: догадка здесь дороже пропущенного
    кубика — мастер поправит вручную.
    """
    parts = []
    statblock = getattr(target, "statblock", None)
    if isinstance(statblock, dict):
        parts.append(str(statblock.get("type") or ""))
    if isinstance(sheet, dict):
        info = sheet.get("info") or {}
        parts.append(str(_cell(info.get("race")) or ""))
    text = " ".join(parts).casefold().replace("ё", "е")
    if not text.strip():
        return False
    return any(word in text for word in _EXTRA_TYPE_WORDS)


# ----------------------------------------------------------------------
# Наложение рук
# ----------------------------------------------------------------------
def lay_on_hands_max(sheet: dict) -> int:
    """Запас лечения: 5 × уровень паладина (0 — не паладин)."""
    return max(0, LAY_ON_HANDS_PER_LEVEL * class_level(sheet, "паладин"))


def _pool(sheet: dict) -> dict:
    block = sheet.setdefault("vtt", {}) if isinstance(sheet, dict) else {}
    if not isinstance(block, dict):
        return {}
    pool = block.setdefault("lay_on_hands", {})
    if not isinstance(pool, dict):
        pool = {}
        block["lay_on_hands"] = pool
    return pool


def lay_on_hands_used(sheet: dict) -> int:
    pool = _pool(sheet)
    try:
        return max(0, int(pool.get("used") or 0))
    except (TypeError, ValueError):
        return 0


def lay_on_hands_left(sheet: dict) -> int:
    return max(0, lay_on_hands_max(sheet) - lay_on_hands_used(sheet))


def lay_on_hands_spend(sheet: dict, amount) -> bool:
    """Списать хиты из запаса. Больше остатка — отказ."""
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return False
    if amount <= 0:
        return False
    left = lay_on_hands_left(sheet)
    if amount > left:
        return False
    pool = _pool(sheet)
    pool["used"] = lay_on_hands_used(sheet) + amount
    return True


def lay_on_hands_restore(sheet: dict) -> None:
    """Долгий отдых возвращает весь запас."""
    pool = _pool(sheet)
    if pool:
        pool["used"] = 0


def lay_on_hands_cost(heal: int = 0, cure: bool = False) -> int:
    """Сколько хитов запаса стоит применение.

    Лечение стоит ровно столько, сколько передали; снятие отравления или
    болезни — 5 хитов сверх лечения (или само по себе).
    """
    try:
        heal = max(0, int(heal))
    except (TypeError, ValueError):
        heal = 0
    return heal + (LAY_ON_HANDS_CURE_COST if cure else 0)


# ----------------------------------------------------------------------
# Метка охотника
# ----------------------------------------------------------------------
def mark_on(target, caster_id: str) -> Optional[dict]:
    """Активная метка этого колдуна на цели (или None)."""
    if target is None or not caster_id:
        return None
    for effect in (getattr(target, "active_effects", None) or []):
        if not isinstance(effect, dict):
            continue
        if str(effect.get("type") or "") != "mark":
            continue
        if str(effect.get("caster_id") or "") != str(caster_id):
            continue
        return effect
    return None


def mark_die(critical: bool = False) -> str:
    """Кубик метки: при крите кости атаки бросаются дважды — тоже 2к6."""
    return "2d6" if critical else MARK_DIE


# ----------------------------------------------------------------------
# Ярость
# ----------------------------------------------------------------------
def rage_bonus(sheet: dict) -> int:
    """Прибавка к урону от ярости: +2, с 9 уровня +3, с 16 — +4."""
    level = class_level(sheet, "варвар")
    if level >= 16:
        return 4
    if level >= 9:
        return 3
    return 2 if level >= 1 else 0


def rage_effect(sheet: dict) -> Optional[dict]:
    """Эффект для сервера: ярость на себя на 1 минуту.

    Кубик и длительность считаются по листу, а не в интерфейсе: сервер
    пересчитает бонус сам, чтобы клиент не мог прислать «+9 к урону».
    """
    bonus = rage_bonus(sheet)
    if not bonus:
        return None
    return {"type": RAGE, "bonus": bonus, "rounds": RAGE_ROUNDS,
            "duration": "1 минута", "fx": RAGE,
            "label": f"Ярость (+{bonus} к урону)"}


def rage_on(token) -> Optional[dict]:
    """Активная ярость на токене (или None)."""
    for effect in (getattr(token, "active_effects", None) or []):
        if not isinstance(effect, dict):
            continue
        if str(effect.get("type") or "") == RAGE:
            return effect
    return None


def add_damage_bonus(formula: str, bonus: int) -> str:
    """Добавить плоскую прибавку к формуле урона.

    Бросок урона понимает только один модификатор, поэтому «1d8+4» и «+2»
    нужно сложить в «1d8+6», а не склеивать в «1d8+4+2». При крите удвоятся
    только кости, а общая прибавка останется одна — как в 5e.
    """
    text = str(formula or "").strip()
    try:
        bonus = int(bonus)
    except (TypeError, ValueError):
        return text
    if not text or bonus <= 0:
        return text
    match = re.search(r"([+-])\s*(\d+)\s*$", text)
    if match:
        value = int(match.group(2))
        if match.group(1) == "-":
            value = -value
        return f"{text[:match.start()].rstrip()}{value + bonus:+d}"
    return f"{text}+{bonus}"


def rage_damage_bonus(token, ranged: bool = False,
                      ability: str = "") -> int:
    """Сколько ярость добавляет к этой атаке.

    По правилам — только рукопашная атака оружием с Силой. Оружие
    ловкости (фехтовальное с Ловкостью) бонус не получает, а дальний бой —
    тем более. Пустая характеристика считается Силой: так приходит ручная
    атака без выбранного оружия.
    """
    effect = rage_on(token)
    if not effect or ranged:
        return 0
    ability = str(ability or "").strip().casefold()
    if ability not in ("", "str"):
        return 0
    try:
        return max(0, min(4, int(effect.get("bonus") or 0)))
    except (TypeError, ValueError):
        return 0


# ----------------------------------------------------------------------
# Заряды классовых особенностей
# ----------------------------------------------------------------------
# В 5e у многих способностей есть счётчик «сколько раз до отдыха».
# Без него Второе дыхание лечило бесконечно, а Вдохновение барда не
# кончалось. Максимум считается от уровня своего класса, расход живёт
# в листе (`vtt.feature_uses`), отдых возвращает заряды.

def _ability_mod(sheet: dict, ability: str) -> int:
    stats = (sheet or {}).get("stats") or {}
    score = (stats.get(ability) or {}).get("score", 10)
    try:
        return lss.modifier(int(score))
    except (TypeError, ValueError):
        return 0


def _rage_uses(sheet: dict) -> int:
    level = class_level(sheet, "варвар")
    return (6 if level >= 17 else 5 if level >= 12 else 4 if level >= 6
            else 3 if level >= 3 else 2 if level >= 1 else 0)


def _channel_uses(level: int) -> int:
    """Сколько раз Божественный канал доступен на этом уровне класса."""
    return 3 if level >= 18 else 2 if level >= 6 else 1 if level >= 2 else 0


def _charges_max(sheet: dict, key: str) -> int:
    """Сколько зарядов даёт особенность на этом листе (0 — без зарядов)."""
    level = {
        "second_wind": class_level(sheet, "воин"),
        "action_surge": class_level(sheet, "воин"),
        "indomitable": class_level(sheet, "воин"),
        "bardic_inspiration": class_level(sheet, "бард"),
        "ki": class_level(sheet, "монах"),
        "sorcery_points": class_level(sheet, "чародей"),
        "wild_shape": class_level(sheet, "друид"),
    }.get(key, 0)
    if key == "second_wind":
        return 1 if level >= 1 else 0
    if key == "action_surge":
        return (2 if level >= 17 else 1) if level >= 2 else 0
    if key == "indomitable":
        return (3 if level >= 17 else 2 if level >= 13 else 1) \
            if level >= 9 else 0
    if key == "bardic_inspiration":
        return max(1, _ability_mod(sheet, "cha")) if level >= 1 else 0
    if key == "ki":
        return level if level >= 2 else 0
    if key == "sorcery_points":
        return level if level >= 2 else 0
    if key == "wild_shape":
        return 2 if level >= 2 else 0
    if key == "rage":
        return _rage_uses(sheet)
    # Каналы паладина и жреца — разные запасы: мультикласс паладин 3 / жрец 6
    # получает 1 + 2 использования, а не 2 на оба класса (правила 5e).
    if key == "paladin_channel":
        return _channel_uses(class_level(sheet, "паладин"))
    if key == "channel_divinity":
        return _channel_uses(class_level(sheet, "жрец"))
    return 0


def _recharge(sheet: dict, key: str) -> str:
    """Когда возвращаются заряды: short / long."""
    if key in ("indomitable", "sorcery_points", "rage"):
        return "long"
    if key == "bardic_inspiration" \
            and class_level(sheet, "бард") < 5:
        # До «Источника вдохновения» (5 уровень) — только долгий отдых.
        return "long"
    return "short"


def _uses_block(sheet: dict) -> dict:
    block = sheet.setdefault("vtt", {}) if isinstance(sheet, dict) else {}
    if not isinstance(block, dict):
        return {}
    used = block.setdefault("feature_uses", {})
    if not isinstance(used, dict):
        used = {}
        block["feature_uses"] = used
    return used


def _entry_used(entry) -> int:
    """Сколько зарядов потрачено: число или {'used', 'recharge'}."""
    if isinstance(entry, dict):
        entry = entry.get("used")
    try:
        return max(0, int(entry or 0))
    except (TypeError, ValueError):
        return 0


def _entry_recharge(entry, sheet: dict, key: str) -> str:
    """Когда возвращается заряд: сохранённое значение важнее таблицы."""
    if isinstance(entry, dict) and entry.get("recharge"):
        return str(entry["recharge"])
    if entry is not None:
        return _recharge(sheet, key)
    return ""


def feature_resource(sheet: dict, key: str,
                     recharge: str = "") -> Optional[dict]:
    """Заряды особенности: {'max', 'used', 'left', 'recharge'} или None.

    `recharge` — поле самой особенности из листа. Если у правила нет
    таблицы зарядов, но оно прямо говорит «раз до отдыха» (Божественное
    чувство, Магическое восстановление, Удар удачи), заряд один.
    """
    if not isinstance(sheet, dict) or not key:
        return None
    maximum = _charges_max(sheet, key)
    declared = str(recharge or "")
    if maximum <= 0 and declared in ("short", "long"):
        maximum = 1
    if maximum <= 0:
        return None
    entry = _uses_block(sheet).get(key)
    used = _entry_used(entry)
    when = _entry_recharge(entry, sheet, key) or declared or _recharge(sheet, key)
    return {"max": maximum, "used": used,
            "left": max(0, maximum - used), "recharge": when}


def feature_uses_left(sheet: dict, key: str, recharge: str = "") -> int:
    resource = feature_resource(sheet, key, recharge)
    return int(resource["left"]) if resource else 0


def feature_spend(sheet: dict, key: str, recharge: str = "") -> bool:
    """Потратить заряд. Без зарядов или без счётчика — False."""
    resource = feature_resource(sheet, key, recharge)
    if not resource or resource["left"] <= 0:
        return False
    _uses_block(sheet)[key] = {"used": int(resource["used"]) + 1,
                               "recharge": resource["recharge"]}
    return True


def sneak_dice(sheet: dict) -> int:
    """Сколько к6 даёт скрытая атака: 1к6 на 1 уровне плута, +1к6 за 2 уровня.

    Считается только по уровню ПЛУТА, а не по сумме уровней листа:
    мультикласс «следопыт 5 / плут 3 / воин 3» имеет 2к6, а не 6к6.
    """
    level = class_level(sheet, "плут")
    if level < 1:
        return 0
    return max(1, (level + 1) // 2)


def sneak_formula(sheet: dict, critical: bool = False) -> str:
    """Формула костей скрытой атаки («2d6»); пусто — если персонаж не плут."""
    dice = sneak_dice(sheet)
    if not dice:
        return ""
    if critical:
        dice *= 2
    return f"{dice}d6"


def stealth_bonus(sheet: dict) -> int:
    """Бонус проверки Скрытности с листа (Ловкость + мастерство)."""
    if not isinstance(sheet, dict):
        return 0
    return lss.skill_bonus(sheet, "stealth")


def passive_perception(sheet: dict = None, token=None) -> int:
    """Пассивное Восприятие: 10 + бонус Внимания.

    У персонажа — с листа; у существа — из статблока (модификатор Мудрости и
    владение навыком, если оно записано). Если ничего нет, остаётся 10:
    скрытность всё равно требует хотя бы обычного броска.
    """
    if isinstance(sheet, dict) and sheet.get("stats"):
        return 10 + lss.skill_bonus(sheet, "perception")
    block = getattr(token, "statblock", None) or {}
    mod = 0
    try:
        from shared import token_stats
        mod = token_stats.token_ability_mod(token, sheet, "wis")
    except Exception:
        mod = 0
    for entry in (block.get("skills") or []):
        text = str(entry if not isinstance(entry, dict)
                   else entry.get("name") or entry.get("key") or "")
        if "perception" in text.casefold() or "внимание" in text.casefold():
            if isinstance(entry, dict):
                try:
                    mod = int(entry.get("mod") or entry.get("bonus") or mod)
                except (TypeError, ValueError):
                    pass
            break
    return 10 + int(mod)


def is_finesse_weapon(name: str = "", ranged: bool = False,
                      ability: str = "") -> bool:
    """Подходит ли оружие для скрытой атаки.

    В 5e — только фехтовальное или дальнобойное. Фехтовальность узнаём
    тремя способами: название из списка, дальнобойность и атака Ловкостью
    в рукопашной (в этом листе так записано фехтовальное оружие).
    """
    if ranged:
        return True
    text = str(name or "").strip().casefold()
    if text:
        for known in FINESSE_WEAPONS:
            if known in text:
                return True
    return str(ability or "").strip().casefold() == "dex"


def sneak_attack_plan(sheet: dict, *, name: str = "", ranged: bool = False,
                      ability: str = "", advantage: bool = False,
                      disadvantage: bool = False,
                      ally_within_5ft: bool = False,
                      used_this_turn: bool = False) -> Optional[dict]:
    """Можно ли добавить скрытую атаку к этому попаданию (5e).

    Условия: уровень плута; раз в ход; фехтовальное или дальнобойное
    оружие; преимущество на бросок ЛИБО союзник цели в 5 футах (и при этом
    нет помехи). Возвращает словарь с костями и причиной или None.
    """
    dice = sneak_dice(sheet)
    if not dice:
        return None
    if used_this_turn:
        return None
    if not is_finesse_weapon(name, ranged=ranged, ability=ability):
        return None
    if disadvantage:
        return None
    if not (advantage or ally_within_5ft):
        return None
    reason = ("преимущество на атаку" if advantage
              else "союзник цели в 5 футах")
    return {"dice": dice, "formula": f"{dice}d6", "reason": reason,
            "label": f"Скрытая атака {dice}к6"}


def restore_resources(sheet: dict, long: bool = False) -> list:
    """Вернуть заряды на отдыхе. Возвращает названия восстановленных.

    Короткий отдых возвращает только то, что по правилам восстанавливается
    на коротком отдыхе; долгий — всё, включая «раз до долгого отдыха».
    """
    used = _uses_block(sheet)
    restored = []
    for key in list(used):
        entry = used[key]
        when = (_entry_recharge(entry, sheet, key)
                or _recharge(sheet, key))
        if long or when == "short":
            if isinstance(entry, dict):
                entry["used"] = 0
                entry["recharge"] = when
            else:
                used[key] = 0
            restored.append(key)
    return restored
