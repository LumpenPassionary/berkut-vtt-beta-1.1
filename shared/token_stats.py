"""Характеристики токена: что берём с листа персонажа, а что из статблока.

Токен в этой игре — не документ актёра, как в Foundry, а лёгкая ссылка на
картинку и координаты. Поэтому все числа для бросков нужно уметь достать
из двух источников:

  * у игрока — из его листа Long Story Short (там `stats`, `saves`, `skills`);
  * у существа — из `Token.statblock` (туда кладётся запись справочника при
    призыве: модификаторы, спасброски, сопротивления).

Функции здесь ничего не меняют и не знают про сеть: их можно проверять
отдельно. Если данных нет, возвращаем разумное значение по умолчанию, а не
падаем — иначе один токен без статблока ломал бы весь бой.
"""

from shared import lss_format
from shared import special_attacks

#: Полное русское название характеристики для инструментов и заклинаний.
ABILITY_LABELS = {
    "str": "Сила", "dex": "Ловкость", "con": "Телосложение",
    "int": "Интеллект", "wis": "Мудрость", "cha": "Харизма",
}
ABILITIES = tuple(ABILITY_LABELS)

#: Принимаем и трёхбуквенные коды, и полные слова — в интерфейсе и в
#: статблоках встречается и то и другое.
_ABILITY_ALIASES = {
    "str": "str", "сила": "str", "сил": "str",
    "dex": "dex", "ловкость": "dex", "лов": "dex",
    "con": "con", "телосложение": "con", "телос": "con", "вын": "con",
    "int": "int", "интеллект": "int", "инт": "int",
    "wis": "wis", "мудрость": "wis", "мудр": "wis",
    "cha": "cha", "харизма": "cha", "хар": "cha",
}


def normalize_ability(value) -> str:
    """Привести характеристику к коду ("dex"), иначе пустая строка."""
    if not value:
        return ""
    return _ABILITY_ALIASES.get(str(value).strip().casefold(), "")


def _int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ----------------------------------------------------------------------
# Спасброски
# ----------------------------------------------------------------------

def statblock_save_bonus(statblock: dict, ability: str) -> int:
    """Бонус спасброска существа из статблока справочника.

    В записи `saves` лежит готовый список [{'key': 'dex', 'mod': 4}], уже
    посчитанный с бонусом мастерства. Если владения нет — берём модификатор
    характеристики: спасбросок без владения всё равно бросается.
    """
    ability = normalize_ability(ability)
    if not ability:
        return 0
    block = statblock or {}
    for entry in block.get("saves") or []:
        if not isinstance(entry, dict):
            continue
        # Записи справочника дают {"key","name","mod"}, но старые миры и
        # сторонние импорты присылали {"ability","bonus"} — принимаем оба вида.
        key = (entry.get("key") or entry.get("name")
               or entry.get("ability"))
        if normalize_ability(key) == ability:
            bonus = entry.get("mod")
            if bonus is None:
                bonus = entry.get("bonus")
            return _int(bonus)
    # Владения нет: модификатор из статблока или считаем из значения.
    mods = block.get("mods") or {}
    if ability in mods:
        return _int(mods[ability])
    score = (block.get("abilities") or {}).get(ability)
    if score is not None:
        return lss_format.modifier(score)
    return 0


def sheet_save_bonus(sheet: dict, ability: str) -> int:
    """Бонус спасброска персонажа — прямо с листа LSS (модификатор + владение)."""
    ability = normalize_ability(ability)
    if not ability or not isinstance(sheet, dict):
        return 0
    return lss_format.save_bonus(sheet, ability)


def token_save_bonus(token, sheet: dict = None, ability: str = "dex") -> int:
    """Бонус спасброска токена: лист игрока важнее статблока.

    Порядок именно такой: у персонажа игрока может быть кольцо защиты или
    класс, который даёт владение, — это точнее, чем статблок существа,
    случайно оказавшийся в токене.
    """
    if isinstance(sheet, dict) and sheet.get("stats"):
        return sheet_save_bonus(sheet, ability)
    statblock = getattr(token, "statblock", None)
    if statblock:
        return statblock_save_bonus(statblock, ability)
    # Последний вариант: у токена есть только Ловкость (её ставит призыв).
    if normalize_ability(ability) == "dex":
        return _int(getattr(token, "dex", 0) or 0)
    return 0


# ----------------------------------------------------------------------
# Производные значения
# ----------------------------------------------------------------------

