"""Особые атаки существ: дыхание, проглатывание, ужас и подобное.

В справочнике SRD такие способности лежат в ``features`` вместе с
пассивными чертами (Мультиатака, Сопротивление магии). Отличить их можно
по механике: у особой атаки есть или урон, или спасбросок цели со Сл.
Именно они и попадают в раздел «АТАКИ» карточки существа, потому что
это действие, а не свойство.

Ключевое правило 5e, которое здесь заложено: **спасбросок бросает цель, а
не существо**. Раньше особенность с механикой превращалась в кнопку
«Спасбросок 1d20 против Сл 16» на самой карточке существа, и по кнопке
бросок шёл за монстра. Теперь это особая атака: мастер выбирает цель (или
направление — для линии и конуса), а спасбросок бросает сервер за цель.

Модуль чистый: ничего не знает ни про сеть, ни про Qt. Поэтому числа
здесь проверяются обычными тестами, а сервер, карточка и справочник
считают одинаково.
"""
import logging
import re

from shared import combat as combat_rules
from shared import spell_effects
from shared import token_stats

#: Характеристика спасброска по умолчанию, если в тексте её не разобрать.
DEFAULT_ABILITY = "dex"

#: Области, которые понимает холст и сервер (shared.templates).
AREA_SHAPES = ("line", "cone", "circle", "square")

#: Клавиши, уже занятые горячими клавишами стола (одна латинская буква).
#: Однобуквенный хоткей особой атаки ищется холстом тем же способом, что
#: и остальные клавиши, поэтому занятая буква отнимала бы действие у стола:
#: С вместо рывка как раз выбрала бы дыхание, а не рывок. Скорость
#: проверяется в shared.hotkey_conflict, чтобы карточка и окно считали
#: одинаково.
RESERVED_HOTKEYS = {
    "Q": "быстрая атака",
    "C": "рывок",
    "R": "поворот токена взглядом",
    "M": "линейка",
    "N": "передать ход (ДМ)",
    "X": "удалить источник света",
    "V": "курсор",
    "W": "шаг WASD вверх",
    "A": "шаг WASD влево",
    "S": "шаг WASD вниз",
    "D": "шаг WASD вправо",
    "B": "кисть",
    "L": "линия",
    "O": "круг",
    "F": "фигура",
    "E": "ластик",
    "T": "надпись",
    "H": "рука (перемещение)",
}


def hotkey_conflict(letter: str) -> str:
    """Почему клавишу нельзя отдать особой атаке. Пустая строка — можно.

    Функция для карточки: там мастер вводит клавишу руками и должен
    сразу увидеть, что C — рывок, а не его дыхание.
    """
    letter = str(letter or "").strip().upper()
    if not letter:
        return ""
    if len(letter) > 1:
        return "нужна одна клавиша без Ctrl/Alt/Shift"
    if letter in RESERVED_HOTKEYS:
        return f"{letter} занята: {RESERVED_HOTKEYS[letter]}"
    if not ("A" <= letter <= "Z" or "0" <= letter <= "9"):
        return f"{letter} не подходит: нужна одна латинская буква или цифра"
    return ""

#: «20-футовой линией», «30-футовый конус», «15-футовый куб» — размер
#: впереди формы: так пишет большинство дыханий в SRD.
_ADJ_RE = re.compile(
    r"(\d+)\s*(?:-|\s)?футов\w*\s+(лини\w+|конус\w*|куб\w*)",
    re.IGNORECASE)
#: Форма впереди размера: «линия длиной 20 футов», «конусом 30 футов»,
#: «линия 100 футов» (у Фаербола и подобных).
_SHAPE_FIRST_RE = re.compile(
    r"(лини\w+|конус\w*|куб\w*)\D{0,25}?(\d+)\s*(?:-|\s)?фут\w*",
    re.IGNORECASE)
#: Ширина линии: «шириной 5 футов».
_WIDTH_RE = re.compile(r"ширин\w*\D{0,8}?(\d+)\s*фут", re.IGNORECASE)
#: Радиус сферы: «радиусом 20 футов».
_RADIUS_RE = re.compile(r"радиус\w*\D{0,14}?(\d+)\s*фут", re.IGNORECASE)
#: Область вокруг самого существа: «все существа в пределах 30 футов от
#: него» (предсмертная агония балора, атака крыльями дракона).
_AROUND_RE = re.compile(
    r"в пределах\s*(\d+)\s*фут\w*\s+от\s+[а-яёa-z]+", re.IGNORECASE)
#: Дальность по одной цели: «в пределах 60 футов», «до 30 футов».
_RANGE_RE = re.compile(r"(?:в пределах|до|на расстоянии)\D{0,8}?(\d+)\s*фут",
                       re.IGNORECASE)
#: Английские формы: «a 60-foot cone», «120-foot line».
_ADJ_EN_RE = re.compile(
    r"(\d+)\s*(?:-|\s)?foot\s+(line|cone|cube)", re.IGNORECASE)
_SHAPE_EN_RE = re.compile(
    r"(line|cone|cube)\D{0,25}?(\d+)\s*(?:-|\s)?foot", re.IGNORECASE)

#: «спасбросок Мудрости Сл 19», «спасброске Ловкости Сл 22» — как пишет SRD.
_SAVE_RE = re.compile(
    r"спасброс\w*\s+(Силы|Ловкости|Телосложения|Интеллекта|Мудрости|Харизмы)"
    r"\s*\(?\s*(?:Сл|DC)\s*(\d+)",
    re.IGNORECASE)
#: Тот же спасбросок без Сл: характеристика известна, число нет.
_SAVE_ONLY_RE = re.compile(
    r"спасброс\w*\s+(Силы|Ловкости|Телосложения|Интеллекта|Мудрости|Харизмы)",
    re.IGNORECASE)
_EN_SAVE_RE = re.compile(
    r"DC\s*(\d+)\s*(Strength|Dexterity|Constitution|Intelligence|Wisdom|"
    r"Charisma)",
    re.IGNORECASE)
#: Формула урона в тексте: «получая 66 (12к10) урона электричеством».
_TEXT_DAMAGE_RE = re.compile(
    r"\d+\s*\((\d*\s*[кdКD]\s*\d+(?:\s*[+–-]\s*\d+)?)\)")
#: Формула рядом со словом «урон»: «наносит 4к6 урона», «урон 2к8 огнём».
#: Без этой привязки формула ловилась из чужого текста: у призыва балора
#: «1к8 вроков» становилось уроном 1d8.
_DAMAGE_WORD_RE = re.compile(
    r"урон\w*[^.;]{0,25}?(\d*\s*[кdКD]\s*\d+(?:\s*[+–-]\s*\d+)?)"
    r"|(\d*\s*[кdКD]\s*\d+(?:\s*[+–-]\s*\d+)?)[^.;]{0,25}?урон\w*",
    re.IGNORECASE,
)

_EN_ABILITIES = {
    "strength": "str", "dexterity": "dex", "constitution": "con",
    "intelligence": "int", "wisdom": "wis", "charisma": "cha",
}

#: В русских правилах спасбросок пишут в родительном падеже («спасбросок
#: Ловкости»), а `token_stats.normalize_ability` знает только именительный.
#: Без этой таблицы характеристика терялась, и спасбросок дыхания дракона
#: уезжал в Ловкость по умолчанию.
_ABILITY_FORMS = {
    "силы": "str", "ловкости": "dex", "телосложения": "con",
    "интеллекта": "int", "мудрости": "wis", "харизмы": "cha",
}


def _ability_key(value) -> str:
    """Ключ характеристики из любого написания (в том числе родительного)."""
    text = _text(value).casefold()
    if not text:
        return ""
    return _ABILITY_FORMS.get(text) or token_stats.normalize_ability(text)

#: «перезарядка 5–6» / «(перезарядка 5-6)» — дыхание драконов.
_RECHARGE_RE = re.compile(r"перезарядк\w*\D{0,6}(\d)\s*[-–—]\s*(\d)",
                          re.IGNORECASE)

#: Слова, по которым видно, что при успехе урон делится пополам.
_HALF_MARKERS = ("половину этого урона", "половину урона", "половина урона",
                 "half as much", "half the damage")

#: Сколько уникальных особых атак держим в статблоке: больше в карточке
#: не читается, а токен со статблоком едет по сети целиком.
MAX_ATTACKS = 24


def _text(value) -> str:
    if isinstance(value, dict):
        value = value.get("value")
    if value is None:
        return ""
    return str(value).strip()


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