def token_ability_mod(token, sheet: dict = None, ability: str = "dex") -> int:
    """Модификатор характеристики токена: лист, статблок, поле токена.

    Нужен там, где число входит в правило (КД Доспехов мага, инициатива),
    а не только в бросок: тогда сервер и карточка считают одинаково.
    """
    ability = normalize_ability(ability) or "dex"
    if isinstance(sheet, dict) and sheet.get("stats"):
        score = (sheet.get("stats", {}).get(ability) or {}).get("score", 10)
        return lss_format.modifier(score)
    statblock = getattr(token, "statblock", None)
    if statblock:
        mods = statblock.get("mods") or {}
        if ability in mods:
            return _int(mods[ability])
        score = (statblock.get("abilities") or {}).get(ability)
        if score is not None:
            return lss_format.modifier(score)
        # Готовый модификатор без кода характеристики.
        if ability == "dex" and statblock.get("dex_mod") is not None:
            return _int(statblock.get("dex_mod"))
    if ability == "dex":
        return _int(getattr(token, "dex", 0) or 0)
    return 0


def token_dex_mod(token, sheet: dict = None) -> int:
    """Модификатор Ловкости для инициативы."""
    return token_ability_mod(token, sheet, "dex")


def parse_speed_ft(value, prefer: str = "ходьба"):
    """Скорость в футах из числа или строки/списка статблока.

    В справочнике скорость хранится списком строк — «ходьба 30 фт»,
    «полёт 60 фт». Для перемещения по клеткам нужна одна цифра, поэтому
    берём наземную скорость, а если её нет — первую доступную.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, (list, tuple)):
        entries = [str(v).strip() for v in value if str(v).strip()]
        if not entries:
            return None
        for entry in entries:
            if prefer in entry.casefold():
                return parse_speed_ft(entry, prefer)
        return parse_speed_ft(entries[0], prefer)
    text = str(value).strip()
    if not text:
        return None
    chunks = [chunk.strip() for chunk in text.replace(";", ",").split(",")
              if chunk.strip()]
    chunk = next((part for part in chunks if prefer in part.casefold()), text)
    digits = "".join(ch if ch.isdigit() else " " for ch in chunk).split()
    if not digits:
        return None
    try:
        return int(digits[0])
    except ValueError:
        return None


def token_speed_ft(token, sheet: dict = None) -> int:
    """Скорость токена в футах: лист игрока, затем статблок, иначе 30.

    30 футов — стандартная скорость существа, поэтому токен без данных
    остаётся играбельным, а не «не может ходить».
    """
    if isinstance(sheet, dict):
        value = ((sheet.get("vitality") or {}).get("speed") or {}).get("value")
        speed = parse_speed_ft(value)
        if speed is not None:
            return max(0, speed)
    block = getattr(token, "statblock", None) or {}
    speed = parse_speed_ft(block.get("speed"))
    if speed is None:
        speed = parse_speed_ft(block.get("speeds"))
    if speed is not None:
        return max(0, speed)
    return 30


def token_condition_immunities(token, sheet: dict = None) -> list:
    """Иммунитеты к состояниям из статблока (в карточке и в правилах)."""
    block = getattr(token, "statblock", None) or {}
    result = []
    for source in (getattr(token, "condition_immunities", None),
                   block.get("condition_immunities")):
        for value in source or []:
            text = str(value).strip()
            if text and text not in result:
                result.append(text)
    return result


def token_ac_bonus(token) -> int:
    """Прибавка к КД от активных эффектов (Щит, доспехи магии).

    Эффекты живут на токене, поэтому КД на сервере и на карточке совпадает:
    без этого Щит тратил ячейку, но попадания по-прежнему проходили.
    """
    total = 0
    for effect in getattr(token, "active_effects", None) or []:
        if not isinstance(effect, dict) or effect.get("type") != "ac":
            continue
        total += _int(effect.get("amount"))
    return total


def token_ac_floor(token, sheet: dict = None) -> int:
    """Нижняя граница КД от эффектов вида «Доспехи мага»: 13 + Ловкость.

    Это не прибавка, а замена базы: в 5e заклинание даёт КД
    «13 + модификатор Ловкости» только пока персонаж без доспехов, поэтому
    поверх лат оно ничего не добавляет — берётся лучшее из двух.
    """
    floor = 0
    for effect in getattr(token, "active_effects", None) or []:
        if not isinstance(effect, dict) or effect.get("type") != "armor":
            continue
        base = _int(effect.get("base")) or 13
        ability = normalize_ability(effect.get("ability")) or "dex"
        floor = max(floor, base + token_ability_mod(token, sheet, ability))
    return floor


def token_ac(token, sheet: dict = None) -> int:
    """КД цели: у токена своё поле, у игрока — с листа, иначе 10.

    Порядок как в 5e: сначала база КД (доспех или «Доспехи мага» — что
    лучше), потом прибавки от эффектов (Щит, Ускорение). Иначе +2 от
    Ускорения терялось бы у цели под Доспехами мага.
    """
    bonus = token_ac_bonus(token)
    floor = token_ac_floor(token, sheet)
    base = None
    ac = getattr(token, "ac", None)
    if ac is not None:
        base = _int(ac)
    if base is None and isinstance(sheet, dict):
        value = ((sheet.get("vitality") or {}).get("ac") or {}).get("value")
        if value is not None:
            base = _int(value)
    statblock = getattr(token, "statblock", None)
    if base is None and statblock and statblock.get("ac") is not None:
        base = _int(statblock.get("ac"))
    if base is None:
        base = 10
    return max(base, floor) + bonus


def token_protection(token, sheet: dict = None) -> dict:
    """Сопротивления, иммунитеты и уязвимости токена одним словарём."""
    statblock = getattr(token, "statblock", None) or {}

    def merged(field, alias):
        """Список без дублей: поле токена и то же поле статблока часто совпадают.

        Раньше значения складывались как есть, и «огонь» приезжал дважды —
        на экране это выглядело как двойное сопротивление.
        """
        values = []
        for source in (getattr(token, field, None), statblock.get(field),
                       statblock.get(alias)):
            for value in (source or []):
                text = str(value).strip()
                if text and text not in values:
                    values.append(text)
        return values

    return {
        "resist": merged("resist", "damage_resistances"),
        "immune": merged("immune", "damage_immunities"),
        "vulnerable": merged("vulnerable", "damage_vulnerabilities"),
    }


def token_conditions(token, sheet: dict = None) -> list:
    """Состояния токена; у персонажа добавляются состояния с листа.

    Мастер ставит «Отравлен» на токен, игрок — на лист; в бою это одно и то
    же, поэтому списки объединяются без дублей.
    """
    names = []
    for name in getattr(token, "conditions", None) or []:
        text = str(name).strip()
        if text and text not in names:
            names.append(text)
    if isinstance(sheet, dict):
        for entry in ((sheet.get("vtt") or {}).get("conditions") or []):
            name = entry.get("name") if isinstance(entry, dict) else entry
            text = str(name or "").strip()
            if text and text not in names:
                names.append(text)
    return names


def statblock_from_record(record: dict) -> dict:
    """Сжать запись справочника до того, что реально нужно в бою.

    Полный статблок весит десятки килобайт (описания, особенности), а в
    токене он едет через сеть при каждом полном состоянии. Оставляем
    характеристики, спасброски и защиту — этого хватает для атаки,
    спасброска и сопротивлений.
    """
    record = record or {}
    return {
        "name": record.get("name") or "",
        "source_name": record.get("source_name") or "",
        # ОП нужен карточке актёра: по нему считается бонус мастерства для
        # спасбросков и атак (CR 10 — +4, CR 5 — +3). Без него карточка
        # показывала +2 всем существам и сохраняла заниженный бонус.
        "cr": record.get("cr"),
        "type": record.get("type") or record.get("ctype") or "",
        "abilities": dict(record.get("abilities") or {}),
        "mods": dict(record.get("mods") or {}),
        "saves": [dict(s) for s in (record.get("saves") or [])
                  if isinstance(s, dict)],
        "ac": record.get("ac"),
        "hp": record.get("hp"),
        "hp_max": record.get("hp_max"),
        # Скорость нужна перемещению по клеткам в бою. Число кладём рядом
        # со строками справочника: карточке проще править одно поле.
        "speeds": [str(s).strip() for s in (record.get("speeds") or [])
                   if str(s).strip()],
        "speed": parse_speed_ft(record.get("speeds")),
        "resist": list(record.get("damage_resistances") or []),
        "immune": list(record.get("damage_immunities") or []),
        "vulnerable": list(record.get("damage_vulnerabilities") or []),
        "condition_immunities": list(record.get("condition_immunities") or []),
        # Атаки существа: без них карточка мастера была пустой в разделе
        # «АТАКИ», а диалог атаки не мог предложить удар из статблока.
        "attacks": [dict(a) for a in (record.get("attacks") or [])
                    if isinstance(a, dict)],
        # Особые атаки (дыхание, проглатывание, ужас): это действия со
        # спасброском цели, а не свойства. Раньше они попадали в карточку
        # кнопкой «Спасбросок» на самом существе — бросок шёл не за того.
        "special_attacks": special_attacks.from_record(record),
        # Чувства («тёмное зрение 90 фт»): из них токен берёт дальность
        # обзора, чтобы дальнозоркое существо видело дальше 60 футов
        # молчаливого умолчания.
        "senses": [str(s).strip() for s in (record.get("senses") or [])
                   if str(s).strip()],
        "traits": list(record.get("traits") or []),
    }