#: Слово формы из текста -> форма для холста.
_SHAPE_WORDS = {
    "лини": "line", "line": "line",
    "конус": "cone", "cone": "cone",
    "куб": "square", "cube": "square",
}


def _shape_word(value) -> str:
    word = _text(value).casefold()
    for stem, shape in _SHAPE_WORDS.items():
        if word.startswith(stem):
            return shape
    return ""


def parse_area(text) -> dict:
    """Область действия из человеческого описания способности.

    Возвращает ``{'shape', 'size_ft'(, 'width_ft')}`` или пустой словарь,
    если по тексту формы не видно: способность тогда бьёт по одной цели.

    Разбираем три написания из SRD: «120-футовой линией», «линия длиной
    20 футов» и «выдыхает молнию 20 футов и шириной 5 футов». Размер
    ищется рядом со словом формы, а не как первое число в тексте: иначе
    ширина линии попадала бы в длину.
    """
    body = _text(text)
    if not body:
        return {}
    low = body.casefold()

    def size_near(word) -> int:
        """Размер, стоящий вплотную к слову формы (до или после него)."""
        for match in _ADJ_RE.finditer(body):
            if _shape_word(match.group(2)) == word:
                return _int(match.group(1), 0) or 0
        for match in _ADJ_EN_RE.finditer(body):
            if _shape_word(match.group(2)) == word:
                return _int(match.group(1), 0) or 0
        for match in _SHAPE_FIRST_RE.finditer(body):
            if _shape_word(match.group(1)) == word:
                return _int(match.group(2), 0) or 0
        for match in _SHAPE_EN_RE.finditer(body):
            if _shape_word(match.group(1)) == word:
                return _int(match.group(2), 0) or 0
        return 0

    for word in ("лини", "line", "конус", "cone", "куб", "cube"):
        if word not in low:
            continue
        shape = _shape_word(word)
        size = size_near(shape)
        if not size:
            # «выдыхает молнию длиной 20 футов и шириной 5 футов»: размер
            # идёт от слова «длин», но обозначает всё ту же линию.
            size = _first_feet(body)
        if size < 5:
            continue
        out = {"shape": shape, "size_ft": size}
        if shape == "line":
            match = _WIDTH_RE.search(body)
            out["width_ft"] = max(5, _int(match.group(1),
                                         0) or 0) if match else 5
        return out
    match = _RADIUS_RE.search(body)
    if match:
        size = _int(match.group(1), 0) or 0
        if size >= 5:
            return {"shape": "circle", "size_ft": size}
    # «все существа в пределах 30 футов от него» — это не радиус заклинания,
    # а область вокруг самого существа. До 30 футов так и есть; у большего
    # числа («в пределах 120 футов от него» у Ужасающего присутствия)
    # область не круг, а дальность выбора цели.
    match = _AROUND_RE.search(body)
    if match:
        size = _int(match.group(1), 0) or 0
        if 5 <= size <= 30:
            return {"shape": "circle", "size_ft": size, "anchor": "self"}
    return {}


def _first_feet(text) -> int:
    """Первое «N футов» в тексте — запасной способ узнать размер.

    Дефисное написание («20-футовой») тоже считается: пробел между
    числом и словом не пропускал бы дефис.
    """
    match = re.search(r"(\d+)\s*(?:-|\s)?фут", text or "", re.IGNORECASE)
    if not match:
        return 0
    return _int(match.group(1), 0) or 0


def _range_ft(text) -> int:
    """Дальность способности по одной цели из текста (0 — не сказано)."""
    match = _RANGE_RE.search(_text(text))
    if not match:
        return 0
    return _int(match.group(1), 0) or 0


def recharge_range(text) -> str:
    """«5–6» из текста про перезарядку дыхания или пустая строка."""
    match = _RECHARGE_RE.search(_text(text))
    return f"{match.group(1)}–{match.group(2)}" if match else ""


def halves_on_success(text, damage: str = "") -> bool:
    """Делится ли урон пополам при успешном спасброске.

    Правило урона по области в 5e: при успехе — половина. В тексте это
    обычно написано прямо, но у дыхания дракона фраза бывает в конце
    абзаца, поэтому «половину» ищем по всему описанию.
    """
    low = f"{_text(text)} {_text(damage)}".casefold()
    return any(marker in low for marker in _HALF_MARKERS)


def save_from_text(text) -> tuple:
    """Характеристика и Сл спасброска из описания: ``('wis', 19)``.

    Текст важнее механики записи: при импорте SRD в `mechanics` у дыхания
    дракона и Ужасающего присутствия оказывался один и тот же спасбросок
    Ловкости с чужим Сл. В описании при этом написано верно: «спасбросок
    Мудрости Сл 19».
    """
    body = _text(text)
    if not body:
        return "", None
    match = _SAVE_RE.search(body)
    if match:
        return _ability_key(match.group(1)), _int(match.group(2))
    match = _EN_SAVE_RE.search(body)
    if match:
        return (_EN_ABILITIES.get(match.group(2).casefold(), ""),
                _int(match.group(1)))
    match = _SAVE_ONLY_RE.search(body)
    if match:
        return _ability_key(match.group(1)), None
    return "", None


def damage_from_text(text) -> str:
    """Формула урона из описания, если она там написана.

    Раньше цифры брались только из `mechanics.damage`, а там у Балора
    стояла аура («3d6 огонь; 3d6 огонь») вместо взрыва при смерти, у
    дракона — урон хвоста вместо дыхания. Описание надёжнее: «получая
    70 (20к6) урона огнём».
    """
    body = _text(text)
    if not body:
        return ""
    match = _TEXT_DAMAGE_RE.search(body)
    if match:
        return combat_rules.clean_damage_formula(match.group(1))
    match = _DAMAGE_WORD_RE.search(body)
    if match:
        found = match.group(1) or match.group(2) or ""
        return combat_rules.clean_damage_formula(found)
    return ""


def is_special_attack(feature: dict) -> bool:
    """Особая атака или пассивная черта.

    Особая атака — то, что требует броска по цели: в описании есть
    спасбросок или конкретный урон. Решает **описание**, а не механика
    записи: при импорте SRD в `mechanics` соседних особенностей часто
    лежат чужие числа (у слабостей вампира — «+20 кислота» от разложения
    родительских пунктов, у призыва балора — урон предсмертной агонии).
    Мультиатака и свойства вроде «Сопротивление магии» остаются
    подписями в разделе заметок и кнопок атаки не получают.
    """
    if not isinstance(feature, dict):
        return False
    text = feature.get("text")
    ability, dc = save_from_text(text)
    mech = feature.get("mechanics")
    mech = mech if isinstance(mech, dict) else {}
    if ability:
        # Спасбросок без Сл — не действие, а упоминание броска (побег из
        # захвата у Перевёртыша вампира): бросать не против чего.
        if dc is not None or _mech_dc(mech) is not None:
            return True
    # Урон из механики годится только тогда, когда в описании он тоже есть:
    # «20d6 огонь» без строки про урон — это чужая механика.
    if damage_from_text(text):
        return True
    return False


def _mech_dc(mech) -> int:
    """Первый Сл из механики записи (или None)."""
    if not isinstance(mech, dict):
        return None
    for save in mech.get("saves") or []:
        if not isinstance(save, dict):
            continue
        value = _int(save.get("dc"))
        if value is not None:
            return value
    return None


def from_feature(feature: dict, index: int = 0) -> dict:
    """Собрать особую атаку из особенности справочника.

    Возвращает словарь с готовыми числами: характеристику и Сл спасброска,
    формулу и тип урона, область. Пустое имя заменяется на «Особая атака»,
    поэтому карточка никогда не показывает пустую кнопку.
    """
    feature = feature if isinstance(feature, dict) else {}
    mech = feature.get("mechanics") if isinstance(feature.get("mechanics"),
                                                 dict) else {}
    text = _text(feature.get("text"))
    name = _text(feature.get("name")) or "Особая атака"

    # Спасбросок цели: сначала описание, потом механика записи.
    ability, dc = save_from_text(text)
    action_without_damage = bool(ability) and not damage_from_text(text)
    if not ability or dc is None:
        for save in mech.get("saves") or []:
            if not isinstance(save, dict):
                continue
            key = _ability_key(save.get("ability"))
            value = _int(save.get("dc"))
            if key and not ability:
                ability = key
            if value is not None and dc is None:
                dc = value
            if ability and dc is not None:
                break

    # Урон тоже сначала из описания: в механике он часто от другого действия.
    formula = damage_from_text(text)
    raw_damage = _text(mech.get("damage"))
    if not formula and not action_without_damage:
        formula = combat_rules.clean_damage_formula(raw_damage)
    damage_type = (combat_rules.damage_type_from_text(formula)
                   or combat_rules.damage_type_from_text(text)
                   or combat_rules.damage_type_from_text(raw_damage))
    area = parse_area(text) or parse_area(feature.get("source_name"))
    activation = _text(feature.get("activation"))

    entry = {
        "id": f"sa:{name.casefold()}",
        "name": name,
        "source_name": _text(feature.get("source_name")),
        "activation": activation,
        "ability": ability or (DEFAULT_ABILITY if dc is not None else ""),
        "dc": dc,
        "damage": formula,
        "damage_type": damage_type,
        "healing": combat_rules.clean_damage_formula(_text(mech.get("healing"))),
        "half_on_success": bool(formula) and dc is not None
        and halves_on_success(text, formula),
        "recharge": recharge_range(f"{activation} {name} {text}"),
        "text": text,
        "hotkey": "",
    }
    entry.update(area)
    entry["anchor"] = _text(area.get("anchor")) or "aim"
    shape = entry.get("shape") or ""
    if shape in ("cone", "line"):
        # Конус и линия по правилам начинаются в клетке существа, поэтому
        # дистанция такой атаки равна её длине.
        entry["range_ft"] = int(entry.get("size_ft") or 0)
    else:
        # Одна цель: дальность берём из текста («в пределах 30 футов»),
        # иначе 30 футов — обычная дистанция касания/выдоха по одной цели.
        entry["range_ft"] = _range_ft(text) or 30
    entry["id"] = f"sa:{name.casefold()}|{index}" if index else entry["id"]
    return entry


def from_record(record: dict) -> list:
    """Все особые атаки записи справочника (без дублей по имени).

    Одноимённые особенности встречаются у драконов: у них отдельными
    пунктами идут «Оружия дыхания (перезарядка 5–6)» и её разновидности.
    Оставляем первую — правки мастера в карточке важнее дублей.
    """
    record = record if isinstance(record, dict) else {}
    out = []
    seen = set()
    for feature in record.get("features") or []:
        if not is_special_attack(feature):
            continue
        try:
            entry = from_feature(feature, 0)
        except Exception:
            logging.exception("Особая атака не разобрана")
            continue
        key = entry["name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
        if len(out) >= MAX_ATTACKS:
            break
    return out


def normalize(entry: dict, index: int = 0) -> dict:
    """Привести запись статблока к известному виду (правки мастера целы).

    Карточка актёра хранит особые атаки в статблоке токена и пишет туда
    свои поля: Сл, формулу, тип урона и назначенную клавишу. Функция
    чистит мусор из старых миров, но не выкидывает саму атаку — иначе
    правки мастера пропадали бы при перезапуске.
    """
    entry = entry if isinstance(entry, dict) else {}
    name = _text(entry.get("name")) or "Особая атака"
    ability = token_stats.normalize_ability(entry.get("ability"))
    raw_damage = _text(entry.get("damage"))
    formula = combat_rules.clean_damage_formula(raw_damage) or ""
    damage_type = (combat_rules.normalize_damage_type(entry.get("damage_type"))
                   or combat_rules.damage_type_from_text(raw_damage))
    shape = _text(entry.get("shape")).casefold()
    if shape not in AREA_SHAPES:
        shape = ""
    out = {
        "id": _text(entry.get("id")) or f"sa:{name.casefold()}|{index}",
        "name": name,
        "source_name": _text(entry.get("source_name")),
        "activation": _text(entry.get("activation")),
        "ability": ability,
        "dc": _int(entry.get("dc")),
        "damage": formula,
        "damage_type": damage_type,
        "healing": combat_rules.clean_damage_formula(_text(entry.get("healing"))),
        "half_on_success": bool(entry.get("half_on_success")),
        "recharge": _text(entry.get("recharge")),
        "text": _text(entry.get("text")),
        "hotkey": _text(entry.get("hotkey")),
        "range_ft": _int(entry.get("range_ft"), 0) or 0,
        "anchor": (_text(entry.get("anchor")).casefold()
                   if _text(entry.get("anchor")).casefold() == "self"
                   else "aim"),
    }
    if shape:
        out["shape"] = shape
        out["size_ft"] = max(5, _int(entry.get("size_ft"), 0) or 5)
        if shape == "line":
            out["width_ft"] = max(5, _int(entry.get("width_ft"), 0) or 5)
    return out


def has_attacks(statblock) -> bool:
    """Есть ли у статблока особые атаки (быстрая проверка без разбора)."""
    block = statblock if isinstance(statblock, dict) else {}
    return any(isinstance(entry, dict)
               for entry in (block.get("special_attacks") or []))


def find(statblock: dict, ability_id) -> dict:
    """Найти особую атаку в статблоке по id (или по имени)."""
    wanted = _text(ability_id).casefold()
    if not wanted:
        return {}
    block = statblock if isinstance(statblock, dict) else {}
    for entry in block.get("special_attacks") or []:
        if not isinstance(entry, dict):
            continue
        if _text(entry.get("id")).casefold() == wanted:
            return normalize(entry)
        if _text(entry.get("name")).casefold() == wanted:
            return normalize(entry)
    return {}


def ability_label(entry: dict) -> str:
    """«Ловкости» для подписи и лога: спасбросок какой характеристики."""
    key = token_stats.normalize_ability((entry or {}).get("ability"))
    return {"str": "Силы", "dex": "Ловкости", "con": "Телосложения",
            "int": "Интеллекта", "wis": "Мудрости",
            "cha": "Харизмы"}.get(key, "")


def shape_label(entry: dict) -> str:
    """Словами, куда бьёт способность: «линия 20×5 футов», «одна цель»."""
    entry = entry or {}
    shape = _text(entry.get("shape"))
    size = _int(entry.get("size_ft"), 0) or 0
    if shape == "line":
        width = _int(entry.get("width_ft"), 0) or 5
        return f"линия {size}×{width} футов"
    if shape == "cone":
        return f"конус {size} футов"
    if shape == "circle":
        if _text(entry.get("anchor")).casefold() == "self":
            return f"радиус {size} футов вокруг существа"
        return f"сфера радиусом {size} футов"
    if shape == "square":
        return f"куб {size} футов"
    return "одна цель"


def summary(entry: dict) -> str:
    """Короткая подпись кнопки: «спасбросок Ловкости Сл 16 · 12к10 молния»."""
    entry = entry or {}
    bits = []
    ability = ability_label(entry)
    dc = _int(entry.get("dc"))
    if ability or dc is not None:
        text = "спасбросок"
        if ability:
            text += f" {ability}"
        if dc is not None:
            text += f" Сл {dc}"
        bits.append(text)
    if entry.get("damage"):
        damage = combat_rules.damage_type_label(entry.get("damage_type"))
        bits.append(f"{entry['damage']}" + (f" {damage}" if damage else ""))
    if entry.get("healing"):
        bits.append(f"лечение {entry['healing']}")
    bits.append(shape_label(entry))
    if entry.get("recharge"):
        bits.append(f"перезарядка {entry['recharge']}")
    return " · ".join(bits)


#: Ключ отклика (звук + анимация) по типу урона. Звуки лежат в
#: client/assets/sounds/spells/<ключ>.wav, анимацию рисует холст.
_FX_BY_DAMAGE = {
    "lightning": "lightning_breath",
    "fire": "fire_breath",
    "cold": "cold_breath",
    "acid": "acid_breath",
    "poison": "poison_breath",
    "thunder": "thunder_breath",
    "necrotic": "necrotic_breath",
}
#: Ключ по названию способности: у проглатывания и ужаса нет своего типа
#: урона, а звук и анимация им нужны отдельные.
_FX_BY_NAME = {
    "проглатыван": "swallow",
    "ужасающ": "frightful_presence",
    "ужас": "frightful_presence",
    "атака крыльями": "wing_attack",
    "хвост": "tail_attack",
}


def fx_key(entry: dict) -> str:
    """Ключ отклика особой атаки: у каждой — свой звук и своя анимация.

    Сначала смотрим на название (проглатывание и ужас не наносят урона
    обычного типа), потом на тип урона. Неизвестная атака получает общий
    ключ ``special_attack`` — лучше общий отклик, чем тишина.
    """
    entry = entry or {}
    name = _text(entry.get("name")).casefold()
    for marker, key in _FX_BY_NAME.items():
        if marker in name:
            return key
    dtype = combat_rules.normalize_damage_type(entry.get("damage_type"))
    return _FX_BY_DAMAGE.get(dtype, "special_attack")


def roll_text(caster_name: str, entry: dict, rows: list,
              damage: dict = None) -> str:
    """Строка в чат: кто применил, куда и что вышло у каждой цели.

    Один бросок урона на всю область — как в 5e: дыхание дракона бросается
    один раз, а не отдельно на каждого, кто попал под линию.
    """
    entry = entry or {}
    parts = [f"☄ {caster_name} — {entry.get('name') or 'Особая атака'}"
             f" ({shape_label(entry)})"]
    damage = damage or {}
    if damage.get("formula"):
        shown = str(damage.get("formula"))
        if damage.get("total") is not None:
            shown += f" = {damage['total']}"
        # Разбор костей: 12к10 на всю область бросается один раз, и по
        # одному итогу нельзя проверить, что выпало.
        if damage.get("breakdown"):
            shown += f" [{damage['breakdown']}]"
        parts.append(f"урон {shown}")
    for row in rows or []:
        name = str(row.get("name") or "?")
        amount = _int(row.get("amount"), 0) or 0
        save = row.get("roll") if isinstance(row.get("roll"), dict) else {}
        if save and save.get("kept") is not None:
            # Показываем сам бросок: за столом важно видеть, что спасбросок
            # действительно бросался, а не «сервер решил».
            roll = (f"спасбросок {combat_rules.d20_text(save)}")
            if row.get("dc") is not None:
                roll += f" против Сл {row['dc']}"
            roll += ", успех" if row.get("save_success") else ", провал"
        elif row.get("save_success") is None:
            roll = "без спасброска"
        else:
            roll = ("спасбросок пройден" if row.get("save_success")
                    else "провалил")
        if amount:
            parts.append(f"{name}: {amount} ({roll})")
        else:
            parts.append(f"{name}: {roll}")
        if row.get("note"):
            parts.append(str(row["note"]))
    return "; ".join(parts)


def shape_cells(entry: dict, origin: tuple, aim: tuple, cols: int,
                rows: int) -> list:
    """Клетки области особой атаки — та же геометрия, что у заклинаний.

    Линия и конус начинаются в клетке существа (origin) и смотрят на
    выбранную цель (aim); круг и куб кладутся в точку цели.
    """
    from shared import templates as aoe_templates

    entry = entry or {}
    shape = _text(entry.get("shape"))
    if shape not in AREA_SHAPES:
        return []
    origin_col, origin_row = int(origin[0]), int(origin[1])
    aim_col, aim_row = int(aim[0]), int(aim[1])
    size_ft = _int(entry.get("size_ft"), 0) or 0
    if shape in ("cone", "line"):
        base_col, base_row = origin_col, origin_row
        direction = (aim_col - origin_col, aim_row - origin_row)
        if direction == (0, 0):
            direction = (1, 0)
    elif _text(entry.get("anchor")).casefold() == "self":
        # Область вокруг самого существа: куда бы ни кликнул мастер,
        # центр — клетка существа (крылья дракона, агония балора).
        base_col, base_row = origin_col, origin_row
        direction = (1, 0)
    else:
        base_col, base_row = aim_col, aim_row
        direction = (1, 0)
    return aoe_templates.shape_cells(shape, base_col, base_row, size_ft,
                                     direction[0], direction[1], cols, rows)


def for_display(record: dict) -> list:
    """Особые атаки для карточки справочника: с готовыми бросками урона."""
    out = []
    for entry in from_record(record):
        out.append({"title": entry["name"], "subtitle": summary(entry),
                    "tooltip": entry.get("text") or summary(entry),
                    "rolls": [("Урон", entry["damage"], None)]
                    if entry.get("damage") else [],
                    "entry": entry})
    return out


def area_from_spell_text(text) -> dict:
    """Запасной разбор формы через shared.spell_effects.

    Тот модуль знает ключевые заклинания по source_name; для способностей
    существ он полезен только как вторая попытка — если свой разбор не
    нашёл форму, но текст похож на описание области.
    """
    spec = spell_effects.area_for_spell({"description": text})
    if not spec:
        return {}
    out = {"shape": spec.get("shape"), "size_ft": spec.get("size_ft")}
    if spec.get("width_ft"):
        out["width_ft"] = spec["width_ft"]
    return out
